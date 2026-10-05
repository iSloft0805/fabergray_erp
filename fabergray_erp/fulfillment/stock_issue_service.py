# -*- coding: utf-8 -*-
"""INVENTARIO-OUT-01 -- the physical stock outflow of a completed order.

Decision (closed): stock leaves the inventory when Bodega presses
"COMPLETAR PEDIDO" (api.bodega.finish_picking()), as ONE native Stock Entry
with purpose "Material Issue" per Pick List. Facturación, Recorrido and
Entrega never move stock.

Link Pick List -> Material Issue (structural, native, no remarks):
- Stock Entry.pick_list (native Link, read_only, indexed) = the Pick List;
- Stock Entry Detail.pick_list_item (native) = the exact Pick List Item row.
Stock Entry's own status_updater then writes Pick List Item.transferred_qty
(native, read_only, no_copy) on submit AND on cancel, so every row carries
how much of it already left the inventory. No reverse Custom Field: that
would be a second source of truth that could diverge from the Stock Entry
itself; get_stock_issue() below is the single lookup.

Quantity per row = picked_qty - delivered_qty - transferred_qty (stock UOM):
only what was physically picked and has not left yet -- never the ordered
qty, so a partial pick (10 ordered, 6 picked) issues 6, and the remainder
Pick List issues its own 4 when it is completed.

Accounts: never set here. ERPNext resolves them natively
(StockEntry.get_item_details(): Item Default -> Item Group Default ->
Company.stock_adjustment_account for the debit; the warehouse's inventory
account -> Company.default_inventory_account for the credit).

What stops counting as committed once issued (see analyzer.
_qty_committed_by_open_pick_lists(), pick_list_mixin._get_pick_list_items()
and issued_pending_delivery_qty() below): the issued units already left
Bin.actual_qty, so they are no longer claimed by the Pick List for stock
availability, while they still COVER their Sales Order line (pick_list_
service._qty_already_claimed_by_open_pick_lists_for_so_item() is untouched:
the 6 issued units are never offered again to a remainder Pick List).
"""

from collections import defaultdict
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt

MATERIAL_ISSUE = "Material Issue"
CONTROLLED_SUBMIT_FLAG = "fg_controlled_pick_list_submit"


class InsufficientStockForIssueError(frappe.ValidationError):
	"""COMPLETAR PEDIDO refused: not enough stock for the Material Issue
	(negative stock is not allowed for this company). `shortages` carries
	the structured detail (see stock_shortages_for_issue())."""

	shortages = ()


class IncompleteStockIssueError(frappe.ValidationError):
	"""A SUBMITTED Pick List still has picked units that never left the
	inventory (no Material Issue, or one that does not cover every line).
	COMPLETAR PEDIDO never re-issues it: that is the administrative
	historical review's job. `detail` carries the structured state."""

	detail = None


class PickListStockIssueOverflowError(frappe.ValidationError):
	"""A Material Issue linked to a Pick List would take out more than what
	is still pending on one of its Pick List Items."""


class DirectPickListSubmitError(frappe.ValidationError):
	"""A Delivery Pick List was submitted outside COMPLETAR PEDIDO."""


@contextmanager
def controlled_pick_list_submit(pick_list_name):
	"""The only door to Pick List.submit() for a Delivery Pick List: opened by
	api.bodega._finish_picking() around its own pl.submit(), for that one
	Pick List, and closed right after. frappe.flags is request/job-local and
	never filled from client input, so Desk's Submit button, frappe.client.
	submit, list-view bulk submit and Data Import cannot open it."""
	previous = frappe.flags.get(CONTROLLED_SUBMIT_FLAG)
	frappe.flags[CONTROLLED_SUBMIT_FLAG] = pick_list_name
	try:
		yield
	finally:
		frappe.flags[CONTROLLED_SUBMIT_FLAG] = previous


def guard_pick_list_submit(doc, method=None):
	"""Pick List before_submit (hooks.py). A Delivery Pick List is submitted
	ONLY by COMPLETAR PEDIDO, which also creates its Material Issue in the
	same transaction; any other submit -- whatever the role, Administrator
	and System Manager included -- would leave picked units that never left
	the inventory. Server-side, so it holds for Desk, REST and background
	jobs alike. Blocks, never issues: the Material Issue has exactly one
	creator (finish_picking()), so there is nothing to duplicate. Other
	purposes (Material Transfer for Manufacture, ...) keep the native flow."""
	if doc.purpose != "Delivery":
		return
	if frappe.flags.get(CONTROLLED_SUBMIT_FLAG) == doc.name:
		return
	frappe.throw(
		_(
			"El Pick List {0} solo se finaliza con COMPLETAR PEDIDO en Bodega, que descuenta el "
			"inventario. El envío (Submit) directo no está permitido."
		).format(doc.name),
		title=_("Usa COMPLETAR PEDIDO"),
		exc=DirectPickListSubmitError,
	)


def get_stock_issue(pick_list_name):
	"""Name of the SUBMITTED Material Issue of this Pick List, or None."""
	return frappe.db.get_value(
		"Stock Entry",
		{"pick_list": pick_list_name, "purpose": MATERIAL_ISSUE, "docstatus": 1},
		"name",
	)


def pending_issue_rows(pick_list_doc):
	"""[(Pick List Item row, qty)] still to leave the inventory: picked and
	neither delivered (legacy Sales Invoice/Delivery Note) nor issued."""
	rows = []
	for row in pick_list_doc.get("locations"):
		qty = flt(
			flt(row.picked_qty) - flt(row.delivered_qty) - flt(row.transferred_qty), row.precision("picked_qty")
		)
		if qty > 0:
			rows.append((row, qty))
	return rows


def lock_stock_for_pick_list(pick_list_name):
	"""Lock order for COMPLETAR PEDIDO: the Bin rows of this Pick List's
	lines (sorted), BEFORE the Pick List itself. Every completion competing
	for the same stock queues here while holding nothing else. Taking the
	Pick List lock first deadlocked: get_doc(for_update=True) also locks its
	Pick List Item rows (next-key locks), which the other completion's own
	submit/stock posting then needs while this one waits on the Bin."""
	pairs = frappe.get_all(
		"Pick List Item",
		filters={"parent": pick_list_name, "parenttype": "Pick List"},
		fields=["item_code", "warehouse"],
		distinct=True,
	)
	for pair in sorted({(p.item_code, p.warehouse) for p in pairs if p.item_code and p.warehouse}):
		frappe.db.get_value("Bin", {"item_code": pair[0], "warehouse": pair[1]}, "name", for_update=True)


def stock_shortages_for_issue(rows):
	"""[{item_code, warehouse, required_qty, available_qty, shortage_qty}]
	for every item/warehouse whose current Bin.actual_qty cannot cover the
	units to issue -- empty when everything fits. Units only: valuation never
	takes part. Locking read of the Bin rows: two orders competing for the
	same stock serialize here, and the second one sees the first one's
	issue."""
	required = defaultdict(float)
	for row, qty in rows:
		required[(row.item_code, row.warehouse)] += qty

	precision = frappe.get_precision("Bin", "actual_qty") or 6
	shortages = []
	for (item_code, warehouse), qty in sorted(required.items()):
		required_qty = flt(qty, precision)
		available_qty = flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty", for_update=True),
			precision,
		)
		if required_qty > available_qty:
			shortages.append(
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"required_qty": required_qty,
					"available_qty": available_qty,
					"shortage_qty": flt(required_qty - available_qty, precision),
				}
			)
	return shortages


def validate_stock_for_issue(rows):
	"""Fail BEFORE anything is written when any item/warehouse lacks the
	stock to issue -- all or nothing for the whole Pick List. ERPNext's own
	negative-stock validation stays the final guard.

	The structured detail travels twice: on the exception (`shortages`, for
	server-side callers) and on the HTTP error response as
	`fg_stock_shortages` (frappe.response keys survive into the error body,
	frappe.utils.response.report_error()), next to the readable message."""
	shortages = stock_shortages_for_issue(rows)
	if not shortages:
		return

	frappe.local.response["fg_stock_shortages"] = shortages
	exc = InsufficientStockForIssueError()
	exc.shortages = shortages
	frappe.throw(
		_(
			"No se puede completar el pedido: no hay stock suficiente para descontar lo alistado. "
			"No se descontó nada y el pedido sigue en alistamiento.<br>{0}"
		).format(
			"<br>".join(
				_("{0} en {1}: se requieren {2}, hay {3} en inventario, faltan {4}").format(
					s["item_code"], s["warehouse"], s["required_qty"], s["available_qty"], s["shortage_qty"]
				)
				for s in shortages
			)
		),
		title=_("Stock insuficiente"),
		exc=exc,
	)


def create_stock_issue(pick_list_doc, rows=None):
	"""Create and submit the Material Issue of a SUBMITTED Pick List -- one
	line per pending row, each from that row's own warehouse. Returns the
	Stock Entry, or None when nothing is pending. The caller holds the Pick
	List row lock and a savepoint (api.bodega.finish_picking()).

	System action: Bodega is deliberately NOT granted Stock Entry create/
	submit (that would let it issue arbitrary stock from Desk/API); the
	caller already checked Pick List write + submit permission, and this is
	the consequence of that authorized action -- same pattern as the
	Fulfillment Engine's Pick List inserts."""
	rows = pending_issue_rows(pick_list_doc) if rows is None else rows
	if not rows:
		return None

	for row, _qty in rows:
		if row.batch_no or row.serial_no or row.serial_and_batch_bundle:
			# 0 batch/serial Items on this site (Commit 25.9 audit); never
			# issue a tracked item without its bundle.
			frappe.throw(
				_("Fila #{0}: artículos con lote/serie no están soportados en la salida automática.").format(row.idx)
			)

	validate_stock_for_issue(rows)

	sales_orders = sorted({row.sales_order for row, _qty in rows if row.sales_order})
	entry = frappe.new_doc("Stock Entry")
	entry.purpose = MATERIAL_ISSUE
	entry.company = pick_list_doc.company
	entry.pick_list = pick_list_doc.name
	entry.set_stock_entry_type()
	entry.remarks = _("Salida de inventario al completar el pedido {0} (Pick List {1}).").format(
		", ".join(sales_orders) or "-", pick_list_doc.name
	)
	for row, qty in rows:
		entry.append(
			"items",
			{
				"item_code": row.item_code,
				"s_warehouse": row.warehouse,
				"qty": qty,
				"transfer_qty": qty,
				"uom": row.stock_uom,
				"stock_uom": row.stock_uom,
				"conversion_factor": 1,
				"pick_list_item": row.name,
			},
		)

	from erpnext.stock.stock_ledger import NegativeStockError

	entry.flags.ignore_permissions = True
	try:
		entry.insert()
		entry.submit()
	except NegativeStockError:
		frappe.throw(
			_(
				"No se puede completar el pedido: ERPNext rechazó la salida por stock insuficiente. "
				"No se descontó nada y el pedido sigue en alistamiento."
			),
			title=_("Stock insuficiente"),
			exc=InsufficientStockForIssueError,
		)
	return entry


def guard_pick_list_stock_issue(doc, method=None):
	"""Stock Entry validate (hooks.py) -- runs on insert, save and submit,
	never on cancel. A Material Issue linked to a Pick List may take out, per
	Pick List Item, at most what is still pending on it:
	picked_qty - delivered_qty - transferred_qty. transferred_qty already
	counts every OTHER submitted issue (native status_updater, on submit and
	on cancel) and not this one (its own on_submit runs after validate), so:
	- finish_picking()'s own issue passes untouched (it issues exactly that);
	- a second issue for the same Pick List (Desk, REST, a duplicated job)
	  is refused -- nothing is pending any more;
	- cancel + amend works: the cancel gives the units back to
	  transferred_qty, and the amended draft validates against that.
	Every line must point at a Pick List Item of THAT submitted Pick List,
	same item, same source warehouse -- a line without that link would move
	stock that transferred_qty never records. Units only: no rate, account or
	valuation is read or set. Other purposes keep the native flow."""
	if doc.purpose != MATERIAL_ISSUE or not doc.pick_list:
		return

	if frappe.db.get_value("Pick List", doc.pick_list, "docstatus") != 1:
		frappe.throw(
			_("La salida de inventario del Pick List {0} solo se crea cuando el Pick List está finalizado.").format(
				doc.pick_list
			),
			exc=PickListStockIssueOverflowError,
		)

	missing = [d.idx for d in doc.get("items") if not d.pick_list_item]
	if missing:
		frappe.throw(
			_("Fila(s) {0}: una salida vinculada al Pick List {1} debe indicar su Pick List Item.").format(
				", ".join(str(i) for i in missing), doc.pick_list
			),
			exc=PickListStockIssueOverflowError,
		)

	names = sorted({d.pick_list_item for d in doc.get("items")})
	# Row lock: two issues for the same lines serialize here.
	pick_list_items = {
		r.name: r
		for r in frappe.db.sql(
			"""
			select name, parent, item_code, warehouse, picked_qty, delivered_qty, transferred_qty
			from `tabPick List Item`
			where name in %(names)s and parenttype = 'Pick List'
			for update
			""",
			{"names": tuple(names)},
			as_dict=True,
		)
	}

	precision = frappe.get_precision("Pick List Item", "picked_qty") or 6
	requested = defaultdict(float)
	for d in doc.get("items"):
		pli = pick_list_items.get(d.pick_list_item)
		if not pli or pli.parent != doc.pick_list or pli.item_code != d.item_code or pli.warehouse != d.s_warehouse:
			frappe.throw(
				_("Fila #{0}: el Pick List Item {1} no corresponde a {2} / {3} del Pick List {4}.").format(
					d.idx, d.pick_list_item, d.item_code, d.s_warehouse, doc.pick_list
				),
				exc=PickListStockIssueOverflowError,
			)
		requested[d.pick_list_item] += flt(d.transfer_qty)

	overflows = []
	for name, qty in requested.items():
		pli = pick_list_items[name]
		pending = max(flt(flt(pli.picked_qty) - flt(pli.delivered_qty) - flt(pli.transferred_qty), precision), 0)
		if flt(qty, precision) > pending:
			overflows.append(
				_("{0} en {1}: se intenta descontar {2}, pendiente {3}").format(
					pli.item_code, pli.warehouse, flt(qty, precision), pending
				)
			)

	if overflows:
		frappe.throw(
			_(
				"La salida supera lo pendiente por descontar del Pick List {0} "
				"(lo alistado menos lo ya descontado). No se descontó nada.<br>{1}"
			).format(doc.pick_list, "<br>".join(overflows)),
			title=_("Salida duplicada"),
			exc=PickListStockIssueOverflowError,
		)


class SalesInvoiceDoubleIssueError(frappe.ValidationError):
	"""A stock-moving Sales Invoice would take out units that COMPLETAR
	PEDIDO's Material Issue already took out."""


def _issued_qty_by_pick_list_item(pick_list_items):
	"""{pick_list_item: (issued qty, [Material Issue names])} -- what the
	SUBMITTED Material Issues linked to these rows took out (Stock Entry
	Detail.pick_list_item, the same link that fills transferred_qty)."""
	if not pick_list_items:
		return {}
	rows = frappe.db.sql(
		"""
		select sed.pick_list_item, sum(sed.transfer_qty) as qty, group_concat(distinct se.name order by se.name) as entries
		from `tabStock Entry Detail` sed
		inner join `tabStock Entry` se on se.name = sed.parent
		where se.docstatus = 1 and se.purpose = %(purpose)s and sed.pick_list_item in %(names)s
		group by sed.pick_list_item
		""",
		{"purpose": MATERIAL_ISSUE, "names": tuple(pick_list_items)},
		as_dict=True,
	)
	return {r.pick_list_item: (flt(r.qty), r.entries.split(",")) for r in rows if flt(r.qty) > 0}


def guard_sales_invoice_double_issue(doc, method=None):
	"""Sales Invoice validate (hooks.py) -- runs on save and submit, never on
	cancel. Invariant: a picked Pick List leaves the inventory exactly once,
	through its Material Issue (finish_picking()). A stock-moving Sales
	Invoice (update_stock=1) -- e.g. ERPNext's native create_delivery(pl,
	target="Sales Invoice"), reachable from Desk -- would take the same units
	out a second time.

	Scope, and nothing else: update_stock=1, not a return, and only the lines
	tied to a Pick List. Link used per line, strongest first:
	- pick_list_item (the exact row; the native join field ERPNext's own
	  status_updater uses for delivered_qty): refused when a submitted
	  Material Issue already issued units of that row (transferred_qty);
	- so_detail without pick_list_item (an invoice made from the Sales
	  Order): refused only for the units of that Sales Order line already
	  issued -- a line may still invoice what was never picked.
	One offending line rejects the whole invoice (validate runs before any
	write), so a mixed invoice never moves stock partially. Read-only:
	transferred_qty/delivered_qty, accounts and Company are never touched."""
	if not cint(doc.get("update_stock")) or cint(doc.get("is_return")):
		return

	items = doc.get("items") or []
	by_pick_list_item = _issued_qty_by_pick_list_item(sorted({d.pick_list_item for d in items if d.pick_list_item}))

	so_lines = sorted({d.so_detail for d in items if d.so_detail and not d.pick_list_item})
	issued_by_so_line = {}
	if so_lines:
		pick_list_items = frappe.get_all(
			"Pick List Item",
			filters={"sales_order_item": ["in", so_lines], "docstatus": 1},
			fields=["name", "sales_order_item"],
		)
		issued = _issued_qty_by_pick_list_item([p.name for p in pick_list_items])
		for p in pick_list_items:
			if p.name in issued:
				qty, entries = issued[p.name]
				total, names = issued_by_so_line.get(p.sales_order_item, (0, []))
				issued_by_so_line[p.sales_order_item] = (total + qty, names + entries)

	precision = frappe.get_precision("Sales Invoice Item", "stock_qty") or 6
	offending, entries = [], set()
	so_requested = defaultdict(float)
	for d in items:
		if d.pick_list_item in by_pick_list_item:
			qty, names = by_pick_list_item[d.pick_list_item]
			offending.append(_("Fila #{0} ({1}): {2} unidades").format(d.idx, d.item_code, flt(qty, precision)))
			entries.update(names)
		elif d.so_detail in issued_by_so_line:
			so_requested[d.so_detail] += flt(d.stock_qty) or flt(d.qty) * (flt(d.conversion_factor) or 1)

	for so_detail, requested in so_requested.items():
		issued, names = issued_by_so_line[so_detail]
		soi = frappe.db.get_value("Sales Order Item", so_detail, ["stock_qty", "delivered_qty", "conversion_factor"], as_dict=True)
		never_issued = flt(soi.stock_qty) - issued - flt(soi.delivered_qty) * (flt(soi.conversion_factor) or 1)
		if flt(requested, precision) > flt(never_issued, precision):
			rows = [d for d in items if d.so_detail == so_detail]
			offending.append(
				_("Fila(s) {0} ({1}): {2} unidades ya descontadas").format(
					", ".join(str(d.idx) for d in rows), rows[0].item_code, flt(issued, precision)
				)
			)
			entries.update(names)

	if offending:
		frappe.throw(
			_(
				"El inventario de este pedido ya fue descontado al finalizar el alistamiento mediante "
				"Material Issue {0}. La factura no puede volver a actualizar stock.<br>{1}"
			).format(", ".join(sorted(entries)), "<br>".join(offending)),
			title=_("Inventario ya descontado"),
			exc=SalesInvoiceDoubleIssueError,
		)


def issued_pending_delivery_qty(item_codes=None):
	"""{(item_code, warehouse): qty} -- units already ISSUED (Pick List
	Item.transferred_qty) that ERPNext's native Bin.reserved_qty still
	counts, because that figure is Sales-Order based (stock_balance.
	get_reserved_qty(): stock_qty * (qty - delivered_qty) / qty for every
	submitted order not On Hold/Closed) and a Material Issue never touches
	Sales Order Item.delivered_qty.

	Why the native Bin may keep showing it (approved, deliberate): Fabrigray
	never creates a Delivery Note, so delivered_qty stays 0 until the order
	is closed natively, and the only native ways to lower reserved_qty
	(delivered_qty, closing the Sales Order, or writing Bin) are all out of
	bounds -- they would fake a delivery or falsify Bin. So after COMPLETAR
	PEDIDO the native Bin reads e.g. actual 16 / reserved 4 / projected 12,
	while Fabrigray's OPERATIONAL figures read reserved 0 / available 16 /
	projected 16: same data, the issued units counted once.

	Read-only: Bin is never written. Callers subtract it from reserved_qty
	and add it to projected_qty to show the operational figures (the stock
	left actual_qty once, and must not also stay reserved). Capped per Sales
	Order line at what that line still reserves natively, and keyed by the
	line's own warehouse -- the Bin its reservation lives in."""
	if item_codes is not None and not item_codes:
		return {}

	conditions = ""
	values = {}
	if item_codes is not None:
		conditions = "and soi.item_code in %(item_codes)s"
		values["item_codes"] = tuple(item_codes)

	rows = frappe.db.sql(
		f"""
		select soi.item_code, soi.warehouse,
			sum(least(issued.qty, soi.stock_qty * (soi.qty - soi.delivered_qty) / soi.qty)) as qty
		from (
			select pli.sales_order_item, sum(pli.transferred_qty) as qty
			from `tabPick List Item` pli
			where pli.docstatus = 1 and pli.transferred_qty > 0 and ifnull(pli.sales_order_item, '') != ''
			group by pli.sales_order_item
		) issued
		inner join `tabSales Order Item` soi on soi.name = issued.sales_order_item
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1 and so.status not in ('On Hold', 'Closed')
			and ifnull(soi.delivered_by_supplier, 0) = 0
			and soi.qty > 0 and soi.qty >= soi.delivered_qty
			{conditions}
		group by soi.item_code, soi.warehouse
		""",
		values,
		as_dict=True,
	)
	return {(r.item_code, r.warehouse): flt(r.qty) for r in rows if flt(r.qty) > 0}
