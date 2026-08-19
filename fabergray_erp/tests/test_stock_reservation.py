# -*- coding: utf-8 -*-
"""Commit 10 -- controlled validation of ERPNext 16's native Stock
Reservation (Stock Reservation Entry), before any Fulfillment Engine code.

Nothing here is application code: no hooks, no Custom Fields, no engine
logic. This suite only exercises ERPNext's own native functions
(Sales Order.create_stock_reservation_entries(), the Stock Settings toggles,
erpnext.selling.doctype.sales_order.sales_order.create_pick_list) against
self-contained fixtures, to document exactly how they behave in this
installed version (16.32.1) before deciding how the future engine should use
them. See the Commit 10 report for the full writeup and the final A/B/C
recommendation.

Two scenarios, tested separately, per the explicit instruction not to assume
which one -- or a combination -- is right without evidence:
- TestStockReservationManual (Escenario B): enable_stock_reservation=1,
  auto_reserve_stock=0 -- reservation only happens via an explicit call to
  Sales Order.create_stock_reservation_entries(). Casos 1-6 all run here.
- TestStockReservationAutomatic (Escenario A): auto_reserve_stock=1 --
  Sales Order.on_submit() reserves with no explicit call. Only enough tests
  to confirm this native behaviour precisely and that Caso 6's finding is
  identical under it.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


def _get_available_qty_to_reserve(item_code, warehouse):
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	return get_available_qty_to_reserve(item_code, warehouse)


def _create_pick_list_unsaved(sales_order_name):
	from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

	return create_pick_list(sales_order_name)


def _reserve_and_track(world, so):
	"""Call the native Sales Order.create_stock_reservation_entries() and
	register whatever Stock Reservation Entry it created with `world` for
	cleanup -- create_stock_reservation_entries() returns None, and deleting
	a Sales Order does NOT cascade-delete its (separate, standalone) Stock
	Reservation Entry documents, so without this every test that reserves
	stock would leak an SRE on every single run."""
	so.create_stock_reservation_entries()
	for name in frappe.get_all("Stock Reservation Entry", filters={"voucher_no": so.name}, pluck="name"):
		world.track_existing("Stock Reservation Entry", name)


class TestStockReservationManual(IntegrationTestCase):
	"""Escenario B -- see module docstring."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def setUp(self):
		super().setUp()
		# Per-test (not per-class) settings scoping, restored via
		# self.addCleanup() (runs after *this* test method specifically) --
		# deliberately not addClassCleanup: with two separate TestCase
		# classes in this module both overriding the same site-wide Stock
		# Settings singleton, relying on class-level restore left the site
		# with enable_stock_reservation stuck at 1 after a full run
		# (confirmed empirically) instead of back at its real starting
		# value. Scoping per test method removes any cross-class or
		# cross-method timing dependency entirely.
		ctx = fx.stock_settings(enable_stock_reservation=1, auto_reserve_stock=0)
		ctx.__enter__()
		self.addCleanup(ctx.__exit__, None, None, None)

	def _new_item_and_warehouse(self, tag):
		wh = self.world.warehouse(f"FG10 {tag}")
		item = self.world.item(f"FG10-{tag.upper()}")
		customer = self.world.customer(f"FG10 {tag} Customer")
		return wh, item, customer

	def _bin(self, item, warehouse):
		return frappe.get_doc("Bin", {"item_code": item, "warehouse": warehouse})

	# -- Caso 1: stock completo ------------------------------------------

	def test_case1_full_stock_reserves_exactly_requested_qty(self):
		wh, item, customer = self._new_item_and_warehouse("Case1")
		self.world.stock_up_real(item.name, wh.name, 20)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		self.assertEqual(so.reserve_stock, 0)  # not auto-set under auto_reserve_stock=0
		so.reload()
		_reserve_and_track(self.world, so)
		so.reload()

		sre_list = frappe.get_all(
			"Stock Reservation Entry",
			filters={"voucher_type": "Sales Order", "voucher_no": so.name, "docstatus": 1},
			fields=["name", "reserved_qty"],
		)
		self.assertEqual(len(sre_list), 1)
		self.assertEqual(sre_list[0].reserved_qty, 10.0)
		self.assertEqual(so.items[0].stock_reserved_qty, 10.0)
		# Bin.reserved_qty happens to also read 10 here, but it is a
		# *different* signal than the SRE reservation above -- it's the
		# legacy "sum of requested qty across all submitted, undelivered SO
		# Items" field (see Caso 4's test for the case where the two
		# genuinely diverge).
		self.assertEqual(flt(self._bin(item.name, wh.name).reserved_qty), 10.0)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 10.0)  # 20 - 10

	# -- Caso 2: stock parcial --------------------------------------------

	def test_case2_partial_stock_reserves_only_whats_available_no_error(self):
		wh, item, customer = self._new_item_and_warehouse("Case2")
		self.world.stock_up_real(item.name, wh.name, 6)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)

		so.reload()
		_reserve_and_track(self.world, so)  # must not raise -- allow_partial_reservation=1
		so.reload()

		self.assertEqual(so.items[0].stock_reserved_qty, 6.0)
		unreserved = flt(so.items[0].stock_qty) - flt(so.items[0].stock_reserved_qty)
		self.assertEqual(unreserved, 4.0)
		# the missing 4 units are simply left unreserved -- no error, no
		# partial-blocking, Sales Order stays fully submitted and valid.
		self.assertEqual(so.docstatus, 1)

	# -- Caso 3: stock cero -------------------------------------------------

	def test_case3_zero_stock_creates_no_sre_and_does_not_block(self):
		wh, item, customer = self._new_item_and_warehouse("Case3")
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)  # no stock_up

		so.reload()
		_reserve_and_track(self.world, so)  # must not raise
		so.reload()

		self.assertEqual(so.items[0].stock_reserved_qty, 0)
		sre_count = frappe.db.count(
			"Stock Reservation Entry", {"voucher_no": so.name, "docstatus": 1}
		)
		self.assertEqual(sre_count, 0)
		self.assertEqual(so.docstatus, 1)  # submit was never blocked by this

	# -- Caso 4: dos Sales Orders compiten por stock -----------------------

	def test_case4_two_sales_orders_competing_for_stock(self):
		wh, item, customer = self._new_item_and_warehouse("Case4")
		self.world.stock_up_real(item.name, wh.name, 10)

		so_a = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		so_a.reload()
		_reserve_and_track(self.world, so_a)
		so_a.reload()
		self.assertEqual(so_a.items[0].stock_reserved_qty, 8.0)

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		so_b.reload()
		_reserve_and_track(self.world, so_b)  # must not raise
		so_b.reload()

		# SO-B asked for 8 but only 2 physically remained unreserved --
		# it gets exactly 2, never touching SO-A's 8. This is the real,
		# hard-capped reservation, tracked on Stock Reservation Entry /
		# Sales Order Item.stock_reserved_qty -- Bin.reserved_qty is a
		# *different*, older field (updated by
		# Sales Order.update_reserved_qty(), unconditionally, unrelated to
		# Stock Reservation Entry/enable_stock_reservation) that sums the
		# full *requested* qty of every submitted, undelivered Sales Order
		# Item for this item+warehouse regardless of actual availability --
		# 8 + 8 = 16 here, not 8 + 2. Confirmed via
		# erpnext.stock.stock_balance.get_reserved_qty(): it is a soft
		# demand signal (feeds projected_qty), not a hard allocation lock --
		# do not read Bin.reserved_qty as "what Stock Reservation actually
		# secured."
		self.assertEqual(so_b.items[0].stock_reserved_qty, 2.0)
		self.assertEqual(flt(self._bin(item.name, wh.name).reserved_qty), 16.0)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 0.0)

	# -- Caso 5: cancelación ------------------------------------------------

	def test_case5_cancelling_sales_order_releases_reservation(self):
		wh, item, customer = self._new_item_and_warehouse("Case5")
		self.world.stock_up_real(item.name, wh.name, 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		so.reload()
		_reserve_and_track(self.world, so)
		so.reload()
		self.assertEqual(so.items[0].stock_reserved_qty, 10.0)

		so.cancel()

		sre_list = frappe.get_all(
			"Stock Reservation Entry", filters={"voucher_no": so.name}, fields=["name", "docstatus"]
		)
		self.assertEqual(len(sre_list), 1)
		self.assertEqual(sre_list[0].docstatus, 2)  # cancelled, not deleted -- audit trail kept

		self.assertEqual(flt(self._bin(item.name, wh.name).reserved_qty), 0.0)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 10.0)  # fully released

	# -- Caso 6: Pick List -------------------------------------------------

	def test_case6_pick_list_blocked_for_sales_order_with_reserved_stock(self):
		"""Critical finding: ERPNext's create_pick_list() (the standard
		"Create Pick List" mapped-doc function, the same one this app's own
		fixtures.pick_list_for() and the real Sales Order UI button both use)
		refuses outright to build a Pick List for a Sales Order that has any
		reserved stock -- "Cannot create a pick list ... because it has
		reserved stock. Please unreserve the stock in order to create a pick
		list." Native SO-level Stock Reservation and this app's entire
		Pick-List-centric Bodega workflow are not compatible as-is. See the
		Commit 10 report for the architectural implications."""
		wh, item, customer = self._new_item_and_warehouse("Case6a")
		self.world.stock_up_real(item.name, wh.name, 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 6, customer.name)
		so.reload()
		_reserve_and_track(self.world, so)

		with self.assertRaises(frappe.ValidationError):
			_create_pick_list_unsaved(so.name)

	def test_case6_unreserved_orders_pick_list_ignores_other_orders_reservation(self):
		"""Second finding: a Pick List for a DIFFERENT, unreserved Sales
		Order is not blocked -- but it also does not exclude stock another
		order has reserved. ERPNext's own
		get_available_item_locations_for_other_item() (pick_list.py) reads
		Bin.actual_qty directly, with no Stock Reservation Entry awareness at
		all. Native Stock Reservation only protects against another
		*reservation* (Caso 4); it provides no protection at the Pick List
		layer against a completely different, unreserved order picking the
		same physical units."""
		wh, item, customer = self._new_item_and_warehouse("Case6b")
		self.world.stock_up_real(item.name, wh.name, 10)

		so_a = self.world.submitted_sales_order(item.name, wh.name, 6, customer.name)
		so_a.reload()
		_reserve_and_track(self.world, so_a)

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)  # never reserved

		pl_b = _create_pick_list_unsaved(so_b.name)
		pl_b.parent_warehouse = wh.name
		pl_b.insert()
		self.world.track_existing("Pick List", pl_b.name)

		# Would be at most 4 (10 - 6 reserved by SO-A) if this were
		# reservation-aware; ERPNext actually suggests all 8, straight off
		# Bin.actual_qty=10, ignoring SO-A's reservation entirely.
		self.assertEqual(pl_b.get("locations")[0].stock_qty, 8.0)


class TestStockReservationAutomatic(IntegrationTestCase):
	"""Escenario A -- see module docstring. Casos 1-6 are already exercised
	in full under Escenario B; this class only confirms the automatic
	trigger itself, and that Caso 6's finding is not specific to the
	explicit-call path."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def setUp(self):
		super().setUp()
		# Per-test settings scoping -- see TestStockReservationManual.setUp()
		# for why this is not addClassCleanup.
		ctx = fx.stock_settings(enable_stock_reservation=1, auto_reserve_stock=1)
		ctx.__enter__()
		self.addCleanup(ctx.__exit__, None, None, None)

	def test_reservation_happens_automatically_on_submit_no_explicit_call(self):
		wh = self.world.warehouse("FG10 Auto")
		item = self.world.item("FG10-AUTO")
		customer = self.world.customer("FG10 Auto Customer")
		self.world.stock_up_real(item.name, wh.name, 20)

		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		# No explicit create_stock_reservation_entries() call anywhere above --
		# ERPNext creates the SRE automatically inside .submit() itself, so it
		# must be tracked for cleanup here rather than via _reserve_and_track().
		for name in frappe.get_all("Stock Reservation Entry", filters={"voucher_no": so.name}, pluck="name"):
			self.world.track_existing("Stock Reservation Entry", name)
		so.reload()

		self.assertEqual(so.reserve_stock, 1)  # forced on at insert() by auto_reserve_stock
		self.assertEqual(so.items[0].stock_reserved_qty, 10.0)
		sre_count = frappe.db.count(
			"Stock Reservation Entry", {"voucher_no": so.name, "docstatus": 1}
		)
		self.assertEqual(sre_count, 1)

	def test_automatic_reservation_also_blocks_pick_list_creation(self):
		wh = self.world.warehouse("FG10 Auto Block")
		item = self.world.item("FG10-AUTO-BLOCK")
		customer = self.world.customer("FG10 Auto Block Customer")
		self.world.stock_up_real(item.name, wh.name, 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 6, customer.name)
		# auto_reserve_stock=1 reserves inside .submit() -- track for cleanup.
		for name in frappe.get_all("Stock Reservation Entry", filters={"voucher_no": so.name}, pluck="name"):
			self.world.track_existing("Stock Reservation Entry", name)

		with self.assertRaises(frappe.ValidationError):
			_create_pick_list_unsaved(so.name)
