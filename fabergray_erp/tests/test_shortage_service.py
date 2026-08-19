# -*- coding: utf-8 -*-
"""Commit 14 -- tests for
fabergray_erp.fulfillment.shortage_service.sync_shortage_reports_for_sales_order().

Every test drives the real service end-to-end: no hand-built Reporte de
Faltante, no parallel shortage calculation -- assertions check what the
service actually produced against analyze_sales_order() (Commit 12) and
against real Reporte de Faltante documents fetched from the database.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_available_stock
from fabergray_erp.fulfillment.shortage_service import sync_shortage_reports_for_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestShortageService(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG14 {tag}")
		item = self.world.item(f"FG14-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG14 {tag} Customer")
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _report(self, name):
		return frappe.get_doc("Reporte de Faltante", name)

	def _track_all(self, names):
		for name in names:
			self.world.track_existing("Reporte de Faltante", name)

	# -- Caso: Purchase shortage -------------------------------------------

	def test_purchase_shortage_creates_correct_automatic_report(self):
		wh, item, customer = self._new_world("Purchase", stock_qty=3, default_material_request_type="Purchase")
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 1)
		self.assertEqual(summary["updated"], [])
		self.assertEqual(summary["resolved"], [])
		self.assertEqual(summary["blocked"], [])

		report = self._report(summary["created"][0])
		self.assertEqual(report.item_code, item.name)
		self.assertEqual(report.warehouse, wh.name)
		self.assertEqual(report.sales_order, so.name)
		self.assertEqual(report.sales_order_item, so.items[0].name)
		self.assertEqual(report.qty_solicitada, 8.0)
		self.assertEqual(report.qty_disponible, 3.0)
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.detected_by, "Fulfillment Engine")
		self.assertEqual(report.shortage_reason, "Compra pendiente")
		self.assertEqual(report.status, "Abierto")

	# -- Caso: Manufacture shortage -------------------------------------------

	def test_manufacture_shortage_creates_correct_automatic_report(self):
		wh, item, customer = self._new_world("Manufacture", stock_qty=None, default_material_request_type="Manufacture")
		raw = self.world.item("FG14-MANUFACTURE-RAW")
		self.world.bom_for(item.name, raw.name)
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 1)
		self.assertEqual(summary["blocked"], [])
		report = self._report(summary["created"][0])
		self.assertEqual(report.shortage_reason, "Producción pendiente")
		self.assertEqual(report.qty_faltante, 5.0)

	# -- Caso: Manufacture sin BOM -> Blocked -----------------------------------

	def test_manufacture_without_bom_is_blocked_with_correct_report(self):
		wh, item, customer = self._new_world("NoBom", stock_qty=None, default_material_request_type="Manufacture")
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 1)
		self.assertEqual(summary["blocked"], summary["created"])
		report = self._report(summary["created"][0])
		self.assertEqual(report.shortage_reason, "Configuración incompleta")

	# -- Caso: stock completo -> no crea reporte ---------------------------------

	def test_full_stock_creates_no_report(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary = sync_shortage_reports_for_sales_order(so.name)

		self.assertEqual(summary, {"created": [], "updated": [], "resolved": [], "blocked": []})
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 0)

	# -- Caso: ejecutar dos veces -> no duplica -----------------------------------

	def test_running_twice_does_not_duplicate(self):
		wh, item, customer = self._new_world("Twice", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary_1 = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary_1["created"])
		summary_2 = sync_shortage_reports_for_sales_order(so.name)

		self.assertEqual(summary_2["created"], [])
		self.assertEqual(summary_2["updated"], [])  # nothing changed between runs
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 1)

	# -- Caso: faltante baja de 8 -> 3 -> actualiza mismo reporte -----------------

	def test_shortage_dropping_updates_the_same_report_instead_of_creating_another(self):
		wh, item, customer = self._new_world("Drops", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary_1 = sync_shortage_reports_for_sales_order(so.name)
		report_name = summary_1["created"][0]
		self._track_all([report_name])
		self.assertEqual(self._report(report_name).qty_faltante, 8.0)

		self.world.stock_up_real(item.name, wh.name, 7)  # absolute qty now 7 (was 2)
		summary_2 = sync_shortage_reports_for_sales_order(so.name)

		self.assertEqual(summary_2["created"], [])
		self.assertEqual(summary_2["updated"], [report_name])
		updated = self._report(report_name)
		self.assertEqual(updated.qty_disponible, 7.0)
		self.assertEqual(updated.qty_faltante, 3.0)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 1)

	# -- Caso: faltante desaparece -> reporte automático pasa a Resuelto ---------

	def test_shortage_disappearing_resolves_the_automatic_report(self):
		wh, item, customer = self._new_world("Resolves", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary_1 = sync_shortage_reports_for_sales_order(so.name)
		report_name = summary_1["created"][0]
		self._track_all([report_name])

		self.world.stock_up_real(item.name, wh.name, 10)  # now fully covers the order
		summary_2 = sync_shortage_reports_for_sales_order(so.name)

		self.assertEqual(summary_2["resolved"], [report_name])
		self.assertEqual(summary_2["created"], [])
		self.assertEqual(summary_2["updated"], [])
		resolved = self._report(report_name)
		self.assertEqual(resolved.status, "Resuelto")
		self.assertTrue(resolved.resolution_note)

	# -- Caso: reporte de Bodega nunca se toca automáticamente -------------------

	def test_bodega_created_report_is_never_auto_resolved_or_modified(self):
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

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		bodega_report.reload()
		self.assertEqual(bodega_report.status, "Abierto")
		self.assertFalse(bodega_report.resolution_note)
		self.assertEqual(len(summary["created"]), 1)
		self.assertNotEqual(summary["created"][0], bodega_report.name)

	# -- Caso: Sales Order mixta -> reportes independientes por línea ------------

	def test_mixed_sales_order_creates_independent_reports_per_line(self):
		wh = self.world.warehouse("FG14 Mixed")
		customer = self.world.customer("FG14 Mixed Customer")
		item_a = self.world.item("FG14-MIXED-A", default_material_request_type="Purchase")
		item_b = self.world.item("FG14-MIXED-B", default_material_request_type="Purchase")
		item_c = self.world.item("FG14-MIXED-C", default_material_request_type="Manufacture")
		raw_c = self.world.item("FG14-MIXED-C-RAW")
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

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 2)  # A fully covered, B and C short
		reports_by_item = {r.item_code: r for r in (self._report(n) for n in summary["created"])}
		self.assertNotIn(item_a.name, reports_by_item)
		self.assertEqual(reports_by_item[item_b.name].shortage_reason, "Compra pendiente")
		self.assertEqual(reports_by_item[item_c.name].shortage_reason, "Producción pendiente")

	# -- Caso: mismo item en dos líneas -> idempotencia sin colisión -------------

	def test_same_item_repeated_in_two_lines_does_not_collide(self):
		item = self.world.item("FG14-REPEATED", default_material_request_type="Purchase")
		wh_1 = self.world.warehouse("FG14 Repeated 1")
		wh_2 = self.world.warehouse("FG14 Repeated 2")
		customer = self.world.customer("FG14 Repeated Customer")

		self.world.stock_up_real(item.name, wh_1.name, 3)
		self.world.stock_up_real(item.name, wh_2.name, 1)

		so = self.world.multi_item_sales_order(
			customer.name,
			[
				{"item_code": item.name, "warehouse": wh_1.name, "qty": 8, "rate": 100},
				{"item_code": item.name, "warehouse": wh_2.name, "qty": 5, "rate": 100},
			],
		)

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 2)
		reports_by_warehouse = {r.warehouse: r for r in (self._report(n) for n in summary["created"])}
		self.assertEqual(reports_by_warehouse[wh_1.name].qty_faltante, 5.0)
		self.assertEqual(reports_by_warehouse[wh_2.name].qty_faltante, 4.0)

		# run again -- must still be exactly 2 reports total, not 4
		summary_2 = sync_shortage_reports_for_sales_order(so.name)
		self.assertEqual(summary_2["created"], [])
		self.assertEqual(summary_2["updated"], [])
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 2)

	# -- Integración con Commit 13: Pick List + faltante = necesidad real --------

	def test_pick_list_plus_shortage_report_covers_the_real_pending_need(self):
		wh, item, customer = self._new_world("Integration", stock_qty=6)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)
		pl_qty = pl.get("locations")[0].stock_qty

		summary = sync_shortage_reports_for_sales_order(so.name)
		self._track_all(summary["created"])
		report_qty = self._report(summary["created"][0]).qty_faltante

		self.assertEqual(pl_qty, 6.0)
		self.assertEqual(report_qty, 4.0)
		self.assertEqual(pl_qty + report_qty, 10.0)  # exactly the order's real pending need
