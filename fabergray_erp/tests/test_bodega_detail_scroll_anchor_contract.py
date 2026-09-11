# -*- coding: utf-8 -*-
"""Hotfix 25.20.2 -- static contract tests for bodega.js's Pick List
detail-refresh scroll-position fix.

Root cause (confirmed by source audit, not guessed): load_detail() always
fully replaces .fg-body's innerHTML -- via a much-shorter
render_skeleton_detail() first -- after every set_picked_qty/
report_shortage/finish_picking(catch) re-sync. Neither
window.location.reload() nor frappe.set_route() nor an explicit scrollTo
appear anywhere in this flow (confirmed below); the jump is a side effect
of the window's own absolute scroll offset pointing at different content
once .fg-body's height changes. bodega.css confirms .fg-bodega/.fg-header/
.fg-body carry no overflow/fixed-height rule of their own, so the real
scroll container is `window` on both desktop and mobile.

The fix: capture the just-touched row's viewport-relative position
(row.row_name -- already the stable `data-row` on every .fg-item-card,
never a visual index) before load_detail() wipes the screen, then correct
`window` scroll by the exact delta once the real content is back.

Same convention as test_ventas_confirm_button_contract.py: this app has no
JS test runner, so these read the real source as text and assert on it.
Backend behaviour (set_picked_qty/report_shortage/finish_picking
themselves, all untouched by this hotfix) stays covered end-to-end by
test_bodega_flow.py/test_bodega_qty_stepper.py/
test_bodega_report_shortage_idempotency.py, unaffected by this file.
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
	"""A. no existe location.reload en flujo de set_picked_qty (section 1/2)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_a_no_location_reload_call_anywhere_in_the_file(self):
		"""Line-based, comment-aware: the file's own comments mention
		`window.location.reload()` (as the thing this hotfix deliberately
		does NOT do, e.g. right on load_detail()'s own preceding doc
		comment) -- full-line `//` comments are skipped so those don't
		produce a false failure, while any REAL call anywhere in the file
		still fails this test."""
		for line in self.js.splitlines():
			if line.strip().startswith("//"):
				continue
			self.assertNotIn("location.reload", line, "a non-comment line calls location.reload")

	def test_no_frappe_set_route_in_the_detail_refresh_flow(self):
		for method in ("load_detail", "sync_row", "open_report_shortage_dialog", "finish_picking"):
			body = _method_body(self.js, method)
			self.assertNotIn("frappe.set_route", body, f"{method}() unexpectedly calls frappe.set_route")


class TestScrollAnchorMechanism(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	# B/C. actualizar cantidad / completar ítem conservan el anchor --
	# both go through the exact same sync_row() .then() path (completing a
	# row is just the qty reaching qty_solicitada through the same
	# set_picked_qty call), so one assertion on sync_row() covers both.
	def test_b_c_sync_row_sets_last_changed_row_before_reloading_detail(self):
		body = _method_body(self.js, "sync_row")
		then_branch = body.split(".catch(")[0]
		set_pos = then_branch.index("this.last_changed_row = row_name;")
		load_pos = then_branch.index("this.load_detail(")
		self.assertLess(
			set_pos,
			load_pos,
			"sync_row()'s success branch must set last_changed_row BEFORE calling load_detail()",
		)

	def test_b_c_sync_row_catch_branch_also_preserves_anchor(self):
		"""A rejected set_picked_qty (over limit, doc closed, etc.) still
		reloads the SAME row the operator was touching -- never silently
		drops back to the top on a validation error either."""
		body = _method_body(self.js, "sync_row")
		catch_branch = body.split(".catch(", 1)[1]
		set_pos = catch_branch.index("this.last_changed_row = row_name;")
		load_pos = catch_branch.index("this.load_detail(")
		self.assertLess(set_pos, load_pos)

	# D. reportar faltante conserva anchor.
	def test_d_report_shortage_sets_last_changed_row_before_reloading_detail(self):
		body = _method_body(self.js, "open_report_shortage_dialog")
		set_pos = body.index("this.last_changed_row = row_name;")
		load_pos = body.index("this.load_detail(")
		self.assertLess(set_pos, load_pos)

	def test_load_detail_captures_anchor_before_the_skeleton_wipes_it(self):
		"""Matched against the literal call statements (`this.` prefix +
		trailing `();`), not a bare substring -- load_detail()'s own
		leading comment also mentions "render_skeleton_detail()" by name,
		which would otherwise produce a false ordering result."""
		body = _method_body(self.js, "load_detail")
		capture_pos = body.index("this.capture_detail_scroll_anchor();")
		skeleton_pos = body.index("this.render_skeleton_detail();")
		self.assertLess(
			capture_pos,
			skeleton_pos,
			"the anchor must be captured while the real .fg-item-card is still on screen, before the skeleton replaces it",
		)

	def test_load_detail_restores_anchor_after_the_real_render(self):
		body = _method_body(self.js, "load_detail")
		render_pos = body.index("this.render_body();")
		restore_pos = body.index("restore_detail_scroll_anchor(")
		self.assertLess(render_pos, restore_pos)

	def test_capture_only_applies_to_an_already_open_detail_not_a_fresh_navigation(self):
		"""A first-time open (state.view still "list" when load_detail() is
		called) must never inherit a stale anchor from a previous screen --
		it should legitimately start at the top."""
		body = _method_body(self.js, "capture_detail_scroll_anchor")
		self.assertIn('this.state.view !== "detail"', body)

	# G/H. identificador estable, nunca un índice visual.
	def test_g_item_card_data_row_uses_the_real_pick_list_item_row_name(self):
		body = _method_body(self.js, "render_item_card")
		self.assertIn('data-row="${frappe.utils.escape_html(row.row_name)}"', body)

	def test_h_anchor_lookup_never_uses_a_visual_index(self):
		for method in ("capture_detail_scroll_anchor", "restore_detail_scroll_anchor", "find_item_card_el"):
			body = _method_body(self.js, method)
			self.assertNotRegex(
				body,
				r"\.eq\(|\[\s*i\s*\]|\[\s*index\s*\]",
				f"{method}() must locate the card by row_name, never by a positional index",
			)
		body = _method_body(self.js, "find_item_card_el")
		self.assertIn("el.dataset.row === row_name", body)

	# No internal scrollable container -- confirms the fix correctly
	# targets `window`, not a made-up inner div.
	def test_restore_uses_window_scrollby_not_an_invented_container(self):
		body = _method_body(self.js, "restore_detail_scroll_anchor")
		self.assertIn("window.scrollBy(", body)


class TestSearchAndFiltersUntouchedByDetailRefresh(IntegrationTestCase):
	"""F. búsqueda/filtro activo (Commit 25.20) no se resetea al refrescar
	el detalle -- load_detail()/capture/restore never reference any of the
	orders/history search-and-filter state fields."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_load_detail_never_touches_orders_or_history_search_state(self):
		for method in ("load_detail", "capture_detail_scroll_anchor", "restore_detail_scroll_anchor"):
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
	"""E. la barra de progreso sigue recalculándose en cada render -- la
	preservación de scroll no debe "congelar" el progreso mostrado."""

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
	"""I. no rompe finish_picking. J. no rompe "Volver a pedidos"."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read()

	def test_i_finish_picking_still_calls_the_real_endpoint(self):
		body = _method_body(self.js, "finish_picking")
		self.assertIn('this.call("finish_picking"', body)

	def test_j_back_button_still_wired_to_back_to_list(self):
		body = _method_body(self.js, "bind_detail_events")
		self.assertIn(".fg-back-btn", body)
		self.assertIn("this.back_to_list()", body)
