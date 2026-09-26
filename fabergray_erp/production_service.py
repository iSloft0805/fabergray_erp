# -*- coding: utf-8 -*-
"""fabergray_erp/production_service.py -- Fase 28.2: routing a Reporte de
Faltante to Compra / Producción, and creating or reusing the native Work
Order that will make it. NOTHING is manufactured here: no Stock Entry,
no stock consumed or produced, no shortage resolved by production (28.4 /
28.5).

route_shortage(report) (called by api.jefe_bodega.route_shortage() after
its own access checks):
1. lock the report (FOR UPDATE) -- a double click / two tabs serialize;
2. Resuelto -> refused; already linked to an active Work Order ->
   returned as-is (already_routed), never a second Work Order;
3. lock the Item row -- two shortages of the same product routed at the
   same time serialize, so capacity is never allocated twice;
4. route = manufacturing.get_manufacturing_route() (the ONE make/buy rule;
   never a value from the client) and store it in procurement_route:
   - Purchase  -> the existing purchase flow continues (status untouched);
   - Blocked   -> reason stored, no Work Order, status untouched (Abierto);
   - Manufacture -> finished-goods warehouse = manufacturing.
     resolve_fg_warehouse(item) (Item Default -> Item Group Default ->
     Company.default_fg_warehouse, Fase 28.4A.3) and it must be the
     report's own warehouse (where Bodega picks);
     every component needs a valid source warehouse (native chain). Any
     problem -> Blocked with the reason, BEFORE creating anything;
5. need = qty_faltante - what purchases already received for it;
6. consolidation without ever editing a submitted Work Order's qty
   (native: qty is not allow_on_submit): reuse an open Work Order (same
   company, item, BOM, finished-goods warehouse, skip_transfer, submitted,
   Not Started / In Process) whose UNALLOCATED capacity (qty - sum of
   production_qty_allocated of its reports) covers the need; otherwise a
   new Work Order for exactly the need (qty = need; producing more later is
   allowed: the excess is free stock, never the order's);
7. the report gets work_order, production_qty_allocated = need,
   procurement_route = Manufacture and status En Proceso.

The new Work Order is submitted (skip_transfer=1, so no Material Transfer
to WIP; submit only books projections -- planned_qty and
reserved_qty_for_production -- never stock or GL; proven in Fase 28.1 /
28.2 tests), ready for Producción.

The routing fields of Reporte de Faltante are written ONLY through this
module (private token, enforced by the controller): Desk, frappe.client
and Bodega's own write permission cannot set them."""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from fabergray_erp import manufacturing

REPORT_DOCTYPE = "Reporte de Faltante"
STATUS_OPEN = "Abierto"
STATUS_IN_PROGRESS = "En Proceso"
STATUS_RESOLVED = "Resuelto"
OPEN_WORK_ORDER_STATUSES = ("Not Started", "In Process")
ROUTING_FIELDS = ("procurement_route", "procurement_route_reason", "work_order", "production_qty_allocated")

_SERVICE_TOKEN = object()


class ShortageRoutingError(frappe.ValidationError):
	pass


def _authorize(doc):
	doc.flags.fg_production_service_token = _SERVICE_TOKEN
	return doc


def is_authorized(doc):
	return doc.flags.get("fg_production_service_token") is _SERVICE_TOKEN


def is_active_work_order(work_order):
	row = frappe.db.get_value("Work Order", work_order, ["docstatus", "status"], as_dict=True)
	return bool(row) and row.docstatus == 1 and row.status in OPEN_WORK_ORDER_STATUSES


def received_by_purchase(report_name):
	"""Units already received for this report through the purchase flow
	(submitted Material Receipt Stock Entries linked by fg_shortage_report
	-- the same source api.jefe_bodega uses). System read. A receipt can
	never predate its own report: `creation >= report.creation` keeps a
	re-used report name (Frappe reverts a naming series when the last
	document is deleted) from inheriting another report's receipts."""
	return flt(
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(d.qty), 0)
			FROM `tabStock Entry` e
			INNER JOIN `tabStock Entry Detail` d ON d.parent = e.name
			INNER JOIN `tabReporte de Faltante` r ON r.name = e.fg_shortage_report
			WHERE e.fg_shortage_report = %s AND e.docstatus = 1 AND e.purpose = 'Material Receipt'
				AND e.creation >= r.creation
			""",
			(report_name,),
		)[0][0]
	)


def _allocated_on(work_order, for_update=False):
	"""Units of `work_order` already allocated to shortages. for_update=True
	is a LOCKING read: under REPEATABLE READ a plain read can return a
	snapshot taken before a concurrent routing committed its allocation
	(proven by test_concurrent_routing_never_overallocates_a_work_order:
	15 + 8 were both allocated on a Work Order of 20). MariaDB's snapshot
	isolation turns such a conflict into a retryable error (1020 ->
	QueryDeadlockError), and the caller's bounded retry re-runs the routing
	with fresh data."""
	lock = " FOR UPDATE" if for_update else ""
	rows = frappe.db.sql(
		f"SELECT production_qty_allocated FROM `tab{REPORT_DOCTYPE}` WHERE work_order = %s{lock}",
		(work_order,),
	)
	return flt(sum(flt(r[0]) for r in rows))


def _work_order_with_capacity(item_code, bom_no, company, fg_warehouse, need):
	"""An open Work Order that can absorb `need` without changing its qty
	(rows locked, oldest first), or None."""
	candidates = frappe.db.sql(
		"""
		SELECT name, qty FROM `tabWork Order`
		WHERE company = %s AND production_item = %s AND bom_no = %s AND fg_warehouse = %s
			AND skip_transfer = 1 AND docstatus = 1 AND status IN %s
		ORDER BY creation ASC, name ASC
		FOR UPDATE
		""",
		(company, item_code, bom_no, fg_warehouse, OPEN_WORK_ORDER_STATUSES),
		as_dict=True,
	)
	for wo in candidates:
		if flt(wo.qty) - _allocated_on(wo.name, for_update=True) >= flt(need) - 1e-9:
			return wo.name
	return None


def _create_work_order(item_code, bom_no, company, fg_warehouse, qty):
	"""Native Work Order: skip_transfer=1 (consume straight from each
	component's source warehouse at manufacture time), required_items and
	their source warehouses computed by ERPNext itself. System action,
	submitted (the caller already passed its role/company checks)."""
	wo = frappe.new_doc("Work Order")
	wo.update(
		{
			"production_item": item_code,
			"bom_no": bom_no,
			"company": company,
			"qty": qty,
			"fg_warehouse": fg_warehouse,
			"skip_transfer": 1,
			"use_multi_level_bom": 0,
			"planned_start_date": now_datetime(),
		}
	)
	wo.flags.ignore_permissions = True
	wo.insert()
	missing = [row.item_code for row in wo.required_items if not row.source_warehouse]
	if missing:  # defensive: resolve_component_warehouses() already checked the same chain
		frappe.throw(
			_("Componentes sin bodega de origen: {0}").format(", ".join(missing)), ShortageRoutingError
		)
	wo.submit()
	return wo


def _save_routing(report, **values):
	report.update(values)
	_authorize(report)
	report.flags.ignore_permissions = True
	report.save()


def route_shortage(report_name, company):
	"""See the module docstring. Returns
	{route, reason, problems, work_order, allocated_qty, created_work_order,
	already_routed, status}."""
	frappe.db.get_value(REPORT_DOCTYPE, report_name, "name", for_update=True)
	report = frappe.get_doc(REPORT_DOCTYPE, report_name, for_update=True)

	if report.status == STATUS_RESOLVED:
		frappe.throw(_("Este Reporte de Faltante ya está Resuelto."), ShortageRoutingError)
	if report.work_order and is_active_work_order(report.work_order):
		return _response(report, already_routed=True)

	frappe.db.get_value("Item", report.item_code, "name", for_update=True)
	route = manufacturing.get_manufacturing_route(report.item_code, company)

	if route["route"] == manufacturing.ROUTE_PURCHASE:
		_save_routing(report, procurement_route="Purchase", procurement_route_reason=None, work_order=None, production_qty_allocated=0)
		return _response(report)

	if route["route"] == manufacturing.ROUTE_BLOCKED:
		_save_routing(report, procurement_route="Blocked", procurement_route_reason=route["reason"], work_order=None, production_qty_allocated=0)
		return _response(report, problems=route["problems"])

	bom_no = route["bom"]
	fg_warehouse, problems = manufacturing.resolve_fg_warehouse(report.item_code, company)
	if fg_warehouse and fg_warehouse != report.warehouse:
		problems.append(
			f"el faltante es de la bodega {report.warehouse} y la producción entrega en {fg_warehouse}"
		)
	_rows, component_problems = manufacturing.resolve_component_warehouses(bom_no, company)
	problems += component_problems
	if problems:
		_save_routing(
			report,
			procurement_route="Blocked",
			procurement_route_reason="No se puede producir: " + "; ".join(problems),
			work_order=None,
			production_qty_allocated=0,
		)
		return _response(report, problems=problems)

	need = max(flt(report.qty_faltante) - received_by_purchase(report.name), 0.0)
	if need <= 0:
		frappe.throw(_("Este faltante ya no tiene cantidad pendiente por producir."), ShortageRoutingError)

	work_order = _work_order_with_capacity(report.item_code, bom_no, company, fg_warehouse, need)
	created = False
	if not work_order:
		work_order = _create_work_order(report.item_code, bom_no, company, fg_warehouse, need).name
		created = True

	_save_routing(
		report,
		procurement_route="Manufacture",
		procurement_route_reason=None,
		work_order=work_order,
		production_qty_allocated=need,
		status=STATUS_IN_PROGRESS,
	)
	return _response(report, created_work_order=created)


def _response(report, already_routed=False, created_work_order=False, problems=None):
	return {
		"shortage_report": report.name,
		"route": report.procurement_route,
		"reason": report.procurement_route_reason,
		"problems": problems or [],
		"work_order": report.work_order,
		"allocated_qty": flt(report.production_qty_allocated),
		"created_work_order": created_work_order,
		"already_routed": already_routed,
		"status": report.status,
	}


def on_work_order_cancel(doc, method=None):
	"""Work Order.on_cancel (hooks.py). A cancelled Work Order never
	leaves a shortage misleadingly "En Proceso": every NOT resolved report
	linked to it loses the link and its allocation, goes back to Abierto
	(or stays En Proceso when purchases already delivered part of it) and
	keeps a note; the route is cleared so the Jefe de Bodega routes it
	again. Resolved reports keep their history untouched."""
	for name in frappe.get_all(
		REPORT_DOCTYPE, filters={"work_order": doc.name, "status": ["!=", STATUS_RESOLVED]}, pluck="name"
	):
		report = frappe.get_doc(REPORT_DOCTYPE, name, for_update=True)
		_save_routing(
			report,
			procurement_route=None,
			procurement_route_reason=_("Orden de producción {0} cancelada; volver a enrutar.").format(doc.name),
			work_order=None,
			production_qty_allocated=0,
			status=STATUS_IN_PROGRESS if received_by_purchase(report.name) > 0 else STATUS_OPEN,
		)
