# -*- coding: utf-8 -*-
"""Commit 18.2 -- API layer for the future Page Ventas (Commit 18.3), used
by the Vendedora role.

Same conventions as api/bodega.py and api/jefe_bodega.py: every read goes
through `frappe.get_list`/`frappe.get_doc()` + `check_permission()`, never
`frappe.get_all` and never `ignore_permissions=True` -- Vendedora always
operates with her own real, restricted permissions (Commit 18.1: read on
Customer/Item/Address/Contact, `select` on Account, create/read/write/
submit on Sales Order scoped `if_owner=1`, nothing on Pick List or
Reporte de Faltante). The one narrow, documented exception approved in
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


def _default_warehouse_for_item(item_code, company):
    """The one place `create_and_submit_sales_order()` and `get_item_info()`
    both resolve which warehouse a line uses -- Vendedora never picks one
    herself (Commit 18's approved design: "La vendedora no debe poder
    modificar configuración... de stock"). Reads the native `Item Default`
    child table (Item.item_defaults) for the resolved company -- the same
    metadata ERPNext's own `get_item_details()` consults for its own
    per-item warehouse default -- not a new concept invented here.
    `frappe.db.get_value` is a raw, single-value read (not `get_all`), the
    same kind already used throughout this app's Fulfillment Engine."""
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
    for this Page.
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


@frappe.whitelist()
def get_my_orders(limit=50):
    """Vendedora's own Sales Orders, most recent first -- operational
    fields only (number, customer, dates, status, line/unit counts,
    observations), never a total or a price. Scoped to her own orders
    exclusively through the native `if_owner=1` Custom DocPerm (Commit
    18.1) -- `frappe.get_list()` applies that automatically; no manual
    `owner` filter is required for correctness, though one is added below
    anyway for defensive clarity. `frappe.get_all()` is never used here.

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
    """
    _require_login()
    frappe.has_permission("Sales Order", "read", throw=True)

    names = frappe.get_list(
        "Sales Order",
        filters={"owner": frappe.session.user},
        fields=["name"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=cint(limit) or 50,
        pluck="name",
    )

    orders = []
    for name in names:
        so = frappe.get_doc("Sales Order", name)
        so.check_permission("read")

        orders.append(
            {
                "name": so.name,
                "customer": so.customer,
                "customer_name": so.customer_name,
                "transaction_date": so.transaction_date,
                "delivery_date": so.delivery_date,
                "status": so.status,
                "item_count": len(so.items),
                "total_qty": so.total_qty,
                "observations": so.fg_observations,
            }
        )

    return orders


@frappe.whitelist()
def get_sales_summary():
    """KPI counts for the Page Ventas dashboard header -- derived
    exclusively from Sales Order/Sales Order Item's own native fields,
    scoped to Vendedora's own orders via `if_owner=1`
    (`frappe.get_list()`, never `get_all()`). Deliberately only the four
    states directly derivable from `Sales Order.status`/`transaction_date`
    (confirmed native values: Draft, On Hold, To Pay, To Deliver and
    Bill, To Bill, To Deliver, Completed, Cancelled, Closed) -- no
    "Alistamiento iniciado" or similar experimental bucket yet, and no
    bucket that would require Pick List/Reporte de Faltante access
    (Commit 18's approved Option B)."""
    _require_login()
    frappe.has_permission("Sales Order", "read", throw=True)

    base_filters = {"owner": frappe.session.user}

    pedidos_hoy = frappe.get_list(
        "Sales Order", filters={**base_filters, "transaction_date": nowdate()}, pluck="name"
    )
    pendientes = frappe.get_list(
        "Sales Order",
        filters={**base_filters, "status": ["in", ["To Deliver and Bill", "To Deliver"]]},
        pluck="name",
    )
    entregados = frappe.get_list("Sales Order", filters={**base_filters, "status": "Completed"}, pluck="name")
    cancelados = frappe.get_list("Sales Order", filters={**base_filters, "status": "Cancelled"}, pluck="name")

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
    (`_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}`) -- any other key
    (`rate`, `price_list_rate`, `discount_percentage`, `discount_amount`,
    `amount`, `net_rate`, `net_amount`, `margin_rate_or_amount`,
    `margin_type`, or anything else, present or future) makes this
    function raise immediately, before any Sales Order is even
    constructed -- never silently dropped. The Sales Order Item rows
    built from the surviving fields carry `item_code`/`qty`/`warehouse`/
    `delivery_date` only; ERPNext's own `AccountsController.validate()`
    (`set_missing_values()` then `calculate_taxes_and_totals()`) resolves
    every price/discount/tax field itself, unconditionally, the moment
    `.insert()` runs -- there is no pricing logic in this function to
    duplicate or get wrong.

    `observations`, if given, is stored in `Sales Order.fg_observations` --
    a Custom Field (fixtures/custom_field.json), not `add_comment()`.
    `add_comment()` was the first choice, but it turned out to create a
    read-side permission gap: `Document.add_comment()` always inserts with
    `ignore_permissions=True` (universal Frappe behaviour, every doctype),
    so *writing* the observation never had a problem -- but *reading* it
    back in `get_my_orders()` does, because the base `Comment` doctype's
    own native permission model only grants `read` to `System Manager`/
    `Website Manager`, which Vendedora is neither. Rather than requesting
    a new Comment permission for her (out of scope for the if_owner=1
    Sales Order grant already approved in Commit 18.1), `fg_observations`
    is read and written through the exact same if_owner=1 Sales Order
    permission she already has -- no new permission of any kind.

    Warehouse is resolved server-side per item
    (`_default_warehouse_for_item()`) -- Vendedora never chooses one,
    matching the approved design that she cannot touch stock
    configuration.

    Returns `{"name": "SAL-ORD-..."}` only -- no total, no price, nothing
    the Fulfillment Engine produced (Pick List/Reporte de Faltante
    names): she has no permission to read either anyway, and the
    dashboard/`get_my_orders()` above are the intended way to see what
    happened to a submitted order.
    """
    _require_login()
    frappe.has_permission("Sales Order", "create", throw=True)

    items = frappe.parse_json(items) if isinstance(items, str) else items
    if not items:
        frappe.throw(_("El pedido debe tener al menos un producto."))

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)

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

        warehouse = _default_warehouse_for_item(item_code, company)
        if not warehouse:
            frappe.throw(_("El producto {0} no tiene una bodega por defecto configurada.").format(item_code))

        so_items.append(
            {
                "item_code": item_code,
                "qty": qty,
                "warehouse": warehouse,
                "delivery_date": delivery_date,
            }
        )

    so = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "customer": customer,
            "company": company,
            "transaction_date": nowdate(),
            "delivery_date": delivery_date,
            "set_warehouse": so_items[0]["warehouse"],
            "items": so_items,
        }
    )
    if observations:
        so.fg_observations = observations

    so.insert()
    so.submit()  # triggers on_submit -> process_sales_order() -- never called directly here

    return {"name": so.name}
