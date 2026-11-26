# -*- coding: utf-8 -*-
"""New design of "Fabrigray Factura Comercial" (integrandoMAS / ecoluminar):
no red BORRADOR box and no red in the header, invoice number in the
document's corporate dark blue, and the fiscal-validity notice moved to the
footer (small, grey -- the 3CORAZONESCAFES treatment) while there is no DIAN
authorization. Everything else (logo, issuer data, products, prices,
signatures, observations, legal text, payment terms) is kept, and the
3CORAZONESCAFES formats are untouched."""

import json
import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from fabergray_erp import invoice_issuers
from fabergray_erp.api import facturacion
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_facturacion_invoice_payment_terms as terms_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

FISCAL_NOTE = "Documento comercial — No válido como factura fiscal"
CORPORATE_BLUE = "#102a43"
RED = "#b42318"
FORMAT = facturacion.INVOICE_PDF_PRINT_FORMAT_NAME
_FIXTURE = os.path.join(frappe.get_app_path("fabergray_erp"), "fixtures", "print_format.json")


def _format(name):
	with open(_FIXTURE) as f:
		return next(p for p in json.load(f) if p["name"] == name)


def _rule(css, selector):
	m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	return m.group(1) if m else ""


class TestFacturacionInvoiceDesign(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FGDSN {sfx} WH")
		cls.item = cls.world.item(f"FGDSN-{sfx}-A")
		cls.customer = cls.world.customer(f"FGDSN {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000003-3")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.bodega_user = cls.world.user(f"fgdsn-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgdsn-{sfx}-facturacion@example.com", ["Facturación"])

	_reviewed_pick_list = terms_base.TestFacturacionInvoicePaymentTerms._reviewed_pick_list
	_invoice = terms_base.TestFacturacionInvoicePaymentTerms._invoice
	_html = terms_base.TestFacturacionInvoicePaymentTerms._html
	_header_value = terms_base.TestFacturacionInvoicePaymentTerms._header_value

	def _invoiced_html(self, issuer, method="Crédito", days=30):
		pl_name = self._reviewed_pick_list()
		self._invoice(pl_name, issuer=issuer, payment_method=method, due_date=str(add_days(nowdate(), days)))
		return pl_name, self._html(pl_name, issuer)

	# -- 1. no red, number in corporate blue -----------------------------------

	def test_01_template_has_no_red_and_blue_number(self):
		fmt = _format(FORMAT)
		for needle in (RED, "fg-inv-draft", "BORRADOR"):
			self.assertNotIn(needle, fmt["html"] + fmt["css"], needle)
		self.assertIn(f"color: {CORPORATE_BLUE}", _rule(fmt["css"], ".fg-inv-box-number"))
		note = _rule(fmt["css"], ".fg-inv-fiscal-note")
		self.assertIn("font-size: 6.5pt", note)
		self.assertIn("color: #7b8794", note)

	def test_02_rendered_header_has_no_red_box_for_both_issuers(self):
		for issuer in ("integrandoMAS", "ecoluminar"):
			pl_name, html = self._invoiced_html(issuer)
			self.assertNotIn("BORRADOR", html, issuer)
			self.assertNotIn("fg-inv-draft", html, issuer)
			self.assertNotIn(RED, html, issuer)
			# the header table is the first block of the document (no top box)
			body = html[html.index('<div class="fg-inv">') :]
			self.assertLess(body.index('class="fg-inv-header"'), 200, issuer)
			number = frappe.db.get_value("Pick List", pl_name, "fg_invoice_number")
			self.assertIn(f'class="fg-inv-box-number" colspan="2">No. {number}</td>', html)

	# -- 2. fiscal notice in the footer ----------------------------------------

	def test_03_fiscal_notice_is_the_last_line_in_the_footer(self):
		for issuer in ("integrandoMAS", "ecoluminar"):
			_pl, html = self._invoiced_html(issuer)
			self.assertEqual(html.count(FISCAL_NOTE), 1, issuer)
			note = html.index(FISCAL_NOTE)
			for earlier in ('class="fg-inv-header"', 'class="fg-inv-signatures"', 'class="fg-inv-legal-text"', "Impreso por:"):
				self.assertLess(html.index(earlier), note, f"{issuer}: {earlier}")

	# -- 3. payment terms and every other element are kept ---------------------

	def test_04_payment_terms_and_content_preserved(self):
		due = add_days(nowdate(), 45)
		for issuer, name in (("integrandoMAS", "INTEGRANDO MAS BGA"), ("ecoluminar", "ECOLUMINAR")):
			pl_name = self._reviewed_pick_list()
			self._invoice(pl_name, issuer=issuer, payment_method="Crédito", due_date=str(due))
			html = self._html(pl_name, issuer)
			self.assertEqual(self._header_value(html, "FORMA DE PAGO"), "Crédito")
			self.assertEqual(self._header_value(html, "VENCIMIENTO"), getdate(due).strftime("%d/%m/%Y"))
			self.assertIn(name, html)
			for cls in (
				"fg-inv-logo",
				"fg-inv-issuer-nit",
				"fg-inv-customer",
				"fg-inv-items",
				"fg-inv-totals",
				"fg-inv-signatures",
				"fg-inv-legal-text",
				"fg-inv-legal-payment",
				"fg-inv-printer",
			):
				self.assertIn(cls, html, f"{issuer}: {cls}")
			self.assertIn(f"Ref. interna: {pl_name}", html)

	# -- 4. historical invoices -------------------------------------------------

	def test_05_historical_unnumbered_invoice_still_prints(self):
		pl_name = self._reviewed_pick_list()
		self._invoice(pl_name, issuer="integrandoMAS", payment_method="Contado", due_date=nowdate())
		# Test setup: a Facturado Pick List from before numbering and payment terms.
		frappe.db.set_value(
			"Pick List",
			pl_name,
			{
				"fg_invoice_number": 0,
				"fg_invoice_number_key": None,
				"fg_invoice_payment_method": None,
				"fg_invoice_due_date": None,
			},
			update_modified=False,
		)
		html = self._html(pl_name, "integrandoMAS")
		self.assertIn("No. SIN NUMERAR", html)
		self.assertIn(FISCAL_NOTE, html)
		self.assertNotIn("BORRADOR", html)
		self.assertEqual(self._header_value(html, "FORMA DE PAGO"), "—")
		self.assertEqual(self._header_value(html, "VENCIMIENTO"), "—")

	# -- 5. 3CORAZONESCAFES untouched -------------------------------------------

	def test_05b_no_other_issuer_name_leaks_into_the_rendered_pdf(self):
		"""Comments in the CSS are rendered: they must never name another issuer."""
		fmt = _format(FORMAT)
		self.assertNotIn("3CORAZONES", fmt["css"])
		for issuer in ("integrandoMAS", "ecoluminar"):
			_pl, html = self._invoiced_html(issuer)
			self.assertNotIn("3CORAZONES", html, issuer)

	def test_06_tres_corazones_formats_untouched(self):
		for name in ("3CORAZONESCAFES Factura Comercial", "3CORAZONESCAFES Factura"):
			fmt = _format(name)
			self.assertNotIn("fg-inv-fiscal-note", fmt["html"] + fmt["css"], name)
			self.assertIn("tcc-inv-fiscal-note", fmt["html"], name)
			self.assertEqual(fmt["modified"], "2026-11-25 00:00:02", name)
		pl_name = self._reviewed_pick_list()
		self._invoice(pl_name, issuer="3CORAZONESCAFES", payment_method="Efectivo", due_date=nowdate())
		html = self._html(pl_name, "3CORAZONESCAFES")
		self.assertIn('class="tcc-inv-fiscal-note"', html)
		self.assertNotIn("fg-inv-fiscal-note", html)

	# -- 6. legal-validity logic is the server's -------------------------------

	def test_07_notice_follows_draft_reasons(self):
		"""The notice is driven by doc.fg_pdf_draft_reasons + the issuer's
		legal_document_enabled (same rule as 3CORAZONESCAFES). Today every
		PDF carries the DIAN reason, so the notice is always shown."""
		fmt = _format(FORMAT)
		self.assertIn(
			"{%- set legal_active = issuer.legal_document_enabled and not doc.fg_pdf_draft_reasons -%}", fmt["html"]
		)
		self.assertIn("{% if not legal_active %}", fmt["html"])
		for issuer in ("integrandoMAS", "ecoluminar"):
			self.assertFalse(invoice_issuers.get_issuer_config(issuer)["legal_document_enabled"])
