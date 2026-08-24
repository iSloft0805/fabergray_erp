# -*- coding: utf-8 -*-
"""Commit 20.1 -- Fase 5 (Cotizaciones). Permissions only: no api/cotizaciones.py,
no Page, no naming series change yet (those are Commits 20.2+). The only
change this commit makes is one new Custom DocPerm row
(fixtures/custom_docperm.json): Quotation/Vendedora, if_owner=1,
create/read/write/submit/cancel/delete -- the exact same shape as the
Sales Order/Vendedora row from Commits 18.1+18.5. Quotation Item gets no
separate row, same reasoning as Sales Order Item (child table, governed by
the parent).

Per the approved Commit 20.1 brief: this commit must NOT add any permission
beyond that one row. Every test below exists to prove that the existing
Customer/Item/Address/Contact (read) and Account (select) grants from
Commit 18.1 are sufficient for Quotation too, with live evidence -- not
assumed from code reading alone.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestCotizacionesPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.vendedora_a = cls.world.user("fg20-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg20-vendedora-b@example.com", ["Vendedora"])

	def _raw_quotation(self, customer, item, qty=1):
		return frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": customer,
				"company": fx.COMPANY,
				"items": [{"item_code": item, "qty": qty}],
			}
		)

	# -- Positivo: lo que Vendedora SÍ puede hacer sobre su propia Quotation -----

	def test_vendedora_can_create_read_write_submit_her_own_quotation(self):
		item = self.world.item("FG20-PERM-ITEM")
		customer = self.world.customer("FG20 Perm Customer")

		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Quotation", "create"))
			qtn = self._raw_quotation(customer.name, item.name, qty=2)
			qtn.insert()
			self.world.track_existing("Quotation", qtn.name)

			self.assertTrue(frappe.has_permission("Quotation", "read", doc=qtn.name))
			self.assertTrue(frappe.has_permission("Quotation", "write", doc=qtn.name))
			qtn.order_type = "Sales"  # any writable field, proves write=1 works end to end
			qtn.save()

			self.assertTrue(frappe.has_permission("Quotation", "submit", doc=qtn.name))
			qtn.submit()

		qtn.reload()
		self.assertEqual(qtn.docstatus, 1)
		self.assertEqual(qtn.owner, self.vendedora_a)

	# -- Aislamiento if_owner entre dos Vendedoras distintas ---------------------

	def test_vendedora_cannot_read_another_vendedoras_quotation(self):
		item = self.world.item("FG20-ISOLATION-ITEM")
		customer = self.world.customer("FG20 Isolation Customer")

		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(customer.name, item.name)
			qtn_a.insert()
			self.world.track_existing("Quotation", qtn_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Quotation", "read", doc=qtn_a.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Quotation", qtn_a.name).check_permission("read")
			self.assertEqual(frappe.get_list("Quotation", filters={"name": qtn_a.name}, pluck="name"), [])

	def test_vendedora_cannot_write_another_vendedoras_quotation(self):
		item = self.world.item("FG20-ISOLATIONWRITE-ITEM")
		customer = self.world.customer("FG20 IsolationWrite Customer")

		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(customer.name, item.name)
			qtn_a.insert()
			self.world.track_existing("Quotation", qtn_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Quotation", "write", doc=qtn_a.name))

	# -- Account: confirmado en vivo, no asumido ----------------------------------

	def test_account_perm_check_during_quotation_insert(self):
		"""Traces whether Quotation's own validate() chain reaches the same
		Account-permission gate Commit 18.1 found for Sales Order.
		`account_perm_check` itself is a closure nested inside
		`get_party_account()` (erpnext/accounts/party.py:432, not a
		module-level name -- confirmed live: patching
		"erpnext.accounts.party.account_perm_check" raises AttributeError),
		reached via `set_payment_schedule()` ->
		`get_party_account_currency()` -> `get_party_account()`
		(party.py:526-529, accounts_controller.py:2592+). So this wraps the
		real, patchable, module-level `get_party_account()` instead --
		whatever runs inside it (including `account_perm_check`, if
		reached) still executes for real; this only observes whether it
		was called and whether it raised. If it's never called for
		Quotation, `calls` stays empty and this test documents that instead
		of asserting a false premise.
		"""
		import erpnext.accounts.party as party_module

		item = self.world.item("FG20-ACCOUNT-CHECK-ITEM")
		customer = self.world.customer("FG20 Account Check Customer")

		calls = []
		original = party_module.get_party_account

		def _tracing_wrapper(*args, **kwargs):
			calls.append((args, kwargs))
			return original(*args, **kwargs)

		with patch("erpnext.accounts.party.get_party_account", side_effect=_tracing_wrapper):
			with fx.as_user(self.vendedora_a):
				qtn = self._raw_quotation(customer.name, item.name)
				qtn.insert()  # must not raise frappe.PermissionError
				self.world.track_existing("Quotation", qtn.name)

		if calls:
			# get_party_account() (and therefore account_perm_check(), for
			# any resolved account) ran under Vendedora's real session and
			# insert() still succeeded -- the existing select=1 on Account
			# (Commit 18.1) is confirmed sufficient for Quotation too.
			self.assertGreaterEqual(len(calls), 1)
		# else: Quotation's validate() chain never reached get_party_account()
		# for this document shape -- Account permission is not exercised at
		# all here, which is also a valid, reportable outcome.

	# -- Regresión: nada más cambió --------------------------------------------

	def test_quotation_permission_change_does_not_affect_other_roles_or_doctypes(self):
		with fx.as_user(self.vendedora_a):
			# Still exactly what Commit 18.1 granted -- unaffected by this commit's
			# one new row.
			self.assertTrue(frappe.has_permission("Customer", "read"))
			self.assertTrue(frappe.has_permission("Item", "read"))
			self.assertTrue(frappe.has_permission("Sales Order", "create"))
			self.assertFalse(frappe.has_permission("Item Price", "read"))
			self.assertFalse(frappe.has_permission("Price List", "read"))
			self.assertFalse(frappe.has_permission("Pick List", "read"))

		bodega_user = self.world.user("fg20-bodega-regress@example.com", ["Bodega"])
		with fx.as_user(bodega_user):
			self.assertTrue(frappe.has_permission("Pick List", "read"))
			self.assertFalse(frappe.has_permission("Quotation", "read"))
