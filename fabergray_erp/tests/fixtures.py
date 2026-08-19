# -*- coding: utf-8 -*-
"""Shared, self-contained test fixtures for the Bodega / Jefe de Bodega test suite (Commit 8).

Deliberately does NOT rely on ERPNext's classic "_Test Company" / "_Test Item" /
"_Test Warehouse - _TC" global fixtures -- this site was never provisioned with
them, so every doctype needed here is created from scratch, scoped under the
one real Company that already exists on this site ("fabrigraysas"). Nothing
here is a test itself (no "test_" prefix), so frappe's test discovery never
picks this file up on its own.

Does NOT import any erpnext test_*.py module (e.g.
erpnext.selling.doctype.sales_order.test_sales_order): every one of those
transitively imports erpnext.stock.doctype.item.test_item, which imports the
legacy erpnext.tests.utils.ERPNextTestSuite -- whose module-level setup code
is broken in this erpnext version (LinkValidationError: Could not find Parent
Department: All Departments). Sales Orders, Pick Lists and Stock
Reconciliations are built here directly against the production doctypes
instead.

Cleanup: frappe.tests.IntegrationTestCase wraps a test class in a DB
transaction rolled back at class teardown, but that turned out not to be a
reliable cleanup mechanism on its own while writing this suite -- ERPNext's
stock ledger posting (erpnext/stock/stock_ledger.py) calls frappe.db.commit()
internally while submitting a document that posts real Stock Ledger Entries,
which commits everything created earlier in the same test for real, past any
later rollback (confirmed empirically; this is why stock_up() below seeds
Bin.actual_qty directly instead of posting a real stock movement -- see its
own docstring). TestWorld tracks every document created through it and
deletes them all explicitly in reverse (child-before-parent) order in
cleanup() instead of depending on the transaction rollback at all. This is
the one place in this whole suite that uses ignore_permissions=True --
documented right there, in TestWorld.cleanup(), test-teardown only, never in
application code.

Fixture creation always runs as Administrator (the default IntegrationTestCase
user), which already has full native permissions on every doctype touched
here for creation -- ignore_permissions is not needed for setup, only for the
teardown case documented above.
"""

from contextlib import contextmanager

import frappe
from frappe.utils import add_days, nowdate

COMPANY = "fabrigraysas"
UOM = "Nos"
TERRITORY = "All Territories"

_ITEM_GROUP = "FG8 Test Item Group"
_CUSTOMER_GROUP = "FG8 Test Customer Group"


class TestWorld:
	"""Creates and tracks every fixture document a test needs, and deletes
	them all explicitly in cleanup() -- see module docstring for why this
	does not rely on IntegrationTestCase's transaction rollback. One
	instance per test class, shared by every test method in it; register
	its cleanup once via `cls.addClassCleanup(cls.world.cleanup)`.
	"""

	def __init__(self):
		self._created = []  # [(doctype, name), ...] in creation order
		self._shared_group_docs_created = False

	def _track(self, doc):
		self._created.append((doc.doctype, doc.name))
		return doc

	def track_existing(self, doctype, name):
		"""Register a document for cleanup that was created some other way
		(e.g. directly through api.bodega._create_shortage_report()) instead
		of through one of the factory methods below."""
		self._created.append((doctype, name))

	# -- Masters ---------------------------------------------------------

	def warehouse(self, name):
		doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": COMPANY})
		doc.insert()
		return self._track(doc)

	def item(self, item_code, stock_uom=UOM):
		self._ensure_leaf_item_group()
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": _ITEM_GROUP,
				"stock_uom": stock_uom,
				"is_stock_item": 1,
			}
		)
		doc.insert()
		return self._track(doc)

	def customer(self, name):
		self._ensure_leaf_customer_group()
		doc = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": _CUSTOMER_GROUP,
				"territory": TERRITORY,
			}
		)
		doc.insert()
		return self._track(doc)

	def _ensure_leaf_item_group(self):
		# Shared across every TestWorld instance in the run; only create (and
		# track for cleanup) once per class -- re-creating per item would
		# hit a DuplicateEntryError.
		if self._shared_group_docs_created or frappe.db.exists("Item Group", _ITEM_GROUP):
			return
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": _ITEM_GROUP,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		)
		doc.insert()
		self._track(doc)

	def _ensure_leaf_customer_group(self):
		if frappe.db.exists("Customer Group", _CUSTOMER_GROUP):
			return
		doc = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": _CUSTOMER_GROUP,
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		)
		doc.insert()
		self._track(doc)

	# -- Stock -------------------------------------------------------------

	def stock_up(self, item_code, warehouse, qty, rate=100):
		"""Seed Bin.actual_qty -- the one thing Pick List's own
		validate_stock_qty() actually reads -- via ERPNext's own
		erpnext.stock.utils.get_bin() (a production helper, not a test one),
		rather than posting a real Stock Reconciliation / Stock Ledger Entry.

		Deliberately NOT using a submitted Stock Reconciliation here: tried
		that first, and hit two real problems specific to this environment --
		(1) posting a Stock Ledger Entry calls frappe.db.commit() internally
		(erpnext/stock/stock_ledger.py), defeating IntegrationTestCase's
		rollback for everything created earlier in the same test (see
		TestWorld's docstring); and (2) cancelling that Stock Reconciliation
		in cleanup() does NOT delete its Stock Ledger Entry rows -- ERPNext
		keeps cancelled SLEs as an audit trail -- which then permanently
		blocks deleting the Warehouse ("stock ledger entry exists"), by
		design, with no supported way around it. None of this app's tests
		care about real stock valuation, only about a large-enough
		Bin.actual_qty, so get_bin()+save() is the correct, minimal fixture:
		no SLE is ever created, so nothing here needs unwinding at all.
		"""
		from erpnext.stock.utils import get_bin

		bin_doc = get_bin(item_code, warehouse)
		bin_doc.actual_qty = qty
		bin_doc.valuation_rate = rate
		bin_doc.save()
		return self._track(bin_doc)

	# -- Selling / picking ---------------------------------------------------

	def submitted_sales_order(self, item_code, warehouse, qty, customer, rate=100):
		return self.multi_item_sales_order(
			customer, [{"item_code": item_code, "warehouse": warehouse, "qty": qty, "rate": rate}]
		)

	def multi_item_sales_order(self, customer, items):
		"""items: list of {"item_code", "warehouse", "qty", "rate"} dicts."""
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": items[0]["warehouse"],
				"items": [{**item, "delivery_date": delivery_date} for item in items],
			}
		)
		doc.insert()
		doc.submit()
		return self._track(doc)

	def pick_list_for(self, sales_order, warehouse):
		"""Pick List via ERPNext's own Sales Order -> Pick List mapping
		(create_pick_list), the same server method behind the standard
		"Create Pick List" button -- not a hand-built Pick List doc."""
		from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

		doc = create_pick_list(sales_order.name)
		doc.parent_warehouse = warehouse
		doc.insert()
		return self._track(doc)

	# -- Reporte de Faltante (direct, for Section 3 doctype-level tests) --

	def shortage_report(self, **kwargs):
		doc = frappe.get_doc({"doctype": "Reporte de Faltante", **kwargs})
		doc.insert()
		return self._track(doc)

	# -- Users / permissions ------------------------------------------------

	def user(self, email, roles, full_name="FG8 Test User"):
		doc = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": full_name, "send_welcome_email": 0}
		)
		doc.insert()
		for role in roles:
			doc.append("roles", {"role": role})
		doc.save()
		self._track(doc)
		return doc.name

	def warehouse_user_permission(self, user, warehouse):
		doc = frappe.get_doc(
			{"doctype": "User Permission", "user": user, "allow": "Warehouse", "for_value": warehouse}
		)
		doc.insert()
		return self._track(doc)

	# -- Cleanup -------------------------------------------------------------

	def cleanup(self):
		"""Test-teardown only. Runs as Administrator with
		ignore_permissions=True/force=True -- the one sanctioned exception to
		the "never ignore_permissions" rule (see module docstring): this
		deletes fixtures this same class created, in reverse
		(child-before-parent) order, cancelling submittable documents first.
		Never used in application code (api/bodega.py, api/jefe_bodega.py) --
		only here.
		"""
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._created):
			if not frappe.db.exists(doctype, name):
				continue
			doc = frappe.get_doc(doctype, name)
			if doc.meta.is_submittable and doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		frappe.db.commit()


@contextmanager
def as_user(user):
	"""Switch frappe.session.user for the duration of a `with` block, always
	restoring the previous user afterwards -- even if the block raises."""
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)
