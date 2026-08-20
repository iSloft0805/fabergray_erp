# -*- coding: utf-8 -*-
"""Commit 17 -- cleanup_fulfillment_for_cancelled_sales_order(): what
happens to the Fulfillment Engine's own automatic artifacts (Commits 13,
14) when the Sales Order they were generated for gets cancelled.

Two artifact kinds, two different fates:
- a still-draft Pick List (docstatus 0) for the cancelled order is
  removed outright -- a draft is not "cancelled" in Frappe's own
  docstatus model (cancel is a 1 -> 2 transition; a draft has never been
  submitted), so deletion, not cancellation, is the semantically correct
  native action, and it is what actually makes the order stop appearing
  as pending work in /app/bodega (get_queue() reads live Pick List Item
  rows -- once deleted, there is nothing left to read).
- an open (`Abierto`/`En Proceso`), Fulfillment-Engine-detected Reporte
  de Faltante is marked `Resuelto` with a clear, automatic note -- kept,
  not deleted, as history.

A *submitted* Pick List is never touched by this module at all -- see
`_open_pick_lists_for()`'s docstring and
FULFILLMENT_ENGINE_CONTRACT.md, "Commit 17" for why that case does not
need any code here: ERPNext's own Sales Order cancellation already
refuses to proceed while one exists.

Reports with `detected_by="Bodega"` are never read, updated, or touched
by anything in this module -- physical evidence of what a person found
while picking, independent of whether the order behind it still exists.
"""

import frappe

RESOLVED_STATUS = "Resuelto"
CANCELLATION_RESOLUTION_NOTE = "Resuelto automáticamente: Orden de Venta cancelada."


def _draft_pick_lists_for(so_name):
    """Pick List Item rows for this Sales Order whose parent is still a
    draft. A child row's docstatus always mirrors its parent's (confirmed
    throughout Commits 12/13/14/16's own queries against this exact
    table) -- no join to the parent needed to know which Pick Lists are
    still draft. A *submitted* Pick List (docstatus 1) is deliberately
    excluded here: touching it is not this function's job at all (see
    module docstring) -- ERPNext's own native Sales Order cancellation
    already blocks the whole operation while one exists, before this
    function's result could matter either way."""
    return frappe.get_all(
        "Pick List Item",
        filters={"sales_order": so_name, "docstatus": 0},
        pluck="parent",
        distinct=True,
    )


def _open_engine_reports_for(so_name):
    """Fulfillment-Engine-detected reports for this Sales Order that are
    not already Resuelto. Scoped to detected_by="Fulfillment Engine",
    exactly like every other query in shortage_service.py -- a
    Bodega-created report is structurally invisible here, never read or
    written by this function."""
    return frappe.get_all(
        "Reporte de Faltante",
        filters={
            "sales_order": so_name,
            "detected_by": "Fulfillment Engine",
            "status": ["!=", RESOLVED_STATUS],
        },
        pluck="name",
    )


def cleanup_fulfillment_for_cancelled_sales_order(sales_order):
    """Remove/resolve whatever the Fulfillment Engine automatically
    created for `sales_order`, now that it has been cancelled.

    `sales_order` may be a name or an already-loaded
    frappe.get_doc("Sales Order", ...), matching every other function in
    this package.

    Idempotent by construction, not by special-casing: both queries above
    only ever return documents that still need handling (a draft Pick
    List that still exists; an Engine report that isn't already
    Resuelto), so running this twice does nothing the second time --
    already-deleted Pick Lists and already-Resuelto reports simply don't
    show up again. No new technical field, matching the standing
    instruction (Commit 9) to prefer native relations/deterministic
    checks over one until proven necessary.

    No `frappe.db.commit()` here, deliberately -- see
    FULFILLMENT_ENGINE_CONTRACT.md, "Commit 17 -- transactional
    behaviour": this runs inside Sales Order.cancel()'s own transaction
    (the exact same guarantee traced and proven for on_submit in Commit
    16 applies identically to on_cancel), so an unhandled exception here
    rolls back everything already done in this call, together with the
    Sales Order's own cancellation itself.

    Returns {"removed_pick_lists": [...], "resolved_reports": [...]} --
    the names actually acted on in this call.
    """
    so_name = sales_order.name if hasattr(sales_order, "doctype") else sales_order

    removed_pick_lists = []
    for name in _draft_pick_lists_for(so_name):
        # ignore_permissions=True (Commit 18.1): this function is
        # Fulfillment Engine automation only, never called by an
        # interactive API -- required for the real, tested scenario of a
        # Vendedora cancelling her own Sales Order: she is intentionally
        # granted zero permission on Pick List, yet this cleanup must
        # still succeed as a consequence of her already-authorized Sales
        # Order cancel. Same native pattern as shortage_service.py (see
        # its module docstring for the full writeup and the ERPNext core
        # precedent) -- frappe.session.user is never touched.
        frappe.delete_doc("Pick List", name, ignore_permissions=True)
        removed_pick_lists.append(name)

    resolved_reports = []
    for name in _open_engine_reports_for(so_name):
        report = frappe.get_doc("Reporte de Faltante", name)
        report.status = RESOLVED_STATUS
        report.resolution_note = CANCELLATION_RESOLUTION_NOTE
        report.save(ignore_permissions=True)
        resolved_reports.append(name)

    return {"removed_pick_lists": removed_pick_lists, "resolved_reports": resolved_reports}
