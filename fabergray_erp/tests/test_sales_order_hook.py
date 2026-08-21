# -*- coding: utf-8 -*-
"""Commit 16 -- end-to-end tests for the real Sales Order.on_submit ->
process_sales_order() hook (hooks.py doc_events ->
fulfillment/sales_order_hooks.py -> fulfillment/engine.py).

Every test here builds and submits its own Sales Order directly (NOT
through TestWorld.multi_item_sales_order()/submitted_sales_order(), which
Commit 16 deliberately wraps in fx.without_sales_order_hook() so every
other test file in this suite keeps behaving exactly as it did before this
commit) -- specifically to exercise the real, live hook.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.engine import process_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestSalesOrderHook(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg16-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG16 {tag}")
		item = self.world.item(f"FG16-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG16 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _draft_sales_order(self, customer, items):
		"""An unsubmitted Sales Order, built directly (not through
		TestWorld.multi_item_sales_order(), which suppresses the hook) --
		the caller submits it explicitly to exercise the real hook."""
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": items[0]["warehouse"],
				"items": [{**item, "delivery_date": delivery_date} for item in items],
			}
		)
		doc.insert()
		self.world.track_existing("Sales Order", doc.name)
		return doc

	def _submit_via_hook(self, customer, items):
		doc = self._draft_sales_order(customer, items)
		doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		return doc

	def _pick_lists_for(self, sales_order_name):
		return frappe.get_all(
			"Pick List Item", filters={"sales_order": sales_order_name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)

	def _reports_for(self, sales_order_name):
		return frappe.get_all("Reporte de Faltante", filters={"sales_order": sales_order_name}, pluck="name")

	# -- Caso 1+2: stock completo -> Pick List automático + aparece en get_queue() --

	def test_submit_full_stock_automatically_creates_pick_list_visible_in_queue(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(pl.get("locations")[0].stock_qty, 10.0)
		self.assertEqual(self._reports_for(so.name), [])

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

	# -- Caso 3: stock parcial -> Pick List parcial + Reporte de Faltante --------

	def test_submit_partial_stock_creates_partial_pick_list_and_shortage(self):
		wh, item, customer = self._new_world("Partial", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(pl.get("locations")[0].stock_qty, 3.0)

		reports = self._reports_for(so.name)
		self.assertEqual(len(reports), 1)
		report = frappe.get_doc("Reporte de Faltante", reports[0])
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.shortage_reason, "Compra pendiente")
		self.assertEqual(report.detected_by, "Fulfillment Engine")

	# -- Caso 4: stock cero, Purchase -> solo faltante Purchase -------------------

	def test_submit_zero_stock_purchase_creates_only_shortage(self):
		wh, item, customer = self._new_world("ZeroPurchase", stock_qty=None, default_material_request_type="Purchase")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		self.assertEqual(self._pick_lists_for(so.name), [])
		reports = self._reports_for(so.name)
		self.assertEqual(len(reports), 1)
		self.assertEqual(frappe.get_doc("Reporte de Faltante", reports[0]).shortage_reason, "Compra pendiente")

	# -- Caso 5: stock cero, Manufacture -> solo faltante Manufacture -------------

	def test_submit_zero_stock_manufacture_creates_only_shortage(self):
		wh, item, customer = self._new_world(
			"ZeroManufacture", stock_qty=None, default_material_request_type="Manufacture"
		)
		raw = self.world.item("FG16-ZEROMANUFACTURE-RAW")
		self.world.bom_for(item.name, raw.name)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		self.assertEqual(self._pick_lists_for(so.name), [])
		reports = self._reports_for(so.name)
		self.assertEqual(frappe.get_doc("Reporte de Faltante", reports[0]).shortage_reason, "Producción pendiente")

	# -- Caso 6: Manufacture sin BOM -> Blocked ------------------------------------

	def test_submit_manufacture_without_bom_is_blocked(self):
		wh, item, customer = self._new_world("NoBom", stock_qty=None, default_material_request_type="Manufacture")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		reports = self._reports_for(so.name)
		self.assertEqual(len(reports), 1)
		self.assertEqual(frappe.get_doc("Reporte de Faltante", reports[0]).shortage_reason, "Configuración incompleta")

	# -- Caso 7: Sales Order mixta -> distribución correcta por línea -------------

	def test_submit_mixed_sales_order_distributes_correctly(self):
		wh = self.world.warehouse("FG16 Mixed")
		customer = self.world.customer("FG16 Mixed Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		item_a = self.world.item("FG16-MIXED-A", default_material_request_type="Purchase")
		item_b = self.world.item("FG16-MIXED-B", default_material_request_type="Purchase")
		item_c = self.world.item("FG16-MIXED-C", default_material_request_type="Manufacture")
		raw_c = self.world.item("FG16-MIXED-C-RAW")
		self.world.bom_for(item_c.name, raw_c.name)

		self.world.stock_up_real(item_a.name, wh.name, 10)
		self.world.stock_up_real(item_b.name, wh.name, 3)

		so = self._submit_via_hook(
			customer.name,
			[
				{"item_code": item_a.name, "warehouse": wh.name, "qty": 10, "rate": 100},
				{"item_code": item_b.name, "warehouse": wh.name, "qty": 8, "rate": 100},
				{"item_code": item_c.name, "warehouse": wh.name, "qty": 20, "rate": 100},
			],
		)

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl_by_item = {row.item_code: row for row in frappe.get_doc("Pick List", pick_lists[0]).get("locations")}
		self.assertEqual(pl_by_item[item_a.name].stock_qty, 10.0)
		self.assertEqual(pl_by_item[item_b.name].stock_qty, 3.0)
		self.assertNotIn(item_c.name, pl_by_item)

		reports_by_item = {r.item_code: r for r in (frappe.get_doc("Reporte de Faltante", n) for n in self._reports_for(so.name))}
		self.assertEqual(reports_by_item[item_b.name].shortage_reason, "Compra pendiente")
		self.assertEqual(reports_by_item[item_c.name].shortage_reason, "Producción pendiente")

	# -- Caso 8: excepción intencional -> rollback transaccional completo --------

	def test_engine_exception_during_submit_rolls_back_everything(self):
		"""Proves the mechanism documented in FULFILLMENT_ENGINE_CONTRACT.md,
		"Commit 16 -- transactional behaviour": an unhandled exception
		anywhere inside process_sales_order() propagates out of
		Sales Order.submit() with nothing committed in between (no
		frappe.db.commit() in the hook, the handler, or the Engine) -- so
		rolling back the surrounding transaction (exactly what Frappe's own
		request-boundary exception handler does in apps/frappe/frappe/app.py,
		traced and cited in the contract doc) undoes the Sales Order's own
		docstatus change together with whatever partial Pick List/Reporte de
		Faltante work already happened, leaving none of the three
		incoherent states the user explicitly did not want."""
		wh, item, customer = self._new_world("Rollback", stock_qty=5)
		so = self._draft_sales_order(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		frappe.db.commit()  # fixtures + draft SO survive the rollback below

		with patch(
			"fabergray_erp.fulfillment.engine.sync_shortage_reports_for_sales_order",
			side_effect=RuntimeError("Commit 16 intentional failure"),
		):
			with self.assertRaises(RuntimeError):
				so.submit()

		# What Frappe's own WSGI request handler does on an unhandled
		# exception (apps/frappe/frappe/app.py: `db.rollback(chain=True)`)
		# -- simulated explicitly here since bench run-tests never goes
		# through a real HTTP request.
		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 0)  # the submit itself was rolled back too
		self.assertEqual(
			frappe.db.sql("""select count(*) from `tabPick List Item` where sales_order=%s""", so.name)[0][0], 0
		)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 0)

		# confirm the Sales Order is left genuinely submittable afterward --
		# not stuck in a broken intermediate state.
		so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		self.assertEqual(len(self._pick_lists_for(so.name)), 1)

	# -- Caso 8b (Commit 19.2): la excepción llega DESPUÉS de crear el MR --------

	def test_engine_exception_after_material_request_creation_rolls_back_everything(self):
		"""Same proof as test_engine_exception_during_submit_rolls_back_everything
		(Commit 16), extended for Commit 19.2: the side_effect below calls the
		REAL sync_material_requests_for_sales_order() first -- so a genuine
		Material Request is actually inserted, in the same open transaction,
		before the intentional exception fires -- then raises. Proves the
		just-created Material Request rolls back together with the Sales
		Order's own docstatus change and the Pick List/Reporte de Faltante
		from the earlier steps, not just that an exception before any
        Purchase Service write would have prevented one."""
		wh, item, customer = self._new_world("RollbackMr", stock_qty=3)
		so = self._draft_sales_order(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])
		frappe.db.commit()  # fixtures + draft SO survive the rollback below

		def _create_real_material_request_then_raise(sales_order):
			from fabergray_erp.fulfillment.purchase_service import sync_material_requests_for_sales_order

			sync_material_requests_for_sales_order(sales_order)
			raise RuntimeError("Commit 19.2 intentional failure after Material Request creation")

		with patch(
			"fabergray_erp.fulfillment.engine.sync_material_requests_for_sales_order",
			side_effect=_create_real_material_request_then_raise,
		):
			with self.assertRaises(RuntimeError):
				so.submit()

		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 0)  # the submit itself was rolled back too
		self.assertEqual(
			frappe.db.sql("""select count(*) from `tabPick List Item` where sales_order=%s""", so.name)[0][0], 0
		)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 0)
		self.assertEqual(
			frappe.db.sql(
				"""select count(*) from `tabMaterial Request Item` where sales_order=%s""", so.name
			)[0][0],
			0,
		)  # the Material Request the side_effect actually created is gone too

		# confirm the Sales Order is left genuinely submittable afterward.
		so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)  # Pick List + Reporte + MR, Commit 19.2
		self.assertEqual(len(self._pick_lists_for(so.name)), 1)

	# -- Caso 9: reprocesar manualmente una SO ya procesada por el hook ----------

	def test_manual_reprocessing_after_hook_submit_is_idempotent(self):
		wh, item, customer = self._new_world("Reprocess", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		pick_lists_after_hook = self._pick_lists_for(so.name)
		reports_after_hook = self._reports_for(so.name)

		# section 6: process_sales_order() must remain directly callable --
		# reprocessing an already-processed order must not duplicate anything.
		result = process_sales_order(so.name)

		self.assertIsNone(result["pick_list"])  # nothing new to claim
		self.assertEqual(result["shortages"]["created"], [])
		self.assertEqual(result["shortages"]["updated"], [])
		self.assertEqual(self._pick_lists_for(so.name), pick_lists_after_hook)
		self.assertEqual(self._reports_for(so.name), reports_after_hook)

	# -- Caso 10: reporte de Bodega existente permanece intacto -------------------

	def test_existing_bodega_report_untouched_by_hook_and_reprocessing(self):
		wh, item, customer = self._new_world("BodegaSafe", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		bodega_report = self.world.shortage_report(
			item_code=item.name,
			warehouse=wh.name,
			sales_order=so.name,
			qty_solicitada=8,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Producto dañado",
		)

		process_sales_order(so.name)  # manual reprocess, must not touch the Bodega report

		bodega_report.reload()
		self.assertEqual(bodega_report.status, "Abierto")
		self.assertFalse(bodega_report.resolution_note)
		self.assertEqual(bodega_report.detected_by, "Bodega")

	# Sales Order cancellation is no longer "investigate only" as of
	# Commit 17 -- an on_cancel hook now actively cleans up draft Pick
	# Lists and resolves open automatic Reporte de Faltante. See
	# test_sales_order_cancel.py for the full Commit 17 suite (this file
	# used to carry a test here asserting the OLD, now-superseded native
	# behaviour -- removed rather than left lying about what happens now).
