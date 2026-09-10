# -*- coding: utf-8 -*-
"""Commit 25.12 -- Ventas: a cancellation reason is now mandatory to cancel
a Sales Order through this app.

`cancel_sales_order(name, reason, note=None)` (api/ventas.py) validates
`reason` against the closed `CANCELLATION_REASONS` enum, requires `note`
too when `reason == "Otro"`, and persists both onto the Sales Order in the
exact same `db_update()` `.cancel()` itself triggers (no `db_set()`,
no second write) -- see that function's own docstring for the full
save/cancel ordering audit. `fg_cancellation_reason`/`fg_cancellation_note`
are Custom Fields on Sales Order (fixtures/custom_field.json), versioned
via hooks.py's `fixtures` list exactly like every other Custom Field this
app owns.

Same convention as test_ventas_cancelled_orders.py (server behaviour,
built directly against `api.ventas`) and test_ventas_confirm_button_
contract.py (client contract -- this app has no JS test runner, so
ventas.js is read as text and asserted on, never executed).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VENTAS_JS_PATH = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js")


class TestCancellationReasonApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2512 Cancellation Reason")
		cls.item = cls.world.item("FG2512-CANCEL-REASON-ITEM", default_warehouse=cls.wh.name)
		cls.customer = cls.world.customer("FG2512 Cancellation Reason Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.vendedora = cls.world.user("fg2512-vendedora@example.com", ["Vendedora"])
		cls.no_role_user = cls.world.user("fg2512-norole@example.com", [])

	def _active_order(self):
		with fx.as_user(self.vendedora):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		return result["name"]

	# A. cancelar sin reason -> rechazado
	def test_a_cancel_without_reason_is_rejected(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				ventas.cancel_sales_order(name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)

	# B. reason inválido -> rechazado
	def test_b_invalid_reason_is_rejected(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				ventas.cancel_sales_order(name, reason="Motivo inventado")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)

	# C. reason válida -> permitido
	def test_c_valid_reason_is_allowed(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			result = ventas.cancel_sales_order(name, reason="Pedido duplicado")
		self.assertEqual(result["name"], name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)

	# D. "Otro" sin note -> rechazado
	def test_d_otro_without_note_is_rejected(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				ventas.cancel_sales_order(name, reason="Otro")
			with self.assertRaises(frappe.ValidationError):
				ventas.cancel_sales_order(name, reason="Otro", note="   ")  # whitespace-only is still empty
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)

	# E. "Otro" con note -> permitido
	def test_e_otro_with_note_is_allowed(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			result = ventas.cancel_sales_order(name, reason="Otro", note="El cliente cambió de proveedor.")
		self.assertEqual(result["name"], name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)

	# F. reason se guarda
	def test_f_reason_is_persisted(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Error en cantidades")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "fg_cancellation_reason"), "Error en cantidades")

	# G. note se guarda (y queda null cuando no se envía para una razón que no la exige)
	def test_g_note_is_persisted(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Otro", note="  Detalle con espacios  ")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "fg_cancellation_note"), "Detalle con espacios")

		other_name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(other_name, reason="Cliente canceló")
		self.assertIsNone(frappe.db.get_value("Sales Order", other_name, "fg_cancellation_note"))

	# H. Sales Order termina docstatus=2
	def test_h_sales_order_ends_docstatus_2(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Cliente canceló")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)

	# I. documento no se elimina
	def test_i_document_is_never_deleted(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Cliente canceló")
		self.assertTrue(frappe.db.exists("Sales Order", name))

	# J. permisos preservados
	def test_j_permissions_preserved(self):
		name = self._active_order()
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				ventas.cancel_sales_order(name, reason="Cliente canceló")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)

	# K. Company isolation preservado
	def test_k_company_isolation_preserved(self):
		other_customer = self.world.customer("FG2512 Other Company Customer")
		other_item = self.world.item("FG2512-OTHER-COMPANY-ITEM")
		other_so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 7),
				"items": [
					{
						"item_code": other_item.name,
						"warehouse": "Finished Goods - _TC",
						"qty": 1,
						"rate": 100,
						"delivery_date": add_days(nowdate(), 7),
					}
				],
			}
		)
		other_so.insert()
		self.world.track_existing("Sales Order", other_so.name)
		other_so.submit()

		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				ventas.cancel_sales_order(other_so.name, reason="Cliente canceló")
		self.assertEqual(frappe.db.get_value("Sales Order", other_so.name, "docstatus"), 1)

	# L. ya cancelado rechazado
	def test_l_already_cancelled_is_rejected(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Cliente canceló")
			with self.assertRaises(Exception):
				ventas.cancel_sales_order(name, reason="Pedido duplicado")
		# the first cancellation's own reason is untouched by the rejected second attempt
		self.assertEqual(frappe.db.get_value("Sales Order", name, "fg_cancellation_reason"), "Cliente canceló")

	# T. Cancelados muestra razón (server half: get_my_orders returns it)
	def test_t_get_my_orders_exposes_reason_and_note_for_cancelled_view(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name, reason="Otro", note="Motivo puntual")
			cancelled = {o["name"]: o for o in ventas.get_my_orders(view="cancelled")}
		self.assertEqual(cancelled[name]["cancellation_reason"], "Otro")
		self.assertEqual(cancelled[name]["cancellation_note"], "Motivo puntual")

	# U. pedidos cancelados históricos (sin razón) no rompen la API
	def test_u_historical_cancellation_without_reason_returns_null_not_an_error(self):
		"""A Sales Order cancelled BEFORE this commit, or cancelled outside
		this app entirely (native Desk `so.cancel()`, no reason ever set),
		must still come back from get_my_orders() -- null reason/note, never
		an exception, never a fabricated value."""
		wh = self.world.warehouse("FG2512 Historical Cancel")
		item = self.world.item("FG2512-HISTORICAL-CANCEL-ITEM", default_warehouse=wh.name)
		self.world.stock_up(item.name, wh.name, 10)

		with fx.as_user(self.vendedora):
			result = ventas.create_and_submit_sales_order(customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}])
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		so = frappe.get_doc("Sales Order", result["name"])
		so.cancel()  # native cancel, never through cancel_sales_order() -- no reason ever set

		with fx.as_user(self.vendedora):
			cancelled = {o["name"]: o for o in ventas.get_my_orders(view="cancelled")}
		self.assertIsNone(cancelled[result["name"]]["cancellation_reason"])
		self.assertIsNone(cancelled[result["name"]]["cancellation_note"])


def _read():
	with open(_VENTAS_JS_PATH, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_ventas_confirm_button_contract.py's own -- from a
	method's own `name(...) {` line to the next one-tab-indented method, or
	end of string if it's the last one."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found in ventas.js")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


class TestCancellationReasonUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()
		cls.confirm_cancel_body = _method_body(cls.js, "confirm_cancel_order")

	# M. UI abre diálogo
	def test_m_ui_opens_a_real_dialog(self):
		self.assertIn("new frappe.ui.Dialog(", self.confirm_cancel_body)
		self.assertIn("dialog.show();", self.confirm_cancel_body)

	# N. UI reason obligatoria
	def test_n_ui_reason_field_is_mandatory(self):
		m = re.search(r'fieldname:\s*"reason".*?reqd:\s*1', self.confirm_cancel_body, re.S)
		self.assertIsNotNone(m, "the 'reason' Select field must be reqd: 1")

	# O. UI Otro exige detalle
	def test_o_ui_otro_requires_note_both_declaratively_and_explicitly(self):
		self.assertIn("mandatory_depends_on: \"eval:doc.reason === 'Otro'\"", self.confirm_cancel_body)
		# explicit, redundant JS-side check too -- never relies on the
		# framework's own mandatory_depends_on wiring alone.
		self.assertIn('values.reason === "Otro"', self.confirm_cancel_body)

	# P. UI no llama API si inválido
	def test_p_both_explicit_validations_return_before_any_server_call(self):
		body = self.confirm_cancel_body
		call_pos = body.index('this.call("cancel_sales_order"')

		reason_check_pos = body.index("if (!values.reason)")
		self.assertLess(reason_check_pos, call_pos)
		reason_check_block = body[reason_check_pos : body.index("}", reason_check_pos)]
		self.assertIn("return;", reason_check_block)

		otro_check_pos = body.index('values.reason === "Otro"')
		self.assertLess(otro_check_pos, call_pos)
		otro_check_block = body[otro_check_pos : body.index("}", otro_check_pos)]
		self.assertIn("return;", otro_check_block)

	# Q. UI envía name/reason/note correctos
	def test_q_ui_sends_exactly_name_reason_note_to_the_server(self):
		body = self.confirm_cancel_body
		call_start = body.index('this.call("cancel_sales_order"')
		call_args = body[call_start : body.index(")", body.index("{", call_start))]
		self.assertIn("name: name", call_args)
		self.assertIn("reason: values.reason", call_args)
		self.assertIn("note:", call_args)
		# never anything beyond these three keys -- no economic/status field,
		# no raw `values` object forwarded verbatim.
		self.assertNotIn("...values", call_args)
		self.assertNotIn("status:", call_args)

	# R. después de cancelar invalida cancelled_orders
	def test_r_success_path_invalidates_the_cancelled_cache(self):
		body = self.confirm_cancel_body
		then_pos = body.index(".then(() => {")
		invalidate_pos = body.index("this.cancelled_orders = null;", then_pos)
		self.assertGreater(invalidate_pos, then_pos)

	# S. refresca dashboard/KPIs
	def test_s_success_path_refreshes_the_dashboard(self):
		body = self.confirm_cancel_body
		invalidate_pos = body.index("this.cancelled_orders = null;")
		load_dashboard_pos = body.index("this.load_dashboard();", invalidate_pos)
		self.assertGreater(load_dashboard_pos, invalidate_pos)

	# T. Cancelados muestra razón (client half)
	def test_t_cancelled_card_renders_reason_and_note(self):
		card_body = _method_body(self.js, "render_order_card")
		self.assertIn("o.cancellation_reason", card_body)
		self.assertIn("o.cancellation_note", card_body)

	# U. cancelados históricos sin razón no rompen la UI
	def test_u_missing_historical_reason_falls_back_to_a_fixed_label_never_a_raw_null(self):
		card_body = _method_body(self.js, "render_order_card")
		self.assertIn('o.cancellation_reason || __("No registrada")', card_body)

	# V. botón principal Confirmar/Guardar sigue siempre enabled
	def test_v_main_confirm_button_is_still_never_disabled(self):
		"""Regression pin for Commit 25.11's own rule -- this commit touches
		confirm_cancel_order()/render_order_card() only, nowhere near
		.fg-confirm-btn, but this guards against a future edit reintroducing
		a disable on it while editing this same file. The exhaustive suite
		for this rule is test_ventas_confirm_button_contract.py -- this is a
		narrow, additional pin scoped to this commit's own change."""
		self.assertNotIn('.fg-confirm-btn").prop("disabled", true)', self.js)
