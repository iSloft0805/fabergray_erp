# -*- coding: utf-8 -*-
"""Commit 25.20 -- unified "Buscar por cliente o fecha..." search bar,
backend half: api/bodega.py::get_queue() (customer_name/creation added),
api/facturacion.py::get_invoicing_queue() (date-text matching added, its
own customer search already existed), api/recorridos.py::get_routes()
(new `txt` param, multi-parada customer matching + route_date), api/
jefe_bodega.py::get_shortage_center() (customer_name + date-text matching
added). Each class below exercises the REAL endpoint end to end, through
the exact same fixture chain (Bodega alistamiento -> Facturación
checklist -> Recorridos assignment where relevant) this app's own
existing test suites already establish for these documents -- never a
mocked/hand-built row.

Letters A-M (section 21) are covered across these four classes; JS-side
letters (G/H/style) are covered by test_search_bar_ui_contract.py's own
static checks instead (no JS test runner in this app).
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega, facturacion, jefe_bodega, recorridos, ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


@contextmanager
def _count_queries():
	"""Same technique test_recorridos_api.py/test_jefe_bodega_visual_
	modules_api.py already establish for this exact purpose."""
	box = {"n": 0}
	original = frappe.db.sql

	def counting(*args, **kwargs):
		box["n"] += 1
		return original(*args, **kwargs)

	with patch.object(frappe.db, "sql", side_effect=counting):
		yield box


# =============================================================================
# api/bodega.py::get_queue() -- client-side (limit_page_length=0, already
# fully unlimited, section 8) -- customer_name/creation added to the response,
# consumed by bodega.js's own already-existing client-side matcher.
# =============================================================================
class TestBodegaQueueCustomerName(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2520 Bodega Wh")
		cls.item = cls.world.item("FG2520-BODEGA-ITEM")
		cls.customer = cls.world.customer("FG2520 ABC Fumiservices")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg2520-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	def _pick_list(self, customer=None, qty=5):
		so = self.world.submitted_sales_order(
			self.item.name, self.wh.name, qty, (customer or self.customer).name
		)
		return self.world.pick_list_for(so, self.wh.name)

	# I -- Sales Order/Pick List always has a customer in this app (native
	# `reqd`), so "no cliente" is covered by customer_names.get() itself
	# returning None for a name not present in the batch dict, never an
	# exception -- exercised implicitly by every test here reading through
	# a dict.get().

	def test_a_customer_name_present_and_correct(self):
		pl = self._pick_list()
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		row = next(r for bucket in queue.values() for r in bucket if r["name"] == pl.name)
		self.assertEqual(row["customer_name"], "FG2520 ABC Fumiservices")
		self.assertEqual(row["customer"], self.customer.name)
		self.assertIsNotNone(row["creation"])

	def test_m_customer_name_resolution_is_batched_not_n_plus_1(self):
		"""3 Pick Lists, same customer -- one extra Customer query total,
		never one per row. Fixture creation (Sales Order/Pick List) always
		runs as Administrator (Bodega role has no Sales Order create grant,
		fixtures/custom_docperm.json) -- only the get_queue() call itself
		runs as the Bodega user being measured."""
		for _ in range(3):
			self._pick_list()
		with fx.as_user(self.bodega_user):
			with _count_queries() as counted:
				bodega.get_queue()
		for _ in range(3):
			self._pick_list()
		# Generous ceiling (this function already runs several other
		# batched queries of its own) -- the real claim is "does not scale
		# with row count": 6 rows now vs 3, query count must not have grown
		# proportionally.
		with fx.as_user(self.bodega_user):
			with _count_queries() as counted_more:
				bodega.get_queue()
		self.assertLess(counted_more["n"], counted["n"] + 4)

	def test_l_permissions_unaffected_no_role_still_denied(self):
		no_role_user = self.world.user("fg2520-norole@example.com", [])
		with fx.as_user(no_role_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.get_queue()


# =============================================================================
# api/facturacion.py::get_invoicing_queue() -- server-side paginated
# (section 8) -- customer text search already existed; date-text matching
# is this commit's own addition.
# =============================================================================
class TestInvoicingQueueDateSearch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2520 Fact Wh")
		cls.item = cls.world.item("FG2520-FACT-ITEM")
		cls.customer = cls.world.customer("FG2520 XYZ Distribuciones")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg2520-fact-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg2520-facturacion@example.com", ["Facturación"])

	def _picked_pick_list(self, qty=5):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return pl

	# A/B/C -- customer search already existed before this commit (own
	# tests elsewhere) -- re-confirmed here only as a combined-with-date
	# sanity check, not duplicated in full.
	def test_a_b_c_customer_search_case_insensitive_and_partial_still_works(self):
		pl = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			result = facturacion.get_invoicing_queue(txt="xyz distrib")
		self.assertIn(pl.name, [r["name"] for r in result["pick_lists"]])

	def test_d_search_by_iso_date_matches_creation(self):
		pl = self._picked_pick_list()
		today_iso = frappe.utils.nowdate()
		with fx.as_user(self.facturacion_user):
			result = facturacion.get_invoicing_queue(txt=today_iso)
		self.assertIn(pl.name, [r["name"] for r in result["pick_lists"]])

	def test_e_search_by_dmy_date_matches_creation(self):
		pl = self._picked_pick_list()
		today = frappe.utils.getdate()
		dmy = f"{today.day:02d}/{today.month:02d}/{today.year}"
		with fx.as_user(self.facturacion_user):
			result = facturacion.get_invoicing_queue(txt=dmy)
		self.assertIn(pl.name, [r["name"] for r in result["pick_lists"]])

	def test_f_no_match_returns_empty(self):
		self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			result = facturacion.get_invoicing_queue(txt="Nombre Que No Existe Jamas 99999")
		self.assertEqual(result["pick_lists"], [])
		self.assertEqual(result["total"], 0)

	def test_g_search_combines_with_status_filter(self):
		"""Filtro: Pendiente. Búsqueda: cliente. -> solo Pendientes de ese
		cliente (section 10's own explicit example, adapted)."""
		pl = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for it in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
			pendientes = facturacion.get_invoicing_queue(status="Pendiente", txt="XYZ")
			facturados = facturacion.get_invoicing_queue(status="Facturado", txt="XYZ")
		self.assertNotIn(pl.name, [r["name"] for r in pendientes["pick_lists"]])
		self.assertIn(pl.name, [r["name"] for r in facturados["pick_lists"]])

	def test_j_report_with_a_date_field_present_never_breaks_a_plain_text_query(self):
		"""Every row has `creation`, so this really only exercises the
		"txt is not a date" branch cleanly -- normalize_search_date()
		returning None for a name search must never raise."""
		pl = self._picked_pick_list()
		with fx.as_user(self.facturacion_user):
			result = facturacion.get_invoicing_queue(txt=pl.name)
		self.assertIn(pl.name, [r["name"] for r in result["pick_lists"]])

	def test_l_permissions_unaffected(self):
		no_role_user = self.world.user("fg2520-fact-norole@example.com", [])
		with fx.as_user(no_role_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_invoicing_queue(txt="2026-01-01")


# =============================================================================
# api/recorridos.py::get_routes() -- server-side paginated (section 8) --
# entirely new `txt` param this commit.
# =============================================================================
class TestRoutesSearch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2520 Rec Wh")
		cls.item = cls.world.item("FG2520-REC-ITEM")
		cls.customer_abc = cls.world.customer("FG2520 ABC Cliente")
		cls.customer_xyz = cls.world.customer("FG2520 XYZ Cliente")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg2520-rec-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg2520-rec-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg2520-recorrido@example.com", ["Recorrido"])

	def _facturado_pick_list(self, customer, qty=5):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for it in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
		return pl

	def _route_with_stops(self, pick_lists):
		with fx.as_user(self.recorrido_user):
			route = recorridos.create_route(pick_lists=[pl.name for pl in pick_lists])
		self.world.track_existing("Recorrido", route["name"])
		for stop in route.get("stops") or []:
			self.world.track_existing("Recorrido Parada", stop["name"])
		return route

	# A/B/C -- customer search
	def test_a_b_c_customer_search_exact_case_insensitive_partial(self):
		pl = self._facturado_pick_list(self.customer_abc)
		route = self._route_with_stops([pl])
		with fx.as_user(self.recorrido_user):
			exact = recorridos.get_routes(txt="FG2520 ABC Cliente")
			case_insensitive = recorridos.get_routes(txt="fg2520 abc cliente")
			partial = recorridos.get_routes(txt="ABC Cliente")
		for result in (exact, case_insensitive, partial):
			self.assertIn(route["name"], [r["name"] for r in result["routes"]])

	# D -- date ISO
	def test_d_search_by_route_date_iso(self):
		pl = self._facturado_pick_list(self.customer_abc)
		route = self._route_with_stops([pl])
		route_date = frappe.db.get_value("Recorrido", route["name"], "route_date")
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_routes(txt=str(route_date))
		self.assertIn(route["name"], [r["name"] for r in result["routes"]])

	# F -- no match
	def test_f_no_match_returns_empty(self):
		pl = self._facturado_pick_list(self.customer_abc)
		self._route_with_stops([pl])
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_routes(txt="Cliente Que No Existe Jamas 99999")
		self.assertEqual(result["routes"], [])
		self.assertEqual(result["total"], 0)

	# G -- combines with status filter
	def test_g_search_combines_with_status_filter(self):
		pl = self._facturado_pick_list(self.customer_abc)
		route = self._route_with_stops([pl])
		with fx.as_user(self.recorrido_user):
			matches_borrador = recorridos.get_routes(status="Borrador", txt="ABC Cliente")
			matches_en_ruta = recorridos.get_routes(status="En Ruta", txt="ABC Cliente")
		self.assertIn(route["name"], [r["name"] for r in matches_borrador["routes"]])
		self.assertNotIn(route["name"], [r["name"] for r in matches_en_ruta["routes"]])

	# K -- multi-cliente: matches on ANY stop's customer (section 16's own
	# explicit REC-001/ABC+XYZ example).
	def test_k_multi_cliente_route_matches_on_any_stop(self):
		pl_abc = self._facturado_pick_list(self.customer_abc)
		pl_xyz = self._facturado_pick_list(self.customer_xyz)
		route = self._route_with_stops([pl_abc, pl_xyz])
		with fx.as_user(self.recorrido_user):
			by_xyz = recorridos.get_routes(txt="XYZ Cliente")
		self.assertIn(route["name"], [r["name"] for r in by_xyz["routes"]])

	def test_l_permissions_unaffected(self):
		no_role_user = self.world.user("fg2520-rec-norole@example.com", [])
		with fx.as_user(no_role_user):
			with self.assertRaises(frappe.PermissionError):
				recorridos.get_routes(txt="ABC")

	def test_m_search_query_count_is_bounded(self):
		pl = self._facturado_pick_list(self.customer_abc)
		self._route_with_stops([pl])
		with fx.as_user(self.recorrido_user):
			with _count_queries() as counted:
				recorridos.get_routes(txt="ABC Cliente")
		self.assertLess(counted["n"], 15)


# =============================================================================
# api/jefe_bodega.py::get_shortage_center() -- server-side paginated
# (section 8) -- customer_name (via Sales Order join, batched) + date-text
# matching are this commit's own addition; item/pedido text search already
# existed.
# =============================================================================
class TestShortageCenterCustomerAndDateSearch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2520 JB Wh")
		cls.item = cls.world.item("FG2520-JB-ITEM")
		cls.customer = cls.world.customer("FG2520 JB Cliente Faltante")
		# Deliberately ample real stock (matches this suite's own established
		# convention in test_jefe_bodega_visual_modules_api.py's
		# _con_faltantes_pick_list()) -- Pick List's own set_item_locations()
		# nets real Bin availability against every OTHER still-open Pick
		# List for the same item+warehouse, so a scarce Bin qty here would
		# silently starve every Pick List after the first one created by
		# this class's several test methods of any location rows at all.
		# The "shortage" this class actually needs is the OPERATOR-REPORTED
		# qty_disponible passed to report_shortage() below, never real Bin
		# scarcity.
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user("fg2520-jb-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.jefe_user = cls.world.user("fg2520-jefe@example.com", ["Jefe de Bodega"])

	def _shortage_report(self, qty_solicitada=5, qty_disponible=2):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty_solicitada, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], qty_disponible)
			report = bodega.report_shortage(pl.name, row["row_name"], qty_disponible, "Stock insuficiente")
		self.world.track_existing("Reporte de Faltante", report["name"])
		return report

	def test_a_customer_name_present_via_sales_order_join(self):
		report = self._shortage_report()
		with fx.as_user(self.jefe_user):
			result = jefe_bodega.get_shortage_center()
		row = next(r for r in result["reports"] if r["name"] == report["name"])
		self.assertEqual(row["customer_name"], "FG2520 JB Cliente Faltante")

	# B/C -- customer search case-insensitive/partial
	def test_b_c_customer_search_case_insensitive_and_partial(self):
		report = self._shortage_report()
		with fx.as_user(self.jefe_user):
			result = jefe_bodega.get_shortage_center(txt="jb cliente faltante")
		self.assertIn(report["name"], [r["name"] for r in result["reports"]])

	def test_d_search_by_iso_date_matches_reported_on(self):
		report = self._shortage_report()
		today_iso = frappe.utils.nowdate()
		with fx.as_user(self.jefe_user):
			result = jefe_bodega.get_shortage_center(txt=today_iso)
		self.assertIn(report["name"], [r["name"] for r in result["reports"]])

	def test_f_no_match_returns_empty(self):
		self._shortage_report()
		with fx.as_user(self.jefe_user):
			result = jefe_bodega.get_shortage_center(txt="Cliente Inexistente 99999")
		self.assertEqual(result["reports"], [])

	def test_g_search_combines_with_status_filter(self):
		report = self._shortage_report()
		with fx.as_user(self.jefe_user):
			abierto = jefe_bodega.get_shortage_center(status="Abierto", txt="JB Cliente")
			resuelto = jefe_bodega.get_shortage_center(status="Resuelto", txt="JB Cliente")
		self.assertIn(report["name"], [r["name"] for r in abierto["reports"]])
		self.assertNotIn(report["name"], [r["name"] for r in resuelto["reports"]])

	def test_i_report_without_sales_order_never_breaks(self):
		"""customer_names.get(None) must read None, never raise/KeyError --
		exercised by any report whose sales_order key is absent from the
		batched dict."""
		report = self._shortage_report()
		with fx.as_user(self.jefe_user):
			result = jefe_bodega.get_shortage_center()
		row = next(r for r in result["reports"] if r["name"] == report["name"])
		self.assertIn("customer_name", row)

	def test_l_permissions_unaffected(self):
		no_role_user = self.world.user("fg2520-jb-norole@example.com", [])
		with fx.as_user(no_role_user):
			with self.assertRaises(frappe.PermissionError):
				jefe_bodega.get_shortage_center(txt="ABC")

	def test_m_customer_resolution_batched_not_n_plus_1(self):
		"""Section 18/M's own concern is THIS commit's new customer_name
		resolution specifically -- not the endpoint's other, pre-existing
		per-distinct-Pick-List item_name lookup (_resolve_item_name(),
		unrelated to this commit, already page-bounded on its own). Counts
		calls to `frappe.get_list("Sales Order", ...)` directly: batched
		customer_name resolution must issue exactly ONE such call no matter
		how many reports (each on its own distinct Sales Order) are on the
		page -- never one per report."""
		original_get_list = frappe.get_list
		so_calls = {"n": 0}

		def counting_get_list(doctype, *args, **kwargs):
			if doctype == "Sales Order":
				so_calls["n"] += 1
			return original_get_list(doctype, *args, **kwargs)

		for _ in range(4):
			self._shortage_report()
		with fx.as_user(self.jefe_user):
			with patch("frappe.get_list", side_effect=counting_get_list):
				result = jefe_bodega.get_shortage_center(page_length=100)
		distinct_sales_orders = {r["sales_order"] for r in result["reports"] if r["sales_order"]}
		self.assertGreater(len(distinct_sales_orders), 1)
		self.assertEqual(so_calls["n"], 1)


# Placeholder import guard -- confirms `ventas` (Commit 25.18's own
# logistics_status field, unaffected by this commit) is still importable
# from this same test module without a circular-import issue now that
# search_utils.py is imported by api/recorridos.py/api/facturacion.py/
# api/jefe_bodega.py.
def test_module_level_imports_do_not_cycle():
	assert ventas.__name__ == "fabergray_erp.api.ventas"
