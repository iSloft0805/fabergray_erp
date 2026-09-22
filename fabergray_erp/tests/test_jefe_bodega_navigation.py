# -*- coding: utf-8 -*-
"""Hotfix 25.20.5 -- navegación operativa del Jefe de Bodega, sin List Views
nativos:

- KPI "Faltantes abiertos" cuenta SOLO status "Abierto" (lo mismo que lista
  la pestaña ABIERTOS de centro-faltantes a la que lleva su "Ver").
- get_shortage_center(pick_list=...) filtra server-side por Pick List,
  combinable con status/txt ("VER FALTANTES" desde jefe-pick-lists).
- Contrato estático de los tres JS (no hay runner JS en esta app).

Nombres con sufijo aleatorio: un setUpClass interrumpido no deja registros
que choquen con la siguiente corrida.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import jefe_bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page")


def _read_page_js(page):
	with open(os.path.join(_PAGE_DIR, page, f"{page}.js"), encoding="utf-8") as f:
		return f.read()


class TestJefeBodegaNavigationApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FG25205 {sfx} WH")
		cls.item = cls.world.item(f"FG25205-{sfx}-ITEM")
		cls.customer = cls.world.customer(f"FG25205 {sfx} Cliente")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)

		so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 5, cls.customer.name)
		cls.pick_list = cls.world.pick_list_for(so, cls.wh.name)
		so_2 = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 3, cls.customer.name)
		cls.other_pick_list = cls.world.pick_list_for(so_2, cls.wh.name)

		cls.jefe = cls.world.user(f"fg25205-{sfx}-jefe@example.com", ["Jefe de Bodega"])
		cls.vendedora = cls.world.user(f"fg25205-{sfx}-vendedora@example.com", ["Vendedora"])

		cls.abierto = cls._shortage(pick_list=cls.pick_list.name)
		cls.en_proceso = cls._shortage(pick_list=cls.pick_list.name, status="En Proceso")
		cls.other_abierto = cls._shortage(pick_list=cls.other_pick_list.name)
		cls.no_pick_list = cls._shortage()

	@classmethod
	def _shortage(cls, pick_list=None, status=None):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": cls.item.name,
				"warehouse": cls.wh.name,
				"pick_list": pick_list,
				"qty_solicitada": 5,
				"qty_disponible": 0,
				"detected_by": "Bodega",
				"shortage_reason": "Compra pendiente",
			}
		)
		doc.insert()
		cls.world.track_existing("Reporte de Faltante", doc.name)
		if status:
			frappe.db.set_value("Reporte de Faltante", doc.name, "status", status)
		return doc.name

	def _center(self, **kwargs):
		with fx.as_user(self.jefe):
			return jefe_bodega.get_shortage_center(page_length=100, **kwargs)

	# -- 1. KPI "Faltantes abiertos" -------------------------------------------

	def test_kpi_faltantes_abiertos_counts_only_abierto(self):
		with fx.as_user(self.jefe):
			summary = jefe_bodega.get_summary()
			abiertos = frappe.get_list("Reporte de Faltante", filters={"status": "Abierto"}, pluck="name")
			en_proceso = frappe.get_list("Reporte de Faltante", filters={"status": "En Proceso"}, pluck="name")
		self.assertIn(self.en_proceso, en_proceso)
		self.assertEqual(summary["faltantes_abiertos"], len(abiertos))
		self.assertNotEqual(summary["faltantes_abiertos"], len(abiertos) + len(en_proceso))

	def test_kpi_matches_the_abiertos_tab_it_links_to(self):
		with fx.as_user(self.jefe):
			summary = jefe_bodega.get_summary()
		self.assertEqual(summary["faltantes_abiertos"], self._center(status="Abierto")["total"])

	# -- 2. get_shortage_center(pick_list=...) ---------------------------------

	def test_pick_list_filter_returns_only_that_pick_list(self):
		res = self._center(pick_list=self.pick_list.name)
		names = {r["name"] for r in res["reports"]}
		self.assertEqual(names, {self.abierto, self.en_proceso})
		self.assertEqual(res["total"], 2)

	def test_pick_list_filter_combines_with_status(self):
		abiertos = self._center(status="Abierto", pick_list=self.pick_list.name)
		self.assertEqual([r["name"] for r in abiertos["reports"]], [self.abierto])
		en_proceso = self._center(status="En Proceso", pick_list=self.pick_list.name)
		self.assertEqual([r["name"] for r in en_proceso["reports"]], [self.en_proceso])
		resueltos = self._center(status="Resuelto", pick_list=self.pick_list.name)
		self.assertEqual(resueltos["total"], 0)

	def test_pick_list_filter_combines_with_search(self):
		res = self._center(pick_list=self.pick_list.name, txt=self.abierto)
		self.assertEqual([r["name"] for r in res["reports"]], [self.abierto])

	def test_without_pick_list_behaves_as_before(self):
		for empty in (None, "", "   "):
			names = {r["name"] for r in self._center(pick_list=empty)["reports"]}
			self.assertTrue({self.abierto, self.en_proceso, self.other_abierto, self.no_pick_list} <= names)

	def test_missing_pick_list_is_rejected(self):
		with fx.as_user(self.jefe):
			with self.assertRaises(frappe.DoesNotExistError):
				jefe_bodega.get_shortage_center(pick_list="FG25205-NO-EXISTE")

	def test_permissions_unchanged(self):
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				jefe_bodega.get_shortage_center(pick_list=self.pick_list.name)


class TestJefeBodegaNavigationUiContract(IntegrationTestCase):
	"""Los tres JS leídos como texto."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.dashboard = _read_page_js("jefe_de_bodega")
		cls.pick_lists = _read_page_js("jefe_pick_lists")
		cls.centro = _read_page_js("centro_faltantes")

	def test_no_native_list_view_navigation_left(self):
		for name, source in (
			("jefe_de_bodega.js", self.dashboard),
			("jefe_pick_lists.js", self.pick_lists),
			("centro_faltantes.js", self.centro),
		):
			self.assertIsNone(re.search(r'set_route\(\s*"List"', source), name)

	def test_kpi_ctas_open_our_pages(self):
		self.assertIn('frappe.set_route("jefe-pick-lists", { status: "listos", date_preset: "todas" })', self.dashboard)
		self.assertIn('frappe.set_route("centro-faltantes", { status: "Abierto" })', self.dashboard)

	def test_ver_faltantes_opens_centro_filtered_by_pick_list(self):
		self.assertIn('frappe.set_route("centro-faltantes", { pick_list: detail.name })', self.pick_lists)

	def test_intentional_form_navigation_untouched(self):
		self.assertIn('frappe.set_route("Form", "Reporte de Faltante", status.shortage_report)', self.dashboard)
		self.assertIn('frappe.set_route("Form", "Pick List", detail.name)', self.pick_lists)

	def test_centro_sends_pick_list_to_the_server_and_can_clear_it(self):
		self.assertIn("pick_list: this.pick_list || null", self.centro)
		self.assertIn("fg-cf-filter-chip-clear", self.centro)
		self.assertIn("clear_pick_list_filter()", self.centro)

	def test_filters_survive_refresh_via_query_string(self):
		for source in (self.centro, self.pick_lists):
			self.assertIn("window.history.replaceState", source)
			self.assertIn("function consume_route_filters()", source)
			self.assertIn('.on_page_show = function (wrapper)', source)
		self.assertIn('{ key: "todas", label: __("Todas las fechas") }', self.pick_lists)
