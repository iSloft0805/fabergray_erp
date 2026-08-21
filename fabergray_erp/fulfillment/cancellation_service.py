# -*- coding: utf-8 -*-
"""Commit 17 -- cleanup_fulfillment_for_cancelled_sales_order(): what
happens to the Fulfillment Engine's own automatic artifacts (Commits 13,
14) when the Sales Order they were generated for gets cancelled. Commit
19.3 extends it with the Compras-side equivalent for Material Request
(Commit 19.1).

Three artifact kinds now, three different fates:
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
- a still-draft Material Request (docstatus 0) that purchase_service.py
  itself created (`fg_created_by_fulfillment_engine=1`, Commit 19.3) has
  its rows for this Sales Order removed; the whole document is deleted if
  that empties it entirely, or just saved (kept, with the other lines
  untouched) if any row for a *different* Sales Order remains -- see
  `_cleanup_engine_material_request_for()`'s own docstring for exactly
  why the Custom Field is what makes this safe.

A *submitted* Pick List is never touched by this module at all -- see
`_open_pick_lists_for()`'s docstring and
FULFILLMENT_ENGINE_CONTRACT.md, "Commit 17" for why that case does not
need any code here: ERPNext's own Sales Order cancellation already
refuses to proceed while one exists. The identical native mechanism
(confirmed live, Commit 19.3) blocks cancellation just as hard while a
*submitted* Material Request is linked -- so this module, exactly like
for Pick List, never needs to consider that case either; a submitted
Material Request that is NOT linked to this Sales Order (i.e. every
lingering reference already belongs to some other, still-open order) is
obviously never touched regardless.

Purchase Order needs zero code here at all: ERPNext's own
`AccountsController.on_cancel()` (`accounts_controller.py:2051-2056`)
already, unconditionally, calls `unlink_ref_doc_from_po()` for every
Sales Order cancellation -- confirmed live (Commit 19.3) to clear
`sales_order`/`sales_order_item` on any linked Purchase Order Item row,
draft OR submitted (`docstatus < 2`), before this module's own on_cancel
code even finishes running. The Purchase Order document itself (qty,
status, received_qty) is never modified or cancelled by that native
behaviour -- only its traceability back to this Sales Order is cleared.
Nothing in this module reads or writes Purchase Order at all.

Reports with `detected_by="Bodega"` are never read, updated, or touched
by anything in this module -- physical evidence of what a person found
while picking, independent of whether the order behind it still exists.
A Material Request without `fg_created_by_fulfillment_engine=1` -- one a
human created directly, e.g. via ERPNext's own native "Create Material
Request" button on the Sales Order, which produces a field-for-field
identical `sales_order`/`sales_order_item` signature (Commit 19.1's own
documented finding) -- is likewise never read, updated, or touched here,
for the same reason: this module can only safely act on what it can
prove it created.
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


def _draft_engine_material_requests_for(so_name):
    """Material Requests that (a) purchase_service.py itself created
    (`fg_created_by_fulfillment_engine=1`, Commit 19.3 -- the one field
    this whole module trusts to distinguish its own output from a human's,
    per Commit 19.1's documented finding that native fields alone cannot),
    (b) are still Draft (docstatus 0 -- a submitted one already blocks
    Sales Order cancellation natively, see module docstring), and (c)
    have at least one Item row still referencing this Sales Order. Every
    check happens in one query, scoped by joining to the parent so a
    Material Request lacking the flag (a human's own, however coincidentally
    it may reference this Sales Order) can never be returned."""
    material_request = frappe.qb.DocType("Material Request")
    material_request_item = frappe.qb.DocType("Material Request Item")

    rows = (
        frappe.qb.from_(material_request_item)
        .inner_join(material_request)
        .on(material_request.name == material_request_item.parent)
        .select(material_request.name)
        .where(
            (material_request_item.sales_order == so_name)
            & (material_request.docstatus == 0)
            & (material_request.fg_created_by_fulfillment_engine == 1)
        )
        .distinct()
    ).run(as_dict=True)

    return [row.name for row in rows]


def _cleanup_engine_material_request_for(mr_name, so_name):
    """Removes this exact Sales Order's rows from one Engine-created draft
    Material Request, then either deletes the whole document (if that was
    every row it had -- "exclusivamente de esa SO", the approved rule) or
    saves it with only those rows gone (if rows for a *different* Sales
    Order remain -- "MR Draft mixto", the approved rule: never delete the
    whole document, and never touch the other rows, which by construction
    can only be there because Compras added them by hand after this
    Engine-created draft was already inserted -- purchase_service.py
    itself never creates a Material Request spanning more than one Sales
    Order in a single call).

    Returns "removed" (whole document deleted) or "trimmed" (document
    kept, only this Sales Order's rows gone) -- the caller uses this to
    build cleanup_fulfillment_for_cancelled_sales_order()'s own summary.

    `ignore_permissions=True` on both the delete and the save -- same
    Commit 18.1 pattern as this module's Pick List/Reporte de Faltante
    handling above: this runs inside a Vendedora's own restricted session
    (zero permission on Material Request, Commit 18.1) as a consequence
    of her own already-authorized Sales Order cancel.
    """
    mr = frappe.get_doc("Material Request", mr_name)
    rows_to_remove = [row for row in mr.items if row.sales_order == so_name]

    for row in rows_to_remove:
        mr.remove(row)

    if not mr.get("items"):
        frappe.delete_doc("Material Request", mr_name, ignore_permissions=True)
        return "removed"

    mr.save(ignore_permissions=True)
    return "trimmed"


def cleanup_fulfillment_for_cancelled_sales_order(sales_order):
    """Remove/resolve whatever the Fulfillment Engine automatically
    created for `sales_order`, now that it has been cancelled.

    `sales_order` may be a name or an already-loaded
    frappe.get_doc("Sales Order", ...), matching every other function in
    this package.

    Idempotent by construction, not by special-casing: every query above
    only ever returns documents/rows that still need handling (a draft
    Pick List that still exists; an Engine report that isn't already
    Resuelto; an Engine-flagged draft Material Request that still has a
    row for this Sales Order), so running this twice does nothing the
    second time -- already-deleted/trimmed Material Requests, already-
    deleted Pick Lists, and already-Resuelto reports simply don't show up
    again. Pick List/Reporte de Faltante cleanup needed no new technical
    field (Commit 9's standing instruction); the Material Request case
    (Commit 19.3) is the one exception, approved after the same
    native-relations-alone-are-insufficient finding was reconfirmed for
    it (see fg_created_by_fulfillment_engine, purchase_service.py).

    No `frappe.db.commit()` here, deliberately -- see
    FULFILLMENT_ENGINE_CONTRACT.md, "Commit 17 -- transactional
    behaviour": this runs inside Sales Order.cancel()'s own transaction
    (the exact same guarantee traced and proven for on_submit in Commit
    16 applies identically to on_cancel), so an unhandled exception here
    rolls back everything already done in this call, together with the
    Sales Order's own cancellation itself. Confirmed (Commit 19.3) this
    still holds with the new Material Request step added.

    Returns {"removed_pick_lists": [...], "resolved_reports": [...],
    "removed_material_requests": [...], "trimmed_material_requests":
    [...]} -- the names actually acted on in this call. "removed" means
    the whole (now-empty) Material Request was deleted; "trimmed" means
    only this Sales Order's own rows were removed from a document that
    still has other rows left (the "MR Draft mixto" case) -- the document
    itself was kept, untouched otherwise.
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

    removed_material_requests = []
    trimmed_material_requests = []
    for name in _draft_engine_material_requests_for(so_name):
        outcome = _cleanup_engine_material_request_for(name, so_name)
        if outcome == "removed":
            removed_material_requests.append(name)
        else:
            trimmed_material_requests.append(name)

    return {
        "removed_pick_lists": removed_pick_lists,
        "resolved_reports": resolved_reports,
        "removed_material_requests": removed_material_requests,
        "trimmed_material_requests": trimmed_material_requests,
    }
