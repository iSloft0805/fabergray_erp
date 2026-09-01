# -*- coding: utf-8 -*-
"""Commit 15 -- Fulfillment Engine Orchestrator: the single function that
composes Commits 12/13/14 for one submitted Sales Order. Commit 19.2 adds
a fourth step, the Compras-side twin of Commit 14 (Commit 19.1):

process_sales_order(sales_order):
    analyze_sales_order()                        (Commit 12, validation + snapshot)
    -> create_pick_list_for_available_stock()     (Commit 13)
    -> analyze_sales_order() again                (Commit 12, post-Pick-List snapshot)
    -> sync_shortage_reports_for_sales_order()     (Commit 14)
    -> sync_material_requests_for_sales_order()    (Commit 19.1)

Nothing here is a new formula, a new insert path, or a new idempotency
mechanism -- it is pure composition of already-approved, already-tested
services, in the order that makes their own documented behaviour compose
correctly (see shortage_service.py's own docstring, "Commit 14 -- ... A
real interaction with Commit 13", for why Pick List must run before
shortage sync; see this function's own docstring below, "Commit 19.2 --
why sync_material_requests runs after sync_shortage_reports, not before",
for the fourth step's own ordering).

Still creates nothing beyond what Commits 13/14/19.1 already create: a
Pick List, a Reporte de Faltante, a draft Material Request. No Purchase
Order, Supplier, Work Order, Production Plan, Delivery Note, or Sales
Invoice. Wired to `Sales Order.on_submit` since Commit 16
(fulfillment/sales_order_hooks.py) -- every step below therefore also
runs inside whatever session actually submitted the Sales Order (e.g. a
Vendedora, Commit 18.1), which is why sync_material_requests_for_
sales_order()'s own one write already uses `ignore_permissions=True`
(Commit 19.1) -- this orchestrator adds no permission logic of its own,
it inherits exactly what its four building blocks already enforce.
"""

import frappe

from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import (
    create_pick_list_for_available_stock,
    create_pick_list_for_full_demand,
)
from fabergray_erp.fulfillment.purchase_service import sync_material_requests_for_sales_order
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
    either window in this commit. Commit 19.1's own Material Request
    idempotency query is a plain read (frappe.qb, no locking) with the
    same class of residual race as the Reporte de Faltante one above --
    not newly introduced by this composition, not closed here either,
    same rationale.

    Commit 19.2 -- why sync_material_requests_for_sales_order() runs
    after sync_shortage_reports_for_sales_order(), not before (confirmed,
    not assumed, before wiring this in): the two services do not read
    each other's writes at all -- sync_shortage_reports_for_sales_order()
    never queries Material Request, and sync_material_requests_for_
    sales_order() never queries Reporte de Faltante. Both independently
    call analyze_sales_order() themselves and both independently apply
    the exact same already-claimed-by-open-Pick-List correction (Commit
    14's formula, reused by both, not duplicated by either) to the same
    Pick List state left by create_pick_list_for_available_stock() above
    -- so their order relative to EACH OTHER has no effect on either
    one's correctness; there is no technical reason to invert them. The
    order below matches the approved sequence: the operational
    representation of the shortage (Reporte de Faltante, what Bodega/Jefe
    de Bodega already see) is kept in sync first, and only then is the
    Purchase-side need materialized into a Material Request -- both
    consuming the identical net shortage number, computed independently
    by each service from the same underlying facts.

    Returns:
        {
            "sales_order": "SAL-ORD-...",
            "pick_list": "STO-PICK-..." or None,
            "analysis": {...},        # analyze_sales_order()'s own shape, post-Pick-List
            "shortages": {"created": [...], "updated": [...], "resolved": [...], "blocked": [...]},
            "purchasing": {"created": [...], "lines_requested": [...]},  # purchase_service.py's own shape, Commit 19.1 -- unmodified
            "status": "processed",
        }
    """
    so = sales_order if hasattr(sales_order, "doctype") else frappe.get_doc("Sales Order", sales_order)

    analyze_sales_order(so)  # fail fast: docstatus/permission check only, result unused

    pick_list = create_pick_list_for_available_stock(so)

    analysis = analyze_sales_order(so)  # re-read: reflects create_pick_list_for_available_stock()'s
    # own claim, so this snapshot and the two syncs below describe the
    # same, post-Pick-List moment.

    shortages = sync_shortage_reports_for_sales_order(so)
    purchasing = sync_material_requests_for_sales_order(so)

    return {
        "sales_order": so.name,
        "pick_list": pick_list.name if pick_list else None,
        "analysis": analysis,
        "shortages": shortages,
        "purchasing": purchasing,
        "status": "processed",
    }


def process_sales_order_for_confirmation(sales_order):
    """Commit 25.4 -- wired to `Sales Order.on_submit` INSTEAD of
    `process_sales_order()` above (see `fulfillment/sales_order_hooks.py`).
    `process_sales_order()` itself is untouched -- still the full,
    four-step composition, still fully valid and tested, still directly
    callable by anyone who explicitly wants it (e.g. a future admin
    "reprocess" action) -- this is a NARROWER, separate entry point, not
    a behavior change to that one.

    New business rule (approved): "Ventas no decide faltantes. El stock
    teórico no decide el faltante definitivo. Bodega debe recibir TODOS
    los pedidos y confirmar físicamente cuánto pudo alistar." Confirming
    a Sales Order from Ventas must do exactly one automated thing: make
    sure Bodega has a complete Pick List to work from, covering the
    FULL requested demand of every line (`create_pick_list_for_full_
    demand()`, Commit 25.4 -- never capped or silently dropped for a
    line with `actual_qty=0`, unlike `create_pick_list_for_available_
    stock()` above). It deliberately does NOT call `sync_shortage_
    reports_for_sales_order()` or `sync_material_requests_for_sales_
    order()` anymore -- both are driven by the exact same theoretical,
    `Bin.actual_qty`-based shortage computation the new rule explicitly
    forbids acting on automatically; a Reporte de Faltante may now only
    ever originate from Bodega's own physical confirmation
    (`api.bodega.report_shortage()`, unchanged, already exactly the
    right mechanism -- see this commit's own audit), and a Material
    Request this Engine creates automatically is the exact same class
    of premature action, so it is withheld for the same reason.

    Returns a narrower shape than `process_sales_order()`'s -- no
    `analysis`/`shortages`/`purchasing` keys, since none of those steps
    run here:
        {
            "sales_order": "SAL-ORD-...",
            "pick_list": "STO-PICK-..." or None,
            "status": "queued_for_bodega",
        }
    """
    so = sales_order if hasattr(sales_order, "doctype") else frappe.get_doc("Sales Order", sales_order)

    pick_list = create_pick_list_for_full_demand(so)

    return {
        "sales_order": so.name,
        "pick_list": pick_list.name if pick_list else None,
        "status": "queued_for_bodega",
    }
