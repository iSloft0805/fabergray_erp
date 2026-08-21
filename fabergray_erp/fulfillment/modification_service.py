# -*- coding: utf-8 -*-
"""Commit 18.5b -- modification_blockers_for(): the read-only gate deciding
whether a *submitted* Sales Order may still be modified via cancel+amend
(api/ventas.py's `modify_submitted_sales_order()`).

Every check here either (a) mirrors a native block `so.cancel()` itself
would already hit -- a submitted Pick List, a submitted Material Request --
so this function adds no new *rule*, only a cheap way to know the answer
*before* attempting the real cancel, or (b) is the one genuinely new
business rule Commit 18.5's audit found and the user explicitly approved:
a linked Purchase Order does NOT block native cancellation at all
(confirmed Commit 19.4: `AccountsController.on_cancel()`'s
`unlink_ref_doc_from_po()` silently detaches `sales_order`/
`sales_order_item` from the Purchase Order Item instead of refusing) --
left unchecked, cancel+amend would silently orphan a real, live purchase
Compras already placed. That case is therefore enforced here, not left to
native behaviour.

Two operational signals native cancellation does not consult at all
(Bodega having physically started picking) are checked here too, for the
same reason: `so.cancel()` only cares whether a Pick List is *submitted*,
never whether picking has *begun* on an as-yet-unsubmitted one.

Every read here uses `frappe.qb` (raw, permission-agnostic query
building), the same "Commit 18.1 pattern" already established throughout
this package (pick_list_service.py, shortage_service.py, purchase_service.py,
cancellation_service.py): this runs inside a Vendedora's own restricted
session, which has zero permission on Pick List/Material Request/Purchase
Order/Delivery Note/Sales Invoice by design (Commit 18.1) -- yet the check
must still see real state to answer correctly. Nothing here is exposed to
the caller beyond a list of opaque reason codes (never a document name, a
quantity, or any other field) -- `frappe.get_all` is deliberately not used
even though it would be permitted for this Fulfillment-Engine-package
module (matching the sibling files); `frappe.qb` keeps every query here
explicit about exactly which columns it touches.

Not wired into anything yet -- called directly by
api/ventas.py's `get_modification_status()` (a cheap, non-authoritative
pre-check for the UI) and `modify_submitted_sales_order()` (the
authoritative gate, re-derived there again right before acting, never
trusting the pre-check's result).
"""

import frappe
from frappe.utils import flt


def _pick_list_signals(so_name):
    """{"pick_list_submitted", "bodega_started", "picked_qty"} subset that
    applies to `so_name` -- one join, three independent signals read off
    the same rows: a submitted parent (blocks natively already), a parent
    Bodega has opened (`fg_started_by`, Commit 18.2 custom field -- native
    cancellation does not consult this at all), or any row with real
    picked quantity (physical evidence picking happened, independent of
    whether the parent ever got submitted)."""
    pick_list = frappe.qb.DocType("Pick List")
    pick_list_item = frappe.qb.DocType("Pick List Item")

    rows = (
        frappe.qb.from_(pick_list_item)
        .inner_join(pick_list)
        .on(pick_list.name == pick_list_item.parent)
        .select(pick_list.docstatus, pick_list.fg_started_by, pick_list_item.picked_qty)
        .where(pick_list_item.sales_order == so_name)
    ).run(as_dict=True)

    signals = set()
    for row in rows:
        if row.docstatus == 1:
            signals.add("pick_list_submitted")
        if row.fg_started_by:
            signals.add("bodega_started")
        if flt(row.picked_qty) > 0:
            signals.add("picked_qty")
    return signals


def _has_submitted_material_request(so_name):
    """True if any Material Request Item row for `so_name` belongs to a
    *submitted* parent -- draft ones (this Engine's own output, Commit
    19.1, or a human's) never block; this mirrors exactly what
    `so.cancel()`'s own native back-link check already enforces (Commit
    19.3), added here only so the UI/pre-check can know the answer without
    attempting a real cancel first."""
    material_request = frappe.qb.DocType("Material Request")
    material_request_item = frappe.qb.DocType("Material Request Item")

    rows = (
        frappe.qb.from_(material_request_item)
        .inner_join(material_request)
        .on(material_request.name == material_request_item.parent)
        .select(material_request.name)
        .where((material_request_item.sales_order == so_name) & (material_request.docstatus == 1))
        .limit(1)
    ).run()
    return bool(rows)


def _has_related_purchase_order(so_name):
    """True if any *non-cancelled* Purchase Order (draft or submitted)
    references `so_name`, either directly (`Purchase Order Item.sales_
    order`) or through a Material Request that itself references this
    Sales Order (`Purchase Order Item.material_request` -> `Material
    Request Item.sales_order`).

    This is the one check in this module that is NOT "native produces the
    same result anyway" -- confirmed (Commit 19.4) that
    `AccountsController.on_cancel()` never blocks on a linked Purchase
    Order at all, it silently clears the traceability fields instead. Left
    unchecked, cancel+amend would succeed natively while quietly orphaning
    a real purchase Compras may already be executing against. Approved as
    an explicit, new business rule for Commit 18.5 (not a native
    mechanism) -- see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 18.5".
    """
    purchase_order = frappe.qb.DocType("Purchase Order")
    purchase_order_item = frappe.qb.DocType("Purchase Order Item")
    material_request_item = frappe.qb.DocType("Material Request Item")

    direct = (
        frappe.qb.from_(purchase_order_item)
        .inner_join(purchase_order)
        .on(purchase_order.name == purchase_order_item.parent)
        .select(purchase_order.name)
        .where((purchase_order_item.sales_order == so_name) & (purchase_order.docstatus != 2))
        .limit(1)
    ).run()
    if direct:
        return True

    via_material_request = (
        frappe.qb.from_(purchase_order_item)
        .inner_join(purchase_order)
        .on(purchase_order.name == purchase_order_item.parent)
        .inner_join(material_request_item)
        .on(material_request_item.parent == purchase_order_item.material_request)
        .select(purchase_order.name)
        .where((material_request_item.sales_order == so_name) & (purchase_order.docstatus != 2))
        .limit(1)
    ).run()
    return bool(via_material_request)


def _has_downstream_delivery_or_invoice(so_name):
    """True if any (non-cancelled) Delivery Note or Sales Invoice
    references `so_name`. In this app's actual flow both are only ever
    created from an already-submitted Pick List, so
    `_pick_list_signals()`'s own `pick_list_submitted` signal above should
    already have caught this case first -- kept as an explicit, defensive
    second check anyway (the audit's own "cualquier documento downstream"
    requirement), not assumed unreachable."""
    delivery_note_item = frappe.qb.DocType("Delivery Note Item")
    sales_invoice_item = frappe.qb.DocType("Sales Invoice Item")

    delivered = (
        frappe.qb.from_(delivery_note_item)
        .select(delivery_note_item.parent)
        .where((delivery_note_item.against_sales_order == so_name) & (delivery_note_item.docstatus != 2))
        .limit(1)
    ).run()
    if delivered:
        return True

    invoiced = (
        frappe.qb.from_(sales_invoice_item)
        .select(sales_invoice_item.parent)
        .where((sales_invoice_item.sales_order == so_name) & (sales_invoice_item.docstatus != 2))
        .limit(1)
    ).run()
    return bool(invoiced)


def modification_blockers_for(so_name):
    """Read-only list of reason codes explaining why `so_name` (assumed
    already confirmed `docstatus == 1` by the caller) cannot currently be
    modified via cancel+amend -- `[]` means modification is currently
    allowed.

    Possible codes: "bodega_started", "picked_qty", "pick_list_submitted",
    "material_request_submitted", "purchase_order_linked",
    "downstream_document". Never more specific than that (no document
    name, no quantity) -- this is a gate, not a report.

    Changes nothing. Safe to call from a read endpoint for a cheap
    pre-check; `modify_submitted_sales_order()` calls this again,
    identically, immediately before acting -- never trusting a result the
    client might have cached or a state that could have changed since the
    pre-check.
    """
    blockers = set()
    blockers |= _pick_list_signals(so_name)
    if _has_submitted_material_request(so_name):
        blockers.add("material_request_submitted")
    if _has_related_purchase_order(so_name):
        blockers.add("purchase_order_linked")
    if _has_downstream_delivery_or_invoice(so_name):
        blockers.add("downstream_document")
    return sorted(blockers)
