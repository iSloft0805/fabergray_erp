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

#: No "Stock Adjustment"/"Temporary" account exists in this company's (real,
#: PUC-based) chart of accounts, and Company.stock_adjustment_account is
#: unset -- so a real Stock Reconciliation (stock_up_real(), Commit 10) needs
#: an explicit expense_account. Stock Ledger Entries already exist for this
#: company and purpose is never "Opening Stock" here, so
#: validate_expense_account() only requires *some* valid account, not a
#: specific report_type -- an existing Stock-type account is accepted.
STOCK_ADJUSTMENT_ACCOUNT = "149910 - Para diferencia de inventario físico - FG"
COST_CENTER = "Principal - FG"

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

	def item(self, item_code, stock_uom=UOM, default_material_request_type=None, default_warehouse=None):
		"""default_warehouse (Commit 18.2): appends a row to the native
		Item.item_defaults child table for COMPANY -- the same metadata
		api/ventas.py's own _default_warehouse_for_item() reads
		(frappe.db.get_value("Item Default", ...)) to resolve a warehouse
		for a Vendedora-submitted Sales Order line without ever asking her
		to pick one. Optional and additive -- every pre-existing caller
		that doesn't pass it keeps getting exactly the same Item as before.
		"""
		self._ensure_leaf_item_group()
		item_dict = {
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": _ITEM_GROUP,
			"stock_uom": stock_uom,
			"is_stock_item": 1,
			"is_sales_item": 1,
		}
		if default_material_request_type:
			item_dict["default_material_request_type"] = default_material_request_type
		doc = frappe.get_doc(item_dict)
		if default_warehouse:
			doc.append("item_defaults", {"company": COMPANY, "default_warehouse": default_warehouse})
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

	def stock_up_real(self, item_code, warehouse, qty, rate=100):
		"""Seed stock through a real, submitted Stock Reconciliation --
		unlike stock_up() (Bin.actual_qty only), this is required for Stock
		Reservation Entry tests (Commit 10): get_available_qty_to_reserve()
		calls ERPNext's own get_stock_balance(), which reads real Stock
		Ledger Entries via get_previous_sle() -- Bin.actual_qty alone is not
		enough for Stock Reservation to see anything as available (confirmed
		empirically; stock_up() would silently reserve 0).

		This reintroduces the two consequences stock_up()'s docstring
		describes avoiding: frappe.db.commit() fires internally during
		submit, and cancelling this Stock Reconciliation in cleanup() will
		not delete its Stock Ledger Entry rows. cleanup() below handles the
		second one directly (purging cancelled SLE/GL Entry rows for a
		Warehouse this same TestWorld created, right before deleting it --
		never touching a warehouse this suite didn't create itself); the
		first one is why every warehouse/item created for a Stock Reservation
        test must go through this TestWorld's explicit cleanup(), the same
		as every other suite in this app -- nothing here is new risk, just a
		second confirmed instance of the same root cause.
		"""
		doc = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": COMPANY,
				"purpose": "Stock Reconciliation",
				"expense_account": STOCK_ADJUSTMENT_ACCOUNT,
				"cost_center": COST_CENTER,
				"items": [
					{"item_code": item_code, "warehouse": warehouse, "qty": qty, "valuation_rate": rate}
				],
			}
		)
		doc.insert()
		doc.submit()
		return self._track(doc)

	# -- Manufacturing (Commit 12) -------------------------------------------

	def bom_for(self, item_code, raw_material_item_code, qty=1, rate=10):
		"""A minimal, submitted, active BOM for item_code -- for
		analyzer.get_default_bom() lookups. is_active/is_default both default
		to 1 on a fresh BOM (bom.py), so the first one created for an item is
		automatically its default -- no extra field needed."""
		currency = frappe.db.get_value("Company", COMPANY, "default_currency")
		doc = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item_code,
				"quantity": 1,
				"company": COMPANY,
				"currency": currency,
				"conversion_rate": 1,
				"items": [{"item_code": raw_material_item_code, "qty": qty, "uom": UOM, "rate": rate}],
			}
		)
		doc.insert()
		doc.submit()
		return self._track(doc)

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
		with without_sales_order_hook():
			doc.submit()
		self.track_existing_pick_lists_and_reports_for(doc.name)
		return self._track(doc)

	def track_existing_pick_lists_and_reports_for(self, sales_order_name):
		"""Commit 16: Sales Order.on_submit is now wired to
		process_sales_order() (hooks.py doc_events) -- every Sales Order
		submitted (through this fixture, or directly by a test exercising
		the real hook in test_sales_order_hook.py) may automatically gain a
		Pick List and/or a Reporte de Faltante as a side effect of that same
		submit() call, created by code this TestWorld instance didn't call
		directly. Without tracking them, that submit's own side effects
		would leave real, untracked residue behind. Public (not the
		`_`-prefixed internal-only convention every other TestWorld helper
		uses) because test_sales_order_hook.py -- which submits Sales
		Orders directly, not through multi_item_sales_order() -- needs to
		call this too. Safe to query unconditionally right after submit --
		the Sales Order didn't exist before this same call, so anything
		linked to it now is new."""
		for name in frappe.get_all(
			"Pick List Item", filters={"sales_order": sales_order_name}, pluck="parent", distinct=True
		):
			self.track_existing("Pick List", name)

		for name in frappe.get_all("Reporte de Faltante", filters={"sales_order": sales_order_name}, pluck="name"):
			self.track_existing("Reporte de Faltante", name)

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
			if doctype == "Warehouse":
				self._purge_stock_ledger_for_warehouse(name)
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	@staticmethod
	def _purge_stock_ledger_for_warehouse(warehouse):
		"""Test-teardown only (see stock_up_real()'s docstring): a Warehouse
		that had a real, now-cancelled stock movement can never be deleted
		through the normal API -- ERPNext preserves cancelled Stock Ledger
		Entries as an audit trail by design, and Warehouse.on_trash() blocks
		on their mere existence, regardless of docstatus. Every Stock
		Ledger/GL Entry this suite ever posts is against a warehouse this
		same TestWorld created and is about to delete entirely, so purging
		them here never touches any real accounting history -- only this
		run's own throwaway test transactions. No-op (three empty deletes)
		for a warehouse that only ever used stock_up() (Bin-only, no real
		Stock Ledger Entry) or stock_up() (never called), so it's always
		safe to call unconditionally before deleting any Warehouse.
		"""
		vouchers = frappe.get_all(
			"Stock Ledger Entry",
			filters={"warehouse": warehouse},
			fields=["voucher_type", "voucher_no"],
			distinct=True,
		)
		for voucher in vouchers:
			frappe.db.delete(
				"GL Entry", {"voucher_type": voucher.voucher_type, "voucher_no": voucher.voucher_no}
			)
		frappe.db.delete("Stock Ledger Entry", {"warehouse": warehouse})
		frappe.db.delete("Bin", {"warehouse": warehouse})


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


@contextmanager
def without_sales_order_hook():
	"""Commit 16: Sales Order.on_submit is now wired (hooks.py doc_events)
	to fabergray_erp.fulfillment.sales_order_hooks.on_submit ->
	process_sales_order(). Every test file written before Commit 16 (and
	most written since) submits Sales Orders through TestWorld's own
	fixtures expecting a clean, unprocessed order to then drive manually
	through analyze_sales_order()/create_pick_list_for_available_stock()/
	sync_shortage_reports_for_sales_order()/process_sales_order() -- if the
	hook fired during that fixture's own submit(), it would have already
	claimed stock and/or created a Reporte de Faltante, breaking those
	tests' assumptions. multi_item_sales_order() wraps its own submit()
	call in this context manager so it keeps behaving exactly as it always
	has; only test_sales_order_hook.py builds and submits its Sales Orders
	directly (bypassing this fixture) specifically to exercise the real
	hook.

	Implemented by directly overriding frappe.get_doc_hooks()'s own
	thread-local cache (frappe.local.doc_events_hooks) -- the exact
	registry Document.hook() (frappe/model/document.py) itself populates
	and reads on every doc_events dispatch -- not a custom hook-bypass
	mechanism of this app's own invention."""
	doc_events_hooks = frappe.get_doc_hooks()
	original = doc_events_hooks.get("Sales Order", {}).pop("on_submit", None)
	try:
		yield
	finally:
		if original is not None:
			doc_events_hooks.setdefault("Sales Order", {})["on_submit"] = original


@contextmanager
def stock_settings(**overrides):
	"""Temporarily override Stock Settings singleton fields (e.g.
	enable_stock_reservation, auto_reserve_stock), restoring the original
	values afterwards -- even if the block raises. Stock Settings is a
	site-wide Single, not scoped to a transaction, so tests that need a
	specific value active must restore it deterministically rather than
	relying on IntegrationTestCase's (unreliable, see TestWorld's docstring)
	rollback. frappe.clear_cache() is called after every write since some
	native code paths read this via frappe.get_cached_value(...)."""
	originals = {field: frappe.db.get_single_value("Stock Settings", field) for field in overrides}
	try:
		for field, value in overrides.items():
			frappe.db.set_single_value("Stock Settings", field, value)
		frappe.clear_cache(doctype="Stock Settings")
		yield
	finally:
		for field, value in originals.items():
			frappe.db.set_single_value("Stock Settings", field, value)
		frappe.clear_cache(doctype="Stock Settings")
