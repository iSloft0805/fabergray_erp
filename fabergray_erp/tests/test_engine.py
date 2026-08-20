# -*- coding: utf-8 -*-
"""Commit 15 -- tests for fabergray_erp.fulfillment.engine.process_sales_order().

Every test drives the real orchestrator end-to-end -- no hand-built Pick
List or Reporte de Faltante, no parallel formula. Assertions check the
returned summary against real documents fetched from the database and
against the already-tested behaviour of Commits 12/13/14.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.engine import process_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestFulfillmentEngine(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg15-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG15 {tag}")
		item = self.world.item(f"FG15-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG15 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _track_result(self, result):
		if result["pick_list"]:
			self.world.track_existing("Pick List", result["pick_list"])
		for name in result["shortages"]["created"]:
			self.world.track_existing("Reporte de Faltante", name)

	def _pick_list(self, name):
		return frappe.get_doc("Pick List", name)

	def _report(self, name):
		return frappe.get_doc("Reporte de Faltante", name)

	# -- Caso: stock completo -> Pick List, sin shortage ----------------------

	def test_full_stock_creates_pick_list_with_no_shortage_report(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		self.assertEqual(result["sales_order"], so.name)
		self.assertIsNotNone(result["pick_list"])
		self.assertEqual(self._pick_list(result["pick_list"]).get("locations")[0].stock_qty, 10.0)
		self.assertEqual(result["shortages"], {"created": [], "updated": [], "resolved": [], "blocked": []})
		self.assertEqual(result["status"], "processed")
		# NOTE: "analysis" is read *after* the Pick List above was created,
		# so analyze_sales_order()'s own qty_available_for_pick for this
		# line is already 0 (claimed by that very Pick List) -- its raw
		# has_shortage/qty_shortage therefore still read True/10, exactly
		# per Commit 12's documented, deliberate design (qty_remaining
		# never accounts for an open Pick List's own claim). "shortages"
		# above -- not "analysis" -- is the authoritative, corrected
		# signal for whether anything is genuinely still missing.
		self.assertTrue(result["analysis"]["has_shortage"])

	# -- Caso: stock parcial -> Pick List parcial + shortage correcto ---------

	def test_partial_stock_creates_partial_pick_list_and_correct_shortage(self):
		wh, item, customer = self._new_world("Partial", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		self.assertEqual(self._pick_list(result["pick_list"]).get("locations")[0].stock_qty, 3.0)
		self.assertEqual(len(result["shortages"]["created"]), 1)
		report = self._report(result["shortages"]["created"][0])
		# Commit 14's own fix applies here: the 3 units already claimed by
		# the Pick List this same run just created are not "still missing"
		# -- only the genuine remainder is.
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.shortage_reason, "Compra pendiente")

	# -- Caso: stock cero, Purchase -> shortage Purchase -----------------------

	def test_zero_stock_purchase_creates_purchase_shortage_no_pick_list(self):
		wh, item, customer = self._new_world("ZeroPurchase", stock_qty=None, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		self.assertIsNone(result["pick_list"])
		self.assertEqual(len(result["shortages"]["created"]), 1)
		report = self._report(result["shortages"]["created"][0])
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.shortage_reason, "Compra pendiente")

	# -- Caso: stock cero, Manufacture -> shortage Manufacture -------------------

	def test_zero_stock_manufacture_creates_manufacture_shortage(self):
		wh, item, customer = self._new_world(
			"ZeroManufacture", stock_qty=None, default_material_request_type="Manufacture"
		)
		raw = self.world.item("FG15-ZEROMANUFACTURE-RAW")
		self.world.bom_for(item.name, raw.name)
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		self.assertIsNone(result["pick_list"])
		report = self._report(result["shortages"]["created"][0])
		self.assertEqual(report.shortage_reason, "Producción pendiente")
		self.assertEqual(result["shortages"]["blocked"], [])

	# -- Caso: Manufacture sin BOM -> blocked -----------------------------------

	def test_manufacture_without_bom_is_blocked(self):
		wh, item, customer = self._new_world("NoBom", stock_qty=None, default_material_request_type="Manufacture")
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		self.assertIsNone(result["pick_list"])
		self.assertEqual(result["shortages"]["blocked"], result["shortages"]["created"])
		report = self._report(result["shortages"]["created"][0])
		self.assertEqual(report.shortage_reason, "Configuración incompleta")
		self.assertTrue(result["analysis"]["blocked"])

	# -- Caso: Sales Order mixta -------------------------------------------------

	def test_mixed_sales_order_creates_pick_list_and_multiple_shortage_routes(self):
		wh = self.world.warehouse("FG15 Mixed")
		customer = self.world.customer("FG15 Mixed Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		item_a = self.world.item("FG15-MIXED-A", default_material_request_type="Purchase")
		item_b = self.world.item("FG15-MIXED-B", default_material_request_type="Purchase")
		item_c = self.world.item("FG15-MIXED-C", default_material_request_type="Manufacture")
		raw_c = self.world.item("FG15-MIXED-C-RAW")
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

		result = process_sales_order(so.name)
		self._track_result(result)

		pl = self._pick_list(result["pick_list"])
		pl_by_item = {row.item_code: row for row in pl.get("locations")}
		self.assertEqual(pl_by_item[item_a.name].stock_qty, 10.0)
		self.assertEqual(pl_by_item[item_b.name].stock_qty, 3.0)
		self.assertNotIn(item_c.name, pl_by_item)

		self.assertEqual(len(result["shortages"]["created"]), 2)
		reports_by_item = {r.item_code: r for r in (self._report(n) for n in result["shortages"]["created"])}
		self.assertEqual(reports_by_item[item_b.name].shortage_reason, "Compra pendiente")
		self.assertEqual(reports_by_item[item_b.name].qty_faltante, 5.0)
		self.assertEqual(reports_by_item[item_c.name].shortage_reason, "Producción pendiente")

	# -- Caso: ejecutar dos veces -> resultado idempotente ------------------------

	def test_running_twice_is_idempotent(self):
		wh, item, customer = self._new_world("Twice", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		result_1 = process_sales_order(so.name)
		self._track_result(result_1)
		result_2 = process_sales_order(so.name)

		# Matches Commit 13's own idempotency contract: a second run finds
		# nothing new to claim (everything is already in result_1's Pick
		# List), so create_pick_list_for_available_stock() returns None --
		# idempotent means "creates nothing new", not "returns the same
		# name again".
		self.assertIsNone(result_2["pick_list"])
		self.assertEqual(result_2["shortages"]["created"], [])
		self.assertEqual(result_2["shortages"]["updated"], [])  # nothing changed between runs
		self.assertEqual(result_2["shortages"]["resolved"], [])
		distinct_pick_lists = frappe.db.sql(
			"""select count(distinct parent) from `tabPick List Item`
			   where sales_order = %s and docstatus != 2""",
			so.name,
		)[0][0]
		self.assertEqual(distinct_pick_lists, 1)  # no second Pick List across the two runs
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 1)

	# -- Caso: llega stock después -> segunda corrida reduce/resuelve shortage ---

	def test_new_stock_arriving_lets_a_second_run_resolve_the_shortage(self):
		wh, item, customer = self._new_world("Arrives", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		result_1 = process_sales_order(so.name)
		self._track_result(result_1)
		report_name = result_1["shortages"]["created"][0]
		self.assertEqual(self._report(report_name).qty_faltante, 7.0)

		self.world.stock_up_real(item.name, wh.name, 10)  # absolute qty now 10 -- covers the rest
		result_2 = process_sales_order(so.name)
		self._track_result(result_2)

		self.assertIsNotNone(result_2["pick_list"])
		self.assertNotEqual(result_2["pick_list"], result_1["pick_list"])  # second Pick List for the new remainder
		self.assertEqual(self._pick_list(result_2["pick_list"]).get("locations")[0].stock_qty, 7.0)
		self.assertEqual(result_2["shortages"]["resolved"], [report_name])
		self.assertEqual(self._report(report_name).status, "Resuelto")

	# -- Caso: Pick List ya alistado parcialmente -> no duplica cantidad ---------

	def test_existing_partially_picked_pick_list_is_not_duplicated(self):
		wh, item, customer = self._new_world("Picked", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl_existing = self.world.pick_list_for(so, wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_existing.name)
			row = bodega.get_pick_list(pl_existing.name)["rows"][0]
			bodega.set_picked_qty(pl_existing.name, row["row_name"], 6)
			report = bodega.report_shortage(
				pick_list=pl_existing.name,
				row_name=row["row_name"],
				qty_disponible=6,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl_existing.name)  # submits; so_item.picked_qty -> 6

		result = process_sales_order(so.name)
		self._track_result(result)

		# 10 stock, 6 already submitted-picked (undelivered) -- only the
		# real remaining 4 should show up in a new Pick List, nothing
		# duplicated from pl_existing.
		self.assertIsNotNone(result["pick_list"])
		self.assertNotEqual(result["pick_list"], pl_existing.name)
		self.assertEqual(self._pick_list(result["pick_list"]).get("locations")[0].stock_qty, 4.0)
		self.assertEqual(result["shortages"]["created"], [])  # fully covered, no shortage

	# -- Caso: reporte de Bodega existente -> no se modifica ---------------------

	def test_existing_bodega_report_is_not_modified(self):
		wh, item, customer = self._new_world("BodegaSafe", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		bodega_report = self.world.shortage_report(
			item_code=item.name,
			warehouse=wh.name,
			sales_order=so.name,
			qty_solicitada=8,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
		)

		result = process_sales_order(so.name)
		self._track_result(result)

		bodega_report.reload()
		self.assertEqual(bodega_report.status, "Abierto")
		self.assertFalse(bodega_report.resolution_note)
		self.assertNotIn(bodega_report.name, result["shortages"]["created"])
		self.assertNotIn(bodega_report.name, result["shortages"]["updated"])
		self.assertNotIn(bodega_report.name, result["shortages"]["resolved"])

	# -- Caso: Sales Order no submitted -> rechazada ------------------------------

	def test_non_submitted_sales_order_is_rejected(self):
		wh, item, customer = self._new_world("Draft", stock_qty=10)
		draft_so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer.name,
				"company": fx.COMPANY,
				"transaction_date": frappe.utils.nowdate(),
				"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
				"set_warehouse": wh.name,
				"items": [
					{
						"item_code": item.name,
						"warehouse": wh.name,
						"qty": 10,
						"rate": 100,
						"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
					}
				],
			}
		)
		draft_so.insert()
		self.world.track_existing("Sales Order", draft_so.name)

		with self.assertRaises(frappe.ValidationError):
			process_sales_order(draft_so.name)

		# rejected before any write -- nothing left behind for this Sales Order
		self.assertEqual(
			frappe.db.sql(
				"""select count(*) from `tabPick List Item` where sales_order = %s""", draft_so.name
			)[0][0],
			0,
		)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": draft_so.name}), 0)

	# -- Caso: integración con get_queue() ---------------------------------------

	def test_created_pick_list_appears_in_get_queue(self):
		wh, item, customer = self._new_world("Queue", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		result = process_sales_order(so.name)
		self._track_result(result)

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		self.assertIn(result["pick_list"], [p["name"] for p in queue["pendientes"]])
