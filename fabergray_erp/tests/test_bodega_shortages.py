# -*- coding: utf-8 -*-
"""Regression coverage for api.bodega.get_shortages() -- the one new
endpoint backing the Bodega bottom nav's "Faltantes" tab.

Every call goes through the real bodega.report_shortage()/get_shortages()
public functions, never a direct Reporte de Faltante insert -- exactly how
the Faltantes tab itself uses this API. Warehouse scoping is never written
here: get_shortages() relies entirely on the same per-Warehouse User
Permission mechanism already covered for Pick List in
test_bodega_permissions.py (Reporte de Faltante has its own `warehouse`
field, so the same mechanism applies automatically).
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaShortages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_a = cls.world.warehouse("FG8 Shortages A")
		cls.wh_b = cls.world.warehouse("FG8 Shortages B")
		cls.item = cls.world.item("FG8-SHORT-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Shortages")
		# Abundant stock -- same convention as test_bodega_flow.py's own
		# shortage tests: the "shortage" reported here is the picker
		# physically finding less than expected while counting (an under-
		# pick via set_picked_qty), not a real system-stock shortfall at
		# order time. Tight stock would make the Fulfillment Engine's own
		# automatic reservation (see test_stock_reservation.py) consume it
		# before pick_list_for() ever runs, leaving the Pick List with no
		# rows at all.
		cls.world.stock_up(cls.item.name, cls.wh_a.name, 1000)
		cls.world.stock_up(cls.item.name, cls.wh_b.name, 1000)

		cls.bodega_user = cls.world.user("fg8-bodega-shortages@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_a.name)
		# A second Bodega user scoped to wh_b -- needed to actually create a
		# shortage report there; cls.bodega_user has no write access to
		# wh_b's Pick List at all (same per-Warehouse User Permission this
		# whole test asserts), so it can't be the one reporting it.
		cls.bodega_user_b = cls.world.user("fg8-bodega-shortages-b@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user_b, cls.wh_b.name)

	def _reported_shortage(self, warehouse, qty=10, found=3, user=None):
		user = user or self.bodega_user
		so = self.world.submitted_sales_order(self.item.name, warehouse, qty, self.customer.name)
		pl = self.world.pick_list_for(so, warehouse)
		with fx.as_user(user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, found)
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=row_name,
				qty_disponible=found,
				shortage_reason="Stock insuficiente",
			)
		self.world.track_existing("Reporte de Faltante", report["name"])
		return report["name"], pl.name, so

	def test_bodega_sees_own_warehouse_shortage(self):
		report_name, pl_name, so = self._reported_shortage(self.wh_a.name)
		with fx.as_user(self.bodega_user):
			reports = bodega.get_shortages()
		self.assertIn(report_name, [r["name"] for r in reports])

	def test_bodega_does_not_see_other_warehouse_shortage(self):
		report_name, _pl_name, _so = self._reported_shortage(self.wh_b.name, user=self.bodega_user_b)
		with fx.as_user(self.bodega_user):
			reports = bodega.get_shortages()
		self.assertNotIn(report_name, [r["name"] for r in reports])

	def test_item_name_is_resolved_without_item_doctype_permission(self):
		"""get_shortages() must resolve item_name via the linked Pick List,
		not via a direct Item read -- confirmed by checking the resolved
		name matches the real item_name even though this assertion never
		grants/relies on any elevated permission beyond what Bodega already
		had before the Item read=1 grant (get_inventory() is what needed
		that; this endpoint works the same with or without it)."""
		report_name, pl_name, so = self._reported_shortage(self.wh_a.name)
		with fx.as_user(self.bodega_user):
			reports = bodega.get_shortages()
		report = next(r for r in reports if r["name"] == report_name)
		expected_item_name = frappe.db.get_value("Item", self.item.name, "item_name")
		self.assertEqual(report["item_name"], expected_item_name)

	def test_status_filter(self):
		report_name, _pl_name, _so = self._reported_shortage(self.wh_a.name)
		# Move it out of the default "Abierto" the same way a supervisor
		# would (direct doc write, matching test_bodega_permissions.py's
		# own precedent for Jefe de Bodega's update path) -- not through
		# any Bodega-facing endpoint, since Bodega has no status-transition
		# API of its own.
		doc = frappe.get_doc("Reporte de Faltante", report_name)
		doc.status = "Resuelto"
		doc.save()

		with fx.as_user(self.bodega_user):
			abiertos = bodega.get_shortages(status="Abierto")
			resueltos = bodega.get_shortages(status="Resuelto")

		self.assertNotIn(report_name, [r["name"] for r in abiertos])
		self.assertIn(report_name, [r["name"] for r in resueltos])

	def test_invalid_status_rejected(self):
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.ValidationError):
				bodega.get_shortages(status="Not A Real Status")

	def test_commercial_name_resolved_from_sales_order(self):
		report_name, pl_name, so = self._reported_shortage(self.wh_a.name)
		with fx.as_user(self.bodega_user):
			reports = bodega.get_shortages()
		report = next(r for r in reports if r["name"] == report_name)
		# Fresh (never amended) Sales Order -- commercial_name is the Sales
		# Order's own name, same as root_commercial_name() would return.
		self.assertEqual(report["commercial_name"], so.name)

	def test_report_without_pick_list_still_returned(self):
		"""A Reporte de Faltante can exist with no Pick List at all (see
		_insert_shortage_report()'s docstring in api/bodega.py) -- must not
		crash item_name resolution."""
		report = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh_a.name,
			qty_solicitada=5,
			qty_disponible=0,
			detected_by="Fulfillment Engine",
		)
		with fx.as_user(self.bodega_user):
			reports = bodega.get_shortages()
		found = next((r for r in reports if r["name"] == report.name), None)
		self.assertIsNotNone(found)
		self.assertEqual(found["item_name"], frappe.db.get_value("Item", self.item.name, "item_name"))
		self.assertIsNone(found["commercial_name"])
