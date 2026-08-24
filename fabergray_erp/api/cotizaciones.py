# -*- coding: utf-8 -*-
"""Commit 20.2 -- Fase 5 (Cotizaciones). Read-only API layer for the future
Page Cotizaciones (Commit 20.5), used by the Vendedora role. Every read
goes through `frappe.get_list()`/`frappe.get_doc()`+`check_permission()`,
never `frappe.get_all()`, never `frappe.set_user()`, never
`ignore_permissions=True` -- Vendedora always operates with her own real,
restricted `if_owner=1` permission on Quotation (Commit 20.1). No Pick
List/Reporte de Faltante/Fulfillment Engine involvement at all -- this
module has nothing to bypass a permission for, unlike api/ventas.py.

`search_customers()`/`search_items()` are NOT duplicated here -- both are
already generic, carry nothing Sales-Order-specific, and are already
`@frappe.whitelist()`-ed in `api/ventas.py`. Page Cotizaciones calls
`fabergray_erp.api.ventas.search_customers`/`search_items` directly.

Fundamental rule this whole module exists to enforce (per the user's
explicit approval of the Fase 5 architecture): Vendedora never sees a
price, a discount, a tax amount, or a total on a Quotation either --
same policy as `api/ventas.py`. Every function below either omits
economic fields entirely from its response (built field by field, never
`.as_dict()`), or -- for `create_and_submit_quotation()`, Commit 20.3 --
will explicitly reject any economic field the caller tries to send.
ERPNext's own native pricing engine resolves `rate`/`price_list_rate`/
taxes/`grand_total` server-side, automatically, the moment the Quotation
is inserted -- nothing here duplicates or second-guesses that.

Stock/inventory is out of scope entirely for this module, by design (the
user's explicit instruction) -- unlike `api/ventas.py`'s `get_item_info()`,
this module's own `get_item_info()` never reads `Bin`/`get_actual_qty()`.
"""

import frappe
from frappe.utils import cint, nowdate

from fabergray_erp.api.bodega import _require_login

# The only two fields a Quotation Item line may carry in from the client
# (Commit 20.3, `create_and_submit_quotation()`). Declared here, not just
# in 20.3, so it is visible from the top of this module as the standing
# rule the whole file exists to enforce -- not yet enforced by any
# function in this commit (Commit 20.2 has no write endpoint at all).
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
