# -*- coding: utf-8 -*-
"""Commit 21.2 -- read-only API tests for api/facturacion.py.

Every scenario from the approved brief's "Tests mínimos" list, plus a few
structural sanity checks (shape of the responses, that nothing here writes
anything). No generate_invoice(), no Page, no Custom Field, no hook --
none of that exists yet; this file only exercises the three read endpoints
this commit actually adds.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock.doctype.pick_list.pick_list import create_delivery
from erpnext.stock.utils import get_bin

from fabergray_erp.api import bodega, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VALID_INCOME_ACCOUNT = "413520 - Venta de productos en almacenes no especializados - FG"


class TestFacturacionApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG212 API WH")
		cls.item = cls.world.item("FG212-API-ITEM")
		cls.customer = cls.world.customer("FG212 API Customer")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg212-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg212-facturacion@example.com", ["Facturación"])
		cls.no_role_user = cls.world.user("fg212-norole@example.com", [])
		cls.vendedora_user = cls.world.user("fg212-vendedora@example.com", ["Vendedora"])

	# -- Shared setup helpers ------------------------------------------------

	def _submitted_pick_list(self, qty, rate=100, so_customer=None, so_item=None, so_wh=None):
		so = self.world.submitted_sales_order(
			so_item or self.item.name,
			so_wh or self.wh.name,
			qty,
			so_customer or self.customer.name,
			rate=rate,
		)
		pl = self.world.pick_list_for(so, so_wh or self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _invoice_fully(self, pl):
		"""Real native flow (Commit 21.1) -- invoices the whole Pick List so
		its delivery_status becomes Fully Delivered."""
		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			si = create_delivery(pl.name, target="Sales Invoice")
			self.world.track_existing("Sales Invoice", si.name)
			si.submit()
		return si

	def _amended_sales_order(self, qty, rate=100):
		"""A real, submitted, once-amended Sales Order -- PEDIDO-N cancelled
		and re-submitted as PEDIDO-N-1, before any Pick List exists for it,
		same native amend mechanism api.ventas.py's own modification
		endpoint uses (frappe.copy_doc(..., ignore_no_copy=False) ->
		amended_from set explicitly -> insert() -> submit())."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		original_name = so.name

		so.cancel()
		amended = frappe.copy_doc(so, ignore_no_copy=False)
		amended.docstatus = 0
		amended.amended_from = original_name
		# delivery_date is no_copy=1 on both Sales Order and Sales Order
		# Item (confirmed live), so copy_doc(ignore_no_copy=False) clears
		# it -- must be re-set here, same as api.ventas.py's own amend
		# endpoint does via _validate_and_build_item_rows().
		new_delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		amended.delivery_date = new_delivery_date
		for item in amended.items:
			item.delivery_date = new_delivery_date
		amended.insert()
		with fx.without_sales_order_hook():
			amended.submit()
		self.world.track_existing("Sales Order", amended.name)
		return amended, original_name

	# -- get_pending_pick_lists() --------------------------------------------

	def test_not_delivered_pick_list_appears(self):
		_, pl = self._submitted_pick_list(qty=3)
		with fx.as_user(self.facturacion_user):
			names = [row["name"] for row in facturacion.get_pending_pick_lists()]
		self.assertIn(pl.name, names)

	def test_partly_delivered_pick_list_appears(self):
		so, pl = self._submitted_pick_list(qty=10, rate=100)
		# Invoice only part of it -- reduce what's picked to leave a
		# remainder by invoicing less than the full picked_qty via a manual
		# partial Sales Invoice built the same way create_delivery() would,
		# is unnecessary here: cancelling after a full picked_qty=10 pick,
		# simplest real way to reach Partly Delivered without a second
		# native endpoint is to invoice the Pick List, then invoice a
		# second Pick List against the remainder of the SAME order -- but
		# the simplest, most direct real reproduction is to set picked_qty
		# short of stock_qty from the start. Rebuild with a short pick.
		so2 = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name, rate=100)
		pl2 = self.world.pick_list_for(so2, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl2.name)
			rows = bodega.get_pick_list(pl2.name)["rows"]
			bodega.set_picked_qty(pl2.name, rows[0]["row_name"], 10)
			bodega.finish_picking(pl2.name)

		pl2 = frappe.get_doc("Pick List", pl2.name)
		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			# Invoice with a client-supplied target_doc whose item qty is
			# reduced below the full picked_qty, so the Pick List ends up
			# Partly Delivered, not Fully Delivered -- create_delivery()
			# itself always proposes the full remaining qty, so the partial
			# amount is applied the same way a human editing the invoice
			# draft before submitting it would.
			si = create_delivery(pl2.name, target="Sales Invoice")
			si.items[0].qty = 4
			si.save()
			self.world.track_existing("Sales Invoice", si.name)
			si.submit()

		pl2_after = frappe.get_doc("Pick List", pl2.name)
		self.assertEqual(pl2_after.delivery_status, "Partly Delivered")

		with fx.as_user(self.facturacion_user):
			names = [row["name"] for row in facturacion.get_pending_pick_lists()]
		self.assertIn(pl2_after.name, names)

	def test_fully_delivered_pick_list_does_not_appear(self):
		_, pl = self._submitted_pick_list(qty=2, rate=100)
		self._invoice_fully(pl)
		pl_after = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_after.delivery_status, "Fully Delivered")

		with fx.as_user(self.facturacion_user):
			names = [row["name"] for row in facturacion.get_pending_pick_lists()]
		self.assertNotIn(pl.name, names)

	def test_draft_pick_list_does_not_appear(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 3, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)  # never picked/submitted -- docstatus 0
		with fx.as_user(self.facturacion_user):
			names = [row["name"] for row in facturacion.get_pending_pick_lists()]
		self.assertNotIn(pl.name, names)

	def test_two_pick_lists_of_same_sales_order_appear_separately(self):
		"""Two distinct Pick Lists both referencing the same Sales Order --
		a short pick (2 of 3, honestly disclosed via report_shortage, same
		as bodega.finish_picking()'s own required flow) followed by a
		second, later picking round for the remaining unit once it's
		available -- must both show up, as two distinct rows."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 3, self.customer.name, rate=100)

		pl_a = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_a.name)
			row_a = bodega.get_pick_list(pl_a.name)["rows"][0]
			bodega.set_picked_qty(pl_a.name, row_a["row_name"], 2)
			report = bodega.report_shortage(
				pick_list=pl_a.name,
				row_name=row_a["row_name"],
				qty_disponible=2,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl_a.name)

		# Native create_pick_list() (via pick_list_for()) now proposes only
		# the 1 unit still remaining on the Sales Order Item.
		pl_b = self.world.pick_list_for(so, self.wh.name)
		self.assertEqual(flt(pl_b.locations[0].stock_qty), 1)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_b.name)
			row_b = bodega.get_pick_list(pl_b.name)["rows"][0]
			bodega.set_picked_qty(pl_b.name, row_b["row_name"], row_b["qty_solicitada"])
			bodega.finish_picking(pl_b.name)

		with fx.as_user(self.facturacion_user):
			names = [row["name"] for row in facturacion.get_pending_pick_lists()]
		self.assertIn(pl_a.name, names)
		self.assertIn(pl_b.name, names)
		self.assertNotEqual(pl_a.name, pl_b.name)

	def test_commercial_name_correct_with_amended_sales_order(self):
		amended, original_name = self._amended_sales_order(qty=4)
		self.assertNotEqual(amended.name, original_name)  # e.g. PEDIDO-N-1 != PEDIDO-N

		pl = self.world.pick_list_for(amended, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)

		with fx.as_user(self.facturacion_user):
			entries = {row["name"]: row for row in facturacion.get_pending_pick_lists()}
		self.assertEqual(entries[pl.name]["sales_order"], amended.name)
		self.assertEqual(entries[pl.name]["commercial_name"], original_name)

	def test_open_shortage_marks_has_open_shortage(self):
		so, pl = self._submitted_pick_list(qty=5, rate=100)
		# picked fully above, so add a real Reporte de Faltante against it
		# directly, mirroring how Bodega would report a physical shortfall
		# on a partially-short pick -- what matters here is only the
		# report's presence/status, not how it was created.
		report = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			pick_list=pl.name,
			pick_list_item=pl.locations[0].name,
			sales_order=so.name,
			qty_solicitada=5,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
			status="Abierto",
		)

		with fx.as_user(self.facturacion_user):
			entries = {row["name"]: row for row in facturacion.get_pending_pick_lists()}
			summary = facturacion.get_facturacion_summary()
		self.assertTrue(entries[pl.name]["has_open_shortage"])
		self.assertGreaterEqual(summary["con_incidencia"], 1)

		frappe.db.set_value("Reporte de Faltante", report.name, "status", "Resuelto")

		with fx.as_user(self.facturacion_user):
			entries_after = {row["name"]: row for row in facturacion.get_pending_pick_lists()}
		self.assertFalse(entries_after[pl.name]["has_open_shortage"])

	def test_resolved_shortage_does_not_count(self):
		so, pl = self._submitted_pick_list(qty=5, rate=100)
		self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			pick_list=pl.name,
			pick_list_item=pl.locations[0].name,
			sales_order=so.name,
			qty_solicitada=5,
			qty_disponible=5,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
			status="Resuelto",
		)
		with fx.as_user(self.facturacion_user):
			entries = {row["name"]: row for row in facturacion.get_pending_pick_lists()}
		self.assertFalse(entries[pl.name]["has_open_shortage"])

	# -- get_pick_list_for_facturacion() -------------------------------------

	def test_qty_to_invoice_is_exact_and_stock_is_purely_informative(self):
		_, pl = self._submitted_pick_list(qty=7, rate=150)

		# Tamper with actual stock AFTER picking -- must never change
		# qty_to_invoice, only the informative actual_qty field. self.wh/
		# self.item are shared across every test in this class, so the
		# tampered value is restored (stock_up_real, real SLE again) before
		# this test returns -- otherwise every later test reusing them
		# would see a permanently-zeroed Bin and fail to build a Pick List
		# at all ("no tiene líneas para alistar").
		bin_doc = get_bin(self.item.name, self.wh.name)
		bin_doc.actual_qty = 0
		bin_doc.save()
		try:
			with fx.as_user(self.facturacion_user):
				detail = facturacion.get_pick_list_for_facturacion(pl.name)

			row = detail["rows"][0]
			self.assertEqual(flt(row["picked_qty"]), 7)
			self.assertEqual(flt(row["delivered_qty"]), 0)
			self.assertEqual(flt(row["qty_to_invoice"]), 7)  # unaffected by actual_qty
			self.assertEqual(flt(row["actual_qty"]), 0)  # reflects the tampered stock, informative only
			self.assertEqual(flt(row["rate"]), 150)
			self.assertEqual(flt(row["amount"]), 7 * 150)
		finally:
			self.world.stock_up_real(self.item.name, self.wh.name, 1000, rate=50)

	def test_rate_preserved_from_sales_order_item(self):
		_, pl = self._submitted_pick_list(qty=3, rate=333)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_pick_list_for_facturacion(pl.name)
		self.assertEqual(flt(detail["rows"][0]["rate"]), 333)

	def test_multi_sales_order_rejected_in_detail(self):
		so_1 = self.world.submitted_sales_order(self.item.name, self.wh.name, 3, self.customer.name)
		so_2 = self.world.submitted_sales_order(self.item.name, self.wh.name, 4, self.customer.name)

		pl = frappe.get_doc(
			{
				"doctype": "Pick List",
				"company": fx.COMPANY,
				"purpose": "Delivery",
				"parent_warehouse": self.wh.name,
				"pick_manually": 1,
				"locations": [
					{
						"item_code": self.item.name,
						"warehouse": self.wh.name,
						"qty": 3,
						"stock_qty": 3,
						"conversion_factor": 1,
						"sales_order": so_1.name,
						"sales_order_item": so_1.items[0].name,
						"picked_qty": 3,
					},
					{
						"item_code": self.item.name,
						"warehouse": self.wh.name,
						"qty": 4,
						"stock_qty": 4,
						"conversion_factor": 1,
						"sales_order": so_2.name,
						"sales_order_item": so_2.items[0].name,
						"picked_qty": 4,
					},
				],
			}
		)
		pl.insert()
		self.world.track_existing("Pick List", pl.name)
		pl.submit()

		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError):
				facturacion.get_pick_list_for_facturacion(pl.name)

	def test_draft_pick_list_rejected_in_detail(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 2, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)  # never submitted
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError):
				facturacion.get_pick_list_for_facturacion(pl.name)

	def test_fully_delivered_pick_list_rejected_in_detail(self):
		_, pl = self._submitted_pick_list(qty=2, rate=100)
		self._invoice_fully(pl)
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError):
				facturacion.get_pick_list_for_facturacion(pl.name)

	def test_detail_never_mutates_the_pick_list(self):
		_, pl = self._submitted_pick_list(qty=5, rate=100)
		before = frappe.get_value("Pick List", pl.name, "modified")
		with fx.as_user(self.facturacion_user):
			facturacion.get_pick_list_for_facturacion(pl.name)
		after = frappe.get_value("Pick List", pl.name, "modified")
		self.assertEqual(before, after)

	# -- Permissions -----------------------------------------------------------

	def test_facturacion_user_can_read_all_three_endpoints(self):
		_, pl = self._submitted_pick_list(qty=1, rate=100)
		with fx.as_user(self.facturacion_user):
			facturacion.get_facturacion_summary()
			facturacion.get_pending_pick_lists()
			facturacion.get_pick_list_for_facturacion(pl.name)  # must not raise

	def test_user_without_facturacion_role_is_blocked(self):
		_, pl = self._submitted_pick_list(qty=1, rate=100)
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_pending_pick_lists()
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_pick_list_for_facturacion(pl.name)

	def test_vendedora_user_blocked_from_facturacion_detail_by_real_pick_list_permission(self):
		"""Vendedora has plenty of other real permissions (Sales Order,
		Customer, Item...) but deliberately zero on Pick List (Commit
		18.1's own design point) -- so calling
		get_pick_list_for_facturacion() must fail at its very first
		check_permission("read") on Pick List, a genuine permission gap,
		not an artificial role check nor the "no role at all" case
		test_user_without_facturacion_role_is_blocked already covers."""
		_, pl = self._submitted_pick_list(qty=1, rate=100)
		with fx.as_user(self.vendedora_user):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Pick List", pl.name).check_permission("read")
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_pick_list_for_facturacion(pl.name)
