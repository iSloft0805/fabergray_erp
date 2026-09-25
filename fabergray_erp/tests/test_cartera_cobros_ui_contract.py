# -*- coding: utf-8 -*-
"""Fase 27.3 -- UI contract for REGISTRAR COBRO and the driver payment
validation inside Page Cartera (page/cartera cartera.js/.css).

Same approach as the 27.2 contract: source checks plus the pure helpers
EXECUTED with node (money input parsing, live summary, form readiness,
UUID, panels, confirm/reject blocks, history), fed with hostile strings.
Node-backed tests skip if node is missing."""

import json
import re
import subprocess
import unittest

from frappe.tests import IntegrationTestCase

from fabergray_erp import cartera_service

# Module import (never the TestCase class: the runner would re-run it here).
from fabergray_erp.tests import test_cartera_ui_contract as ui27

_css_rule = ui27._css_rule
_item = ui27._item

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

XSS = "<img src=x onerror=alert(1)>"


def _detail(**overrides):
	d = _item(
		today="2026-09-25",
		paid_on=None,
		delivered_by="driver@example.com",
		delivered_by_name="Juan Conductor",
		driver_payment_note=None,
		has_driver_proof=False,
		delivery_issues=None,
		invoice_issuer=None,
		payments=[],
		payment_verified_by=None,
		payment_verified_by_name=None,
		payment_verified_on=None,
		payment_rejection_reason=None,
		can_register_payment=True,
		can_confirm_driver_payment=False,
		can_reject_driver_report=False,
		driver_report_has_payment=False,
		invoice_amount=1000000,
		outstanding_amount=1000000,
	)
	d.update(overrides)
	return d


def _form(**overrides):
	form = {
		"request_id": "8f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b",
		"amount_text": "",
		"payment_date": "2026-09-25",
		"payment_method": None,
		"reference": "",
		"notes": "",
		"proof_blob": None,
		"proof_url": None,
		"proof_processing": False,
		"submitting": False,
	}
	form.update(overrides)
	return form


class TestCarteraCobrosUIContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = ui27._read("cartera.js")
		cls.css = ui27._read("cartera.css")
		cls.code = ui27._code(cls.js)

	def _node(self, body):
		if not ui27._NODE:
			raise unittest.SkipTest("node not available")
		script = ui27._NODE_PRELUDE + self.js + "\n;(function(){\n" + body + "\n})();"
		out = subprocess.run([ui27._NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
		return json.loads(out.stdout)

	def _html(self, d, **ui):
		return self._node(f"console.log(JSON.stringify(render_detail_html({json.dumps(d)}, {json.dumps(ui)})));")

	# =====================================================================
	# Money input + live summary (node)
	# =====================================================================

	def test_money_input_parsing_is_exact(self):
		out = self._node(
			"console.log(JSON.stringify(["
			"parse_money_input('400.000'), parse_money_input('400000'), parse_money_input('1.250.000,50'),"
			"parse_money_input('$ 1.000'), parse_money_input(''), parse_money_input(null),"
			"String(parse_money_input('12,345')), String(parse_money_input('1.2')), String(parse_money_input('abc')),"
			"String(parse_money_input('-5')), amount_to_wire(400000), amount_to_wire(1250000.5), amount_to_wire(0.07),"
			"format_money_input(1250000.5), format_money_input(600000)]));"
		)
		self.assertEqual(
			out,
			[400000, 400000, 1250000.5, 1000, None, None, "NaN", "NaN", "NaN", "NaN", "400000", "1250000.50", "0.07", "1.250.000,50", "600.000"],
		)

	def test_live_summary(self):
		d = _detail()
		out = self._node(
			"const d = " + json.dumps(d) + ";"
			"console.log(JSON.stringify(["
			"payment_summary(d, {amount_text: '400.000'}), payment_summary(d, {amount_text: '1.000.000'}),"
			"payment_summary(d, {amount_text: '1.000.001'}), payment_summary(d, {amount_text: ''}),"
			"payment_summary(d, {amount_text: '1,999'}), payment_summary_html(d, {amount_text: '400.000'}),"
			"payment_summary_html(d, {amount_text: '1.000.000'})]));"
		)
		self.assertEqual(out[0], {"kind": "parcial", "amount": 400000, "after": 600000, "outstanding": 1000000})
		self.assertEqual(out[1]["kind"], "total")
		self.assertEqual(out[1]["after"], 0)
		self.assertEqual(out[2]["kind"], "excede")
		self.assertEqual(out[3]["kind"], "vacio")
		self.assertEqual(out[4]["kind"], "invalido")
		for text in ("SALDO ACTUAL", "$ 1.000.000", "VALOR RECIBIDO", "$ 400.000", "SALDO DESPUÉS", "$ 600.000", "ABONO PARCIAL"):
			self.assertIn(text, out[5])
		self.assertIn("PAGO TOTAL", out[6])
		self.assertIn("$ 0", out[6])

	def test_form_readiness(self):
		d = _detail()
		ok = _form(amount_text="400.000", payment_method="Efectivo")
		cases = [
			(ok, True),
			(_form(amount_text="1.000.001", payment_method="Efectivo"), False),  # > saldo
			(_form(amount_text="", payment_method="Efectivo"), False),
			(_form(amount_text="400.000"), False),  # sin medio
			(_form(amount_text="400.000", payment_method="Bitcoin"), False),
			(_form(amount_text="400.000", payment_method="Efectivo", payment_date="2026-09-26"), False),  # futuro
			(_form(amount_text="400.000", payment_method="Efectivo", payment_date=""), False),
			(_form(amount_text="400.000", payment_method="Efectivo", reference="x" * 141), False),
			(_form(amount_text="400.000", payment_method="Efectivo", notes="x" * 501), False),
			(_form(amount_text="400.000", payment_method="Efectivo", submitting=True), False),  # doble click
			(_form(amount_text="400.000", payment_method="Efectivo", proof_processing=True), False),
			(_form(amount_text="400.000", payment_method="Efectivo", payment_date="2026-01-01"), True),  # antes de la entrega
		]
		script = "const d = " + json.dumps(d) + "; console.log(JSON.stringify([" + ",".join(
			f"pay_form_ready(d, {json.dumps(f)})" for f, _ok in cases
		) + "]));"
		self.assertEqual(self._node(script), [expected for _f, expected in cases])
		# Methods mirror the server list exactly.
		methods = re.findall(r'\{ value: "([^"]+)", label: __\("([^"]+)"\) \}', self.js)
		self.assertEqual([m for m, _l in methods], list(cartera_service.PAYMENT_METHODS))
		self.assertEqual([l for _m, l in methods], ["TRANSFERENCIA", "EFECTIVO", "CONSIGNACIÓN", "OTRO"])
		for const, value in (
			("REFERENCE_MAX_LENGTH", cartera_service.REFERENCE_MAX_LENGTH),
			("NOTES_MAX_LENGTH", cartera_service.NOTES_MAX_LENGTH),
			("REASON_MIN_LENGTH", cartera_service.REASON_MIN_LENGTH),
			("REASON_MAX_LENGTH", cartera_service.REASON_MAX_LENGTH),
		):
			self.assertIn(f"const {const} = {value};", self.code)

	def test_request_id_is_one_uuid_per_form_kept_on_retry(self):
		out = self._node(
			"const a = new_pay_form({today: '2026-09-25'}); const b = new_pay_form({today: '2026-09-25'});"
			"const saved = crypto.randomUUID; Object.defineProperty(crypto, 'randomUUID', {value: undefined, configurable: true}); const c = new_request_id(); Object.defineProperty(crypto, 'randomUUID', {value: saved, configurable: true});"
			"console.log(JSON.stringify([a.request_id, b.request_id, c, a.payment_date]));"
		)
		uuid_re = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
		for value in out[:3]:
			self.assertRegex(value, uuid_re)
		self.assertNotEqual(out[0], out[1])
		self.assertEqual(out[3], "2026-09-25")
		# The UUID is created ONLY when a new form opens, sent as-is, and a
		# failure keeps the same form (and id) for the retry.
		self.assertEqual(len(re.findall(r"new_request_id\(\)", self.code)), 1 + 1)  # definition use + new_pay_form
		submit = self.code[self.code.index("submit_payment() {") : self.code.index("\tconfirm_driver_payment() {")]
		self.assertIn('data.append("client_request_id", form.request_id)', submit)
		self.assertIn("if (!form || form.submitting || !pay_form_ready(d, form)) return;", submit)
		self.assertIn("form.submitting = true;", submit)
		catch_block = submit[submit.index(".catch(") :]
		self.assertNotIn("new_pay_form", catch_block)
		self.assertNotIn("request_id =", catch_block)
		self.assertIn("form.submitting = false;", catch_block)
		self.assertIn('post_multipart("fabergray_erp.api.cartera.register_payment", data)', submit)
		self.assertIn('"X-Frappe-CSRF-Token": frappe.csrf_token', self.code)
		# Only these fields are sent -- never company/source/recorded_by/status.
		sent = set(re.findall(r'data\.append\("([a-z_]+)"', submit))
		self.assertEqual(
			sent,
			{"obligation_name", "amount", "payment_date", "payment_method", "reference", "notes", "client_request_id", "payment_proof"},
		)

	# =====================================================================
	# Panels (node)
	# =====================================================================

	def test_register_button_and_in_page_panel(self):
		html = self._html(_detail())
		self.assertIn("fg-cartera-open-pay", html)
		self.assertIn("REGISTRAR COBRO", html)
		self.assertNotIn("fg-cartera-pay-panel", html)

		panel = self._html(_detail(), pay_form=_form(amount_text="400.000", payment_method="Efectivo", reference=XSS, notes=XSS))
		for text in (
			"fg-cartera-pay-panel",
			"VALOR RECIBIDO",
			"PAGO TOTAL",
			"SALDO ACTUAL",
			"SALDO DESPUÉS",
			"ABONO PARCIAL",
			"FECHA",
			'type="date"',
			'max="2026-09-25"',
			"MEDIO DE PAGO",
			"TRANSFERENCIA",
			"EFECTIVO",
			"CONSIGNACIÓN",
			"OTRO",
			'aria-pressed="true"',
			"REFERENCIA",
			'maxlength="140"',
			"COMPROBANTE (OPCIONAL)",
			"SUBIR COMPROBANTE",
			'capture="environment"',
			'accept="image/jpeg,image/png,image/webp"',
			"OBSERVACIONES",
			'maxlength="500"',
			"sin contabilizar",
			"CANCELAR",
		):
			self.assertIn(text, panel)
		self.assertNotIn("<img src=x", panel)
		self.assertIn('inputmode="decimal"', panel)
		# The submit button is disabled until the form is valid.
		self.assertRegex(
			self._html(_detail(), pay_form=_form(amount_text="2.000.000", payment_method="Efectivo")),
			r'fg-cartera-pay-submit" disabled',
		)
		self.assertNotRegex(panel, r'fg-cartera-pay-submit" disabled')
		# Panel inside the Page, never a dialog.
		open_pay = self.code[self.code.index("open_pay_form() {") : self.code.index("close_pay_form() {")]
		self.assertNotIn("Dialog", open_pay)
		self.assertNotIn("Dialog", self.code[self.code.index("function render_payment_panel_html(") :][:4000])

		for blocked in (
			_detail(can_register_payment=False, outstanding_amount=0, status="Pagado", bucket="pagado"),
			_detail(can_register_payment=False, amount_available=False),
		):
			self.assertNotIn("fg-cartera-open-pay", self._html(blocked))

	def test_driver_payment_confirm_and_reject(self):
		d = _detail(
			status="Pagado",
			bucket="pagado",
			paid_amount=1000000,
			outstanding_amount=0,
			payment_verification="Sin confirmar",
			por_confirmar=True,
			driver_payment_status="Pagado",
			has_driver_proof=True,
			can_register_payment=False,
			can_confirm_driver_payment=True,
			can_reject_driver_report=True,
			driver_report_has_payment=True,
		)
		html = self._html(d)
		for text in ("PAGO REPORTADO POR CONDUCTOR", "$ 1.000.000", "VER COMPROBANTE", "CONFIRMAR PAGO", "RECHAZAR PAGO", 'data-kind="payment"'):
			self.assertIn(text, html)
		self.assertNotIn("REGISTRAR COBRO", html)

		panel = self._html(d, reject_form={"kind": "payment", "reason": ""})
		self.assertIn("EL SALDO VOLVERÁ A $ 1.000.000", panel)
		self.assertIn("MOTIVO DEL RECHAZO (OBLIGATORIO)", panel)
		self.assertRegex(panel, r'fg-cartera-reject-submit" disabled')
		self.assertNotIn("fg-cartera-confirm-btn", panel)  # one decision at a time
		ready = self._html(d, reject_form={"kind": "payment", "reason": "No llegó el dinero"})
		self.assertNotRegex(ready, r'fg-cartera-reject-submit" disabled')
		busy = self._html(d, busy=True)
		self.assertRegex(busy, r"fg-cartera-confirm-btn\" disabled")

		# Explicit confirmation before confirming.
		confirm = self.code[self.code.index("confirm_driver_payment() {") : self.code.index("open_reject_form(kind) {")]
		self.assertIn('frappe.confirm(__("¿CONFIRMAS QUE CARTERA RECIBIÓ {0}?"', confirm)
		self.assertIn('this.run_action("confirm_driver_payment"', confirm)
		out = self._node(
			"console.log(JSON.stringify([reject_form_ready({reason: ' abc '}), reject_form_ready({reason: 'abcde'}),"
			"reject_form_ready({reason: 'x'.repeat(501)})]));"
		)
		self.assertEqual(out, [False, True, False])

	def test_paid_without_proof_only_offers_reject_report(self):
		d = _detail(
			status="Por Validar",
			bucket="por_validar",
			payment_verification="Sin confirmar",
			por_confirmar=False,
			driver_payment_status="Pagado",
			can_register_payment=False,
			can_reject_driver_report=True,
		)
		html = self._html(d)
		self.assertIn("PAGO REPORTADO SIN COMPROBANTE", html)
		self.assertIn("Debe rechazarse el reporte antes de registrar un cobro real.", html)
		self.assertIn("RECHAZAR REPORTE", html)
		self.assertIn('data-kind="report"', html)
		self.assertNotIn("CONFIRMAR PAGO", html)
		self.assertNotIn("fg-cartera-open-pay", html)
		panel = self._html(d, reject_form={"kind": "report", "reason": ""})
		self.assertIn("LA OBLIGACIÓN QUEDARÁ PENDIENTE POR $ 1.000.000", panel)

	def test_verification_audit_and_history(self):
		confirmed = self._html(
			_detail(
				payment_verification="Confirmado",
				payment_verified_by_name=XSS,
				payment_verified_on="2026-09-25 10:30:00",
				can_register_payment=False,
			)
		)
		self.assertIn("PAGO DEL CONDUCTOR CONFIRMADO", confirmed)
		self.assertIn("25-09-2026 10:30", confirmed)
		rejected = self._html(
			_detail(
				payment_verification="Rechazado",
				payment_verified_by_name="Ana Cartera",
				payment_verified_on="2026-09-25 11:00:00",
				payment_rejection_reason=XSS,
				payments=[
					{
						"name": "CPAG-1",
						"payment_date": "2026-09-25",
						"amount": 1000000,
						"currency": "COP",
						"payment_method": None,
						"source": "Conductor",
						"reference": None,
						"notes": None,
						"accounting_status": "Sin contabilizar",
						"recorded_by": "d@x.com",
						"recorded_by_name": "Juan",
						"recorded_on": "2026-09-25 09:00:00",
						"cancelled": True,
						"cancellation_reason": XSS,
						"cancelled_by_name": "Ana Cartera",
						"cancelled_on": "2026-09-25 11:00:00",
						"has_payment_proof": True,
						"proof_is_driver_proof": True,
						"proof_kind": "driver",
					},
					{
						"name": 'CPAG-2"><b>x',
						"payment_date": "2026-09-25",
						"amount": 400000,
						"currency": "COP",
						"payment_method": "Efectivo",
						"source": "Cartera",
						"reference": XSS,
						"notes": None,
						"accounting_status": "Sin contabilizar",
						"recorded_by": "c@x.com",
						"recorded_by_name": "Ana Cartera",
						"recorded_on": "2026-09-25 12:00:00",
						"cancelled": False,
						"cancellation_reason": None,
						"cancelled_by_name": None,
						"cancelled_on": None,
						"has_payment_proof": True,
						"proof_is_driver_proof": False,
						"proof_kind": "payment",
					},
				],
			)
		)
		for text in ("REPORTE DEL CONDUCTOR RECHAZADO", "Ana Cartera", "25-09-2026 11:00", "MOTIVO", "ANULADO", "fg-cartera-cancel-info", "fg-cartera-payment-proof-btn", "CARTERA", "CONDUCTOR"):
			self.assertIn(text, rejected)
		for html in (confirmed, rejected):
			self.assertNotIn("<img src=x", html)
			self.assertNotIn('"><b>', html)
		self.assertIn('data-payment="CPAG-2&quot;&gt;&lt;b&gt;x"', rejected)

	# =====================================================================
	# Refresh without reload + proof handling (source)
	# =====================================================================

	def test_actions_refresh_detail_and_kpis_then_list_once(self):
		apply = self.code[self.code.index("apply_action_response(res, message) {") : self.code.index("show_proof(method, args, title, $btn) {")]
		for fragment in ("this.detail = res.detail;", "this.dashboard = res.dashboard;", "this.list_stale = true;", "this.render_detail();"):
			self.assertIn(fragment, apply)
		self.assertNotIn("load_all", apply)
		self.assertNotIn("location.reload", self.code)
		back = self.code[self.code.index("back_to_dashboard() {") : self.code.index("reset_detail_forms() {")]
		self.assertIn("if (this.list_stale) {", back)
		self.assertIn("this.refresh_list();", back)
		self.assertIn("this.list_stale = false;", back)
		self.assertNotIn("fetch_dashboard", back)  # KPIs already came with the action
		for method in ("register_payment", "confirm_driver_payment", "reject_driver_payment"):
			self.assertIn(method, self.code)
		self.assertIn("COBRO REGISTRADO EN CARTERA", self.js)
		self.assertNotIn("PAGO CONTABILIZADO", self.js.upper())

	def test_proof_preview_object_urls_are_released(self):
		self.assertIn("URL.revokeObjectURL(this.pay_form.proof_url)", self.code)
		self.assertIn("if (form.proof_url) URL.revokeObjectURL(form.proof_url);", self.code)
		self.assertIn("URL.createObjectURL(blob)", self.code)
		prep = self.code[self.code.index("async function prepare_payment_proof(file) {") :]
		prep = prep[: prep.index("\n}\n")]
		for fragment in ('imageOrientation: "from-image"', "PROOF_MAX_SIDE", '"image/jpeg"', "source.close"):
			self.assertIn(fragment, prep)

	def test_panel_css_mobile(self):
		css = self.css
		self.assertIn("min-height: var(--fg-touch)", _css_rule(css, ".fg-cartera .fg-cartera-input"))
		self.assertIn("font-size: 16px", _css_rule(css, ".fg-cartera .fg-cartera-input"))
		self.assertIn("min-height: var(--fg-touch)", _css_rule(css, ".fg-cartera .fg-cartera-method"))
		self.assertIn("repeat(2, minmax(0, 1fr))", _css_rule(css, ".fg-cartera .fg-cartera-methods"))
		self.assertIn("flex-direction: column", _css_rule(css, ".fg-cartera .fg-cartera-pay-proof-inputs,\n.fg-cartera .fg-cartera-action-row,\n.fg-cartera .fg-cartera-panel-actions"))
		self.assertIn("max-width: 100%", _css_rule(css, ".fg-cartera .fg-cartera-pay-proof-img"))
		self.assertIn("width: 100%", _css_rule(css, ".fg-cartera .fg-cartera-main-action"))
