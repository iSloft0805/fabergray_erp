# -*- coding: utf-8 -*-
"""Hotfix 25.26.1 -- "OBSERVACIONES DEL PEDIDO" en Facturación.

La observación que la Vendedora escribe en Ventas vive en UN solo lugar,
`Sales Order.fg_observations`. Facturación (modal REVISAR PEDIDO, vía
get_invoicing_detail()) y el PDF comercial (before_print ->
`fg_pdf_order_observations`, solo contexto de impresión) únicamente la LEEN.
Sin campo nuevo, sin snapshot en Pick List: la inmutabilidad del texto de
un pedido ya facturado la garantizan las reglas existentes, que esta suite
también fija (TestObservationImmutabilityGuard).

Mismo andamiaje que test_facturacion_invoice_pdf.py: Pick List real
(Bodega alista -> Facturación revisa y marca Facturado) y
`frappe.get_print(..., as_pdf=False)` para el HTML del Print Format.
"""

import ast
import inspect
import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega, facturacion, ventas
from fabergray_erp.fulfillment.modification_service import modification_blockers_for
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

INVOICE_FORMAT = facturacion.INVOICE_PDF_PRINT_FORMAT_NAME
HEADING = "OBSERVACIONES DEL PEDIDO"

_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)
_FACTURACION_CSS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.css"
)

MULTILINE = "Cliente solicita entregar después de las 2:00 p. m.\nEnviar 2 galones sin fragancia.\nConfirmar con recepción antes de descargar."
MALICIOUS = '<script>alert("x")</script><img src=x onerror="alert(1)"> {{ 7*7 }} {{ 7*7777 }} {% if 1 %}JINJA{% endif %}'
SPECIAL = "Ñandú & Cía. — «urgente» 50% \"comillas\" 'simples' áéíóú"


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_cotizaciones_billing_review.py's own."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


class _ObservationsWorld(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.sfx = sfx
		cls.wh = cls.world.warehouse(f"FG25261 {sfx} WH")
		cls.item_a = cls.world.item(f"FG25261-{sfx}-A")
		cls.item_b = cls.world.item(f"FG25261-{sfx}-B")
		cls.customer = cls.world.customer(f"FG25261 {sfx} Cliente")
		frappe.db.set_value("Customer", cls.customer.name, "tax_id", "900000001-1")
		cls.world.stock_up_real(cls.item_a.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up_real(cls.item_b.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user(f"fg25261-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fg25261-{sfx}-facturacion@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user(f"fg25261-{sfx}-vendedora@example.com", ["Vendedora"])

	# -- helpers --------------------------------------------------------------

	def _sales_order(self, observations=None):
		"""Same shape as fx.TestWorld.multi_item_sales_order(), plus the one
		thing it cannot set: fg_observations, which has no allow_on_submit
		and therefore must be on the document BEFORE submit()."""
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": self.wh.name,
				"items": [
					{"item_code": self.item_a.name, "warehouse": self.wh.name, "qty": 2, "rate": 5750, "delivery_date": delivery_date},
					{"item_code": self.item_b.name, "warehouse": self.wh.name, "qty": 3, "rate": 1200, "delivery_date": delivery_date},
				],
			}
		)
		if observations is not None:
			doc.fg_observations = observations
		doc.insert()
		with fx.without_sales_order_hook():
			doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		self.world.track_existing("Sales Order", doc.name)
		return doc

	def _picked_pick_list(self, observations=None):
		so = self._sales_order(observations)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, pl.name

	def _invoiced_pick_list(self, observations=None, issuer="integrandoMAS"):
		so, pl_name = self._picked_pick_list(observations)
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl_name)
			facturacion.set_invoice_issuer(pl_name, issuer)
		return so, pl_name

	def _detail(self, pl_name):
		with fx.as_user(self.facturacion_user):
			return facturacion.get_invoicing_detail(pl_name)

	def _html(self, pl_name):
		with fx.as_user(self.facturacion_user):
			return frappe.get_print("Pick List", pl_name, print_format=INVOICE_FORMAT, as_pdf=False)


class TestVentasPersistsObservation(_ObservationsWorld):
	"""1-2: la observación escrita en Ventas queda en Sales Order.fg_observations."""

	def test_01_ventas_payload_is_persisted_on_sales_order(self):
		item = self.world.item(f"FG25261-{self.sfx}-C", default_warehouse=self.wh.name)
		with fx.as_user(self.vendedora_user):
			result = ventas.create_draft_sales_order(
				self.customer.name, [{"item_code": item.name, "qty": 1}], observations=MULTILINE
			)
		name = result["name"] if isinstance(result, dict) else result
		self.world.track_existing("Sales Order", name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "fg_observations"), MULTILINE)

	def test_02_observation_belongs_to_its_own_order(self):
		so_a = self._sales_order("Comentario A")
		so_b = self._sales_order("Comentario B")
		self.assertEqual(frappe.db.get_value("Sales Order", so_a.name, "fg_observations"), "Comentario A")
		self.assertEqual(frappe.db.get_value("Sales Order", so_b.name, "fg_observations"), "Comentario B")


class TestFacturacionScreenObservation(_ObservationsWorld):
	"""3-8: get_invoicing_detail() (modal REVISAR PEDIDO)."""

	def test_03_detail_returns_the_order_observation(self):
		_, pl_name = self._picked_pick_list(MULTILINE)
		self.assertEqual(self._detail(pl_name)["order_observations"], MULTILINE)

	def test_04_no_observation_returns_empty_string(self):
		_, pl_name = self._picked_pick_list(None)
		self.assertEqual(self._detail(pl_name)["order_observations"], "")

	def test_04b_whitespace_only_counts_as_no_observation(self):
		_, pl_name = self._picked_pick_list("   \n\t  ")
		self.assertEqual(self._detail(pl_name)["order_observations"], "")

	def test_05_multiline_is_preserved(self):
		_, pl_name = self._picked_pick_list(MULTILINE)
		self.assertEqual(self._detail(pl_name)["order_observations"].split("\n"), MULTILINE.split("\n"))

	def test_06_long_text_is_returned_whole(self):
		long_text = "Instrucción de entrega muy detallada. " * 120
		_, pl_name = self._picked_pick_list(long_text)
		self.assertEqual(self._detail(pl_name)["order_observations"], long_text.strip())

	def test_07_order_a_never_shows_order_b_observation(self):
		_, pl_a = self._picked_pick_list("Solo para A")
		_, pl_b = self._picked_pick_list("Solo para B")
		self.assertEqual(self._detail(pl_a)["order_observations"], "Solo para A")
		self.assertEqual(self._detail(pl_b)["order_observations"], "Solo para B")

	def test_08_facturacion_cannot_write_the_observation(self):
		self.assertFalse(frappe.has_permission("Sales Order", "write", user=self.facturacion_user))
		# No facturacion.py function assigns fg_observations anywhere.
		tree = ast.parse(inspect.getsource(facturacion))
		for node in ast.walk(tree):
			if isinstance(node, ast.Assign):
				for target in node.targets:
					if isinstance(target, ast.Attribute):
						self.assertNotEqual(target.attr, "fg_observations")
		source = inspect.getsource(facturacion)
		self.assertNotRegex(source, r"set_value\([^)]*fg_observations")
		self.assertNotRegex(source, r"db_set\([^)]*fg_observations")


class TestInvoicePdfObservation(_ObservationsWorld):
	"""9-19: PDF comercial."""

	def test_09_pdf_shows_observation_exactly_once(self):
		_, pl_name = self._invoiced_pick_list(MULTILINE)
		html = self._html(pl_name)
		self.assertEqual(html.count(HEADING), 1)
		self.assertIn("Enviar 2 galones sin fragancia.", html)

	def test_10_pdf_without_observation_has_no_section(self):
		_, pl_name = self._invoiced_pick_list(None)
		html = self._html(pl_name)
		self.assertNotIn(HEADING, html)
		self.assertNotIn("Nota / Observaciones", html)
		self.assertNotIn('class="fg-inv-notes-cell"', html)
		# The rest of that row (seller / purchases) is untouched.
		self.assertIn("Ejecutivo Ventas:", html)

	def test_10b_pdf_whitespace_only_observation_has_no_section(self):
		_, pl_name = self._invoiced_pick_list("  \n  ")
		self.assertNotIn(HEADING, self._html(pl_name))

	def test_11_both_issuers_show_the_same_observation(self):
		_, pl_name = self._invoiced_pick_list(MULTILINE, issuer="integrandoMAS")
		html_integrando = self._html(pl_name)
		with fx.as_user(self.facturacion_user):
			facturacion.set_invoice_issuer(pl_name, "ecoluminar")
		html_eco = self._html(pl_name)
		for html in (html_integrando, html_eco):
			self.assertEqual(html.count(HEADING), 1)
			self.assertIn("Confirmar con recepción antes de descargar.", html)
		self.assertIn("ECOLUMINAR", html_eco)

	def test_12_line_breaks_preserved_in_pdf(self):
		_, pl_name = self._invoiced_pick_list(MULTILINE)
		html = self._html(pl_name)
		self.assertIn(
			"Cliente solicita entregar después de las 2:00 p. m.\nEnviar 2 galones sin fragancia.",
			html,
		)

	def test_13_special_characters(self):
		_, pl_name = self._invoiced_pick_list(SPECIAL)
		html = self._html(pl_name)
		self.assertIn("Ñandú &amp; Cía. — «urgente» 50%", html)
		self.assertIn("áéíóú", html)

	def test_14_html_is_escaped_and_jinja_not_evaluated(self):
		"""Layer 1 (Frappe, on save): Small Text is sanitized -- <script> and
		event handlers are stripped before fg_observations is stored.
		Layer 2 (this hotfix): the template escapes whatever is stored."""
		so, pl_name = self._invoiced_pick_list(MALICIOUS)
		stored = frappe.db.get_value("Sales Order", so.name, "fg_observations")
		self.assertNotIn("<script", stored)
		self.assertNotIn("onerror", stored)
		html = self._html(pl_name)
		self.assertNotIn("<script>alert", html)
		self.assertNotIn('<img src="x">', html)
		self.assertIn("&lt;img src=", html)  # shown as text, never as a tag
		self.assertIn("{{ 7*7 }}", html)
		self.assertIn("{{ 7*7777 }}", html)
		self.assertNotIn("54439", html)
		self.assertIn("{% if 1 %}JINJA{% endif %}", html)

	def test_14b_template_escapes_even_unsanitized_stored_html(self):
		"""Defense in depth: force raw HTML into the stored value (db-level,
		test setup only -- bypasses Frappe's save-time sanitizer) and prove
		the Print Format itself still renders it as inert text."""
		so, pl_name = self._invoiced_pick_list("placeholder")
		frappe.db.set_value("Sales Order", so.name, "fg_observations", MALICIOUS, update_modified=False)
		html = self._html(pl_name)
		self.assertNotIn("<script>alert", html)
		self.assertNotIn('<img src=x onerror="alert(1)">', html)
		self.assertIn("&lt;script&gt;alert(", html)
		self.assertIn("{{ 7*7 }}", html)
		self.assertNotIn("54439", html)
		self.assertEqual(html.count(HEADING), 1)

	def test_15_observation_is_print_context_only_never_saved(self):
		_, pl_name = self._invoiced_pick_list(MULTILINE)
		self._html(pl_name)
		self.assertFalse(frappe.get_meta("Pick List").get_field("fg_pdf_order_observations"))
		pl = frappe.get_doc("Pick List", pl_name)
		self.assertFalse(pl.get("fg_pdf_order_observations"))

	def test_16_observation_does_not_change_prices_totals_issuer_or_status(self):
		so, pl_name = self._invoiced_pick_list(MULTILINE, issuer="ecoluminar")
		pl = frappe.get_doc("Pick List", pl_name)
		so_doc = frappe.get_doc("Sales Order", so.name)

		with fx.as_user(self.facturacion_user):
			facturacion._build_invoice_pdf_context(pl, so_doc)
			with_obs = (pl.fg_pdf_lines, pl.fg_pdf_totals, pl.fg_pdf_issuer)
			so_doc.fg_observations = ""  # in memory only
			facturacion._build_invoice_pdf_context(pl, so_doc)
			without_obs = (pl.fg_pdf_lines, pl.fg_pdf_totals, pl.fg_pdf_issuer)
		self.assertEqual(with_obs, without_obs)
		self.assertEqual(pl.fg_pdf_order_observations, "")

		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "grand_total"), so.grand_total)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoice_issuer"), "ecoluminar")
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoicing_status"), "Facturado")

	def test_17_pdf_totals_identical_with_and_without_observation(self):
		_, pl_with = self._invoiced_pick_list(MULTILINE)
		_, pl_without = self._invoiced_pick_list(None)
		totals = []
		for pl_name in (pl_with, pl_without):
			pl = frappe.get_doc("Pick List", pl_name)
			with fx.as_user(self.facturacion_user):
				so = facturacion._invoice_sales_order(pl)
				facturacion._build_invoice_pdf_context(pl, so)
			totals.append((pl.fg_pdf_totals, [(l["qty_display"], l["rate_display"], l["amount_display"]) for l in pl.fg_pdf_lines]))
		self.assertEqual(totals[0], totals[1])

	def test_18_historical_pick_list_without_observation_still_prints(self):
		"""Every order invoiced before this hotfix that never had an
		observation behaves exactly as before, minus the old empty box."""
		_, pl_name = self._invoiced_pick_list(None)
		html = self._html(pl_name)
		self.assertIn("FACTURA DE VENTA", html)
		self.assertIn("TOTAL:", html)
		self.assertNotIn(HEADING, html)

	def test_19_mark_as_invoiced_flow_unchanged(self):
		_, pl_name = self._invoiced_pick_list(MULTILINE)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoicing_status"), "Facturado")
		self.assertTrue(frappe.db.get_value("Pick List", pl_name, "fg_invoiced_on"))
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoiced_by"), self.facturacion_user)


class TestObservationImmutabilityGuard(_ObservationsWorld):
	"""Why no snapshot is needed -- if any of these ever fails, an invoiced
	Pick List's observation could change after the fact, and the snapshot
	decision of Hotfix 25.26.1 must be revisited."""

	def test_fg_observations_cannot_be_edited_after_submit(self):
		self.assertEqual(
			frappe.db.get_value("Custom Field", {"dt": "Sales Order", "fieldname": "fg_observations"}, "allow_on_submit"),
			0,
		)
		self.assertFalse(
			frappe.get_all(
				"Property Setter",
				filters={"doc_type": "Sales Order", "field_name": "fg_observations", "property": "allow_on_submit"},
			)
		)

	def test_submitted_pick_list_blocks_order_modification(self):
		so, _ = self._invoiced_pick_list(MULTILINE)
		self.assertIn("pick_list_submitted", modification_blockers_for(so.name))

	def test_pdf_requires_the_sales_order_to_stay_submitted(self):
		source = inspect.getsource(facturacion._invoice_sales_order)
		self.assertIn("if so.docstatus != 1:", source)


class TestPermissionsUnchanged(_ObservationsWorld):
	"""20: no permission changed for this hotfix."""

	def test_20_facturacion_sales_order_permissions_unchanged(self):
		perms = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": "Sales Order", "role": "Facturación", "permlevel": 0},
			["read", "write", "create", "submit", "cancel"],
			as_dict=True,
		)
		self.assertEqual(
			(perms.read, perms.write, perms.create, perms.submit, perms.cancel), (1, 0, 0, 0, 0)
		)

	def test_20b_other_roles_still_cannot_read_invoicing_detail(self):
		_, pl_name = self._picked_pick_list(MULTILINE)
		with fx.as_user(self.vendedora_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_invoicing_detail(pl_name)


class TestObservationsUiContract(IntegrationTestCase):
	"""Static checks on facturacion.js/.css (no JS runner in this app)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_FACTURACION_JS_PATH)
		cls.css = _read(_FACTURACION_CSS_PATH)
		cls.body = _method_body(cls.js, "render_review_dialog_body")

	def test_ui_renders_heading_exactly_once(self):
		self.assertEqual(self.js.count('__("OBSERVACIONES DEL PEDIDO")'), 1)
		self.assertIn('__("OBSERVACIONES DEL PEDIDO")', self.body)
		self.assertEqual(self.body.count("${observations_html}"), 1)

	def test_ui_hidden_without_observation(self):
		self.assertIn('const observations = (d.order_observations || "").trim();', self.body)
		self.assertRegex(self.body, r"const observations_html = observations\s*\?")
		self.assertRegex(self.body, r':\s*"";\s*\n')

	def test_ui_text_is_escaped(self):
		self.assertIn("frappe.utils.escape_html(observations)", self.body)

	def test_ui_is_read_only(self):
		block = self.body.split("const observations_html")[1].split("const items_html")[0]
		for forbidden in ("<textarea", "<input", "contenteditable"):
			self.assertNotIn(forbidden, block)
		self.assertNotRegex(self.js, r"fg_observations\s*[:=]")  # never sent/assigned client-side
		self.assertNotIn('"fg_observations"', self.js)

	def test_ui_wraps_long_text_without_overflow(self):
		self.assertRegex(self.css, r"\.fg-fact-review-observations-text \{[^}]*white-space: pre-wrap")
		self.assertRegex(self.css, r"\.fg-fact-review-observations-text \{[^}]*overflow-wrap: anywhere")
		self.assertRegex(self.css, r"\.fg-fact-review-observations \{[^}]*min-width: 0")

	def test_print_format_section_is_conditional_and_escaped(self):
		html = frappe.db.get_value("Print Format", INVOICE_FORMAT, "html")
		self.assertIn("{% if doc.fg_pdf_order_observations %}", html)
		self.assertIn("{{ doc.fg_pdf_order_observations | e }}", html)
		self.assertNotIn("Nota / Observaciones", html)
		self.assertNotIn("fg-inv-notes-space", html)
		css = frappe.db.get_value("Print Format", INVOICE_FORMAT, "css")
		self.assertRegex(css, r"\.fg-inv-notes-text \{[^}]*white-space: pre-wrap")
