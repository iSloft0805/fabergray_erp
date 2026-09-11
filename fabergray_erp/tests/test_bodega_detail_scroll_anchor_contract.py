# -*- coding: utf-8 -*-
"""Hotfix 25.20.3 -- static contract tests for bodega.js's Pick List
detail-refresh scroll-position fix.

Hotfix 25.20.2 targeted `window.scrollBy()` and did NOT work -- the user
confirmed the jump still happened. Root cause, confirmed by a structural
audit (not guessed): this Frappe Desk build renders every Page's body
inside `.main-section` (frappe/www/desk.html), and frappe/public/scss/
desk/main.scss gives that element `height: 100vh; overflow: scroll;
overflow-x: hidden;` -- per the CSS Overflow spec, an `overflow-y:
visible` paired with a non-"visible" overflow-x on the same box computes
to `auto`, so `.main-section` is the element that actually scrolls
internally, and `window` itself never scrolls at all in this Desk build.
25.20.2's fix was a no-op against the wrong target.

25.20.3's fix has two parts:
1. load_detail() no longer shows render_skeleton_detail() for an
   in-place refresh of an ALREADY-open detail (qty +/-, reportar
   faltante, or the manual "Actualizar" tap) -- only for a genuine fresh
   navigation. The skeleton's own much-shorter markup collapsing
   .fg-body's height was itself a major contributor to the jump, on top
   of targeting the wrong scroll element.
2. get_detail_scroll_container() discovers the REAL scrollable ancestor
   at runtime (never hardcoded, never assumed to be `window`), and
   restore_detail_scroll_anchor() corrects that container's own
   scrollTop by the exact delta, after two animation frames (giving Desk
   chrome layout a chance to settle), with a raw-scrollTop fallback if
   the touched row can no longer be found.

Same convention as test_ventas_confirm_button_contract.py: this app has
no JS test runner, so these read the real source as text and assert on
it. Backend behaviour (set_picked_qty/report_shortage/finish_picking
themselves, all untouched by this hotfix) stays covered end-to-end by
test_bodega_flow.py/test_bodega_qty_stepper.py/
test_bodega_report_shortage_idempotency.py, unaffected by this file.

IMPORTANT (documented honestly, not glossed over): this environment has
no interactive browser, so the live instrumentation ([FG BODEGA SCROLL]
console logging) the hotfix brief asked for could not actually be run or
observed here -- the root cause above was established by reading Frappe's
own shipped CSS/HTML instead (frappe/www/desk.html,
frappe/public/scss/desk/main.scss), which is the more reliable source
for "what container really scrolls" than a guess. These static tests
pin the MECHANICS of the fix (skeleton skipped on refresh, container
discovered dynamically, delta-based correction, two-frame wait,
raw-scrollTop fallback) -- they cannot themselves prove the visual
symptom is gone. Manual verification in a real browser (steps in this
hotfix's own STOP AND REPORT) is still required before calling this
resolved.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_BODEGA_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "bodega", "bodega.js"
)


def _read():
	with open(_BODEGA_JS_PATH, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_ventas_confirm_button_contract.py's own --
	extracts one class method's body by name, from its own `name(...) {`
	line to the next `\\tidentifier(...) {` at the same one-tab
	indentation, or end of string if it's the last method."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found in bodega.js")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


class TestNoHardReloadOrRouteJumpAnywhereInBodega(IntegrationTestCase):
	"""I. no existe location.reload. J. no existe scroll-to-top explícito."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_i_no_location_reload_call_anywhere_in_the_file(self):
		"""Line-based, comment-aware: the file's own comments mention
		`window.location.reload()` (as the thing this hotfix deliberately
		does NOT do) -- full-line `//` comments are skipped so those don't
		produce a false failure, while any REAL call anywhere still fails
		this test."""
		for line in self.js.splitlines():
			if line.strip().startswith("//"):
				continue
			self.assertNotIn("location.reload", line, "a non-comment line calls location.reload")

	def test_no_frappe_set_route_in_the_detail_refresh_flow(self):
		for method in ("load_detail", "sync_row", "open_report_shortage_dialog", "finish_picking"):
			body = _method_body(self.js, method)
			self.assertNotIn("frappe.set_route", body, f"{method}() unexpectedly calls frappe.set_route")

	def test_j_no_explicit_scroll_to_top_anywhere_in_the_detail_flow(self):
		"""No scrollTo(0, ...)/scrollTop = 0/scrollIntoView({block: "start"})
		anywhere in the detail-refresh-adjacent methods."""
		forbidden_patterns = (
			r"scrollTo\(\s*0\s*,\s*0",
			r"scrollTop\s*=\s*0\b",
			r'scrollIntoView\(\s*\{\s*block:\s*"start"',
		)
		for method in (
			"load_detail",
			"sync_row",
			"open_report_shortage_dialog",
			"finish_picking",
			"capture_detail_scroll_anchor",
			"restore_detail_scroll_anchor",
			"get_detail_scroll_container",
		):
			body = _method_body(self.js, method)
			for pattern in forbidden_patterns:
				self.assertNotRegex(body, pattern, f"{method}() unexpectedly resets scroll to the top")


class TestSkeletonOnlyOnFreshNavigation(IntegrationTestCase):
	"""A. refresh de cantidad NO llama render_skeleton_detail().
	B. initial load SÍ puede llamar render_skeleton_detail().
	C. refresh usa preserve_scroll (auto-detected, no new public API)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_a_skeleton_call_is_gated_by_is_refresh(self):
		body = _method_body(self.js, "load_detail")
		self.assertIn("if (!is_refresh && this.$body) this.render_skeleton_detail();", body)

	def test_b_render_skeleton_detail_still_exists_for_fresh_opens(self):
		"""Confirms the skeleton was gated, not removed (section 3's own
		explicit "no eliminar skeleton salvo necesidad real")."""
		self.assertIn("render_skeleton_detail() {", self.js)

	def test_c_is_refresh_detected_from_existing_state_no_new_call_signature(self):
		"""load_detail(pick_list) keeps its original single-argument
		signature -- the refresh/fresh-open distinction is derived from
		state already on `this`, per the brief's own "no hacer una API
		nueva si no hace falta"."""
		body = _method_body(self.js, "load_detail")
		self.assertIn(
			'const is_refresh = this.state.view === "detail" && this.state.pick_list === pick_list && !!this.state.detail;',
			body,
		)
		self.assertIn("load_detail(pick_list) {", self.js)
		self.assertNotIn("load_detail({", self.js)

	def test_capture_only_gated_by_is_refresh(self):
		body = _method_body(self.js, "load_detail")
		self.assertIn("const scroll_ctx = is_refresh ? this.capture_detail_scroll_anchor() : null;", body)

	def test_restore_only_called_for_a_refresh(self):
		body = _method_body(self.js, "load_detail")
		self.assertIn("if (is_refresh) this.restore_detail_scroll_anchor(scroll_ctx);", body)


class TestRealScrollContainerDiscoveredAtRuntime(IntegrationTestCase):
	"""D. helper detecta scrollable ancestor. E. fallback usa
	document.scrollingElement."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_d_container_helper_walks_ancestors_checking_real_overflow(self):
		body = _method_body(self.js, "get_detail_scroll_container")
		self.assertIn("el.parentElement", body)
		self.assertIn("getComputedStyle", body)
		self.assertRegex(body, r'style\.overflowY\s*===\s*"auto"')
		self.assertRegex(body, r'style\.overflowY\s*===\s*"scroll"')
		self.assertIn("el.scrollHeight > el.clientHeight", body)

	def test_d_never_hardcodes_main_section_or_any_specific_frappe_class(self):
		body = _method_body(self.js, "get_detail_scroll_container")
		self.assertNotIn("main-section", body)
		self.assertNotIn(".page-container", body)

	def test_e_fallback_is_document_scrolling_element(self):
		body = _method_body(self.js, "get_detail_scroll_container")
		self.assertIn("document.scrollingElement", body)

	def test_no_method_hardcodes_window_as_the_scroll_target_anymore(self):
		"""25.20.2's own window.scrollBy() is gone -- every scroll
		correction now goes through the dynamically-discovered container."""
		for method in ("capture_detail_scroll_anchor", "restore_detail_scroll_anchor"):
			body = _method_body(self.js, method)
			self.assertNotIn("window.scrollBy", body)
			self.assertNotIn("window.scrollY", body)


class TestRestoreMechanism(IntegrationTestCase):
	"""F. restore espera layout mediante requestAnimationFrame.
	G. restore utiliza delta relativo al container.
	H. si anchor desaparece, conserva previousScrollTop."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_f_restore_waits_two_animation_frames(self):
		body = _method_body(self.js, "restore_detail_scroll_anchor")
		self.assertEqual(body.count("requestAnimationFrame("), 2, "expected exactly two nested requestAnimationFrame calls")

	def test_g_restore_computes_a_delta_relative_to_the_container_not_an_absolute_value(self):
		body = _method_body(self.js, "restore_detail_scroll_anchor")
		self.assertIn("const delta = new_top - ctx.anchor_top;", body)
		self.assertIn("container.scrollTop += delta;", body)

	def test_g_container_top_offset_handles_document_scrolling_element_specially(self):
		"""containerRect.top must read as 0 for document.scrollingElement
		(it has no meaningful bounding rect of its own for this purpose),
		per the brief's own explicit pseudocode."""
		body = _method_body(self.js, "restore_detail_scroll_anchor")
		self.assertIn('container === document.scrollingElement ? 0 : container.getBoundingClientRect().top', body)

	def test_h_fallback_restores_previous_scroll_top_never_resets_to_zero(self):
		body = _method_body(self.js, "restore_detail_scroll_anchor")
		self.assertIn("container.scrollTop = ctx.container_scroll_top;", body)
		self.assertNotIn("container.scrollTop = 0", body)

	def test_capture_also_records_the_raw_scroll_top_for_the_fallback(self):
		body = _method_body(self.js, "capture_detail_scroll_anchor")
		self.assertIn("const container_scroll_top = container.scrollTop;", body)
		self.assertIn("container_scroll_top", body)

	# G/H (identifier). identificador estable, nunca un índice visual.
	def test_item_card_data_row_uses_the_real_pick_list_item_row_name(self):
		body = _method_body(self.js, "render_item_card")
		self.assertIn('data-row="${frappe.utils.escape_html(row.row_name)}"', body)

	def test_anchor_lookup_never_uses_a_visual_index(self):
		for method in ("capture_detail_scroll_anchor", "restore_detail_scroll_anchor", "find_item_card_el"):
			body = _method_body(self.js, method)
			self.assertNotRegex(
				body,
				r"\.eq\(|\[\s*i\s*\]|\[\s*index\s*\]",
				f"{method}() must locate the card by row_name, never by a positional index",
			)
		body = _method_body(self.js, "find_item_card_el")
		self.assertIn("el.dataset.row === row_name", body)


class TestQtyStepperAndShortageBothTriggerAnUngatedRefresh(IntegrationTestCase):
	"""K. report_shortage también usa refresh sin skeleton -- and so does
	the qty stepper (+/-) -- both simply call load_detail() the same way
	they always did; the skip-skeleton/preserve-scroll behaviour is
	entirely load_detail()'s own auto-detection (state.view/state.
	pick_list), so neither call site needed to change at all."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_sync_row_sets_last_changed_row_before_reloading_detail_success(self):
		body = _method_body(self.js, "sync_row")
		then_branch = body.split(".catch(")[0]
		set_pos = then_branch.index("this.last_changed_row = row_name;")
		load_pos = then_branch.index("this.load_detail(")
		self.assertLess(set_pos, load_pos)

	def test_sync_row_catch_branch_also_preserves_anchor(self):
		"""A rejected set_picked_qty (over limit, doc closed, etc.) still
		reloads the SAME row the operator was touching."""
		body = _method_body(self.js, "sync_row")
		catch_branch = body.split(".catch(", 1)[1]
		set_pos = catch_branch.index("this.last_changed_row = row_name;")
		load_pos = catch_branch.index("this.load_detail(")
		self.assertLess(set_pos, load_pos)

	def test_k_report_shortage_sets_last_changed_row_before_reloading_detail(self):
		body = _method_body(self.js, "open_report_shortage_dialog")
		set_pos = body.index("this.last_changed_row = row_name;")
		load_pos = body.index("this.load_detail(")
		self.assertLess(set_pos, load_pos)

	def test_k_report_shortage_calls_the_same_load_detail_no_new_options(self):
		"""No caller-supplied preserve_scroll/show_skeleton flags -- confirms
		load_detail()'s single-argument call site is unchanged here too."""
		body = _method_body(self.js, "open_report_shortage_dialog")
		self.assertIn("this.load_detail(this.state.pick_list);", body)


class TestFocusAudit(IntegrationTestCase):
	"""Section 9 -- confirms no .focus()/autofocus/frappe.utils.set_focus
	anywhere in this file that could itself cause a browser auto-scroll."""

	def test_no_focus_calls_anywhere_in_bodega_js(self):
		js = _read()
		self.assertNotIn(".focus(", js)
		self.assertNotIn("autofocus", js)
		self.assertNotIn('trigger("focus")', js)
		self.assertNotIn("frappe.utils.set_focus", js)


class TestSearchAndFiltersUntouchedByDetailRefresh(IntegrationTestCase):
	"""Commit 25.20's búsqueda/filtro no se resetea al refrescar el
	detalle -- load_detail()/capture/restore never reference any of the
	orders/history search-and-filter state fields."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_load_detail_never_touches_orders_or_history_search_state(self):
		for method in (
			"load_detail",
			"capture_detail_scroll_anchor",
			"restore_detail_scroll_anchor",
			"get_detail_scroll_container",
		):
			body = _method_body(self.js, method)
			for forbidden in (
				"orders_search",
				"history_search",
				"orders_filter",
				"history_date_from",
				"history_date_to",
			):
				self.assertNotIn(forbidden, body, f"{method}() unexpectedly references {forbidden!r}")


class TestProgressStillRefreshesEveryRender(IntegrationTestCase):
	"""E. (section 10 original numbering) la barra de progreso sigue
	recalculándose en cada render -- la preservación de scroll no debe
	"congelar" el progreso mostrado."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_progress_card_markup_still_driven_by_fresh_picked_lines_and_pct(self):
		body = _method_body(self.js, "render_detail_html")
		self.assertIn("picked_lines", body)
		self.assertIn("fg-progress-fill", body)
		self.assertIn("${pct}%", body)


class TestFinishPickingAndBackNavigationUnaffected(IntegrationTestCase):
	"""L. finish_picking mantiene su comportamiento actual. Back button
	navigation unaffected."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_l_finish_picking_still_calls_the_real_endpoint(self):
		body = _method_body(self.js, "finish_picking")
		self.assertIn('this.call("finish_picking"', body)

	def test_l_finish_picking_success_path_still_navigates_to_the_list(self):
		"""Unchanged on purpose -- a successful finish is a real
		navigation away from the detail, not an in-place refresh."""
		body = _method_body(self.js, "finish_picking")
		self.assertIn('this.state.view = "list";', body)

	def test_back_button_still_wired_to_back_to_list(self):
		body = _method_body(self.js, "bind_detail_events")
		self.assertIn(".fg-back-btn", body)
		self.assertIn("this.back_to_list()", body)
