# -*- coding: utf-8 -*-
"""Commit 8 -- Section 1: permission tests for Bodega and Jefe de Bodega.

All data is created fresh in setUpClass via fabergray_erp.tests.fixtures and
explicitly deleted in cleanup() (see fixtures.TestWorld) -- nothing here
depends on, or leaves behind, anything beyond the PRUEBA-* records already on
this site.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaWarehousePermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_a = cls.world.warehouse("FG8 Bodega Perm A")
		cls.wh_b = cls.world.warehouse("FG8 Bodega Perm B")
		cls.item = cls.world.item("FG8-PERM-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Perm")
		cls.world.stock_up(cls.item.name, cls.wh_a.name, 100)
		cls.world.stock_up(cls.item.name, cls.wh_b.name, 100)

		so_a = cls.world.submitted_sales_order(cls.item.name, cls.wh_a.name, 5, cls.customer.name)
		so_b = cls.world.submitted_sales_order(cls.item.name, cls.wh_b.name, 5, cls.customer.name)
		cls.pl_a = cls.world.pick_list_for(so_a, cls.wh_a.name)
		cls.pl_b = cls.world.pick_list_for(so_b, cls.wh_b.name)

		cls.bodega_user = cls.world.user("fg8-bodega-perm@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_a.name)

	def test_bodega_sees_pick_lists_from_own_warehouse(self):
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
			all_names = [pl["name"] for bucket in queue.values() for pl in bucket]
		self.assertIn(self.pl_a.name, all_names)

	def test_bodega_does_not_see_pick_lists_from_other_warehouse(self):
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
			all_names = [pl["name"] for bucket in queue.values() for pl in bucket]
		self.assertNotIn(self.pl_b.name, all_names)

	def test_bodega_cannot_read_other_warehouse_pick_list_directly(self):
		with fx.as_user(self.bodega_user):
			doc = frappe.get_doc("Pick List", self.pl_b.name)
			with self.assertRaises(frappe.PermissionError):
				doc.check_permission("read")


class TestJefeDeBodegaPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG8 Jefe Perm")
		cls.item = cls.world.item("FG8-PERM-ITEM-JEFE")
		cls.customer = cls.world.customer("FG8 Test Customer Jefe")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 5, cls.customer.name)
		cls.pl = cls.world.pick_list_for(so, cls.wh.name)

		cls.jefe_user = cls.world.user("fg8-jefe-perm@example.com", ["Jefe de Bodega"])

		cls.existing_report = cls.world.shortage_report(
			item_code=cls.item.name,
			warehouse=cls.wh.name,
			pick_list=cls.pl.name,
			qty_solicitada=5,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
		)

	def test_jefe_can_read_pick_list(self):
		with fx.as_user(self.jefe_user):
			doc = frappe.get_doc("Pick List", self.pl.name)
			doc.check_permission("read")  # must not raise

	def test_jefe_cannot_start_picking(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.start_picking(self.pl.name)

	def test_jefe_cannot_set_picked_qty(self):
		row_name = self.pl.get("locations")[0].name
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.set_picked_qty(self.pl.name, row_name, 1)

	def test_jefe_cannot_finish_picking(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.finish_picking(self.pl.name)

	def test_jefe_can_read_warehouse(self):
		with fx.as_user(self.jefe_user):
			doc = frappe.get_doc("Warehouse", self.wh.name)
			doc.check_permission("read")  # must not raise

	def test_jefe_cannot_write_warehouse(self):
		with fx.as_user(self.jefe_user):
			doc = frappe.get_doc("Warehouse", self.wh.name)
			with self.assertRaises(frappe.PermissionError):
				doc.check_permission("write")

	def test_jefe_can_read_and_update_existing_shortage_report(self):
		with fx.as_user(self.jefe_user):
			doc = frappe.get_doc("Reporte de Faltante", self.existing_report.name)
			doc.check_permission("read")
			doc.check_permission("write")
			doc.status = "En Proceso"
			doc.save()  # must not raise
		self.assertEqual(
			frappe.db.get_value("Reporte de Faltante", self.existing_report.name, "status"), "En Proceso"
		)

	def test_jefe_cannot_create_shortage_report(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Reporte de Faltante",
						"item_code": self.item.name,
						"warehouse": self.wh.name,
						"qty_solicitada": 5,
						"qty_disponible": 1,
						"detected_by": "Bodega",
						"shortage_reason": "Otro",
					}
				).insert()
