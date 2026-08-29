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
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import inventario as api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_READ_ENDPOINTS = ("get_inventory_summary", "get_inventory_items", "get_inventory_item_detail")

# Commit 22.6 -- the three write endpoints. Deliberately checked against
# the exact same _FORBIDDEN_CALLS list as the read endpoints below (never
# ignore_permissions=, frappe.set_user/get_all/db.commit/db.sql anywhere in
# this module, writes included) plus a dedicated direct-write-to-Bin/SLE/GL
# guardrail (test_module_never_writes_directly_to_bin_sle_or_gl below).
_WRITE_ENDPOINTS = ("record_opening_count", "adjust_item_quantity", "update_item_master")

_ALL_ENDPOINTS = _READ_ENDPOINTS + _WRITE_ENDPOINTS

_FORBIDDEN_CALLS = {"frappe.set_user", "frappe.get_all", "frappe.db.commit", "frappe.db.sql"}

_DIRECT_WRITE_DOCTYPES = {"Bin", "Stock Ledger Entry", "GL Entry"}


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


def _direct_write_findings(source):
    """Commit 22.6 guardrail: walks the AST for any frappe.get_doc({...})/
    frappe.new_doc(...)/frappe.db.set_value(...) call that targets Bin,
    Stock Ledger Entry or GL Entry directly -- the three things this module
    must only ever change through a native Stock Reconciliation. Read-only
    use of these doctype names (frappe.get_list(...) filters, already
    present in the Commit 22.4 read endpoints and in _get_current_qty())
    is untouched -- only a literal "doctype" dict key or a
    frappe.db.set_value("<Doctype>", ...) first argument is flagged."""
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)

        if dotted in ("frappe.get_doc", "frappe.new_doc") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Dict):
                for key, value in zip(first.keys, first.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "doctype"
                        and isinstance(value, ast.Constant)
                        and value.value in _DIRECT_WRITE_DOCTYPES
                    ):
                        findings.append(f"frappe.get_doc/new_doc(doctype={value.value!r})")
            elif isinstance(first, ast.Constant) and first.value in _DIRECT_WRITE_DOCTYPES:
                findings.append(f"{dotted}({first.value!r}, ...)")

        if dotted == "frappe.db.set_value" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in _DIRECT_WRITE_DOCTYPES:
                findings.append(f"frappe.db.set_value({first.value!r}, ...)")

    return findings


class TestInventarioApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        # Commit 22.6: a fresh per-class suffix for every FG2206-* item/warehouse
        # name below -- a run that fails partway through (e.g. Stock
        # Reconciliation's own db_update()-before-post-save-hooks ordering can
        # leave a real docstatus=1 document even when submit() raises, see
        # fabrigray-orphan-stock-reco-residue memory) must never poison the
        # exact same names for the NEXT run the way two earlier attempts at
        # writing this suite already did.
        cls.run_id = frappe.generate_hash(length=6)
        cls.bodega_user = cls.world.user("fg2204-bodega@example.com", ["Bodega"])
        cls.jefe_user = cls.world.user("fg2204-jefe@example.com", ["Jefe de Bodega"])
        cls.noaccess_user = cls.world.user("fg2204-noaccess@example.com", ["Gestión de Clientes"])
        # Commit 22.6
        cls.sysmanager_user = cls.world.user("fg2206-sysmanager@example.com", ["System Manager"])

    # -- Commit 22.6: tracked wrappers -----------------------------------------------------
    # api.record_opening_count()/adjust_item_quantity() create a real, submitted
    # Stock Reconciliation as a side effect -- calling the bare api.* function
    # directly (as every earlier read-only test in this file safely could)
    # would leave it out of self.world._created, and cleanup() force-deleting
    # the tracked Item/Warehouse without ever cancelling that Stock
    # Reconciliation first leaves it permanently orphaned (confirmed the hard
    # way -- see fabrigray-orphan-stock-reco-residue memory, "second batch").
    # Every test below that expects a call to SUCCEED goes through one of
    # these three wrappers instead of calling api.* directly; a call that is
    # itself the one under assertRaises() never reaches insert() and needs no
    # tracking.

    def _record_opening_count(self, *args, **kwargs):
        result = api.record_opening_count(*args, **kwargs)
        self.world.track_existing("Stock Reconciliation", result["stock_reconciliation"])
        self._track_item_price(result["item_code"], "Standard Buying")
        return result

    def _adjust_item_quantity(self, *args, **kwargs):
        result = api.adjust_item_quantity(*args, **kwargs)
        self.world.track_existing("Stock Reconciliation", result["stock_reconciliation"])
        return result

    def _update_item_master(self, item_code, **kwargs):
        result = api.update_item_master(item_code, **kwargs)
        self._track_item_price(item_code, "Standard Buying")
        self._track_item_price(item_code, "Standard Selling")
        return result

    def _track_item_price(self, item_code, price_list):
        name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list}, "name")
        if name:
            self.world.track_existing("Item Price", name)

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

    def test_module_exposes_exactly_the_expected_public_endpoints(self):
        """Commit 22.6: 3 read + 3 write, nothing else public."""
        own_functions = {
            name
            for name, fn in inspect.getmembers(api, inspect.isfunction)
            if fn.__module__ == api.__name__ and not name.startswith("_")
        }
        self.assertEqual(own_functions, set(_ALL_ENDPOINTS))
        for name in own_functions:
            self.assertIn(getattr(api, name), frappe.whitelisted, f"{name} must be @frappe.whitelist()-ed")

    def test_module_source_never_writes_or_bypasses_permissions(self):
        for name in _ALL_ENDPOINTS:
            fn = getattr(api, name)
            source = inspect.getsource(fn)
            findings = _forbidden_findings(source)
            self.assertEqual(findings, [], f"{name}() contains forbidden pattern(s): {findings}")

    def test_module_never_calls_db_commit_anywhere(self):
        """Commit 22.6: scans the WHOLE module source (public endpoints AND
        private helpers like _prepare_quantity_adjustment/_upsert_item_price/
        _get_and_lock_item), not just the public functions -- a helper could
        just as easily hide a frappe.db.commit() that would break the
        request-boundary atomicity every write endpoint relies on."""
        source = inspect.getsource(api)
        findings = [f for f in _forbidden_findings(source) if f == "frappe.db.commit"]
        self.assertEqual(findings, [], f"module contains frappe.db.commit(): {findings}")

    def test_module_never_writes_directly_to_bin_sle_or_gl(self):
        """Commit 22.6: every quantity change must go exclusively through a
        native Stock Reconciliation -- this module must never build/insert/
        set_value a Bin, Stock Ledger Entry or GL Entry document itself.
        Read-only frappe.get_list() filters against these doctypes (already
        used by the Commit 22.4 endpoints and by _get_current_qty()) are not
        flagged -- only a literal write target is."""
        source = inspect.getsource(api)
        findings = _direct_write_findings(source)
        self.assertEqual(findings, [], f"module writes directly to Bin/SLE/GL: {findings}")

    # =====================================================================
    # Commit 22.6 -- inventario editable
    # =====================================================================

    # -- Primer conteo (Opening Stock) ---------------------------------------------------

    def test_opening_count_zero_to_48(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} OpeningWh")
        item = self.world.item(f"FG2206-{self.run_id}-OPENING-ITEM")

        result = self._record_opening_count(
            item_code=item.name,
            warehouse=wh.name,
            qty=48,
            reason="Conteo físico inicial",
            expected_current_qty=0,
            purchase_rate=100,
        )

        self.assertEqual(result["previous_qty"], 0)
        self.assertEqual(result["new_qty"], 48)
        self.assertEqual(result["difference"], 48)
        self.assertEqual(api._get_current_qty(item.name, wh.name), 48)

        sr = frappe.get_doc("Stock Reconciliation", result["stock_reconciliation"])
        self.assertEqual(sr.purpose, "Opening Stock")
        self.assertEqual(sr.fg_adjustment_reason, "Conteo físico inicial")
        self.assertTrue(api._has_opening_stock(item.name, wh.name))

    def test_opening_count_with_purchase_rate_sets_native_valuation(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} OpeningRateWh")
        item = self.world.item(f"FG2206-{self.run_id}-OPENINGRATE-ITEM")

        result = self._record_opening_count(
            item_code=item.name,
            warehouse=wh.name,
            qty=48,
            reason="Conteo físico inicial",
            expected_current_qty=0,
            purchase_rate=23000,
        )

        # Standard Buying was updated first, and no valuation_rate was ever
        # sent -- Stock Reconciliation's own native fallback (validate_data())
        # picked it up on its own.
        self.assertEqual(
            frappe.db.get_value(
                "Item Price", {"item_code": item.name, "price_list": "Standard Buying"}, "price_list_rate"
            ),
            23000,
        )
        self.assertEqual(result["valuation_rate"], 23000)

        sr = frappe.get_doc("Stock Reconciliation", result["stock_reconciliation"])
        self.assertEqual(flt(sr.items[0].valuation_rate), 23000)
        self.assertEqual(flt(sr.items[0].amount), 48 * 23000)

        bin_row = frappe.get_list(
            "Bin", filters={"item_code": item.name, "warehouse": wh.name}, fields=["actual_qty", "stock_value"]
        )[0]
        self.assertEqual(flt(bin_row.actual_qty), 48)
        self.assertEqual(flt(bin_row.stock_value), 48 * 23000)

    def test_record_opening_count_rolls_back_item_price_if_reconciliation_fails(self):
        """Same proof pattern already established in this app
        (test_sales_order_hook.py's own rollback tests): force a real
        exception AFTER the Item Price write already happened (patching
        get_difference_account, called right after the purchase_rate
        branch in record_opening_count()), then simulate the real
        request-boundary rollback explicitly (bench run-tests never goes
        through a real HTTP request) and confirm the Item Price write did
        not survive it either."""
        wh = self.world.warehouse(f"FG2206 {self.run_id} RollbackWh")
        item = self.world.item(f"FG2206-{self.run_id}-ROLLBACK-ITEM")
        frappe.db.commit()  # fixtures survive the rollback below

        with patch(
            "fabergray_erp.api.inventario.get_difference_account",
            side_effect=RuntimeError("Commit 22.6 intentional failure after Item Price write"),
        ):
            with self.assertRaises(RuntimeError):
                api.record_opening_count(
                    item_code=item.name,
                    warehouse=wh.name,
                    qty=48,
                    reason="Conteo físico inicial",
                    expected_current_qty=0,
                    purchase_rate=23000,
                )

        frappe.db.rollback()

        self.assertIsNone(
            frappe.db.get_value("Item Price", {"item_code": item.name, "price_list": "Standard Buying"}, "name")
        )
        self.assertFalse(api._has_opening_stock(item.name, wh.name))

    def test_second_opening_count_same_item_warehouse_rejected(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} DupOpenWh")
        item = self.world.item(f"FG2206-{self.run_id}-DUPOPEN-ITEM")

        self._record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0, purchase_rate=100)

        with self.assertRaises(api.OpeningStockAlreadyDoneError):
            api.record_opening_count(item.name, wh.name, 5, "Segundo intento", 10)

    def test_opening_count_rejects_when_live_qty_is_not_zero(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} NonZeroOpenWh")
        item = self.world.item(f"FG2206-{self.run_id}-NONZEROOPEN-ITEM")
        self.world.stock_up(item.name, wh.name, 7)  # Bin-only, no Opening Stock evidence

        with self.assertRaises(api.NonZeroOpeningStockError):
            api.record_opening_count(item.name, wh.name, 48, "Conteo inicial", 7)

    def test_opening_count_without_any_valuation_reference_rejected(self):
        """Real, verified native behaviour (see _resolve_opening_valuation_rate()'s
        own docstring): a first-ever count with no purchase_rate, no
        existing Standard Buying price, and no Item.valuation_rate has no
        valid valuation to submit with -- must be rejected clearly by this
        module, never silently submitted as a zero valuation."""
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} NoValRefWh")
        item = self.world.item(f"FG2206-{self.run_id}-NOVALREF-ITEM")

        with self.assertRaises(api.PurchaseRateRequiredForOpeningError):
            api.record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0)

    def test_cancelled_opening_stock_document_does_not_count_as_initialized(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} CancelOpenWh")
        item = self.world.item(f"FG2206-{self.run_id}-CANCELOPEN-ITEM")

        result = self._record_opening_count(item.name, wh.name, 20, "Conteo inicial", 0, purchase_rate=100)
        self.assertTrue(api._has_opening_stock(item.name, wh.name))

        sr = frappe.get_doc("Stock Reconciliation", result["stock_reconciliation"])
        sr.cancel()

        self.assertFalse(api._has_opening_stock(item.name, wh.name))
        # and record_opening_count() is usable again for this same pair --
        # no purchase_rate needed this time, Standard Buying from the first
        # (now-cancelled) attempt is still there to fall back to.
        result2 = self._record_opening_count(item.name, wh.name, 15, "Segundo conteo inicial real", 0)
        self.assertEqual(result2["new_qty"], 15)

    # -- Ajuste posterior (Stock Reconciliation) -----------------------------------------

    def test_adjust_quantity_48_to_45(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} AdjustWh")
        item = self.world.item(f"FG2206-{self.run_id}-ADJUST-ITEM")
        self._record_opening_count(item.name, wh.name, 48, "Conteo inicial", 0, purchase_rate=100)

        with fx.company_defaults(stock_adjustment_account=fx.STOCK_ADJUSTMENT_ACCOUNT):
            result = self._adjust_item_quantity(
                item_code=item.name,
                warehouse=wh.name,
                qty=45,
                reason="Merma detectada en conteo",
                expected_current_qty=48,
            )

        self.assertEqual(result["previous_qty"], 48)
        self.assertEqual(result["new_qty"], 45)
        self.assertEqual(result["difference"], -3)
        self.assertEqual(api._get_current_qty(item.name, wh.name), 45)

    def test_adjust_quantity_preserves_existing_valuation(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} KeepValWh")
        item = self.world.item(f"FG2206-{self.run_id}-KEEPVAL-ITEM")
        self._record_opening_count(item.name, wh.name, 48, "Conteo inicial", 0, purchase_rate=23000)

        with fx.company_defaults(stock_adjustment_account=fx.STOCK_ADJUSTMENT_ACCOUNT):
            self._adjust_item_quantity(item.name, wh.name, 45, "Ajuste posterior", 48)

        bin_row = frappe.get_list(
            "Bin", filters={"item_code": item.name, "warehouse": wh.name}, fields=["valuation_rate"]
        )[0]
        self.assertEqual(flt(bin_row.valuation_rate), 23000)  # unchanged by the qty-only adjustment

    def test_48_to_0_to_10_is_adjustment_not_a_new_opening(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} CycleWh")
        item = self.world.item(f"FG2206-{self.run_id}-CYCLE-ITEM")
        with fx.company_defaults(stock_adjustment_account=fx.STOCK_ADJUSTMENT_ACCOUNT):
            self._record_opening_count(item.name, wh.name, 48, "Conteo inicial", 0, purchase_rate=100)
            self._adjust_item_quantity(item.name, wh.name, 0, "Se agotó", 48)

            with self.assertRaises(api.OpeningStockAlreadyDoneError):
                api.record_opening_count(item.name, wh.name, 10, "Reingreso", 0)

            result = self._adjust_item_quantity(item.name, wh.name, 10, "Reingreso de mercancía", 0)
            self.assertEqual(result["new_qty"], 10)
            sr = frappe.get_doc("Stock Reconciliation", result["stock_reconciliation"])
            self.assertEqual(sr.purpose, "Stock Reconciliation")

    def test_adjust_quantity_requires_opening_stock_first(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} NoOpenWh")
        item = self.world.item(f"FG2206-{self.run_id}-NOOPEN-ITEM")

        with self.assertRaises(api.OpeningStockRequiredError):
            api.adjust_item_quantity(item.name, wh.name, 10, "Ajuste", 0)

    # -- Validaciones compartidas ---------------------------------------------------------

    def test_negative_qty_rejected(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} NegWh")
        item = self.world.item(f"FG2206-{self.run_id}-NEG-ITEM")

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, wh.name, -1, "Conteo", 0)
        with self.assertRaises(frappe.ValidationError):
            api.adjust_item_quantity(item.name, wh.name, -1, "Ajuste", 0)

    def test_reason_required(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} ReasonWh")
        item = self.world.item(f"FG2206-{self.run_id}-REASON-ITEM")

        with self.assertRaises(api.AdjustmentReasonRequiredError):
            api.record_opening_count(item.name, wh.name, 5, "   ", 0)

    def test_stale_expected_current_qty_rejected(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} StaleWh")
        item = self.world.item(f"FG2206-{self.run_id}-STALE-ITEM")
        self._record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0, purchase_rate=100)

        with self.assertRaises(api.StaleInventoryStateError):
            api.adjust_item_quantity(item.name, wh.name, 5, "Ajuste", expected_current_qty=999)

    def test_disabled_item_rejected(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} DisabledItemWh")
        item = self.world.item(f"FG2206-{self.run_id}-DISABLEDITEM-ITEM")
        frappe.db.set_value("Item", item.name, "disabled", 1)

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, wh.name, 5, "Conteo", 0)

    def test_non_stock_item_rejected(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} NonStockWh")
        item = self.world.item(f"FG2206-{self.run_id}-NONSTOCK-ITEM")
        frappe.db.set_value("Item", item.name, "is_stock_item", 0)

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, wh.name, 5, "Conteo", 0)

    def test_group_warehouse_rejected(self):
        item = self.world.item(f"FG2206-{self.run_id}-GROUPWH-ITEM")
        group_wh = frappe.db.get_value("Warehouse", {"is_group": 1, "company": fx.COMPANY}, "name")
        self.assertTrue(group_wh, "expected at least one group Warehouse under fabrigraysas")

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, group_wh, 5, "Conteo", 0)

    def test_disabled_warehouse_rejected(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} DisabledWhTest")
        item = self.world.item(f"FG2206-{self.run_id}-DISABLEDWH-ITEM")
        frappe.db.set_value("Warehouse", wh.name, "disabled", 1)

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, wh.name, 5, "Conteo", 0)

    def test_warehouse_from_another_company_rejected(self):
        self.world.temporary_opening_account()
        item = self.world.item(f"FG2206-{self.run_id}-OTHERCOMPANY-ITEM")
        other_wh = frappe.get_doc(
            {"doctype": "Warehouse", "warehouse_name": f"FG2206 {self.run_id} OtherCompanyWh", "company": "_Test Company"}
        )
        other_wh.insert()
        self.world.track_existing("Warehouse", other_wh.name)

        with self.assertRaises(frappe.ValidationError):
            api.record_opening_count(item.name, other_wh.name, 5, "Conteo", 0)

    def test_adjustment_requires_stock_adjustment_account(self):
        """Real, unconfigured site state (Company.stock_adjustment_account
        is empty) -- confirmed live before this commit, and this test does
        NOT set one. get_difference_account("Stock Reconciliation", ...)
        must fail loudly and functionally (native get_company_default()
        throw), never fall back to any specific account."""
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} NoAdjAcctWh")
        item = self.world.item(f"FG2206-{self.run_id}-NOADJACCT-ITEM")
        self._record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0, purchase_rate=100)

        self.assertIsNone(frappe.db.get_value("Company", fx.COMPANY, "stock_adjustment_account"))
        with self.assertRaises(frappe.ValidationError):
            api.adjust_item_quantity(item.name, wh.name, 8, "Ajuste posterior", 10)

    # -- update_item_master() ------------------------------------------------------------

    def test_update_item_master_purchase_price_no_duplicates(self):
        item = self.world.item(f"FG2206-{self.run_id}-MASTERBUY-ITEM")

        self._update_item_master(item.name, purchase_rate=1000)
        self._update_item_master(item.name, purchase_rate=1200)

        rows = frappe.get_list(
            "Item Price", filters={"item_code": item.name, "price_list": "Standard Buying"}, fields=["price_list_rate"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(flt(rows[0].price_list_rate), 1200)

    def test_update_item_master_selling_price_no_duplicates(self):
        item = self.world.item(f"FG2206-{self.run_id}-MASTERSELL-ITEM")

        self._update_item_master(item.name, selling_rate=5000)
        self._update_item_master(item.name, selling_rate=5500)

        rows = frappe.get_list(
            "Item Price", filters={"item_code": item.name, "price_list": "Standard Selling"}, fields=["price_list_rate"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(flt(rows[0].price_list_rate), 5500)

    def test_update_item_master_item_group(self):
        item = self.world.item(f"FG2206-{self.run_id}-MASTERGROUP-ITEM")

        self._update_item_master(item.name, item_group="Productos Terminados")

        self.assertEqual(frappe.db.get_value("Item", item.name, "item_group"), "Productos Terminados")

    def test_changing_price_never_alters_stock(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} PriceNoStockWh")
        item = self.world.item(f"FG2206-{self.run_id}-PRICENOSTOCK-ITEM")
        self._record_opening_count(item.name, wh.name, 48, "Conteo inicial", 0, purchase_rate=100)

        self._update_item_master(item.name, purchase_rate=30000, selling_rate=45000)

        self.assertEqual(api._get_current_qty(item.name, wh.name), 48)

    # -- Permisos -------------------------------------------------------------------------

    def test_bodega_cannot_write(self):
        wh = self.world.warehouse(f"FG2206 {self.run_id} BodegaWriteWh")
        item = self.world.item(f"FG2206-{self.run_id}-BODEGAWRITE-ITEM")

        with fx.as_user(self.bodega_user):
            with self.assertRaises(frappe.PermissionError):
                api.record_opening_count(item.name, wh.name, 5, "Conteo", 0)
            with self.assertRaises(frappe.PermissionError):
                api.adjust_item_quantity(item.name, wh.name, 5, "Ajuste", 0)
            with self.assertRaises(frappe.PermissionError):
                api.update_item_master(item.name, purchase_rate=100)

    def test_jefe_de_bodega_can_write(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} JefeWriteWh")
        item = self.world.item(f"FG2206-{self.run_id}-JEFEWRITE-ITEM")

        with fx.company_defaults(stock_adjustment_account=fx.STOCK_ADJUSTMENT_ACCOUNT), fx.as_user(self.jefe_user):
            self._record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0, purchase_rate=500)
            self._adjust_item_quantity(item.name, wh.name, 8, "Ajuste posterior", 10)
            self._update_item_master(item.name, selling_rate=900, item_group="Productos Terminados")

        self.assertEqual(api._get_current_qty(item.name, wh.name), 8)

    def test_system_manager_can_write(self):
        self.world.temporary_opening_account()
        wh = self.world.warehouse(f"FG2206 {self.run_id} SysMgrWriteWh")
        item = self.world.item(f"FG2206-{self.run_id}-SYSMGRWRITE-ITEM")

        with fx.company_defaults(stock_adjustment_account=fx.STOCK_ADJUSTMENT_ACCOUNT), fx.as_user(
            self.sysmanager_user
        ):
            self._record_opening_count(item.name, wh.name, 6, "Conteo inicial", 0, purchase_rate=100)
            self._adjust_item_quantity(item.name, wh.name, 4, "Ajuste posterior", 6)
            self._update_item_master(item.name, purchase_rate=100)

        self.assertEqual(api._get_current_qty(item.name, wh.name), 4)


class TestInventarioMissingAccounts(IntegrationTestCase):
    """Isolated from TestInventarioApi's own TestWorld on purpose: any test
    that relies on "this company has NO Temporary-type account" must run
    with nothing else able to leave one lying around mid-suite (a
    temporary_opening_account() created by another test class's still-open
    TestWorld only gets cleaned up at THAT class's own teardown, which
    already happens before this class starts, but the precondition
    assertion below still guards against it explicitly rather than trusting
    ordering alone)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.run_id = frappe.generate_hash(length=6)

    def test_opening_count_fails_clearly_without_a_temporary_account(self):
        self.assertFalse(
            frappe.db.exists("Account", {"company": fx.COMPANY, "account_type": "Temporary", "is_group": 0}),
            "precondition failed: another test already left a Temporary account behind",
        )
        wh = self.world.warehouse(f"FG2206 {self.run_id} MissingOpenAcctWh")
        item = self.world.item(f"FG2206-{self.run_id}-MISSINGOPENACCT-ITEM")

        with self.assertRaises(api.MissingOpeningAccountError):
            api.record_opening_count(item.name, wh.name, 10, "Conteo inicial", 0, purchase_rate=100)
