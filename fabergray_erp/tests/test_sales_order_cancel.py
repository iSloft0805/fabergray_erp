# -*- coding: utf-8 -*-
"""Commit 17 -- Sales Order cancellation lifecycle for the Fulfillment
Engine: end-to-end tests for the real Sales Order.on_cancel ->
cleanup_fulfillment_for_cancelled_sales_order() hook (hooks.py doc_events
-> fulfillment/sales_order_hooks.py -> fulfillment/cancellation_service.py).

Every test builds and submits its own Sales Order directly (like
test_sales_order_hook.py, not through TestWorld.multi_item_sales_order(),
which stays wrapped in fx.without_sales_order_hook() so every other test
file keeps behaving exactly as before) so the real on_submit hook creates
real artifacts to then cancel against.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.cancellation_service import cleanup_fulfillment_for_cancelled_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestSalesOrderCancel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg17-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG17 {tag}")
		item = self.world.item(f"FG17-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG17 {tag} Customer")
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

	def _pick_lists_for(self, sales_order_name):
		return frappe.get_all(
			"Pick List Item",
			filters={"sales_order": sales_order_name, "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)

	def _reports_for(self, sales_order_name):
		return frappe.get_all("Reporte de Faltante", filters={"sales_order": sales_order_name}, pluck="name")

	# -- Caso 1+2: Pick List draft se elimina y deja de aparecer en get_queue() --

	def test_cancelling_so_with_draft_pick_list_removes_it_and_from_queue(self):
		wh, item, customer = self._new_world("DraftPL", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		pick_list_name = self._pick_lists_for(so.name)[0]

		with fx.as_user(self.bodega_user):
			queue_before = bodega.get_queue()
		self.assertIn(pick_list_name, [p["name"] for p in queue_before["pendientes"]])

		so.cancel()

		so.reload()
		self.assertEqual(so.docstatus, 2)
		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))  # removed, not just cancelled

		with fx.as_user(self.bodega_user):
			queue_after = bodega.get_queue()
		self.assertNotIn(pick_list_name, [p["name"] for p in queue_after["pendientes"]])
		self.assertNotIn(pick_list_name, [p["name"] for p in queue_after["en_alistamiento"]])

	# -- Caso 3+4: faltante automático abierto -> Resuelto, con nota clara -------

	def test_open_automatic_report_is_resolved_with_clear_note_on_cancel(self):
		wh, item, customer = self._new_world("AutoReport", stock_qty=None, default_material_request_type="Purchase")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		report_name = self._reports_for(so.name)[0]
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Abierto")

		so.cancel()

		report = frappe.get_doc("Reporte de Faltante", report_name)
		self.assertEqual(report.status, "Resuelto")
		self.assertIn("Orden de Venta cancelada", report.resolution_note)
		self.assertEqual(report.detected_by, "Fulfillment Engine")  # untouched otherwise

	# -- Caso 5: reporte de Bodega permanece intacto -----------------------------

	def test_bodega_report_untouched_on_cancel(self):
		wh, item, customer = self._new_world("BodegaSafe", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])

		# release the automatically-claimed stock first (delete the draft
		# Pick List manually) so the Sales Order has nothing but our own
		# hand-crafted Bodega report left when we cancel it.
		for pl_name in self._pick_lists_for(so.name):
			frappe.delete_doc("Pick List", pl_name)

		bodega_report = self.world.shortage_report(
			item_code=item.name,
			warehouse=wh.name,
			sales_order=so.name,
			qty_solicitada=8,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Producto dañado",
		)

		so.cancel()

		bodega_report.reload()
		self.assertEqual(bodega_report.status, "Abierto")
		self.assertFalse(bodega_report.resolution_note)
		self.assertEqual(bodega_report.detected_by, "Bodega")

	# -- Caso 6: Pick List submitted -- comportamiento nativo, sin bypass --------

	def test_submitted_pick_list_blocks_cancellation_natively(self):
		wh, item, customer = self._new_world("SubmittedPL", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		pick_list_name = self._pick_lists_for(so.name)[0]

		with fx.as_user(self.bodega_user):
			bodega.start_picking(pick_list_name)
			row = bodega.get_pick_list(pick_list_name)["rows"][0]
			bodega.set_picked_qty(pick_list_name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pick_list_name)  # submits -> docstatus 1

		pl = frappe.get_doc("Pick List", pick_list_name)
		self.assertEqual(pl.docstatus, 1)
		frappe.db.commit()  # fixtures + submitted Pick List survive the rollback below

		# ERPNext's own native back-link check (delete_doc.py) blocks Sales
		# Order cancellation while a *submitted* linked Pick List exists --
		# not something this commit builds, only relies on. No bypass. The
		# check runs *after* our on_cancel hook (check_no_back_links_exist()
		# comes after run_method("on_cancel") in run_post_save_methods()),
		# so docstatus=2 is already written to this same, still-open
		# transaction by the time the error fires -- roll back explicitly
		# (same simulated request-boundary rollback as Commit 16's own
		# tests) before inspecting state, exactly like a real failed
		# request would end up doing.
		with self.assertRaises(frappe.LinkExistsError):
			so.cancel()
		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 1)  # cancellation did not go through
		pl.reload()
		self.assertEqual(pl.docstatus, 1)  # untouched, still submitted

	# -- Caso 7: SO sin artefactos -> cancelación normal --------------------------

	def test_cancelling_so_without_artifacts_works_normally(self):
		wh = self.world.warehouse("FG17 NoArtifacts")
		customer = self.world.customer("FG17 NoArtifacts Customer")
		# a non-stock item -- analyze_sales_order() skips it entirely
		# (Commit 12), so process_sales_order() creates neither a Pick
		# List nor a Reporte de Faltante for it. world.item() always sets
		# is_stock_item=1, so this one is built directly; its own
		# _ensure_leaf_item_group() is reused (not duplicated) to
		# guarantee the shared test Item Group exists regardless of
		# whether any other test in this class has run first.
		self.world._ensure_leaf_item_group()
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "FG17-NONSTOCK",
				"item_name": "FG17-NONSTOCK",
				"item_group": "FG8 Test Item Group",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.world.track_existing("Item", item.name)

		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		self.assertEqual(self._pick_lists_for(so.name), [])
		self.assertEqual(self._reports_for(so.name), [])

		so.cancel()  # must not raise

		so.reload()
		self.assertEqual(so.docstatus, 2)

	# -- Caso 8: SO mixta -> limpieza completa -------------------------------------

	def test_mixed_sales_order_cleanup_is_complete(self):
		wh = self.world.warehouse("FG17 Mixed")
		customer = self.world.customer("FG17 Mixed Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		item_a = self.world.item("FG17-MIXED-A", default_material_request_type="Purchase")
		item_b = self.world.item("FG17-MIXED-B", default_material_request_type="Purchase")
		item_c = self.world.item("FG17-MIXED-C", default_material_request_type="Manufacture")
		raw_c = self.world.item("FG17-MIXED-C-RAW")
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

		pick_list_name = self._pick_lists_for(so.name)[0]
		report_names = self._reports_for(so.name)
		self.assertEqual(len(report_names), 2)  # B (Purchase) and C (Manufacture)

		so.cancel()

		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))
		for name in report_names:
			self.assertEqual(frappe.get_doc("Reporte de Faltante", name).status, "Resuelto")

	# -- Caso 9: error intencional en cleanup -> rollback de todo -----------------

	def test_intentional_error_during_cleanup_rolls_back_cancellation(self):
		wh, item, customer = self._new_world("CleanupRollback", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		pick_list_name = self._pick_lists_for(so.name)[0]
		frappe.db.commit()  # fixtures + hook-created artifacts survive the rollback below

		with patch(
			"fabergray_erp.fulfillment.cancellation_service._open_engine_reports_for",
			side_effect=RuntimeError("Commit 17 intentional cleanup failure"),
		):
			with self.assertRaises(RuntimeError):
				so.cancel()

		# What Frappe's own WSGI request handler does on an unhandled
		# exception (traced in Commit 16, identical mechanism for
		# on_cancel) -- simulated explicitly since bench run-tests never
		# goes through a real HTTP request.
		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 1)  # the cancellation itself was rolled back too
		self.assertTrue(frappe.db.exists("Pick List", pick_list_name))  # cleanup's own partial work undone

		# confirm the Sales Order is left genuinely cancellable afterward.
		so.cancel()
		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))

	# -- Caso 10: servicio ejecutado nuevamente -> idempotente -------------------

	def test_cleanup_service_run_twice_is_idempotent(self):
		wh, item, customer = self._new_world("Idempotent", stock_qty=None, default_material_request_type="Purchase")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		report_name = self._reports_for(so.name)[0]

		so.cancel()
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Resuelto")

		# calling the service directly again (e.g. a stray manual
		# reprocess, or the hook somehow firing twice) must be a no-op --
		# no error, no duplicate work, no modification.
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
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Resuelto")

	# -- Caso 11 (Commit 19.3): MR automático, exclusivo de esa SO -> se elimina --

	def test_cancellation_deletes_the_engine_created_draft_material_request_exclusive_to_this_so(self):
		"""Supersedes the Commit 19.2 test of the same scenario, which
		asserted the OLD, now-intentionally-changed behaviour ("Material
		Request completely untouched") -- Commit 19.3 implements exactly
		this cleanup, approved by the user after `fg_created_by_
		fulfillment_engine` (Custom Field) was confirmed necessary to do it
		safely. Same "a stale-but-passing test that lies about current
		behaviour is worse than a smaller count" reasoning already applied
		in Commit 17."""
		wh, item, customer = self._new_world("MrDeleted", stock_qty=2, default_material_request_type="Purchase")
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		pick_list_name = self._pick_lists_for(so.name)[0]
		report_name = self._reports_for(so.name)[0]
		mr_names = frappe.get_all(
			"Material Request Item", filters={"sales_order": so.name}, pluck="parent", distinct=True
		)
		self.assertEqual(len(mr_names), 1)
		mr_name = mr_names[0]
		self.assertEqual(
			frappe.db.get_value(
				"Material Request", mr_name, ["docstatus", "fg_created_by_fulfillment_engine"], as_dict=True
			),
			{"docstatus": 0, "fg_created_by_fulfillment_engine": 1},
		)

		so.cancel()

		# Commit 17 behaviour, unchanged: draft Pick List removed, open
		# automatic report resolved.
		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Resuelto")

		# Commit 19.3: the Engine-created draft Material Request, every
		# row of which belonged to this now-cancelled Sales Order, is
		# deleted outright -- same "draft is not cancelled, it's deleted"
		# reasoning Commit 17 already applied to Pick List.
		self.assertFalse(frappe.db.exists("Material Request", mr_name))
