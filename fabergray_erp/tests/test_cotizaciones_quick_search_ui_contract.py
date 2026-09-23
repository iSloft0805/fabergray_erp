# -*- coding: utf-8 -*-
"""Commit 25.26 -- contrato de UI del buscador rápido de Cotizaciones.

Esta app no tiene runner de JS: igual que test_quick_order_ui_contract.py y
las clases de contrato UI de test_cotizaciones_*.py, se lee cotizaciones.js/
.css como texto y se fija la forma de lo que importa (teclado, una sola
ruta de agregado, +1 sin duplicar, una llamada batch por búsqueda, payload
sin precio). Que ventas.search_items() no cambió lo fija
test_cotizaciones_quick_search.py (test_37).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page")
_COTIZACIONES_JS_PATH = os.path.join(_PAGE_DIR, "cotizaciones", "cotizaciones.js")
_COTIZACIONES_CSS_PATH = os.path.join(_PAGE_DIR, "cotizaciones", "cotizaciones.css")


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


class TestCotizacionesQuickSearchUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_COTIZACIONES_JS_PATH)
		cls.css = _read(_COTIZACIONES_CSS_PATH)

	# -- teclado --------------------------------------------------------------

	def test_keydown_is_bound_on_search_input(self):
		body = _method_body(self.js, "bind_item_search_input")
		self.assertIn('$item_input.on("keydown", (e) => this.handle_item_search_keydown(e))', body)

	def test_24_arrow_down_moves_to_next_result(self):
		body = _method_body(self.js, "handle_item_search_keydown")
		self.assertIn('e.key === "ArrowDown"', body)
		self.assertIn("this.set_active_result(Math.min(this.nc.active_index + 1, results.length - 1))", body)

	def test_25_arrow_up_moves_to_previous_result(self):
		body = _method_body(self.js, "handle_item_search_keydown")
		self.assertIn('e.key === "ArrowUp"', body)
		self.assertIn("this.set_active_result(Math.max(this.nc.active_index - 1, 0))", body)

	def test_26_enter_adds_active_result(self):
		body = _method_body(self.js, "handle_item_search_keydown")
		self.assertIn('e.key === "Enter"', body)
		self.assertIn("this.add_item_from_search(this.nc.active_index)", body)

	def test_26b_enter_ignored_for_stale_results(self):
		body = _method_body(self.js, "handle_item_search_keydown")
		self.assertIn("this.nc.results_txt === current_txt", body)
		self.assertIn("if (!fresh || this.nc.active_index < 0) return;", body)

	def test_26c_first_result_preselected(self):
		body = _method_body(self.js, "search_items")
		self.assertIn("this.nc.active_index = rows.length ? 0 : -1;", body)
		self.assertIn("this.nc.results_txt = txt;", body)

	def test_27_escape_closes_results(self):
		body = _method_body(self.js, "handle_item_search_keydown")
		self.assertIn('e.key === "Escape"', body)
		self.assertIn("this.close_item_results()", body)
		close = _method_body(self.js, "close_item_results")
		self.assertIn("this._item_search_seq++", close)
		self.assertIn("this.nc.item_results = [];", close)

	def test_27b_pending_debounced_search_cannot_reopen_after_close(self):
		close = _method_body(self.js, "close_item_results")
		self.assertIn("this.nc.search_dismissed = true;", close)
		search = _method_body(self.js, "search_items")
		self.assertIn("if (this.nc.search_dismissed) return Promise.resolve();", search.split("const seq")[0])
		bind = _method_body(self.js, "bind_item_search_input")
		self.assertIn("this.nc.search_dismissed = false;", bind)

	# -- agregado -------------------------------------------------------------

	def test_28_29_after_add_input_cleared_and_refocused(self):
		body = _method_body(self.js, "add_item_from_search")
		self.assertIn('$input.val("")', body)
		self.assertIn("this.close_item_results()", body)
		self.assertIn('$input.trigger("focus")', body)

	def test_30_add_button_uses_same_path_as_enter(self):
		body = _method_body(self.js, "bind_item_result_events")
		self.assertIn('.on("click", () => this.add_item_from_search(index))', body)
		card = _method_body(self.js, "render_item_result_card")
		self.assertIn("fg-qs-add", card)
		self.assertIn('__("AGREGAR")', card)
		# Exactly one function adds from search: Enter + click only.
		self.assertEqual(len(re.findall(r"this\.add_item_from_search\(", self.js)), 2)

	def test_22_23_repeat_increments_qty_never_duplicates_line(self):
		body = _method_body(self.js, "add_item_from_search")
		self.assertIn("this.set_cart_qty(r.item_code, this.cart_qty(r.item_code) + 1)", body)
		self.assertIn("cart: new Map(), // item_code ->", self.js)
		set_qty = _method_body(self.js, "set_cart_qty")
		self.assertIn("this.nc.cart.set(item_code, {", set_qty)

	def test_11_stock_never_disables_add(self):
		card = _method_body(self.js, "render_item_result_card")
		self.assertNotIn("disabled", card)
		add = _method_body(self.js, "add_item_from_search")
		self.assertNotIn("qty_disponible", add)

	# -- precio / stock en resultados ----------------------------------------

	def test_result_shows_price_or_sin_precio_and_stock(self):
		card = _method_body(self.js, "render_item_result_card")
		self.assertIn('__("Precio público")', card)
		self.assertIn('__("SIN PRECIO")', card)
		self.assertIn("details.has_public_price && details.public_price != null", card)
		self.assertIn('__("Stock")', card)
		self.assertIn("fg-qs-stock--zero", card)

	def test_price_never_copied_into_cart(self):
		set_qty = _method_body(self.js, "set_cart_qty")
		self.assertNotIn("price", set_qty)
		self.assertNotIn("_item_details_cache", set_qty)

	# -- llamadas / debounce / respuestas viejas ------------------------------

	def test_31_debounce_kept(self):
		body = _method_body(self.js, "bind_item_search_input")
		self.assertIn("frappe.utils.debounce((txt) => this.search_items(txt), 300)", body)

	def test_32_stale_response_guard_kept(self):
		body = _method_body(self.js, "search_items")
		self.assertIn("const seq = ++this._item_search_seq;", body)
		self.assertEqual(body.count("if (seq !== this._item_search_seq) return;"), 2)

	def test_33_one_batch_details_call_per_search(self):
		search = _method_body(self.js, "search_items")
		self.assertIn('this.call_ventas("search_items", { txt: txt })', search)
		hydrate = _method_body(self.js, "hydrate_item_details")
		self.assertIn('this.call("get_quick_search_item_details", { item_codes: missing })', hydrate)
		self.assertNotIn("Promise.all", hydrate)
		self.assertEqual(hydrate.count("this.call("), 1)
		self.assertNotIn('"get_item_info"', self.js)

	def test_20_empty_search_never_calls_server(self):
		body = _method_body(self.js, "search_items")
		guard = body.split("const seq")[0]
		self.assertIn("if (!txt || !txt.trim())", guard)
		self.assertNotIn("this.call", guard)

	# -- payload --------------------------------------------------------------

	def test_34_payload_still_item_code_and_qty_only(self):
		body = _method_body(self.js, "build_quotation_payload")
		self.assertIn(".map((l) => ({ item_code: l.item_code, qty: l.qty }))", body)
		for forbidden in ("rate", "price", "discount"):
			self.assertNotIn(forbidden, body)

	def test_no_discount_buttons_in_cotizaciones(self):
		self.assertNotIn("\"apply_quotation_price_mode\"", self.js)
		self.assertNotIn("PRECIO COMPLETO", self.js)

	# -- responsive -----------------------------------------------------------

	def test_add_button_touch_target_and_wrapping(self):
		self.assertRegex(self.css, r"\.fg-qs-add \{[^}]*min-height: var\(--fg-touch\)")
		self.assertRegex(self.css, r"\.fg-qs-name \{[^}]*overflow-wrap: anywhere")
		self.assertRegex(self.css, r"\.fg-qs-info \{[^}]*min-width: 0")


class TestCotizacionRapidaUiContract(IntegrationTestCase):
	"""Commit 25.26 (ampliación) -- selector de modo + Cotización rápida."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_COTIZACIONES_JS_PATH)
		cls.css = _read(_COTIZACIONES_CSS_PATH)

	# -- selector de modo -----------------------------------------------------

	def test_both_mode_labels_present(self):
		body = _method_body(self.js, "render_nueva_cotizacion")
		self.assertIn('__("Buscar manualmente")', body)
		self.assertIn('__("Cotización rápida")', body)
		self.assertIn('data-mode="manual"', body)
		self.assertIn('data-mode="quick"', body)

	def test_pedido_rapido_never_appears_in_cotizaciones(self):
		self.assertNotIn("pedido rápido", self.js.lower())
		self.assertNotIn("pedido rapido", self.js.lower())
		self.assertNotIn("Agregar productos al pedido", self.js)
		self.assertNotIn("Interpretar pedido", self.js)

	def test_initial_mode_is_manual(self):
		body = _method_body(self.js, "blank_nueva_cotizacion_state")
		self.assertIn('item_mode: "manual"', body)

	def test_quick_mode_replaces_manual_search_box(self):
		body = _method_body(self.js, "render_item_mode_body")
		quick_branch = body.split("return;")[0]
		self.assertIn('this.nc.item_mode === "quick"', quick_branch)
		self.assertIn("this.quick_quote_panel_html()", quick_branch)
		self.assertNotIn("fg-item-search-input", quick_branch)
		self.assertIn("fg-item-search-input", body.split("return;")[1])

	def test_switching_mode_never_touches_cart(self):
		for method in ("set_item_mode", "render_item_mode_body"):
			body = _method_body(self.js, method)
			self.assertNotIn("cart", body, method)
		self.assertIn("this.set_item_mode(", _method_body(self.js, "bind_nueva_cotizacion_events"))

	def test_quick_state_survives_mode_switch(self):
		panel = _method_body(self.js, "quick_quote_panel_html")
		self.assertIn("this.nc.quick.text", panel)
		body = _method_body(self.js, "render_item_mode_body")
		self.assertIn("this.render_quick_quote_lines()", body)

	# -- reutilización del parser ---------------------------------------------

	def test_reuses_ventas_parse_quick_order_endpoint(self):
		body = _method_body(self.js, "process_quick_quote")
		self.assertIn('this.call_ventas("parse_quick_order", { text: text })', body)
		self.assertEqual(self.js.count('call_ventas("parse_quick_order"'), 1)
		self.assertNotIn('this.call("parse_quick_order"', self.js)

	def test_only_server_preselection_auto_selects(self):
		body = _method_body(self.js, "build_quick_quote_line")
		self.assertIn("const pre = server_line.preselected_item;", body)
		self.assertIn("selected: pre ?", body)
		self.assertNotIn("top_candidate", body)

	def test_status_states_match_ventas_rules(self):
		body = _method_body(self.js, "quick_quote_line_status")
		first = body.strip().splitlines()[0]
		self.assertIn("!line.candidates.length", first)  # "No encontrado" checked first
		self.assertIn('line.confidence === "high" && !line.ambiguous', body)
		self.assertIn('line.confidence === "high" && line.ambiguous', body)
		self.assertIn('line.confidence === "medium"', body)

	def test_quick_panel_texts(self):
		panel = _method_body(self.js, "quick_quote_panel_html")
		self.assertIn('__("COTIZACIÓN RÁPIDA")', panel)
		self.assertIn('__("Pega o escribe varios productos con sus cantidades.")', panel)
		self.assertIn('__("PROCESAR COTIZACIÓN")', panel)
		self.assertIn("fg-cq-textarea", panel)
		bar = _method_body(self.js, "render_quick_quote_apply_bar")
		self.assertIn('__("AGREGAR A COTIZACIÓN")', bar)

	# -- no encontrados / ambiguos no se agregan en silencio -------------------

	def test_unresolved_lines_block_add(self):
		body = _method_body(self.js, "validate_quick_quote_lines")
		self.assertIn("!l.selected || !(flt(l.qty) > 0)", body)
		self.assertIn("missing.length === 0", body)
		bar = _method_body(self.js, "render_quick_quote_apply_bar")
		self.assertIn('${valid ? "" : "disabled"}', bar)
		apply = _method_body(self.js, "apply_quick_quote_to_cart")
		self.assertIn("if (!this.validate_quick_quote_lines().valid) return;", apply)
		self.assertIn("if (line.ignored || !line.selected) continue;", apply)

	# -- carrito compartido / duplicados --------------------------------------

	def test_apply_feeds_same_cart_and_sums_quantities(self):
		apply = _method_body(self.js, "apply_quick_quote_to_cart")
		self.assertIn("current.qty += qty;", apply)
		self.assertIn(
			"this.set_cart_qty(item_code, this.cart_qty(item_code) + a.qty, { item_name: a.item_name, stock_uom: a.stock_uom })",
			apply,
		)
		self.assertNotIn("this.nc.cart.set(", apply)
		self.assertNotIn("new Map(), // item_code -> {item_code", apply)
		self.assertEqual(self.js.count("cart: new Map()"), 1)

	def test_apply_never_creates_quotation_or_reads_stock(self):
		apply = _method_body(self.js, "apply_quick_quote_to_cart")
		for forbidden in ("create_and_submit_quotation", "update_draft_quotation", "modify_submitted_quotation", "qty_disponible", "price", "rate"):
			self.assertNotIn(forbidden, apply, forbidden)

	def test_cart_meta_is_name_and_uom_only(self):
		body = _method_body(self.js, "set_cart_qty")
		self.assertIn("(meta && meta.item_name)", body)
		self.assertIn("(meta && meta.stock_uom)", body)
		self.assertNotIn("price", body)
		self.assertNotIn("rate", body)

	def test_quotation_created_only_through_existing_endpoints(self):
		self.assertEqual(self.js.count('this.call("create_and_submit_quotation"'), 1)
		self.assertIn("this.call(\"create_and_submit_quotation\", payload)", self.js)

	# -- responsive -----------------------------------------------------------

	def test_responsive_rules(self):
		self.assertRegex(self.css, r"\.fg-cq-textarea \{[^}]*width: 100%")
		self.assertRegex(self.css, r"\.fg-item-mode-btn \{[^}]*min-height: var\(--fg-touch\)")
		self.assertRegex(self.css, r"\.fg-cq-candidate \{[^}]*min-height: var\(--fg-touch\)")
		self.assertRegex(self.css, r"\.fg-cq-qty-input \{[^}]*height: var\(--fg-touch\)")
		self.assertRegex(self.css, r"\.fg-cq-line-title \{[^}]*overflow-wrap: anywhere")
		self.assertIn("@media (max-width: 360px)", self.css)

	def test_format_help_is_visible_and_quantity_first(self):
		panel = _method_body(self.js, "quick_quote_panel_html")
		self.assertIn('__("Pega o escribe un producto por línea.")', panel)
		self.assertIn('__("Escribe primero la cantidad y después el producto.")', panel)
		self.assertIn('__("Ejemplo:")', panel)
		self.assertIn("escape_html(QUICK_QUOTE_EXAMPLE)", panel.split("<textarea")[0])  # visible help
		self.assertIn('placeholder="${frappe.utils.escape_html(QUICK_QUOTE_EXAMPLE)}"', panel)
		self.assertIn(
			'const QUICK_QUOTE_EXAMPLE = "5 Desengrasante 1 Galón\\n3 Escoba Industrial\\n10 Hipoclorito Galón";',
			self.js,
		)
		example = re.search(r'const QUICK_QUOTE_EXAMPLE = "([^"]+)";', self.js).group(1)
		for line in example.split("\\n"):
			self.assertRegex(line, r"^\d+ \S")  # quantity FIRST
			self.assertNotRegex(line.lower(), r"(x\s*\d+|-\s*\d+)$")  # never a trailing quantity

	def test_detected_quantity_shown_per_line(self):
		body = _method_body(self.js, "render_quick_quote_line")
		self.assertIn('__("Cantidad")', body)
		self.assertIn('class="fg-cq-qty-input" value="${line.qty}"', body)
		self.assertIn('status.mod === "high" ? "✓ "', body)
		self.assertIn('status.mod === "not-found" ? "⚠ "', body)

	def test_help_style_is_discreet(self):
		self.assertRegex(self.css, r"\.fg-cq-help \{[^}]*font-size: 0\.8rem")
		self.assertRegex(self.css, r"\.fg-cq-help-example \{[^}]*white-space: pre-wrap")
