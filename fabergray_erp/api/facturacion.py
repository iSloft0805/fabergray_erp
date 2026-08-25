# -*- coding: utf-8 -*-
"""api/facturacion.py -- interactive API layer for the future Page
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
21.1) explicitly INCLUDES seeing money: `rate`/`amount`/`grand_total` below
are read straight off the real, already-submitted Sales Order Item row a
Pick List Item points to (`Pick List Item.sales_order_item`) -- never off
Item Price, which can change after the order was placed and would silently
misprice an invoice built from a Pick List. No `.as_dict()` anywhere -- every
response is built field-by-field, same discipline the rest of the app uses
even where (like here) there is no economic-data allowlist to enforce, just
to keep one consistent, auditable style.

Commit 21.1/21.2 were read-only end to end. Commit 21.3 adds the one write
this module has: `generate_invoice()`, which does NOT reimplement invoice
creation -- it is a thin, validating wrapper around ERPNext's own audited
mapper, `erpnext.stock.doctype.pick_list.pick_list.create_delivery(...,
target="Sales Invoice")`. A checklist workflow, the Page itself, any new
Custom Field, any new hook, and a cancellation endpoint are all still
explicitly out of scope -- see the Commit 21.3 brief.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, nowdate

from erpnext.stock.doctype.pick_list.pick_list import create_delivery, get_actual_qty

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


@frappe.whitelist()
def generate_invoice(pick_list_name):
	"""Generate and submit a native Sales Invoice from an already-submitted,
	still-invoiceable Pick List -- via ERPNext's own audited mapper,
	`erpnext.stock.doctype.pick_list.pick_list.create_delivery(...,
	target="Sales Invoice")` (the exact function api.ventas/Commit 21.1's
	own functional test already proved works correctly under a real,
	restricted Facturación session, with zero `ignore_permissions`). This
	function does not reimplement any of that mapping -- it only validates
	before calling it and submits after.

	Quantity: never computed here. The mapper itself (`map_pl_locations()`)
	sets `child_item.qty = picked_qty - delivered_qty` per line and removes
	any line whose result is <= 0 -- this is exactly why calling this
	function twice on the same Pick List is safe without any custom
	idempotency field: the first call's own `delivered_qty` update (native,
	via Sales Invoice's own `on_submit` -> `update_prevdoc_status()`) is
	what the mapper reads on the second call. A Fully Delivered Pick List
	is rejected outright (nothing left); a Partly Delivered one produces an
	invoice for only the real remainder.

	Price: never touched. `rate` on every mapped line comes straight from
	the real Sales Order Item row (`field_map: {"rate": "rate"}` inside
	`create_delivery_from_so()`) -- this function reads it back afterwards
	only to report it, never to set or recompute it, and never queries Item
	Price.

	Write: `create_delivery()` already calls `.save()` internally (its own
	`create_delivery_with_so()` -- confirmed during the Commit 21.1 audit,
	re-confirmed live by the `isinstance` check below) -- this function
	never calls `.insert()` a second time, only `.submit()`.

	Security: runs entirely under the caller's own real Facturación
	session -- no `ignore_permissions`, no `frappe.set_user`, no
	`frappe.get_all`, no manual `frappe.db.commit()` anywhere in this
	function. Atomicity beyond that is exactly what `create_delivery()` +
	`.submit()` already natively provide -- nothing here adds, skips, or
	reorders any database work relative to what a human clicking "Create
	Sales Invoice" on the Pick List would trigger.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", pick_list_name)
	pl.check_permission("read")

	if pl.docstatus != 1:
		frappe.throw(_("Este Pick List no está sometido; no puede facturarse todavía."))

	if pl.delivery_status == "Fully Delivered":
		frappe.throw(_("Este Pick List ya fue facturado por completo; no queda nada pendiente."))

	locations = pl.get("locations") or []
	if not any(flt(row.picked_qty) - flt(row.delivered_qty) > 0 for row in locations):
		frappe.throw(_("Este Pick List no tiene líneas pendientes de facturar."))

	row_sales_orders = [row.sales_order for row in locations]
	if not all(row_sales_orders):
		frappe.throw(
			_(
				"Este Pick List tiene líneas sin Orden de Venta asociada; "
				"Facturación todavía no soporta esa combinación."
			)
		)
	distinct_sales_orders = set(row_sales_orders)
	if len(distinct_sales_orders) > 1:
		frappe.throw(
			_(
				"Este Pick List está asociado a más de una Orden de Venta ({0}); "
				"Facturación todavía no soporta facturación multi-orden."
			).format(", ".join(sorted(distinct_sales_orders)))
		)
	sales_order = next(iter(distinct_sales_orders))

	# The audited mapper itself -- no target_doc supplied, so it always
	# builds and inserts a brand-new Sales Invoice.
	invoice = create_delivery(pl.name, target="Sales Invoice")

	# Defensive checks against divergence from the audited behaviour --
	# stop and surface a clear diagnostic rather than improvise around it.
	# create_delivery() returns a single Document only when it produced
	# exactly one target document (erpnext/stock/doctype/pick_list/
	# pick_list.py); for a single-Sales-Order Pick List (already validated
	# above) this is the only real ERPNext path -- any other outcome
	# (None, because 0 or >1 documents were produced; or a docstatus that
	# isn't still 0) means create_delivery()'s behaviour diverged from
	# what Commit 21.1 audited, not a normal business rejection.
	if not isinstance(invoice, Document) or invoice.doctype != "Sales Invoice":
		frappe.throw(
			_(
				"create_delivery() no devolvió una única Sales Invoice para este Pick List "
				"(posiblemente generó ninguna o más de una). Facturación solo admite una "
				"factura por Pick List -- deteniendo antes de improvisar."
			),
			title=_("Divergencia respecto al comportamiento auditado"),
		)
	if invoice.docstatus != 0:
		frappe.throw(
			_("La Sales Invoice generada no quedó en estado Borrador antes de someterla; deteniendo."),
			title=_("Divergencia respecto al comportamiento auditado"),
		)

	if not any(flt(item.qty) > 0 for item in invoice.items):
		frappe.throw(_("La factura generada no tiene ninguna línea con cantidad pendiente de facturar."))

	if not invoice.update_stock:
		# Per the brief: never set this manually -- create_delivery() (via
		# create_delivery_with_so(), Commit 21.1's own audit) always sets
		# it for target="Sales Invoice"; if it's missing, that is itself a
		# divergence worth stopping for, not a gap to silently patch.
		frappe.throw(
			_(
				"La factura generada no quedó con update_stock=1 -- esto contradice el "
				"comportamiento auditado de create_delivery(). Deteniendo antes de improvisar."
			),
			title=_("Divergencia respecto al comportamiento auditado"),
		)

	invoice.submit()

	return {
		"sales_invoice": invoice.name,
		"pick_list": pl.name,
		"sales_order": sales_order,
		"commercial_name": root_commercial_name(sales_order),
		"status": invoice.status,
		"item_count": len(invoice.items),
		"total_qty": flt(invoice.total_qty),
		"grand_total": flt(invoice.grand_total),
	}
