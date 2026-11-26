# -*- coding: utf-8 -*-
"""invoice_numbering_admin -- controlled, idempotent and auditable procedure
to set the next internal invoice number of integrandoMAS / ecoluminar.

Runs against the TestWorld's own isolated series (fixtures.TestWorld patches
invoice_issuers.INVOICE_NUMBERING with FG-FACT-TEST-<ns>- keys), so the real
series are never read for writing nor moved. The Deleted Document source is
mocked where a test needs determinism (the local site keeps thousands of
deleted test Pick Lists)."""

import os
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, nowdate

from fabergray_erp import invoice_issuers, invoice_numbering_admin as admin
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_facturacion_invoice_payment_terms as terms_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

ECO = "ecoluminar"
INT = "integrandoMAS"
TCC = "3CORAZONESCAFES"
NO_DELETED = ([], [])


class TestInvoiceNumberingAdmin(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FGNUM {sfx} WH")
		cls.item = cls.world.item(f"FGNUM-{sfx}-A")
		cls.customer = cls.world.customer(f"FGNUM {sfx} Cliente")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.bodega_user = cls.world.user(f"fgnum-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgnum-{sfx}-facturacion@example.com", ["Facturación"])
		cls.plain_user = cls.world.user(f"fgnum-{sfx}-plain@example.com", ["Facturación"])

	_reviewed_pick_list = terms_base.TestFacturacionInvoicePaymentTerms._reviewed_pick_list
	_invoice = terms_base.TestFacturacionInvoicePaymentTerms._invoice

	def setUp(self):
		frappe.set_user("Administrator")
		self._activity_before = self._activity_count()

	def tearDown(self):
		frappe.set_user("Administrator")
		# Test-only rows of this TestWorld's series + their audit entries.
		for name in frappe.get_all(
			"Activity Log", filters={"subject": ["like", "Ajuste de consecutivo%"], "creation": [">=", nowdate()]}, pluck="name"
		):
			frappe.delete_doc("Activity Log", name, ignore_permissions=True, force=True)

	# -- helpers -----------------------------------------------------------------
	def _series(self, issuer):
		return self.world.invoice_numbering[issuer]["series"]

	def _current(self, issuer):
		return admin._series_current(self._series(issuer))

	def _set_current(self, issuer, value):
		# Test setup: this TestWorld's OWN series row (never a real one).
		frappe.db.sql(
			"INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `current` = %s",
			(self._series(issuer), value, value),
		)

	def _activity_count(self):
		return frappe.db.count("Activity Log", {"subject": ["like", "Ajuste de consecutivo%"]})

	def _real_series_snapshot(self):
		return frappe.db.sql("SELECT name, current FROM `tabSeries` WHERE name LIKE 'FG-FACT-%%' AND name NOT LIKE 'FG-FACT-TEST-%%' ORDER BY name")

	def _apply(self, issuer, number, confirm=1, reason="Consecutivo solicitado por el negocio (prueba)"):
		return admin.apply_next_invoice_number(issuer, number, reason=reason, confirm=confirm)

	# -- 1. read-only audit ------------------------------------------------------

	def test_01_audit_is_read_only_and_reports_every_issuer(self):
		series_before = frappe.db.sql("SELECT name, current FROM `tabSeries` ORDER BY name")
		report = admin.audit_invoice_numbering(targets={INT: 7055, ECO: 2451})
		self.assertEqual([a["issuer"] for a in report["issuers"]], list(invoice_issuers.INVOICE_ISSUERS))
		for a in report["issuers"]:
			for key in ("series_current", "assigned_max", "duplicated_numbers", "deleted_max", "highest_used"):
				self.assertIn(key, a)
		eco = next(a for a in report["issuers"] if a["issuer"] == ECO)
		self.assertEqual(eco["target"], 2451)
		self.assertFalse(next(a for a in report["issuers"] if a["issuer"] == TCC)["adjustable"])
		self.assertEqual(frappe.db.sql("SELECT name, current FROM `tabSeries` ORDER BY name"), series_before)
		self.assertEqual(self._activity_count(), self._activity_before)

	# -- 2. dry run by default ---------------------------------------------------

	def test_02_dry_run_writes_nothing(self):
		before = self._current(ECO)
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			result = admin.apply_next_invoice_number(ECO, 2451, reason="solo revisar")
		self.assertTrue(result["dry_run"])
		self.assertIn(result["status"], (admin.STATUS_SAFE, admin.STATUS_BLOCKED, admin.STATUS_ALREADY_APPLIED))
		self.assertEqual(self._current(ECO), before)
		self.assertEqual(self._activity_count(), self._activity_before)

	# -- 3. safe forward adjustment, exact next number, audit ----------------------

	def test_03_safe_adjustment_sets_the_exact_next_number(self):
		real_before = self._real_series_snapshot()
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			result = self._apply(ECO, 2451)
		self.assertTrue(result["applied"])
		self.assertEqual(result["series_current_after"], 2450)
		self.assertEqual(self._current(ECO), 2450)
		log = frappe.get_last_doc("Activity Log", filters={"subject": ["like", "Ajuste de consecutivo%"]})
		self.assertIn("ecoluminar", log.subject)
		self.assertIn('"next_number": 2451', log.content)
		self.assertIn("Consecutivo solicitado", log.content)
		self.assertEqual(log.user, "Administrator")
		# FACTURAR now issues exactly 2451, then 2452.
		for expected in (2451, 2452):
			pl_name = self._reviewed_pick_list()
			result = self._invoice(pl_name, issuer=ECO, payment_method="Contado", due_date=nowdate())
			self.assertEqual(result["fg_invoice_number"], expected)
		# Other series untouched (real and test).
		self.assertEqual(self._real_series_snapshot(), real_before)
		self.assertIsNone(self._current(TCC))

	def test_04_idempotent(self):
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			self._apply(ECO, 2600)
			logs = self._activity_count()
			again = self._apply(ECO, 2600)
		self.assertFalse(again["applied"])
		self.assertEqual(again["status"], admin.STATUS_ALREADY_APPLIED)
		self.assertEqual(self._current(ECO), 2599)
		self.assertEqual(self._activity_count(), logs)

	# -- 4. never backwards / never a used number ----------------------------------

	def test_05_backwards_is_blocked_like_7055_after_7131(self):
		"""Production shows integrandoMAS 7131: asking for 7055 must stop."""
		self._set_current(INT, 7131)
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			plan = admin.plan_next_invoice_number(INT, 7055)
			self.assertEqual(plan["status"], admin.STATUS_BLOCKED)
			self.assertTrue(any("no es seguro" in r for r in plan["reasons"]))
			self.assertGreaterEqual(plan["safe_alternative"], 7132)
			with self.assertRaises(admin.InvoiceNumberingAdjustmentError):
				self._apply(INT, 7055)
		self.assertEqual(self._current(INT), 7131)
		self.assertEqual(self._activity_count(), self._activity_before)

	def test_06_number_already_assigned_is_blocked(self):
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			self._apply(ECO, 2700)
			pl_name = self._reviewed_pick_list()
			number = self._invoice(pl_name, issuer=ECO, payment_method="Contado", due_date=nowdate())["fg_invoice_number"]
			self.assertEqual(number, 2700)
			for target in (2700, 2650, 2701 - 1):
				plan = admin.plan_next_invoice_number(ECO, target)
				self.assertEqual(plan["status"], admin.STATUS_BLOCKED, target)
				self.assertEqual(plan["safe_alternative"], 2701)
		self.assertEqual(self._current(ECO), 2700)

	def test_07_cancelled_and_deleted_numbers_also_block(self):
		with patch.object(admin, "_deleted_numbers", return_value=([3000], [])):
			plan = admin.plan_next_invoice_number(ECO, 2900)
		self.assertEqual(plan["status"], admin.STATUS_BLOCKED)
		self.assertEqual(plan["safe_alternative"], 3001)
		# Deleted Pick Lists of the isolated TEST series never block.
		with patch.object(admin, "_deleted_numbers", return_value=([], [9999])):
			self.assertNotEqual(admin.plan_next_invoice_number(ECO, 2900)["status"], admin.STATUS_BLOCKED)

	def test_08_duplicated_numbers_block_any_adjustment(self):
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			self._apply(ECO, 2800)
			first = self._reviewed_pick_list()
			self._invoice(first, issuer=ECO, payment_method="Contado", due_date=nowdate())
			second = self._reviewed_pick_list()
			self._invoice(second, issuer=ECO, payment_method="Contado", due_date=nowdate())
			original = frappe.db.get_value("Pick List", second, ["fg_invoice_number", "fg_invoice_number_key"], as_dict=True)
			# Test setup: an inconsistent duplicate (different key, same number).
			frappe.db.set_value(
				"Pick List", second, {"fg_invoice_number": 2800, "fg_invoice_number_key": "DUP-TEST"}, update_modified=False
			)
			try:
				plan = admin.plan_next_invoice_number(ECO, 5000)
				with self.assertRaises(admin.InvoiceNumberingAdjustmentError):
					self._apply(ECO, 5000)
			finally:
				frappe.db.set_value("Pick List", second, original, update_modified=False)
		self.assertEqual(plan["status"], admin.STATUS_BLOCKED)
		self.assertTrue(any("duplicados" in r for r in plan["reasons"]))

	def test_09_below_configured_start_is_blocked(self):
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			plan = admin.plan_next_invoice_number(ECO, 100)
		self.assertEqual(plan["status"], admin.STATUS_BLOCKED)

	# -- 5. 3CORAZONESCAFES, permissions, reason ----------------------------------

	def test_10_tres_corazones_is_never_adjusted(self):
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			self.assertEqual(admin.plan_next_invoice_number(TCC, 50)["status"], admin.STATUS_BLOCKED)
		with self.assertRaises(admin.InvoiceNumberingAdjustmentError):
			self._apply(TCC, 50)
		self.assertIsNone(self._current(TCC))

	def test_11_system_manager_only_and_reason_required(self):
		before = self._current(ECO)
		with fx.as_user(self.plain_user), self.assertRaises(frappe.PermissionError):
			admin.apply_next_invoice_number(ECO, 9000, reason="intento sin permiso", confirm=1)
		with self.assertRaises(frappe.ValidationError):
			self._apply(ECO, 9000, reason="corto")
		self.assertEqual(self._current(ECO), before)

	# -- 6. never automatic --------------------------------------------------------

	def test_12_not_wired_into_migrate_hooks_or_scheduler(self):
		app = frappe.get_app_path("fabergray_erp")
		for relative in ("hooks.py", "patches.txt"):
			with open(os.path.join(app, relative)) as f:
				self.assertNotIn("invoice_numbering_admin", f.read(), relative)
		self.assertNotIn(admin.apply_next_invoice_number, frappe.whitelisted)
		self.assertNotIn(admin.audit_invoice_numbering, frappe.whitelisted)

	def test_12b_no_public_endpoint_and_system_manager_only(self):
		"""Through Frappe's real HTTP dispatcher (frappe.handler.execute_cmd,
		what /api/method/<path> calls): not whitelisted -> refused for every
		user, Guest and System Manager included. Direct server-side calls by
		a non System Manager are refused too, read-only ones included."""
		from frappe.handler import execute_cmd

		before = self._current(ECO)
		for fn in ("audit_invoice_numbering", "plan_next_invoice_number", "apply_next_invoice_number"):
			for user in ("Guest", self.plain_user, "Administrator"):
				frappe.form_dict.clear()
				frappe.form_dict.update({"issuer": ECO, "next_number": 9100, "reason": "intento por API", "confirm": 1})
				with fx.as_user(user), self.assertRaises(frappe.PermissionError, msg=f"{fn} as {user}"):
					execute_cmd(f"fabergray_erp.invoice_numbering_admin.{fn}")
		frappe.form_dict.clear()
		for call in (
			lambda: admin.audit_invoice_numbering(),
			lambda: admin.plan_next_invoice_number(ECO, 9100),
			lambda: admin.apply_next_invoice_number(ECO, 9100, reason="intento sin rol", confirm=1),
		):
			with fx.as_user(self.plain_user), self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(self._current(ECO), before)
		self.assertEqual(self._activity_count(), self._activity_before)

	def test_13_series_stay_independent(self):
		int_before, tcc_before = self._current(INT), self._current(TCC)
		with patch.object(admin, "_deleted_numbers", return_value=NO_DELETED):
			self._apply(ECO, 2900)
		self.assertEqual((self._current(INT), self._current(TCC)), (int_before, tcc_before))
		self.assertEqual(cint(self._current(ECO)), 2899)
