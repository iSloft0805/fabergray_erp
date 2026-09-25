# -*- coding: utf-8 -*-
"""Fase 28.1 -- Producción, fundación.

1. fabergray_erp.manufacturing.get_manufacturing_route() /
   validate_bom_for_production(): the ONE make/buy rule (native
   Item.default_material_request_type + a usable BOM), shared by the
   Fulfillment analyzer.
2. Retiring an unusable pilot (BOM + Work Order) with native operations,
   reproduced on this suite's OWN fixtures (the real pilot documents are
   never touched): a Work Order with no Stock Entry cancels without any
   stock/GL effect; the BOM can then be cancelled; history is kept.
   Also proves the future architecture: Work Order with skip_transfer=1
   (no WIP), per-component source warehouses from the BOM, and a
   Manufacture Stock Entry built by ERPNext IN MEMORY only (never saved).
3. Reproduction of the partial Pick List case (pedido 10, Bodega alista 6,
   faltante 4): what the system does today and the native way the
   remaining 4 come back to Bodega without duplicating the first 6."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

from fabergray_erp import manufacturing
from fabergray_erp.api import bodega, facturacion
from fabergray_erp.fulfillment import analyzer
from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_full_demand
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

OTHER_COMPANY = "_Test Company"


def _counts():
	return {
		dt: frappe.db.count(dt)
		for dt in ("BOM", "Work Order", "Stock Entry", "Material Request", "Stock Ledger Entry", "GL Entry", "Pick List")
	}


class TestManufacturingRoute(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.raw = cls.world.item("FG281-RAW", default_material_request_type="Purchase")
		cls.pack = cls.world.item("FG281-PACK", default_material_request_type="Purchase")

	def _item(self, code, policy="Manufacture"):
		return self.world.item(f"FG281-{code}-{frappe.generate_hash(length=5)}", default_material_request_type=policy)

	def _bom(self, item_code, components=None, submit=True, company=fx.COMPANY):
		currency = frappe.db.get_value("Company", company, "default_currency")
		doc = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item_code,
				"quantity": 5,
				"company": company,
				"currency": currency,
				"conversion_rate": 1,
				"items": components
				or [
					{"item_code": self.raw.name, "qty": 2, "uom": fx.UOM, "rate": 10},
					{"item_code": self.pack.name, "qty": 5, "uom": fx.UOM, "rate": 1},
				],
			}
		)
		doc.insert()
		if submit:
			doc.submit()
		self.world.track_existing("BOM", doc.name)
		return doc

	def _analyzer(self, item_code, company=fx.COMPANY):
		return analyzer._procurement_route_for_item(item_code, company)

	def test_purchase(self):
		route = manufacturing.get_manufacturing_route(self.raw.name)
		self.assertEqual((route["route"], route["bom"], route["reason"]), ("Purchase", None, None))

	def test_purchase_is_never_promoted_even_with_a_valid_bom(self):
		item = self._item("BUY", policy="Purchase")
		self._bom(item.name)
		self.assertEqual(manufacturing.get_manufacturing_route(item.name)["route"], "Purchase")

	def test_manufacture_with_a_valid_bom(self):
		item = self._item("OK")
		bom = self._bom(item.name)
		route = manufacturing.get_manufacturing_route(item.name)
		self.assertEqual(route, {"route": "Manufacture", "bom": bom.name, "reason": None, "problems": []})
		self.assertEqual(manufacturing.validate_bom_for_production(bom.name, item.name), [])

	def test_manufacture_without_bom_is_blocked(self):
		item = self._item("NOBOM")
		route = manufacturing.get_manufacturing_route(item.name)
		self.assertEqual((route["route"], route["reason"]), ("Blocked", "Missing BOM"))

	def test_cancelled_or_inactive_bom_is_not_usable(self):
		item = self._item("CANC")
		bom = self._bom(item.name)
		bom.cancel()
		self.assertEqual(manufacturing.get_manufacturing_route(item.name)["reason"], "Missing BOM")
		problems = manufacturing.validate_bom_for_production(bom.name, item.name)
		self.assertIn("la BOM no está enviada (submitted)", problems)
		self.assertIn("la BOM está inactiva", problems)

		item2 = self._item("INACT")
		bom2 = self._bom(item2.name)
		bom2.is_active = 0
		bom2.save()  # allow_on_submit -- the native way to deactivate
		self.assertEqual(manufacturing.get_manufacturing_route(item2.name)["reason"], "Missing BOM")
		self.assertIn("la BOM está inactiva", manufacturing.validate_bom_for_production(bom2.name, item2.name))
		draft = self._bom(self._item("DRAFT").name, submit=False)
		self.assertIn("la BOM no está enviada (submitted)", manufacturing.validate_bom_for_production(draft.name, draft.item))

	def test_bom_of_another_product(self):
		a, b = self._item("A"), self._item("B")
		bom_a = self._bom(a.name)
		problems = manufacturing.validate_bom_for_production(bom_a.name, b.name)
		self.assertTrue(any("produce" in p for p in problems), problems)

	def test_company_isolation(self):
		item = self._item("CO")
		bom = self._bom(item.name)
		problems = manufacturing.validate_bom_for_production(bom.name, item.name, company=OTHER_COMPANY)
		self.assertTrue(any("otra empresa" in p for p in problems), problems)
		route = manufacturing.get_manufacturing_route(item.name, company=OTHER_COMPANY)
		self.assertEqual(route["route"], "Blocked")
		self.assertEqual(route["bom"], bom.name)
		self.assertEqual(self._analyzer(item.name, OTHER_COMPANY)[0], "Blocked")
		self.assertEqual(manufacturing.get_manufacturing_route(item.name)["route"], "Manufacture")

	def test_missing_disabled_and_unsupported_items(self):
		self.assertEqual(manufacturing.get_manufacturing_route("NO-EXISTE-281")["route"], "Blocked")
		self.assertIn("Item inexistente", manufacturing.get_manufacturing_route("NO-EXISTE-281")["reason"])
		self.assertIn("Item inexistente", manufacturing.get_manufacturing_route(None)["reason"])
		other = self._item("MT", policy="Material Transfer")
		self.assertEqual(
			manufacturing.get_manufacturing_route(other.name)["reason"], "Unsupported procurement policy: Material Transfer"
		)
		disabled = self._item("DIS")
		self._bom(disabled.name)
		frappe.db.set_value("Item", disabled.name, "disabled", 1)
		frappe.clear_document_cache("Item", disabled.name)
		self.assertEqual(manufacturing.get_manufacturing_route(disabled.name)["reason"], "Item deshabilitado")

	def test_component_rules(self):
		service = self.world.item(f"FG281-SERV-{frappe.generate_hash(length=5)}")
		frappe.db.set_value("Item", service.name, "is_stock_item", 0)
		item = self._item("COMP")
		bom = self._bom(item.name, components=[{"item_code": service.name, "qty": 1, "uom": fx.UOM, "rate": 1}])
		self.assertTrue(
			any("no es un Item de inventario" in p for p in manufacturing.validate_bom_for_production(bom.name, item.name))
		)
		# A component disabled after the BOM was approved makes it unusable.
		item2 = self._item("COMP2")
		comp = self._item("COMPX", policy="Purchase")
		bom2 = self._bom(item2.name, components=[{"item_code": comp.name, "qty": 1, "uom": fx.UOM, "rate": 1}])
		self.assertEqual(manufacturing.get_manufacturing_route(item2.name)["route"], "Manufacture")
		frappe.db.set_value("Item", comp.name, "disabled", 1)
		route = manufacturing.get_manufacturing_route(item2.name)
		self.assertEqual(route["route"], "Blocked")
		self.assertEqual(route["bom"], bom2.name)
		self.assertTrue(any("deshabilitado" in p for p in route["problems"]))

	def test_analyzer_uses_the_same_rule(self):
		cases = [self.raw.name, self._item("SAME-NOBOM").name, "NO-EXISTE-281"]
		ok = self._item("SAME-OK")
		self._bom(ok.name)
		cases.append(ok.name)
		for code in cases:
			route = manufacturing.get_manufacturing_route(code)
			self.assertEqual(self._analyzer(code), (route["route"], route["bom"], route["reason"]), code)
		with open(analyzer.__file__, encoding="utf-8") as fh:
			source = fh.read()
		self.assertNotIn("get_default_bom", source)  # no second copy of the rule
		self.assertIn("get_manufacturing_route(item_code, company=company)", source)

	def test_route_is_read_only(self):
		ok = self._item("RO")
		self._bom(ok.name)
		before = _counts()
		modified = frappe.db.get_value("Item", ok.name, "modified")
		for code in (ok.name, self.raw.name, "NO-EXISTE-281"):
			manufacturing.get_manufacturing_route(code)
		self.assertEqual(_counts(), before)
		self.assertEqual(frappe.db.get_value("Item", ok.name, "modified"), modified)
		self.assertEqual(frappe.db.get_value("Item", ok.name, "default_material_request_type"), "Manufacture")


class TestPilotRetirementAndWorkOrderArchitecture(IntegrationTestCase):
	"""On this suite's OWN fixtures -- the real BOM-FG-DES-1G-001 /
	MFG-WO-2026-00001 are never touched."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.wh_raw = cls.world.warehouse("FG281 Materias")
		cls.wh_pack = cls.world.warehouse("FG281 Empaque")
		cls.wh_fg = cls.world.warehouse("FG281 Terminado")
		cls.raw = cls.world.item("FG281W-RAW", default_material_request_type="Purchase")
		cls.pack = cls.world.item("FG281W-PACK", default_material_request_type="Purchase")
		cls.fg = cls.world.item("FG281W-FG", default_material_request_type="Manufacture")
		currency = frappe.db.get_value("Company", fx.COMPANY, "default_currency")
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cls.fg.name,
				"quantity": 5,
				"company": fx.COMPANY,
				"currency": currency,
				"conversion_rate": 1,
				"items": [
					# multi-warehouse: chemicals and packaging come from different warehouses
					{"item_code": cls.raw.name, "qty": 2, "uom": fx.UOM, "rate": 10, "source_warehouse": cls.wh_raw.name},
					{"item_code": cls.pack.name, "qty": 5, "uom": fx.UOM, "rate": 1, "source_warehouse": cls.wh_pack.name},
				],
			}
		)
		bom.insert()
		bom.submit()
		cls.bom = cls.world._track(bom)

	def _bin(self, item_code, warehouse):
		return frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			["actual_qty", "planned_qty", "reserved_qty_for_production", "projected_qty"],
			as_dict=True,
		) or frappe._dict(actual_qty=0, planned_qty=0, reserved_qty_for_production=0, projected_qty=0)

	def _work_order(self, qty=5):
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": self.fg.name,
				"bom_no": self.bom.name,
				"qty": qty,
				"company": fx.COMPANY,
				"skip_transfer": 1,  # no Material Transfer to WIP
				"fg_warehouse": self.wh_fg.name,
				"planned_start_date": nowdate(),
			}
		)
		wo.insert()
		self.world._track(wo)
		return wo

	def test_skip_transfer_work_order_uses_per_component_warehouses(self):
		wo = self._work_order(qty=10)
		self.assertEqual(wo.skip_transfer, 1)
		self.assertFalse(wo.wip_warehouse)  # not required with skip_transfer
		required = {row.item_code: row for row in wo.required_items}
		self.assertEqual(flt(required[self.raw.name].required_qty), 4)  # 2 per 5 -> 10 = 4
		self.assertEqual(flt(required[self.pack.name].required_qty), 10)
		self.assertEqual(required[self.raw.name].source_warehouse, self.wh_raw.name)
		self.assertEqual(required[self.pack.name].source_warehouse, self.wh_pack.name)
		wo.submit()
		# skip_transfer: ERPNext treats materials as already "at hand", so the
		# Work Order is In Process right after submit (nothing produced yet).
		self.assertEqual((wo.status, flt(wo.produced_qty)), ("In Process", 0))

		# The future REGISTRAR PRODUCCIÓN step: ERPNext builds a Manufacture
		# Stock Entry straight from the Work Order (partial qty allowed),
		# consuming each component from ITS warehouse -- built in memory,
		# never saved here.
		before = _counts()
		entry = make_stock_entry(wo.name, "Manufacture", 6)
		self.assertEqual(_counts(), before)
		self.assertEqual(entry["purpose"], "Manufacture")
		self.assertEqual(entry["work_order"], wo.name)
		self.assertEqual(flt(entry["fg_completed_qty"]), 6)
		rows = {row["item_code"]: row for row in entry["items"]}
		self.assertEqual(rows[self.raw.name]["s_warehouse"], self.wh_raw.name)
		self.assertEqual(rows[self.pack.name]["s_warehouse"], self.wh_pack.name)
		self.assertEqual(rows[self.fg.name]["t_warehouse"], self.wh_fg.name)
		self.assertAlmostEqual(flt(rows[self.raw.name]["qty"]), 2.4)
		self.assertAlmostEqual(flt(rows[self.pack.name]["qty"]), 6)
		wo.cancel()

	def test_unstarted_work_order_cancels_without_stock_effects_then_bom_can_be_retired(self):
		fg_bin_before = self._bin(self.fg.name, self.wh_fg.name)
		raw_bin_before = self._bin(self.raw.name, self.wh_raw.name)
		sle_before = frappe.db.count("Stock Ledger Entry", {"item_code": ["in", [self.fg.name, self.raw.name, self.pack.name]]})
		gl_before = frappe.db.count("GL Entry")

		wo = self._work_order(qty=5)
		wo.submit()
		self.assertEqual(flt(self._bin(self.fg.name, self.wh_fg.name).planned_qty), flt(fg_bin_before.planned_qty) + 5)
		self.assertEqual(
			flt(self._bin(self.raw.name, self.wh_raw.name).reserved_qty_for_production),
			flt(raw_bin_before.reserved_qty_for_production) + 2,
		)
		self.assertEqual(frappe.db.count("Stock Entry", {"work_order": wo.name}), 0)

		# BOM cannot be cancelled while a submitted Work Order links it (in a
		# real request the error rolls everything back; here a savepoint).
		frappe.db.savepoint("fg281_bom_cancel")
		with self.assertRaises(frappe.LinkExistsError):
			frappe.get_doc("BOM", self.bom.name).cancel()
		frappe.db.rollback(save_point="fg281_bom_cancel")
		self.assertEqual(frappe.db.get_value("BOM", self.bom.name, "docstatus"), 1)

		wo.reload()
		wo.cancel()
		self.assertEqual((wo.docstatus, wo.status), (2, "Cancelled"))
		self.assertTrue(frappe.db.exists("Work Order", wo.name))  # history kept
		fg_bin = self._bin(self.fg.name, self.wh_fg.name)
		raw_bin = self._bin(self.raw.name, self.wh_raw.name)
		# Projections restored, real stock never moved, no ledger/GL written.
		self.assertEqual(flt(fg_bin.planned_qty), flt(fg_bin_before.planned_qty))
		self.assertEqual(flt(raw_bin.reserved_qty_for_production), flt(raw_bin_before.reserved_qty_for_production))
		self.assertEqual(flt(fg_bin.actual_qty), flt(fg_bin_before.actual_qty))
		self.assertEqual(flt(raw_bin.actual_qty), flt(raw_bin_before.actual_qty))
		self.assertEqual(
			frappe.db.count("Stock Ledger Entry", {"item_code": ["in", [self.fg.name, self.raw.name, self.pack.name]]}),
			sle_before,
		)
		self.assertEqual(frappe.db.count("GL Entry"), gl_before)

		# Now the BOM retires natively (cancel -> inactive, not default,
		# Item.default_bom cleared), kept as history.
		bom = frappe.get_doc("BOM", self.bom.name)
		bom.cancel()
		bom.reload()
		self.assertEqual((bom.docstatus, bom.is_active, bom.is_default), (2, 0, 0))
		self.assertFalse(frappe.db.get_value("Item", self.fg.name, "default_bom"))
		self.assertEqual(manufacturing.get_manufacturing_route(self.fg.name)["reason"], "Missing BOM")


class TestPartialPickListRemainder(IntegrationTestCase):
	"""Pedido 10 -> Bodega alista 6 -> faltante 4 -> ¿cómo vuelven las 4?

	Two real shapes, both reproduced:
	A. theoretical stock covers the order (one Pick List row of 10) but
	   Bodega physically finds 6: Bodega may finish with 6 -> the 4 are
	   orphaned today (nothing re-offers them);
	B. theoretical stock is short (6): the full-demand Pick List has two
	   rows (6 with a location + a 4 top-up row); a 0-picked row blocks
	   finishing, so Bodega must wait and completes the same Pick List."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.wh = cls.world.warehouse("FG281 Remanente")
		cls.item_a = cls.world.item("FG281-REMANENTE-A", default_material_request_type="Purchase")
		cls.item_b = cls.world.item("FG281-REMANENTE-B", default_material_request_type="Purchase")
		cls.world.stock_up_real(cls.item_a.name, cls.wh.name, 10)
		cls.world.stock_up_real(cls.item_b.name, cls.wh.name, 6)
		cls.customer = cls.world.customer("FG281 Remanente Cliente")
		cls.bodega_user = cls.world.user("fg281-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	def _submit_order(self, item_code, qty):
		delivery_date = add_days(nowdate(), 7)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": self.wh.name,
				"items": [{"item_code": item_code, "warehouse": self.wh.name, "qty": qty, "rate": 1000, "delivery_date": delivery_date}],
			}
		)
		so.insert()
		self.world.track_existing("Sales Order", so.name)
		so.submit()  # real hook -> full-demand Pick List
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		return so

	def _pick_lists(self, so_name):
		return frappe.get_all(
			"Pick List Item",
			filters={"sales_order": so_name, "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
			order_by="parent asc",
		)

	def _rows(self, pl_name):
		with fx.as_user(self.bodega_user):
			return bodega.get_pick_list(pl_name)["rows"]

	def _bucket(self, pl_name):
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		for key, rows in queue.items():
			if any(row.get("name") == pl_name for row in rows):
				return key
		return None

	def _line_qty(self, pl_name, so):
		lines, _totals = facturacion._build_invoice_lines_and_totals(frappe.get_doc("Pick List", pl_name), so)
		return sum(flt(line["qty"]) for line in lines)

	def test_a_finished_partial_pick_orphans_the_remainder_until_a_new_pick_list(self):
		so = self._submit_order(self.item_a.name, 10)
		[pl1] = self._pick_lists(so.name)
		rows = self._rows(pl1)
		self.assertEqual(len(rows), 1)
		row_name = rows[0]["row_name"]
		self.assertEqual(flt(frappe.get_doc("Pick List", pl1).locations[0].stock_qty), 10)

		# Bodega physically finds 6, reports 4 and finishes (a partial > 0 may finish).
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl1)
			bodega.set_picked_qty(pl1, row_name, 6)
			report = bodega.report_shortage(pl1, row_name, 6, "Stock físico no encontrado")
			self.world.track_existing("Reporte de Faltante", report["name"])
			bodega.finish_picking(pl1)

		pl = frappe.get_doc("Pick List", pl1)
		row = pl.locations[0]
		self.assertEqual((pl.docstatus, pl.status), (1, "Open"))
		self.assertEqual((flt(row.stock_qty), flt(row.picked_qty), flt(row.delivered_qty)), (10, 6, 0))
		so.reload()
		so_item = so.items[0]
		self.assertEqual((flt(so_item.picked_qty), flt(so_item.delivered_qty)), (6, 0))
		self.assertEqual((flt(so.per_picked), flt(so.per_delivered)), (60, 0))
		self.assertEqual(so.status, "To Deliver and Bill")
		report = frappe.get_doc("Reporte de Faltante", {"pick_list_item": row.name})
		self.assertEqual((flt(report.qty_faltante), report.status), (4, "Abierto"))

		# Bodega's queue shows the submitted Pick List as "listos" although
		# its shortage is still open: the 4 are no longer work in Bodega.
		self.assertEqual(self._bucket(pl1), "listos")
		# Facturación bills what was picked: 6.
		self.assertEqual(self._line_qty(pl1, so), 6)

		# TODAY nothing brings the 4 back, whatever happens to stock.
		self.world.stock_up_real(self.item_a.name, self.wh.name, 14)
		self.assertEqual(self._pick_lists(so.name), [pl1])

		# NATIVE PATH (proven, not wired): the native mapper and this app's
		# full-demand builder both compute qty - picked = 4 -- never 10,
		# never the 6 again -- without touching the Sales Order.
		native = create_pick_list(so.name)
		self.assertEqual(sum(flt(r.qty) for r in native.locations), 4)
		before = frappe.db.get_value("Sales Order", so.name, ["modified", "per_picked", "status"], as_dict=True)
		pl2 = create_pick_list_for_full_demand(so.name)
		self.world.track_existing("Pick List", pl2.name)
		self.assertEqual(sum(flt(r.stock_qty) for r in pl2.locations), 4)
		self.assertEqual({r.sales_order_item for r in pl2.locations}, {so_item.name})
		self.assertIsNone(create_pick_list_for_full_demand(so.name))  # idempotent: nothing left
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, ["modified", "per_picked", "status"], as_dict=True), before)
		self.assertEqual(
			self._bucket(pl2.name),
			"pendientes",
			frappe.get_all("Reporte de Faltante", filters={"sales_order": so.name}, fields=["name", "pick_list", "pick_list_item", "status"]),
		)

		# Bodega picks the 4 on the new Pick List: order complete, Facturación
		# bills 6 + 4 = 10, never the first 6 twice.
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl2.name)
			for r in self._rows(pl2.name):
				bodega.set_picked_qty(pl2.name, r["row_name"], r["qty_solicitada"])
			bodega.finish_picking(pl2.name)
		so.reload()
		self.assertEqual((flt(so.items[0].picked_qty), flt(so.per_picked)), (10, 100))
		self.assertEqual(self._line_qty(pl2.name, so), 4)
		self.assertEqual(self._line_qty(pl1, so) + self._line_qty(pl2.name, so), 10)

	def test_b_theoretical_shortage_blocks_finishing_until_stock_arrives(self):
		so = self._submit_order(self.item_b.name, 10)
		[pl1] = self._pick_lists(so.name)
		rows = sorted(self._rows(pl1), key=lambda r: -flt(r["qty_solicitada"]))
		self.assertEqual([flt(r["qty_solicitada"]) for r in rows], [6, 4])  # location row + top-up row
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl1)
			bodega.set_picked_qty(pl1, rows[0]["row_name"], 6)
			report = bodega.report_shortage(pl1, rows[1]["row_name"], 0, "Stock insuficiente")
			self.world.track_existing("Reporte de Faltante", report["name"])
			with self.assertRaisesRegex(frappe.ValidationError, "quedaron en 0"):
				bodega.finish_picking(pl1)
		self.assertEqual(self._bucket(pl1), "con_faltantes")

		# Goods arrive (production/purchase): Bodega completes the SAME Pick List.
		self.world.stock_up_real(self.item_b.name, self.wh.name, 10)
		with fx.as_user(self.bodega_user):
			bodega.set_picked_qty(pl1, rows[1]["row_name"], 4)
			bodega.finish_picking(pl1)
		so.reload()
		self.assertEqual(flt(so.per_picked), 100)
		self.assertEqual(self._pick_lists(so.name), [pl1])
		self.assertEqual(self._line_qty(pl1, so), 10)
