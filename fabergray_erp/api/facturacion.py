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

Commit 23.0 -- "Facturación operativa sin Sales Invoice". `generate_invoice()`
above is now LEGACY: this commit's own audit found no other Page/API calls
it (grep across fabergray_erp/ confirmed it), and its own dedicated test
suite (test_facturacion_generate_invoice.py) still exercises it directly,
so it is kept, unmodified, exactly per the approved brief ("no borrarlo
arbitrariamente") -- the new Page (facturacion.js) never calls it.

The new flow (get_invoicing_summary()/get_invoicing_queue()/
mark_as_invoiced() below) is a purely OPERATIONAL checkbox, deliberately
decoupled from ERPNext's real billing engine: no Sales Invoice, no GL
Entry, no Payment Entry, no receivable account, ever. State is persisted
on Pick List itself (three new Custom Fields: fg_invoicing_status/
fg_invoiced_on/fg_invoiced_by, allow_on_submit=1 -- see mark_as_invoiced()'s
own docstring for why that native flag, not a workaround, is what makes
`.save()` on an already-submitted Pick List legal), never on Sales Order.

Why Pick List, not Sales Order (the brief's own preferred default,
overridden here with the reason it asked for): this module's entire
existing architecture -- the queue (get_pending_pick_lists()), the detail
view (get_pick_list_for_facturacion()), generate_invoice() itself -- is
already keyed by Pick List, never Sales Order, precisely because one Sales
Order can have MORE THAN ONE Pick List (partial/backorder deliveries,
already handled by the existing `delivery_status`-based queue above).
Storing the operational "Facturado" flag on Sales Order would mark an
entire order done the moment ANY one of its Pick Lists was invoiced,
silently hiding that a second, still-pending Pick List for the same order
exists -- exactly the granularity bug this architecture already had to
solve once for native delivery tracking, and must not reintroduce for its
own operational state.

Facturación's Custom DocPerm on Pick List gained `write=1` AND `submit=1`
this commit (was read-only) -- the only permission change here, and
explicitly NOT an accounting one: this role still has zero Account
permission and its existing Sales Invoice grant (from Commit 21.1/21.3,
kept for generate_invoice()) is never exercised by the new flow at all --
the whole point of this redesign is that Facturación no longer needs it
for this operation. `submit=1` is required even though mark_as_invoiced()
never calls `.submit()`: Frappe's own Document.check_docstatus_transition()
treats ANY save on an already-submitted document (Submit(1) -> Submit(1),
i.e. exactly what updating an allow_on_submit field is) as the
`update_after_submit` action, which unconditionally calls
`check_permission("submit")` -- confirmed empirically, not assumed, when
the first version of this commit (write=1 only) raised a real
PermissionError from `pl.save()` in this exact scenario.

Correction to Commit 23.0, same commit number: the first version of
mark_as_invoiced() let a single click flip a Pick List straight to
Facturado with no per-item review, which turned out to remove real
functionality the previous (Sales-Invoice-backed) flow had -- Commit
21.5's own "VERIFICADO" checklist, gating GENERAR FACTURA one line at a
time. That checklist was audited first and found to be frontend-only
(`this.verified_rows = new Set()`, reset on every page load, explicitly
never persisted, per that commit's own docstring) -- so there was no
existing server-side mechanism to reuse, and this correction adds one
from scratch: three more Custom Fields, this time on "Pick List Item"
(fg_invoicing_checked/fg_invoicing_checked_on/fg_invoicing_checked_by,
allow_on_submit=1 -- required on the CHILD doctype's own field
definition, confirmed empirically via
Document._validate_update_after_submit(), which reads
`self.meta.get_field(key).allow_on_submit` where `self` is each child row,
not the parent). set_invoicing_item_checked() is the one write; it never
touches qty/picked_qty/delivered_qty, and is rejected once
fg_invoicing_status is already Facturado (read-only from then on).
mark_as_invoiced() itself now additionally requires every row checked
(ChecklistIncompleteError otherwise) -- server-side, not merely a
disabled button in the JS. Pick List Item rows are never grouped/collapsed by item_code here:
`item_code` carries no `unique` constraint on that child doctype
(confirmed via its own meta, not assumed) -- current live data happens to
have no duplicates, but nothing prevents two rows for the same item_code
at different warehouses/batches/sales_order lines, so collapsing by
item_code would risk silently losing which physical row was actually
reviewed. get_invoicing_detail() below returns one entry per child row,
verbatim, never grouped.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, nowdate

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


# ---------------------------------------------------------------------------
# Commit 23.0 -- "Facturación operativa sin Sales Invoice". Everything below
# is new; everything above this line is untouched legacy (see this module's
# own top docstring for the full audit). No function below ever creates a
# Sales Invoice, a GL Entry or a Payment Entry, ever calls generate_invoice()/
# create_delivery()/erpnext.accounts.doctype.sales_invoice.*, and never
# touches Sales Order.per_billed/billing_status or any other native
# accounting field -- see test_facturacion_invoicing_status.py's own AST
# guardrail for the executable version of this claim.
# ---------------------------------------------------------------------------

FG_INVOICING_PENDIENTE = "Pendiente"
FG_INVOICING_FACTURADO = "Facturado"


class PickListNotReadyForInvoicingError(frappe.ValidationError):
	pass


class AlreadyInvoicedError(frappe.ValidationError):
	pass


class ChecklistIncompleteError(frappe.ValidationError):
	pass


class ChecklistReadOnlyError(frappe.ValidationError):
	pass


def _checklist_counts(pl):
	"""(total_items, checked_items, progress_percent) for one already-loaded
	Pick List doc -- reads `pl.locations` in memory, never a fresh query, so
	callers that already have the doc (mark_as_invoiced(),
	get_invoicing_detail(), set_invoicing_item_checked()) get a result
	consistent with whatever they just changed, before any reload."""
	rows = pl.get("locations") or []
	total_items = len(rows)
	checked_items = sum(1 for r in rows if cint(r.fg_invoicing_checked))
	progress_percent = round((checked_items / total_items) * 100, 2) if total_items else 0.0
	return total_items, checked_items, progress_percent


def _pick_lists_matching_sales_order(txt):
	"""Pick List names whose own Pick List Item rows reference a Sales
	Order matching `txt` -- same reasoning/pattern as
	api.jefe_bodega._pick_lists_matching_sales_order() (Commit 22.9), but
	frappe.get_list(parent_doctype="Pick List") here rather than
	jefe_bodega's frappe.get_all: this module's own guardrail (see
	test_regression.py's test_facturacion_api_never_calls_get_all_*)
	forbids frappe.get_all anywhere in api/facturacion.py. "Pick List Item"
	has no Role Permission of its own (confirmed empirically -- zero
	DocPerm/Custom DocPerm rows for it, for any role), so a bare
	frappe.get_list("Pick List Item", ...) would raise PermissionError even
	for Facturación; passing parent_doctype="Pick List" makes
	check_select_permission() check the PARENT doctype's own (already
	granted) read permission instead, exactly the mechanism
	frappe.get_all's internal ignore_permissions=True was standing in for."""
	if not txt:
		return []
	return frappe.get_list(
		"Pick List Item",
		filters={"sales_order": ["like", f"%{txt}%"]},
		pluck="parent",
		distinct=True,
		parent_doctype="Pick List",
	)


@frappe.whitelist()
def get_invoicing_summary():
	"""KPI row for the new Facturación dashboard: Pendientes / Facturados
	hoy / Facturados -- exclusively derived from Pick List's own
	fg_invoicing_status/fg_invoiced_on (Commit 23.0's own operational
	state), never from Sales Invoice (get_facturacion_summary() above,
	unchanged, still does that for the legacy flow -- this is a
	deliberately separate, parallel KPI set, not a replacement)."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	pendientes = frappe.get_list(
		"Pick List",
		filters=[["docstatus", "=", 1], ["fg_invoicing_status", "!=", FG_INVOICING_FACTURADO]],
		pluck="name",
	)
	facturados = frappe.get_list(
		"Pick List",
		filters={"docstatus": 1, "fg_invoicing_status": FG_INVOICING_FACTURADO},
		pluck="name",
	)

	today = nowdate()
	facturados_hoy = frappe.get_list(
		"Pick List",
		filters=[
			["docstatus", "=", 1],
			["fg_invoicing_status", "=", FG_INVOICING_FACTURADO],
			["fg_invoiced_on", ">=", f"{today} 00:00:00"],
			["fg_invoiced_on", "<=", f"{today} 23:59:59"],
		],
		pluck="name",
	)

	return {
		"pendientes": len(pendientes),
		"facturados_hoy": len(facturados_hoy),
		"facturados": len(facturados),
	}


@frappe.whitelist()
def get_invoicing_queue(status=None, txt=None, start=0, page_length=20):
	"""Paginated Pick List list for the new Facturación Page: "Todos" /
	"Pendientes" / "Facturados", driven exclusively by fg_invoicing_status
	-- delivery_status/Sales Invoice are never consulted here.

	status: FG_INVOICING_PENDIENTE | FG_INVOICING_FACTURADO, or
	falsy/unrecognized for "todos" (same convention as
	api.clientes.search_customers()). Both are plain, indexed-column
	filters -- real DB-level pagination, no bounded-fetch-then-filter
	needed (unlike api.jefe_bodega.get_pick_list_history()'s own computed
	state).

	Per-row item_count/total_qty/sales_order are resolved with ONE
	batched Pick List Item query, scoped to only the page being returned
	-- never one query/get_doc per Pick List (same "bulk read, never
	N+1" rule api.jefe_bodega.get_pick_list_history() already
	established for the equivalent problem)."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	start = max(cint(start), 0)
	page_length = min(max(cint(page_length) or 20, 1), 100)
	txt = (txt or "").strip()
	status = status if status in (FG_INVOICING_PENDIENTE, FG_INVOICING_FACTURADO) else None

	filters = [["docstatus", "=", 1]]
	if status == FG_INVOICING_FACTURADO:
		filters.append(["fg_invoicing_status", "=", FG_INVOICING_FACTURADO])
	elif status == FG_INVOICING_PENDIENTE:
		filters.append(["fg_invoicing_status", "!=", FG_INVOICING_FACTURADO])

	or_filters = None
	if txt:
		or_filters = [
			["name", "like", f"%{txt}%"],
			["customer_name", "like", f"%{txt}%"],
			["customer", "like", f"%{txt}%"],
		]
		matching_by_so = _pick_lists_matching_sales_order(txt)
		if matching_by_so:
			or_filters.append(["name", "in", matching_by_so])

	page_rows = frappe.get_list(
		"Pick List",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"fg_invoicing_status",
			"fg_invoiced_on",
			"fg_invoiced_by",
			"modified",
		],
		order_by="modified desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	total = len(frappe.get_list("Pick List", filters=filters, or_filters=or_filters, pluck="name"))

	names = [r.name for r in page_rows]
	item_counts, checked_counts, total_qtys, sales_order_by_pl = {}, {}, {}, {}
	if names:
		for row in frappe.get_list(
			"Pick List Item",
			filters={"parent": ["in", names]},
			fields=["parent", "sales_order", "picked_qty", "fg_invoicing_checked"],
			parent_doctype="Pick List",
		):
			item_counts[row.parent] = item_counts.get(row.parent, 0) + 1
			if cint(row.fg_invoicing_checked):
				checked_counts[row.parent] = checked_counts.get(row.parent, 0) + 1
			total_qtys[row.parent] = total_qtys.get(row.parent, 0.0) + flt(row.picked_qty)
			if row.sales_order and row.parent not in sales_order_by_pl:
				sales_order_by_pl[row.parent] = row.sales_order

	commercial_name_cache = {}

	def _commercial_name(sales_order):
		if not sales_order:
			return None
		if sales_order not in commercial_name_cache:
			commercial_name_cache[sales_order] = root_commercial_name(sales_order)
		return commercial_name_cache[sales_order]

	results = []
	for pl in page_rows:
		sales_order = sales_order_by_pl.get(pl.name)
		total_items = item_counts.get(pl.name, 0)
		checked_items = checked_counts.get(pl.name, 0)
		progress_percent = round((checked_items / total_items) * 100, 2) if total_items else 0.0
		results.append(
			{
				"name": pl.name,
				"sales_order": sales_order,
				"commercial_name": _commercial_name(sales_order),
				"customer": pl.customer,
				"customer_name": pl.customer_name,
				"item_count": total_items,
				"total_qty": total_qtys.get(pl.name, 0.0),
				"checked_items": checked_items,
				"total_items": total_items,
				"progress_percent": progress_percent,
				"fg_invoicing_status": pl.fg_invoicing_status or FG_INVOICING_PENDIENTE,
				"fg_invoiced_on": pl.fg_invoiced_on,
				"fg_invoiced_by": pl.fg_invoiced_by,
				"fg_invoiced_by_fullname": (
					frappe.utils.get_fullname(pl.fg_invoiced_by) if pl.fg_invoiced_by else None
				),
			}
		)

	return {"pick_lists": results, "total": total}


@frappe.whitelist()
def get_invoicing_detail(pick_list):
	"""Full item checklist for the "REVISAR PEDIDO" modal: one entry per
	Pick List Item row, verbatim -- never grouped/collapsed by item_code
	(see this module's own top docstring for why). No rate/amount/
	grand_total/account anywhere in this response, unlike the legacy
	get_pick_list_for_facturacion() above -- this is operational
	facturación, not accounting, end to end."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	pl = frappe.get_doc("Pick List", pick_list)
	pl.check_permission("read")

	sales_order = _sales_order_of(pl)
	total_items, checked_items, progress_percent = _checklist_counts(pl)

	items = [
		{
			"row_name": r.name,
			"item_code": r.item_code,
			"item_name": r.item_name,
			"qty": flt(r.picked_qty),
			"uom": r.uom,
			"checked": cint(r.fg_invoicing_checked),
			"checked_on": r.fg_invoicing_checked_on,
			"checked_by": r.fg_invoicing_checked_by,
		}
		for r in (pl.get("locations") or [])
	]

	return {
		"pick_list": pl.name,
		"sales_order": sales_order,
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
		"customer": pl.customer,
		"customer_name": pl.customer_name,
		"fg_invoicing_status": pl.fg_invoicing_status or FG_INVOICING_PENDIENTE,
		"total_items": total_items,
		"total_qty": sum(flt(i["qty"]) for i in items),
		"checked_items": checked_items,
		"progress_percent": progress_percent,
		"items": items,
	}


@frappe.whitelist()
def set_invoicing_item_checked(pick_list, pick_list_item, checked):
	"""The one write behind the checklist: marks (or unmarks) a single Pick
	List Item row as reviewed for Facturación. Never touches qty/
	picked_qty/delivered_qty -- only fg_invoicing_checked/
	fg_invoicing_checked_on/fg_invoicing_checked_by, the three Custom
	Fields on "Pick List Item" this correction adds (allow_on_submit=1, see
	this module's own top docstring for why that flag lives on the CHILD
	doctype's own field definition, not the parent's).

	Validation, in order: real Pick List -- real write permission (same
	Pick List grant mark_as_invoiced() already uses, nothing new) --
	pick_list_item genuinely belongs to THIS Pick List's own `locations`
	(never trusts the child row name alone; a row name from a different
	Pick List raises DoesNotExistError here, never silently no-ops) --
	not already Facturado (once Facturado the checklist is read-only,
	matching the brief exactly -- ChecklistReadOnlyError below).

	checked=0 clears checked_on/checked_by along with the flag itself, so
	an unchecked row never carries a stale "reviewed by/on" from a previous
	check -- a clean toggle, not merely flipping one field.

	Atomicity: the only write is this one `.save()` (the whole parent Pick
	List, exactly like mark_as_invoiced()'s own pattern) -- no
	frappe.db.commit() anywhere in this function or reachable from it."""
	_require_login()
	frappe.has_permission("Pick List", "write", throw=True)

	pl = frappe.get_doc("Pick List", pick_list)
	pl.check_permission("write")

	row = next((r for r in (pl.get("locations") or []) if r.name == pick_list_item), None)
	if not row:
		frappe.throw(
			_("La línea {0} no pertenece al Pick List {1}.").format(pick_list_item, pick_list),
			frappe.DoesNotExistError,
		)

	if pl.fg_invoicing_status == FG_INVOICING_FACTURADO:
		frappe.throw(
			_("Este pedido ya fue facturado; el checklist es de solo lectura."),
			ChecklistReadOnlyError,
		)

	checked = cint(checked)
	row.fg_invoicing_checked = checked
	if checked:
		row.fg_invoicing_checked_on = frappe.utils.now_datetime()
		row.fg_invoicing_checked_by = frappe.session.user
	else:
		row.fg_invoicing_checked_on = None
		row.fg_invoicing_checked_by = None

	pl.save()  # real permission, no ignore_permissions

	total_items, checked_items, progress_percent = _checklist_counts(pl)

	return {
		"pick_list": pl.name,
		"row_name": row.name,
		"checked": row.fg_invoicing_checked,
		"checked_on": row.fg_invoicing_checked_on,
		"checked_by": row.fg_invoicing_checked_by,
		"total_items": total_items,
		"checked_items": checked_items,
		"progress_percent": progress_percent,
	}


@frappe.whitelist()
def mark_as_invoiced(pick_list_name):
	"""The one write in the new flow: marks a Pick List as operationally
	"Facturado" -- fg_invoicing_status/fg_invoiced_on/fg_invoiced_by only,
	via a plain `.save()` (real permission, no ignore_permissions). Never
	creates a Sales Invoice, a GL Entry or a Payment Entry; never touches
	Sales Order.per_billed/billing_status, Pick List.delivery_status/
	per_delivered, or any other native accounting/delivery field.

	`.save()` on an already-submitted Pick List is legal here specifically
	because the three Custom Fields were created with `allow_on_submit=1`
	(Frappe's own native mechanism for "this field may still change after
	submit", not a workaround) -- without it, Document.
	validate_update_after_submit() would reject changing any field on a
	docstatus=1 document. No other field is touched, so no other change
	could slip through that same door.

	Validation, in order: real Pick List (frappe.get_doc() raises
	DoesNotExistError otherwise) -- real write permission
	(check_permission("write"), the only accounting-adjacent-sounding
	requirement this endpoint has, and it is a plain Pick List grant, not
	an Accounts one) -- submitted (docstatus==1: "en la etapa correcta"
	for this operational flow, matching the rest of this module's own
	convention) -- not already Facturado (AlreadyInvoicedError, the exact
	"ya fue marcado como facturado" message the brief asks for) -- this
	last check is also the whole idempotency story: a second click always
	lands on an already-Facturado Pick List and is rejected before
	touching anything, never creating or duplicating any record, because
	there is no accounting movement here to duplicate in the first place --
	then every Pick List Item row checked (ChecklistIncompleteError
	otherwise, the exact "Debes revisar todos los productos..." message).
	Server-side, not merely a disabled CONFIRMAR FACTURACIÓN button in the
	JS: total_items must be > 0 (an empty Pick List can never be marked
	Facturado) and checked_items must equal total_items exactly.

	Atomicity: the only write is this one `.save()`; no
	frappe.db.commit() anywhere in this function or reachable from it --
	an exception at any point above rolls back the whole request exactly
	like every other write endpoint in this app."""
	_require_login()
	frappe.has_permission("Pick List", "write", throw=True)

	pl = frappe.get_doc("Pick List", pick_list_name)
	pl.check_permission("write")

	if pl.docstatus != 1:
		frappe.throw(
			_("Este Pick List no está sometido; no puede marcarse como facturado todavía."),
			PickListNotReadyForInvoicingError,
		)

	if pl.fg_invoicing_status == FG_INVOICING_FACTURADO:
		frappe.throw(_("Este pedido ya fue marcado como facturado."), AlreadyInvoicedError)

	total_items, checked_items, _progress = _checklist_counts(pl)
	if total_items == 0 or checked_items != total_items:
		frappe.throw(
			_("Debes revisar todos los productos antes de marcar el pedido como facturado."),
			ChecklistIncompleteError,
		)

	sales_order = _sales_order_of(pl)

	pl.fg_invoicing_status = FG_INVOICING_FACTURADO
	pl.fg_invoiced_on = frappe.utils.now_datetime()
	pl.fg_invoiced_by = frappe.session.user
	pl.save()  # real permission, no ignore_permissions

	return {
		"pick_list": pl.name,
		"sales_order": sales_order,
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
		"customer": pl.customer,
		"customer_name": pl.customer_name,
		"fg_invoicing_status": pl.fg_invoicing_status,
		"fg_invoiced_on": pl.fg_invoiced_on,
		"fg_invoiced_by": pl.fg_invoiced_by,
		"fg_invoiced_by_fullname": frappe.utils.get_fullname(pl.fg_invoiced_by),
	}
