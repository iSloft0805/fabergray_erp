# -*- coding: utf-8 -*-
"""Commit 21.2 -- strictly read-only API layer for the future Page
Facturación.

Same permission policy as every other interactive module in this app
(api/bodega.py, api/jefe_bodega.py, api/ventas.py, api/cotizaciones.py):
only `frappe.get_list` / `frappe.get_doc` + `doc.check_permission()` /
`frappe.has_permission()` -- Role Permissions apply automatically, nothing
here uses `frappe.get_all`, `ignore_permissions` or `frappe.set_user`.
Facturación is a shared queue (no if_owner anywhere in Commit 21.1's grant),
so unlike api/ventas.py/api/cotizaciones.py there is no owner-scoping to
preserve -- every Facturación user sees the same queue.

Unlike Ventas/Cotizaciones, Facturación's own Custom DocPerm grant (Commit
21.1) explicitly INCLUDES seeing money: `rate`/`amount` below are read
straight off the real, already-submitted Sales Order Item row a Pick List
Item points to (`Pick List Item.sales_order_item`) -- never off Item Price,
which can change after the order was placed and would silently misprice an
invoice built from a Pick List. No `.as_dict()` anywhere -- every response is
built field-by-field, same discipline the rest of the app uses even where
(like here) there is no economic-data allowlist to enforce, just to keep one
consistent, auditable style.

Read-only end to end: no doc.save()/insert()/submit()/cancel()/db_set()
anywhere in this module. generate_invoice(), a checklist workflow, the Page
itself, any new Custom Field, and any new hook are all explicitly out of
scope for this commit -- see the Commit 21.2 brief.
"""

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from erpnext.stock.doctype.pick_list.pick_list import get_actual_qty

from fabergray_erp.api.bodega import OPEN_SHORTAGE_STATUSES, _require_login
from fabergray_erp.sales_order_naming import root_commercial_name

#: The corrected queue-entry contract from Commit 21.1's live audit,
#: applied everywhere in this module: a Pick List belongs to Facturación's
#: queue iff it is submitted AND not yet fully delivered. `Pick
#: List.status == "Completed"` is deliberately never used -- for a
#: purpose="Delivery" Pick List that status means "already fully invoiced,
#: nothing left to do" (erpnext/controllers/status_updater.py's own
#: status_map), the opposite of "ready to invoice".
_QUEUE_FILTERS = {"docstatus": 1, "delivery_status": ["!=", "Fully Delivered"]}


def _sales_order_of(pick_list_doc):
	"""The (first) Sales Order a Pick List's locations reference -- same
	"first non-null match" reasoning api.bodega.get_queue() already uses for
	its own card display, not a multi-SO resolution of any kind. A Pick
	List spanning more than one Sales Order is a real possibility this
	function does not reject (only get_pick_list_for_facturacion() below
	does, explicitly, per the brief) -- it is still shown in the queue list,
	just labelled by whichever Sales Order its first row happens to
	reference."""
	return next((row.sales_order for row in pick_list_doc.get("locations") if row.sales_order), None)


@frappe.whitelist()
def get_facturacion_summary():
	"""KPI counts for the future Page Facturación's dashboard header.

	pendientes/parciales are disjoint subsets of the queue (Pick
	List.delivery_status has exactly three native values -- Not Delivered /
	Partly Delivered / Fully Delivered -- so together they equal the whole
	queue). facturados_hoy counts real, already-submitted Sales Invoices,
	never a draft or a Pick List state. con_incidencia is the queue
	intersected with "has an open Reporte de Faltante" -- read via
	frappe.get_list (Facturación's own read=1 grant on Reporte de Faltante,
	added in this commit alongside this module, see the Commit 21.2 report)
	so Role Permissions apply exactly the same way they do everywhere else
	in this app.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)
	frappe.has_permission("Sales Invoice", "read", throw=True)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	pendientes = frappe.get_list(
		"Pick List", filters={"docstatus": 1, "delivery_status": "Not Delivered"}, pluck="name"
	)
	parciales = frappe.get_list(
		"Pick List", filters={"docstatus": 1, "delivery_status": "Partly Delivered"}, pluck="name"
	)

	# Child-table condition ("Sales Invoice Item" as a filter's own
	# doctype, differing from the main "Sales Invoice") makes
	# frappe.get_list auto-join Sales Invoice Item on `parent`, exactly
	# like a report-builder filter. "is set" means "not null and not
	# empty", i.e. genuinely linked to a Pick List -- never a plain
	# invoice with no Pick List origin at all. `distinct=True` because
	# an invoice with more than one against_pick_list line would
	# otherwise be joined in (and counted) once per matching line.
	facturados_hoy = frappe.get_list(
		"Sales Invoice",
		filters=[
			["Sales Invoice", "docstatus", "=", 1],
			["Sales Invoice", "update_stock", "=", 1],
			["Sales Invoice", "posting_date", "=", nowdate()],
			["Sales Invoice Item", "against_pick_list", "is", "set"],
		],
		pluck="name",
		distinct=True,
	)

	queue_names = frappe.get_list("Pick List", filters=_QUEUE_FILTERS, pluck="name")
	con_incidencia = set()
	if queue_names:
		con_incidencia = set(
			frappe.get_list(
				"Reporte de Faltante",
				filters={"pick_list": ["in", queue_names], "status": ["in", OPEN_SHORTAGE_STATUSES]},
				pluck="pick_list",
			)
		)

	return {
		"pendientes": len(pendientes),
		"parciales": len(parciales),
		"facturados_hoy": len(facturados_hoy),
		"con_incidencia": len(con_incidencia),
	}


@frappe.whitelist()
def get_pending_pick_lists():
	"""Facturación's shared queue: every submitted Pick List not yet fully
	delivered, regardless of who started/picked it -- no if_owner anywhere
	(Commit 21.1's grant is deliberately not owner-scoped).

	One get_doc()+check_permission("read") per Pick List (never
	frappe.get_all on the child table) -- same reasoning
	api.jefe_bodega.get_active_pick_lists() already documents: this queue
	is expected to stay small, so a batched-but-permission-bypassing query
	is not worth it. `total_qty` is picked units, summed over
	`locations` -- Pick List (unlike Sales Order/Quotation) has no native
	top-level total_qty field of its own.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	pick_lists = frappe.get_list(
		"Pick List",
		filters=_QUEUE_FILTERS,
		fields=[
			"name",
			"customer",
			"customer_name",
			"delivery_status",
			"per_delivered",
			"fg_started_by",
			"fg_started_on",
		],
		order_by="modified desc",
		limit_page_length=0,
	)

	names = [pl.name for pl in pick_lists]
	shortage_pick_lists = set()
	if names:
		shortage_pick_lists = set(
			frappe.get_list(
				"Reporte de Faltante",
				filters={"pick_list": ["in", names], "status": ["in", OPEN_SHORTAGE_STATUSES]},
				pluck="pick_list",
			)
		)

	result = []
	for pl in pick_lists:
		doc = frappe.get_doc("Pick List", pl.name)
		doc.check_permission("read")
		rows = doc.get("locations") or []

		sales_order = _sales_order_of(doc)
		fecha = (
			frappe.db.get_value("Sales Order", sales_order, "transaction_date") if sales_order else None
		)

		result.append(
			{
				"name": pl.name,
				"sales_order": sales_order,
				"commercial_name": root_commercial_name(sales_order) if sales_order else None,
				"cliente": pl.customer,
				"customer_name": pl.customer_name,
				"fecha": fecha,
				"item_count": len(rows),
				"total_qty": sum(flt(row.picked_qty) for row in rows),
				"delivery_status": pl.delivery_status,
				"per_delivered": flt(pl.per_delivered),
				"fg_started_by": pl.fg_started_by,
				"fg_started_by_fullname": (
					frappe.utils.get_fullname(pl.fg_started_by) if pl.fg_started_by else None
				),
				"fg_started_on": pl.fg_started_on,
				"has_open_shortage": pl.name in shortage_pick_lists,
			}
		)

	return result


@frappe.whitelist()
def get_pick_list_for_facturacion(name):
	"""Non-mutating detail view of one Pick List, for the future "Nueva
	factura" screen -- loads and validates, never writes (no save/submit/
	db_set anywhere in this function or reachable from it).

	Rejects, in order: no real read permission (check_permission raises
	frappe.PermissionError), not submitted, already fully delivered, or
	spanning more than one Sales Order (the guardrail Commit 21.1 already
	proved detectable -- this is its first real production use;
	generate_invoice() itself still does not exist).

	Per-line `rate` is read off the real Sales Order Item row (indexed out
	of `so_doc.get("items", ...)` after `so_doc.check_permission("read")`
	-- Commit 21.1's permission model: Facturación reads Sales Order for
	exactly this), never off current Item Price --
	so a later Item Price change can never retroactively change what an
	already-placed order is invoiced at. `amount` is computed here
	(qty_to_invoice * rate), not read from anywhere -- there is no native
	field that already means "amount for the still-pending portion of a
	partially delivered line". `actual_qty` (informative only) never
	influences `qty_to_invoice` -- confirmed by construction: qty_to_invoice
	is computed from picked_qty/delivered_qty alone, actual_qty is fetched
	afterwards and only added to the returned dict.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", name)
	pl.check_permission("read")

	if pl.docstatus != 1:
		frappe.throw(_("Este Pick List no está sometido; no puede facturarse todavía."))

	if pl.delivery_status == "Fully Delivered":
		frappe.throw(_("Este Pick List ya fue facturado por completo; no queda nada pendiente."))

	distinct_sales_orders = {row.sales_order for row in pl.get("locations") if row.sales_order}
	if len(distinct_sales_orders) > 1:
		frappe.throw(
			_(
				"Este Pick List está asociado a más de una Orden de Venta ({0}); "
				"Facturación todavía no soporta facturación multi-orden."
			).format(", ".join(sorted(distinct_sales_orders)))
		)

	sales_order = next(iter(distinct_sales_orders), None)
	so_doc = None
	if sales_order:
		so_doc = frappe.get_doc("Sales Order", sales_order)
		so_doc.check_permission("read")

	rows = []
	for row in pl.get("locations"):
		qty_to_invoice = flt(row.picked_qty) - flt(row.delivered_qty)

		rate = 0.0
		if row.sales_order_item and so_doc:
			so_item = so_doc.get("items", {"name": row.sales_order_item})
			if so_item:
				rate = flt(so_item[0].rate)

		rows.append(
			{
				"row_name": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"warehouse": row.warehouse,
				"picked_qty": flt(row.picked_qty),
				"delivered_qty": flt(row.delivered_qty),
				"qty_to_invoice": qty_to_invoice,
				"actual_qty": flt(get_actual_qty(row.item_code, row.warehouse)),
				"rate": rate,
				"amount": qty_to_invoice * rate,
			}
		)

	return {
		"pick_list": pl.name,
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
		"sales_order": sales_order,
		"customer": pl.customer,
		"customer_name": pl.customer_name,
		"fecha": so_doc.transaction_date if so_doc else None,
		"fg_started_by": pl.fg_started_by,
		"fg_started_by_fullname": (
			frappe.utils.get_fullname(pl.fg_started_by) if pl.fg_started_by else None
		),
		"fg_started_on": pl.fg_started_on,
		"delivery_status": pl.delivery_status,
		"per_delivered": flt(pl.per_delivered),
		"rows": rows,
	}
