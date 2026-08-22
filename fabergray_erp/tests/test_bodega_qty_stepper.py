# -*- coding: utf-8 -*-
"""Regression coverage for the Bodega qty-stepper coalescing fix.

The fix itself lives entirely in page/bodega/bodega.js (the frontend now
coalesces a burst of +/- clicks per row into at most one in-flight
set_picked_qty request, chasing the latest desired value instead of
dropping clicks) -- there is no JS test runner set up in this app, so this
file only proves the *backend* contract the new frontend logic depends on:
set_picked_qty() still takes plain absolute values (never deltas) and can
be called repeatedly, including with values that skip over intermediate
numbers (exactly what coalescing does -- e.g. 0 -> 5 directly instead of
0,1,2,3,4,5), while still enforcing the negative/over-tolerance guards and
persisting exactly what was last accepted. Every call goes only through the
public fabergray_erp.api.bodega functions, the same way the page itself
does.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaQtyStepperCoalescing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG8 Stepper")
		cls.item = cls.world.item("FG8-STEPPER-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Stepper")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg8-bodega-stepper@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	def _pick_list(self, qty=10):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name)
		return self.world.pick_list_for(so, self.wh.name)

	def test_coalesced_jump_skips_intermediate_values(self):
		"""Mirrors what the frontend now does for a burst of 5 rapid clicks:
		one request straight from 0 to 5, not five separate +1 requests."""
		pl = self._pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]

			result = bodega.set_picked_qty(pl.name, row_name, 5)
			self.assertEqual(flt(result["picked_qty"]), 5.0)

			result = bodega.set_picked_qty(pl.name, row_name, 10)
			self.assertEqual(flt(result["picked_qty"]), 10.0)

			result = bodega.set_picked_qty(pl.name, row_name, 7)
			self.assertEqual(flt(result["picked_qty"]), 7.0)

	def test_repeated_absolute_calls_never_accumulate(self):
		"""set_picked_qty always takes the qty it's given at face value --
		calling it twice with the same value must not double-apply anything
		(this is what makes it safe for sync_row() to resend the last
		desired value after a server round-trip without any extra state)."""
		pl = self._pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]

			bodega.set_picked_qty(pl.name, row_name, 3)
			result = bodega.set_picked_qty(pl.name, row_name, 3)
			self.assertEqual(flt(result["picked_qty"]), 3.0)

	def test_over_limit_jump_still_rejected_and_state_unchanged(self):
		"""A coalesced jump straight past the ceiling (e.g. desired_qty went
		0 -> 11 while a request was in flight) must be rejected exactly like
		a single click would be, and must not leave a partial write behind."""
		pl = self._pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]

			bodega.set_picked_qty(pl.name, row_name, 6)
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, 11)

			# rejected jump must not have touched the last accepted value
			row = bodega.get_pick_list(pl.name)["rows"][0]
			self.assertEqual(flt(row["qty_alistada"]), 6.0)

	def test_negative_coalesced_value_still_rejected(self):
		pl = self._pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, -1)

	def test_value_persists_across_reload(self):
		"""'recargar el detalle -> el valor persiste' from the manual test
		plan: get_pick_list() after a set_picked_qty() must reflect exactly
		what was accepted, independent of any client-side optimistic state."""
		pl = self._pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]

			bodega.set_picked_qty(pl.name, row_name, 7)
			reloaded = bodega.get_pick_list(pl.name)["rows"][0]
			self.assertEqual(flt(reloaded["qty_alistada"]), 7.0)

	def test_two_rows_sync_independently(self):
		"""The lock in the frontend is per row_name -- confirm the backend
		has no shared/global state that would make touching one row affect
		another (two different products, two different pick lists)."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name)
		item_2 = self.world.item("FG8-STEPPER-ITEM-2")
		self.world.stock_up(item_2.name, self.wh.name, 1000)
		so_2 = self.world.submitted_sales_order(item_2.name, self.wh.name, 10, self.customer.name)

		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_1 = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_1, 4)

		pl_2 = self.world.pick_list_for(so_2, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_2.name)
			row_2 = bodega.get_pick_list(pl_2.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl_2.name, row_2, 9)

			row_1_state = bodega.get_pick_list(pl.name)["rows"][0]
			row_2_state = bodega.get_pick_list(pl_2.name)["rows"][0]
		self.assertEqual(flt(row_1_state["qty_alistada"]), 4.0)
		self.assertEqual(flt(row_2_state["qty_alistada"]), 9.0)
