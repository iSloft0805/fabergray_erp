# -*- coding: utf-8 -*-
"""INVENTARIO-OUT-01 -- historical audit of completed Pick Lists that never
issued their stock (finished before COMPLETAR PEDIDO created a Material
Issue), counted from the physical inventory cut-off.

Administrative, never whitelisted: run with
    bench --site <site> execute fabergray_erp.fulfillment.stock_issue_reconciliation.reconcile_historical_stock_issues

First version is DRY RUN ONLY. It reads, it never writes: no Stock Entry,
no Stock Ledger Entry, no GL Entry, no Pick List change. dry_run=False is
refused until the real correction (posting date, approval) is decided.

Scope: every SUBMITTED Pick List of the company finalized (its own
docstatus 0 -> 1 Version, Pick List track_changes=1) on/after `from_date`
with units picked that neither left through a Material Issue
(transferred_qty) nor through a legacy stock-moving invoice/delivery
(delivered_qty). That covers "listos", Facturado, en Recorrido and
Entregado alike -- none of those steps ever moves stock."""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, get_datetime

from erpnext import get_default_company

from fabergray_erp.fulfillment.stock_issue_service import MATERIAL_ISSUE, get_stock_issue

CUTOFF_DATE = "2026-09-26"
ACTIVE_ROUTE_STATUSES = ("Borrador", "Planificado", "En Ruta")


def _require_admin():
	if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Solo un administrador puede auditar la salida histórica de inventario."), frappe.PermissionError)


def _finalized_on_by_pick_list(names):
	"""{pick_list: datetime of its submit} from its own Version log."""
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		select docname, min(creation) as finalized_on
		from `tabVersion`
		where ref_doctype = 'Pick List' and docname in %(names)s
			and data like %(pattern)s
		group by docname
		""",
		{"names": tuple(names), "pattern": '%["docstatus",0,1]%'},
		as_dict=True,
	)
	return {r.docname: r.finalized_on for r in rows}


def _route_state_by_pick_list(names):
	"""{pick_list: {"en_recorrido": bool, "entregado": bool, "recorridos": [...]}}."""
	state = defaultdict(lambda: {"en_recorrido": False, "entregado": False, "recorridos": []})
	if not names:
		return state
	stops = frappe.get_all(
		"Recorrido Parada",
		filters={"pick_list": ["in", list(names)]},
		fields=["pick_list", "status", "recorrido"],
	)
	route_status = dict(
		frappe.get_all(
			"Recorrido",
			filters={"name": ["in", list({s.recorrido for s in stops if s.recorrido}) or [""]]},
			fields=["name", "status"],
			as_list=True,
		)
	)
	for stop in stops:
		entry = state[stop.pick_list]
		entry["recorridos"].append(f"{stop.recorrido} ({route_status.get(stop.recorrido)}, parada {stop.status})")
		if stop.status == "Entregado":
			entry["entregado"] = True
		if route_status.get(stop.recorrido) in ACTIVE_ROUTE_STATUSES or stop.status == "Entregado":
			entry["en_recorrido"] = True
	return state


def _stock_movements_by_pick_list(names):
	"""{pick_list: [voucher labels]} -- any submitted document that moved (or
	would move) stock for the Pick List: Stock Entry of any purpose, legacy
	Sales Invoice with update_stock, Delivery Note."""
	movements = defaultdict(list)
	if not names:
		return movements
	for se in frappe.get_all(
		"Stock Entry", filters={"pick_list": ["in", list(names)], "docstatus": 1}, fields=["name", "pick_list", "purpose"]
	):
		movements[se.pick_list].append(f"Stock Entry {se.name} ({se.purpose})")
	for doctype, parent_doctype, extra in (
		("Sales Invoice Item", "Sales Invoice", " and p.update_stock = 1"),
		("Delivery Note Item", "Delivery Note", ""),
	):
		for r in frappe.db.sql(
			f"""
			select distinct c.against_pick_list as pick_list, p.name
			from `tab{doctype}` c inner join `tab{parent_doctype}` p on p.name = c.parent
			where p.docstatus = 1 and c.against_pick_list in %(names)s{extra}
			""",
			{"names": tuple(names)},
			as_dict=True,
		):
			movements[r.pick_list].append(f"{parent_doctype} {r.name}")
	return movements


def audit_pick_lists_without_stock_issue(from_date=CUTOFF_DATE, company=None):
	"""Read-only. One entry per Pick List (with its pending rows) finalized
	on/after `from_date` that still has picked units that never left the
	inventory, plus the totals by item, by warehouse and overall."""
	company = company or get_default_company()
	cutoff = get_datetime(f"{from_date} 00:00:00")

	submitted = frappe.get_all(
		"Pick List",
		filters={"docstatus": 1, "company": company, "purpose": "Delivery"},
		fields=["name", "customer", "status", "delivery_status", "fg_invoicing_status", "creation"],
		order_by="creation asc",
		limit_page_length=0,
	)
	finalized_on = _finalized_on_by_pick_list([p.name for p in submitted])
	candidates = []
	for pl in submitted:
		# No Version row (never expected: track_changes=1) -> the Pick
		# List's own creation, so it is never silently dropped.
		pl.finalized_on = finalized_on.get(pl.name) or pl.creation
		pl.finalized_on_source = "Version" if pl.name in finalized_on else "creation"
		if get_datetime(pl.finalized_on) >= cutoff:
			candidates.append(pl)

	names = [pl.name for pl in candidates]
	rows_by_pick_list = defaultdict(list)
	if names:
		for row in frappe.get_all(
			"Pick List Item",
			filters={"parent": ["in", names]},
			fields=[
				"name",
				"parent",
				"idx",
				"item_code",
				"item_name",
				"warehouse",
				"picked_qty",
				"delivered_qty",
				"transferred_qty",
				"stock_uom",
				"sales_order",
			],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		):
			row.pending_qty = flt(flt(row.picked_qty) - flt(row.delivered_qty) - flt(row.transferred_qty), 6)
			rows_by_pick_list[row.parent].append(row)

	route_state = _route_state_by_pick_list(names)
	movements = _stock_movements_by_pick_list(names)
	customer_names = dict(
		frappe.get_all(
			"Customer",
			filters={"name": ["in", list({pl.customer for pl in candidates if pl.customer}) or [""]]},
			fields=["name", "customer_name"],
			as_list=True,
		)
	)

	pick_lists = []
	by_item = defaultdict(float)
	by_warehouse = defaultdict(float)
	by_item_warehouse = defaultdict(float)
	for pl in candidates:
		stock_issue = get_stock_issue(pl.name)
		pending = [r for r in rows_by_pick_list[pl.name] if r.pending_qty > 0]
		if stock_issue or not pending:
			continue
		for r in pending:
			by_item[r.item_code] += r.pending_qty
			by_warehouse[r.warehouse] += r.pending_qty
			by_item_warehouse[(r.item_code, r.warehouse)] += r.pending_qty
		sales_orders = sorted({r.sales_order for r in rows_by_pick_list[pl.name] if r.sales_order})
		pick_lists.append(
			{
				"pick_list": pl.name,
				"sales_orders": sales_orders,
				"customer": pl.customer,
				"customer_name": customer_names.get(pl.customer),
				"finalized_on": pl.finalized_on,
				"finalized_on_source": pl.finalized_on_source,
				"status": pl.status,
				"facturado": pl.fg_invoicing_status == "Facturado",
				"en_recorrido": route_state[pl.name]["en_recorrido"],
				"entregado": route_state[pl.name]["entregado"],
				"recorridos": route_state[pl.name]["recorridos"],
				"material_issue": stock_issue,
				"stock_movements": movements.get(pl.name, []),
				"rows": [
					{
						"pick_list_item": r.name,
						"idx": r.idx,
						"item_code": r.item_code,
						"item_name": r.item_name,
						"warehouse": r.warehouse,
						"picked_qty": flt(r.picked_qty),
						"delivered_qty": flt(r.delivered_qty),
						"transferred_qty": flt(r.transferred_qty),
						"pending_qty": r.pending_qty,
						"stock_uom": r.stock_uom,
					}
					for r in pending
				],
			}
		)

	return {
		"company": company,
		"from_date": str(from_date),
		"pick_lists": pick_lists,
		"totals": {
			"by_item": dict(sorted(by_item.items())),
			"by_warehouse": dict(sorted(by_warehouse.items())),
			"by_item_warehouse": [
				{"item_code": item_code, "warehouse": warehouse, "qty": qty}
				for (item_code, warehouse), qty in sorted(by_item_warehouse.items())
			],
			"total_qty": flt(sum(by_item.values()), 6),
			"pick_list_count": len(pick_lists),
		},
	}


def reconcile_historical_stock_issues(from_date=CUTOFF_DATE, dry_run=True, company=None):
	"""DRY RUN ONLY (this version). Returns what a correction WOULD do: which
	Pick Lists, which Material Issues (one per Pick List, one line per
	pending row, each from its row's own warehouse, linked by pick_list/
	pick_list_item exactly like COMPLETAR PEDIDO), which quantities, and
	whether today's stock could absorb them (negative stock is not
	allowed). Writes nothing."""
	_require_admin()
	# bench execute passes kwargs as strings ("False").
	if isinstance(dry_run, str):
		dry_run = dry_run.strip().lower() not in ("0", "false", "no")
	if not dry_run:
		frappe.throw(
			_("La corrección histórica real no está habilitada todavía: solo dry_run=True."),
			title=_("Solo simulación"),
		)

	audit = audit_pick_lists_without_stock_issue(from_date=from_date, company=company)

	proposed = []
	for pl in audit["pick_lists"]:
		proposed.append(
			{
				"pick_list": pl["pick_list"],
				"stock_entry": {
					"doctype": "Stock Entry",
					"purpose": MATERIAL_ISSUE,
					"company": audit["company"],
					"pick_list": pl["pick_list"],
					"items": [
						{
							"item_code": r["item_code"],
							"s_warehouse": r["warehouse"],
							"qty": r["pending_qty"],
							"pick_list_item": r["pick_list_item"],
						}
						for r in pl["rows"]
					],
				},
			}
		)

	stock_check = []
	for line in audit["totals"]["by_item_warehouse"]:
		actual_qty = flt(
			frappe.db.get_value("Bin", {"item_code": line["item_code"], "warehouse": line["warehouse"]}, "actual_qty")
		)
		stock_check.append(
			{
				**line,
				"actual_qty": actual_qty,
				"qty_after": flt(actual_qty - line["qty"], 6),
				"sufficient": actual_qty >= line["qty"],
			}
		)

	return {
		"dry_run": True,
		"from_date": audit["from_date"],
		"company": audit["company"],
		"pick_lists": audit["pick_lists"],
		"proposed_stock_entries": proposed,
		"stock_check": stock_check,
		"totals": audit["totals"],
	}
