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

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, nowdate

from erpnext.stock.doctype.pick_list.pick_list import create_delivery, get_actual_qty

from fabergray_erp.api.bodega import OPEN_SHORTAGE_STATUSES, _require_login
from fabergray_erp.api.cotizaciones import (
	_require_facturacion_role,
	_resolve_pdf_advisor_name,
	_resolve_pdf_contact,
)
from fabergray_erp.invoice_issuers import (
	INVOICE_ISSUERS,
	get_issuer_config,
	get_print_provider,
	missing_issuer_fields,
)
from fabergray_erp.permission_conditions import assert_same_company
from fabergray_erp.pricing import (
	PRICE_MODE_DISCOUNTS,
	PRICE_MODE_LABELS,
	discounted_rate,
	reference_selling_rates,
)
from fabergray_erp.sales_order_naming import root_commercial_name
from fabergray_erp.search_utils import normalize_search_date

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


def _clean_order_observations(value):
	"""Hotfix 25.26.1 -- `Sales Order.fg_observations` (the observación the
	Vendedora typed in Ventas) normalized for display: surrounding
	whitespace trimmed, internal line breaks untouched, "" when there is no
	real content. The ONLY source of this text is the Sales Order itself --
	never copied/snapshotted onto the Pick List: `fg_observations` has no
	allow_on_submit, and a Sales Order with a submitted Pick List can be
	neither modified (modification_blockers_for) nor cancelled, so the text
	an invoiced Pick List reads here cannot change after the fact
	(test_facturacion_order_observations pins that guarantee)."""
	return (value or "").strip()


def _order_observations(sales_order):
	"""Read-only lookup of one Sales Order's observación, for callers that
	only hold the name (get_invoicing_detail()). The caller has already
	authorised the Pick List that references this Sales Order -- same
	single-value read pattern get_pending_pick_lists() already uses for
	that Sales Order's transaction_date."""
	if not sales_order:
		return ""
	return _clean_order_observations(frappe.db.get_value("Sales Order", sales_order, "fg_observations"))


def _customer_company_types(customer_names):
	"""Commit 25.22 -- bulk lookup of Customer.fg_customer_company_type
	for exactly these customer names, one query (never one get_doc() per
	Pick List/row) -- same "bulk read, never N+1" convention
	get_invoicing_queue()'s own sales_order/item_count batching below
	already established. Guarded by frappe.has_permission("Customer",
	"read") (never assumed): Facturación's own Custom DocPerm already
	grants it today, but this module never reads a doctype's fields
	without checking, even via a raw frappe.get_list() call that itself
	does not enforce row-level permission the way check_permission()
	does -- a caller without it simply sees None everywhere, never a
	PermissionError from an incidental enrichment lookup, same reasoning
	api.clientes.get_customer_detail() already documents for its own
	Contact/Address resolution.

	Source of truth is always this live read against Customer -- never a
	value copied onto Pick List/Sales Order/Quotation (section 12 of the
	Commit 25.22 brief's own explicit "preferir fuente dinámica")."""
	customer_names = [c for c in dict.fromkeys(customer_names) if c]
	if not customer_names or not frappe.has_permission("Customer", "read"):
		return {}
	rows = frappe.get_list(
		"Customer",
		filters={"name": ["in", customer_names]},
		fields=["name", "fg_customer_company_type"],
	)
	return {r.name: (r.fg_customer_company_type or None) for r in rows}


def _customer_company_type(customer_name):
	"""Single-Customer counterpart of _customer_company_types() above, for
	the detail endpoints below that only ever resolve one Pick List/
	Quotation (and therefore one Customer) at a time."""
	if not customer_name or not frappe.has_permission("Customer", "read"):
		return None
	return frappe.db.get_value("Customer", customer_name, "fg_customer_company_type") or None


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
	established for the equivalent problem).

	Commit 25.22 -- `fg_customer_company_type` per row, one batched
	Customer lookup for the whole page (_customer_company_types()) --
	`None` for a historical Customer never classified, the tray renders
	that as "Sin clasificar"."""
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
		# Commit 25.20 -- section 4's own "comparar contra la fecha real del
		# documento, no solo contra texto visual": Pick List has no
		# `transaction_date` of its own, so `creation` (always present) and
		# `fg_invoiced_on` (set once actually invoiced) are its own two real
		# Date/Datetime fields -- both compared against the FULL day the
		# query parses to, never a plain text `like` on a formatted string.
		# `normalize_search_date()` returns `None` for an ordinary customer-
		# name query, so this branch is simply skipped then -- never a false
		# match.
		date_query = normalize_search_date(txt)
		if date_query:
			or_filters.append(["creation", "between", [f"{date_query} 00:00:00", f"{date_query} 23:59:59"]])
			or_filters.append(
				["fg_invoiced_on", "between", [f"{date_query} 00:00:00", f"{date_query} 23:59:59"]]
			)

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
			"fg_invoice_issuer",
			"modified",
			"creation",
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

	# Commit 25.22 -- one batched Customer lookup for the whole page, never
	# one per Pick List. See _customer_company_types()'s own docstring.
	company_types = _customer_company_types([r.customer for r in page_rows])

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
				"fg_customer_company_type": company_types.get(pl.customer),
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
				"fg_invoice_issuer": pl.fg_invoice_issuer or None,
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
	facturación, not accounting, end to end.

	Commit 25.22 -- `fg_customer_company_type` resolved fresh from the
	real Customer (`_customer_company_type()`), `None` for a historical
	Customer never classified -- the modal renders that as "Sin
	clasificar" with a visible warning, never a blocked/hidden view
	(section 11 of the brief)."""
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
		"fg_customer_company_type": _customer_company_type(pl.customer),
		"fg_invoicing_status": pl.fg_invoicing_status or FG_INVOICING_PENDIENTE,
		# Hotfix 25.26.1 -- read-only; "" when the order has none.
		"order_observations": _order_observations(sales_order),
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

	# Commit 25.25 -- congela el precio final de CADA línea (el precio de
	# Facturación si existe; si no, el rate actual del Sales Order) antes de
	# pasar a Facturado. Valida todas las líneas primero y lanza sin tocar
	# nada si alguna no tiene precio válido; luego estado + precios viajan
	# en este MISMO y único .save() -- nunca un estado parcial.
	_freeze_invoice_prices(pl, _invoice_pricing_sales_orders(pl))

	pl.fg_invoicing_status = FG_INVOICING_FACTURADO
	pl.fg_invoiced_on = frappe.utils.now_datetime()
	pl.fg_invoiced_by = frappe.session.user
	with _invoice_pricing_write():
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


# =============================================================================
# Commit 25.23 -- "Fabrigray Factura Comercial": PDF comercial de factura para
# un Pick List ya marcado Facturado, con empresa emisora elegida y persistida
# por Facturación (Pick List.fg_invoice_issuer: integrandoMAS | ecoluminar).
#
# Mismo patrón que el PDF de Cotizaciones (api/cotizaciones.py, Commit 25.15):
#   - El Print Format (fixtures/print_format.json) es solo Jinja/HTML/CSS y
#     únicamente lee atributos `doc.fg_pdf_*` calculados aquí.
#   - Dos endpoints dedicados (VER/DESCARGAR) validan TODO antes de tocar el
#     pipeline de impresión de Frappe, y el nombre del formato está fijo en
#     el servidor -- el cliente nunca lo envía.
#   - `prepare_and_guard_invoice_pdf()`, un `before_print` de Pick List
#     acotado SOLO a este formato vía `frappe.form_dict.format`, repite la
#     misma validación como defensa en profundidad (p. ej. Desk printview:
#     validate_print_permission() acepta `read` a secas, así que Bodega/
#     Jefe de Bodega/Recorrido -- que leen Pick List -- llegarían al
#     formato sin este guard). Cualquier otro formato de Pick List no se toca.
#
# Documento fuente: el flujo activo de Facturación (Commit 23.0) no crea
# Sales Invoice -- la "factura" operativa es el Pick List con
# fg_invoicing_status == "Facturado". Las líneas son sus filas con
# picked_qty > 0, al precio real de la fila de Sales Order Item que cada una
# referencia (misma regla que get_pick_list_for_facturacion()). Los totales:
#   - si este Pick List cubre el pedido completo, se usan tal cual
#     rounded_total/grand_total (y net_total/impuestos/descuento) del Sales
#     Order -- el documento es la fuente de verdad;
#   - si es parcial y el pedido no tiene impuestos ni descuento global, el
#     total es la suma de las líneas (qty x rate real), calculada aquí;
#   - si es parcial y el pedido SÍ tiene impuestos/descuento global, se
#     rechaza: prorratearlos sería inventar un cálculo contable.
#
# Numeración: no existe un número de factura real en este flujo (no hay Sales
# Invoice y nunca se diseñó una serie). `_resolve_invoice_number()` devuelve
# None a propósito -- ni el PEDIDO ni el Pick List se usan como número. El PDF
# muestra "SIN NUMERAR" + la referencia interna del Pick List, y se marca
# BORRADOR mientras falte numeración o datos del emisor (invoice_issuers.py).
# =============================================================================

INVOICE_PDF_PRINT_FORMAT_NAME = "Fabrigray Factura Comercial"

MSG_SELECT_INVOICE_ISSUER = "Selecciona la empresa emisora de la factura."


class InvoiceIssuerRequiredError(frappe.ValidationError):
	pass


class InvalidInvoiceIssuerError(frappe.ValidationError):
	pass


class InvoicePdfNotEligibleError(frappe.ValidationError):
	pass


def _validate_invoice_issuer(issuer):
	"""Whitelist estricta -- nunca texto libre del navegador."""
	if not issuer:
		frappe.throw(_(MSG_SELECT_INVOICE_ISSUER), InvoiceIssuerRequiredError)
	if issuer not in INVOICE_ISSUERS:
		frappe.throw(
			_("Empresa emisora no permitida: {0}.").format(frappe.bold(issuer)),
			InvalidInvoiceIssuerError,
		)
	return issuer


def _assert_invoiced_pick_list(pl, ptype="read"):
	"""Rol + permiso + Company + estado Facturado -- lo común a guardar el
	emisor y a generar el PDF. _require_facturacion_role() es el mismo
	chequeo explícito de rol del flujo de Cotizaciones (Facturación, System
	Manager o Administrator); los demás roles que leen Pick List quedan
	fuera aunque su DocPerm les dé `read`/`print`."""
	_require_facturacion_role()
	pl.check_permission(ptype)
	assert_same_company(pl)

	if pl.docstatus != 1:
		frappe.throw(
			_("Este Pick List no está sometido; no puede generarse su factura."),
			InvoicePdfNotEligibleError,
		)
	if pl.fg_invoicing_status != FG_INVOICING_FACTURADO:
		frappe.throw(
			_("El pedido debe estar marcado como facturado antes de generar la factura."),
			InvoicePdfNotEligibleError,
		)


def _invoice_sales_order(pl):
	"""El único Sales Order del Pick List, ya validado (permiso, Company,
	sometido). Multi-orden o líneas sin orden se rechazan, igual que
	generate_invoice()/get_pick_list_for_facturacion()."""
	locations = pl.get("locations") or []
	if not locations or not all(row.sales_order for row in locations):
		frappe.throw(
			_("Este Pick List tiene líneas sin Orden de Venta asociada; no puede generarse su factura."),
			InvoicePdfNotEligibleError,
		)
	distinct_sales_orders = {row.sales_order for row in locations}
	if len(distinct_sales_orders) > 1:
		frappe.throw(
			_(
				"Este Pick List está asociado a más de una Orden de Venta ({0}); "
				"la factura comercial todavía no soporta facturación multi-orden."
			).format(", ".join(sorted(distinct_sales_orders))),
			InvoicePdfNotEligibleError,
		)

	so = frappe.get_doc("Sales Order", next(iter(distinct_sales_orders)))
	so.check_permission("read")
	assert_same_company(so)
	if so.docstatus != 1:
		frappe.throw(
			_("La Orden de Venta {0} no está vigente; no puede generarse la factura.").format(so.name),
			InvoicePdfNotEligibleError,
		)
	return so


def _assert_invoice_pdf_eligible(pl):
	"""La ÚNICA validación de "este Pick List puede producir la Factura
	Comercial", llamada desde los dos endpoints y desde el before_print --
	nunca tres variantes que puedan divergir. Devuelve el Sales Order."""
	_assert_invoiced_pick_list(pl)
	_validate_invoice_issuer(pl.fg_invoice_issuer)
	return _invoice_sales_order(pl)


def _resolve_invoice_number(pl):
	"""Número de factura a imprimir. None a propósito: el flujo actual no
	crea Sales Invoice ni existe una serie de facturación autorizada, y el
	PEDIDO/Pick List no deben usarse como número sin autorización. Este es
	el único punto a cambiar cuando se defina la numeración real."""
	return None


def _format_money_co(value):
	"""Formato monetario colombiano fijo: "$ 11.500" (punto de miles, coma
	decimal solo si hay centavos). Independiente del number_format del
	sitio, igual que _format_date_es_long() lo es del idioma del sitio."""
	value = flt(value, 2)
	sign = "-" if value < 0 else ""
	integer, cents = divmod(int(round(abs(value) * 100)), 100)
	text = f"{integer:,}".replace(",", ".")
	if cents:
		text += f",{cents:02d}"
	return f"{sign}$ {text}"


def _amount_in_words_es(value):
	"""Valor en letras para "Son:", convención colombiana: "ONCE MIL
	QUINIENTOS PESOS M/CTE". num2words(lang="es") no aplica apócope
	("veintiuno mil", "treinta y uno millones"), así que se corrige aquí:
	ante mil/millón/pesos va siempre "un"/"veintiún"."""
	import re

	from num2words import num2words

	value = flt(value, 2)
	integer, cents = divmod(int(round(abs(value) * 100)), 100)
	if integer == 1:
		words = "UN PESO"
	else:
		words = num2words(integer, lang="es")
		words = re.sub(r"\bveintiuno\b", "veintiún", words)
		words = re.sub(r"\buno\b", "un", words)
		words = words.upper()
		if integer and integer % 1_000_000 == 0:
			words += " DE"
		words += " PESOS"
	if cents:
		words += f" CON {cents:02d}/100"
	return f"{words} M/CTE"


def _format_date_co(value):
	return frappe.utils.getdate(value).strftime("%d/%m/%Y") if value else None


def _resolve_invoice_customer(so):
	"""NIT/dirección/ciudad/teléfono del cliente. Dirección: la Address real
	del pedido (so.customer_address) o, si no tiene, la principal actual del
	Customer; si solo existe el texto address_display, se usa ese. Nada se
	inventa -- cada campo es None si no existe. "Barrio" no existe en el
	modelo de Address de este sitio, así que no se imprime."""
	customer = frappe.db.get_value(
		"Customer", so.customer, ["tax_id", "customer_primary_address"], as_dict=True
	) or frappe._dict()

	address_line = city = address_phone = None
	address_name = so.customer_address or customer.customer_primary_address
	if address_name and frappe.db.exists("Address", address_name):
		address = frappe.get_doc("Address", address_name)
		address_line = ", ".join(p for p in (address.address_line1, address.address_line2) if p) or None
		city = address.city or None
		address_phone = address.phone or None
	elif so.address_display:
		address_line = frappe.utils.strip_html(so.address_display.replace("<br>", ", ")).strip(", ") or None

	contact_name, contact_phone, _contact_email = _resolve_pdf_contact(so)
	return {
		"name": so.customer_name or so.customer,
		"tax_id": customer.tax_id or None,
		"address": address_line,
		"city": city,
		"phone": contact_phone or address_phone,
		"contact_name": contact_name,
	}


def _build_invoice_lines_and_totals(pl, so):
	"""Líneas + totales desde los documentos reales (ver cabecera de esta
	sección). Lee las filas de una copia FRESCA del Pick List: el
	before_print nativo de ERPNext (PickList.group_similar_items(), si
	group_same_items está activo) muta `locations` en memoria antes de que
	corra este hook.

	Commit 25.25 -- precio unitario = fg_invoice_rate CONGELADO al marcar
	Facturado (el PDF solo existe para Pick Lists Facturados); nunca se
	vuelve a leer Sales Order.rate si hay precio congelado, así una factura
	histórica no cambia si luego cambian Item Price, Sales Order o Price
	List. Solo un Pick List facturado antes de esta mejora (sin precio
	congelado) cae al rate del Sales Order, como hasta ahora."""
	so_items = {row.name: row for row in so.items}
	precision = 2

	lines = []
	picked_by_so_item = {}
	prices_adjusted = False
	for row in frappe.get_doc("Pick List", pl.name).get("locations") or []:
		qty = flt(row.picked_qty)
		if qty <= 0:
			continue
		so_item = so_items.get(row.sales_order_item)
		if not so_item:
			frappe.throw(
				_("La línea {0} ({1}) no está vinculada a una línea de la Orden de Venta.").format(
					row.idx, row.item_code
				),
				InvoicePdfNotEligibleError,
			)
		picked_by_so_item[so_item.name] = picked_by_so_item.get(so_item.name, 0) + qty
		frozen_rate = flt(row.fg_invoice_rate, precision)
		rate = frozen_rate if frozen_rate > 0 else flt(so_item.rate)
		if flt(rate, precision) != flt(so_item.rate, precision):
			prices_adjusted = True
		amount = flt(qty * rate, precision)
		lines.append(
			{
				"qty": qty,
				"qty_display": f"{qty:g}",
				"description": so_item.item_name or row.item_name or row.item_code,
				"item_code": row.item_code,
				"rate": rate,
				"rate_display": _format_money_co(rate),
				"amount": amount,
				"amount_display": _format_money_co(amount),
			}
		)

	if not lines:
		frappe.throw(
			_("Este Pick List no tiene productos alistados para facturar."), InvoicePdfNotEligibleError
		)

	covers_whole_order = all(
		abs(picked_by_so_item.get(item.name, 0) - flt(item.qty)) < 1e-9 for item in so.items
	)
	has_order_adjustments = bool(flt(so.total_taxes_and_charges) or flt(so.discount_amount))

	breakdown = []
	if prices_adjusted and has_order_adjustments:
		# Commit 25.25 -- precios de Facturación distintos a los del pedido +
		# impuestos/descuento global del Sales Order: recalcularlos exigiría
		# un prorrateo fiscal que no está definido. Se bloquea, no se inventa.
		frappe.throw(
			_(
				"Los precios de esta factura difieren de los de la Orden de Venta {0}, que tiene impuestos "
				"o descuento global; la factura comercial no puede recalcularlos sin una regla fiscal definida."
			).format(so.name),
			InvoicePdfNotEligibleError,
		)
	if covers_whole_order and not prices_adjusted:
		total = flt(so.rounded_total) or flt(so.grand_total)
		if has_order_adjustments:
			breakdown.append({"label": "Subtotal", "amount_display": _format_money_co(so.total)})
			if flt(so.discount_amount):
				breakdown.append(
					{"label": "Descuento", "amount_display": _format_money_co(-flt(so.discount_amount))}
				)
			if flt(so.total_taxes_and_charges):
				breakdown.append(
					{"label": "Impuestos", "amount_display": _format_money_co(so.total_taxes_and_charges)}
				)
	elif has_order_adjustments:
		frappe.throw(
			_(
				"Esta factura es parcial y la Orden de Venta {0} tiene impuestos o descuento global; "
				"la factura comercial todavía no puede prorratearlos."
			).format(so.name),
			InvoicePdfNotEligibleError,
		)
	else:
		total = flt(sum(line["amount"] for line in lines), precision)

	return lines, {
		"total": total,
		"total_display": _format_money_co(total),
		"total_in_words": _amount_in_words_es(total),
		"breakdown": breakdown,
	}


def _build_invoice_pdf_context(pl, so):
	"""Todos los `doc.fg_pdf_*` que lee el template -- solo desde el
	before_print (atributos efímeros sobre el `doc` en memoria, nunca se
	guardan)."""
	issuer_key = pl.fg_invoice_issuer
	issuer = get_issuer_config(issuer_key)
	if issuer.get("logo") and issuer["logo"].startswith("/"):
		issuer["logo"] = frappe.utils.get_url(issuer["logo"])
	pl.fg_pdf_issuer = issuer
	# Imprenta: común a todos los emisores (invoice_issuers.INVOICE_PRINT_PROVIDER).
	pl.fg_pdf_print_provider = get_print_provider()

	number = _resolve_invoice_number(pl)
	pl.fg_pdf_number = number
	pl.fg_pdf_internal_reference = pl.name

	draft_reasons = []
	if not number:
		draft_reasons.append(_("sin numeración de factura autorizada"))
	missing = missing_issuer_fields(issuer_key)
	if missing:
		draft_reasons.append(_("faltan datos del emisor: {0}").format(", ".join(missing)))
	pl.fg_pdf_draft_reasons = draft_reasons

	pl.fg_pdf_invoice_date = _format_date_co(pl.fg_invoiced_on)
	pl.fg_pdf_payment_terms = None
	pl.fg_pdf_due_date = None
	if so.payment_terms_template:
		pl.fg_pdf_payment_terms = (
			frappe.db.get_value("Payment Terms Template", so.payment_terms_template, "template_name")
			or so.payment_terms_template
		)
		due_dates = [row.due_date for row in (so.payment_schedule or []) if row.due_date]
		pl.fg_pdf_due_date = _format_date_co(max(due_dates)) if due_dates else None

	pl.fg_pdf_customer = _resolve_invoice_customer(so)
	pl.fg_pdf_seller_name = _resolve_pdf_advisor_name(so)
	# Hotfix 25.26.1 -- print-only context (like every fg_pdf_*), never
	# saved; the template hides the whole section when this is "".
	pl.fg_pdf_order_observations = _clean_order_observations(so.fg_observations)

	lines, totals = _build_invoice_lines_and_totals(pl, so)
	pl.fg_pdf_lines = lines
	pl.fg_pdf_totals = totals


def prepare_and_guard_invoice_pdf(pl, method=None, print_settings=None):
	"""Pick List `before_print` (hooks.py). Acotado SOLO a "Fabrigray
	Factura Comercial" -- cualquier otro formato de Pick List (Standard, los
	de Bodega) retorna de inmediato sin calcular ni bloquear nada. Mismo
	razonamiento que cotizaciones.prepare_and_guard_quotation_pdf(): get_
	print() fija form_dict.format antes de renderizar en todos los caminos."""
	if frappe.form_dict.get("format") != INVOICE_PDF_PRINT_FORMAT_NAME:
		return

	so = _assert_invoice_pdf_eligible(pl)
	_build_invoice_pdf_context(pl, so)


@frappe.whitelist()
def set_invoice_issuer(pick_list_name, issuer):
	"""Guarda/cambia la empresa emisora de la factura de un Pick List
	Facturado. Persistida en el documento (no en el navegador) para que
	VER/DESCARGAR/regenerar produzcan siempre el mismo emisor. Un `.save()`
	real, sin ignore_permissions -- el campo es allow_on_submit=1, igual que
	fg_invoicing_status (ver mark_as_invoiced()). Mismo valor -> no escribe."""
	_require_login()
	_validate_invoice_issuer(issuer)

	pl = frappe.get_doc("Pick List", pick_list_name)
	_assert_invoiced_pick_list(pl, ptype="write")

	if pl.fg_invoice_issuer != issuer:
		pl.fg_invoice_issuer = issuer
		pl.save()  # real permission, no ignore_permissions

	return {"pick_list": pl.name, "fg_invoice_issuer": pl.fg_invoice_issuer}


@frappe.whitelist()
def get_invoice_pdf_view_url(pick_list_name):
	"""VER PDF: valida todo (_assert_invoice_pdf_eligible) y solo entonces
	devuelve la URL de printview, con el formato fijo en el servidor."""
	_require_login()
	pl = frappe.get_doc("Pick List", pick_list_name)
	_assert_invoice_pdf_eligible(pl)

	from urllib.parse import quote

	return frappe.utils.get_url(
		"/printview?doctype=Pick%20List&name="
		+ quote(pl.name)
		+ "&format="
		+ quote(INVOICE_PDF_PRINT_FORMAT_NAME)
		+ "&no_letterhead=1&trigger_print=0"
	)


@frappe.whitelist()
def download_invoice_pdf(pick_list_name):
	"""DESCARGAR PDF: misma validación primero, luego el pipeline nativo
	(frappe.utils.print_format.download_pdf) con el formato fijo. Solo se
	reescribe el nombre del archivo."""
	_require_login()
	pl = frappe.get_doc("Pick List", pick_list_name)
	_assert_invoice_pdf_eligible(pl)
	from frappe.utils.print_format import download_pdf

	download_pdf(doctype="Pick List", name=pl.name, format=INVOICE_PDF_PRINT_FORMAT_NAME, no_letterhead=1)

	safe_name = pl.name.replace(" ", "-").replace("/", "-")
	frappe.local.response.filename = f"Factura-{pl.fg_invoice_issuer}-{safe_name}.pdf"


# =============================================================================
# Commit 25.25 -- precios de factura por línea (opción B aprobada).
#
# El precio definitivo de Facturación vive en Pick List Item
# (fg_invoice_rate / fg_invoice_price_mode / fg_invoice_public_rate). El
# Sales Order NUNCA se modifica: su rate sigue siendo el precio original del
# pedido (auditoría: cambiarlo exigiría dar `write` sobre Sales Order a
# Facturación vía "Update Items", o cancel/amend con Pick List y reservas ya
# vinculados). Consecuencia aceptada: reportes nativos del Sales Order siguen
# mostrando el precio original.
#
# Una sola resolución por línea, _resolve_invoice_lines(), alimenta la
# pantalla (get_invoicing_pricing), el congelamiento (mark_as_invoiced) y el
# PDF usa el precio congelado -- UI, estado Facturado y PDF ven el mismo
# número. El navegador nunca calcula ni envía totales.
#
# Reglas de precio (fabergray_erp/pricing.py, compartido con Cotizaciones):
# los modos generales siempre calculan sobre el PRECIO PÚBLICO vigente
# (Item Price de la selling_price_list del pedido), nunca sobre un precio ya
# descontado. Sin fg_invoice_rate persistido, el precio efectivo es el rate
# del Sales Order (se respeta un precio negociado en Cotización). Al pasar a
# Facturado todo queda congelado: ni Item Price, ni Sales Order, ni Price
# List vuelven a cambiar una factura histórica.
#
# Escritura: solo estos endpoints + mark_as_invoiced(), dentro de
# _invoice_pricing_write(); guard_invoice_pricing_fields() (hooks.py) rechaza
# cualquier otra escritura de los tres campos. Auditoría: Pick List tiene
# track_changes=1 -- cada .save() deja un Version con valor anterior/nuevo,
# usuario y fecha; no hay un sistema de auditoría paralelo.
# =============================================================================

INVOICE_PRICE_MODE_ORDER = "Precio del pedido"
INVOICE_PRICE_MODE_SPECIAL = "Precio especial"
INVOICE_PRICING_FIELDS = ("fg_invoice_rate", "fg_invoice_price_mode", "fg_invoice_public_rate")
_INVOICE_PRICING_FLAG = "fg_invoice_pricing_write"


class InvoicePricingLockedError(frappe.ValidationError):
	pass


class InvalidInvoicePriceError(frappe.ValidationError):
	pass


class MissingPublicPriceError(frappe.ValidationError):
	pass


class InvoiceLinePriceMissingError(frappe.ValidationError):
	pass


class OrderAdjustmentsPricingError(frappe.ValidationError):
	pass


def _positive_rate(value, precision):
	"""EL contrato de precio en todo este módulo: > 0 (a la precisión del
	campo) es un precio válido; 0, negativo o vacío es "SIN PRECIO" (None).
	fg_invoice_public_rate/fg_invoice_rate son Currency NOT NULL default 0,
	así que 0 nunca puede leerse como un precio real de $0."""
	value = flt(value, precision)
	return value if value > 0 else None


@contextmanager
def _invoice_pricing_write():
	"""Bandera interna que autoriza, solo durante este bloque, escribir los
	tres campos de precio. Nunca viene del navegador (ningún endpoint la
	acepta como parámetro) y se restaura a su valor previo aunque el bloque
	lance -- mismo patrón que frappe.flags.fg_billing_price_mode_insert en
	api/cotizaciones.py."""
	previous = frappe.flags.get(_INVOICE_PRICING_FLAG)
	frappe.flags[_INVOICE_PRICING_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[_INVOICE_PRICING_FLAG] = previous


def _pricing_field_value(fieldname, value):
	# Currency: NOT NULL default 0 en BD, así que None y 0 son lo mismo.
	return (value or "") if fieldname == "fg_invoice_price_mode" else flt(value)


def guard_invoice_pricing_fields(doc, method=None):
	"""Pick List `validate` + `before_update_after_submit` (hooks.py):
	rechaza cualquier cambio a fg_invoice_rate/fg_invoice_price_mode/
	fg_invoice_public_rate que no venga de _invoice_pricing_write() --
	Bodega tiene write/submit sobre Pick List y, sin esto, podría
	reescribirlos por API (frappe.client.set_value) o crear un borrador ya
	"con precio". Compara contra la versión previa del documento; un
	documento nuevo debe traerlos vacíos."""
	if frappe.flags.get(_INVOICE_PRICING_FLAG):
		return
	before = doc.get_doc_before_save()
	old_rows = {row.name: row for row in (before.get("locations") or [])} if before else {}
	for row in doc.get("locations") or []:
		old = old_rows.get(row.name)
		for fieldname in INVOICE_PRICING_FIELDS:
			new_value = _pricing_field_value(fieldname, row.get(fieldname))
			old_value = _pricing_field_value(fieldname, old.get(fieldname) if old else None)
			if new_value != old_value:
				frappe.throw(
					_("Los precios de factura solo pueden modificarse desde Facturación."),
					frappe.PermissionError,
				)


def _invoice_pricing_sales_orders(pl):
	"""Sales Orders reales de las líneas del Pick List, cada uno con
	permiso de lectura, misma Company y sometido. Líneas sin Sales Order
	quedan sin precio del pedido (necesitan precio especial)."""
	sales_orders = {}
	for row in pl.get("locations") or []:
		if row.sales_order and row.sales_order not in sales_orders:
			so = frappe.get_doc("Sales Order", row.sales_order)
			so.check_permission("read")
			assert_same_company(so)
			if so.docstatus != 1:
				frappe.throw(
					_("La Orden de Venta {0} no está vigente; no se pueden resolver sus precios.").format(so.name),
					PickListNotReadyForInvoicingError,
				)
			sales_orders[so.name] = so
	return sales_orders


def _load_pricing_pick_list(pick_list_name, ptype):
	"""Validación común de los endpoints comerciales: rol Facturación
	(o System Manager/Administrator), permiso real, Company, sometido.
	Devuelve (pl, sales_orders)."""
	_require_login()
	_require_facturacion_role()
	pl = frappe.get_doc("Pick List", pick_list_name)
	pl.check_permission(ptype)
	assert_same_company(pl)
	if pl.docstatus != 1:
		frappe.throw(
			_("Este Pick List no está sometido; todavía no se pueden definir sus precios."),
			PickListNotReadyForInvoicingError,
		)
	return pl, _invoice_pricing_sales_orders(pl)


def _assert_pricing_editable(pl):
	if pl.fg_invoicing_status == FG_INVOICING_FACTURADO:
		frappe.throw(
			_("Este pedido ya fue facturado; sus precios están congelados."),
			InvoicePricingLockedError,
		)


def _live_public_rates(pl, sales_orders):
	"""Precio público vigente por línea (row.name -> rate), desde la
	selling_price_list de SU Sales Order -- la misma fuente que Cotizaciones
	(pricing.reference_selling_rates). Una consulta por Price List."""
	rows_by_price_list = {}
	for row in pl.get("locations") or []:
		so = sales_orders.get(row.sales_order)
		if so and so.selling_price_list:
			rows_by_price_list.setdefault(so.selling_price_list, []).append(row)
	result = {}
	for price_list, rows in rows_by_price_list.items():
		rates = reference_selling_rates({row.item_code for row in rows}, price_list)
		for row in rows:
			if flt(rates.get(row.item_code)) > 0:
				result[row.name] = flt(rates[row.item_code])
	return result


def _detect_price_mode(final_rate, public_rate, precision):
	"""Etiqueta visual de un precio NO persistido: el modo cuyo resultado
	coincide exactamente (a la precisión del campo) o "Precio del pedido".
	Nunca convierte un precio del pedido en "Precio especial"."""
	if final_rate and final_rate > 0 and public_rate and public_rate > 0:
		for code, percentage in PRICE_MODE_DISCOUNTS.items():
			if discounted_rate(public_rate, percentage, precision, precision) == flt(final_rate, precision):
				return PRICE_MODE_LABELS[code]
	return INVOICE_PRICE_MODE_ORDER if final_rate else None


def _resolve_invoice_lines(pl, sales_orders, public_by_row=None):
	"""LA resolución de precio por línea. Precio final = fg_invoice_rate
	persistido; si no hay, el rate del Sales Order. Precio público = el
	congelado si el Pick List ya está Facturado, si no el vigente."""
	frozen = pl.fg_invoicing_status == FG_INVOICING_FACTURADO
	if public_by_row is None:
		public_by_row = {} if frozen else _live_public_rates(pl, sales_orders)
	so_items = {item.name: item for so in sales_orders.values() for item in so.items}

	lines = []
	for row in pl.get("locations") or []:
		precision = row.precision("fg_invoice_rate")
		so_item = so_items.get(row.sales_order_item)
		order_rate = flt(so_item.rate, precision) if so_item else None
		persisted_rate = _positive_rate(row.fg_invoice_rate, precision)
		persisted = persisted_rate is not None
		if frozen:
			public_rate = _positive_rate(row.fg_invoice_public_rate, precision)
		else:
			public_rate = _positive_rate(public_by_row.get(row.name), precision)
		final_rate = persisted_rate if persisted else _positive_rate(order_rate, precision)
		if persisted and row.fg_invoice_price_mode:
			price_mode = row.fg_invoice_price_mode
		else:
			price_mode = _detect_price_mode(final_rate, public_rate, precision)
		qty = flt(row.picked_qty)
		lines.append(
			frappe._dict(
				row_name=row.name,
				item_code=row.item_code,
				item_name=row.item_name,
				qty=qty,
				uom=row.uom,
				public_rate=public_rate,
				order_rate=order_rate,
				final_rate=final_rate,
				price_mode=price_mode,
				is_special=price_mode == INVOICE_PRICE_MODE_SPECIAL,
				is_persisted=persisted,
				amount=flt(qty * final_rate, precision) if final_rate else None,
				public_amount=flt(qty * public_rate, precision) if public_rate else None,
				precision=precision,
			)
		)
	return lines


def _invoice_pricing_totals(lines):
	"""Subtotal público / ajuste comercial / total final, en el servidor.
	Ningún total se inventa: si falta un precio público, el subtotal público
	y el ajuste son None (N/D); si falta un precio final, el total es None."""
	precision = lines[0].precision if lines else 2
	total = None
	if lines and all(line.final_rate for line in lines):
		total = flt(sum(line.amount for line in lines), precision)
	public_subtotal = None
	if lines and all(line.public_rate for line in lines):
		public_subtotal = flt(sum(line.public_amount for line in lines), precision)
	adjustment = flt(total - public_subtotal, precision) if total is not None and public_subtotal is not None else None
	return {"public_subtotal": public_subtotal, "adjustment": adjustment, "total": total}


def _pricing_payload(pl, sales_orders, lines=None):
	lines = lines if lines is not None else _resolve_invoice_lines(pl, sales_orders)
	currency = next((so.currency for so in sales_orders.values()), None)
	return {
		"pick_list": pl.name,
		"fg_invoicing_status": pl.fg_invoicing_status or FG_INVOICING_PENDIENTE,
		"locked": pl.fg_invoicing_status == FG_INVOICING_FACTURADO,
		"currency": currency,
		"price_lists": sorted({so.selling_price_list for so in sales_orders.values() if so.selling_price_list}),
		"modes": [{"code": code, "label": PRICE_MODE_LABELS[code]} for code in PRICE_MODE_DISCOUNTS],
		"lines": [
			{key: value for key, value in line.items() if key not in ("precision", "public_amount")} for line in lines
		],
		"totals": _invoice_pricing_totals(lines),
		"has_special": any(line.is_special for line in lines),
		"missing_price_items": [line.item_code for line in lines if not line.final_rate],
		"missing_public_items": [line.item_code for line in lines if not line.public_rate],
		"order_adjustments": any(
			flt(so.total_taxes_and_charges) or flt(so.discount_amount) for so in sales_orders.values()
		),
	}


def _assert_no_order_adjustments(sales_orders):
	"""Pedido con impuestos o descuento global: cambiar precios exigiría un
	prorrateo fiscal que no está definido -- se rechaza ANTES de modificar
	cualquier línea (el PDF lo re-valida como defensa en profundidad)."""
	for so in sales_orders.values():
		if flt(so.total_taxes_and_charges) or flt(so.discount_amount):
			frappe.throw(
				_(
					"La Orden de Venta {0} tiene impuestos o descuento global; sus precios no pueden "
					"modificarse desde Facturación sin una regla fiscal definida."
				).format(so.name),
				OrderAdjustmentsPricingError,
			)


def _save_invoice_pricing(pl):
	with _invoice_pricing_write():
		pl.save()  # real permission, no ignore_permissions -- Version registra el cambio


def _parse_invoice_price(raw, precision):
	"""Precio manual: numérico, finito, > 0 y sin más decimales que la
	precisión monetaria del campo. Formato de máquina ("41500" o
	"41500.50"): nunca adivina separadores de miles."""
	if raw is None or isinstance(raw, bool):
		frappe.throw(_("Ingresa un precio válido."), InvalidInvoicePriceError)
	try:
		value = Decimal(str(raw).strip())
	except (InvalidOperation, ValueError):
		frappe.throw(_("El precio debe ser un número."), InvalidInvoicePriceError)
	if not value.is_finite():
		frappe.throw(_("El precio debe ser un número finito."), InvalidInvoicePriceError)
	if value <= 0:
		frappe.throw(_("El precio debe ser mayor que cero."), InvalidInvoicePriceError)
	if -value.as_tuple().exponent > cint(precision):
		frappe.throw(
			_("El precio admite como máximo {0} decimales.").format(cint(precision)), InvalidInvoicePriceError
		)
	return flt(value, precision)


def _freeze_invoice_prices(pl, sales_orders):
	"""mark_as_invoiced(): congela precio final, precio público de
	referencia y modo de CADA línea (en memoria; el .save() lo hace el
	llamador junto con el cambio de estado). Valida TODAS las líneas antes
	de escribir cualquiera."""
	lines = _resolve_invoice_lines(pl, sales_orders)
	missing = [line.item_code for line in lines if not line.final_rate]
	if missing:
		frappe.throw(
			_("No se puede facturar: estos productos no tienen un precio válido: {0}.").format(
				", ".join(missing)
			),
			InvoiceLinePriceMissingError,
		)
	rows = {row.name: row for row in pl.get("locations") or []}
	for line in lines:
		row = rows[line.row_name]
		row.fg_invoice_rate = line.final_rate
		row.fg_invoice_price_mode = line.price_mode
		row.fg_invoice_public_rate = line.public_rate or 0


@frappe.whitelist()
def get_invoicing_pricing(pick_list_name):
	"""Vista comercial de un Pick List (endpoint separado a propósito:
	get_invoicing_detail() mantiene su contrato sin dinero). Por línea:
	precio público, precio del pedido, precio final, modo y total; más
	subtotal público / ajuste comercial / total final. Solo lectura."""
	pl, sales_orders = _load_pricing_pick_list(pick_list_name, "read")
	return _pricing_payload(pl, sales_orders)


@frappe.whitelist()
def apply_invoice_price_mode(pick_list_name, price_mode, replace_special=0):
	"""Descuento general: aplica `price_mode` (FULL/DISCOUNT_10/15/20/25) a
	TODAS las líneas, siempre sobre el precio público vigente.

	Atómico: si CUALQUIER línea no tiene precio público, lanza
	MissingPublicPriceError nombrando los productos y no modifica ninguna.
	Si hay precios especiales y replace_special no es 1, no modifica nada y
	responde requires_confirmation=True (la UI pregunta y repite con
	replace_special=1). Reaplicar el mismo modo no escribe (idempotente)."""
	if price_mode not in PRICE_MODE_DISCOUNTS:
		frappe.throw(_("Selecciona una modalidad de precio válida."), InvalidInvoicePriceError)

	pl, sales_orders = _load_pricing_pick_list(pick_list_name, "write")
	_assert_pricing_editable(pl)
	_assert_no_order_adjustments(sales_orders)

	public_by_row = _live_public_rates(pl, sales_orders)
	rows = pl.get("locations") or []
	missing = [
		row.item_code
		for row in rows
		if _positive_rate(public_by_row.get(row.name), row.precision("fg_invoice_rate")) is None
	]
	if missing:
		frappe.throw(
			_("No se aplicó el descuento: estos productos no tienen precio público: {0}.").format(
				", ".join(missing)
			),
			MissingPublicPriceError,
		)

	specials = [row.item_code for row in rows if row.fg_invoice_price_mode == INVOICE_PRICE_MODE_SPECIAL]
	if specials and not cint(replace_special):
		return {
			"requires_confirmation": True,
			"special_items": specials,
			"changed": False,
			"pricing": _pricing_payload(pl, sales_orders),
		}

	label = PRICE_MODE_LABELS[price_mode]
	percentage = PRICE_MODE_DISCOUNTS[price_mode]
	changed = False
	for row in rows:
		precision = row.precision("fg_invoice_rate")
		public_rate = flt(public_by_row[row.name], precision)
		new_rate = discounted_rate(public_rate, percentage, precision, precision)
		current = (flt(row.fg_invoice_rate, precision), row.fg_invoice_price_mode, flt(row.fg_invoice_public_rate, precision))
		if current != (new_rate, label, public_rate):
			row.fg_invoice_rate = new_rate
			row.fg_invoice_price_mode = label
			row.fg_invoice_public_rate = public_rate
			changed = True
	if changed:
		_save_invoice_pricing(pl)

	return {
		"requires_confirmation": False,
		"special_items": [],
		"changed": changed,
		"pricing": _pricing_payload(pl, sales_orders),
	}


@frappe.whitelist()
def set_invoice_line_price(pick_list_name, pick_list_item, rate):
	"""Precio especial manual de UNA línea. Se permite por encima del
	precio público (es un precio especial, no un descuento) y también en
	líneas sin precio público (fg_invoice_public_rate queda vacío)."""
	pl, sales_orders = _load_pricing_pick_list(pick_list_name, "write")
	_assert_pricing_editable(pl)
	_assert_no_order_adjustments(sales_orders)

	row = next((r for r in (pl.get("locations") or []) if r.name == pick_list_item), None)
	if not row:
		frappe.throw(
			_("La línea {0} no pertenece al Pick List {1}.").format(pick_list_item, pl.name),
			frappe.DoesNotExistError,
		)

	precision = row.precision("fg_invoice_rate")
	new_rate = _parse_invoice_price(rate, precision)
	public_rate = flt(_live_public_rates(pl, sales_orders).get(row.name), precision)

	current = (flt(row.fg_invoice_rate, precision), row.fg_invoice_price_mode, flt(row.fg_invoice_public_rate, precision))
	changed = current != (new_rate, INVOICE_PRICE_MODE_SPECIAL, public_rate)
	if changed:
		row.fg_invoice_rate = new_rate
		row.fg_invoice_price_mode = INVOICE_PRICE_MODE_SPECIAL
		row.fg_invoice_public_rate = public_rate
		_save_invoice_pricing(pl)

	return {"changed": changed, "row_name": row.name, "pricing": _pricing_payload(pl, sales_orders)}
