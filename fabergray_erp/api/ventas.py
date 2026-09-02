# -*- coding: utf-8 -*-
"""Commit 18.2 -- API layer for the future Page Ventas (Commit 18.3), used
by the Vendedora role.

Same conventions as api/bodega.py and api/jefe_bodega.py: every read goes
through `frappe.get_list`/`frappe.get_doc()` + `check_permission()`, never
`frappe.get_all` and never `ignore_permissions=True` -- Vendedora always
operates with her own real, restricted permissions (Commit 18.1: read on
Customer/Item/Address/Contact, `select` on Account, create/read/write/
submit on Sales Order; Commit 25.1 dropped the original `if_owner=1`
scoping -- "el rol controla el área, no el owner" -- so this is now
shared across every Vendedora of the same Company, Company isolation
enforced by `fabergray_erp/permission_conditions.py` instead), nothing on
Pick List or Reporte de Faltante. The one narrow, documented exception
approved in
Commit 18.1 -- `ignore_permissions=True`/`via_fulfillment_engine=True`
inside `fulfillment/pick_list_service.py`, `shortage_service.py`,
`cancellation_service.py`, and `api/bodega.py._insert_shortage_report()`
-- is reached only through `Sales Order.submit()`'s own `on_submit` hook
(Commit 16), never called directly from here. Nothing in this module
accepts, forwards, or could ever trigger that bypass from the client --
see `test_ventas_security.py`'s guardrail for the automated check.

Fundamental rule this whole module exists to enforce (Commit 18's
approved design): Vendedora never sees or sends a price, a discount, a
tax amount, or a total. Every function below either omits economic
fields entirely from its response, or -- for `create_and_submit_sales_order()`
-- explicitly rejects any economic field the caller tries to send.
ERPNext's own native pricing engine resolves `rate`/`price_list_rate`/
taxes/`grand_total` server-side, automatically, the moment the Sales
Order is inserted (confirmed in the Commit 18 proposal by reading
`accounts_controller.py`'s `validate()` chain directly) -- nothing here
duplicates or second-guesses that.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, nowdate

from erpnext.stock.doctype.pick_list.pick_list import get_actual_qty

from fabergray_erp.api.bodega import _require_login
from fabergray_erp.fulfillment.modification_service import modification_blockers_for
from fabergray_erp.permission_conditions import assert_same_company
from fabergray_erp.quick_order import catalog as quick_order_catalog
from fabergray_erp.quick_order import matcher as quick_order_matcher
from fabergray_erp.quick_order import parser as quick_order_parser
from fabergray_erp.sales_order_naming import root_commercial_name

# No native default exists for Sales Order.delivery_date -- confirmed by
# reading validate_delivery_date() (sales_order.py) directly: it throws
# "Please enter Delivery Date" if neither the header nor any item row has
# one set, with no computed fallback. A value must be supplied. Kept as
# one named, centralized constant (not a magic number inline) so it is
# easy to find, change, and assert against in tests.
DEFAULT_DELIVERY_LEAD_DAYS = 7

# The only two fields a Sales Order line may carry in from the client.
# Anything else -- rate, price_list_rate, discount_percentage,
# discount_amount, amount, net_rate, net_amount,
# margin_rate_or_amount, margin_type, or any other/future economic
# field -- is rejected outright, not silently dropped, by
# create_and_submit_sales_order() below.
_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}

# Commit 25.8.5 -- defensive input limits for parse_quick_order() below.
# 50 lines / 10,000 characters is generous for what this feature is for (a
# single pasted WhatsApp order) while still bounding the work one request
# can force the server to do -- each line runs its own
# search_catalog_candidates() + match_order_line() pass. Exceeding either
# throws immediately (frappe.ValidationError, via frappe.throw()'s own
# default) -- never silently truncated, so the asesora is never shown a
# partial interpretation of her own pasted text without knowing it.
QUICK_ORDER_MAX_LINES = 50
QUICK_ORDER_MAX_CHARS = 10000

# The only fields a quick-order candidate is ever serialized with (Commit
# 25.8 audit + this commit's own brief, section 8) -- built explicitly, key
# by key, in _serialize_quick_order_candidate() below, never by forwarding
# scoring.score_candidate()'s own dict or Candidate itself verbatim. No
# rate/price/price_list_rate/valuation_rate/cost/standard_rate/stock field
# is ever read into a variable here, matching get_item_info()/search_items()'s
# own "nothing to leak by construction" convention elsewhere in this module.


class SalesOrderAlreadyCancelledError(frappe.ValidationError):
    pass


def _validate_and_build_item_rows(items, company, delivery_date):
    """The one place a Sales Order Item row list is built from
    client-supplied data -- shared by `create_and_submit_sales_order()`,
    `update_draft_sales_order()` (Commit 18.5) and
    `modify_submitted_sales_order()`, extracted so every entry point that
    accepts an `items` payload from Vendedora enforces the exact same
    allowlist/validation, never two parallel copies of the same security
    boundary.

    Rejects any line carrying a key outside `_ALLOWED_ITEM_FIELDS`
    (`rate`, `price_list_rate`, `discount_percentage`, `discount_amount`,
    `amount`, `net_rate`, `net_amount`, `margin_rate_or_amount`,
    `margin_type`, or anything else, present or future) -- never
    silently dropped. `delivery_date` is the caller's single,
    already-resolved value, applied uniformly to every line.

    Commit 25.2 -- "no duplicar la resolución nativa de warehouse": no
    `warehouse` key is ever added to a row here, on purpose. Confirmed
    live, during this exact commit's own audit, that Frappe's own child
    row `.get("warehouse")` returns `None` for a key genuinely never set
    (not `""`) -- exactly the condition `AccountsController.
    set_missing_item_details()` checks (`item.get(fieldname) is None`)
    before auto-filling a field from `get_item_details()`'s own response.
    So omitting the key here is not "doing nothing" -- it is the
    documented, correct way to hand the decision to ERPNext's own
    `Sales Order.validate()` pipeline (`set_missing_item_details()` ->
    `get_item_details()` -> `get_item_warehouse_()`), which already
    resolves, in this exact order and entirely inside `apps/erpnext`,
    never duplicated here: Item Default -> Item Group Default -> Brand
    Default -> `Stock Settings.default_warehouse` (only if that
    warehouse's own `company` matches this Sales Order's `company` --
    confirmed live by this commit's own audit, including the negative
    case: a `Stock Settings.default_warehouse` belonging to a DIFFERENT
    company is correctly ignored). If none of those resolve anything,
    `Sales Order.validate_warehouse()` itself raises ERPNext's own
    `WarehouseRequired` ("Source warehouse required for stock item
    {item}") -- this module no longer raises a custom message for that
    case; see this commit's own report for the full precedence audit.

    `company` is still accepted as a parameter (kept for every existing
    caller's own call shape) but is no longer used inside this function
    -- ERPNext's own pipeline resolves company-scoped defaults itself
    from the Sales Order's own `company` field, already set by the
    caller before `.insert()`/`.save()` runs.
    """
    items = frappe.parse_json(items) if isinstance(items, str) else items
    if not items:
        frappe.throw(_("El pedido debe tener al menos un producto."))

    so_items = []
    for row in items:
        if not isinstance(row, dict):
            frappe.throw(_("Formato de línea de pedido inválido."))

        disallowed = set(row.keys()) - _ALLOWED_ITEM_FIELDS
        if disallowed:
            frappe.throw(
                _("Campos no permitidos en la línea del pedido: {0}").format(", ".join(sorted(disallowed)))
            )

        if "item_code" not in row or "qty" not in row:
            frappe.throw(_("Cada línea del pedido debe incluir item_code y qty."))

        item_code = row["item_code"]
        qty = flt(row["qty"])

        if not frappe.db.exists("Item", item_code):
            frappe.throw(_("El producto {0} no existe.").format(item_code))
        if qty <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(item_code))

        so_items.append(
            {
                "item_code": item_code,
                "qty": qty,
                "delivery_date": delivery_date,
            }
        )

    return so_items


def _default_warehouse_for_item(item_code, company):
    """Commit 25.2 -- narrowed to exactly one caller: `get_item_info()`'s
    own `qty_disponible` preview, informational only. NOT used to build
    Sales Order Item rows anymore (`_validate_and_build_item_rows()`
    leaves `warehouse` unset on purpose -- see its own docstring) -- this
    function never raises for a missing default, it simply returns
    `None`, and `get_item_info()` already renders that as "no disponible"
    without blocking anything.

    Deliberately reads ONLY the native `Item Default` child table
    (`Item.item_defaults`) for the resolved company -- NOT the full
    Item Group Default / Brand Default / Stock Settings.default_warehouse
    chain ERPNext itself applies during `Sales Order.insert()` (see
    `_validate_and_build_item_rows()`'s own docstring for why that chain
    is deliberately never replicated here): this is a cheap, best-effort
    preview shown before any Sales Order exists, not the authoritative
    resolution, so a slightly more conservative preview (occasionally
    showing "no disponible" for a product whose warehouse would in fact
    resolve at insert time via Item Group/Brand/Stock Settings) is an
    accepted, documented trade-off rather than a second, duplicated copy
    of ERPNext's own precedence logic. `frappe.db.get_value` is a raw,
    single-value read (not `get_all`), the same kind already used
    throughout this app's Fulfillment Engine."""
    return frappe.db.get_value("Item Default", {"parent": item_code, "company": company}, "default_warehouse")


@frappe.whitelist()
def search_customers(txt=""):
    """Existing customers Vendedora may pick for a new Sales Order.
    Returns only what is needed to identify one -- no credit limit,
    outstanding balance, price list, or any other financial/portfolio
    field."""
    _require_login()
    frappe.has_permission("Customer", "read", throw=True)

    or_filters = None
    if txt:
        or_filters = [["customer_name", "like", f"%{txt}%"], ["name", "like", f"%{txt}%"]]

    return frappe.get_list(
        "Customer",
        filters={"disabled": 0},
        or_filters=or_filters,
        fields=["name", "customer_name"],
        order_by="customer_name asc",
        limit_page_length=20,
    )


@frappe.whitelist()
def search_items(txt=""):
    """Sellable products Vendedora may add to a new Sales Order. Returns
    only what she needs to identify one -- never `rate`,
    `price_list_rate`, `valuation_rate`, `standard_rate`,
    `last_purchase_rate`, or anything from Item Price; this function
    never even reads those fields, so there is nothing to accidentally
    leak. `is_sales_item`/`has_variants`/`disabled` mirror the standard,
    native filters for "is this a real, directly sellable product" --
    not custom criteria of this app's own invention."""
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    or_filters = None
    if txt:
        or_filters = [["item_code", "like", f"%{txt}%"], ["item_name", "like", f"%{txt}%"]]

    return frappe.get_list(
        "Item",
        filters={"disabled": 0, "is_sales_item": 1, "has_variants": 0},
        or_filters=or_filters,
        fields=["item_code", "item_name", "description", "stock_uom", "image"],
        order_by="item_name asc",
        limit_page_length=20,
    )


@frappe.whitelist()
def get_item_info(item_code, customer=None, qty=None):
    """Operational detail for one product, for the "Nuevo Pedido" cart
    line -- an explicitly-built dict of only the allowed keys, never a
    forwarded `get_item_details()` response or `Item.as_dict()` (both of
    which carry pricing fields this function must never expose, by
    construction, not by after-the-fact filtering).

    `qty_disponible` is informative only (Commit 18's approved design --
    never blocks adding a line) and reuses `get_actual_qty()`
    (erpnext.stock.doctype.pick_list.pick_list), the exact same helper
    api/bodega.py's own `qty_disponible` and the Fulfillment Engine's
    analyzer already read Bin availability through -- one source of
    truth for "how much is physically there", not a second one invented
    for this Page. `warehouse` here comes from `_default_warehouse_for_item()`
    -- Item Default ONLY, not the fuller Item Group/Brand/Stock Settings
    chain ERPNext itself applies at `Sales Order.insert()` time (Commit
    25.2) -- so this preview can show "no disponible" for a product whose
    warehouse would in fact resolve once she actually confirms the order;
    an accepted, documented trade-off (see `_default_warehouse_for_item()`'s
    own docstring) rather than duplicating ERPNext's precedence logic just
    for a pre-submit preview.
    """
    _require_login()

    item = frappe.get_doc("Item", item_code)
    item.check_permission("read")

    company = frappe.defaults.get_global_default("company")
    warehouse = _default_warehouse_for_item(item.item_code, company)

    qty_disponible = flt(get_actual_qty(item.item_code, warehouse)) if warehouse else None

    return {
        "item_code": item.item_code,
        "item_name": item.item_name,
        "description": item.description,
        "stock_uom": item.stock_uom,
        "image": item.image,
        "qty_disponible": qty_disponible,
    }


def _serialize_quick_order_candidate(scored_candidate, catalog_index):
    """The one place a `scoring.score_candidate()` result is turned into
    what the client actually receives -- an explicit, field-by-field dict,
    never the scoring result forwarded verbatim (same convention
    `get_order_detail()`'s own docstring establishes elsewhere in this
    module: nothing economic is ever read into a variable here in the first
    place, so there is nothing to filter after the fact).

    `stock_uom` is the one field `scoring.score_candidate()` itself doesn't
    carry (it only knows `item_code`/`item_name`/score/tokens) -- looked up
    from the already-loaded `catalog_index["by_code"]` Candidate, never a
    fresh `frappe.get_doc()` per candidate.
    """
    candidate = catalog_index["by_code"].get(scored_candidate["item_code"])
    return {
        "item_code": scored_candidate["item_code"],
        "item_name": scored_candidate["item_name"],
        "stock_uom": candidate["stock_uom"] if candidate else None,
        "score": scored_candidate["score"],
        "confidence": scored_candidate["confidence"],
        "matched_tokens": scored_candidate["matched_tokens"],
        "conflicts": scored_candidate["conflicts"],
        "reasons": scored_candidate["reasons"],
    }


def _build_quick_order_line_response(parsed_line, match_result, catalog_index):
    """One line of `parse_quick_order()`'s own response -- see that
    function's own docstring for the full shape and the
    `top_candidate`/`preselected_item` distinction (Commit 25.8.5 brief,
    section 6), which is decided HERE, once, server-side -- `ventas.js`
    (Commit 25.8.6+, not built yet) will never need to re-derive the
    high/ambiguous business rule itself."""
    candidates = [_serialize_quick_order_candidate(c, catalog_index) for c in match_result["candidates"]]
    top_candidate = candidates[0] if candidates else None
    confidence = top_candidate["confidence"] if top_candidate else "low"

    preselected_item = None
    if top_candidate and top_candidate["confidence"] == "high" and not match_result["ambiguous"]:
        preselected_item = top_candidate

    return {
        "source_text": parsed_line["source_text"],
        "qty": parsed_line["qty"],
        "detected_uom": parsed_line["detected_uom"],
        "product_text": parsed_line["product_text"],
        "top_candidate": top_candidate,
        "preselected_item": preselected_item,
        "confidence": confidence,
        "ambiguous": match_result["ambiguous"],
        "score_margin": match_result["score_margin"],
        "candidates": candidates,
    }


@frappe.whitelist()
def parse_quick_order(text):
    """Commit 25.8.5 -- "Pedido rápido": interprets a pasted, multi-line,
    free-text order against the real sellable Item catalog and returns a
    per-line list of scored candidates. Read-only, no side effect of any
    kind beyond the catalog's own already-designed read-through cache
    (Commit 25.8.3's `get_cached_catalog()`) -- never touches a Sales
    Order, a Quotation, a Pick List, a cart, or an Item. Never accepts an
    `item_code` from the client -- the only input is free text; every
    candidate this function can ever return comes from
    `quick_order.catalog.get_sellable_item_candidates()`'s own filter
    (`disabled=0`/`is_sales_item=1`/`has_variants=0`, identical to
    `search_items()` above), never anything the caller named directly.

    This function is deliberately thin -- it is glue, not logic. Every bit
    of actual interpretation lives in `quick_order.parser`/`.catalog`/
    `.matcher` (Commits 25.8.1-25.8.4, all pure Python except `catalog.py`'s
    own read-only ERPNext integration) and is reused here verbatim, never
    duplicated: `parse_order_text()` for parsing, `get_cached_catalog()` +
    `search_catalog_candidates()` for retrieval, `match_order_line()` for
    scoring/ranking. `get_cached_catalog()` is called exactly ONCE per
    request (never once per line) -- the loop below only calls
    `search_catalog_candidates()`/`match_order_line()` per line, both of
    which take the already-loaded `catalog_index` as an argument rather
    than re-fetching it.

    Security: `_require_login()` + `frappe.has_permission("Item", "read",
    throw=True)` -- the exact same two calls `search_items()`/
    `get_item_info()` above already use for the same reason (Vendedora's
    existing Item-read grant, Commit 18.1). No new permission of any kind
    was added for this endpoint.

    Input validation (Commit 25.8.5 brief, section 3): `text` must be a
    non-empty string (after `.strip()`); longer than
    `QUICK_ORDER_MAX_CHARS` or more than `QUICK_ORDER_MAX_LINES` non-blank
    lines raises immediately (`frappe.throw()`'s own default
    `frappe.ValidationError`) -- never silently truncated, so nothing is
    ever interpreted from a request the asesora doesn't know was cut short.

    A line with no reasonable candidate at all (`candidates: []`, e.g.
    "producto que no existe xyz") never fails the whole request -- see
    `_build_quick_order_line_response()`'s own construction, which always
    returns a well-formed line dict regardless of whether anything matched.

    Returns:
        {
            "lines": [
                {
                    "source_text": str,
                    "qty": int | float,
                    "detected_uom": str | None,
                    "product_text": str,
                    "top_candidate": <candidate> | None,       # best result, if any -- NOT a selection
                    "preselected_item": <candidate> | None,    # ONLY if confidence=="high" AND not ambiguous
                    "confidence": "high" | "medium" | "low",   # of top_candidate, "low" if none
                    "ambiguous": bool,
                    "score_margin": int | float | None,
                    "candidates": [<candidate>, ...],          # <= 5, score DESC
                },
                ...
            ],
            "line_count": int,
        }

        <candidate> = {
            "item_code": str,
            "item_name": str,
            "stock_uom": str | None,
            "score": int,                     # 0-100
            "confidence": "high" | "medium" | "low",
            "matched_tokens": [str, ...],
            "conflicts": [{"category", "order_value", "candidate_value"}, ...],
            "reasons": [str, ...],
        }

        Never any economic field (`rate`/`price`/`price_list_rate`/
        `valuation_rate`/`cost`/`standard_rate`) or stock-quantity field (no
        Bin/Stock Ledger read of any kind happens here), or `description`
        (omitted on purpose -- Commit 25.8.5 brief, section 9: item_name is
        already long enough, description would only
        grow the payload with nothing the UI needs yet).
    """
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    if not isinstance(text, str) or not text.strip():
        frappe.throw(_("El texto del pedido no puede estar vacío."))

    if len(text) > QUICK_ORDER_MAX_CHARS:
        frappe.throw(
            _("El texto del pedido es demasiado largo (máximo {0} caracteres).").format(QUICK_ORDER_MAX_CHARS)
        )

    parsed_lines = quick_order_parser.parse_order_text(text)

    if len(parsed_lines) > QUICK_ORDER_MAX_LINES:
        frappe.throw(
            _("El pedido tiene demasiadas líneas (máximo {0}).").format(QUICK_ORDER_MAX_LINES)
        )

    catalog_index = quick_order_catalog.get_cached_catalog()  # ONE load for the whole request, never per line

    lines = [
        _build_quick_order_line_response(
            parsed_line,
            quick_order_matcher.match_order_line(
                parsed_line, quick_order_catalog.search_catalog_candidates(parsed_line, catalog=catalog_index)
            ),
            catalog_index,
        )
        for parsed_line in parsed_lines
    ]

    return {"lines": lines, "line_count": len(lines)}


@frappe.whitelist()
def get_my_orders(limit=50):
    """All of Fabrigray's Sales Orders (Commit 25.1: "el rol controla el
    área, no el owner" -- if_owner dropped from Sales Order/Vendedora's
    Custom DocPerm), most recent first -- operational fields only
    (number, customer, dates, status, line/unit counts, observations),
    never a total or a price. Every Vendedora sees every order of this
    site's own Company, regardless of who created it -- `owner` is no
    longer a visibility filter anywhere in this function, only ever a
    display/audit field elsewhere. Company isolation is enforced
    centrally by `fabergray_erp.permission_conditions.
    sales_order_permission_query_conditions()` (hooks.py's own
    `permission_query_conditions`), applied automatically by
    `frappe.get_list()` below -- never re-derived here, never trusting a
    `company` value from the client. `frappe.get_all()` is never used
    here.

    Per-order line/unit counts are read by loading each of her own,
    already-authorized Sales Orders individually
    (`frappe.get_doc()` + `len(so.items)`) rather than a batched query
    against the Sales Order Item child table -- child doctypes have no
    permission model of their own, so a batched child-table read would
    not reliably respect `if_owner` the way the parent-level query
    already does. Same pattern api/jefe_bodega.py's own
    `get_active_pick_lists()` already established for the identical
    reason. `total_qty` (a native, top-level Sales Order field -- no
    child-table read needed for it at all) supplies the unit count.

    `observations` is read straight off `so.fg_observations` (a Custom
    Field, see `create_and_submit_sales_order()`'s docstring for why this
    is not `Comment`) -- covered by the exact same if_owner=1 Sales Order
    permission already checked above, no separate permission needed.

    One card per commercial order (Commit 18.5): a Sales Order that some
    OTHER one of her own documents already lists as `amended_from` was
    superseded by that later amendment -- it is skipped here entirely, so
    only the current tip of each amend chain is ever returned (a chain can
    never fork -- Frappe forbids cancelling an already-cancelled document,
    so at most one document can ever amend a given one). This is what
    keeps "PEDIDO-1 Cancelado" and "PEDIDO-1-1 Activo" from ever appearing
    as two separate cards -- only the still-relevant tip shows, labelled
    with `commercial_name` (the walk-back-to-the-root helper below), never
    the raw, possibly-suffixed technical `name`. A genuinely cancelled
    order (nothing amends it) is unaffected and still shows, exactly as
    before.

    `modifiable` is the same non-authoritative pre-check
    `get_modification_status()` exposes standalone, computed only for a
    docstatus==1 order (never for Draft/Cancelled, which are never
    modifiable via this path regardless) -- lets the UI hide/disable
    "MODIFICAR PEDIDO" per card without a second round-trip; the
    authoritative check still lives in `modify_submitted_sales_order()`
    itself, re-derived there, never trusted from here.
    """
    _require_login()
    frappe.has_permission("Sales Order", "read", throw=True)

    names = frappe.get_list(
        "Sales Order",
        fields=["name"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=0,
        pluck="name",
    )

    max_orders = cint(limit) or 50
    orders = []
    for name in names:
        if len(orders) >= max_orders:
            break
        if frappe.db.exists("Sales Order", {"amended_from": name}):
            continue  # superseded by a later amendment -- the chain's tip is listed instead

        so = frappe.get_doc("Sales Order", name)
        so.check_permission("read")

        modifiable = so.docstatus == 1 and not modification_blockers_for(so.name)

        orders.append(
            {
                "name": so.name,
                "commercial_name": root_commercial_name(so.name),
                "customer": so.customer,
                "customer_name": so.customer_name,
                "transaction_date": so.transaction_date,
                "delivery_date": so.delivery_date,
                "status": so.status,
                "item_count": len(so.items),
                "total_qty": so.total_qty,
                "observations": so.fg_observations,
                "modifiable": modifiable,
            }
        )

    return orders


@frappe.whitelist()
def get_order_detail(name):
    """Line-level detail for any Sales Order of this Company -- the "VER
    PEDIDO" view in Page Ventas (Commit 18.4). `check_permission("read")`
    now only enforces the role-level grant (Commit 25.1 -- if_owner=0,
    Vendedora reads any Sales Order of her Company); `assert_same_company()`
    right after it is what actually keeps a Vendedora from reading another
    Company's order by name -- `check_permission()` alone no longer does,
    since the Custom DocPerm grant carries no Company concept of its own
    (see `fabergray_erp/permission_conditions.py`'s own module docstring).

    The response is built field by field, never `so.as_dict()` or
    `row.as_dict()` (both of which carry every economic field on the
    document) -- there is nothing to filter after the fact because
    nothing economic is ever read into a variable here in the first
    place. See `test_regression.py`'s
    `test_get_order_detail_never_calls_as_dict_and_only_returns_allowlisted_keys`
    for the static guardrail that keeps this true if this function is
    ever touched again.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("read")
    assert_same_company(so)

    return {
        "name": so.name,
        "commercial_name": root_commercial_name(so.name),
        "customer": so.customer,
        "customer_name": so.customer_name,
        "transaction_date": so.transaction_date,
        "delivery_date": so.delivery_date,
        "status": so.status,
        "item_count": len(so.items),
        "total_qty": so.total_qty,
        "observations": so.fg_observations,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty,
                "stock_uom": row.stock_uom,
            }
            for row in so.items
        ],
    }


@frappe.whitelist()
def get_sales_summary():
    """KPI counts for the Page Ventas dashboard header -- derived
    exclusively from Sales Order/Sales Order Item's own native fields,
    scoped to this site's own Company (Commit 25.1: no longer to
    Vendedora's own orders -- if_owner dropped from the Custom DocPerm).
    Company isolation comes from `frappe.get_list()`'s own
    `permission_query_conditions` (see `permission_conditions.py`), never
    a manual filter here; `frappe.get_all()` is never used. Deliberately
    only the four states directly derivable from `Sales Order.status`/
    `transaction_date` (confirmed native values: Draft, On Hold, To Pay,
    To Deliver and Bill, To Bill, To Deliver, Completed, Cancelled,
    Closed) -- no "Alistamiento iniciado" or similar experimental bucket
    yet, and no bucket that would require Pick List/Reporte de Faltante
    access (Commit 18's approved Option B)."""
    _require_login()
    frappe.has_permission("Sales Order", "read", throw=True)

    pedidos_hoy = frappe.get_list("Sales Order", filters={"transaction_date": nowdate()}, pluck="name")
    pendientes = frappe.get_list(
        "Sales Order",
        filters={"status": ["in", ["To Deliver and Bill", "To Deliver"]]},
        pluck="name",
    )
    entregados = frappe.get_list("Sales Order", filters={"status": "Completed"}, pluck="name")
    cancelados = frappe.get_list("Sales Order", filters={"status": "Cancelled"}, pluck="name")

    return {
        "pedidos_hoy": len(pedidos_hoy),
        "pendientes": len(pendientes),
        "entregados": len(entregados),
        "cancelados": len(cancelados),
    }


@frappe.whitelist()
def create_and_submit_sales_order(customer, items, observations=None):
    """The critical operation: build and submit a standard Sales Order
    from exactly what Vendedora is allowed to send -- `customer` and, per
    line, `item_code`/`qty` only -- and let it run through the exact same
    `.insert()` -> `.submit()` -> `on_submit` hook -> Fulfillment Engine
    path (Commits 15/16/18.1) as any other Sales Order in this app. This
    function calls `process_sales_order()` or any Engine internal
    function *nowhere* -- `.submit()` alone is what triggers it, exactly
    per the standing "don't call the Engine directly if submit already
    does" rule.

    Security (Commit 18's approved design, verified here, not just
    documented): each line is checked against an explicit allowlist
    (`_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}`) via
    `_validate_and_build_item_rows()` (extracted in Commit 18.5, shared
    verbatim with `update_draft_sales_order()` -- one security boundary,
    not two) -- any other key (`rate`, `price_list_rate`,
    `discount_percentage`, `discount_amount`, `amount`, `net_rate`,
    `net_amount`, `margin_rate_or_amount`, `margin_type`, or anything
    else, present or future) makes this function raise immediately,
    before any Sales Order is even constructed -- never silently
    dropped. The Sales Order Item rows built from the surviving fields
    carry `item_code`/`qty`/`delivery_date` only -- `warehouse` is
    deliberately absent, see `_validate_and_build_item_rows()`'s own
    docstring (Commit 25.2); ERPNext's own `AccountsController.validate()`
    (`set_missing_item_details()` then `calculate_taxes_and_totals()`)
    resolves both warehouse and every price/discount/tax field itself,
    unconditionally, the moment `.insert()` runs -- there is no pricing
    or warehouse-resolution logic in this function to duplicate or get
    wrong.

    `observations`, if given, is stored in `Sales Order.fg_observations` --
    a Custom Field (fixtures/custom_field.json), not `add_comment()`.
    `add_comment()` was the first choice, but it turned out to create a
    read-side permission gap: `Document.add_comment()` always inserts with
    `ignore_permissions=True` (universal Frappe behaviour, every doctype),
    so *writing* the observation never had a problem -- but *reading* it
    back in `get_my_orders()` does, because the base `Comment` doctype's
    own native permission model only grants `read` to `System Manager`/
    `Website Manager`, which Vendedora is neither. `fg_observations` is
    read and written through the same Sales Order permission she already
    has (role+Company-scoped since Commit 25.1) -- no new permission of
    any kind.

    Warehouse is deliberately NEVER set here (neither per item nor via
    `set_warehouse`) -- Commit 25.2: ERPNext's own `Sales Order.insert()`
    pipeline resolves it, per line, through its own native precedence
    (Item Default -> Item Group Default -> Brand Default -> `Stock
    Settings.default_warehouse`, Company-checked); see
    `_validate_and_build_item_rows()`'s own docstring for the full audit.
    Vendedora still never chooses one herself either way, matching the
    approved design that she cannot touch stock configuration -- she
    simply no longer has to have one pre-configured for the order to go
    through.

    Returns `{"name": "SAL-ORD-..."}` only -- no total, no price, nothing
    the Fulfillment Engine produced (Pick List/Reporte de Faltante
    names): she has no permission to read either anyway, and the
    dashboard/`get_my_orders()` above are the intended way to see what
    happened to a submitted order.
    """
    _require_login()
    frappe.has_permission("Sales Order", "create", throw=True)

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)

    so = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "customer": customer,
            "company": company,
            "transaction_date": nowdate(),
            "delivery_date": delivery_date,
            "items": so_items,
        }
    )
    if observations:
        so.fg_observations = observations

    so.insert()
    so.submit()  # triggers on_submit -> process_sales_order_for_confirmation() (Commit 25.4)

    return {"name": so.name}


@frappe.whitelist()
def create_draft_sales_order(customer, items, observations=None):
    """Commit 25.4 -- the new "Nuevo Pedido" entry point: builds the exact
    same Sales Order `create_and_submit_sales_order()` above builds
    (same allowlist, same `_validate_and_build_item_rows()`, same
    server-side warehouse resolution, Commit 25.2) but deliberately
    never calls `.submit()` -- the order is left in Borrador
    (`docstatus=0`), editable via `get_editable_order()`/`update_draft_
    sales_order()` (already existing, Commit 18.5, previously only
    reachable for a Draft created some other way -- now the real,
    ordinary path every new order takes) until the Vendedora explicitly
    calls `confirm_order()` below.

    `create_and_submit_sales_order()` itself is untouched, still valid,
    still callable -- this is a new, separate entry point, not a
    behavior change to that one (kept for any caller that genuinely
    wants create+submit in one shot).

    Returns `{"name": "PEDIDO-...", "docstatus": 0}` -- no economic
    field, matching every other write in this module.
    """
    _require_login()
    frappe.has_permission("Sales Order", "create", throw=True)

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)

    so = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "customer": customer,
            "company": company,
            "transaction_date": nowdate(),
            "delivery_date": delivery_date,
            "items": so_items,
        }
    )
    if observations:
        so.fg_observations = observations

    so.insert()  # stays Borrador -- docstatus=0, never submitted here

    return {"name": so.name, "docstatus": so.docstatus}


@frappe.whitelist()
def confirm_order(name):
    """Commit 25.4 -- "Confirmar pedido": the ONE transition from
    Borrador (`docstatus=0`) to Confirmado (`docstatus=1`) a Vendedora
    ever performs herself. `check_permission("submit")` is where access
    is actually enforced -- Vendedora already holds `submit=1` on her
    Sales Order Custom DocPerm (Commit 18.1), System Manager holds full
    access (Commit 25.1's own System Manager grant) -- no new
    permission of any kind was added for this. `assert_same_company()`
    (Commit 25.1) keeps it scoped to this Company, same as every other
    single-document function in this module.

    Idempotent by construction, per the approved contract:
    - `docstatus == 0` (Borrador): validate, then `.submit()` for real
      -- `docstatus` goes 0 -> 1. Validates customer/at-least-one-line/
      qty>0 explicitly here (defensive: every write path that can
      produce or edit a Draft --
      `create_draft_sales_order()`/`update_draft_sales_order()` --
      already enforces the same allowlist/qty>0 rule, so this should
      never actually fire in practice, but "revisión final" before an
      irreversible transition is re-derived here rather than trusted
      stale, the same standing convention `plan_route()`/
      `modify_submitted_sales_order()` already establish elsewhere in
      this app). Deliberately NEVER checks stock/availability -- see
      this commit's own brief, "NO validar existencia/disponibilidad
      de stock" -- Sales Order represents commercial demand, not a
      physical stock movement.
    - `docstatus == 1` (already Confirmado): returns success
      immediately, `status: "already_confirmed"` -- never calls
      `.submit()` again, so a second click (or a genuine network retry)
      can never trigger `on_submit` twice, which is what actually
      guarantees no duplicate Pick List/Reporte de Faltante/any other
      derived document -- `.submit()` itself is the one and only thing
      that can create one, and it is never reached on this branch.
    - `docstatus == 2` (Cancelado): rejected outright with a specific,
      functional message -- `SalesOrderAlreadyCancelledError`, never a
      generic native error.

    Concurrency note (not solved by a custom lock, deliberately, same
    reasoning this app's Fulfillment Engine already documents for its
    own residual races): two genuinely concurrent `confirm_order()`
    calls for the same still-Borrador order could both read
    `docstatus == 0` before either commits, but Frappe's own
    `Document.submit()` already carries its native optimistic-
    concurrency check (`modified` timestamp comparison) -- the second
    call to actually reach `.submit()` fails with Frappe's own
    `TimestampMismatchError` rather than silently succeeding a second
    time, so no duplicate submit (and therefore no duplicate derived
    document) can occur either way; it simply fails loudly instead of
    responding "already_confirmed" for that specific race window.

    Never sets `warehouse`, resolves pricing, or touches any economic
    field -- exactly like `create_and_submit_sales_order()`, ERPNext's
    own native pipeline does all of that during `.submit()`. Triggers
    `on_submit` -> `process_sales_order_for_confirmation()` (Commit
    25.4) -- the Pick List Bodega needs, never a Reporte de Faltante or
    Material Request created automatically; see that function's own
    docstring for the full reasoning.

    Returns `{"name": "PEDIDO-...", "docstatus": 0 or 1, "status":
    "confirmed" | "already_confirmed"}` -- no economic field.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("submit")
    assert_same_company(so)

    if so.docstatus == 2:
        frappe.throw(
            _("Este pedido está cancelado y no puede confirmarse."), SalesOrderAlreadyCancelledError
        )

    if so.docstatus == 1:
        return {"name": so.name, "docstatus": 1, "status": "already_confirmed"}

    if not so.customer:
        frappe.throw(_("El pedido debe tener un cliente."))
    if not so.items:
        frappe.throw(_("El pedido debe tener al menos un producto."))
    for row in so.items:
        if flt(row.qty) <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(row.item_code))

    so.submit()  # triggers on_submit -> process_sales_order_for_confirmation() (Commit 25.4)

    return {"name": so.name, "docstatus": 1, "status": "confirmed"}


@frappe.whitelist()
def get_editable_order(name):
    """Prefill data for the "Editar pedido" view (Commit 18.5) -- reuses
    `get_order_detail()`'s own exact response shape verbatim (same
    allowlist, same field-by-field construction, same static guardrail
    in `test_regression.py`), since editing reuses the identical "Nuevo
    Pedido" screen just prefilled. `check_permission("read")` +
    `assert_same_company()` (Commit 25.1) are where access is actually
    enforced -- role + same Company, no longer ownership; the
    `docstatus` check is the read-side half of the same "Draft only"
    boundary `update_draft_sales_order()` enforces independently on the
    write side below.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("read")
    assert_same_company(so)

    if so.docstatus != 0:
        frappe.throw(_("Solo se pueden editar pedidos en borrador."))

    return get_order_detail(name)


@frappe.whitelist()
def update_draft_sales_order(name, customer, items, observations=None):
    """Edits one of Vendedora's own Draft Sales Orders in place (Commit
    18.5) -- `customer`, `items` (`item_code`/`qty` only, via the exact
    same `_validate_and_build_item_rows()` allowlist
    `create_and_submit_sales_order()` uses -- one shared security
    boundary, not two independently-maintained copies), and
    `fg_observations`. Replaces the entire item list rather than
    patching individual rows -- exactly what "Editar pedido" reusing the
    "Nuevo Pedido" screen naturally produces (she rebuilds her cart from
    the prefilled state, the same UI flow as creating a new order).
    Never submits -- "GUARDAR CAMBIOS" is deliberately not "CONFIRMAR
    PEDIDO"; the only path that ever triggers the Fulfillment Engine is
    `create_and_submit_sales_order()`'s own `.submit()` call (Commit 16's
    `on_submit` hook), never reached from here.

    `check_permission("write")` + `assert_same_company()` (Commit 25.1)
    are where access is actually enforced -- role + same Company, no
    longer ownership. `docstatus == 0` is required explicitly, throwing
    a clear, specific message -- ERPNext's own docstatus-transition
    guard would eventually reject writing to a submitted document too,
    but only after doing more work first.

    Returns `{"name": "SAL-ORD-..."}` only -- no economic field, no
    Fulfillment Engine artifact name, matching every other write in this
    module.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("write")
    assert_same_company(so)

    if so.docstatus != 0:
        frappe.throw(_("Solo se pueden editar pedidos en borrador."))

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)

    so.customer = customer
    so.set("items", [])
    for row in so_items:
        so.append("items", row)
    if observations is not None:
        so.fg_observations = observations

    so.save()  # no ignore_permissions -- her real role+Company write permission already covers this

    return {"name": so.name}


@frappe.whitelist()
def delete_draft_sales_order(name):
    """Deletes a Draft Sales Order of this Company (Commit 18.5; role +
    Company since Commit 25.1, no longer ownership).

    `check_permission("delete")` + `assert_same_company()` are where
    access is actually enforced -- `delete=1` on Vendedora's Custom
    DocPerm row grants the base right, `assert_same_company()` keeps it
    scoped to this Company. `docstatus == 0` is required explicitly,
    matching the native rule that a submitted document can never be
    deleted (Frappe's own `check_permission_and_not_submitted()` would
    reject it too, but this throws a specific, clear message first).
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("delete")
    assert_same_company(so)

    if so.docstatus != 0:
        frappe.throw(_("Solo se pueden eliminar pedidos en borrador."))

    frappe.delete_doc("Sales Order", name)  # no ignore_permissions

    return {"name": name, "deleted": True}


@frappe.whitelist()
def cancel_sales_order(name):
    """Cancels a submitted Sales Order of this Company (Commit 18.5;
    role + Company since Commit 25.1, no longer ownership).

    `check_permission("cancel")` + `assert_same_company()` are where
    access is actually enforced. `so.cancel()` is called with
    no bypass of any kind: ERPNext's own native back-link protection (a
    submitted Pick List/Material Request/Purchase Order still
    referencing this order, Commits 17/19.3) runs exactly as it does for
    any other Sales Order in this app, and its real error (e.g.
    `LinkExistsError`) propagates to the caller unmodified -- never
    caught, swallowed, or worked around here. The existing on_submit/
    on_cancel hooks (Commits 16/17/19.2/19.3) do every bit of Fulfillment
    cleanup exactly as they already do for a cancellation triggered any
    other way; this function calls no Fulfillment Engine internal
    directly -- `.cancel()` alone is what triggers
    `cleanup_fulfillment_for_cancelled_sales_order()`, the same standing
    "don't call the Engine directly if submit/cancel already does" rule
    `create_and_submit_sales_order()` already follows for submit.

    `docstatus == 1` is required explicitly, throwing a clear, specific
    message for an already-Draft or already-Cancelled order rather than
    letting ERPNext's own docstatus-transition error surface instead.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")
    assert_same_company(so)

    if so.docstatus != 1:
        frappe.throw(_("Solo se pueden cancelar pedidos sometidos."))

    so.cancel()  # no ignore_permissions -- native back-link checks apply unmodified

    return {"name": name, "status": "Cancelled"}


@frappe.whitelist()
def get_modification_status(name):
    """Cheap, read-only pre-check for whether "MODIFICAR PEDIDO" should be
    shown/enabled in the UI (Commit 18.5) -- NOT authoritative.
    `modify_submitted_sales_order()` re-derives this exact same check
    itself, independently, immediately before acting; nothing here is ever
    trusted as a substitute for that.

    `check_permission("cancel")` -- not "read" -- because that is the
    exact grant `modify_submitted_sales_order()` itself requires (cancel+
    amend starts with `so.cancel()`); using the same permission +
    `assert_same_company()` here means this pre-check can never say
    "yes" for an order the real operation would then reject.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")
    assert_same_company(so)

    if so.docstatus != 1:
        return {"modifiable": False, "blockers": ["not_submitted"]}

    blockers = modification_blockers_for(name)
    return {"modifiable": not blockers, "blockers": blockers}


@frappe.whitelist()
def get_order_for_modification(name):
    """Prefill for "MODIFICAR PEDIDO" (Commit 18.5) -- reuses
    `get_order_detail()`'s own exact response shape verbatim, exactly like
    `get_editable_order()` does for Draft. Unlike the pre-check above,
    this re-derives `modification_blockers_for()` authoritatively and
    refuses outright if anything blocks -- a stale UI state (button was
    enabled when the card last rendered, but Bodega started picking a
    moment ago) can never even reach the prefill screen.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")
    assert_same_company(so)

    if so.docstatus != 1:
        frappe.throw(_("Solo se pueden modificar pedidos sometidos."))

    if modification_blockers_for(name):
        frappe.throw(
            _("Este pedido ya no puede modificarse: Bodega ya inició el alistamiento u otro proceso relacionado.")
        )

    return get_order_detail(name)


@frappe.whitelist()
def modify_submitted_sales_order(name, customer, items, observations=None):
    """Commit 18.5 -- modifies a submitted Sales Order via ERPNext's own
    native cancel+amend, never by touching a single field or child row of
    the submitted document directly.

    Sequence: validate the new `items` payload FIRST (fail fast, before
    anything is cancelled) -> `so.cancel()` (no bypass; triggers the
    existing `cleanup_fulfillment_for_cancelled_sales_order()` on_cancel
    hook exactly as `cancel_sales_order()` already does) ->
    `frappe.copy_doc(so, ignore_no_copy=False)` (confirmed by reading
    `frappe/model/document.py` directly: with `ignore_no_copy=False` this
    also clears every `no_copy`-flagged field -- `picked_qty`,
    `delivered_qty`, `billed_amt`, `requested_qty`, etc. -- on the new
    document and its child rows, so the amended version never carries
    over fulfillment progress from the one just cancelled; `copy_doc()`
    itself already clears `amended_from`, `name`, `docstatus`, `owner`) ->
    `amended_from` is set explicitly (`copy_doc()` clears it by default,
    same as ERPNext's own client-side "Amend" button then re-sets it) ->
    the new customer/items/observations are applied via the exact same
    `_validate_and_build_item_rows()` allowlist every other write in this
    module uses -> `.insert()` + `.submit()` (triggers `on_submit` ->
    `process_sales_order_for_confirmation()` fresh, Commit 25.4, exactly
    like `create_and_submit_sales_order()`/`confirm_order()` -- never
    called directly here).

    `check_permission("cancel")` + `assert_same_company()` are where
    access is enforced -- the exact same grant `cancel_sales_order()`
    already relies on, no new Custom DocPerm needed. The authoritative
    gate is
    `modification_blockers_for()`, re-derived here (never trusted from
    `get_modification_status()`/`get_order_for_modification()`, both of
    which the client may have called any amount of time before this
    request actually lands): if Bodega started picking, a Pick List or
    Material Request got submitted, a Purchase Order is linked, or a
    Delivery Note/Sales Invoice exists, this throws a clear message and
    the Sales Order is never touched -- `so.cancel()` is not even
    attempted, so there is no risk of a half-cancelled state to recover
    from.

    Naming (see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 18.5 -- naming"):
    the returned `name` is the new technical document name (e.g.
    `PEDIDO-1-1`) -- Frappe's own amend mechanism cannot preserve the
    literal original name. `commercial_name` is the stable "PEDIDO-1"
    identity `get_my_orders()`/`get_order_detail()` already show
    throughout the UI, resolved via `root_commercial_name()`.

    Returns `{"name": "PEDIDO-1-1", "commercial_name": "PEDIDO-1"}` only --
    no economic field, matching every other write in this module.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")
    assert_same_company(so)

    if so.docstatus != 1:
        frappe.throw(_("Solo se pueden modificar pedidos sometidos."))

    if modification_blockers_for(name):
        frappe.throw(
            _("Este pedido ya no puede modificarse: Bodega ya inició el alistamiento u otro proceso relacionado.")
        )

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)  # fail fast -- nothing cancelled yet

    commercial_name = root_commercial_name(name)

    so.cancel()  # no ignore_permissions -- triggers cleanup_fulfillment_for_cancelled_sales_order() via on_cancel

    amended = frappe.copy_doc(so, ignore_no_copy=False)
    # frappe.copy_doc() only clears docstatus when NOT frappe.in_test (see
    # frappe/model/document.py) -- confirmed live, not assumed, while
    # writing this commit's own tests: under IntegrationTestCase that flag
    # is always true, so `so`'s already-cancelled docstatus=2 would
    # otherwise be copied verbatim onto the new, still-unsaved document,
    # making .insert() below fail with DocstatusTransitionError. Set
    # explicitly here so behaviour is identical and correct in both a real
    # request and a test, never dependent on that environment flag.
    amended.docstatus = 0
    amended.amended_from = name
    amended.customer = customer
    amended.set("items", [])
    for row in so_items:
        amended.append("items", row)
    if observations is not None:
        amended.fg_observations = observations

    amended.insert()
    amended.submit()  # triggers on_submit -> process_sales_order_for_confirmation() (Commit 25.4)

    return {"name": amended.name, "commercial_name": commercial_name}
