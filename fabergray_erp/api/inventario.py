# -*- coding: utf-8 -*-
"""Commit 22.4 -- read-only API layer for Page Inventario, used by
Bodega/Jefe de Bodega/System Manager. This commit's own setup: 9 Custom
DocPerm read grants on Item/Bin/Item Price/Stock Ledger Entry -- versioned
in fixtures/custom_docperm.json + fixtures/system_manager_custom_docperm.json
(a separate file, deliberately: filtering the main export by
role="System Manager" alone also matched unrelated Custom DocPerm rows
from other ERPNext localizations that have nothing to do with this app).
Every grant was demonstrated missing via a real has_permission() audit
under real restricted sessions before being created -- none assumed.

Commit 22.6 -- editable inventory, added on top without touching a single
line of the three Commit 22.4 read endpoints below. Bodega stays strictly
read-only (confirmed by this commit's own permission tests); only Jefe de
Bodega/System Manager/Administrator can call the three write endpoints
(record_opening_count/adjust_item_quantity/update_item_master), enforced
server-side via real frappe.has_permission()/doc.insert()/doc.submit()
checks against this commit's own Custom DocPerm grants -- never a role-name
string check. Every quantity change goes exclusively through a native
Stock Reconciliation (Purpose "Opening Stock" for a never-before-initialized
Item+Warehouse, "Stock Reconciliation" for every adjustment after that) --
this module never writes Bin.actual_qty, a Stock Ledger Entry, or a GL Entry
directly. adjust_item_quantity() never sets valuation_rate explicitly --
Stock Reconciliation's own remove_items_with_no_change() resolves it from
the real ledger automatically once one exists, preserving the current
valuation unchanged. record_opening_count() DOES resolve and set
valuation_rate explicitly (purchase_rate given -> existing Standard Buying
-> Item.valuation_rate, see _resolve_opening_valuation_rate()) -- verified
empirically that Stock Reconciliation's own native "fall back to Standard
Buying" logic never actually engages for a first-ever count (its own
remove_items_with_no_change() sets a real 0.0 valuation first, before that
fallback would even run). Existencia from the Access Excel is still out of
scope entirely -- every Item starts at 0 and is counted physically through this
module.

Same conventions as api/clientes.py/ventas.py/bodega.py: every read/write
goes through frappe.get_list()/frappe.get_doc()+check_permission()/
frappe.has_permission(throw=True), never frappe.get_all() (bypasses
permissions), never ignore_permissions=True, never frappe.set_user()
outside of tests, never frappe.db.sql()/frappe.db.commit() (a request's
own commit/rollback boundary is what makes each write endpoint atomic --
see record_opening_count()'s own docstring).

Stock is always aggregated with ONE grouped frappe.get_list() query over
Bin (_bin_totals() below), never one query per Item -- the same "bulk
read, never N+1" rule api/bodega.py's own get_inventory() already
follows for its own Bin reads.

low_stock is deliberately null (with an explicit low_stock_status =
"not_configured") -- no reorder level exists yet for any migrated Item
(Stock Minimo/Maximo from Access were never imported), so there is no
threshold to compute this from that would not be invented.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext import get_default_company
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import get_difference_account

from fabergray_erp.api.bodega import _require_login

PRICE_LIST = "Standard Selling"
PRICE_LIST_BUYING = "Standard Buying"

LOW_STOCK_STATUS = "not_configured"

_ITEM_LIST_FIELDS = ["item_code", "item_name", "item_group", "stock_uom", "disabled"]

_STATUS_FILTERS = {
    "all": {},
    "active": {"disabled": 0},
    "disabled": {"disabled": 1},
}


# -- Commit 22.6: write-endpoint exceptions -----------------------------
# Distinct, named subclasses of frappe.ValidationError -- same convention
# ERPNext's own Stock Reconciliation uses (OpeningEntryAccountError,
# EmptyStockReconciliationItemsError) -- so a test (or a future caller)
# can assertRaises() the exact functional reason, not just "something
# failed", and so a translated message never has to be pattern-matched.
class AdjustmentReasonRequiredError(frappe.ValidationError):
    pass


class StaleInventoryStateError(frappe.ValidationError):
    pass


class OpeningStockAlreadyDoneError(frappe.ValidationError):
    pass


class OpeningStockRequiredError(frappe.ValidationError):
    pass


class NonZeroOpeningStockError(frappe.ValidationError):
    pass


class MissingOpeningAccountError(frappe.ValidationError):
    pass


class PurchaseRateRequiredForOpeningError(frappe.ValidationError):
    pass


def _bin_totals():
    """One grouped query -- {item_code: total_actual_qty} across every
    warehouse for every item that has at least one Bin row. Never one
    query per Item."""
    rows = frappe.get_list(
        "Bin",
        fields=["item_code", {"SUM": "actual_qty", "as": "total_qty"}],
        group_by="item_code",
        limit_page_length=0,
    )
    return {r.item_code: flt(r.total_qty) for r in rows}


def _selling_rates(item_codes):
    """One bulk query for exactly the item_codes on the current page --
    never one Item Price lookup per row."""
    if not item_codes:
        return {}
    rows = frappe.get_list(
        "Item Price",
        filters={"price_list": PRICE_LIST, "item_code": ["in", item_codes]},
        fields=["item_code", "price_list_rate"],
    )
    return {r.item_code: r.price_list_rate for r in rows}


@frappe.whitelist()
def get_inventory_summary():
    """KPI counts for the future Page Inventario dashboard header.

    low_stock is null + low_stock_status="not_configured" -- approved
    explicitly: no reliable reorder level exists for any migrated Item
    yet, and no threshold is invented here to fill that gap."""
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    references = frappe.get_list("Item", pluck="name")
    totals = _bin_totals()
    total_stock = sum(totals.values())

    stock_items = frappe.get_list("Item", filters={"is_stock_item": 1}, pluck="name")
    out_of_stock = sum(1 for code in stock_items if totals.get(code, 0) <= 0)

    return {
        "references": len(references),
        "total_stock": total_stock,
        "out_of_stock": out_of_stock,
        "low_stock": None,
        "low_stock_status": LOW_STOCK_STATUS,
    }


@frappe.whitelist()
def get_inventory_items(txt=None, status=None, start=0, page_length=20):
    """List + search, combined (same shape as api/clientes.py's own
    search_customers()): txt="" for the plain paginated list, txt set for
    search-as-you-type.

    status: "all" | "active" | "disabled" | "out_of_stock" -- the first
    three are plain Item.disabled filters, pushed to the database and
    paginated there. "out_of_stock" has no native Item field to filter
    on (it is derived from the Bin aggregate), so -- exactly like
    api/clientes.py's own "incomplete" tab -- it fetches every matching
    Item once (still ONE query, never per-item), filters in Python
    against the same single Bin aggregate _bin_totals() already
    computed, and paginates the filtered list in Python. An unrecognized
    status falls back to "all"."""
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    txt = txt or ""
    status = status or "all"
    start = cint(start)
    page_length = cint(page_length) or 20

    totals = _bin_totals()

    or_filters = None
    if txt:
        or_filters = [["item_code", "like", f"%{txt}%"], ["item_name", "like", f"%{txt}%"]]

    if status == "out_of_stock":
        all_matching = frappe.get_list(
            "Item",
            or_filters=or_filters,
            fields=_ITEM_LIST_FIELDS,
            order_by="item_code asc",
            limit_page_length=0,
        )
        rows = [i for i in all_matching if totals.get(i.item_code, 0) <= 0]
        total = len(rows)
        page_rows = rows[start : start + page_length]
    else:
        filters = dict(_STATUS_FILTERS.get(status, {}))
        page_rows = frappe.get_list(
            "Item",
            filters=filters,
            or_filters=or_filters,
            fields=_ITEM_LIST_FIELDS,
            order_by="item_code asc",
            limit_start=start,
            limit_page_length=page_length,
        )
        total = len(frappe.get_list("Item", filters=filters, or_filters=or_filters, pluck="name"))

    codes = [r.item_code for r in page_rows]
    rates = _selling_rates(codes)

    items = [
        {
            "item_code": r.item_code,
            "item_name": r.item_name,
            "item_group": r.item_group,
            "stock_uom": r.stock_uom,
            "disabled": r.disabled,
            "total_actual_qty": totals.get(r.item_code, 0),
            "selling_rate": rates.get(r.item_code),
        }
        for r in page_rows
    ]

    return {"items": items, "total": total}


@frappe.whitelist()
def get_inventory_item_detail(item_code, warehouse=None):
    """Full read-only detail for one Item: header, Standard Selling
    price, total stock, per-warehouse breakdown (Bin rows), and the 20
    most recent Stock Ledger Entry movements -- via frappe.get_doc() +
    check_permission("read") for the header (real record-level
    permission), frappe.get_list() for everything else (bulk, permission-
    aware reads, never a raw SQL join).

    warehouse (Commit 22.6, optional, additive -- every existing caller
    that omits it keeps getting exactly the same response as before):
    when given, also returns warehouse_current_qty/has_opening_stock for
    that exact Item+Warehouse pair -- the UI's own signal for whether to
    offer "Registrar inventario inicial" or "Ajustar inventario" for that
    warehouse (see _has_opening_stock()'s docstring for why current_qty
    alone, e.g. ==0, is never used for this). Deliberately NOT assumed
    from stock_by_warehouse (a Warehouse with no Bin row yet -- the exact
    case a first-ever count walks into -- would never appear there)."""
    _require_login()

    doc = frappe.get_doc("Item", item_code)
    doc.check_permission("read")

    bin_rows = frappe.get_list(
        "Bin",
        filters={"item_code": item_code},
        fields=["warehouse", "actual_qty", "reserved_qty", "projected_qty"],
        order_by="warehouse asc",
    )
    total_stock = sum(flt(r.actual_qty) for r in bin_rows)

    price_rows = frappe.get_list(
        "Item Price",
        filters={"item_code": item_code, "price_list": PRICE_LIST},
        fields=["price_list_rate", "currency"],
        limit_page_length=1,
    )
    selling_rate = price_rows[0].price_list_rate if price_rows else None
    currency = price_rows[0].currency if price_rows else None

    recent_movements = frappe.get_list(
        "Stock Ledger Entry",
        filters={"item_code": item_code},
        fields=[
            "posting_date",
            "posting_time",
            "warehouse",
            "actual_qty",
            "qty_after_transaction",
            "voucher_type",
            "voucher_no",
        ],
        order_by="posting_date desc, posting_time desc, creation desc",
        limit_page_length=20,
    )

    result = {
        "item_code": doc.item_code,
        "item_name": doc.item_name,
        "item_group": doc.item_group,
        "stock_uom": doc.stock_uom,
        "disabled": doc.disabled,
        "is_stock_item": doc.is_stock_item,
        "selling_rate": selling_rate,
        "currency": currency,
        "total_stock": total_stock,
        "stock_by_warehouse": bin_rows,
        "recent_movements": recent_movements,
    }

    if warehouse:
        result["warehouse"] = warehouse
        result["warehouse_current_qty"] = _get_current_qty(item_code, warehouse)
        result["has_opening_stock"] = _has_opening_stock(item_code, warehouse)

    return result


# =========================================================================
# Commit 22.6 -- editable inventory (write endpoints)
# =========================================================================
#
# Three endpoints, three distinct jobs -- deliberately not folded into one
# "save everything" call, so that a failure in one never leaves a *different*
# kind of change (e.g. a price) half-applied together with a *quantity*
# change in the same request:
#
# - record_opening_count(): the ONE time an Item+Warehouse pair is counted
#   for the first time. May also set Standard Buying in the very same
#   request (so Stock Reconciliation's own native fallback -- see
#   get_difference_account usage below -- picks it up as the opening
#   valuation without this module ever setting valuation_rate itself).
# - adjust_item_quantity(): every count after that. Never touches price.
# - update_item_master(): item_group/Standard Buying/Standard Selling,
#   completely independent of any quantity change, no confirmation needed.
#
# Atomicity: each of the three is exactly one whitelisted function call --
# one HTTP request. Frappe's own request boundary (frappe.app's
# sync_database() on the exception-free path, db.rollback(chain=True) on
# any exception) is what makes every write inside one of these functions
# all-or-nothing; none of them ever calls frappe.db.commit() itself, so an
# exception raised after, say, an Item Price update but before the Stock
# Reconciliation submit rolls back the Item Price write too -- proven by
# test_record_opening_count_rolls_back_item_price_if_reconciliation_fails
# in test_inventario_api.py, not just asserted here.


def _get_and_lock_item(item_code):
    """Fetch Item (raising frappe.DoesNotExistError if missing) AND take a
    real row lock on it (SELECT ... FOR UPDATE via frappe.db's own
    for_update=True) before anything else reads live stock/opening-stock
    state for it. Same idiom already proven in this app (Commit 16's
    Sales Order row lock) -- two concurrent adjustment requests for the
    same Item now serialize instead of racing; the second one only
    proceeds once the first has fully committed or rolled back, and then
    re-reads genuinely current state (see _prepare_quantity_adjustment()).
    Locking Item rather than Bin: a first-ever count can target a
    Warehouse this Item has no Bin row for yet, so there may be nothing to
    lock there."""
    locked_name = frappe.db.get_value("Item", item_code, "name", for_update=True)
    if not locked_name:
        frappe.throw(_("El producto {0} no existe.").format(item_code), frappe.DoesNotExistError)
    return frappe.get_doc("Item", item_code)


def _validate_warehouse_for_write(warehouse, company):
    """Warehouse checks NOT enforced by Stock Reconciliation's own
    validate() when a document is built directly (frappe.get_doc(...).insert())
    instead of through the "Get Items" button's own get_items_for_stock_reco()
    helper -- confirmed by reading stock_reconciliation.py directly: is_group/
    disabled/company are only checked inside that other helper, not in
    validate()/validate_data(). Without this, a hand-built Stock
    Reconciliation could silently target a group or disabled Warehouse."""
    wh = frappe.db.get_value(
        "Warehouse", warehouse, ["name", "is_group", "disabled", "company"], as_dict=True
    )
    if not wh:
        frappe.throw(_("El almacén {0} no existe.").format(warehouse), frappe.DoesNotExistError)
    if wh.is_group:
        frappe.throw(_("El almacén {0} es un grupo; seleccione un almacén específico.").format(warehouse))
    if wh.disabled:
        frappe.throw(_("El almacén {0} está deshabilitado.").format(warehouse))
    if wh.company != company:
        frappe.throw(_("El almacén {0} no pertenece a la compañía {1}.").format(warehouse, company))
    return wh


def _get_current_qty(item_code, warehouse):
    """Live Bin.actual_qty for this exact Item+Warehouse -- 0 if no Bin row
    exists yet (an Item that has never had stock in that Warehouse), never
    an error. Read-only -- this module never writes to Bin."""
    rows = frappe.get_list(
        "Bin",
        filters={"item_code": item_code, "warehouse": warehouse},
        fields=["actual_qty"],
        limit_page_length=1,
    )
    return flt(rows[0].actual_qty) if rows else 0.0


def _has_opening_stock(item_code, warehouse):
    """The persistent, auditable "was this Item+Warehouse ever counted for
    the first time" signal -- deliberately NOT current_qty != 0 (an Item
    can legitimately go 48 -> 0 -> 10 without that 10 being a new opening;
    the evidence must live in history, not in today's snapshot). Answer:
    does a submitted (docstatus=1), non-cancelled Stock Reconciliation
    with purpose="Opening Stock" exist whose items include this exact
    Item+Warehouse? Two bulk frappe.get_list() calls (never N+1, same
    convention as _bin_totals()); parent_doctype="Stock Reconciliation" on
    the first one is required for permission checks to run against the
    real parent doctype -- Frappe treats a child doctype's own permission
    map as empty otherwise (frappe.permissions.has_child_permission()),
    confirmed by reading frappe/permissions.py + frappe/model/qb_query.py
    directly. No custom field, no new DocType -- reuses the exact
    Stock Reconciliation documents this module's own endpoints create."""
    parents = frappe.get_list(
        "Stock Reconciliation Item",
        filters={"item_code": item_code, "warehouse": warehouse},
        pluck="parent",
        parent_doctype="Stock Reconciliation",
    )
    if not parents:
        return False
    return bool(
        frappe.get_list(
            "Stock Reconciliation",
            filters={"name": ["in", parents], "purpose": "Opening Stock", "docstatus": 1},
            limit_page_length=1,
        )
    )


def _prepare_quantity_adjustment(item_code, warehouse, qty, reason, expected_current_qty):
    """Shared validation for both quantity endpoints: reason/qty/Item/
    Warehouse checks, the Item row lock, and the optimistic
    expected_current_qty check -- all BEFORE either endpoint decides
    Purpose or touches Standard Buying/Stock Reconciliation. Returns
    (company, current_qty) once every check has passed. Read-only by
    itself -- callers do the actual writing."""
    qty = flt(qty)
    expected_current_qty = flt(expected_current_qty)
    reason = (reason or "").strip()

    if not reason:
        frappe.throw(_("El motivo del ajuste es obligatorio."), AdjustmentReasonRequiredError)
    if qty < 0:
        frappe.throw(_("La cantidad no puede ser negativa."))

    item = _get_and_lock_item(item_code)
    if item.disabled:
        frappe.throw(_("El producto {0} está deshabilitado.").format(item_code))
    if not item.is_stock_item:
        frappe.throw(_("El producto {0} no es un producto de inventario (is_stock_item=0).").format(item_code))

    company = get_default_company()
    if not company:
        frappe.throw(_("No hay una compañía configurada por defecto."))

    _validate_warehouse_for_write(warehouse, company)

    current_qty = _get_current_qty(item_code, warehouse)
    if current_qty != expected_current_qty:
        frappe.throw(
            _(
                "La existencia cambió desde que se cargó esta pantalla (existencia actual: {0}). "
                "Actualiza la página e intenta de nuevo."
            ).format(current_qty),
            StaleInventoryStateError,
        )

    return company, current_qty


def _upsert_item_price(item_code, price_list, rate):
    """Update the existing Item Price for (item_code, price_list) in
    place, or create exactly one if none exists yet -- never a duplicate.
    buying/selling/currency are never set here: Item Price.validate_item()
    fetches them natively from the Price List itself the moment
    price_list is set (item_price.py) -- setting them by hand would be
    redundant and could drift from the Price List's own configuration."""
    existing = frappe.get_list(
        "Item Price",
        filters={"item_code": item_code, "price_list": price_list},
        fields=["name"],
        limit_page_length=1,
    )
    if existing:
        doc = frappe.get_doc("Item Price", existing[0].name)
        doc.price_list_rate = rate
        doc.save()
        return doc

    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
    doc = frappe.get_doc(
        {
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "uom": stock_uom,
            "price_list_rate": rate,
        }
    )
    doc.insert()
    return doc


def _resolve_opening_valuation_rate(item_code, purchase_rate):
    """Corrected finding, verified empirically (not just by reading
    validate_data() in isolation, which is what the original design was
    based on and which turned out to be incomplete): for a genuinely
    first-ever Item+Warehouse pair, Stock Reconciliation's own
    remove_items_with_no_change() runs BEFORE validate_data(), and its own
    _changed() helper unconditionally sets item.valuation_rate from
    get_stock_balance_for()'s "rate" -- which is purely ledger-based
    (get_stock_balance()) and returns exactly 0 when no Stock Ledger Entry
    exists yet, with NO Item Price awareness at all. Once valuation_rate
    is set to a real 0.0 (not None) by that earlier step,
    validate_data()'s own later "no valuation_rate supplied -> fall back
    to Standard Buying" cascade never runs (it only triggers when the
    field is still None/""), so the reconciliation always fails at submit
    ("Valuation Rate required") for a first count unless THIS module sets
    valuation_rate itself. Reproduces the same priority the user asked
    for, applied here instead of relying on it happening natively:
    purchase_rate given (already written to Standard Buying by the
    caller) -> existing Standard Buying Item Price -> Item.valuation_rate.
    Returns 0 (falsy) if none of the three resolve to anything -- the
    caller must reject before building the Stock Reconciliation, never
    submit a zero valuation silently."""
    if purchase_rate:
        return flt(purchase_rate)
    existing = frappe.db.get_value(
        "Item Price", {"item_code": item_code, "price_list": PRICE_LIST_BUYING}, "price_list_rate"
    )
    if existing:
        return flt(existing)
    return flt(frappe.db.get_value("Item", item_code, "valuation_rate"))


@frappe.whitelist()
def record_opening_count(item_code, warehouse, qty, reason, expected_current_qty, purchase_rate=None):
    """The ONE-TIME first physical count for an Item+Warehouse pair that
    has never been counted before (see _has_opening_stock()). qty is the
    absolute counted quantity (never a delta) -- Stock Reconciliation's
    own qty field already means exactly that natively.

    If purchase_rate is given, Standard Buying is updated/created FIRST,
    in the same request. valuation_rate IS explicitly resolved and set on
    the Stock Reconciliation item here -- see _resolve_opening_valuation_rate()'s
    own docstring for why relying on Stock Reconciliation's native
    fallback (the original, unverified design) does not actually work for
    a first-ever count. Both writes (Item Price, then the Stock
    Reconciliation) are inside this one function call, so a failure
    between them rolls back together (see the module-level docstring
    above). If no purchase_rate is given AND the Item has no existing
    Standard Buying price AND no Item.valuation_rate, this throws
    PurchaseRateRequiredForOpeningError rather than submit a zero
    valuation.

    expense_account is resolved via ERPNext's own get_difference_account
    ("Opening Stock", company) -- a plain lookup by Account.account_type
    == "Temporary" for this company, never a hardcoded account name. If
    none exists, this throws MissingOpeningAccountError instead of
    guessing one."""
    _require_login()
    frappe.has_permission("Stock Reconciliation", "create", throw=True)
    # "write" is required too, not just "create"/"submit" -- confirmed live:
    # Stock Reconciliation's own native validate() -> remove_items_with_no_change()
    # -> get_stock_balance_for() calls frappe.has_permission("Stock Reconciliation",
    # "write", throw=True) unconditionally on every insert(), a real, load-bearing
    # requirement discovered by testing under a real restricted session, not
    # assumed from reading the doctype's own permission model alone.
    frappe.has_permission("Stock Reconciliation", "write", throw=True)
    frappe.has_permission("Stock Reconciliation", "submit", throw=True)

    qty = flt(qty)
    company, current_qty = _prepare_quantity_adjustment(item_code, warehouse, qty, reason, expected_current_qty)

    if _has_opening_stock(item_code, warehouse):
        frappe.throw(
            _("Este producto ya tiene un inventario inicial registrado para este almacén. Usa el ajuste normal."),
            OpeningStockAlreadyDoneError,
        )
    if current_qty != 0:
        frappe.throw(
            _(
                "El producto ya tiene existencia en ERPNext ({0}) para este almacén; no corresponde un "
                "inventario inicial. Usa el ajuste normal."
            ).format(current_qty),
            NonZeroOpeningStockError,
        )

    if purchase_rate is not None:
        purchase_rate = flt(purchase_rate)
        if purchase_rate < 0:
            frappe.throw(_("El valor de compra no puede ser negativo."))
        frappe.has_permission("Item Price", "write", throw=True)
        _upsert_item_price(item_code, PRICE_LIST_BUYING, purchase_rate)

    valuation_rate = _resolve_opening_valuation_rate(item_code, purchase_rate)
    if not valuation_rate:
        frappe.throw(
            _(
                "Este producto no tiene un valor de compra ni una valuación de referencia. "
                "Indique 'Valor de compra' para registrar el inventario inicial."
            ),
            PurchaseRateRequiredForOpeningError,
        )

    expense_account = get_difference_account("Opening Stock", company)
    if not expense_account:
        frappe.throw(
            _(
                "No hay una cuenta contable de tipo 'Temporary' configurada en {0} para registrar "
                "inventario inicial. Configure una en el Plan de Cuentas antes de continuar."
            ).format(company),
            MissingOpeningAccountError,
        )

    sr = frappe.get_doc(
        {
            "doctype": "Stock Reconciliation",
            "company": company,
            "purpose": "Opening Stock",
            "expense_account": expense_account,
            "fg_adjustment_reason": reason,
            "items": [{"item_code": item_code, "warehouse": warehouse, "qty": qty, "valuation_rate": valuation_rate}],
        }
    )
    sr.insert()
    sr.submit()

    return {
        "stock_reconciliation": sr.name,
        "item_code": item_code,
        "warehouse": warehouse,
        "previous_qty": current_qty,
        "new_qty": flt(sr.items[0].qty),
        "difference": flt(sr.items[0].qty) - current_qty,
        "valuation_rate": flt(sr.items[0].valuation_rate),
    }


@frappe.whitelist()
def adjust_item_quantity(item_code, warehouse, qty, reason, expected_current_qty):
    """Every physical count AFTER the first one for this Item+Warehouse
    (see _has_opening_stock() -- rejects otherwise, pointing at
    record_opening_count() instead). qty is the absolute counted quantity,
    never a delta -- 48 -> 45 means qty=45, not qty=-3. valuation_rate is
    never sent here either: with real ledger history now in place, Stock
    Reconciliation's own native fallback reuses the current valuation
    unchanged whenever only qty is supplied (validate_data()) -- exactly
    "no confundir purchase price con valuation_rate, no tocar valuation_rate
    al cambiar solo cantidad".

    expense_account is resolved via get_difference_account("Stock
    Reconciliation", company), which reads Company.stock_adjustment_account
    through ERPNext's own get_company_default() -- that call itself throws
    a native, functional error ("Please set default Stock Adjustment
    Account in Company ...") if the field is empty. Deliberately not
    caught or replaced here, and never defaulted to any specific account
    (not "Apertura Temporal - FG", not "149910 - ..."): configuring that
    account is a real accounting decision, out of scope for this module to
    guess."""
    _require_login()
    frappe.has_permission("Stock Reconciliation", "create", throw=True)
    # "write" is required too, not just "create"/"submit" -- confirmed live:
    # Stock Reconciliation's own native validate() -> remove_items_with_no_change()
    # -> get_stock_balance_for() calls frappe.has_permission("Stock Reconciliation",
    # "write", throw=True) unconditionally on every insert(), a real, load-bearing
    # requirement discovered by testing under a real restricted session, not
    # assumed from reading the doctype's own permission model alone.
    frappe.has_permission("Stock Reconciliation", "write", throw=True)
    frappe.has_permission("Stock Reconciliation", "submit", throw=True)

    qty = flt(qty)
    company, current_qty = _prepare_quantity_adjustment(item_code, warehouse, qty, reason, expected_current_qty)

    if not _has_opening_stock(item_code, warehouse):
        frappe.throw(
            _(
                "Este producto todavía no tiene un inventario inicial registrado para este almacén. "
                "Registra el inventario inicial primero."
            ),
            OpeningStockRequiredError,
        )

    expense_account = get_difference_account("Stock Reconciliation", company)

    sr = frappe.get_doc(
        {
            "doctype": "Stock Reconciliation",
            "company": company,
            "purpose": "Stock Reconciliation",
            "expense_account": expense_account,
            "fg_adjustment_reason": reason,
            "items": [{"item_code": item_code, "warehouse": warehouse, "qty": qty}],
        }
    )
    sr.insert()
    sr.submit()

    return {
        "stock_reconciliation": sr.name,
        "item_code": item_code,
        "warehouse": warehouse,
        "previous_qty": current_qty,
        "new_qty": flt(sr.items[0].qty),
        "difference": flt(sr.items[0].qty) - current_qty,
    }


@frappe.whitelist()
def update_item_master(item_code, item_group=None, purchase_rate=None, selling_rate=None):
    """item_group/Standard Buying/Standard Selling -- independent of any
    quantity change (no Stock Reconciliation involved at all here), so no
    confirmation dialog is required client-side for this one. Never
    touches stock or valuation_rate. Item Price is always updated in
    place via _upsert_item_price() -- never a second row for the same
    (item_code, price_list)."""
    _require_login()

    if item_group is None and purchase_rate is None and selling_rate is None:
        frappe.throw(_("No se especificó ningún cambio."))

    if not frappe.db.exists("Item", item_code):
        frappe.throw(_("El producto {0} no existe.").format(item_code), frappe.DoesNotExistError)

    if item_group is not None:
        frappe.has_permission("Item", "write", throw=True)
        if not frappe.db.exists("Item Group", item_group):
            frappe.throw(_("El grupo de producto {0} no existe.").format(item_group))
        item = frappe.get_doc("Item", item_code)
        item.item_group = item_group
        item.save()

    if purchase_rate is not None:
        purchase_rate = flt(purchase_rate)
        if purchase_rate < 0:
            frappe.throw(_("El valor de compra no puede ser negativo."))
        frappe.has_permission("Item Price", "write", throw=True)
        _upsert_item_price(item_code, PRICE_LIST_BUYING, purchase_rate)

    if selling_rate is not None:
        selling_rate = flt(selling_rate)
        if selling_rate < 0:
            frappe.throw(_("El precio de venta no puede ser negativo."))
        frappe.has_permission("Item Price", "write", throw=True)
        _upsert_item_price(item_code, PRICE_LIST, selling_rate)

    return {
        "item_code": item_code,
        "item_group": frappe.db.get_value("Item", item_code, "item_group"),
        "purchase_rate": frappe.db.get_value(
            "Item Price", {"item_code": item_code, "price_list": PRICE_LIST_BUYING}, "price_list_rate"
        ),
        "selling_rate": frappe.db.get_value(
            "Item Price", {"item_code": item_code, "price_list": PRICE_LIST}, "price_list_rate"
        ),
    }
