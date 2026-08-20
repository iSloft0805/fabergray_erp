# -*- coding: utf-8 -*-
"""Commit 18.2 -- api/ventas.py: the six endpoints behind the future Page
Ventas. Vendedora always operates under her own real session and real,
if_owner-scoped permissions (Commit 18.1) -- every test here proves that
directly (frappe.get_list, never frappe.get_all/ignore_permissions/
frappe.set_user inside api/ventas.py itself -- see test_regression.py's
structural guardrails for the static half of that proof).

Central theme, tested from several angles: Vendedora never sees or sends
a price, discount, tax or total, yet the Sales Orders she submits still
get correctly priced by ERPNext's own native pricing engine, and still
correctly trigger the Commit 15/16/18.1 Fulfillment Engine end to end.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega as bodega_api
from fabergray_erp.api import jefe_bodega as jefe_bodega_api
from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

# Every field create_and_submit_sales_order() must reject if a line tries
# to send it -- exactly the list the Commit 18.2 brief named explicitly.
_FORBIDDEN_ITEM_FIELDS = [
	"rate",
	"price_list_rate",
	"discount_percentage",
	"discount_amount",
	"amount",
	"net_rate",
	"net_amount",
	"margin_rate_or_amount",
	"margin_type",
]

_ECONOMIC_KEYS = {
	"rate",
	"price_list_rate",
	"amount",
	"net_rate",
	"net_amount",
	"base_rate",
	"base_amount",
	"total",
	"grand_total",
	"net_total",
	"base_grand_total",
	"base_net_total",
	"discount_percentage",
	"discount_amount",
	"taxes",
	"margin_rate_or_amount",
}


class TestVentasApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG18-2 Main")
		cls.item = cls.world.item("FG18-2-ITEM", default_warehouse=cls.wh.name)
		cls.item_no_stock = cls.world.item(
			"FG18-2-NOSTOCK-ITEM", default_material_request_type="Purchase", default_warehouse=cls.wh.name
		)
		cls.customer = cls.world.customer("FG18-2 Customer")

		cls.vendedora_a = cls.world.user("fg18-2-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg18-2-vendedora-b@example.com", ["Vendedora"])

	# -- search_customers ----------------------------------------------------

	def test_vendedora_can_search_customers(self):
		with fx.as_user(self.vendedora_a):
			results = ventas.search_customers(self.customer.name[:6])
		self.assertIn(self.customer.name, [r["name"] for r in results])
		for row in results:
			self.assertEqual(set(row.keys()), {"name", "customer_name"})

	# -- search_items ---------------------------------------------------------

	def test_vendedora_can_search_items(self):
		with fx.as_user(self.vendedora_a):
			results = ventas.search_items("FG18-2-ITEM")
		self.assertIn(self.item.name, [r["item_code"] for r in results])

	def test_search_items_response_never_contains_prices_or_costs(self):
		with fx.as_user(self.vendedora_a):
			results = ventas.search_items("FG18-2")
		allowed = {"item_code", "item_name", "description", "stock_uom", "image"}
		for row in results:
			self.assertTrue(set(row.keys()).issubset(allowed), row.keys())
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

	# -- get_item_info ----------------------------------------------------------

	def test_get_item_info_has_a_strict_response_allowlist(self):
		self.world.stock_up(self.item.name, self.wh.name, 42)
		with fx.as_user(self.vendedora_a):
			info = ventas.get_item_info(self.item.name)

		self.assertEqual(
			set(info.keys()),
			{"item_code", "item_name", "description", "stock_uom", "image", "qty_disponible"},
		)
		self.assertEqual(info["item_code"], self.item.name)
		self.assertEqual(info["qty_disponible"], 42)
		self.assertFalse(_ECONOMIC_KEYS & set(info.keys()))

	# -- create_and_submit_sales_order: happy path + pricing isolation -------

	def test_vendedora_can_create_and_submit_her_own_sales_order(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 3}],
				observations="Entregar en la mañana",
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		so = frappe.get_doc("Sales Order", result["name"])
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.owner, self.vendedora_a)
		self.assertEqual(so.customer, self.customer.name)
		self.assertEqual(so.items[0].qty, 3)

	def test_final_price_comes_from_erpnext_native_pricing_not_from_vendedora(self):
		"""Vendedora never sends a rate, yet the submitted Sales Order ends
		up with a real, non-zero rate/amount -- resolved entirely by
		ERPNext's own AccountsController.validate() during insert(), never
		by anything in api/ventas.py (which never reads or writes a price
		field)."""
		price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		if not frappe.db.exists("Item Price", {"item_code": self.item.name, "price_list": price_list}):
			ip = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": self.item.name,
					"price_list": price_list,
					"price_list_rate": 250,
				}
			)
			ip.insert()
			self.world.track_existing("Item Price", ip.name)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		so = frappe.get_doc("Sales Order", result["name"])
		self.assertEqual(so.items[0].rate, 250)
		self.assertEqual(so.grand_total, 500)

	def test_injecting_rate_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.create_and_submit_sales_order(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "rate": 1}],
				)

	def test_injecting_price_list_rate_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.create_and_submit_sales_order(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "price_list_rate": 999}],
				)

	def test_injecting_discounts_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.create_and_submit_sales_order(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "discount_percentage": 50}],
				)
			with self.assertRaises(frappe.ValidationError):
				ventas.create_and_submit_sales_order(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "discount_amount": 10}],
				)

	def test_all_forbidden_item_fields_are_rejected(self):
		with fx.as_user(self.vendedora_a):
			for field in _FORBIDDEN_ITEM_FIELDS:
				with self.assertRaises(frappe.ValidationError, msg=field):
					ventas.create_and_submit_sales_order(
						customer=self.customer.name,
						items=[{"item_code": self.item.name, "qty": 1, field: 1}],
					)

	def test_unknown_field_in_a_line_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.create_and_submit_sales_order(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "warehouse": self.wh.name}],
				)

	# -- get_my_orders / get_sales_summary: if_owner isolation --------------

	def test_vendedora_only_gets_her_own_orders(self):
		with fx.as_user(self.vendedora_a):
			result_a = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result_a["name"])
		self.world.track_existing_pick_lists_and_reports_for(result_a["name"])

		with fx.as_user(self.vendedora_b):
			result_b = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result_b["name"])
		self.world.track_existing_pick_lists_and_reports_for(result_b["name"])

		with fx.as_user(self.vendedora_a):
			orders_a = ventas.get_my_orders()
		names_a = [o["name"] for o in orders_a]
		self.assertIn(result_a["name"], names_a)
		self.assertNotIn(result_b["name"], names_a)

		with fx.as_user(self.vendedora_b):
			orders_b = ventas.get_my_orders()
		names_b = [o["name"] for o in orders_b]
		self.assertIn(result_b["name"], names_b)
		self.assertNotIn(result_a["name"], names_b)

	def test_order_responses_never_contain_economic_data(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			orders = ventas.get_my_orders()
		order = next(o for o in orders if o["name"] == result["name"])
		self.assertFalse(_ECONOMIC_KEYS & set(order.keys()))

	def test_sales_summary_respects_if_owner(self):
		"""Each Vendedora's `pedidos_hoy` moves by exactly the orders *she*
		submits, regardless of what the other one does in between --
		relative before/after diffs on both sides, deliberately not an
		absolute count, since this class-level TestWorld may already carry
		state from other test methods run earlier (see fixtures.py's own
		docstring on why IntegrationTestCase's rollback isn't relied on
		here)."""
		with fx.as_user(self.vendedora_a):
			before_a = ventas.get_sales_summary()["pedidos_hoy"]
		with fx.as_user(self.vendedora_b):
			before_b = ventas.get_sales_summary()["pedidos_hoy"]

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			after_a = ventas.get_sales_summary()["pedidos_hoy"]
		with fx.as_user(self.vendedora_b):
			after_b = ventas.get_sales_summary()["pedidos_hoy"]

		self.assertEqual(after_a, before_a + 1)
		self.assertEqual(after_b, before_b)  # B's own count is untouched by A's order

	# -- E2E: full stock / partial stock / zero stock, through the real hook -

	def test_e2e_full_stock_creates_pick_list_visible_to_bodega(self):
		wh = self.world.warehouse("FG18-2 E2E Full")
		item = self.world.item("FG18-2-E2E-FULL-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)

		bodega_user = self.world.user("fg18-2-bodega-full@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 4}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		pick_lists = frappe.get_all(
			"Pick List Item",
			filters={"sales_order": result["name"], "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(len(pick_lists), 1)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": result["name"]}), 0)

		with fx.as_user(bodega_user):
			queue = bodega_api.get_queue()
		self.assertIn(pick_lists[0], [p["name"] for p in queue["pendientes"]])

	def test_e2e_partial_stock_creates_pick_list_and_shortage_report(self):
		wh = self.world.warehouse("FG18-2 E2E Partial")
		item = self.world.item(
			"FG18-2-E2E-PARTIAL-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 3)

		jefe_user = self.world.user("fg18-2-jefe-partial@example.com", ["Jefe de Bodega"])

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")
		self.assertEqual(len(reports), 1)
		report = frappe.get_doc("Reporte de Faltante", reports[0])
		self.assertEqual(report.qty_faltante, 7.0)
		self.assertEqual(report.shortage_reason, "Compra pendiente")

		with fx.as_user(jefe_user):
			open_reports = jefe_bodega_api.get_open_shortage_reports()
		self.assertIn(report.name, [r["name"] for r in open_reports])

	def test_e2e_zero_stock_creates_correct_shortage_report(self):
		wh = self.world.warehouse("FG18-2 E2E Zero")
		item = self.world.item(
			"FG18-2-E2E-ZERO-ITEM", default_material_request_type="Manufacture", default_warehouse=wh.name
		)
		raw_material = self.world.item("FG18-2-E2E-ZERO-RAW")
		self.world.bom_for(item.name, raw_material.name)  # needed so the route resolves to "Fabricación pendiente" and not "Configuración incompleta"

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 5}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		pick_lists = frappe.get_all(
			"Pick List Item", filters={"sales_order": result["name"]}, pluck="parent", distinct=True
		)
		self.assertEqual(len(pick_lists), 0)

		reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")
		self.assertEqual(len(reports), 1)
		report = frappe.get_doc("Reporte de Faltante", reports[0])
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.shortage_reason, "Producción pendiente")

	def test_e2e_vendedora_still_cannot_read_pick_list_or_shortage_report(self):
		wh = self.world.warehouse("FG18-2 E2E NoAccess")
		item = self.world.item(
			"FG18-2-E2E-NOACCESS-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 2)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 6}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		pl_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": result["name"]}, pluck="parent", distinct=True
		)[0]
		report_name = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")[0]

		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Pick List", "read", doc=pl_name))
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read", doc=report_name))

	def test_another_vendedora_cannot_read_or_modify_the_created_sales_order(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Sales Order", "read", doc=result["name"]))
			self.assertFalse(frappe.has_permission("Sales Order", "write", doc=result["name"]))
			self.assertEqual(
				frappe.get_list("Sales Order", filters={"name": result["name"]}, pluck="name"), []
			)
