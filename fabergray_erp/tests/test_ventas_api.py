# -*- coding: utf-8 -*-
"""Commit 18.2 -- api/ventas.py: the six endpoints behind the future Page
Ventas. Vendedora always operates under her own real session and real
permissions (Commit 18.1; Commit 25.1 dropped the original if_owner=1
scoping -- "el rol controla el área, no el owner", so every Vendedora now
shares every Sales Order of this site's own Company, Company isolation
enforced by fabergray_erp/permission_conditions.py instead) -- every test
here proves that directly (frappe.get_list, never frappe.get_all/
ignore_permissions/frappe.set_user inside api/ventas.py itself -- see
test_regression.py's structural guardrails for the static half of that
proof).

Central theme, tested from several angles: Vendedora never sees or sends
a price, discount, tax or total, yet the Sales Orders she submits still
get correctly priced by ERPNext's own native pricing engine, and still
correctly trigger the Commit 15/16/18.1 Fulfillment Engine end to end.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

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

	def test_vendedora_sees_every_company_order_including_others(self):
		"""Commit 25.1: "el rol controla el área, no el owner" -- get_my_
		orders() is no longer scoped to the caller's own orders; both
		Vendedoras see BOTH orders (was assertNotIn on the other one's
		order, pre-25.1)."""
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
		self.assertIn(result_b["name"], names_a)

		with fx.as_user(self.vendedora_b):
			orders_b = ventas.get_my_orders()
		names_b = [o["name"] for o in orders_b]
		self.assertIn(result_b["name"], names_b)
		self.assertIn(result_a["name"], names_b)

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

	# -- get_order_detail (Commit 18.4) --------------------------------------

	def test_vendedora_can_get_order_detail_with_items(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 3}],
				observations="Entregar en la mañana",
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			detail = ventas.get_order_detail(result["name"])

		self.assertEqual(detail["name"], result["name"])
		self.assertEqual(detail["customer"], self.customer.name)
		self.assertEqual(detail["item_count"], 1)
		self.assertEqual(detail["total_qty"], 3)
		self.assertEqual(detail["observations"], "Entregar en la mañana")
		self.assertEqual(len(detail["items"]), 1)
		self.assertEqual(detail["items"][0]["item_code"], self.item.name)
		self.assertEqual(detail["items"][0]["qty"], 3)
		self.assertEqual(detail["items"][0]["stock_uom"], self.item.stock_uom)

	def test_get_order_detail_response_never_contains_economic_data(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			detail = ventas.get_order_detail(result["name"])

		self.assertFalse(_ECONOMIC_KEYS & set(detail.keys()))
		for row in detail["items"]:
			self.assertEqual(set(row.keys()), {"item_code", "item_name", "qty", "stock_uom"})
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

	def test_another_vendedora_can_read_order_detail(self):
		"""Commit 25.1: get_order_detail() is now readable by any Vendedora
		of the same Company, not just the order's own creator (was
		assertRaises(PermissionError) pre-25.1)."""
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_b):
			detail = ventas.get_order_detail(result["name"])
		self.assertEqual(detail["name"], result["name"])

	def test_sales_summary_reflects_every_vendedoras_orders(self):
		"""Commit 25.1: get_sales_summary() is company-wide, not per-
		owner -- BOTH Vendedoras' `pedidos_hoy` move by +1 when A submits
		one order (was "only A moves, B untouched" pre-25.1). Relative
		before/after diffs on both sides, deliberately not an absolute
		count, since this class-level TestWorld may already carry state
		from other test methods run earlier (see fixtures.py's own
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
		self.assertEqual(after_b, before_b + 1)  # shared: B sees A's new order too

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

	# -- E2E (Commit 19.2): Purchase Service creates a Material Request too --

	def test_e2e_vendedora_submit_creates_material_request_despite_no_permission(self):
		"""Vendedora has zero permission on Material Request, by design
		(Commit 18.1's Option B, same as Pick List/Reporte de Faltante) --
		this proves sync_material_requests_for_sales_order() (Commit 19.1,
		wired into process_sales_order() this commit) still succeeds as a
		consequence of her own already-authorized Sales Order submit,
		exactly like Pick List/Reporte de Faltante already do."""
		wh = self.world.warehouse("FG19-2 E2E MR")
		item = self.world.item(
			"FG19-2-E2E-MR-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 2)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		so = frappe.get_doc("Sales Order", result["name"])
		mr_names = frappe.get_all(
			"Material Request Item", filters={"sales_order": so.name}, pluck="parent", distinct=True
		)
		self.assertEqual(len(mr_names), 1)
		mr = frappe.get_doc("Material Request", mr_names[0])
		self.assertEqual(mr.docstatus, 0)
		self.assertEqual(mr.owner, self.vendedora_a)  # her session, never a substituted identity
		self.assertEqual(flt(mr.items[0].qty), 8.0)
		self.assertEqual(mr.items[0].sales_order, so.name)
		self.assertEqual(mr.items[0].sales_order_item, so.items[0].name)

	def test_e2e_vendedora_still_cannot_access_material_request_directly(self):
		wh = self.world.warehouse("FG19-2 E2E MR NoAccess")
		item = self.world.item(
			"FG19-2-E2E-MR-NOACCESS-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 2)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 6}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		mr_name = frappe.get_all(
			"Material Request Item", filters={"sales_order": result["name"]}, pluck="parent", distinct=True
		)[0]

		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Material Request", "read", doc=mr_name))
			self.assertFalse(frappe.has_permission("Material Request", "create"))
			self.assertFalse(frappe.has_permission("Material Request", "write", doc=mr_name))

	def test_another_vendedora_can_read_the_created_sales_order(self):
		"""Commit 25.1: read/write are role+Company-scoped, not owner-
		scoped -- both has_permission() and her own get_list() now agree
		the order is visible (was assertFalse/empty-list pre-25.1)."""
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_b):
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=result["name"]))
			self.assertTrue(frappe.has_permission("Sales Order", "write", doc=result["name"]))
			self.assertEqual(
				frappe.get_list("Sales Order", filters={"name": result["name"]}, pluck="name"), [result["name"]]
			)

	# =====================================================================
	# Commit 18.5 -- get_editable_order / update_draft_sales_order /
	# delete_draft_sales_order / cancel_sales_order
	# =====================================================================

	def _draft_so(self, vendedora, item=None, qty=5, customer=None):
		"""A plain Draft Sales Order built directly as `vendedora` herself
		(her own real create=1/if_owner=1 permission, Commit 18.1) --
		never through create_and_submit_sales_order(), which always
		submits. Mirrors exactly what that function builds, minus the
		final .submit() call."""
		item = item or self.item
		customer = customer or self.customer
		delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		with fx.as_user(vendedora):
			so = frappe.get_doc(
				{
					"doctype": "Sales Order",
					"customer": customer.name,
					"company": frappe.defaults.get_global_default("company"),
					"transaction_date": frappe.utils.nowdate(),
					"delivery_date": delivery_date,
					"set_warehouse": self.wh.name,
					"items": [
						{
							"item_code": item.name,
							"warehouse": self.wh.name,
							"qty": qty,
							"delivery_date": delivery_date,
						}
					],
				}
			)
			so.insert()
		self.world.track_existing("Sales Order", so.name)
		return so

	# -- get_editable_order / update_draft_sales_order -----------------------

	def test_vendedora_can_edit_her_own_draft(self):
		other_customer = self.world.customer("FG18-5 Other Customer")
		other_item = self.world.item("FG18-5-OTHER-ITEM", default_warehouse=self.wh.name)
		so = self._draft_so(self.vendedora_a)

		with fx.as_user(self.vendedora_a):
			editable = ventas.get_editable_order(so.name)
			self.assertEqual(editable["name"], so.name)

			result = ventas.update_draft_sales_order(
				name=so.name,
				customer=other_customer.name,
				items=[{"item_code": other_item.name, "qty": 9}],
				observations="Pedido editado",
			)
		self.assertEqual(result["name"], so.name)

		so.reload()
		self.assertEqual(so.docstatus, 0)
		self.assertEqual(so.customer, other_customer.name)
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].item_code, other_item.name)
		self.assertEqual(so.items[0].qty, 9)
		self.assertEqual(so.fg_observations, "Pedido editado")

	def test_another_vendedora_can_edit_it_while_draft(self):
		"""Commit 25.1: a Draft Sales Order is editable by any Vendedora
		of the same Company, not just its creator -- docstatus/estado
		still governs (see test_editing_a_submitted_order_fails right
		below, unchanged by this commit), owner no longer does."""
		so = self._draft_so(self.vendedora_a)

		with fx.as_user(self.vendedora_b):
			editable = ventas.get_editable_order(so.name)
			self.assertEqual(editable["name"], so.name)
			result = ventas.update_draft_sales_order(
				name=so.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.assertEqual(result["name"], so.name)
		so.reload()
		self.assertEqual(so.items[0].qty, 2)

	def test_editing_a_submitted_order_fails(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.get_editable_order(result["name"])
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=result["name"], customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
				)

	def test_injecting_rate_fails_on_update(self):
		so = self._draft_so(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=so.name,
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "rate": 999}],
				)

	def test_injecting_amount_and_discount_fails_on_update(self):
		so = self._draft_so(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=so.name,
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "amount": 500}],
				)
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=so.name,
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "discount_percentage": 20}],
				)

	# -- delete_draft_sales_order ---------------------------------------------

	def test_vendedora_can_delete_her_own_draft(self):
		so = self._draft_so(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			result = ventas.delete_draft_sales_order(so.name)
		self.assertEqual(result["name"], so.name)
		self.assertFalse(frappe.db.exists("Sales Order", so.name))

	def test_another_vendedora_can_delete_it_while_draft(self):
		"""Commit 25.1: `delete=1` on the Custom DocPerm row carries the
		same if_owner=0 as read/write/create/cancel/submit -- Frappe's own
		permission model has no per-ptype if_owner override on a single
		row, so this follows the identical role+Company+Draft-state rule
		as edit (was assertRaises(PermissionError) pre-25.1)."""
		so = self._draft_so(self.vendedora_a)
		with fx.as_user(self.vendedora_b):
			result = ventas.delete_draft_sales_order(so.name)
		self.assertEqual(result["name"], so.name)
		self.assertFalse(frappe.db.exists("Sales Order", so.name))

	def test_submitted_order_cannot_be_deleted(self):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.delete_draft_sales_order(result["name"])
		self.assertTrue(frappe.db.exists("Sales Order", result["name"]))

	# -- cancel_sales_order ----------------------------------------------------

	def test_vendedora_can_cancel_her_own_submitted_order_when_erpnext_allows(self):
		wh = self.world.warehouse("FG18-5 Cancel OK")
		item = self.world.item("FG18-5-CANCEL-OK-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			cancel_result = ventas.cancel_sales_order(result["name"])
		self.assertEqual(cancel_result["name"], result["name"])
		self.assertEqual(frappe.db.get_value("Sales Order", result["name"], "docstatus"), 2)

	def test_another_vendedora_can_cancel_it_when_erpnext_allows(self):
		"""Commit 25.1: cancelling a submitted Sales Order is now
		role+Company-gated, not owner-gated -- section 1's own core
		example ("Vendedora B debe poder ver/gestionar PEDIDO-001").
		ERPNext's own native back-link protection (Pick List/Material
		Request/etc.) still applies unmodified either way -- unrelated to
		who is cancelling, see test_cancellation_blocked_by_submitted_
		document_preserves_native_block below, unchanged by this commit."""
		wh = self.world.warehouse("FG18-5 Cancel Blocked Other")
		item = self.world.item("FG18-5-CANCEL-BLOCKED-OTHER-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_b):
			cancel_result = ventas.cancel_sales_order(result["name"])
		self.assertEqual(cancel_result["name"], result["name"])
		self.assertEqual(frappe.db.get_value("Sales Order", result["name"], "docstatus"), 2)

	def test_cancel_triggers_real_cleanup(self):
		wh = self.world.warehouse("FG18-5 Cleanup")
		item = self.world.item(
			"FG18-5-CLEANUP-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 3)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 8}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		pick_list_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": result["name"], "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]
		report_name = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")[0]

		with fx.as_user(self.vendedora_a):
			ventas.cancel_sales_order(result["name"])

		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))  # Commit 17 cleanup, draft removed
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Resuelto")

	def test_cancellation_blocked_by_submitted_document_preserves_native_block(self):
		from fabergray_erp.api import bodega

		wh = self.world.warehouse("FG18-5 Cancel Blocked PL")
		item = self.world.item("FG18-5-CANCEL-BLOCKED-PL-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		bodega_user = self.world.user("fg18-5-bodega@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		pl_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": result["name"], "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]
		with fx.as_user(bodega_user):
			bodega.start_picking(pl_name)
			row = bodega.get_pick_list(pl_name)["rows"][0]
			bodega.set_picked_qty(pl_name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl_name)  # submits the Pick List
		frappe.db.commit()  # fixtures + submitted Pick List survive the rollback below

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.LinkExistsError):
				ventas.cancel_sales_order(result["name"])

		# Same finding Commit 17 already documented: ERPNext's back-link
		# check runs AFTER on_cancel, so docstatus=2 is already written to
		# this same, still-open transaction by the time the error fires --
		# roll back explicitly (simulating what a real request's error
		# handler does) before inspecting state.
		frappe.db.rollback()

		self.assertEqual(frappe.db.get_value("Sales Order", result["name"], "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "docstatus"), 1)  # untouched, still submitted

	# -- Cancelled queda solo lectura ------------------------------------------

	def test_cancelled_order_is_read_only(self):
		wh = self.world.warehouse("FG18-5 ReadOnly")
		item = self.world.item("FG18-5-READONLY-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)

		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		with fx.as_user(self.vendedora_a):
			ventas.cancel_sales_order(result["name"])

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=result["name"], customer=self.customer.name, items=[{"item_code": item.name, "qty": 1}]
				)
			with self.assertRaises(frappe.ValidationError):
				ventas.delete_draft_sales_order(result["name"])
			with self.assertRaises(frappe.ValidationError):
				ventas.cancel_sales_order(result["name"])

		# VER (get_order_detail) still works -- read-only access is preserved.
		with fx.as_user(self.vendedora_a):
			detail = ventas.get_order_detail(result["name"])
		self.assertEqual(detail["status"], "Cancelled")
