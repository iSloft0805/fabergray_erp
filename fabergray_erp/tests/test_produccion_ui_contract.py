# -*- coding: utf-8 -*-
"""Fase 28.3 -- UI contract for Page Producción (page/produccion).

Same approach as the Cartera/Recorridos UI contracts: source checks, plus
the pure render/format helpers EXECUTED with node (KPIs, cards, detail,
materials, quantities, dates), fed with hostile strings to prove every
server text is escaped. Node-backed tests skip if node is missing.

TestProduccionLayout renders the same helpers with the real fg_shell.css
+ produccion.css in a headless Chromium at 360 / 768 / 1280 px and
measures the layout (no horizontal overflow, grid columns per width).
It needs a Chromium binary: FG_CHROME_BIN, or Playwright's cached
headless shell; FG_CHROME_LD_LIBRARY_PATH is passed as LD_LIBRARY_PATH
when the host lacks a shared library. It skips when none is available.
Everything is written to a temporary directory -- no screenshots and no
artifacts are left behind."""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import produccion as produccion_api

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_APP = frappe.get_app_path("fabergray_erp")
_PAGE_DIR = os.path.join(_APP, "fabrigray_erp", "page", "produccion")
_NODE = shutil.which("node")

XSS = "<img src=x onerror=alert(1)><script>alert(2)</script>"
XSS_ESCAPED = "&lt;img src&#x3D;x onerror&#x3D;alert(1)&gt;"

# Minimal browser/Frappe stand-ins so produccion.js can be evaluated.
# escape_html mirrors frappe.utils.escape_html (frappe/public/js/frappe/utils/utils.js).
_PRELUDE = r"""
var __ = function (s, args) {
	return String(s).replace(/\{(\d+)\}/g, function (m, i) { return args && args[i] !== undefined ? args[i] : m; });
};
var frappe = {
	pages: { produccion: {} },
	provide: function () {},
	session: { user: "u@example.com", user_fullname: "U" },
	user: { roles: ["Producción"], has_role: function (r) { return this.roles.indexOf(r) !== -1; } },
	utils: {
		escape_html: function (txt) {
			var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;" };
			return String(txt).replace(/[&<>"'`=]/g, function (c) { return map[c]; });
		},
	},
	ui: {},
};
var fabergray_erp = {};
"""


def _read(*parts):
	with open(os.path.join(*parts), encoding="utf-8") as f:
		return f.read()


def _code(source):
	"""Source without // line comments."""
	return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def _css_rule(css, selector):
	match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	assert match, f"missing CSS rule {selector}"
	return match.group(1)


def _order(**overrides):
	order = {
		"name": "MFG-WO-2026-00010",
		"production_item": "FG-DES-1G",
		"item_name": "DESENGRASANTE 1 GALÓN",
		"bom_no": "BOM-FG-DES-1G-002",
		"stock_uom": "Unidad",
		"qty": 20,
		"produced_qty": 6,
		"pending_qty": 14,
		"allocated_qty": 13,
		"available_lot_qty": 7,
		"native_status": "In Process",
		"state": "en_produccion",
		"material_level": "ok",
		"planned_start_date": "2026-09-25 10:30:00",
		"last_manufacture_date": None,
		"oldest_reported_on": "2026-09-25 10:35:12.123",
		"oldest_sales_order": "PEDIDO-120",
		"oldest_customer": "CUST-1",
		"oldest_customer_name": "Ferretería Uno",
		"orders_count": 2,
	}
	order.update(overrides)
	return order


def _material(**overrides):
	row = {
		"item_code": "MP-ACIDO",
		"item_name": "ÁCIDO SULFÓNICO",
		"stock_uom": "kg",
		"source_warehouse": "Materias Primas - FG",
		"warehouse_problem": None,
		"required_qty": 10,
		"consumed_qty": 0,
		"pending_qty": 10,
		"available_qty": 15,
		"shortfall_qty": 0,
		"level": "ok",
	}
	row.update(overrides)
	return row


def _report(**overrides):
	row = {
		"name": "FALT-0001",
		"customer": "CUST-1",
		"customer_name": "Cliente X",
		"sales_order": "PEDIDO-120",
		"pick_list": "STO-PICK-9",
		"item_code": "FG-DES-1G",
		"item_name": "DESENGRASANTE 1 GALÓN",
		"qty_faltante": 5,
		"production_qty_allocated": 5,
		"status": "En Proceso",
		"reported_on": "2026-09-25 10:35:12",
	}
	row.update(overrides)
	return row


def _detail(**overrides):
	detail = _order(
		fg_warehouse="Producto Terminado - FG",
		reports=[
			_report(),
			_report(name="FALT-0002", customer="CUST-2", customer_name="Cliente Y", sales_order="PEDIDO-121", qty_faltante=8, production_qty_allocated=8),
		],
		reports_allocated_total=13,
		materials=[
			_material(),
			_material(item_code="MP-ENV", item_name="ENVASE GALÓN", stock_uom="Unidad", source_warehouse="Empaques - FG", required_qty=20, pending_qty=20, available_qty=17, shortfall_qty=3, level="parcial"),
			_material(item_code="MP-TAPA", item_name="TAPA", stock_uom="Unidad", source_warehouse=None, warehouse_problem="sin_bodega", available_qty=None, shortfall_qty=20, required_qty=20, pending_qty=20, level="config"),
		],
	)
	detail.update(overrides)
	return detail


class _PageSource(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_PAGE_DIR, "produccion.js")
		cls.css = _read(_PAGE_DIR, "produccion.css")
		cls.shell_css = _read(_APP, "public", "css", "fg_shell.css")
		cls.code = _code(cls.js)

	def _node(self, body):
		if not _NODE:
			raise unittest.SkipTest("node not available")
		script = _PRELUDE + self.js + "\n;(function(){\n" + body + "\n})();"
		out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
		return json.loads(out.stdout)

	def _call(self, expr):
		return self._node(f"console.log(JSON.stringify({expr}));")


class TestProduccionUIContract(_PageSource):
	# =====================================================================
	# Page / roles / navigation
	# =====================================================================

	def test_page_definition_and_roles(self):
		page = json.loads(_read(_PAGE_DIR, "produccion.json"))
		self.assertEqual((page["name"], page["page_name"], page["module"]), ("produccion", "produccion", "Fabrigray ERP"))
		self.assertEqual({r["role"] for r in page["roles"]}, {"Producción", "Jefe de Producción", "System Manager"})
		self.assertTrue(os.path.exists(os.path.join(_PAGE_DIR, "__init__.py")))
		self.assertIn('frappe.pages["produccion"].on_page_load', self.js)
		self.assertIn('class="fg-shell fg-produccion"', self.js)

	def test_home_card_and_workspace_are_role_gated(self):
		blocks = json.loads(_read(_APP, "fixtures", "custom_html_block.json"))
		block = next(b for b in blocks if b["name"] == "Fabrigray Home")
		card = re.search(r'<a class="fg-home-card"[^>]*data-route="produccion"[^>]*>', block["html"])
		self.assertTrue(card)
		self.assertIn('data-page="produccion"', card.group(0))
		self.assertIn(" hidden", card.group(0))  # hidden until the session may open the Page
		self.assertIn("frappe.boot.page_info", block["script"])
		self.assertIn("card.hidden = false", block["script"])
		self.assertIn(".fg-home-card[hidden]", block["style"])
		workspace = json.loads(_read(_APP, "fabrigray_erp", "workspace", "fabrigray_erp", "fabrigray_erp.json"))
		roles = {r["role"] for r in workspace["roles"]}
		self.assertTrue({"Producción", "Jefe de Producción", "System Manager"} <= roles)

	# =====================================================================
	# Server authority: KPIs, tabs, filters, search, pagination
	# =====================================================================

	def test_only_the_three_read_endpoints_are_called_with_get(self):
		calls = set(re.findall(r'this\.call\("([a-z_]+)"', self.code))
		self.assertEqual(calls, {"get_production_dashboard", "get_production_orders", "get_production_order_detail"})
		self.assertIn('this.method_prefix = "fabergray_erp.api.produccion."', self.code)
		self.assertIn('type: "GET"', self.code)
		for forbidden in ("frappe.client", "frappe.db.", "frappe.xcall", "make_stock_entry", "/api/resource", "type: \"POST\""):
			self.assertNotIn(forbidden, self.code, forbidden)

	def test_no_production_or_inventory_actions(self):
		for label in (
			"INICIAR PRODUCCIÓN",
			"REGISTRAR PRODUCCIÓN",
			"FINALIZAR",
			"CANCELAR",
			"EDITAR BOM",
			"COMPRAR MATERIAL",
			"Stock Entry",
		):
			self.assertNotIn(label, self.code, label)
		buttons = set(re.findall(r'class="fg-btn [^"]*\b(fg-prod-[a-z-]+)"', self.code))
		self.assertEqual(buttons, {"fg-prod-retry-list", "fg-prod-retry-kpis", "fg-prod-card-detail", "fg-prod-retry-detail"})

	def test_kpis_come_from_the_server(self):
		html = self._call(
			"render_kpis_html({kpis: {pendientes: 3, en_produccion: 2, falta_material: 1, completadas_hoy: 4}}, false, 'pendientes')"
		)
		for label, value in (("PENDIENTES", 3), ("EN PRODUCCIÓN", 2), ("FALTA MATERIAL", 1), ("COMPLETADAS HOY", 4)):
			self.assertIn(label, html)
			self.assertIn(f'<div class="fg-kpi-number">{value}</div>', html)
		self.assertIn('data-tab="completadas"', html)  # COMPLETADAS HOY opens COMPLETADAS
		self.assertEqual(html.count("is-active"), 1)
		error = self._call("render_kpis_html(null, true)")
		self.assertIn("fg-prod-retry-kpis", error)
		# Never aggregated from the loaded cards.
		self.assertNotIn(".reduce(", self.code)

	def test_tabs_and_filters_match_the_backend(self):
		tabs = self._call("TABS.map(function (t) { return t.key; })")
		self.assertEqual(tabs, list(produccion_api.TABS))
		self.assertEqual(self._call("MATERIAL_FILTERS.map(function (o) { return o.value; })"), list(produccion_api.MATERIAL_FILTERS))
		self.assertEqual(self._call("DATE_FILTERS.map(function (o) { return o.value; })"), list(produccion_api.DATE_RANGES))
		self.assertEqual(
			self._call("render_tabs_html('en_produccion')").count('aria-selected="true"'), 1
		)
		self.assertEqual(self._call("PAGE_LENGTH"), produccion_api.DEFAULT_PAGE_LENGTH)

	def test_search_is_server_side_and_debounced(self):
		self.assertIn("SEARCH_DEBOUNCE_MS", self.code)
		self.assertGreaterEqual(self._call("SEARCH_DEBOUNCE_MS"), 250)
		self.assertIn("search: this.list_search", self.code)
		self.assertIn("_list_request_seq", self.code)  # out-of-order responses dropped
		self.assertNotIn(".filter((item", self.code)

	def test_pagination_uses_server_total_and_has_more(self):
		html = self._call("render_pagination_html({total: 45, page: 2, page_size: 20, has_more: true}, false)")
		self.assertIn("Mostrando 21 a 40 de 45", html)
		self.assertNotIn("disabled", html)
		last = self._call("render_pagination_html({total: 45, page: 3, page_size: 20, has_more: false}, false)")
		self.assertIn("Mostrando 41 a 45 de 45", last)
		self.assertEqual(last.count("disabled"), 1)
		self.assertEqual(self._call("render_pagination_html({total: 0, items: []}, false)"), "")

	# =====================================================================
	# Formatting
	# =====================================================================

	def test_quantity_and_date_format(self):
		self.assertEqual(
			self._call("[format_qty(0), format_qty(20), format_qty(1234567), format_qty(2.5), format_qty(0.125), format_qty(10.0004), format_qty(null), format_qty('x')]"),
			["0", "20", "1.234.567", "2,5", "0,125", "10", "—", "—"],
		)
		self.assertEqual(
			self._call("[format_datetime_short('2026-09-25 10:35:12.1'), format_datetime_short('2026-09-25 00:05:00'), format_datetime_short('2026-01-02 15:00:00'), format_datetime_short(null)]"),
			["25 sep · 10:35 AM", "25 sep · 12:05 AM", "2 ene · 3:00 PM", "—"],
		)

	# =====================================================================
	# Cards
	# =====================================================================

	def test_card_shows_the_three_quantities_apart(self):
		html = self._call(f"render_order_card({json.dumps(_order())})")
		self.assertIn("DESENGRASANTE 1 GALÓN", html)
		self.assertIn("MFG-WO-2026-00010", html)
		self.assertIn("PLANEADO", html)
		self.assertIn("20 Unidad", html)
		self.assertIn("6 / 20 Unidad", html)  # PRODUCIDO
		self.assertIn("ASIGNADO A PEDIDOS", html)
		self.assertIn(">13<", html)
		self.assertIn("DISPONIBLE DEL LOTE", html)
		self.assertIn(">7<", html)
		self.assertIn("EN PRODUCCIÓN", html)
		self.assertIn("MATERIALES DISPONIBLES", html)
		self.assertIn("PEDIDO-120", html)
		self.assertIn("+1 pedidos", html)  # several orders share the Work Order
		self.assertIn("Ferretería Uno", html)
		self.assertIn("25 sep · 10:35 AM", html)
		self.assertIn('aria-valuenow="30"', html)
		self.assertIn('data-name="MFG-WO-2026-00010"', html)

	def test_card_states_and_material_traffic_light_have_text(self):
		cases = {
			("pendiente", "falta"): ("PENDIENTE", "FALTA MATERIAL"),
			("en_produccion", "parcial"): ("EN PRODUCCIÓN", "MATERIALES PARCIALES"),
			("pendiente", "config"): ("PENDIENTE", "CONFIGURACIÓN INCOMPLETA"),
			("completada", None): ("COMPLETADA", None),
		}
		for (state, level), (state_label, level_label) in cases.items():
			html = self._call(f"render_order_card({json.dumps(_order(state=state, material_level=level))})")
			self.assertIn(state_label, html)
			if level_label:
				self.assertIn(level_label, html)
				self.assertIn("<svg", html[html.index("fg-prod-material ") :])  # icon + text, not color only
			else:
				self.assertNotIn("fg-prod-material ", html)

	def test_long_names_and_large_quantities(self):
		long_name = "DESENGRASANTE INDUSTRIAL CONCENTRADO " * 6 + "X" * 80
		html = self._call(
			f"render_order_card({json.dumps(_order(item_name=long_name, qty=123456789.125, produced_qty=98765432.5, allocated_qty=100000000, available_lot_qty=23456789.125))})"
		)
		self.assertIn("123.456.789,125", html)
		self.assertIn("98.765.432,5", html)
		self.assertIn(long_name.strip(), html)

	def test_empty_state(self):
		self.assertEqual(self._call("empty_message('todas')"), "NO HAY ÓRDENES DE PRODUCCIÓN PENDIENTES")
		for tab in produccion_api.TABS:
			self.assertTrue(self._call(f"empty_message('{tab}')"))

	# =====================================================================
	# Detail
	# =====================================================================

	def test_detail_sections(self):
		html = self._call(f"render_detail_html({json.dumps(_detail())})")
		for text in (
			"Producto",
			"Item Code",
			"Work Order",
			"BOM-FG-DES-1G-002",
			"CANTIDAD PLANEADA",
			"CANTIDAD PRODUCIDA",
			"CANTIDAD PENDIENTE",
			"ASIGNADA A FALTANTES",
			"Producto Terminado - FG",
			"PEDIDOS / FALTANTES ASOCIADOS",
			"Cliente X",
			"Cliente Y",
			"PEDIDO-121",
			"STO-PICK-9",
			"Faltante original",
			"TOTAL ASIGNADO",
			"13 Unidad",
			"MATERIAS PRIMAS",
			"ÁCIDO SULFÓNICO",
			"NECESARIO",
			"DISPONIBLE",
			"CONSUMIDO",
			"FALTA",
			"Materias Primas - FG",
			"✓ DISPONIBLE",
			"FALTAN 3 Unidad",
			"BODEGA NO CONFIGURADA",
		):
			self.assertIn(text, html, text)
		# Read-only: no inputs, no forms, no action buttons in the detail.
		for tag in ("<input", "<form", "<select", "<textarea", "fg-btn"):
			self.assertNotIn(tag, html, tag)

	def test_detail_material_rows(self):
		consumed = self._call(
			f"render_material_html({json.dumps(_material(consumed_qty=4, pending_qty=6, available_qty=3, shortfall_qty=3, level='parcial'))})"
		)
		self.assertIn("Pendiente por consumir", consumed)
		self.assertIn("6 kg", consumed)
		self.assertIn("FALTAN 3 kg", consumed)
		other = self._call(
			f"render_material_html({json.dumps(_material(source_warehouse=None, warehouse_problem='otra_empresa', available_qty=None, level='config'))})"
		)
		self.assertIn("BODEGA DE OTRA EMPRESA", other)
		self.assertIn("—", other)
		done = self._call(f"render_material_html({json.dumps(_material(consumed_qty=10, pending_qty=0, level='consumido'))})")
		self.assertIn("CONSUMIDO", done)

	# =====================================================================
	# XSS
	# =====================================================================

	def test_every_server_text_is_escaped(self):
		hostile_order = _order(
			name=XSS,
			production_item=XSS,
			item_name=XSS,
			bom_no=XSS,
			stock_uom=XSS,
			oldest_sales_order=XSS,
			oldest_customer=XSS,
			oldest_customer_name=XSS,
			native_status=XSS,
		)
		card = self._call(f"render_order_card({json.dumps(hostile_order)})")
		detail = self._call(
			"render_detail_html({})".format(
				json.dumps(
					dict(
						hostile_order,
						fg_warehouse=XSS,
						reports=[
							_report(
								name=XSS,
								customer=XSS,
								customer_name=XSS,
								sales_order=XSS,
								pick_list=XSS,
								item_code=XSS,
								item_name=XSS,
								status=XSS,
								reported_on=XSS,
							)
						],
						reports_allocated_total=1,
						materials=[
							_material(item_code=XSS, item_name=XSS, stock_uom=XSS, source_warehouse=XSS, level=XSS),
							_material(item_code=XSS, item_name=XSS, stock_uom=XSS, source_warehouse=XSS, warehouse_problem="invalida", level="config"),
						],
					)
				)
			)
		)
		search = self._call(f"render_search_bar_html({json.dumps(XSS)})")
		for html in (card, detail, search):
			self.assertNotIn("<img", html)
			self.assertNotIn("<script", html)
			self.assertNotIn("onerror=alert", html)
			self.assertIn(XSS_ESCAPED, html)
		self.assertIn("esc(fullname)", self.code)
		self.assertIn("esc(role_label())", self.code)
		# No raw HTML sinks for server data.
		self.assertNotIn("innerHTML", self.code)
		self.assertNotIn("dangerouslySetInnerHTML", self.code)
		self.assertNotIn("eval(", self.code)

	# =====================================================================
	# Mobile / desktop CSS
	# =====================================================================

	def test_mobile_first_css(self):
		css = self.css
		for selector in (
			".fg-produccion .fg-prod-tab",
			".fg-produccion .fg-prod-select select",
			".fg-produccion .fg-prod-card-detail",
			".fg-produccion .fg-prod-kpi",
		):
			self.assertIn("min-height: var(--fg-touch)", _css_rule(css, selector), selector)
		self.assertIn("height: 44px", _css_rule(css, ".fg-produccion .fg-prod-pagination-btn"))
		self.assertIn("height: var(--fg-touch)", _css_rule(css, ".fg-produccion .fg-np-back"))
		self.assertIn("repeat(2, minmax(0, 1fr))", _css_rule(css, ".fg-produccion.fg-shell .fg-prod-kpis,\n.fg-produccion.fg-shell .fg-prod-skeleton-kpis"))
		self.assertIn("minmax(0, 1fr)", _css_rule(css, ".fg-produccion .fg-prod-cards"))
		self.assertIn("@media (min-width: 700px)", css)
		self.assertIn("@media (min-width: 1100px)", css)
		wide = css[css.index("@media (min-width: 1100px)") :]
		self.assertIn("repeat(4, minmax(0, 1fr))", wide)  # KPIs in one row
		self.assertIn("repeat(2, minmax(0, 1fr))", wide)  # two-column detail
		self.assertNotIn("<table", self.code)  # raw materials as rows/cards, never a wide table

	def test_no_horizontal_overflow_rules(self):
		css = self.css
		self.assertIn("overflow-x: hidden", _css_rule(css, ".fg-produccion"))
		for selector in (
			".fg-produccion .fg-prod-card-name,\n.fg-produccion .fg-prod-report-name,\n.fg-produccion .fg-prod-material-name",
			".fg-produccion .fg-prod-metric-value,\n.fg-produccion .fg-prod-detail-value",
			".fg-produccion .fg-prod-badge",
		):
			self.assertIn("overflow-wrap: anywhere", _css_rule(css, selector), selector)
		for columns in re.findall(r"grid-template-columns:\s*([^;]+);", css):
			self.assertIn("minmax(0", columns, columns)
		for prop, value in re.findall(r"^\s*(width|min-width):\s*(\d+)px", css, flags=re.MULTILINE):
			self.assertLessEqual(int(value), 360, f"{prop}: {value}px")
		self.assertNotIn("white-space: nowrap", css)


# =========================================================================
# Real layout in a headless browser (360 / 768 / 1280 px)
# =========================================================================


def _chrome_bin():
	candidates = [os.environ.get("FG_CHROME_BIN")]
	candidates += sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
	candidates += [shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")]
	env = dict(os.environ)
	if os.environ.get("FG_CHROME_LD_LIBRARY_PATH"):
		env["LD_LIBRARY_PATH"] = os.environ["FG_CHROME_LD_LIBRARY_PATH"]
	for binary in filter(None, candidates):
		try:
			subprocess.run([binary, "--version"], capture_output=True, timeout=20, check=True, env=env)
		except Exception:
			continue
		return binary, env
	return None, None


_MEASURE = r"""
function fg_measure() {
	var vw = window.innerWidth;
	var offenders = [];
	document.querySelectorAll(".fg-shell *").forEach(function (el) {
		var r = el.getBoundingClientRect();
		if (r.width && (r.right > vw + 0.5 || r.left < -0.5)) {
			offenders.push((el.className && el.className.baseVal === undefined ? el.className : el.tagName) + " " + Math.round(r.right));
		}
	});
	function cols(sel) {
		var el = document.querySelector(sel);
		return el ? getComputedStyle(el).gridTemplateColumns.split(" ").filter(Boolean).length : null;
	}
	var small = [];
	document.querySelectorAll(".fg-prod-tab, .fg-prod-card-detail, .fg-prod-select select, .fg-np-back, .fg-prod-pagination-btn, .fg-prod-kpi").forEach(function (el) {
		if (el.getBoundingClientRect().height < 43.5) small.push(el.className);
	});
	return {
		viewport: vw,
		scroll_width: document.documentElement.scrollWidth,
		offenders: offenders.slice(0, 10),
		kpi_cols: cols(".fg-prod-kpis"),
		card_cols: cols(".fg-prod-cards"),
		detail_cols: cols(".fg-prod-detail"),
		small_targets: small,
	};
}
"""


class TestProduccionLayout(_PageSource):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.chrome, cls.chrome_env = _chrome_bin()

	def _render(self, width, body_js):
		if not self.chrome:
			raise unittest.SkipTest("headless Chromium not available")
		body_js = body_js.replace("</script", "<\\/script")  # hostile strings stay inside the <script>
		header = """
			<div class="fg-header">
				<div class="fg-header-brand"><span class="fg-header-logo">FABRIGRAY</span><span class="fg-header-sep">|</span>
				<span class="fg-header-title">PRODUCCIÓN</span></div>
				<div class="fg-header-user"><div class="fg-header-user-info"><div class="fg-header-user-name">Usuaria Producción</div>
				<div class="fg-header-user-role">Jefe de Producción</div></div><div class="fg-header-avatar">UP</div>
				<button type="button" class="fg-refresh-btn">R</button></div>
			</div>"""
		page = f"""<!doctype html><html><head><meta charset="utf-8">
			<meta name="viewport" content="width=device-width, initial-scale=1">
			<style>body {{ margin: 0; }} {self.shell_css}\n{self.css}</style></head>
			<body><div class="fg-shell fg-produccion">{header}<div class="fg-body" id="fg-body"></div></div>
			<script>{_PRELUDE}\n{self.js}\n{_MEASURE}
			document.getElementById("fg-body").insertAdjacentHTML("beforeend", (function () {{ {body_js} }})());
			var pre = document.createElement("pre"); pre.id = "fg-result";
			pre.textContent = JSON.stringify(fg_measure());
			document.body.appendChild(pre);
			</script></body></html>"""
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "page.html")
			with open(path, "w", encoding="utf-8") as f:
				f.write(page)
			out = subprocess.run(
				[
					self.chrome,
					"--headless",
					"--no-sandbox",
					"--disable-gpu",
					f"--user-data-dir={os.path.join(tmp, 'profile')}",
					f"--window-size={width},900",
					"--dump-dom",
					"file://" + path,
				],
				capture_output=True,
				text=True,
				timeout=90,
				env=self.chrome_env,
			)
		match = re.search(r'<pre id="fg-result">(.*?)</pre>', out.stdout, flags=re.S)
		self.assertTrue(match, out.stderr[-2000:])
		return json.loads(
			match.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
		)

	def _dashboard_js(self, items):
		return f"""
			var items = {json.dumps(items)};
			return '<div class="fg-prod-intro"><h1 class="fg-prod-title">PRODUCCIÓN</h1><p class="fg-prod-subtitle">Órdenes y necesidades de fabricación</p></div>'
				+ '<div class="fg-prod-kpis-slot">' + render_kpis_html({{kpis: {{pendientes: 1234, en_produccion: 56, falta_material: 7, completadas_hoy: 0}}}}, false, "todas") + '</div>'
				+ render_search_bar_html("{XSS.replace('"', '')}")
				+ '<div class="fg-prod-tabs">' + render_tabs_html("todas") + '</div>'
				+ '<div class="fg-prod-filters">' + render_filters_html("", "") + '</div>'
				+ '<div class="fg-prod-cards">' + (items.length ? items.map(render_order_card).join("") : '<div class="fg-empty fg-prod-empty">' + empty_message("todas") + '</div>') + '</div>'
				+ '<div class="fg-prod-pagination">' + render_pagination_html({{total: 45, page: 2, page_size: 20, has_more: true}}, false) + '</div>';
		"""

	def _items(self):
		long_name = "DESENGRASANTE INDUSTRIAL CONCENTRADO " * 4 + "SINESPACIOS" * 12
		return [
			_order(state="pendiente", material_level="falta", produced_qty=0),
			_order(state="en_produccion", material_level="parcial", item_name=long_name, production_item="FG-" + "X" * 70,
				qty=123456789.125, produced_qty=98765432.5, allocated_qty=100000000, available_lot_qty=23456789.125,
				oldest_customer_name="CLIENTE CON NOMBRE MUY LARGO " * 5, orders_count=12),
			_order(state="completada", material_level=None, produced_qty=20, pending_qty=0),
			_order(item_name=XSS, oldest_customer_name=XSS, material_level="config"),
		]

	def _detail_js(self):
		detail = _detail(
			item_name="DESENGRASANTE INDUSTRIAL CONCENTRADO " * 4,
			fg_warehouse="Producto Terminado Bodega Principal Norte - FG" * 2,
			reports=[_report(customer_name="CLIENTE CON NOMBRE MUY LARGO " * 5, qty_faltante=123456789.5)] + [_report(name=f"FALT-{i}") for i in range(5)],
			materials=_detail()["materials"]
			+ [_material(item_name="MATERIA PRIMA DE NOMBRE EXTRAORDINARIAMENTE LARGO " * 3, source_warehouse="BODEGA" * 20, required_qty=123456789.125, available_qty=1.5, shortfall_qty=123456787.625, level="parcial")],
		)
		return f"""
			return render_detail_header_html() + render_detail_html({json.dumps(detail)});
		"""

	def test_dashboard_without_horizontal_overflow(self):
		expected = {360: (2, 1), 768: (2, 2), 1280: (4, 3)}
		for width, (kpi_cols, card_cols) in expected.items():
			m = self._render(width, self._dashboard_js(self._items()))
			self.assertLessEqual(m["scroll_width"], m["viewport"], m)
			self.assertEqual(m["offenders"], [], m)
			self.assertEqual((m["kpi_cols"], m["card_cols"]), (kpi_cols, card_cols), m)
			self.assertEqual(m["small_targets"], [], m)

	def test_empty_state_without_horizontal_overflow(self):
		m = self._render(360, self._dashboard_js([]))
		self.assertLessEqual(m["scroll_width"], m["viewport"], m)
		self.assertEqual(m["offenders"], [], m)

	def test_detail_without_horizontal_overflow(self):
		for width, detail_cols in {360: 1, 768: 1, 1280: 2}.items():
			m = self._render(width, self._detail_js())
			self.assertLessEqual(m["scroll_width"], m["viewport"], m)
			self.assertEqual(m["offenders"], [], m)
			self.assertEqual(m["detail_cols"], detail_cols, m)
			self.assertEqual(m["small_targets"], [], m)
