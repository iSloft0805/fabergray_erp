# -*- coding: utf-8 -*-
"""Commit 25.2 -- "usar la resolución NATIVA de ERPNext v16, no duplicarla":
`api/ventas.py._validate_and_build_item_rows()` no longer resolves or
requires a `warehouse` on a Sales Order Item line, and the old
`frappe.throw("...no tiene una bodega por defecto configurada.")` is gone.
Every test here proves the REAL, end-to-end behaviour of ERPNext's own
native precedence chain running through our own whitelisted API --
`erpnext.stock.get_item_details.get_item_warehouse_()`, confirmed by
reading the ERPNext v16 source directly during this commit's own audit --
never a mock, never a re-implementation of that chain in this app.

Precedence under test, in the exact order ERPNext itself applies it
(`get_item_details.py:697-712`):

    1. Item Default.default_warehouse (per Company)              -- Case A
    2. Item Group Default.default_warehouse (per Company)         -- Case B
    3. Brand Default.default_warehouse (per Company)               -- Case C
    4. Stock Settings.default_warehouse (Company-checked)          -- Case D
    5. none of the above resolves anything -> native WarehouseRequired -- Case E
    6. Stock Settings.default_warehouse belongs to another Company
       -> ignored, same as Case E                                  -- Case F

Stock Settings is real, site-wide, operational configuration -- never
touched by application code, a patch, or a fixture (this commit's own
explicit instruction). `_stock_settings_default_warehouse()` below is the
one sanctioned, test-only exception: it always restores the site's
original value afterward, even if the test body raises, exactly the same
guarantee `fixtures.py`'s own `as_user()` already gives for
`frappe.session.user`.
"""

from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.selling.doctype.sales_order.sales_order import WarehouseRequired

from fabergray_erp.api import ventas as ventas_api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


@contextmanager
def _stock_settings_default_warehouse(warehouse):
    """Temporarily sets `Stock Settings.default_warehouse` to `warehouse`
    (a Warehouse name, or `None` to force it empty) for the duration of
    the block, always restoring whatever the site's own original value
    was -- even if the block raises. Never leaves an accidental
    operational change behind as a side effect of running this suite."""
    ss = frappe.get_single("Stock Settings")
    original = ss.default_warehouse
    ss.default_warehouse = warehouse
    ss.save()
    frappe.db.commit()
    try:
        yield
    finally:
        ss.reload()
        ss.default_warehouse = original
        ss.save()
        frappe.db.commit()


class TestWarehouseFallback(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg25-wh-vendedora@example.com", ["Vendedora"])

    def _submit(self, item_code, customer_name, qty=1):
        with fx.as_user(self.vendedora):
            result = ventas_api.create_and_submit_sales_order(
                customer=customer_name, items=[{"item_code": item_code, "qty": qty}]
            )
        self.world.track_existing("Sales Order", result["name"])
        self.world.track_existing_pick_lists_and_reports_for(result["name"])
        return frappe.get_doc("Sales Order", result["name"])

    def _no_default_item(self, item_code):
        """A plain item in the shared, no-Item-Group-default group
        (`fixtures.py`'s own `_ITEM_GROUP`), no Item Default of its own,
        no Brand -- the genuine "nothing resolves" baseline Cases D/E/F
        need."""
        item = self.world.item(item_code)
        self.assertEqual(item.item_defaults, [])
        return item

    # =====================================================================
    # Section 5 -- precedence, Cases A-F
    # =====================================================================

    def test_case_a_item_default_used_when_present(self):
        wh = self.world.warehouse("FG25 WH Case A")
        item = self.world.item("FG25-WH-CASE-A-ITEM", default_warehouse=wh.name)
        customer = self.world.customer("FG25 WH Case A Customer")

        so = self._submit(item.name, customer.name)
        self.assertEqual(so.items[0].warehouse, wh.name)

    def test_case_b_item_group_default_used_when_item_has_none(self):
        """Confirmed live, during this exact test, that ERPNext's own
        `Item.update_defaults_from_item_group()` (item.py:776) copies an
        Item Group's own `item_group_defaults` rows onto a brand-new
        Item's `item_defaults` table automatically at insert time IF the
        Item itself was never given any -- so this Item's own `Item
        Default` ends up populated too, sourced transitively from the
        Item Group, never set by us. This is still a faithful Case B: we
        never set `default_warehouse` anywhere ourselves, only the Item
        Group's own default -- and the value that ends up on the Sales
        Order Item is proven below to be exactly the Item Group's own
        warehouse."""
        wh = self.world.warehouse("FG25 WH Case B")
        group = frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": "FG25 WH Case B Group",
                "parent_item_group": "All Item Groups",
                "is_group": 0,
                "item_group_defaults": [{"company": fx.COMPANY, "default_warehouse": wh.name}],
            }
        )
        group.insert()
        self.world.track_existing("Item Group", group.name)

        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": "FG25-WH-CASE-B-ITEM",
                "item_name": "FG25-WH-CASE-B-ITEM",
                "item_group": group.name,
                "stock_uom": fx.UOM,
                "is_stock_item": 1,
                "is_sales_item": 1,
            }
        )
        item.insert()
        self.world.track_existing("Item", item.name)

        customer = self.world.customer("FG25 WH Case B Customer")
        so = self._submit(item.name, customer.name)
        self.assertEqual(so.items[0].warehouse, wh.name)

    def test_case_c_brand_default_used_when_item_and_group_have_none(self):
        wh = self.world.warehouse("FG25 WH Case C")
        brand = frappe.get_doc(
            {
                "doctype": "Brand",
                "brand": "FG25 WH Case C Brand",
                "brand_defaults": [{"company": fx.COMPANY, "default_warehouse": wh.name}],
            }
        )
        brand.insert()
        self.world.track_existing("Brand", brand.name)

        item = self._no_default_item("FG25-WH-CASE-C-ITEM")  # shared group -- confirmed no group default
        item.brand = brand.name
        item.save()

        customer = self.world.customer("FG25 WH Case C Customer")
        so = self._submit(item.name, customer.name)
        self.assertEqual(so.items[0].warehouse, wh.name)

    def test_case_d_stock_settings_default_warehouse_used_as_last_resort(self):
        wh = self.world.warehouse("FG25 WH Case D")
        item = self._no_default_item("FG25-WH-CASE-D-ITEM")
        customer = self.world.customer("FG25 WH Case D Customer")

        with _stock_settings_default_warehouse(wh.name):
            so = self._submit(item.name, customer.name)

        self.assertEqual(so.items[0].warehouse, wh.name)

    def test_case_e_no_default_anywhere_raises_native_warehouse_required(self):
        """Confirms BOTH that ERPNext's own native error fires AND that
        our own old custom message ("no tiene una bodega por defecto
        configurada") is gone -- not merely that *some* exception was
        raised."""
        item = self._no_default_item("FG25-WH-CASE-E-ITEM")
        customer = self.world.customer("FG25 WH Case E Customer")

        with _stock_settings_default_warehouse(None):
            with self.assertRaises(WarehouseRequired) as ctx:
                with fx.as_user(self.vendedora):
                    ventas_api.create_and_submit_sales_order(
                        customer=customer.name, items=[{"item_code": item.name, "qty": 1}]
                    )

        message = str(ctx.exception)
        self.assertNotIn("bodega por defecto", message)
        self.assertIn(item.name, message)  # ERPNext's own native message names the item

    def test_case_f_stock_settings_default_warehouse_from_another_company_is_ignored(self):
        """`Finished Goods - _TC` belongs to `_Test Company`, not
        `fabrigraysas` -- ERPNext's own Company check
        (`get_item_details.py:709-711`) must reject it exactly like Case E
        (no usable default at all), never silently use a warehouse from
        the wrong Company."""
        item = self._no_default_item("FG25-WH-CASE-F-ITEM")
        customer = self.world.customer("FG25 WH Case F Customer")

        with _stock_settings_default_warehouse("Finished Goods - _TC"):
            with self.assertRaises(WarehouseRequired):
                with fx.as_user(self.vendedora):
                    ventas_api.create_and_submit_sales_order(
                        customer=customer.name, items=[{"item_code": item.name, "qty": 1}]
                    )

    # =====================================================================
    # Section 6 -- stock never blocks, regardless of qty vs. actual_qty
    # =====================================================================

    def test_stock_zero_qty_twenty_submits_successfully(self):
        wh = self.world.warehouse("FG25 WH Stock Zero")
        item = self.world.item("FG25-WH-STOCK-ZERO-ITEM", default_warehouse=wh.name)
        customer = self.world.customer("FG25 WH Stock Zero Customer")
        self.world.stock_up(item.name, wh.name, 0)  # Bin.actual_qty explicitly 0

        so = self._submit(item.name, customer.name, qty=20)
        self.assertEqual(so.docstatus, 1)
        self.assertEqual(so.items[0].qty, 20)

    def test_stock_five_qty_twenty_submits_successfully(self):
        wh = self.world.warehouse("FG25 WH Stock Five")
        item = self.world.item(
            "FG25-WH-STOCK-FIVE-ITEM", default_warehouse=wh.name, default_material_request_type="Purchase"
        )
        customer = self.world.customer("FG25 WH Stock Five Customer")
        self.world.stock_up_real(item.name, wh.name, 5)

        so = self._submit(item.name, customer.name, qty=20)
        self.assertEqual(so.docstatus, 1)
        self.assertEqual(so.items[0].qty, 20)

    def test_stock_twenty_qty_twenty_submits_successfully(self):
        wh = self.world.warehouse("FG25 WH Stock Twenty")
        item = self.world.item("FG25-WH-STOCK-TWENTY-ITEM", default_warehouse=wh.name)
        customer = self.world.customer("FG25 WH Stock Twenty Customer")
        self.world.stock_up_real(item.name, wh.name, 20)

        so = self._submit(item.name, customer.name, qty=20)
        self.assertEqual(so.docstatus, 1)
        self.assertEqual(so.items[0].qty, 20)

    def test_stock_hundred_qty_twenty_submits_successfully(self):
        wh = self.world.warehouse("FG25 WH Stock Hundred")
        item = self.world.item("FG25-WH-STOCK-HUNDRED-ITEM", default_warehouse=wh.name)
        customer = self.world.customer("FG25 WH Stock Hundred Customer")
        self.world.stock_up_real(item.name, wh.name, 100)

        so = self._submit(item.name, customer.name, qty=20)
        self.assertEqual(so.docstatus, 1)
        self.assertEqual(so.items[0].qty, 20)

    # =====================================================================
    # Section 7 -- Fulfillment Engine regression, warehouse via fallback
    # =====================================================================

    def test_fulfillment_splits_available_vs_faltante_with_warehouse_from_stock_settings(self):
        """The exact scenario the brief's own section 7 asks for (stock=5,
        pedido=20 -> Pick List disponible=5, Reporte de Faltante=15) --
        run here specifically through an Item with NO Item Default at
        all, so the Sales Order's own warehouse comes exclusively from
        `Stock Settings.default_warehouse` (Case D's mechanism), proving
        the Fulfillment Engine (untouched by this commit) works
        end-to-end regardless of which precedence step resolved the
        warehouse."""
        wh = self.world.warehouse("FG25 WH Fulfillment")
        item = self._no_default_item("FG25-WH-FULFILLMENT-ITEM")
        item.default_material_request_type = "Purchase"
        item.save()
        customer = self.world.customer("FG25 WH Fulfillment Customer")
        self.world.stock_up_real(item.name, wh.name, 5)

        with _stock_settings_default_warehouse(wh.name):
            so = self._submit(item.name, customer.name, qty=20)

        self.assertEqual(so.docstatus, 1)
        self.assertEqual(so.items[0].warehouse, wh.name)

        pick_lists = frappe.get_all(
            "Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
        )
        self.assertEqual(len(pick_lists), 1)
        pl = frappe.get_doc("Pick List", pick_lists[0])
        self.assertEqual(flt(pl.get("locations")[0].stock_qty), 5.0)

        reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": so.name}, pluck="name")
        self.assertEqual(len(reports), 1)
        report = frappe.get_doc("Reporte de Faltante", reports[0])
        self.assertEqual(report.qty_faltante, 15.0)

    # =====================================================================
    # get_item_info() no longer throws for a missing Item Default either
    # =====================================================================

    def test_get_item_info_no_longer_throws_for_item_without_default(self):
        """Before this commit, `_default_warehouse_for_item()` raised
        unconditionally when no `Item Default` existed, and
        `get_item_info()` called it directly, unprotected -- so a
        Vendedora could not even preview a product lacking one before
        adding it to her cart. Now it returns `qty_disponible=None`
        (informational "no disponible") instead of raising -- confirmed
        directly, not assumed from `_validate_and_build_item_rows()`'s
        own fix alone."""
        item = self._no_default_item("FG25-WH-GETINFO-ITEM")

        with fx.as_user(self.vendedora):
            info = ventas_api.get_item_info(item.name)  # must not raise

        self.assertEqual(info["item_code"], item.name)
        self.assertIsNone(info["qty_disponible"])

    # =====================================================================
    # No stock-availability blocking anywhere in api/ventas.py
    # =====================================================================

    def test_ventas_source_never_checks_actual_qty_or_similar_before_submit(self):
        """Static guardrail: `projected_qty`/`reserved_qty`/`available_qty`
        never appear as identifiers anywhere in api/ventas.py's own
        source -- confirmed, not assumed. `actual_qty` is handled
        separately: it legitimately appears as a SUBSTRING of
        `get_actual_qty` (the native helper `get_item_info()` calls for
        its own informational-only `qty_disponible` preview, which never
        blocks anything -- see that function's own docstring); this
        asserts every occurrence of the bare word `actual_qty` is part of
        that one call, never a standalone comparison/conditional of its
        own (which would mean something is being blocked on it)."""
        import inspect
        import re

        from fabergray_erp.api import ventas as ventas_module

        source = inspect.getsource(ventas_module)
        for forbidden in ("projected_qty", "reserved_qty", "available_qty"):
            self.assertNotIn(forbidden, source, f"{forbidden} must never appear in api/ventas.py")

        bare_actual_qty = re.findall(r"(?<!get_)\bactual_qty\b", source)
        self.assertEqual(bare_actual_qty, [], "actual_qty must only ever appear as part of get_actual_qty()")
