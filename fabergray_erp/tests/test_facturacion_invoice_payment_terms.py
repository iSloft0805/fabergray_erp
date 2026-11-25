# -*- coding: utf-8 -*-
"""Forma de pago y fecha de vencimiento de la factura comercial (Revisar
pedido): Pick List.fg_invoice_payment_method / fg_invoice_due_date.

Cubre: selección y persistencia de las cuatro formas de pago, plazos rápidos
y fecha personalizada, validación de fechas y opciones (también server-side),
obligatoriedad al FACTURAR, bloqueo después de Facturado, permisos y
aislamiento por Company, impresión en los tres emisores, documentos
históricos, series independientes intactas y que nada toque Cartera ni pagos.
Mismo TestWorld que el resto de Facturación (series de numeración aisladas).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from fabergray_erp import invoice_issuers
from fabergray_erp.api import bodega, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

TCC = "3CORAZONESCAFES"
_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)


def _co(value):
	return getdate(value).strftime("%d/%m/%Y")


class TestFacturacionInvoicePaymentTerms(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FGPAY {sfx} WH")
		cls.item = cls.world.item(f"FGPAY-{sfx}-A")
		cls.customer = cls.world.customer(f"FGPAY {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000002-2")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user(f"fgpay-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgpay-{sfx}-facturacion@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user(f"fgpay-{sfx}-vendedora@example.com", ["Vendedora"])
		cls.jefe_user = cls.world.user(f"fgpay-{sfx}-jefe@example.com", ["Jefe de Bodega"])
		# Facturación de OTRA empresa: mismo rol, otra Company.
		cls.other_facturacion_user = cls.world.user(f"fgpay-{sfx}-otra@example.com", ["Facturación"])
		cls.world.company_user_permission(cls.other_facturacion_user, "_Test Company")

	# -- helpers --------------------------------------------------------------

	def _reviewed_pick_list(self, qty=2, rate=5750):
		"""Pick List sometido, alistado y con el checklist completo: listo para
		FACTURAR, todavía sin forma de pago."""
		so = self.world.multi_item_sales_order(
			self.customer.name, [{"item_code": self.item.name, "warehouse": self.wh.name, "qty": qty, "rate": rate}]
		)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl.name)["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
		return pl.name

	def _detail(self, pl_name, user=None):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.get_invoicing_detail(pl_name)

	def _set(self, pl_name, method, due_date, user=None):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.set_invoice_payment_terms(pl_name, method, due_date)

	def _invoice(self, pl_name, issuer="integrandoMAS", user=None, **kwargs):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.mark_as_invoiced(pl_name, issuer, **kwargs)

	def _stored(self, pl_name):
		return frappe.db.get_value(
			"Pick List",
			pl_name,
			["fg_invoice_payment_method", "fg_invoice_due_date", "fg_invoicing_status", "fg_invoice_number"],
			as_dict=True,
		)

	def _series_current(self, issuer):
		series = self.world.invoice_numbering[issuer]["series"]
		row = frappe.db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s", series)
		return row[0][0] if row else None

	def _html(self, pl_name, issuer):
		with fx.as_user(self.facturacion_user):
			return frappe.get_print(
				"Pick List", pl_name, print_format=invoice_issuers.invoice_print_format(issuer), as_pdf=False
			)

	def _header_value(self, html, label):
		m = re.search(re.escape(label) + r"</td>\s*<td[^>]*>([^<]*)</td>", html)
		self.assertIsNotNone(m, f"{label} not found in the PDF header")
		return m.group(1).strip()

	# -- 1. pedido nuevo: nada preseleccionado ------------------------------------

	def test_01_new_order_has_no_payment_method_preselected(self):
		pl_name = self._reviewed_pick_list()
		detail = self._detail(pl_name)
		self.assertIsNone(detail["fg_invoice_payment_method"])
		self.assertIsNone(detail["fg_invoice_due_date"])
		self.assertIsNone(detail["invoice_due_days"])
		self.assertTrue(detail["payment_terms_editable"])
		self.assertEqual(detail["invoice_billing_date"], nowdate())  # zona horaria del sitio
		self.assertEqual(detail["payment_methods"], ["Contado", "Crédito", "Efectivo", "Transferencia"])
		self.assertEqual(
			detail["payment_suggested_days"], {"Contado": 0, "Crédito": 30, "Efectivo": 0, "Transferencia": 0}
		)
		self.assertEqual(detail["due_quick_days"], [0, 15, 30, 45, 60, 90])
		stored = self._stored(pl_name)
		self.assertFalse(stored.fg_invoice_payment_method)  # NULL o "" (Select vacío)
		self.assertFalse(stored.fg_invoice_due_date)

	# -- 2. las cuatro opciones se guardan y persisten al reabrir -------------------

	def test_02_each_payment_method_persists_across_reopen(self):
		pl_name = self._reviewed_pick_list()
		for method, days in facturacion.INVOICE_PAYMENT_SUGGESTED_DAYS.items():
			due = add_days(nowdate(), days)
			result = self._set(pl_name, method, str(due))
			self.assertEqual(result["fg_invoice_payment_method"], method)
			self.assertEqual(result["fg_invoice_due_date"], str(getdate(due)))
			# "cerrar y volver a abrir": una lectura nueva desde el servidor
			reopened = self._detail(pl_name)
			self.assertEqual(reopened["fg_invoice_payment_method"], method)
			self.assertEqual(reopened["fg_invoice_due_date"], str(getdate(due)))
			self.assertEqual(reopened["invoice_due_days"], days)
			self.assertEqual(self._stored(pl_name).fg_invoice_payment_method, method)

	# -- 3. plazos rápidos y fecha personalizada -------------------------------------

	def test_03_six_quick_terms_computed_from_billing_date(self):
		pl_name = self._reviewed_pick_list()
		billing = getdate(self._detail(pl_name)["invoice_billing_date"])
		for days in (0, 15, 30, 45, 60, 90):
			due = add_days(billing, days)
			self._set(pl_name, "Crédito", str(due))
			detail = self._detail(pl_name)
			self.assertEqual(detail["invoice_due_days"], days)
			self.assertEqual(getdate(detail["fg_invoice_due_date"]), getdate(due))

	def test_04_custom_due_date_persists(self):
		pl_name = self._reviewed_pick_list()
		custom = str(add_days(nowdate(), 23))
		self._set(pl_name, "Transferencia", custom)
		detail = self._detail(pl_name)
		self.assertEqual(detail["fg_invoice_due_date"], custom)
		self.assertEqual(detail["invoice_due_days"], 23)
		# El tope (365 días) es válido; un día más no.
		self._set(pl_name, "Crédito", str(add_days(nowdate(), facturacion.MAX_INVOICE_DUE_DAYS)))

	# -- 5/6. opciones y fechas inválidas, rechazadas por el servidor ----------------

	def test_05_invalid_payment_methods_rejected(self):
		pl_name = self._reviewed_pick_list()
		due = nowdate()
		for bad in ("contado", "CONTADO", "Cheque", "Consignación", "Otro", "<script>x</script>", "Credito"):
			with self.assertRaises(facturacion.InvalidInvoicePaymentMethodError, msg=bad):
				self._set(pl_name, bad, due)
		for empty in (None, ""):
			with self.assertRaises(facturacion.InvoicePaymentMethodRequiredError):
				self._set(pl_name, empty, due)
		self.assertFalse(self._stored(pl_name).fg_invoice_payment_method)

	def test_06_invalid_due_dates_rejected(self):
		pl_name = self._reviewed_pick_list()
		yesterday = str(add_days(nowdate(), -1))
		too_far = str(add_days(nowdate(), facturacion.MAX_INVOICE_DUE_DAYS + 1))
		for bad in (yesterday, too_far, "2026-13-40", "2026-02-30", "25/12/2026", "12/25/2026", "abc", "2026-1-5"):
			with self.assertRaises(facturacion.InvalidInvoiceDueDateError, msg=bad):
				self._set(pl_name, "Crédito", bad)
		for empty in (None, "", "   "):
			with self.assertRaises(facturacion.InvoiceDueDateRequiredError):
				self._set(pl_name, "Crédito", empty)
		stored = self._stored(pl_name)
		self.assertFalse(stored.fg_invoice_payment_method)  # NULL o "" (Select vacío)
		self.assertFalse(stored.fg_invoice_due_date)

	# -- 7/8. obligatoria para FACTURAR --------------------------------------------

	def test_07_cannot_invoice_without_payment_method_or_due_date(self):
		pl_name = self._reviewed_pick_list()
		before = self._series_current("integrandoMAS")
		with self.assertRaises(facturacion.InvoicePaymentMethodRequiredError):
			self._invoice(pl_name)
		with self.assertRaises(facturacion.InvoiceDueDateRequiredError):
			self._invoice(pl_name, payment_method="Contado")
		with self.assertRaises(facturacion.InvalidInvoicePaymentMethodError):
			self._invoice(pl_name, payment_method="Cheque", due_date=nowdate())
		with self.assertRaises(facturacion.InvalidInvoiceDueDateError):
			self._invoice(pl_name, payment_method="Contado", due_date=str(add_days(nowdate(), -1)))
		stored = self._stored(pl_name)
		self.assertNotEqual(stored.fg_invoicing_status, facturacion.FG_INVOICING_FACTURADO)
		self.assertFalse(stored.fg_invoice_number)
		self.assertEqual(self._series_current("integrandoMAS"), before)  # ningún número consumido

	def test_08_invoice_uses_saved_terms_and_locks_them(self):
		pl_name = self._reviewed_pick_list()
		due = str(add_days(nowdate(), 30))
		self._set(pl_name, "Crédito", due)
		result = self._invoice(pl_name)  # sin parámetros: usa lo guardado
		self.assertEqual(result["fg_invoice_payment_method"], "Crédito")
		self.assertEqual(result["fg_invoice_due_date"], due)
		stored = self._stored(pl_name)
		self.assertEqual(stored.fg_invoicing_status, facturacion.FG_INVOICING_FACTURADO)
		self.assertEqual(stored.fg_invoice_payment_method, "Crédito")
		self.assertEqual(str(stored.fg_invoice_due_date), due)
		detail = self._detail(pl_name)
		self.assertFalse(detail["payment_terms_editable"])
		self.assertEqual(detail["invoice_due_days"], 30)

	def test_08b_invoice_params_override_saved_terms(self):
		pl_name = self._reviewed_pick_list()
		self._set(pl_name, "Crédito", str(add_days(nowdate(), 30)))
		result = self._invoice(pl_name, payment_method="Efectivo", due_date=nowdate())
		self.assertEqual(result["fg_invoice_payment_method"], "Efectivo")
		self.assertEqual(result["fg_invoice_due_date"], nowdate())

	def test_08c_stale_saved_due_date_rejected_at_invoice_time(self):
		"""Guardado ayer con "Hoy": hoy ya es anterior a la fecha de facturación."""
		pl_name = self._reviewed_pick_list()
		self._set(pl_name, "Contado", nowdate())
		frappe.db.set_value(
			"Pick List", pl_name, "fg_invoice_due_date", add_days(nowdate(), -1), update_modified=False
		)  # test setup: simula el paso de un día
		with self.assertRaises(facturacion.InvalidInvoiceDueDateError):
			self._invoice(pl_name)
		self.assertNotEqual(self._stored(pl_name).fg_invoicing_status, facturacion.FG_INVOICING_FACTURADO)

	# -- 9. elegir no consume números ------------------------------------------

	def test_09_selecting_terms_never_consumes_invoice_numbers(self):
		pl_name = self._reviewed_pick_list()
		before = {issuer: self._series_current(issuer) for issuer in invoice_issuers.INVOICE_ISSUERS}
		for method in facturacion.INVOICE_PAYMENT_METHODS:
			for days in (0, 15, 90):
				self._set(pl_name, method, str(add_days(nowdate(), days)))
		after = {issuer: self._series_current(issuer) for issuer in invoice_issuers.INVOICE_ISSUERS}
		self.assertEqual(before, after)
		self.assertFalse(self._stored(pl_name).fg_invoice_number)

	# -- 10/11. bloqueo después de facturar y escrituras directas ------------------

	def test_10_terms_locked_after_invoicing(self):
		pl_name = self._reviewed_pick_list()
		self._invoice(pl_name, payment_method="Contado", due_date=nowdate())
		with self.assertRaises(facturacion.InvoicePaymentTermsLockedError):
			self._set(pl_name, "Crédito", str(add_days(nowdate(), 30)))
		# Ni siquiera con la bandera interna (p. ej. otro código del servidor).
		pl = frappe.get_doc("Pick List", pl_name)
		pl.fg_invoice_payment_method = "Crédito"
		with facturacion._invoice_payment_write(), self.assertRaises(facturacion.InvoicePaymentTermsLockedError):
			pl.save()
		self.assertEqual(self._stored(pl_name).fg_invoice_payment_method, "Contado")

	def test_11_direct_writes_rejected_for_every_role(self):
		"""Bodega y Facturación tienen write sobre Pick List: sin el guard
		podrían escribir los campos por Desk/REST."""
		pl_name = self._reviewed_pick_list()
		for user in (self.bodega_user, self.facturacion_user):
			for field, value in (("fg_invoice_payment_method", "Crédito"), ("fg_invoice_due_date", nowdate())):
				with fx.as_user(user):
					pl = frappe.get_doc("Pick List", pl_name)
					pl.set(field, value)
					with self.assertRaises(frappe.PermissionError, msg=f"{user} {field}"):
						pl.save()
		stored = self._stored(pl_name)
		self.assertFalse(stored.fg_invoice_payment_method)  # NULL o "" (Select vacío)
		self.assertFalse(stored.fg_invoice_due_date)

	# -- 12. permisos y Company --------------------------------------------------

	def test_12_other_roles_and_other_company_rejected(self):
		pl_name = self._reviewed_pick_list()
		for user in (self.bodega_user, self.jefe_user, self.vendedora_user, self.other_facturacion_user):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self._set(pl_name, "Contado", nowdate(), user=user)
		self.assertFalse(self._stored(pl_name).fg_invoice_payment_method)

	def test_12b_other_company_cannot_invoice(self):
		pl_name = self._reviewed_pick_list()
		with self.assertRaises(frappe.PermissionError):
			self._invoice(pl_name, user=self.other_facturacion_user, payment_method="Contado", due_date=nowdate())

	# -- 13. impresión en los tres emisores -----------------------------------------

	def test_13_pdf_header_shows_terms_for_all_three_issuers(self):
		due = add_days(nowdate(), 45)
		for issuer in ("integrandoMAS", "ecoluminar", TCC):
			pl_name = self._reviewed_pick_list()
			self._set(pl_name, "Crédito", str(due))
			self._invoice(pl_name, issuer=issuer)
			html = self._html(pl_name, issuer)
			self.assertEqual(self._header_value(html, "FORMA DE PAGO"), "Crédito", issuer)
			self.assertEqual(self._header_value(html, "VENCIMIENTO"), _co(due), issuer)
			self.assertEqual(self._header_value(html, "FECHA DE FACTURA"), _co(nowdate()), issuer)

	def test_13b_each_method_prints(self):
		for method in facturacion.INVOICE_PAYMENT_METHODS:
			pl_name = self._reviewed_pick_list()
			self._invoice(pl_name, payment_method=method, due_date=nowdate())
			html = self._html(pl_name, "integrandoMAS")
			self.assertEqual(self._header_value(html, "FORMA DE PAGO"), method)
			self.assertEqual(self._header_value(html, "VENCIMIENTO"), _co(nowdate()))

	# -- 14. documentos históricos ------------------------------------------------

	def test_14_historical_invoice_without_terms_still_prints_untouched(self):
		pl_name = self._reviewed_pick_list()
		self._invoice(pl_name, payment_method="Contado", due_date=nowdate())
		# Test setup: un Facturado de antes de esta función (sin forma de pago).
		frappe.db.set_value(
			"Pick List",
			pl_name,
			{"fg_invoice_payment_method": None, "fg_invoice_due_date": None},
			update_modified=False,
		)
		html = self._html(pl_name, "integrandoMAS")
		self.assertEqual(self._header_value(html, "FORMA DE PAGO"), "—")
		self.assertEqual(self._header_value(html, "VENCIMIENTO"), "—")
		detail = self._detail(pl_name)
		self.assertIsNone(detail["fg_invoice_payment_method"])
		self.assertFalse(detail["payment_terms_editable"])
		with self.assertRaises(facturacion.InvoicePaymentTermsLockedError):
			self._set(pl_name, "Contado", nowdate())
		stored = self._stored(pl_name)  # nunca se le asigna un valor
		self.assertFalse(stored.fg_invoice_payment_method)  # NULL o "" (Select vacío)
		self.assertFalse(stored.fg_invoice_due_date)

	# -- 15. series independientes ------------------------------------------------

	def test_15_issuer_series_stay_independent(self):
		before = {issuer: self._series_current(issuer) for issuer in invoice_issuers.INVOICE_ISSUERS}
		numbers = {}
		for issuer in ("integrandoMAS", "ecoluminar", TCC):
			pl_name = self._reviewed_pick_list()
			self._set(pl_name, "Transferencia", nowdate())
			numbers[issuer] = self._invoice(pl_name, issuer=issuer)["fg_invoice_number"]
		for issuer, number in numbers.items():
			start = invoice_issuers.INVOICE_NUMBERING[issuer]["start"]
			expected = (before[issuer] if before[issuer] is not None else start - 1) + 1
			self.assertEqual(number, expected, issuer)
			self.assertEqual(self._series_current(issuer), expected, issuer)
		# Las series de prueba nunca son las reales.
		for issuer in numbers:
			self.assertIn("FG-FACT-TEST-", self.world.invoice_numbering[issuer]["series"])

	# -- 16. Cartera, pagos y contabilidad intactos ---------------------------------

	def test_16_no_cartera_payment_or_accounting_side_effects(self):
		counts = lambda: {  # noqa: E731
			dt: frappe.db.count(dt)
			for dt in ("Cartera Obligacion", "Cartera Pago", "Payment Entry", "GL Entry", "Sales Invoice", "Stock Ledger Entry")
		}
		pl_name = self._reviewed_pick_list()
		before = counts()
		self._set(pl_name, "Crédito", str(add_days(nowdate(), 30)))
		self._invoice(pl_name)
		self.assertEqual(counts(), before)
		self.assertFalse(frappe.db.exists("Cartera Obligacion", {"pick_list": pl_name}))

	# -- 17. contrato de la interfaz ---------------------------------------------

	def test_17_ui_contract(self):
		with open(_FACTURACION_JS_PATH) as f:
			js = f.read()
		for name, icon_name in (
			("Contado", "wallet"),
			('"Crédito"', "calendar-clock"),
			("Efectivo", "banknote"),
			("Transferencia", "landmark"),
		):
			self.assertRegex(js, re.escape(name) + r':\s*"' + re.escape(icon_name) + '"')
		# Sin preselección: el estado inicial es lo guardado o null.
		self.assertIn("this._review_payment_method = detail.fg_invoice_payment_method || null;", js)
		# Persistencia inmediata y envío con FACTURAR.
		self.assertIn('this.call("set_invoice_payment_terms"', js)
		self.assertRegex(js, r'this\.call\("mark_as_invoiced", \{[^}]*payment_method: this\._review_payment_method')
		# CONFIRMAR FACTURACIÓN exige forma de pago y vencimiento.
		self.assertIn("!!this._review_payment_method &&", js)
		self.assertIn("!!this._review_due_date &&", js)
		for label in ("Hoy", "Fecha de vencimiento", "Fecha de facturación:", "Resumen de facturación", "Plazo"):
			self.assertIn(label, js)
