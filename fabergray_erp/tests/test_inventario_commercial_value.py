# -*- coding: utf-8 -*-
"""Hotfix Inventario -- total units and commercial value of the sellable
stock, in api.inventario.get_inventory_summary().

- per Bin row (item_code + warehouse): available = max(actual_qty, 0);
  value = available x the Item's current Standard Selling price (0 without
  one); one price per Item, never duplicated by several stock rows;
- warehouses: only the commercial ones (Producto Terminado + Líquidos,
  Varios, Cafetería, Jardinería, Piscina), enabled leaves under the
  company's root; raw material, packaging, WIP, Devoluciones, Cuarentena,
  test warehouses (no parent) and other companies' warehouses never count,
  even when their Items have a price;
- price: Standard Selling only, no customer price, currently valid, > 0,
  stock UOM;
- read-only.

Calculation tests pin the warehouse set (patch _commercial_warehouses) so
the site's own stock never leaks into the expected numbers; the warehouse
rules themselves are tested unpatched. Stock is Bin-only (fixtures.
stock_up()), never a Stock Ledger Entry."""

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
		cls.devoluciones = warehouse_name("Devoluciones", fx.COMPANY)
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

	def _summary(self, warehouses):
		with patch.object(api, "_commercial_warehouses", return_value=warehouses):
			return api._commercial_summary(fx.COMPANY)

	def _numbers(self, summary):
		return (
			summary["total_units"],
			summary["commercial_value"],
			summary["items_with_stock"],
			summary["items_without_selling_price"],
		)

	# -- Calculation ----------------------------------------------------------

	def test_ten_units_at_5000_is_50000(self):
		item = self._item()
		self._price(item, 5000)
		self.world.stock_up(item.name, self.wh_a.name, 10)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (10, 50000, 1, 0))

	def test_several_items(self):
		a, b = self._item(), self._item()
		self._price(a, 5000)
		self._price(b, 2000)
		self.world.stock_up(a.name, self.wh_a.name, 10)
		self.world.stock_up(b.name, self.wh_a.name, 3)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name])), (13, 56000, 2, 0))

	def test_same_item_in_two_warehouses_is_priced_once_per_unit(self):
		item = self._item()
		self._price(item, 10000)
		self.world.stock_up(item.name, self.wh_a.name, 5)
		self.world.stock_up(item.name, self.wh_b.name, 3)
		self.assertEqual(self._numbers(self._summary([self.wh_a.name, self.wh_b.name])), (8, 80000, 1, 0))

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

	COMMERCIAL = ("Producto Terminado", "Líquidos", "Varios", "Cafetería", "Jardinería", "Piscina")
	NON_COMMERCIAL = (
		"Materia Prima",
		"Materias Primas",
		"Envases",
		"Material de Empaque",
		"Producción WIP",
		"Devoluciones",
		"Cuarentena",
	)

	def _delta(self, stock):
		"""Commercial numbers after `stock` [(item, warehouse, qty)] minus before."""
		before = self._numbers(api._commercial_summary(fx.COMPANY))
		for item, wh, qty in stock:
			self.world.stock_up(item.name, wh, qty)
		after = self._numbers(api._commercial_summary(fx.COMPANY))
		return tuple(round(a - b, 2) for a, b in zip(after, before))

	def test_commercial_warehouses_are_exactly_the_sellable_ones(self):
		self.assertEqual(
			set(api._commercial_warehouses(fx.COMPANY)), {warehouse_name(b, fx.COMPANY) for b in self.COMMERCIAL}
		)

	def test_each_commercial_warehouse_counts(self):
		for base in self.COMMERCIAL:
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, warehouse_name(base, fx.COMPANY), 3)]), (3, 3000, 1, 0), base)

	def test_non_commercial_stock_never_counts_even_with_a_price(self):
		excluded = [warehouse_name(b, fx.COMPANY) for b in self.NON_COMMERCIAL]
		excluded += [self.wh_test.name, self.wh_a.name, self.other_company_wh]  # test / not listed / other company
		self.assertTrue(self.other_company_wh)
		for wh in excluded:
			item = self._item()
			self._price(item, 1000)
			self.assertEqual(self._delta([(item, wh, 50)]), (0, 0, 0, 0), wh)

	def test_commercial_item_without_price_counts_units_at_zero_value(self):
		item = self._item()
		self.assertEqual(self._delta([(item, warehouse_name("Varios", fx.COMPANY), 7)]), (7, 0, 1, 1))

	def test_commercial_negative_stock_does_not_count(self):
		item = self._item()
		self._price(item, 1000)
		stock = [(item, warehouse_name("Líquidos", fx.COMPANY), -4), (item, warehouse_name("Piscina", fx.COMPANY), 6)]
		self.assertEqual(self._delta(stock), (6, 6000, 1, 0))

	def test_breakdown_adds_up_to_the_totals(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, warehouse_name("Jardinería", fx.COMPANY), 2)
		summary = api._commercial_summary(fx.COMPANY)
		rows = summary["commercial_breakdown"]
		self.assertEqual([r["warehouse"] for r in rows], summary["commercial_warehouses"])
		self.assertEqual(round(sum(r["commercial_qty"] for r in rows), 2), summary["total_units"])
		self.assertEqual(round(sum(r["commercial_value"] for r in rows), 2), summary["commercial_value"])

	# -- Endpoint ---------------------------------------------------------------

	def test_summary_endpoint_keeps_its_keys_and_adds_the_commercial_ones(self):
		with fx.as_user(self.bodega_user):
			summary = api.get_inventory_summary()
		self.assertTrue(
			{"references", "total_stock", "out_of_stock", "low_stock", "low_stock_status"} <= set(summary)
		)
		for key in ("total_units", "commercial_value", "items_with_stock", "items_without_selling_price"):
			self.assertIn(key, summary)
		self.assertEqual(summary["commercial_price_list"], SELLING)
		self.assertNotIn(self.devoluciones, summary["commercial_warehouses"])
		self.assertIn("commercial_breakdown", summary)

	def test_summary_is_read_only(self):
		item = self._item()
		self._price(item, 1000)
		self.world.stock_up(item.name, self.wh_a.name, 3)
		counts = lambda: [frappe.db.count(dt) for dt in ("Bin", "Item Price", "Stock Ledger Entry", "GL Entry", "Stock Entry")]
		modified = frappe.db.get_value("Bin", {"item_code": item.name}, "modified")
		before = counts()
		api.get_inventory_summary()
		self.assertEqual(counts(), before)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": item.name}, "modified"), modified)
		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": item.name}, "actual_qty")), 3)
