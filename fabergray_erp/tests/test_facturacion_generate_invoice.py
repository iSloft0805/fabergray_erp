# -*- coding: utf-8 -*-
"""Commit 21.3 -- tests for api.facturacion.generate_invoice().

Every scenario from the approved brief's "Tests mínimos" list. "ningún
commit manual" is enforced statically, not here -- see
test_regression.py::test_facturacion_api_never_calls_get_all_ignore_permissions_set_user_or_commit
(AST guardrail, extended this commit with a frappe.db.commit check).

Everything here calls the real, whitelisted facturacion.generate_invoice()
under a real, restricted Facturación (or other role, for the negative
cases) session -- never erpnext's create_delivery() directly (that was
Commit 21.1's own functional-flow test, kept as-is; this file exercises the
production endpoint that now wraps it).

INVENTARIO-OUT-01 (1ea7dad) -- current contract: COMPLETAR PEDIDO
(bodega.finish_picking()) already took the stock out through ONE Material
Issue. generate_invoice() is the legacy stock-moving Sales Invoice
(update_stock=1), so on a completed Pick List it is refused before any
Sales Invoice exists; invoicing is mark_as_invoiced(), which moves no stock
(test_inventario_out_stock_issue.test_invoicing_does_not_move_stock). Every
test that completes a Pick List asserts the stock left exactly once.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock.utils import get_bin

from fabergray_erp.api import bodega, facturacion
from fabergray_erp.fulfillment.stock_issue_service import (
	SalesInvoiceDoubleIssueError,
	controlled_pick_list_submit,
	guard_sales_invoice_double_issue,
)
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VALID_INCOME_ACCOUNT = "413520 - Venta de productos en almacenes no especializados - FG"


def _actual_qty(item_code, warehouse):
	return flt(get_bin(item_code, warehouse).actual_qty)


def _sle_count(item_code):
	return frappe.db.count("Stock Ledger Entry", {"item_code": item_code, "is_cancelled": 0})


def _invoices_of(pick_list_name):
	return frappe.get_all(
		"Sales Invoice Item", filters={"against_pick_list": pick_list_name}, pluck="parent", distinct=True
	)


class TestGenerateInvoice(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG213 WH")
		cls.item = cls.world.item("FG213-ITEM")
		cls.customer = cls.world.customer("FG213 Customer")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)
		# A product that never goes through a Pick List (normal / mixed invoices).
		cls.other_item = cls.world.item("FG213-OTHER-ITEM")
		cls.world.stock_up_real(cls.other_item.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg213-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg213-facturacion@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user("fg213-vendedora@example.com", ["Vendedora"])
		cls.no_role_user = cls.world.user("fg213-norole@example.com", [])

	# -- Shared setup helper --------------------------------------------------

	def _submitted_pick_list(self, qty, rate=100):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _assert_issued_once(self, pl, qty):
		"""COMPLETAR PEDIDO's own outflow: exactly one submitted Material
		Issue for this Pick List, `qty` units, linked row by row."""
		issues = frappe.get_all(
			"Stock Entry", filters={"pick_list": pl.name, "docstatus": 1}, fields=["name", "purpose"]
		)
		self.assertEqual([i.purpose for i in issues], ["Material Issue"])
		entry = frappe.get_doc("Stock Entry", issues[0].name)
		self.assertEqual(sum(flt(d.qty) for d in entry.items), qty)
		self.assertEqual({d.pick_list_item for d in entry.items}, {r.name for r in pl.locations})
		self.assertEqual(sum(flt(r.transferred_qty) for r in pl.locations), qty)
		return entry.name

	def _assert_generate_invoice_refused(self, pl, stock_issue):
		"""The legacy invoice is refused with the issue's name, and nothing
		moved: no Sales Invoice, same Bin, same Stock Ledger. The refusal
		comes before any accounting step, so no income-account override
		(Company is never written here)."""
		qty_before = _actual_qty(self.item.name, self.wh.name)
		sle_before = _sle_count(self.item.name)
		with fx.as_user(self.facturacion_user), self.assertRaises(frappe.ValidationError) as ctx:
			facturacion.generate_invoice(pl.name)
		self.assertIn("ya se descontó", str(ctx.exception))
		self.assertIn(stock_issue, str(ctx.exception))
		self.assertEqual(_invoices_of(pl.name), [])
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before)
		self.assertEqual(_sle_count(self.item.name), sle_before)

	# -- Happy path: full pick, one call -------------------------------------

	def test_generate_invoice_full_flow(self):
		"""Completed Pick List: the Material Issue took the 6 units out once;
		the legacy invoice is refused and takes nothing out again."""
		qty_start = _actual_qty(self.item.name, self.wh.name)
		so, pl = self._submitted_pick_list(qty=6, rate=275)

		issue = self._assert_issued_once(pl, 6)
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 6)

		self._assert_generate_invoice_refused(pl, issue)

		# Still exactly one outflow of 6 -- never 12.
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 6)
		pl_after = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(flt(pl_after.locations[0].delivered_qty), 0)
		self.assertEqual(flt(pl_after.locations[0].transferred_qty), 6)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_billed")), 0)

	# -- Idempotency: Fully Delivered rejects a second call -------------------

	def test_second_call_on_fully_delivered_pick_list_is_rejected(self):
		"""Repeated legacy calls are refused every time; no Sales Invoice is
		ever created and the stock left once, through the Material Issue."""
		qty_start = _actual_qty(self.item.name, self.wh.name)
		_, pl = self._submitted_pick_list(qty=2, rate=100)
		issue = self._assert_issued_once(pl, 2)

		self._assert_generate_invoice_refused(pl, issue)
		self._assert_generate_invoice_refused(pl, issue)

		self.assertEqual(_invoices_of(pl.name), [])
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 2)
		self._assert_issued_once(frappe.get_doc("Pick List", pl.name), 2)

	# -- Partly Delivered: only the real remainder gets invoiced -------------

	def test_partly_delivered_invoices_only_the_remainder(self):
		"""The native mapper the legacy endpoint wraps (create_delivery(...,
		target="Sales Invoice"), also reachable from Desk) builds a stock-
		moving Sales Invoice (update_stock=1). On a Pick List whose Material
		Issue already took the units out it must be refused (stock_issue_
		service.guard_sales_invoice_double_issue()): the same units can never
		leave twice, partially or fully."""
		qty_start = _actual_qty(self.item.name, self.wh.name)
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], 10)
			bodega.finish_picking(pl.name)
		pl = frappe.get_doc("Pick List", pl.name)
		issue = self._assert_issued_once(pl, 10)
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 10)
		sle_before = _sle_count(self.item.name)

		from erpnext.stock.doctype.pick_list.pick_list import create_delivery

		# Rejected at the draft's own save (Sales Invoice validate), before
		# any quantity can be edited or submitted: nothing is ever written.
		with fx.as_user(self.facturacion_user), self.assertRaises(SalesInvoiceDoubleIssueError):
			create_delivery(pl.name, target="Sales Invoice")

		self.assertEqual(_invoices_of(pl.name), [])
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 10)
		self.assertEqual(_sle_count(self.item.name), sle_before)
		self.assertEqual(flt(frappe.db.get_value("Pick List Item", pl.locations[0].name, "delivered_qty")), 0)
		# The legacy endpoint refuses the remainder too.
		self._assert_generate_invoice_refused(pl, issue)

	# -- Draft Pick List rejected ---------------------------------------------

	def test_draft_pick_list_rejected(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 3, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)  # never submitted -- docstatus 0
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError):
				facturacion.generate_invoice(pl.name)

	# -- Multi-SO rejected ------------------------------------------------------

	def test_multi_sales_order_pick_list_rejected(self):
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
		# INVENTARIO-OUT-01: a direct submit of a Delivery Pick List is refused by
		# stock_issue_service.guard_pick_list_submit(); this fixture opens the same
		# controlled door finish_picking() uses.
		with controlled_pick_list_submit(pl.name):
			pl.submit()

		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError):
				facturacion.generate_invoice(pl.name)

	# -- Permission gate ----------------------------------------------------

	def test_user_without_role_or_permission_is_blocked(self):
		_, pl = self._submitted_pick_list(qty=1, rate=100)
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.generate_invoice(pl.name)
		with fx.as_user(self.vendedora_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.generate_invoice(pl.name)

	# -- Native cancellation reverts everything --------------------------------

	def test_native_cancellation_reverts_everything(self):
		"""The only stock-moving document of a completed Pick List is its
		Material Issue: cancelling it natively gives back exactly the units
		it took and clears transferred_qty; the refused legacy invoice left
		nothing to revert."""
		qty_start = _actual_qty(self.item.name, self.wh.name)
		_, pl = self._submitted_pick_list(qty=5, rate=100)
		issue = self._assert_issued_once(pl, 5)
		self._assert_generate_invoice_refused(pl, issue)
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 5)

		frappe.get_doc("Stock Entry", issue).cancel()  # native cancellation, Administrator

		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start)
		pl_after = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(flt(pl_after.locations[0].transferred_qty), 0)
		self.assertEqual(flt(pl_after.locations[0].delivered_qty), 0)
		self.assertEqual(_invoices_of(pl.name), [])
		# Issue and reversal net to zero in the ledger.
		self.assertEqual(
			flt(
				frappe.db.sql(
					"select sum(actual_qty) from `tabStock Ledger Entry` where voucher_no=%s", issue
				)[0][0]
			),
			0,
		)

	# -- Accounting blocker: still reproducible without the test override -----

	def test_accounting_blocker_131505_still_reproducible_without_override(self):
		"""Commit 21.1's documented, deliberately-unfixed precondition is still
		live (Company.default_income_account = "131505 - Ventas - FG", a
		Receivable account) -- but since 1ea7dad generate_invoice() never
		reaches the Sales Invoice for a completed Pick List: the stock guard
		refuses first, with no override active and no draft left behind."""
		self.assertEqual(
			frappe.db.get_value("Company", fx.COMPANY, "default_income_account"),
			"131505 - Ventas - FG",
		)
		self.assertEqual(
			frappe.db.get_value("Account", "131505 - Ventas - FG", "account_type"), "Receivable"
		)

		qty_start = _actual_qty(self.item.name, self.wh.name)
		_, pl = self._submitted_pick_list(qty=2, rate=100)
		issue = self._assert_issued_once(pl, 2)

		self._assert_generate_invoice_refused(pl, issue)
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_start - 2)


	# -- INVENTARIO-OUT-01: Sales Invoice never re-issues picked stock ----------
	#
	# Invoices submitted here carry their own line income_account (the valid
	# test account) instead of a Company default override: Company is never
	# written by this section.

	def _completed_pick_list(self, qty):
		_, pl = self._submitted_pick_list(qty=qty, rate=100)
		return pl, self._assert_issued_once(pl, qty)

	def _legacy_submitted_pick_list(self, qty):
		"""A Pick List completed before INVENTARIO-OUT-01: submitted, no
		Material Issue, transferred_qty 0 (controlled submit door)."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
		with controlled_pick_list_submit(pl.name):
			frappe.get_doc("Pick List", pl.name).submit()
		return so, frappe.get_doc("Pick List", pl.name)

	def _submittable(self, invoice):
		for row in invoice.items:
			row.income_account = _VALID_INCOME_ACCOUNT
		return invoice

	def _ledger_state(self):
		return (
			_actual_qty(self.item.name, self.wh.name),
			_actual_qty(self.other_item.name, self.wh.name),
			_sle_count(self.item.name),
			_sle_count(self.other_item.name),
			frappe.db.count("Sales Invoice", {"docstatus": 1}),
		)

	def test_native_stock_invoice_after_material_issue_is_rejected_and_moves_nothing(self):
		"""A + B: completed Pick List -> Material Issue -> native stock Sales
		Invoice is refused, naming the Material Issue; nothing moved."""
		from erpnext.stock.doctype.pick_list.pick_list import create_delivery

		pl, issue = self._completed_pick_list(3)
		before = self._ledger_state()

		with self.assertRaises(SalesInvoiceDoubleIssueError) as ctx:
			create_delivery(pl.name, target="Sales Invoice")

		self.assertIn(issue, str(ctx.exception))
		self.assertIn("ya fue descontado al finalizar el alistamiento", str(ctx.exception))
		self.assertEqual(self._ledger_state(), before)
		self.assertEqual(_invoices_of(pl.name), [])
		self._assert_issued_once(frappe.get_doc("Pick List", pl.name), 3)  # 1 issue, transferred 3 (not 6)

	def test_update_stock_zero_is_never_intervened(self):
		"""C: update_stock=0 moves no stock, so the guard stays out -- even
		for a line pointing at an issued Pick List Item -- and a real
		update_stock=0 invoice of the same order bills it with no movement.
		(ERPNext itself refuses update_stock=0 on lines carrying
		against_pick_list -- validate_update_stock_for_pick_list_reference --
		hence the Sales Order mapper for the real invoice.) Saved, not
		submitted: validate -- where the guard lives -- runs on save; a
		submitted update_stock=0 invoice would post GL on real accounts with
		no Stock Ledger row, which TestWorld's warehouse purge never removes."""
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		so, pl = self._submitted_pick_list(qty=2, rate=100)
		self._assert_issued_once(pl, 2)
		probe = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"update_stock": 0,
				"items": [
					{"item_code": self.item.name, "qty": 2, "pick_list_item": pl.locations[0].name, "idx": 1}
				],
			}
		)
		self.assertIsNone(guard_sales_invoice_double_issue(probe))

		before = self._ledger_state()
		gl_before = frappe.db.count("GL Entry")
		invoice = self._submittable(make_sales_invoice(so.name))
		invoice.update_stock = 0
		invoice.insert()
		self.world.track_existing("Sales Invoice", invoice.name)

		self.assertFalse(invoice.is_new())
		self.assertEqual(self._ledger_state(), before)
		self.assertEqual(frappe.db.count("GL Entry"), gl_before)

	def test_normal_stock_invoice_without_pick_list_is_not_affected(self):
		"""D: a plain update_stock=1 invoice (no Sales Order, no Pick List)
		moves its stock as always -- and cancels as always (F)."""
		qty_before = _actual_qty(self.other_item.name, self.wh.name)
		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"update_stock": 1,
				"items": [
					{
						"item_code": self.other_item.name,
						"warehouse": self.wh.name,
						"qty": 3,
						"rate": 100,
						"income_account": _VALID_INCOME_ACCOUNT,
					}
				],
			}
		)
		invoice.insert()
		self.world.track_existing("Sales Invoice", invoice.name)
		invoice.submit()
		self.assertEqual(_actual_qty(self.other_item.name, self.wh.name), qty_before - 3)

		invoice.cancel()
		self.assertEqual(invoice.docstatus, 2)
		self.assertEqual(_actual_qty(self.other_item.name, self.wh.name), qty_before)

	def test_mixed_invoice_with_one_already_issued_line_is_rejected_whole(self):
		"""E: one normal line + one line of an issued Pick List -> the whole
		invoice is refused; the normal line does not move either."""
		pl, issue = self._completed_pick_list(2)
		row = pl.locations[0]
		before = self._ledger_state()
		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"update_stock": 1,
				"items": [
					{
						"item_code": self.other_item.name,
						"warehouse": self.wh.name,
						"qty": 1,
						"rate": 100,
						"income_account": _VALID_INCOME_ACCOUNT,
					},
					{
						"item_code": self.item.name,
						"warehouse": row.warehouse,
						"qty": 2,
						"rate": 100,
						"income_account": _VALID_INCOME_ACCOUNT,
						"sales_order": row.sales_order,
						"so_detail": row.sales_order_item,
						"against_pick_list": pl.name,
						"pick_list_item": row.name,
					},
				],
			}
		)

		with self.assertRaises(SalesInvoiceDoubleIssueError) as ctx:
			invoice.insert()

		self.assertIn(issue, str(ctx.exception))
		self.assertIn("Fila #2", str(ctx.exception))
		self.assertNotIn("Fila #1", str(ctx.exception))
		self.assertTrue(invoice.is_new())
		self.assertEqual(self._ledger_state(), before)

	def test_valid_pick_list_stock_invoice_still_submits_and_cancels(self):
		"""F: a Pick List with nothing issued (pre-INVENTARIO-OUT-01) keeps
		the native stock invoice: the guard lets it submit and never blocks
		its cancellation (validate does not run on cancel)."""
		from erpnext.stock.doctype.pick_list.pick_list import create_delivery

		_, pl = self._legacy_submitted_pick_list(4)
		self.assertEqual(sum(flt(r.transferred_qty) for r in pl.locations), 0)
		qty_before = _actual_qty(self.item.name, self.wh.name)

		invoice = create_delivery(pl.name, target="Sales Invoice")
		self.world.track_existing("Sales Invoice", invoice.name)
		self._submittable(invoice).save()
		invoice.submit()
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before - 4)

		invoice.reload()
		invoice.cancel()
		self.assertEqual(invoice.docstatus, 2)
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before)

	def test_stock_invoice_from_sales_order_cannot_reissue_but_may_take_never_picked_units(self):
		"""G (so_detail, no pick_list_item -- e.g. invoiced from the Sales
		Order): the units of that Sales Order line the Material Issue took
		are refused; units that were never picked are still invoiceable."""
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], 6)
			report = bodega.report_shortage(pl.name, row["row_name"], 6, "Stock insuficiente")
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl.name)
		pl = frappe.get_doc("Pick List", pl.name)
		issue = self._assert_issued_once(pl, 6)
		before = self._ledger_state()

		too_much = self._submittable(make_sales_invoice(so.name))
		too_much.update_stock = 1
		too_much.items[0].qty = 5  # only 4 were never picked
		with self.assertRaises(SalesInvoiceDoubleIssueError) as ctx:
			too_much.insert()
		self.assertIn(issue, str(ctx.exception))
		self.assertEqual(self._ledger_state(), before)

		remainder = self._submittable(make_sales_invoice(so.name))
		remainder.update_stock = 1
		remainder.items[0].qty = 4
		remainder.insert()
		self.world.track_existing("Sales Invoice", remainder.name)
		remainder.submit()
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), before[0] - 4)
