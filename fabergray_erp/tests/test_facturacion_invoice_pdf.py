# -*- coding: utf-8 -*-
"""Commit 25.23 -- "Fabrigray Factura Comercial": PDF comercial de factura de
un Pick List Facturado, con empresa emisora (integrandoMAS | ecoluminar)
elegida y persistida en Pick List.fg_invoice_issuer.

Mismo enfoque que test_cotizaciones_pdf.py: el PDF se ejercita por el
pipeline real de Frappe (`frappe.get_print(..., as_pdf=False)` -> before_
print), nunca llamando al hook en aislamiento, y la generación binaria se
sustituye por un capturador en los tests de DESCARGAR (este entorno no tiene
wkhtmltopdf). Nombres con sufijo aleatorio: un setUpClass interrumpido no
deja registros que choquen con la siguiente corrida.
"""

import inspect
import os
import re

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp import invoice_issuers
from fabergray_erp.api import bodega, cotizaciones, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

INVOICE_FORMAT = facturacion.INVOICE_PDF_PRINT_FORMAT_NAME

_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)


def _function_body(source, function_name):
	m = re.search(r"\nfunction " + re.escape(function_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"function {function_name!r} not found")
	start = m.end()
	next_decl = re.search(r"\n(function |const )[a-zA-Z_]", source[start:])
	end = start + next_decl.start() if next_decl else len(source)
	return source[start:end]


class _CaptureDownload:
	"""Sustituye frappe.utils.print_format.download_pdf (sin wkhtmltopdf aquí)
	y registra con qué argumentos lo llamó el endpoint."""

	def __init__(self):
		self.calls = []

	def __enter__(self):
		from frappe.utils import print_format as print_format_module

		self._module = print_format_module
		self._original = print_format_module.download_pdf
		print_format_module.download_pdf = lambda **kwargs: self.calls.append(kwargs)
		return self

	def __exit__(self, *exc):
		self._module.download_pdf = self._original
		return False


class TestFacturacionInvoicePdf(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FG2523 {sfx} WH")
		cls.item_a = cls.world.item(f"FG2523-{sfx}-A")
		cls.item_b = cls.world.item(f"FG2523-{sfx}-B")
		cls.customer = cls.world.customer(f"FG2523 {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000001-1")
		cls.world.stock_up_real(cls.item_a.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up_real(cls.item_b.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user(f"fg2523-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fg2523-{sfx}-facturacion@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user(f"fg2523-{sfx}-vendedora@example.com", ["Vendedora"])
		cls.jefe_user = cls.world.user(f"fg2523-{sfx}-jefe@example.com", ["Jefe de Bodega"])

	# -- helpers --------------------------------------------------------------

	def _picked_pick_list(self, qty_a=2, rate_a=5750, qty_b=3, rate_b=1200):
		so = self.world.multi_item_sales_order(
			self.customer.name,
			[
				{"item_code": self.item_a.name, "warehouse": self.wh.name, "qty": qty_a, "rate": rate_a},
				{"item_code": self.item_b.name, "warehouse": self.wh.name, "qty": qty_b, "rate": rate_b},
			],
		)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, pl.name

	def _invoiced_pick_list(self, **kwargs):
		so, pl_name = self._picked_pick_list(**kwargs)
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl_name)
		return so, pl_name

	def _with_issuer(self, issuer="integrandoMAS", **kwargs):
		so, pl_name = self._invoiced_pick_list(**kwargs)
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, issuer)
		return so, pl_name

	def _html(self, pl_name, user=None, print_format=INVOICE_FORMAT):
		with fx.as_user(user or self.facturacion_user):
			return frappe.get_print("Pick List", pl_name, print_format=print_format, as_pdf=False)

	def _view_url(self, pl_name, user=None):
		with fx.as_user(user or self.facturacion_user):
			return facturacion.get_invoice_pdf_view_url(pl_name)

	def _download(self, pl_name, user=None):
		with _CaptureDownload() as capture, fx.as_user(user or self.facturacion_user):
			facturacion.download_invoice_pdf(pl_name)
		return capture.calls

	def _all_pdf_paths(self, pl_name, user=None):
		"""Los tres caminos al PDF: VER, DESCARGAR y el before_print (Desk)."""
		return (
			lambda: self._view_url(pl_name, user=user),
			lambda: self._download(pl_name, user=user),
			lambda: self._html(pl_name, user=user),
		)

	# -- 1/2. emisores aceptados ----------------------------------------------

	def test_01_integrandomas_accepted(self):
		_, pl_name = self._invoiced_pick_list()
		with fx.as_user(self.facturacion_user):
			result = facturacion.set_invoice_issuer(pl_name, "integrandoMAS")
		self.assertEqual(result["fg_invoice_issuer"], "integrandoMAS")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "integrandoMAS")
		self.assertIn("INTEGRANDO MAS BGA", self._html(pl_name))

	def test_02_ecoluminar_accepted(self):
		_, pl_name = self._invoiced_pick_list()
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "ecoluminar")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")
		self.assertIn("ECOLUMINAR", self._html(pl_name))

	# -- 3/4. emisores rechazados ----------------------------------------------

	def test_03_other_issuer_rejected(self):
		_, pl_name = self._invoiced_pick_list()
		for bad in ("fabrigraySAS", "IVA", "amore", "INTEGRANDOMAS", "<script>x</script>"):
			with fx.as_user(self.facturacion_user):
				with self.assertRaises(facturacion.InvalidInvoiceIssuerError, msg=bad):
					facturacion.set_invoice_issuer(pl_name, bad)
		self.assertFalse(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"))

	def test_04_empty_issuer_rejected(self):
		_, pl_name = self._invoiced_pick_list()
		for empty in (None, ""):
			with fx.as_user(self.facturacion_user):
				with self.assertRaises(facturacion.InvoiceIssuerRequiredError):
					facturacion.set_invoice_issuer(pl_name, empty)

	def test_04b_pdf_without_saved_issuer_rejected_everywhere(self):
		"""Facturado pero sin emisor persistido -- ningún camino genera PDF."""
		_, pl_name = self._invoiced_pick_list()
		for path in self._all_pdf_paths(pl_name):
			with self.assertRaises(facturacion.InvoiceIssuerRequiredError):
				path()

	# -- 5. usuario sin permiso ---------------------------------------------------

	def test_05_users_without_facturacion_role_rejected(self):
		"""Bodega/Jefe de Bodega LEEN Pick List (y validate_print_permission()
		acepta `read`), así que el guard de rol es lo que los detiene --
		también en el camino Desk/printview. Vendedora no lee Pick List."""
		_, pl_name = self._with_issuer()
		for user in (self.bodega_user, self.jefe_user, self.vendedora_user):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					facturacion.set_invoice_issuer(pl_name, "ecoluminar")
			for path in self._all_pdf_paths(pl_name, user=user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					path()
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "integrandoMAS")

	def test_05b_administrator_allowed(self):
		_, pl_name = self._with_issuer()
		self.assertIn("/printview?", self._view_url(pl_name, user="Administrator"))
		self.assertIn("FACTURA DE VENTA", self._html(pl_name, user="Administrator"))

	# -- 6. documento inexistente -------------------------------------------------

	def test_06_missing_document_rejected(self):
		missing = "FG2523-NO-EXISTE"
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(frappe.DoesNotExistError):
				facturacion.set_invoice_issuer(missing, "integrandoMAS")
			with self.assertRaises(frappe.DoesNotExistError):
				facturacion.get_invoice_pdf_view_url(missing)
			with self.assertRaises(frappe.DoesNotExistError):
				facturacion.download_invoice_pdf(missing)

	# -- 7. no facturado / no elegible --------------------------------------------

	def test_07_not_invoiced_rejected(self):
		_, pl_name = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(facturacion.InvoicePdfNotEligibleError):
				facturacion.set_invoice_issuer(pl_name, "integrandoMAS")
		# aunque alguien forzara el campo, los tres caminos siguen rechazando
		frappe.db.set_value("Pick List", pl_name, "fg_invoice_issuer", "integrandoMAS")
		for path in self._all_pdf_paths(pl_name):
			with self.assertRaises(facturacion.InvoicePdfNotEligibleError):
				path()

	def test_07b_forced_invalid_stored_issuer_rejected(self):
		"""Un valor fuera de la whitelist en la BD nunca llega al PDF."""
		_, pl_name = self._with_issuer()
		frappe.db.set_value("Pick List", pl_name, "fg_invoice_issuer", "fabrigraySAS")
		for path in self._all_pdf_paths(pl_name):
			with self.assertRaises(facturacion.InvalidInvoiceIssuerError):
				path()

	# -- 8. solo el Print Format autorizado ---------------------------------------

	def test_08_endpoints_only_use_the_authorized_format(self):
		_, pl_name = self._with_issuer()
		calls = self._download(pl_name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0]["format"], INVOICE_FORMAT)
		self.assertEqual(calls[0]["doctype"], "Pick List")
		self.assertEqual(calls[0]["name"], pl_name)
		self.assertEqual(frappe.local.response.filename, f"Factura-integrandoMAS-{pl_name}.pdf")

		url = self._view_url(pl_name)
		self.assertIn("format=Fabrigray%20Factura%20Comercial", url)

		# ningún endpoint acepta un formato (ni nada más) del cliente
		for fn in (facturacion.get_invoice_pdf_view_url, facturacion.download_invoice_pdf):
			self.assertEqual(list(inspect.signature(fn).parameters), ["pick_list_name"])
		self.assertEqual(
			list(inspect.signature(facturacion.set_invoice_issuer).parameters), ["pick_list_name", "issuer"]
		)

	def test_08b_print_format_fixture_is_isolated(self):
		self.assertEqual(frappe.db.get_value("Print Format", INVOICE_FORMAT, "doc_type"), "Pick List")
		self.assertEqual(frappe.db.get_value("Print Format", INVOICE_FORMAT, "disabled"), 0)
		self.assertEqual(frappe.db.get_value("Print Format", INVOICE_FORMAT, "standard"), "No")
		self.assertNotEqual(frappe.get_meta("Pick List").default_print_format, INVOICE_FORMAT)

	# -- 9/10. persistencia del emisor ------------------------------------------

	def test_09_regenerating_keeps_the_same_issuer(self):
		_, pl_name = self._with_issuer("ecoluminar")
		first = self._html(pl_name)
		second = self._html(pl_name)
		for html in (first, second):
			self.assertIn("ECOLUMINAR", html)
			self.assertNotIn("INTEGRANDO MAS BGA", html)
		self._download(pl_name)
		self.assertEqual(frappe.local.response.filename, f"Factura-ecoluminar-{pl_name}.pdf")

	def test_10_issuer_change_is_persisted(self):
		_, pl_name = self._with_issuer("integrandoMAS")
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "ecoluminar")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")
		html = self._html(pl_name)
		self.assertIn("ECOLUMINAR", html)
		self.assertNotIn("INTEGRANDO MAS BGA", html)

		# y la cola de Facturación lo devuelve para pintar el select
		with fx.as_user(self.facturacion_user):
			queue = facturacion.get_invoicing_queue(status="Facturado", txt=pl_name)
		row = next(r for r in queue["pick_lists"] if r["name"] == pl_name)
		self.assertEqual(row["fg_invoice_issuer"], "ecoluminar")

	def test_10b_same_issuer_twice_is_a_noop(self):
		_, pl_name = self._with_issuer("integrandoMAS")
		modified = frappe.db.get_value("Pick List", pl_name, "modified")
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "integrandoMAS")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "modified"), modified)

	# -- 11. datos y totales del documento real -----------------------------------

	def test_11_lines_and_totals_come_from_the_real_documents(self):
		so, pl_name = self._with_issuer(qty_a=2, rate_a=5750, qty_b=3, rate_b=1200)
		html = self._html(pl_name)

		self.assertIn(self.item_a.name, html)  # item_name == item_code en fixtures
		self.assertIn("$ 5.750", html)
		self.assertIn("$ 11.500", html)
		self.assertIn("$ 1.200", html)
		self.assertIn("$ 3.600", html)
		# pedido completo -> TOTAL = grand_total/rounded_total real del pedido
		so_total = frappe.db.get_value("Sales Order", so.name, "rounded_total") or frappe.db.get_value(
			"Sales Order", so.name, "grand_total"
		)
		self.assertEqual(so_total, 15100)
		self.assertIn("$ 15.100", html)
		self.assertIn("QUINCE MIL CIEN PESOS M/CTE", html)

		self.assertIn(self.customer.customer_name, html)
		self.assertIn("900000001-1", html)
		# nunca el PEDIDO como número de factura
		self.assertIn("SIN NUMERAR", html)
		self.assertNotIn(f"No. {so.name}", html)
		self.assertIn(f"Ref. interna: {pl_name}", html)
		# sin datos reales del emisor -> marcado como borrador
		self.assertIn("NO VÁLIDO COMO FACTURA", html)

	def test_11b_partial_pick_list_totals_use_picked_qty(self):
		so, pl_name = self._with_issuer(qty_a=2, rate_a=5750, qty_b=3, rate_b=1200)
		row = frappe.get_all(
			"Pick List Item", filters={"parent": pl_name, "item_code": self.item_b.name}, pluck="name"
		)[0]
		frappe.db.set_value("Pick List Item", row, "picked_qty", 1)
		html = self._html(pl_name)
		self.assertIn("$ 12.700", html)  # 2 x 5.750 + 1 x 1.200
		self.assertNotIn("$ 15.100", html)

	def test_11c_money_and_words_helpers(self):
		fmt = facturacion._format_money_co
		self.assertEqual(fmt(11500), "$ 11.500")
		self.assertEqual(fmt(5750), "$ 5.750")
		self.assertEqual(fmt(1234567.5), "$ 1.234.567,50")
		self.assertEqual(fmt(0), "$ 0")

		words = facturacion._amount_in_words_es
		self.assertEqual(words(1), "UN PESO M/CTE")
		self.assertEqual(words(21), "VEINTIÚN PESOS M/CTE")
		self.assertEqual(words(11500), "ONCE MIL QUINIENTOS PESOS M/CTE")
		self.assertEqual(words(21000), "VEINTIÚN MIL PESOS M/CTE")
		self.assertEqual(words(1000000), "UN MILLÓN DE PESOS M/CTE")
		self.assertEqual(words(31000000), "TREINTA Y UN MILLONES DE PESOS M/CTE")
		self.assertEqual(words(101), "CIENTO UN PESOS M/CTE")

	def test_11d_issuer_config_only_logo_file_pending(self):
		"""Datos reales entregados por el negocio; la ruta del logo está
		configurada, pero mientras el PNG no exista en disco es lo único
		faltante -- nunca un logo inventado ni reutilizado."""
		self.assertEqual(invoice_issuers.INVOICE_ISSUERS, ("integrandoMAS", "ecoluminar"))
		for issuer in invoice_issuers.INVOICE_ISSUERS:
			self.assertTrue(invoice_issuers.ISSUER_CONFIG[issuer]["logo"], issuer)
			expected = [] if invoice_issuers.logo_available(issuer) else ["logo"]
			self.assertEqual(invoice_issuers.missing_issuer_fields(issuer), expected, issuer)

	def test_11e_normalized_bank_data_matches_the_literal_payment_text(self):
		"""bank_* y payment_instruction describen la misma cuenta -- y cada
		emisor solo contiene la suya."""
		configs = invoice_issuers.ISSUER_CONFIG
		for issuer, other in (("integrandoMAS", "ecoluminar"), ("ecoluminar", "integrandoMAS")):
			config, other_config = configs[issuer], configs[other]
			self.assertIn(config["bank_account_number"], config["payment_instruction"])
			self.assertIn(config["bank_account_holder"], config["payment_instruction"])
			self.assertIn(config["bank_name"], config["payment_instruction"])
			self.assertEqual(config["bank_account_type"], "Cuenta de ahorro")
			for value in config.values():
				text = " ".join(value) if isinstance(value, tuple) else str(value)
				self.assertNotIn(other_config["bank_account_number"], text)
				self.assertNotIn(other_config["bank_account_holder"], text)
				self.assertNotIn(other_config["nit"], text)

	# -- 12. mismas validaciones en VER y DESCARGAR ---------------------------------
	# (cubierto por _all_pdf_paths() en 04b/05/07/07b) -- aquí el caso feliz de ambos.

	def test_12_view_and_download_succeed_for_the_same_eligible_document(self):
		_, pl_name = self._with_issuer()
		url = self._view_url(pl_name)
		self.assertIn("doctype=Pick%20List", url)
		self.assertIn(f"name={pl_name}", url)
		self.assertEqual(len(self._download(pl_name)), 1)

	# -- 13/14. sin efectos sobre otros Print Formats -------------------------------

	def test_13_quotation_pdf_hook_untouched(self):
		quotation_before_print = frappe.get_hooks("doc_events")["Quotation"]["before_print"]
		quotation_hook = "fabergray_erp.api.cotizaciones.prepare_and_guard_quotation_pdf"
		invoice_hook = "fabergray_erp.api.facturacion.prepare_and_guard_invoice_pdf"
		self.assertIn(quotation_hook, quotation_before_print)
		self.assertNotIn(invoice_hook, quotation_before_print)
		self.assertEqual(
			frappe.db.get_value("Print Format", cotizaciones.PDF_PRINT_FORMAT_NAME, "doc_type"), "Quotation"
		)

		# el hook de factura ignora por completo el formato de Cotización
		frappe.form_dict.format = cotizaciones.PDF_PRINT_FORMAT_NAME
		try:
			sentinel = frappe._dict(doctype="Pick List", name="x")
			self.assertIsNone(facturacion.prepare_and_guard_invoice_pdf(sentinel))
			self.assertFalse(hasattr(sentinel, "fg_pdf_issuer") and sentinel.fg_pdf_issuer)
		finally:
			frappe.form_dict.pop("format", None)

	def test_14_other_pick_list_print_formats_never_blocked(self):
		"""Un Pick List NO facturado, sin emisor, impreso por Bodega con otro
		formato: el before_print de factura no debe bloquear ni calcular nada."""
		other_format = "FG2523 Internal Pick List Format"
		if not frappe.db.exists("Print Format", other_format):
			frappe.get_doc(
				{
					"doctype": "Print Format",
					"name": other_format,
					"doc_type": "Pick List",
					"module": "Fabrigray ERP",
					"standard": "No",
					"custom_format": 1,
					"print_format_type": "Jinja",
					"html": "<div>{{ doc.name }}|{{ doc.fg_pdf_issuer or 'NO-CONTEXT' }}</div>",
				}
			).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Print Format", other_format, force=True))

		_, pl_name = self._picked_pick_list()
		html = self._html(pl_name, user=self.bodega_user, print_format=other_format)
		self.assertIn(f"{pl_name}|NO-CONTEXT", html)


class TestFacturacionInvoicePdfIssuerData(TestFacturacionInvoicePdf):
	"""Datos reales de cada emisor en su PDF, y NUNCA los del otro -- en
	particular la cuenta bancaria --, sus logos y la línea de imprenta. Reutiliza los fixtures/helpers de la
	clase base; `test_*` heredados se desactivan para no correrlos dos veces."""

	ISSUER_DATA = {
		"integrandoMAS": {
			"display_name": "INTEGRANDO MAS BGA",
			"nit": "1005281903-1",
			"address": "Calle 46 # 22-28 Oficina 2",
			"email": "integrandomas@hotmail.com",
			"bank_account_number": "79600011630",
			"bank_account_holder": "NICOLAS FELIPE HERRERA MATEUS",
		},
		"ecoluminar": {
			"display_name": "ECOLUMINAR",
			"nit": "1005109961-2",
			"address": "Calle 46 Número 22-28 Oficina 3",
			"email": "ecodluminar@outlook.com",
			"bank_account_number": "09036542699",
			"bank_account_holder": "JUAN ANDRES LOZANO GARCIA",
		},
	}

	def _issuer_html(self, issuer):
		_, pl_name = self._with_issuer(issuer)
		return pl_name, self._html(pl_name)

	def _assert_only_own_data(self, issuer, other):
		pl_name, html = self._issuer_html(issuer)
		own, foreign = self.ISSUER_DATA[issuer], self.ISSUER_DATA[other]
		for key, value in own.items():
			self.assertIn(value, html, f"{issuer}: falta {key}")
		self.assertEqual(html.count(own["bank_account_number"]), 1, "cuenta propia exactamente una vez")
		for key in ("nit", "email", "address", "bank_account_number", "bank_account_holder"):
			self.assertNotIn(foreign[key], html, f"{issuer}: aparece {key} de {other}")
		# encabezado: régimen, ciudad y teléfonos normalizados
		self.assertIn("Régimen Simplificado", html)
		self.assertIn("Girón", html)
		self.assertIn("Tel. 3118814375 - 321 5351749", html)
		return pl_name, html

	def test_15_integrandomas_pdf_has_only_its_own_data(self):
		_, html = self._assert_only_own_data("integrandoMAS", "ecoluminar")
		self.assertIn("Adeudo a INTEGRANDO MAS BGA el monto neto indicado", html)
		self.assertNotIn("ECODLUMINAR", html)

	def test_16_ecoluminar_pdf_has_only_its_own_data(self):
		_, html = self._assert_only_own_data("ecoluminar", "integrandoMAS")
		# texto legal literal, con la grafía entregada por el negocio
		self.assertIn("Adeudo a ECODLUMINAR el monto neto indicado", html)
		self.assertNotIn("INTEGRANDO MAS BGA", html)

	def test_17_both_still_unnumbered_and_marked_draft(self):
		for issuer in ("integrandoMAS", "ecoluminar"):
			pl_name, html = self._issuer_html(issuer)
			self.assertIn("No. SIN NUMERAR", html)
			self.assertIn(f"Ref. interna: {pl_name}", html)
			self.assertIn("BORRADOR — NO VÁLIDO COMO FACTURA DE VENTA", html)
			self.assertIn("sin numeración de factura autorizada", html)

	def test_18_switching_issuer_swaps_all_data_and_persists(self):
		_, pl_name = self._with_issuer("integrandoMAS")
		self.assertIn("79600011630", self._html(pl_name))
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "ecoluminar")
		html = self._html(pl_name)
		self.assertIn("09036542699", html)
		self.assertNotIn("79600011630", html)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")
		# generar el PDF nunca cambia el emisor guardado
		self._html(pl_name)
		self._download(pl_name)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")

	def test_19_quotation_print_format_carries_no_issuer_data(self):
		html = frappe.db.get_value("Print Format", cotizaciones.PDF_PRINT_FORMAT_NAME, "html")
		self.assertTrue(html)
		for data in self.ISSUER_DATA.values():
			for key in ("nit", "email", "bank_account_number", "bank_account_holder"):
				value = data[key]
				self.assertNotIn(value, html)
		self.assertNotIn("fg_pdf_issuer", html)

	# -- logos --------------------------------------------------------------

	def test_20_each_issuer_resolves_its_own_logo_path(self):
		expected = {
			"integrandoMAS": "/assets/fabergray_erp/images/invoice/integrandomas-logo.png",
			"ecoluminar": "/assets/fabergray_erp/images/invoice/ecoluminar-logo.png",
		}
		public_dir = os.path.join(frappe.get_app_path("fabergray_erp"), "public", "images", "invoice")
		for issuer, url in expected.items():
			self.assertEqual(invoice_issuers.ISSUER_CONFIG[issuer]["logo"], url)
			self.assertEqual(invoice_issuers.get_issuer_config(issuer)["logo_configured"], url)
			self.assertEqual(
				invoice_issuers.logo_file_path(url), os.path.join(public_dir, os.path.basename(url))
			)
		self.assertNotEqual(expected["integrandoMAS"], expected["ecoluminar"])

	def test_21_logo_path_never_escapes_the_invoice_assets_folder(self):
		for bad in (
			None,
			"",
			"https://example.com/logo.png",
			"/assets/fabergray_erp/images/invoice/../../css/fg_shell.css",
			"/assets/frappe/images/frappe-logo.png",
			"/files/logo.png",
		):
			self.assertIsNone(invoice_issuers.logo_file_path(bad), bad)

	def test_22_missing_logo_file_does_not_break_generation(self):
		"""Simula que el PNG no está en disco (los reales ya están versionados)."""
		from unittest.mock import patch

		for issuer in ("integrandoMAS", "ecoluminar"):
			with patch.object(invoice_issuers, "logo_available", return_value=False):
				self.assertIsNone(invoice_issuers.get_issuer_config(issuer)["logo"])
				self.assertIn("logo", invoice_issuers.missing_issuer_fields(issuer))
				_, html = self._issuer_html(issuer)
			self.assertIn('<div class="fg-inv-logo-placeholder">', html)
			self.assertNotIn('class="fg-inv-logo"', html)
			self.assertIn("faltan datos del emisor: logo", html)

	def test_23_existing_logo_file_is_used_without_touching_the_print_format(self):
		"""Simula que el PNG ya fue copiado (sin crear ningún archivo)."""
		from unittest.mock import patch

		for issuer, other in (("integrandoMAS", "ecoluminar"), ("ecoluminar", "integrandoMAS")):
			own_url = invoice_issuers.ISSUER_CONFIG[issuer]["logo"]
			other_url = invoice_issuers.ISSUER_CONFIG[other]["logo"]
			with patch.object(invoice_issuers, "logo_available", return_value=True):
				self.assertEqual(invoice_issuers.missing_issuer_fields(issuer), [])
				_, html = self._issuer_html(issuer)
			self.assertIn(f'src="{frappe.utils.get_url(own_url)}"', html)
			self.assertNotIn(other_url, html)
			self.assertNotIn('<div class="fg-inv-logo-placeholder">', html)
			# el logo no hace válida la factura: sigue sin numeración
			self.assertIn("BORRADOR — NO VÁLIDO COMO FACTURA DE VENTA", html)
			self.assertIn("No. SIN NUMERAR", html)
			self.assertNotIn("faltan datos del emisor", html)

	# -- imprenta -----------------------------------------------------------

	PRINTER_LINE = "Impreso por: LITO CARIBE Nit: 28410436-9 Tel (7) 6336124"

	def test_24_both_pdfs_end_with_the_shared_print_provider_line(self):
		for issuer in ("integrandoMAS", "ecoluminar"):
			pl_name, html = self._issuer_html(issuer)
			for value in ("LITO CARIBE", "28410436-9", "(7) 6336124"):
				self.assertIn(value, html)
			self.assertEqual(html.count(self.PRINTER_LINE), 1, issuer)
			# orden: firmas -> texto legal -> pago -> imprenta
			positions = [
				html.index('class="fg-inv-signatures"'),
				html.index('class="fg-inv-legal-text"'),
				html.index('class="fg-inv-legal-payment"'),
				html.index(self.PRINTER_LINE),
			]
			self.assertEqual(positions, sorted(positions), issuer)
			self.assertIn("BORRADOR — NO VÁLIDO COMO FACTURA DE VENTA", html)
			self.assertIn("No. SIN NUMERAR", html)
			self.assertIn(f"Ref. interna: {pl_name}", html)

	def test_25_print_provider_is_configured_once_and_outside_the_issuers(self):
		self.assertEqual(
			invoice_issuers.INVOICE_PRINT_PROVIDER,
			{"name": "LITO CARIBE", "nit": "28410436-9", "phone": "(7) 6336124"},
		)
		for config in invoice_issuers.ISSUER_CONFIG.values():
			for value in config.values():
				self.assertNotIn("LITO CARIBE", str(value))
		template = frappe.db.get_value("Print Format", INVOICE_FORMAT, "html")
		self.assertNotIn("LITO CARIBE", template)  # se arma desde la config, no hardcodeado
		quotation = frappe.db.get_value("Print Format", cotizaciones.PDF_PRINT_FORMAT_NAME, "html")
		self.assertNotIn("LITO CARIBE", quotation)
		self.assertNotIn("fg_pdf_print_provider", quotation)


# Solo los test_15+ propios: los heredados ya corren en la clase base.
for _name in [n for n in dir(TestFacturacionInvoicePdf) if n.startswith("test_")]:
	setattr(TestFacturacionInvoicePdfIssuerData, _name, None)


class TestFacturacionInvoicePdfUiContract(IntegrationTestCase):
	"""facturacion.js leído como texto (no hay runner JS en esta app)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		with open(_FACTURACION_JS_PATH, encoding="utf-8") as f:
			cls.source = f.read()

	def test_view_pdf_opens_blank_tab_then_calls_the_secure_endpoint(self):
		body = _function_body(self.source, "open_fabrigray_invoice_pdf")
		self.assertIn('window.open("about:blank")', body)
		self.assertIn("fabergray_erp.api.facturacion.get_invoice_pdf_view_url", body)
		self.assertIn("tab.close()", body)
		self.assertNotIn("printview", body)
		self.assertLess(body.index('window.open("about:blank")'), body.index("frappe"))

	def test_download_pdf_uses_only_the_controlled_endpoint(self):
		body = _function_body(self.source, "download_fabrigray_invoice_pdf")
		self.assertIn("fabergray_erp.api.facturacion.download_invoice_pdf", body)
		self.assertNotIn("format", body)
		self.assertNotIn("printview", body)

	def test_issuer_selector_and_required_message(self):
		self.assertIn('const INVOICE_ISSUERS = ["integrandoMAS", "ecoluminar"];', self.source)
		self.assertIn("EMPRESA EMISORA", self.source)
		self.assertIn("Selecciona la empresa emisora de la factura.", self.source)
		self.assertIn('this.call("set_invoice_issuer"', self.source)
