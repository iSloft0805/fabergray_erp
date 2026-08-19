# -*- coding: utf-8 -*-
"""Commit 8 -- Section 2: the full Bodega picking flow, happy path and every
documented failure case, exercised only through the public api.bodega.*
functions (never by calling Pick List methods directly) -- exactly how
/app/bodega itself uses this API.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaFlow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG8 Flow")
		cls.item_1 = cls.world.item("FG8-FLOW-ITEM-1")
		cls.item_2 = cls.world.item("FG8-FLOW-ITEM-2")
		cls.customer = cls.world.customer("FG8 Test Customer Flow")
		cls.world.stock_up(cls.item_1.name, cls.wh.name, 1000)
		cls.world.stock_up(cls.item_2.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg8-bodega-flow@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	def _two_line_pick_list(self, qty_1=10, qty_2=10):
		"""Fresh Sales Order (2 lines) -> Pick List, via the real ERPNext
		mapping, every time -- so each test starts from an untouched,
		un-started Pick List regardless of test order."""
		so = self.world.multi_item_sales_order(
			self.customer.name,
			[
				{"item_code": self.item_1.name, "warehouse": self.wh.name, "qty": qty_1, "rate": 100},
				{"item_code": self.item_2.name, "warehouse": self.wh.name, "qty": qty_2, "rate": 100},
			],
		)
		return self.world.pick_list_for(so, self.wh.name)

	def _one_line_pick_list(self, qty=10):
		so = self.world.submitted_sales_order(self.item_1.name, self.wh.name, qty, self.customer.name)
		return self.world.pick_list_for(so, self.wh.name)

	# -- Happy paths ----------------------------------------------------

	def test_full_pick_completes_and_updates_sales_order(self):
		pl = self._two_line_pick_list(qty_1=10, qty_2=10)
		sales_order = pl.get("locations")[0].sales_order
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			result = bodega.finish_picking(pl.name)

		self.assertEqual(result["docstatus"], 1)
		# per_picked is updated by Pick List's own on_submit ->
		# update_sales_order_picking_status(), the real ERPNext flow -- not
		# recomputed by us. See Section 5 regression tests too.
		self.assertEqual(flt(frappe.db.get_value("Sales Order", sales_order, "per_picked")), 100.0)

	def test_partial_pick_with_reported_shortage_completes(self):
		pl = self._two_line_pick_list(qty_1=10, qty_2=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			rows = bodega.get_pick_list(pl.name)["rows"]
			bodega.set_picked_qty(pl.name, rows[0]["row_name"], rows[0]["qty_solicitada"])
			bodega.set_picked_qty(pl.name, rows[1]["row_name"], 4)  # short of 10
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=rows[1]["row_name"],
				qty_disponible=4,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			result = bodega.finish_picking(pl.name)

		self.assertEqual(result["docstatus"], 1)

	# -- Failure cases ----------------------------------------------------

	def test_zero_picked_row_blocks_finish_even_if_reported(self):
		pl = self._two_line_pick_list(qty_1=10, qty_2=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			rows = bodega.get_pick_list(pl.name)["rows"]
			bodega.set_picked_qty(pl.name, rows[0]["row_name"], rows[0]["qty_solicitada"])
			# row 2 left at 0, but disclosed via a shortage report
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=rows[1]["row_name"],
				qty_disponible=0,
				shortage_reason="Stock físico no encontrado",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			with self.assertRaises(frappe.ValidationError):
				bodega.finish_picking(pl.name)

	def test_undisclosed_shortfall_blocks_finish(self):
		pl = self._two_line_pick_list(qty_1=10, qty_2=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			rows = bodega.get_pick_list(pl.name)["rows"]
			bodega.set_picked_qty(pl.name, rows[0]["row_name"], rows[0]["qty_solicitada"])
			bodega.set_picked_qty(pl.name, rows[1]["row_name"], 5)  # short, no report filed
			with self.assertRaises(frappe.ValidationError):
				bodega.finish_picking(pl.name)

	def test_negative_picked_qty_rejected(self):
		pl = self._one_line_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, -1)

	def test_picked_qty_over_tolerance_rejected(self):
		allowance_pct = 100 + flt(
			frappe.get_single_value("Stock Settings", "over_delivery_receipt_allowance")
		)
		pl = self._one_line_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			over_qty = (allowance_pct / 100.0) * 10 + 1  # 1 unit past the allowed ceiling
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, over_qty)

	def test_row_from_another_pick_list_is_rejected(self):
		pl_1 = self._one_line_pick_list(qty=10)
		pl_2 = self._one_line_pick_list(qty=10)
		foreign_row_name = pl_2.get("locations")[0].name
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl_1.name)
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl_1.name, foreign_row_name, 1)
			with self.assertRaises(frappe.ValidationError):
				bodega.report_shortage(
					pick_list=pl_1.name,
					row_name=foreign_row_name,
					qty_disponible=1,
					shortage_reason="Otro",
				)

	def test_starting_twice_is_rejected(self):
		pl = self._one_line_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			with self.assertRaises(frappe.ValidationError):
				bodega.start_picking(pl.name)

	# -- Concurrency / stale document --------------------------------------

	def test_two_concurrent_loads_second_save_hits_native_optimistic_lock(self):
		"""Real, unmocked proof that Pick List participates in Frappe's
		native optimistic locking: two independently loaded copies of the
		same document (simulating two users/sessions), the first save wins,
		the second raises TimestampMismatchError -- exactly the condition
		api.bodega.* catches and re-raises with a Spanish message."""
		pl = self._one_line_pick_list(qty=10)
		copy_1 = frappe.get_doc("Pick List", pl.name)
		copy_2 = frappe.get_doc("Pick List", pl.name)

		copy_1.get("locations")[0].picked_qty = 1
		copy_1.pick_manually = 1
		copy_1.save()

		copy_2.get("locations")[0].picked_qty = 2
		copy_2.pick_manually = 1
		with self.assertRaises(frappe.exceptions.TimestampMismatchError):
			copy_2.save()

	def test_start_picking_translates_timestamp_mismatch(self):
		"""api.bodega.start_picking() must catch TimestampMismatchError and
		re-throw its own Spanish message rather than letting the raw
		framework exception surface. Pick List.save() is patched to raise it
		deterministically -- see module docstring: this only patches an
		object in the running test process, it does not modify any
		frappe/erpnext source file."""
		pl = self._one_line_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			frappe.clear_messages()
			with patch(
				"frappe.model.document.Document.save",
				side_effect=frappe.exceptions.TimestampMismatchError,
			):
				with self.assertRaises(frappe.exceptions.TimestampMismatchError):
					bodega.start_picking(pl.name)
			# frappe.throw(msg, exc=SomeErrorClass) raises a bare instance of
			# that class (empty str()); the actual message it shows the user
			# lives in the message log, not in the exception object itself --
			# assert on that instead of str(exception).
			logged = [m.get("message", "") for m in frappe.message_log]
			self.assertTrue(any("Otro usuario" in m for m in logged))
