# -*- coding: utf-8 -*-
"""Fase 26.2 -- api.recorridos.start_route() (Planificado -> En Ruta), the
Recorrido controller's lifecycle hardening, refresh_route_geolocation()
while Planificado, and the Ventas IN_ROUTE integration.

Fixtures reuse test_recorridos_api.py's own helper methods (the real
Bodega -> Facturación -> Recorrido chain, never a shortcut) by borrowing
the functions, not by inheriting the TestCase -- inheriting would re-run
all of that suite's own tests inside this module."""

import threading
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, flt, get_datetime

from erpnext import get_default_company

from fabergray_erp import geocoding
from fabergray_erp.api import recorridos, ventas
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []



class TestRecorridosStartRoute(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG262 WH")
		cls.item = cls.world.item("FG262-ITEM")
		cls.item2 = cls.world.item("FG262-ITEM-2")
		cls.customer = cls.world.customer("FG262 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg262-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg262-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg262-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg262-recorrido-b@example.com", ["Recorrido"])
		cls.no_role_user = cls.world.user("fg262-norole@example.com", [])
		cls.gestion_clientes_user = cls.world.user("fg262-gestion@example.com", ["Gestión de Clientes"])
		cls.system_manager_user = cls.world.user("fg262-sysmanager@example.com", ["System Manager"])
		cls._seq = 0

	# -- Borrowed helpers (functions only, see module docstring) -------------
	_facturado_pick_list = base.TestRecorridosApi._facturado_pick_list
	_set_customer_primary_address = base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = base.TestRecorridosApi._geocode_customer_address
	_track_route = base.TestRecorridosApi._track_route
	_create_route = base.TestRecorridosApi._create_route
	_plan_route = base.TestRecorridosApi._plan_route
	_cancel_route = base.TestRecorridosApi._cancel_route
	_driver = base.TestRecorridosApi._driver
	_force_route_status = base.TestRecorridosApi._force_route_status

	def _unique(self, label):
		type(self)._seq += 1
		return f"FG262 {label} {self._seq} {frappe.generate_hash(length=5)}"

	def _stop_customer(self, geocoded=True, latitude=7.119349, longitude=-73.122741):
		customer = self.world.customer(self._unique("Cliente"))
		self._set_customer_primary_address(customer, address_line1=f"Calle {self._seq} # 26-2", city="Bucaramanga")
		if geocoded:
			self._geocode_customer_address(customer, latitude=latitude, longitude=longitude)
		return customer

	def _route(self, n_stops=1, geocoded=None, with_driver=True, plan=True):
		"""A route of `n_stops` real Facturado Pick Lists. `geocoded`: list of
		booleans per stop (default: all geocoded)."""
		geocoded = geocoded if geocoded is not None else [True] * n_stops
		pick_lists, sales_orders = [], []
		for i in range(n_stops):
			customer = self._stop_customer(geocoded=geocoded[i], latitude=7.10 + i * 0.01)
			so, pl = self._facturado_pick_list(customer=customer)
			pick_lists.append(pl.name)
			sales_orders.append(so.name)
		driver = self._driver(self._unique("Conductor")).name if with_driver else None
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=pick_lists, driver=driver)
			if plan:
				route = self._plan_route(route["name"])
		route["_sales_orders"] = sales_orders
		return route

	def _start(self, route_name, user=None):
		with fx.as_user(user or self.recorrido_user):
			return self._track_route(recorridos.start_route(route_name))

	# =====================================================================
	# start_route() -- happy path + idempotency
	# =====================================================================

	def test_start_route_planificado_to_en_ruta(self):
		route = self._route(n_stops=2)
		self.assertIsNone(route["started_on"])
		result = self._start(route["name"])
		self.assertEqual(result["status"], "En Ruta")
		self.assertFalse(result["already_started"])
		self.assertIsNotNone(result["started_on"])
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "En Ruta")

	def test_started_on_written_once_and_second_call_is_idempotent(self):
		route = self._route()
		first = self._start(route["name"])
		started_on = frappe.db.get_value("Recorrido", route["name"], "started_on")
		self.assertEqual(get_datetime(first["started_on"]), get_datetime(started_on))

		second = self._start(route["name"])
		self.assertTrue(second["already_started"])
		self.assertEqual(second["status"], "En Ruta")
		self.assertEqual(get_datetime(second["started_on"]), get_datetime(started_on))
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "started_on"), started_on)

	def test_get_route_detail_exposes_started_on(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			before = recorridos.get_route_detail(route["name"])
		self.assertIn("started_on", before)
		self.assertIsNone(before["started_on"])
		self._start(route["name"])
		with fx.as_user(self.recorrido_user):
			after = recorridos.get_route_detail(route["name"])
		self.assertIsNotNone(after["started_on"])

	# =====================================================================
	# start_route() -- preconditions
	# =====================================================================

	def test_start_route_requires_driver(self):
		route = self._route(with_driver=False)
		with fx.as_user(self.recorrido_user):
			with self.assertRaisesRegex(recorridos.RouteValidationError, "conductor"):
				recorridos.start_route(route["name"])
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Planificado")

	def test_start_route_requires_at_least_one_stop(self):
		driver = self._driver(self._unique("Conductor"))
		route_doc = frappe.get_doc({"doctype": "Recorrido", "company": self._company(), "driver": driver.name})
		route_doc.insert(ignore_permissions=True)
		self.world.track_existing("Recorrido", route_doc.name)
		self._force_route_status(route_doc.name, "Planificado")
		with fx.as_user(self.recorrido_user):
			with self.assertRaisesRegex(recorridos.RouteValidationError, "no tiene paradas"):
				recorridos.start_route(route_doc.name)

	def test_start_route_requires_coordinates_and_names_missing_stops(self):
		route = self._route(n_stops=2, geocoded=[True, False])
		missing = route["stops"][1]
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteValidationError) as ctx:
				recorridos.start_route(route["name"])
		message = str(ctx.exception)
		self.assertIn("Parada 2", message)
		self.assertIn(frappe.utils.escape_html(missing["customer_name"]), message)
		self.assertNotIn("Parada 1 ", message)
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Planificado")

	def test_start_route_rejects_invalid_coordinate_snapshots(self):
		# Frappe Float columns are NOT NULL (default 0): an empty/null
		# coordinate is stored as 0, so (0, 0) is also the "empty" case.
		for lat, lng in ((0, 0), (95, -73.1), (-91, -73.1), (7.1, 190), (7.1, -181)):
			route = self._route()
			frappe.db.set_value("Recorrido Parada", route["stops"][0]["name"], {"latitude": lat, "longitude": lng})
			with fx.as_user(self.recorrido_user):
				with self.assertRaises(recorridos.RouteValidationError, msg=f"{lat},{lng}"):
					recorridos.start_route(route["name"])

	def test_start_route_never_geocodes(self):
		route = self._route(n_stops=2, geocoded=[True, False])
		with patch.object(geocoding, "geocode_address") as geocode, patch.object(
			geocoding, "_google_geocode_address"
		) as google:
			with fx.as_user(self.recorrido_user):
				with self.assertRaises(recorridos.RouteValidationError):
					recorridos.start_route(route["name"])
			geocode.assert_not_called()
			google.assert_not_called()

	# =====================================================================
	# start_route() -- rejected statuses
	# =====================================================================

	def test_start_route_rejects_borrador(self):
		route = self._route(plan=False)
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.start_route(route["name"])

	def test_start_route_rejects_cancelado(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			self._cancel_route(route["name"])
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.start_route(route["name"])

	def test_start_route_rejects_completado(self):
		route = self._route()
		self._force_route_status(route["name"], "Completado")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.start_route(route["name"])

	# =====================================================================
	# start_route() -- permissions / Company
	# =====================================================================

	def test_start_route_requires_permission(self):
		route = self._route()
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				recorridos.start_route(route["name"])
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Planificado")

	def test_start_route_company_isolation(self):
		other = frappe.get_doc({"doctype": "Recorrido", "company": "_Test Company"})
		other.insert(ignore_permissions=True)
		self.world.track_existing("Recorrido", other.name)
		self._force_route_status(other.name, "Planificado")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.PermissionError):
				recorridos.start_route(other.name)
		self.assertEqual(frappe.db.get_value("Recorrido", other.name, "status"), "Planificado")

	def test_debt_any_recorrido_user_of_same_company_can_start(self):
		"""KNOWN DEBT (Fase 26.2, approved): Driver is not linked to User yet,
		so any authorized Recorrido user of the same Company -- not only the
		assigned driver, and not only whoever created/planned the route --
		can start it. When Driver.user lands, this test must be replaced by
		one proving only the assigned driver (or a planner) can."""
		route = self._route()
		result = self._start(route["name"], user=self.recorrido_user_b)
		self.assertEqual(result["status"], "En Ruta")

	# =====================================================================
	# start_route() -- integrity: nothing but Recorrido.status/started_on
	# =====================================================================

	def test_start_route_keeps_stops_pick_lists_sales_orders_and_stock_untouched(self):
		route = self._route(n_stops=2)
		pick_lists = [s["pick_list"] for s in route["stops"]]
		stops_before = [(s["name"], s["sequence"], s["pick_list"], s["status"]) for s in route["stops"]]
		pl_before = {
			name: frappe.db.get_value("Pick List", name, ["docstatus", "status", "fg_invoicing_status", "modified"])
			for name in pick_lists
		}
		pl_items_before = {
			name: sorted(
				(flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty))
				for r in frappe.get_all("Pick List Item", filters={"parent": name}, fields=["qty", "picked_qty", "delivered_qty"])
			)
			for name in pick_lists
		}
		so_before = {
			name: frappe.db.get_value("Sales Order", name, ["status", "per_delivered", "per_billed", "modified"])
			for name in route["_sales_orders"]
		}
		counts = ("Stock Ledger Entry", "GL Entry", "Delivery Note", "Sales Invoice", "Stock Entry", "Delivery Trip")
		counts_before = {dt: frappe.db.count(dt) for dt in counts}

		result = self._start(route["name"])

		stops_after = [(s["name"], s["sequence"], s["pick_list"], s["status"]) for s in result["stops"]]
		self.assertEqual(stops_after, stops_before)
		for name in pick_lists:
			self.assertEqual(
				frappe.db.get_value("Pick List", name, ["docstatus", "status", "fg_invoicing_status", "modified"]),
				pl_before[name],
			)
			self.assertEqual(
				sorted(
					(flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty))
					for r in frappe.get_all(
						"Pick List Item", filters={"parent": name}, fields=["qty", "picked_qty", "delivered_qty"]
					)
				),
				pl_items_before[name],
			)
		for name in route["_sales_orders"]:
			self.assertEqual(
				frappe.db.get_value("Sales Order", name, ["status", "per_delivered", "per_billed", "modified"]),
				so_before[name],
			)
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts_before)

	def test_start_route_keeps_pick_lists_out_of_available_orders(self):
		route = self._route()
		self._start(route["name"])
		with fx.as_user(self.recorrido_user):
			available = recorridos.get_available_orders(page_length=100)
		self.assertNotIn(route["stops"][0]["pick_list"], [r["pick_list"] for r in available["pick_lists"]])

	# =====================================================================
	# start_route() -- real concurrency (double tap / double request)
	# =====================================================================

	def test_double_start_under_real_concurrency_is_idempotent(self):
		"""Two real threads, each on its OWN DB connection (same technique as
		test_recorridos_api.test_double_assignment_protected_under_real_
		concurrency), start the SAME Planificado route at once. Both must
		succeed: exactly one performs the transition (already_started=False),
		the other waits on the row lock, then takes the idempotent branch
		(already_started=True) -- and started_on is written exactly once.
		Setup data is committed so the threads can see it; it is tracked by
		world.cleanup() like every other fixture here."""
		route = self._route()
		frappe.db.commit()

		site = frappe.local.site
		results = {}

		def attempt(key, user_email):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user_email)
			try:
				detail = recorridos.start_route(route["name"])
				frappe.db.commit()
				results[key] = ("ok", detail["already_started"], str(detail["started_on"]))
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		t1 = threading.Thread(target=attempt, args=("a", self.recorrido_user))
		t2 = threading.Thread(target=attempt, args=("b", self.recorrido_user_b))
		t1.start()
		t2.start()
		t1.join(timeout=30)
		t2.join(timeout=30)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")

		outcomes = [results.get("a"), results.get("b")]
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual(sorted(o[1] for o in outcomes), [False, True], outcomes)
		self.assertEqual(outcomes[0][2], outcomes[1][2], outcomes)
		started_on = frappe.db.get_value("Recorrido", route["name"], "started_on")
		self.assertEqual(str(started_on), outcomes[0][2])
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "En Ruta")

	# =====================================================================
	# Ventas -- IN_ROUTE through the real start_route()
	# =====================================================================

	def test_ventas_shows_in_route_after_real_start(self):
		route = self._route()
		so_name = route["_sales_orders"][0]
		self.assertIsNone(ventas._resolve_sales_order_logistics_status(so_name))

		result = self._start(route["name"])

		logistics = ventas._resolve_sales_order_logistics_status(so_name)
		self.assertEqual(logistics["logistics_status"], "IN_ROUTE")
		self.assertEqual(logistics["route_name"], route["name"])
		self.assertEqual(get_datetime(logistics["dispatched_on"]), get_datetime(result["started_on"]))
		self.assertIsNone(logistics["delivered_on"])

	# =====================================================================
	# refresh_route_geolocation() -- Planificado allowed, the rest rejected
	# =====================================================================

	def test_fix_location_while_planificado_then_start(self):
		"""The approved use case: a Planificado route missing one location is
		fixed (Gestión de Clientes corrects the Address, Recorrido refreshes
		the snapshot) and then started -- without cancelling or re-planning,
		and without the planned order changing."""
		route = self._route(n_stops=2, geocoded=[True, False])
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteValidationError):
				recorridos.start_route(route["name"])

		missing_address = route["stops"][1]["customer_address"]
		with fx.as_user(self.gestion_clientes_user):
			recorridos.set_address_geolocation(missing_address, 7.13, -73.12)
		with fx.as_user(self.recorrido_user):
			refreshed = recorridos.refresh_route_geolocation(route["name"])
		self.assertEqual(refreshed["status"], "Planificado")
		self.assertEqual(
			[(s["name"], s["sequence"]) for s in refreshed["stops"]],
			[(s["name"], s["sequence"]) for s in route["stops"]],
		)
		self.assertEqual(refreshed["stops"][1]["geolocation_status"], "Geolocalizado")

		result = self._start(route["name"])
		self.assertEqual(result["status"], "En Ruta")

	def test_refresh_route_geolocation_rejected_en_ruta(self):
		route = self._route()
		self._start(route["name"])
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.refresh_route_geolocation(route["name"])

	def test_refresh_route_geolocation_rejected_cancelado(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			self._cancel_route(route["name"])
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.refresh_route_geolocation(route["name"])

	def test_refresh_route_geolocation_rejected_completado(self):
		route = self._route()
		self._force_route_status(route["name"], "Completado")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.refresh_route_geolocation(route["name"])

	# =====================================================================
	# Recorrido controller -- generic save paths cannot bypass the lifecycle
	# =====================================================================

	def _company(self):
		return get_default_company()

	def test_controller_new_route_must_be_borrador(self):
		doc = frappe.get_doc({"doctype": "Recorrido", "company": self._company(), "status": "Planificado"})
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_controller_new_route_cannot_have_started_on(self):
		doc = frappe.get_doc(
			{"doctype": "Recorrido", "company": self._company(), "started_on": frappe.utils.now_datetime()}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_controller_rejects_disallowed_transitions_via_save(self):
		cases = (
			("Cancelado", "Borrador"),
			("Cancelado", "Planificado"),
			("Borrador", "En Ruta"),
			("Borrador", "Completado"),
			("Planificado", "Borrador"),
			("Planificado", "Completado"),
			("En Ruta", "Planificado"),
			("En Ruta", "Cancelado"),
			("En Ruta", "Borrador"),
			("Completado", "En Ruta"),
		)
		for previous, target in cases:
			route = self._route(plan=False)
			self._force_route_status(route["name"], previous)
			with fx.as_user(self.system_manager_user):
				doc = frappe.get_doc("Recorrido", route["name"])
				doc.status = target
				with self.assertRaises(frappe.ValidationError, msg=f"{previous} -> {target}"):
					doc.save()
			self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), previous)

	def test_controller_rejects_cancelado_to_borrador_via_client_set_value(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			self._cancel_route(route["name"])
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value("Recorrido", route["name"], "status", "Borrador")
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Cancelado")

	def test_controller_en_ruta_via_save_enforces_same_preconditions(self):
		route = self._route(n_stops=2, geocoded=[True, False])
		with fx.as_user(self.recorrido_user):
			doc = frappe.get_doc("Recorrido", route["name"])
			doc.status = "En Ruta"
			with self.assertRaises(recorridos.RouteValidationError):
				doc.save()
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Planificado")

	def test_controller_en_ruta_via_save_without_driver_rejected(self):
		route = self._route(with_driver=False)
		with fx.as_user(self.recorrido_user):
			with self.assertRaisesRegex(recorridos.RouteValidationError, "conductor"):
				frappe.client.set_value("Recorrido", route["name"], "status", "En Ruta")

	def test_controller_en_ruta_via_save_sets_started_on_when_valid(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			frappe.client.set_value("Recorrido", route["name"], "status", "En Ruta")
		self.assertIsNotNone(frappe.db.get_value("Recorrido", route["name"], "started_on"))

	def test_controller_started_on_is_immutable(self):
		route = self._route()
		self._start(route["name"])
		original = frappe.db.get_value("Recorrido", route["name"], "started_on")
		for new_value in (add_to_date(original, hours=-2), None):
			with fx.as_user(self.recorrido_user):
				doc = frappe.get_doc("Recorrido", route["name"])
				doc.started_on = new_value
				with self.assertRaises(frappe.ValidationError, msg=str(new_value)):
					doc.save()
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "started_on"), original)

	def test_controller_started_on_empty_before_en_ruta(self):
		route = self._route()
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value("Recorrido", route["name"], "started_on", frappe.utils.now_datetime())
		self.assertIsNone(frappe.db.get_value("Recorrido", route["name"], "started_on"))

	def test_controller_company_is_immutable(self):
		route = self._route(plan=False)
		with fx.as_user(self.system_manager_user):
			doc = frappe.get_doc("Recorrido", route["name"])
			doc.company = "_Test Company"
			with self.assertRaises(frappe.ValidationError):
				doc.save()
		self.assertNotEqual(frappe.db.get_value("Recorrido", route["name"], "company"), "_Test Company")
