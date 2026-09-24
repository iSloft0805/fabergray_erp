# -*- coding: utf-8 -*-
"""Fase 26.3 -- UI contract for ENTREGAR PEDIDO (foto + firma) inside Modo
Recorrido (page/recorridos recorridos.js/.css).

Same approach as test_recorridos_active_route_ui_contract.py: source checks,
plus the pure helpers EXECUTED with node -- delivery_evidence_ready(),
current_stop_of() after deliveries, and create_signature_pad() driven with
simulated Pointer Events on a stub canvas (empty signature, strokes, clear).
Node-backed tests skip if node is not installed."""

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
_NODE = shutil.which("node")


def _read(name):
	with open(os.path.join(_PAGE_DIR, name), encoding="utf-8") as f:
		return f.read()


def _block(source, start_marker):
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
	return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def _css_rule(css, selector):
	match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	assert match, f"missing CSS rule {selector}"
	return match.group(1)


def _min_height(rule):
	return int(re.search(r"min-height:\s*(\d+)px", rule).group(1))


class TestRecorridosDeliveryUIContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read("recorridos.js")
		cls.css = _read("recorridos.css")

	def _node(self, script):
		if not _NODE:
			raise unittest.SkipTest("node not available")
		out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
		return json.loads(out.stdout)

	def _helpers(self, *names):
		return "\n".join(_block(self.js, f"function {n}(") for n in names)

	def _const(self, name):
		"""A module-level `const NAME = ...;` statement, verbatim."""
		start = self.js.index(f"const {name} =")
		depth = 0
		for j in range(start, len(self.js)):
			c = self.js[j]
			if c in "[{(":
				depth += 1
			elif c in "]})":
				depth -= 1
			elif c == ";" and depth == 0:
				return self.js[start : j + 1]
		raise AssertionError(name)

	def _ready_harness(self):
		return (
			"const __ = (s) => s;\n"
			+ self._const("PAYMENT_STATUS_PAID")
			+ "\n"
			+ self._const("PAYMENT_STATUS_OPTIONS")
			+ "\n"
			+ self._helpers("delivery_evidence_ready")
		)

	# =====================================================================
	# ENTREGAR PEDIDO -- only for an En Ruta route's Pendiente stop
	# =====================================================================

	def test_entregar_only_when_en_ruta(self):
		stop_html = _block(self.js, "render_active_stop_html(stop, position, total) {")
		self.assertIn('this.active_route && this.active_route.status === "En Ruta"', stop_html)
		self.assertIn('__("ENTREGAR PEDIDO")', stop_html)
		self.assertLess(stop_html.index("fg-active-route-nav"), stop_html.index('__("ENTREGAR PEDIDO")'))

		open_panel = _block(self.js, "open_delivery_panel(stop_name) {")
		self.assertIn('stop.status !== "Pendiente"', open_panel)
		self.assertIn('this.active_route.status !== "En Ruta"', open_panel)

	def test_delivery_is_a_panel_inside_active_route_not_a_dialog(self):
		self.assertIn('if (this.active_panel === "delivery") return this.render_delivery_panel();', self.js)
		panel = _block(self.js, "render_delivery_panel() {")
		self.assertNotIn("frappe.ui.Dialog", panel)
		self.assertIn("this.$body.html(", panel)
		for needle in ('__("VOLVER")', '__("ENTREGAR PEDIDO")', '__("Cliente")', '__("Pedido")', '__("Dirección")'):
			self.assertIn(needle, panel)

	# =====================================================================
	# Foto
	# =====================================================================

	def test_photo_inputs_camera_and_gallery(self):
		panel = _block(self.js, "render_delivery_panel() {")
		inputs = re.findall(r"<input[^>]*fg-delivery-photo-input[^>]*>", panel)
		self.assertEqual(len(inputs), 2, inputs)
		camera, gallery = inputs
		self.assertIn('type="file"', camera)
		self.assertIn('accept="image/*"', camera)
		self.assertIn('capture="environment"', camera)
		self.assertIn('accept="image/*"', gallery)
		self.assertNotIn("capture", gallery)
		self.assertIn('__("TOMAR FOTO")', panel)
		self.assertIn('__("SUBIR FOTO")', panel)

	def test_photo_is_oriented_resized_jpeg_before_sending(self):
		prepare = _block(self.js, "async function prepare_delivery_photo(file) {")
		self.assertIn('imageOrientation: "from-image"', prepare)
		self.assertIn("DELIVERY_PHOTO_MAX_SIDE / Math.max(width, height)", prepare)
		self.assertIn('canvas_to_blob(canvas, "image/jpeg", DELIVERY_PHOTO_QUALITY)', prepare)
		self.assertIn("const DELIVERY_PHOTO_MAX_SIDE = 1600;", self.js)
		self.assertIn("const DELIVERY_PHOTO_QUALITY = 0.8;", self.js)

	def test_photo_preview_and_object_urls_released(self):
		selected = _block(self.js, "on_delivery_photo_selected(input) {")
		self.assertIn("URL.revokeObjectURL(state.photo_url)", selected)
		self.assertIn("state.photo_url = URL.createObjectURL(blob)", selected)
		preview = _block(self.js, "render_delivery_photo_preview() {")
		self.assertIn('<img class="fg-delivery-photo-img" src="${state.photo_url}"', preview)
		reset = _block(self.js, "reset_delivery_state() {")
		self.assertIn("URL.revokeObjectURL(this._delivery.photo_url)", reset)
		self.assertIn("URL.revokeObjectURL(this._delivery.signature_url)", reset)

	# =====================================================================
	# Firma
	# =====================================================================

	def test_signature_pad_pointer_events_and_dpr(self):
		pad = _code(_block(self.js, "function create_signature_pad(canvas, on_change) {"))
		for event in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
			self.assertIn(f'canvas.addEventListener("{event}"', pad)
			self.assertIn(f'canvas.removeEventListener("{event}"', pad)
		self.assertIn("window.devicePixelRatio", pad)
		self.assertIn("setPointerCapture", pad)
		self.assertIn('ctx.fillStyle = "#ffffff"', pad)
		self.assertIn('canvas_to_blob(canvas, "image/png")', pad)
		self.assertNotIn("touchstart", pad)
		self.assertNotIn("jSignature", self.js)

		canvas_css = _css_rule(self.css, ".fg-recorridos .fg-signature-canvas")
		self.assertIn("touch-action: none", canvas_css)
		self.assertIn("background: #ffffff", canvas_css)

	def test_signature_ui_clear_accept_redo(self):
		area = _block(self.js, "render_signature_area() {")
		self.assertIn('__("FIRMA DEL CLIENTE")', _block(self.js, "render_delivery_panel() {"))
		self.assertIn('__("LIMPIAR")', area)
		self.assertRegex(area, r'fg-signature-accept-btn" disabled>')
		self.assertIn('__("ACEPTAR FIRMA")', area)
		self.assertIn('__("FIRMAR DE NUEVO")', area)
		self.assertIn('<img class="fg-signature-preview" src="${state.signature_url}"', area)
		accept = _block(self.js, "accept_signature() {")
		self.assertLess(accept.index("pad.is_empty()"), accept.index("pad.to_blob()"))

	def test_signature_pad_behaviour_in_node(self):
		script = (
			"const window = {devicePixelRatio: 2, addEventListener(){}, removeEventListener(){}};\n"
			"const SIGNATURE_MIN_POINTS = 5;\n"
			+ self._helpers("canvas_to_blob", "create_signature_pad")
			+ """
const handlers = {};
const ctx = new Proxy({}, {get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => ((t[k] = v), true)});
const canvas = {
	width: 0, height: 0,
	getContext: () => ctx,
	getBoundingClientRect: () => ({left: 0, top: 0, width: 300, height: 150}),
	addEventListener: (n, f) => (handlers[n] = f),
	removeEventListener: (n) => delete handlers[n],
	setPointerCapture() {},
};
let changes = 0;
const pad = create_signature_pad(canvas, () => changes++);
const ev = (x, y) => ({clientX: x, clientY: y, pointerId: 1, preventDefault() {}});
const out = {size: [canvas.width, canvas.height], empty_start: pad.is_empty()};
handlers.pointerdown(ev(10, 10)); handlers.pointerup();
out.after_tap = pad.is_empty();
handlers.pointerdown(ev(10, 10));
for (let i = 1; i <= 6; i++) handlers.pointermove(ev(10 + i * 10, 10 + i * 5));
handlers.pointerup();
out.after_stroke = pad.is_empty();
pad.clear();
out.after_clear = pad.is_empty();
out.changes = changes;
pad.destroy();
out.listeners_left = Object.keys(handlers).length;
process.stdout.write(JSON.stringify(out));
"""
		)
		result = self._node(script)
		self.assertEqual(result["size"], [600, 300])  # CSS box x devicePixelRatio
		self.assertTrue(result["empty_start"])
		self.assertTrue(result["after_tap"])  # a single tap is not a signature
		self.assertFalse(result["after_stroke"])
		self.assertTrue(result["after_clear"])
		self.assertEqual(result["changes"], 3)
		self.assertEqual(result["listeners_left"], 0)

	# =====================================================================
	# Confirmar
	# =====================================================================

	def test_confirm_disabled_until_photo_and_signature(self):
		panel = _block(self.js, "render_delivery_panel() {")
		self.assertRegex(panel, r'fg-delivery-confirm-btn" disabled>')
		self.assertIn('__("CONFIRMAR ENTREGA")', panel)
		update = _block(self.js, "update_delivery_confirm_state() {")
		self.assertIn("delivery_evidence_ready(state)", update)

		result = self._node(
			self._ready_harness()
			+ """
const b = {};
const ok = {photo_blob: b, signature_blob: b, payment_status: "Crédito"};
process.stdout.write(JSON.stringify([
	delivery_evidence_ready(null),
	delivery_evidence_ready({photo_blob: b, payment_status: "Pagado"}),
	delivery_evidence_ready({signature_blob: b, payment_status: "Pagado"}),
	delivery_evidence_ready({photo_blob: b, signature_blob: b}),
	delivery_evidence_ready({...ok, payment_status: "Otro"}),
	delivery_evidence_ready(ok),
	delivery_evidence_ready({...ok, submitting: true}),
	delivery_evidence_ready({...ok, processing_photo: true}),
	delivery_evidence_ready({...ok, processing_proof: true}),
	delivery_evidence_ready({...ok, has_issues: true, issues_text: "   "}),
	delivery_evidence_ready({...ok, has_issues: true, issues_text: "Faltó 1 galón"}),
	delivery_evidence_ready({...ok, payment_status: "Pagado"}),
	delivery_evidence_ready({...ok, payment_status: "Pagado", proof_blob: b}),
	delivery_evidence_ready({...ok, payment_note: ""}),
]));
"""
		)
		# photo + signature + payment status required; faltantes detail only
		# when SÍ; proof and notes never required.
		self.assertEqual(
			result, [False, False, False, False, False, True, False, False, False, False, True, True, True, True]
		)

	def test_submit_uses_formdata_fetch_with_csrf_not_frappe_call(self):
		submit = _block(self.js, "submit_delivery() {")
		self.assertIn("if (!delivery_evidence_ready(state)) return;", submit)
		self.assertIn("state.submitting = true;", submit)
		self.assertIn("new FormData()", submit)
		for key in (
			"route_name",
			"stop_name",
			"notes",
			"photo",
			"signature",
			"has_delivery_issues",
			"delivery_issues",
			"payment_status",
			"payment_note",
			"payment_proof",
		):
			self.assertIn(f'form.append("{key}"', submit)
		self.assertIn('form.append("has_delivery_issues", state.has_issues ? "1" : "0");', submit)
		self.assertIn('if (state.has_issues) form.append("delivery_issues"', submit)
		proof_branch = _block(submit, "if (state.payment_status === PAYMENT_STATUS_PAID && state.proof_blob) {")
		self.assertIn('form.append("payment_proof", state.proof_blob, "comprobante.jpg");', proof_branch)
		self.assertEqual(submit.count('form.append("payment_proof"'), 1)
		for forbidden in ("payment_proof_url", "file_url", "delivered_by", "delivered_on"):
			self.assertNotIn(forbidden, submit)
		for forbidden in ("file_url", '"status"', "delivered_on", "delivered_by", '"customer"', '"pick_list"'):
			self.assertNotIn(f"form.append({forbidden}", submit)
		self.assertIn('post_multipart("fabergray_erp.api.recorridos.deliver_stop", form)', submit)
		self.assertNotIn("frappe.call(", submit)
		self.assertNotIn('this.call("deliver_stop"', self.js)

		post = _block(self.js, "function post_multipart(method, form_data) {")
		self.assertIn("fetch(`/api/method/${method}`", post)
		self.assertIn('method: "POST"', post)
		self.assertIn('"X-Frappe-CSRF-Token": frappe.csrf_token', post)
		self.assertIn('credentials: "same-origin"', post)

	def test_success_refreshes_detail_and_returns_to_stop(self):
		submit = _block(self.js, "submit_delivery() {")
		self.assertIn('.then(() => this.call("get_route_detail", { route_name: route_name }))', submit)
		success = _block(submit, ".then((detail) => {")
		self.assertIn("this.reset_delivery_state();", success)
		self.assertIn("this.active_route = detail;", success)
		self.assertIn('this.active_panel = "stop";', success)
		self.assertIn("this.render_active_route();", success)
		self.assertNotIn("location.reload", self.js)

	def test_failure_keeps_evidence_for_retry(self):
		submit = _block(self.js, "submit_delivery() {")
		failure = _block(submit, ".catch(() => {")
		self.assertNotIn("reset_delivery_state", failure)
		self.assertNotIn("photo_blob", failure)
		self.assertIn("state.submitting = false;", _block(submit, ".finally(() => {"))
		close = _block(self.js, "close_delivery_panel() {")
		self.assertNotIn("reset_delivery_state", close)

	def test_next_stop_advances_after_delivery_and_last_stop_message(self):
		result = self._node(
			"const cint = (v) => parseInt(v, 10) || 0;\n"
			+ self._helpers("current_stop_of")
			+ """
const s = (n, seq, status) => ({name: n, sequence: seq, status});
process.stdout.write(JSON.stringify([
	current_stop_of([s("p1", 1, "Pendiente"), s("p2", 2, "Pendiente")]).name,
	current_stop_of([s("p1", 1, "Entregado"), s("p2", 2, "Pendiente")]).name,
	current_stop_of([s("p1", 1, "Entregado"), s("p2", 2, "Entregado")]),
]));
"""
		)
		self.assertEqual(result, ["p1", "p2", None])
		render = _block(self.js, "render_active_route() {")
		self.assertIn('__("TODAS LAS PARADAS FUERON PROCESADAS")', render)
		# The route is never finished from the page in this phase.
		for forbidden in ("complete_route", "finish_route", "completed_on"):
			self.assertNotIn(forbidden, self.js)

	# =====================================================================
	# Fase 26.3 (extensión) -- faltantes / pago / comprobante / dirección
	# =====================================================================

	def test_panel_section_order(self):
		panel = _block(self.js, "render_delivery_panel() {")
		order = [
			'__("ENTREGAR PEDIDO")',
			'__("Cliente")',
			'__("Pedido")',
			'__("Dirección")',
			'__("FOTO DE ENTREGA")',
			'__("FIRMA DEL CLIENTE")',
			"fg-delivery-issues",
			"fg-delivery-payment",
			'__("OBSERVACIONES DE ENTREGA")',
			'__("CONFIRMAR ENTREGA")',
		]
		positions = [panel.index(needle) for needle in order]
		self.assertEqual(positions, sorted(positions), order)
		payment = _block(self.js, "render_payment_section() {")
		self.assertLess(payment.index('__("COMPROBANTE DE PAGO")'), payment.index('__("OBSERVACIÓN DEL PAGO")'))
		self.assertIn("this.render_issues_section();", panel)
		self.assertIn("this.render_payment_section();", panel)

	def test_issues_default_no_and_textarea_only_for_si(self):
		open_panel = _block(self.js, "open_delivery_panel(stop_name) {")
		self.assertIn("has_issues: false,", open_panel)
		issues = _block(self.js, "render_issues_section() {")
		self.assertIn('__("FALTANTES / CAMBIOS")', issues)
		self.assertIn('__("¿Quedó algún faltante, cambio o pendiente del pedido?")', issues)
		self.assertIn('data-issues="0"', issues)
		self.assertIn('data-issues="1"', issues)
		self.assertIn('__("NO")', issues)
		self.assertIn('__("SÍ")', issues)
		textarea_branch = issues[issues.index("state.has_issues\n") :]
		self.assertIn('__("DETALLE DE FALTANTES / CAMBIOS")', textarea_branch)
		self.assertIn("Ej: faltó 1 galón, cliente solicita cambio de producto...", textarea_branch)
		self.assertIn('if (!has_issues) state.issues_text = "";', issues)
		self.assertIn("this.update_delivery_confirm_state();", _block(issues, '.on("input", (e) => {'))

	def test_payment_status_buttons_required_no_default(self):
		open_panel = _block(self.js, "open_delivery_panel(stop_name) {")
		self.assertIn("payment_status: null,", open_panel)
		payment = _block(self.js, "render_payment_section() {")
		self.assertIn('__("ESTADO DEL PAGO")', payment)
		self.assertIn('class="fg-choice', payment)
		self.assertIn("PAYMENT_STATUS_OPTIONS.map(", payment)
		self.assertNotIn("<select", payment)
		result = self._node(self._ready_harness() + "process.stdout.write(JSON.stringify(PAYMENT_STATUS_OPTIONS.map((o) => [o.value, o.label])));")
		self.assertEqual(
			result,
			[["Pagado", "PAGADO"], ["Pendiente por Pago", "PENDIENTE POR PAGO"], ["Crédito", "CRÉDITO"]],
		)
		options = self._const("PAYMENT_STATUS_OPTIONS")
		self.assertIn("Ej: cliente indica que realizará la transferencia mañana.", options)
		self.assertIn("Ej: factura a crédito 30 días.", options)

	def test_proof_only_for_pagado_and_cleared_when_switching(self):
		payment = _block(self.js, "render_payment_section() {")
		paid_branch = payment[payment.index("is_paid\n") : payment.index("selected\n")]
		inputs = re.findall(r"<input[^>]*fg-delivery-proof-input[^>]*>", paid_branch)
		self.assertEqual(len(inputs), 2)
		self.assertIn('capture="environment"', inputs[0])
		self.assertNotIn("capture", inputs[1])
		self.assertIn('__("COMPROBANTE DE PAGO")', paid_branch)
		self.assertIn('__("(opcional)")', paid_branch)
		self.assertEqual(len(re.findall(r"<input[^>]*fg-delivery-proof-input", payment)), 2)

		select = _block(self.js, "select_payment_status(value) {")
		self.assertIn("if (value !== PAYMENT_STATUS_PAID) this.clear_payment_proof();", select)
		clear = _block(self.js, "clear_payment_proof() {")
		self.assertIn("URL.revokeObjectURL(state.proof_url)", clear)
		self.assertIn("state.proof_blob = null;", clear)

		selected_proof = _block(self.js, "on_payment_proof_selected(input) {")
		self.assertIn("state.payment_status !== PAYMENT_STATUS_PAID) return;", selected_proof)
		self.assertIn("prepare_delivery_photo(file)", selected_proof)
		self.assertIn("URL.revokeObjectURL(this._delivery.proof_url)", _block(self.js, "reset_delivery_state() {"))

	def test_address_helper_strips_br_and_prevents_xss(self):
		result = self._node(
			"const __ = (s) => s;\n"
			+ self._const("HTML_ENTITIES")
			+ "\n"
			+ self._helpers("escape_text", "address_lines", "address_html", "address_text")
			+ """
process.stdout.write(JSON.stringify([
	address_html("Calle 10 # 5-20<br>Bucaramanga<br/>Santander<BR />Colombia<br>"),
	address_text("Calle 10<br>Bucaramanga<br>"),
	address_text("Calle 10<br>Bucaramanga", "\\n"),
	address_html('<img src=x onerror="alert(1)">Calle 1<br><script>alert(2)</script>Girón'),
	address_html("&lt;script&gt;alert(3)&lt;/script&gt;<br>Cra 15 &amp; 20"),
	address_html(""),
	address_html(null),
	address_html("  Calle   1  <br>  <br> Bucaramanga "),
]));
"""
		)
		self.assertEqual(result[0], "Calle 10 # 5-20<br>Bucaramanga<br>Santander<br>Colombia")
		self.assertEqual(result[1], "Calle 10, Bucaramanga")
		self.assertEqual(result[2], "Calle 10\nBucaramanga")
		self.assertEqual(result[3], "Calle 1<br>alert(2)Girón")
		self.assertNotIn("<img", result[3])
		self.assertNotIn("<script", result[3])
		self.assertEqual(result[4], "&lt;script&gt;alert(3)&lt;/script&gt;<br>Cra 15 &amp; 20")
		self.assertEqual(result[5], "Sin dirección registrada")
		self.assertEqual(result[6], "Sin dirección registrada")
		self.assertEqual(result[7], "Calle 1<br>Bucaramanga")

	def test_every_address_display_goes_through_the_helper(self):
		code = _code(self.js)
		self.assertNotRegex(code, r"escape_html\(\s*\w+\.address_display")
		self.assertNotRegex(code, r"\$\{\s*\w+\.address_display\s*\}")
		uses = re.findall(r"\w+\.address_display", code)
		helper_uses = re.findall(r"address_(?:html|text)\(\w+\.address_display", code)
		# Every read of address_display is wrapped by the helper (the only
		# other mention is the configure-location field's own fieldname).
		self.assertEqual(len(uses), len(helper_uses), uses)
		for method in (
			"render_active_stop_html(stop, position, total) {",
			"render_active_route() {",
			"render_delivery_panel() {",
		):
			self.assertIn("address_html(", _block(self.js, method), method)

	# =====================================================================
	# Responsive / mobile
	# =====================================================================

	def test_delivery_css_mobile(self):
		confirm_bar = _css_rule(self.css, ".fg-recorridos .fg-delivery-confirm-bar")
		self.assertIn("position: sticky", confirm_bar)
		self.assertIn("bottom: 0", confirm_bar)
		self.assertIn("safe-area-inset-bottom", confirm_bar)
		self.assertGreaterEqual(_min_height(_css_rule(self.css, ".fg-recorridos .fg-delivery-confirm-btn")), 44)
		self.assertIn("width: 100%", _css_rule(self.css, ".fg-recorridos .fg-delivery-confirm-btn"))
		self.assertGreaterEqual(_min_height(_css_rule(self.css, ".fg-recorridos .fg-active-route-deliver-btn")), 44)
		photo_btn = _css_rule(self.css, ".fg-recorridos .fg-delivery-photo-btn,\n.fg-recorridos .fg-signature-actions .fg-btn")
		self.assertGreaterEqual(_min_height(photo_btn), 44)
		self.assertIn("width: 100%", _css_rule(self.css, ".fg-recorridos .fg-signature-canvas"))
		fase = self.css[self.css.index("Fase 26.3 -- ENTREGAR PEDIDO") :]
		self.assertIn("@media (max-width: 640px)", fase)
		self.assertNotIn("<table", _block(self.js, "render_delivery_panel() {"))
		self.assertIn('maxlength="${DELIVERY_NOTES_MAX_LENGTH}"', _block(self.js, "render_delivery_panel() {"))
