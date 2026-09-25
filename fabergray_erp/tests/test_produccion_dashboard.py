# -*- coding: utf-8 -*-
"""Fase 28.3 -- Page Producción, read API (api/produccion.py).

- access: Producción / Jefe de Producción / System Manager only; other
  roles and Guest refused; company resolved server-side; GET only;
- population: only submitted Work Orders linked to a Manufacture-routed
  Reporte de Faltante of the company (no bare Work Order, no Purchase,
  no cancelled order);
- estado operativo from the native status + produced_qty, material level
  from LIVE Bin stock per source warehouse, with incomplete configuration
  and other-company warehouses handled without errors;
- the three quantities (planned / produced / allocated) kept apart;
- pedidos / faltantes asociados, search, filters, pagination;
- company isolation (Work Order, faltante, Sales Order, Bin, Warehouse);
- the KPIs agree with the tabs; COMPLETADAS HOY uses the posting date of
  the last submitted Manufacture Stock Entry;
- every endpoint is really read-only (nothing changes in Stock Entry,
  Stock Ledger Entry, GL Entry, Work Order, Reporte de Faltante, Sales
  Order, Pick List or Bin).

Produced quantities and native statuses are set directly on the Work
Order (db) to reach each state: 28.3 never manufactures, and neither do
these tests. The "Manufacture" Stock Entry used for COMPLETADAS HOY is a
bare header row (no items, no ledger), deleted by the same test."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, now_datetime, nowdate

from fabergray_erp import production_service
from fabergray_erp.api import jefe_bodega as jefe_api
from fabergray_erp.api import produccion as api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_READ_ONLY_TABLES = (
	"Stock Entry",
	"Stock Ledger Entry",
	"GL Entry",
	"Work Order",
	"Work Order Item",
	"Reporte de Faltante",
	"Sales Order",
	"Pick List",
	"Bin",
)


def _snapshot():
	"""COUNT + MAX(modified) of every table the endpoints must never touch,
	plus the Bin quantities and every Work Order's status/produced_qty."""
	snap = {}
	for doctype in _READ_ONLY_TABLES:
		snap[doctype] = tuple(frappe.db.sql(f"SELECT COUNT(*), MAX(modified) FROM `tab{doctype}`")[0])
	snap["bin_qty"] = tuple(
		frappe.db.sql(
			"SELECT COALESCE(SUM(actual_qty), 0), COALESCE(SUM(reserved_qty_for_production), 0), "
			"COALESCE(SUM(projected_qty), 0) FROM `tabBin`"
		)[0]
	)
	snap["wo_state"] = tuple(frappe.db.sql("SELECT name, status, produced_qty, docstatus FROM `tabWork Order` ORDER BY name"))
	snap["report_state"] = tuple(
		frappe.db.sql(
			"SELECT name, status, work_order, production_qty_allocated, procurement_route FROM `tabReporte de Faltante` ORDER BY name"
		)
	)
	return snap


class _Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		tag = frappe.generate_hash(length=5).upper()
		cls.tag = tag
		cls.wh_fg = cls.world.warehouse(f"FG283 Terminado {tag}")
		cls.wh_raw = cls.world.warehouse(f"FG283 Materias {tag}")
		cls.wh_pack = cls.world.warehouse(f"FG283 Empaque {tag}")
		cls.raw = cls.world.item(f"FG283-{tag}-RAW", default_material_request_type="Purchase")
		cls.pack = cls.world.item(
			f"FG283-{tag}-PACK", default_material_request_type="Purchase", default_warehouse=cls.wh_pack.name
		)
		cls.jefe = cls.world.user(f"fg283-jefe-{tag.lower()}@example.com", ["Jefe de Bodega"])
		cls.produccion = cls.world.user(f"fg283-prod-{tag.lower()}@example.com", ["Producción"])
		cls.jefe_produccion = cls.world.user(f"fg283-jefeprod-{tag.lower()}@example.com", ["Jefe de Producción"])
		cls.sysmgr = cls.world.user(f"fg283-sm-{tag.lower()}@example.com", ["System Manager"])
		cls.no_role = cls.world.user(f"fg283-norole-{tag.lower()}@example.com", [])
		cls.bodega_user = cls.world.user(f"fg283-bodega-{tag.lower()}@example.com", ["Bodega"])
		cls.customer_a = cls.world.customer(f"FG283 Cliente A {tag}")
		cls.customer_b = cls.world.customer(f"FG283 Cliente B {tag}")
		cls._seq = 0

	def _code(self, label):
		type(self)._seq += 1
		return f"FG283-{self.tag}-{label}-{self._seq}"

	def _manufactured_item(self, name_suffix=""):
		item = self.world.item(self._code("FG") + name_suffix, default_material_request_type="Manufacture")
		currency = frappe.db.get_value("Company", fx.COMPANY, "default_currency")
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item.name,
				"quantity": 1,
				"company": fx.COMPANY,
				"currency": currency,
				"conversion_rate": 1,
				"items": [
					{"item_code": self.raw.name, "qty": 2, "uom": fx.UOM, "rate": 10, "source_warehouse": self.wh_raw.name},
					{"item_code": self.pack.name, "qty": 1, "uom": fx.UOM, "rate": 1},  # Item Default fallback
				],
			}
		)
		bom.insert()
		bom.submit()
		self.world._track(bom)
		return item

	def _sales_order(self, item_code, customer, qty=5):
		return self.world.submitted_sales_order(item_code, self.wh_fg.name, qty, customer.name)

	def _report(self, item_code, qty=5, sales_order=None):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": item_code,
				"warehouse": self.wh_fg.name,
				"sales_order": sales_order,
				"qty_solicitada": qty,
				"qty_disponible": 0,
				"detected_by": "Bodega",
				"shortage_reason": "Stock insuficiente",
			}
		)
		doc.insert()
		self.world.track_existing("Reporte de Faltante", doc.name)
		return doc

	def _route(self, report_name):
		with fx.company_defaults(default_fg_warehouse=self.wh_fg.name):
			with fx.as_user(self.jefe):
				result = jefe_api.route_shortage(report_name)
		if result.get("work_order"):
			self._track_wo(result["work_order"])
		return result

	def _track_wo(self, name):
		if ("Work Order", name) not in self.world._created:
			self.world.track_existing("Work Order", name)

	def _routed_order(self, qty=5, sales_order=None, item=None):
		"""A manufactured item + a report routed to its own new Work Order."""
		item = item or self._manufactured_item()
		report = self._report(item.name, qty, sales_order=sales_order)
		result = self._route(report.name)
		self.assertEqual(result["route"], "Manufacture", result)
		return item, report, result["work_order"]

	def _orders(self, user=None, **kwargs):
		kwargs.setdefault("search", self.tag)
		with fx.as_user(user or self.produccion):
			return api.get_production_orders(**kwargs)

	def _names(self, **kwargs):
		return [o["name"] for o in self._orders(**kwargs)["items"]]

	def _detail(self, work_order, user=None):
		with fx.as_user(user or self.produccion):
			return api.get_production_order_detail(work_order)

	def _dashboard(self, user=None):
		with fx.as_user(user or self.produccion):
			return api.get_production_dashboard()

	def _set_bin(self, item_code, warehouse, qty):
		from erpnext.stock.utils import get_bin

		bin_doc = get_bin(item_code, warehouse)
		bin_doc.db_set("actual_qty", qty, update_modified=False)

	@contextmanager
	def _wo_state(self, work_order, **values):
		"""Temporarily put a Work Order in a state 28.3 cannot reach by
		itself (production is 28.4), restoring it for teardown."""
		fields = list(values)
		original = frappe.db.get_value("Work Order", work_order, fields, as_dict=True)
		frappe.db.set_value("Work Order", work_order, values, update_modified=False)
		try:
			yield
		finally:
			frappe.db.set_value("Work Order", work_order, original, update_modified=False)

	@contextmanager
	def _manufacture_evidence(self, work_order, posting_date):
		"""A bare submitted "Manufacture" Stock Entry header (no rows, no
		ledger) -- the native evidence COMPLETADAS HOY reads."""
		name = f"FG283-SE-{frappe.generate_hash(length=8)}"
		now = now_datetime()
		frappe.db.sql(
			"""
			INSERT INTO `tabStock Entry`
				(name, creation, modified, owner, modified_by, docstatus, company, purpose, stock_entry_type,
				 posting_date, posting_time, work_order)
			VALUES (%s, %s, %s, 'Administrator', 'Administrator', 1, %s, 'Manufacture', 'Manufacture', %s, '10:00:00', %s)
			""",
			(name, now, now, fx.COMPANY, posting_date, work_order),
		)
		try:
			yield name
		finally:
			frappe.db.sql("DELETE FROM `tabStock Entry` WHERE name = %s", (name,))


class TestProduccionAccess(_Base):
	def test_roles_that_can_read(self):
		_item, _report, wo = self._routed_order()
		for user in (self.produccion, self.jefe_produccion, self.sysmgr, "Administrator"):
			self.assertIn("kpis", self._dashboard(user))
			self.assertIn(wo, self._names(user=user))
			self.assertEqual(self._detail(wo, user)["name"], wo)

	def test_other_roles_and_guest_are_refused(self):
		_item, _report, wo = self._routed_order()
		for user in (self.no_role, self.bodega_user, self.jefe):
			with fx.as_user(user):
				for call in (
					api.get_production_dashboard,
					api.get_production_orders,
					lambda: api.get_production_order_detail(wo),
				):
					with self.assertRaises(frappe.PermissionError, msg=user):
						call()
		with fx.as_user("Guest"):
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.get_production_dashboard()
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.get_production_order_detail(wo)

	def test_endpoints_are_get_only_and_never_take_a_company(self):
		import inspect

		for fn in (api.get_production_dashboard, api.get_production_orders, api.get_production_order_detail):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["GET"], fn.__name__)
			self.assertNotIn("company", inspect.signature(fn).parameters, fn.__name__)

	def test_company_outside_the_callers_allowed_companies_is_refused(self):
		_item, _report, wo = self._routed_order()
		with patch("fabergray_erp.api.produccion._allowed_companies", return_value=["_Test Company"]):
			with fx.as_user(self.produccion):
				for call in (
					api.get_production_dashboard,
					api.get_production_orders,
					lambda: api.get_production_order_detail(wo),
				):
					with self.assertRaises(frappe.PermissionError):
						call()

	def test_roles_still_have_no_broad_permissions(self):
		for role in ("Producción", "Jefe de Producción"):
			self.assertFalse(frappe.db.exists("Custom DocPerm", {"role": role}))
			self.assertFalse(frappe.db.exists("DocPerm", {"role": role}))
		for doctype in ("Work Order", "Stock Entry", "BOM", "Item", "Reporte de Faltante"):
			for user in (self.produccion, self.jefe_produccion):
				for ptype in ("write", "create", "submit", "cancel", "delete"):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=user), f"{user} {doctype} {ptype}")


class TestProduccionPopulation(_Base):
	def test_only_manufacture_routed_orders_appear(self):
		item, _report, routed = self._routed_order()
		bom = frappe.db.get_value("BOM", {"item": item.name, "docstatus": 1}, "name")
		bare = production_service._create_work_order(item.name, bom, fx.COMPANY, self.wh_fg.name, 3)
		self._track_wo(bare.name)
		purchase_report = self._report(self.raw.name)
		self.assertEqual(self._route(purchase_report.name)["route"], "Purchase")

		names = self._names()
		self.assertIn(routed, names)
		self.assertNotIn(bare.name, names)  # Work Order without an associated shortage
		with self.assertRaises(frappe.DoesNotExistError):
			self._detail(bare.name)
		with self.assertRaises(frappe.DoesNotExistError):
			self._detail("MFG-WO-NO-EXISTE")

	def test_cancelled_orders_never_appear(self):
		_item, report, wo = self._routed_order()
		frappe.get_doc("Work Order", wo).cancel()  # 28.2 hook unlinks the (open) report
		self.assertNotIn(wo, self._names())
		with self.assertRaises(frappe.DoesNotExistError):
			self._detail(wo)
		# A resolved report keeps its link to a cancelled order: still hidden.
		_item, report, wo2 = self._routed_order()
		frappe.db.set_value("Reporte de Faltante", report.name, "status", "Resuelto", update_modified=False)
		frappe.get_doc("Work Order", wo2).cancel()
		self.assertEqual(frappe.db.get_value("Reporte de Faltante", report.name, "work_order"), wo2)
		self.assertNotIn(wo2, self._names())

	def test_empty_state(self):
		res = self._orders(search=f"NADA-{self.tag}-ZZZ")
		self.assertEqual((res["items"], res["total"], res["has_more"]), ([], 0, False))


class TestProduccionStatesAndQuantities(_Base):
	def _card(self, wo):
		[card] = [o for o in self._orders()["items"] if o["name"] == wo]
		return card

	def test_states_from_native_status_and_produced_qty(self):
		_item, _report, wo = self._routed_order(qty=5)
		# skip_transfer: ERPNext says "In Process" before producing anything.
		self.assertEqual(frappe.db.get_value("Work Order", wo, "status"), "In Process")
		self.assertEqual(self._card(wo)["state"], "pendiente")
		with self._wo_state(wo, produced_qty=2):
			card = self._card(wo)
			self.assertEqual((card["state"], card["produced_qty"], card["pending_qty"]), ("en_produccion", 2, 3))
			self.assertEqual(self._detail(wo)["state"], "en_produccion")
		with self._wo_state(wo, produced_qty=5, status="Completed"):
			card = self._card(wo)
			self.assertEqual((card["state"], card["material_level"], card["pending_qty"]), ("completada", None, 0))
			self.assertIn(wo, self._names(tab="completadas"))
			self.assertNotIn(wo, self._names(tab="pendientes"))
		with self._wo_state(wo, status="Stopped"):
			self.assertEqual(self._card(wo)["state"], "detenida")
			for tab in ("pendientes", "en_produccion", "falta_material", "completadas"):
				self.assertNotIn(wo, self._names(tab=tab))
			self.assertIn(wo, self._names(tab="todas"))

	def test_three_quantities_never_mixed(self):
		item = self._manufactured_item()
		bom = frappe.db.get_value("BOM", {"item": item.name, "docstatus": 1}, "name")
		big = production_service._create_work_order(item.name, bom, fx.COMPANY, self.wh_fg.name, 20)
		self._track_wo(big.name)
		so_a = self._sales_order(item.name, self.customer_a, 5)
		so_b = self._sales_order(item.name, self.customer_b, 8)
		r_a = self._report(item.name, 5, sales_order=so_a.name)
		r_b = self._report(item.name, 8, sales_order=so_b.name)
		self.assertEqual(self._route(r_a.name)["work_order"], big.name)
		self.assertEqual(self._route(r_b.name)["work_order"], big.name)
		with self._wo_state(big.name, produced_qty=6):
			card = self._card(big.name)
			self.assertEqual(
				(card["qty"], card["produced_qty"], card["allocated_qty"], card["available_lot_qty"], card["pending_qty"]),
				(20, 6, 13, 7, 14),
			)
			self.assertEqual((card["oldest_sales_order"], card["orders_count"]), (so_a.name, 2))
			self.assertEqual(card["oldest_customer_name"], self.customer_a.customer_name)
			detail = self._detail(big.name)
			self.assertEqual((detail["qty"], detail["produced_qty"], detail["allocated_qty"]), (20, 6, 13))
			self.assertEqual(detail["reports_allocated_total"], 13)
			rows = {r["name"]: r for r in detail["reports"]}
			self.assertEqual(set(rows), {r_a.name, r_b.name})
			self.assertEqual(
				(rows[r_a.name]["sales_order"], rows[r_a.name]["customer"], rows[r_a.name]["qty_faltante"], rows[r_a.name]["production_qty_allocated"]),
				(so_a.name, self.customer_a.name, 5, 5),
			)
			self.assertEqual(rows[r_b.name]["production_qty_allocated"], 8)
			self.assertEqual(rows[r_b.name]["status"], "En Proceso")
			self.assertTrue(rows[r_b.name]["reported_on"])
			self.assertEqual(detail["fg_warehouse"], self.wh_fg.name)
			self.assertEqual(detail["bom_no"], bom)


class TestProduccionMaterials(_Base):
	def _level(self, wo):
		[card] = [o for o in self._orders()["items"] if o["name"] == wo]
		detail = self._detail(wo)
		self.assertEqual(card["material_level"], detail["material_level"])  # one rule
		return detail

	def test_levels_follow_live_bin_stock(self):
		_item, _report, wo = self._routed_order(qty=5)  # raw 10 @ wh_raw, pack 5 @ wh_pack
		self._set_bin(self.raw.name, self.wh_raw.name, 0)
		self._set_bin(self.pack.name, self.wh_pack.name, 0)
		self.assertEqual(self._level(wo)["material_level"], "falta")
		self.assertIn(wo, self._names(tab="falta_material"))
		self.assertIn(wo, self._names(tab="pendientes"))  # one order, two indicators

		self._set_bin(self.raw.name, self.wh_raw.name, 15)
		self._set_bin(self.pack.name, self.wh_pack.name, 5)
		detail = self._level(wo)
		self.assertEqual(detail["material_level"], "ok")
		self.assertNotIn(wo, self._names(tab="falta_material"))
		rows = {m["item_code"]: m for m in detail["materials"]}
		self.assertEqual(
			{k: rows[self.raw.name][k] for k in ("required_qty", "consumed_qty", "pending_qty", "available_qty", "shortfall_qty", "level", "source_warehouse")},
			{"required_qty": 10, "consumed_qty": 0, "pending_qty": 10, "available_qty": 15, "shortfall_qty": 0, "level": "ok", "source_warehouse": self.wh_raw.name},
		)
		self.assertEqual(rows[self.pack.name]["source_warehouse"], self.wh_pack.name)  # Item Default

		self._set_bin(self.raw.name, self.wh_raw.name, 7)
		detail = self._level(wo)
		self.assertEqual(detail["material_level"], "parcial")
		raw = next(m for m in detail["materials"] if m["item_code"] == self.raw.name)
		self.assertEqual((raw["available_qty"], raw["shortfall_qty"], raw["level"]), (7, 3, "parcial"))
		self.assertIn(wo, self._names(tab="falta_material", material="parcial"))
		self.assertNotIn(wo, self._names(material="ok"))

		# consumed_qty (ERPNext's own figure) reduces what is still needed.
		row = frappe.db.get_value("Work Order Item", {"parent": wo, "item_code": self.raw.name}, "name")
		frappe.db.set_value("Work Order Item", row, "consumed_qty", 4, update_modified=False)
		try:
			raw = next(m for m in self._level(wo)["materials"] if m["item_code"] == self.raw.name)
			self.assertEqual((raw["pending_qty"], raw["shortfall_qty"], raw["level"]), (6, 0, "ok"))
			self.assertEqual(self._level(wo)["material_level"], "ok")
		finally:
			frappe.db.set_value("Work Order Item", row, "consumed_qty", 0, update_modified=False)
		# Never the snapshot stored on the Work Order Item.
		frappe.db.set_value("Work Order Item", row, "available_qty_at_source_warehouse", 9999, update_modified=False)
		self.assertEqual(self._level(wo)["material_level"], "parcial")

	def test_component_without_or_with_invalid_warehouse(self):
		_item, _report, wo = self._routed_order(qty=5)
		self._set_bin(self.raw.name, self.wh_raw.name, 100)
		self._set_bin(self.pack.name, self.wh_pack.name, 100)
		row = frappe.db.get_value("Work Order Item", {"parent": wo, "item_code": self.pack.name}, "name")
		try:
			# Historical data: no source warehouse -> config problem, no error.
			frappe.db.set_value("Work Order Item", row, "source_warehouse", None, update_modified=False)
			detail = self._level(wo)
			self.assertEqual(detail["material_level"], "config")
			pack = next(m for m in detail["materials"] if m["item_code"] == self.pack.name)
			self.assertEqual((pack["warehouse_problem"], pack["available_qty"], pack["level"]), ("sin_bodega", None, "config"))
			self.assertIn(wo, self._names(tab="falta_material", material="config"))

			# Another company's warehouse: its name and its stock are never read.
			other = frappe.db.get_value("Warehouse", {"company": ["!=", fx.COMPANY], "is_group": 0}, "name")
			if other:
				frappe.db.set_value("Work Order Item", row, "source_warehouse", other, update_modified=False)
				self._set_bin(self.pack.name, other, 500)
				pack = next(m for m in self._level(wo)["materials"] if m["item_code"] == self.pack.name)
				self.assertEqual(
					(pack["warehouse_problem"], pack["source_warehouse"], pack["available_qty"], pack["level"]),
					("otra_empresa", None, None, "config"),
				)
				frappe.db.delete("Bin", {"item_code": self.pack.name, "warehouse": other})
		finally:
			frappe.db.set_value("Work Order Item", row, "source_warehouse", self.wh_pack.name, update_modified=False)
		self.assertEqual(self._level(wo)["material_level"], "ok")


class TestProduccionListing(_Base):
	def test_search_by_order_item_name_sales_order_and_customer(self):
		item = self._manufactured_item("-DESENGRASANTE")
		so = self._sales_order(item.name, self.customer_a, 5)
		_item, _report, wo = self._routed_order(sales_order=so.name, item=item)
		for text in (wo, item.name, "DESENGRASANTE", so.name, self.customer_a.customer_name):
			self.assertIn(wo, self._names(search=text), text)
		self.assertNotIn(wo, self._names(search=f"{self.tag}-NO-EXISTE"))
		# LIKE wildcards are literal.
		self.assertEqual(self._orders(search="%")["total"], 0)

	def test_pagination(self):
		marker = f"PG{self.tag}"  # only this test's orders
		created = [self._routed_order(item=self._manufactured_item(f"-{marker}"))[2] for _ in range(3)]
		page1 = self._orders(search=marker, page=1, page_length=2)
		page2 = self._orders(search=marker, page=2, page_length=2)
		self.assertEqual((page1["total"], page1["page"], page1["page_size"], page1["has_more"]), (3, 1, 2, True))
		self.assertEqual((page2["total"], len(page2["items"]), page2["has_more"]), (3, 1, False))
		self.assertEqual(sorted([o["name"] for o in page1["items"] + page2["items"]]), sorted(created))
		self.assertEqual(self._orders(page_length=500)["page_size"], api.MAX_PAGE_LENGTH)

	def test_filters(self):
		_item, _report, recent = self._routed_order()
		_item, _report, old = self._routed_order()
		frappe.db.set_value("Work Order", old, "planned_start_date", add_days(nowdate(), -40), update_modified=False)
		self.assertEqual(set(self._names()), {recent, old})
		self.assertEqual(self._names(date_range="30d"), [recent])
		self.assertEqual(self._names(date_range="today"), [recent])
		self.assertEqual(set(self._names(tab="pendientes")), {recent, old})
		self.assertEqual(self._names(tab="en_produccion"), [])
		for kwargs in ({"tab": "x"}, {"material": "x"}, {"date_range": "1y"}):
			with self.assertRaises(frappe.ValidationError, msg=kwargs):
				self._orders(**kwargs)

	def test_kpis_agree_with_tabs_and_completed_today(self):
		_item, _report, wo = self._routed_order(qty=5)
		base = self._dashboard()["kpis"]
		totals = {tab: self._orders(search="", tab=tab)["total"] for tab in ("pendientes", "en_produccion", "falta_material")}
		self.assertEqual(totals, {k: base[k] for k in ("pendientes", "en_produccion", "falta_material")})

		with self._wo_state(wo, produced_qty=5, status="Completed"):
			self.assertEqual(self._dashboard()["kpis"]["completadas_hoy"], base["completadas_hoy"])  # no evidence
			with self._manufacture_evidence(wo, add_days(nowdate(), -1)):
				self.assertEqual(self._dashboard()["kpis"]["completadas_hoy"], base["completadas_hoy"])
			with self._manufacture_evidence(wo, nowdate()):
				kpis = self._dashboard()["kpis"]
				self.assertEqual(kpis["completadas_hoy"], base["completadas_hoy"] + 1)
				self.assertEqual(kpis["pendientes"], base["pendientes"] - 1)
				card = next(o for o in self._orders()["items"] if o["name"] == wo)
				self.assertEqual(card["last_manufacture_date"], nowdate())


class TestProduccionCompanyIsolation(_Base):
	def test_work_order_of_another_company(self):
		_item, _report, wo = self._routed_order()
		before = self._dashboard()["kpis"]["pendientes"]
		frappe.db.set_value("Work Order", wo, "company", "_Test Company", update_modified=False)
		try:
			self.assertNotIn(wo, self._names())
			self.assertNotIn(wo, self._names(search=wo))
			with self.assertRaises(frappe.PermissionError):
				self._detail(wo)
			self.assertEqual(self._dashboard()["kpis"]["pendientes"], before - 1)
		finally:
			frappe.db.set_value("Work Order", wo, "company", fx.COMPANY, update_modified=False)

	def test_shortage_and_sales_order_of_another_company(self):
		item = self._manufactured_item()
		so = self._sales_order(item.name, self.customer_a, 5)
		_item, report, wo = self._routed_order(sales_order=so.name, item=item)
		other_wh = frappe.db.get_value("Warehouse", {"company": ["!=", fx.COMPANY], "is_group": 0}, "name")
		# Sales Order of another company: never exposed, never searchable.
		frappe.db.set_value("Sales Order", so.name, "company", "_Test Company", update_modified=False)
		try:
			[row] = self._detail(wo)["reports"]
			self.assertEqual((row["sales_order"], row["customer"], row["customer_name"]), (None, None, None))
			card = next(o for o in self._orders()["items"] if o["name"] == wo)
			self.assertEqual((card["oldest_sales_order"], card["oldest_customer"]), (None, None))
			self.assertNotIn(wo, self._names(search=so.name))
			self.assertNotIn(wo, self._names(search=self.customer_a.customer_name))
		finally:
			frappe.db.set_value("Sales Order", so.name, "company", fx.COMPANY, update_modified=False)
		# Faltante of another company (its warehouse): the order leaves the population.
		if other_wh:
			frappe.db.set_value("Reporte de Faltante", report.name, "warehouse", other_wh, update_modified=False)
			try:
				self.assertNotIn(wo, self._names())
				with self.assertRaises(frappe.DoesNotExistError):
					self._detail(wo)
			finally:
				frappe.db.set_value("Reporte de Faltante", report.name, "warehouse", self.wh_fg.name, update_modified=False)
		self.assertIn(wo, self._names())


class TestProduccionReadOnly(_Base):
	def test_every_endpoint_is_read_only(self):
		item = self._manufactured_item()
		so = self._sales_order(item.name, self.customer_a, 5)
		_item, _report, wo = self._routed_order(sales_order=so.name, item=item)
		self._set_bin(self.raw.name, self.wh_raw.name, 3)
		frappe.db.commit()
		before = _snapshot()
		for user in (self.produccion, self.jefe_produccion, self.sysmgr):
			self._dashboard(user)
			for tab in api.TABS:
				for material in api.MATERIAL_FILTERS:
					self._orders(user=user, tab=tab, material=material, search="")
			for date_range in api.DATE_RANGES:
				self._orders(user=user, date_range=date_range, page=2, page_length=1)
			self._orders(user=user, search=so.name)
			self._detail(wo, user)
		self.assertEqual(_snapshot(), before)
		self.assertEqual(flt(frappe.db.get_value("Work Order", wo, "produced_qty")), 0)
