# -*- coding: utf-8 -*-
"""Commit 19.1 -- sync_material_requests_for_sales_order(): turns
analyze_sales_order()'s Purchase-route shortages into Material Request
records. The Compras-side twin of shortage_service.py's Reporte de
Faltante sync -- same source of truth (the analyzer), same "reuse native
relations, no new technical field" standing rule (Commit 9), same
Fulfillment-Engine-automation-only calling convention.

Flow this closes: Sales Order -> analyze_sales_order() -> Pick List
(pick_list_service, Commit 13) covers whatever is physically available;
whatever is still short AND routes to "Purchase" -> a draft Material
Request here, so Compras has something to work from. Manufacture/Blocked
routes are still Reporte de Faltante only (shortage_service.py,
unchanged) -- this module never touches those.

Still creates nothing beyond Material Request: no Supplier is chosen, no
Purchase Order, no submit. The resulting Material Request is always left
in Draft (docstatus 0) -- see sync_material_requests_for_sales_order()'s
own docstring for the full reasoning, confirmed (not assumed) against
material_request.py directly: Draft is what the user approved after
being shown the exact native consequence (Sales Order Item.requested_qty
is never touched by a draft) and this module's own answer to it (a
dedicated, narrower "already claimed" query -- see
qty_already_claimed_by_open_material_requests_for_so_item() below --
mirroring the same pattern pick_list_service.py already established for
Pick List/Commit 13, for the same underlying reason: the one native field
that *would* answer this only updates on submit).

Commit 18.1 pattern followed here too, even though this module is not
yet wired to any hook (Commit 19.2): every read uses frappe.qb (raw,
permission-agnostic query building, same as pick_list_service.py's own
_qty_already_claimed_by_open_pick_lists_for_so_item()) and the one write
uses .insert(ignore_permissions=True) -- required for the same reason
shortage_service.py needs it: once wired into process_sales_order(), this
runs inside whichever session actually submitted the Sales Order (e.g. a
Vendedora, who has zero Material Request permission by design, Commit
18.1). frappe.session.user is never touched -- the resulting Material
Request's `owner` still correctly reflects whoever actually submitted the
Sales Order.

Explicit, deliberate non-goals for this commit (approved scope): no
Supplier selection, no Purchase Order, no submit, no hook wiring, no
Custom Field, no change to the Fulfillment Engine's orchestrator
(engine.py) or to shortage_service.py/pick_list_service.py. Updating or
deleting a pre-existing draft Material Request when the shortage shrinks
or clears is explicitly OUT of this commit -- see this module's own
"Known, deliberately unresolved gap" section below for why, confirmed by
reading material_request_item.json directly rather than assumed.
"""

import frappe
from frappe.utils import flt, nowdate

from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import (
    _qty_already_claimed_by_open_pick_lists_for_so_item,
)

#: The only Material Request purpose this module ever creates or counts.
#: A Material Transfer/Manufacture/Subcontracting/Customer Provided MR
#: that happens to reference the same sales_order_item (native fields,
#: nothing stops a human or a future module from creating one) must never
#: be treated as covering a Purchase shortage -- it is a different kind
#: of need, even if it points at the same Sales Order line.
MATERIAL_REQUEST_TYPE = "Purchase"

#: Native Material Request status (`update_status()`/the form's "Stop"
#: button, material_request.py:308) meaning "no longer being pursued",
#: independent of docstatus -- a Stopped MR stays docstatus=1 (submitted)
#: forever, it is never cancelled. Confirmed native precedent for
#: excluding it from "still active" demand: erpnext/buying/utils.py's own
#: check_on_hold_or_closed_status() and material_request.py:852 both
#: filter `status != "Stopped"` when deciding what still needs action.
#: Mirrors OPEN_PICK_LIST_STATUSES_EXCLUDED (analyzer.py) in spirit --
#: same "docstatus alone is not enough, some doctypes have a second,
#: independent 'no longer active' status" lesson, applied here to MR
#: rather than reproduced blindly from Pick List's own status values
#: (which do not include "Stopped" at all).
STOPPED_MATERIAL_REQUEST_STATUS = "Stopped"


def qty_already_claimed_by_open_material_requests_for_so_item(sales_order_item):
    """How much of THIS Sales Order Item's Purchase need is already
    represented by a Material Request Item row -- draft or submitted,
    this module's own or a human's, as long as it carries the native
    sales_order_item relation (Commit 19's audit confirmed: that field is
    `hidden`+`read_only` on the Material Request Item grid, so a human
    can never type it in by hand -- only server-side code, a mapper, or
    this module itself, ever populates it).

    Why this exists instead of reading Sales Order Item.requested_qty
    directly (investigated, not assumed, before writing this function):
    requested_qty is written by MaterialRequest.update_prevdoc_status()
    (material_request.py:285,343), which only runs from on_submit()/
    on_cancel() -- never from validate()/before_save(), which is all that
    runs for a Draft document. Confirmed by reading the exact SQL
    _update_children() (controllers/status_updater.py:583-593) uses to
    write it: `SELECT SUM(stock_qty) FROM \\`tabMaterial Request Item\\`
    WHERE sales_order_item=%s AND docstatus=1 ...` -- docstatus=1 only,
    a Draft (docstatus=0) row is invisible to it. Since this module's own
    Material Requests are approved to stay Draft (Compras reviews before
    they become official native demand), requested_qty would read 0 for
    exactly the rows this function most needs to see -- it cannot be the
    idempotency signal, only a secondary, native confirmation once
    Compras submits (see the "Known, deliberately unresolved gap" note
    below for the one thing that still requires care once that happens).

    Included (confirmed by reading material_request.py directly, not
    assumed): every Material Request Item row where...
    - `sales_order_item` matches this exact Sales Order Item row name
      (never item_code+warehouse -- same reasoning as
      pick_list_service.py's own sibling function: two lines of the same
      Sales Order with the same item must resolve independently);
    - the parent Material Request's `material_request_type` is
      "Purchase" (MATERIAL_REQUEST_TYPE above);
    - the row's own `docstatus != 2` -- includes BOTH Draft (0, the
      normal case for this module's own output) and Submitted (1, e.g.
      once Compras submits, or a pre-existing MR created some other way);
    - the parent's `status != "Stopped"` -- a submitted-but-Stopped MR
      is native ERPNext's own way of saying "no longer being pursued"
      without cancelling it (see STOPPED_MATERIAL_REQUEST_STATUS above);
      counting it as still-claimed would silently block Compras from
      ever being offered the real remaining need again.

    Excluded: cancelled rows (docstatus=2) and Stopped Material Requests.

    Sums `stock_qty` per matching row -- the same field ERPNext's own
    native aggregation (`source_field: "stock_qty"`,
    material_request.py:110) uses for the identical purpose, kept for
    consistency rather than `qty` (which is only equal to `stock_qty`
    when conversion_factor is 1, true everywhere in this app's data
    today but not guaranteed in general).
    """
    material_request = frappe.qb.DocType("Material Request")
    material_request_item = frappe.qb.DocType("Material Request Item")

    rows = (
        frappe.qb.from_(material_request_item)
        .inner_join(material_request)
        .on(material_request.name == material_request_item.parent)
        .select(material_request_item.stock_qty)
        .where(
            (material_request_item.sales_order_item == sales_order_item)
            & (material_request.material_request_type == MATERIAL_REQUEST_TYPE)
            & (material_request_item.docstatus != 2)
            & (material_request.status != STOPPED_MATERIAL_REQUEST_STATUS)
        )
    ).run(as_dict=True)

    return sum(flt(row.stock_qty) for row in rows)


def _insert_draft_material_request(so, lines_to_request):
    """The one insert path this module has -- builds one Material Request
    directly via frappe.get_doc({...}) (approved: sales_order.py's own
    native make_material_request() cannot express this module's
    procurement-route filtering or its analyzer-driven, Pick-List-aware
    shortage quantity -- see this module's own top docstring / the
    Commit 19.1 approval message for the full comparison; there is no
    parallel-mapping duplication here, only field names ERPNext's own
    Material Request Item already defines natively: item_code, qty,
    warehouse, schedule_date, sales_order, sales_order_item). One
    Material Request per call, one Item row per Sales Order line that
    still genuinely needs Purchase -- mirrors what a human clicking
    ERPNext's native "Create Material Request" button on the Sales Order
    would produce shape-wise (one MR grouping every eligible line), just
    scoped to Purchase-route shortages and quantities this module's own
    idempotency math computed, not the native button's own
    qty-requested-delivered formula.

    `schedule_date` reuses the exact same field ERPNext's own
    make_material_request() maps it from (`Sales Order Item.delivery_date`,
    sales_order.py:1104's field_map, `"delivery_date": "schedule_date"`)
    -- not a new lead-time constant. Falls back to the Sales Order's own
    header `delivery_date` only if a line somehow has none (defensive;
    every line in this app's own Sales Orders always has one, Commit
    18.2's DEFAULT_DELIVERY_LEAD_DAYS ensures that for Vendedora-created
    orders, and native Sales Order validation requires it for any order).

    `ignore_permissions=True` -- see this module's own top docstring for
    why (same Commit 18.1 pattern as shortage_service.py/
    pick_list_service.py; required once this runs inside a Vendedora's
    own restricted session via process_sales_order(), Commit 19.2, which
    this commit does not yet wire up but must already be safe for).
    """
    so_items_by_name = {item.name: item for item in so.items}

    material_request = frappe.get_doc(
        {
            "doctype": "Material Request",
            "material_request_type": MATERIAL_REQUEST_TYPE,
            "company": so.company,
            "transaction_date": nowdate(),
        }
    )

    for line in lines_to_request:
        so_item = so_items_by_name[line["sales_order_item"]]
        material_request.append(
            "items",
            {
                "item_code": line["item_code"],
                "qty": line["qty_to_request"],
                "warehouse": line["warehouse"],
                "schedule_date": so_item.delivery_date or so.delivery_date,
                "sales_order": so.name,
                "sales_order_item": line["sales_order_item"],
            },
        )

    material_request.insert(ignore_permissions=True)  # Commit 18.1 pattern -- see module docstring
    return material_request


def sync_material_requests_for_sales_order(sales_order):
    """For every line of `sales_order` where analyze_sales_order() reports
    procurement_route == "Purchase" and a genuine, Pick-List-aware
    shortage remains, ensure a draft Material Request exists covering
    exactly the net-still-needed quantity -- creating one new Material
    Request (grouping every such line) when there is anything left to
    request, doing nothing otherwise. `sales_order` may be a name or an
    already-loaded frappe.get_doc("Sales Order", ...), exactly like
    analyze_sales_order()/sync_shortage_reports_for_sales_order().

    Decision source, exactly mirroring shortage_service.py's own standing
    rule: analyze_sales_order() (Commit 12) is the only place that
    computes availability/shortage -- this function adds no parallel
    version of that math. It applies the SAME already-claimed-by-open-
    Pick-List adjustment shortage_service.py already validated (Commit
    14), reusing (not duplicating) pick_list_service.py's own
    _qty_already_claimed_by_open_pick_lists_for_so_item():
        qty_pending = max(qty_remaining - already_claimed_by_open_pick_lists, 0)
        qty_procurement_shortage = max(qty_pending - qty_available_for_pick, 0)
    This is the exact formula the Commit 19 approval required reused, not
    reimplemented -- deliberately deviating from the raw
    `line["qty_shortage"]` field for the same reason Commit 14 already
    established: a line already (fully or partially) claimed by an open
    Pick List still shows a raw qty_shortage > 0 from the analyzer on
    purpose (Commit 12), and would over-request Purchase material if used
    as-is here.

    On top of that, this module applies its OWN idempotency subtraction,
    the Compras-side equivalent of what create_pick_list_for_available_
    stock() (Commit 13) already does for Pick List:
        qty_to_request = max(qty_procurement_shortage - qty_already_claimed_by_open_material_requests_for_so_item, 0)
    using qty_already_claimed_by_open_material_requests_for_so_item()
    above (draft + submitted-and-not-Stopped Material Request Item rows
    for this exact line, Purchase type only). A line where nothing is
    left to request (already fully covered by an existing draft or
    submitted Material Request) is skipped entirely -- running this
    twice in a row with nothing changed creates nothing new.

    Manufacture and Blocked routes are never touched here -- unchanged,
    still Reporte de Faltante only (shortage_service.py).

    No Supplier is ever chosen, no Purchase Order is ever created, and
    the resulting Material Request is never submitted -- Compras reviews
    and acts on it manually. See _insert_draft_material_request()'s own
    docstring for why a direct frappe.get_doc({...}) build was used
    instead of sales_order.make_material_request().

    Returns {"created": [<material_request_name>] or [], "lines_requested":
    [<sales_order_item>, ...]} -- "created" is a list (not a single name
    or None) for the same reason shortage_service.py's summary dict uses
    lists throughout: a consistent, always-iterable shape regardless of
    whether 0 or 1 Material Request actually got created this call
    (never more than 1 today -- one call always groups every eligible
    line into a single Material Request, same shape as ERPNext's own
    native "Create Material Request" button).

    Known, deliberately unresolved gap (flagged to the user, not silently
    worked around): once a Material Request this module created is later
    submitted by Compras (turning it into real native demand,
    Sales Order Item.requested_qty included) or the underlying shortage
    changes (more stock arrives, or the shortage clears), this function
    still never updates or deletes that pre-existing document -- it only
    ever computes a new, smaller-or-larger net remainder and creates a
    fresh Material Request for whatever is still missing (never negative,
    floored at 0 like every other quantity in this module). Confirmed
    directly against material_request_item.json: `sales_order`/
    `sales_order_item` are `hidden`+`read_only` on the Item grid, so a
    human cannot type them in manually -- but ERPNext's own native
    "Create Material Request" button on the Sales Order form (a real,
    unmodified, always-available native action) populates the exact same
    fields the same way this module does, with no way to distinguish
    "created by this Engine" from "created by a human via that native
    button" using only native fields. Deliberately not resolved with a
    heuristic (e.g. a `title` convention) or a new Custom Field in this
    commit -- both were flagged as options requiring an explicit decision,
    not something to decide unilaterally here. This is why updating or
    deleting an existing draft Material Request is out of this commit's
    scope entirely; every path through this function only ever reads
    existing Material Requests (via the query above) and creates new
    ones, never writes to a pre-existing one.
    """
    so = sales_order if hasattr(sales_order, "doctype") else frappe.get_doc("Sales Order", sales_order)

    analysis = analyze_sales_order(so)

    lines_to_request = []
    for line in analysis["lines"]:
        if line["procurement_route"] != "Purchase":
            continue
        if line["qty_shortage"] <= 0:
            continue

        already_claimed_pl = _qty_already_claimed_by_open_pick_lists_for_so_item(line["sales_order_item"])
        qty_pending = max(flt(line["qty_remaining"]) - already_claimed_pl, 0.0)
        qty_procurement_shortage = max(qty_pending - flt(line["qty_available_for_pick"]), 0.0)

        if qty_procurement_shortage <= 0:
            continue

        already_claimed_mr = qty_already_claimed_by_open_material_requests_for_so_item(line["sales_order_item"])
        qty_to_request = max(qty_procurement_shortage - already_claimed_mr, 0.0)

        if qty_to_request <= 0:
            continue

        lines_to_request.append({**line, "qty_to_request": qty_to_request})

    if not lines_to_request:
        return {"created": [], "lines_requested": []}

    material_request = _insert_draft_material_request(so, lines_to_request)

    return {
        "created": [material_request.name],
        "lines_requested": [line["sales_order_item"] for line in lines_to_request],
    }
