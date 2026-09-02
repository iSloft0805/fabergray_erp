# -*- coding: utf-8 -*-
"""Commit 25.10 -- static contract tests for two ventas.js changes:

1. CONFIRMAR PEDIDO/GUARDAR CAMBIOS stays visually enabled through the
   whole Nuevo Pedido/Editar/Modificar flow -- the button is only ever
   disabled while a request is genuinely in flight (`this.busy`); every
   other "is this ready" check moved into confirm_order() itself, checked
   at click time, each with its own specific message.
2. Cancelled Sales Orders are fetched through a separate `view` param
   (never a client-side filter over the same list active orders come
   from) and their cards never offer an edit/modify/re-cancel action.

Same convention as test_quick_order_ui_contract.py (Commit 25.8.6): this
app has no JS test runner (no package.json/node_modules), so these read
the real source as text and assert on it -- nothing here executes
JavaScript. The SERVER-side behaviour these UI changes depend on (Nuevo/
Editar/Modificar themselves, and the view="active"/"cancelled" split) is
covered end-to-end, in Python, by test_ventas_draft_confirm.py,
test_ventas_api.py, and this commit's own test_ventas_cancelled_orders.py
-- unaffected by this file, which only pins the CLIENT-side contract.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VENTAS_JS_PATH = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js")


def _read():
    with open(_VENTAS_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _method_body(source, method_name):
    """Extracts one class method's body by name -- from its own
    `name(...) {` line to the next `\\tidentifier(...) {` at the same
    one-tab indentation, or end of string if it's the last method. Same
    helper as test_quick_order_ui_contract.py's own -- good enough for
    this file's source-contract checks, not a general JS parser."""
    m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
    if not m:
        raise AssertionError(f"method {method_name!r} not found in ventas.js")
    start = m.end()
    next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
    end = start + next_method.start() if next_method else len(source)
    return source[start:end]


class TestConfirmButtonContract(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = _read()

    # A/B. botón no disabled por carrito vacío / cliente vacío
    def test_a_b_refresh_confirm_state_only_depends_on_busy(self):
        body = _method_body(self.js, "refresh_confirm_state")
        self.assertNotIn("cart.size", body)
        self.assertNotIn("np.customer", body)
        self.assertIn("this.busy", body)

    def test_no_disable_condition_based_on_forbidden_reasons(self):
        """Section 2's own explicit list -- none of these may ever appear
        inside refresh_confirm_state() as a *disabling* condition."""
        body = _method_body(self.js, "refresh_confirm_state")
        for forbidden in (
            "cart.size",
            "np.customer",
            "editing_order_name",
            "modifying_order_name",
            "np.observations",
            "quick_order",
        ):
            self.assertNotIn(forbidden, body, f"{forbidden!r} must never appear in refresh_confirm_state()")

    def test_confirm_button_markup_has_no_hardcoded_disabled_attribute(self):
        """The initial markup itself must not ship pre-disabled --
        refresh_confirm_state() is the single source of truth for this."""
        m = re.search(r'class="[^"]*fg-confirm-btn[^"]*"([^>]*)>', self.js)
        self.assertIsNotNone(m)
        self.assertNotIn("disabled", m.group(1))

    # C. click con cliente vacío muestra validación y no llama servidor
    def test_c_customer_check_happens_before_any_server_call(self):
        body = _method_body(self.js, "confirm_order")
        customer_check_pos = body.index("!this.np.customer")
        first_call_pos = body.index("this.call(")
        self.assertLess(customer_check_pos, first_call_pos)
        self.assertIn("frappe.show_alert", body[customer_check_pos:first_call_pos])

    # D. click con carrito vacío muestra validación y no llama servidor
    def test_d_empty_cart_check_happens_before_any_server_call(self):
        body = _method_body(self.js, "confirm_order")
        cart_check_pos = body.index("this.np.cart.size === 0")
        first_call_pos = body.index("this.call(")
        self.assertLess(cart_check_pos, first_call_pos)
        self.assertIn("frappe.show_alert", body[cart_check_pos:first_call_pos])

    # E. qty inválida bloquea request
    def test_e_invalid_quantities_check_happens_before_any_server_call(self):
        body = _method_body(self.js, "confirm_order")
        qty_check_pos = body.index("payload.items.length")
        first_call_pos = body.index("this.call(")
        self.assertLess(qty_check_pos, first_call_pos)
        self.assertIn("frappe.show_alert", body[qty_check_pos:first_call_pos])

    def test_three_validation_messages_are_distinct(self):
        body = _method_body(self.js, "confirm_order")
        messages = re.findall(r'message: __\("([^"]+)"\)', body)
        self.assertEqual(len(messages), len(set(messages)), "each validation must show its own distinct message")
        self.assertGreaterEqual(len(messages), 3)

    # F. request en curso sí bloquea doble click
    def test_f_busy_guard_and_temporary_disable_around_the_real_call(self):
        body = _method_body(self.js, "confirm_order")
        self.assertTrue(body.strip().startswith("if (this.busy) return;"))
        self.assertIn("this.busy = true;", body)
        self.assertIn('.prop("disabled", true)', body)

    # G. error de servidor reactiva botón
    def test_g_every_write_path_reenables_the_button_in_finally(self):
        for method_name in ("confirm_order", "save_draft_edit", "save_submitted_modification"):
            body = _method_body(self.js, method_name)
            self.assertIn('.prop("disabled", false)', body)
            self.assertIn("this.busy = false;", body)

    # H/I/J: Nuevo/Editar/Modificar -- el despacho por modo permanece
    # intacto (sin cambios de este commit); el comportamiento SERVER-SIDE
    # de los tres flujos está cubierto end-to-end en Python (ver docstring
    # del módulo) -- esto solo fija que confirm_order() sigue enrutando
    # correctamente a los tres.
    def test_h_i_j_confirm_order_still_dispatches_to_all_three_modes(self):
        body = _method_body(self.js, "confirm_order")
        self.assertIn("this.np.editing_order_name", body)
        self.assertIn("this.save_draft_edit(payload)", body)
        self.assertIn("this.np.modifying_order_name", body)
        self.assertIn("this.save_submitted_modification(payload)", body)
        self.assertIn('this.call("create_and_submit_sales_order"', body)


class TestCancelledOrdersUiContract(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = _read()

    def test_cancelados_is_no_longer_a_client_side_filter_branch(self):
        """It is its own separate list (this.cancelled_orders), never a
        filter re-applied to the active list -- see
        render_orders_section()/set_order_filter()."""
        body = _method_body(self.js, "order_matches_filter")
        self.assertNotIn('"cancelados"', body)

    def test_active_orders_are_loaded_with_the_active_view(self):
        body = _method_body(self.js, "load_dashboard")
        self.assertIn('"get_my_orders", { view: "active" }', body)

    def test_cancelled_orders_are_fetched_via_a_separate_view_param(self):
        body = _method_body(self.js, "set_order_filter")
        self.assertIn('"get_my_orders", { view: "cancelled" }', body)

    def test_cancelled_orders_are_lazy_loaded_only_once(self):
        body = _method_body(self.js, "set_order_filter")
        self.assertIn("this.cancelled_orders === null", body)

    def test_render_orders_section_switches_data_source_for_cancelled_view(self):
        body = _method_body(self.js, "render_orders_section")
        self.assertIn('this.order_filter === "cancelados"', body)
        self.assertIn("this.cancelled_orders", body)

    # R. cancelado no tiene acciones editar/confirmar
    def test_r_cancelled_order_card_only_offers_the_view_action(self):
        body = _method_body(self.js, "render_order_card_actions")
        cancelled_branch = re.search(r'if \(o\.status === "Cancelled"\)[^\{]*\{([^}]*)\}', body)
        self.assertIsNotNone(cancelled_branch, "no explicit Cancelled branch found in render_order_card_actions()")
        branch_body = cancelled_branch.group(1)
        self.assertNotIn("fg-order-card-modify", branch_body)
        self.assertNotIn("fg-order-card-cancel", branch_body)
        self.assertNotIn("fg-order-card-edit", branch_body)
        self.assertNotIn("fg-order-card-delete", branch_body)
        self.assertIn("view_btn", branch_body)

    def test_cancelled_view_never_requests_an_edit_or_modify_endpoint(self):
        """Static guardrail: nothing in set_order_filter()/
        render_orders_section() ever calls a write endpoint -- viewing
        Cancelados is read-only end to end."""
        for method_name in ("set_order_filter", "render_orders_section"):
            body = _method_body(self.js, method_name)
            for forbidden in (
                "update_draft_sales_order",
                "modify_submitted_sales_order",
                "create_and_submit_sales_order",
                "cancel_sales_order",
            ):
                self.assertNotIn(forbidden, body)


class TestCancelledOrdersCacheInvalidation(IntegrationTestCase):
    """Commit 25.10.1 -- pins the exact sequence from the brief: open
    Cancelados (cache populated) -> back to Activos -> cancel an active
    order -> cache must be invalidated -> back to Cancelados -> a FRESH
    get_my_orders(view="cancelled") call happens -> the just-cancelled
    document appears. Same static-contract convention as the rest of this
    file -- see its own module docstring. The server-side half of "the
    document actually appears" (docstatus=2 is the one authority) is
    proven end-to-end, against the real database, by
    test_ventas_cancelled_orders.py::test_o_order_appears_in_cancelled_after_cancel
    -- this class only pins that the CLIENT never second-guesses that with
    a stale cache or a manually-patched array.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = _read()

    def test_successful_cancel_invalidates_the_cancelled_cache(self):
        body = _method_body(self.js, "confirm_cancel_order")
        invalidate_pos = body.index("this.cancelled_orders = null;")
        load_dashboard_pos = body.index("this.load_dashboard();")
        # invalidation happens BEFORE the reload that follows it, inside the
        # same success (.then()) callback -- never after, never in some
        # separate, easy-to-skip code path.
        self.assertLess(invalidate_pos, load_dashboard_pos)

    def test_invalidation_only_happens_on_the_success_path(self):
        """Never on the .catch() branch -- a failed cancel (native block:
        Pick List/Material Request/Purchase Order still linked) must not
        wipe a perfectly valid, already-loaded Cancelados cache."""
        body = _method_body(self.js, "confirm_cancel_order")
        catch_start = body.index(".catch(")
        catch_body = body[catch_start:]
        self.assertNotIn("this.cancelled_orders = null", catch_body)

    def test_reopening_cancelled_after_invalidation_refetches_from_server(self):
        """set_order_filter()'s own `=== null` guard is what turns
        confirm_cancel_order()'s `= null` into a real, fresh server call the
        next time "Cancelados" is opened -- checked together here so a
        change to either side alone would be caught."""
        cancel_body = _method_body(self.js, "confirm_cancel_order")
        self.assertIn("this.cancelled_orders = null;", cancel_body)
        filter_body = _method_body(self.js, "set_order_filter")
        self.assertIn("this.cancelled_orders === null", filter_body)
        self.assertIn('"get_my_orders", { view: "cancelled" }', filter_body)

    def test_no_manual_array_mutation_of_cancelled_orders_anywhere(self):
        """Section 2's own explicit rule: "NO insertar manualmente el
        pedido cancelado dentro del array cacheado" -- invalidate + refetch
        only, never `.push(`/`.unshift(`/`.concat(`/`.splice(` on
        this.cancelled_orders, anywhere in the file."""
        for method_name in ("confirm_cancel_order", "load_dashboard", "set_order_filter"):
            body = _method_body(self.js, method_name)
            for forbidden in (
                "cancelled_orders.push(",
                "cancelled_orders.unshift(",
                "cancelled_orders.concat(",
                "cancelled_orders.splice(",
            ):
                self.assertNotIn(forbidden, body)

    def test_summary_kpi_is_refreshed_after_a_successful_cancel(self):
        """confirm_cancel_order()'s success path reaches load_dashboard(),
        which always fetches get_sales_summary() fresh -- the same call
        that already backs the "Cancelados" KPI number and (Commit 25.10's
        own fix) the "Pedidos de hoy" one; no manual count adjustment of
        any kind happens client-side."""
        cancel_body = _method_body(self.js, "confirm_cancel_order")
        self.assertIn("this.load_dashboard();", cancel_body)
        dashboard_body = _method_body(self.js, "load_dashboard")
        self.assertIn('this.call("get_sales_summary")', dashboard_body)

    def test_active_list_is_refreshed_from_the_server_after_cancel(self):
        """The cancelled order disappears from the active list without a
        full browser reload -- load_dashboard() always re-fetches
        get_my_orders(view="active") fresh."""
        cancel_body = _method_body(self.js, "confirm_cancel_order")
        self.assertIn("this.load_dashboard();", cancel_body)
        dashboard_body = _method_body(self.js, "load_dashboard")
        self.assertIn('"get_my_orders", { view: "active" }', dashboard_body)

    def test_load_dashboard_still_refreshes_cancelled_orders_when_already_loaded(self):
        """The pre-existing safety net (Commit 25.10) stays intact and
        additive to the new explicit invalidation above -- e.g. a manual
        refresh click while ALREADY viewing "Cancelados" (a different
        scenario from the cancel-then-navigate-back one this fix targets)
        still gets a fresh fetch too, never a stale in-memory replay."""
        body = _method_body(self.js, "load_dashboard")
        self.assertIn("this.cancelled_orders !== null", body)
        self.assertIn('"get_my_orders", { view: "cancelled" }', body)
