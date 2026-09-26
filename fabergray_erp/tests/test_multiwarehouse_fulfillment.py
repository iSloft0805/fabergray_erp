# -*- coding: utf-8 -*-
"""Fase 28.4A.3 -- multi-warehouse fulfillment.

Every Sales Order line is picked ONLY from its own warehouse (Sales Order
Item.warehouse, resolved natively from the Item Default):
pick_list_mixin.set_item_locations() runs the native allocation once per
line warehouse instead of letting a Pick List without parent_warehouse
search every warehouse of the company in Bin.creation order.

- a Líquidos line never consumes Varios stock, and vice versa;
- Devoluciones / Cuarentena (warehouses.non_picking_warehouses()) are never
  a picking source, whatever the order of Bin creation;
- one Pick List carries the lines of several warehouses, each row keeps
  its line's warehouse, and Bodega's shortage reports keep it too;
- the remainder Pick List (Fase 28.2) is per warehouse as well, with no
  duplicates;
- Bodega (queue/detail) and Jefe de Bodega (history filtered by warehouse)
  still show a Pick List whose parent_warehouse is empty.

Stock is seeded with fixtures.stock_up() (Bin only, never a Stock Ledger
Entry). Real Devoluciones/Cuarentena/Líquidos/Varios warehouses only ever
get Bins of this suite's own throwaway Items, removed at teardown."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp import warehouses

from fabergray_erp.api import bodega
from fabergray_erp.api import jefe_bodega as jefe_api
from fabergray_erp.fulfillment.analyzer import analyze_sales_order
from fabergray_erp.fulfillment.pick_list_service import (
	create_pick_list_for_available_stock,
	create_pick_list_for_full_demand,
	create_pick_list_for_remaining_demand,
)
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.warehouses import non_picking_warehouses, warehouse_name

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestMultiWarehouseFulfillment(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.item_codes = []
		# Registered BEFORE the TestWorld cleanup, so it runs AFTER it (LIFO):
		# cancelling a Sales Order re-creates Bins in its (real) warehouses.
		cls.addClassCleanup(cls._purge_own_bins)
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.tag = frappe.generate_hash(length=4).upper()
		cls.wh_liq = cls.world.warehouse(f"FG284 Liquidos {cls.tag}")
		cls.wh_var = cls.world.warehouse(f"FG284 Varios {cls.tag}")
		cls.wh_other = cls.world.warehouse(f"FG284 Otra {cls.tag}")
		cls.wh_unrelated = cls.world.warehouse(f"FG284 Ajena {cls.tag}")
		cls.devoluciones = warehouse_name("Devoluciones", fx.COMPANY)
		cls.cuarentena = warehouse_name("Cuarentena", fx.COMPANY)
		cls.customer = cls.world.customer(f"FG284 Cliente {cls.tag}")
		cls.bodega_user = cls.world.user(f"fg284-bodega-{cls.tag.lower()}@example.com", ["Bodega"])
		cls.jefe = cls.world.user(f"fg284-jefe-{cls.tag.lower()}@example.com", ["Jefe de Bodega"])
		cls._seq = 0

	@classmethod
	def _purge_own_bins(cls):
		"""Bins (and the Item Prices native Sales Order insert auto-creates)
		of this suite's own throwaway Items -- never left behind."""
		if cls.item_codes:
			frappe.db.delete("Bin", {"item_code": ["in", cls.item_codes]})
			frappe.db.delete("Item Price", {"item_code": ["in", cls.item_codes]})
			frappe.db.commit()

	def _item(self, warehouse):
		type(self)._seq += 1
		item = self.world.item(
			f"FG284-{self.tag}-{self._seq}", default_material_request_type="Purchase", default_warehouse=warehouse
		)
		self.item_codes.append(item.name)
		return item

	def _order(self, *lines):
		"""lines: (item, warehouse, qty). Submitted without the Sales Order
		hook. Always a MULTI-warehouse order: one extra line in wh_other,
		because a single-warehouse order keeps set_warehouse, the Pick List
		gets it as parent_warehouse and native picking is already scoped --
		the unscoped (cross-picking) case is the mixed order."""
		filler = self._item(self.wh_other.name)
		lines = [*lines, (filler, self.wh_other.name, 1)]
		so = self.world.multi_item_sales_order(
			self.customer.name,
			[{"item_code": item.name, "warehouse": wh, "qty": qty, "rate": 100} for item, wh, qty in lines],
		)
		self.assertFalse(so.set_warehouse)
		return so

	def _track(self, pick_list):
		if pick_list:
			self.world.track_existing("Pick List", pick_list.name)
		return pick_list

	def _rows(self, pick_list, item):
		"""{warehouse: stock_qty} of `item`'s rows."""
		out = {}
		for row in pick_list.get("locations"):
			if row.item_code == item.name:
				out[row.warehouse] = out.get(row.warehouse, 0) + flt(row.stock_qty)
		return out

	# -- Cross-picking --------------------------------------------------------

	def test_liquidos_line_never_takes_varios_stock(self):
		item = self._item(self.wh_liq.name)
		self.world.stock_up(item.name, self.wh_var.name, 10)
		so = self._order((item, self.wh_liq.name, 5))
		pl = self._track(create_pick_list_for_full_demand(so))
		self.assertEqual(self._rows(pl, item), {self.wh_liq.name: 5})

	def test_varios_line_never_takes_liquidos_stock(self):
		item = self._item(self.wh_var.name)
		self.world.stock_up(item.name, self.wh_liq.name, 10)  # older Bin, more stock
		self.world.stock_up(item.name, self.wh_var.name, 2)
		so = self._order((item, self.wh_var.name, 6))
		self.assertEqual(analyze_sales_order(so)["lines"][0]["qty_available_for_pick"], 2)
		pl = self._track(create_pick_list_for_available_stock(so))
		self.assertEqual(self._rows(pl, item), {self.wh_var.name: 2})

	def test_line_with_stock_only_in_another_warehouse_gets_no_available_pick_list(self):
		item = self._item(self.wh_var.name)
		self.world.stock_up(item.name, self.wh_liq.name, 10)
		so = self._order((item, self.wh_var.name, 3))
		self.assertIsNone(create_pick_list_for_available_stock(so))

	# -- Devoluciones / Cuarentena ----------------------------------------------

	def test_non_picking_warehouses(self):
		self.assertTrue({self.devoluciones, self.cuarentena} <= non_picking_warehouses(fx.COMPANY))
		for wh in (self.devoluciones, self.cuarentena):
			self.assertTrue(frappe.db.exists("Warehouse", {"name": wh, "company": fx.COMPANY}), wh)

	def test_never_picks_from_devoluciones_whatever_the_bin_order(self):
		item = self._item(self.wh_liq.name)
		self.world.stock_up(item.name, self.devoluciones, 100)  # created first: native Bin.creation order
		self.world.stock_up(item.name, self.wh_liq.name, 1)
		so = self._order((item, self.wh_liq.name, 5))
		self.assertEqual(analyze_sales_order(so)["lines"][0]["qty_available_for_pick"], 1)
		pl = self._track(create_pick_list_for_full_demand(so))
		self.assertEqual(self._rows(pl, item), {self.wh_liq.name: 5})  # 1 located + 4 demand, all Líquidos

	def test_never_picks_from_cuarentena(self):
		item = self._item(self.wh_liq.name)
		self.world.stock_up(item.name, self.cuarentena, 100)
		so = self._order((item, self.wh_liq.name, 4))
		self.assertIsNone(create_pick_list_for_available_stock(so))
		pl = self._track(create_pick_list_for_full_demand(so))
		self.assertEqual(self._rows(pl, item), {self.wh_liq.name: 4})

	def test_native_pick_list_without_parent_warehouse_would_cross_pick(self):
		"""Documents the native behaviour the mixin corrects: without
		parent_warehouse (every multi-warehouse order) and with the mixin's
		per-line scoping bypassed, the native search takes Devoluciones."""
		from erpnext.stock.doctype.pick_list.pick_list import PickList

		item = self._item(self.wh_liq.name)
		self.world.stock_up(item.name, self.devoluciones, 100)
		so = self._order((item, self.wh_liq.name, 5))
		pl = self._track(create_pick_list_for_full_demand(so))
		native = frappe.copy_doc(pl)
		self.assertFalse(native.parent_warehouse)  # every multi-warehouse order
		PickList.set_item_locations(native)  # the erpnext method alone, no mixin
		self.assertIn(self.devoluciones, self._rows(native, item))

	# -- One Pick List, several warehouses --------------------------------------

	def test_multiwarehouse_order_keeps_each_line_in_its_warehouse(self):
		a, b = self._item(self.wh_liq.name), self._item(self.wh_var.name)
		self.world.stock_up(a.name, self.wh_var.name, 50)  # decoys: the other line's warehouse
		self.world.stock_up(b.name, self.wh_liq.name, 50)
		self.world.stock_up(a.name, self.wh_liq.name, 3)
		self.world.stock_up(b.name, self.wh_var.name, 10)
		so = self._order((a, self.wh_liq.name, 5), (b, self.wh_var.name, 4))
		pl = self._track(create_pick_list_for_full_demand(so))
		self.assertEqual(frappe.get_all("Pick List Item", filters={"sales_order": so.name}, pluck="parent", distinct=True), [pl.name])
		self.assertFalse(pl.parent_warehouse)
		self.assertEqual(self._rows(pl, a), {self.wh_liq.name: 5})
		self.assertEqual(self._rows(pl, b), {self.wh_var.name: 4})
		# Persisted, and a re-save (native before_save) keeps it.
		pl.reload()
		pl.save()
		self.assertEqual(self._rows(pl, a), {self.wh_liq.name: 5})
		self.assertEqual(self._rows(pl, b), {self.wh_var.name: 4})
		# Idempotent: nothing is handed out twice.
		self.assertIsNone(create_pick_list_for_full_demand(so))

		# Bodega's shortage reports keep each line's warehouse.
		reports = {}
		for row in pl.get("locations"):
			if row.name and row.item_code not in reports:
				reports[row.item_code] = bodega.report_shortage(pl.name, row.name, 0, "Stock insuficiente")["name"]
				self.world.track_existing("Reporte de Faltante", reports[row.item_code])
		self.assertEqual(frappe.db.get_value("Reporte de Faltante", reports[a.name], "warehouse"), self.wh_liq.name)
		self.assertEqual(frappe.db.get_value("Reporte de Faltante", reports[b.name], "warehouse"), self.wh_var.name)

	def test_remaining_pick_list_is_per_warehouse_without_duplicates(self):
		a, b = self._item(self.wh_liq.name), self._item(self.wh_var.name)
		self.world.stock_up(a.name, self.wh_var.name, 50)
		self.world.stock_up(b.name, self.wh_var.name, 2)
		so = self._order((a, self.wh_liq.name, 5), (b, self.wh_var.name, 4))
		lines = [row.name for row in so.items]
		pl = self._track(create_pick_list_for_remaining_demand(so, lines))
		self.assertEqual(self._rows(pl, a), {self.wh_liq.name: 5})
		self.assertEqual(self._rows(pl, b), {self.wh_var.name: 4})
		self.assertIsNone(create_pick_list_for_remaining_demand(so, lines))  # already claimed
		self.assertIsNone(create_pick_list_for_full_demand(so))

	# -- Bodega / Jefe de Bodega ----------------------------------------------

	def test_bodega_and_jefe_show_the_multiwarehouse_pick_list(self):
		a, b = self._item(self.wh_liq.name), self._item(self.wh_var.name)
		so = self._order((a, self.wh_liq.name, 1), (b, self.wh_var.name, 1))
		pl = self._track(create_pick_list_for_full_demand(so))
		expected = sorted([self.wh_liq.name, self.wh_var.name, self.wh_other.name])

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
			entry = next(e for bucket in queue.values() for e in bucket if e["name"] == pl.name)
			detail = bodega.get_pick_list(pl.name)
		self.assertIsNone(entry["parent_warehouse"])
		self.assertEqual(entry["warehouses"], expected)
		self.assertEqual(detail["warehouses"], expected)
		by_item = {r["item_code"]: r["warehouse"] for r in detail["rows"]}
		self.assertEqual((by_item[a.name], by_item[b.name]), (self.wh_liq.name, self.wh_var.name))

		with fx.as_user(self.jefe):
			for wh in expected:
				history = jefe_api.get_pick_list_history(warehouse=wh, page_length=100)
				row = next((r for r in history["pick_lists"] if r["name"] == pl.name), None)
				self.assertIsNotNone(row, wh)
				self.assertEqual(row["warehouses"], expected)
			other = jefe_api.get_pick_list_history(warehouse=self.wh_unrelated.name, page_length=100)
			self.assertNotIn(pl.name, [r["name"] for r in other["pick_lists"]])

	# -- Real warehouses, real confirmation flow -------------------------------

	def test_real_liquidos_and_varios_through_the_sales_order_hook(self):
		"""Sales Order with no warehouse on its lines (as Ventas creates it):
		ERPNext resolves each one from the Item Default, and the real
		on_submit hook (process_sales_order_for_confirmation) builds the
		Pick List."""
		liquidos, varios = warehouse_name("Líquidos", fx.COMPANY), warehouse_name("Varios", fx.COMPANY)
		a, b = self._item(liquidos), self._item(varios)
		self.world.stock_up(a.name, varios, 20)
		self.world.stock_up(b.name, liquidos, 20)
		self.world.stock_up(a.name, liquidos, 2)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": frappe.utils.nowdate(),
				"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
				"items": [
					{"item_code": a.name, "qty": 5, "rate": 100},
					{"item_code": b.name, "qty": 3, "rate": 100},
				],
			}
		)
		so.insert()
		self.assertEqual({r.item_code: r.warehouse for r in so.items}, {a.name: liquidos, b.name: varios})
		self.world.track_existing("Sales Order", so.name)  # before its Pick List: cleanup runs in reverse
		so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		names = frappe.get_all("Pick List Item", filters={"sales_order": so.name}, pluck="parent", distinct=True)
		self.assertEqual(len(names), 1)
		pl = frappe.get_doc("Pick List", names[0])
		self.assertEqual(self._rows(pl, a), {liquidos: 5})
		self.assertEqual(self._rows(pl, b), {varios: 3})


class TestOperationalWarehouses(IntegrationTestCase):
	"""warehouses.ensure_operational_warehouses() -- run once on this site."""

	PRESERVED = ("Producto Terminado", "Materias Primas", "Material de Empaque", "Producción WIP", "Devoluciones", "Cuarentena")

	def _count(self):
		return frappe.db.count("Warehouse", {"company": fx.COMPANY})

	def test_operational_warehouses_exist_under_the_root(self):
		root = warehouse_name(warehouses.ROOT_WAREHOUSE, fx.COMPANY)
		for base in warehouses.OPERATIONAL_WAREHOUSES:
			row = frappe.db.get_value(
				"Warehouse", warehouse_name(base, fx.COMPANY), ["company", "parent_warehouse", "is_group", "disabled"], as_dict=True
			)
			self.assertEqual(dict(row or {}), {"company": fx.COMPANY, "parent_warehouse": root, "is_group": 0, "disabled": 0}, base)
		for name in ("Sucursal - FG", "Mercancías en Tránsito - FG"):
			self.assertFalse(frappe.db.exists("Warehouse", name), name)

	def test_previous_warehouses_are_preserved(self):
		for base in self.PRESERVED:
			name = frappe.db.get_value("Warehouse", {"name": ["like", f"{base}%"], "company": fx.COMPANY}, "name")
			self.assertTrue(name, base)
			self.assertEqual(frappe.db.get_value("Warehouse", name, "disabled"), 0, base)
		self.assertEqual(
			frappe.db.get_value("Company", fx.COMPANY, "default_fg_warehouse"), warehouse_name("Producto Terminado", fx.COMPANY)
		)

	def test_helper_is_idempotent(self):
		before = self._count()
		for dry_run in (True, False):
			result = warehouses.ensure_operational_warehouses(fx.COMPANY, dry_run=dry_run)
			self.assertEqual({a[1] for a in result["actions"]}, {"ok"}, result)
			self.assertEqual(len(result["actions"]), len(warehouses.OPERATIONAL_WAREHOUSES))
		self.assertEqual(self._count(), before)

	def test_conflicting_warehouse_stops_before_writing(self):
		before = self._count()
		spec = (*warehouses.OPERATIONAL_WAREHOUSES, f"FG284 Nueva {frappe.generate_hash(length=4)}", "Todos los almacenes")
		with patch.object(warehouses, "OPERATIONAL_WAREHOUSES", spec):  # the root is a group: conflict
			with self.assertRaises(warehouses.WarehouseMasterConflict):
				warehouses.ensure_operational_warehouses(fx.COMPANY)
		self.assertEqual(self._count(), before)  # the new one before the conflict was not created either

	def test_helper_requires_system_manager(self):
		with fx.as_user("Guest"):
			with self.assertRaises(frappe.PermissionError):
				warehouses.ensure_operational_warehouses(fx.COMPANY, dry_run=True)
