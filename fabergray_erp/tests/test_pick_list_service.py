# -*- coding: utf-8 -*-
"""Commit 13 -- tests for
fabergray_erp.fulfillment.pick_list_service.create_pick_list_for_available_stock().

Every test drives the real function end-to-end: no hand-built Pick List, no
parallel availability calculation -- the assertions check what the
function actually produced against what analyze_sales_order() (Commit 12)
independently reports, and against the existing /app/bodega API
(get_queue/get_pick_list/...), never a second, invented formula.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_available_stock
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestPickListService(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg13-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None):
		wh = self.world.warehouse(f"FG13 {tag}")
		item = self.world.item(f"FG13-{tag.upper()}", default_material_request_type="Purchase")
		customer = self.world.customer(f"FG13 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _row(self, pick_list, item_code):
		for row in pick_list.get("locations"):
			if row.item_code == item_code:
				return row
		return None

	# -- Caso: stock completo -----------------------------------------------

	def test_full_stock_creates_pick_list_for_full_qty(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)

		row = self._row(pl, item.name)
		self.assertIsNotNone(row)
		self.assertEqual(row.sales_order, so.name)
		self.assertEqual(row.sales_order_item, so.items[0].name)
		self.assertEqual(row.warehouse, wh.name)
		self.assertEqual(row.stock_qty, 10.0)

	# -- Caso: stock parcial --------------------------------------------------

	def test_partial_stock_creates_pick_list_only_for_available_qty(self):
		wh, item, customer = self._new_world("Partial", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)

		row = self._row(pl, item.name)
		self.assertEqual(row.stock_qty, 3.0)  # min(8, 3), matches analyzer's qty_to_pick

	# -- Caso: stock cero ------------------------------------------------------

	def test_zero_stock_creates_no_pick_list(self):
		wh, item, customer = self._new_world("Zero", stock_qty=None)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		self.assertIsNone(create_pick_list_for_available_stock(so.name))
		# no residue: nothing was ever inserted for this Sales Order
		self.assertEqual(frappe.db.count("Pick List Item", {"sales_order": so.name}), 0)

	# -- Caso: Sales Order mixta -----------------------------------------------

	def test_mixed_sales_order_only_includes_available_or_partially_available_lines(self):
		"""The exact shape from the brief: A 10/10 -> full row, B 8 ordered/3
		available -> row capped at 3, C 20 ordered/0 available -> absent
		from the Pick List entirely."""
		wh = self.world.warehouse("FG13 Mixed")
		customer = self.world.customer("FG13 Mixed Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		item_a = self.world.item("FG13-MIXED-A")
		item_b = self.world.item("FG13-MIXED-B")
		item_c = self.world.item("FG13-MIXED-C")

		self.world.stock_up_real(item_a.name, wh.name, 10)
		self.world.stock_up_real(item_b.name, wh.name, 3)
		# item_c: 0 stock

		so = self.world.multi_item_sales_order(
			customer.name,
			[
				{"item_code": item_a.name, "warehouse": wh.name, "qty": 10, "rate": 100},
				{"item_code": item_b.name, "warehouse": wh.name, "qty": 8, "rate": 100},
				{"item_code": item_c.name, "warehouse": wh.name, "qty": 20, "rate": 100},
			],
		)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)

		self.assertEqual(self._row(pl, item_a.name).stock_qty, 10.0)
		self.assertEqual(self._row(pl, item_b.name).stock_qty, 3.0)
		self.assertIsNone(self._row(pl, item_c.name))
		self.assertEqual(len(pl.get("locations")), 2)

	# -- Caso: ejecutar dos veces -> no duplica -------------------------------

	def test_running_twice_does_not_duplicate_quantity(self):
		wh, item, customer = self._new_world("Twice", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl_1 = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl_1.name)
		self.assertEqual(self._row(pl_1, item.name).stock_qty, 10.0)

		pl_2 = create_pick_list_for_available_stock(so.name)
		self.assertIsNone(pl_2)  # everything already claimed by pl_1, nothing left to offer

		total_claimed = frappe.db.sql(
			"""select sum(stock_qty) from `tabPick List Item`
			   where sales_order = %s and docstatus != 2""",
			so.name,
		)[0][0]
		self.assertEqual(float(total_claimed), 10.0)

	# -- Caso: Pick List previo abierto -> nueva ejecución toma remanente real -

	def test_existing_open_pick_list_second_run_only_takes_real_remainder(self):
		wh, item, customer = self._new_world("Remainder", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 20, customer.name)

		pl_1 = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl_1.name)
		self.assertEqual(self._row(pl_1, item.name).stock_qty, 10.0)

		# more stock arrives after pl_1 already claimed the first 10 --
		# stock_up_real() sets an absolute balance (Stock Reconciliation),
		# not a delta, so this brings actual_qty to 25, not 10+15.
		self.world.stock_up_real(item.name, wh.name, 25)

		pl_2 = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl_2.name)
		# 25 actual - 10 already claimed by pl_1 = 15 available, order still
		# wants 10 more (20 ordered - 10 already in pl_1) -> exactly 10, not
		# the full 15 available
		self.assertEqual(self._row(pl_2, item.name).stock_qty, 10.0)

	# -- Caso: Pick List previo submitted -> respeta lo ya pickeado -----------

	def test_existing_submitted_pick_list_respects_what_was_already_picked(self):
		wh, item, customer = self._new_world("Submitted", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl_1 = self.world.pick_list_for(so, wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_1.name)
			row = bodega.get_pick_list(pl_1.name)["rows"][0]
			bodega.set_picked_qty(pl_1.name, row["row_name"], 6)
			report = bodega.report_shortage(
				pick_list=pl_1.name,
				row_name=row["row_name"],
				qty_disponible=6,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl_1.name)  # submits, so_item.picked_qty -> 6

		# 4 units never got picked (undisclosed as available at pl_1 time,
		# but still physically in the warehouse and never claimed) -- a
		# second run must offer exactly that real remainder.
		pl_2 = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl_2.name)
		self.assertEqual(self._row(pl_2, item.name).stock_qty, 4.0)

	# -- Caso: pedido parcialmente entregado -----------------------------------

	def test_partially_delivered_sales_order_does_not_recreate_delivered_qty(self):
		wh, item, customer = self._new_world("Delivered", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		so.items[0].db_set("delivered_qty", 6)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)

		# 10 ordered - 6 delivered = 4 remaining; 10 physically available,
		# but only the real remaining need is offered.
		self.assertEqual(self._row(pl, item.name).stock_qty, 4.0)

	# -- Caso: dos Sales Orders compiten por stock -----------------------------

	def test_two_sales_orders_competing_respect_analyzer_atp(self):
		wh, item, customer = self._new_world("Competing", stock_qty=10)

		so_a = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl_a = create_pick_list_for_available_stock(so_a.name)
		self.world.track_existing("Pick List", pl_a.name)
		self.assertEqual(self._row(pl_a, item.name).stock_qty, 8.0)

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		analysis_b = analyze_sales_order(so_b.name)
		self.assertEqual(analysis_b["lines"][0]["qty_available_for_pick"], 2.0)

		pl_b = create_pick_list_for_available_stock(so_b.name)
		self.world.track_existing("Pick List", pl_b.name)
		self.assertEqual(self._row(pl_b, item.name).stock_qty, 2.0)  # matches analyzer's ATP exactly

	# -- Caso: aparece en get_queue() -------------------------------------------

	def test_created_pick_list_appears_in_get_queue(self):
		wh, item, customer = self._new_world("Queue", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

	# -- Concurrencia: ventana de carrera documentada, no cerrada -------------

	def test_concurrency_race_self_corrects_within_one_connection_not_proof_of_true_concurrency_safety(self):
		"""Simulates two processes racing for the same Sales Order the same
		way test_bodega_flow's optimistic-lock test simulates two racing
		saves: build both unsaved Pick Lists from the exact same
		pre-insert state (mirrors two requests arriving almost
		simultaneously, before either has written anything), then let both
		inserts land one after the other.

		Empirical finding (this is *not* what was originally assumed):
		both builds do independently propose the full 10 units, matching
		the over-claim expected from a naive read-then-insert race -- but
		`Pick List.before_save()` (pick_list.py) unconditionally re-runs
		`set_item_locations()` against *live* state right before writing,
		not just once at build time. Because this test uses a single
		database connection/transaction (the only kind `bench run-tests`
		can drive), process 2's own `insert()` necessarily sees process
		1's already-written row and self-corrects to zero -- exactly what
		create_pick_list_for_available_stock() detects and cleans up
		(Commit 13, pick_list_service.py).

		What this does **not** prove: two independent, truly concurrent
		database transactions each get their own MVCC read snapshot: one
		does not have to see the other's insert, committed or not, until
		its own snapshot is refreshed. A real concurrent race across two
		separate connections could still each compute "10 available" and
		both insert successfully -- that window is not exercised by this
		single-connection test and is not closed by this commit. See
		FULFILLMENT_ENGINE_CONTRACT.md, "Commit 13 -- known concurrency
		window", for the full writeup and why no locking was bolted on
		instead: the only native mechanism that would actually close it
		(Stock Reservation Entry's `for_update()`-locked
		`get_available_qty_to_reserve()`) was already ruled out in
		Commits 10/11 for this app's Pick-List-centric workflow.
		"""
		from erpnext.selling.doctype.sales_order.sales_order import create_pick_list as _native_create_pick_list

		wh, item, customer = self._new_world("Race", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		# Both "processes" build their unsaved Pick List before either inserts.
		pl_process_1 = _native_create_pick_list(so.name)
		pl_process_2 = _native_create_pick_list(so.name)
		self.assertEqual(self._row(pl_process_1, item.name).stock_qty, 10.0)
		self.assertEqual(self._row(pl_process_2, item.name).stock_qty, 10.0)  # both see it "available"

		pl_process_1.insert()
		self.world.track_existing("Pick List", pl_process_1.name)

		pl_process_2.insert()  # before_save() re-derives -> self-corrects to empty
		self.assertEqual(pl_process_2.get("locations"), [])
		pl_process_2.delete()  # what create_pick_list_for_available_stock() does automatically

		total_claimed = frappe.db.sql(
			"""select sum(stock_qty) from `tabPick List Item`
			   where sales_order = %s and docstatus != 2""",
			so.name,
		)[0][0]
		self.assertEqual(float(total_claimed), 10.0)  # no over-claim in this same-connection simulation
