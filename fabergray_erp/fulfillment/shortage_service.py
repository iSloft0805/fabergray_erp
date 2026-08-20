# -*- coding: utf-8 -*-
"""Commit 14 -- sync_shortage_reports_for_sales_order(): turns
analyze_sales_order()'s per-line shortage numbers into Reporte de Faltante
records.

Flow this closes: Sales Order -> analyze_sales_order() -> stock disponible
(Commit 13's Pick List) + qty_shortage > 0 -> Reporte de Faltante. Together
with create_pick_list_for_available_stock(), Pick List quantity + open
shortage-report quantity for a line always add up to that line's real
pending need -- nothing is silently dropped between the two.

Still creates nothing beyond Reporte de Faltante: no Material Request, no
Work Order, no Production Plan, no hook, no job, no fg_fulfillment_status.
"""

import frappe
from frappe.utils import flt

from fabergray_erp.api.bodega import OPEN_SHORTAGE_STATUSES, _insert_shortage_report
from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import (
    _qty_already_claimed_by_open_pick_lists_for_so_item,
)

DETECTED_BY = "Fulfillment Engine"

# V1 explicit, non-inferred reasons -- see FULFILLMENT_ENGINE_CONTRACT.md,
# "Commit 14 -- shortage_reason for automatic reports" for why "Blocked"
# needed a new Select option instead of reusing "Otro".
SHORTAGE_REASON_BY_ROUTE = {
    "Purchase": "Compra pendiente",
    "Manufacture": "Producción pendiente",
    "Blocked": "Configuración incompleta",
}


def _find_open_engine_report(sales_order_item):
    """The one relation Commit 14 needs for unambiguous idempotency:
    Reporte de Faltante.sales_order_item, a native reference to the exact
    Sales Order Item row (added this commit -- see the doctype JSON and
    FULFILLMENT_ENGINE_CONTRACT.md). No hash, no item_code+warehouse
    guess -- a Sales Order with the same item on two lines (different
    warehouses or otherwise) resolves correctly for free because each
    line has its own distinct sales_order_item row name.

    frappe.get_list(), not get_all(), matching this app's existing
    convention (_get_shortage_report_rows() in this same module) of never
    reading past the current user's own permissions by default.
    """
    names = frappe.get_list(
        "Reporte de Faltante",
        filters={
            "sales_order_item": sales_order_item,
            "detected_by": DETECTED_BY,
            "status": ["in", OPEN_SHORTAGE_STATUSES],
        },
        pluck="name",
        order_by="creation desc",
        limit=1,
    )
    return frappe.get_doc("Reporte de Faltante", names[0]) if names else None


def _resolution_note_for_cleared_shortage(qty_available, qty_pending):
    return (
        "Resuelto automáticamente por el Fulfillment Engine: disponible "
        f"({qty_available}) ya cubre lo solicitado ({qty_pending})."
    )


def sync_shortage_reports_for_sales_order(sales_order):
    """For every line of `sales_order` where analyze_sales_order() reports
    qty_shortage > 0, ensure exactly one open, Fulfillment-Engine-detected
    Reporte de Faltante exists with today's numbers -- creating one if
    none exists yet, updating the existing one in place if the shortage
    changed (still short, same episode), and doing nothing otherwise.
    Lines whose shortage has cleared get their open Engine report marked
    Resuelto automatically. Reports created by Bodega (detected_by=
    "Bodega") are never read, updated, or resolved by this function --
    every query here is scoped to detected_by="Fulfillment Engine".

    `sales_order` may be a name or an already-loaded
    frappe.get_doc("Sales Order", ...), exactly like analyze_sales_order().

    Decision source: analyze_sales_order() (Commit 12) is the only place
    that computes qty_shortage/qty_remaining/qty_available_for_pick/
    procurement_route -- this function adds no parallel version of that
    availability/shortage math. It does apply one adjustment, reusing (not
    reinventing) the exact signal Commit 13 already introduced for a
    related reason: qty_remaining is deliberately delivery-only (Commit 12
    proved this on purpose), so a line already fully claimed by an open
    Pick List -- created moments earlier by
    create_pick_list_for_available_stock(), by a human, or by Bodega --
    still shows a raw qty_shortage > 0, even though nothing about it
    actually needs Purchase/Manufacture, only delivery once picked.
    Skipping this adjustment would mean running this service right after
    Commit 13's own service (the exact, expected, documented usage
    pattern -- see "Integración con Commit 13" below) would report a full
    shortage for a line that is, in truth, entirely covered. So for every
    line with a raw qty_shortage > 0, this function computes:
        qty_pending = max(qty_remaining - qty_already_claimed_by_open_pick_lists_for_this_so_item, 0)
        qty_procurement_shortage = max(qty_pending - qty_available_for_pick, 0)
    reusing pick_list_service._qty_already_claimed_by_open_pick_lists_for_so_item()
    as-is (imported, not duplicated, not modified). When there is no
    pre-existing open Pick List for the line (already_claimed = 0, true
    for every scenario except the Commit 13 integration case), this
    reduces exactly to qty_pending = qty_remaining and
    qty_procurement_shortage = qty_shortage -- the literal analyzer
    fields, unchanged. `qty_solicitada`/`qty_disponible` on the report are
    qty_pending/qty_available_for_pick respectively, so `qty_faltante`
    (the doctype's own computed field) always equals
    qty_procurement_shortage. Every Reporte de Faltante is created or
    updated through _insert_shortage_report() (Commit 9's one approved
    insert path) or a plain .save() on an already-existing report this
    same function found -- never a second insert path.

    Idempotency: no new technical field beyond sales_order_item itself
    (which serves both idempotency and traceability, per the standing
    instruction from Commit 9 to prefer native relations over a dedicated
    technical field until proven necessary). Running this twice in a row
    with nothing changed creates and updates nothing.

    Returns a summary: {"created": [...], "updated": [...], "resolved":
    [...], "blocked": [...]} -- each a list of Reporte de Faltante names.
    "blocked" is not exclusive with "created"/"updated": a line with
    procurement_route="Blocked" always has qty_shortage > 0, so it always
    gets a report (created or updated in the same pass) -- "blocked" just
    also lists that same report's name, flagging which reports need
    master-data attention (e.g. a missing BOM) rather than a Purchase
    Order or Work Order.

    Concurrency (Commit 16): the check-then-insert window Commit 15
    identified (two concurrent callers for the same Sales Order line could
    each see "no open report yet" and both insert one) is closed here with
    a plain `SELECT ... FOR UPDATE` row lock on the Sales Order itself,
    held for the rest of the current transaction -- native Frappe/MariaDB
    locking, the same idiom ERPNext's own `get_available_qty_to_reserve()`
    uses, not a global lock and not an invented mechanism. Every
    `sales_order_item` belongs to exactly one Sales Order, so locking this
    one row precisely serializes every concurrent caller that could
    possibly race for any of this order's lines -- no broader and no
    narrower than the actual contention. A DB-level partial unique
    constraint (a generated column + unique index, so only "open" rows
    collide, letting resolved history and Bodega reports coexist freely)
    was evaluated first and rejected as too invasive for this commit: it
    requires a raw SQL column/index Frappe's own DocType framework has no
    declarative way to express or track, undeclared to every doctype
    export/migration tool this app otherwise relies on -- see
    FULFILLMENT_ENGINE_CONTRACT.md, "Commit 16" for the full comparison.
    Proven with a real two-connection test
    (test_concurrent_calls_do_not_create_duplicate_open_reports), not
    simulated with a single connection like Commit 13's own concurrency
    test had to be.
    """
    so_name = sales_order.name if hasattr(sales_order, "doctype") else sales_order
    frappe.db.get_value("Sales Order", so_name, "name", for_update=True)

    analysis = analyze_sales_order(sales_order)

    summary = {"created": [], "updated": [], "resolved": [], "blocked": []}

    for line in analysis["lines"]:
        existing = _find_open_engine_report(line["sales_order_item"])
        qty_available = flt(line["qty_available_for_pick"])

        if line["qty_shortage"] <= 0:
            # Ready outright -- cheapest, most common case: never even
            # worth the extra query for already-claimed-by-open-PL qty.
            if existing:
                existing.status = "Resuelto"
                existing.resolution_note = _resolution_note_for_cleared_shortage(
                    qty_available, flt(line["qty_remaining"])
                )
                existing.save()
                summary["resolved"].append(existing.name)
            continue

        already_claimed = _qty_already_claimed_by_open_pick_lists_for_so_item(line["sales_order_item"])
        qty_pending = max(flt(line["qty_remaining"]) - already_claimed, 0.0)
        qty_procurement_shortage = max(qty_pending - qty_available, 0.0)

        if qty_procurement_shortage <= 0:
            # Fully absorbed by an already-open Pick List for this exact
            # line -- nothing left that genuinely needs Purchase/Manufacture.
            if existing:
                existing.status = "Resuelto"
                existing.resolution_note = _resolution_note_for_cleared_shortage(qty_available, qty_pending)
                existing.save()
                summary["resolved"].append(existing.name)
            continue

        shortage_reason = SHORTAGE_REASON_BY_ROUTE.get(line["procurement_route"])

        if existing:
            changed = (
                flt(existing.qty_solicitada) != qty_pending
                or flt(existing.qty_disponible) != qty_available
                or existing.shortage_reason != shortage_reason
            )
            if changed:
                existing.qty_solicitada = qty_pending
                existing.qty_disponible = qty_available
                existing.shortage_reason = shortage_reason
                existing.save()
                summary["updated"].append(existing.name)
            report_name = existing.name
        else:
            report_name = _insert_shortage_report(
                item_code=line["item_code"],
                warehouse=line["warehouse"],
                qty_solicitada=qty_pending,
                qty_disponible=qty_available,
                detected_by=DETECTED_BY,
                sales_order=so_name,
                sales_order_item=line["sales_order_item"],
                shortage_reason=shortage_reason,
            )
            summary["created"].append(report_name)

        if line["procurement_route"] == "Blocked":
            summary["blocked"].append(report_name)

    return summary
