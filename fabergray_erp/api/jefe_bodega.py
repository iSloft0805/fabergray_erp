# -*- coding: utf-8 -*-
"""Read-only supervision API for the Jefe de Bodega Page.

Every function here is a thin, read-only wrapper. The four Pick List states
(pendientes/en_alistamiento/con_faltantes/listos) are never re-bucketed here
-- get_queue() from api.bodega is the single source of truth, imported and
called directly. Nothing in this module writes, submits, or creates anything.

Same permission policy as api/bodega.py: only frappe.get_list / frappe.get_doc
+ doc.check_permission() / frappe.has_permission(), so Role Permissions AND
User Permissions (including per-Warehouse scoping) are applied by frappe
itself. No frappe.get_all, no ignore_permissions, no manual bypass -- child
table data (Pick List Item / Pick List.locations) is only ever read off a
Pick List document after check_permission("read") has been called on that
specific document, exactly like get_pick_list() already does in api/bodega.py.
"""

import frappe
from frappe.utils import flt

from fabergray_erp.api.bodega import _require_login, get_queue

OPEN_STATUS = "Abierto"


@frappe.whitelist()
def get_summary():
	"""Counts for the 5 KPI cards. Single round-trip for the whole row.

	The four Pick List counts come straight out of get_queue()'s buckets --
	no re-implementation of the bucketing rule. "Faltantes abiertos" is a
	plain, permission-scoped count on Reporte de Faltante.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	queue = get_queue()
	faltantes_abiertos = frappe.get_list(
		"Reporte de Faltante", filters={"status": OPEN_STATUS}, pluck="name"
	)

	return {
		"pendientes": len(queue["pendientes"]),
		"en_alistamiento": len(queue["en_alistamiento"]),
		"con_faltantes": len(queue["con_faltantes"]),
		"listos": len(queue["listos"]),
		"faltantes_abiertos": len(faltantes_abiertos),
	}


@frappe.whitelist()
def get_open_shortage_reports():
	"""Open Reporte de Faltante records for the "Requieren atención" section.

	Reporte de Faltante does not store item_name, only item_code. Rather than
	querying the Item doctype (Jefe de Bodega has no permission on Item) or
	using frappe.get_all on the Pick List Item child table (forbidden by
	policy for this API), the item name is read off the linked Pick List
	document itself -- after confirming read access to that specific Pick
	List -- exactly the same pattern _get_pick_list_row() already uses in
	api/bodega.py. Pick List docs are cached per request so a batch of
	reports referencing the same Pick List only loads it once.
	"""
	_require_login()
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	reports = frappe.get_list(
		"Reporte de Faltante",
		filters={"status": OPEN_STATUS},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"sales_order",
			"pick_list",
			"pick_list_item",
			"qty_solicitada",
			"qty_disponible",
			"qty_faltante",
			"shortage_reason",
			"reported_by",
			"reported_on",
		],
		order_by="reported_on desc",
		limit_page_length=0,
	)

	pick_list_cache = {}

	def _pick_list(name):
		if name not in pick_list_cache:
			doc = frappe.get_doc("Pick List", name)
			doc.check_permission("read")
			pick_list_cache[name] = doc
		return pick_list_cache[name]

	for report in reports:
		item_name = None
		if report.pick_list and report.pick_list_item:
			try:
				pl_doc = _pick_list(report.pick_list)
				rows = pl_doc.get("locations", {"name": report.pick_list_item})
				if rows:
					item_name = rows[0].item_name
			except frappe.PermissionError:
				# Report references a Pick List this user can no longer read
				# (e.g. outside their Warehouse User Permission). Fall back to
				# the report's own item_code below rather than failing the
				# whole list or fetching Item data without permission.
				item_name = None

		report["item_name"] = item_name or report.item_code
		report["reported_by_fullname"] = frappe.utils.get_fullname(report.reported_by)

	return reports


@frappe.whitelist()
def get_active_pick_lists():
	"""Pick Lists currently being picked, for the "Alistamientos activos" section.

	The bucket itself is get_queue()["en_alistamiento"] -- not recomputed here.
	items_completos/items_totales are exact counts, read off doc.locations
	after check_permission("read") on each individual authorized Pick List
	(en_alistamiento is expected to be a small list in this first version, so
	one get_doc per row is preferred over any batched/child-table query that
	would bypass the parent's permission check). If a Pick List has no rows
	at all, both fields are returned as null so the UI never fabricates a
	fraction.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	active = get_queue()["en_alistamiento"]

	for pl in active:
		doc = frappe.get_doc("Pick List", pl["name"])
		doc.check_permission("read")
		rows = doc.get("locations") or []
		total = len(rows)
		done = sum(1 for row in rows if flt(row.picked_qty) >= flt(row.stock_qty))

		pl["items_completos"] = done if total else None
		pl["items_totales"] = total if total else None
		pl["fg_started_by_fullname"] = (
			frappe.utils.get_fullname(pl["fg_started_by"]) if pl.get("fg_started_by") else None
		)

	return active
