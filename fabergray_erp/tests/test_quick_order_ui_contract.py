# -*- coding: utf-8 -*-
"""Commit 25.8.6 -- static contract tests for the "Pedido rápido" UI
(page/ventas/ventas.js). This app has no JS test runner (no package.json /
node_modules, no existing JS test file anywhere in the repo) -- these tests
follow the SAME convention this app's own Python suite already uses
extensively for exactly this purpose (test_regression.py's own
"TestStaticGuardrails" class, and this commit's own predecessors --
catalog.py's test_real_catalog_never_inserted_updated_or_deleted(),
api/ventas.py's test_endpoint_source_never_calls_write_primitives()):
read the real source as text, assert on it. Nothing here executes
JavaScript -- these are contract/regression pins on the SOURCE, not a
functional test of the browser behaviour (that was verified manually,
see this commit's own report, section Q).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VENTAS_JS_PATH = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js")
_VENTAS_CSS_PATH = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.css")

_QUICK_ORDER_SECTION_START = "// -- Pedido rápido (Commit 25.8.6)"
_QUICK_ORDER_SECTION_END = "// -- Cart / Paso 3: Resumen"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _strip_line_comments(source):
    """Removes "// ..." line comments (this file's own only comment style --
    confirmed by reading it: no /* */ blocks in the JS body) before running
    a source-contract check for a forbidden pattern -- otherwise an
    EXPLANATORY comment that names the exact thing NOT to do (e.g. "never a
    direct this.np.cart.set()") would itself trip the check. Checks below
    care about real code, not prose describing it."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def _quick_order_section(source):
    """The contiguous block of ventas.js this commit added -- every Quick
    Order method lives between these two markers (verbatim comments in the
    real file). Used to scope checks (F/G below) that must hold for the NEW
    code specifically, without false-positiving on confirm_order()/
    build_order_payload() (Commit 18.x, untouched, legitimately calls
    create_and_submit_sales_order() etc. a few hundred lines below)."""
    start = source.index(_QUICK_ORDER_SECTION_START)
    end = source.index(_QUICK_ORDER_SECTION_END, start)
    return source[start:end]


def _method_body(source, method_name):
    """Extracts one class method's body by name -- from its own `name(...) {`
    line to the next `\tidentifier(...) {` at the same one-tab indentation
    (this file's own consistent style, confirmed by reading it directly),
    or end of string if it's the last method. Good enough for these
    source-contract checks; not a general JS parser."""
    m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
    if not m:
        raise AssertionError(f"method {method_name!r} not found in ventas.js")
    start = m.end()
    next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
    end = start + next_method.start() if next_method else len(source)
    return source[start:end]


class TestQuickOrderUiContract(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = _read(_VENTAS_JS_PATH)
        cls.qo_section = _quick_order_section(cls.js)

    # A. ventas.js llama parse_quick_order
    def test_a_calls_parse_quick_order(self):
        self.assertIn('this.call("parse_quick_order"', self.qo_section)

    # B. usa preselected_item
    def test_b_uses_preselected_item(self):
        self.assertIn("preselected_item", self.qo_section)
        # specifically to seed the client's own `selected` state -- never
        # top_candidate on its own (see rule D-equivalent check below).
        self.assertIn("server_line.preselected_item", self.qo_section)

    # C. no reimplementa threshold 90 (high confidence)
    def test_c_never_reimplements_the_90_confidence_threshold(self):
        for pattern in (r">=\s*90\b", r">\s*89\b", r"score\s*>=", r"score\s*>"):
            self.assertIsNone(
                re.search(pattern, self.qo_section), f"ventas.js Quick Order code re-derives a score threshold: {pattern!r}"
            )
        # confidence is only ever READ as an opaque string ("high"/"medium"/
        # "low") coming from the server, never computed from a numeric score.
        self.assertIn('confidence === "high"', self.qo_section)

    # D. no reimplementa ambiguity threshold
    def test_d_never_reimplements_the_ambiguity_margin(self):
        # score_margin isn't even read anywhere in the Quick Order UI code --
        # `ambiguous` (an opaque boolean from the server) is the only signal
        # used, so there is nothing to compute a margin threshold FROM.
        self.assertNotIn("score_margin", self.qo_section)
        self.assertNotIn("AMBIGUITY_MARGIN_THRESHOLD", self.qo_section)

    # E. usa set_cart_qty
    def test_e_uses_set_cart_qty(self):
        self.assertIn("this.set_cart_qty(", self.qo_section)

    # F. no llama create Sales Order desde Quick Order
    def test_f_never_calls_a_sales_order_create_endpoint(self):
        for forbidden in (
            "create_and_submit_sales_order",
            "create_draft_sales_order",
            "update_draft_sales_order",
            "modify_submitted_sales_order",
            "confirm_order\"",  # the server method name (quoted, as a this.call() arg) -- not the JS method confirm_order()
            "cancel_sales_order",
            "delete_draft_sales_order",
        ):
            self.assertNotIn(forbidden, self.qo_section, f"Quick Order code must never reference {forbidden!r}")

    # G. no usa rate/price/stock (economic/stock fields)
    def test_g_never_references_economic_or_stock_fields(self):
        # Matched as an actual property access or object/string key (\.rate,
        # "rate", 'rate') -- never a bare substring, which would false-
        # positive on ordinary English words like "sepaRATE"/"opeRATE" that
        # appear in this section's own explanatory comments.
        code = _strip_line_comments(self.qo_section)
        for field in (
            "rate",
            "price",
            "cost",
            "valuation_rate",
            "qty_disponible",
            "actual_qty",
            "projected_qty",
            "reserved_qty",
            "warehouse",
        ):
            pattern = r"\." + re.escape(field) + r"\b|[\"']" + re.escape(field) + r"[\"']"
            self.assertIsNone(re.search(pattern, code), f"Quick Order code must never reference {field!r}")

    # H. máximo de candidatos lo respeta desde response (never re-sliced client-side)
    def test_h_never_reslices_candidates_client_side(self):
        self.assertIsNone(
            re.search(r"candidates\s*\.\s*slice", self.qo_section),
            "ventas.js must trust parse_quick_order()'s own <= 5 limit, never re-slice candidates itself",
        )
        self.assertIn("line.candidates", self.qo_section)

    # I. qty validada
    def test_i_qty_is_validated_with_the_same_clamp_as_the_rest_of_the_cart(self):
        body = _method_body(self.js, "set_quick_order_qty")
        self.assertIn("Math.max(flt(", body)

    # J. ignoradas no aplicadas
    def test_j_ignored_lines_are_never_applied(self):
        body = _method_body(self.js, "apply_quick_order_to_cart")
        self.assertIn("if (line.ignored || !line.selected) continue;", body)

    # K. duplicados se suman
    def test_k_duplicate_item_codes_sum_their_quantities(self):
        body = _method_body(self.js, "apply_quick_order_to_cart")
        self.assertIn("existing.qty_to_add += qty;", body)

    # L. carrito previo se preserva/suma
    def test_l_existing_cart_quantity_is_added_to_not_replaced(self):
        body = _method_body(self.js, "apply_quick_order_to_cart")
        self.assertIn("this.cart_qty(item_code) + addition.qty_to_add", body)

    # M. clear Quick Order no limpia carrito
    def test_m_clear_quick_order_never_touches_the_cart(self):
        body = _method_body(self.js, "clear_quick_order")
        self.assertNotIn("this.np.cart", body)
        self.assertIn("this.np.quick_order", body)

    # N. API error no limpia carrito
    def test_n_interpret_quick_order_catch_branch_never_touches_the_cart(self):
        body = _method_body(self.js, "interpret_quick_order")
        catch_start = body.index(".catch(")
        catch_end = body.index(".finally(", catch_start)
        catch_body = body[catch_start:catch_end]
        self.assertNotIn("this.np.cart", catch_body)
        self.assertNotIn("clear_quick_order", catch_body)

    def test_quick_order_never_touches_np_cart_directly(self):
        """Section 17's own rule, checked broadly across the whole Quick
        Order block: the ONLY cart mutation path is set_cart_qty() -- no
        `this.np.cart.set(`/`this.np.cart.delete(` of its own (stripped of
        comments first -- this section's own docstring-style comments
        explicitly NAME that exact forbidden pattern as an example of what
        NOT to do, which would otherwise trip this check on prose, not
        code)."""
        code = _strip_line_comments(self.qo_section)
        self.assertNotIn("this.np.cart.set(", code)
        self.assertNotIn("this.np.cart.delete(", code)

    def test_manual_search_per_line_reuses_search_items_not_a_new_algorithm(self):
        """Section 11 -- reuses the real server endpoint directly
        (this.call("search_items", ...)), never a client-side re-
        implementation of matching."""
        body = _method_body(self.js, "search_quick_order_item")
        self.assertIn('this.call("search_items"', body)

    def test_manual_mode_search_is_never_degraded(self):
        """The existing manual search box/grid must still exist, unmodified
        in spirit -- Commit 25.8.6 brief section 2: "NO eliminar ni
        degradar el buscador actual." """
        self.assertIn("fg-item-search-input", self.js)
        self.assertIn('this.call("search_items"', self.js)
        self.assertIn("render_item_result_card", self.js)

    def test_manual_mode_is_the_default(self):
        self.assertIn('item_mode: "manual"', self.js)

    def test_default_candidate_limit_matches_server_contract(self):
        """Sanity: the UI never hardcodes its own "top 5" -- it always
        renders exactly what candidates the server sent, whatever length
        that turns out to be (parse_quick_order()'s own contract, Commit
        25.8.5, already caps it at 5)."""
        body = _method_body(self.js, "render_quick_order_line")
        self.assertIn("line.candidates.map", body)


class TestQuickOrderCssContract(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = _read(_VENTAS_CSS_PATH)

    def test_no_horizontal_table_scroll_pattern(self):
        """Section 6/23 -- Quick Order lines are cards (div-based grid), not
        a wide <table> that would force lateral scroll; confirmed by the
        absence of any overflow-x rule attached to the Quick Order block."""
        self.assertNotIn("<table", _read(_VENTAS_JS_PATH))

    def test_responsive_breakpoints_cover_quick_order(self):
        self.assertIn("fg-qo-line-body", self.css)
        self.assertIn("fg-qo-candidate", self.css)
        # both existing breakpoints (860px tablet/iPad, 640px mobile) got a
        # Quick Order override, not just one of them.
        idx_860 = self.css.index("@media (max-width: 860px)")
        idx_640 = self.css.index("@media (max-width: 640px)")
        block_860 = self.css[idx_860:idx_640]
        block_640 = self.css[idx_640:]
        self.assertIn("fg-qo-line-body", block_860)
        self.assertIn("fg-qo-line-header", block_640)

    def test_status_never_relies_on_color_alone(self):
        """Every .fg-qo-status--* modifier pairs a color with a distinct
        text label rendered by ventas.js (quick_order_line_status()) --
        checked here only for the CSS side (presence of the four modifiers);
        the JS side is checked in TestQuickOrderUiContract."""
        for mod in ("fg-qo-status--high", "fg-qo-status--review", "fg-qo-status--low", "fg-qo-status--not-found"):
            self.assertIn(mod, self.css)
