# -*- coding: utf-8 -*-
"""Commit 13 -- create_pick_list_for_available_stock(): the first write that
turns the Commit 12 analyzer's read-only decision into a real, native Pick
List, capped at what is actually available -- nothing beyond that. Not
wired into any hook, job, or UI yet; a future Fulfillment Engine calls this
directly, the same way it would call `analyze_sales_order()` first.

Flow this closes: Sales Order -> analyze_sales_order() -> Pick List ->
/app/bodega. Purchase/Manufacture routing (the rest of what the analyzer
already reports) is still out of scope -- this only ever creates a Pick
List, never a Material Request, Work Order, Purchase Order, or Reporte de
Faltante.
"""

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import Case
from frappe.utils import flt

from erpnext.stock.doctype.packed_item.packed_item import is_product_bundle
from erpnext.stock.get_item_details import get_bin_details

from fabergray_erp.fulfillment.analyzer import OPEN_PICK_LIST_STATUSES_EXCLUDED, analyze_sales_order


def _qty_already_claimed_by_open_pick_lists_for_so_item(sales_order_item):
    """How much of THIS Sales Order Item specifically is already sitting in
    an open (draft or submitted-but-undelivered) Pick List Item row.

    This is deliberately a second, narrower query, not a reuse of
    analyzer._qty_committed_by_open_pick_lists() -- and not a competing
    availability formula either. analyze_sales_order()'s qty_remaining is
    intentionally delivery-only (proven by Commit 12's own
    test_partially_picked_sales_order_reflects_real_picked_qty: it stays
    at the full undelivered qty even right after a Pick List picks and
    submits part of it) -- it does not, and should not, shrink just
    because *some* open Pick List already exists for the line, since a
    shortage still needs to show up as a shortage regardless of whether a
    Pick List is already chasing part of it. Idempotency for *this*
    function needs the opposite: how much of this exact line's remaining
    need has *already* been handed to an open Pick List, so a second run
    does not hand out the same paper twice. Same query shape as the
    analyzer's (same OPEN_PICK_LIST_STATUSES_EXCLUDED constant, same
    picked_qty-vs-stock_qty CASE), reused rather than reinvented, just
    filtered by `sales_order_item` -- a native Pick List Item field --
    instead of `item_code + warehouse`.
    """
    pick_list = frappe.qb.DocType("Pick List")
    pick_list_item = frappe.qb.DocType("Pick List Item")

    rows = (
        frappe.qb.from_(pick_list_item)
        .inner_join(pick_list)
        .on(pick_list.name == pick_list_item.parent)
        .select(
            Case()
            .when(
                (pick_list_item.picked_qty > 0) & (pick_list_item.docstatus == 1),
                pick_list_item.picked_qty - pick_list_item.delivered_qty,
            )
            .else_(pick_list_item.stock_qty)
            .as_("qty")
        )
        .where(
            (pick_list_item.sales_order_item == sales_order_item)
            & (pick_list.status.notin(OPEN_PICK_LIST_STATUSES_EXCLUDED))
            & (pick_list_item.docstatus != 2)
        )
    ).run(as_dict=True)

    return sum(flt(row.qty) for row in rows)


def _cap_rows_for_so_item(pick_list, sales_order_item, max_stock_qty):
    """Trim (or drop) the location rows create_pick_list() proposed for one
    Sales Order Item down to at most `max_stock_qty`, in place, before
    insert. Mirrors the same "consume a running total across ordered
    rows" technique get_items_with_location_and_quantity() (pick_list.py)
    already uses to allocate a quantity across candidate locations --
    applied here to shrink rather than grow, so a batch/serial/multi-
    warehouse split stays physically accurate (no row ends up claiming
    more than a single location actually offered)."""
    remaining = max_stock_qty
    rows_to_remove = []

    for row in pick_list.get("locations"):
        if row.sales_order_item != sales_order_item:
            continue
        if remaining <= 0:
            rows_to_remove.append(row)
            continue
        if row.stock_qty > remaining:
            row.stock_qty = remaining
            row.qty = remaining / (row.conversion_factor or 1)
            remaining = 0
        else:
            remaining -= row.stock_qty

    for row in rows_to_remove:
        pick_list.remove(row)


def _validate_sales_order_has_no_reserved_stock(so_name):
    """Reproduced verbatim from erpnext...sales_order.create_pick_list()'s
    local `validate_sales_order()` -- not importable (a closure inside that
    function). In this app `enable_stock_reservation` stays `0` (Commits
    10/11), so `stock_reserved_qty` is always 0 in practice -- this check
    is a no-op safety net here, kept only for exact behavioural parity
    with the native function rather than assumed to never matter."""
    so = frappe.get_doc("Sales Order", so_name)
    for item in so.items:
        if item.stock_reserved_qty > 0:
            frappe.throw(
                _(
                    "Cannot create a pick list for Sales Order {0} because it has reserved stock. "
                    "Please unreserve the stock in order to create a pick list."
                ).format(frappe.bold(so_name))
            )


def _update_item_quantity_for_pick_list(source, target, source_parent) -> None:
    """Reproduced verbatim from create_pick_list()'s local
    `update_item_quantity()` postprocess -- not importable. `get_bin_details`
    itself is imported, not duplicated (erpnext.stock.get_item_details)."""
    picked_qty = flt(source.picked_qty) / (flt(source.conversion_factor) or 1)
    qty_to_be_picked = flt(source.qty) - max(picked_qty, flt(source.delivered_qty))

    target.qty = qty_to_be_picked
    target.stock_qty = qty_to_be_picked * flt(source.conversion_factor)

    bin_details = get_bin_details(source.item_code, source.warehouse, source_parent.company)
    target.actual_qty = bin_details.get("actual_qty")
    target.company_total_stock = bin_details.get("company_total_stock")


def _should_pick_sales_order_item(item) -> bool:
    """Reproduced verbatim from create_pick_list()'s local
    `should_pick_order_item()` -- not importable. `is_product_bundle` itself
    is imported, not duplicated (erpnext.stock.doctype.packed_item.packed_item)."""
    return (
        abs(item.delivered_qty) < abs(item.qty)
        and item.delivered_by_supplier != 1
        and not is_product_bundle(item.item_code)
    )


def _create_pick_list_ignoring_permissions(so_name):
    """ERPNext compatibility adapter -- mirrors Sales Order.create_pick_list
    mapping because the native wrapper does not expose
    get_mapped_doc(ignore_permissions=True).

    Commit 18.1 -- why this exists: the native `create_pick_list()`
    (erpnext.selling.doctype.sales_order.sales_order) is a thin wrapper
    around `frappe.model.mapper.get_mapped_doc()` that ALWAYS calls it with
    `ignore_permissions=False` (hardcoded, not a parameter `create_pick_list()`
    forwards) -- meaning `get_mapped_doc()` itself checks
    `target_doc.check_permission("create")` on the new Pick List *and*
    `source_doc.check_permission("read")` on the Sales Order (mapper.py,
    confirmed by reading it directly) *before* `create_pick_list()` ever
    returns control to us. That happens too early for our own
    `pick_list.insert(ignore_permissions=True)` below to matter -- by the
    time we'd call it, the native function has already thrown. There is no
    other lever: no global `frappe.flags.ignore_permissions` is consulted
    anywhere in this exact chain (checked directly in
    frappe/permissions.py, frappe/model/document.py and
    frappe/model/mapper.py -- none reference it), and pre-building our own
    `target_doc` with `.flags.ignore_permissions` already set does not
    help either, since `get_mapped_doc()`'s check is gated purely on its
    own `ignore_permissions` *parameter*, independent of anything already
    set on the document it is given.

    Minimal surface actually reproduced (measured against
    apps/erpnext/erpnext/selling/doctype/sales_order/sales_order.py's real
    `create_pick_list()`, ~78 lines total):
    - IMPORTED, not duplicated: `get_mapped_doc` itself (frappe.model.mapper
      -- the exact function we need to call differently), `is_product_bundle`
      (erpnext.stock.doctype.packed_item.packed_item), `get_bin_details`
      (erpnext.stock.get_item_details) -- all real, public, module-level
      ERPNext/Frappe functions, not local closures, so nothing about *their*
      behaviour is duplicated.
    - REPRODUCED (could not be imported -- each is a closure defined inside
      create_pick_list()'s own function body, invisible outside it):
      `_validate_sales_order_has_no_reserved_stock()` (~10 effective lines),
      `_update_item_quantity_for_pick_list()` (~8 effective lines),
      `_should_pick_sales_order_item()` (~5 effective lines), and the
      "Sales Order" + "Sales Order Item" table_map entries themselves
      (~10 lines of configuration). ~33 effective lines of ERPNext's own
      behaviour replicated, out of ~78 in the native function.
    - DELIBERATELY NOT reproduced: the "Packed Item" table_map entry and
      its `update_packed_item_qty()` postprocess (product bundle support).
      This app's Fulfillment Engine (analyze_sales_order() included) has
      never processed product bundles at any stage -- not a new gap this
      commit introduces, staying consistent with an existing, undocumented
      scope boundary rather than silently expanding it while also reducing
      how much native behaviour needs to be kept in sync by hand.
    - NOT reproduced at all, still fully native and unduplicated: Pick
      List's own `set_item_locations()` (still called below exactly as
      the native function calls it), all warehouse/Bin/availability
      lookups inside it, and the picked-qty-aware location suggestion
      logic (Commits 11/13's whole finding) -- none of that lives in
      `create_pick_list()` itself, so none of it needed reproducing here.

    Drift detector: no source hash of ERPNext's own file (would make
    routine ERPNext upgrades fail this app's tests for unrelated reasons).
    Instead, test_pick_list_service.py's own parity test drives both the
    real native `create_pick_list()` and this adapter against equivalent
    Sales Orders, as a privileged user, and asserts they produce the same
    sales_order/item_code/sales_order_item/qty/stock_qty/warehouse/uom per
    line -- a behavioural tripwire that fails loudly if a future ERPNext
    version changes this mapping in a way that matters.

    Never `@frappe.whitelist()`-ed, never called by an interactive API --
    only create_pick_list_for_available_stock() (Fulfillment Engine
    automation only) calls this. `ignore_permissions=True` exists solely
    on this one call to `get_mapped_doc()`, nowhere else.
    """
    _validate_sales_order_has_no_reserved_stock(so_name)

    doc = get_mapped_doc(
        "Sales Order",
        so_name,
        {
            "Sales Order": {
                "doctype": "Pick List",
                "field_map": {"set_warehouse": "parent_warehouse"},
                "validation": {"docstatus": ["=", 1]},
            },
            "Sales Order Item": {
                "doctype": "Pick List Item",
                "field_map": {"parent": "sales_order", "name": "sales_order_item"},
                "postprocess": _update_item_quantity_for_pick_list,
                "condition": _should_pick_sales_order_item,
            },
        },
        ignore_permissions=True,
    )
    doc.purpose = "Delivery"
    doc.set_item_locations()  # fully native -- see docstring above

    return doc


def create_pick_list_for_available_stock(sales_order):
    """Create one native Pick List for whatever part of `sales_order` can
    actually be picked right now, using ERPNext's own Sales Order -> Pick
    List mapping (via `_create_pick_list_ignoring_permissions()` above --
    an internal adapter that reproduces only the minimal, non-importable
    mapping configuration `create_pick_list()` itself uses, so this call
    can run with `ignore_permissions=True`, Commit 18.1; this app's own
    test fixtures still use the real, unmodified native `create_pick_list()`
    directly, since interactive/test contexts have no reason to need the
    bypass) -- not a hand-built Pick List document.

    `sales_order` may be a name or an already-loaded
    frappe.get_doc("Sales Order", ...), exactly like analyze_sales_order().

    Decision source: analyze_sales_order() (Commit 12) is the only place
    that computes availability/shortage -- this function adds no
    parallel version of that formula. Per line it only asks one further,
    narrower question analyze_sales_order() deliberately does not answer
    (see _qty_already_claimed_by_open_pick_lists_for_so_item() above):
    how much of this exact line is already sitting in an open Pick List,
    so the same paper is never handed out twice. Everything about *where*
    stock actually is (warehouse/batch/serial) still comes from ERPNext's
    own mapper -- this function only ever trims the quantities it
    proposed, never invents new ones.

    Returns the inserted Pick List document, or None if there is currently
    nothing pickable (every line already fully covered by picked/
    delivered/already-open-Pick-List quantity, or zero stock available
    everywhere).

    Idempotency ("Determina la cantidad todavía pendiente de asignar
    considerando: picked_qty, Pick Lists draft/open ya creados, Pick
    Lists submitted, delivered_qty"): for each Sales Order Item,
        qty_still_needed = max(qty_remaining - qty_already_claimed_by_open_pick_lists_for_so_item, 0)
        qty_to_pick      = min(qty_still_needed, qty_available_for_pick)
    where `qty_remaining` and `qty_available_for_pick` come straight from
    analyze_sales_order() and already account for delivered_qty and for
    stock committed to *other* open Pick Lists (any Sales Order) via the
    same native relations Commits 11/12 established. No new technical
    idempotency field was added, per the standing instruction not to add
    one until proven necessary -- native relations (Pick List Item <->
    Sales Order Item <-> Bin) already prove it here, with a passing test
    for every required scenario, including a case where a prior *open*
    (not yet submitted) Pick List for the same line coexists with newly
    arrived stock and only the genuine remainder gets offered.

    Concurrency: see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 13 -- known
    concurrency window" for the precise, deliberately unsolved race this
    function inherits from ERPNext's own Pick List creation (no locking
    protects the read-then-insert sequence, in this function or in the
    native "Create Pick List" button alike) and why it is safe to leave
    open rather than patched ad hoc. In short: `Pick List.before_save()`
    (pick_list.py) unconditionally re-runs `set_item_locations()` against
    live state right before writing -- so this function checks for an
    empty result *after* `insert()`, not before, and removes the draft it
    just created if the live re-derivation found nothing left. That closes
    the race for two calls sharing one database connection/transaction
    (proven by test); it does **not** prove two independent, truly
    concurrent transactions are safe -- each gets its own MVCC read
    snapshot, and neither has to see the other's not-yet-committed insert.
    """
    so_name = sales_order.name if hasattr(sales_order, "doctype") else sales_order

    analysis = analyze_sales_order(sales_order)

    qty_to_pick_by_so_item = {}
    for line in analysis["lines"]:
        already_claimed = _qty_already_claimed_by_open_pick_lists_for_so_item(line["sales_order_item"])
        qty_still_needed = max(line["qty_remaining"] - already_claimed, 0.0)
        qty_to_pick_by_so_item[line["sales_order_item"]] = min(qty_still_needed, line["qty_available_for_pick"])

    if not any(qty > 0 for qty in qty_to_pick_by_so_item.values()):
        return None

    pick_list = _create_pick_list_ignoring_permissions(so_name)

    for sales_order_item, qty_to_pick in qty_to_pick_by_so_item.items():
        _cap_rows_for_so_item(pick_list, sales_order_item, qty_to_pick)

    if not pick_list.get("locations"):
        return None

    # Commit 18.1: this function is Fulfillment Engine automation only,
    # never called by an interactive API -- Bodega's own Pick List
    # creation goes through the native "Create Pick List" mapper directly
    # (fixtures.pick_list_for() mirrors the real button), not through
    # here. ignore_permissions=True is required for the real, tested
    # scenario of a Vendedora submitting her own Sales Order: she is
    # intentionally granted zero permission on Pick List, yet this must
    # still succeed as a consequence of her already-authorized Sales
    # Order submit -- same native pattern used throughout this module
    # (see shortage_service.py's module docstring for the full writeup
    # and the ERPNext core precedent). frappe.session.user is never
    # touched, so the resulting Pick List's `owner` still correctly
    # reflects whoever actually submitted the Sales Order.
    pick_list.insert(ignore_permissions=True)

    if not pick_list.get("locations"):
        # before_save() re-derived against live state at the moment of
        # writing and found nothing left -- most likely because a
        # concurrent process claimed it via another open Pick List in the
        # meantime. Fail closed: remove the now-useless empty draft rather
        # than leaving it behind.
        pick_list.delete(ignore_permissions=True)
        return None

    return pick_list
