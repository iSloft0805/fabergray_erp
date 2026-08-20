# -*- coding: utf-8 -*-
"""Commit 15 -- Fulfillment Engine Orchestrator: the single function that
composes Commits 12/13/14 for one submitted Sales Order.

process_sales_order(sales_order):
    analyze_sales_order()                        (Commit 12, validation + snapshot)
    -> create_pick_list_for_available_stock()     (Commit 13)
    -> analyze_sales_order() again                (Commit 12, post-Pick-List snapshot)
    -> sync_shortage_reports_for_sales_order()     (Commit 14)

Nothing here is a new formula, a new insert path, or a new idempotency
mechanism -- it is pure composition of three already-approved, already-
tested services, in the order that makes their own documented behaviour
compose correctly (see the module-level docstring of shortage_service.py,
"Commit 14 -- ... A real interaction with Commit 13" for exactly why this
order, not the reverse, is required).

Still creates nothing beyond what Commits 13/14 already create: a Pick
List, a Reporte de Faltante. No Material Request, Purchase Order, Work
Order, Production Plan, Delivery Note, or Sales Invoice. Not wired to
`on_submit`, a hook, or a job -- callable only directly, exactly like its
three building blocks.
"""

import frappe

from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_available_stock
from fabergray_erp.fulfillment.shortage_service import sync_shortage_reports_for_sales_order


def process_sales_order(sales_order):
    """Run the full read-then-act pipeline for one submitted Sales Order:
    claim whatever stock is genuinely available into a native Pick List,
    then make sure exactly one open, Fulfillment-Engine-detected Reporte
    de Faltante exists for whatever is left over -- created, updated, or
    resolved as appropriate.

    `sales_order` may be a name or an already-loaded
    frappe.get_doc("Sales Order", ...), exactly like all three functions
    it composes.

    Validation ("docstatus = 1; orden no cancelada"): delegated entirely
    to analyze_sales_order()'s own existing check (`so.docstatus != 1` ->
    frappe.throw()) via the first call below -- not duplicated here. That
    one check already covers both a draft (docstatus 0) and a cancelled
    (docstatus 2) Sales Order uniformly, and it runs *before* any write is
    attempted, so a rejected Sales Order never reaches
    create_pick_list_for_available_stock() or
    sync_shortage_reports_for_sales_order() at all -- there is no partial
    side effect to clean up on rejection.

    Permissions/execution context: this function adds no permission logic
    of its own -- it inherits exactly what its three building blocks
    already enforce (analyze_sales_order()'s `check_permission("read")`,
    create_pick_list_for_available_stock()'s plain, non-ignore_permissions
    `.insert()`, and _insert_shortage_report()'s
    `frappe.has_permission(..., "create", throw=True)`). Same open
    question already flagged in Commit 9 and left unresolved on purpose:
    deciding what identity a future automated caller (a hook, a job) runs
    as is still future work -- this function is only ever meant to be
    called directly (console, tests) until that is decided.

    Order of operations, confirmed correct rather than assumed (see
    shortage_service.py's own docstring for the full reasoning): analyze
    -> create Pick List -> analyze again -> sync shortages. The *second*
    analyze_sales_order() call -- not the first -- is the one returned in
    the result and the one whose numbers the shortage sync is consistent
    with, because create_pick_list_for_available_stock() may have just
    claimed part of a line; re-reading afterwards is what makes
    "analysis" and "shortages" describe the *same* moment, and what makes
    sync_shortage_reports_for_sales_order() report only what is genuinely
    still unassigned to picking rather than a line's full raw shortage
    that a Pick List already covers. The first analyze() call exists only
    to fail fast, before any write is attempted, and to resolve
    `sales_order` into one loaded document reused for every step below
    (avoiding three separate re-fetches of the same header doc).

    Idempotency: no new mechanism, no new technical field. Running this
    two or more times in a row is idempotent purely because each of its
    three calls already is (Commits 12/13/14): a second call's Pick List
    creation offers only the genuine remainder (or nothing, returning
    None) and a second call's shortage sync updates the same open report
    in place, or resolves it, rather than duplicating it.

    Concurrency: see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 15 -- known
    concurrency window" for the full writeup. In short: this function adds
    no locking of its own (deliberately, per instruction) on top of what
    Commits 13/14 already do or don't have. Commit 13's residual race
    (two truly concurrent transactions could each claim the same stock
    for a moment) still exists, unchanged, with the same safety net
    (Bodega's report_shortage()/finish_picking() disclosure flow).
    Composing it with Commit 14 surfaces a second, analogous window this
    commit does not close either: two concurrent process_sales_order()
    calls could each independently find "no open report yet" for the same
    line and both insert one, before either commit -- a duplicate Reporte
    de Faltante, not a duplicate Pick List. No ad hoc lock was added for
    either window in this commit.

    Returns:
        {
            "sales_order": "SAL-ORD-...",
            "pick_list": "STO-PICK-..." or None,
            "analysis": {...},        # analyze_sales_order()'s own shape, post-Pick-List
            "shortages": {"created": [...], "updated": [...], "resolved": [...], "blocked": [...]},
            "status": "processed",
        }
    """
    so = sales_order if hasattr(sales_order, "doctype") else frappe.get_doc("Sales Order", sales_order)

    analyze_sales_order(so)  # fail fast: docstatus/permission check only, result unused

    pick_list = create_pick_list_for_available_stock(so)

    analysis = analyze_sales_order(so)  # re-read: reflects create_pick_list_for_available_stock()'s
    # own claim, so this snapshot and the shortage sync below describe the
    # same, post-Pick-List moment.

    shortages = sync_shortage_reports_for_sales_order(so)

    return {
        "sales_order": so.name,
        "pick_list": pick_list.name if pick_list else None,
        "analysis": analysis,
        "shortages": shortages,
        "status": "processed",
    }
