# -*- coding: utf-8 -*-
"""Commit 24.1 -- api.recorridos: base data model + backend for the new
Recorridos (delivery-route) module. NO maps/geocoding/optimization/GPS/
signature/photo/delivery-proof/novedades/driver-UI in this commit -- see
api/recorridos.py's own top docstring for the full scope note and the
audited architecture decisions (Delivery Trip NOT reused, Driver/Vehicle
reused, Recorrido Parada a standalone DocType not a child table).

Every scenario from the approved brief's own "Tests" list (43 items,
numbered in each test's own comment) plus company isolation, a real
two-thread concurrency reproduction (not simulated -- see
test_double_assignment_protected_under_real_concurrency's own docstring
for how this was actually built and validated), and AST guardrails proving
no accounting/stock/Delivery Note document is ever created."""

import ast
import inspect
import threading

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega, facturacion, recorridos
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_FORBIDDEN_CALLS = {
	"frappe.get_all",
	"frappe.set_user",
}


def _dotted_name(node):
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return ".".join(reversed(parts))
	return None


def _forbidden_findings(source):
	"""Same AST-walk technique test_facturacion_invoicing_status.py's own
	_forbidden_findings() uses. Flags frappe.get_all/frappe.set_user calls,
	any Call whose first argument is one of the forbidden accounting/
	stock/dispatch doctype names (covers frappe.get_doc("Sales
	Invoice", ...)/frappe.new_doc("Delivery Note")/... specifically), and
	any literal ignore_permissions=True."""
	forbidden_doctypes = {
		"Sales Invoice",
		"Purchase Invoice",
		"Payment Entry",
		"GL Entry",
		"Journal Entry",
		"Delivery Note",
		"Stock Entry",
	}
	tree = ast.parse(source)
	findings = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			dotted = _dotted_name(node.func)
			if dotted in _FORBIDDEN_CALLS:
				findings.append(dotted)
			if dotted in ("frappe.get_doc", "frappe.new_doc") and node.args:
				arg = node.args[0]
				if isinstance(arg, ast.Constant) and arg.value in forbidden_doctypes:
					findings.append(f"{dotted}({arg.value!r})")
		if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
			if isinstance(node.value, ast.Constant) and node.value.value in (True, 1):
				findings.append("ignore_permissions=True")
	return findings


class TestRecorridosApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG241 WH")
		cls.item = cls.world.item("FG241-ITEM")
		cls.item2 = cls.world.item("FG241-ITEM-2")
		cls.customer = cls.world.customer("FG241 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg241-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg241-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg241-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg241-recorrido-b@example.com", ["Recorrido"])
		cls.no_role_user = cls.world.user("fg241-norole@example.com", [])

		# Company-isolation fixtures (Commit 24.1 section 16/17) -- created
		# ONCE here (not per-test) since IntegrationTestCase in this app
		# does not roll back between test methods within the same class
		# (only fx.TestWorld.cleanup() at class end does) -- confirmed the
		# hard way when a first draft of this file re-created the same
		# fixed-name User in two different tests and hit a real
		# DuplicateEntryError on the second one.
		cls.other_wh = "Finished Goods - _TC"
		cls.other_customer = cls.world.customer("FG241 Other Company Customer")
		cls.other_bodega_user = cls.world.user("fg241-other-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.other_bodega_user, cls.other_wh)
		# Pick List.set_item_locations() (called by ERPNext's own native
		# create_pick_list() mapper, see world.pick_list_for()) allocates
		# from REAL Bin.actual_qty -- with none stocked here, it silently
		# produced zero Pick List Item rows (confirmed empirically: a
		# first draft of this fixture hit "El Pick List no tiene líneas
		# para alistar" from bodega.start_picking() for exactly this
		# reason). stock_up() only touches this one Warehouse's own Bin,
		# same as every other fixture in this suite.
		cls.world.stock_up(cls.item.name, cls.other_wh, 1000, rate=50)

	# -- Shared setup helpers --------------------------------------------------

	def _facturado_pick_list(self, qty=5, rate=100, item=None, customer=None):
		"""The full real chain: Bodega alistamiento -> Facturación checklist
		100% -> mark_as_invoiced(). Never a shortcut -- every eligible Pick
		List in this suite is genuinely fg_invoicing_status=Facturado via
		the real Commit 23.0 endpoints, same as production."""
		item = item or self.item
		customer = customer or self.customer
		so = self.world.submitted_sales_order(item.name, self.wh.name, qty, customer.name, rate=rate)
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

	def _submitted_not_facturado_pick_list(self, qty=5, rate=100):
		"""Submitted (picked to completion) but Facturación's own checklist
		was never run -- stays fg_invoicing_status="Pendiente"."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _draft_pick_list(self, qty=5):
		"""Never picked/submitted -- docstatus stays 0."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _multi_line_facturado_pick_list(self, qty_a=4, qty_b=6):
		"""Two Pick List Item rows -- for item_count/total_qty tests."""
		so = self.world.multi_item_sales_order(
			self.customer.name,
			[
				{"item_code": self.item.name, "warehouse": self.wh.name, "qty": qty_a, "rate": 100},
				{"item_code": self.item2.name, "warehouse": self.wh.name, "qty": qty_b, "rate": 100},
			],
		)
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

	def _set_customer_primary_address(self, customer, address_line1="Calle 10 # 5-20", city="Bogotá"):
		from fabergray_erp.api import clientes

		return clientes.update_customer(
			customer.name, address={"address_line1": address_line1, "city": city, "state": "Bogotá D.C."}
		)

	def _force_route_status(self, route_name, status):
		"""Simulates a future commit's own start_route()/deliver_stop() --
		neither exists yet in 24.1 (deliberately out of scope), so tests
		that need an En Ruta/Completado route set it directly, exactly as
		the brief's own section 13 anticipates."""
		frappe.db.set_value("Recorrido", route_name, "status", status)

	def _track_route(self, route):
		"""Every recorridos.* write call below goes through the thin
		_create_route()/_plan_route()/_update_route_stops()/
		_cancel_route() wrappers rather than the raw API directly, so the
		real Recorrido/Recorrido Parada rows they create get cleaned up by
		world.cleanup() same as any other fixture -- otherwise an orphaned
		Recorrido Parada still Link-ing to a test's own ad hoc Customer/
		Address blocks THAT cleanup later (LinkExistsError), confirmed by
		a real run during this suite's own development. Tracking the same
		name twice is harmless (world.cleanup() skips anything already
		gone), so calling this after every write is always safe, whether
		or not this particular row was already tracked."""
		if not route:
			return route
		self.world.track_existing("Recorrido", route["name"])
		for stop in route.get("stops") or []:
			self.world.track_existing("Recorrido Parada", stop["name"])
		return route

	def _create_route(self, **kwargs):
		return self._track_route(recorridos.create_route(**kwargs))

	def _plan_route(self, route_name):
		return self._track_route(recorridos.plan_route(route_name))

	def _update_route_stops(self, route_name, pick_lists):
		return self._track_route(recorridos.update_route_stops(route_name, pick_lists=pick_lists))

	def _cancel_route(self, route_name):
		return self._track_route(recorridos.cancel_route(route_name))

	# =====================================================================
	# 1/2. Permisos sobre get_available_orders()
	# =====================================================================

	def test_recorrido_role_can_list_available_orders(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders()
		self.assertIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])

	def test_unauthorized_user_rejected_from_available_orders(self):
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				recorridos.get_available_orders()

	# =====================================================================
	# 3/4/5. Elegibilidad -- solo submitted + Facturado
	# =====================================================================

	def test_draft_pick_list_excluded(self):
		so, pl = self._draft_pick_list()
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders()
		self.assertNotIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])

	def test_pending_facturacion_pick_list_excluded(self):
		so, pl = self._submitted_not_facturado_pick_list()
		self.assertEqual(pl.fg_invoicing_status, "Pendiente")
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders()
		self.assertNotIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])

	def test_facturado_pick_list_included(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders()
		self.assertIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])

	# =====================================================================
	# 6. Company isolation
	# =====================================================================

	def _other_company_facturado_pick_list(self):
		""""_Test Company" is a pre-existing ERPNext demo company on this
		site (confirmed live, same convention test_inventario_api.py's own
		test_warehouse_from_another_company_rejected() established) --
		"Finished Goods - _TC" is its own real Warehouse, self.other_wh
		(class fixture -- see setUpClass for why these are shared, not
		created fresh per test). Built through the exact same real
		Bodega+Facturación chain as every other fixture in this suite,
		just against a different Company, to prove eligibility is
		genuinely company-scoped end to end, not just at one layer.
		currency="INR" explicit -- "_Test Company"'s own default_currency
		(confirmed live), needed because this site has no COP->INR
		Currency Exchange record configured and the Customer/Item masters
		here default to COP (this app's real company's currency)."""
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"transaction_date": frappe.utils.nowdate(),
				"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
				"set_warehouse": self.other_wh,
				"items": [
					{
						"item_code": self.item.name,
						"warehouse": self.other_wh,
						"qty": 5,
						"rate": 100,
						"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
					}
				],
			}
		)
		so.insert()
		with fx.without_sales_order_hook():
			so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		self.world._track(so)

		pl = self.world.pick_list_for(so, self.other_wh)
		with fx.as_user(self.other_bodega_user):
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

	def test_other_company_pick_list_excluded(self):
		so, pl = self._other_company_facturado_pick_list()
		self.assertEqual(pl.company, "_Test Company")
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders()
		self.assertNotIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])

	def test_other_company_pick_list_rejected_by_create_route(self):
		so, pl = self._other_company_facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.PickListNotEligibleError):
				self._create_route(pick_lists=[pl.name])

	# =====================================================================
	# 7/8/9. Crear Recorrido
	# =====================================================================

	def test_create_route_with_one_pick_list(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		self.assertEqual(route["status"], "Borrador")
		self.assertEqual(route["total_stops"], 1)
		self.assertEqual(route["stops"][0]["pick_list"], pl.name)

	def test_create_route_with_multiple_pick_lists(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
		self.assertEqual(route["total_stops"], 2)
		self.assertEqual({s["pick_list"] for s in route["stops"]}, {pl1.name, pl2.name})

	def test_create_route_sequence_matches_input_order(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		so3, pl3 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl3.name, pl1.name, pl2.name])
		by_pick_list = {s["pick_list"]: s["sequence"] for s in route["stops"]}
		self.assertEqual(by_pick_list[pl3.name], 1)
		self.assertEqual(by_pick_list[pl1.name], 2)
		self.assertEqual(by_pick_list[pl2.name], 3)

	# =====================================================================
	# 10/11/12/13/14. Resolución server-side de datos de la parada
	# =====================================================================

	def test_customer_data_resolved_server_side(self):
		"""create_route() takes no customer/address/qty argument at all --
		the only way to prove "resolved server-side" is to show the
		persisted stop matches the REAL Pick List, never something a
		client could have supplied (there is no parameter for it)."""
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		stop = route["stops"][0]
		self.assertEqual(stop["customer"], pl.customer)
		self.assertEqual(stop["customer_name"], pl.customer_name)
		self.assertEqual(stop["sales_order"], so.name)

	def test_primary_address_resolved_correctly(self):
		customer = self.world.customer("FG241 Address Customer")
		self._set_customer_primary_address(customer, address_line1="Cra 7 # 20-15", city="Medellín")
		so, pl = self._facturado_pick_list(customer=customer)
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		stop = route["stops"][0]
		self.assertIsNotNone(stop["customer_address"])
		expected_address = frappe.db.get_value("Customer", customer.name, "customer_primary_address")
		self.assertEqual(stop["customer_address"], expected_address)

	def test_address_display_is_text_snapshot(self):
		customer = self.world.customer("FG241 Snapshot Customer")
		self._set_customer_primary_address(customer, address_line1="Av Siempre Viva 742", city="Cali")
		so, pl = self._facturado_pick_list(customer=customer)
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		stop = route["stops"][0]
		self.assertIsInstance(stop["address_display"], str)
		self.assertIn("Cali", stop["address_display"])

		# Snapshot, not live -- changing the customer's address afterward
		# must NOT retroactively change the already-created parada.
		frappe.db.set_value("Address", stop["customer_address"], "city", "Barranquilla")
		with fx.as_user(self.recorrido_user):
			detail_again = recorridos.get_route_detail(route["name"])
		self.assertIn("Cali", detail_again["stops"][0]["address_display"])

	def test_item_count_correct(self):
		so, pl = self._multi_line_facturado_pick_list(qty_a=4, qty_b=6)
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		self.assertEqual(route["stops"][0]["item_count"], 2)

	def test_total_qty_correct(self):
		so, pl = self._multi_line_facturado_pick_list(qty_a=4, qty_b=6)
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
		self.assertEqual(flt(route["stops"][0]["total_qty"]), 10)

	# =====================================================================
	# 15. Sin valores económicos
	# =====================================================================

	def test_no_economic_values_anywhere(self):
		forbidden_keys = {"rate", "amount", "grand_total", "net_total", "price", "account", "outstanding_amount"}
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			available = recorridos.get_available_orders()
			for row in available["pick_lists"]:
				self.assertFalse(forbidden_keys & set(row.keys()), row.keys())

			detail = recorridos.get_available_order_detail(pl.name)
			self.assertFalse(forbidden_keys & set(detail.keys()), detail.keys())

			route = self._create_route(pick_lists=[pl.name])
			self.assertFalse(forbidden_keys & set(route.keys()), route.keys())
			for stop in route["stops"]:
				self.assertFalse(forbidden_keys & set(stop.keys()), stop.keys())

	# =====================================================================
	# 16/17/18/19. Rechazo de asignación duplicada
	# =====================================================================

	def test_duplicate_pick_list_in_request_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteValidationError):
				self._create_route(pick_lists=[pl.name, pl.name])

	def test_pick_list_already_in_borrador_route_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			self._create_route(pick_lists=[pl.name])
			with self.assertRaises(recorridos.PickListAlreadyAssignedError):
				self._create_route(pick_lists=[pl.name])

	def test_pick_list_already_in_planificado_route_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
			with self.assertRaises(recorridos.PickListAlreadyAssignedError):
				self._create_route(pick_lists=[pl.name])

	def test_pick_list_already_in_en_ruta_route_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.PickListAlreadyAssignedError):
				self._create_route(pick_lists=[pl.name])

	def test_cancelled_route_releases_pick_list(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._cancel_route(route["name"])
			result = recorridos.get_available_orders()
		self.assertIn(pl.name, [r["pick_list"] for r in result["pick_lists"]])
		# and re-assignable
		with fx.as_user(self.recorrido_user):
			route2 = self._create_route(pick_lists=[pl.name])
		self.assertEqual(route2["total_stops"], 1)

	# =====================================================================
	# 21/22/23/24/25. Editar paradas
	# =====================================================================

	def test_reorder_stops_in_borrador(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
			updated = self._update_route_stops(route["name"], pick_lists=[pl2.name, pl1.name])
		by_pick_list = {s["pick_list"]: s["sequence"] for s in updated["stops"]}
		self.assertEqual(by_pick_list[pl2.name], 1)
		self.assertEqual(by_pick_list[pl1.name], 2)

	def test_reorder_keeps_stop_identity(self):
		"""The architectural point of Recorrido Parada being a standalone
		DocType: a stop that stays in the route through a reorder keeps
		its own `name`, never deleted-and-recreated."""
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
			original_names = {s["pick_list"]: s["name"] for s in route["stops"]}
			updated = self._update_route_stops(route["name"], pick_lists=[pl2.name, pl1.name])
		new_names = {s["pick_list"]: s["name"] for s in updated["stops"]}
		self.assertEqual(original_names, new_names)

	def test_add_stop_in_borrador(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name])
			updated = self._update_route_stops(route["name"], pick_lists=[pl1.name, pl2.name])
		self.assertEqual(updated["total_stops"], 2)

	def test_remove_stop_in_borrador(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
			updated = self._update_route_stops(route["name"], pick_lists=[pl1.name])
		self.assertEqual(updated["total_stops"], 1)
		self.assertEqual(updated["stops"][0]["pick_list"], pl1.name)
		# the removed Pick List is available again
		with fx.as_user(self.recorrido_user):
			available = recorridos.get_available_orders()
		self.assertIn(pl2.name, [r["pick_list"] for r in available["pick_lists"]])

	def test_cannot_edit_stops_in_planificado(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
			with self.assertRaises(recorridos.RouteNotEditableError):
				self._update_route_stops(route["name"], pick_lists=[])

	def test_cannot_edit_stops_in_en_ruta(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				self._update_route_stops(route["name"], pick_lists=[])

	# =====================================================================
	# Protección de borrado directo de Recorrido Parada (on_trash guard) --
	# la brecha: Recorrido Parada tiene permiso "delete" para el rol
	# Recorrido (necesario para que update_route_stops() pueda quitar una
	# parada legítimamente), pero eso también permitiría, sin este guard,
	# borrar una parada directamente desde Desk/API sin pasar por
	# update_route_stops() -- incluso en un recorrido que ya no está en
	# Borrador, destruyendo en silencio el historial de lo que
	# efectivamente se entregó. Cada test de abajo llama
	# frappe.get_doc("Recorrido Parada", ...).delete() DIRECTAMENTE, nunca
	# a través de recorridos.update_route_stops(), para probar
	# exactamente el hook on_trash() en fabrigray_erp.doctype.
	# recorrido_parada.recorrido_parada, no la lógica de la API.
	# =====================================================================

	def test_direct_delete_stop_allowed_when_route_borrador(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			stop_name = route["stops"][0]["name"]
			stop_doc = frappe.get_doc("Recorrido Parada", stop_name)
			stop_doc.check_permission("delete")
			stop_doc.delete()
		self.assertFalse(frappe.db.exists("Recorrido Parada", stop_name))

	def test_direct_delete_stop_rejected_when_route_planificado(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
			stop_name = route["stops"][0]["name"]
			with self.assertRaises(frappe.ValidationError) as ctx:
				frappe.get_doc("Recorrido Parada", stop_name).delete()
		self.assertIn("Solo se pueden eliminar paradas de un recorrido en estado Borrador", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Recorrido Parada", stop_name))

	def test_direct_delete_stop_rejected_when_route_en_ruta(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "En Ruta")
		stop_name = route["stops"][0]["name"]
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.ValidationError) as ctx:
				frappe.get_doc("Recorrido Parada", stop_name).delete()
		self.assertIn("Solo se pueden eliminar paradas de un recorrido en estado Borrador", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Recorrido Parada", stop_name))

	def test_direct_delete_stop_rejected_when_route_completado(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "Completado")
		stop_name = route["stops"][0]["name"]
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.ValidationError) as ctx:
				frappe.get_doc("Recorrido Parada", stop_name).delete()
		self.assertIn("Solo se pueden eliminar paradas de un recorrido en estado Borrador", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Recorrido Parada", stop_name))

	def test_direct_delete_stop_rejected_when_route_cancelado(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			stop_name = route["stops"][0]["name"]
			self._cancel_route(route["name"])
			with self.assertRaises(frappe.ValidationError) as ctx:
				frappe.get_doc("Recorrido Parada", stop_name).delete()
		self.assertIn("Solo se pueden eliminar paradas de un recorrido en estado Borrador", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Recorrido Parada", stop_name))

	def test_update_route_stops_can_still_remove_stop_while_borrador(self):
		"""update_route_stops() itself deletes a Recorrido Parada row (see
		its own "Remove stops for Pick Lists no longer requested" block) --
		this must keep working now that on_trash() guards every delete,
		since that removal only ever happens while route.status ==
		"Borrador" was just re-checked at the top of update_route_stops()."""
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
			removed_stop_name = next(s["name"] for s in route["stops"] if s["pick_list"] == pl2.name)
			updated = self._update_route_stops(route["name"], pick_lists=[pl1.name])
		self.assertEqual(updated["total_stops"], 1)
		self.assertFalse(frappe.db.exists("Recorrido Parada", removed_stop_name))

	# =====================================================================
	# 26/27/28. Planificar
	# =====================================================================

	def test_plan_route_valid(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			planned = self._plan_route(route["name"])
		self.assertEqual(planned["status"], "Planificado")

	def test_plan_route_without_stops_rejected(self):
		company = frappe.db.get_value("Warehouse", self.wh.name, "company")
		route_doc = frappe.get_doc({"doctype": "Recorrido", "company": company})
		route_doc.insert(ignore_permissions=True)
		self.world.track_existing("Recorrido", route_doc.name)
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteValidationError):
				self._plan_route(route_doc.name)

	def test_plan_route_with_duplicate_sequence_rejected(self):
		so1, pl1 = self._facturado_pick_list()
		so2, pl2 = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl1.name, pl2.name])
		# Corrupt sequence directly -- normal API flow never produces
		# this, but plan_route() must defend against it anyway per the
		# brief's own "diseña de forma extensible" instruction.
		stop_names = [s["name"] for s in route["stops"]]
		frappe.db.set_value("Recorrido Parada", stop_names[1], "sequence", 1)
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteValidationError):
				self._plan_route(route["name"])

	# =====================================================================
	# 29/30/31/32. Cancelar
	# =====================================================================

	def test_cancel_route_from_borrador(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			cancelled = self._cancel_route(route["name"])
		self.assertEqual(cancelled["status"], "Cancelado")

	def test_cancel_route_from_planificado(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
			cancelled = self._cancel_route(route["name"])
		self.assertEqual(cancelled["status"], "Cancelado")

	def test_cancel_route_from_en_ruta_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "En Ruta")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				self._cancel_route(route["name"])

	def test_cancel_route_from_completado_rejected(self):
		so, pl = self._facturado_pick_list()
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self._force_route_status(route["name"], "Completado")
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(recorridos.RouteNotEditableError):
				self._cancel_route(route["name"])

	# =====================================================================
	# 33/34/35/36/37/38/39/40. Nunca toca inventario/contabilidad/dispatch
	# =====================================================================

	def test_does_not_modify_pick_list_item_qty(self):
		so, pl = self._facturado_pick_list(qty=7)
		before = [(r.name, flt(r.qty)) for r in pl.locations]
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		pl.reload()
		after = [(r.name, flt(r.qty)) for r in pl.locations]
		self.assertEqual(before, after)

	def test_does_not_modify_picked_qty(self):
		so, pl = self._facturado_pick_list(qty=7)
		before = [(r.name, flt(r.picked_qty)) for r in pl.locations]
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		pl.reload()
		after = [(r.name, flt(r.picked_qty)) for r in pl.locations]
		self.assertEqual(before, after)

	def test_does_not_modify_delivered_qty(self):
		so, pl = self._facturado_pick_list(qty=7)
		before = [(r.name, flt(r.delivered_qty)) for r in pl.locations]
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		pl.reload()
		after = [(r.name, flt(r.delivered_qty)) for r in pl.locations]
		self.assertEqual(before, after)

	def test_does_not_modify_sales_order(self):
		so, pl = self._facturado_pick_list(qty=7)
		before = frappe.db.get_value(
			"Sales Order", so.name, ["per_billed", "per_delivered", "billing_status", "status"]
		)
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
			self._cancel_route(route["name"])
		after = frappe.db.get_value(
			"Sales Order", so.name, ["per_billed", "per_delivered", "billing_status", "status"]
		)
		self.assertEqual([flt(v) if i < 2 else v for i, v in enumerate(before)], [flt(v) if i < 2 else v for i, v in enumerate(after)])

	def test_no_sales_invoice_created(self):
		so, pl = self._facturado_pick_list()
		before = frappe.db.count("Sales Invoice")
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self.assertEqual(frappe.db.count("Sales Invoice"), before)

	def test_no_delivery_note_created(self):
		so, pl = self._facturado_pick_list()
		before = frappe.db.count("Delivery Note")
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self.assertEqual(frappe.db.count("Delivery Note"), before)

	def test_no_stock_entry_created(self):
		so, pl = self._facturado_pick_list()
		before = frappe.db.count("Stock Entry")
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self.assertEqual(frappe.db.count("Stock Entry"), before)

	def test_no_gl_entry_created(self):
		so, pl = self._facturado_pick_list()
		before = frappe.db.count("GL Entry")
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl.name])
			self._plan_route(route["name"])
		self.assertEqual(frappe.db.count("GL Entry"), before)

	# =====================================================================
	# 41. Guardrails estructurales
	# =====================================================================

	def test_no_frappe_db_commit_in_any_whitelisted_function(self):
		for fn in (
			recorridos.get_available_orders,
			recorridos.get_available_order_detail,
			recorridos.get_route_detail,
			recorridos.create_route,
			recorridos.update_route_stops,
			recorridos.plan_route,
			recorridos.cancel_route,
		):
			source = inspect.getsource(fn)
			self.assertNotIn("frappe.db.commit()", source, f"{fn.__name__}() must never call frappe.db.commit()")

	def test_whole_module_never_creates_accounting_or_dispatch_documents(self):
		import fabergray_erp.api.recorridos as recorridos_module

		source = inspect.getsource(recorridos_module)
		findings = _forbidden_findings(source)
		self.assertEqual(findings, [], f"api/recorridos.py reaches a forbidden document/pattern: {findings}")

	# =====================================================================
	# 42. Concurrencia real -- doble asignación
	# =====================================================================

	def test_double_assignment_protected_under_real_concurrency(self):
		"""Real concurrency, not simulated: two independent threads, each
		with its OWN frappe DB connection (frappe.init()+frappe.connect()+
		frappe.db.begin() -- a plain frappe.connect() alone stays in
		autocommit mode with no open transaction, which was confirmed,
		during this commit's own development, to let the row lock provide
		NO protection at all), both call update_route_stops() targeting
		TWO DIFFERENT, pre-existing, empty Borrador routes with the SAME
		Pick List at (as close as achievable) the same instant.

		update_route_stops() on two separate pre-existing routes (not
		create_route() for both) deliberately isolates this test to the
		Pick-List-row-lock mechanism itself -- an earlier version of this
		test used two concurrent create_route() calls and saw unrelated
		naming-series counter contention on the new Recorrido's own
		autoname interfere with the result, which is why Recorrido
		Parada's own autoname is "hash" (no shared counter row at all,
		see its own doctype JSON) rather than a sequential series.

		Exactly one attempt must succeed; the other must be rejected with
		PickListAlreadyAssignedError -- never both succeeding (the bug
		this whole locking mechanism exists to prevent), and never a raw
		QueryDeadlockError reaching the caller (api.recorridos.
		_retrying_on_deadlock's own job, exercised for real here, not
		mocked).

		Needs a real frappe.db.commit() for the setup data ONLY (so the
		two threads' own separate connections can see it -- their fresh
		transactions cannot see this test's own IntegrationTestCase
		transaction, which never commits until teardown) -- the one
		justified commit in this whole suite, with matching manual cleanup
		below rather than relying on IntegrationTestCase's automatic
		rollback, which only ever covers the OUTER connection's own
		transaction."""
		so, pl = self._facturado_pick_list()
		company = pl.company
		frappe.db.commit()

		route_a = frappe.get_doc({"doctype": "Recorrido", "company": company})
		route_a.insert(ignore_permissions=True)
		route_b = frappe.get_doc({"doctype": "Recorrido", "company": company})
		route_b.insert(ignore_permissions=True)
		frappe.db.commit()

		site = frappe.local.site
		results = {}

		def attempt(key, user_email, route_name):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user_email)
			try:
				recorridos.update_route_stops(route_name, pick_lists=[pl.name])
				frappe.db.commit()
				results[key] = ("ok", route_name)
			except frappe.ValidationError as e:
				frappe.db.rollback()
				results[key] = ("error", str(e))
			except Exception as e:  # pragma: no cover -- diagnostic only
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		t1 = threading.Thread(target=attempt, args=("a", self.recorrido_user, route_a.name))
		t2 = threading.Thread(target=attempt, args=("b", self.recorrido_user_b, route_b.name))
		t1.start()
		t2.start()
		t1.join(timeout=30)
		t2.join(timeout=30)

		try:
			outcomes = [results.get("a"), results.get("b")]
			successes = [o for o in outcomes if o and o[0] == "ok"]
			errors = [o for o in outcomes if o and o[0] == "error"]

			self.assertEqual(len(successes), 1, outcomes)
			self.assertEqual(len(errors), 1, outcomes)
			self.assertIn("ya está asignado a otro recorrido activo", errors[0][1])
		finally:
			# Manual cleanup -- this test committed real data, bypassing
			# the outer IntegrationTestCase transaction's automatic
			# rollback.
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			for name in (route_a.name, route_b.name):
				for stop in frappe.get_all("Recorrido Parada", filters={"recorrido": name}, pluck="name"):
					frappe.delete_doc("Recorrido Parada", stop, force=True, ignore_permissions=True)
				frappe.delete_doc("Recorrido", name, force=True, ignore_permissions=True)
			frappe.db.commit()
