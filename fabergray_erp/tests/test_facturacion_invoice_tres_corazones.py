# -*- coding: utf-8 -*-
"""3CORAZONESCAFES como TERCERA empresa emisora del flujo de Facturación
(Pick List Facturado): mismo selector, misma validación y mismo contexto
`doc.fg_pdf_*` que integrandoMAS/ecoluminar, con serie de numeración propia y
su propio Print Format ("3CORAZONESCAFES Factura Comercial").

Numeración: la primera factura es la 0001 (start 1, width 4: se guarda el
entero 1 y se imprime "0001"). fixtures.TestWorld aísla la serie con una
clave propia de esta clase y el MISMO inicio, así que los tests afirman
0001/0002/0003 sin tocar nunca la serie real del sitio (test_99 lo
verifica). Helpers prestados de test_facturacion_invoice_pdf.py y de
test_facturacion_invoice_numbering.py (funciones, sin heredar el TestCase)."""

from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fabergray_erp import invoice_issuers
from fabergray_erp.api import bodega, facturacion
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_facturacion_invoice_numbering as numbering_base
from fabergray_erp.tests import test_facturacion_invoice_pdf as pdf_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

TCC = "3CORAZONESCAFES"
TCC_FORMAT = "3CORAZONESCAFES Factura Comercial"
DEFAULT_FORMAT = "Fabrigray Factura Comercial"
REAL_SERIES = ("FG-FACT-INTEGRANDOMAS-", "FG-FACT-ECOLUMINAR-", "FG-FACT-3CORAZONESCAFES-")
PRICE_LIST = "Standard Selling"
TAX_ACCOUNT = "2408 - Impuesto sobre las ventas por pagar - FG"


class TestTresCorazonesInvoice(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Estado de las series REALES antes de crear el TestWorld (que las aísla).
		cls.real_series_before = cls._real_series()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.invoice_numbering = cls.world.invoice_numbering

		sfx = frappe.generate_hash(length=5)
		cls.sfx = sfx
		cls.wh = cls.world.warehouse(f"FGTCC {sfx} WH")
		cls.item_a = cls.world.item(f"FGTCC-{sfx}-A")
		cls.item_b = cls.world.item(f"FGTCC-{sfx}-B")
		cls.item_c = cls.world.item(f"FGTCC-{sfx}-C")
		cls.customer = cls.world.customer(f"FGTCC {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000077-7")
		for item in (cls.item_a, cls.item_b, cls.item_c):
			cls.world.stock_up_real(item.name, cls.wh.name, 1000, rate=50)
		# Precio público (Standard Selling) solo para item_c: la fuente que
		# Facturación congela como fg_invoice_public_rate.
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": cls.item_c.name,
				"price_list": PRICE_LIST,
				"selling": 1,
				"price_list_rate": 10000,
			}
		).insert()
		cls.world.track_existing("Item Price", price.name)

		cls.bodega_user = cls.world.user(f"fgtcc-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgtcc-{sfx}-facturacion@example.com", ["Facturación"])
		cls.facturacion_user_b = cls.world.user(f"fgtcc-{sfx}-facturacion-b@example.com", ["Facturación"])

	@staticmethod
	def _real_series():
		return {
			name: frappe.db.get_value("Series", name, "current", order_by=None) for name in REAL_SERIES
		}

	# -- helpers --------------------------------------------------------------

	_picked_pick_list = pdf_base.TestFacturacionInvoicePdf._picked_pick_list
	_html = pdf_base.TestFacturacionInvoicePdf._html
	_view_url = pdf_base.TestFacturacionInvoicePdf._view_url
	_download = pdf_base.TestFacturacionInvoicePdf._download
	_concurrent = numbering_base.TestFacturacionInvoiceNumbering._concurrent

	def _series_current(self, issuer):
		return frappe.db.get_value("Series", self.invoice_numbering[issuer]["series"], "current", order_by=None)

	def _check_all(self, pl_name):
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)

	def _facturar(self, pl_name, issuer):
		self._check_all(pl_name)
		with fx.as_user(self.facturacion_user):
			return facturacion.mark_as_invoiced(pl_name, issuer)

	def _invoiced(self, issuer=TCC, **kwargs):
		so, pl_name = self._picked_pick_list(**kwargs)
		result = self._facturar(pl_name, issuer)
		return so, pl_name, result["fg_invoice_number"]

	def _checked(self):
		_, pl_name = self._picked_pick_list()
		self._check_all(pl_name)
		return pl_name

	def _pick(self, so):
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return pl.name

	def _sales_order(self, items, taxes=None, additional_discount_percentage=0):
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": self.wh.name,
				"selling_price_list": PRICE_LIST,
				"items": [
					{"warehouse": self.wh.name, "delivery_date": delivery_date, **item} for item in items
				],
				"taxes": taxes or [],
			}
		)
		if additional_discount_percentage:
			doc.apply_discount_on = "Net Total"
			doc.additional_discount_percentage = additional_discount_percentage
		with fx.stock_settings(auto_insert_price_list_rate_if_missing=0):
			doc.insert()
			with fx.without_sales_order_hook():
				doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		self.world._track(doc)
		return doc

	def _foreign_values(self):
		"""Every identifying value of the OTHER two issuers."""
		values = []
		for issuer in ("integrandoMAS", "ecoluminar"):
			config = invoice_issuers.ISSUER_CONFIG[issuer]
			for key in ("display_name", "nit", "email", "address", "bank_account_number", "bank_account_holder"):
				values.append(config[key])
			values.append(config["logo"].rsplit("/", 1)[-1])
		return values + ["LITO CARIBE", "28410436-9", "ECODLUMINAR", "procredito"]

	# -- 1. selector ------------------------------------------------------------

	def test_01_third_issuer_is_selectable_without_consuming_a_number(self):
		_, pl_name = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			result = facturacion.set_invoice_issuer(pl_name, TCC)
		self.assertEqual(result, {"pick_list": pl_name, "fg_invoice_issuer": TCC, "fg_invoice_number": None, "fg_invoice_number_display": None})
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), TCC)
		self.assertIsNone(self._series_current(TCC))
		# y se puede volver a cambiar mientras no tenga número
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "ecoluminar")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")

	def test_01b_select_field_and_labels(self):
		options = frappe.get_meta("Pick List").get_field("fg_invoice_issuer").options.split("\n")
		self.assertEqual(options, ["", "integrandoMAS", "ecoluminar", TCC])
		self.assertEqual(invoice_issuers.INVOICE_ISSUER_LABELS[TCC], "3CORAZONESCAFÉS")

	# -- 2. numeración --------------------------------------------------------

	def test_02_first_three_invoices_are_0001_0002_0003(self):
		"""Primera facturación de 3CORAZONESCAFES en esta clase (orden
		alfabético): la serie aislada arranca igual que la real."""
		self.assertEqual(invoice_issuers.INVOICE_NUMBERING[TCC]["start"], 1)
		self.assertIsNone(self._series_current(TCC))  # nunca usada todavía
		others_before = (self._series_current("integrandoMAS"), self._series_current("ecoluminar"))

		expected = ["0001", "0002", "0003"]
		for position, text in enumerate(expected, start=1):
			_, pl_name = self._picked_pick_list()
			self._check_all(pl_name)
			with fx.as_user(self.facturacion_user):
				result = facturacion.mark_as_invoiced(pl_name, TCC)
			self.assertEqual(result["fg_invoice_number"], position)
			self.assertEqual(result["fg_invoice_number_display"], text)
			self.assertEqual(
				frappe.db.get_value("Pick List", pl_name, ["fg_invoice_issuer", "fg_invoice_number"]), (TCC, position)
			)
			html = self._html(pl_name, print_format=TCC_FORMAT)
			self.assertIn(f"No. {text}", html)
			# la cola y el detalle de Facturación muestran el mismo número formateado
			with fx.as_user(self.facturacion_user):
				queue = facturacion.get_invoicing_queue(status="Facturado", txt=pl_name)
				detail = facturacion.get_invoicing_detail(pl_name)
			row = next(r for r in queue["pick_lists"] if r["name"] == pl_name)
			self.assertEqual(row["fg_invoice_number_display"], text)
			self.assertEqual(detail["fg_invoice_number_display"], text)

		self.assertEqual(self._series_current(TCC), 3)
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), others_before)

	def test_02b_display_format_per_issuer(self):
		fmt = invoice_issuers.format_invoice_number
		self.assertEqual([fmt(TCC, n) for n in (1, 2, 3, 42, 9999, 10000)], ["0001", "0002", "0003", "0042", "9999", "10000"])
		self.assertEqual(fmt("integrandoMAS", 6886), "6886")
		self.assertEqual(fmt("ecoluminar", 2263), "2263")
		self.assertIsNone(fmt(TCC, 0))
		self.assertIsNone(fmt(TCC, None))

	def test_02c_unconfigured_start_still_blocks_without_creating_the_series(self):
		"""Guarda genérica: un emisor sin número inicial no factura."""
		series = self.invoice_numbering[TCC]["series"] + "UNSET-"
		_, pl_name = self._picked_pick_list()
		with patch.dict(invoice_issuers.INVOICE_NUMBERING, {TCC: {"series": series, "start": None}}):
			with self.assertRaises(facturacion.InvoiceNumberingNotConfiguredError):
				self._facturar(pl_name, TCC)
		row = frappe.db.get_value(
			"Pick List", pl_name, ["fg_invoicing_status", "fg_invoice_number", "fg_invoice_issuer"], as_dict=True
		)
		self.assertNotEqual(row.fg_invoicing_status, facturacion.FG_INVOICING_FACTURADO)
		self.assertFalse(row.fg_invoice_number)
		self.assertIsNone(frappe.db.get_value("Series", series, "current", order_by=None))

	def test_03_independent_series_never_touches_the_other_issuers(self):
		others_before = (self._series_current("integrandoMAS"), self._series_current("ecoluminar"))
		start = self._series_current(TCC) or 0
		_, pl_1, n1 = self._invoiced(TCC)
		_, pl_2, n2 = self._invoiced(TCC)
		self.assertEqual((n1, n2), (start + 1, start + 2))
		self.assertEqual(self._series_current(TCC), start + 2)
		self.assertEqual((self._series_current("integrandoMAS"), self._series_current("ecoluminar")), others_before)
		self.assertEqual(frappe.db.get_value("Pick List", pl_1, "fg_invoice_number_key"), f"{self.invoice_numbering[TCC]['series']}{n1}")

		# y numerar integrandoMAS no avanza la serie de 3CORAZONESCAFES
		self._invoiced("integrandoMAS")
		self.assertEqual(self._series_current(TCC), start + 2)
		# en producción la clave UNIQUE guarda el entero: "3CORAZONESCAFES-1"
		self.assertEqual(self.world.production_invoice_number_key(TCC, 1), "3CORAZONESCAFES-1")

	def test_03b_two_simultaneous_invoices_never_share_a_number(self):
		pl_a, pl_b = self._checked(), self._checked()
		start = self._series_current(TCC) or 0
		outcomes = self._concurrent([(pl_a, TCC, self.facturacion_user), (pl_b, TCC, self.facturacion_user_b)])
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual(sorted(o[1] for o in outcomes), [start + 1, start + 2])
		self.assertEqual(self._series_current(TCC), start + 2)

	def test_03c_double_click_on_the_same_invoice_consumes_one_number(self):
		pl_name = self._checked()
		start = self._series_current(TCC) or 0
		outcomes = self._concurrent([(pl_name, TCC, self.facturacion_user), (pl_name, TCC, self.facturacion_user_b)])
		self.assertEqual(sorted(outcomes, key=str), [("already", None), ("ok", start + 1)], outcomes)
		self.assertEqual(self._series_current(TCC), start + 1)

	def test_04_numbered_issuer_is_locked(self):
		_, pl_name, number = self._invoiced(TCC)
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(facturacion.InvoiceIssuerLockedError):
				facturacion.set_invoice_issuer(pl_name, "integrandoMAS")
		self.assertEqual(
			frappe.db.get_value("Pick List", pl_name, ["fg_invoice_issuer", "fg_invoice_number"]), (TCC, number)
		)

	# -- 3. formato propio ------------------------------------------------------

	def test_05_view_and_download_use_its_own_format(self):
		_, pl_name, _n = self._invoiced(TCC)
		calls = self._download(pl_name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0]["format"], TCC_FORMAT)
		self.assertEqual(frappe.local.response.filename, f"Factura-{TCC}-{pl_name}.pdf")
		self.assertIn("format=" + quote(TCC_FORMAT), self._view_url(pl_name))

	def test_06_pdf_shows_only_its_own_identity(self):
		so, pl_name, number = self._invoiced(TCC)
		number = invoice_issuers.format_invoice_number(TCC, number)
		html = self._html(pl_name, print_format=TCC_FORMAT)

		self.assertIn('<span class="tcc-inv-brand-light">3CORAZONES</span>CAFÉS', html)
		self.assertIn("3corazonescafes-logo.png", html)
		self.assertIn("Adeudo a 3CORAZONESCAFÉS el monto neto indicado", html)
		self.assertIn("INFORMACIÓN DE PAGO", html)
		self.assertIn("[pendiente]", html)  # banco / DIAN / régimen aún no entregados
		self.assertIn("BORRADOR — NO VÁLIDO COMO FACTURA DE VENTA", html)
		self.assertIn("faltan datos del emisor: NIT, régimen, dirección, ciudad, teléfono, email", html)
		self.assertIn(f"No. {number}", html)
		self.assertIn(f"Ref. interna: {pl_name}", html)
		self.assertIn(self.customer.customer_name, html)
		self.assertIn("900000077-7", html)
		self.assertIn("$ 15.100", html)
		self.assertIn("QUINCE MIL CIEN PESOS M/CTE", html)

		for value in self._foreign_values():
			self.assertNotIn(value, html, f"aparece dato de otro emisor: {value}")
		lowered = html.lower()
		for name in ("integrando", "ecoluminar", "ecodluminar"):
			self.assertNotIn(name, lowered)
		self.assertNotIn('class="fg-inv', html)  # nada del formato compartido

	def test_07_each_format_only_prints_its_own_issuer(self):
		_, pl_tcc, _n = self._invoiced(TCC)
		_, pl_int, _n = self._invoiced("integrandoMAS")
		with self.assertRaises(facturacion.InvoicePdfNotEligibleError):
			self._html(pl_int, print_format=TCC_FORMAT)
		with self.assertRaises(facturacion.InvoicePdfNotEligibleError):
			self._html(pl_tcc, print_format=DEFAULT_FORMAT)
		# los dos emisores originales siguen con su formato y su imprenta
		html = self._html(pl_int, print_format=DEFAULT_FORMAT)
		self.assertIn("INTEGRANDO MAS BGA", html)
		self.assertIn("Impreso por: LITO CARIBE", html)
		self.assertNotIn("3CORAZONES", html)

	def test_07b_format_fixture(self):
		self.assertEqual(frappe.db.get_value("Print Format", TCC_FORMAT, "doc_type"), "Pick List")
		self.assertEqual(frappe.db.get_value("Print Format", TCC_FORMAT, "disabled"), 0)
		self.assertNotEqual(frappe.get_meta("Pick List").default_print_format, TCC_FORMAT)
		self.assertEqual(invoice_issuers.invoice_print_formats(), {DEFAULT_FORMAT, TCC_FORMAT})
		self.assertEqual(invoice_issuers.invoice_print_format("integrandoMAS"), DEFAULT_FORMAT)
		self.assertEqual(invoice_issuers.invoice_print_format("ecoluminar"), DEFAULT_FORMAT)
		self.assertEqual(invoice_issuers.invoice_print_format(TCC), TCC_FORMAT)
		self.assertIsNone(invoice_issuers.invoice_print_format("fabrigraySAS"))

	# -- 4. varios productos, descuentos, impuestos, ajuste, letras -------------

	def test_08_multiple_products_discount_tax_and_rounding(self):
		so = self._sales_order(
			[
				{"item_code": self.item_a.name, "qty": 1, "rate": 3333},
				{"item_code": self.item_b.name, "qty": 3, "rate": 1200},
				{"item_code": self.item_c.name, "qty": 2, "rate": 9000},  # 10% bajo el público
			],
			taxes=[
				{
					"charge_type": "On Net Total",
					"account_head": TAX_ACCOUNT,
					"description": "IVA 19%",
					"rate": 19,
				}
			],
			additional_discount_percentage=5,
		)
		pl_name = self._pick(so)
		number = invoice_issuers.format_invoice_number(TCC, self._facturar(pl_name, TCC)["fg_invoice_number"])

		so.reload()
		total = flt(so.rounded_total) or flt(so.grand_total)
		with fx.as_user(self.facturacion_user):
			lines, totals = facturacion._build_invoice_lines_and_totals(frappe.get_doc("Pick List", pl_name), so)

		self.assertEqual(len(lines), 3)
		self.assertEqual(totals["total"], total)
		labels = [row["label"] for row in totals["detail"]]
		self.assertEqual(labels[:3], ["Subtotal", "Descuento (5%)", "IVA 19%"])
		# el desglose suma exactamente el TOTAL (descuento, IVA y ajuste al peso incluidos)
		detail_sum = flt(so.total) - flt(so.discount_amount) + flt(so.total_taxes_and_charges)
		if flt(so.rounded_total):
			detail_sum += flt(so.rounding_adjustment)
			if flt(so.rounding_adjustment):
				self.assertEqual(labels[-1], "Ajuste al peso")
		self.assertAlmostEqual(detail_sum, total, places=2)
		# descuento de línea: precio de lista y % solo como información
		line_c = next(line for line in lines if line["item_code"] == self.item_c.name)
		self.assertEqual((line_c["rate"], line_c["list_rate_display"], line_c["discount_display"]), (9000, "$ 10.000", "10%"))
		# vendido a su propio precio -> sin columna de descuento en su fila
		line_b = next(line for line in lines if line["item_code"] == self.item_b.name)
		self.assertEqual((line_b["list_rate_display"], line_b["discount_display"]), ("", ""))
		self.assertTrue(totals["has_line_discounts"])
		# breakdown de los formatos originales intacto
		self.assertEqual([row["label"] for row in totals["breakdown"]], ["Subtotal", "Descuento", "Impuestos"])

		html = self._html(pl_name, print_format=TCC_FORMAT)
		self.assertIn(f"No. {number}", html)
		self.assertIn("Vlr. Lista", html)
		for row in totals["detail"]:
			self.assertIn(row["amount_display"], html)
		self.assertIn(facturacion._format_money_co(total), html)
		self.assertIn(facturacion._amount_in_words_es(total), html)

	# -- 5. una y varias páginas (HTML; este entorno no tiene wkhtmltopdf) ------

	def test_09_single_and_multi_page_layout(self):
		_, pl_short, _n = self._invoiced(TCC)
		short = self._html(pl_short, print_format=TCC_FORMAT)
		self.assertIn('class="tcc-inv-items-filler"', short)  # 1 página: relleno hasta 12 filas

		items = [self.world.item(f"FGTCC-{self.sfx}-L{i:02d}") for i in range(18)]
		for item in items:
			self.world.stock_up_real(item.name, self.wh.name, 10, rate=50)
		so = self._sales_order([{"item_code": item.name, "qty": 1, "rate": 1000 + i} for i, item in enumerate(items)])
		pl_long = self._pick(so)
		self._facturar(pl_long, TCC)
		html = self._html(pl_long, print_format=TCC_FORMAT)
		for item in items:
			self.assertIn(item.name, html)
		self.assertNotIn('class="tcc-inv-items-filler"', html)
		self.assertEqual(html.count("<thead>"), 1)  # wkhtmltopdf repite el thead en cada página
		css = frappe.db.get_value("Print Format", TCC_FORMAT, "css")
		self.assertIn("page-break-inside: avoid", css)
		total = sum(1000 + i for i in range(18))
		self.assertIn(facturacion._format_money_co(total), html)
		self.assertIn(facturacion._amount_in_words_es(total), html)

	# -- 6. configuración / aislamiento -----------------------------------------

	def test_10_config_has_no_data_of_the_other_issuers(self):
		config = invoice_issuers.ISSUER_CONFIG[TCC]
		text = " ".join(" ".join(v) if isinstance(v, tuple) else str(v) for v in config.values())
		for value in self._foreign_values():
			self.assertNotIn(value, text)
		self.assertEqual(
			invoice_issuers.missing_issuer_fields(TCC), ["NIT", "régimen", "dirección", "ciudad", "teléfono", "email"]
		)
		for key in ("nit", "tax_regime", "dian_resolution", "bank_account_number", "legal_name"):
			self.assertIsNone(config[key], key)  # nada inventado
		context = invoice_issuers.get_issuer_config(TCC)
		self.assertFalse(context["show_print_provider"])
		self.assertTrue(invoice_issuers.get_issuer_config("integrandoMAS")["show_print_provider"])

	def test_11_sales_invoice_format_reads_the_same_config(self):
		from fabergray_erp import sales_invoice_print  # noqa: F401 -- el import mismo es la prueba

		issuer = invoice_issuers.get_tres_corazones_issuer()
		config = invoice_issuers.ISSUER_CONFIG[TCC]
		self.assertEqual(issuer["display_name"], "3CORAZONESCAFÉS")
		self.assertEqual(issuer["logo"], config["logo"])
		self.assertEqual(issuer["legal_text"], config["legal_footer"])
		self.assertIsNone(issuer["phones_display"])

	# -- 7. la serie real nunca se toca -----------------------------------------

	def test_99_real_series_never_touched_by_tests(self):
		"""Último test de la clase: tras facturar ~15 veces con los tres
		emisores, las filas REALES de tabSeries siguen exactamente igual
		(la de 3CORAZONESCAFES ni siquiera existe hasta la primera factura
		real)."""
		self.assertEqual(self._real_series(), self.real_series_before)
		for issuer in invoice_issuers.INVOICE_ISSUERS:
			self.assertNotIn(self.invoice_numbering[issuer]["series"], REAL_SERIES)
