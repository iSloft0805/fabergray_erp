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

import frappe
from frappe import _
from frappe.utils import flt

MATERIAL_ISSUE = "Material Issue"


class InsufficientStockForIssueError(frappe.ValidationError):
	"""COMPLETAR PEDIDO refused: not enough stock for the Material Issue
	(negative stock is not allowed for this company)."""


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


def validate_stock_for_issue(rows):
	"""Fail BEFORE anything is written when any item/warehouse lacks the
	stock to issue. Locking read of the Bin rows: two orders competing for
	the same stock serialize here, and the second one sees the first one's
	issue. ERPNext's own negative-stock validation stays the final guard."""
	required = defaultdict(float)
	for row, qty in rows:
		required[(row.item_code, row.warehouse)] += qty

	precision = frappe.get_precision("Bin", "actual_qty") or 6
	shortages = []
	for (item_code, warehouse), qty in sorted(required.items()):
		actual_qty = flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty", for_update=True)
		)
		if flt(qty, precision) > flt(actual_qty, precision):
			shortages.append(
				_("{0} en {1}: se requieren {2}, hay {3} en inventario").format(
					item_code, warehouse, flt(qty, precision), flt(actual_qty, precision)
				)
			)

	if shortages:
		frappe.throw(
			_(
				"No se puede completar el pedido: no hay stock suficiente para descontar lo alistado. "
				"No se descontó nada y el pedido sigue en alistamiento.<br>{0}"
			).format("<br>".join(shortages)),
			title=_("Stock insuficiente"),
			exc=InsufficientStockForIssueError,
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
