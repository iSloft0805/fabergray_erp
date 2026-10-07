# -*- coding: utf-8 -*-
"""Jefe de Bodega -- "Validar pedidos con faltantes resueltos"
(api.jefe_bodega.validate_resolved_shortage_orders(),
fulfillment.resolved_shortage_service).

Real stock (fixtures.stock_up_real()) in this suite's own throwaway
warehouses, the real Bodega flow for picking/shortage reports, and a real
.save() to resolve each report (so the Version log records the transition,
exactly like production). Discovery is scoped to each test's own reports
(_run()) so a run never touches whatever else the dev site resolved today;
everything below discovery is the real code path."""

import os
import threading
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega, jefe_bodega
from fabergray_erp.fulfillment import resolved_shortage_service as rss
from fabergray_erp.fulfillment import stock_issue_service
from fabergray_erp.fulfillment.remainder_service import ensure_remaining_pick_list
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests.test_inventario_out_stock_issue import RATE, _StockIssueMixin, _actual, _stock_issues

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_JS = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "jefe_de_bodega", "jefe_de_bodega.js"
)


class TestResolvedShortageValidation(_StockIssueMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_world("FGRSV")
		cls.jefe = cls.world.user(f"fgrsv-jefe-{cls.tag.lower()}@example.com", ["Jefe de Bodega"])
		cls.all_reports = []
		cls.addClassCleanup(cls._purge_report_versions)
		# The service commits per Pick List and some tests roll back: the
		# shared fixtures must already be committed (TestWorld cleans them).
		frappe.db.commit()

	@classmethod
	def _purge_report_versions(cls):
		if cls.all_reports:
			frappe.db.delete("Version", {"ref_doctype": "Reporte de Faltante", "docname": ["in", cls.all_reports]})
			frappe.db.commit()

	def setUp(self):
		self.my_reports = []

	# -- helpers ----------------------------------------------------------

	def _reports(self, pick_list):
		return frappe.get_all(
			"Reporte de Faltante",
			filters={"pick_list": pick_list},
			fields=["name", "item_code", "pick_list_item", "status", "qty_faltante", "shortage_reason"],
		)

	def _report_for(self, pick_list, item_code=None):
		reports = [r for r in self._reports(pick_list) if not item_code or r.item_code == item_code]
		self.assertEqual(len(reports), 1, reports)
		return reports[0]

	def _resolve(self, report_name):
		"""Real save, like every resolving path: the Version log records it."""
		doc = frappe.get_doc("Reporte de Faltante", report_name)
		doc.status = "Resuelto"
		# ignore_version=False: Frappe skips Version rows under frappe.in_test
		# by default; production always records them (same as
		# test_reporte_faltantes.py).
		doc.save(ignore_version=False)
		self.my_reports.append(report_name)
		self.all_reports.append(report_name)

	def _run(self, user=None, limit=None):
		# A real request starts with nothing pending: the service rolls back
		# a refused Pick List, which must never take this test's fixtures
		# with it.
		frappe.db.commit()
		original = rss._reports_resolved_today
		mine = set(self.my_reports)

		def scoped():
			return [r for r in original() if r.name in mine]

		with patch.object(rss, "_reports_resolved_today", scoped), fx.as_user(user or self.jefe):
			if limit is not None:
				return rss.validate_resolved_shortage_orders(limit=limit)
			return jefe_bodega.validate_resolved_shortage_orders()

	def _row(self, result, pick_list):
		rows = [r for r in result["results"] if r["pick_list"] == pick_list]
		self.assertEqual(len(rows), 1, result)
		return rows[0]

	def _pick_lists_of(self, so_name):
		return set(
			frappe.get_all(
				"Pick List Item",
				filters={"sales_order": so_name, "docstatus": ["!=", 2]},
				pluck="parent",
				distinct=True,
			)
		)

	def _draft_with_resolved_shortage(self, qty=4, stock_after=4):
		"""Order of `qty`, nothing in stock: Bodega picks 0 and reports it;
		then stock arrives (`stock_after`) and the report is resolved."""
		item = self._item(self.wh_liq)
		so, pl = self._order((item, self.wh_liq, qty))
		self._pick(pl, {item: 0})
		report = self._report_for(pl)
		if stock_after:
			self.world.stock_up_real(item, self.wh_liq, stock_after, rate=RATE)
		self._resolve(report.name)
		return item, so, pl, report

	def _picked(self, pick_list):
		return [flt(r.picked_qty) for r in frappe.get_doc("Pick List", pick_list).locations]

	# =====================================================================
	# Original Pick List still a draft
	# =====================================================================

	def test_draft_original_with_stock_is_completed_in_place(self):
		item, so, pl, report = self._draft_with_resolved_shortage()

		result = self._run()

		row = self._row(result, pl)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(row["sales_order"], so.name)
		self.assertTrue(row["customer"])
		self.assertEqual(result["summary"]["completed"], 1)
		self.assertEqual(result["remaining_count"], 0)

		doc = frappe.get_doc("Pick List", pl)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(self._picked(pl), [4])
		self.assertEqual(doc.fg_started_by, self.bodega_user)  # already started: kept
		issues = _stock_issues(pl)
		self.assertEqual([i.name for i in issues], [row["material_issue"]])
		self.assertEqual(_actual(item, self.wh_liq), 0)

		# The report itself is never touched.
		after = frappe.get_doc("Reporte de Faltante", report.name)
		self.assertEqual(
			(after.status, flt(after.qty_faltante), after.shortage_reason),
			("Resuelto", flt(report.qty_faltante), report.shortage_reason),
		)

	def test_running_twice_never_duplicates_the_issue(self):
		item, so, pl, _report = self._draft_with_resolved_shortage()

		first = self._run()
		second = self._run()

		self.assertEqual(self._row(first, pl)["status"], rss.STATUS_COMPLETED)
		self.assertEqual([r for r in second["results"] if r["status"] == rss.STATUS_COMPLETED], [])
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 0)
		self.assertEqual(self._pick_lists_of(so.name), {pl})  # no remainder for a fully issued line

	def test_concurrent_runs_complete_once(self):
		item, _so, pl, report = self._draft_with_resolved_shortage()
		frappe.db.commit()
		target = {"reports": [report.name]}

		site = frappe.local.site
		results = [None, None]
		barrier = threading.Barrier(2)

		def attempt(index):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.jefe)
			try:
				barrier.wait(timeout=30)
				results[index] = rss._process_isolated(pl, target)
			except Exception as e:
				results[index] = {"status": type(e).__name__, "detail": str(e)}
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=120)
		frappe.db.rollback()

		self.assertEqual(
			sorted(r["status"] for r in results),
			sorted([rss.STATUS_COMPLETED, rss.STATUS_ALREADY_COMPLETED]),
			results,
		)
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 0)

	def test_insufficient_real_stock_is_still_short_and_writes_nothing(self):
		item, _so, pl, _report = self._draft_with_resolved_shortage(qty=4, stock_after=2)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			[(s["item_code"], s["warehouse"], s["required_qty"], s["available_qty"], s["shortage_qty"]) for s in row["shortages"]],
			[(item, self.wh_liq, 4, 2, 2)],
		)
		self.assertEqual(
			row["detail"], f"{item} — {self.wh_liq} · Requerido: 4 · Disponible: 2 · Faltante: 2"
		)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(self._picked(pl), [0])
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(_actual(item, self.wh_liq), 2)

	def test_stock_physically_picked_for_another_open_pick_list_is_not_taken(self):
		item = self._item(self.wh_liq)
		_so_a, pl_a = self._order((item, self.wh_liq, 4))
		self._pick(pl_a, {item: 0})
		report = self._report_for(pl_a)
		self.world.stock_up_real(item, self.wh_liq, 4, rate=RATE)
		_so_b, pl_b = self._order((item, self.wh_liq, 4))
		self._pick(pl_b)  # Bodega set the 4 aside for B, not completed yet
		self._resolve(report.name)

		row = self._row(self._run(), pl_a)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(row["shortages"][0]["available_qty"], 0)
		self.assertEqual(frappe.db.get_value("Pick List", pl_a, "docstatus"), 0)
		self.assertEqual(self._picked(pl_a), [0])
		self.assertEqual(self._picked(pl_b), [4])
		self.assertEqual(frappe.db.get_value("Pick List", pl_b, "docstatus"), 0)
		self.assertEqual(_actual(item, self.wh_liq), 4)

	# =====================================================================
	# The WHOLE Pick List is re-validated, not only the resolved line
	# =====================================================================

	def _start_and_report(self, pl, item_code):
		"""Bodega starts the Pick List and reports ONE line short at 0; every
		other line stays untouched (picked 0, no report)."""
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl)
			row = next(r for r in bodega.get_pick_list(pl)["rows"] if r["item_code"] == item_code)
			bodega.set_picked_qty(pl, row["row_name"], 0)
			report = bodega.report_shortage(pl, row["row_name"], 0, "Stock insuficiente")
		self.world.track_existing("Reporte de Faltante", report["name"])
		return report["name"]

	def _three_line_order(self, stock=(4, 4, 4)):
		"""Pedido de 3 líneas (4 c/u), sin stock al crearlo; Bodega solo
		reporta la primera; luego llega `stock` por línea y se resuelve."""
		items = [self._item(self.wh_liq) for _i in range(3)]
		_so, pl = self._order(*[(i, self.wh_liq, 4) for i in items])
		report = self._start_and_report(pl, items[0])
		for item, qty in zip(items, stock):
			if qty:
				self.world.stock_up_real(item, self.wh_liq, qty, rate=RATE)
		self._resolve(report)
		return items, pl

	def _comments(self, pick_list):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Pick List", "reference_name": pick_list, "comment_type": "Comment"},
			pluck="content",
			order_by="creation asc",
		)

	def test_unpicked_lines_with_stock_are_filled_and_completed(self):
		items, pl = self._three_line_order()

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._picked(pl), [4, 4, 4])
		[issue] = _stock_issues(pl)
		self.assertEqual(
			sorted((d.item_code, flt(d.qty)) for d in frappe.get_doc("Stock Entry", issue.name).items),
			sorted((i, 4) for i in items),
		)
		self.assertEqual([_actual(i, self.wh_liq) for i in items], [0, 0, 0])

	def test_one_line_without_stock_changes_nothing(self):
		items, pl = self._three_line_order(stock=(4, 4, 0))
		before = frappe.db.get_value("Pick List", pl, ["fg_started_by", "fg_started_on", "modified"], as_dict=True)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			row["detail"], f"{items[2]} — {self.wh_liq} · Requerido: 4 · Disponible: 0 · Faltante: 4"
		)
		self.assertNotIn("aún no la alista", row["detail"])
		self.assertEqual(self._picked(pl), [0, 0, 0])
		self.assertEqual(
			frappe.db.get_value("Pick List", pl, ["fg_started_by", "fg_started_on", "modified"], as_dict=True), before
		)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual([_actual(i, self.wh_liq) for i in items], [4, 4, 0])

	def test_partially_picked_line_is_completed_to_the_requested_qty(self):
		"""10 pedidas, 6 ya alistadas (siguen en el Bin hasta la salida), faltan
		4. Bin 9 = 6 propias + 3 libres -> no alcanza; Bin 13 = 6 + 7 libres
		-> pasa a 10 y sale 10 una sola vez."""
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 10))
		self._pick(pl, {item: 6})
		self._resolve(self._report_for(pl).name)
		self.world.stock_up_real(item, self.wh_liq, 9, rate=RATE)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual([(s["required_qty"], s["available_qty"]) for s in row["shortages"]], [(10, 9)])
		self.assertEqual(self._picked(pl), [6])

		self.world.stock_up_real(item, self.wh_liq, 13, rate=RATE)
		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._picked(pl), [10])
		self.assertEqual([flt(r.transferred_qty) for r in frappe.get_doc("Pick List", pl).locations], [10])
		self.assertEqual(_actual(item, self.wh_liq), 3)

	def test_unpicked_line_does_not_take_stock_held_by_another_open_pick_list(self):
		items, pl = self._three_line_order(stock=(4, 4, 4))
		# Another order's draft holds 4 of items[1] physically picked.
		_so_b, pl_b = self._order((items[1], self.wh_liq, 4))
		self._pick(pl_b)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			[(s["item_code"], s["available_qty"]) for s in row["shortages"]], [(items[1], 0)]
		)
		self.assertEqual(self._picked(pl), [0, 0, 0])
		self.assertEqual(self._picked(pl_b), [4])

	def test_each_warehouse_is_validated_on_its_own(self):
		item_1 = self._item(self.wh_liq)
		item_2 = self._item(self.wh_var)
		_so, pl = self._order((item_1, self.wh_liq, 4), (item_2, self.wh_var, 4))
		report = self._start_and_report(pl, item_1)
		self.world.stock_up_real(item_1, self.wh_liq, 4, rate=RATE)
		self.world.stock_up_real(item_2, self.wh_liq, 4, rate=RATE)  # stock in the WRONG warehouse
		self._resolve(report)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			[(s["item_code"], s["warehouse"], s["available_qty"]) for s in row["shortages"]], [(item_2, self.wh_var, 0)]
		)
		self.assertEqual(self._picked(pl), [0, 0])

		self.world.stock_up_real(item_2, self.wh_var, 4, rate=RATE)
		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		issue = frappe.get_doc("Stock Entry", row["material_issue"])
		self.assertEqual(
			sorted((d.item_code, d.s_warehouse, flt(d.qty)) for d in issue.items),
			sorted([(item_1, self.wh_liq, 4), (item_2, self.wh_var, 4)]),
		)
		self.assertEqual((_actual(item_1, self.wh_liq), _actual(item_2, self.wh_var), _actual(item_2, self.wh_liq)), (0, 0, 4))

	def test_same_item_in_several_rows_is_aggregated_per_warehouse(self):
		item = self._item(self.wh_liq)
		other = self._item(self.wh_liq)
		_so, pl = self._order((other, self.wh_liq, 1), (item, self.wh_liq, 2), (item, self.wh_liq, 3))
		report = self._start_and_report(pl, other)
		self.world.stock_up_real(other, self.wh_liq, 1, rate=RATE)
		self.world.stock_up_real(item, self.wh_liq, 4, rate=RATE)  # each row alone fits, together (5) they do not
		self._resolve(report)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			[(s["item_code"], s["required_qty"], s["available_qty"], s["shortage_qty"]) for s in row["shortages"]],
			[(item, 5, 4, 1)],
		)
		self.assertTrue(all(q == 0 for q in self._picked(pl)))

		self.world.stock_up_real(item, self.wh_liq, 5, rate=RATE)
		row = self._row(self._run(), pl)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(_actual(item, self.wh_liq), 0)

	def test_second_run_on_a_whole_pick_list_never_issues_twice(self):
		items, pl = self._three_line_order()

		first = self._row(self._run(), pl)
		second = self._run()

		self.assertEqual(first["status"], rss.STATUS_COMPLETED)
		self.assertEqual([r for r in second["results"] if r["pick_list"] == pl], [])
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual([_actual(i, self.wh_liq) for i in items], [0, 0, 0])
		self.assertEqual(len(self._comments(pl)), 2)  # never repeated by the re-run

	def test_note_lists_only_the_lines_the_system_filled(self):
		item_done = self._item(self.wh_liq, stock=20)
		item_partial = self._item(self.wh_liq, stock=20)
		item_unpicked = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item_done, self.wh_liq, 2), (item_partial, self.wh_liq, 5), (item_unpicked, self.wh_liq, 1))
		self._pick(pl, {item_partial: 3, item_unpicked: 0})  # item_done fully picked by Bodega
		self._resolve(self._report_for(pl, item_partial).name)
		self._resolve(self._report_for(pl, item_unpicked).name)

		row = self._row(self._run(user=self.jefe), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._picked(pl), [2, 5, 1])
		general, note = self._comments(pl)
		self.assertIn("Completado por", general)
		self.assertIn(f"Validación automática de faltantes resueltos por {self.jefe}.", note)
		self.assertIn(f"- {item_partial} — 2 unidades — {self.wh_liq}", note)
		self.assertIn(f"- {item_unpicked} — 1 unidad — {self.wh_liq}", note)
		self.assertNotIn(item_done, note)

	def test_a_later_failure_reverts_every_automatic_fill(self):
		items, pl = self._three_line_order()
		before = frappe.db.get_value("Pick List", pl, ["fg_started_by", "fg_started_on"], as_dict=True)

		with patch.object(bodega, "create_stock_issue", side_effect=RuntimeError("salida falló")):
			row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(self._picked(pl), [0, 0, 0])
		self.assertEqual(frappe.db.get_value("Pick List", pl, ["fg_started_by", "fg_started_on"], as_dict=True), before)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(self._comments(pl), [])
		self.assertEqual([_actual(i, self.wh_liq) for i in items], [4, 4, 4])

	def test_open_shortage_blocks_even_with_stock(self):
		item_1 = self._item(self.wh_liq)
		item_2 = self._item(self.wh_var)
		_so, pl = self._order((item_1, self.wh_liq, 4), (item_2, self.wh_var, 4))
		self._pick(pl, {item_1: 0, item_2: 0})
		self.world.stock_up_real(item_1, self.wh_liq, 4, rate=RATE)
		self.world.stock_up_real(item_2, self.wh_var, 4, rate=RATE)
		self._resolve(self._report_for(pl, item_1).name)
		open_report = self._report_for(pl, item_2)  # still Abierto

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(row["detail"], f"{open_report.name} — faltante todavía Abierto/En Proceso")
		self.assertEqual(self._picked(pl), [0, 0])
		self.assertEqual(frappe.db.get_value("Reporte de Faltante", open_report.name, "status"), "Abierto")

	def test_line_already_picked_in_a_non_picking_warehouse_does_not_block(self):
		item_1 = self._item(self.wh_liq)
		item_2 = self._item(self.wh_var, stock=4)
		_so, pl = self._order((item_1, self.wh_liq, 4), (item_2, self.wh_var, 4))
		self._pick(pl, {item_1: 0})  # item_2 fully picked by Bodega
		self.world.stock_up_real(item_1, self.wh_liq, 4, rate=RATE)
		self._resolve(self._report_for(pl, item_1).name)

		with patch.object(rss, "non_picking_warehouses", return_value={self.wh_var}):
			row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._picked(pl), [4, 4])

	def test_another_open_shortage_blocks_completion(self):
		item_1 = self._item(self.wh_liq)
		item_2 = self._item(self.wh_var)
		_so, pl = self._order((item_1, self.wh_liq, 4), (item_2, self.wh_var, 4))
		self._pick(pl, {item_1: 0, item_2: 0})
		self.world.stock_up_real(item_1, self.wh_liq, 4, rate=RATE)
		self.world.stock_up_real(item_2, self.wh_var, 4, rate=RATE)
		self._resolve(self._report_for(pl, item_1).name)
		open_report = self._report_for(pl, item_2)
		frappe.db.set_value("Reporte de Faltante", open_report.name, "status", "En Proceso")

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertIn(open_report.name, row["detail"])
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(self._picked(pl), [0, 0])
		self.assertEqual(_stock_issues(pl), [])

	def test_non_picking_warehouse_is_never_filled(self):
		item, _so, pl, _report = self._draft_with_resolved_shortage()

		with patch.object(rss, "non_picking_warehouses", return_value={self.wh_liq}):
			row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertIn("almacén no válido para alistamiento", row["detail"])
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(self._picked(pl), [0])
		self.assertEqual(_actual(item, self.wh_liq), 4)

	# =====================================================================
	# Original Pick List submitted (partial) -> remainder
	# =====================================================================

	def _partial_then_resolved(self):
		"""Pedido 10, Bodega alista 6 y completa (sale 6), faltante 4
		resuelto: quedan 14 en bodega."""
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 10))
		self._pick(pl, {item: 6})
		self._complete(pl)
		self.assertEqual(_actual(item, self.wh_liq), 14)
		report = self._report_for(pl)
		self._resolve(report.name)
		return item, so, pl, report

	def test_partial_original_creates_the_remainder_and_completes_it(self):
		item, so, pl, _report = self._partial_then_resolved()

		result = self._run()

		remainders = self._pick_lists_of(so.name) - {pl}
		for name in remainders:
			self.world.track_existing("Pick List", name)
		self.assertEqual(len(remainders), 1)
		remainder = remainders.pop()
		row = self._row(result, remainder)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		doc = frappe.get_doc("Pick List", remainder)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(self._picked(remainder), [4])
		# Nobody had started it: the Jefe who pressed the button did.
		self.assertEqual(doc.fg_started_by, self.jefe)
		self.assertTrue(doc.fg_started_on)
		self.assertEqual(len(_stock_issues(remainder)), 1)
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 10)

	def test_existing_remainder_is_reused_not_duplicated(self):
		item, so, pl, report = self._partial_then_resolved()
		existing = ensure_remaining_pick_list(report.name)
		self.assertTrue(existing)
		self.world.track_existing("Pick List", existing)

		row = self._row(self._run(), existing)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._pick_lists_of(so.name), {pl, existing})
		self.assertEqual(_actual(item, self.wh_liq), 10)

	def test_legacy_submitted_pick_list_without_issue_is_an_error_never_repaired(self):
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		with stock_issue_service.controlled_pick_list_submit(pl):
			frappe.get_doc("Pick List", pl).submit()
		pl_row = frappe.get_doc("Pick List", pl).locations[0]
		report = self.world.shortage_report(
			item_code=item,
			warehouse=self.wh_liq,
			sales_order=so.name,
			sales_order_item=pl_row.sales_order_item,
			pick_list=pl,
			pick_list_item=pl_row.name,
			qty_solicitada=4,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
		)
		self._resolve(report.name)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self.assertIn("revisión administrativa", row["detail"])
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": pl}), 0)
		self.assertEqual(_actual(item, self.wh_liq), 20)
		self.assertEqual(self._pick_lists_of(so.name), {pl})

	# =====================================================================
	# Isolation, batch size, permissions
	# =====================================================================

	def test_a_failing_pick_list_never_reverts_the_completed_ones(self):
		item_a, _so_a, pl_a, _ra = self._draft_with_resolved_shortage()
		item_b, _so_b, pl_b, _rb = self._draft_with_resolved_shortage()
		original = rss._process_pick_list

		def failing_after_completion(pick_list, target):
			result = original(pick_list, target)
			if pick_list == pl_b:
				raise RuntimeError("fallo simulado después de completar")
			return result

		with patch.object(rss, "_process_pick_list", failing_after_completion):
			result = self._run()
		frappe.db.rollback()  # whatever was not committed is gone now

		self.assertEqual(self._row(result, pl_a)["status"], rss.STATUS_COMPLETED)
		b = self._row(result, pl_b)
		self.assertEqual(b["status"], rss.STATUS_ERROR)
		self.assertIn("fallo simulado", b["detail"])
		self.assertEqual(frappe.db.get_value("Pick List", pl_a, "docstatus"), 1)
		self.assertEqual(len(_stock_issues(pl_a)), 1)
		self.assertEqual(_actual(item_a, self.wh_liq), 0)
		# B's own transaction was rolled back entirely.
		self.assertEqual(frappe.db.get_value("Pick List", pl_b, "docstatus"), 0)
		self.assertEqual(_stock_issues(pl_b), [])
		self.assertEqual(_actual(item_b, self.wh_liq), 4)
		self.assertEqual(result["summary"], {"completed": 1, "still_short": 0, "already_completed": 0, "errors": 1})

	def test_batch_is_capped_and_reports_the_remaining_count(self):
		self.assertEqual(rss.MAX_PICK_LISTS_PER_RUN, 50)
		_a = self._draft_with_resolved_shortage()
		_b = self._draft_with_resolved_shortage()

		first = self._run(limit=1)
		self.assertEqual((len(first["results"]), first["remaining_count"]), (1, 1))

		second = self._run(limit=1)
		self.assertEqual((len(second["results"]), second["remaining_count"]), (1, 0))
		completed = {r["pick_list"] for r in first["results"] + second["results"] if r["status"] == rss.STATUS_COMPLETED}
		self.assertEqual(completed, {_a[2], _b[2]})

	def test_jefe_de_bodega_can_run_it(self):
		result = self._run()
		self.assertEqual(
			result, {"summary": {"completed": 0, "still_short": 0, "already_completed": 0, "errors": 0}, "results": [], "remaining_count": 0}
		)

	def test_bodega_cannot_run_it(self):
		_item, _so, pl, _report = self._draft_with_resolved_shortage()
		with self.assertRaises(frappe.PermissionError):
			self._run(user=self.bodega_user)
		with fx.as_user(self.bodega_user), self.assertRaises(frappe.PermissionError):
			rss.validate_resolved_shortage_orders()
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)

	def test_bodega_finish_picking_is_unchanged(self):
		"""COMPLETAR PEDIDO still requires Pick List write: Jefe de Bodega
		cannot call it directly -- only through the bulk validation."""
		item = self._item(self.wh_liq, stock=4)
		_so, pl = self._order((item, self.wh_liq, 4))
		self._pick(pl)
		with fx.as_user(self.jefe), self.assertRaises(frappe.PermissionError):
			bodega.finish_picking(pl)
		self.assertFalse(self._complete(pl)["already_completed"])

	# =====================================================================
	# "Resuelto" window: the status transition, not any edit
	# =====================================================================

	def _backdate_versions(self, report_name, days=1):
		"""Test-only: move every Version row of this report `days` back, as
		if all its history had happened before today."""
		frappe.db.sql(
			"""update `tabVersion` set creation = creation - interval %s day
			where ref_doctype = 'Reporte de Faltante' and docname = %s""",
			(days, report_name),
		)

	def test_window_is_today_and_the_two_previous_days(self):
		self.assertEqual(rss.RESOLVED_SHORTAGE_LOOKBACK_DAYS, 3)
		self.assertEqual(
			rss.resolution_window("2026-10-07"), (frappe.utils.getdate("2026-10-05"), frappe.utils.getdate("2026-10-07"))
		)

	def test_resolved_today_yesterday_and_two_days_ago_are_included(self):
		for days in (0, 1, 2):
			with self.subTest(days=days):
				item, _so, pl, report = self._draft_with_resolved_shortage()
				if days:
					self._backdate_versions(report.name, days)
				row = self._row(self._run(), pl)
				self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
				self.assertEqual(_actual(item, self.wh_liq), 0)

	def test_resolved_three_days_ago_is_out_of_the_window(self):
		_item, _so, pl, report = self._draft_with_resolved_shortage()
		self._backdate_versions(report.name, 3)

		result = self._run()

		self.assertEqual([r for r in result["results"] if r["pick_list"] == pl], [])
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)

	def test_resolved_before_the_window_and_edited_today_is_not_included(self):
		_item, _so, pl, report = self._draft_with_resolved_shortage()
		self._backdate_versions(report.name, 3)
		doc = frappe.get_doc("Reporte de Faltante", report.name)
		doc.resolution_note = "Nota editada hoy"
		doc.save(ignore_version=False)  # a Version row created today, without a status change
		self.assertTrue(
			frappe.db.exists(
				"Version",
				{"ref_doctype": "Reporte de Faltante", "docname": report.name, "creation": [">=", frappe.utils.nowdate()]},
			)
		)

		result = self._run()

		self.assertEqual([r for r in result["results"] if r["pick_list"] == pl], [])
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(self._picked(pl), [0])

	def test_reopened_and_resolved_again_inside_the_window_is_included(self):
		item, _so, pl, report = self._draft_with_resolved_shortage()
		self._backdate_versions(report.name, 3)
		doc = frappe.get_doc("Reporte de Faltante", report.name)
		doc.status = "Abierto"
		doc.save(ignore_version=False)
		self._resolve(report.name)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(_actual(item, self.wh_liq), 0)

	# =====================================================================
	# Operational reservation: Bin.actual_qty - held by OTHER draft Pick Lists
	# =====================================================================

	def test_reservation_a_held_units_of_another_open_pick_list_are_subtracted(self):
		"""Bin 10, Pick List A (draft) holds 6 picked, B needs 5 -> 4 available."""
		item = self._item(self.wh_liq)
		_so_b, pl_b = self._order((item, self.wh_liq, 5))
		self._pick(pl_b, {item: 0})
		report = self._report_for(pl_b)
		self.world.stock_up_real(item, self.wh_liq, 10, rate=RATE)
		_so_a, pl_a = self._order((item, self.wh_liq, 6))
		self._pick(pl_a)
		self._resolve(report.name)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, pl_b), 6)

		row = self._row(self._run(), pl_b)

		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual(
			[(s["required_qty"], s["available_qty"], s["shortage_qty"]) for s in row["shortages"]], [(5, 4, 1)]
		)
		self.assertEqual(frappe.db.get_value("Pick List", pl_b, "docstatus"), 0)
		self.assertEqual(self._picked(pl_b), [0])
		self.assertEqual(_actual(item, self.wh_liq), 10)

	def test_reservation_b_issued_units_are_not_subtracted_again(self):
		"""Bin 10, Pick List A picked 6 and already issued 6 (transferred_qty):
		nothing of A counts as held; B (5) completes."""
		item = self._item(self.wh_liq, stock=6)
		_so_a, pl_a = self._order((item, self.wh_liq, 6))
		self._pick(pl_a)
		self._complete(pl_a)
		self.assertEqual([flt(r.transferred_qty) for r in frappe.get_doc("Pick List", pl_a).locations], [6])
		_so_b, pl_b = self._order((item, self.wh_liq, 5))
		self._pick(pl_b, {item: 0})
		report = self._report_for(pl_b)
		self.world.stock_up_real(item, self.wh_liq, 10, rate=RATE)
		self._resolve(report.name)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, pl_b), 0)

		row = self._row(self._run(), pl_b)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(_actual(item, self.wh_liq), 5)

	def test_reservation_formula_subtracts_transferred_and_skips_nothing_pending(self):
		"""The formula itself on a DRAFT row: picked - delivered - transferred,
		only when > 0 (transferred_qty forced at db level, test-only -- a
		draft row never has it natively)."""
		item = self._item(self.wh_liq, stock=10)
		_so, pl = self._order((item, self.wh_liq, 6))
		self._pick(pl)
		row_name = frappe.get_doc("Pick List", pl).locations[0].name
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, "otro"), 6)
		frappe.db.set_value("Pick List Item", row_name, "transferred_qty", 4)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, "otro"), 2)
		frappe.db.set_value("Pick List Item", row_name, "transferred_qty", 6)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, "otro"), 0)
		frappe.db.set_value("Pick List Item", row_name, "transferred_qty", 0)

	def test_reservation_c_own_pick_list_is_never_subtracted_from_itself(self):
		"""The Pick List being processed already holds 2 picked: they are part
		of what it issues, never a reservation against itself."""
		item = self._item(self.wh_liq, stock=20)
		_so, pl = self._order((item, self.wh_liq, 5))
		self._pick(pl, {item: 2})
		report = self._report_for(pl)
		self.world.stock_up_real(item, self.wh_liq, 5, rate=RATE)
		self._resolve(report.name)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, pl), 0)
		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, "otro"), 2)

		row = self._row(self._run(), pl)

		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._picked(pl), [5])
		self.assertEqual(_actual(item, self.wh_liq), 0)

	def test_reservation_d_submitted_and_cancelled_pick_lists_hold_nothing(self):
		item = self._item(self.wh_liq, stock=20)
		_so_1, submitted = self._order((item, self.wh_liq, 4))
		self._pick(submitted)
		with stock_issue_service.controlled_pick_list_submit(submitted):
			frappe.get_doc("Pick List", submitted).submit()  # legacy: picked 4, never issued
		_so_2, cancelled = self._order((item, self.wh_liq, 3))
		self._pick(cancelled)
		with stock_issue_service.controlled_pick_list_submit(cancelled):
			frappe.get_doc("Pick List", cancelled).submit()
		frappe.get_doc("Pick List", cancelled).cancel()
		self.assertEqual(frappe.db.get_value("Pick List", cancelled, "docstatus"), 2)

		self.assertEqual(rss._held_by_other_draft_pick_lists(item, self.wh_liq, "otro"), 0)

	# =====================================================================
	# Atomicity: whatever prepare() set is gone if anything after it fails
	# =====================================================================

	def _unstarted_remainder(self):
		item, so, pl, report = self._partial_then_resolved()
		remainder = ensure_remaining_pick_list(report.name)
		self.assertTrue(remainder)
		self.world.track_existing("Pick List", remainder)
		doc = frappe.get_doc("Pick List", remainder)
		self.assertEqual((doc.fg_started_by, doc.fg_started_on, self._picked(remainder)), (None, None, [0]))
		return item, remainder

	def _assert_untouched(self, remainder, item, actual):
		doc = frappe.get_doc("Pick List", remainder)
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual(self._picked(remainder), [0])
		self.assertIsNone(doc.fg_started_by)
		self.assertIsNone(doc.fg_started_on)
		self.assertEqual(frappe.db.count("Stock Entry", {"pick_list": remainder}), 0)
		self.assertEqual(_actual(item, self.wh_liq), actual)

	def test_atomic_when_validate_stock_for_issue_fails(self):
		item, remainder = self._unstarted_remainder()
		self.world.stock_up_real(item, self.wh_liq, 2, rate=RATE)
		# Let the strict pre-check through so COMPLETAR PEDIDO's own
		# validate_stock_for_issue() is the one that refuses.
		with patch.object(rss, "_operational_shortages", return_value=[]):
			row = self._row(self._run(), remainder)
		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		self.assertEqual([s["available_qty"] for s in row["shortages"]], [2])
		self._assert_untouched(remainder, item, 2)

	def test_atomic_when_controlled_submit_fails(self):
		item, remainder = self._unstarted_remainder()
		with patch.object(bodega, "controlled_pick_list_submit", side_effect=RuntimeError("submit controlado falló")):
			row = self._row(self._run(), remainder)
		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self._assert_untouched(remainder, item, 14)

	def test_atomic_when_pick_list_submit_fails_after_writing(self):
		item, remainder = self._unstarted_remainder()
		pick_list_class = type(frappe.get_doc("Pick List", remainder))
		with patch.object(pick_list_class, "on_submit", side_effect=RuntimeError("on_submit falló")):
			row = self._row(self._run(), remainder)
		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self.assertIn("on_submit falló", row["detail"])
		self._assert_untouched(remainder, item, 14)

	def test_atomic_when_create_stock_issue_fails(self):
		item, remainder = self._unstarted_remainder()
		with patch.object(bodega, "create_stock_issue", side_effect=RuntimeError("salida falló")):
			row = self._row(self._run(), remainder)
		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self._assert_untouched(remainder, item, 14)

	def test_atomic_when_stock_entry_submit_fails(self):
		item, remainder = self._unstarted_remainder()
		stock_entry_class = type(frappe.new_doc("Stock Entry"))
		with patch.object(stock_entry_class, "on_submit", side_effect=RuntimeError("Stock Entry falló")):
			row = self._row(self._run(), remainder)
		self.assertEqual(row["status"], rss.STATUS_ERROR, row)
		self.assertIn("Stock Entry falló", row["detail"])
		self._assert_untouched(remainder, item, 14)

	# =====================================================================
	# Remainders
	# =====================================================================

	def test_remainder_without_stock_stays_a_coherent_draft_and_is_reused(self):
		item = self._item(self.wh_liq, stock=20)
		so, pl = self._order((item, self.wh_liq, 10))
		self._pick(pl, {item: 6})
		self._complete(pl)
		self.world.stock_up_real(item, self.wh_liq, 2, rate=RATE)
		self._resolve(self._report_for(pl).name)

		first = self._run()
		remainders = self._pick_lists_of(so.name) - {pl}
		for name in remainders:
			self.world.track_existing("Pick List", name)
		self.assertEqual(len(remainders), 1)
		remainder = next(iter(remainders))
		self.assertEqual(self._row(first, remainder)["status"], rss.STATUS_STILL_SHORT)
		doc = frappe.get_doc("Pick List", remainder)
		self.assertEqual((doc.docstatus, doc.fg_started_by), (0, None))
		self.assertEqual(sum(flt(r.stock_qty) for r in doc.locations), 4)
		self.assertTrue(all(q == 0 for q in self._picked(remainder)))

		second = self._run()
		self.assertEqual(self._row(second, remainder)["status"], rss.STATUS_STILL_SHORT)
		self.assertEqual(self._pick_lists_of(so.name), {pl, remainder})
		self.assertEqual(_actual(item, self.wh_liq), 2)

	def test_concurrent_remainder_creation_creates_exactly_one(self):
		_item, so, pl, report = self._partial_then_resolved()
		frappe.db.commit()
		report_row = frappe.get_all(
			"Reporte de Faltante",
			filters={"name": report.name},
			fields=["name", "pick_list", "pick_list_item", "sales_order", "sales_order_item"],
		)[0]

		site = frappe.local.site
		errors = []
		barrier = threading.Barrier(2)

		def attempt():
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.jefe)
			try:
				barrier.wait(timeout=30)
				_targets, early = rss._collect_targets([report_row])
				errors.extend(early)
			except Exception as e:
				errors.append(repr(e))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=attempt) for _ in range(2)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=120)
		frappe.db.rollback()

		remainders = self._pick_lists_of(so.name) - {pl}
		for name in remainders:
			self.world.track_existing("Pick List", name)
		self.assertEqual(errors, [])
		self.assertEqual(len(remainders), 1)

	# =====================================================================
	# Bulk response shape
	# =====================================================================

	RESULT_KEYS = {"pick_list", "sales_order", "pedido", "customer", "status", "material_issue", "shortages", "detail"}

	def test_response_shape_and_counters_match_rows(self):
		_i1, _s1, completed, _r1 = self._draft_with_resolved_shortage()
		_i2, _s2, short, _r2 = self._draft_with_resolved_shortage(qty=4, stock_after=1)

		result = self._run()

		self.assertEqual(set(result), {"summary", "results", "remaining_count"})
		allowed = {rss.STATUS_COMPLETED, rss.STATUS_STILL_SHORT, rss.STATUS_ALREADY_COMPLETED, rss.STATUS_ERROR}
		for row in result["results"]:
			self.assertEqual(set(row), self.RESULT_KEYS)
			self.assertIn(row["status"], allowed)
		counts = {
			"completed": rss.STATUS_COMPLETED,
			"still_short": rss.STATUS_STILL_SHORT,
			"already_completed": rss.STATUS_ALREADY_COMPLETED,
			"errors": rss.STATUS_ERROR,
		}
		self.assertEqual(
			result["summary"], {k: sum(1 for r in result["results"] if r["status"] == v) for k, v in counts.items()}
		)
		self.assertEqual(self._row(result, completed)["status"], rss.STATUS_COMPLETED)
		self.assertTrue(self._row(result, completed)["material_issue"])
		self.assertIsNone(self._row(result, short)["material_issue"])

	def test_remaining_count_counts_pick_lists_not_reports(self):
		item_1 = self._item(self.wh_liq)
		item_2 = self._item(self.wh_var)
		_so, two_reports = self._order((item_1, self.wh_liq, 4), (item_2, self.wh_var, 4))
		self._pick(two_reports, {item_1: 0, item_2: 0})
		for report in self._reports(two_reports):
			self._resolve(report.name)
		_one = self._draft_with_resolved_shortage()
		self.assertEqual(len(self.my_reports), 3)

		result = self._run(limit=0)

		self.assertEqual((result["results"], result["remaining_count"]), ([], 2))

	# =====================================================================
	# UI contract (no JS runner in this app)
	# =====================================================================

	def test_ui_contract(self):
		with open(_PAGE_JS, encoding="utf-8") as f:
			js = f.read()
		self.assertIn('__("Validar pedidos con faltantes resueltos")', js)
		self.assertIn(
			"Se revisarán los pedidos con faltantes resueltos en los últimos 3 días (hoy, ayer y anteayer). "
			"Solo se completarán los que tengan "
			"existencias suficientes en bodega. ¿Deseas continuar?",
			js,
		)
		self.assertIn("frappe.confirm(", js)
		self.assertIn('"validate_resolved_shortage_orders"', js)
		self.assertIn('frappe.user.has_role(["Jefe de Bodega", "System Manager"])', js)
		self.assertIn('__("Validación terminada")', js)
		for label in ("Completados", "Aún con faltantes", "Ya completados", "Errores"):
			self.assertIn(f'__("{label}")', js)
		for column in ("Pick List", "Pedido", "Cliente", "Estado", "Material Issue", "Detalle"):
			self.assertIn(f'__("{column}")', js)
		self.assertIn("remaining_count", js)
		body = js.split("show_resolved_validation_result((r && r.message) || {});", 1)[1][:80]
		self.assertIn("this.load_all();", body)
