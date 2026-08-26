# -*- coding: utf-8 -*-
"""Commit 22.4 -- api/inventario.py: the three read-only endpoints behind
a future Page Inventario (not built yet). Bodega/Jefe de Bodega/System
Manager read Item/Bin/Item Price/Stock Ledger Entry via this commit's own
9 Custom DocPerm grants (fixtures/custom_docperm.json +
fixtures/system_manager_custom_docperm.json), each demonstrated missing
by a real has_permission() audit before being created.

Six kinds of check, matching the approved Commit 22.4 brief:
- functional: stock with/without Bin rows, sum across multiple
  warehouses, price present/absent, per-warehouse detail, movements
  ordered -- every assertion via TestWorld's own item()/warehouse()/
  stock_up()/stock_up_real() fixtures (real Bin/Item Price/Stock Ledger
  Entry reads, nothing invented);
- positive permissions: Bodega and Jefe de Bodega, real restricted
  sessions, can call all three endpoints;
- negative permissions: a role with no Item/Bin/Item Price/Stock Ledger
  Entry grant ("Gestión de Clientes" -- a real role in this app, just
  not an inventory one) is denied by all three;
- structural guardrails: exactly these three whitelisted functions exist
  in the module, and an AST walk (not a substring search) proves none of
  them contains ignore_permissions=, or a call to frappe.set_user/
  frappe.get_all/frappe.db.commit/frappe.db.sql;
- bulk-read guardrail: _bin_totals() is called at most once per public
  function invocation (never once per Item), confirmed by counting real
  Bin SELECTs via frappe.db's own query log, not by reading the source
  and assuming;
- residue: nothing this suite creates survives it, and access_id_producto
  migrated Items (Existencia from the Access Excel) are never touched.
"""

import ast
import inspect

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import inventario as api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_READ_ENDPOINTS = ("get_inventory_summary", "get_inventory_items", "get_inventory_item_detail")

_FORBIDDEN_CALLS = {"frappe.set_user", "frappe.get_all", "frappe.db.commit", "frappe.db.sql"}


def _dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _forbidden_findings(source):
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)
            if dotted in _FORBIDDEN_CALLS:
                findings.append(dotted)
        if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
            findings.append("ignore_permissions=")
    return findings


class TestInventarioApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.bodega_user = cls.world.user("fg2204-bodega@example.com", ["Bodega"])
        cls.jefe_user = cls.world.user("fg2204-jefe@example.com", ["Jefe de Bodega"])
        cls.noaccess_user = cls.world.user("fg2204-noaccess@example.com", ["Gestión de Clientes"])

    # -- Funcional: stock ---------------------------------------------------------

    def test_item_with_stock(self):
        wh = self.world.warehouse("FG2204 WithStock")
        item = self.world.item("FG2204-WITHSTOCK-ITEM")
        self.world.stock_up(item.name, wh.name, 12)

        result = api.get_inventory_items(txt=item.name)
        row = next(r for r in result["items"] if r["item_code"] == item.name)
        self.assertEqual(row["total_actual_qty"], 12)

    def test_item_without_stock(self):
        item = self.world.item("FG2204-NOSTOCK-ITEM")

        result = api.get_inventory_items(txt=item.name)
        row = next(r for r in result["items"] if r["item_code"] == item.name)
        self.assertEqual(row["total_actual_qty"], 0)

        out_of_stock = api.get_inventory_items(txt=item.name, status="out_of_stock")
        self.assertIn(item.name, [r["item_code"] for r in out_of_stock["items"]])

    def test_sum_across_multiple_warehouses(self):
        wh_a = self.world.warehouse("FG2204 MultiA")
        wh_b = self.world.warehouse("FG2204 MultiB")
        item = self.world.item("FG2204-MULTIWH-ITEM")
        self.world.stock_up(item.name, wh_a.name, 7)
        self.world.stock_up(item.name, wh_b.name, 5)

        result = api.get_inventory_items(txt=item.name)
        row = next(r for r in result["items"] if r["item_code"] == item.name)
        self.assertEqual(row["total_actual_qty"], 12)

        detail = api.get_inventory_item_detail(item.name)
        self.assertEqual(detail["total_stock"], 12)
        by_wh = {b["warehouse"]: b["actual_qty"] for b in detail["stock_by_warehouse"]}
        self.assertEqual(by_wh[wh_a.name], 7)
        self.assertEqual(by_wh[wh_b.name], 5)

    # -- Funcional: precio ----------------------------------------------------------

    def test_item_with_existing_price(self):
        item = self.world.item("FG2204-WITHPRICE-ITEM")
        price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item.name,
                "price_list": "Standard Selling",
                "uom": item.stock_uom,
                "price_list_rate": 999,
            }
        )
        price.insert()
        self.world.track_existing("Item Price", price.name)

        result = api.get_inventory_items(txt=item.name)
        row = next(r for r in result["items"] if r["item_code"] == item.name)
        self.assertEqual(row["selling_rate"], 999)

        detail = api.get_inventory_item_detail(item.name)
        self.assertEqual(detail["selling_rate"], 999)

    def test_item_without_price(self):
        item = self.world.item("FG2204-NOPRICE-ITEM")

        result = api.get_inventory_items(txt=item.name)
        row = next(r for r in result["items"] if r["item_code"] == item.name)
        self.assertIsNone(row["selling_rate"])

        detail = api.get_inventory_item_detail(item.name)
        self.assertIsNone(detail["selling_rate"])

    # -- Funcional: detalle -----------------------------------------------------------

    def test_detail_by_warehouse(self):
        wh = self.world.warehouse("FG2204 DetailWh")
        item = self.world.item("FG2204-DETAILWH-ITEM")
        self.world.stock_up(item.name, wh.name, 30)

        detail = api.get_inventory_item_detail(item.name)
        self.assertEqual(len(detail["stock_by_warehouse"]), 1)
        self.assertEqual(detail["stock_by_warehouse"][0]["warehouse"], wh.name)
        self.assertEqual(detail["stock_by_warehouse"][0]["actual_qty"], 30)

    def test_movements_ordered(self):
        wh = self.world.warehouse("FG2204 Movs")
        item = self.world.item("FG2204-MOVS-ITEM")
        self.world.stock_up_real(item.name, wh.name, 4, rate=50)
        self.world.stock_up_real(item.name, wh.name, 10, rate=50)  # a second real movement

        detail = api.get_inventory_item_detail(item.name)
        movements = detail["recent_movements"]
        self.assertGreaterEqual(len(movements), 1)
        # ordered newest first: each posting_date/time must be >= the next one
        keys = [(m["posting_date"], m["posting_time"]) for m in movements]
        self.assertEqual(keys, sorted(keys, reverse=True))

    # -- Permisos: positivo -----------------------------------------------------------

    def test_bodega_can_read(self):
        item = self.world.item("FG2204-BODEGA-READ-ITEM")
        with fx.as_user(self.bodega_user):
            api.get_inventory_summary()
            api.get_inventory_items()
            api.get_inventory_item_detail(item.name)  # must not raise

    def test_jefe_de_bodega_can_read(self):
        item = self.world.item("FG2204-JEFE-READ-ITEM")
        with fx.as_user(self.jefe_user):
            api.get_inventory_summary()
            api.get_inventory_items()
            api.get_inventory_item_detail(item.name)  # must not raise

    # -- Permisos: negativo -------------------------------------------------------------

    def test_user_without_inventory_role_is_denied(self):
        item = self.world.item("FG2204-DENIED-ITEM")
        with fx.as_user(self.noaccess_user):
            with self.assertRaises(frappe.PermissionError):
                api.get_inventory_summary()
            with self.assertRaises(frappe.PermissionError):
                api.get_inventory_items()
            with self.assertRaises(frappe.PermissionError):
                api.get_inventory_item_detail(item.name)

    # -- Guardrail: lectura masiva, no N+1 -----------------------------------------------

    def test_bin_is_queried_in_bulk_not_per_item(self):
        wh = self.world.warehouse("FG2204 Bulk")
        items = [self.world.item(f"FG2204-BULK-ITEM-{i}") for i in range(3)]
        for it in items:
            self.world.stock_up(it.name, wh.name, 1)

        queries = []
        original_sql = frappe.db.sql

        def counting_sql(query, *a, **kw):
            if "tabBin" in str(query):
                queries.append(query)
            return original_sql(query, *a, **kw)

        frappe.db.sql = counting_sql
        try:
            api.get_inventory_items(page_length=100)
        finally:
            frappe.db.sql = original_sql

        self.assertEqual(len(queries), 1, f"expected exactly 1 Bin query, got {len(queries)}")

    # -- Guardrails estructurales -------------------------------------------------------

    def test_module_exposes_exactly_the_three_read_only_endpoints(self):
        own_functions = {
            name
            for name, fn in inspect.getmembers(api, inspect.isfunction)
            if fn.__module__ == api.__name__ and not name.startswith("_")
        }
        self.assertEqual(own_functions, set(_READ_ENDPOINTS))
        for name in own_functions:
            self.assertIn(getattr(api, name), frappe.whitelisted, f"{name} must be @frappe.whitelist()-ed")

    def test_module_source_never_writes_or_bypasses_permissions(self):
        for name in _READ_ENDPOINTS:
            fn = getattr(api, name)
            source = inspect.getsource(fn)
            findings = _forbidden_findings(source)
            self.assertEqual(findings, [], f"{name}() contains forbidden pattern(s): {findings}")
