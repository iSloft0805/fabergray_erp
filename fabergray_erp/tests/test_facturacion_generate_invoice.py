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
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock.utils import get_bin

from fabergray_erp.api import bodega, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VALID_INCOME_ACCOUNT = "413520 - Venta de productos en almacenes no especializados - FG"


def _actual_qty(item_code, warehouse):
	return flt(get_bin(item_code, warehouse).actual_qty)


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

	# -- Happy path: full pick, one call -------------------------------------

	def test_generate_invoice_full_flow(self):
		so, pl = self._submitted_pick_list(qty=6, rate=275)
		so_item_name = so.items[0].name
		pl_row_name = pl.locations[0].name
		qty_before = _actual_qty(self.item.name, self.wh.name)

		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			result = facturacion.generate_invoice(pl.name)

		# -- Response shape (only the approved keys, economic values allowed)
		self.assertEqual(
			set(result.keys()),
			{
				"sales_invoice",
				"pick_list",
				"sales_order",
				"commercial_name",
				"status",
				"item_count",
				"total_qty",
				"grand_total",
			},
		)
		self.assertEqual(result["pick_list"], pl.name)
		self.assertEqual(result["sales_order"], so.name)
		self.assertEqual(result["commercial_name"], so.name)
		self.assertEqual(result["item_count"], 1)
		self.assertEqual(flt(result["total_qty"]), 6)
		self.assertEqual(flt(result["grand_total"]), 6 * 275)

		invoice = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.world.track_existing("Sales Invoice", invoice.name)

		# docstatus / update_stock
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.update_stock, 1)

		# qty / rate / native cross-references, per line
		item_row = invoice.items[0]
		self.assertEqual(flt(item_row.qty), 6)  # picked_qty(6) - delivered_qty(0)
		self.assertEqual(flt(item_row.rate), 275)  # preserved from Sales Order Item
		self.assertEqual(item_row.against_pick_list, pl.name)
		self.assertEqual(item_row.pick_list_item, pl_row_name)
		self.assertEqual(item_row.so_detail, so_item_name)
		self.assertEqual(item_row.warehouse, self.wh.name)

		# Bin decreased by exactly the invoiced qty
		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before - 6)

		# Pick List / Sales Order native cross-references updated
		pl_after = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_after.delivery_status, "Fully Delivered")
		self.assertEqual(flt(pl_after.locations[0].delivered_qty), 6)

		so_after = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(flt(so_after.per_billed), 100)

	# -- Idempotency: Fully Delivered rejects a second call -------------------

	def test_second_call_on_fully_delivered_pick_list_is_rejected(self):
		_, pl = self._submitted_pick_list(qty=2, rate=100)
		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			result = facturacion.generate_invoice(pl.name)
			self.world.track_existing("Sales Invoice", result["sales_invoice"])

			with self.assertRaises(frappe.ValidationError):
				facturacion.generate_invoice(pl.name)

		# No second Sales Invoice was ever created against this Pick List.
		invoices = frappe.get_all(
			"Sales Invoice Item", filters={"against_pick_list": pl.name}, pluck="parent", distinct=True
		)
		self.assertEqual(len(invoices), 1)

	# -- Partly Delivered: only the real remainder gets invoiced -------------

	def test_partly_delivered_invoices_only_the_remainder(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], 10)
			bodega.finish_picking(pl.name)

		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			# First invoice: only for a partial qty -- edit the draft
			# before submitting, same as a human would in the desk UI, to
			# genuinely reach Partly Delivered (create_delivery() itself
			# always proposes the full remaining qty).
			from erpnext.stock.doctype.pick_list.pick_list import create_delivery

			first = create_delivery(pl.name, target="Sales Invoice")
			first.items[0].qty = 4
			first.save()
			self.world.track_existing("Sales Invoice", first.name)
			first.submit()

			pl_after_first = frappe.get_doc("Pick List", pl.name)
			self.assertEqual(pl_after_first.delivery_status, "Partly Delivered")

			# Second call: through the real endpoint, must invoice only
			# the genuine remainder (10 - 4 = 6), not the original 10.
			result = facturacion.generate_invoice(pl.name)
			self.world.track_existing("Sales Invoice", result["sales_invoice"])

		second = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertEqual(len(second.items), 1)
		self.assertEqual(flt(second.items[0].qty), 6)
		self.assertEqual(flt(result["total_qty"]), 6)

		pl_final = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_final.delivery_status, "Fully Delivered")
		self.assertEqual(flt(pl_final.locations[0].delivered_qty), 10)

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
		so, pl = self._submitted_pick_list(qty=5, rate=100)
		qty_before = _actual_qty(self.item.name, self.wh.name)

		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			result = facturacion.generate_invoice(pl.name)
			self.world.track_existing("Sales Invoice", result["sales_invoice"])
			invoice = frappe.get_doc("Sales Invoice", result["sales_invoice"])
			invoice.cancel()  # native cancellation mechanism -- no custom endpoint

		pl_after = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_after.delivery_status, "Not Delivered")
		self.assertEqual(flt(pl_after.locations[0].delivered_qty), 0)
		self.assertEqual(flt(pl_after.per_delivered), 0)

		so_after = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(flt(so_after.per_billed), 0)

		self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before)

		# The Pick List is billable again -- generate_invoice() succeeds a
		# second time now that the cancellation genuinely freed it up.
		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			second_result = facturacion.generate_invoice(pl.name)
			self.world.track_existing("Sales Invoice", second_result["sales_invoice"])
		self.assertNotEqual(second_result["sales_invoice"], result["sales_invoice"])

	# -- Accounting blocker: still reproducible without the test override -----

	def test_accounting_blocker_131505_still_reproducible_without_override(self):
		"""Commit 21.1's documented, deliberately-unfixed precondition:
		Company.default_income_account = "131505 - Ventas - FG" is a
		Receivable-type account, and this site's real Items have no
		income_account of their own -- so generate_invoice(), called with
		NO override active, must still fail with the same real, native
		ERPNext error. Confirms the blocker is still live on this site and
		would still block a real Facturación user in production today."""
		self.assertEqual(
			frappe.db.get_value("Company", fx.COMPANY, "default_income_account"),
			"131505 - Ventas - FG",
		)
		self.assertEqual(
			frappe.db.get_value("Account", "131505 - Ventas - FG", "account_type"), "Receivable"
		)

		_, pl = self._submitted_pick_list(qty=2, rate=100)
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.ValidationError) as ctx:
				facturacion.generate_invoice(pl.name)
		self.assertIn("Receivable account", str(ctx.exception))
		self.assertIn("131505 - Ventas - FG", str(ctx.exception))

		# The draft invoice create_delivery() built before submit() failed
		# is real, left-over residue of the (still real, unfixed) blocker
		# -- clean it up so this test leaves zero FG213-* residue like
		# every other test in this suite.
		leftover = frappe.get_all(
			"Sales Invoice Item", filters={"against_pick_list": pl.name}, pluck="parent", distinct=True
		)
		for name in leftover:
			self.world.track_existing("Sales Invoice", name)
