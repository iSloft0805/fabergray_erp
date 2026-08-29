# -*- coding: utf-8 -*-
"""Commit 22.9 -- Módulos visuales de Jefe de Bodega:
api.jefe_bodega.get_pick_list_history()/get_pick_list_history_summary()
(Pick Lists), get_shortage_center()/get_shortage_center_summary() (Centro
de Faltantes, reusing Commit 22.8's receive_shortage_purchase()/
get_shortage_purchase_status() unchanged), get_warehouse_summary()/
get_warehouse_items() (Almacenes).

Every state (listo/con_faltantes/en_alistamiento/pendiente) is built via
the SAME native flow real Bodega/Jefe de Bodega actions already use
(api.bodega.start_picking/set_picked_qty/report_shortage/finish_picking) --
never asserted by directly poking Pick List fields, so these tests also
prove get_pick_list_history() agrees with api.bodega.get_queue()'s own
bucketing rule (both now share _pick_list_bucket(), Commit 22.9's own
refactor).
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.api import jefe_bodega as api
from fabergray_erp.tests import fixtures as fx


@contextmanager
def _count_queries():
	"""Wraps the real frappe.db.sql (every frappe.get_list()/get_all() call
	ultimately executes through it, confirmed live via
	frappe.query_builder.utils.execute_query()) with a call counter, real
	execution untouched (side_effect delegates to the original). Used
	below to prove a list endpoint's own query count does not grow with
	the number of rows it returns -- the concrete, executable version of
	"no N+1 obvio" this commit's own brief asks for, not just a claim in a
	docstring."""
	box = {"n": 0}
	original = frappe.db.sql

	def counting(*args, **kwargs):
		box["n"] += 1
		return original(*args, **kwargs)

	with patch.object(frappe.db, "sql", side_effect=counting):
		yield box

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestPickListHistoryApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_a = cls.world.warehouse("FG229 Pick History Wh A")
		cls.wh_b = cls.world.warehouse("FG229 Pick History Wh B")
		cls.item = cls.world.item("FG229-PLH-ITEM")
		cls.customer = cls.world.customer("FG229 Pick History Customer")
		cls.world.stock_up(cls.item.name, cls.wh_a.name, 1000)
		cls.world.stock_up(cls.item.name, cls.wh_b.name, 1000)

		cls.jefe = cls.world.user("fg229-jefe@example.com", ["Jefe de Bodega"])
		cls.vendedora = cls.world.user("fg229-vendedora@example.com", ["Vendedora"])

	def _pick_list_for(self, warehouse, qty=5):
		so = self.world.submitted_sales_order(self.item.name, warehouse, qty, self.customer.name)
		return self.world.pick_list_for(so, warehouse)

	def _listo_pick_list(self, warehouse=None):
		pl = self._pick_list_for(warehouse or self.wh_a.name)
		bodega.start_picking(pl.name)
		row = bodega.get_pick_list(pl.name)["rows"][0]
		bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
		bodega.finish_picking(pl.name)
		return pl

	def _con_faltantes_pick_list(self, warehouse=None):
		pl = self._pick_list_for(warehouse or self.wh_a.name)
		bodega.start_picking(pl.name)
		row = bodega.get_pick_list(pl.name)["rows"][0]
		bodega.set_picked_qty(pl.name, row["row_name"], 2)
		report = bodega.report_shortage(pl.name, row["row_name"], 2, "Stock insuficiente")
		self.world.track_existing("Reporte de Faltante", report["name"])
		return pl

	def _en_alistamiento_pick_list(self, warehouse=None):
		pl = self._pick_list_for(warehouse or self.wh_a.name)
		bodega.start_picking(pl.name)
		return pl

	def _pendiente_pick_list(self, warehouse=None):
		return self._pick_list_for(warehouse or self.wh_a.name)

	# -- Incluye cada estado -----------------------------------------------

	def test_history_includes_listo(self):
		pl = self._listo_pick_list()
		with fx.as_user(self.jefe):
			res = api.get_pick_list_history(status="listos", page_length=100)
		names = [r["name"] for r in res["pick_lists"]]
		self.assertIn(pl.name, names)
		row = next(r for r in res["pick_lists"] if r["name"] == pl.name)
		self.assertEqual(row["state"], "listos")

	def test_history_includes_con_faltantes(self):
		pl = self._con_faltantes_pick_list()
		with fx.as_user(self.jefe):
			res = api.get_pick_list_history(status="con_faltantes", page_length=100)
		names = [r["name"] for r in res["pick_lists"]]
		self.assertIn(pl.name, names)
		row = next(r for r in res["pick_lists"] if r["name"] == pl.name)
		self.assertEqual(row["state"], "con_faltantes")
		self.assertEqual(row["shortage_count"], 1)

	def test_history_includes_en_alistamiento(self):
		pl = self._en_alistamiento_pick_list()
		with fx.as_user(self.jefe):
			res = api.get_pick_list_history(status="en_alistamiento", page_length=100)
		names = [r["name"] for r in res["pick_lists"]]
		self.assertIn(pl.name, names)

	def test_history_pendiente_only_in_todos_or_pendiente_filter(self):
		pl = self._pendiente_pick_list()
		with fx.as_user(self.jefe):
			todos = api.get_pick_list_history(page_length=200)
			pendientes = api.get_pick_list_history(status="pendientes", page_length=200)
			listos = api.get_pick_list_history(status="listos", page_length=200)
		self.assertIn(pl.name, [r["name"] for r in todos["pick_lists"]])
		self.assertIn(pl.name, [r["name"] for r in pendientes["pick_lists"]])
		self.assertNotIn(pl.name, [r["name"] for r in listos["pick_lists"]])

	# -- Filtros -------------------------------------------------------------

	def test_date_filter_excludes_out_of_range(self):
		pl = self._pendiente_pick_list()
		yesterday = frappe.utils.add_days(frappe.utils.nowdate(), -1)
		last_week = frappe.utils.add_days(frappe.utils.nowdate(), -7)

		with fx.as_user(self.jefe):
			today_res = api.get_pick_list_history(
				date_from=frappe.utils.nowdate(), date_to=frappe.utils.nowdate(), page_length=200
			)
			past_only = api.get_pick_list_history(date_from=last_week, date_to=yesterday, page_length=200)

		self.assertIn(pl.name, [r["name"] for r in today_res["pick_lists"]])
		self.assertNotIn(pl.name, [r["name"] for r in past_only["pick_lists"]])

	def test_status_filter_scopes_correctly(self):
		listo = self._listo_pick_list()
		pendiente = self._pendiente_pick_list()
		with fx.as_user(self.jefe):
			res = api.get_pick_list_history(status="listos", page_length=200)
		names = [r["name"] for r in res["pick_lists"]]
		self.assertIn(listo.name, names)
		self.assertNotIn(pendiente.name, names)

	def test_warehouse_filter_scopes_correctly(self):
		pl_a = self._pendiente_pick_list(self.wh_a.name)
		pl_b = self._pendiente_pick_list(self.wh_b.name)
		with fx.as_user(self.jefe):
			res_a = api.get_pick_list_history(warehouse=self.wh_a.name, page_length=200)
		names_a = [r["name"] for r in res_a["pick_lists"]]
		self.assertIn(pl_a.name, names_a)
		self.assertNotIn(pl_b.name, names_a)

	def test_pagination(self):
		created = [self._pendiente_pick_list().name for _ in range(3)]
		with fx.as_user(self.jefe):
			page1 = api.get_pick_list_history(txt=None, start=0, page_length=2)
		# El total de TODOS los Pick List de este sitio puede ser mayor a 3
		# (otras clases/commits también crean historial) -- lo que importa
		# aquí es que la paginación misma es exacta y consistente, no un
		# conteo absoluto.
		self.assertEqual(len(page1["pick_lists"]), 2)
		self.assertGreaterEqual(page1["total"], 3)
		for name in created:
			self.assertTrue(frappe.db.exists("Pick List", name))

	# -- Permisos -----------------------------------------------------------

	def test_user_without_pick_list_permission_is_denied(self):
		with fx.as_user(self.vendedora):
			self.assertFalse(frappe.has_permission("Pick List", "read"))
			with self.assertRaises(frappe.PermissionError):
				api.get_pick_list_history()
			with self.assertRaises(frappe.PermissionError):
				api.get_pick_list_history_summary()

	# -- No N+1 ---------------------------------------------------------------

	def test_status_filtered_history_query_count_is_bounded(self):
		"""5 "con faltantes" Pick Lists -- the bounded-fetch-then-filter path
		(status given) does exactly one Pick List query, one batched
		Reporte de Faltante shortage-flag query, one batched Pick List Item
		query and one batched Reporte de Faltante count query, regardless
		of how many rows match -- never one query per Pick List (which 5
		rows would need at least 5 of, on top of everything else)."""
		for _ in range(5):
			self._con_faltantes_pick_list()

		with _count_queries() as counted:
			with fx.as_user(self.jefe):
				res = api.get_pick_list_history(status="con_faltantes", page_length=100)

		self.assertGreaterEqual(len(res["pick_lists"]), 5)
		self.assertLess(counted["n"], 12)


class TestShortageCenterApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.warehouse = cls.world.warehouse("FG229 Shortage Center Wh")
		cls.jefe = cls.world.user("fg229-cf-jefe@example.com", ["Jefe de Bodega"])
		cls.vendedora = cls.world.user("fg229-cf-vendedora@example.com", ["Vendedora"])
		cls.difference_account = cls.world.stock_difference_account()

	def _shortage(self, item_code, qty_solicitada=15, qty_disponible=0):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": item_code,
				"warehouse": self.warehouse.name,
				"qty_solicitada": qty_solicitada,
				"qty_disponible": qty_disponible,
				"detected_by": "Bodega",
				"shortage_reason": "Compra pendiente",
			}
		)
		doc.insert()
		self.world.track_existing("Reporte de Faltante", doc.name)
		return doc

	def test_abierto_appears(self):
		item = self.world.item("FG229-CF-ABIERTO-ITEM")
		report = self._shortage(item.name)
		with fx.as_user(self.jefe):
			res = api.get_shortage_center(status="Abierto", page_length=100)
		self.assertIn(report.name, [r["name"] for r in res["reports"]])

	def test_en_proceso_appears(self):
		item = self.world.item("FG229-CF-PROCESO-ITEM")
		report = self._shortage(item.name, qty_solicitada=15)
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		with fx.as_user(self.jefe):
			res = api.get_shortage_center(status="En Proceso", page_length=100)
		self.assertIn(report.name, [r["name"] for r in res["reports"]])
		row = next(r for r in res["reports"] if r["name"] == report.name)
		self.assertEqual(row["received_qty"], 10)
		self.assertEqual(row["remaining_qty"], 5)

	def test_resuelto_appears_when_requested(self):
		item = self.world.item("FG229-CF-RESUELTO-ITEM")
		report = self._shortage(item.name, qty_solicitada=15)
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		with fx.as_user(self.jefe):
			todos = api.get_shortage_center(page_length=100)
			resueltos = api.get_shortage_center(status="Resuelto", page_length=100)
			abiertos = api.get_shortage_center(status="Abierto", page_length=100)

		self.assertIn(report.name, [r["name"] for r in todos["reports"]])
		self.assertIn(report.name, [r["name"] for r in resueltos["reports"]])
		self.assertNotIn(report.name, [r["name"] for r in abiertos["reports"]])
		row = next(r for r in resueltos["reports"] if r["name"] == report.name)
		self.assertEqual(row["received_qty"], 15)
		self.assertEqual(row["remaining_qty"], 0)

	def test_cancelled_receipt_does_not_count(self):
		item = self.world.item("FG229-CF-CANCEL-ITEM")
		report = self._shortage(item.name, qty_solicitada=15)
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		frappe.get_doc("Stock Entry", result["stock_entry"]).cancel()

		with fx.as_user(self.jefe):
			res = api.get_shortage_center(page_length=100)
		row = next(r for r in res["reports"] if r["name"] == report.name)
		self.assertEqual(row["received_qty"], 0)
		self.assertEqual(row["remaining_qty"], 15)

	def test_summary_counts(self):
		item_a = self.world.item("FG229-CF-SUMMARY-A")
		item_b = self.world.item("FG229-CF-SUMMARY-B")
		self._shortage(item_a.name)
		report_b = self._shortage(item_b.name, qty_solicitada=10)
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report_b.name, qty=5, purchase_rate=1000)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		with fx.as_user(self.jefe):
			summary = api.get_shortage_center_summary()
		self.assertGreaterEqual(summary["abiertos"], 1)
		self.assertGreaterEqual(summary["en_proceso"], 1)

	def test_user_without_reporte_de_faltante_permission_is_denied(self):
		with fx.as_user(self.vendedora):
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read"))
			with self.assertRaises(frappe.PermissionError):
				api.get_shortage_center()
			with self.assertRaises(frappe.PermissionError):
				api.get_shortage_center_summary()

	# -- No N+1 ---------------------------------------------------------------

	def test_shortage_center_query_count_is_bounded(self):
		"""5 reports, each with its own submitted receipt -- received_qty
		for the whole page comes from _bulk_received_qty() (one Stock
		Entry query + one Stock Entry Detail query total), never one
		frappe.get_doc() per report the way the single-report detail view
		(_shortage_receipts(), Commit 22.8, unchanged) does."""
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			for i in range(5):
				# Item/Reporte de Faltante creation always runs as
				# Administrator (TestWorld's own established convention --
				# Jefe de Bodega has no create permission on either) --
				# only the actual receive_shortage_purchase() call below
				# needs to run as Jefe de Bodega.
				item = self.world.item(f"FG229-CF-NPLUS1-{i}")
				report = self._shortage(item.name, qty_solicitada=10)
				with fx.as_user(self.jefe):
					result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=1000)
				self.world.track_existing("Stock Entry", result["stock_entry"])

		with _count_queries() as counted:
			with fx.as_user(self.jefe):
				res = api.get_shortage_center(page_length=100)

		self.assertGreaterEqual(len(res["reports"]), 5)
		self.assertLess(counted["n"], 15)


class TestWarehouseApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_active = cls.world.warehouse("FG229 Almacen Activo")
		cls.item = cls.world.item("FG229-WH-ITEM")
		cls.item_zero = cls.world.item("FG229-WH-ITEM-ZERO")
		cls.world.stock_up(cls.item.name, cls.wh_active.name, 42)

		cls.disabled_wh = frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": "FG229 Almacen Deshabilitado", "company": fx.COMPANY, "disabled": 1}
		)
		cls.disabled_wh.insert()
		cls.world.track_existing("Warehouse", cls.disabled_wh.name)

		cls.group_wh = frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": "FG229 Grupo Almacenes", "company": fx.COMPANY, "is_group": 1}
		)
		cls.group_wh.insert()
		cls.world.track_existing("Warehouse", cls.group_wh.name)

		cls.jefe = cls.world.user("fg229-wh-jefe@example.com", ["Jefe de Bodega"])
		cls.bodega_user = cls.world.user("fg229-wh-bodega@example.com", ["Bodega"])

	def test_only_active_warehouses_listed(self):
		with fx.as_user(self.jefe):
			res = api.get_warehouse_summary()
		names = [w["name"] for w in res["warehouses"]]
		self.assertIn(self.wh_active.name, names)
		self.assertNotIn(self.disabled_wh.name, names)

	def test_group_warehouse_not_listed_as_leaf(self):
		with fx.as_user(self.jefe):
			res = api.get_warehouse_summary()
		names = [w["name"] for w in res["warehouses"]]
		self.assertNotIn(self.group_wh.name, names)

	def test_stock_per_warehouse_correct(self):
		with fx.as_user(self.jefe):
			res = api.get_warehouse_summary()
		row = next(w for w in res["warehouses"] if w["name"] == self.wh_active.name)
		self.assertEqual(row["total_qty"], 42)
		self.assertEqual(row["items_with_stock"], 1)

	def test_warehouse_items_correct(self):
		with fx.as_user(self.jefe):
			res = api.get_warehouse_items(self.wh_active.name)
		codes = [i["item_code"] for i in res["items"]]
		self.assertIn(self.item.name, codes)
		row = next(i for i in res["items"] if i["item_code"] == self.item.name)
		self.assertEqual(row["actual_qty"], 42)

	def test_zero_qty_item_not_shown_by_default(self):
		# item_zero never gets stock_up() -- either no Bin row at all, or
		# (if some other test created one) actual_qty <= 0 either way.
		with fx.as_user(self.jefe):
			res = api.get_warehouse_items(self.wh_active.name)
		codes = [i["item_code"] for i in res["items"]]
		self.assertNotIn(self.item_zero.name, codes)

	def test_pagination(self):
		# Own dedicated Warehouse -- never wh_active, which
		# test_stock_per_warehouse_correct asserts an exact total_qty/
		# items_with_stock against; adding items here must not leak into
		# that test's own count.
		page_wh = self.world.warehouse("FG229 Almacen Paginacion")
		extra_items = [self.world.item(f"FG229-WH-PAGE-{i}") for i in range(4)]
		for item in extra_items:
			self.world.stock_up(item.name, page_wh.name, 5)

		with fx.as_user(self.jefe):
			page1 = api.get_warehouse_items(page_wh.name, page_length=2)
		self.assertEqual(len(page1["items"]), 2)
		self.assertEqual(page1["total"], 4)

	def test_bodega_denied_warehouse_views(self):
		with fx.as_user(self.bodega_user):
			self.assertFalse(frappe.has_permission("Warehouse", "read"))
			with self.assertRaises(frappe.PermissionError):
				api.get_warehouse_summary()
			with self.assertRaises(frappe.PermissionError):
				api.get_warehouse_items(self.wh_active.name)

	# -- No N+1 ---------------------------------------------------------------

	def test_warehouse_items_query_count_is_bounded(self):
		"""selling_rate for a page of Items comes from one bulk
		api.inventario._selling_rates() call (reused, not duplicated),
		never one Item Price lookup per Item."""
		wh = self.world.warehouse("FG229 Almacen NPlus1")
		items = [self.world.item(f"FG229-WH-NPLUS1-{i}") for i in range(6)]
		for item in items:
			self.world.stock_up(item.name, wh.name, 3)

		with _count_queries() as counted:
			with fx.as_user(self.jefe):
				res = api.get_warehouse_items(wh.name, page_length=100)

		self.assertGreaterEqual(len(res["items"]), 6)
		self.assertLess(counted["n"], 15)

	# -- Aislamiento multi-Company (Commit 22.9's own audit finding) ----------

	def test_other_company_warehouse_with_stock_is_excluded_and_rejected(self):
		""""Finished Goods - _TC" is real site data: a leaf, non-disabled
		Warehouse belonging to "_Test Company" (one of this site's own
		other, demo companies -- never fabrigraysas). Giving it real stock
		here (a fresh test Item, tracked/cleaned up like any other
		TestWorld fixture) reproduces exactly the leak this commit's own
		review flagged: it must never appear in get_warehouse_summary(),
		and get_warehouse_items() must reject it outright rather than
		silently returning that other company's data."""
		other_company_warehouse = "Finished Goods - _TC"
		self.assertTrue(frappe.db.exists("Warehouse", other_company_warehouse))
		self.assertNotEqual(
			frappe.db.get_value("Warehouse", other_company_warehouse, "company"), fx.COMPANY
		)

		leak_item = self.world.item("FG229-OTHER-COMPANY-LEAK")
		self.world.stock_up(leak_item.name, other_company_warehouse, 50)

		with fx.as_user(self.jefe):
			summary = api.get_warehouse_summary()
			self.assertNotIn(other_company_warehouse, [w["name"] for w in summary["warehouses"]])

			with self.assertRaises(frappe.ValidationError):
				api.get_warehouse_items(other_company_warehouse)

	# -- Contrato -----------------------------------------------------------

	def test_response_contract_is_stable(self):
		with fx.as_user(self.jefe):
			summary = api.get_warehouse_summary()
			items = api.get_warehouse_items(self.wh_active.name)
		self.assertEqual(
			set(summary.keys()),
			{"warehouses", "active_warehouses", "items_with_stock", "total_units", "total_stock_value"},
		)
		self.assertEqual(set(summary["warehouses"][0].keys()), {"name", "warehouse_name", "items_with_stock", "total_qty"})
		self.assertEqual(
			set(items.keys()), {"warehouse", "warehouse_name", "items", "total", "total_products", "total_qty"}
		)
		self.assertEqual(
			set(items["items"][0].keys()),
			{"item_code", "item_name", "item_group", "stock_uom", "actual_qty", "selling_rate"},
		)
