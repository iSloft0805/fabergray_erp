# -*- coding: utf-8 -*-
"""Hotfix 25.20.4 -- "APLICAR PRECIOS queda deshabilitado" en Facturación
-> Revisar cotización.

REPORTED SYMPTOM (real UI, real data -- item "disp" at $2.210,00, qty 2):
selecting -25% highlighted correctly, "Ajustado" showed $1.657,50,
"Subtotal estimado" showed $3.315,00 -- and APLICAR PRECIOS still read as
permanently disabled, while "Diferencia" showed $0,00 next to a REAL of
$2.210,00.

TWO INDEPENDENT DEFECTS, both pinned here:

  1. VISUAL (the actual "disabled" the user saw). The button carries
     `class="fg-btn fg-btn--solid-primary ..."`, but every `.fg-btn` rule
     lives in public/css/fg_shell.css scoped under `.fg-shell`, and a
     `frappe.ui.Dialog` modal is appended to <body>, OUTSIDE `.fg-shell`.
     It inherited no background/color/border/cursor and painted as bare
     browser-default chrome -- indistinguishable from disabled. The one
     disabled rule in facturacion.css targeted `.disabled` (a CLASS) while
     facturacion.js emits the `disabled` ATTRIBUTE, so the genuinely
     disabled state was not dimmed either: both states looked identical.

  2. LOGIC (the rule the hotfix brief requires). The enable expression was
     literally `selected_mode ? "" : "disabled"` -- it had no notion of
     "would applying this mode change anything", no eligibility check and
     no busy guard. `has_price_mode_changes()` (api/cotizaciones.py) is
     now the single source of truth, and it compares the PERSISTED
     `rate`/`price_list_rate`/`discount_percentage` of every line against
     what `apply_quotation_price_mode()` would really write -- never the
     `fg_billing_price_mode` audit label, which can disagree with the
     rates and must never win over them.

"Diferencia" ($0,00) was audited and found CORRECT: it is "Actual - Base"
(persisted rate vs current Item Price), never "Actual - Ajustado". Its
formula is untouched; only the column header was disambiguated to "Dif.
vs base". test_n below pins that semantics so it cannot drift.

Same conventions as test_cotizaciones_price_mode.py: `fx.TestWorld`
fixtures, one class per concern, plus a static UI-contract class reading
facturacion.js/facturacion.css as text (no JS test runner in this app).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)
_FACTURACION_CSS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.css"
)

# The exact numbers from the bug report, so this suite reproduces the
# reported case and not merely a similar one.
REPORTED_BASE_RATE = 2210.0
REPORTED_QTY = 2


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_cotizaciones_price_mode.py's own."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


class _PriceModeApplyBase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG25204 Customer")
		cls.vendedora = cls.world.user("fg25204-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg25204-facturacion@example.com", ["Facturación"])

	def _priced_item(self, item_code, rate=REPORTED_BASE_RATE, price_list="Standard Selling"):
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

	def _pending_quotation(self, item_code, qty=REPORTED_QTY):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item_code, "qty": qty}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		return result["name"]

	def _apply(self, name, price_mode):
		with fx.as_user(self.facturacion):
			result = cotizaciones.apply_quotation_price_mode(name, price_mode)
		self.world.track_existing("Quotation", result["name"])
		return result

	def _detail(self, name):
		with fx.as_user(self.facturacion):
			return cotizaciones.get_quotation_billing_detail(name)

	def _changes(self, name):
		"""`{price_mode: bool}` exactly as the dialog receives it."""
		return self._detail(name)["price_mode_changes"]

	def _first_row(self, name):
		return frappe.get_doc("Quotation", name).items[0]


class TestApplyPriceButtonEnableRule(_PriceModeApplyBase):
	"""Brief sections 3/4/5/6 -- when may APLICAR PRECIOS be pressed."""

	# A. FULL persisted + seleccionar -25 habilita Apply
	# This IS the reported case: base 2210, rate 2210, qty 2.
	def test_a_full_persisted_then_selecting_25_enables_apply(self):
		item = self._priced_item("FG25204-A-ITEM")
		name = self._pending_quotation(item.name)

		detail = self._detail(name)
		self.assertTrue(detail["can_apply_price_mode"])
		# The reported screen, reproduced exactly.
		self.assertEqual(flt(detail["items"][0]["reference_rate"]), REPORTED_BASE_RATE)
		self.assertEqual(flt(detail["items"][0]["rate"]), REPORTED_BASE_RATE)
		self.assertEqual(flt(detail["items"][0]["qty"]), REPORTED_QTY)

		changes = detail["price_mode_changes"]
		self.assertTrue(changes["DISCOUNT_25"], "APLICAR PRECIOS must be enabled for -25% on a full-price quotation")
		self.assertTrue(changes["DISCOUNT_10"])
		self.assertTrue(changes["DISCOUNT_15"])
		self.assertTrue(changes["DISCOUNT_20"])
		# ...and disabled only for the mode already persisted.
		self.assertFalse(changes["FULL"])

	# B. -25 preview calcula rate correcto (2210 -> 1657.50)
	def test_b_discount_25_expected_rate_is_the_previewed_one(self):
		item = self._priced_item("FG25204-B-ITEM")
		name = self._pending_quotation(item.name)
		row = self._first_row(name)
		self.assertEqual(
			cotizaciones._expected_price_mode_rate(REPORTED_BASE_RATE, "DISCOUNT_25", row),
			1657.50,
		)
		self.assertEqual(cotizaciones._expected_price_mode_rate(REPORTED_BASE_RATE, "FULL", row), 2210.00)
		self.assertEqual(cotizaciones._expected_price_mode_rate(REPORTED_BASE_RATE, "DISCOUNT_10", row), 1989.00)

	# C. el audit field dice -25 pero el rate sigue en FULL -> Apply DEBE
	# habilitarse. `fg_billing_price_mode` is an AUDIT record; the
	# persisted rates are the economic reality and they win.
	def test_c_stale_audit_field_never_beats_the_persisted_rates(self):
		item = self._priced_item("FG25204-C-ITEM")
		name = self._pending_quotation(item.name)

		# Only the audit label is made inconsistent -- rates untouched,
		# exactly the situation section 4 of the brief describes.
		frappe.db.set_value("Quotation", name, "fg_billing_price_mode", "Descuento 25%", update_modified=False)

		detail = self._detail(name)
		self.assertEqual(detail["fg_billing_price_mode"], "Descuento 25%")
		self.assertEqual(flt(detail["items"][0]["rate"]), REPORTED_BASE_RATE)  # still full price
		self.assertTrue(
			detail["price_mode_changes"]["DISCOUNT_25"],
			"a Quotation labelled -25% whose rate is still the full price has NOT had -25% applied",
		)

	# D. el rate ya corresponde exactamente a -25 -> seleccionar -25 no
	# genera cambio (y sólo entonces queda deshabilitado)
	def test_d_reapplying_the_same_mode_is_not_a_change(self):
		item = self._priced_item("FG25204-D-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		self.assertFalse(self._changes(applied["name"])["DISCOUNT_25"])

	# E. rate -25 + seleccionar -20 habilita Apply
	def test_e_from_25_selecting_20_enables_apply(self):
		item = self._priced_item("FG25204-E-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		self.assertTrue(self._changes(applied["name"])["DISCOUNT_20"])

	# F. rate -25 + seleccionar Precio completo habilita Apply
	def test_f_from_25_selecting_full_enables_apply(self):
		item = self._priced_item("FG25204-F-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		changes = self._changes(applied["name"])
		self.assertTrue(changes["FULL"])
		self.assertTrue(changes["DISCOUNT_10"])
		self.assertTrue(changes["DISCOUNT_15"])
		self.assertTrue(changes["DISCOUNT_20"])

	# Every one of the 5 closed modes is answered, and only those 5 --
	# section 15's own "NO agregar descuentos nuevos".
	def test_price_mode_changes_covers_exactly_the_five_closed_modes(self):
		item = self._priced_item("FG25204-MODES-ITEM")
		name = self._pending_quotation(item.name)
		self.assertEqual(
			set(self._changes(name)),
			{"FULL", "DISCOUNT_10", "DISCOUNT_15", "DISCOUNT_20", "DISCOUNT_25"},
		)

	# A line with no Item Price on the Quotation's own list can never be
	# applied (apply_quotation_price_mode() refuses the whole call, naming
	# the item) -- so the button must not offer a guaranteed error.
	def test_line_without_reference_price_never_enables_apply(self):
		item = self.world.item("FG25204-NOPRICE-ITEM")  # deliberately unpriced
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		self.assertFalse(any(self._changes(result["name"]).values()))

	# A drifted stored base (`price_list_rate` no longer the live Item
	# Price) is a REAL change an apply would write, even when `rate`
	# happens to match.
	def test_drifted_price_list_rate_is_a_real_change(self):
		item = self._priced_item("FG25204-DRIFT-ITEM")
		name = self._pending_quotation(item.name)
		qtn = frappe.get_doc("Quotation", name)
		frappe.db.set_value(
			"Quotation Item", qtn.items[0].name, "price_list_rate", REPORTED_BASE_RATE - 10, update_modified=False
		)
		self.assertTrue(self._changes(name)["FULL"])


class TestApplyPriceButtonEligibility(_PriceModeApplyBase):
	"""Brief sections 3/12 -- state/permission gating, never relaxed."""

	# J. sigue Pendiente de Facturación después de aplicar
	# K. no auto-aprueba
	def test_j_k_apply_keeps_pending_and_never_auto_approves(self):
		item = self._priced_item("FG25204-JK-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		status = frappe.db.get_value("Quotation", applied["name"], "fg_billing_review_status")
		self.assertEqual(status, "Pendiente de Facturación")
		self.assertNotEqual(status, "Aprobada")
		self.assertIsNone(frappe.db.get_value("Quotation", applied["name"], "fg_billing_reviewed_by") or None)

	# I. después del amendment se usa el NUEVO nombre -- y el original,
	# ya cancelado, nunca vuelve a habilitar el botón.
	def test_i_after_amendment_only_the_new_name_is_applicable(self):
		item = self._priced_item("FG25204-I-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		self.assertNotEqual(applied["name"], name)

		original = self._detail(name)
		self.assertFalse(original["can_apply_price_mode"])
		self.assertFalse(any(original["price_mode_changes"].values()))

		amended = self._detail(applied["name"])
		self.assertTrue(amended["can_apply_price_mode"])
		self.assertTrue(amended["price_mode_changes"]["DISCOUNT_20"])

	# Una cotización ya Aprobada nunca habilita APLICAR PRECIOS.
	def test_approved_quotation_never_enables_apply(self):
		item = self._priced_item("FG25204-APPROVED-ITEM")
		name = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			cotizaciones.approve_quotation_billing(name)
		detail = self._detail(name)
		self.assertFalse(detail["can_apply_price_mode"])
		self.assertFalse(any(detail["price_mode_changes"].values()))

	# El frontend no decide autorización: sin rol Facturación no hay
	# payload en absoluto.
	def test_non_facturacion_never_receives_the_enable_payload(self):
		item = self._priced_item("FG25204-PERM-ITEM")
		name = self._pending_quotation(item.name)
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_quotation_billing_detail(name)
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_25")


class TestApplyPricePersistedResult(_PriceModeApplyBase):
	"""Brief sections 7/9/10 -- what actually gets persisted."""

	# H. aplicar -25 persiste el rate esperado (2210 -> 1657.50)
	def test_h_apply_25_persists_the_expected_rate(self):
		item = self._priced_item("FG25204-H-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")
		row = self._first_row(applied["name"])
		self.assertEqual(flt(row.rate), 1657.50)
		self.assertEqual(flt(row.price_list_rate), REPORTED_BASE_RATE)
		self.assertEqual(flt(row.discount_percentage), 25)
		# ...and the dialog now shows it as the "Actual"/real price.
		self.assertEqual(flt(self._detail(applied["name"])["items"][0]["rate"]), 1657.50)

	# G. los descuentos nunca componen -- siempre desde el precio de
	# referencia actual, nunca sobre el rate ya descontado (Commit 25.15).
	def test_g_discounts_never_compound(self):
		item = self._priced_item("FG25204-G-ITEM")
		name = self._pending_quotation(item.name)
		first = self._apply(name, "DISCOUNT_25")
		self.assertEqual(flt(self._first_row(first["name"]).rate), 1657.50)

		second = self._apply(first["name"], "DISCOUNT_10")
		# 10% of the BASE (2210), never 10% of 1657.50 (= 1491.75).
		self.assertEqual(flt(self._first_row(second["name"]).rate), 1989.00)
		self.assertNotEqual(flt(self._first_row(second["name"]).rate), 1491.75)

		third = self._apply(second["name"], "FULL")
		self.assertEqual(flt(self._first_row(third["name"]).rate), REPORTED_BASE_RATE)

	# Section 10 -- volver a -25 después de aplicarlo ya no cambia nada,
	# pero -20 sí. El ciclo completo del brief.
	def test_full_reported_cycle(self):
		item = self._priced_item("FG25204-CYCLE-ITEM")
		name = self._pending_quotation(item.name)

		self.assertTrue(self._changes(name)["DISCOUNT_25"])  # arranca en FULL
		applied = self._apply(name, "DISCOUNT_25")

		changes = self._changes(applied["name"])
		self.assertFalse(changes["DISCOUNT_25"])  # ya no cambiaría nada
		self.assertTrue(changes["DISCOUNT_20"])  # vuelve a habilitarse
		self.assertTrue(changes["FULL"])

	# N. "Diferencia" -- AUDITADA, semántica correcta y sin tocar:
	# `rate_difference` es "Actual - Base", nunca "Actual - Ajustado".
	# $0,00 sobre una cotización a precio completo es el valor VERDADERO.
	def test_n_rate_difference_is_actual_minus_base(self):
		item = self._priced_item("FG25204-N-ITEM")
		name = self._pending_quotation(item.name)

		row = self._detail(name)["items"][0]
		self.assertEqual(flt(row["reference_rate"]), REPORTED_BASE_RATE)
		self.assertEqual(flt(row["rate"]), REPORTED_BASE_RATE)
		# Exactly the reported $0,00 -- and it is correct: nothing is
		# discounted yet, which is WHY the button must be enabled.
		self.assertEqual(flt(row["rate_difference"]), 0.0)
		self.assertTrue(self._detail(name)["price_mode_changes"]["DISCOUNT_25"])

		applied = self._apply(name, "DISCOUNT_25")
		row = self._detail(applied["name"])["items"][0]
		self.assertEqual(flt(row["rate"]), 1657.50)
		self.assertEqual(flt(row["reference_rate"]), REPORTED_BASE_RATE)
		self.assertEqual(flt(row["rate_difference"]), -552.50)

	# O. el descuento comercial nunca se confunde con el IVA: vive en
	# `discount_percentage`/`rate` (base imponible), los impuestos se
	# recalculan nativamente ENCIMA de esa base, nunca al revés.
	def test_o_commercial_discount_is_not_a_tax_adjustment(self):
		item = self._priced_item("FG25204-O-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_25")

		qtn = frappe.get_doc("Quotation", applied["name"])
		row = qtn.items[0]
		self.assertEqual(flt(row.discount_percentage), 25)
		self.assertEqual(flt(row.rate), 1657.50)
		self.assertEqual(flt(row.amount), 1657.50 * REPORTED_QTY)
		# Subtotal (pre-tax) is the discounted base, not a taxed figure.
		self.assertEqual(flt(qtn.total), 1657.50 * REPORTED_QTY)
		# Tax total is a separate field and is never where the discount landed.
		self.assertEqual(
			flt(qtn.grand_total),
			flt(qtn.total) + flt(qtn.total_taxes_and_charges),
		)

	# P. el resto del payload de billing review no sufre regresión.
	def test_p_billing_review_payload_keeps_every_pre_existing_key(self):
		item = self._priced_item("FG25204-P-ITEM")
		name = self._pending_quotation(item.name)
		detail = self._detail(name)
		for key in (
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"owner",
			"owner_fullname",
			"status",
			"fg_billing_review_status",
			"fg_billing_review_note",
			"fg_billing_price_mode",
			"fg_billing_price_adjusted_by",
			"fg_billing_price_adjusted_on",
			"item_count",
			"total_qty",
			"total",
			"total_taxes_and_charges",
			"grand_total",
			"rounded_total",
			"items",
		):
			self.assertIn(key, detail)
		for key in ("item_code", "item_name", "qty", "rate", "amount", "reference_rate", "rate_difference",
					"price_list", "warehouse", "requested_qty", "available_qty", "shortage_qty"):
			self.assertIn(key, detail["items"][0])

	# Section 12 -- computing the enable flags never writes anything.
	def test_reading_the_detail_never_changes_a_price(self):
		item = self._priced_item("FG25204-RO-ITEM")
		name = self._pending_quotation(item.name)
		before = frappe.db.get_value(
			"Quotation Item", {"parent": name}, ["rate", "price_list_rate", "discount_percentage"]
		)
		for _ in range(3):
			self._detail(name)
		after = frappe.db.get_value(
			"Quotation Item", {"parent": name}, ["rate", "price_list_rate", "discount_percentage"]
		)
		self.assertEqual(before, after)


class TestApplyPriceUiContract(IntegrationTestCase):
	"""Brief sections 3/11 (L/M) and the visual root cause -- static
	source contracts, the same technique test_cotizaciones_price_mode.py's
	own TestPriceModeUiContract already established."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_FACTURACION_JS_PATH)
		cls.css = _read(_FACTURACION_CSS_PATH)

	# El criterio anterior (`selected_mode ? "" : "disabled"`) ya no decide
	# el botón: lo decide can_apply_billing_price_mode().
	def test_apply_button_is_gated_by_the_single_enable_rule(self):
		body = _method_body(self.js, "render_billing_price_mode_section")
		self.assertIn("this.can_apply_billing_price_mode(d, selected_mode)", body)
		self.assertIn("can_apply ? \"\" : \"disabled\"", body)
		self.assertNotIn('selected_mode ? "" : "disabled"', body)

	# La regla lee la respuesta del SERVIDOR, nunca el audit field.
	def test_enable_rule_reads_the_server_answer_never_the_audit_field(self):
		body = _method_body(self.js, "can_apply_billing_price_mode")
		self.assertIn("d.price_mode_changes", body)
		self.assertIn("d.can_apply_price_mode", body)
		self.assertNotIn("fg_billing_price_mode", body)

	# L. busy evita doble ejecución
	def test_l_busy_state_prevents_a_double_apply(self):
		enable_body = _method_body(self.js, "can_apply_billing_price_mode")
		self.assertIn("this._billing_review_applying", enable_body)

		apply_body = _method_body(self.js, "apply_billing_price_mode")
		self.assertIn("if (!this.can_apply_billing_price_mode(d, mode)) return;", apply_body)
		self.assertIn("if (this._billing_review_applying) return;", apply_body)
		self.assertIn("this._billing_review_applying = true;", apply_body)

	# M. un error devuelve el botón (nunca queda bloqueado para siempre)
	def test_m_error_path_restores_the_button(self):
		apply_body = _method_body(self.js, "apply_billing_price_mode")
		finally_at = apply_body.index(".finally(")
		finally_block = apply_body[finally_at:]
		self.assertIn("this._billing_review_applying = false;", finally_block)
		self.assertIn("this.set_busy(false);", finally_block)
		self.assertIn("this.render_billing_review_dialog_body();", finally_block)

	# El endpoint y la arquitectura cancel+amend siguen siendo los mismos:
	# ninguna segunda ruta.
	def test_apply_still_uses_the_one_existing_endpoint(self):
		apply_body = _method_body(self.js, "apply_billing_price_mode")
		self.assertIn('call_cotizaciones("apply_quotation_price_mode"', apply_body)
		self.assertEqual(self.js.count('"apply_quotation_price_mode"'), 1)
		self.assertIn("this._billing_review_quotation = result.name", apply_body)

	# ROOT CAUSE VISUAL: el botón ya no depende de `.fg-shell .fg-btn`
	# (fuera de alcance en un modal) -- tiene fondo/color/cursor propios
	# dentro del scope del diálogo.
	def test_apply_button_is_styled_inside_the_dialog_scope(self):
		start = self.css.index(".fg-fact-billing-review-dialog .fg-fact-billing-apply-price-btn {")
		rule = self.css[start : self.css.index("}", start)]
		self.assertIn("background:", rule)
		self.assertIn("color:", rule)
		self.assertIn("cursor: pointer", rule)

	# ...y el estado deshabilitado real (ATRIBUTO `disabled`) ahora sí se
	# ve deshabilitado; antes sólo existía el selector de CLASE `.disabled`.
	def test_disabled_state_matches_the_attribute_the_js_emits(self):
		self.assertIn(
			".fg-fact-billing-review-dialog .fg-fact-billing-apply-price-btn:disabled,\n"
			".fg-fact-billing-review-dialog .fg-fact-billing-apply-price-btn.disabled {",
			self.css,
		)

	# El usuario siempre puede leer POR QUÉ está deshabilitado en el único
	# caso legítimo y no obvio.
	def test_disabled_because_already_applied_is_explained(self):
		body = _method_body(self.js, "render_billing_price_mode_section")
		self.assertIn("Los precios ya corresponden a esta modalidad.", body)

	# Sección 8 -- la columna quedó desambiguada, la fórmula NO cambió.
	def test_difference_column_header_is_disambiguated(self):
		self.assertIn('${__("Dif. vs base")}', self.js)
		self.assertIn("flt(item.rate_difference)", self.js)

	# Sección 15 -- siguen siendo exactamente 5 modos.
	def test_no_new_discount_modes_were_introduced(self):
		self.assertIn(
			"const PRICE_MODE_DISCOUNTS = { FULL: 0, DISCOUNT_10: 10, DISCOUNT_15: 15, DISCOUNT_20: 20, DISCOUNT_25: 25 };",
			self.js,
		)
		self.assertEqual(set(cotizaciones.PRICE_MODE_DISCOUNTS), {
			"FULL",
			"DISCOUNT_10",
			"DISCOUNT_15",
			"DISCOUNT_20",
			"DISCOUNT_25",
		})
