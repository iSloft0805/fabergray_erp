# -*- coding: utf-8 -*-
"""Commit 21.1 -- functional test of the native Pick List -> Sales Invoice
flow under a real, restricted Facturación session.

Scope (per the approved brief): prove the native ERPNext mechanism (ERPNext's
own erpnext.stock.doctype.pick_list.pick_list.create_delivery(...,
target="Sales Invoice")) works end to end for a real Facturación user with
ONLY the 6 granted permissions -- no ignore_permissions, no api/facturacion.py
(that module does not exist yet, deliberately out of scope this commit), no
new production code at all. Also documents, with a live reproduction, the
accounting precondition found during the live audit: Company
(fabrigraysas).default_income_account = "131505 - Ventas - FG" is an
account_type="Receivable" account, which blocks ANY Sales Invoice with
update_stock=1 whose items have no income_account of their own -- fixed here
only inside tests, via fx.company_defaults() (temporary, restored
afterwards), exactly as instructed. NOT corrected on the real site.

Also documents, as a passing contract test grounded directly in ERPNext's own
status_map (erpnext/controllers/status_updater.py), why the Facturación queue
must gate on `Pick List.docstatus == 1 and delivery_status != "Fully
Delivered"` and never on `Pick List.status == "Completed"` -- for a
purpose="Delivery" Pick List, "Completed" is the status AFTER everything has
already been invoiced (nothing left to do), the exact opposite of "ready to
invoice".

Finally, a guardrail test proving (not yet enforcing -- generate_invoice()
does not exist yet) that a Pick List spanning more than one Sales Order is
mechanically detectable from Pick List Item.sales_order alone, for a future
service to reject.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock.doctype.pick_list.pick_list import create_delivery
from erpnext.stock.utils import get_bin

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

#: A real, leaf, account_type="Income Account" account in this company's
#: chart of accounts -- used ONLY inside tests (fx.company_defaults()) to
#: temporarily work around the real site's misconfigured
#: default_income_account, never written to the real site itself.
_VALID_INCOME_ACCOUNT = "413520 - Venta de productos en almacenes no especializados - FG"


def _actual_qty(item_code, warehouse):
	return flt(get_bin(item_code, warehouse).actual_qty)


class TestFacturacionAccountingPrecondition(IntegrationTestCase):
	"""Documents, with a live reproduction, the accounting precondition the
	brief asked to be documented (not fixed) before Commit 21.1: this site's
	real Company.default_income_account is a Receivable-type account, which
	blocks any stock-updating Sales Invoice whose items don't carry their
	own income_account."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG21 Precondición")
		# Deliberately no income_account on this Item -- reproduces the real
		# site's real Items, none of which have one set either.
		cls.item = cls.world.item("FG21-PRECOND-ITEM")
		cls.customer = cls.world.customer("FG21 Precondición Customer")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 50, rate=50)

	def test_default_income_account_is_receivable_type(self):
		"""The precondition itself, confirmed live against the real site
		configuration (not assumed) -- must never be corrected by this
		commit."""
		default_income_account = frappe.db.get_value(
			"Company", fx.COMPANY, "default_income_account"
		)
		self.assertEqual(default_income_account, "131505 - Ventas - FG")
		self.assertEqual(
			frappe.db.get_value("Account", default_income_account, "account_type"), "Receivable"
		)

	def test_stock_updating_invoice_blocked_by_real_site_configuration(self):
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"update_stock": 1,
				"items": [
					{"item_code": self.item.name, "qty": 5, "rate": 100, "warehouse": self.wh.name}
				],
			}
		)
		si.insert()
		self.world.track_existing("Sales Invoice", si.name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			si.submit()
		# Real, live error text -- confirms this is exactly the Receivable/
		# income-account mismatch, not some unrelated failure.
		self.assertIn("Receivable account", str(ctx.exception))
		self.assertIn("131505 - Ventas - FG", str(ctx.exception))

	def test_same_invoice_succeeds_with_temporary_income_account_override(self):
		"""Proves the ONLY sanctioned workaround for this commit: a
		temporary, test-scoped override via fx.company_defaults(), restored
		automatically at the end of the `with` block -- never a permanent
		change to Company.default_income_account. The override must be
		active for BOTH insert() and submit() -- confirmed live, not
		assumed: Sales Invoice Item.income_account is resolved once, at
		insert() time (set_missing_values()), and simply carried as-is into
		the GL entries at submit() time, never re-resolved -- inserting
		first against the real (Receivable) default and only wrapping
		submit() in the override still fails with the exact same error,
		because the item row already has the wrong income_account baked in
		by then."""
		with fx.company_defaults(default_income_account=_VALID_INCOME_ACCOUNT):
			si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": self.customer.name,
					"company": fx.COMPANY,
					"update_stock": 1,
					"items": [
						{"item_code": self.item.name, "qty": 5, "rate": 100, "warehouse": self.wh.name}
					],
				}
			)
			si.insert()
			self.world.track_existing("Sales Invoice", si.name)
			si.submit()  # must not raise
			self.assertEqual(si.docstatus, 1)
			si.cancel()

		# Restored automatically -- confirmed, not assumed.
		self.assertEqual(
			frappe.db.get_value("Company", fx.COMPANY, "default_income_account"),
			"131505 - Ventas - FG",
		)


class TestFacturacionFullFlow(IntegrationTestCase):
	"""The mandatory functional test: Pick List submitted -> create_delivery
	(target="Sales Invoice") -> submit -> cancel, entirely under a real,
	restricted Facturación session (no ignore_permissions anywhere in this
	test)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG21 Flujo")
		cls.item = cls.world.item("FG21-FLOW-ITEM")
		cls.customer = cls.world.customer("FG21 Flujo Customer")
		# Real Stock Ledger history (not just Bin.actual_qty) -- required so
		# the Sales Invoice's own update_stock=1 consumption has a real
		# previous balance to draw down from, same reasoning as Commit 10's
		# stock_up_real().
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 100, rate=50)

		cls.bodega_user = cls.world.user("fg21-bodega-flow@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg21-facturacion-flow@example.com", ["Facturación"])

	def _submitted_pick_list(self, qty, rate):
		"""Fresh Sales Order (1 line) -> Pick List -> fully picked and
		submitted via the real api.bodega.* flow (Commit 8) -- exactly how a
		real Pick List reaches Facturación's queue precondition
		(docstatus==1)."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def test_full_flow_create_submit_cancel(self):
		so, pl = self._submitted_pick_list(qty=5, rate=250)
		so_item_name = so.items[0].name
		pl_row_name = pl.locations[0].name

		# -- Precondition: Pick List submitted, nothing delivered yet ------
		self.assertEqual(pl.docstatus, 1)
		self.assertEqual(pl.delivery_status, "Not Delivered")
		self.assertEqual(flt(pl.per_delivered), 0)
		self.assertEqual(flt(pl.locations[0].delivered_qty), 0)

		so_before = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(flt(so_before.per_billed), 0)
		self.assertEqual(so_before.billing_status, "Not Billed")

		qty_before = _actual_qty(self.item.name, self.wh.name)

		# -- Real Facturación session, real native flow, temporary
		# accounting override (see TestFacturacionAccountingPrecondition) --
		with fx.as_user(self.facturacion_user), fx.company_defaults(
			default_income_account=_VALID_INCOME_ACCOUNT
		):
			si = create_delivery(pl.name, target="Sales Invoice")
			self.world.track_existing("Sales Invoice", si.name)

			# -- Draft Sales Invoice: native field mapping from Pick List --
			self.assertEqual(si.update_stock, 1)
			self.assertEqual(len(si.items), 1)
			si_item = si.items[0]
			self.assertEqual(flt(si_item.qty), 5)  # picked_qty(5) - delivered_qty(0)
			self.assertEqual(flt(si_item.rate), 250)  # preserved from Sales Order Item
			self.assertEqual(si_item.against_pick_list, pl.name)
			self.assertEqual(si_item.pick_list_item, pl_row_name)
			self.assertEqual(si_item.so_detail, so_item_name)
			self.assertEqual(si_item.warehouse, self.wh.name)

			si.submit()
			self.assertEqual(si.docstatus, 1)

			# -- After submit: Pick List / Sales Order / Bin all updated
			# through ERPNext's own native update_prevdoc_status() chain --
			pl_after = frappe.get_doc("Pick List", pl.name)
			self.assertEqual(flt(pl_after.locations[0].delivered_qty), 5)
			self.assertEqual(pl_after.delivery_status, "Fully Delivered")
			self.assertEqual(flt(pl_after.per_delivered), 100)
			# Confirms the queue-rule contract test below, live: fully
			# delivered now genuinely means "Completed".
			self.assertEqual(pl_after.status, "Completed")

			so_after = frappe.get_doc("Sales Order", so.name)
			self.assertEqual(flt(so_after.per_billed), 100)
			self.assertEqual(so_after.billing_status, "Fully Billed")

			self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before - 5)

			si.cancel()
			self.assertEqual(si.docstatus, 2)

			# -- After cancel: everything back to its initial state --------
			pl_final = frappe.get_doc("Pick List", pl.name)
			self.assertEqual(flt(pl_final.locations[0].delivered_qty), 0)
			self.assertEqual(pl_final.delivery_status, "Not Delivered")
			self.assertEqual(flt(pl_final.per_delivered), 0)
			self.assertEqual(pl_final.status, "Open")

			so_final = frappe.get_doc("Sales Order", so.name)
			self.assertEqual(flt(so_final.per_billed), 0)
			self.assertEqual(so_final.billing_status, "Not Billed")

			self.assertEqual(_actual_qty(self.item.name, self.wh.name), qty_before)


class TestFacturacionQueueContract(IntegrationTestCase):
	"""Grounds the corrected queue rule directly in ERPNext's own
	status_map["Pick List"] (erpnext/controllers/status_updater.py): for a
	purpose="Delivery" Pick List, `status == "Completed"` means "fully
	delivered, nothing left to invoice" -- the OPPOSITE of "ready to
	invoice". A freshly-submitted, nothing-delivered-yet Pick List's status
	is "Open", not "Completed". Using `status == "Completed"` as the
	Facturación entry condition would show nothing to invoice at exactly the
	moment there is the most to invoice, and would keep showing a Pick List
	forever after it's fully invoiced. The correct, already-corrected
	condition is `docstatus == 1 and delivery_status != "Fully Delivered"`.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG21 Contrato Cola")
		cls.item = cls.world.item("FG21-QUEUE-ITEM")
		cls.customer = cls.world.customer("FG21 Contrato Cola Customer")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 100, rate=50)

		cls.bodega_user = cls.world.user("fg21-bodega-queue@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	@staticmethod
	def _is_ready_to_invoice(pick_list_doc):
		"""The approved contract, as a pure function -- not production code
		yet (no api/facturacion.py this commit), used only to prove the
		condition here."""
		return pick_list_doc.docstatus == 1 and pick_list_doc.delivery_status != "Fully Delivered"

	def test_freshly_submitted_pick_list_is_open_not_completed(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 5, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)

		pl_doc = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_doc.docstatus, 1)
		self.assertEqual(pl_doc.status, "Open")
		self.assertNotEqual(pl_doc.status, "Completed")
		self.assertEqual(pl_doc.delivery_status, "Not Delivered")

		# The naive (wrong) condition would WRONGLY exclude this ready-to-
		# invoice Pick List:
		self.assertFalse(pl_doc.status == "Completed")
		# The corrected condition correctly INCLUDES it:
		self.assertTrue(self._is_ready_to_invoice(pl_doc))

	def test_fully_invoiced_pick_list_is_completed_and_excluded(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 5, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)

		with fx.company_defaults(default_income_account=_VALID_INCOME_ACCOUNT):
			si = create_delivery(pl.name, target="Sales Invoice")
			self.world.track_existing("Sales Invoice", si.name)
			si.submit()

			pl_doc = frappe.get_doc("Pick List", pl.name)
			self.assertEqual(pl_doc.status, "Completed")
			self.assertEqual(pl_doc.delivery_status, "Fully Delivered")

			# Now that it's genuinely done, the corrected condition
			# correctly EXCLUDES it -- nothing left to invoice:
			self.assertFalse(self._is_ready_to_invoice(pl_doc))

			si.cancel()  # restore, so class cleanup can delete cleanly


class TestFacturacionMultiSalesOrderGuardrail(IntegrationTestCase):
	"""A future generate_invoice() must reject a Pick List spanning more
	than one Sales Order (explicit brief instruction: validate the
	logic/condition only, do not build generate_invoice() yet). This proves
	the condition is mechanically detectable from Pick List Item.sales_order
	alone, for two real, submitted Sales Orders sharing one Pick List --
	exactly the shape ERPNext's own "Get Item Locations" multi-order picking
	produces (create_delivery() itself already handles this case internally
	by grouping per Sales Order, erpnext/stock/doctype/pick_list/
	pick_list.py's create_delivery())."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG21 Multi SO")
		cls.item = cls.world.item("FG21-MULTISO-ITEM")
		cls.customer = cls.world.customer("FG21 Multi SO Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)

	@staticmethod
	def _distinct_sales_orders(pick_list_doc):
		return {row.sales_order for row in pick_list_doc.locations if row.sales_order}

	def test_single_sales_order_pick_list_has_one_distinct_sales_order(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 5, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		self.assertEqual(len(self._distinct_sales_orders(pl)), 1)

	def test_multi_sales_order_pick_list_is_detectable(self):
		so_1 = self.world.submitted_sales_order(self.item.name, self.wh.name, 3, self.customer.name)
		so_2 = self.world.submitted_sales_order(self.item.name, self.wh.name, 4, self.customer.name)

		pl = frappe.get_doc(
			{
				"doctype": "Pick List",
				"company": fx.COMPANY,
				"purpose": "Delivery",
				"parent_warehouse": self.wh.name,
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

		distinct = self._distinct_sales_orders(pl)
		self.assertEqual(len(distinct), 2)
		self.assertEqual(distinct, {so_1.name, so_2.name})
		# The condition a future generate_invoice() must apply:
		self.assertTrue(len(distinct) > 1, "must be rejected by a future generate_invoice()")
