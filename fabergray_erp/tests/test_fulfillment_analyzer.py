# -*- coding: utf-8 -*-
"""Commit 12 -- tests for fabergray_erp.fulfillment.analyzer.analyze_sales_order().

Pure read-only analysis: no Reporte de Faltante, no Pick List, no Material
Request/Work Order/Purchase Order created by the analyzer itself in any of
these tests -- where a Pick List is needed (Caso 4, Caso 10) it is created
and driven through the real api.bodega.* functions, exactly like every
other suite in this app, never by the analyzer.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestFulfillmentAnalyzer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg12-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type=None):
		wh = self.world.warehouse(f"FG12 {tag}")
		item = self.world.item(f"FG12-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG12 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _line(self, result, item_code):
		by_item = {line["item_code"]: line for line in result["lines"]}
		return by_item[item_code]

	# -- Caso: stock completo -------------------------------------------------

	def test_full_stock_is_ready_with_zero_shortage(self):
		wh, item, customer = self._new_world("Full", stock_qty=10, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		result = analyze_sales_order(so.name)
		line = self._line(result, item.name)

		self.assertEqual(line["qty_ordered"], 10.0)
		self.assertEqual(line["qty_remaining"], 10.0)
		self.assertEqual(line["qty_available_for_pick"], 10.0)
		self.assertEqual(line["qty_shortage"], 0.0)
		self.assertEqual(line["procurement_route"], "Ready")
		self.assertIsNone(line["bom_no"])
		self.assertIsNone(line["blocking_reason"])
		self.assertFalse(result["has_shortage"])
		self.assertFalse(result["purchase_required"])
		self.assertFalse(result["manufacturing_required"])
		self.assertFalse(result["blocked"])

	# -- Caso: stock parcial ---------------------------------------------------

	def test_partial_stock_computes_exact_shortage(self):
		wh, item, customer = self._new_world("Partial", stock_qty=4, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		line = self._line(analyze_sales_order(so.name), item.name)

		self.assertEqual(line["qty_available_for_pick"], 4.0)
		self.assertEqual(line["qty_shortage"], 6.0)  # max(10 - 4, 0)
		self.assertEqual(line["procurement_route"], "Purchase")

	# -- Caso: stock cero -------------------------------------------------------

	def test_zero_stock_shortage_equals_full_qty(self):
		wh, item, customer = self._new_world("Zero", stock_qty=None, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		line = self._line(analyze_sales_order(so.name), item.name)

		self.assertEqual(line["qty_available_for_pick"], 0.0)
		self.assertEqual(line["qty_shortage"], 10.0)
		self.assertEqual(line["procurement_route"], "Purchase")

	# -- Caso: stock reducido por otro Pick List abierto -----------------------

	def test_availability_excludes_qty_committed_by_another_open_pick_list(self):
		"""Same protection proven live in Commit 11 (Caso 3b), now read
		through the analyzer instead of through create_pick_list() directly:
		Pick List A picks 6 of 10 units and stays a draft (never submitted,
		never reserved) -- the analyzer for a competing Sales Order must see
		only the remaining 4, not the raw Bin.actual_qty=10."""
		wh, item, customer = self._new_world("Competing", stock_qty=10, default_material_request_type="Purchase")

		so_a = self.world.submitted_sales_order(item.name, wh.name, 6, customer.name)
		pl_a = self.world.pick_list_for(so_a, wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_a.name)
			row_a = bodega.get_pick_list(pl_a.name)["rows"][0]
			bodega.set_picked_qty(pl_a.name, row_a["row_name"], 6)

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		line = self._line(analyze_sales_order(so_b.name), item.name)

		self.assertEqual(line["qty_available_for_pick"], 4.0)  # 10 - 6, not 10
		self.assertEqual(line["qty_shortage"], 4.0)  # 8 requested - 4 available

	# -- Caso: Purchase -----------------------------------------------------

	def test_purchase_policy_item_routes_to_purchase_when_short(self):
		wh, item, customer = self._new_world("PurchaseRoute", stock_qty=None, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		line = self._line(analyze_sales_order(so.name), item.name)
		self.assertEqual(line["procurement_route"], "Purchase")
		self.assertIsNone(line["bom_no"])
		self.assertIsNone(line["blocking_reason"])

	# -- Caso: Manufacture con BOM --------------------------------------------

	def test_manufacture_policy_item_with_bom_routes_to_manufacture(self):
		wh, item, customer = self._new_world(
			"ManufactureRoute", stock_qty=None, default_material_request_type="Manufacture"
		)
		raw = self.world.item("FG12-MANUFACTUREROUTE-RAW")
		bom = self.world.bom_for(item.name, raw.name)
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		line = self._line(analyze_sales_order(so.name), item.name)
		self.assertEqual(line["procurement_route"], "Manufacture")
		self.assertEqual(line["bom_no"], bom.name)
		self.assertIsNone(line["blocking_reason"])

	# -- Caso: Manufacture sin BOM ---------------------------------------------

	def test_manufacture_policy_item_without_bom_is_blocked_not_silently_purchase(self):
		wh, item, customer = self._new_world(
			"NoBom", stock_qty=None, default_material_request_type="Manufacture"
		)
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		line = self._line(analyze_sales_order(so.name), item.name)
		self.assertEqual(line["procurement_route"], "Blocked")
		self.assertEqual(line["blocking_reason"], "Missing BOM")
		self.assertIsNone(line["bom_no"])

		result = analyze_sales_order(so.name)
		self.assertTrue(result["blocked"])
		self.assertFalse(result["purchase_required"])  # never silently downgraded

	# -- Caso: Sales Order mixta -----------------------------------------------

	def test_mixed_sales_order_keeps_each_line_independent(self):
		"""The exact shape from the brief: A ready, B short-and-Purchase, C
		short-and-Manufacture, all on the same Sales Order -- never
		collapsed into a single header-level route."""
		wh = self.world.warehouse("FG12 Mixed")
		customer = self.world.customer("FG12 Mixed Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		item_a = self.world.item("FG12-MIXED-A", default_material_request_type="Purchase")
		item_b = self.world.item("FG12-MIXED-B", default_material_request_type="Purchase")
		item_c = self.world.item("FG12-MIXED-C", default_material_request_type="Manufacture")
		raw_c = self.world.item("FG12-MIXED-C-RAW")
		self.world.bom_for(item_c.name, raw_c.name)

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

		result = analyze_sales_order(so.name)
		line_a = self._line(result, item_a.name)
		line_b = self._line(result, item_b.name)
		line_c = self._line(result, item_c.name)

		self.assertEqual(line_a["procurement_route"], "Ready")
		self.assertEqual(line_a["qty_shortage"], 0.0)

		self.assertEqual(line_b["procurement_route"], "Purchase")
		self.assertEqual(line_b["qty_shortage"], 5.0)

		self.assertEqual(line_c["procurement_route"], "Manufacture")
		self.assertEqual(line_c["qty_shortage"], 20.0)

		self.assertTrue(result["has_shortage"])
		self.assertTrue(result["purchase_required"])
		self.assertTrue(result["manufacturing_required"])
		self.assertFalse(result["blocked"])

	# -- Caso: Sales Order parcialmente entregada -------------------------------

	def test_partially_delivered_sales_order_reduces_qty_remaining(self):
		"""Simulates the post-delivery state directly on Sales Order Item
		(db_set) rather than building a full Delivery Note (price
		list/tax-template setup unrelated to what this test verifies) --
		this tests the analyzer's own reading of delivered_qty, not
		ERPNext's delivery mechanics."""
		wh, item, customer = self._new_world(
			"Delivered", stock_qty=10, default_material_request_type="Purchase"
		)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		so.items[0].db_set("delivered_qty", 6)

		line = self._line(analyze_sales_order(so.name), item.name)

		self.assertEqual(line["qty_delivered"], 6.0)
		self.assertEqual(line["qty_remaining"], 4.0)  # 10 - 6, not 10
		self.assertEqual(line["qty_shortage"], 0.0)  # 4 remaining, 10 available -- still Ready

	# -- Caso: Sales Order parcialmente picked ----------------------------------

	def test_partially_picked_sales_order_reflects_real_picked_qty(self):
		"""A real, disclosed partial pick, finished (submitted) through the
		actual Bodega API -- Sales Order Item.picked_qty only updates on
		Pick List submit (Pick List.update_reference_qty()), not while
		picking is still in progress on a draft."""
		wh, item, customer = self._new_world("Picked", stock_qty=6, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		pl = self.world.pick_list_for(so, wh.name)

		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], 6)
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=row["row_name"],
				qty_disponible=6,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl.name)  # submits -> Sales Order Item.picked_qty updates

		so.reload()
		line = self._line(analyze_sales_order(so.name), item.name)

		self.assertEqual(line["qty_picked"], 6.0)
		# Not yet delivered -- qty_remaining is still the full undelivered
		# qty (10), and the 6 physically picked units are no longer sitting
		# free in the Bin for anyone else (still claimed by the now-submitted
		# Pick List until a Delivery Note consumes them) -- qty_shortage
		# reflects that honestly instead of assuming the pick already
		# resolved the order.
		self.assertEqual(line["qty_remaining"], 10.0)
		self.assertEqual(line["qty_available_for_pick"], 0.0)
		self.assertEqual(line["qty_shortage"], 10.0)
