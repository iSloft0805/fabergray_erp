# -*- coding: utf-8 -*-
"""Hotfix Inventario -- EXISTENCIAS VENDIBLES / VALOR COMERCIAL in
api.inventario.get_inventory_summary(), built from the SAME per-product
numbers the product list and detail show:

- _bin_totals(): per product, physical_qty = SUM(max(Bin.actual_qty, 0))
  over every leaf, enabled warehouse of the company under its real root
  (Devoluciones/Cuarentena included: the stock exists), and sellable_qty =
  the same minus Devoluciones, Cuarentena and native rejected warehouses;
  test warehouses (no parent) and other companies' warehouses never count;
- price = _selling_rates(): ONE current Standard Selling price (no
  customer, valid today, > 0, stock UOM);
- value = sellable_qty x price, per product (summed first, priced once);
  a product without price adds units but $0;
- summary == detail, product by product; the detail keeps every warehouse
  row and flags the non-sellable ones.

Calculation tests pin the warehouse set (patch _stock_warehouses) so the
site's own stock never leaks into the expected numbers; the warehouse rules
are tested unpatched. Stock is Bin-only (fixtures.stock_up()), never a
Stock Ledger Entry -- except TestCommercialValueAfterStockIssue, which
needs real stock for COMPLETAR PEDIDO's Material Issue."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fabergray_erp.api import bodega, facturacion
from fabergray_erp.api import inventario as api
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as recorridos_base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base
from fabergray_erp.warehouses import ROOT_WAREHOUSE, warehouse_name

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

SELLING = "Standard Selling"


class TestInventoryCommercialValue(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.tag = frappe.generate_hash(length=4).upper()
		cls.root = warehouse_name(ROOT_WAREHOUSE, fx.COMPANY)
		cls.wh_test = cls.world.warehouse(f"FGINV Test {cls.tag}")  # no parent: a test warehouse
		cls.other_company_wh = frappe.db.get_value(
			"Warehouse", {"company": ["!=", fx.COMPANY], "is_group": 0, "disabled": 0}, "name"
		)
		cls.bodega_user = cls.world.user(f"fginv-bodega-{cls.tag.lower()}@example.com", ["Bodega"])
		cls._seq = 0

	def setUp(self):
		# Fresh warehouses per test: stock seeded by one test never leaks into another's totals.
		type(self)._seq += 1
		self.wh_a = self._real_warehouse(f"FGINV A {self.tag} {self._seq}")
		self.wh_b = self._real_warehouse(f"FGINV B {self.tag} {self._seq}")

	@classmethod
	def _real_warehouse(cls, name):
		doc = frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": name, "company": fx.COMPANY, "parent_warehouse": cls.root}
		).insert()
		return cls.world._track(doc)

	def _item(self):
		type(self)._seq += 1
		item = self.world.item(f"FGINV-{self.tag}-{self._seq}")
		for name in frappe.get_all("Item Price", filters={"item_code": item.name}, pluck="name"):
			self.world.track_existing("Item Price", name)  # native auto price, if any
		return item

	def _price(self, item, rate, price_list=SELLING, **extra):
		existing = not extra and frappe.db.get_value(
			"Item Price", {"item_code": item.name, "price_list": price_list, "customer": ["is", "not set"]}
		)
		if existing:  # e.g. the native auto price of a new Item
			doc = frappe.get_doc("Item Price", existing)
			doc.price_list_rate = rate
			doc.save()
			return doc
		doc = frappe.get_doc(
			{"doctype": "Item Price", "item_code": item.name, "price_list": price_list, "price_list_rate": rate, **extra}
		).insert()
		return self.world._track(doc)

	def _numbers(self, summary):
		return (
			summary["sellable_units"],
			summary["commercial_value"],
			summary["items_with_stock"],
			summary["items_without_selling_price"],
		)

	def _summary(self, warehouses):
		with patch.object(api, "_stock_warehouses", return_value=warehouses):
			return api._inventory_value_summary(api._bin_totals())

	def _delta(self, stock):
		"""Unpatched summary numbers after `stock` [(item, warehouse, qty)] minus before."""
		before = self._numbers(api.get_inventory_summary())
		for item, wh, qty in stock:
			self.world.stock_up(item.name, wh, qty)
		after = self._numbers(api.get_inventory_summary())
		return tuple(round(a - b, 2) for a, b in zip(after, before))

	# -- Calculation ----------------------------------------------------------

	def test_one_unit_at_37700_is_37700(self):
		"""Fixture equivalent of 00002 (ACEITE 2 TIEMPOS): 1 unit x $37.700."""
		item = self._item()
		self._price(item, 37700)
		self.world.stock_up(item.name, self.wh_a.name, 1)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (1, 37700, 1, 0))

	def test_several_items(self):
		a, b = self._item(), self._item()
		self._price(a, 5000)
		self._price(b, 2000)
		self.world.stock_up(a.name, self.wh_a.name, 10)
		self.world.stock_up(b.name, self.wh_a.name, 3)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (13, 56000, 2, 0))

	def test_same_item_in_two_warehouses_is_summed_then_priced_once(self):
		item = self._item()
		self._price(item, 10000)
		self.world.stock_up(item.name, self.wh_a.name, 3)
		self.world.stock_up(item.name, self.wh_b.name, 2)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name, self.wh_b.name])), (5, 50000, 1, 0))

	def test_item_without_price_counts_units_but_no_value(self):
		priced, unpriced = self._item(), self._item()
		self._price(priced, 1000)
		self.world.stock_up(priced.name, self.wh_a.name, 2)
		self.world.stock_up(unpriced.name, self.wh_a.name, 7)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (9, 2000, 2, 1))

	def test_negative_stock_is_not_available(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_a.name, -4)
		self.world.stock_up(item.name, self.wh_b.name, 6)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name, self.wh_b.name])), (6, 6000, 1, 0))

	def test_zero_stock(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_a.name, 0)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (0, 0, 0, 0))
		self.assertEqual(self._numbers(self._summary([])), (0, 0, 0, 0))

	def test_disabled_item_is_not_counted(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_a.name, 4)
		frappe.db.set_value("Item", item.name, "disabled", 1)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (0, 0, 0, 0))

	def test_only_the_current_standard_selling_price_counts(self):
		customer = self.world.customer(f"FGINV Cliente {self.tag}")
		cases = {
			"lista de compra": dict(price_list="Standard Buying"),
			"precio de cliente": dict(customer=customer.name),
			"vencido": dict(valid_from=add_days(nowdate(), -30), valid_upto=add_days(nowdate(), -1)),
			"futuro": dict(valid_from=add_days(nowdate(), 5)),
		}
		for label, extra in cases.items():
			item = self._item()
			self.world.stock_up(item.name, self.wh_a.name, 4)
			price_list = extra.pop("price_list", SELLING)
			self._price(item, 9999, price_list=price_list, **extra)
			self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (4, 0, 1, 1), label)
			frappe.db.set_value("Bin", {"item_code": item.name, "warehouse": self.wh_a.name}, "actual_qty", 0)

		# A current Standard Selling price next to all of those wins alone.
		item = self._item()
		self.world.stock_up(item.name, self.wh_a.name, 4)
		self._price(item, 100, price_list="Standard Buying")
		self._price(item, 9999, customer=customer.name)
		self._price(item, 2500)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (4, 10000, 1, 0))

	# -- Physical vs sellable ---------------------------------------------------

	def _stock(self, item):
		return api._bin_totals([item.name]).get(item.name)

	def test_devoluciones_and_cuarentena_are_physical_but_not_sellable(self):
		for base in ("Devoluciones", "Cuarentena"):
			item = self._item()
			self._price(item, 1000)
			wh = warehouse_name(base, fx.COMPANY)
			self.assertEqual(self._delta([(item, wh, 50)]), (0, 0, 0, 0), base)  # no sellable KPI change
			self.assertEqual(dict(self._stock(item)), {"physical_qty": 50, "sellable_qty": 0}, base)

	def test_devoluciones_50_and_cuarentena_100_are_no_vendible_in_the_detail(self):
		"""00002/00006-like: priced, physically there, never sellable nor valued."""
		for base, qty, rate in (("Devoluciones", 50, 37700), ("Cuarentena", 100, 16245)):
			item = self._item()
			self._price(item, rate)
			wh = warehouse_name(base, fx.COMPANY)
			self.world.stock_up(item.name, wh, qty)
			detail = api.get_inventory_item_detail(item.name)
			self.assertEqual(
				(detail["physical_stock"], detail["sellable_stock"], detail["selling_rate"], detail["commercial_value"]),
				(qty, 0, rate, 0),
				base,
			)
			self.assertEqual([(b["warehouse"], b["sellable"]) for b in detail["stock_by_warehouse"]], [(wh, False)], base)

	def test_native_rejected_warehouse_is_physical_but_not_sellable(self):
		frappe.db.set_value("Warehouse", self.wh_b.name, "is_rejected_warehouse", 1)
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_b.name, 8)
		self.assertEqual(dict(self._stock(item)), {"physical_qty": 8, "sellable_qty": 0})

	def test_same_item_sellable_plus_returned(self):
		item = self._item()
		self._price(item, 10000)
		devoluciones = warehouse_name("Devoluciones", fx.COMPANY)
		self.world.stock_up(item.name, self.wh_a.name, 3)
		self.world.stock_up(item.name, devoluciones, 5)
		self.assertEqual(dict(self._stock(item)), {"physical_qty": 8, "sellable_qty": 3})
		summary = self._summary([self.wh_a.name, devoluciones])
		self.assertEqual(self._numbers(summary), (3, 30000, 1, 0))  # the price multiplies only sellable_qty

	# -- Warehouses (unpatched) -------------------------------------------------

	def test_stock_warehouses(self):
		warehouses = api._stock_warehouses(fx.COMPANY)
		for base in ("Producto Terminado", "Líquidos", "Varios", "Materia Prima", "Envases", "Devoluciones", "Cuarentena"):
			self.assertIn(warehouse_name(base, fx.COMPANY), warehouses, base)
		self.assertIn(self.wh_a.name, warehouses)
		for excluded in (self.wh_test.name, self.root, self.other_company_wh):
			self.assertNotIn(excluded, warehouses)

	def test_sellable_warehouses_count(self):
		for base in ("Producto Terminado", "Líquidos", "Varios", "Materia Prima"):
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, warehouse_name(base, fx.COMPANY), 3)]), (3, 3000, 1, 0), base)

	def test_non_sellable_test_other_company_and_disabled_warehouses_never_count(self):
		disabled = self._real_warehouse(f"FGINV Off {self.tag} {self._seq}")
		frappe.db.set_value("Warehouse", disabled.name, "disabled", 1)
		self.assertTrue(self.other_company_wh)
		for wh in (
			warehouse_name("Devoluciones", fx.COMPANY),
			warehouse_name("Cuarentena", fx.COMPANY),
			self.wh_test.name,
			self.other_company_wh,
			disabled.name,
		):
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, wh, 50)]), (0, 0, 0, 0), wh)

	# -- Summary == detail ------------------------------------------------------

	def test_detail_shows_physical_sellable_and_value(self):
		"""00002-like: 50 in Devoluciones -> physical 50, sellable 0, $0; plus
		1 sellable unit at $37.700 -> sellable 1, $37.700."""
		item = self._item()
		self._price(item, 37700)
		devoluciones = warehouse_name("Devoluciones", fx.COMPANY)
		self.world.stock_up(item.name, devoluciones, 50)
		self.world.stock_up(item.name, self.wh_test.name, 9)  # test warehouse: never counted
		detail = api.get_inventory_item_detail(item.name)
		self.assertEqual(
			(detail["physical_stock"], detail["sellable_stock"], detail["selling_rate"], detail["commercial_value"]),
			(50, 0, 37700, 0),
		)
		self.assertEqual([(b["warehouse"], b["sellable"]) for b in detail["stock_by_warehouse"]], [(devoluciones, False)])

		self.world.stock_up(item.name, self.wh_a.name, 1)
		detail = api.get_inventory_item_detail(item.name)
		self.assertEqual((detail["physical_stock"], detail["sellable_stock"], detail["commercial_value"]), (51, 1, 37700))
		self.assertEqual(
			{b["warehouse"]: b["sellable"] for b in detail["stock_by_warehouse"]}, {devoluciones: False, self.wh_a.name: True}
		)
		row = next(r for r in api.get_inventory_items(txt=item.name)["items"] if r["item_code"] == item.name)
		self.assertEqual((row["total_actual_qty"], row["sellable_qty"], row["selling_rate"]), (51, 1, 37700))

	def test_every_product_with_stock_matches_its_detail(self):
		"""Real site data, read-only: the KPIs are exactly the sum of what
		each product's detail shows (00002 included when it has stock)."""
		summary = api.get_inventory_summary()
		physical = sellable = value = 0.0
		items = without_price = 0
		for code, stock in api._bin_totals().items():
			if frappe.db.get_value("Item", code, "disabled") or stock.physical_qty <= 0:
				continue
			detail = api.get_inventory_item_detail(code)
			self.assertEqual((detail["physical_stock"], detail["sellable_stock"]), (stock.physical_qty, stock.sellable_qty), code)
			physical += detail["physical_stock"]
			sellable += detail["sellable_stock"]
			value += detail["commercial_value"]
			if detail["sellable_stock"] > 0:
				items += 1
				without_price += detail["selling_rate"] is None
		self.assertEqual(
			(summary["physical_units"], summary["sellable_units"], summary["commercial_value"]),
			(physical, sellable, flt(value, 2)),
		)
		self.assertEqual((summary["items_with_stock"], summary["items_without_selling_price"]), (items, without_price))

	# -- Endpoint ---------------------------------------------------------------

	def test_summary_endpoint_keeps_its_keys_and_adds_the_value_ones(self):
		with fx.as_user(self.bodega_user):
			summary = api.get_inventory_summary()
		self.assertTrue(
			{"references", "total_stock", "out_of_stock", "low_stock", "low_stock_status"} <= set(summary)
		)
		for key in ("physical_units", "sellable_units", "commercial_value", "items_with_stock", "items_without_selling_price"):
			self.assertIn(key, summary)
		self.assertEqual(summary["commercial_price_list"], SELLING)

	def test_summary_is_read_only(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_a.name, 3)
		counts = lambda: [frappe.db.count(dt) for dt in ("Bin", "Item Price", "Stock Ledger Entry", "GL Entry", "Stock Entry")]
		modified = frappe.db.get_value("Bin", {"item_code": item.name}, "modified")
		before = counts()
		api.get_inventory_summary()
		api.get_inventory_item_detail(item.name)
		self.assertEqual(counts(), before)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": item.name}, "modified"), modified)
		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": item.name}, "actual_qty")), 3)


class TestCommercialValueAfterStockIssue(IntegrationTestCase):
	"""INVENTARIO-OUT-01 + commercial value: the value is computed from the
	REAL stock after COMPLETAR PEDIDO's Material Issue (Bin 20 -> 16), never
	minus the Pick List again (never 12), and invoicing / the route never
	change it. Real stock (Stock Reconciliation) in this suite's own
	warehouse; _stock_warehouses is pinned to it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		tag = frappe.generate_hash(length=4).upper()
		cls.wh = cls.world.warehouse(f"FGINV Valor {tag}")
		cls.item = cls.world.item(f"FGINV-VALOR-{tag}")
		cls.item_codes = [cls.item.name]
		cls.addClassCleanup(_purge_item_prices, cls.item_codes)
		cls.customer = cls.world.customer(f"FGINV Valor Cliente {tag}")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 20, rate=5000)
		for name in frappe.get_all("Item Price", filters={"item_code": cls.item.name}, pluck="name"):
			frappe.delete_doc("Item Price", name, ignore_permissions=True, force=True)  # native auto price
		cls.world._track(
			frappe.get_doc(
				{"doctype": "Item Price", "item_code": cls.item.name, "price_list": SELLING, "price_list_rate": 10000}
			).insert()
		)
		cls.bodega_user = cls.world.user(f"fginv-valor-bodega-{tag.lower()}@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fginv-valor-facturacion-{tag.lower()}@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user(f"fginv-valor-recorrido-{tag.lower()}@example.com", ["Recorrido"])
		cls._seq = 0
		cls._evidence_file_names = []
		cls.addClassCleanup(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__, cls)

	_set_customer_primary_address = recorridos_base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = recorridos_base.TestRecorridosApi._geocode_customer_address
	_track_route = recorridos_base.TestRecorridosApi._track_route
	_create_route = recorridos_base.TestRecorridosApi._create_route
	_plan_route = recorridos_base.TestRecorridosApi._plan_route
	_driver = recorridos_base.TestRecorridosApi._driver
	_unique = start_base.TestRecorridosStartRoute._unique
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_start = start_base.TestRecorridosStartRoute._start
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files

	def _value(self):
		"""(Bin.actual_qty, summary sellable/value, detail sellable/value)."""
		with patch.object(api, "_stock_warehouses", return_value=[self.wh.name]):
			summary = api._inventory_value_summary(api._bin_totals())
			detail = api.get_inventory_item_detail(self.item.name)
		actual = flt(frappe.db.get_value("Bin", {"item_code": self.item.name, "warehouse": self.wh.name}, "actual_qty"))
		return (
			actual,
			summary["sellable_units"],
			summary["commercial_value"],
			detail["sellable_stock"],
			detail["commercial_value"],
		)

	def test_value_follows_real_stock_through_complete_invoice_and_route(self):
		self.assertEqual(self._value(), (20, 20, 200000, 20, 200000))

		customer = self._stop_customer()
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 4, customer.name, rate=10000)
		pl = self.world.pick_list_for(so, self.wh.name).name
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl)
			for row in bodega.get_pick_list(pl)["rows"]:
				bodega.set_picked_qty(pl, row["row_name"], row["qty_solicitada"])
			self.assertTrue(bodega.finish_picking(pl)["stock_entry"])
		# The open order still reserves 4 natively; the value is NOT 12.
		self.assertEqual(self._value(), (16, 16, 160000, 16, 160000))

		with fx.as_user(self.facturacion_user):
			for it in facturacion.get_invoicing_detail(pl)["items"]:
				facturacion.set_invoicing_item_checked(pl, it["row_name"], 1)
			facturacion.mark_as_invoiced(pl, "integrandoMAS")
		self.assertEqual(self._value(), (16, 16, 160000, 16, 160000))

		driver = self._driver(self._unique("Conductor")).name
		with fx.as_user(self.recorrido_user):
			route = self._create_route(pick_lists=[pl], driver=driver)
			self._plan_route(route["name"])
		started = self._start(route["name"])
		self.assertEqual(self._value(), (16, 16, 160000, 16, 160000))

		self._deliver(route["name"], started["stops"][0]["name"])
		self.assertEqual(frappe.db.get_value("Recorrido Parada", started["stops"][0]["name"], "status"), "Entregado")
		self.assertEqual(self._value(), (16, 16, 160000, 16, 160000))


def _purge_item_prices(item_codes):
	frappe.db.delete("Item Price", {"item_code": ["in", item_codes]})
	frappe.db.delete("Bin", {"item_code": ["in", item_codes]})
	frappe.db.commit()
