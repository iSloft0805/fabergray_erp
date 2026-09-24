# -*- coding: utf-8 -*-
"""Fase 26.2 -- UI contract for INICIAR RECORRIDO + MODO RECORRIDO +
Waze/Maps in page/recorridos (recorridos.js/.css).

This app has no JS test runner, so -- like test_search_bar_ui_contract.py --
most checks read the source. The three pure helpers the navigation depends
on (valid_coordinate_pair/navigation_links/current_stop_of) are also
EXECUTED with node, extracted verbatim from recorridos.js, so the exact
URLs, the coordinate rule and the current-stop rule are tested for real,
not only by regex. Those tests skip if node is not installed."""

import json
import os
import re
import shutil
import subprocess
import unittest

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "recorridos")
_JS_PATH = os.path.join(_PAGE_DIR, "recorridos.js")
_CSS_PATH = os.path.join(_PAGE_DIR, "recorridos.css")
_NODE = shutil.which("node")


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _block(source, start_marker):
	"""The balanced-brace body that starts at the first "{" after
	`start_marker` -- a function/method/if-branch body."""
	start = source.index(start_marker)
	i = source.index("{", start)
	depth = 0
	for j in range(i, len(source)):
		if source[j] == "{":
			depth += 1
		elif source[j] == "}":
			depth -= 1
			if depth == 0:
				return source[start : j + 1]
	raise AssertionError(f"unbalanced block after {start_marker!r}")


def _code(source):
	"""`source` without whole-line // comments -- so explanatory comments that
	mention e.g. window.open() never count as code."""
	return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def _css_rule(css, selector):
	match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	assert match, f"missing CSS rule {selector}"
	return match.group(1)


class TestRecorridosActiveRouteUIContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_JS_PATH)
		cls.css = _read(_CSS_PATH)

	# -- node harness ----------------------------------------------------------
	def _run_helpers(self, expression):
		if not _NODE:
			raise unittest.SkipTest("node not available")
		helpers = "\n".join(
			_block(self.js, f"function {name}(")
			for name in ("valid_coordinate_pair", "navigation_links", "current_stop_of")
		)
		script = (
			"const cint = (v) => parseInt(v, 10) || 0;\n"
			+ helpers
			+ f"\nprocess.stdout.write(JSON.stringify({expression}));"
		)
		out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
		return json.loads(out.stdout)

	# =====================================================================
	# INICIAR RECORRIDO / CONTINUAR
	# =====================================================================

	def test_iniciar_only_in_planificado_branch(self):
		render = _block(self.js, "render_detail_body() {")
		planificado = _block(render, "} else if (is_planificado) {")
		self.assertIn('__("INICIAR RECORRIDO")', planificado)
		self.assertIn("confirm_start_route_from_detail", planificado)
		self.assertEqual(render.count('__("INICIAR RECORRIDO")'), 1)
		self.assertEqual(self.js.count('__("INICIAR RECORRIDO")'), 1)

	def test_continuar_only_for_en_ruta(self):
		render = _block(self.js, "render_detail_body() {")
		en_ruta = _block(render, '} else if (d.status === "En Ruta") {')
		self.assertIn('__("CONTINUAR RECORRIDO")', en_ruta)
		self.assertIn("enter_active_route", en_ruta)
		self.assertEqual(self.js.count('__("CONTINUAR RECORRIDO")'), 1)

		card = _block(self.js, "render_route_card(r, is_history) {")
		continuar = _block(card, 'if (!is_history && r.status === "En Ruta") {')
		self.assertIn('__("CONTINUAR")', continuar)
		self.assertIn("fg-recorridos-continue-btn", continuar)
		self.assertEqual(self.js.count('__("CONTINUAR")'), 1)

	def test_start_confirmation_dialog(self):
		confirm = _block(self.js, "confirm_start_route_from_detail() {")
		self.assertIn('__("¿Iniciar recorrido?")', confirm)
		self.assertIn(
			"Una vez iniciado comenzarás la ruta de entrega. Las paradas conservarán el orden planificado.", confirm
		)
		self.assertIn('primary_action_label: __("INICIAR")', confirm)
		self.assertIn('secondary_action_label: __("CANCELAR")', confirm)
		self.assertNotIn("frappe.confirm(", confirm)

	def test_start_disables_iniciar_while_running_then_enters_active_route(self):
		submit = _block(self.js, "submit_start_route(confirm, route_name) {")
		self.assertIn('this.call("start_route"', submit)
		self.assertLess(submit.index("disable_primary_action()"), submit.index('this.call("start_route"'))
		self.assertIn("enable_primary_action()", submit)
		success = _block(submit, ".then((detail) => {")
		self.assertIn("this._detail_dialog.hide()", success)
		self.assertIn("this.enter_active_route(detail)", success)

	# =====================================================================
	# MODO RECORRIDO
	# =====================================================================

	def test_active_route_is_a_page_view_not_a_dialog(self):
		self.assertIn('this.view = "list";', self.js)
		enter = _block(self.js, "enter_active_route(detail) {")
		self.assertIn('this.view = "active-route";', enter)
		render = _block(self.js, "render_active_route() {")
		self.assertIn("this.$body.html(", render)
		self.assertNotIn("frappe.ui.Dialog", render)
		self.assertNotIn("frappe.ui.Dialog", _block(self.js, "render_active_stop_html(stop, position, total) {"))

	def test_active_route_shows_required_information(self):
		stop = _block(self.js, "render_active_stop_html(stop, position, total) {")
		for needle in (
			'__("PARADA {0} DE {1}", [position, total])',
			"stop.customer_name",
			"stop.address_display",
			'__("PEDIDO")',
			"stop.item_count",
			"stop.total_qty",
		):
			self.assertIn(needle, stop)
		render = _block(self.js, "render_active_route() {")
		self.assertIn("status_badge_html(d.status)", render)
		self.assertIn('__("Próxima parada")', render)
		self.assertIn("current_stop_of(stops)", render)

	def test_current_stop_is_first_pendiente_by_sequence(self):
		result = self._run_helpers(
			"[current_stop_of(["
			"{sequence: 3, status: 'Pendiente', n: 'c'},"
			"{sequence: 1, status: 'Entregado', n: 'a'},"
			"{sequence: 2, status: 'Pendiente', n: 'b'}]).n,"
			"current_stop_of([{sequence: 1, status: 'Entregado'}, {sequence: 2, status: 'No Entregado'}]),"
			"current_stop_of([])]"
		)
		self.assertEqual(result, ["b", None, None])

	def test_current_stop_is_derived_not_persisted(self):
		self.assertNotIn("current_stop", _read(os.path.join(os.path.dirname(_PAGE_DIR), "..", "doctype", "recorrido", "recorrido.json")))
		self.assertNotIn("current_stop", self.js.replace("current_stop_of", ""))

	# =====================================================================
	# Waze / Google Maps
	# =====================================================================

	def test_waze_and_maps_urls(self):
		links = self._run_helpers("navigation_links({latitude: 7.119349, longitude: -73.1227416})")
		self.assertEqual(links["waze"], "https://waze.com/ul?ll=7.119349,-73.122742&navigate=yes")
		self.assertEqual(
			links["maps"],
			"https://www.google.com/maps/dir/?api=1&destination=7.119349,-73.122742&travelmode=driving",
		)
		string_input = self._run_helpers("navigation_links({latitude: '4.7', longitude: '-74.07'})")
		self.assertEqual(string_input["waze"], "https://waze.com/ul?ll=4.700000,-74.070000&navigate=yes")

	def test_invalid_coordinates_produce_no_links(self):
		cases = (
			"null, 1",
			"1, undefined",
			"'', ''",
			"0, 0",
			"'0', '0'",
			"91, 0",
			"-91, 0",
			"0, 181",
			"0, -181",
			"'abc', 1",
			"NaN, 1",
			"Infinity, 1",
		)
		result = self._run_helpers(
			"[" + ",".join(f"navigation_links({{latitude: {c.split(', ')[0]}, longitude: {c.split(', ')[1]}}})" for c in cases) + ", navigation_links(null)]"
		)
		self.assertEqual(result, [None] * (len(cases) + 1))

	def test_navigation_uses_real_anchor_links(self):
		stop = _code(_block(self.js, "render_active_stop_html(stop, position, total) {"))
		anchors = re.findall(r"<a\b[^>]*>", stop)
		self.assertEqual(len(anchors), 2, anchors)
		for anchor, href in zip(anchors, ("${links.waze}", "${links.maps}")):
			self.assertIn(f'href="{href}"', anchor)
			self.assertIn('target="_blank"', anchor)
			self.assertIn('rel="noopener noreferrer"', anchor)
		self.assertIn('__("ABRIR EN WAZE")', stop)
		self.assertIn('__("GOOGLE MAPS")', stop)
		self.assertLess(stop.index("fg-active-route-nav-btn--waze"), stop.index("fg-active-route-nav-btn--maps"))

	def test_no_coordinates_fallback_has_no_href(self):
		stop = _block(self.js, "render_active_stop_html(stop, position, total) {")
		self.assertIn("const links = navigation_links(stop);", stop)
		fallback = stop[stop.index(": `", stop.index("const nav_html = links")) :]
		fallback = fallback[: fallback.index("`;") + 2]
		self.assertIn('__("UBICACIÓN NO DISPONIBLE")', fallback)
		self.assertNotIn("href", fallback)
		self.assertNotIn("<a", fallback)

	def test_no_window_open_and_no_maps_api_calls(self):
		self.assertNotIn("window.open", _code(self.js))
		self.assertNotIn("maps.googleapis.com", self.js)
		self.assertNotIn("waze.com/sdk", self.js)

	# =====================================================================
	# Responsive / mobile first
	# =====================================================================

	def test_active_route_css_mobile_first(self):
		container = _css_rule(self.css, ".fg-recorridos .fg-active-route")
		self.assertIn("max-width: 560px", container)
		self.assertIn("margin: 0 auto", container)
		self.assertIn("min-width: 0", container)

		bar = _css_rule(self.css, ".fg-recorridos .fg-active-route-bar")
		self.assertIn("position: sticky", bar)

		waze = _css_rule(self.css, ".fg-recorridos .fg-active-route-nav-btn--waze")
		maps = _css_rule(self.css, ".fg-recorridos .fg-active-route-nav-btn--maps")
		self.assertGreaterEqual(int(re.search(r"min-height:\s*(\d+)px", waze).group(1)), 44)
		self.assertGreaterEqual(int(re.search(r"min-height:\s*(\d+)px", maps).group(1)), 44)
		self.assertGreater(
			int(re.search(r"min-height:\s*(\d+)px", waze).group(1)),
			int(re.search(r"min-height:\s*(\d+)px", maps).group(1)),
		)
		self.assertIn("width: 100%", _css_rule(self.css, ".fg-recorridos .fg-active-route-nav-btn"))

		for selector in (".fg-recorridos .fg-active-route-customer", ".fg-recorridos .fg-active-route-address"):
			self.assertIn("overflow-wrap: anywhere", _css_rule(self.css, selector))

		start_btn = _css_rule(self.css, ".fg-recorridos-detail-dialog .standard-actions .fg-route-btn-start")
		self.assertGreaterEqual(int(re.search(r"min-height:\s*(\d+)px", start_btn).group(1)), 44)

		fase = self.css[self.css.index("Fase 26.2 -- INICIAR RECORRIDO + MODO RECORRIDO") :]
		self.assertIn("@media (max-width: 640px)", fase)
		self.assertNotIn("<table", self.js[self.js.index("render_active_route() {") :])
