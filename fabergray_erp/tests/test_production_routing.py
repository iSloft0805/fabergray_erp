# -*- coding: utf-8 -*-
"""Fase 28.2 -- Enrutamiento de faltantes -> Producción, and the remainder
Pick List after a shortage is really resolved.

- api.jefe_bodega.route_shortage() / production_service.route_shortage():
  procurement_route from the ONE make/buy rule, Blocked with the reason,
  native Work Order (skip_transfer, per-component source warehouses),
  consolidation into unallocated capacity without editing a submitted
  Work Order, no double routing, Work Order cancellation releases the
  shortage, access/company/IDOR, routing fields closed to Desk.
- fulfillment.remainder_service.ensure_remaining_pick_list(), wired into
  receive_shortage_purchase(): pedido 10 / alistado 6 / faltante 4 ->
  compra -> Pick List of exactly 4, idempotent, concurrent-safe, never for
  a draft Pick List, only the resolved line.
- Integrity: nothing here manufactures (no Manufacture Stock Entry, no
  stock or ledger movement from routing)."""

import threading
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fabergray_erp import production_service
from fabergray_erp.api import bodega, facturacion
from fabergray_erp.api import jefe_bodega as jefe_api
from fabergray_erp.fulfillment.remainder_service import ensure_remaining_pick_list
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


def _stock_counts():
	return {
		"sle": frappe.db.count("Stock Ledger Entry"),
		"gl": frappe.db.count("GL Entry"),
		"manufacture": frappe.db.count("Stock Entry", {"purpose": "Manufacture"}),
	}


class _Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		tag = frappe.generate_hash(length=4).upper()
		cls.tag = tag
		cls.wh_fg = cls.world.warehouse(f"FG282 Terminado {tag}")
		cls.wh_raw = cls.world.warehouse(f"FG282 Materias {tag}")
		cls.wh_pack = cls.world.warehouse(f"FG282 Empaque {tag}")
		cls.raw = cls.world.item(f"FG282-{tag}-RAW", default_material_request_type="Purchase")
		cls.pack = cls.world.item(f"FG282-{tag}-PACK", default_material_request_type="Purchase", default_warehouse=cls.wh_pack.name)
		cls.jefe = cls.world.user(f"fg282-jefe-{tag.lower()}@example.com", ["Jefe de Bodega"])
		cls.bodega_user = cls.world.user(f"fg282-bodega-{tag.lower()}@example.com", ["Bodega"])
		for wh in (cls.wh_fg, cls.wh_raw, cls.wh_pack):
			cls.world.warehouse_user_permission(cls.bodega_user, wh.name)
		cls.produccion = cls.world.user(f"fg282-prod-{tag.lower()}@example.com", ["Producción"])
		cls.jefe_produccion = cls.world.user(f"fg282-jefeprod-{tag.lower()}@example.com", ["Jefe de Producción"])
		cls.no_role = cls.world.user(f"fg282-norole-{tag.lower()}@example.com", [])
		cls.sysmgr = cls.world.user(f"fg282-sm-{tag.lower()}@example.com", ["System Manager"])
		cls._seq = 0

	def _code(self, label):
		type(self)._seq += 1
		return f"FG282-{self.tag}-{label}-{self._seq}"

	def _manufactured_item(self, with_bom=True, pack_source=True):
		item = self.world.item(self._code("FG"), default_material_request_type="Manufacture")
		if with_bom:
			pack = self.pack if pack_source else self.world.item(self._code("PACKNOWH"), default_material_request_type="Purchase")
			self._bom(item.name, pack.name)
		return item

	def _bom(self, item_code, pack_code):
		currency = frappe.db.get_value("Company", fx.COMPANY, "default_currency")
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item_code,
				"quantity": 1,
				"company": fx.COMPANY,
				"currency": currency,
				"conversion_rate": 1,
				"items": [
					{"item_code": self.raw.name, "qty": 2, "uom": fx.UOM, "rate": 10, "source_warehouse": self.wh_raw.name},
					{"item_code": pack_code, "qty": 1, "uom": fx.UOM, "rate": 1},  # source from Item Default
				],
			}
		)
		bom.insert()
		bom.submit()
		return self.world._track(bom)

	def _report(self, item_code, qty=5, warehouse=None):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": item_code,
				"warehouse": warehouse or self.wh_fg.name,
				"qty_solicitada": qty,
				"qty_disponible": 0,
				"detected_by": "Bodega",
				"shortage_reason": "Stock insuficiente",
			}
		)
		doc.insert()
		self.world.track_existing("Reporte de Faltante", doc.name)
		return doc

	def _route(self, report_name, user=None, fg=None):
		with fx.company_defaults(default_fg_warehouse=fg if fg is not None else self.wh_fg.name):
			with fx.as_user(user or self.jefe):
				result = jefe_api.route_shortage(report_name)
		if result.get("work_order"):
			self._track_wo(result["work_order"])
		return result

	def _track_wo(self, name):
		if ("Work Order", name) not in self.world._created:
			self.world.track_existing("Work Order", name)

	def _new_wo(self, item_code, bom_no, qty):
		wo = production_service._create_work_order(item_code, bom_no, fx.COMPANY, self.wh_fg.name, qty)
		self._track_wo(wo.name)
		return wo


class TestShortageRouting(_Base):
	def test_purchase_route_keeps_the_purchase_flow(self):
		report = self._report(self.raw.name)
		result = self._route(report.name)
		self.assertEqual((result["route"], result["work_order"], result["status"]), ("Purchase", None, "Abierto"))
		doc = frappe.get_doc("Reporte de Faltante", report.name)
		self.assertEqual((doc.procurement_route, doc.status, flt(doc.production_qty_allocated)), ("Purchase", "Abierto", 0))

	def test_manufacture_creates_a_submitted_skip_transfer_work_order(self):
		item = self._manufactured_item()
		report = self._report(item.name, qty=5)
		before = _stock_counts()
		result = self._route(report.name)
		self.assertEqual((result["route"], result["status"], result["allocated_qty"]), ("Manufacture", "En Proceso", 5))
		self.assertTrue(result["created_work_order"])
		wo = frappe.get_doc("Work Order", result["work_order"])
		bom = frappe.db.get_value("BOM", {"item": item.name, "docstatus": 1}, "name")
		self.assertEqual(
			(wo.docstatus, wo.production_item, wo.bom_no, wo.company, wo.fg_warehouse, wo.skip_transfer, flt(wo.qty)),
			(1, item.name, bom, fx.COMPANY, self.wh_fg.name, 1, 5),
		)
		self.assertEqual(wo.status, "In Process")  # skip_transfer: native status right after submit
		required = {r.item_code: r for r in wo.required_items}
		self.assertEqual(required[self.raw.name].source_warehouse, self.wh_raw.name)  # BOM Item.source_warehouse
		self.assertEqual(required[self.pack.name].source_warehouse, self.wh_pack.name)  # Item Default fallback
		self.assertEqual(flt(required[self.raw.name].required_qty), 10)
		doc = frappe.get_doc("Reporte de Faltante", report.name)
		self.assertEqual((doc.work_order, doc.procurement_route, flt(doc.production_qty_allocated)), (wo.name, "Manufacture", 5))
		# Nothing manufactured: no Manufacture entry, no ledger/GL.
		self.assertEqual(_stock_counts(), before)
		self.assertEqual(flt(wo.produced_qty), 0)

	def test_double_routing_never_creates_a_second_work_order(self):
		item = self._manufactured_item()
		report = self._report(item.name)
		first = self._route(report.name)
		wo_count = frappe.db.count("Work Order", {"production_item": item.name})
		second = self._route(report.name)
		self.assertTrue(second["already_routed"])
		self.assertEqual(second["work_order"], first["work_order"])
		self.assertEqual(frappe.db.count("Work Order", {"production_item": item.name}), wo_count)

	def test_blocked_cases_create_nothing(self):
		cases = {
			"no BOM": (self._manufactured_item(with_bom=False), None, "Missing BOM"),
			"sin bodega de componente": (self._manufactured_item(pack_source=False), None, "sin bodega configurada"),
			"fg distinta": (self._manufactured_item(), self.wh_raw.name, "la producción entrega en"),
			"fg sin configurar": (self._manufactured_item(), "", "sin bodega configurada"),
		}
		for label, (item, fg, reason) in cases.items():
			report = self._report(item.name)
			result = self._route(report.name, fg=fg)
			self.assertEqual(result["route"], "Blocked", label)
			self.assertIn(reason, result["reason"], label)
			self.assertIsNone(result["work_order"], label)
			self.assertEqual(result["status"], "Abierto", label)
			self.assertEqual(frappe.db.count("Work Order", {"production_item": item.name}), 0, label)

		# An invalid BOM (component disabled after approval) is Blocked too.
		item = self._manufactured_item()
		comp = self.world.item(self._code("COMPOFF"), default_material_request_type="Purchase", default_warehouse=self.wh_pack.name)
		frappe.db.set_value("BOM", {"item": item.name}, "is_active", 0)
		frappe.db.set_value("BOM", {"item": item.name}, "is_default", 0)
		bom = self._bom(item.name, comp.name)
		frappe.db.set_value("Item", comp.name, "disabled", 1)
		result = self._route(self._report(item.name).name)
		self.assertEqual(result["route"], "Blocked")
		self.assertTrue(any("deshabilitado" in p for p in result["problems"]), result)
		self.assertEqual(frappe.db.count("Work Order", {"bom_no": bom.name}), 0)

		# Blocked can be routed again once master data is fixed.
		item = self._manufactured_item(with_bom=False)
		report = self._report(item.name)
		self.assertEqual(self._route(report.name)["route"], "Blocked")
		self._bom(item.name, self.pack.name)
		self.assertEqual(self._route(report.name)["route"], "Manufacture")

	def test_resolved_report_cannot_be_routed(self):
		report = self._report(self.raw.name)
		frappe.db.set_value("Reporte de Faltante", report.name, "status", "Resuelto")
		with self.assertRaisesRegex(production_service.ShortageRoutingError, "Resuelto"):
			self._route(report.name)

	def test_consolidation_uses_unallocated_capacity_only(self):
		item = self._manufactured_item()
		bom = frappe.db.get_value("BOM", {"item": item.name, "docstatus": 1, "is_active": 1}, "name")
		big = self._new_wo(item.name, bom, 20)
		a, b, c = self._report(item.name, 5), self._report(item.name, 8), self._report(item.name, 10)
		ra, rb, rc = self._route(a.name), self._route(b.name), self._route(c.name)
		self.assertEqual((ra["work_order"], ra["created_work_order"]), (big.name, False))
		self.assertEqual((rb["work_order"], rb["created_work_order"]), (big.name, False))  # 5 + 8 <= 20
		self.assertNotEqual(rc["work_order"], big.name)  # 13 + 10 > 20 -> own Work Order
		self.assertTrue(rc["created_work_order"])
		self.assertEqual(flt(frappe.db.get_value("Work Order", rc["work_order"], "qty")), 10)
		self.assertEqual(flt(frappe.db.get_value("Work Order", big.name, "qty")), 20)  # never edited
		self.assertEqual(production_service._allocated_on(big.name), 13)

	def test_concurrent_routing_never_overallocates_a_work_order(self):
		item = self._manufactured_item()
		bom = frappe.db.get_value("BOM", {"item": item.name, "docstatus": 1, "is_active": 1}, "name")
		big = self._new_wo(item.name, bom, 20)
		r1, r2 = self._report(item.name, 15), self._report(item.name, 8)
		original = frappe.db.get_value("Company", fx.COMPANY, "default_fg_warehouse")
		frappe.db.set_value("Company", fx.COMPANY, "default_fg_warehouse", self.wh_fg.name)
		frappe.db.commit()
		site, results = frappe.local.site, {}

		def run(key, name):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.jefe)
			try:
				results[key] = ("ok", jefe_api.route_shortage(name))
				frappe.db.commit()
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("error", repr(e))
			finally:
				frappe.destroy()

		try:
			threads = [threading.Thread(target=run, args=("a", r1.name)), threading.Thread(target=run, args=("b", r2.name))]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=60)
		finally:
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			frappe.db.set_value("Company", fx.COMPANY, "default_fg_warehouse", original)
			frappe.db.commit()
		for key in results:
			if results[key][0] == "ok" and results[key][1].get("work_order"):
				self._track_wo(results[key][1]["work_order"])
		self.assertEqual({v[0] for v in results.values()}, {"ok"}, results)
		on_big = [v[1] for v in results.values() if v[1]["work_order"] == big.name]
		self.assertEqual(len(on_big), 1, results)  # 15 + 8 > 20: only one fits
		self.assertLessEqual(production_service._allocated_on(big.name), 20)

	def test_cancelled_work_order_releases_the_shortage(self):
		item = self._manufactured_item()
		report = self._report(item.name, 4)
		wo_name = self._route(report.name)["work_order"]
		frappe.get_doc("Work Order", wo_name).cancel()
		doc = frappe.get_doc("Reporte de Faltante", report.name)
		self.assertEqual(
			(doc.status, doc.work_order, doc.procurement_route, flt(doc.production_qty_allocated)), ("Abierto", None, None, 0)
		)
		self.assertIn(wo_name, doc.procurement_route_reason)
		again = self._route(report.name)
		self.assertTrue(again["created_work_order"])
		self.assertNotEqual(again["work_order"], wo_name)

	def test_purchase_receipt_refused_on_a_shortage_routed_to_production(self):
		item = self._manufactured_item()
		report = self._report(item.name, 4)
		self._route(report.name)
		with fx.as_user(self.jefe):
			with self.assertRaises(jefe_api.ShortageRoutedToProductionError):
				jefe_api.receive_shortage_purchase(report.name, qty=4, purchase_rate=100)

	def test_access_and_company(self):
		item = self._manufactured_item()
		report = self._report(item.name)
		for user in (self.bodega_user, self.produccion, self.jefe_produccion, self.no_role):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self._route(report.name, user=user)
		with fx.as_user("Guest"):
			with self.assertRaises((frappe.AuthenticationError, frappe.PermissionError)):
				jefe_api.route_shortage(report.name)
		with patch("fabergray_erp.permission_conditions._allowed_companies", return_value=["_Test Company"]):
			with self.assertRaises(frappe.PermissionError):
				self._route(report.name)
		with self.assertRaises(frappe.DoesNotExistError):
			self._route("FALT-NO-EXISTE")
		self.assertFalse(frappe.db.get_value("Reporte de Faltante", report.name, "procurement_route"))
		self.assertEqual(self._route(report.name, user=self.sysmgr)["route"], "Manufacture")
		# The client can only name the report -- never route/BOM/warehouse/qty.
		import inspect

		self.assertEqual(list(inspect.signature(jefe_api.route_shortage).parameters), ["shortage_report"])
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[jefe_api.route_shortage], ["POST"])

	def test_routing_fields_are_closed_to_desk_and_client(self):
		item = self._manufactured_item()
		report = self._report(item.name)
		other_wo = self._route(self._report(item.name).name)["work_order"]  # a real, existing Work Order
		for user in (self.jefe, "Administrator"):
			with fx.as_user(user):
				for field, value in (("procurement_route", "Manufacture"), ("work_order", other_wo), ("production_qty_allocated", 99)):
					with self.assertRaises(frappe.PermissionError, msg=f"{user} {field}"):
						frappe.client.set_value("Reporte de Faltante", report.name, field, value)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Reporte de Faltante",
					"item_code": item.name,
					"warehouse": self.wh_fg.name,
					"qty_solicitada": 1,
					"qty_disponible": 0,
					"detected_by": "Bodega",
					"shortage_reason": "Otro",
					"procurement_route": "Manufacture",
				}
			).insert()
		# Normal Bodega/Jefe edits of other fields still work.
		with fx.as_user(self.jefe):
			frappe.client.set_value("Reporte de Faltante", report.name, "resolution_note", "revisado")
		self.assertFalse(frappe.db.get_value("Reporte de Faltante", report.name, "procurement_route"))

	def test_new_roles_exist_without_broad_permissions(self):
		for role in ("Producción", "Jefe de Producción"):
			self.assertTrue(frappe.db.exists("Role", role))
			self.assertFalse(frappe.db.exists("Custom DocPerm", {"role": role}))
			for doctype in ("Item", "BOM", "Work Order", "Stock Entry", "Reporte de Faltante"):
				for user in (self.produccion, self.jefe_produccion):
					self.assertFalse(frappe.has_permission(doctype, "write", user=user), f"{user} {doctype}")


class TestRemainingPickList(_Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.customer = cls.world.customer(f"FG282 Remanente {cls.tag}")
		cls.difference_account = cls.world.stock_difference_account()

	def _stocked_item(self, qty):
		item = self.world.item(self._code("SELL"), default_material_request_type="Purchase")
		self.world.stock_up_real(item.name, self.wh_fg.name, qty)
		return item

	def _order(self, lines):
		delivery_date = add_days(nowdate(), 7)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": self.wh_fg.name,
				"items": [
					{"item_code": code, "warehouse": self.wh_fg.name, "qty": qty, "rate": 1000, "delivery_date": delivery_date}
					for code, qty in lines
				],
			}
		)
		so.insert()
		self.world.track_existing("Sales Order", so.name)
		so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		return so

	def _pick_lists(self, so_name):
		names = frappe.get_all(
			"Pick List Item", filters={"sales_order": so_name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True, order_by="parent asc"
		)
		for name in names:
			if ("Pick List", name) not in self.world._created:
				self.world.track_existing("Pick List", name)
		return names

	def _rows(self, pl):
		with fx.as_user(self.bodega_user):
			return bodega.get_pick_list(pl)["rows"]

	def _partial_pick(self, pl, picks):
		"""picks: {item_code: (picked, report?)}. Returns report names."""
		reports = []
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl)
			for row in bodega.get_pick_list(pl)["rows"]:
				picked, report = picks[row["item_code"]]
				bodega.set_picked_qty(pl, row["row_name"], picked)
				if report:
					name = bodega.report_shortage(pl, row["row_name"], picked, "Stock físico no encontrado")["name"]
					self.world.track_existing("Reporte de Faltante", name)
					reports.append(name)
			bodega.finish_picking(pl)
		return reports

	def _receive(self, report, qty):
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = jefe_api.receive_shortage_purchase(report, qty=qty, purchase_rate=500)
		self.world.track_existing("Stock Entry", result["stock_entry"])
		if result.get("remaining_pick_list"):
			self.world.track_existing("Pick List", result["remaining_pick_list"])
		return result

	def _billed(self, pl, so):
		lines, _t = facturacion._build_invoice_lines_and_totals(frappe.get_doc("Pick List", pl), so)
		return sum(flt(line["qty"]) for line in lines)

	def test_purchase_resolution_brings_back_exactly_the_remainder(self):
		item = self._stocked_item(10)
		so = self._order([(item.name, 10)])
		[pl1] = self._pick_lists(so.name)
		[report] = self._partial_pick(pl1, {item.name: (6, True)})
		pl1_before = frappe.db.get_value("Pick List", pl1, ["modified", "docstatus", "status"], as_dict=True)
		so_before = frappe.db.get_value("Sales Order", so.name, ["modified", "per_picked", "status"], as_dict=True)
		before = _stock_counts()

		result = self._receive(report, 4)
		self.assertEqual(result["status"], "Resuelto")
		pl2 = result["remaining_pick_list"]
		self.assertTrue(pl2)
		rows = frappe.get_doc("Pick List", pl2).locations
		self.assertEqual(sum(flt(r.stock_qty) for r in rows), 4)
		self.assertEqual({r.sales_order_item for r in rows}, {so.items[0].name})
		self.assertEqual(self._pick_lists(so.name), sorted([pl1, pl2]))
		# The original Pick List and the Sales Order are untouched; the only
		# ledger movement is the purchase's own Material Receipt.
		self.assertEqual(frappe.db.get_value("Pick List", pl1, ["modified", "docstatus", "status"], as_dict=True), pl1_before)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, ["modified", "per_picked", "status"], as_dict=True), so_before)
		self.assertEqual(_stock_counts()["manufacture"], before["manufacture"])

		# Idempotent: a second call creates nothing.
		self.assertIsNone(ensure_remaining_pick_list(report))
		self.assertEqual(len(self._pick_lists(so.name)), 2)

		# Bodega picks the 4: the order ends at 10 and Facturación bills 6 + 4.
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl2)
			for row in bodega.get_pick_list(pl2)["rows"]:
				bodega.set_picked_qty(pl2, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl2)
		so.reload()
		self.assertEqual((flt(so.items[0].picked_qty), flt(so.per_picked)), (10, 100))
		self.assertEqual(self._billed(pl1, so) + self._billed(pl2, so), 10)
		self.assertEqual(self._billed(pl1, so), 6)

	def test_draft_pick_list_with_complement_gets_no_new_pick_list(self):
		item = self._stocked_item(6)
		so = self._order([(item.name, 10)])
		[pl1] = self._pick_lists(so.name)
		rows = sorted(self._rows(pl1), key=lambda r: -flt(r["qty_solicitada"]))
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl1)
			bodega.set_picked_qty(pl1, rows[0]["row_name"], 6)
			report = bodega.report_shortage(pl1, rows[1]["row_name"], 0, "Stock insuficiente")["name"]
		self.world.track_existing("Reporte de Faltante", report)
		result = self._receive(report, 4)
		self.assertEqual(result["status"], "Resuelto")
		self.assertIsNone(result["remaining_pick_list"])  # Pick List still draft
		self.assertEqual(self._pick_lists(so.name), [pl1])
		with fx.as_user(self.bodega_user):
			bodega.set_picked_qty(pl1, rows[1]["row_name"], 4)
			bodega.finish_picking(pl1)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 100)

	def test_only_the_resolved_line_comes_back(self):
		a, b = self._stocked_item(10), self._stocked_item(10)
		so = self._order([(a.name, 10), (b.name, 10)])
		[pl1] = self._pick_lists(so.name)
		report_a, report_b = self._partial_pick(pl1, {a.name: (6, True), b.name: (7, True)})
		result = self._receive(report_a, 4)
		rows = frappe.get_doc("Pick List", result["remaining_pick_list"]).locations
		self.assertEqual({r.item_code for r in rows}, {a.name})
		self.assertEqual(sum(flt(r.stock_qty) for r in rows), 4)
		result_b = self._receive(report_b, 3)
		rows_b = frappe.get_doc("Pick List", result_b["remaining_pick_list"]).locations
		self.assertEqual(({r.item_code for r in rows_b}, sum(flt(r.stock_qty) for r in rows_b)), ({b.name}, 3))

	def test_concurrent_resolutions_create_one_pick_list(self):
		item = self._stocked_item(10)
		so = self._order([(item.name, 10)])
		[pl1] = self._pick_lists(so.name)
		[report] = self._partial_pick(pl1, {item.name: (6, True)})
		frappe.db.set_value("Reporte de Faltante", report, "status", "Resuelto")
		frappe.db.commit()
		site, results = frappe.local.site, {}

		def run(key):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.jefe)
			try:
				# As in production: the caller's transaction is retried (bounded)
				# on MariaDB's snapshot-isolation conflict (1020 -> deadlock).
				from fabergray_erp.api.recorridos import _retrying_on_deadlock

				results[key] = ("ok", _retrying_on_deadlock(ensure_remaining_pick_list)(report))
				frappe.db.commit()
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("error", repr(e))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=run, args=(k,)) for k in ("a", "b")]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		names = self._pick_lists(so.name)
		self.assertEqual({v[0] for v in results.values()}, {"ok"}, results)
		created = [v[1] for v in results.values() if v[1]]
		self.assertEqual(len(created), 1, results)  # the second one saw the first Pick List
		self.assertEqual(len(names), 2)
		self.assertEqual(sum(flt(r.stock_qty) for r in frappe.get_doc("Pick List", created[0]).locations), 4)
