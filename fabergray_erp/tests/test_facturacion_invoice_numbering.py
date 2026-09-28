# -*- coding: utf-8 -*-
"""Ajuste numeración por empresa -- número INTERNO de factura independiente
por emisor (integrandoMAS desde 6886, ecoluminar desde 2263), asignado UNA vez
al FACTURAR (facturacion.mark_as_invoiced()) desde la serie nativa de Frappe
(tabSeries). Elegir/cambiar el emisor antes de facturar y renderizar el PDF
nunca consumen la serie.

Las series se aíslan por clase (fixtures.TestWorld: claves únicas de esta
corrida, MISMOS números de inicio), así que los tests pueden afirmar
6886/2263 y nunca consumen la serie real del sitio. Fixtures reutilizan los
helpers de test_facturacion_invoice_pdf.py (funciones prestadas, nunca
heredando el TestCase -- heredarlo re-ejecutaría toda esa suite)."""

import threading

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp import invoice_issuers
from fabergray_erp.api import facturacion
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_facturacion_invoice_pdf as pdf_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestFacturacionInvoiceNumbering(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.invoice_numbering = cls.world.invoice_numbering

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FGNUM {sfx} WH")
		cls.item_a = cls.world.item(f"FGNUM-{sfx}-A")
		cls.item_b = cls.world.item(f"FGNUM-{sfx}-B")
		cls.customer = cls.world.customer(f"FGNUM {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000001-1")
		cls.world.stock_up_real(cls.item_a.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up_real(cls.item_b.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user(f"fgnum-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgnum-{sfx}-facturacion@example.com", ["Facturación"])
		cls.facturacion_user_b = cls.world.user(f"fgnum-{sfx}-facturacion-b@example.com", ["Facturación"])

	# -- Borrowed helpers (functions only, see module docstring) -------------
	_picked_pick_list = pdf_base.TestFacturacionInvoicePdf._picked_pick_list
	_html = pdf_base.TestFacturacionInvoicePdf._html
	_download = pdf_base.TestFacturacionInvoicePdf._download

	def _checked(self):
		"""Picked + checklist complete: ready to FACTURAR, not invoiced yet."""
		_, pl_name = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)
		return pl_name

	def _set_issuer(self, pl_name, issuer, user=None):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.set_invoice_issuer(pl_name, issuer)

	def _facturar(self, pl_name, issuer=None, user=None):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.mark_as_invoiced(pl_name, issuer)

	def _invoiced(self, issuer):
		pl_name = self._checked()
		return pl_name, self._facturar(pl_name, issuer)["fg_invoice_number"]

	def _series_current(self, issuer):
		"""None while the series row does not exist yet (never consumed)."""
		return frappe.db.get_value("Series", self.invoice_numbering[issuer]["series"], "current", order_by=None)

	def _number(self, pl_name):
		return frappe.db.get_value("Pick List", pl_name, "fg_invoice_number")

	# The tests below share the class-level series and run in alphabetical
	# order: test_01 is the first one to invoice, so it sees 6886/2263.

	def test_00_real_configuration(self):
		self.assertEqual(set(invoice_issuers.INVOICE_NUMBERING), set(invoice_issuers.INVOICE_ISSUERS))
		self.assertEqual(self.invoice_numbering["integrandoMAS"]["start"], 6886)
		self.assertEqual(self.invoice_numbering["ecoluminar"]["start"], 2263)
		self.assertEqual(len({c["series"] for c in self.invoice_numbering.values()}), 2)

	# =====================================================================
	# Elegir emisor antes de facturar: nunca consume la serie
	# =====================================================================

	def test_01_selecting_and_changing_issuer_before_invoicing_consumes_nothing(self):
		pl_name = self._checked()
		self._set_issuer(pl_name, "integrandoMAS")
		self._set_issuer(pl_name, "ecoluminar")
		self._set_issuer(pl_name, "integrandoMAS")
		self.assertIsNone(self._series_current("integrandoMAS"))
		self.assertIsNone(self._series_current("ecoluminar"))
		self.assertFalse(self._number(pl_name))
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "integrandoMAS")

		# FACTURAR with the saved issuer -> first Integrando number.
		result = self._facturar(pl_name)
		self.assertEqual(result["fg_invoice_number"], 6886)
		self.assertEqual(result["fg_invoice_issuer"], "integrandoMAS")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_number_key"), "integrandoMAS-6886")
		self.assertIsNone(self._series_current("ecoluminar"))

	def test_02_sequence_6887_and_2263_2264_independent(self):
		_, n_i2 = self._invoiced("integrandoMAS")
		_, n_e1 = self._invoiced("ecoluminar")
		_, n_i3 = self._invoiced("integrandoMAS")
		_, n_e2 = self._invoiced("ecoluminar")
		self.assertEqual((n_i2, n_i3), (6887, 6888))
		self.assertEqual((n_e1, n_e2), (2263, 2264))
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), (6888, 2264))

	def test_03_facturar_requires_an_issuer_and_consumes_nothing_without_it(self):
		pl_name = self._checked()
		before = (self._series_current("integrandoMAS"), self._series_current("ecoluminar"))
		with self.assertRaises(facturacion.InvoiceIssuerRequiredError):
			self._facturar(pl_name)
		with self.assertRaises(facturacion.InvalidInvoiceIssuerError):
			self._facturar(pl_name, "fabrigraySAS")
		self.assertNotEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoicing_status"), "Facturado")
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), before)

	# =====================================================================
	# Idempotencia: segundo clic, PDF, descarga
	# =====================================================================

	def test_04_second_facturar_click_pdf_and_download_keep_the_same_number(self):
		pl_name, number = self._invoiced("integrandoMAS")
		current = self._series_current("integrandoMAS")
		with self.assertRaises(facturacion.AlreadyInvoicedError):
			self._facturar(pl_name, "integrandoMAS")
		html = self._html(pl_name)
		self._html(pl_name)
		self._download(pl_name)
		self.assertIn(f"No. {number}", html)
		self.assertNotIn("SIN NUMERAR", html)
		self.assertIn("BORRADOR — NO VÁLIDO COMO FACTURA DE VENTA", html)
		self.assertEqual(self._number(pl_name), number)
		self.assertEqual(self._series_current("integrandoMAS"), current)
		# re-saving the same issuer is a no-op
		self.assertEqual(self._set_issuer(pl_name, "integrandoMAS")["fg_invoice_number"], number)
		self.assertEqual(self._series_current("integrandoMAS"), current)

	def test_05_pdf_shows_own_number_and_logo(self):
		for issuer, name, logo, other_logo in (
			("integrandoMAS", "INTEGRANDO MAS BGA", "integrandomas-logo.png", "ecoluminar-logo.png"),
			("ecoluminar", "ECOLUMINAR", "ecoluminar-logo.png", "integrandomas-logo.png"),
		):
			pl_name, number = self._invoiced(issuer)
			html = self._html(pl_name)
			self.assertIn(f"No. {number}", html)
			self.assertIn(name, html)
			self.assertIn(logo, html)
			self.assertNotIn(other_logo, html)

	# =====================================================================
	# Bloqueo del emisor / históricos / guard / unicidad
	# =====================================================================

	def test_06_issuer_locked_once_numbered(self):
		pl_name, number = self._invoiced("ecoluminar")
		currents = (self._series_current("integrandoMAS"), self._series_current("ecoluminar"))
		with self.assertRaises(facturacion.InvoiceIssuerLockedError):
			self._set_issuer(pl_name, "integrandoMAS")
		self.assertEqual(
			frappe.db.get_value("Pick List", pl_name, ["fg_invoice_issuer", "fg_invoice_number"]), ("ecoluminar", number)
		)
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), currents)

	def test_07_historical_invoiced_pick_list_is_never_numbered(self):
		"""Facturado before this change (no number): choosing an issuer for its
		PDF never numbers it, and it prints SIN NUMERAR."""
		pl_name, _number = self._invoiced("integrandoMAS")
		frappe.db.set_value(
			"Pick List", pl_name, {"fg_invoice_issuer": None, "fg_invoice_number": 0, "fg_invoice_number_key": None}
		)
		currents = (self._series_current("integrandoMAS"), self._series_current("ecoluminar"))
		self._set_issuer(pl_name, "ecoluminar")
		self._set_issuer(pl_name, "integrandoMAS")
		with self.assertRaises(facturacion.AlreadyInvoicedError):
			self._facturar(pl_name, "integrandoMAS")
		self.assertFalse(self._number(pl_name))
		self.assertIn("No. SIN NUMERAR", self._html(pl_name))
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), currents)

	def test_08_number_fields_cannot_be_written_directly(self):
		pl_name, number = self._invoiced("integrandoMAS")
		for field, value in (
			("fg_invoice_number", number + 100),
			("fg_invoice_number_key", "integrandoMAS-1"),
			("fg_invoice_issuer", "ecoluminar"),
		):
			with fx.as_user(self.facturacion_user):
				with self.assertRaises((frappe.PermissionError, frappe.ValidationError), msg=field):
					frappe.client.set_value("Pick List", pl_name, field, value)
		self.assertEqual(
			frappe.db.get_value("Pick List", pl_name, ["fg_invoice_issuer", "fg_invoice_number"]),
			("integrandoMAS", number),
		)

	def test_09_database_rejects_a_duplicate_number_in_the_same_issuer(self):
		_, number = self._invoiced("ecoluminar")
		other = self._checked()
		frappe.db.savepoint("fg_dup_number")
		with self.assertRaises(frappe.UniqueValidationError):
			with facturacion._invoice_numbering_write():
				doc = frappe.get_doc("Pick List", other)
				doc.fg_invoice_issuer = "ecoluminar"
				doc.fg_invoice_number = number
				doc.fg_invoice_number_key = facturacion._invoice_number_key("ecoluminar", number)
				doc.save(ignore_permissions=True)
		frappe.db.rollback(save_point="fg_dup_number")

	def test_10_cancelled_pick_list_keeps_its_number_and_it_is_never_reused(self):
		pl_name, number = self._invoiced("integrandoMAS")
		frappe.get_doc("Pick List", pl_name).cancel()
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, ["docstatus", "fg_invoice_number"]), (2, number))
		_, next_number = self._invoiced("integrandoMAS")
		self.assertEqual(next_number, number + 1)

	def test_11_series_never_restarts_if_it_already_exists(self):
		current = self._series_current("integrandoMAS")
		self.assertGreaterEqual(current, 6886)
		self.assertEqual(facturacion._next_invoice_number("integrandoMAS"), current + 1)
		self.assertEqual(self._series_current("integrandoMAS"), current + 1)

	# =====================================================================
	# Concurrencia real (dos conexiones)
	# =====================================================================

	def _concurrent(self, attempts):
		"""attempts: [(pl_name, issuer, user)] run at once through
		mark_as_invoiced(), each on its own DB connection and transaction.
		Returns [("ok", number) | ("already", None) | ("exception", repr)]."""
		frappe.db.commit()
		site = frappe.local.site
		results = {}

		def attempt(key, pl_name, issuer, user):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user)
			try:
				result = facturacion.mark_as_invoiced(pl_name, issuer)
				frappe.db.commit()
				results[key] = ("ok", result["fg_invoice_number"])
			except facturacion.AlreadyInvoicedError:
				frappe.db.rollback()
				results[key] = ("already", None)
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		threads = [
			threading.Thread(target=attempt, args=(i, pl_name, issuer, user))
			for i, (pl_name, issuer, user) in enumerate(attempts)
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		return [results.get(i) for i in range(len(attempts))]

	def test_12_two_pick_lists_same_issuer_at_once_get_distinct_consecutive_numbers(self):
		pl_a, pl_b = self._checked(), self._checked()
		start = self._series_current("integrandoMAS")
		outcomes = self._concurrent(
			[(pl_a, "integrandoMAS", self.facturacion_user), (pl_b, "integrandoMAS", self.facturacion_user_b)]
		)
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual(sorted(o[1] for o in outcomes), [start + 1, start + 2])

	def test_13_one_pick_list_per_issuer_at_once_keeps_series_independent(self):
		pl_i, pl_e = self._checked(), self._checked()
		int_start, eco_start = self._series_current("integrandoMAS"), self._series_current("ecoluminar")
		outcomes = self._concurrent(
			[(pl_i, "integrandoMAS", self.facturacion_user), (pl_e, "ecoluminar", self.facturacion_user_b)]
		)
		self.assertEqual(outcomes, [("ok", int_start + 1), ("ok", eco_start + 1)])

	def test_14_double_click_same_pick_list_consumes_one_number(self):
		pl_name = self._checked()
		start = self._series_current("ecoluminar")
		outcomes = self._concurrent(
			[(pl_name, "ecoluminar", self.facturacion_user), (pl_name, "ecoluminar", self.facturacion_user_b)]
		)
		self.assertEqual(sorted(outcomes, key=str), [("already", None), ("ok", start + 1)], outcomes)
		self.assertEqual(self._series_current("ecoluminar"), start + 1)
		self.assertEqual(self._number(pl_name), start + 1)
