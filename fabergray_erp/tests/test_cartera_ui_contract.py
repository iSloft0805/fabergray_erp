# -*- coding: utf-8 -*-
"""Fase 27.2 -- UI contract for Page Cartera (page/cartera cartera.js/.css).

Same approach as the Recorridos UI contracts: source checks, plus the pure
render/format helpers EXECUTED with node (cards, detail, due labels, money,
dates, proof data URL, sync summary), fed with hostile strings to prove
every server text is escaped. Node-backed tests skip if node is missing."""

import json
import os
import re
import shutil
import subprocess
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import cartera as cartera_api

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cartera")
_NODE = shutil.which("node")

XSS = "<img src=x onerror=alert(1)><script>alert(2)</script>"

# Minimal browser/Frappe stand-ins so cartera.js can be evaluated in node.
# escape_html mirrors frappe.utils.escape_html (frappe/public/js/frappe/utils/utils.js).
_NODE_PRELUDE = r"""
var __ = function (s, args) {
	return String(s).replace(/\{(\d+)\}/g, function (m, i) { return args && args[i] !== undefined ? args[i] : m; });
};
var frappe = {
	pages: { cartera: {} },
	provide: function () {},
	session: { user: "u@example.com", user_fullname: "U" },
	user: { roles: ["Cartera"], has_role: function (r) { return this.roles.indexOf(r) !== -1; } },
	utils: {
		escape_html: function (txt) {
			var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;" };
			return String(txt).replace(/[&<>"'`=]/g, function (c) { return map[c]; });
		},
	},
	ui: {},
};
var fabergray_erp = {};
var window = { scrollTo: function () {} };
"""


def _read(name):
	with open(os.path.join(_PAGE_DIR, name), encoding="utf-8") as f:
		return f.read()


def _code(source):
	"""Source without // line comments."""
	return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def _css_rule(css, selector):
	match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	assert match, f"missing CSS rule {selector}"
	return match.group(1)


def _item(**overrides):
	item = {
		"name": "CART-2026-00001",
		"customer": "CUST-1",
		"customer_name": "Ferretería Uno",
		"customer_commercial_name": "La Uno",
		"commercial_name": "PEDIDO-12",
		"sales_order": "PEDIDO-12",
		"pick_list": "STO-PICK-1",
		"recorrido": "REC-1",
		"invoice_amount": 1250000,
		"paid_amount": 0,
		"outstanding_amount": 1250000,
		"currency": "COP",
		"amount_source": "Factura comercial",
		"amount_available": True,
		"status": "Pendiente",
		"payment_verification": None,
		"credit_days": 0,
		"due_date": None,
		"delivery_date": "2026-09-22",
		"delivered_on": "2026-09-22 10:15:00.000000",
		"driver_payment_status": "Pendiente por Pago",
		"has_delivery_issues": 0,
		"bucket": "pendiente",
		"days_to_due": None,
		"days_since_delivery": 3,
		"por_confirmar": False,
	}
	item.update(overrides)
	return item


class TestCarteraUIContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read("cartera.js")
		cls.css = _read("cartera.css")
		cls.code = _code(cls.js)

	def _node(self, body):
		if not _NODE:
			raise unittest.SkipTest("node not available")
		script = _NODE_PRELUDE + self.js + "\n;(function(){\n" + body + "\n})();"
		out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
		return json.loads(out.stdout)

	def _render_card(self, item):
		return self._node(f"console.log(JSON.stringify(render_obligation_card({json.dumps(item)})));")

	def _render_detail(self, detail):
		return self._node(f"console.log(JSON.stringify(render_detail_html({json.dumps(detail)})));")

	# =====================================================================
	# Page / roles / structure
	# =====================================================================

	def test_page_definition_and_roles(self):
		page = json.loads(_read("cartera.json"))
		self.assertEqual((page["name"], page["page_name"], page["module"]), ("cartera", "cartera", "Fabrigray ERP"))
		self.assertEqual({r["role"] for r in page["roles"]}, {"Cartera", "System Manager"})
		self.assertTrue(os.path.exists(os.path.join(_PAGE_DIR, "__init__.py")))
		self.assertIn('frappe.pages["cartera"].on_page_load', self.js)
		self.assertIn('class="fg-shell fg-cartera"', self.js)

	def test_five_server_side_kpis(self):
		block = self.code[self.code.index("render_kpis_html() {") : self.code.index("render_sync_button_html() {")]
		for key, label in (
			("cartera_actual", "CARTERA ACTUAL"),
			("por_vencer", "POR VENCER"),
			("vencida", "VENCIDA"),
			("cobrado_mes", "COBRADO EN {0}"),
			("por_confirmar", "PAGOS POR CONFIRMAR"),
		):
			self.assertIn(f'key: "{key}"', block)
			self.assertIn(label, block)
		self.assertEqual(len(re.findall(r'\{ key: "', block)), 5)
		# Values come only from get_dashboard's kpis -- the page never sums
		# the cards it has loaded.
		self.assertIn("this.dashboard.kpis", block)
		self.assertNotIn(".reduce(", self.code)
		self.assertIn("month.label", block)

	def test_search_is_server_side_and_debounced(self):
		self.assertIn("BUSCAR CLIENTE O PEDIDO...", self.js)
		self.assertIn("SEARCH_DEBOUNCE_MS = 300", self.code)
		self.assertIn("clearTimeout(this._search_debounce)", self.code)
		fetch = self.code[self.code.index("fetch_list() {") : self.code.index("render_skeleton() {")]
		for arg in ("filter: this.list_filter", "search: this.list_search", "page: this.list_page", "page_length: PAGE_LENGTH"):
			self.assertIn(arg, fetch)
		self.assertIn('this.call("get_obligations"', fetch)
		# Out-of-order responses are discarded.
		self.assertIn("seq !== this._list_request_seq", fetch)
		self.assertNotIn(".filter((", self.code)  # no client-side filtering of rows

	def test_por_confirmar_comes_from_the_server_flag_only(self):
		helper = self.code[self.code.index("function is_unconfirmed_driver_payment(") :]
		helper = helper[: helper.index("\n}\n")]
		self.assertIn("item.por_confirmar === true", helper)
		self.assertNotIn("payment_verification", helper)
		out = self._node(
			"console.log(JSON.stringify(["
			"is_unconfirmed_driver_payment({status:'Por Validar', payment_verification:'Sin confirmar', por_confirmar:false}),"
			"is_unconfirmed_driver_payment({status:'Pagado', payment_verification:'Sin confirmar', por_confirmar:true}),"
			"is_unconfirmed_driver_payment({status:'Pagado', payment_verification:'Confirmado', por_confirmar:false})]));"
		)
		self.assertEqual(out, [False, True, False])

	def test_chips_match_backend_filters(self):
		keys = re.findall(r'\{ key: "([a-z_]+)", label: __\("([^"]+)"\) \}', self.code[self.code.index("const FILTERS") :])
		self.assertEqual([k for k, _l in keys][:7], list(cartera_api.FILTERS))
		self.assertEqual(
			[l for _k, l in keys][:7], ["TODOS", "PENDIENTES", "CRÉDITO", "VENCIDOS", "PAGADOS", "POR VALIDAR", "POR CONFIRMAR"]
		)
		for key in cartera_api.FILTERS:
			self.assertIn(f"{key}: __(", self.code[self.code.index("const EMPTY_MESSAGES") :])

	def test_pagination_uses_server_total_and_has_more(self):
		block = self.code[self.code.index("render_pagination_html()") : self.code.index("refresh_list()")]
		self.assertIn("this.list.total", block)
		self.assertIn("this.list.has_more", block)
		self.assertIn("PAGE_LENGTH = 20", self.code)

	def test_no_economic_actions_in_27_2(self):
		upper = self.code.upper()  # comments may say what is NOT here yet
		for forbidden in ("REGISTRAR COBRO", "CONFIRMAR PAGO", "RECHAZAR", "NOTA CRÉDITO", "PAYMENT ENTRY", "SALES INVOICE"):
			self.assertNotIn(forbidden, upper)
		methods = set(re.findall(r'this\.call\(\s*"([A-Za-z0-9_]+)"', self.js))
		self.assertEqual(
			methods,
			{"get_dashboard", "get_obligations", "get_obligation_detail", "get_driver_payment_proof", "sync_missing_obligations"},
		)
		self.assertNotIn("frappe.set_route", self.code)  # detail stays inside the Page
		self.assertNotIn("frappe.db.", self.code)
		self.assertNotIn("frappe.client", self.code)

	def test_sync_is_manual_role_gated_and_refreshes(self):
		self.assertIn("SINCRONIZAR", self.js)
		sync = self.code[self.code.index("\tsync() {") : self.code.index("open_detail(name) {")]
		self.assertIn('this.call("sync_missing_obligations"', sync)
		self.assertIn("this.load_all()", sync)
		self.assertIn("can_sync()", sync)
		# Only the button handler triggers it (never on render/load).
		self.assertEqual(len(re.findall(r"this\.sync\(\)", self.code)), 1)
		self.assertIn('.fg-cartera-sync-btn").on("click", () => this.sync())', self.code)
		load_all = self.code[self.code.index("load_all() {") : self.code.index("fetch_dashboard() {")]
		self.assertNotIn("sync", load_all)
		summary = self._node(
			'console.log(JSON.stringify([sync_summary_html({created: 2, already_existing: 40, failed: 1}), (frappe.user.roles=["Vendedora"], can_sync()), (frappe.user.roles=["System Manager"], can_sync())]));'
		)
		self.assertIn("2 nuevas obligaciones", summary[0])
		self.assertIn("40 ya existentes", summary[0])
		self.assertIn("1 errores", summary[0])
		self.assertEqual(summary[1:], [False, True])

	def test_loading_error_and_retry(self):
		for marker in ("render_skeleton()", "render_detail_skeleton()", "fg-cartera-retry-list", "fg-cartera-retry-kpis", "fg-cartera-retry-detail"):
			self.assertIn(marker, self.code)
		self.assertIn("REINTENTAR", self.js)
		# First load fetches KPIs and list in parallel, once.
		load_all = self.code[self.code.index("load_all() {") : self.code.index("fetch_dashboard() {")]
		self.assertIn("Promise.all([this.fetch_dashboard(), this.fetch_list()])", load_all)
		# Filter/search/page changes reload only the list; going back from the
		# detail re-renders cached data without a new request.
		refresh = self.code[self.code.index("refresh_list() {") : self.code.index("bind_dashboard_events() {")]
		self.assertNotIn("fetch_dashboard", refresh)
		back = self.code[self.code.index("back_to_dashboard() {") : self.code.index("render_detail_header_html() {")]
		self.assertIn("this.render_dashboard()", back)
		# Detail loads in its own view with a stale-response guard.
		self.assertIn("seq !== this._detail_request_seq", self.code)

	# =====================================================================
	# Formatting helpers (node)
	# =====================================================================

	def test_money_format(self):
		out = self._node(
			"console.log(JSON.stringify(["
			"format_money(1250000,'COP'), format_money(1250000.5,'COP'), format_money(0,'COP'), format_money(999,'COP'),"
			"format_money(1234567.89), format_money(10,'USD'), format_money(null,'COP'), format_money('abc'),"
			"format_money(-1500,'COP'), format_money(125000000,'COP'), format_money(0.07,'COP')]));"
		)
		self.assertEqual(
			out,
			[
				"$ 1.250.000",
				"$ 1.250.000,50",
				"$ 0",
				"$ 999",
				"$ 1.234.567,89",
				"USD 10",
				"—",
				"—",
				"-$ 1.500",
				"$ 125.000.000",
				"$ 0,07",
			],
		)

	def test_long_money_is_marked_to_shrink_not_break(self):
		out = self._node(
			"console.log(JSON.stringify([money_html(125450000.5,'COP','fg-cartera-money-value'), money_html(1250000,'COP','fg-cartera-kpi-amount')]));"
		)
		self.assertEqual(out[0], '<div class="fg-cartera-money-value is-long">$ 125.450.000,50</div>')
		self.assertEqual(out[1], '<div class="fg-cartera-kpi-amount">$ 1.250.000</div>')
		self.assertIn(".fg-cartera-money-value.is-long", self.css)

	def test_kpi_count_labels(self):
		out = self._node(
			"console.log(JSON.stringify([count_label(1,'obligation'), count_label(4,'obligation'), count_label(0,'payment'),"
			"count_label(1,'payment'), count_label(2,'unconfirmed')]));"
		)
		self.assertEqual(out, ["1 obligación", "4 obligaciones", "0 pagos", "1 pago", "2 por confirmar"])

	def test_date_format(self):
		out = self._node(
			"console.log(JSON.stringify([format_date('2026-09-05'), format_date('2026-09-05 23:59:59.1'),"
			"format_datetime('2026-09-05 07:03:11.123'), format_date(null), format_date('basura')]));"
		)
		self.assertEqual(out, ["05-09-2026", "05-09-2026", "05-09-2026 07:03", "—", "—"])

	def test_due_labels(self):
		out = self._node(
			"console.log(JSON.stringify(["
			f"due_label({json.dumps(_item(credit_days=30, due_date='2026-10-07', days_to_due=12, bucket='por_vencer'))}),"
			f"due_label({json.dumps(_item(credit_days=30, due_date='2026-09-25', days_to_due=0, bucket='por_vencer'))}),"
			f"due_label({json.dumps(_item(credit_days=30, due_date='2026-09-21', days_to_due=-4, bucket='vencido'))}),"
			f"due_label({json.dumps(_item(credit_days=30, due_date='2026-09-24', days_to_due=-1, bucket='vencido'))}),"
			f"due_label({json.dumps(_item(days_since_delivery=3))}),"
			f"due_label({json.dumps(_item(days_since_delivery=0))}),"
			f"due_label({json.dumps(_item(status='Pagado', outstanding_amount=0, paid_amount=1250000, bucket='pagado'))}),"
			"]));"
		)
		self.assertEqual(out[0], {"text": "VENCE EN 12 DÍAS", "mod": "por-vencer"})
		self.assertEqual(out[1], {"text": "VENCE HOY", "mod": "por-vencer"})
		self.assertEqual(out[2], {"text": "VENCIDO HACE 4 DÍAS", "mod": "vencido"})
		self.assertEqual(out[3]["text"], "VENCIDO HACE 1 DÍA")
		self.assertEqual(out[4], {"text": "PENDIENTE DE COBRO", "sub": "3 DÍAS DESDE LA ENTREGA", "mod": "pendiente"})
		self.assertEqual(out[5]["sub"], "ENTREGADO HOY")
		self.assertIsNone(out[6])

	# =====================================================================
	# Cards (node)
	# =====================================================================

	def test_card_credit_upcoming(self):
		html = self._render_card(_item(credit_days=30, due_date="2026-10-07", days_to_due=12, bucket="por_vencer"))
		for text in ("FERRETERÍA UNO".title(), "La Uno", "#PEDIDO-12", "$ 1.250.000", "22-09-2026", "CRÉDITO 30 DÍAS", "VENCE EN 12 DÍAS", "POR VENCER", "VER DETALLE"):
			self.assertIn(text, html)
		self.assertIn('data-name="CART-2026-00001"', html)
		self.assertIn("fg-cartera-card--por-vencer", html)

	def test_card_due_today_and_overdue(self):
		self.assertIn("VENCE HOY", self._render_card(_item(credit_days=30, due_date="2026-09-25", days_to_due=0, bucket="por_vencer")))
		html = self._render_card(_item(credit_days=30, due_date="2026-09-21", days_to_due=-4, bucket="vencido"))
		self.assertIn("VENCIDO HACE 4 DÍAS", html)
		self.assertIn("fg-cartera-card--vencido", html)
		self.assertIn(">VENCIDO<", html)

	def test_card_pending_without_due_date(self):
		html = self._render_card(_item())
		self.assertIn("PENDIENTE DE COBRO", html)
		self.assertIn("3 DÍAS DESDE LA ENTREGA", html)
		self.assertNotIn("VENCE", html)
		self.assertNotIn("CRÉDITO", html)

	def test_card_paid_unconfirmed(self):
		html = self._render_card(
			_item(status="Pagado", paid_amount=1250000, outstanding_amount=0, payment_verification="Sin confirmar", bucket="pagado", driver_payment_status="Pagado", por_confirmar=True)
		)
		self.assertIn(">PAGADO<", html)
		self.assertIn("PAGO REPORTADO · SIN CONFIRMAR", html)
		self.assertIn("fg-cartera-card--pagado", html)
		self.assertIn("$ 0", html)
		self.assertNotIn("PENDIENTE DE COBRO", html)

	def test_card_por_validar_and_unavailable_amount(self):
		html = self._render_card(
			_item(status="Por Validar", payment_verification="Sin confirmar", bucket="por_validar", driver_payment_status="Pagado")
		)
		self.assertIn("POR VALIDAR", html)
		# Reported "Pagado" without proof: no payment exists -> never "por confirmar".
		self.assertIn("PAGO REPORTADO SIN COMPROBANTE", html)
		self.assertNotIn("SIN CONFIRMAR", html)
		self.assertIn("fg-cartera-card--atencion", html)
		html = self._render_card(
			_item(status="Por Validar", amount_available=False, invoice_amount=0, outstanding_amount=0, amount_source="Sin valor calculable", bucket="por_validar")
		)
		self.assertIn("VALOR POR VALIDAR", html)
		self.assertNotIn("$ 1.250.000", html)

	def test_card_delivery_issues(self):
		html = self._render_card(_item(has_delivery_issues=1))
		self.assertIn("⚠ FALTANTES / CAMBIOS", html)
		self.assertNotIn("FALTANTES", self._render_card(_item()))

	# =====================================================================
	# Detail (node)
	# =====================================================================

	def _detail(self, **overrides):
		d = _item(
			paid_on=None,
			delivered_by="driver@example.com",
			delivered_by_name="Juan Conductor",
			driver_payment_note=None,
			has_driver_proof=False,
			delivery_issues=None,
			invoice_issuer=None,
			payments=[],
		)
		d.update(overrides)
		return d

	def test_detail_sections(self):
		html = self._render_detail(self._detail(credit_days=30, due_date="2026-10-07", days_to_due=12, bucket="por_vencer", driver_payment_status="Crédito"))
		for text in (
			"CLIENTE",
			"PEDIDO",
			"PICK LIST",
			"STO-PICK-1",
			"RECORRIDO",
			"REC-1",
			"FECHA DE ENTREGA",
			"VALOR ORIGINAL",
			"PAGADO",
			"SALDO PENDIENTE",
			"CRÉDITO",
			"30 DÍAS",
			"VENCE EL",
			"07-10-2026",
			"VENCE EN 12 DÍAS",
			"REPORTE DE ENTREGA",
			"Crédito",
			"Juan Conductor",
			"22-09-2026 10:15",
			"HISTORIAL DE PAGOS",
			"Sin pagos registrados.",
		):
			self.assertIn(text, html)
		self.assertNotIn("VER COMPROBANTE", html)
		self.assertNotIn("PAGO REPORTADO POR CONDUCTOR", html)

	def test_detail_driver_reported_payment(self):
		html = self._render_detail(
			self._detail(
				status="Pagado",
				bucket="pagado",
				paid_amount=1250000,
				outstanding_amount=0,
				payment_verification="Sin confirmar",
				por_confirmar=True,
				driver_payment_status="Pagado",
				driver_payment_note="Efectivo",
				has_driver_proof=True,
				payments=[
					{
						"name": "CPAG-1",
						"payment_date": "2026-09-22",
						"amount": 1250000,
						"currency": "COP",
						"payment_method": None,
						"source": "Conductor",
						"reference": None,
						"notes": None,
						"accounting_status": "Sin contabilizar",
						"recorded_by": "driver@example.com",
						"recorded_by_name": "Juan Conductor",
						"recorded_on": "2026-09-22 10:15:00",
						"cancelled": False,
						"has_payment_proof": True,
						"proof_is_driver_proof": True,
					}
				],
			)
		)
		for text in (
			"PAGO REPORTADO POR CONDUCTOR",
			"$ 1.250.000",
			"⚠ PENDIENTE DE CONFIRMACIÓN",
			"VER COMPROBANTE",
			"OBSERVACIÓN DEL PAGO",
			"Efectivo",
			"CONDUCTOR",
			"Sin contabilizar",
			"REGISTRADO POR",
			"FECHA DE REGISTRO",
			"22-09-2026 10:15",
		):
			self.assertIn(text, html)
		# Read-only: no CONFIRMAR/RECHAZAR action (only the status texts).
		self.assertNotIn("CONFIRMAR", html.upper().replace("CONFIRMACIÓN", "").replace("SIN CONFIRMAR", ""))
		self.assertNotIn("RECHAZAR", html.upper())
		self.assertNotIn("<input", html)
		self.assertNotIn("<textarea", html)

	def test_detail_paid_without_proof_and_issues(self):
		html = self._render_detail(
			self._detail(status="Por Validar", bucket="por_validar", payment_verification="Sin confirmar", driver_payment_status="Pagado", has_delivery_issues=1, delivery_issues="Faltó 1 galón")
		)
		self.assertIn("PAGO REPORTADO POR CONDUCTOR SIN COMPROBANTE", html)
		self.assertNotIn("VER COMPROBANTE", html)
		self.assertNotIn("PENDIENTE DE CONFIRMACIÓN", html)
		self.assertNotIn("SIN CONFIRMAR", html)
		self.assertIn("⚠ ENTREGA CON FALTANTES / CAMBIOS", html)
		self.assertIn("Faltó 1 galón", html)

	def test_detail_cancelled_payment_marked(self):
		html = self._render_detail(
			self._detail(
				payments=[
					{
						"name": "CPAG-2",
						"payment_date": "2026-09-23",
						"amount": 100,
						"currency": "COP",
						"payment_method": "Efectivo",
						"source": "Cartera",
						"reference": "R-1",
						"notes": "nota",
						"accounting_status": "Sin contabilizar",
						"recorded_by": "c@example.com",
						"recorded_by_name": "Cartera",
						"recorded_on": "2026-09-23 09:00:00",
						"cancelled": True,
						"has_payment_proof": False,
						"proof_is_driver_proof": False,
					}
				]
			)
		)
		for text in ("ANULADO", "is-cancelled", "Efectivo", "R-1", "nota", "$ 100", "23-09-2026"):
			self.assertIn(text, html)

	# =====================================================================
	# XSS
	# =====================================================================

	def test_every_server_text_is_escaped(self):
		hostile = {
			"name": XSS,
			"customer": XSS,
			"customer_name": XSS,
			"customer_commercial_name": XSS,
			"commercial_name": XSS,
			"sales_order": XSS + "x",
			"pick_list": XSS,
			"recorrido": XSS,
			"has_delivery_issues": 1,
		}
		card = self._render_card(_item(**hostile))
		detail = self._render_detail(
			self._detail(
				**hostile,
				driver_payment_status=XSS,
				driver_payment_note=XSS,
				delivery_issues=XSS,
				delivered_by=XSS,
				delivered_by_name=XSS,
				payments=[
					{
						"name": XSS,
						"payment_date": XSS,
						"amount": 1,
						"currency": XSS,
						"payment_method": XSS,
						"source": XSS,
						"reference": XSS,
						"notes": XSS,
						"accounting_status": XSS,
						"recorded_by": XSS,
						"recorded_by_name": XSS,
						"recorded_on": XSS,
						"cancelled": False,
						"has_payment_proof": False,
						"proof_is_driver_proof": False,
					}
				],
			)
		)
		for html in (card, detail):
			self.assertNotIn("<img", html)
			self.assertNotIn("<script", html)
			self.assertNotIn("onerror=alert", html)
			self.assertIn("&lt;img src&#x3D;x onerror&#x3D;alert(1)&gt;", html)
		# The search box value and the header user name are escaped too.
		out = self._node(f"console.log(JSON.stringify(render_search_bar_html({json.dumps(XSS)})));")
		self.assertNotIn("<img", out)
		self.assertIn("esc(fullname)", self.code)
		# No raw HTML sinks for server data.
		self.assertNotIn("innerHTML", self.code)
		self.assertNotIn("dangerouslySetInnerHTML", self.code)

	def test_driver_proof_is_a_validated_data_url(self):
		out = self._node(
			"console.log(JSON.stringify(["
			"proof_data_url({content_type: 'image/jpeg', data: '/9j/4AAQSkZJRg=='}),"
			"proof_data_url({content_type: 'image/png', data: 'iVBORw0KGgo='}),"
			"proof_data_url({content_type: 'text/html', data: 'PHNjcmlwdD4='}),"
			"proof_data_url({content_type: 'image/svg+xml', data: 'PHN2Zz4='}),"
			"proof_data_url({content_type: 'image/jpeg', data: 'AAA\" onerror=\"alert(1)'}),"
			"proof_data_url({content_type: 'image/jpeg', data: ''}),"
			"proof_data_url(null)"
			"]));"
		)
		self.assertEqual(out, ["data:image/jpeg;base64,/9j/4AAQSkZJRg==", "data:image/png;base64,iVBORw0KGgo=", None, None, None, None, None])
		show = self.code[self.code.index("show_driver_proof(obligation_name, $btn) {") :]
		self.assertIn('this.call("get_driver_payment_proof", { obligation_name: obligation_name })', show)
		self.assertIn('$img.attr("src", src)', show)
		self.assertNotIn("/private/files", self.js)
		self.assertNotIn("file_url", self.code)
		self.assertNotIn("window.open", self.code)

	# =====================================================================
	# Mobile CSS
	# =====================================================================

	def test_mobile_css(self):
		css = self.css
		# Touch targets >= 44px.
		for selector in (".fg-cartera .fg-cartera-chip", ".fg-cartera .fg-cartera-sync-btn"):
			self.assertIn("min-height: var(--fg-touch)", _css_rule(css, selector), selector)
		self.assertIn("height: 44px", _css_rule(css, ".fg-cartera .fg-cartera-pagination-btn"))
		self.assertIn("width: 44px", _css_rule(css, ".fg-cartera .fg-cartera-search .fg-search-clear"))
		self.assertIn("height: 52px", _css_rule(css, ".fg-cartera .fg-cartera-search .fg-search-input"))
		self.assertIn("height: var(--fg-touch)", _css_rule(css, ".fg-cartera .fg-np-back"))
		self.assertIn("width: 100%", _css_rule(css, ".fg-cartera .fg-cartera-card-detail"))
		# Mobile-first: KPIs 2 columns, CARTERA ACTUAL full width; 1 card column.
		self.assertIn("repeat(2, minmax(0, 1fr))", _css_rule(css, ".fg-cartera.fg-shell .fg-cartera-kpis"))
		self.assertIn("grid-column: 1 / -1", _css_rule(css, ".fg-cartera .fg-cartera-kpi--actual"))
		self.assertIn("minmax(0, 1fr)", _css_rule(css, ".fg-cartera .fg-cartera-cards"))
		self.assertIn("@media (min-width: 700px)", css)
		self.assertIn("@media (min-width: 1100px)", css)
		self.assertIn("repeat(5, minmax(0, 1fr))", css)

	def test_no_horizontal_overflow(self):
		css = self.css
		self.assertIn("overflow-x: hidden", _css_rule(css, ".fg-cartera"))
		for selector in (
			".fg-cartera .fg-cartera-card-name,\n.fg-cartera .fg-cartera-detail-name",
			".fg-cartera .fg-cartera-kpi-amount",
			".fg-cartera .fg-cartera-money-value",
			".fg-cartera .fg-cartera-detail-value",
			".fg-cartera .fg-cartera-pre",
		):
			self.assertIn("overflow-wrap: anywhere", _css_rule(css, selector), selector)
		self.assertIn("white-space: normal", _css_rule(css, ".fg-cartera .fg-cartera-badge"))
		self.assertIn("max-width: 100%", _css_rule(css, ".fg-cartera-proof-dialog .fg-cartera-proof-img"))
		# Every grid track can shrink (minmax(0, ...)) and nothing has a fixed
		# width wider than a 360px phone.
		for columns in re.findall(r"grid-template-columns:\s*([^;]+);", css):
			self.assertIn("minmax(0", columns, columns)
		for prop, value in re.findall(r"^\s*(width|min-width):\s*(\d+)px", css, flags=re.MULTILINE):
			self.assertLessEqual(int(value), 360, f"{prop}: {value}px")
		# The only nowrap: KPI amounts, sized to their card with cqi.
		self.assertEqual(css.count("white-space: nowrap"), 1)
		self.assertIn("container-type: inline-size", _css_rule(css, ".fg-cartera .fg-cartera-kpi"))
		supports = css[css.index("@supports (font-size: 1cqi)") :]
		self.assertIn("white-space: nowrap", supports[: supports.index("\n}\n")])
		self.assertIn("cqi", supports[: supports.index("\n}\n")])
