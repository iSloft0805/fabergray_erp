# -*- coding: utf-8 -*-
"""Commits 20.2-20.6 -- Fase 5 (Cotizaciones). API layer for Page
Cotizaciones (Commit 20.5), used by the Vendedora role. Every read goes
through `frappe.get_list()`/`frappe.get_doc()`+`check_permission()`, and
every write (`create_and_submit_quotation()`, Commit 20.3;
`update_draft_quotation()`, Commit 20.6) goes through a plain
`.insert()`/`.submit()`/`.save()` under Vendedora's own real session --
never `frappe.get_all()`, never `frappe.set_user()`, never
`ignore_permissions=True`, never `db_set`/`frappe.db.set_value` to skip a
validation. Vendedora always operates with her own real, restricted
`if_owner=1` permission on Quotation (Commit 20.1). No Pick List/Reporte
de Faltante/Fulfillment Engine/Sales Order/Material Request involvement
at all -- this module has nothing to bypass a permission for, unlike
api/ventas.py.

`search_customers()`/`search_items()` are NOT duplicated here -- both are
already generic, carry nothing Sales-Order-specific, and are already
`@frappe.whitelist()`-ed in `api/ventas.py`. Page Cotizaciones calls
`fabergray_erp.api.ventas.search_customers`/`search_items` directly.

Fundamental rule this whole module exists to enforce (per the user's
explicit approval of the Fase 5 architecture): Vendedora never sees or
sends a price, a discount, a tax amount, or a total on a Quotation either
-- same policy as `api/ventas.py`. Every read function below omits
economic fields entirely from its response (built field by field, never
`.as_dict()`); `create_and_submit_quotation()` explicitly rejects any
economic field the caller tries to send, via a strict per-line allowlist
(`_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}`). ERPNext's own native
pricing engine resolves `rate`/`price_list_rate`/taxes/`grand_total`
server-side, automatically, the moment the Quotation is inserted --
nothing here duplicates or second-guesses that.

Stock/inventory is out of scope entirely for this module, by design (the
user's explicit instruction) -- unlike `api/ventas.py`'s `get_item_info()`,
this module's own `get_item_info()` never reads `Bin`/`get_actual_qty()`.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from fabergray_erp.api.bodega import _require_login

# The only two fields a Quotation Item line may carry in from the client,
# enforced by `_validate_and_build_quotation_item_rows()` below (Commit
# 20.3) -- any other key (`rate`, `price_list_rate`, `discount_percentage`,
# `discount_amount`, `amount`, `net_rate`, `net_amount`,
# `margin_rate_or_amount`, `margin_type`, `currency`, `conversion_rate`,
# `taxes`, `total`/`grand_total`, or anything else, present or future) is
# rejected outright, never silently dropped.
_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}


@frappe.whitelist()
def get_item_info(item_code):
    """Operational detail for one product, for the "Nueva Cotización" cart
    line -- an explicitly-built dict of only the allowed keys, never a
    forwarded `get_item_details()` response or `Item.as_dict()` (both of
    which carry pricing fields this function must never expose, by
    construction, not by after-the-fact filtering).

    No `qty_disponible`/`Bin`/`get_actual_qty()` here at all -- unlike
    `api/ventas.py`'s own `get_item_info()`, inventory has no role in a
    Quotation by explicit design (the user's own instruction: "El
    inventario puede incluso omitirse completamente de esta interfaz").
    """
    _require_login()

    item = frappe.get_doc("Item", item_code)
    item.check_permission("read")

    return {
        "item_code": item.item_code,
        "item_name": item.item_name,
        "description": item.description,
        "stock_uom": item.stock_uom,
        "image": item.image,
    }


@frappe.whitelist()
def get_quotation_summary():
    """KPI counts for the Page Cotizaciones dashboard header -- derived
    exclusively from `Quotation.status`/`transaction_date`'s own native
    values, scoped to Vendedora's own quotations via `if_owner=1`
    (`frappe.get_list()`, never `get_all()`).

    Native `status` values (`quotation.json`): Draft, Open, Replied,
    Partially Ordered, Ordered, Lost, Cancelled, Expired.
    - `cotizaciones_hoy`: `transaction_date == hoy` (any status).
    - `pendientes`: `status == "Open"` (submitted, no Sales Order made
      from it yet).
    - `aprobadas`: `status in ("Ordered", "Partially Ordered")` -- native,
      derived by ERPNext's own `get_ordered_status()` from submitted Sales
      Order Item rows referencing this Quotation. Will read 0 until the
      future Quotation -> Sales Order conversion phase exists (out of
      scope here) -- included now anyway since it is zero-cost and
      already correct the day that phase ships.
    - `vencidas`: `status == "Expired"` -- set automatically, daily, by
      ERPNext's own already-active scheduled job
      (`erpnext.selling.doctype.quotation.quotation.set_expired_status`),
      zero scheduling of our own required.
    """
    _require_login()
    frappe.has_permission("Quotation", "read", throw=True)

    base_filters = {"owner": frappe.session.user}

    cotizaciones_hoy = frappe.get_list(
        "Quotation", filters={**base_filters, "transaction_date": nowdate()}, pluck="name"
    )
    pendientes = frappe.get_list("Quotation", filters={**base_filters, "status": "Open"}, pluck="name")
    aprobadas = frappe.get_list(
        "Quotation", filters={**base_filters, "status": ["in", ["Ordered", "Partially Ordered"]]}, pluck="name"
    )
    vencidas = frappe.get_list("Quotation", filters={**base_filters, "status": "Expired"}, pluck="name")

    return {
        "cotizaciones_hoy": len(cotizaciones_hoy),
        "pendientes": len(pendientes),
        "aprobadas": len(aprobadas),
        "vencidas": len(vencidas),
    }


@frappe.whitelist()
def get_my_quotations(limit=50):
    """Vendedora's own Quotations, most recent first -- operational fields
    only (number, customer, dates, status, line/unit counts, terms),
    never a rate/amount/total. Scoped to her own quotations exclusively
    through the native `if_owner=1` Custom DocPerm (Commit 20.1) --
    `frappe.get_list()` applies that automatically; an explicit `owner`
    filter is added below anyway for defensive clarity, same convention
    as `api/ventas.py.get_my_orders()`. `frappe.get_all()` is never used
    here.

    Per-quotation line/unit counts are read by loading each of her own,
    already-authorized Quotations individually (`frappe.get_doc()` +
    `len(qtn.items)`) rather than a batched query against the Quotation
    Item child table -- same reasoning as `get_my_orders()`: child
    doctypes have no permission model of their own, so a batched
    child-table read would not reliably respect `if_owner`. `total_qty`
    (a native, top-level Quotation field, purely a unit count -- not
    money) supplies the unit count without a child-table read.

    `terms` (a native `Text Editor` field, NOT a Custom Field -- unlike
    Sales Order's `fg_observations`, Quotation already ships this field
    natively) is returned as `observations`, matching the key name Page
    Ventas already uses, so the future Page Cotizaciones (Commit 20.5)
    can reuse the same rendering logic. Covered by the exact same
    if_owner=1 Quotation permission already checked above -- no separate
    permission needed.

    No amend-chain walk-back here (unlike `get_my_orders()`'s
    `commercial_name`/superseded-amendment skip) -- Quotation/Vendedora's
    `amend=0` (Commit 20.1) means she can never amend one of her own
    quotations in the first place, so that whole concern does not apply.
    """
    _require_login()
    frappe.has_permission("Quotation", "read", throw=True)

    names = frappe.get_list(
        "Quotation",
        filters={"owner": frappe.session.user},
        fields=["name"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=0,
        pluck="name",
    )

    max_quotations = cint(limit) or 50
    quotations = []
    for name in names:
        if len(quotations) >= max_quotations:
            break

        qtn = frappe.get_doc("Quotation", name)
        qtn.check_permission("read")

        quotations.append(
            {
                "name": qtn.name,
                "customer": qtn.party_name,
                "customer_name": qtn.customer_name,
                "transaction_date": qtn.transaction_date,
                "valid_till": qtn.valid_till,
                "status": qtn.status,
                "item_count": len(qtn.items),
                "total_qty": qtn.total_qty,
                "observations": qtn.terms,
            }
        )

    return quotations


@frappe.whitelist()
def get_quotation_detail(name):
    """Line-level detail for one of Vendedora's own Quotations -- the "VER
    COTIZACIÓN" view in the future Page Cotizaciones. Same permission
    model as every other read in this module: `get_doc()` +
    `check_permission("read")`, which is where `if_owner=1` (Commit 20.1)
    is actually enforced -- a different Vendedora's own quotation raises
    `PermissionError` here exactly like it does everywhere else in this
    module.

    The response is built field by field, never `qtn.as_dict()` or
    `row.as_dict()` (both of which carry every economic field on the
    document) -- there is nothing to filter after the fact because
    nothing economic is ever read into a variable here in the first
    place. See `test_regression.py` for the static guardrail that keeps
    this true if this function is ever touched again.
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("read")

    return {
        "name": qtn.name,
        "customer": qtn.party_name,
        "customer_name": qtn.customer_name,
        "transaction_date": qtn.transaction_date,
        "valid_till": qtn.valid_till,
        "status": qtn.status,
        "item_count": len(qtn.items),
        "total_qty": qtn.total_qty,
        "observations": qtn.terms,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty,
                "stock_uom": row.stock_uom,
            }
            for row in qtn.items
        ],
    }


def _validate_and_build_quotation_item_rows(items):
    """The one place a Quotation Item row list is built from
    client-supplied data. Rejects any line carrying a key outside
    `_ALLOWED_ITEM_FIELDS` -- `rate`, `price_list_rate`,
    `discount_percentage`, `discount_amount`, `amount`, `net_rate`,
    `net_amount`, `margin_rate_or_amount`, `margin_type`, `currency`,
    `conversion_rate`, `taxes`, `total`, or anything else, present or
    future -- never silently dropped. No warehouse, no delivery_date, no
    stock check of any kind (unlike `ventas._validate_and_build_item_rows()`)
    -- inventory has no role in a Quotation, by explicit design."""
    items = frappe.parse_json(items) if isinstance(items, str) else items
    if not items:
        frappe.throw(_("La cotización debe tener al menos un producto."))

    qtn_items = []
    for row in items:
        if not isinstance(row, dict):
            frappe.throw(_("Formato de línea de cotización inválido."))

        disallowed = set(row.keys()) - _ALLOWED_ITEM_FIELDS
        if disallowed:
            frappe.throw(
                _("Campos no permitidos en la línea de la cotización: {0}").format(", ".join(sorted(disallowed)))
            )

        if "item_code" not in row or "qty" not in row:
            frappe.throw(_("Cada línea de la cotización debe incluir item_code y qty."))

        item_code = row["item_code"]
        qty = flt(row["qty"])

        if not frappe.db.exists("Item", item_code):
            frappe.throw(_("El producto {0} no existe.").format(item_code))
        if qty <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(item_code))

        qtn_items.append({"item_code": item_code, "qty": qty})

    return qtn_items


@frappe.whitelist()
def create_and_submit_quotation(customer, items, valid_till=None, terms=None):
    """The critical operation: build and submit a standard Quotation from
    exactly what Vendedora is allowed to send -- `customer`, an optional
    `valid_till`/`terms`, and, per line, `item_code`/`qty` only -- and let
    `.insert()` run ERPNext's own native pipeline
    (`SellingController.validate()` -> `set_missing_values()` ->
    `calculate_taxes_and_totals()`) to resolve pricing/taxes/totals.
    Nothing in this function reads or writes a price field, queries `Item
    Price`, or calls any inventory helper (`get_actual_qty()`/`Bin`/Pick
    List/the Fulfillment Analyzer) -- a Quotation may be submitted
    regardless of stock level, by explicit design.

    Native field names used to build the document (confirmed against the
    installed ERPNext 16.32.1 `Quotation` doctype, and already proven live
    across every test in Commits 20.1/20.2 -- every one of those tests
    inserts and submits a real Quotation with exactly this shape):
    `quotation_to` (hard-set to `"Customer"`, never accepted from the
    client -- Vendedora only ever quotes an existing Customer),
    `party_name` (Dynamic Link, the actual customer -- there is no plain
    `customer` field on Quotation, unlike Sales Order), `company`,
    `items` (child table `Quotation Item`), `valid_till` (Date, optional,
    native `validate_valid_till()` rejects a value before
    `transaction_date` -- not re-validated here, ERPNext's own check is
    sufficient), `terms` (`Text Editor`, optional, NATIVE field -- not a
    Custom Field, see `get_my_quotations()`'s docstring). `currency`,
    `conversion_rate`, `selling_price_list`, `taxes_and_charges`,
    `order_type`, `transaction_date` are all left unset here and resolve
    to their own native defaults during `.insert()` -- exactly as already
    proven by every Commit 20.1/20.2 test, none of which sets them either.

    Security: each line is checked against `_ALLOWED_ITEM_FIELDS =
    {"item_code", "qty"}` via `_validate_and_build_quotation_item_rows()`
    -- any other key makes this function raise `frappe.ValidationError`
    immediately, before any Quotation is even constructed.

    No `ignore_permissions`, no `frappe.get_all`, no `frappe.set_user`, no
    `db_set`/`frappe.db.set_value` anywhere in this function -- `owner`
    ends up as `frappe.session.user` purely because `.insert()` runs
    under Vendedora's own real, unmodified session, and she can read the
    result back afterward only because of the real `if_owner=1` Custom
    DocPerm grant from Commit 20.1, not because of anything special done
    here.

    Returns only non-economic fields -- never `grand_total`,
    `rounded_total`, `total`, `taxes`, `rate`, or `amount`, all of which
    ERPNext computed internally and this function never reads.
    """
    _require_login()
    frappe.has_permission("Quotation", "create", throw=True)

    company = frappe.defaults.get_global_default("company")
    qtn_items = _validate_and_build_quotation_item_rows(items)

    qtn = frappe.get_doc(
        {
            "doctype": "Quotation",
            "quotation_to": "Customer",
            "party_name": customer,
            "company": company,
            "items": qtn_items,
        }
    )
    if valid_till:
        qtn.valid_till = valid_till
    if terms:
        qtn.terms = terms

    qtn.insert()
    qtn.submit()

    return {
        "name": qtn.name,
        "status": qtn.status,
        "customer": qtn.party_name,
        "customer_name": qtn.customer_name,
        "transaction_date": qtn.transaction_date,
        "valid_till": qtn.valid_till,
        "item_count": len(qtn.items),
        "total_qty": qtn.total_qty,
    }


@frappe.whitelist()
def get_editable_quotation(name):
    """Prefill data for the "Editar cotización" view (Commit 20.6) --
    reuses `get_quotation_detail()`'s own exact response shape verbatim
    (same allowlist, same field-by-field construction, same static
    guardrail in `test_regression.py`), since editing reuses the
    identical "Nueva Cotización" screen just prefilled. The one thing
    added on top: only a Draft quotation can be prefilled for editing
    here -- `check_permission("read")` (if_owner=1, Commit 20.1) is where
    ownership is actually enforced, exactly like every other read in this
    module; the `docstatus` check is the read-side half of the same
    "Draft only" boundary `update_draft_quotation()` enforces
    independently on the write side below.
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("read")

    if qtn.docstatus != 0:
        frappe.throw(_("Solo se pueden editar cotizaciones en borrador."))

    return get_quotation_detail(name)


@frappe.whitelist()
def update_draft_quotation(name, customer, items, valid_till=None, terms=None):
    """Edits one of Vendedora's own Draft Quotations in place (Commit
    20.6) -- `customer`, `items` (`item_code`/`qty` only, via the exact
    same `_validate_and_build_quotation_item_rows()` allowlist
    `create_and_submit_quotation()` uses, Commit 20.3 -- one shared
    security boundary, not two independently-maintained copies), and
    `valid_till`/`terms`. Replaces the entire item list rather than
    patching individual rows -- exactly what "Editar cotización" reusing
    the "Nueva Cotización" screen naturally produces (she rebuilds her
    cart from the prefilled state, the same UI flow as creating a new
    quotation).

    Never submits -- "GUARDAR CAMBIOS" is deliberately not "CREAR
    COTIZACIÓN"; `.save()` alone lets ERPNext's own native pipeline
    (`SellingController.validate()` -> `set_missing_values()` ->
    `calculate_taxes_and_totals()`) re-resolve pricing/taxes/totals
    internally, exactly as it already does on insert -- nothing here
    reads or writes a price field, and the response below never returns
    one either.

    `check_permission("write")` is where if_owner=1 (Commit 20.1) is
    actually enforced -- a different Vendedora's own quotation raises
    `PermissionError` here exactly like `get_quotation_detail()`/
    `get_my_quotations()` already do for read. `docstatus == 0` is
    required explicitly, throwing a clear, specific message -- ERPNext's
    own docstatus-transition guard would eventually reject writing to a
    submitted document too, but only after doing more work first, and
    with a more generic message.

    Nothing here ever touches the document's naming series field, `owner`,
    `status`, `docstatus`, `currency`, `selling_price_list`, `rate`,
    `taxes`, or any discount/total field -- this function has no
    parameter that could carry any of them in, and
    `_validate_and_build_quotation_item_rows()` rejects
    any attempt to smuggle one through a line.

    Returns only `{"name": qtn.name}` -- no economic field, matching
    `update_draft_sales_order()`'s own minimal response shape (Commit
    18.5). (Deliberately avoids spelling out the Quotation naming series'
    own prefix anywhere in this docstring -- Commit 20.4's own guardrail
    confirms this module's source never contains it.)
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("write")

    if qtn.docstatus != 0:
        frappe.throw(_("Solo se pueden editar cotizaciones en borrador."))

    qtn_items = _validate_and_build_quotation_item_rows(items)

    qtn.party_name = customer
    qtn.set("items", [])
    for row in qtn_items:
        qtn.append("items", row)
    if valid_till is not None:
        qtn.valid_till = valid_till or None
    if terms is not None:
        qtn.terms = terms

    qtn.save()  # no ignore_permissions -- her real if_owner=1 write permission already covers this

    return {"name": qtn.name}
