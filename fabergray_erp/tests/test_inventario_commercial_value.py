# -*- coding: utf-8 -*-
"""Hotfix Inventario -- EXISTENCIAS TOTALES / VALOR COMERCIAL in
api.inventario.get_inventory_summary(), built from the SAME per-product
numbers the product list and detail show:

- stock total of a product = _bin_totals(): SUM(max(Bin.actual_qty, 0))
  over every leaf, enabled warehouse of the company under its real root
  (Devoluciones/Cuarentena included, as the detail always showed); test
  warehouses (no parent) and other companies' warehouses never count;
- price = _selling_rates(): ONE current Standard Selling price (no
  customer, valid today, > 0, stock UOM);
- value = stock total x price, per product (summed first, priced once);
  a product without price adds units but $0;
- summary == detail, product by product.

Calculation tests pin the warehouse set (patch _stock_warehouses) so the
site's own stock never leaks into the expected numbers; the warehouse rules
are tested unpatched. Stock is Bin-only (fixtures.stock_up()), never a
Stock Ledger Entry."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fabergray_erp.api import inventario as api
from fabergray_erp.tests import fixtures as fx
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
			summary["total_units"],
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

	# -- Warehouses (unpatched) -------------------------------------------------

	def test_stock_warehouses(self):
		warehouses = api._stock_warehouses(fx.COMPANY)
		for base in ("Producto Terminado", "Líquidos", "Materia Prima", "Envases", "Devoluciones", "Cuarentena"):
			self.assertIn(warehouse_name(base, fx.COMPANY), warehouses, base)
		self.assertIn(self.wh_a.name, warehouses)
		for excluded in (self.wh_test.name, self.root, self.other_company_wh):
			self.assertNotIn(excluded, warehouses)

	def test_real_warehouses_count_like_the_detail(self):
		for wh in (self.wh_a.name, warehouse_name("Producto Terminado", fx.COMPANY), warehouse_name("Devoluciones", fx.COMPANY)):
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, wh, 3)]), (3, 3000, 1, 0), wh)

	def test_test_other_company_and_disabled_warehouses_never_count(self):
		disabled = self._real_warehouse(f"FGINV Off {self.tag} {self._seq}")
		frappe.db.set_value("Warehouse", disabled.name, "disabled", 1)
		self.assertTrue(self.other_company_wh)
		for wh in (self.wh_test.name, self.other_company_wh, disabled.name):
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, wh, 50)]), (0, 0, 0, 0), wh)

	# -- Summary == detail ------------------------------------------------------

	def test_summary_matches_the_product_detail(self):
		item = self._item()
		self._price(item, 37700)
		self.world.stock_up(item.name, self.wh_a.name, 1)
		self.world.stock_up(item.name, self.wh_test.name, 9)  # test warehouse: neither side counts it
		detail = api.get_inventory_item_detail(item.name)
		self.assertEqual((detail["total_stock"], detail["selling_rate"]), (1, 37700))
		self.assertEqual([b["warehouse"] for b in detail["stock_by_warehouse"]], [self.wh_a.name])
		row = next(r for r in api.get_inventory_items(txt=item.name)["items"] if r["item_code"] == item.name)
		self.assertEqual((row["total_actual_qty"], row["selling_rate"]), (1, 37700))
		# Its whole contribution to the KPIs is 1 x 37.700.
		frappe.db.set_value("Bin", {"item_code": item.name, "warehouse": self.wh_a.name}, "actual_qty", 0)
		self.assertEqual(self._delta([(item, self.wh_a.name, 1)]), (1, 37700, 1, 0))

	def test_every_product_with_stock_matches_its_detail(self):
		"""Real site data, read-only: the KPIs are exactly the sum of what
		each product's detail shows (00002 included when it has stock)."""
		summary = api.get_inventory_summary()
		totals = {code: qty for code, qty in api._bin_totals().items() if qty > 0}
		units = value = 0.0
		for code, qty in totals.items():
			if frappe.db.get_value("Item", code, "disabled"):
				continue
			detail = api.get_inventory_item_detail(code)
			self.assertEqual(detail["total_stock"], qty, code)
			units += detail["total_stock"]
			value += detail["total_stock"] * flt(detail["selling_rate"])
		self.assertEqual(summary["total_units"], units)
		self.assertEqual(summary["commercial_value"], flt(value, 2))
		# total_stock (legacy key) also counts disabled Items; the KPI does not.
		self.assertGreaterEqual(summary["total_stock"], summary["total_units"])

	# -- Endpoint ---------------------------------------------------------------

	def test_summary_endpoint_keeps_its_keys_and_adds_the_value_ones(self):
		with fx.as_user(self.bodega_user):
			summary = api.get_inventory_summary()
		self.assertTrue(
			{"references", "total_stock", "out_of_stock", "low_stock", "low_stock_status"} <= set(summary)
		)
		for key in ("total_units", "commercial_value", "items_with_stock", "items_without_selling_price"):
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
