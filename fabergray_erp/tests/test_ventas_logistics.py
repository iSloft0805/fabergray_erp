# -*- coding: utf-8 -*-
"""Commit 25.18 -- "EN RUTA"/"ENTREGADO" logistics status shown in Page
Ventas. `_resolve_sales_order_logistics_status()`/`_logistics_fields()`
(api/ventas.py) are the ONE source of truth get_my_orders()/
get_order_detail()/get_sales_summary() all read from -- see that module's
own Commit 25.18 comment block for the full audit (why `Recorrido.status
== "En Ruta"`/`Recorrido Parada.status == "Entregado"` are real, existing
fields no whitelisted function in api/recorridos.py has ever set yet, and
why `_force_route_status()`/`_force_parada_delivered()` below are the
correct, already-established way this app's OWN test_recorridos_api.py
simulates that future dispatch/delivery commit).

Same convention as test_cotizaciones_pedido.py: one class for server-side
classification/KPI/data correctness, `fx.TestWorld` fixtures reusing the
exact real chain (Bodega alistamiento -> Facturación checklist ->
Recorridos assignment) test_recorridos_api.py already established, plus a
static UI-contract class at the bottom reading ventas.js as text (no JS
test runner in this app).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from fabergray_erp.api import bodega, facturacion, recorridos, ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VENTAS_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js"
)


class TestSalesOrderLogisticsStatus(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2518 WH")
		cls.item = cls.world.item("FG2518-ITEM")
		cls.customer = cls.world.customer("FG2518 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)

		cls.vendedora = cls.world.user("fg2518-vendedora@example.com", ["Vendedora"])
		cls.bodega_user = cls.world.user("fg2518-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg2518-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg2518-recorrido@example.com", ["Recorrido"])

		driver = frappe.get_doc({"doctype": "Driver", "full_name": "FG2518 Conductor"})
		driver.insert()
		cls.world.track_existing("Driver", driver.name)
		cls.driver = driver

	# -- Fixture chain: SO -> Pick List (Facturado) -> Recorrido stop -------

	def _facturado_pick_list(self, qty=5, rate=100):
		"""Same real chain test_recorridos_api.py's own helper of the exact
		same name already establishes -- Bodega alistamiento -> Facturación
		checklist 100% -> mark_as_invoiced(), never a shortcut."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for it in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _route_with_stop(self, pl, driver=None):
		with fx.as_user(self.recorrido_user):
			route = recorridos.create_route(pick_lists=[pl.name], driver=(driver or self.driver.name))
		self.world.track_existing("Recorrido", route["name"])
		for stop in route.get("stops") or []:
			self.world.track_existing("Recorrido Parada", stop["name"])
		return route

	def _force_route_status(self, route_name, status):
		"""Simulates a future commit's own dispatch action -- neither
		"Planificado -> En Ruta" nor delivery confirmation exist yet
		anywhere in api/recorridos.py (confirmed live, see api/ventas.py's
		own Commit 25.18 module comment) -- same convention
		test_recorridos_api.py's own `_force_route_status()` already
		established for the identical reason."""
		frappe.db.set_value("Recorrido", route_name, "status", status)
		if status == "En Ruta":
			frappe.db.set_value("Recorrido", route_name, "started_on", now_datetime())

	def _force_parada_delivered(self, parada_name, delivered_on=None):
		frappe.db.set_value(
			"Recorrido Parada",
			parada_name,
			{"status": "Entregado", "delivered_on": delivered_on or now_datetime()},
		)

	# -- A: no route at all --------------------------------------------------

	def test_a_pedido_sin_ruta_no_aparece_en_ruta(self):
		so, _pl = self._facturado_pick_list()
		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders(limit=500)
		row = next(o for o in orders if o["name"] == so.name)
		self.assertIsNone(row["logistics_status"])
		self.assertIsNone(row["route_name"])

	# -- B/C: en ruta ----------------------------------------------------------

	def test_b_c_pedido_en_ruta_cuenta_en_kpi_y_expone_el_estado(self):
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()
		self._force_route_status(route["name"], "En Ruta")

		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()
			orders = ventas.get_my_orders(limit=500)
		row = next(o for o in orders if o["name"] == so.name)

		self.assertEqual(after["en_ruta"], before["en_ruta"] + 1)  # B
		self.assertEqual(row["logistics_status"], "IN_ROUTE")  # C (drives the JS filter)
		self.assertEqual(row["route_name"], route["name"])
		self.assertEqual(row["driver_name"], "FG2518 Conductor")
		self.assertIsNotNone(row["dispatched_on"])

	# -- D/E/F: entregado --------------------------------------------------

	def test_d_e_f_pedido_entregado(self):
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.vendedora):
			mid = ventas.get_sales_summary()

		parada_name = route["stops"][0]["name"]
		self._force_parada_delivered(parada_name)

		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()
			orders = ventas.get_my_orders(limit=500)
		row = next(o for o in orders if o["name"] == so.name)

		self.assertEqual(after["en_ruta"], mid["en_ruta"] - 1)  # D -- no longer counted as en ruta
		self.assertEqual(after["entregados_logistica"], mid["entregados_logistica"] + 1)  # E
		self.assertEqual(row["logistics_status"], "DELIVERED")  # F (drives the JS filter)
		self.assertIsNotNone(row["delivered_on"])

	# -- I: delivered has priority over in-route ------------------------------

	def test_i_entregado_tiene_prioridad_sobre_en_ruta(self):
		"""Force BOTH conditions true at once (parada Entregado, its own
		parent route still reads "En Ruta" -- e.g. the route itself has not
		been marked Completado yet, only this one stop was confirmed) --
		DELIVERED must still win, section 11's own explicit rule."""
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		self._force_route_status(route["name"], "En Ruta")
		parada_name = route["stops"][0]["name"]
		self._force_parada_delivered(parada_name)
		# Route itself deliberately left "En Ruta" -- both conditions true.
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "En Ruta")

		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders(limit=500)
		row = next(o for o in orders if o["name"] == so.name)
		self.assertEqual(row["logistics_status"], "DELIVERED")

	# -- J: cancelled sales order never counts ---------------------------------

	def test_j_pedido_cancelado_no_cuenta(self):
		"""Empirically confirmed while writing this test: once a Sales
		Order has a submitted Pick List (required to ever reach a route at
		all, `_validate_pick_list_eligible()`), Frappe's own native
		LinkExistsError blocks cancelling it directly -- and this app's own
		`cancel_sales_order()` is blocked earlier still, before picking even
		starts (test_ventas_api.py's own `test_submitted_order_cannot_be_
		deleted`-style guard). A route-assigned order therefore can never
		legitimately become docstatus=2 in production through ANY path this
		app exposes today -- so this test proves the underlying KPI-query
		guarantee directly (`active_names` in get_sales_summary() filters
		`docstatus != 2` unconditionally), the same "simulate a state no
		current whitelisted path produces yet" convention `_force_route_
		status()`/`_force_parada_delivered()` above already use, applied to
		`docstatus` itself here via a raw `frappe.db.set_value()` -- never
		claiming this is how a real cancellation happens today."""
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()

		frappe.db.set_value("Sales Order", so.name, "docstatus", 2)

		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()
		self.assertEqual(after["en_ruta"], before["en_ruta"] - 1)

	# -- K: amendments never inflate KPIs -------------------------------------

	def test_k_stale_parada_on_a_cancelled_order_never_inflates_kpi(self):
		"""Section 13's own "si aplica" hedge -- confirmed empirically
		(test_j's own docstring) that a route-assigned order can never
		reach a real `modify_submitted_sales_order()` amendment in this
		app today (Bodega has already started picking by the time it is
		route-eligible, which that function itself blocks on). The real,
		reachable risk this letter guards against is narrower: a Recorrido
		Parada row is NEVER deleted/updated when its own `sales_order`
		later becomes cancelled by some other, unrelated means -- so the
		per-row resolver (queried by name directly) still legitimately
		returns "IN_ROUTE" for it (that document itself never changes),
		while the KPI aggregate (which only ever iterates already-
		docstatus-filtered `active_names`) must still never count it."""
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()

		frappe.db.set_value("Sales Order", so.name, "docstatus", 2)

		# The Recorrido Parada row itself is untouched -- the per-row
		# resolver, if asked directly by name, still legitimately reports it.
		self.assertEqual(
			ventas._resolve_sales_order_logistics_status(so.name)["logistics_status"], "IN_ROUTE"
		)

		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()
		self.assertEqual(after["en_ruta"], before["en_ruta"] - 1)  # the KPI still excludes it

	# -- L: Commit 25.17's Cotización reference still shows alongside logistics -

	def test_l_cotizacion_reference_still_shows_alongside_logistics(self):
		from fabergray_erp.api import cotizaciones

		item = self.world.item("FG2518-L-ITEM", default_warehouse=self.wh.name)
		self.world.stock_up(item.name, self.wh.name, 1000, rate=50)
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 200,
			}
		)
		price.insert()
		self.world.track_existing("Item Price", price.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 5}]
			)
		qtn_name = result["name"]
		self.world.track_existing("Quotation", qtn_name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(qtn_name)
		with fx.as_user(self.facturacion_user):
			cotizaciones.approve_quotation_billing(qtn_name)
		with fx.as_user(self.vendedora):
			so_result = cotizaciones.create_sales_order_from_quotation(qtn_name)
		self.world.track_existing("Sales Order", so_result["sales_order"])

		pl = self.world.pick_list_for(frappe.get_doc("Sales Order", so_result["sales_order"]), self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for it in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
		route = self._route_with_stop(frappe.get_doc("Pick List", pl.name))
		self._force_route_status(route["name"], "En Ruta")

		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders(limit=500)
		row = next(o for o in orders if o["name"] == so_result["sales_order"])
		self.assertEqual(row["quotation"], qtn_name)  # 25.17, never broken
		self.assertEqual(row["logistics_status"], "IN_ROUTE")  # 25.18, both present at once

	# -- M: 25.17 still works, standalone -------------------------------------

	def test_m_2517_create_sales_order_from_quotation_still_works(self):
		from fabergray_erp.api import cotizaciones

		item = self.world.item("FG2518-M-ITEM", default_warehouse=self.wh.name)
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 150,
			}
		)
		price.insert()
		self.world.track_existing("Item Price", price.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 2}]
			)
		qtn_name = result["name"]
		self.world.track_existing("Quotation", qtn_name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(qtn_name)
		with fx.as_user(self.facturacion_user):
			cotizaciones.approve_quotation_billing(qtn_name)
		with fx.as_user(self.vendedora):
			so_result = cotizaciones.create_sales_order_from_quotation(qtn_name)
		self.world.track_existing("Sales Order", so_result["sales_order"])
		self.assertFalse(so_result["already_exists"])
		self.assertTrue(frappe.db.exists("Sales Order", so_result["sales_order"]))

	# -- N: get_order_detail exposes the correct state ------------------------

	def test_n_get_order_detail_exposes_the_correct_state(self):
		so, pl = self._facturado_pick_list()
		route = self._route_with_stop(pl)
		self._force_route_status(route["name"], "En Ruta")

		with fx.as_user(self.vendedora):
			detail = ventas.get_order_detail(so.name)
		self.assertEqual(detail["logistics_status"], "IN_ROUTE")
		self.assertEqual(detail["route_name"], route["name"])
		self.assertEqual(detail["driver_name"], "FG2518 Conductor")

	# -- Existing KPIs never broken ------------------------------------------

	def test_existing_kpi_keys_and_values_unaffected(self):
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()
		self.assertEqual(
			set(before.keys()),
			{"pedidos_hoy", "pendientes", "entregados", "cancelados", "en_ruta", "entregados_logistica"},
		)
		so, _pl = self._facturado_pick_list()
		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()
		self.assertEqual(after["pedidos_hoy"], before["pedidos_hoy"] + 1)


class TestVentasLogisticsUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		with open(_VENTAS_JS_PATH, encoding="utf-8") as f:
			cls.js = f.read()

	def test_g_en_ruta_badge_label_correct(self):
		m = re.search(r"function render_logistics_block\(o\) \{[\s\S]*?\n\}\n", self.js)
		self.assertIsNotNone(m)
		self.assertIn('"IN_ROUTE"', m.group())
		self.assertIn('label: __("EN RUTA")', m.group())
		self.assertIn("logistics-in-route", m.group())

	def test_h_entregado_badge_label_correct(self):
		m = re.search(r"function render_logistics_block\(o\) \{[\s\S]*?\n\}\n", self.js)
		self.assertIsNotNone(m)
		self.assertIn('"DELIVERED"', m.group())
		self.assertIn('label: __("ENTREGADO")', m.group())
		self.assertIn("logistics-delivered", m.group())

	def test_o_filters_never_derive_logistics_from_native_status(self):
		m = re.search(r"order_matches_filter\(o, filter\) \{[\s\S]*?\n\t\}\n", self.js)
		self.assertIsNotNone(m)
		body = m.group()
		self.assertIn('filter === "en_ruta"', body)
		self.assertIn('filter === "entregados_logistica"', body)
		# The en_ruta/entregados_logistica branches must read o.logistics_status,
		# never re-derive from o.status.
		en_ruta_line = next(line for line in body.splitlines() if "en_ruta" in line and "filter ===" in line)
		entregados_line = next(
			line for line in body.splitlines() if "entregados_logistica" in line and "filter ===" in line
		)
		self.assertIn("o.logistics_status", en_ruta_line)
		self.assertIn("o.logistics_status", entregados_line)
		self.assertNotIn("o.status", en_ruta_line)
		self.assertNotIn("o.status", entregados_line)

	def test_kpi_cards_include_en_ruta_and_entregados_logistica(self):
		m = re.search(r"render_kpis\(\) \{[\s\S]*?\n\t\}\n", self.js)
		self.assertIsNotNone(m)
		body = m.group()
		self.assertIn('key: "en_ruta"', body)
		self.assertIn('key: "entregados_logistica"', body)
		# Existing cards never removed.
		self.assertIn('key: "pedidos_hoy"', body)
		self.assertIn('key: "pendientes"', body)
		self.assertIn('key: "entregados"', body)
		self.assertIn('key: "cancelados"', body)

	def test_no_ban_preparacion_status_invented(self):
		"""Section 10/15's own explicit "no inventar etapas" -- confirmed
		here that no fabricated PENDING/PICKING/READY/"EN PREPARACIÓN"
		logistics badge state was added; only the two real ones."""
		m = re.search(r"function render_logistics_block\(o\) \{[\s\S]*?\n\}\n", self.js)
		self.assertIsNotNone(m)
		self.assertNotIn("EN PREPARACIÓN", m.group())
		self.assertNotIn("PICKING", m.group())
		self.assertNotIn("READY", m.group())
