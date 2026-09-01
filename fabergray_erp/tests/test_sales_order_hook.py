# -*- coding: utf-8 -*-
"""Commit 16, rewritten by Commit 25.4 -- end-to-end tests for the real
Sales Order.on_submit hook (hooks.py doc_events -> fulfillment/
sales_order_hooks.py -> fulfillment/engine.py).

Commit 25.4 -- "Ventas no decide faltantes. El stock teórico no decide el
faltante definitivo": the hook no longer calls `process_sales_order()`
(the original Commit 15 four-step orchestrator, still fully intact and
tested elsewhere -- see test_engine.py -- just no longer wired here). It
now calls `process_sales_order_for_confirmation()` instead, which does
exactly ONE automated thing: create a Pick List for the Sales Order's
FULL requested demand (`create_pick_list_for_full_demand()`), regardless
of `Bin.actual_qty` -- never a Reporte de Faltante, never a Material
Request. Every test below was rewritten (not just the assertions --
several no longer apply at all, see the removed-cases note at the bottom)
to prove exactly that live, not assumed from reading the code alone.

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
from fabergray_erp.fulfillment.engine import process_sales_order_for_confirmation
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

	def _material_requests_for(self, sales_order_name):
		return frappe.get_all(
			"Material Request Item", filters={"sales_order": sales_order_name}, pluck="parent", distinct=True
		)

	# -- Caso 1: stock completo -> Pick List automático + aparece en get_queue() --

	def test_submit_full_stock_creates_full_pick_list_no_shortage(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(pl.get("locations")[0].stock_qty, 10.0)
		self.assertEqual(self._reports_for(so.name), [])
		self.assertEqual(self._material_requests_for(so.name), [])

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

	# -- Caso 2 (Commit 25.4): stock parcial -> Pick List por la demanda COMPLETA,
	# NUNCA un Reporte de Faltante automático ------------------------------------

	def test_submit_partial_stock_creates_full_demand_pick_list_no_automatic_shortage(self):
		wh, item, customer = self._new_world("Partial", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		rows = pl.get("locations")
		# Bodega must see the FULL 8 requested, not the 3 theoretically
		# available -- the whole point of Commit 25.4. Native mapping
		# already produces one row capped at the real Bin qty (3); the
		# top-up loop in create_pick_list_for_full_demand() APPENDS a
		# second row for the shortfall (5) rather than editing the first
		# in place -- so the guarantee is on the SUM across rows for this
		# sales_order_item, not on a single row.
		self.assertEqual(sum(row.stock_qty for row in rows), 8.0)
		self.assertTrue(all(row.picked_qty == 0.0 for row in rows))

		self.assertEqual(self._reports_for(so.name), [])
		self.assertEqual(self._material_requests_for(so.name), [])

	# -- Caso 3 (Commit 25.4): stock cero, Purchase -> Pick List igual se crea,
	# con la demanda completa, sin faltante automático ----------------------------

	def test_submit_zero_stock_purchase_creates_full_demand_pick_list_no_automatic_shortage(self):
		wh, item, customer = self._new_world("ZeroPurchase", stock_qty=None, default_material_request_type="Purchase")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(pl.get("locations")[0].stock_qty, 5.0)  # full demand, actual_qty was 0
		self.assertEqual(pl.get("locations")[0].warehouse, wh.name)

		self.assertEqual(self._reports_for(so.name), [])
		self.assertEqual(self._material_requests_for(so.name), [])

	# -- Caso 4 (Commit 25.4): stock cero, Manufacture -> misma garantía,
	# independiente de la ruta de abastecimiento -----------------------------------

	def test_submit_zero_stock_manufacture_creates_full_demand_pick_list_no_automatic_shortage(self):
		wh, item, customer = self._new_world(
			"ZeroManufacture", stock_qty=None, default_material_request_type="Manufacture"
		)
		raw = self.world.item("FG16-ZEROMANUFACTURE-RAW")
		self.world.bom_for(item.name, raw.name)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		self.assertEqual(frappe.get_doc("Pick List", pick_lists[0]).get("locations")[0].stock_qty, 5.0)
		self.assertEqual(self._reports_for(so.name), [])

	# -- Caso 5 (Commit 25.4): Manufacture SIN BOM -- ya no bloquea nada
	# automático (procurement_route ya no decide nada en el hook) -----------------

	def test_submit_manufacture_without_bom_still_creates_full_demand_pick_list(self):
		"""Pre-25.4 this case produced a Reporte de Faltante with
		shortage_reason="Configuración incompleta" and no Pick List at
		all -- procurement_route/blocking_reason drove that decision.
		Commit 25.4's hook never consults procurement_route at all
		anymore (create_pick_list_for_full_demand() doesn't call
		_procurement_route_for_item() or read `blocking_reason` in any
		way) -- Bodega still gets the full line to look at, exactly like
		every other case; the missing-BOM problem remains a real,
		unresolved master-data gap, but it is no longer this hook's job
		to surface it automatically. analyze_sales_order() itself is
		untouched and still reports `blocking_reason` correctly for
		whoever calls it directly."""
		wh, item, customer = self._new_world("NoBom", stock_qty=None, default_material_request_type="Manufacture")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 1)
		self.assertEqual(frappe.get_doc("Pick List", pick_lists[0]).get("locations")[0].stock_qty, 5.0)
		self.assertEqual(self._reports_for(so.name), [])

	# -- Caso 6: Sales Order mixta -> TODAS las líneas con demanda completa -------

	def test_submit_mixed_sales_order_every_line_gets_full_demand(self):
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
		# item_c: 0 stock, Manufacture route -- must still appear in full.

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
		rows = frappe.get_doc("Pick List", pick_lists[0]).get("locations")
		# One sales_order_item can legitimately span more than one Pick
		# List Item row (native capped row + top-up row for the
		# shortfall) -- sum per item_code rather than assume one row.
		qty_by_item = {}
		for row in rows:
			qty_by_item[row.item_code] = qty_by_item.get(row.item_code, 0.0) + row.stock_qty
		self.assertEqual(qty_by_item[item_a.name], 10.0)
		self.assertEqual(qty_by_item[item_b.name], 8.0)  # full demand, not capped at 3
		self.assertEqual(qty_by_item[item_c.name], 20.0)  # zero stock -- still present, full demand
		self.assertEqual(len(qty_by_item), 3)  # every line present, none silently dropped

		self.assertEqual(self._reports_for(so.name), [])
		self.assertEqual(self._material_requests_for(so.name), [])

	# -- Caso 7: excepción intencional -> rollback transaccional completo --------

	def test_engine_exception_during_submit_rolls_back_everything(self):
		"""Proves the same transactional guarantee Commit 16 originally
		established, updated for Commit 25.4's much smaller hook: an
		unhandled exception inside create_pick_list_for_full_demand()
		propagates out of Sales Order.submit() with nothing committed in
		between (no frappe.db.commit() in the hook, the handler, or
		pick_list_service.py) -- rolling back the surrounding transaction
		undoes the Sales Order's own docstatus change together with
		whatever partial Pick List work already happened."""
		wh, item, customer = self._new_world("Rollback", stock_qty=5)
		so = self._draft_sales_order(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		frappe.db.commit()  # fixtures + draft SO survive the rollback below

		with patch(
			"fabergray_erp.fulfillment.engine.create_pick_list_for_full_demand",
			side_effect=RuntimeError("Commit 25.4 intentional failure"),
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

	# -- Caso 8: reprocesar manualmente no duplica la Pick List -------------------

	def test_manual_reprocessing_after_hook_submit_is_idempotent(self):
		"""Section 6 of the new contract: create_pick_list_for_full_demand()
		must remain directly callable (e.g. a future admin "reprocess"
		action) without duplicating anything -- calling it again right
		after the hook already ran finds nothing left to claim (the
		Sales Order's own full demand is already sitting in the open
		Pick List the hook created) and returns None, exactly like
		create_pick_list_for_available_stock()'s own established
		idempotency guarantee (Commit 13), reused verbatim here via the
		same _qty_already_claimed_by_open_pick_lists_for_so_item()
		helper."""
		wh, item, customer = self._new_world("Reprocess", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		pick_lists_after_hook = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists_after_hook), 1)

		result = process_sales_order_for_confirmation(so.name)

		self.assertIsNone(result["pick_list"])  # nothing new to claim
		self.assertEqual(self._pick_lists_for(so.name), pick_lists_after_hook)  # no duplicate created

	# -- Caso 9: reporte de Bodega existente permanece intacto -------------------

	def test_existing_bodega_report_untouched_by_hook(self):
		"""A Reporte de Faltante Bodega creates herself (via her own,
		unrelated report_shortage() flow) must never be touched by this
		hook -- confirmed here by creating one right after the hook fires
		and re-reading it unchanged; the hook itself no longer creates
		any Reporte de Faltante of its own to potentially conflict with
		it in the first place (Commit 25.4), so this is now a simpler,
		stronger guarantee than before."""
		wh, item, customer = self._new_world("BodegaSafe", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		self.assertEqual(self._reports_for(so.name), [])  # confirmed: hook created none

		bodega_report = self.world.shortage_report(
			item_code=item.name,
			warehouse=wh.name,
			sales_order=so.name,
			qty_solicitada=8,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Producto dañado",
		)

		bodega_report.reload()
		self.assertEqual(bodega_report.status, "Abierto")
		self.assertFalse(bodega_report.resolution_note)
		self.assertEqual(bodega_report.detected_by, "Bodega")

	# Casos eliminados de la versión Commit 16 de este archivo, y por qué:
	#
	# - "Caso 8b" (excepción DESPUÉS de crear un Material Request real):
	#   ya no aplica -- sync_material_requests_for_sales_order() nunca se
	#   llama desde el hook (Commit 25.4), así que no hay ningún Material
	#   Request real que crear-y-luego-fallar en este camino en absoluto.
	#
	# - El caso que verificaba "Manufacture sin BOM -> shortage_reason
	#   Configuración incompleta" fue reemplazado (no eliminado) por
	#   test_submit_manufacture_without_bom_still_creates_full_demand_
	#   pick_list arriba, que prueba la garantía correcta bajo la nueva
	#   regla en vez de la que ya no aplica.
	#
	# Sales Order cancellation is no longer "investigate only" as of
	# Commit 17 -- an on_cancel hook now actively cleans up draft Pick
	# Lists and resolves open automatic Reporte de Faltante. See
	# test_sales_order_cancel.py for the full Commit 17 suite (this file
	# used to carry a test here asserting the OLD, now-superseded native
	# behaviour -- removed rather than left lying about what happens now).
