# -*- coding: utf-8 -*-
"""Fase 28.4A.1 -- master data of the first real manufactured product,
01216 GALON HIPOCLORITO 13% (production_masters.ensure_hipoclorito_13_galon).

Fase 28.4A.3 -- multi-warehouse architecture: 01216 is delivered to
Líquidos - FG (its Item Default, resolved by manufacturing.
resolve_fg_warehouse() before the Company fallback), the chemicals come
from Materia Prima - FG and the container + label from Envases - FG;
BOM-01216-002 replaced BOM-01216-001 (natively cancelled, kept as history).

These tests read the site's REAL master data (they do not build their own
fixtures): the product policy, the four Gram raw materials, the packaging,
the Item Defaults, BOM-01216-*, the 28.1 route, the native Work Order
required_items at x1 and x5, the 2-decimal precision (0.36 Gram of
bicromato never becomes 0) and that nothing touched stock. They skip on a
site where the master has not been configured yet.

The only documents a test writes are DRAFT Work Orders (never submitted,
so no projection, stock or ledger effect) and a Reporte de Faltante for the
routing tests, all deleted by the same test."""

import copy
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, flt

from fabergray_erp import production_masters as masters
from fabergray_erp import production_service
from fabergray_erp.manufacturing import get_manufacturing_route, resolve_component_warehouses, resolve_fg_warehouse

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

COMPANY = "fabrigraysas"
SPEC = masters.HIPOCLORITO_13_GALON
PRODUCT = "01216"
RAW = {"MP-HIPO-SODIO": 1672, "MP-AGUA": 2072.19, "MP-BICROMATO": 0.36, "MP-SODA-LIQ": 5.45}
PACKAGING = {"01068": 1, "ACCESS-235684": 1}
RAW_WH = "Materia Prima - FG"
PACK_WH = "Envases - FG"
FG_WH = "Líquidos - FG"
COMPANY_FG_WH = "Producto Terminado - FG"  # Company.default_fg_warehouse, fallback only
LEGACY_BOM = "BOM-01216-001"
BOM = "BOM-01216-002"
X5 = {
	"MP-HIPO-SODIO": 8360,
	"MP-AGUA": 10360.95,
	"MP-BICROMATO": 1.80,
	"MP-SODA-LIQ": 27.25,
	"01068": 5,
	"ACCESS-235684": 5,
}
ALL_ITEMS = [*RAW, *PACKAGING, PRODUCT]


def _movement_counts():
	return {
		"sle": frappe.db.count("Stock Ledger Entry", {"item_code": ["in", ALL_ITEMS]}),
		"sle_total": frappe.db.count("Stock Ledger Entry"),
		"gl_total": frappe.db.count("GL Entry"),
		"stock_entries": frappe.db.count("Stock Entry"),
		"manufacture": frappe.db.count("Stock Entry", {"purpose": "Manufacture"}),
		"bin_qty": frappe.db.sql(
			"SELECT COALESCE(SUM(actual_qty), 0), COUNT(*) FROM `tabBin` WHERE item_code IN %s", (tuple(ALL_ITEMS),)
		)[0],
		"work_orders": frappe.db.count("Work Order"),
	}


class TestHipocloritoManufacturingMaster(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.bom_name = frappe.db.get_value("Item", PRODUCT, "default_bom")
		if not cls.bom_name:
			raise unittest.SkipTest("01216 master not configured on this site")
		cls.bom = frappe.get_doc("BOM", cls.bom_name)

	def _work_order(self, qty):
		wo = frappe.new_doc("Work Order")
		wo.update(
			{
				"production_item": PRODUCT,
				"bom_no": self.bom_name,
				"company": COMPANY,
				"qty": qty,
				"fg_warehouse": resolve_fg_warehouse(PRODUCT, COMPANY)[0],
				"skip_transfer": 1,
				"use_multi_level_bom": 0,
			}
		)
		wo.set_required_items()
		return wo

	def _required(self, wo):
		return {
			r.item_code: (flt(r.required_qty, r.precision("required_qty")), r.stock_uom, r.source_warehouse)
			for r in wo.required_items
		}

	# -- Items ------------------------------------------------------------

	def test_product_is_manufactured_and_keeps_its_identity(self):
		item = frappe.db.get_value(
			"Item",
			PRODUCT,
			["item_name", "stock_uom", "is_stock_item", "disabled", "default_material_request_type", "default_bom"],
			as_dict=True,
		)
		self.assertEqual(
			dict(item),
			{
				"item_name": "GALON HIPOCLORITO 13%",
				"stock_uom": "Unidad",
				"is_stock_item": 1,
				"disabled": 0,
				"default_material_request_type": "Manufacture",
				"default_bom": self.bom_name,
			},
		)
		self.assertEqual(
			frappe.get_all("Item Price", filters={"item_code": PRODUCT}, pluck="price_list_rate"), [13000.0]
		)
		self.assertEqual(self._defaults(PRODUCT), [(COMPANY, FG_WH)])

	def test_other_hipoclorito_gallons_untouched(self):
		for code in ("01217", "01218", "01219"):
			item = frappe.db.get_value("Item", code, ["default_material_request_type", "default_bom"], as_dict=True)
			self.assertEqual((item.default_material_request_type, item.default_bom), ("Purchase", None), code)
			self.assertEqual(self._defaults(code), [], code)
			self.assertFalse(frappe.db.exists("BOM", {"item": code}), code)

	def _defaults(self, code):
		return [
			(d.company, d.default_warehouse)
			for d in frappe.get_all("Item Default", filters={"parent": code, "parenttype": "Item"}, fields=["company", "default_warehouse"])
		]

	def test_four_raw_materials_in_gram(self):
		names = {r["item_code"]: r["item_name"] for r in SPEC["raw_materials"]}
		self.assertEqual(
			names,
			{
				"MP-HIPO-SODIO": "HIPOCLORITO DE SODIO PURO",
				"MP-AGUA": "AGUA PRODUCCION",
				"MP-BICROMATO": "BICROMATO",
				"MP-SODA-LIQ": "SODA LIQUIDA",
			},
		)
		for code, name in names.items():
			item = frappe.db.get_value(
				"Item",
				code,
				["item_name", "item_group", "stock_uom", "is_stock_item", "is_sales_item", "disabled", "valuation_rate"],
				as_dict=True,
			)
			self.assertEqual(
				dict(item),
				{
					"item_name": name,
					"item_group": "Materias Prima",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"is_sales_item": 0,
					"disabled": 0,
					"valuation_rate": 0.0,  # no invented cost
				},
				code,
			)
			self.assertEqual(self._defaults(code), [(COMPANY, RAW_WH)], code)
			self.assertEqual(frappe.db.count("Item Price", {"item_code": code}), 0, code)
			self.assertEqual(frappe.db.count("Item", {"item_name": name}), 1, code)  # no duplicate master
			self.assertNotIn("%", name)  # no invented concentration
		# No litre <-> gram (density) conversion on the chemicals.
		self.assertFalse(
			frappe.db.exists("UOM Conversion Detail", {"parent": ["in", list(RAW)], "uom": ["!=", "Gram"]})
		)

	def test_packaging(self):
		for code in PACKAGING:
			self.assertEqual(frappe.db.get_value("Item", code, "stock_uom"), "Unidad", code)
			self.assertEqual(self._defaults(code), [(COMPANY, PACK_WH)], code)
		for rejected in ("01069", "EMP-GAL-3750L", "ACCESS-5224"):
			self.assertNotIn(rejected, [r.item_code for r in self.bom.items])
			self.assertEqual(self._defaults(rejected), [], rejected)

	# -- BOM --------------------------------------------------------------

	def test_bom_submitted_active_default_and_unique(self):
		b = self.bom
		self.assertEqual(
			(b.item, b.company, b.docstatus, b.is_active, b.is_default, flt(b.quantity), b.uom, b.with_operations),
			(PRODUCT, COMPANY, 1, 1, 1, 1, "Unidad", 0),
		)
		self.assertEqual(frappe.db.count("BOM", {"item": PRODUCT, "docstatus": 1, "is_active": 1}), 1)

	def test_bom_has_exactly_the_six_approved_components(self):
		rows = {r.item_code: (flt(r.qty), r.uom, flt(r.stock_qty), r.stock_uom, flt(r.conversion_factor), r.source_warehouse) for r in self.bom.items}
		expected = {code: (qty, "Gram", qty, "Gram", 1, RAW_WH) for code, qty in RAW.items()}
		expected.update({code: (qty, "Unidad", qty, "Unidad", 1, PACK_WH) for code, qty in PACKAGING.items()})
		self.assertEqual(rows, expected)
		self.assertEqual(len(self.bom.items), 6)
		# Persisted exactly (not only in memory).
		stored = dict(frappe.db.sql("SELECT item_code, stock_qty FROM `tabBOM Item` WHERE parent = %s", self.bom_name))
		self.assertEqual({k: flt(v) for k, v in stored.items()}, {**RAW, **PACKAGING})
		self.assertEqual(round(sum(RAW.values()), 2), 3750.00)  # approved liquid total

	def test_route_is_manufacture(self):
		self.assertEqual(
			get_manufacturing_route(PRODUCT, COMPANY),
			{"route": "Manufacture", "bom": self.bom_name, "reason": None, "problems": []},
		)

	# -- Native Work Order (memory / draft only) ----------------------------

	def test_work_order_required_items_x1(self):
		expected = {code: (flt(qty), "Gram", RAW_WH) for code, qty in RAW.items()}
		expected.update({code: (flt(qty), "Unidad", PACK_WH) for code, qty in PACKAGING.items()})
		self.assertEqual(self._required(self._work_order(1)), expected)

	def test_work_order_required_items_x5(self):
		wo = self._work_order(5)
		expected = {code: (flt(qty), "Gram" if code in RAW else "Unidad", RAW_WH if code in RAW else PACK_WH) for code, qty in X5.items()}
		self.assertEqual(self._required(wo), expected)
		self.assertTrue(wo.is_new())

	def test_precision_preserves_bicromato(self):
		wo = self._work_order(1)
		row = next(r for r in wo.required_items if r.item_code == "MP-BICROMATO")
		self.assertEqual(cint(frappe.db.get_single_value("System Settings", "float_precision")), 2)
		self.assertEqual(flt(0.36, row.precision("required_qty")), 0.36)
		bom_row = next(r for r in self.bom.items if r.item_code == "MP-BICROMATO")
		self.assertEqual(flt(bom_row.qty, bom_row.precision("qty")), 0.36)
		se_row = frappe.new_doc("Stock Entry").append("items", {})
		for field in ("qty", "transfer_qty"):
			for value in (0.36, 1.8, 5.45, 2072.19, 10360.95):
				self.assertEqual(flt(value, se_row.precision(field)), value, (field, value))
		# Why Gram: the same mass in Kg would be lost at this precision.
		self.assertEqual(flt(0.00036, row.precision("required_qty")), 0)

	def test_persisted_draft_work_order_keeps_exact_quantities(self):
		before = _movement_counts()
		wo = self._work_order(5)
		wo.insert()  # draft only: never submitted
		try:
			stored = {
				r.item_code: flt(r.required_qty)
				for r in frappe.get_all("Work Order Item", filters={"parent": wo.name}, fields=["item_code", "required_qty"])
			}
			self.assertEqual(stored, {k: flt(v) for k, v in X5.items()})
			self.assertEqual(frappe.db.get_value("Work Order", wo.name, "docstatus"), 0)
		finally:
			frappe.delete_doc("Work Order", wo.name, force=True)
		after = _movement_counts()
		self.assertEqual(after, before)

	# -- No inventory / idempotency ---------------------------------------

	def test_no_stock_and_no_movements(self):
		counts = _movement_counts()
		self.assertEqual(counts["sle"], 0)
		self.assertEqual(flt(counts["bin_qty"][0]), 0)
		self.assertEqual(counts["manufacture"], 0)

	def test_helper_is_idempotent(self):
		before = (_movement_counts(), frappe.db.count("Item"), frappe.db.count("BOM"), frappe.db.count("Item Default"))
		for dry_run in (True, False):
			result = masters.ensure_hipoclorito_13_galon(COMPANY, dry_run=dry_run)
			self.assertEqual({a[2] for a in result["actions"]}, {"ok"}, result)
			self.assertEqual(result["bom"], self.bom_name)
			self.assertNotIn("legacy_bom", [a[0] for a in result["actions"]])  # already retired
		after = (_movement_counts(), frappe.db.count("Item"), frappe.db.count("BOM"), frappe.db.count("Item Default"))
		self.assertEqual(after, before)
		self.assertFalse(frappe.db.exists("BOM", "BOM-01216-003"))

	def test_conflicting_master_is_never_overwritten(self):
		spec = copy.deepcopy(SPEC)
		spec["raw_materials"][1]["item_name"] = "AGUA DESTILADA"  # differs from the existing MP-AGUA
		with patch.object(masters, "HIPOCLORITO_13_GALON", spec):
			with self.assertRaises(masters.ProductionMasterConflict):
				masters.ensure_hipoclorito_13_galon(COMPANY)
		self.assertEqual(frappe.db.get_value("Item", "MP-AGUA", "item_name"), "AGUA PRODUCCION")
		spec = copy.deepcopy(SPEC)
		spec["packaging_warehouse"] = RAW_WH  # 01068 already defaults to Envases
		with patch.object(masters, "HIPOCLORITO_13_GALON", spec):
			with self.assertRaises(masters.ProductionMasterConflict):
				masters.ensure_hipoclorito_13_galon(COMPANY)
		self.assertEqual(self._defaults("01068"), [(COMPANY, PACK_WH)])

	def test_helper_requires_system_manager(self):
		from fabergray_erp.tests import fixtures as fx

		with fx.as_user("Guest"):
			with self.assertRaises(frappe.PermissionError):
				masters.ensure_hipoclorito_13_galon(COMPANY, dry_run=True)

	# -- Fase 28.4A.3: multi-warehouse ----------------------------------------

	def test_bom_002_replaced_the_legacy_bom(self):
		self.assertEqual(self.bom_name, BOM)
		legacy = frappe.db.get_value("BOM", LEGACY_BOM, ["docstatus", "is_active", "is_default", "item"], as_dict=True)
		self.assertEqual(dict(legacy), {"docstatus": 2, "is_active": 0, "is_default": 0, "item": PRODUCT})
		# The legacy components are history, untouched (old warehouses).
		self.assertEqual(
			set(frappe.get_all("BOM Item", filters={"parent": LEGACY_BOM}, pluck="source_warehouse")),
			{"Materias Primas - FG", "Material de Empaque - FG"},
		)
		self.assertEqual(frappe.get_all("BOM", filters={"item": PRODUCT, "docstatus": ["<", 2]}, pluck="name"), [BOM])

	def test_new_operational_warehouses_are_used(self):
		for wh in (FG_WH, RAW_WH, PACK_WH):
			row = frappe.db.get_value("Warehouse", wh, ["company", "is_group", "disabled", "parent_warehouse"], as_dict=True)
			self.assertEqual(dict(row), {"company": COMPANY, "is_group": 0, "disabled": 0, "parent_warehouse": "Todos los almacenes - FG"})

	def test_fg_warehouse_resolves_liquidos_before_the_company_fallback(self):
		self.assertEqual(frappe.db.get_value("Company", COMPANY, "default_fg_warehouse"), COMPANY_FG_WH)
		self.assertEqual(resolve_fg_warehouse(PRODUCT, COMPANY), (FG_WH, []))

	def test_components_resolve_to_materia_prima_and_envases(self):
		rows, problems = resolve_component_warehouses(self.bom_name, COMPANY)
		self.assertEqual(problems, [])
		self.assertEqual(
			{r["item_code"]: r["source_warehouse"] for r in rows},
			{**{code: RAW_WH for code in RAW}, **{code: PACK_WH for code in PACKAGING}},
		)

	def test_draft_work_order_targets_liquidos(self):
		wo = self._work_order(1)
		self.assertEqual(wo.fg_warehouse, FG_WH)
		self.assertEqual({r.source_warehouse for r in wo.required_items}, {RAW_WH, PACK_WH})

	def _shortage(self, warehouse):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": PRODUCT,
				"warehouse": warehouse,
				"qty_solicitada": 3,
				"qty_disponible": 0,
				"detected_by": "Bodega",
				"shortage_reason": "Stock insuficiente",
			}
		)
		doc.insert()
		self.addCleanup(frappe.delete_doc, "Reporte de Faltante", doc.name, force=True)
		return doc

	def _route_with_draft_work_orders(self, report_name):
		"""route_shortage() for the REAL 01216, with the Work Order kept as a
		draft (never submitted: no projection on the real Bins)."""
		created = []

		def draft_work_order(item_code, bom_no, company, fg_warehouse, qty):
			wo = frappe.new_doc("Work Order")
			wo.update(
				{
					"production_item": item_code,
					"bom_no": bom_no,
					"company": company,
					"qty": qty,
					"fg_warehouse": fg_warehouse,
					"skip_transfer": 1,
					"use_multi_level_bom": 0,
				}
			)
			wo.insert()
			created.append(wo.name)
			return wo

		before = _movement_counts()
		with patch.object(production_service, "_create_work_order", draft_work_order):
			result = production_service.route_shortage(report_name, COMPANY)
		for name in created:
			self.addCleanup(frappe.delete_doc, "Work Order", name, force=True)
		after = _movement_counts()
		self.assertEqual({k: v for k, v in after.items() if k != "work_orders"}, {k: v for k, v in before.items() if k != "work_orders"})
		return result, created

	def test_route_shortage_in_liquidos_manufactures_into_liquidos(self):
		report = self._shortage(FG_WH)
		result, created = self._route_with_draft_work_orders(report.name)
		self.assertEqual((result["route"], result["problems"]), ("Manufacture", []))
		self.assertEqual(len(created), 1)
		wo = frappe.get_doc("Work Order", created[0])
		self.assertEqual((wo.production_item, wo.bom_no, wo.fg_warehouse, flt(wo.qty)), (PRODUCT, BOM, FG_WH, 3))
		self.assertEqual(
			{r.item_code: r.source_warehouse for r in wo.required_items},
			{**{code: RAW_WH for code in RAW}, **{code: PACK_WH for code in PACKAGING}},
		)

	def test_route_shortage_in_another_warehouse_is_blocked(self):
		report = self._shortage(COMPANY_FG_WH)  # the Company fallback, not 01216's own warehouse
		result, created = self._route_with_draft_work_orders(report.name)
		self.assertEqual(result["route"], "Blocked")
		self.assertEqual(created, [])
		self.assertIn(FG_WH, result["reason"])
		self.assertIn(COMPANY_FG_WH, result["reason"])
