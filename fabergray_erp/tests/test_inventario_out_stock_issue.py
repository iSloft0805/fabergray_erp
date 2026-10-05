# -*- coding: utf-8 -*-
"""INVENTARIO-OUT-01 -- COMPLETAR PEDIDO (api.bodega.finish_picking()) issues
the picked stock as ONE native Material Issue per Pick List; Facturación,
Recorrido and Entrega never move stock; issued units stop counting as
committed; the historical audit/reconciliation is dry-run only.

Stock is seeded with fixtures.stock_up_real() (a real Stock Reconciliation,
so the Material Issue's own negative-stock validation sees it) in this
suite's own throwaway warehouses only -- never a real warehouse. Every
Stock Entry created by COMPLETAR PEDIDO is cancelled and deleted by
TestWorld.cleanup() before its Pick List, and the warehouses' Stock
Ledger/GL rows are purged with them."""

import threading
from datetime import date, datetime, time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, get_datetime, nowdate

from erpnext.stock import get_warehouse_account_map
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

from fabergray_erp.api import bodega, facturacion, inventario
from fabergray_erp.fulfillment import analyzer, stock_issue_service
from fabergray_erp.fulfillment.pick_list_service import (
	create_pick_list_for_full_demand,
	create_pick_list_for_remaining_demand,
)
from fabergray_erp.fulfillment.stock_issue_reconciliation import (
	audit_pick_lists_without_stock_issue,
	reconcile_historical_stock_issues,
)
from fabergray_erp.fulfillment.stock_issue_service import (
	DirectPickListSubmitError,
	IncompleteStockIssueError,
	InsufficientStockForIssueError,
	PickListStockIssueOverflowError,
	get_stock_issue,
	pending_issue_rows,
)
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as recorridos_base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

RATE = 1000


def _actual(item_code, warehouse):
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def _bin(item_code, warehouse):
	return frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "reserved_qty", "projected_qty"],
		as_dict=True,
	)


def _native_claim(item_code, warehouse):
	"""What ERPNext's own Pick List location search sees as already claimed
	by other open Pick Lists (through pick_list_mixin._get_pick_list_items)."""
	probe = frappe.new_doc("Pick List")
	probe.company = fx.COMPANY
	return sum(
		flt(r.picked_qty)
		for r in probe._get_pick_list_items([frappe._dict(item_code=item_code)])
		if r.warehouse == warehouse
	)


def _stock_issues(pick_list):
	return frappe.get_all(
		"Stock Entry", filters={"pick_list": pick_list, "docstatus": 1}, fields=["name", "purpose", "company"]
	)


def _ledger_counts(item_code):
	return (
		frappe.db.count("Stock Ledger Entry", {"item_code": item_code}),
		frappe.db.count("Stock Entry"),
		frappe.db.count("GL Entry"),
	)


class _StockIssueMixin:
	"""Shared fixtures: throwaway Líquidos/Varios warehouses, Items with a
	real stock ledger, the real Bodega flow."""

	@classmethod
	def _setup_world(cls, prefix):
		cls.item_codes = []
		cls.addClassCleanup(cls._purge_item_prices)
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.tag = frappe.generate_hash(length=4).upper()
		cls.prefix = prefix
		cls.wh_liq = cls.world.warehouse(f"{prefix} Liquidos {cls.tag}").name
		cls.wh_var = cls.world.warehouse(f"{prefix} Varios {cls.tag}").name
		cls.customer_name = cls.world.customer(f"{prefix} Cliente {cls.tag}").name
		cls.bodega_user = cls.world.user(f"{prefix.lower()}-bodega-{cls.tag.lower()}@example.com", ["Bodega"])
		for warehouse in (cls.wh_liq, cls.wh_var):
			cls.world.warehouse_user_permission(cls.bodega_user, warehouse)
		cls.facturacion_user = cls.world.user(
			f"{prefix.lower()}-facturacion-{cls.tag.lower()}@example.com", ["Facturación"]
		)
		cls._seq = 0

	@classmethod
	def _purge_item_prices(cls):
		"""Item Prices native Sales Order insert auto-creates for this suite's
		own throwaway Items (runs after world.cleanup -- LIFO)."""
		if cls.item_codes:
			frappe.db.delete("Item Price", {"item_code": ["in", cls.item_codes]})
			frappe.db.delete("Bin", {"item_code": ["in", cls.item_codes]})
			frappe.db.commit()

	def _item(self, warehouse, stock=None):
		type(self)._seq += 1
		item_code = self.world.item(f"{self.prefix}-{self.tag}-{self._seq}", default_warehouse=warehouse).name
		self.item_codes.append(item_code)
		if stock is not None:
			self.world.stock_up_real(item_code, warehouse, stock, rate=RATE)
		return item_code

	def _order(self, *lines):
		"""(item_code, warehouse, qty) lines -> submitted Sales Order + the
		full-demand Pick List the real on_submit hook builds."""
		so = self.world.multi_item_sales_order(
			self.customer_name,
			[{"item_code": i, "warehouse": w, "qty": q, "rate": 5000} for i, w, q in lines],
		)
		pl = create_pick_list_for_full_demand(so)
		self.world.track_existing("Pick List", pl.name)
		return so, pl.name

	def _pick(self, pl_name, picked_by_item=None):
		"""Real Bodega flow: start + picked_qty per row (default: everything
		requested); a short row gets its Reporte de Faltante, as required
		before finishing."""
		picked_by_item = dict(picked_by_item or {})
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_name)
			for row in bodega.get_pick_list(pl_name)["rows"]:
				requested = flt(row["qty_solicitada"])
				qty = min(picked_by_item.pop(row["item_code"], requested), requested)
				bodega.set_picked_qty(pl_name, row["row_name"], qty)
				if qty < requested:
					report = bodega.report_shortage(pl_name, row["row_name"], qty, "Stock insuficiente")
					self.world.track_existing("Reporte de Faltante", report["name"])

	def _complete(self, pl_name):
		with fx.as_user(self.bodega_user):
			return bodega.finish_picking(pl_name)


class TestCompleteOrderStockIssue(_StockIssueMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_world("FGOUT")

	# =====================================================================
	# COMPLETAR PEDIDO -> Material Issue, committed/reserved/projected
	# =====================================================================

	def test_complete_order_issues_stock_and_stops_counting_it_as_committed(self):
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 4))

		# Pedido abierto: 4 comprometidas, 16 disponibles para otro alistamiento.
		self.assertEqual(analyzer._qty_committed_by_open_pick_lists(item, self.wh_liq), 4)
		self.assertEqual(analyzer._qty_available_for_pick(item, self.wh_liq, fx.COMPANY), 16)

		self._pick(pl)
		before = _bin(item, self.wh_liq)
		self.assertEqual((before.actual_qty, before.reserved_qty, before.projected_qty), (20, 4, 16))
		self.assertEqual(_native_claim(item, self.wh_liq), 4)

		result = self._complete(pl)

		self.assertFalse(result["already_completed"])
		self.assertEqual(result["docstatus"], 1)
		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual((entry.purpose, entry.docstatus, entry.company), ("Material Issue", 1, fx.COMPANY))
		self.assertEqual(entry.pick_list, pl)
		pl_row = frappe.get_doc("Pick List", pl).locations[0]
		self.assertEqual(
			[(d.item_code, d.s_warehouse, flt(d.qty), d.pick_list_item) for d in entry.items],
			[(item, self.wh_liq, 4, pl_row.name)],
		)
		self.assertEqual(get_stock_issue(pl), entry.name)

		# 20 -> 16, once.
		self.assertEqual(_actual(item, self.wh_liq), 16)
		self.assertEqual(flt(pl_row.transferred_qty), 4)
		self.assertEqual(flt(pl_row.picked_qty), 4)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "delivery_status"), "Not Delivered")

		# Native Bin keeps the Sales Order reservation (no Delivery Note ever
		# touches delivered_qty): documented, never written.
		after = _bin(item, self.wh_liq)
		self.assertEqual((after.actual_qty, after.reserved_qty, after.projected_qty), (16, 4, 12))

		# ...but nothing counts the issued 4 as committed any more.
		self.assertEqual(analyzer._qty_committed_by_open_pick_lists(item, self.wh_liq), 0)
		self.assertEqual(analyzer._qty_available_for_pick(item, self.wh_liq, fx.COMPANY), 16)
		self.assertEqual(_native_claim(item, self.wh_liq), 0)
		with fx.as_user(self.bodega_user):
			row = next(r for r in bodega.get_inventory() if r["item_code"] == item)
		self.assertEqual((row["actual_qty"], row["reserved_qty"], row["available_qty"]), (16, 0, 16))
		with patch.object(inventario, "_stock_warehouses", return_value=[self.wh_liq, self.wh_var]):
			detail = inventario.get_inventory_item_detail(item)
		bin_row = next(r for r in detail["stock_by_warehouse"] if r["warehouse"] == self.wh_liq)
		self.assertEqual((bin_row["actual_qty"], bin_row["reserved_qty"], bin_row["projected_qty"]), (16, 0, 16))

	# =====================================================================
	# Fixture: stock_up_real() posts safely in the past (WSL2 clock steps)
	# =====================================================================

	def _posting(self, doctype, name):
		posting_date, posting_time = frappe.db.get_value(doctype, name, ["posting_date", "posting_time"])
		return get_datetime(f"{posting_date} {posting_time}")

	def test_stock_up_real_seed_is_posted_before_the_issue_that_consumes_it(self):
		item = self.world.item(f"{self.prefix}-{self.tag}-SEED").name
		self.item_codes.append(item)
		seed = self.world.stock_up_real(item, self.wh_liq, 20, rate=RATE)
		self.assertEqual((seed.docstatus, seed.set_posting_time), (1, 1))
		self.assertEqual(_actual(item, self.wh_liq), 20)

		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		entry = self._complete(pl)["stock_entry"]

		self.assertLess(self._posting("Stock Reconciliation", seed.name), self._posting("Stock Entry", entry))
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_seed_posting_datetime_moves_the_date_across_midnight(self):
		item = self.world.item(f"{self.prefix}-{self.tag}-MIDNIGHT").name
		self.item_codes.append(item)
		with patch.object(fx, "now_datetime", return_value=datetime(2026, 9, 30, 0, 0, 30)):
			posting = fx.TestWorld._seed_posting_datetime(item, self.wh_liq)
		self.assertEqual(posting, datetime(2026, 9, 29, 23, 59, 30))
		self.assertEqual((posting.date(), posting.time()), (date(2026, 9, 29), time(23, 59, 30)))

	def test_reseed_is_never_posted_before_the_latest_movement(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		entry = self._complete(pl)["stock_entry"]
		self.assertEqual(_actual(item, self.wh_liq), 16)

		reseed = self.world.stock_up_real(item, self.wh_liq, 20, rate=RATE)

		self.assertGreater(self._posting("Stock Reconciliation", reseed.name), self._posting("Stock Entry", entry))
		self.assertEqual(_actual(item, self.wh_liq), 20)  # never 20 - 4 replayed on top

	def test_native_bin_versus_operational_availability(self):
		"""Documents the approved difference: after COMPLETAR PEDIDO the native
		Bin still reserves the 4 (Sales Order based, no Delivery Note ever
		lowers it) while Fabrigray's operational figures count them once."""
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)

		def operational():
			with fx.as_user(self.bodega_user):
				row = next(r for r in bodega.get_inventory() if r["item_code"] == item)
			with patch.object(inventario, "_stock_warehouses", return_value=[self.wh_liq]):
				detail = inventario.get_inventory_item_detail(item)
			bin_row = next(r for r in detail["stock_by_warehouse"] if r["warehouse"] == self.wh_liq)
			return row, bin_row

		# Before: actual 20, native reserved 4, operational available 16.
		native = _bin(item, self.wh_liq)
		self.assertEqual((native.actual_qty, native.reserved_qty), (20, 4))
		self.assertEqual(stock_issue_service.issued_pending_delivery_qty([item]), {})
		row, bin_row = operational()
		self.assertEqual((row["reserved_qty"], row["available_qty"]), (4, 16))
		self.assertEqual(bin_row["projected_qty"], 16)

		self._complete(pl)

		# After: actual 16; the NATIVE Bin may keep reserved 4 / projected 12...
		native = _bin(item, self.wh_liq)
		self.assertEqual((native.actual_qty, native.reserved_qty, native.projected_qty), (16, 4, 12))
		# ...but the 4 are issued, so operationally: reserved 0, available 16, projected 16.
		self.assertEqual(stock_issue_service.issued_pending_delivery_qty([item]), {(item, self.wh_liq): 4})
		row, bin_row = operational()
		self.assertEqual((row["actual_qty"], row["reserved_qty"], row["available_qty"]), (16, 0, 16))
		self.assertEqual((bin_row["reserved_qty"], bin_row["projected_qty"]), (0, 16))
		self.assertEqual(analyzer._qty_committed_by_open_pick_lists(item, self.wh_liq), 0)
		self.assertEqual(analyzer._qty_available_for_pick(item, self.wh_liq, fx.COMPANY), 16)

	def test_a_new_order_sees_the_issued_stock_only_once(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		self._complete(pl)

		_so2, pl2 = self._order((item, self.wh_liq, 16))
		rows = frappe.get_all("Pick List Item", filters={"parent": pl2}, fields=["warehouse", "stock_qty"])
		self.assertEqual(sum(flt(r.stock_qty) for r in rows), 16)
		self._pick(pl2)
		self._complete(pl2)
		self.assertEqual(_actual(item, self.wh_liq), 0)

	# =====================================================================
	# Idempotency / concurrency
	# =====================================================================

	def test_double_click_is_idempotent(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)

		first = self._complete(pl)
		second = self._complete(pl)

		self.assertFalse(first["already_completed"])
		self.assertTrue(second["already_completed"])
		self.assertEqual(second["stock_entry"], first["stock_entry"])
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_concurrent_complete_creates_exactly_one_issue(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		frappe.db.commit()

		results = self._run_concurrently([pl, pl])

		self.assertTrue(all(r and r[0] == "ok" for r in results), results)
		self.assertEqual(sorted(r[1]["already_completed"] for r in results), [False, True])
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_concurrent_orders_competing_for_scarce_stock(self):
		item = self._item(self.wh_liq, stock=5)
		_so_a, pl_a = self._order((item, self.wh_liq, 4))
		_so_b, pl_b = self._order((item, self.wh_liq, 4))
		self._pick(pl_a)
		self._pick(pl_b)
		frappe.db.commit()

		results = self._run_concurrently([pl_a, pl_b])

		outcomes = sorted(r[0] for r in results)
		self.assertEqual(outcomes, ["InsufficientStockForIssueError", "ok"], results)
		self.assertEqual(_actual(item, self.wh_liq), 1)
		issued = [pl for pl in (pl_a, pl_b) if _stock_issues(pl)]
		self.assertEqual(len(issued), 1)
		failed = pl_b if issued == [pl_a] else pl_a
		self.assertEqual(frappe.db.get_value("Pick List", failed, "docstatus"), 0)

	def _run_concurrently(self, pick_lists):
		site = frappe.local.site
		results = [None] * len(pick_lists)
		barrier = threading.Barrier(len(pick_lists))

		def attempt(index, pl_name):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.bodega_user)
			try:
				barrier.wait(timeout=30)
				result = bodega.finish_picking(pl_name)
				frappe.db.commit()
				results[index] = ("ok", result)
			except Exception as e:
				frappe.db.rollback()
				results[index] = (type(e).__name__, str(e))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=attempt, args=(i, pl)) for i, pl in enumerate(pick_lists)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=120)

		# frappe.local is per thread: the main thread keeps its own context
		# (re-initialising it would reset the test runner's flags); the
		# commit above ended its transaction, so it now reads the threads'.
		frappe.db.rollback()
		return results

	# =====================================================================
	# Insufficient stock -- never a partial state
	# =====================================================================

	def test_insufficient_stock_fails_before_anything_is_written(self):
		item = self._item(self.wh_liq, stock=3)
		so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		counts = _ledger_counts(item)

		with self.assertRaises(InsufficientStockForIssueError) as ctx:
			self._complete(pl)

		self.assertIn("stock suficiente", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(_actual(item, self.wh_liq), 3)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 0)
		self.assertEqual(_ledger_counts(item), counts)

	def test_erpnext_negative_stock_guard_rolls_back_the_submit_too(self):
		"""Even if the pre-check were bypassed (a race it cannot see), ERPNext's
		own negative-stock validation refuses the Material Issue and the
		savepoint takes the Pick List submit back with it."""
		item = self._item(self.wh_liq, stock=3)
		so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)

		with patch.object(bodega, "validate_stock_for_issue"), patch.object(
			stock_issue_service, "validate_stock_for_issue"
		):
			with self.assertRaises(InsufficientStockForIssueError):
				self._complete(pl)

		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": pl}), 0)
		self.assertEqual(_actual(item, self.wh_liq), 3)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 0)

	# =====================================================================
	# Partial pick + remainder
	# =====================================================================

	def test_partial_then_remainder_issue_only_what_was_picked(self):
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 10))
		self._pick(pl, {item: 6})

		first = self._complete(pl)
		entry = frappe.get_doc("Stock Entry", first["stock_entry"])
		self.assertEqual([flt(d.qty) for d in entry.items], [6])
		self.assertEqual(_actual(item, self.wh_liq), 14)

		# The issued 6 still cover the order line: the remainder is 4, not 10.
		remainder = create_pick_list_for_remaining_demand(so, [so.items[0].name])
		self.world.track_existing("Pick List", remainder.name)
		self.assertEqual(sum(flt(r.stock_qty) for r in remainder.locations), 4)
		self.assertIsNone(create_pick_list_for_remaining_demand(so, [so.items[0].name]))

		self._pick(remainder.name)
		second = self._complete(remainder.name)
		self.assertEqual([flt(d.qty) for d in frappe.get_doc("Stock Entry", second["stock_entry"]).items], [4])
		self.assertEqual(_actual(item, self.wh_liq), 10)
		self.assertEqual(analyzer._qty_committed_by_open_pick_lists(item, self.wh_liq), 0)
		with fx.as_user(self.bodega_user):
			row = next(r for r in bodega.get_inventory() if r["item_code"] == item)
		self.assertEqual((row["reserved_qty"], row["available_qty"]), (0, 10))

	# =====================================================================
	# Multi-warehouse + GL resolved by ERPNext
	# =====================================================================

	def test_multiwarehouse_order_issues_each_line_from_its_own_warehouse(self):
		a = self._item(self.wh_liq, stock=20)
		b = self._item(self.wh_var, stock=20)
		_so, pl = self._order((a, self.wh_liq, 5), (b, self.wh_var, 3))
		self._pick(pl)

		result = self._complete(pl)

		self.assertEqual(len(_stock_issues(pl)), 1)
		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual(
			sorted((d.item_code, d.s_warehouse, flt(d.qty)) for d in entry.items),
			sorted([(a, self.wh_liq, 5), (b, self.wh_var, 3)]),
		)
		self.assertEqual((_actual(a, self.wh_liq), _actual(b, self.wh_var)), (15, 17))
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": entry.name, "is_cancelled": 0},
			fields=["item_code", "warehouse", "actual_qty"],
		)
		self.assertEqual(
			sorted((r.item_code, r.warehouse, flt(r.actual_qty)) for r in sle),
			sorted([(a, self.wh_liq, -5), (b, self.wh_var, -3)]),
		)
		self._assert_native_gl(entry, {self.wh_liq: 5 * RATE, self.wh_var: 3 * RATE})

	def test_gl_accounts_are_resolved_natively(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		entry = frappe.get_doc("Stock Entry", self._complete(pl)["stock_entry"])

		stock_adjustment_account = frappe.get_cached_value("Company", fx.COMPANY, "stock_adjustment_account")
		self.assertTrue(stock_adjustment_account)
		# No Item/Item Group default overrides it: ERPNext fell back to the
		# company's own Stock Adjustment account -- nothing set by this app.
		self.assertFalse(frappe.db.get_value("Item Default", {"parent": item}, "expense_account"))
		self.assertEqual({d.expense_account for d in entry.items}, {stock_adjustment_account})
		self._assert_native_gl(entry, {self.wh_liq: 4 * RATE})

	def _assert_native_gl(self, entry, value_by_warehouse):
		stock_adjustment_account = frappe.get_cached_value("Company", fx.COMPANY, "stock_adjustment_account")
		warehouse_accounts = get_warehouse_account_map(fx.COMPANY)
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Stock Entry", "voucher_no": entry.name, "is_cancelled": 0},
			fields=["account", "debit", "credit", "company"],
		)
		self.assertEqual({g.company for g in gl}, {fx.COMPANY})
		debit = _sum_by_account(gl, "debit")
		credit = _sum_by_account(gl, "credit")
		total = sum(value_by_warehouse.values())
		self.assertEqual(debit, {stock_adjustment_account: total})
		expected_credit = {}
		for warehouse, value in value_by_warehouse.items():
			account = warehouse_accounts[warehouse].account
			expected_credit[account] = expected_credit.get(account, 0) + value
		self.assertEqual(credit, expected_credit)

	# =====================================================================
	# Facturación never moves stock
	# =====================================================================

	def test_invoicing_does_not_move_stock(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		self._complete(pl)
		self.assertEqual(_actual(item, self.wh_liq), 16)
		counts = _ledger_counts(item)

		with fx.as_user(self.facturacion_user):
			for it in facturacion.get_invoicing_detail(pl)["items"]:
				facturacion.set_invoicing_item_checked(pl, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl, "integrandoMAS")

		self.assertEqual(frappe.db.get_value("Pick List", pl, "fg_invoicing_status"), "Facturado")
		self.assertEqual(_actual(item, self.wh_liq), 16)
		self.assertEqual(_ledger_counts(item), counts)

	def test_legacy_stock_invoice_is_refused_after_the_issue(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		self._complete(pl)
		invoices = frappe.db.count("Sales Invoice")

		with self.assertRaises(frappe.ValidationError) as ctx:
			facturacion.generate_invoice(pl)

		self.assertIn("ya se descontó", str(ctx.exception))
		self.assertEqual(frappe.db.count("Sales Invoice"), invoices)
		self.assertEqual(_actual(item, self.wh_liq), 16)

	# =====================================================================
	# Company isolation / permissions
	# =====================================================================

	def test_issue_belongs_to_the_pick_list_company_and_bodega_gets_no_stock_entry_grant(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 2))
		self._pick(pl)
		entry = frappe.get_doc("Stock Entry", self._complete(pl)["stock_entry"])

		self.assertEqual(entry.company, frappe.db.get_value("Pick List", pl, "company"))
		self.assertEqual(
			set(frappe.get_all("Stock Ledger Entry", filters={"voucher_no": entry.name}, pluck="company")), {fx.COMPANY}
		)
		# System action: Bodega itself still cannot create/submit a Stock Entry.
		self.assertFalse(frappe.has_permission("Stock Entry", "create", user=self.bodega_user))
		self.assertFalse(frappe.has_permission("Stock Entry", "submit", user=self.bodega_user))
		other_company = frappe.db.get_value("Company", {"name": ["!=", fx.COMPANY]}, "name")
		if other_company:
			audit = audit_pick_lists_without_stock_issue(from_date=nowdate(), company=other_company)
			self.assertNotIn(pl, [p["pick_list"] for p in audit["pick_lists"]])

	# =====================================================================
	# Historical audit / dry-run reconciliation
	# =====================================================================

	def _legacy_completed_pick_list(self, item, qty):
		"""A Pick List completed the way it was before INVENTARIO-OUT-01: a
		plain submit, no Material Issue."""
		_so, pl = self._order((item, self.wh_liq, qty))
		self._pick(pl)
		with stock_issue_service.controlled_pick_list_submit(pl):
			frappe.get_doc("Pick List", pl).submit()
		return pl

	def test_audit_lists_only_completed_pick_lists_without_issue(self):
		item = self._item(self.wh_liq, stock=20)
		legacy = self._legacy_completed_pick_list(item, 4)
		_so, issued = self._order((item, self.wh_liq, 3))
		self._pick(issued)
		self._complete(issued)

		audit = audit_pick_lists_without_stock_issue(from_date=nowdate())
		by_name = {p["pick_list"]: p for p in audit["pick_lists"]}

		self.assertIn(legacy, by_name)
		self.assertNotIn(issued, by_name)
		entry = by_name[legacy]
		self.assertIsNone(entry["material_issue"])
		self.assertEqual(entry["stock_movements"], [])
		self.assertFalse(entry["facturado"])
		self.assertEqual(
			[(r["item_code"], r["warehouse"], r["picked_qty"], r["pending_qty"]) for r in entry["rows"]],
			[(item, self.wh_liq, 4, 4)],
		)
		self.assertEqual(audit["totals"]["by_item"][item], 4)
		self.assertEqual(
			[l["qty"] for l in audit["totals"]["by_item_warehouse"] if l["item_code"] == item], [4]
		)
		# Before the cut-off date nothing is reported.
		later = audit_pick_lists_without_stock_issue(from_date=add_days(nowdate(), 1))
		self.assertNotIn(legacy, [p["pick_list"] for p in later["pick_lists"]])

	def test_dry_run_proposes_the_issue_and_writes_nothing(self):
		item = self._item(self.wh_liq, stock=20)
		legacy = self._legacy_completed_pick_list(item, 4)
		counts = _ledger_counts(item)
		modified = frappe.db.get_value("Pick List", legacy, "modified")

		result = reconcile_historical_stock_issues(from_date=nowdate(), dry_run=True)

		self.assertTrue(result["dry_run"])
		proposal = next(p for p in result["proposed_stock_entries"] if p["pick_list"] == legacy)
		self.assertEqual(proposal["stock_entry"]["purpose"], "Material Issue")
		self.assertEqual(
			[(i["item_code"], i["s_warehouse"], i["qty"]) for i in proposal["stock_entry"]["items"]],
			[(item, self.wh_liq, 4)],
		)
		check = next(c for c in result["stock_check"] if c["item_code"] == item)
		self.assertEqual((check["actual_qty"], check["qty_after"], check["sufficient"]), (20, 16, True))
		self.assertEqual(_ledger_counts(item), counts)
		self.assertEqual(frappe.db.get_value("Pick List", legacy, "modified"), modified)
		self.assertIsNone(get_stock_issue(legacy))

	def test_real_reconciliation_is_not_enabled(self):
		for value in (False, 0, "False", "0"):
			with self.assertRaises(frappe.ValidationError):
				reconcile_historical_stock_issues(from_date=nowdate(), dry_run=value)
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				reconcile_historical_stock_issues(from_date=nowdate())

	# =====================================================================
	# Direct submit bypass -- only COMPLETAR PEDIDO submits a Delivery Pick List
	# =====================================================================

	def _picked_draft(self, qty=2):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, qty))
		self._pick(pl)
		return item, pl

	def _assert_direct_submit_refused(self, pl, item, user=None):
		def submit():
			frappe.get_doc("Pick List", pl).submit()

		if user:
			with fx.as_user(user):
				self.assertRaises(DirectPickListSubmitError, submit)
		else:
			self.assertRaises(DirectPickListSubmitError, submit)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertIsNone(get_stock_issue(pl))
		self.assertEqual(_actual(item, self.wh_liq), 20)

	def test_direct_submit_refused_for_bodega(self):
		item, pl = self._picked_draft()
		self.assertTrue(frappe.has_permission("Pick List", "submit", user=self.bodega_user))
		self._assert_direct_submit_refused(pl, item, self.bodega_user)

	def test_direct_submit_refused_for_jefe_de_bodega_with_stock_manager(self):
		"""Production's jefebodega@: Jefe de Bodega + Bodega + Stock Manager.
		Its submit permission comes from Bodega (Custom DocPerm replaces the
		native Stock Manager rows for Pick List)."""
		jefe = self.world.user(
			f"fgout-jefe-{frappe.generate_hash(length=4).lower()}@example.com",
			["Jefe de Bodega", "Bodega", "Stock Manager"],
		)
		self.world.warehouse_user_permission(jefe, self.wh_liq)
		item, pl = self._picked_draft()
		self.assertTrue(frappe.has_permission("Pick List", "submit", user=jefe))
		self._assert_direct_submit_refused(pl, item, jefe)

	def test_direct_submit_refused_for_stock_manager_and_facturacion(self):
		stock_manager = self.world.user(
			f"fgout-sm-{frappe.generate_hash(length=4).lower()}@example.com", ["Stock Manager"]
		)
		item, pl = self._picked_draft()
		with fx.as_user(stock_manager):
			with self.assertRaises((frappe.PermissionError, DirectPickListSubmitError)):
				frappe.get_doc("Pick List", pl).submit()
		self._assert_direct_submit_refused(pl, item, self.facturacion_user)

	def test_direct_submit_refused_for_administrator_and_system_manager(self):
		item, pl = self._picked_draft()
		self._assert_direct_submit_refused(pl, item)  # Administrator
		system_manager = self.world.user(
			f"fgout-sysm-{frappe.generate_hash(length=4).lower()}@example.com", ["System Manager", "Stock Manager"]
		)
		with fx.as_user(system_manager):
			with self.assertRaises((frappe.PermissionError, DirectPickListSubmitError)):
				frappe.get_doc("Pick List", pl).submit()
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)

	def test_direct_submit_refused_through_the_api(self):
		from frappe.client import submit as client_submit

		item, pl = self._picked_draft()
		doc = frappe.get_doc("Pick List", pl).as_dict()
		doc["flags"] = {"fg_controlled_pick_list_submit": pl}  # client input never opens the door
		with fx.as_user(self.bodega_user):
			with self.assertRaises(DirectPickListSubmitError):
				client_submit(doc)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(_actual(item, self.wh_liq), 20)

	def test_controlled_submit_is_scoped_to_one_pick_list_and_closes(self):
		item, pl = self._picked_draft()
		other_item, other = self._picked_draft()
		with stock_issue_service.controlled_pick_list_submit(other):
			self._assert_direct_submit_refused(pl, item)
		self.assertIsNone(frappe.flags.get(stock_issue_service.CONTROLLED_SUBMIT_FLAG))
		# Non-Delivery purposes keep the native flow.
		self.assertIsNone(
			stock_issue_service.guard_pick_list_submit(frappe._dict(name=pl, purpose="Material Transfer for Manufacture"))
		)

	def test_completar_pedido_submits_once_and_issues_once(self):
		item, pl = self._picked_draft(qty=3)
		first = self._complete(pl)
		second = self._complete(pl)

		self.assertFalse(first["already_completed"])
		self.assertTrue(second["already_completed"])
		self.assertEqual(first["stock_entry"], second["stock_entry"])
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 1)
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 17)
		self.assertEqual(frappe.get_all("Pick List Item", filters={"parent": pl}, pluck="transferred_qty"), [3])
		self.assertIsNone(frappe.flags.get(stock_issue_service.CONTROLLED_SUBMIT_FLAG))


	# =====================================================================
	# Forward flow hardening -- structured shortage, pending_qty, links,
	# valuation independence, rollback after SLE, Stock Entry guard
	# =====================================================================

	def test_insufficient_stock_returns_structured_shortage_and_writes_nothing(self):
		enough = self._item(self.wh_liq, stock=20)
		short = self._item(self.wh_var, stock=2)
		so, pl = self._order((enough, self.wh_liq, 3), (short, self.wh_var, 5))
		self._pick(pl)
		frappe.local.response.pop("fg_stock_shortages", None)

		with self.assertRaises(InsufficientStockForIssueError) as ctx:
			self._complete(pl)

		expected = [
			{
				"item_code": short,
				"warehouse": self.wh_var,
				"required_qty": 5,
				"available_qty": 2,
				"shortage_qty": 3,
			}
		]
		self.assertEqual(ctx.exception.shortages, expected)
		self.assertEqual(frappe.local.response.pop("fg_stock_shortages"), expected)
		# All or nothing: the line that had stock was not issued either.
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": pl}), 0)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual((_actual(enough, self.wh_liq), _actual(short, self.wh_var)), (20, 2))
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 0)

	def test_pending_qty_is_picked_minus_delivered_minus_transferred_never_negative(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		doc = frappe.get_doc("Pick List", pl)
		row = doc.locations[0]

		for picked, delivered, transferred, expected in (
			(4, 0, 0, [4]),
			(4, 1, 1, [2]),
			(4, 0, 4, []),  # fully issued
			(4, 4, 0, []),  # fully delivered (legacy stock invoice)
			(4, 2, 3, []),  # over-covered: negative -> nothing, never a negative qty
			(0, 0, 0, []),
		):
			row.picked_qty, row.delivered_qty, row.transferred_qty = picked, delivered, transferred
			self.assertEqual([qty for _r, qty in pending_issue_rows(doc)], expected, (picked, delivered, transferred))

	def test_pending_zero_line_is_never_issued_again(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		first = self._complete(pl)
		doc = frappe.get_doc("Pick List", pl)
		counts = _ledger_counts(item)

		self.assertEqual(pending_issue_rows(doc), [])
		self.assertIsNone(stock_issue_service.create_stock_issue(doc))
		again = self._complete(pl)

		self.assertTrue(again["already_completed"])
		self.assertEqual(again["stock_entry"], first["stock_entry"])
		self.assertEqual(_ledger_counts(item), counts)
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_multiline_issue_is_one_entry_linked_to_pick_list_and_every_pick_list_item(self):
		a = self._item(self.wh_liq, stock=20)
		b = self._item(self.wh_liq, stock=20)
		c = self._item(self.wh_var, stock=20)
		_so, pl = self._order((a, self.wh_liq, 5), (b, self.wh_liq, 2), (c, self.wh_var, 3))
		self._pick(pl, {b: 1})  # partial line: issues 1, not 2

		result = self._complete(pl)

		self.assertEqual(len(_stock_issues(pl)), 1)
		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual((entry.purpose, entry.pick_list, entry.docstatus), ("Material Issue", pl, 1))
		# Current date/time, never backdated.
		self.assertEqual((entry.set_posting_time, str(entry.posting_date)), (0, nowdate()))
		rows = {r.name: r for r in frappe.get_doc("Pick List", pl).locations}
		self.assertEqual(len(entry.items), 3)
		self.assertEqual({d.pick_list_item for d in entry.items}, set(rows))
		for d in entry.items:
			row = rows[d.pick_list_item]
			self.assertEqual((d.item_code, d.s_warehouse, d.t_warehouse), (row.item_code, row.warehouse, None))
			self.assertEqual(flt(d.qty), flt(row.picked_qty))
			self.assertEqual(flt(row.transferred_qty), flt(row.picked_qty))
		self.assertEqual(
			sorted((d.item_code, flt(d.qty)) for d in entry.items), sorted([(a, 5), (b, 1), (c, 3)])
		)

	def test_valuation_rate_zero_or_one_never_blocks_the_issue(self):
		for rate in (0, 1):
			with self.subTest(rate=rate):
				item = self._item(self.wh_liq)
				self.world.stock_up_real(item, self.wh_liq, 10, rate=rate)
				_so, pl = self._order((item, self.wh_liq, 4))
				self._pick(pl)

				result = self._complete(pl)

				entry = frappe.get_doc("Stock Entry", result["stock_entry"])
				self.assertEqual(entry.docstatus, 1)
				self.assertEqual([flt(d.qty) for d in entry.items], [4])
				self.assertEqual(_actual(item, self.wh_liq), 6)
				self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 1)

	def test_failure_after_stock_ledger_is_written_rolls_back_everything(self):
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		counts = _ledger_counts(item)
		seen = {}

		def fail_after_sle(entry):
			# on_submit already posted the SLE, moved Bin and wrote transferred_qty.
			seen["sle"] = frappe.db.count("Stock Ledger Entry", {"voucher_no": entry.name, "is_cancelled": 0})
			seen["actual"] = _actual(item, self.wh_liq)
			raise frappe.ValidationError("GL failure simulated")

		with patch.object(StockEntry, "make_gl_entries", autospec=True, side_effect=fail_after_sle):
			with self.assertRaises(frappe.ValidationError):
				self._complete(pl)

		self.assertEqual((seen["sle"], seen["actual"]), (1, 16))  # it really failed mid-posting
		self.assertEqual(_ledger_counts(item), counts)
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": pl}), 0)
		self.assertEqual(_actual(item, self.wh_liq), 20)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(frappe.get_all("Pick List Item", filters={"parent": pl}, pluck="transferred_qty"), [0])
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 0)

		# Nothing half-applied: the same Pick List completes normally afterwards.
		result = self._complete(pl)
		self.assertFalse(result["already_completed"])
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_submitted_pick_list_without_issue_is_reported_incomplete(self):
		item = self._item(self.wh_liq, stock=20)
		legacy = self._legacy_completed_pick_list(item, 4)
		frappe.local.response.pop("fg_incomplete_stock_issue", None)

		with self.assertRaises(IncompleteStockIssueError) as ctx:
			self._complete(legacy)

		detail = ctx.exception.detail
		self.assertEqual(frappe.local.response.pop("fg_incomplete_stock_issue"), detail)
		self.assertEqual((detail["pick_list"], detail["stock_entries"]), (legacy, []))
		self.assertEqual(
			[(r["item_code"], r["warehouse"], r["pending_qty"]) for r in detail["pending_rows"]],
			[(item, self.wh_liq, 4)],
		)
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": legacy}), 0)
		self.assertEqual(_actual(item, self.wh_liq), 20)

	def _manual_issue(self, pl, row, qty):
		entry = frappe.new_doc("Stock Entry")
		entry.purpose = "Material Issue"
		entry.company = fx.COMPANY
		entry.pick_list = pl
		entry.set_stock_entry_type()
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
		return entry

	def test_stock_entry_guard_refuses_a_second_issue_for_the_same_pick_list(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		self._complete(pl)
		row = frappe.get_doc("Pick List", pl).locations[0]

		with self.assertRaises(PickListStockIssueOverflowError):
			self._manual_issue(pl, row, 1).insert()  # Administrator, from Desk/API

		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 16)

	def test_stock_entry_guard_refuses_unlinked_foreign_or_draft_lines(self):
		item = self._item(self.wh_liq, stock=20)
		_so, done = self._order((item, self.wh_liq, 2))
		self._pick(done)
		self._complete(done)
		_so, draft = self._order((item, self.wh_liq, 3))
		self._pick(draft)
		draft_row = frappe.get_doc("Pick List", draft).locations[0]

		unlinked = self._manual_issue(done, draft_row, 1)
		unlinked.items[0].pick_list_item = None
		foreign = self._manual_issue(done, draft_row, 1)  # row of another Pick List
		on_draft = self._manual_issue(draft, draft_row, 1)  # Pick List not finished
		for entry in (unlinked, foreign, on_draft):
			with self.assertRaises(PickListStockIssueOverflowError):
				entry.insert()
		self.assertEqual(_actual(item, self.wh_liq), 18)

	def test_stock_entry_guard_allows_cancel_and_amend(self):
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		original = frappe.get_doc("Stock Entry", self._complete(pl)["stock_entry"])

		original.cancel()
		self.assertEqual(_actual(item, self.wh_liq), 20)
		self.assertEqual(frappe.get_all("Pick List Item", filters={"parent": pl}, pluck="transferred_qty"), [0])
		# Cancelled issue: COMPLETAR PEDIDO reports it, never re-issues on its own.
		with self.assertRaises(IncompleteStockIssueError):
			self._complete(pl)
		self.assertEqual(_actual(item, self.wh_liq), 20)

		# Same as Desk's Amend (copy_doc(from_amend): no_copy fields such as
		# pick_list_item are kept), back to draft.
		amended = frappe.copy_doc(original)
		amended.docstatus = 0
		amended.amended_from = original.name
		self.assertEqual([d.pick_list_item for d in amended.items], [d.pick_list_item for d in original.items])
		amended.insert()
		amended.submit()

		self.assertEqual(_actual(item, self.wh_liq), 16)
		self.assertEqual(frappe.get_all("Pick List Item", filters={"parent": pl}, pluck="transferred_qty"), [4])
		result = self._complete(pl)
		self.assertTrue(result["already_completed"])
		self.assertEqual(result["stock_entry"], amended.name)
		self.assertEqual(_actual(item, self.wh_liq), 16)


def _sum_by_account(rows, field):
	totals = {}
	for row in rows:
		if flt(row[field]):
			totals[row.account] = totals.get(row.account, 0) + flt(row[field])
	return totals


class TestRecorridoDoesNotMoveStock(IntegrationTestCase):
	"""Completar 20 -> 16, Facturar 16, Iniciar recorrido 16, Entregar 16 --
	the real Bodega -> Facturación -> Recorrido chain, borrowing the Recorridos
	suites' own helpers."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		tag = frappe.generate_hash(length=4).upper()
		cls.wh = cls.world.warehouse(f"FGOUT Ruta {tag}")
		cls.item = cls.world.item(f"FGOUT-RUTA-{tag}")
		cls.item_codes = [cls.item.name]
		cls.addClassCleanup(_StockIssueMixin._purge_item_prices.__func__, cls)
		cls.customer = cls.world.customer(f"FGOUT Ruta Cliente {tag}")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 20, rate=RATE)
		cls.bodega_user = cls.world.user(f"fgout-ruta-bodega-{tag.lower()}@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgout-ruta-facturacion-{tag.lower()}@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user(f"fgout-ruta-recorrido-{tag.lower()}@example.com", ["Recorrido"])
		cls._seq = 0
		cls._evidence_file_names = []
		cls.addClassCleanup(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__, cls)

	_facturado_pick_list = recorridos_base.TestRecorridosApi._facturado_pick_list
	_set_customer_primary_address = recorridos_base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = recorridos_base.TestRecorridosApi._geocode_customer_address
	_track_route = recorridos_base.TestRecorridosApi._track_route
	_create_route = recorridos_base.TestRecorridosApi._create_route
	_plan_route = recorridos_base.TestRecorridosApi._plan_route
	_driver = recorridos_base.TestRecorridosApi._driver
	_unique = start_base.TestRecorridosStartRoute._unique
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_start = start_base.TestRecorridosStartRoute._start
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files

	def test_route_start_and_delivery_never_move_stock(self):
		customer = self._stop_customer()
		_so, pl = self._facturado_pick_list(qty=4, customer=customer)
		self.assertIsNotNone(get_stock_issue(pl.name))
		self.assertEqual(_actual(self.item.name, self.wh.name), 16)
		counts = _ledger_counts(self.item.name)

		driver = self._driver(self._unique("Conductor")).name
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name], driver=driver)
			self._plan_route(route["name"])
		started = self._start(route["name"])
		self.assertEqual(started["status"], "En Ruta")
		self.assertEqual(_actual(self.item.name, self.wh.name), 16)

		stop_name = started["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop_name, "status"), "Entregado")
		self.assertEqual(_actual(self.item.name, self.wh.name), 16)
		self.assertEqual(_ledger_counts(self.item.name), counts)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "delivery_status"), "Not Delivered")
		self.assertEqual(len(_stock_issues(pl.name)), 1)
