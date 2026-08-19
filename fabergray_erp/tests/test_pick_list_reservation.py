# -*- coding: utf-8 -*-
"""Commit 11 -- controlled validation of ERPNext 16's native Pick List-level
Stock Reservation (Pick List.create_stock_reservation_entries()), as a
possible allocation/available-to-promise mechanism compatible with this
app's Pick-List-centric Bodega workflow -- Commit 10 already proved
Sales-Order-level reservation is NOT compatible (create_pick_list() refuses
outright for a Sales Order with reserved stock).

Nothing here is application code: no hooks, no Custom Fields, no changes to
api/bodega.py/api/jefe_bodega.py, no Fulfillment Engine. Every test drives
the real api.bodega.* functions (get_queue, get_pick_list, start_picking,
set_picked_qty, report_shortage, finish_picking) exactly as /app/bodega
does, then calls ERPNext's own Pick List.create_stock_reservation_entries()
directly (there is no application code wrapping it yet) to observe its
native behaviour. See the Commit 11 report for the full writeup and the
final C1/C2/C3/C4 recommendation.

Headline finding, decisive for that recommendation: Pick List.before_submit()
calls validate_sales_order(), which throws the exact same "Cannot create a
pick list ... because it has reserved stock" error used to block
create_pick_list() -- but here it blocks *submitting* a Pick List whose own
Sales Order has any reserved stock, REGARDLESS of whether that reservation
came from the Sales Order or from this same Pick List. Reserving via
Pick List does not avoid the Commit 10 problem -- it just moves the block
from "create the Pick List" to "finish picking it", which is worse for this
app's flow (Bodega would pick, reserve, and then be unable to call
finish_picking() at all without first releasing the very reservation meant
to protect the stock it just picked).

Second, unrelated but very important finding: ERPNext's own create_pick_list()
already provides real allocation protection with zero Stock Reservation
Entry involvement -- get_available_item_locations_for_other_item() excludes
whatever is already picked_qty'd on *other* Pick List Item rows for the same
item+warehouse (filter_locations_by_picked_materials(), pick_list.py) before
ever looking at Bin.actual_qty. This has been true since Commit 5/6; this
commit is the first time it was specifically tested and confirmed.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega
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


class TestPickListReservation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg11-bodega@example.com", ["Bodega"])

	def setUp(self):
		super().setUp()
		# Per-test (not per-class) settings scoping -- see Commit 10's
		# fixtures.stock_settings() docstring / test_stock_reservation.py
		# for why class-level restore is unreliable with multiple test
		# methods touching the same site-wide Single.
		ctx = fx.stock_settings(enable_stock_reservation=1, auto_reserve_stock=0)
		ctx.__enter__()
		self.addCleanup(ctx.__exit__, None, None, None)

	# -- Helpers -------------------------------------------------------------

	def _new_world(self, tag, stock_qty=None):
		wh = self.world.warehouse(f"FG11 {tag}")
		item = self.world.item(f"FG11-{tag.upper()}")
		customer = self.world.customer(f"FG11 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _bin(self, item, warehouse):
		return frappe.get_doc("Bin", {"item_code": item, "warehouse": warehouse})

	def _pick_fully(self, pl_name, item_code=None):
		"""Starts picking (if not already) and sets every row's picked_qty
		to its full qty_solicitada via the real Bodega API."""
		with fx.as_user(self.bodega_user):
			detail = bodega.get_pick_list(pl_name)
			if not detail["fg_started_by"]:
				bodega.start_picking(pl_name)
				detail = bodega.get_pick_list(pl_name)
			for row in detail["rows"]:
				if item_code and row["item_code"] != item_code:
					continue
				bodega.set_picked_qty(pl_name, row["row_name"], row["qty_solicitada"])

	def _reserve_pick_list(self, pl):
		"""Call the native Pick List reservation directly (no application
		code wraps this yet) and track whatever Stock Reservation Entry it
		creates for cleanup -- same leak this app hit in Commit 10 for the
		Sales-Order-level call: create_stock_reservation_entries() returns
		None and deleting/cancelling the Pick List does not cascade to it."""
		pl.reload()
		pl.create_stock_reservation_entries()
		sre_list = frappe.get_all(
			"Stock Reservation Entry",
			filters={"from_voucher_type": "Pick List", "from_voucher_no": pl.name},
			fields=[
				"name",
				"voucher_type",
				"voucher_no",
				"voucher_detail_no",
				"from_voucher_detail_no",
				"reserved_qty",
				"docstatus",
				"status",
			],
		)
		for sre in sre_list:
			self.world.track_existing("Stock Reservation Entry", sre.name)
		return sre_list

	# -- Caso 1: Pick List + reserva ---------------------------------------

	def test_case1_pick_list_reservation_creates_sre_linked_to_sales_order(self):
		wh, item, customer = self._new_world("Case1", 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl = self.world.pick_list_for(so, wh.name)
		pl_row_name = pl.get("locations")[0].name

		self._pick_fully(pl.name)
		sre_list = self._reserve_pick_list(pl)

		self.assertEqual(len(sre_list), 1)
		sre = sre_list[0]
		# The SRE itself is always anchored to the Sales Order (voucher_type/
		# voucher_no), regardless of what triggered it -- the Pick List is
		# recorded separately, in from_voucher_type/from_voucher_no/
		# from_voucher_detail_no. Confirmed by reading
		# create_stock_reservation_entries_for_so_items() directly.
		self.assertEqual(sre.voucher_type, "Sales Order")
		self.assertEqual(sre.voucher_no, so.name)
		self.assertEqual(sre.reserved_qty, 8.0)
		self.assertEqual(sre.from_voucher_detail_no, pl_row_name)

		pl.reload()
		self.assertTrue(pl.has_reserved_stock())
		self.assertEqual(flt(self._bin(item.name, wh.name).reserved_qty), 8.0)

	# -- Caso 2: Bodega sigue funcionando después de reservar ----------------

	def test_case2_bodega_apis_all_keep_working_after_reservation(self):
		"""Exercises get_queue/get_pick_list/start_picking/set_picked_qty/
		report_shortage, in that order, with a native Pick List reservation
		active in the middle -- exactly the same sequence /app/bodega itself
		would drive. finish_picking() is checked separately below: it is
		correctly *blocked* while the reservation is active (the headline
		finding), and succeeds cleanly once released -- never a crash
		either way, always the same clear native ValidationError."""
		wh, item1, customer = self._new_world("Case2", 20)
		item2 = self.world.item("FG11-CASE2-B")
		self.world.stock_up_real(item2.name, wh.name, 6)

		so = self.world.multi_item_sales_order(
			customer.name,
			[
				{"item_code": item1.name, "warehouse": wh.name, "qty": 10, "rate": 100},
				{"item_code": item2.name, "warehouse": wh.name, "qty": 10, "rate": 100},
			],
		)
		pl = self.world.pick_list_for(so, wh.name)

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
			self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

			bodega.start_picking(pl.name)
			detail = bodega.get_pick_list(pl.name)
			rows = {r["item_code"]: r for r in detail["rows"]}
			bodega.set_picked_qty(pl.name, rows[item1.name]["row_name"], 10)  # full

		self._reserve_pick_list(pl)  # reserves item1's row only (item2 still 0 picked)

		with fx.as_user(self.bodega_user):
			# get_queue()/get_pick_list() must still work with an active SRE
			queue = bodega.get_queue()
			self.assertIn(pl.name, [p["name"] for p in queue["en_alistamiento"]])
			detail = bodega.get_pick_list(pl.name)
			rows = {r["item_code"]: r for r in detail["rows"]}
			self.assertEqual(rows[item1.name]["qty_alistada"], 10.0)

			# pick and report the shortage on item2 (only 6 of 10 available)
			bodega.set_picked_qty(pl.name, rows[item2.name]["row_name"], 6)
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=rows[item2.name]["row_name"],
				qty_disponible=6,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])

			# finish_picking() -> pl.submit() -> Pick List's own
			# before_submit() -> validate_sales_order(): blocked while the
			# Sales Order has ANY reserved stock. Not a crash -- the exact
			# same clear native ValidationError create_pick_list() itself
			# raises in Commit 10.
			with self.assertRaises(frappe.ValidationError):
				bodega.finish_picking(pl.name)

		# Release the reservation (as a future engine/UI action would have
		# to) and confirm finish_picking() then succeeds cleanly.
		pl.reload()
		pl.cancel_stock_reservation_entries()

		with fx.as_user(self.bodega_user):
			result = bodega.finish_picking(pl.name)
		self.assertEqual(result["docstatus"], 1)

	# -- Caso 3: dos pedidos compiten (el más importante) --------------------

	def test_case3_two_pick_lists_competing_for_stock(self):
		wh, item, customer = self._new_world("Case3", 10)

		so_a = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl_a = self.world.pick_list_for(so_a, wh.name)
		self._pick_fully(pl_a.name)
		sre_a = self._reserve_pick_list(pl_a)
		self.assertEqual(sre_a[0].reserved_qty, 8.0)

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl_b = self.world.pick_list_for(so_b, wh.name)

		# What Pick List B's own creation suggests as available: NOT 8, and
		# NOT because of the Stock Reservation Entry above -- see the
		# no-reservation-at-all sub-test below for proof of that. ERPNext's
		# create_pick_list() already excludes what's picked_qty'd on A's
		# row for the same item+warehouse
		# (filter_locations_by_picked_materials(), pick_list.py) before it
		# ever looks at Bin.actual_qty, so B is correctly offered only the
		# 2 units A did not take.
		self.assertEqual(pl_b.get("locations")[0].stock_qty, 2.0)

		# The reservation layer itself is also honest, independently: if B
		# is picked (only 2 are offered) and then actually reserved,
		# get_available_qty_to_reserve() finds exactly 2 left -- same
		# protection already proven at the Sales-Order level in Commit 10's
		# Caso 4, now proven again triggered via Pick List.
		self._pick_fully(pl_b.name)
		sre_b = self._reserve_pick_list(pl_b)

		self.assertEqual(len(sre_b), 1)
		self.assertEqual(sre_b[0].reserved_qty, 2.0)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 0.0)

	def test_case3b_picked_quantity_protection_works_without_any_reservation(self):
		"""Decisive for the C1 vs C2 recommendation: the protection Caso 3
		relies on is NOT the Stock Reservation Entry -- it is ERPNext's own
		picked_qty-aware location suggestion, active today, with zero
		reservation calls anywhere. Pick List A is picked but deliberately
		never reserved; Pick List B still only sees 2 units."""
		wh, item, customer = self._new_world("Case3b", 10)

		so_a = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl_a = self.world.pick_list_for(so_a, wh.name)
		self._pick_fully(pl_a.name)
		# Deliberately no self._reserve_pick_list(pl_a) call here.

		so_b = self.world.submitted_sales_order(item.name, wh.name, 8, customer.name)
		pl_b = self.world.pick_list_for(so_b, wh.name)

		self.assertEqual(pl_b.get("locations")[0].stock_qty, 2.0)

	# -- Caso 4: reserva parcial + Reporte de Faltante -----------------------

	def test_case4_partial_pick_list_reservation_coexists_with_shortage_report(self):
		wh, item, customer = self._new_world("Case4", 6)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		pl = self.world.pick_list_for(so, wh.name)

		# Important, separate finding: create_pick_list()'s own
		# set_item_locations() already caps the Pick List Item's stock_qty
		# to what's physically available (6), not the Sales Order's true
		# requested qty (10) -- confirmed via the row read below. This means
		# the Pick List Item itself does NOT represent "10 requested, 6
		# available, 4 short" -- it just shows a smaller row (6/6, looking
		# complete). The true original request (10) is only visible on the
		# Sales Order Item, never on the Pick List Item. This is pre-existing
		# behaviour (unchanged since Commit 5/6), not something Commit 11
		# introduces -- surfaced here for the first time because earlier
		# test suites always had abundant stock at Pick List creation time.
		row_before = pl.get("locations")[0]
		self.assertEqual(row_before.stock_qty, 6.0)  # capped, not 10

		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			self.assertEqual(row["qty_solicitada"], 6.0)  # same cap, visible through our own API
			bodega.set_picked_qty(pl.name, row["row_name"], 6)  # everything physically there
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=row["row_name"],
				qty_disponible=6,
				shortage_reason="Stock insuficiente",
			)
		self.world.track_existing("Reporte de Faltante", report["name"])

		# The Reporte de Faltante inherits the same cap for the same reason
		# (api.bodega._create_shortage_report() derives qty_solicitada from
		# the Pick List row, per its Commit 9 contract) -- qty_faltante
		# reads 0, not 4. The true 4-unit shortfall against the customer's
		# original order is only reconstructable by comparing against the
		# Sales Order Item (qty=10) separately -- it is invisible here.
		report_doc = frappe.get_doc("Reporte de Faltante", report["name"])
		self.assertEqual(report_doc.qty_faltante, 0.0)
		so.reload()
		self.assertEqual(so.items[0].stock_qty, 10.0)  # the true request, elsewhere

		sre_list = self._reserve_pick_list(pl)
		self.assertEqual(len(sre_list), 1)
		self.assertEqual(sre_list[0].reserved_qty, 6.0)  # reserves exactly what was picked

		# finish_picking() is blocked while reserved (Caso 2/6 finding) --
		# release first, then it completes normally with the disclosed
		# partial pick.
		pl.reload()
		pl.cancel_stock_reservation_entries()
		with fx.as_user(self.bodega_user):
			result = bodega.finish_picking(pl.name)
		self.assertEqual(result["docstatus"], 1)

	# -- Caso 5: cancelación -------------------------------------------------

	def test_case5_releasing_reservation_before_deleting_draft_works_cleanly(self):
		wh, item, customer = self._new_world("Case5", 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		pl = self.world.pick_list_for(so, wh.name)
		self._pick_fully(pl.name)
		sre_list = self._reserve_pick_list(pl)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 0.0)

		pl.reload()
		pl.cancel_stock_reservation_entries()

		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_list[0].name)
		self.assertEqual(sre_doc.docstatus, 2)
		# Bin.reserved_qty is the OTHER, legacy field (Commit 10 finding) --
		# it reflects the Sales Order's own outstanding demand, unrelated to
		# Stock Reservation Entry, and correctly stays at 10 as long as the
		# Sales Order itself is still submitted and undelivered. The real
		# "did the reservation actually release" signal is
		# get_available_qty_to_reserve(), not Bin.reserved_qty.
		self.assertEqual(flt(self._bin(item.name, wh.name).reserved_qty), 10.0)
		self.assertEqual(_get_available_qty_to_reserve(item.name, wh.name), 10.0)  # fully released

	def test_case5b_deleting_a_reserved_draft_pick_list_without_releasing_first(self):
		"""The risky path, documented as a real finding, not swept under the
		rug. A Pick List can never reach a *submitted* state while it holds
		a reservation (Caso 2/6), so the only reachable "cancel/delete a
		reserved Pick List" scenario is discarding the still-draft Pick List
		itself. Frappe's generic delete_doc() link-existence check catches
		this by default (LinkExistsError) -- the reservation is not silently
		orphaned unless the caller forces past that check."""
		wh, item, customer = self._new_world("Case5b", 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		pl = self.world.pick_list_for(so, wh.name)
		self._pick_fully(pl.name)
		sre_list = self._reserve_pick_list(pl)

		with self.assertRaises(frappe.LinkExistsError):
			frappe.delete_doc("Pick List", pl.name)  # no force= -- the real, unprivileged default

		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_list[0].name)
		self.assertEqual(sre_doc.docstatus, 1)  # untouched, still active

		# The safe path: release first, then delete succeeds normally.
		pl.reload()
		pl.cancel_stock_reservation_entries()
		frappe.delete_doc("Pick List", pl.name)
		self.world._created.remove(("Pick List", pl.name))  # already gone, nothing left for cleanup()

	# -- Caso 6: submit del Pick List ----------------------------------------

	def test_case6_submit_is_blocked_while_reserved_and_succeeds_once_released(self):
		"""The headline finding, isolated as its own test: Pick List.submit()
		(and therefore api.bodega.finish_picking()) is unconditionally
		blocked by before_submit() -> validate_sales_order() whenever the
		Sales Order has ANY reserved stock -- with no exception for stock
		reserved by this very Pick List. The reservation window necessarily
		closes *before* the pick can be finalized, so it provides no
		protection during the step that actually matters (from "fully
		picked" to "ready to ship"). Once released, submit proceeds exactly
		as it always has (Commits 4-9, unchanged)."""
		wh, item, customer = self._new_world("Case6", 10)
		so = self.world.submitted_sales_order(item.name, wh.name, 10, customer.name)
		pl = self.world.pick_list_for(so, wh.name)
		self._pick_fully(pl.name)
		self._reserve_pick_list(pl)

		pl.reload()
		with self.assertRaises(frappe.ValidationError):
			pl.submit()

		pl.reload()
		self.assertEqual(pl.docstatus, 0)  # still draft -- submit never partially applied

		pl.cancel_stock_reservation_entries()
		pl.reload()
		pl.submit()  # native call, not finish_picking() -- proves it's ERPNext's own gate, not ours
		self.assertEqual(pl.docstatus, 1)
