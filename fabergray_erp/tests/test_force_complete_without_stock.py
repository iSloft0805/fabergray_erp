# -*- coding: utf-8 -*-
"""Bodega -- "Completar pedidos sin importar inventario"
(fulfillment.bodega_settings, api.bodega._complete_pick_list(),
stock_issue_service.create_stock_issue(allow_shortage=...)).

Real stock, real Bodega flow, real Material Issues in this suite's own
throwaway warehouses. The global policy is restored after every test."""

import os
import threading
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock import stock_ledger
from erpnext.stock.stock_ledger import NegativeStockError

from fabergray_erp.api import bodega, facturacion, jefe_bodega, recorridos
from fabergray_erp.fulfillment import bodega_settings, stock_issue_service
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests.test_inventario_out_stock_issue import RATE, _StockIssueMixin, _actual, _stock_issues

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_APP = frappe.get_app_path("fabergray_erp")
_BODEGA_JS = os.path.join(_APP, "fabrigray_erp", "page", "bodega", "bodega.js")
_JEFE_JS = os.path.join(_APP, "fabrigray_erp", "page", "jefe_de_bodega", "jefe_de_bodega.js")


class TestForceCompleteWithoutStock(_StockIssueMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_world("FGFCW")
		cls.jefe = cls.world.user(f"fgfcw-jefe-{cls.tag.lower()}@example.com", ["Jefe de Bodega"])
		cls.sysman = cls.world.user(f"fgfcw-sm-{cls.tag.lower()}@example.com", ["System Manager"])
		cls.recorrido_user = cls.world.user(f"fgfcw-rec-{cls.tag.lower()}@example.com", ["Recorrido"])
		cls._initial_policy = bodega_settings.force_complete_enabled()
		cls.addClassCleanup(cls._restore_policy)
		frappe.db.commit()

	@classmethod
	def _restore_policy(cls):
		with fx.as_user(cls.jefe):
			bodega_settings.set_force_complete(cls._initial_policy)
		# This run's own audit rows only (authored by its throwaway users).
		test_users = [cls.jefe, cls.sysman]
		for doctype, ref_field in (("Version", "ref_doctype"), ("Comment", "reference_doctype")):
			frappe.db.delete(doctype, {ref_field: bodega_settings.DOCTYPE, "owner": ["in", test_users]})
		frappe.db.commit()

	def setUp(self):
		self._set(False)

	def tearDown(self):
		frappe.db.rollback()
		self._set(False)

	# -- helpers ----------------------------------------------------------

	def _set(self, enabled, user=None):
		with fx.as_user(user or self.jefe):
			result = jefe_bodega.set_force_complete_without_stock(1 if enabled else 0)
		frappe.db.commit()
		return result

	def _valued_item(self, stock=None):
		"""An item ERPNext can value even with no stock history."""
		item = self._item(self.wh_liq, stock=stock)
		frappe.db.set_value("Item", item, "valuation_rate", RATE)
		return item

	def _picked_order(self, *lines):
		"""Order + Pick List with every line picked in full by Bodega."""
		_so, pl = self._order(*lines)
		self._pick(pl)
		frappe.db.commit()  # a refused completion rolls back: never the fixtures with it
		return pl

	def _comments(self, pl):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Pick List", "reference_name": pl, "comment_type": "Comment"},
			pluck="content",
		)

	def _manual_entry(self, purpose, item, qty, s_wh=None, t_wh=None):
		entry = frappe.new_doc("Stock Entry")
		entry.stock_entry_type = purpose
		entry.purpose = purpose
		entry.company = fx.COMPANY
		entry.append(
			"items",
			{"item_code": item, "qty": qty, "s_warehouse": s_wh, "t_warehouse": t_wh, "basic_rate": RATE},
		)
		entry.insert()
		return entry

	# =====================================================================
	# 1-4, 18: policy OFF/ON and fresh reads
	# =====================================================================

	def test_01_off_and_no_stock_blocks(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		with self.assertRaises(stock_issue_service.InsufficientStockForIssueError):
			self._complete(pl)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(flt(_actual(item, self.wh_liq)), 0)  # no Bin at all: never moved

	def test_02_on_and_no_stock_completes_with_a_real_issue(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)

		result = self._complete(pl)

		self.assertTrue(result["completed_without_stock"])
		doc = frappe.get_doc("Pick List", pl)
		self.assertEqual(doc.docstatus, 1)
		(issue,) = _stock_issues(pl)
		self.assertEqual(issue.name, result["stock_entry"])
		self.assertEqual([flt(r.transferred_qty) for r in doc.locations], [3])
		self.assertEqual(flt(_actual(item, self.wh_liq)), -3)
		self.assertIn(bodega.FORCED_COMPLETION_NOTE, self._comments(pl))
		self.assertIn("Salida sin inventario suficiente", frappe.db.get_value("Stock Entry", issue.name, "remarks"))
		# The exception never outlives the submit.
		self.assertIsNone(getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None))
		self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=item))

	def test_03_on_with_enough_stock_is_the_normal_flow(self):
		item = self._valued_item(stock=5)
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)

		result = self._complete(pl)

		self.assertFalse(result["completed_without_stock"])
		self.assertEqual(flt(_actual(item, self.wh_liq)), 2)
		self.assertNotIn(bodega.FORCED_COMPLETION_NOTE, self._comments(pl))
		self.assertNotIn("Salida sin inventario", frappe.db.get_value("Stock Entry", result["stock_entry"], "remarks"))

	def test_04_18_on_then_off_the_next_order_is_blocked_again(self):
		first_item, second_item = self._valued_item(), self._valued_item()
		first = self._picked_order((first_item, self.wh_liq, 3))
		second = self._picked_order((second_item, self.wh_liq, 3))

		self._set(True)
		self._complete(first)
		self._set(False)
		with self.assertRaises(stock_issue_service.InsufficientStockForIssueError):
			self._complete(second)
		frappe.db.rollback()

		self.assertEqual(frappe.db.get_value("Pick List", first, "docstatus"), 1)  # not retroactive
		self.assertEqual(flt(_actual(first_item, self.wh_liq)), -3)
		self.assertEqual(frappe.db.get_value("Pick List", second, "docstatus"), 0)

	def test_18_completion_reads_the_policy_fresh_never_a_stale_cache(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)
		self.assertTrue(frappe.db.get_single_value(bodega_settings.DOCTYPE, bodega_settings.FIELD))  # cache warm: ON
		# Another process turns it OFF behind the cache's back.
		frappe.db.sql(
			"update `tabSingles` set value = 0 where doctype = %s and field = %s",
			(bodega_settings.DOCTYPE, bodega_settings.FIELD),
		)
		frappe.db.commit()

		with self.assertRaises(stock_issue_service.InsufficientStockForIssueError):
			self._complete(pl)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)

	# =====================================================================
	# 5-8: permissions and audit
	# =====================================================================

	def test_05_bodega_sees_the_policy_but_cannot_change_it(self):
		self._set(True)
		with fx.as_user(self.bodega_user):
			state = jefe_bodega.get_force_complete_without_stock()
			queue_state = bodega.get_queue()["force_complete_without_stock"]
			with self.assertRaises(frappe.PermissionError):
				jefe_bodega.set_force_complete_without_stock(0)
			doc = frappe.get_doc(bodega_settings.DOCTYPE)
			doc.force_complete_without_stock = 0
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		frappe.db.rollback()
		self.assertEqual(
			(state["enabled"], state["can_change"], state["label"]),
			(True, False, bodega_settings.LABEL_ON),
		)
		self.assertEqual(state["warning"], bodega_settings.WARNING_ON)
		self.assertEqual(queue_state, state)
		self.assertTrue(bodega_settings.force_complete_enabled())

	def test_06_07_jefe_and_system_manager_can_change_it(self):
		for user in (self.jefe, self.sysman):
			with self.subTest(user=user):
				on = self._set(True, user=user)
				off = self._set(False, user=user)
				self.assertEqual((on["enabled"], on["previous"], on["changed"], on["can_change"]), (True, False, True, True))
				self.assertEqual((off["enabled"], off["previous"], off["changed"]), (False, True, True))
				self.assertEqual(off["label"], bodega_settings.LABEL_OFF)
		self.assertFalse(self._set(False)["changed"])  # same value: nothing written

	def test_enabled_is_validated_strictly(self):
		for bad in ("si", "yes", 2, -1, None, "", "on", 1.5):
			with self.subTest(value=bad), fx.as_user(self.jefe):
				with self.assertRaises(bodega_settings.InvalidForceCompleteValueError):
					jefe_bodega.set_force_complete_without_stock(bad)
		for good, expected in (("1", True), ("0", False), ("true", True), ("false", False), (True, True), (0, False)):
			with self.subTest(value=good):
				self.assertIs(bodega_settings.parse_enabled(good), expected)

	def test_desk_or_api_writes_of_the_policy_are_refused_even_for_the_jefe(self):
		with fx.as_user(self.jefe):
			doc = frappe.get_doc(bodega_settings.DOCTYPE)
			doc.force_complete_without_stock = 1
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		frappe.db.rollback()
		self.assertFalse(bodega_settings.force_complete_enabled())

	def test_08_every_change_is_audited(self):
		since = frappe.utils.now_datetime()
		self._set(True)
		self._set(False)
		versions = frappe.get_all(
			"Version",
			filters={"ref_doctype": bodega_settings.DOCTYPE, "creation": [">=", since]},
			fields=["owner", "data"],
			order_by="creation asc",
		)
		changes = [frappe.parse_json(v.data)["changed"] for v in versions]
		self.assertEqual([v.owner for v in versions], [self.jefe, self.jefe])
		self.assertEqual(
			[[c[1:] for c in changed if c[0] == bodega_settings.FIELD] for changed in changes],
			[[[0, 1]], [[1, 0]]],
		)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": bodega_settings.DOCTYPE, "creation": [">=", since]},
			pluck="content",
			order_by="creation asc",
		)
		self.assertIn(f"ACTIVADO por {self.jefe}", comments[0])
		self.assertIn("(antes: DESACTIVADO)", comments[0])
		self.assertIn(f"DESACTIVADO por {self.jefe}", comments[1])

	# =====================================================================
	# 9-10, 19: all or nothing
	# =====================================================================

	def test_09_10_an_item_without_valuation_rate_blocks_the_whole_pick_list(self):
		valued = self._valued_item()
		unvalued = self._item(self.wh_liq)  # no history, no valuation_rate, no buying price
		pl = self._picked_order((valued, self.wh_liq, 2), (unvalued, self.wh_liq, 1))
		self._set(True)

		with self.assertRaises(stock_issue_service.MissingValuationRateForForcedIssueError) as ctx:
			self._complete(pl)
		frappe.db.rollback()

		self.assertIn(
			f"No se puede completar el pedido sin inventario porque el artículo {unvalued} "
			"no tiene una tasa de valoración válida.",
			str(ctx.exception),
		)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual((flt(_actual(valued, self.wh_liq)), flt(_actual(unvalued, self.wh_liq))), (0, 0))

	def test_19_the_scope_is_cleared_even_when_the_submit_fails(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)
		original = stock_ledger.is_negative_stock_allowed
		stock_entry_class = type(frappe.new_doc("Stock Entry"))

		with patch.object(stock_entry_class, "on_submit", side_effect=RuntimeError("submit falló")):
			with self.assertRaises(RuntimeError):
				self._complete(pl)
		frappe.db.rollback()

		self.assertIs(stock_ledger.is_negative_stock_allowed, original)
		self.assertIsNone(getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None))
		self.assertEqual(frappe.db.get_value("Pick List", pl, "docstatus"), 0)
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(flt(_actual(item, self.wh_liq)), 0)

	# =====================================================================
	# 11-13: nothing else may go negative
	# =====================================================================

	def test_11_12_13_other_stock_movements_keep_the_normal_rules_while_on(self):
		item = self._valued_item(stock=1)
		self._set(True)
		self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=item))

		manual_issue = self._manual_entry("Material Issue", item, 5, s_wh=self.wh_liq)
		with self.assertRaises(NegativeStockError):
			manual_issue.submit()
		frappe.db.rollback()

		transfer = self._manual_entry("Material Transfer", item, 5, s_wh=self.wh_liq, t_wh=self.wh_var)
		with self.assertRaises(NegativeStockError):
			transfer.submit()
		frappe.db.rollback()

		# Incoming movements (purchases/production receipts) are untouched.
		receipt = self._manual_entry("Material Receipt", item, 2, t_wh=self.wh_liq)
		receipt.submit()
		self.assertEqual(flt(_actual(item, self.wh_liq)), 3)
		frappe.db.rollback()

		self.assertEqual(flt(_actual(item, self.wh_liq)), 1)
		self.assertEqual(frappe.db.get_single_value("Stock Settings", "allow_negative_stock"), 0)
		self.assertEqual(frappe.db.get_value("Item", item, "allow_negative_stock"), 0)

	# =====================================================================
	# Negative-stock policy: installed once, request-local authorization
	# =====================================================================

	def test_policy_is_installed_once_and_never_rewrapped(self):
		import importlib

		installed = stock_ledger.is_negative_stock_allowed
		self.assertTrue(stock_issue_service.negative_stock_policy_installed())
		original = getattr(installed, stock_issue_service._POLICY_ORIGINAL)
		self.assertFalse(getattr(original, stock_issue_service._POLICY_MARKER, False))  # the real ERPNext one
		self.assertIs(stock_issue_service.install_negative_stock_policy(), installed)
		importlib.reload(stock_issue_service)  # a re-import never wraps twice
		self.assertIs(stock_ledger.is_negative_stock_allowed, installed)
		self.assertIs(stock_issue_service.install_negative_stock_policy(), installed)

	def test_authorization_needs_this_flow_this_entry_this_item_and_this_voucher(self):
		item, other = self._valued_item(), self._valued_item()
		entry = frappe._dict(name="MAT-STE-SCOPE-A", items=[frappe._dict(item_code=item)])

		self.assertFalse(_ask_as_voucher("Stock Entry", entry.name, item))  # no context
		with stock_issue_service.allow_negative_stock_for_bodega(entry):
			self.assertTrue(_ask_as_voucher("Stock Entry", entry.name, item))
			self.assertFalse(_ask_as_voucher("Stock Entry", entry.name, other))  # another item
			self.assertFalse(_ask_as_voucher("Stock Entry", "MAT-STE-OTHER", item))  # another Stock Entry
			self.assertFalse(_ask_as_voucher("Stock Reconciliation", entry.name, item))  # another voucher type
			self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=item))  # voucher unknown: fail closed
			scope = getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE)
			scope["flow"] = "otro flujo"
			self.assertFalse(_ask_as_voucher("Stock Entry", entry.name, item))  # another flow
		self.assertIsNone(getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None))
		self.assertFalse(_ask_as_voucher("Stock Entry", entry.name, item))

	def test_contexts_are_request_local_while_overlapping_in_threads(self):
		"""Deterministic overlap: two requests hold their authorizations at
		the same time; one fails inside its block; neither ever sees the
		other's, the global function never changes, and a later request in
		the same worker starts clean."""
		installed = stock_ledger.is_negative_stock_allowed
		site = frappe.local.site
		both_inside, a_failed, b_checked = threading.Barrier(2), threading.Event(), threading.Event()
		seen = {}

		def request_a():
			frappe.init(site=site)
			frappe.connect()
			try:
				entry = frappe._dict(name="MAT-STE-THREAD-A", items=[frappe._dict(item_code="ITEM-A")])
				try:
					with stock_issue_service.allow_negative_stock_for_bodega(entry):
						both_inside.wait(timeout=30)
						seen["a_own"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-A", "ITEM-A")
						seen["a_sees_b"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-B", "ITEM-B")
						raise RuntimeError("submit falló en A")
				except RuntimeError:
					pass
				seen["a_scope_after_failure"] = getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None)
				seen["a_after"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-A", "ITEM-A")
				a_failed.set()
				b_checked.wait(timeout=30)
			finally:
				frappe.destroy()
			# E: the next request on this same thread/worker inherits nothing.
			frappe.init(site=site)
			frappe.connect()
			try:
				seen["next_request_scope"] = getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None)
				seen["next_request"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-A", "ITEM-A")
			finally:
				frappe.destroy()

		def request_b():
			frappe.init(site=site)
			frappe.connect()
			try:
				entry = frappe._dict(name="MAT-STE-THREAD-B", items=[frappe._dict(item_code="ITEM-B")])
				with stock_issue_service.allow_negative_stock_for_bodega(entry):
					both_inside.wait(timeout=30)
					seen["b_sees_a"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-A", "ITEM-A")
					a_failed.wait(timeout=30)
					seen["b_own_after_a_failed"] = _ask_as_voucher("Stock Entry", "MAT-STE-THREAD-B", "ITEM-B")
					seen["global_unchanged"] = stock_ledger.is_negative_stock_allowed is installed
					b_checked.set()
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=request_a), threading.Thread(target=request_b)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=60)

		self.assertEqual(
			seen,
			{
				"a_own": True,
				"a_sees_b": False,
				"b_sees_a": False,
				"a_scope_after_failure": None,
				"a_after": False,
				"b_own_after_a_failed": True,
				"global_unchanged": True,
				"next_request_scope": None,
				"next_request": False,
			},
		)
		self.assertIs(stock_ledger.is_negative_stock_allowed, installed)

	# =====================================================================
	# Real concurrency against the database
	# =====================================================================

	def _concurrently(self, *jobs):
		"""Run each job(index) in its own request/connection, released
		together; returns their results (or the exception repr)."""
		site = frappe.local.site
		results = [None] * len(jobs)
		start = threading.Barrier(len(jobs))

		def run(index, job):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			try:
				start.wait(timeout=30)
				results[index] = job()
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				results[index] = {"error": type(e).__name__, "detail": str(e)[:200]}
			finally:
				results[index] = {
					**(results[index] or {}),
					"scope_after": getattr(frappe.local, stock_issue_service._NEGATIVE_STOCK_SCOPE, None),
				}
				frappe.destroy()

		threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=180)
		frappe.db.rollback()
		return results

	def _finish_as_bodega(self, pl):
		def job():
			frappe.set_user(self.bodega_user)
			return dict(bodega.finish_picking(pl))

		return job

	def test_concurrency_a_two_forced_pick_lists_both_complete(self):
		item_1, item_2 = self._valued_item(), self._valued_item()
		pl_1 = self._picked_order((item_1, self.wh_liq, 3))
		pl_2 = self._picked_order((item_2, self.wh_liq, 2))
		self._set(True)

		results = self._concurrently(self._finish_as_bodega(pl_1), self._finish_as_bodega(pl_2))

		self.assertEqual([r.get("completed_without_stock") for r in results], [True, True], results)
		self.assertEqual([r["scope_after"] for r in results], [None, None])
		self.assertEqual((len(_stock_issues(pl_1)), len(_stock_issues(pl_2))), (1, 1))
		self.assertEqual((flt(_actual(item_1, self.wh_liq)), flt(_actual(item_2, self.wh_liq))), (-3, -2))
		self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=item_1))  # D

	def test_concurrency_b_forced_completion_and_manual_issue(self):
		forced_item, manual_item = self._valued_item(), self._valued_item()
		pl = self._picked_order((forced_item, self.wh_liq, 3))
		self._set(True)

		def manual_issue():
			frappe.set_user("Administrator")
			entry = self._manual_entry("Material Issue", manual_item, 4, s_wh=self.wh_liq)
			entry.submit()
			return {"submitted": entry.name}

		forced, manual = self._concurrently(self._finish_as_bodega(pl), manual_issue)

		self.assertTrue(forced.get("completed_without_stock"), forced)
		self.assertEqual(manual.get("error"), "NegativeStockError", manual)
		self.assertEqual(flt(_actual(forced_item, self.wh_liq)), -3)
		self.assertEqual(flt(_actual(manual_item, self.wh_liq)), 0)
		self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=forced_item))  # D

	def test_concurrency_c_one_forced_submit_fails_the_other_completes(self):
		ok_item, failing_item = self._valued_item(), self._valued_item()
		ok_pl = self._picked_order((ok_item, self.wh_liq, 3))
		failing_pl = self._picked_order((failing_item, self.wh_liq, 2))
		self._set(True)
		stock_entry_class = type(frappe.new_doc("Stock Entry"))
		original_on_submit = stock_entry_class.on_submit

		def on_submit(doc, *args, **kwargs):
			if doc.pick_list == failing_pl:
				raise RuntimeError("submit falló")
			return original_on_submit(doc, *args, **kwargs)

		with patch.object(stock_entry_class, "on_submit", on_submit):
			ok, failed = self._concurrently(self._finish_as_bodega(ok_pl), self._finish_as_bodega(failing_pl))

		self.assertTrue(ok.get("completed_without_stock"), ok)
		self.assertEqual(failed.get("error"), "RuntimeError", failed)
		self.assertEqual((ok["scope_after"], failed["scope_after"]), (None, None))
		self.assertEqual(len(_stock_issues(ok_pl)), 1)
		self.assertEqual((_stock_issues(failing_pl), frappe.db.get_value("Pick List", failing_pl, "docstatus")), ([], 0))
		self.assertEqual((flt(_actual(ok_item, self.wh_liq)), flt(_actual(failing_item, self.wh_liq))), (-3, 0))
		for item in (ok_item, failing_item):  # D: no residual authorization
			self.assertFalse(stock_ledger.is_negative_stock_allowed(item_code=item))

	# =====================================================================
	# 14-17
	# =====================================================================

	def test_14_two_concurrent_completions_issue_once(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)
		frappe.db.commit()

		site = frappe.local.site
		results = [None, None]
		barrier = threading.Barrier(2)

		def attempt(index):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.bodega_user)
			try:
				barrier.wait(timeout=30)
				results[index] = bodega.finish_picking(pl)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				results[index] = {"error": repr(e)}
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=120)
		frappe.db.rollback()

		self.assertEqual(sorted(bool(r.get("already_completed")) for r in results), [False, True], results)
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(flt(_actual(item, self.wh_liq)), -3)

	def test_15_a_later_receipt_brings_the_stock_back_to_zero(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)
		result = self._complete(pl)
		self.assertEqual(flt(_actual(item, self.wh_liq)), -3)

		receipt = self._manual_entry("Material Receipt", item, 3, t_wh=self.wh_liq)
		receipt.submit()
		self.assertEqual(flt(_actual(item, self.wh_liq)), 0)

		# Test-owned undo, in an order that never goes negative: the issue
		# first (0 -> 3), then the receipt (3 -> 0).
		frappe.get_doc("Stock Entry", result["stock_entry"]).cancel()
		frappe.get_doc("Stock Entry", receipt.name).cancel()
		frappe.delete_doc("Stock Entry", receipt.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_16_17_facturacion_and_recorridos_see_a_normal_order(self):
		item = self._valued_item()
		pl = self._picked_order((item, self.wh_liq, 3))
		self._set(True)
		self._complete(pl)

		with fx.as_user(self.facturacion_user):
			queue = facturacion.get_invoicing_queue(status="Pendiente", txt=pl)
			(entry,) = [r for r in queue["pick_lists"] if r["name"] == pl]
			self.assertEqual((entry["total_qty"], entry["authorized_with_shortage"]), (3, False))
			for line in facturacion.get_invoicing_detail(pl)["items"]:
				facturacion.set_invoicing_item_checked(pl, line["row_name"], 1)
			facturacion.mark_as_invoiced(pl, "integrandoMAS")

		with fx.as_user(self.recorrido_user):
			orders = recorridos.get_available_orders(txt=pl)["pick_lists"]
		self.assertEqual([o["pick_list"] for o in orders], [pl])

	# =====================================================================
	# 20: UI contract (no JS runner in this app)
	# =====================================================================

	def test_20_ui_contract(self):
		for path in (_BODEGA_JS, _JEFE_JS):
			with self.subTest(page=os.path.basename(path)):
				with open(path, encoding="utf-8") as f:
					js = f.read()
				self.assertIn("force_complete_without_stock", js)
				self.assertIn("set_force_complete_without_stock", js)
				self.assertIn("can_change", js)
				self.assertIn("fg-force-policy-warning", js)
		self.assertEqual(bodega_settings.LABEL_ON, "COMPLETAR SIN INVENTARIO: ACTIVADO")
		self.assertEqual(bodega_settings.LABEL_OFF, "COMPLETAR SIN INVENTARIO: DESACTIVADO")
		self.assertEqual(
			bodega_settings.WARNING_ON,
			"Los pedidos podrán finalizarse aunque no exista inventario suficiente. "
			"Esto puede generar existencias negativas.",
		)
		with fx.as_user(self.bodega_user):
			self.assertIn("force_complete_without_stock", bodega.get_queue())


def _ask_as_voucher(voucher_type, voucher_no, item_code):
	"""Ask ERPNext's negative-stock question the way the stock ledger does:
	from a frame that holds the voucher being processed (`args`, like
	update_entries_after / validate_negative_qty_in_future_sle)."""
	args = frappe._dict(voucher_type=voucher_type, voucher_no=voucher_no, item_code=item_code)
	return stock_ledger.is_negative_stock_allowed(item_code=args.item_code)
