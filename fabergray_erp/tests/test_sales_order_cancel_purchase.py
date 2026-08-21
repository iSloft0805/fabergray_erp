# -*- coding: utf-8 -*-
"""Commit 19.3 -- Sales Order cancellation lifecycle for Material Request
(the Compras-side twin of Commit 17's Pick List/Reporte de Faltante
cleanup). Every test builds and submits its own Sales Order directly
(like test_sales_order_hook.py/test_sales_order_cancel.py, not through
TestWorld.multi_item_sales_order(), which stays wrapped in
fx.without_sales_order_hook()) so the real on_submit hook creates real
artifacts, then exercises the real on_cancel hook against them.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from fabergray_erp.fulfillment.cancellation_service import cleanup_fulfillment_for_cancelled_sales_order
from fabergray_erp.fulfillment.purchase_service import MATERIAL_REQUEST_TYPE
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestSalesOrderCancelPurchase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg193-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG193 {tag}")
		item = self.world.item(f"FG193-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG193 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _draft_sales_order(self, customer, items):
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

	def _mr_for(self, so_name):
		names = frappe.get_all(
			"Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
		)
		self.assertEqual(len(names), 1, f"expected exactly one Material Request for {so_name}, found {names}")
		return names[0]

	def _standalone_mr(self, company, item_code, warehouse, qty, sales_order=None, sales_order_item=None, flagged=False):
		"""A Material Request built directly, mirroring test_purchase_service.py's
		own helper -- never through purchase_service.py, so tests using
		this prove cancellation_service.py's own query keys off the
		Custom Field, not off "however it was created"."""
		row = {
			"item_code": item_code,
			"qty": qty,
			"warehouse": warehouse,
			"schedule_date": nowdate(),
		}
		if sales_order:
			row["sales_order"] = sales_order
			row["sales_order_item"] = sales_order_item
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": MATERIAL_REQUEST_TYPE,
				"company": company,
				"transaction_date": nowdate(),
				"fg_created_by_fulfillment_engine": 1 if flagged else 0,
				"items": [row],
			}
		)
		mr.insert()
		self.world.track_existing("Material Request", mr.name)
		return mr

	# -- Caso 1: MR draft automático, exclusivo de esa SO -> se elimina ---------

	def test_engine_draft_material_request_exclusive_to_the_so_is_deleted(self):
		wh, item, customer = self._new_world("Exclusive", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)
		self.assertEqual(frappe.db.get_value("Material Request", mr_name, "fg_created_by_fulfillment_engine"), 1)

		so.cancel()

		self.assertFalse(frappe.db.exists("Material Request", mr_name))

	# -- Caso 2: MR submitted -> bloquea la cancelación nativamente --------------

	def test_submitted_material_request_blocks_cancellation_natively(self):
		wh, item, customer = self._new_world("Submitted", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)
		pick_list_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]

		frappe.get_doc("Material Request", mr_name).submit()
		frappe.db.commit()  # fixtures + submitted MR survive the rollback below

		with self.assertRaises(frappe.LinkExistsError):
			so.cancel()
		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 1)  # cancellation did not go through
		self.assertTrue(frappe.db.exists("Pick List", pick_list_name))  # nothing partially cleaned up either
		mr = frappe.get_doc("Material Request", mr_name)
		self.assertEqual(mr.docstatus, 1)  # untouched, still submitted

	# -- Caso 3: Purchase Order existente -> ERPNext lo desvincula solo ---------

	def test_existing_purchase_order_is_natively_unlinked_not_modified(self):
		wh, item, customer = self._new_world("PO", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		supplier_name = "FG193-PO-SUPPLIER"
		if not frappe.db.exists("Supplier", supplier_name):
			supplier = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}
			)
			supplier.insert()
			self.world.track_existing("Supplier", supplier.name)

		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": supplier_name,
				"company": so.company,
				"transaction_date": nowdate(),
				"schedule_date": getdate(nowdate()),
				"items": [
					{
						"item_code": item.name,
						"qty": 8,
						"warehouse": wh.name,
						"schedule_date": getdate(nowdate()),
						"rate": 10,
						"sales_order": so.name,
						"sales_order_item": so.items[0].name,
					}
				],
			}
		)
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)

		so.cancel()  # must succeed -- ERPNext's own AccountsController.on_cancel()
		# (unlink_ref_doc_from_po()) clears the link before this module's own
		# cleanup or the native back-link check ever run.

		so.reload()
		self.assertEqual(so.docstatus, 2)

		po.reload()
		self.assertEqual(po.docstatus, 1)  # never cancelled
		self.assertEqual(po.items[0].qty, 8.0)  # never modified
		self.assertIsNone(po.items[0].sales_order)  # native unlink, not our code
		self.assertIsNone(po.items[0].sales_order_item)

	# -- Caso 4: MR mixto con otra SO -> se retiran solo las líneas de esta SO --

	def test_mixed_material_request_with_another_so_only_loses_this_sos_rows(self):
		wh, item, customer = self._new_world("Mixed", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		# Compras adds a row for a completely different, still-open Sales
		# Order onto the SAME Engine-created draft -- the only realistic
		# way a "mixed" document happens, since purchase_service.py itself
		# never spans more than one Sales Order per call.
		other_wh, other_item, other_customer = self._new_world("MixedOther", stock_qty=1)
		other_so = self.world.multi_item_sales_order(
			other_customer.name, [{"item_code": other_item.name, "warehouse": other_wh.name, "qty": 3, "rate": 100}]
		)

		mr = frappe.get_doc("Material Request", mr_name)
		mr.append(
			"items",
			{
				"item_code": other_item.name,
				"qty": 2,
				"warehouse": other_wh.name,
				"schedule_date": getdate(nowdate()),
				"sales_order": other_so.name,
				"sales_order_item": other_so.items[0].name,
			},
		)
		mr.save()
		self.assertEqual(len(mr.items), 2)

		so.cancel()

		self.assertTrue(frappe.db.exists("Material Request", mr_name))  # NOT deleted
		mr.reload()
		self.assertEqual(len(mr.items), 1)  # only this SO's row is gone
		self.assertEqual(mr.items[0].sales_order, other_so.name)  # Compras' own row, untouched
		self.assertEqual(mr.items[0].qty, 2.0)

	# -- Caso 5: MR ya cancelado -> se ignora, cancelación de la SO procede normal --

	def test_already_cancelled_material_request_is_ignored(self):
		wh, item, customer = self._new_world("MrCancelled", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		mr = frappe.get_doc("Material Request", mr_name)
		mr.submit()
		mr.cancel()

		so.cancel()  # must not raise, must not attempt to touch the already-cancelled MR

		so.reload()
		self.assertEqual(so.docstatus, 2)
		mr.reload()
		self.assertEqual(mr.docstatus, 2)  # untouched, still cancelled

	# -- Caso 6: SO sin Material Request -> cancelación normal -------------------

	def test_cancelling_so_without_material_request_works_normally(self):
		wh, item, customer = self._new_world("NoMr", stock_qty=10)  # full stock -- no shortage, no MR
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		self.assertEqual(frappe.get_all("Material Request Item", filters={"sales_order": so.name}), [])

		so.cancel()  # must not raise

		so.reload()
		self.assertEqual(so.docstatus, 2)

	# -- Caso 7: rollback -- excepción durante el cleanup del MR ------------------

	def test_intentional_error_during_material_request_cleanup_rolls_back_everything(self):
		wh, item, customer = self._new_world("MrRollback", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		pick_list_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]
		mr_name = self._mr_for(so.name)
		frappe.db.commit()  # fixtures + hook-created artifacts survive the rollback below

		with patch(
			"fabergray_erp.fulfillment.cancellation_service._cleanup_engine_material_request_for",
			side_effect=RuntimeError("Commit 19.3 intentional failure"),
		):
			with self.assertRaises(RuntimeError):
				so.cancel()

		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 1)  # the cancellation itself was rolled back too
		self.assertTrue(frappe.db.exists("Pick List", pick_list_name))  # cleanup's own earlier work undone
		self.assertTrue(frappe.db.exists("Material Request", mr_name))  # never actually deleted

		# confirm the Sales Order is left genuinely cancellable afterward.
		so.cancel()
		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))
		self.assertFalse(frappe.db.exists("Material Request", mr_name))

	# -- Caso 8: idempotencia -- correr el servicio dos veces --------------------

	def test_cleanup_service_run_twice_is_idempotent_for_material_request_too(self):
		wh, item, customer = self._new_world("MrIdempotent", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		so.cancel()
		self.assertFalse(frappe.db.exists("Material Request", mr_name))

		result = cleanup_fulfillment_for_cancelled_sales_order(so.name)
		self.assertEqual(
			result,
			{
				"removed_pick_lists": [],
				"resolved_reports": [],
				"removed_material_requests": [],
				"trimmed_material_requests": [],
			},
		)

	# -- Caso 9: distinguir manual vs automático -- MR sin el flag nunca se toca --

	def test_manually_created_material_request_without_the_flag_is_never_touched(self):
		wh, item, customer = self._new_world("Manual", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		engine_mr_name = self._mr_for(so.name)

		# A second Material Request, exclusively for this same Sales Order,
		# built the same way a human would via ERPNext's own native
		# "Create Material Request" button -- everything field-for-field
		# identical to the Engine's own output EXCEPT the flag.
		manual_mr = self._standalone_mr(
			so.company, item.name, wh.name, 4, sales_order=so.name, sales_order_item=so.items[0].name, flagged=False
		)

		so.cancel()

		self.assertFalse(frappe.db.exists("Material Request", engine_mr_name))  # Engine's own -- deleted
		manual_mr.reload()
		self.assertEqual(manual_mr.docstatus, 0)  # manual's own -- completely untouched
		self.assertEqual(len(manual_mr.items), 1)
		self.assertEqual(manual_mr.items[0].sales_order, so.name)

	# -- Caso 10: Vendedora sigue sin acceso directo a Material Request/PO ------

	def test_vendedora_still_has_no_direct_access_after_cancellation_cleanup(self):
		from fabergray_erp.api import ventas

		wh = self.world.warehouse("FG193 VendedoraNoAccess")
		item = self.world.item("FG193-VENDEDORANOACCESS", default_material_request_type="Purchase", default_warehouse=wh.name)
		customer = self.world.customer("FG193 VendedoraNoAccess Customer")
		self.world.stock_up_real(item.name, wh.name, 2)
		vendedora = self.world.user("fg193-vendedora@example.com", ["Vendedora"])

		with fx.as_user(vendedora):
			result = ventas.create_and_submit_sales_order(
				customer=customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		mr_name = self._mr_for(result["name"])

		so = frappe.get_doc("Sales Order", result["name"])
		so.cancel()  # Vendedora has no cancel permission on Sales Order (by
		# design, Commit 18.1) -- run as the ambient Administrator session,
		# exactly like every other cancellation test in this suite.

		self.assertFalse(frappe.db.exists("Material Request", mr_name))

		with fx.as_user(vendedora):
			self.assertFalse(frappe.has_permission("Material Request", "read"))
			self.assertFalse(frappe.has_permission("Material Request", "create"))
			self.assertFalse(frappe.has_permission("Purchase Order", "read"))
