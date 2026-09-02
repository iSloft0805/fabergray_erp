# -*- coding: utf-8 -*-
"""Commit 25.9 -- regression tests for the real Bodega bug:

    "Insuficiente Stock
    En la fila #1: La cantidad seleccionada 1.0 para el artículo 01460
    es mayor que el stock disponible 0.0 en el almacén
    Productos terminados - FG."

`picked_qty` represents the quantity Bodega physically found and set aside
-- it must be bounded by what was REQUESTED on the Pick List row
(`stock_qty`), never by the ERP's own live `Bin.actual_qty`, which this
site's opening inventory frequently leaves at 0 even for items that
physically exist. See `fulfillment/pick_list_mixin.py`'s own module
docstring for the full incident writeup and why the fix lives there
(`extend_doctype_class`, never a global negative-stock bypass, never a
Bin/Stock Ledger write).

Sales Orders here are submitted directly (not through
`TestWorld.multi_item_sales_order()`, which deliberately suppresses
`Sales Order.on_submit()` -- see fixtures.py's own
`without_sales_order_hook()` docstring) -- same "build a draft, submit it
for real" pattern test_sales_order_hook.py already establishes, because
this suite specifically needs the REAL production Pick List (built by
`fulfillment.pick_list_service.create_pick_list_for_full_demand()` via the
real `on_submit` hook), not the plain native-mapper Pick List
`TestWorld.pick_list_for()` builds for the other Bodega test files (which
never exercises a genuinely zero-stock line -- confirmed empirically while
writing this suite: ERPNext's own native `create_pick_list()` mapper
produces a Pick List with ZERO rows for a zero-stock item, which is
exactly the "silently dropped" failure mode this app's own Commit 25.4
full-demand Pick List exists to prevent).
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment import pick_list_mixin
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestBodegaPhysicalPicking(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		# Deliberately left at 0 stock -- world.stock_up()/stock_up_real() is
		# never called for this pair -- this is exactly the real-world
		# scenario the bug report describes (item 01460, 0.0 actual_qty in
		# "Productos terminados - FG").
		cls.wh_zero = cls.world.warehouse("FG259 Zero Stock")
		cls.item_zero = cls.world.item("FG259-ZERO-STOCK-ITEM")

		cls.wh_stocked = cls.world.warehouse("FG259 Stocked")
		cls.item_stocked = cls.world.item("FG259-STOCKED-ITEM")
		cls.world.stock_up_real(cls.item_stocked.name, cls.wh_stocked.name, 100)

		cls.customer = cls.world.customer("FG259 Physical Picking Customer")
		cls.bodega_user = cls.world.user("fg259-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_zero.name)
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_stocked.name)

	# -- Sales Order submitted directly (real on_submit hook fires) --------

	def _draft_sales_order(self, items):
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
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

	def _submit_via_hook(self, items):
		doc = self._draft_sales_order(items)
		doc.submit()  # real on_submit -> process_sales_order_for_confirmation() -> create_pick_list_for_full_demand()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		return doc

	def _pick_list_for(self, sales_order_name):
		names = frappe.get_all(
			"Pick List Item",
			filters={"sales_order": sales_order_name, "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(len(names), 1, "expected exactly one Pick List for this Sales Order")
		return frappe.get_doc("Pick List", names[0])

	def _zero_stock_pick_list(self, qty=10):
		so = self._submit_via_hook([{"item_code": self.item_zero.name, "warehouse": self.wh_zero.name, "qty": qty, "rate": 100}])
		return self._pick_list_for(so.name)

	def _stocked_pick_list(self, qty=10):
		so = self._submit_via_hook(
			[{"item_code": self.item_stocked.name, "warehouse": self.wh_stocked.name, "qty": qty, "rate": 100}]
		)
		return self._pick_list_for(so.name)

	# A/B/C. requested=10, stock=0, picked=1/7/10 -> permitido
	def test_a_picked_1_with_zero_stock_is_allowed(self):
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			result = bodega.set_picked_qty(pl.name, row_name, 1)
		self.assertEqual(flt(result["picked_qty"]), 1.0)

	def test_b_picked_7_with_zero_stock_is_allowed(self):
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			result = bodega.set_picked_qty(pl.name, row_name, 7)
		self.assertEqual(flt(result["picked_qty"]), 7.0)

	def test_c_picked_10_with_zero_stock_is_allowed(self):
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			result = bodega.set_picked_qty(pl.name, row_name, 10)
		self.assertEqual(flt(result["picked_qty"]), 10.0)

	# D. requested=10, stock=100, picked=10 -> permitido
	def test_d_picked_equals_requested_with_real_stock_is_allowed(self):
		pl = self._stocked_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			result = bodega.set_picked_qty(pl.name, row_name, 10)
		self.assertEqual(flt(result["picked_qty"]), 10.0)

	# E. requested=10, stock=100, picked=11 -> rechazado
	def test_e_picked_over_requested_is_rejected_even_with_real_stock(self):
		pl = self._stocked_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, 11)

	# F. requested=10, picked=-1 -> rechazado
	def test_f_negative_picked_is_rejected(self):
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			with self.assertRaises(frappe.ValidationError):
				bodega.set_picked_qty(pl.name, row_name, -1)

	# G. requested=10, picked=7 -> faltante calculable = 3
	def test_g_shortfall_is_calculable_after_partial_pick(self):
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 7)
			row = bodega.get_pick_list(pl.name)["rows"][0]
		self.assertEqual(flt(row["qty_solicitada"]) - flt(row["qty_alistada"]), 3.0)

	# H. stock=0 -> la línea del Pick List sigue existiendo (full-demand,
	# nunca recortada por stock -- decisión arquitectónica preservada,
	# Commit 25.4)
	def test_h_pick_list_line_exists_even_with_zero_stock(self):
		pl = self._zero_stock_pick_list(qty=10)
		self.assertEqual(len(pl.get("locations")), 1)
		self.assertEqual(flt(pl.get("locations")[0].stock_qty), 10.0)
		self.assertEqual(pl.get("locations")[0].item_code, self.item_zero.name)

	# I. no escritura directa de Bin
	def test_i_picking_never_writes_bin_actual_qty(self):
		pl = self._zero_stock_pick_list(qty=10)
		filters = {"item_code": self.item_zero.name, "warehouse": self.wh_zero.name}
		before = flt(frappe.db.get_value("Bin", filters, "actual_qty"))
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 7)
		after = flt(frappe.db.get_value("Bin", filters, "actual_qty"))
		self.assertEqual(before, after)
		self.assertEqual(after, 0.0)

	# J. no Stock Entry automático
	def test_j_picking_never_creates_a_stock_entry(self):
		pl = self._zero_stock_pick_list(qty=10)
		before = frappe.db.count("Stock Entry")
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 7)
		after = frappe.db.count("Stock Entry")
		self.assertEqual(before, after)

	# K. no Material Request automático
	def test_k_picking_never_creates_a_material_request(self):
		pl = self._zero_stock_pick_list(qty=10)
		before = frappe.db.count("Material Request")
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 7)
		after = frappe.db.count("Material Request")
		self.assertEqual(before, after)

	# L. no bypass global de negative stock
	def test_l_no_global_negative_stock_bypass_introduced(self):
		"""Static guardrail: the fix must never touch
		Stock Settings.allow_negative_stock or any frappe.flags global --
		it is scoped to Pick List's own validate_stock_qty() only. Checked
		against the MIXIN CLASS's own source only (not the module docstring,
		which explains, in prose, exactly why those mechanisms were NOT
		used -- and therefore legitimately names them)."""
		source = inspect.getsource(pick_list_mixin.PickListPhysicalCountMixin)
		for forbidden in ("ignore_negative_stock", "allow_negative_stock", "frappe.flags"):
			self.assertNotIn(forbidden, source, f"{forbidden!r} must never appear in the mixin's own code")

	def test_finish_picking_no_longer_blocked_by_zero_erp_stock(self):
		"""End-to-end: the exact real-world scenario (0 ERP stock, physical
		count of the full requested qty) must reach a submitted Pick List,
		never "Insuficiente Stock"."""
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 10)
			result = bodega.finish_picking(pl.name)
		self.assertEqual(result["docstatus"], 1)

	def test_partial_physical_pick_with_zero_erp_stock_can_finish_with_shortage_report(self):
		"""The requested=10/stock=0/picked=7 example from the brief itself,
		carried all the way through finish_picking() via the existing
		Reporte de Faltante mechanism -- never auto-created, filed
		explicitly, exactly like every other partial-pick test in this
		suite already does."""
		pl = self._zero_stock_pick_list(qty=10)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row_name = bodega.get_pick_list(pl.name)["rows"][0]["row_name"]
			bodega.set_picked_qty(pl.name, row_name, 7)
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=row_name,
				qty_disponible=0,
				shortage_reason="Stock físico no encontrado",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			result = bodega.finish_picking(pl.name)
		self.assertEqual(result["docstatus"], 1)
