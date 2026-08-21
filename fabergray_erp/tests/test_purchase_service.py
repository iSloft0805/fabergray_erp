# -*- coding: utf-8 -*-
"""Commit 19.1 -- tests for
fabergray_erp.fulfillment.purchase_service.sync_material_requests_for_sales_order().

Every test drives the real service end-to-end: no hand-built shortage
math of its own -- assertions check what the service actually produced
against analyze_sales_order() (Commit 12) and against real Material
Request documents fetched from the database. A handful of tests build a
Material Request directly (frappe.get_doc({...}), never through the
service under test) to simulate "a document that already exists for some
other reason" (a prior call, a human via ERPNext's own native "Create
Material Request" button, or Compras submitting one) -- exactly the kind
of pre-existing document sync_material_requests_for_sales_order()'s own
idempotency query must see and net against, regardless of how it came to
exist.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_available_stock
from fabergray_erp.fulfillment.purchase_service import (
	MATERIAL_REQUEST_TYPE,
	qty_already_claimed_by_open_material_requests_for_so_item,
	sync_material_requests_for_sales_order,
)
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestPurchaseService(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG19 {tag}")
		item = self.world.item(f"FG19-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG19 {tag} Customer")
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _mr(self, name):
		return frappe.get_doc("Material Request", name)

	def _track_all(self, names):
		for name in names:
			self.world.track_existing("Material Request", name)

	def _insert_standalone_mr(self, so, sales_order_item, item_code, warehouse, qty, submit=False):
		"""Builds a Material Request directly, exactly like a human would
		via ERPNext's native "Create Material Request" button would end up
		producing field-for-field (sales_order/sales_order_item populated,
		material_request_type="Purchase") -- deliberately NOT going through
		sync_material_requests_for_sales_order(), so tests using this prove
		the idempotency query works regardless of a document's origin, not
		just against the service's own prior output."""
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": MATERIAL_REQUEST_TYPE,
				"company": so.company,
				"transaction_date": nowdate(),
				"items": [
					{
						"item_code": item_code,
						"qty": qty,
						"warehouse": warehouse,
						"schedule_date": nowdate(),
						"sales_order": so.name,
						"sales_order_item": sales_order_item,
					}
				],
			}
		)
		mr.insert()
		self.world.track_existing("Material Request", mr.name)
		if submit:
			mr.submit()
		return mr

	# -- Caso: Purchase shortage crea MR correcto --------------------------

	def test_purchase_shortage_creates_correct_material_request(self):
		wh, item, customer = self._new_world("Purchase", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 1)
		self.assertEqual(summary["lines_requested"], [so.items[0].name])

		mr = self._mr(summary["created"][0])
		self.assertEqual(mr.docstatus, 0)
		self.assertEqual(mr.material_request_type, "Purchase")
		self.assertEqual(len(mr.items), 1)
		row = mr.items[0]
		self.assertEqual(row.item_code, item.name)
		self.assertEqual(row.warehouse, wh.name)
		self.assertEqual(flt(row.qty), 5.0)
		self.assertEqual(row.sales_order, so.name)
		self.assertEqual(row.sales_order_item, so.items[0].name)

	# -- Caso: stock parcial -> MR solo por remanente -----------------------

	def test_partial_stock_creates_material_request_only_for_remainder(self):
		wh, item, customer = self._new_world("Partial", stock_qty=6)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		mr = self._mr(summary["created"][0])
		self.assertEqual(flt(mr.items[0].qty), 4.0)

	# -- Caso: stock completo -> no crea MR ----------------------------------

	def test_full_stock_creates_no_material_request(self):
		wh, item, customer = self._new_world("Full", stock_qty=10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary, {"created": [], "lines_requested": []})
		self.assertEqual(frappe.db.count("Material Request Item", {"sales_order": so.name}), 0)

	# -- Caso: Manufacture -> no crea MR --------------------------------------

	def test_manufacture_route_creates_no_material_request(self):
		wh, item, customer = self._new_world("Manufacture", stock_qty=None, default_material_request_type="Manufacture")
		raw = self.world.item("FG19-MANUFACTURE-RAW")
		self.world.bom_for(item.name, raw.name)
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary, {"created": [], "lines_requested": []})

	# -- Caso: Blocked (Manufacture sin BOM) -> no crea MR --------------------

	def test_blocked_route_creates_no_material_request(self):
		wh, item, customer = self._new_world("Blocked", stock_qty=None, default_material_request_type="Manufacture")
		so = self.world.submitted_sales_order(item.name, wh.name, 5, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary, {"created": [], "lines_requested": []})

	# -- Caso: Sales Order mixta -> solo líneas Purchase ----------------------

	def test_mixed_sales_order_only_requests_purchase_route_lines(self):
		wh = self.world.warehouse("FG19 Mixed")
		customer = self.world.customer("FG19 Mixed Customer")
		item_ready = self.world.item("FG19-MIXED-READY", default_material_request_type="Purchase")
		item_purchase = self.world.item("FG19-MIXED-PURCHASE", default_material_request_type="Purchase")
		item_manufacture = self.world.item("FG19-MIXED-MANUFACTURE", default_material_request_type="Manufacture")
		raw = self.world.item("FG19-MIXED-RAW")
		self.world.bom_for(item_manufacture.name, raw.name)

		self.world.stock_up_real(item_ready.name, wh.name, 10)
		self.world.stock_up_real(item_purchase.name, wh.name, 3)
		# item_manufacture: 0 stock

		so = self.world.multi_item_sales_order(
			customer.name,
			[
				{"item_code": item_ready.name, "warehouse": wh.name, "qty": 10, "rate": 100},
				{"item_code": item_purchase.name, "warehouse": wh.name, "qty": 8, "rate": 100},
				{"item_code": item_manufacture.name, "warehouse": wh.name, "qty": 20, "rate": 100},
			],
		)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		self.assertEqual(len(summary["created"]), 1)
		mr = self._mr(summary["created"][0])
		self.assertEqual(len(mr.items), 1)
		self.assertEqual(mr.items[0].item_code, item_purchase.name)
		self.assertEqual(flt(mr.items[0].qty), 5.0)

	# -- Caso: segunda ejecución -> no duplica --------------------------------

	def test_running_twice_does_not_duplicate(self):
		wh, item, customer = self._new_world("Twice", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary_1 = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary_1["created"])
		summary_2 = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary_2, {"created": [], "lines_requested": []})
		self.assertEqual(frappe.db.count("Material Request Item", {"sales_order": so.name}), 1)

	# -- Caso: MR draft existente parcial (de otro origen) -> solo pide el remanente --

	def test_pre_existing_partial_draft_from_another_origin_only_leaves_remainder(self):
		"""The pre-existing Material Request is built directly
		(_insert_standalone_mr), never through the service under test --
		proves qty_already_claimed_by_open_material_requests_for_so_item()
		nets correctly against a document it did not itself create (e.g. a
		human via ERPNext's own native "Create Material Request" button)."""
		wh, item, customer = self._new_world("PreExisting", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		# Real shortage = 10 - 2 = 8. A pre-existing draft already covers 3.
		self._insert_standalone_mr(so, so.items[0].name, item.name, wh.name, 3)

		already_claimed = qty_already_claimed_by_open_material_requests_for_so_item(so.items[0].name)
		self.assertEqual(already_claimed, 3.0)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		mr = self._mr(summary["created"][0])
		self.assertEqual(flt(mr.items[0].qty), 5.0)  # 8 - 3, not the full 8
		self.assertEqual(frappe.db.count("Material Request Item", {"sales_order": so.name}), 2)

		# Running again with nothing changed -- still no new document.
		summary_2 = sync_material_requests_for_sales_order(so.name)
		self.assertEqual(summary_2, {"created": [], "lines_requested": []})

	# -- Caso: Pick List abierto cubre parte -> MR solo por faltante neto real --

	def test_open_pick_list_reduces_the_material_request_by_the_real_net_need(self):
		wh, item, customer = self._new_world("PickListIntegration", stock_qty=6)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		pl = create_pick_list_for_available_stock(so.name)
		self.world.track_existing("Pick List", pl.name)
		pl_qty = flt(pl.get("locations")[0].stock_qty)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])
		mr = self._mr(summary["created"][0])
		mr_qty = flt(mr.items[0].qty)

		self.assertEqual(pl_qty, 6.0)
		self.assertEqual(mr_qty, 4.0)
		self.assertEqual(pl_qty + mr_qty, 10.0)  # exactly the order's real pending need

	# -- Caso: llega más stock antes del submit -> el draft existente NO se toca --

	def test_more_stock_arriving_does_not_mutate_the_existing_draft(self):
		"""Documents the approved, deliberately conservative behaviour for
		the flagged gap (see purchase_service.py's own docstring): this
		module never updates or deletes a pre-existing Material Request, so
		when the real need shrinks after a draft already exists, the
		correct/safe outcome is "request nothing further", NOT "shrink the
		existing draft" -- the existing draft is left exactly as it was for
		Compras to review manually."""
		wh, item, customer = self._new_world("StockArrives", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary_1 = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary_1["created"])
		mr = self._mr(summary_1["created"][0])
		self.assertEqual(flt(mr.items[0].qty), 8.0)

		self.world.stock_up_real(item.name, wh.name, 9)  # absolute qty now 9 (was 2) -- shortage now only 1

		summary_2 = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary_2, {"created": [], "lines_requested": []})  # no over-fetch, no new MR
		mr.reload()
		self.assertEqual(mr.docstatus, 0)
		self.assertEqual(flt(mr.items[0].qty), 8.0)  # untouched, exactly as it was

	# -- Caso: shortage desaparece -> draft automático se deja intacto, no se toca --

	def test_shortage_disappearing_does_not_delete_or_modify_the_existing_draft(self):
		wh, item, customer = self._new_world("Disappears", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary_1 = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary_1["created"])
		mr = self._mr(summary_1["created"][0])

		self.world.stock_up_real(item.name, wh.name, 10)  # now fully covers the order

		summary_2 = sync_material_requests_for_sales_order(so.name)

		self.assertEqual(summary_2, {"created": [], "lines_requested": []})
		self.assertTrue(frappe.db.exists("Material Request", mr.name))  # never auto-deleted
		mr.reload()
		self.assertEqual(mr.docstatus, 0)
		self.assertEqual(flt(mr.items[0].qty), 8.0)  # untouched

	# -- Caso: MR submitted existente -> Engine no lo modifica, solo remanente --

	def test_submitted_material_request_is_never_modified_only_remainder_is_requested(self):
		wh, item, customer = self._new_world("Submitted", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		# Real shortage = 8. A pre-existing, ALREADY SUBMITTED MR covers only 3 of it.
		submitted_mr = self._insert_standalone_mr(so, so.items[0].name, item.name, wh.name, 3, submit=True)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		submitted_mr.reload()
		self.assertEqual(submitted_mr.docstatus, 1)
		self.assertEqual(flt(submitted_mr.items[0].qty), 3.0)  # never touched

		new_mr = self._mr(summary["created"][0])
		self.assertNotEqual(new_mr.name, submitted_mr.name)  # a distinct new document
		self.assertEqual(flt(new_mr.items[0].qty), 5.0)  # 8 - 3

	# -- Caso: MR cancelado -> vuelve a considerar la cantidad completa ------

	def test_cancelled_material_request_is_excluded_and_full_need_is_reconsidered(self):
		wh, item, customer = self._new_world("Cancelled", stock_qty=2)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		summary_1 = sync_material_requests_for_sales_order(so.name)
		mr_1 = self._mr(summary_1["created"][0])
		self.assertEqual(flt(mr_1.items[0].qty), 8.0)

		mr_1.submit()
		mr_1.cancel()
		self.world.track_existing("Material Request", mr_1.name)

		already_claimed = qty_already_claimed_by_open_material_requests_for_so_item(so.items[0].name)
		self.assertEqual(already_claimed, 0.0)  # cancelled -- no longer counts

		summary_2 = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary_2["created"])

		mr_2 = self._mr(summary_2["created"][0])
		self.assertEqual(flt(mr_2.items[0].qty), 8.0)  # full need again, nothing lost

	# -- Caso: mismo item en dos Sales Order Items -> filas independientes ---

	def test_same_item_repeated_in_two_lines_gets_independent_rows(self):
		item = self.world.item("FG19-REPEATED", default_material_request_type="Purchase")
		wh_1 = self.world.warehouse("FG19 Repeated 1")
		wh_2 = self.world.warehouse("FG19 Repeated 2")
		customer = self.world.customer("FG19 Repeated Customer")

		self.world.stock_up_real(item.name, wh_1.name, 3)
		self.world.stock_up_real(item.name, wh_2.name, 1)

		so = self.world.multi_item_sales_order(
			customer.name,
			[
				{"item_code": item.name, "warehouse": wh_1.name, "qty": 8, "rate": 100},
				{"item_code": item.name, "warehouse": wh_2.name, "qty": 5, "rate": 100},
			],
		)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		mr = self._mr(summary["created"][0])
		self.assertEqual(len(mr.items), 2)
		rows_by_warehouse = {row.warehouse: row for row in mr.items}
		self.assertEqual(flt(rows_by_warehouse[wh_1.name].qty), 5.0)
		self.assertEqual(flt(rows_by_warehouse[wh_2.name].qty), 4.0)
		self.assertNotEqual(rows_by_warehouse[wh_1.name].sales_order_item, rows_by_warehouse[wh_2.name].sales_order_item)

		# run again -- must still be exactly 1 Material Request, 2 rows total
		summary_2 = sync_material_requests_for_sales_order(so.name)
		self.assertEqual(summary_2, {"created": [], "lines_requested": []})
		self.assertEqual(frappe.db.count("Material Request Item", {"sales_order": so.name}), 2)

	# -- Caso: enlaces nativos sales_order/sales_order_item correctos --------

	def test_material_request_item_links_natively_to_sales_order_and_item(self):
		wh, item, customer = self._new_world("Links", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		row = self._mr(summary["created"][0]).items[0]
		self.assertEqual(row.sales_order, so.name)
		self.assertEqual(row.sales_order_item, so.items[0].name)

	# -- Caso: ningún Supplier ni Purchase Order es creado --------------------

	def test_no_supplier_or_purchase_order_is_ever_created(self):
		wh, item, customer = self._new_world("NoPO", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		mr = self._mr(summary["created"][0])
		for row in mr.items:
			self.assertFalse(row.get("supplier"))
		self.assertEqual(frappe.db.count("Purchase Order Item", {"sales_order": so.name}), 0)

	# -- Caso: Sales Order Item.requested_qty permanece 0 para un MR en Draft --

	def test_sales_order_item_requested_qty_stays_zero_for_draft_material_request(self):
		"""Explicit confirmation of the audited native behaviour this whole
		module's idempotency design is built on: MaterialRequest.
		update_prevdoc_status() (the only writer of Sales Order Item.
		requested_qty) only runs from on_submit()/on_cancel() -- never for
		a Draft document. Not asserting a private implementation detail --
		asserting the exact real-database field a future engineer might be
		tempted to read instead of qty_already_claimed_by_open_material_
		requests_for_so_item()."""
		wh, item, customer = self._new_world("RequestedQtyZero", stock_qty=3)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)

		summary = sync_material_requests_for_sales_order(so.name)
		self._track_all(summary["created"])

		mr = self._mr(summary["created"][0])
		self.assertEqual(mr.docstatus, 0)

		requested_qty = frappe.db.get_value("Sales Order Item", so.items[0].name, "requested_qty")
		self.assertEqual(flt(requested_qty), 0.0)

		# Confirmed the other direction too: submitting the SAME draft MR
		# does make the native field catch up -- proving the 0 above is
		# specifically a Draft-only fact, not a general bug.
		mr.submit()
		self.world.track_existing("Material Request", mr.name)
		requested_qty_after_submit = frappe.db.get_value("Sales Order Item", so.items[0].name, "requested_qty")
		self.assertEqual(flt(requested_qty_after_submit), flt(mr.items[0].qty))
