# -*- coding: utf-8 -*-
"""Commit 25.14 (Commit 25.15 review fix, section 6 -- final closed set
corrected to FULL/10%/15%/20%/25%, "Descuento 5%" removed before this
feature ever shipped/committed) -- Facturación can apply one of five
closed price modes ("Precio completo"/"Descuento 10%"/"Descuento 15%"/
"Descuento 20%"/"Descuento 25%") to a whole Quotation while it is
"Pendiente de Facturación", via `apply_quotation_price_mode()`
(api/cotizaciones.py).

CRITICAL mechanic this suite exists to pin: the discount is NEVER computed
by hand here and written to `rate` -- every call sets ONLY `discount_
percentage` (0/10/15/20/25) on freshly-built item rows (`item_code`/`qty`
only, exactly like every other write in this module), and lets ERPNext's
own native `calculate_item_rate()`/`calculate_taxes_and_totals()`
(erpnext/controllers/taxes_and_totals.py, confirmed by reading it
directly during this commit's own audit) resolve `price_list_rate` FRESH
from Item Price on `qtn.selling_price_list` and derive `rate` from THAT --
never from whatever `rate` the previous, now-cancelled version of the
document happened to carry. This is what actually guarantees "20% never
compounds on a previous 10%" -- not a manual check in this app's own code.

Same convention as test_cotizaciones_billing_review.py: one class per
server-side concern, `fx.TestWorld` fixtures, plus a static UI-contract
class at the bottom reading facturacion.js/facturacion.css as text (no JS
test runner in this app).
"""

import ast
import inspect
import os
import re

import frappe
from frappe.tests import IntegrationTestCase

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


class TestApplyQuotationPriceMode(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG2514 Customer")
		cls.vendedora = cls.world.user("fg2514-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2514-facturacion@example.com", ["Facturación"])
		cls.bodega = cls.world.user("fg2514-bodega@example.com", ["Bodega"])
		cls.jefe_bodega = cls.world.user("fg2514-jefebodega@example.com", ["Jefe de Bodega"])
		cls.recorrido = cls.world.user("fg2514-recorrido@example.com", ["Recorrido"])
		cls.gestion_clientes = cls.world.user("fg2514-gestioncli@example.com", ["Gestión de Clientes"])

	def _priced_item(self, item_code, rate=200, price_list="Standard Selling"):
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

	def _pending_quotation(self, item_code, qty=3):
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

	# A. FULL = reference_rate
	def test_a_full_equals_reference_rate(self):
		item = self._priced_item("FG2514-A-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "FULL")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].rate, 200)

	# B. 15% = reference_rate * 0.85 (Commit 25.15 review fix, section 6 --
	# the final closed set is FULL/10%/15%/20%/25%; "Descuento 5%" was
	# removed before this feature ever shipped/committed, this test slot
	# is repurposed for 15% rather than left testing a now-invalid mode).
	def test_b_discount_15_equals_85_percent_of_reference(self):
		item = self._priced_item("FG2514-B-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_15")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].rate, 170)  # 200 * 0.85

	# C. 10% = reference_rate * 0.90
	def test_c_discount_10_equals_90_percent_of_reference(self):
		item = self._priced_item("FG2514-C-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].rate, 180)  # 200 * 0.90

	# C2. 20% = reference_rate * 0.80
	def test_c2_discount_20_equals_80_percent_of_reference(self):
		item = self._priced_item("FG2514-C2-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_20")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].rate, 160)  # 200 * 0.80

	# C3. 25% = reference_rate * 0.75
	def test_c3_discount_25_equals_75_percent_of_reference(self):
		item = self._priced_item("FG2514-C3-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_25")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].rate, 150)  # 200 * 0.75

	# D. 20% nunca se calcula sobre un 10% previo
	def test_d_discount_20_never_compounds_on_a_previous_10(self):
		item = self._priced_item("FG2514-D-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		after_10 = self._apply(name, "DISCOUNT_10")
		self.assertEqual(frappe.db.get_value("Quotation", after_10["name"], "docstatus"), 1)

		after_20 = self._apply(after_10["name"], "DISCOUNT_20")
		doc = frappe.get_doc("Quotation", after_20["name"])
		self.assertEqual(doc.items[0].rate, 160)  # 200 * 0.80, NEVER 200 * 0.90 * 0.80 (= 144)
		self.assertNotEqual(doc.items[0].rate, 144)

	# E. repetir FULL es idempotente
	def test_e_repeating_full_is_idempotent(self):
		item = self._priced_item("FG2514-E-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		first = self._apply(name, "FULL")
		second = self._apply(first["name"], "FULL")
		doc1 = frappe.get_doc("Quotation", first["name"])
		doc2 = frappe.get_doc("Quotation", second["name"])
		self.assertEqual(doc1.items[0].rate, doc2.items[0].rate)
		self.assertEqual(doc2.items[0].rate, 200)

	# F. repetir 15% es idempotente
	def test_f_repeating_discount_15_is_idempotent(self):
		item = self._priced_item("FG2514-F-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		first = self._apply(name, "DISCOUNT_15")
		second = self._apply(first["name"], "DISCOUNT_15")
		doc1 = frappe.get_doc("Quotation", first["name"])
		doc2 = frappe.get_doc("Quotation", second["name"])
		self.assertEqual(doc1.items[0].rate, doc2.items[0].rate)
		self.assertEqual(doc2.items[0].rate, 170)

	# G. repetir 10% es idempotente
	def test_g_repeating_discount_10_is_idempotent(self):
		item = self._priced_item("FG2514-G-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		first = self._apply(name, "DISCOUNT_10")
		second = self._apply(first["name"], "DISCOUNT_10")
		doc1 = frappe.get_doc("Quotation", first["name"])
		doc2 = frappe.get_doc("Quotation", second["name"])
		self.assertEqual(doc1.items[0].rate, doc2.items[0].rate)
		self.assertEqual(doc2.items[0].rate, 180)

	# H. price_mode inválido rechazado
	def test_h_invalid_price_mode_is_rejected(self):
		item = self._priced_item("FG2514-H-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(name, "BOGUS")
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 1)  # untouched

	# I. porcentaje arbitrario rechazado
	def test_i_arbitrary_percentage_string_is_rejected(self):
		item = self._priced_item("FG2514-I-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_30")
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(name, "7")

	# I2. "Descuento 5%"/DISCOUNT_5 -- ya NO es una modalidad comercial
	# válida (Commit 25.15 review fix, section 6/13-T) -- rechazado
	# exactamente igual que cualquier otro código desconocido, nunca
	# tratado como un caso especial.
	def test_i2_discount_5_is_no_longer_a_valid_commercial_modality(self):
		self.assertNotIn("DISCOUNT_5", cotizaciones.PRICE_MODE_DISCOUNTS)
		self.assertNotIn("Descuento 5%", cotizaciones._PRICE_MODE_LABELS.values())

		item = self._priced_item("FG2514-I2-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_5")
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 1)  # untouched

	# J. Vendedora no puede aplicar
	def test_j_vendedora_cannot_apply(self):
		item = self._priced_item("FG2514-J-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")

	def test_j2_other_roles_cannot_apply(self):
		item = self._priced_item("FG2514-J2-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		for user in (self.bodega, self.jefe_bodega, self.recorrido, self.gestion_clientes):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError):
					cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")

	# K. Facturación sí
	def test_k_facturacion_can_apply(self):
		item = self._priced_item("FG2514-K-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertEqual(result["fg_billing_price_mode"], "Descuento 10%")

	# L. Company isolation
	def test_l_company_isolation(self):
		other_customer = self.world.customer("FG2514 Other Company Customer")
		other_item = self.world.item("FG2514-OTHER-COMPANY-ITEM")
		other_qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": other_item.name, "qty": 1, "rate": 100}],
			}
		)
		other_qtn.insert()
		self.world.track_existing("Quotation", other_qtn.name)
		other_qtn.submit()
		other_qtn.fg_billing_review_status = "Pendiente de Facturación"
		other_qtn.save()

		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.apply_quotation_price_mode(other_qtn.name, "DISCOUNT_10")

	# M. solo Pending permite ajuste
	def test_m_only_pending_status_allows_adjustment(self):
		item = self._priced_item("FG2514-M-ITEM", rate=200)

		# Borrador (never sent)
		with fx.as_user(self.vendedora):
			borrador = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", borrador["name"])
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(borrador["name"], "DISCOUNT_10")

		# Aprobada
		pending = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			cotizaciones.approve_quotation_billing(pending)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(pending, "DISCOUNT_10")

		# Devuelta
		pending2 = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(pending2, reason="Motivo de prueba")
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(pending2, "DISCOUNT_10")

	# N. no reference_rate bloquea
	def test_n_missing_reference_rate_blocks_with_a_readable_message(self):
		item = self.world.item("FG2514-N-ITEM")  # no Item Price at all
		name = self._pending_quotation(item.name)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError) as ctx:
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		message = str(ctx.exception)
		self.assertIn(item.name, message)
		self.assertIn("Standard Selling", message)
		# untouched -- still submitted, still Pendiente, never cancelled
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Pendiente de Facturación")

	# O. usa selling_price_list real (never hardcoded Standard Selling)
	def test_o_uses_the_quotations_own_selling_price_list(self):
		other_list = frappe.get_doc(
			{"doctype": "Price List", "price_list_name": "FG2514 Other Selling List", "selling": 1, "currency": "INR"}
		)
		other_list.insert()
		self.world.track_existing("Price List", other_list.name)

		item = self.world.item("FG2514-O-ITEM")
		standard_price = frappe.get_doc(
			{"doctype": "Item Price", "item_code": item.name, "price_list": "Standard Selling", "selling": 1, "price_list_rate": 100}
		)
		standard_price.insert()
		self.world.track_existing("Item Price", standard_price.name)
		other_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": other_list.name,
				"selling": 1,
				"currency": "INR",
				"price_list_rate": 500,
			}
		)
		other_price.insert()
		self.world.track_existing("Item Price", other_price.name)

		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": fx.COMPANY,
				"selling_price_list": other_list.name,
				"items": [{"item_code": item.name, "qty": 1}],
			}
		)
		with fx.as_user(self.vendedora):
			qtn.insert()
			qtn.fg_billing_review_status = "Borrador"
			qtn.save()
			qtn.submit()
		self.world.track_existing("Quotation", qtn.name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(qtn.name)

		result = self._apply(qtn.name, "DISCOUNT_10")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.selling_price_list, other_list.name)
		self.assertEqual(doc.items[0].rate, 450)  # 500 * 0.90, never 100 * 0.90 (Standard Selling's own rate)

	# Q. amount recalculado
	def test_q_amount_is_recalculated_natively(self):
		item = self._priced_item("FG2514-Q-ITEM", rate=200)
		name = self._pending_quotation(item.name, qty=4)
		result = self._apply(name, "DISCOUNT_10")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.items[0].amount, 4 * 180)  # qty * adjusted rate

	# R. grand_total recalculado
	def test_r_grand_total_is_recalculated_natively(self):
		item = self._priced_item("FG2514-R-ITEM", rate=200)
		name = self._pending_quotation(item.name, qty=2)
		result = self._apply(name, "DISCOUNT_10")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.grand_total, 2 * 180)

	# S. impuestos preservados/recalculados -- no tax template configured on
	# this site (confirmed during this commit's own audit: zero active
	# Pricing Rules, and every existing Quotation test in this app never
	# sets one either) -- this pins that the native, empty-taxes case
	# stays consistent (0, never a stale non-zero value from a cancelled
	# original), the same native calculate_taxes_and_totals() call that
	# would apply any REAL tax template identically.
	def test_s_taxes_field_stays_consistent(self):
		item = self._priced_item("FG2514-S-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.total_taxes_and_charges, 0)
		self.assertEqual(doc.grand_total, doc.total)  # no taxes -> equal

	# T. no costos expuestos
	def test_t_response_never_leaks_economic_or_cost_data(self):
		item = self._priced_item("FG2514-T-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertEqual(set(result.keys()), {"name", "fg_billing_review_status", "fg_billing_price_mode"})

	# U. no db_set/bypass -- static source check, same convention
	# test_regression.py's own TestStaticGuardrails already uses.
	def test_u_no_db_set_or_permission_bypass(self):
		source = inspect.getsource(cotizaciones.apply_quotation_price_mode)
		tree = ast.parse(source)
		for node in ast.walk(tree):
			if isinstance(node, ast.Attribute) and node.attr in ("db_set", "db_update"):
				self.fail("apply_quotation_price_mode() must never call .db_set()/.db_update()")
			if isinstance(node, ast.Call):
				for kw in node.keywords:
					if kw.arg in ("ignore_permissions", "ignore_validate") and isinstance(kw.value, ast.Constant) and kw.value.value is True:
						self.fail(f"apply_quotation_price_mode() must never pass {kw.arg}=True")
		self.assertNotIn("frappe.db.set_value", source)

	# V. no auto-aprueba
	def test_v_never_auto_approves(self):
		item = self._priced_item("FG2514-V-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertEqual(result["fg_billing_review_status"], "Pendiente de Facturación")
		self.assertIsNone(frappe.db.get_value("Quotation", result["name"], "fg_billing_reviewed_by"))
		self.assertIsNone(frappe.db.get_value("Quotation", result["name"], "fg_billing_reviewed_on"))

	# W. adjusted_by guardado
	def test_w_adjusted_by_is_saved(self):
		item = self._priced_item("FG2514-W-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "fg_billing_price_adjusted_by"), self.facturacion)

	# X. adjusted_on guardado
	def test_x_adjusted_on_is_saved(self):
		item = self._priced_item("FG2514-X-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertIsNotNone(frappe.db.get_value("Quotation", result["name"], "fg_billing_price_adjusted_on"))

	# Y. mode guardado
	def test_y_price_mode_label_is_saved(self):
		item = self._priced_item("FG2514-Y-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		result_full = self._apply(name, "FULL")
		self.assertEqual(frappe.db.get_value("Quotation", result_full["name"], "fg_billing_price_mode"), "Precio completo")

		item2 = self._priced_item("FG2514-Y2-ITEM", rate=200)
		name2 = self._pending_quotation(item2.name)
		result_10 = self._apply(name2, "DISCOUNT_10")
		self.assertEqual(frappe.db.get_value("Quotation", result_10["name"], "fg_billing_price_mode"), "Descuento 10%")

	# Z. modificar luego invalida price mode
	def test_z_editing_afterwards_clears_the_price_mode_audit_fields(self):
		item = self._priced_item("FG2514-Z-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_10")
		self.assertEqual(frappe.db.get_value("Quotation", applied["name"], "fg_billing_price_mode"), "Descuento 10%")

		# Facturación returns it (the only path back OUT of Pendiente, so
		# Vendedora can legitimately edit it again).
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(applied["name"], reason="Corrige la cantidad")

		with fx.as_user(self.vendedora):
			edited = cotizaciones.modify_submitted_quotation(
				name=applied["name"], customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
			)
		self.world.track_existing("Quotation", edited["name"])

		# fg_billing_price_mode is a Select column (like every other Select
		# field in this app) -- MariaDB stores it as "" not NULL, so a raw
		# `frappe.db.get_value()` legitimately returns "" here. The `or None`
		# normalization lives at the API boundary (get_quotation_billing_detail(),
		# api/cotizaciones.py) for real consumers -- assert through that, the
		# same way callers actually observe "cleared".
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(edited["name"])
		self.assertIsNone(detail["fg_billing_price_mode"])
		self.assertIsNone(detail["fg_billing_price_adjusted_by"])
		self.assertIsNone(detail["fg_billing_price_adjusted_on"])
		self.assertEqual(frappe.db.get_value("Quotation", edited["name"], "fg_billing_review_status"), "Borrador")

	# AH. aprobar sigue funcionando (después de ajustar precio)
	def test_ah_approve_still_works_after_a_price_adjustment(self):
		item = self._priced_item("FG2514-AH-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_10")
		with fx.as_user(self.facturacion):
			approved = cotizaciones.approve_quotation_billing(applied["name"])
		self.assertEqual(approved["fg_billing_review_status"], "Aprobada")
		# the APPROVED document is the one carrying the adjusted price.
		doc = frappe.get_doc("Quotation", approved["name"])
		self.assertEqual(doc.items[0].rate, 180)

	# AI. devolver sigue funcionando (después de ajustar precio)
	def test_ai_return_still_works_after_a_price_adjustment(self):
		item = self._priced_item("FG2514-AI-ITEM", rate=200)
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_10")
		with fx.as_user(self.facturacion):
			returned = cotizaciones.return_quotation_from_billing(applied["name"], reason="Precio no autorizado")
		self.assertEqual(returned["fg_billing_review_status"], "Devuelta")


def _bare_dangerous_selector_matches(css):
	pattern = re.compile(
		r"(?m)^(?:svg|img|button|table|\.modal|\.modal-dialog|\.modal-content|\.modal-body|\.modal-footer|\.modal-header)"
		r"(?:\s*,\s*(?:svg|img|button|table|\.modal|\.modal-dialog|\.modal-content|\.modal-body|\.modal-footer|\.modal-header))*"
		r"\s*\{"
	)
	return pattern.findall(css)


class TestPriceModeUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.facturacion_js = _read(_FACTURACION_JS_PATH)
		cls.facturacion_css = _read(_FACTURACION_CSS_PATH)

	# AA. UI selector muestra 3 opciones
	# Commit 25.15 review fix, section 6 -- the final closed set is 5
	# options (FULL/10%/15%/20%/25%), not 3; "Descuento 5%" is gone.
	def test_aa_selector_shows_exactly_five_options(self):
		# The 5 buttons are rendered from a data-driven `options` array (each
		# entry's `mode` interpolated into `data-mode="${o.mode}"`), never as
		# 5 literal `data-mode="FULL"`-style strings -- so the contract check
		# is on the array's 5 declared modes plus the templated attribute
		# that wires each one into the DOM, not a literal string match.
		body = _method_body(self.facturacion_js, "render_billing_price_mode_section")
		self.assertIn('mode: "FULL"', body)
		self.assertIn('mode: "DISCOUNT_10"', body)
		self.assertIn('mode: "DISCOUNT_15"', body)
		self.assertIn('mode: "DISCOUNT_20"', body)
		self.assertIn('mode: "DISCOUNT_25"', body)
		self.assertNotIn('mode: "DISCOUNT_5"', body)
		self.assertEqual(body.count('{ mode: "'), 5)
		self.assertIn('data-mode="${o.mode}"', body)

	# AB. modal no aplica automáticamente al abrir
	def test_ab_opening_the_dialog_never_calls_apply_by_itself(self):
		open_body = _method_body(self.facturacion_js, "open_billing_review_dialog")
		self.assertNotIn("apply_quotation_price_mode", open_body)

	# AC. APLICAR PRECIOS llama endpoint
	def test_ac_apply_button_calls_the_real_endpoint(self):
		self.assertIn("apply_quotation_price_mode", self.facturacion_js)

	# AD/AE. preview 10%/15%/20%/25% -- computed client-side, never a server
	# round-trip per selector click (section 4's own "no persistir solo por
	# cambiar visualmente el selector").
	def test_ad_ae_preview_is_computed_client_side_for_every_discount(self):
		select_body = _method_body(self.facturacion_js, "select_billing_price_mode")
		for multiplier in ("0.9", "0.85", "0.8", "0.75"):
			self.assertIn(multiplier, self.facturacion_js)
		self.assertNotIn("call_cotizaciones", select_body)  # no server call just from selecting

	# AF. FULL restaura precio base
	def test_af_full_mode_uses_the_base_rate_with_no_discount(self):
		self.assertIn("PRICE_MODE_MULTIPLIERS", self.facturacion_js)

	# AG. sin referencia muestra error legible -- server-side message is
	# already pinned by test_n above; this confirms the client surfaces the
	# server's own default error dialog rather than swallowing it (same
	# ".catch(() => {})" convention already pinned across this whole app).
	def test_ag_apply_price_mode_catch_never_swallows_silently(self):
		apply_body = _method_body(self.facturacion_js, "apply_billing_price_mode")
		self.assertIn(".catch(", apply_body)

	# AJ. modal 25.13.1 no regresa al layout roto
	def test_aj_dialog_root_scope_still_correct(self):
		self.assertIn(".fg-fact-billing-review-dialog .modal-dialog", self.facturacion_css)
		self.assertNotIn(".fg-facturacion .fg-fact-billing-review-dialog", self.facturacion_css)
		self.assertEqual(_bare_dangerous_selector_matches(self.facturacion_css), [])

	# AK. iconos siguen correctamente scoped
	def test_ak_icons_still_scoped_and_sized(self):
		start = self.facturacion_css.index(".fg-fact-billing-review-dialog .fg-icon {")
		end = self.facturacion_css.index("}", start)
		icon_rule = self.facturacion_css[start:end]
		self.assertIn("width:", icon_rule)
		self.assertIn("height:", icon_rule)

	# AL. botón principal de Ventas sigue siempre enabled
	def test_al_ventas_confirm_button_still_never_disabled(self):
		ventas_js_path = os.path.join(
			frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js"
		)
		ventas_js = _read(ventas_js_path)
		self.assertNotIn('.fg-confirm-btn").prop("disabled", true)', ventas_js)

	# F/G. el frontend deja de usar el nombre cancelado y consulta el
	# nuevo -- fija en el propio código fuente que apply_billing_price_
	# mode() reasigna `this._billing_review_quotation`/`_billing_review_
	# detail` al `result.name` devuelto por el servidor, nunca sigue
	# usando el `d.name`/closure original tras un apply exitoso.
	def test_fg_apply_reassigns_current_quotation_state_to_the_new_name(self):
		apply_body = _method_body(self.facturacion_js, "apply_billing_price_mode")
		self.assertIn("this._billing_review_quotation = result.name", apply_body)
		self.assertIn('get_quotation_billing_detail", { name: result.name }', apply_body)
		self.assertIn("this._billing_review_detail = detail", apply_body)

	def test_fg_devolver_button_reads_current_state_never_a_stale_closure(self):
		# open_billing_review_dialog() wires DEVOLVER via add_custom_action();
		# the callback must read `this._billing_review_quotation` (which
		# apply_billing_price_mode() keeps current), never the `name`
		# parameter captured in the dialog-opening closure (stale the
		# moment a price adjustment supersedes it).
		open_body = _method_body(self.facturacion_js, "open_billing_review_dialog")
		self.assertIn("this.open_return_quotation_dialog(this._billing_review_quotation)", open_body)


class TestPriceModeAmendmentAudit(IntegrationTestCase):
	"""Commit 25.14 "AUDITORÍA FINAL ANTES DE COMMIT" review --
	section 1 (A-K, el frontend/backend deben seguir siempre la versión
	VIGENTE tras un amendment), section 2 (cadena completa de
	amendments), section 3 (flujo end-to-end aplicar -> aprobar),
	section 6 (campos nativos rate/discount/amount/net_rate/net_amount/
	grand_total verificados en un documento real), section 7 (el
	amendment conserva "Pendiente de Facturación", nunca una aprobación
	previa), y section 8 (los documentos cancelados por
	apply_quotation_price_mode() son trazables vía amended_from, nunca
	reaparecen como pendientes, nunca se confunden con "Devuelta", y
	nunca se borran)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG2514AUD Customer")
		cls.vendedora = cls.world.user("fg2514aud-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2514aud-facturacion@example.com", ["Facturación"])

	def _priced_item(self, item_code, rate=200, price_list="Standard Selling"):
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

	def _pending_quotation(self, item_code, qty=3):
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

	# A. apply devuelve el nombre del amended
	def test_a_apply_returns_amended_name(self):
		item = self._priced_item("FG2514AUD-A-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertIn("name", result)
		self.assertTrue(result["name"])

	# B. nombre devuelto != original cuando hubo amendment
	def test_b_returned_name_differs_from_original(self):
		item = self._priced_item("FG2514AUD-B-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertNotEqual(result["name"], name)

	# C. original queda docstatus=2
	def test_c_original_ends_docstatus_2(self):
		item = self._priced_item("FG2514AUD-C-ITEM")
		name = self._pending_quotation(item.name)
		self._apply(name, "FULL")
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 2)

	# D. nueva queda docstatus=1
	def test_d_new_is_docstatus_1(self):
		item = self._priced_item("FG2514AUD-D-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "FULL")
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "docstatus"), 1)

	# E. nueva conserva Pendiente de Facturación
	def test_e_new_keeps_pending_status(self):
		item = self._priced_item("FG2514AUD-E-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")
		self.assertEqual(
			frappe.db.get_value("Quotation", result["name"], "fg_billing_review_status"),
			"Pendiente de Facturación",
		)

	# H. Aprobar después de aplicar precios aprueba la NUEVA cotización;
	# el original permanece Cancelled, nunca re-aprobado.
	def test_h_approve_after_apply_approves_the_new_document_only(self):
		item = self._priced_item("FG2514AUD-H-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_10")
		with fx.as_user(self.facturacion):
			approved = cotizaciones.approve_quotation_billing(applied["name"])
		self.assertEqual(approved["name"], applied["name"])
		self.assertEqual(frappe.db.get_value("Quotation", applied["name"], "fg_billing_review_status"), "Aprobada")
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 2)
		self.assertNotEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Aprobada")

	# I. Devolver después de aplicar precios devuelve la NUEVA cotización;
	# el original nunca es tocado por el return.
	def test_i_return_after_apply_returns_the_new_document_only(self):
		item = self._priced_item("FG2514AUD-I-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "DISCOUNT_10")
		with fx.as_user(self.facturacion):
			returned = cotizaciones.return_quotation_from_billing(applied["name"], reason="Ajustar de nuevo")
		self.assertEqual(returned["name"], applied["name"])
		self.assertEqual(frappe.db.get_value("Quotation", applied["name"], "fg_billing_review_status"), "Devuelta")
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 2)

	# J. la cotización original cancelada nunca reaparece en pendientes
	def test_j_cancelled_original_never_reappears_in_pending(self):
		item = self._priced_item("FG2514AUD-J-ITEM")
		name = self._pending_quotation(item.name)
		applied = self._apply(name, "FULL")
		with fx.as_user(self.facturacion):
			pending = cotizaciones.get_pending_billing_review_quotations()
		names = [row["name"] for row in pending]
		self.assertNotIn(name, names)
		self.assertIn(applied["name"], names)

	# K. doble aplicación sucesiva sigue siempre la versión más reciente
	def test_k_successive_applications_always_follow_the_latest_version(self):
		item = self._priced_item("FG2514AUD-K-ITEM", rate=300)
		name = self._pending_quotation(item.name)
		first = self._apply(name, "DISCOUNT_10")
		second = self._apply(first["name"], "DISCOUNT_10")
		self.assertNotEqual(second["name"], first["name"])
		self.assertNotEqual(second["name"], name)
		doc = frappe.get_doc("Quotation", second["name"])
		self.assertEqual(doc.items[0].rate, 270)  # 300 * 0.90 off the ORIGINAL base each time, never compounded

		# the now-superseded intermediate version (`first["name"]`) is no
		# longer docstatus=1 -- a further apply against it must fail.
		self.assertEqual(frappe.db.get_value("Quotation", first["name"], "docstatus"), 2)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(first["name"], "FULL")

	# ------------------------------------------------------------------
	# Section 2 -- cadena completa: original -> 15% -> 25% -> Precio completo
	# ------------------------------------------------------------------
	def test_full_amendment_chain_15_then_25_then_full(self):
		item = self._priced_item("FG2514AUD-CHAIN-ITEM", rate=400)
		original = self._pending_quotation(item.name)

		amend_a = self._apply(original, "DISCOUNT_15")
		self.assertEqual(frappe.get_doc("Quotation", amend_a["name"]).items[0].rate, 340)  # 400*0.85
		self.assertEqual(frappe.db.get_value("Quotation", amend_a["name"], "amended_from"), original)

		amend_b = self._apply(amend_a["name"], "DISCOUNT_25")
		self.assertEqual(frappe.get_doc("Quotation", amend_b["name"]).items[0].rate, 300)  # 400*0.75, NEVER 340*0.75
		self.assertEqual(frappe.db.get_value("Quotation", amend_b["name"], "amended_from"), amend_a["name"])

		amend_c = self._apply(amend_b["name"], "FULL")
		self.assertEqual(frappe.get_doc("Quotation", amend_c["name"]).items[0].rate, 400)
		self.assertEqual(frappe.db.get_value("Quotation", amend_c["name"], "amended_from"), amend_b["name"])

		# exactly one live (docstatus=1) version across the whole chain --
		# no parallel branches.
		chain = [original, amend_a["name"], amend_b["name"], amend_c["name"]]
		docstatuses = [frappe.db.get_value("Quotation", n, "docstatus") for n in chain]
		self.assertEqual(docstatuses, [2, 2, 2, 1])

		# only the last version is still Pendiente
		self.assertEqual(
			frappe.db.get_value("Quotation", amend_c["name"], "fg_billing_review_status"), "Pendiente de Facturación"
		)

		# price_mode corresponds to the LAST selection only
		self.assertEqual(frappe.db.get_value("Quotation", amend_c["name"], "fg_billing_price_mode"), "Precio completo")

		# the pending tray shows exactly the last version of this lineage
		with fx.as_user(self.facturacion):
			pending = cotizaciones.get_pending_billing_review_quotations()
		names = [row["name"] for row in pending]
		self.assertIn(amend_c["name"], names)
		for n in chain[:-1]:
			self.assertNotIn(n, names)

		# a further apply against any superseded intermediate version fails
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(amend_a["name"], "DISCOUNT_10")
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.apply_quotation_price_mode(amend_b["name"], "DISCOUNT_10")

	# ------------------------------------------------------------------
	# Section 3 -- flujo end-to-end: crear -> enviar -> ajustar -> aprobar
	# ------------------------------------------------------------------
	def test_end_to_end_apply_then_approve_leaves_a_clean_tray(self):
		item = self._priced_item("FG2514AUD-E2E-ITEM", rate=500)
		original = self._pending_quotation(item.name)

		applied = self._apply(original, "DISCOUNT_10")
		self.assertEqual(frappe.get_doc("Quotation", applied["name"]).items[0].rate, 450)  # 500*0.90

		with fx.as_user(self.facturacion):
			approved = cotizaciones.approve_quotation_billing(applied["name"])
		self.assertEqual(approved["fg_billing_review_status"], "Aprobada")
		self.assertEqual(frappe.db.get_value("Quotation", original, "docstatus"), 2)

		# the tray is clean -- neither the original nor the now-Aprobada
		# amendment show as pending any more.
		with fx.as_user(self.facturacion):
			pending = cotizaciones.get_pending_billing_review_quotations()
		names = [row["name"] for row in pending]
		self.assertNotIn(original, names)
		self.assertNotIn(applied["name"], names)

	# ------------------------------------------------------------------
	# Section 6 -- campos nativos verificados en un documento real
	# ------------------------------------------------------------------
	def test_native_price_fields_are_correct_after_a_price_mode(self):
		item = self._priced_item("FG2514AUD-NATIVE-ITEM", rate=250)
		name = self._pending_quotation(item.name, qty=4)
		result = self._apply(name, "DISCOUNT_10")

		doc = frappe.get_doc("Quotation", result["name"])
		row = doc.items[0]
		self.assertEqual(row.price_list_rate, 250)
		self.assertEqual(row.discount_percentage, 10)
		self.assertEqual(row.rate, 225)  # 250 * 0.90
		self.assertEqual(row.amount, 900)  # 225 * 4
		self.assertEqual(row.net_rate, 225)
		self.assertEqual(row.net_amount, 900)
		self.assertEqual(doc.total, 900)  # native subtotal, not hand-computed here
		self.assertGreaterEqual(doc.grand_total, doc.total)  # taxes only ever ADD, never subtract

	# ------------------------------------------------------------------
	# Section 7 -- no regresión de la semántica de billing review
	# ------------------------------------------------------------------
	def test_billing_review_semantics_stay_consistent_with_pending(self):
		item = self._priced_item("FG2514AUD-SEM-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "DISCOUNT_10")

		doc = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(doc.fg_billing_review_status, "Pendiente de Facturación")
		self.assertIsNone(doc.fg_billing_reviewed_by)  # never carries a prior approval forward
		self.assertIsNone(doc.fg_billing_reviewed_on)
		self.assertEqual(doc.fg_billing_price_mode, "Descuento 10%")
		self.assertEqual(doc.fg_billing_price_adjusted_by, self.facturacion)
		self.assertIsNotNone(doc.fg_billing_price_adjusted_on)

	# ------------------------------------------------------------------
	# Section 8 -- auditoría de documentos cancelados internos
	# ------------------------------------------------------------------
	def test_cancelled_amendment_is_traceable_but_never_pending_or_confused_with_returned(self):
		item = self._priced_item("FG2514AUD-CANCEL-ITEM")
		name = self._pending_quotation(item.name)
		result = self._apply(name, "FULL")

		# never physically deleted
		self.assertTrue(frappe.db.exists("Quotation", name))
		self.assertEqual(frappe.db.get_value("Quotation", name, "docstatus"), 2)

		# traceable via amended_from
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "amended_from"), name)

		# never counted twice -- exactly 1 pending row for this lineage
		with fx.as_user(self.facturacion):
			pending = cotizaciones.get_pending_billing_review_quotations()
			summary = cotizaciones.get_quotation_billing_summary()
		lineage_pending = [n for n in [row["name"] for row in pending] if n in (name, result["name"])]
		self.assertEqual(lineage_pending, [result["name"]])
		self.assertGreaterEqual(summary["cotizaciones_pendientes"], 1)

		# never confused with "Devuelta" -- that status is only ever set by
		# return_quotation_from_billing(), never by apply_quotation_price_mode().
		self.assertNotEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Devuelta")
		self.assertNotEqual(frappe.db.get_value("Quotation", result["name"], "fg_billing_review_status"), "Devuelta")


class TestFacturacionCannotCreateArbitraryQuotation(IntegrationTestCase):
	"""Commit 25.14 "AUDITORÍA FINAL ANTES DE COMMIT" review -- section 4/5:
	Facturación's Quotation Custom DocPerm now grants `create: 1`/
	`cancel: 1`/`amend: 1` (needed ONLY for apply_quotation_price_mode()'s
	own controlled cancel+amend -- see its own docstring, and
	`_facturacion_billing_review_gate()`'s, for the full "amend is never
	actually enforced by Frappe, create is the real gate" finding).

	These tests pin that this grant does NOT ALSO let a Facturación user
	create/edit an arbitrary commercial Quotation through any path:
	- this app's own 3 Vendedora write functions, now explicitly gated by
	  `_require_vendedora_role()` (api/cotizaciones.py); and
	- a raw Desk/API insert, blocked by `guard_facturacion_quotation_
	  insert()` (Quotation's `before_insert` doc_event, hooks.py).
	`apply_quotation_price_mode()` itself remains the ONE legitimate
	insert path for Facturación (L/M below)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG2514AUD2 Customer")
		cls.vendedora = cls.world.user("fg2514aud2-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2514aud2-facturacion@example.com", ["Facturación"])
		cls.item = cls.world.item("FG2514AUD2-ITEM")
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": cls.item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 100,
			}
		)
		price.insert()
		cls.world.track_existing("Item Price", price.name)

	def _pending_quotation(self):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		return result["name"]

	# L. Facturación puede ejecutar apply_quotation_price_mode()
	def test_l_facturacion_can_execute_apply_quotation_price_mode(self):
		name = self._pending_quotation()
		with fx.as_user(self.facturacion):
			result = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")
		self.world.track_existing("Quotation", result["name"])
		self.assertTrue(result["name"])

	# M. Facturación puede crear el amendment autorizado (docstatus=1,
	# amended_from correcto) -- la ÚNICA vía de insert legítima para ella.
	def test_m_facturacion_created_amendment_is_correctly_linked(self):
		name = self._pending_quotation()
		with fx.as_user(self.facturacion):
			result = cotizaciones.apply_quotation_price_mode(name, "FULL")
		self.world.track_existing("Quotation", result["name"])
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "amended_from"), name)
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "docstatus"), 1)

	# N. Facturación NO puede crear una Quotation comercial arbitraria
	# fuera del flujo, por NINGUNA vía.
	def test_n_facturacion_cannot_create_via_create_and_submit_quotation(self):
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.create_and_submit_quotation(
					customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
				)

	def test_n_facturacion_cannot_edit_a_draft_via_update_draft_quotation(self):
		draft_doc = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": frappe.defaults.get_global_default("company"),
				"items": [{"item_code": self.item.name, "qty": 1}],
			}
		)
		with fx.as_user(self.vendedora):
			draft_doc.insert()
		self.world.track_existing("Quotation", draft_doc.name)

		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.update_draft_quotation(
					name=draft_doc.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 5}]
				)

	def test_n_facturacion_cannot_call_modify_submitted_quotation_directly(self):
		name = self._pending_quotation()
		with fx.as_user(self.facturacion):
			# Out of "Pendiente" first (via a legitimate Facturación action)
			# so this test isolates the ROLE gate, not the separate
			# pending-lock guard modify_submitted_quotation() already has.
			cotizaciones.return_quotation_from_billing(name, reason="Auditoría de permisos 25.14")

		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.modify_submitted_quotation(
					name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 9}]
				)

	def test_n_facturacion_cannot_insert_a_raw_quotation_bypassing_the_api(self):
		"""Simulates Desk's own native "New Quotation"/"Amend" button -- a
		plain `.insert()`, never through any whitelisted function in this
		app -- confirming `guard_facturacion_quotation_insert()`'s own
		`before_insert` doc_event is what actually closes this gap, not
		just this app's own API-level checks."""
		doc = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": frappe.defaults.get_global_default("company"),
				"items": [{"item_code": self.item.name, "qty": 1}],
			}
		)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				doc.insert()

	def test_n_vendedora_holding_both_roles_keeps_her_ordinary_rights(self):
		"""A user holding BOTH "Vendedora" and "Facturación" must never be
		penalized by either new guard -- her own ordinary create/edit
		rights stay exactly as they were."""
		dual = self.world.user("fg2514aud2-dual@example.com", ["Vendedora", "Facturación"])
		with fx.as_user(dual):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		self.assertTrue(result["name"])
