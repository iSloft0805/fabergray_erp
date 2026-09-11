# -*- coding: utf-8 -*-
"""Commit 25.15 -- "Fabrigray Cotización Comercial" Print Format, the
customer-facing commercial PDF for an Aprobada Quotation.

Every non-trivial resolution (Spanish long-form dates, Contact phone/email
off Contact's own child tables, address, tax breakdown off
`item_wise_tax_details`, advisor name, Company info, AND the security gate
itself) lives in `cotizaciones.prepare_and_guard_quotation_pdf()`, a
Quotation `before_print` doc_event -- these tests exercise it the same way
Frappe itself does, via `frappe.get_print(..., as_pdf=False)` (HTML mode --
no wkhtmltopdf/browser dependency needed to verify the ACTUAL rendered
content; this environment has neither wkhtmltopdf nor a working headless
browser installed, confirmed during this commit's own implementation, see
the STOP AND REPORT), never by calling `prepare_and_guard_quotation_pdf()`
directly (that would test the hook in isolation, not the real pipeline a
user actually hits through Desk print preview/download_pdf).

Same convention as test_cotizaciones_price_mode.py: one class per
concern, `fx.TestWorld` fixtures, plus a static UI-contract class at the
bottom reading cotizaciones.js/facturacion.js as text (no JS test runner
in this app).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

PDF_FORMAT = cotizaciones.PDF_PRINT_FORMAT_NAME

_COTIZACIONES_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
)
_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


def _function_body(source, function_name):
	"""Same idea as `_method_body()`, but for a module-level (column-0,
	`function name(...) {`) declaration -- `open_fabrigray_quotation_pdf()`/
	`download_fabrigray_quotation_pdf()` are plain functions, not class
	methods, so `_method_body()`'s own leading-tab requirement never
	matches them."""
	m = re.search(r"\nfunction " + re.escape(function_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"function {function_name!r} not found")
	start = m.end()
	next_decl = re.search(r"\n(function |const )[a-zA-Z_]", source[start:])
	end = start + next_decl.start() if next_decl else len(source)
	return source[start:end]


class TestQuotationPdf(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG2515 Customer")
		cls.vendedora = cls.world.user("fg2515-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2515-facturacion@example.com", ["Facturación"])
		cls.bodega = cls.world.user("fg2515-bodega@example.com", ["Bodega"])

	def _priced_item(self, item_code, rate=200, price_list="Standard Selling"):
		item = self.world.item(item_code)
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": price_list,
				"selling": 1,
				"price_list_rate": rate,
			}
		)
		price.insert()
		self.world.track_existing("Item Price", price.name)
		return item

	def _pending_quotation(self, item_codes_qty):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name,
				items=[{"item_code": code, "qty": qty} for code, qty in item_codes_qty],
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		return result["name"]

	def _approve(self, name):
		with fx.as_user(self.facturacion):
			approved = cotizaciones.approve_quotation_billing(name)
		return approved["name"]

	def _approved_quotation(self, item_codes_qty=None, item_code=None, qty=3):
		if item_codes_qty is None:
			item_codes_qty = [(item_code, qty)]
		name = self._pending_quotation(item_codes_qty)
		return self._approve(name)

	def _html(self, name, user=None):
		user = user or self.facturacion
		with fx.as_user(user):
			return frappe.get_print("Quotation", name, print_format=PDF_FORMAT, as_pdf=False)

	# A. Print Format existe
	def test_a_print_format_exists(self):
		self.assertTrue(frappe.db.exists("Print Format", PDF_FORMAT))

	# B. DocType == Quotation
	def test_b_doc_type_is_quotation(self):
		self.assertEqual(frappe.db.get_value("Print Format", PDF_FORMAT, "doc_type"), "Quotation")
		self.assertEqual(frappe.db.get_value("Print Format", PDF_FORMAT, "disabled"), 0)

	# C. aprobado permite PDF
	def test_c_approved_allows_pdf(self):
		item = self._priced_item("FG2515-C-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn(name, html)

	# D. Pending rechaza
	def test_d_pending_rejects(self):
		item = self._priced_item("FG2515-D-ITEM")
		name = self._pending_quotation([(item.name, 1)])
		with self.assertRaises(frappe.ValidationError):
			self._html(name)

	# E. Devuelta rechaza
	def test_e_returned_rejects(self):
		item = self._priced_item("FG2515-E-ITEM")
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(name, reason="Corrige el precio")
		with self.assertRaises(frappe.ValidationError):
			self._html(name)

	# F. Borrador rechaza
	def test_f_draft_review_status_rejects(self):
		item = self._priced_item("FG2515-F-ITEM")
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])
		with self.assertRaises(frappe.ValidationError):
			self._html(result["name"])

	# G. Cancelled rechaza
	def test_g_cancelled_rejects(self):
		item = self._priced_item("FG2515-G-ITEM")
		name = self._approved_quotation(item_code=item.name)
		qtn = frappe.get_doc("Quotation", name)
		qtn.check_permission("cancel")
		qtn.cancel()
		# Frappe's OWN native "Not allowed to print cancelled documents"
		# gate (Print Settings.allow_print_for_cancelled, default 0) fires
		# BEFORE this commit's own `before_print` hook even runs -- still a
		# correct rejection, just `frappe.PermissionError` (a sibling of
		# `ValidationError`, not a subclass -- confirmed by reading
		# frappe/exceptions.py directly), not this app's own message.
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			self._html(name)

	# H. amendment vigente aprobado permite
	def test_h_current_approved_amendment_allows(self):
		item = self._priced_item("FG2515-H-ITEM", rate=300)
		name = self._pending_quotation([(item.name, 2)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		html = self._html(approved_name)
		self.assertIn(approved_name, html)

	# I. amendment viejo cancelado rechaza
	def test_i_superseded_original_rejects(self):
		item = self._priced_item("FG2515-I-ITEM", rate=300)
		name = self._pending_quotation([(item.name, 2)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", applied["name"])
		self._approve(applied["name"])
		# `name` (the ORIGINAL) is now docstatus=2, superseded -- rejected
		# by Frappe's own native cancelled-document print gate (see test_g's
		# own comment for why this is `PermissionError`, not
		# `ValidationError`) before this commit's own hook is even reached.
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			self._html(name)

	# J. número dinámico
	def test_j_dynamic_number(self):
		item = self._priced_item("FG2515-J-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn(f"No. {name}", html)
		self.assertNotIn("6969", html)

	# K. fecha dinámica
	def test_k_dynamic_date(self):
		item = self._priced_item("FG2515-K-ITEM")
		name = self._approved_quotation(item_code=item.name)
		qtn = frappe.get_doc("Quotation", name)
		expected = cotizaciones._format_date_es_long(qtn.transaction_date)
		html = self._html(name)
		self.assertIn(expected, html)
		self.assertNotIn("10 de septiembre de 2026", html)

	# L. cliente dinámico
	def test_l_dynamic_customer(self):
		item = self._priced_item("FG2515-L-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn("FG2515 Customer", html)

	# M. NIT dinámico
	def test_m_dynamic_nit(self):
		item = self._priced_item("FG2515-M-ITEM")
		frappe.db.set_value("Customer", self.customer.name, "tax_id", "900123456-7")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn("900123456-7", html)

	# N. contacto
	def test_n_contact_name(self):
		item = self._priced_item("FG2515-N-ITEM")
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "FG2515 Contacto",
				"links": [{"link_doctype": "Customer", "link_name": self.customer.name}],
			}
		)
		contact.insert()
		self.world.track_existing("Contact", contact.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])
		frappe.db.set_value("Quotation", result["name"], "contact_person", contact.name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		approved_name = self._approve(result["name"])

		html = self._html(approved_name)
		self.assertIn("FG2515 Contacto", html)

	# O. teléfono (resolved from Contact's own phone_nos child table, not
	# just the Quotation's own possibly-empty contact_mobile snapshot --
	# see prepare_and_guard_quotation_pdf()'s own docstring).
	def test_o_contact_phone_from_child_table(self):
		item = self._priced_item("FG2515-O-ITEM")
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "FG2515 Contacto Tel",
				"links": [{"link_doctype": "Customer", "link_name": self.customer.name}],
				"phone_nos": [{"phone": "(300)-1234567", "is_primary_mobile_no": 1}],
			}
		)
		contact.insert()
		self.world.track_existing("Contact", contact.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])
		frappe.db.set_value("Quotation", result["name"], "contact_person", contact.name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		approved_name = self._approve(result["name"])

		html = self._html(approved_name)
		self.assertIn("(300)-1234567", html)

	# P. dirección
	def test_p_address_shows_dash_when_absent(self):
		item = self._priced_item("FG2515-P-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn("—", html)  # never invents an address

	# Q. correo
	def test_q_email_from_child_table(self):
		item = self._priced_item("FG2515-Q-ITEM")
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "FG2515 Contacto Mail",
				"links": [{"link_doctype": "Customer", "link_name": self.customer.name}],
				"email_ids": [{"email_id": "contacto2515@example.com", "is_primary": 1}],
			}
		)
		contact.insert()
		self.world.track_existing("Contact", contact.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])
		frappe.db.set_value("Quotation", result["name"], "contact_person", contact.name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		approved_name = self._approve(result["name"])

		html = self._html(approved_name)
		self.assertIn("contacto2515@example.com", html)

	# R. payment terms
	def test_r_payment_terms_label(self):
		if not frappe.db.exists("Payment Terms Template", "FG2515 Terms"):
			ptt = frappe.get_doc(
				{
					"doctype": "Payment Terms Template",
					"template_name": "FG2515 Terms",
					"terms": [{"invoice_portion": 100, "credit_days_based_on": "Day(s) after invoice date", "credit_days": 30}],
				}
			)
			ptt.insert()
			self.world.track_existing("Payment Terms Template", ptt.name)

		item = self._priced_item("FG2515-R-ITEM")
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])
		frappe.db.set_value("Quotation", result["name"], "payment_terms_template", "FG2515 Terms")
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		approved_name = self._approve(result["name"])

		html = self._html(approved_name)
		self.assertIn("FG2515 Terms", html)

	# S. todos los items aparecen
	def test_s_every_item_appears(self):
		item1 = self._priced_item("FG2515-S1-ITEM")
		item2 = self._priced_item("FG2515-S2-ITEM")
		name = self._approved_quotation(item_codes_qty=[(item1.name, 1), (item2.name, 2)])
		html = self._html(name)
		self.assertIn(item1.name, html)
		self.assertIn(item2.name, html)

	# T. qty correcta
	def test_t_correct_qty(self):
		item = self._priced_item("FG2515-T-ITEM")
		name = self._approved_quotation(item_code=item.name, qty=7)
		html = self._html(name)
		self.assertIn(">7<", html)

	# U. rate persistido correcto (nunca recalculado en el PDF)
	def test_u_persisted_rate_is_printed(self):
		item = self._priced_item("FG2515-U-ITEM", rate=333)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])

		doc = frappe.get_doc("Quotation", approved_name)
		self.assertAlmostEqual(doc.items[0].rate, 333 * 0.90)  # 299.7, native float rounding

		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(doc.items[0].rate, currency=doc.currency, precision=0), html)

	# V. descuento no se aplica dos veces
	def test_v_discount_never_applied_twice(self):
		item = self._priced_item("FG2515-V-ITEM", rate=1000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])

		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 900)  # 1000 * 0.9, never 1000*0.9*0.9=810

		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(900, currency=doc.currency, precision=0), html)
		self.assertNotIn(frappe.utils.fmt_money(810, currency=doc.currency, precision=0), html)

	# W. IVA correcto -- sin plantilla de impuestos aplicada (estado real de
	# este sitio hoy), cada línea imprime "Exe", nunca un porcentaje
	# inventado.
	def test_w_tax_label_is_exe_when_no_tax_template_applied(self):
		item = self._priced_item("FG2515-W-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertIn("Exe", html)

	# X. grand_total correcto
	def test_x_grand_total_correct(self):
		item = self._priced_item("FG2515-X-ITEM", rate=450)
		name = self._approved_quotation(item_code=item.name, qty=2)
		doc = frappe.get_doc("Quotation", name)
		html = self._html(name)
		self.assertIn(frappe.utils.fmt_money(doc.grand_total, currency=doc.currency, precision=0), html)
		self.assertEqual(doc.grand_total, 900)

	# Y. valid_till correcto
	def test_y_valid_till_correct(self):
		item = self._priced_item("FG2515-Y-ITEM")
		name = self._pending_quotation([(item.name, 1)])
		frappe.db.set_value("Quotation", name, "valid_till", "2026-12-31")
		approved_name = self._approve(name)
		html = self._html(approved_name)
		self.assertIn(cotizaciones._format_date_es_long("2026-12-31"), html)

	# Z. asesora dinámica -- Administrator (the only real "Vendedora" this
	# site has today) is explicitly EXCLUDED, per section 15's own rule.
	def test_z_advisor_name_never_shows_administrator(self):
		item = self._priced_item("FG2515-Z-ITEM")
		with fx.as_user(self.facturacion):
			# create as Administrator context (owner ends up Administrator)
			pass
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": frappe.defaults.get_global_default("company"),
				"items": [{"item_code": item.name, "qty": 1}],
			}
		)
		qtn.insert()
		self.world.track_existing("Quotation", qtn.name)
		qtn.submit()
		qtn.fg_billing_review_status = "Pendiente de Facturación"
		qtn.save()
		approved_name = self._approve(qtn.name)

		self.assertEqual(frappe.db.get_value("Quotation", approved_name, "owner"), "Administrator")
		html = self._html(approved_name)
		self.assertNotIn("Administrator", html)
		self.assertNotIn("Asesor(a) Comercial", html)

	# AA. datos Company dinámicos -- nunca los valores de ejemplo de la
	# referencia.
	def test_aa_company_data_is_dynamic_never_the_reference_example(self):
		item = self._priced_item("FG2515-AA-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertNotIn("Calle 46 Numero 22-28", html)
		self.assertNotIn("6533068", html)
		self.assertNotIn("3118814375", html)
		self.assertNotIn("mercadeo@fabrigraysas.com", html)

	# AB. no valuation_rate
	def test_ab_no_valuation_rate(self):
		item = self._priced_item("FG2515-AB-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertNotIn("valuation_rate", html)

	# AC. no buying_rate / precio de compra
	def test_ac_no_buying_rate(self):
		item = self._priced_item("FG2515-AC-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertNotIn("buying_rate", html)
		self.assertNotIn("last_purchase_rate", html)

	# AD. no disponibilidad ERP
	def test_ad_no_erp_availability(self):
		item = self._priced_item("FG2515-AD-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertNotIn("Disponible", html)
		self.assertNotIn("Faltante", html)

	# AE. no warehouse
	def test_ae_no_warehouse(self):
		item = self._priced_item("FG2515-AE-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		self.assertNotIn("warehouse", html.lower())
		self.assertNotIn("almacén", html.lower())

	# AF. no notas internas de Facturación
	def test_af_no_internal_facturacion_notes(self):
		item = self._priced_item("FG2515-AF-ITEM")
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(name, reason="Precio secreto interno XYZ")
		with fx.as_user(self.vendedora):
			edited = cotizaciones.modify_submitted_quotation(
				name=name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", edited["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(edited["name"])
		approved_name = self._approve(edited["name"])

		html = self._html(approved_name)
		self.assertNotIn("Precio secreto interno XYZ", html)
		self.assertNotIn("reviewed_by", html)

	# AG. 1 item
	def test_ag_single_item_renders(self):
		item = self._priced_item("FG2515-AG-ITEM")
		name = self._approved_quotation(item_code=item.name)
		html = self._html(name)
		# 1 header cell + 1 body cell for the single row (a 3rd occurrence
		# of the class name is the CSS rule itself, embedded in the same
		# printview page this HTML-mode get_print() call returns).
		self.assertEqual(html.count('class="fg-pdf-col-desc"'), 2)
		self.assertEqual(html.count(item.name), 2)  # item_code shown once + item name row's own td text

	# AH. 30 items
	def test_ah_thirty_items_render(self):
		items = [self._priced_item(f"FG2515-AH-ITEM-{i}", rate=100 + i) for i in range(30)]
		name = self._approved_quotation(item_codes_qty=[(it.name, 1) for it in items])
		html = self._html(name)
		for it in items:
			self.assertIn(it.name, html)

	# AI. tabla multipágina -- CSS contract (no wkhtmltopdf in this
	# environment to render an actual page break, see class docstring).
	def test_ai_multipage_css_rules_present(self):
		css = frappe.db.get_value("Print Format", PDF_FORMAT, "css")
		self.assertIn("display: table-header-group", css)
		self.assertIn("page-break-inside: avoid", css)

	# AM. Company isolation
	def test_am_company_isolation(self):
		other_customer = self.world.customer("FG2515 Other Company Customer")
		other_item = self.world.item("FG2515-OTHER-COMPANY-ITEM")
		other_qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": other_item.name, "qty": 1, "rate": 100}],
			}
		)
		other_qtn.insert()
		self.world.track_existing("Quotation", other_qtn.name)
		other_qtn.submit()
		other_qtn.fg_billing_review_status = "Aprobada"
		other_qtn.save()

		with self.assertRaises(frappe.PermissionError):
			self._html(other_qtn.name, user=self.facturacion)

	# AN. permisos Quotation -- un rol sin acceso a Quotation en absoluto
	# (Bodega) es rechazado por el chequeo nativo de permisos, antes de
	# que este hook siquiera importe.
	def test_an_user_without_quotation_permission_is_blocked(self):
		item = self._priced_item("FG2515-AN-ITEM")
		name = self._approved_quotation(item_code=item.name)
		with self.assertRaises(frappe.PermissionError):
			self._html(name, user=self.bodega)

	# AO. filename correcto
	def test_ao_download_sets_the_expected_filename(self):
		item = self._priced_item("FG2515-AO-ITEM")
		name = self._approved_quotation(item_code=item.name)

		from frappe.utils import print_format as print_format_module

		original = print_format_module.download_pdf
		print_format_module.download_pdf = lambda **kwargs: None
		try:
			with fx.as_user(self.facturacion):
				cotizaciones.download_fabrigray_quotation_pdf(name)
		finally:
			print_format_module.download_pdf = original

		self.assertEqual(frappe.local.response.filename, f"Cotizacion-Fabrigray-{name}.pdf")

	# ------------------------------------------------------------------
	# Commit 25.15 "REVISIÓN FINAL OBLIGATORIA" review fix
	# ------------------------------------------------------------------

	# F. otro Print Format de Quotation no queda bloqueado globalmente --
	# section 1/3's own explicit concern: `before_print` must be scoped to
	# ONLY "Fabrigray Cotización Comercial", never a blanket block on every
	# print of a Quotation regardless of format. A second, throwaway Print
	# Format is created here (Standard-style, no business-rule content of
	# its own) and applied to a Borrador Quotation -- something the
	# commercial format would reject outright -- to prove the hook truly
	# lets it through untouched.
	def test_f_other_print_format_never_blocked_globally(self):
		other_format_name = "FG2515 Internal Test Format"
		if not frappe.db.exists("Print Format", other_format_name):
			frappe.get_doc(
				{
					"doctype": "Print Format",
					"name": other_format_name,
					"doc_type": "Quotation",
					"module": "Fabrigray ERP",
					"standard": "No",
					"custom_format": 1,
					"print_format_type": "Jinja",
					"html": "<div>{{ doc.name }}</div>",
				}
			).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Print Format", other_format_name, force=True))

		item = self._priced_item("FG2515-F2-ITEM")
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Quotation", result["name"])

		# still "Borrador" review status -- the commercial format would
		# reject this outright; the OTHER format must render it fine.
		with fx.as_user(self.facturacion):
			html = frappe.get_print("Quotation", result["name"], print_format=other_format_name, as_pdf=False)
		self.assertIn(result["name"], html)

	# G. endpoint usa exactamente "Fabrigray Cotización Comercial" -- nunca
	# un formato distinto, ni uno elegido por el cliente.
	def test_g_endpoint_uses_exactly_the_commercial_format(self):
		item = self._priced_item("FG2515-G2-ITEM")
		name = self._approved_quotation(item_code=item.name)

		captured = {}
		from frappe.utils import print_format as print_format_module

		original = print_format_module.download_pdf

		def _capture(**kwargs):
			captured.update(kwargs)

		print_format_module.download_pdf = _capture
		try:
			with fx.as_user(self.facturacion):
				cotizaciones.download_fabrigray_quotation_pdf(name)
		finally:
			print_format_module.download_pdf = original

		self.assertEqual(captured.get("format"), PDF_FORMAT)
		self.assertEqual(captured.get("doctype"), "Quotation")

	# L. VER PDF usa el endpoint seguro -- devuelve una URL solo tras
	# validar server-side; nunca acepta un formato/nombre libre del
	# cliente que salte `_assert_quotation_pdf_eligible()`.
	def test_l_view_pdf_uses_the_secure_endpoint(self):
		item = self._priced_item("FG2515-L2-ITEM")
		name = self._approved_quotation(item_code=item.name)
		with fx.as_user(self.facturacion):
			url = cotizaciones.get_fabrigray_quotation_pdf_view_url(name)
		self.assertIn(name, url)
		self.assertIn("Fabrigray", url)

		# a NON-eligible Quotation (still Pendiente) is rejected by the
		# SAME endpoint, before any URL is ever returned.
		item2 = self._priced_item("FG2515-L3-ITEM")
		pending_name = self._pending_quotation([(item2.name, 1)])
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.get_fabrigray_quotation_pdf_view_url(pending_name)

	# M. DESCARGAR PDF usa el endpoint seguro -- misma validación explícita
	# ANTES de generar cualquier PDF, nunca delegada solo al hook
	# before_print.
	def test_m_download_pdf_uses_the_secure_endpoint(self):
		item = self._priced_item("FG2515-M2-ITEM")
		pending_name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.download_fabrigray_quotation_pdf(pending_name)
		# proof the rejection happened BEFORE the native call, never after:
		# frappe.local.response.filename was never set for this attempt.
		self.assertNotEqual(
			getattr(frappe.local.response, "filename", None), f"Cotizacion-Fabrigray-{pending_name}.pdf"
		)

	# O-S. cada modo de precio queda reflejado tal cual persistido, sobre
	# una base de 100.000 -- el PDF nunca recalcula, solo imprime.
	def test_o_price_mode_discount_10_reflected(self):
		item = self._priced_item("FG2515-O2-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 90000)
		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(90000, currency=doc.currency, precision=0), html)

	def test_p_price_mode_discount_15_reflected(self):
		item = self._priced_item("FG2515-P2-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_15")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 85000)
		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(85000, currency=doc.currency, precision=0), html)

	def test_q_price_mode_discount_20_reflected(self):
		item = self._priced_item("FG2515-Q2-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_20")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 80000)
		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(80000, currency=doc.currency, precision=0), html)

	def test_r_price_mode_discount_25_reflected(self):
		item = self._priced_item("FG2515-R2-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_25")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 75000)
		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(75000, currency=doc.currency, precision=0), html)

	def test_s_price_mode_full_reflected(self):
		item = self._priced_item("FG2515-S3-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			applied = cotizaciones.apply_quotation_price_mode(name, "FULL")
		self.world.track_existing("Quotation", applied["name"])
		approved_name = self._approve(applied["name"])
		doc = frappe.get_doc("Quotation", approved_name)
		self.assertEqual(doc.items[0].rate, 100000)
		html = self._html(approved_name)
		self.assertIn(frappe.utils.fmt_money(100000, currency=doc.currency, precision=0), html)

	# T. "Descuento 5%"/DISCOUNT_5 nunca aparece como modalidad comercial
	# válida -- ni en el backend, ni renderizable en el PDF.
	def test_t_discount_5_never_a_valid_commercial_modality(self):
		self.assertNotIn("DISCOUNT_5", cotizaciones.PRICE_MODE_DISCOUNTS)
		self.assertNotIn("Descuento 5%", cotizaciones._PRICE_MODE_LABELS.values())
		item = self._priced_item("FG2515-T2-ITEM", rate=100000)
		name = self._pending_quotation([(item.name, 1)])
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_5")

	# U. IVA 5% sigue totalmente soportado e independiente de la
	# modalidad comercial -- el renglón de IMPUESTO usa el `rate` real de
	# `item_wise_tax_details`, nunca los códigos de descuento comercial
	# (nunca hubo ninguna relación entre ambos, esto solo confirma que el
	# rename de la sección 6 no tocó -- ni podía tocar -- esta ruta).
	def test_u_iva_5_percent_still_fully_supported(self):
		item = self._priced_item("FG2515-U2-ITEM", rate=1000)
		name = self._approved_quotation(item_code=item.name)
		doc = frappe.get_doc("Quotation", name)
		row = doc.items[0]

		# simulate a real 5% VAT line the same shape ERPNext's own
		# taxes_and_totals.py would have produced, had a tax template with
		# a 5% rate been applied -- proves the PDF's tax-rendering path
		# (independent of the commercial price-mode enum) still correctly
		# supports and displays ANY real rate, including 5%.
		doc.append(
			"item_wise_tax_details",
			{"item_row": row.name, "tax_row": "test-tax-row", "rate": 5, "amount": 50, "taxable_amount": 1000},
		)
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)

		html = self._html(name)
		self.assertIn("5%", html)
		self.assertIn(frappe.utils.fmt_money(50, currency=doc.currency, precision=0), html)


class TestQuotationPdfUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cotizaciones_js = _read(_COTIZACIONES_JS_PATH)
		cls.facturacion_js = _read(_FACTURACION_JS_PATH)

	# AJ. botón Ver PDF solo Approved
	def test_aj_view_pdf_button_only_for_approved_and_not_cancelled(self):
		body = _method_body(self.cotizaciones_js, "render_quotation_card_actions")
		self.assertIn('billing_status === "Aprobada" && q.docstatus !== 2', body)
		self.assertIn("fg-quotation-card-view-pdf", body)

	# AK. Descargar PDF solo Approved
	def test_ak_download_pdf_button_only_for_approved_and_not_cancelled(self):
		body = _method_body(self.cotizaciones_js, "render_quotation_card_actions")
		self.assertIn("fg-quotation-card-download-pdf", body)
		# both buttons share the same single `pdf_btns` gate.
		self.assertEqual(body.count("fg-quotation-card-download-pdf"), 1)

	# AL. Facturación abre PDF de versión vigente
	def test_al_facturacion_approve_feedback_opens_the_current_version(self):
		body = _method_body(self.facturacion_js, "approve_billing_review")
		self.assertIn("open_fabrigray_quotation_pdf(result.name)", body)
		self.assertNotIn("open_fabrigray_quotation_pdf(name)", body)

	# L. VER PDF (ambas páginas) usa el endpoint seguro
	# get_fabrigray_quotation_pdf_view_url() -- nunca construye
	# `/printview?...&format=...` directamente desde un `name`, lo que
	# saltaría la validación server-side.
	def test_l_open_pdf_helper_calls_the_secure_endpoint_in_both_pages(self):
		for label, js in (("cotizaciones.js", self.cotizaciones_js), ("facturacion.js", self.facturacion_js)):
			with self.subTest(file=label):
				body = _function_body(js, "open_fabrigray_quotation_pdf")
				self.assertIn("get_fabrigray_quotation_pdf_view_url", body)
				self.assertNotIn("/printview?doctype=Quotation&name=", body)

	# M. DESCARGAR PDF (Cotizaciones) sigue apuntando exclusivamente al
	# endpoint controlado propio -- nunca al download_pdf nativo genérico
	# con un `format` libre.
	def test_m_download_pdf_helper_calls_only_the_controlled_endpoint(self):
		body = _function_body(self.cotizaciones_js, "download_fabrigray_quotation_pdf")
		self.assertIn("fabergray_erp.api.cotizaciones.download_fabrigray_quotation_pdf", body)
		self.assertNotIn("frappe.utils.print_format.download_pdf", body)
