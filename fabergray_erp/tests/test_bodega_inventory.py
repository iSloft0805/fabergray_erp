# -*- coding: utf-8 -*-
"""Regression coverage for api.bodega.get_inventory() -- the read-only
"Inventario" tab under Más, and the one new permission it required
(Custom DocPerm: Bodega -> Item, read=1 only -- see
fabergray_erp/fixtures/custom_docperm.json).

Confirms: Bodega only sees Bin rows for warehouses it has a User
Permission on (same mechanism already covered for Pick List/Reporte de
Faltante), the endpoint is genuinely read-only (no write permission is
ever granted or used), and a user with no Item permission at all is
correctly blocked, proving the grant is what makes this endpoint work,
not an accidental permission bypass. That last check uses a disposable,
purpose-built role rather than a real one (Commit 22.4 gave "Jefe de
Bodega" its own, unrelated Item read=1 for api/inventario.py, so it can
no longer serve as a "definitely no Item permission" example).
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaInventory(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_a = cls.world.warehouse("FG8 Inventory A")
		cls.wh_b = cls.world.warehouse("FG8 Inventory B")
		cls.item_a = cls.world.item("FG8-INV-ITEM-A")
		cls.item_b = cls.world.item("FG8-INV-ITEM-B")
		cls.world.stock_up(cls.item_a.name, cls.wh_a.name, 42)
		cls.world.stock_up(cls.item_b.name, cls.wh_b.name, 17)

		cls.bodega_user = cls.world.user("fg8-bodega-inventory@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_a.name)

	def test_bodega_sees_own_warehouse_stock(self):
		with fx.as_user(self.bodega_user):
			rows = bodega.get_inventory()
		row = next((r for r in rows if r["item_code"] == self.item_a.name and r["warehouse"] == self.wh_a.name), None)
		self.assertIsNotNone(row)
		self.assertEqual(row["actual_qty"], 42.0)
		self.assertEqual(row["item_name"], frappe.db.get_value("Item", self.item_a.name, "item_name"))

	def test_bodega_does_not_see_other_warehouse_stock(self):
		with fx.as_user(self.bodega_user):
			rows = bodega.get_inventory()
		self.assertFalse(any(r["warehouse"] == self.wh_b.name for r in rows))

	def test_available_qty_is_actual_minus_reserved(self):
		with fx.as_user(self.bodega_user):
			rows = bodega.get_inventory()
		row = next(r for r in rows if r["item_code"] == self.item_a.name and r["warehouse"] == self.wh_a.name)
		self.assertEqual(row["available_qty"], row["actual_qty"] - row["reserved_qty"])

	def test_role_without_item_permission_is_rejected(self):
		"""A role with zero grants on Item at all must still be blocked --
		proves get_inventory() is actually enforcing
		frappe.has_permission("Item", ...), not silently bypassing it.

		Uses a disposable, purpose-built role (not "Jefe de Bodega" --
		Commit 22.4 gave that role its own, legitimate Item read=1 for
		api/inventario.py, so it stopped being a valid "no Item
		permission" example) so this test's premise can never again be
		invalidated by an unrelated, correctly-approved permission grant
		to one of this app's real business roles."""
		role = frappe.get_doc({"doctype": "Role", "role_name": "FG8 No Item Permission Test Role", "desk_access": 1})
		role.insert()
		self.world.track_existing("Role", role.name)

		no_item_user = self.world.user("fg8-noitem-inventory@example.com", [role.name])
		self.world.warehouse_user_permission(no_item_user, self.wh_a.name)
		with fx.as_user(no_item_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.get_inventory()
