# -*- coding: utf-8 -*-
"""Commit 12 -- Fulfillment Analyzer: pure, read-only analysis of one
submitted Sales Order, deciding per line what can be picked now, what is
short, and whether the shortfall should route to Purchase or Manufacture.

Architectural decision this module encodes (Commits 10/11, both proven with
live tests against this exact ERPNext version before writing a line of this
file): neither Sales-Order-level nor Pick-List-level Stock Reservation Entry
is usable in this app's Pick-List-centric Bodega workflow -- reserving via
either one blocks Pick List submission outright. `enable_stock_reservation`
stays `0`. Availability here is computed the same way ERPNext's own
create_pick_list() already computes it when suggesting locations for a new
Pick List: Bin.actual_qty minus whatever is already committed (picked_qty
or, once submitted, picked_qty - delivered_qty) on *other* open Pick List
Item rows for the same item+warehouse. No Stock Reservation Entry, no new
Custom Field (`fg_allocated_qty` was explicitly rejected).

This module:
- reads Sales Order / Sales Order Item / Bin / Pick List Item / Item / BOM
  (the make/buy rule itself: fabergray_erp.manufacturing, Fase 28.1);
- never writes anything, anywhere;
- never creates Reporte de Faltante, Pick List, Material Request, Work
  Order or Purchase Order;
- never changes Sales Order status;
- is not wired into any hook, job, or Custom Field yet.

Everything it returns is recomputed fresh on every call from standard
documents -- nothing here is a cache.
"""

import frappe
from frappe.query_builder import Case
from frappe.utils import flt

from erpnext.stock.doctype.pick_list.pick_list import get_actual_qty

from fabergray_erp.manufacturing import get_manufacturing_route
from fabergray_erp.warehouses import non_picking_warehouses

OPEN_PICK_LIST_STATUSES_EXCLUDED = ("Completed", "Cancelled")


def _qty_committed_by_open_pick_lists(item_code, warehouse):
	"""How much of this item+warehouse is already claimed by *other* open
	Pick Lists, using the exact same rule ERPNext's own Pick List uses
	internally when suggesting locations for a new Pick List.

	Reproduced here, not called directly, because the real logic lives in
	Pick List._get_pick_list_items() (pick_list.py) -- a private
	(underscore-prefixed), instance-bound method with no public equivalent.
	Calling a private method of another app's controller from here would be
	fragile (no compatibility guarantee across ERPNext versions) and would
	require instantiating a throwaway Pick List document just to reach it.
	The query below mirrors it field-for-field: for every Pick List Item row
	for this item+warehouse, on a Pick List that is not Completed/Cancelled
	and whose row is not itself cancelled (docstatus != 2), count
	`picked_qty - delivered_qty` once the row is submitted and has actually
	been picked (docstatus == 1 and picked_qty > 0), otherwise count the
	row's full `stock_qty` -- a still-draft row (regardless of how much of
	it has been picked so far) provisionally claims its whole suggested
	quantity, exactly like ERPNext's own version does, confirmed empirically
	in Commit 11 (Caso 3b: an unsubmitted, partially-picked Pick List still
	fully excludes its row's stock_qty from what a competing Pick List sees
	as available).

	Deliberately does NOT use `.for_update()` the way the original does --
	that row lock exists there because it runs inside an actual document
	save; this function never writes anything, so there is nothing to
	protect with a lock.
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
			(pick_list_item.item_code == item_code)
			& (pick_list_item.warehouse == warehouse)
			& ((pick_list_item.picked_qty > 0) | (pick_list_item.stock_qty > 0))
			& (pick_list.status.notin(OPEN_PICK_LIST_STATUSES_EXCLUDED))
			& (pick_list_item.docstatus != 2)
		)
	).run(as_dict=True)

	return sum(flt(row.qty) for row in rows)


def _qty_available_for_pick(item_code, warehouse, company=None):
	"""Bin.actual_qty (via get_actual_qty() -- the same public helper
	api/bodega.py already uses for qty_disponible) minus whatever other
	open Pick Lists already claim, floored at 0. Fase 28.4A.3: always 0 in
	a non-picking warehouse (Devoluciones, Cuarentena -- never a picking
	source, see pick_list_mixin.set_item_locations())."""
	if company and warehouse in non_picking_warehouses(company):
		return 0.0
	actual_qty = flt(get_actual_qty(item_code, warehouse))
	committed = _qty_committed_by_open_pick_lists(item_code, warehouse)
	return max(actual_qty - committed, 0.0)


def _procurement_route_for_item(item_code, company=None):
	"""(route, bom_no, blocking_reason) -- Purchase vs Manufacture vs
	Blocked. Fase 28.1: the rule itself now lives in ONE place,
	fabergray_erp.manufacturing.get_manufacturing_route() (native
	Item.default_material_request_type + a default BOM that
	validate_bom_for_production() accepts for this company); this is only
	the analyzer's tuple adapter, so there are never two implementations
	of the same rule. Unchanged contract: Manufacture without a resolvable
	BOM is never silently downgraded to Purchase -- it comes back Blocked
	("Missing BOM"), and any other native policy is Blocked
	("Unsupported procurement policy: ...")."""
	result = get_manufacturing_route(item_code, company=company)
	return result["route"], result["bom"], result["reason"]


def analyze_sales_order(sales_order):
	"""Analyze one submitted Sales Order and return, per stock line, exactly
	what can be picked now and what would need Purchase or Manufacture --
	without writing anything, anywhere. `sales_order` may be a name or an
	already-loaded frappe.get_doc("Sales Order", ...).
	"""
	so = sales_order if hasattr(sales_order, "doctype") else frappe.get_doc("Sales Order", sales_order)
	so.check_permission("read")

	if so.docstatus != 1:
		frappe.throw(frappe._("El análisis de abastecimiento requiere una Sales Order sometida."))

	lines = []
	for item in so.items:
		# Non-stock items never go through Pick List/Bodega -- nothing to
		# analyze. Drop-shipped lines (delivered_by_supplier) never get
		# picked from our own warehouse either -- same exclusion
		# create_pick_list()'s own should_pick_order_item() already applies.
		if not frappe.get_cached_value("Item", item.item_code, "is_stock_item"):
			continue
		if item.delivered_by_supplier:
			continue

		qty_ordered = flt(item.stock_qty)
		qty_delivered = flt(item.delivered_qty)
		# Sales Order Item.picked_qty is only updated by Pick List's own
		# update_reference_qty(), which runs on Pick List submit/cancel --
		# not while a Pick List is still a draft in progress. A
		# partially-picked *draft* Pick List correctly reads 0 here; its
		# effect is already captured in qty_available_for_pick below via the
		# Pick List Item-level query, not through this field.
		qty_picked = flt(item.picked_qty)
		qty_remaining = max(qty_ordered - qty_delivered, 0.0)

		qty_available_for_pick = _qty_available_for_pick(item.item_code, item.warehouse, so.company)
		qty_shortage = max(qty_remaining - qty_available_for_pick, 0.0)

		if qty_shortage <= 0:
			procurement_route, bom_no, blocking_reason = "Ready", None, None
		else:
			procurement_route, bom_no, blocking_reason = _procurement_route_for_item(item.item_code, so.company)

		lines.append(
			{
				"sales_order_item": item.name,
				"item_code": item.item_code,
				"warehouse": item.warehouse,
				"qty_ordered": qty_ordered,
				"qty_delivered": qty_delivered,
				"qty_picked": qty_picked,
				"qty_remaining": qty_remaining,
				"qty_available_for_pick": qty_available_for_pick,
				"qty_shortage": qty_shortage,
				"procurement_route": procurement_route,
				"bom_no": bom_no,
				"blocking_reason": blocking_reason,
			}
		)

	return {
		"sales_order": so.name,
		"lines": lines,
		"has_shortage": any(line["qty_shortage"] > 0 for line in lines),
		"purchase_required": any(line["procurement_route"] == "Purchase" for line in lines),
		"manufacturing_required": any(line["procurement_route"] == "Manufacture" for line in lines),
		"blocked": any(line["procurement_route"] == "Blocked" for line in lines),
	}
