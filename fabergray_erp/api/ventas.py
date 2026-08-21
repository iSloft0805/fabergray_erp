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
from fabergray_erp.fulfillment.modification_service import modification_blockers_for

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


def _validate_and_build_item_rows(items, company, delivery_date):
    """The one place a Sales Order Item row list is built from
    client-supplied data -- shared by `create_and_submit_sales_order()`
    and `update_draft_sales_order()` (Commit 18.5), extracted so both
    entry points that accept an `items` payload from Vendedora enforce
    the exact same allowlist/validation, never two parallel copies of
    the same security boundary. Behavior is byte-for-byte identical to
    what `create_and_submit_sales_order()` already had since Commit 18.2
    -- this refactor changes nothing about what is accepted or rejected,
    only where the code lives.

    Rejects any line carrying a key outside `_ALLOWED_ITEM_FIELDS`
    (`rate`, `price_list_rate`, `discount_percentage`, `discount_amount`,
    `amount`, `net_rate`, `net_amount`, `margin_rate_or_amount`,
    `margin_type`, or anything else, present or future) -- never
    silently dropped. Warehouse is resolved server-side per item
    (`_default_warehouse_for_item()`); `delivery_date` is the caller's
    single, already-resolved value, applied uniformly to every line.
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

    return so_items


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


def _root_commercial_name(so_name):
    """Walks the native `amended_from` chain backward to the original
    document name -- the stable "PEDIDO-N" commercial identity shown
    throughout /app/ventas, independent of how many times the order has
    since been amended (Commit 18.5: the technical name becomes
    `PEDIDO-N-1`, `PEDIDO-N-2`, ... on each amend -- confirmed directly
    against `frappe/model/naming.py`'s `_set_amended_name()`, which always
    takes priority over the `PEDIDO-.#` naming series once `amended_from`
    is set, and cannot be configured to preserve the original literal name
    -- see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 18.5 -- naming"). Only
    ever walks a chain of this Vendedora's own documents (a chain can only
    grow through her own `modify_submitted_sales_order()` calls), so no
    separate permission check is needed per hop -- same reasoning as
    `_default_warehouse_for_item()` above (a raw, single-field read used
    only to compute a label, not to expose document data)."""
    current = so_name
    seen = set()
    while current not in seen:
        seen.add(current)
        parent = frappe.db.get_value("Sales Order", current, "amended_from")
        if not parent:
            return current
        current = parent
    return current  # defensive: amended_from can never actually cycle


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
        filters={"owner": frappe.session.user},
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
                "commercial_name": _root_commercial_name(so.name),
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
    """Line-level detail for one of Vendedora's own Sales Orders -- the
    "VER PEDIDO" view in Page Ventas (Commit 18.4). Same permission model
    as every other read in this module: `get_doc()` + `check_permission
    ("read")`, which is where if_owner=1 (Commit 18.1) is actually
    enforced -- a second Vendedora's own order raises `PermissionError`
    here exactly like it does everywhere else in this module.

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

    return {
        "name": so.name,
        "commercial_name": _root_commercial_name(so.name),
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
    (`_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}`) via
    `_validate_and_build_item_rows()` (extracted in Commit 18.5, shared
    verbatim with `update_draft_sales_order()` -- one security boundary,
    not two) -- any other key (`rate`, `price_list_rate`,
    `discount_percentage`, `discount_amount`, `amount`, `net_rate`,
    `net_amount`, `margin_rate_or_amount`, `margin_type`, or anything
    else, present or future) makes this function raise immediately,
    before any Sales Order is even constructed -- never silently
    dropped. The Sales Order Item rows built from the surviving fields
    carry `item_code`/`qty`/`warehouse`/`delivery_date` only; ERPNext's
    own `AccountsController.validate()`
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
            "set_warehouse": so_items[0]["warehouse"],
            "items": so_items,
        }
    )
    if observations:
        so.fg_observations = observations

    so.insert()
    so.submit()  # triggers on_submit -> process_sales_order() -- never called directly here

    return {"name": so.name}


@frappe.whitelist()
def get_editable_order(name):
    """Prefill data for the "Editar pedido" view (Commit 18.5) -- reuses
    `get_order_detail()`'s own exact response shape verbatim (same
    allowlist, same field-by-field construction, same static guardrail
    in `test_regression.py`), since editing reuses the identical "Nuevo
    Pedido" screen just prefilled. The one thing added on top: only a
    Draft order can be prefilled for editing here -- `check_permission
    ("read")` (if_owner=1, Commit 18.1) is where ownership is actually
    enforced, exactly like every other read in this module; the
    `docstatus` check is the read-side half of the same "Draft only"
    boundary `update_draft_sales_order()` enforces independently on the
    write side below.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("read")

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

    `check_permission("write")` is where if_owner=1 (Commit 18.1) is
    actually enforced -- a second Vendedora's own order raises
    `PermissionError` here exactly like `get_order_detail()`/
    `get_my_orders()` already do for read. `docstatus == 0` is required
    explicitly, throwing a clear, specific message -- ERPNext's own
    docstatus-transition guard would eventually reject writing to a
    submitted document too, but only after doing more work first.

    Returns `{"name": "SAL-ORD-..."}` only -- no economic field, no
    Fulfillment Engine artifact name, matching every other write in this
    module.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("write")

    if so.docstatus != 0:
        frappe.throw(_("Solo se pueden editar pedidos en borrador."))

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)

    so.customer = customer
    so.set("items", [])
    for row in so_items:
        so.append("items", row)
    so.set_warehouse = so_items[0]["warehouse"]
    if observations is not None:
        so.fg_observations = observations

    so.save()  # no ignore_permissions -- her real if_owner=1 write permission already covers this

    return {"name": so.name}


@frappe.whitelist()
def delete_draft_sales_order(name):
    """Deletes one of Vendedora's own Draft Sales Orders (Commit 18.5).

    `check_permission("delete")` is where if_owner=1 -- now including
    `delete=1`, the one new grant this commit adds to the existing
    Custom DocPerm row (Commit 18.1's own row, not a second one) -- is
    actually enforced; a second Vendedora's own order raises
    `PermissionError` exactly like every other function in this module.
    `docstatus == 0` is required explicitly, matching the native rule
    that a submitted document can never be deleted (Frappe's own
    `check_permission_and_not_submitted()` would reject it too, but this
    throws a specific, clear message first).
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("delete")

    if so.docstatus != 0:
        frappe.throw(_("Solo se pueden eliminar pedidos en borrador."))

    frappe.delete_doc("Sales Order", name)  # no ignore_permissions

    return {"name": name, "deleted": True}


@frappe.whitelist()
def cancel_sales_order(name):
    """Cancels one of Vendedora's own submitted Sales Orders (Commit
    18.5).

    `check_permission("cancel")` is where if_owner=1 -- now including
    `cancel=1`, the other new grant this commit adds to the same
    existing row -- is actually enforced. `so.cancel()` is called with
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
    amend starts with `so.cancel()`); using the same permission here means
    this pre-check can never say "yes" for an order the real operation
    would then reject on ownership grounds alone.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")

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
    `process_sales_order()` fresh, exactly like `create_and_submit_sales_
    order()` -- never called directly here).

    `check_permission("cancel")` is where if_owner=1 is enforced -- the
    exact same grant `cancel_sales_order()` already relies on, no new
    Custom DocPerm needed. The authoritative gate is
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
    throughout the UI, resolved via `_root_commercial_name()`.

    Returns `{"name": "PEDIDO-1-1", "commercial_name": "PEDIDO-1"}` only --
    no economic field, matching every other write in this module.
    """
    _require_login()

    so = frappe.get_doc("Sales Order", name)
    so.check_permission("cancel")

    if so.docstatus != 1:
        frappe.throw(_("Solo se pueden modificar pedidos sometidos."))

    if modification_blockers_for(name):
        frappe.throw(
            _("Este pedido ya no puede modificarse: Bodega ya inició el alistamiento u otro proceso relacionado.")
        )

    company = frappe.defaults.get_global_default("company")
    delivery_date = add_days(nowdate(), DEFAULT_DELIVERY_LEAD_DAYS)
    so_items = _validate_and_build_item_rows(items, company, delivery_date)  # fail fast -- nothing cancelled yet

    commercial_name = _root_commercial_name(name)

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
    amended.set_warehouse = so_items[0]["warehouse"]
    if observations is not None:
        amended.fg_observations = observations

    amended.insert()
    amended.submit()  # triggers on_submit -> process_sales_order() -- never called directly here

    return {"name": amended.name, "commercial_name": commercial_name}
