# -*- coding: utf-8 -*-
"""Commit 25.4 -- direct tests for create_draft_sales_order()/confirm_order()
(api/ventas.py), the two endpoints behind "Nuevo Pedido"/"Confirmar pedido".

Written after an audit found these two functions had zero direct test
coverage -- every existing green test exercised the same on_submit hook
through create_and_submit_sales_order() (submit-immediately) or by
submitting a hand-built Sales Order directly, never through the Draft ->
Confirmar path a Vendedora actually uses in the real UI. This file closes
that gap; it does not change any production code.

Business rule under test throughout: Ventas creates the Draft and confirms
it -- Bodega decides what is genuinely missing, never the Fulfillment
Engine acting on theoretical stock. So confirm_order() must never leave a
Reporte de Faltante or Material Request behind, must never be blocked by
zero stock, and must be idempotent on a second call.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


def _pick_lists_for(so_name):
    return frappe.get_all(
        "Pick List Item", filters={"sales_order": so_name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
    )


def _reports_for(so_name):
    return frappe.get_all("Reporte de Faltante", filters={"sales_order": so_name}, pluck="name")


def _material_requests_for(so_name):
    return frappe.get_all(
        "Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
    )


class TestVentasDraftConfirm(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)

        cls.wh = cls.world.warehouse("FG254 Draft Confirm")
        cls.item = cls.world.item("FG254-DC-ITEM", default_warehouse=cls.wh.name)
        cls.item_zero_stock = cls.world.item(
            "FG254-DC-ZERO-ITEM", default_material_request_type="Purchase", default_warehouse=cls.wh.name
        )
        cls.world.stock_up_real(cls.item.name, cls.wh.name, 20)
        cls.customer = cls.world.customer("FG254 Draft Confirm Customer")

        cls.vendedora = cls.world.user("fg254-vendedora@example.com", ["Vendedora"])
        cls.no_role_user = cls.world.user("fg254-norole@example.com", [])

    def _track_draft(self, result):
        self.world.track_existing("Sales Order", result["name"])
        return result["name"]

    # -- create_draft_sales_order() ------------------------------------------

    def test_1_creates_sales_order_in_draft(self):
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 4}]
            )
        so_name = self._track_draft(result)

        self.assertEqual(result["docstatus"], 0)
        self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 0)

    def test_2_creates_no_pick_list(self):
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 4}]
            )
        so_name = self._track_draft(result)

        self.assertEqual(_pick_lists_for(so_name), [])

    def test_3_creates_no_reporte_de_faltante(self):
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item_zero_stock.name, "qty": 4}]
            )
        so_name = self._track_draft(result)

        self.assertEqual(_reports_for(so_name), [])

    def test_4_creates_no_material_request(self):
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item_zero_stock.name, "qty": 4}]
            )
        so_name = self._track_draft(result)

        self.assertEqual(_material_requests_for(so_name), [])

    def test_5_user_without_permission_cannot_create_draft(self):
        with fx.as_user(self.no_role_user):
            with self.assertRaises(frappe.PermissionError):
                ventas.create_draft_sales_order(
                    customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 4}]
                )
        # Nothing was left behind by the rejected attempt.
        self.assertFalse(frappe.db.exists("Sales Order", {"customer": self.customer.name, "docstatus": 0,
                                                            "owner": self.no_role_user}))

    def test_modifying_a_draft_created_via_the_new_endpoint_works(self):
        """Item 2 of the requested checklist: "modificar Draft -> funciona",
        specifically through the create_draft_sales_order() -> update_draft_
        sales_order() pair a Vendedora actually uses (Nuevo Pedido -> Editar),
        not just via update_draft_sales_order() in isolation (already covered
        elsewhere for a Draft built some other way)."""
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
            )
            so_name = self._track_draft(result)

            ventas.update_draft_sales_order(
                name=so_name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 9}]
            )

        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.docstatus, 0)
        self.assertEqual(so.items[0].qty, 9)

    # -- confirm_order() ------------------------------------------------------

    def _draft(self, item, qty):
        with fx.as_user(self.vendedora):
            result = ventas.create_draft_sales_order(customer=self.customer.name, items=[{"item_code": item, "qty": qty}])
        return self._track_draft(result)

    def test_6_confirm_transitions_draft_to_submitted(self):
        so_name = self._draft(self.item.name, 3)

        with fx.as_user(self.vendedora):
            result = ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        self.assertEqual(result, {"name": so_name, "docstatus": 1, "status": "confirmed"})
        self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)

    def test_7_confirming_twice_is_idempotent(self):
        so_name = self._draft(self.item.name, 3)

        with fx.as_user(self.vendedora):
            first = ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)
        pick_lists_after_first = _pick_lists_for(so_name)

        with fx.as_user(self.vendedora):
            second = ventas.confirm_order(so_name)

        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(second, {"name": so_name, "docstatus": 1, "status": "already_confirmed"})
        # No second on_submit -- the artifact set from the first confirm is untouched.
        self.assertEqual(_pick_lists_for(so_name), pick_lists_after_first)

    def test_8_confirming_a_cancelled_order_raises_a_controlled_error(self):
        so_name = self._draft(self.item.name, 3)

        with fx.as_user(self.vendedora):
            ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        with fx.as_user(self.vendedora):
            ventas.cancel_sales_order(so_name)

        with fx.as_user(self.vendedora):
            with self.assertRaises(ventas.SalesOrderAlreadyCancelledError):
                ventas.confirm_order(so_name)

    def test_9_user_without_submit_permission_cannot_confirm(self):
        so_name = self._draft(self.item.name, 3)

        with fx.as_user(self.no_role_user):
            with self.assertRaises(frappe.PermissionError):
                ventas.confirm_order(so_name)

        self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 0)

    # -- confirm_order() under zero real stock ---------------------------------

    def test_10_zero_stock_still_confirms_successfully(self):
        so_name = self._draft(self.item_zero_stock.name, 6)

        with fx.as_user(self.vendedora):
            result = ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)

    def test_11_zero_stock_line_still_reaches_the_pick_list(self):
        so_name = self._draft(self.item_zero_stock.name, 6)

        with fx.as_user(self.vendedora):
            ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        pick_lists = _pick_lists_for(so_name)
        self.assertEqual(len(pick_lists), 1)
        rows = frappe.get_doc("Pick List", pick_lists[0]).get("locations")
        self.assertEqual(sum(row.stock_qty for row in rows), 6.0)

    def test_12_zero_stock_creates_no_reporte_de_faltante(self):
        so_name = self._draft(self.item_zero_stock.name, 6)

        with fx.as_user(self.vendedora):
            ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        self.assertEqual(_reports_for(so_name), [])

    def test_13_zero_stock_creates_no_material_request(self):
        so_name = self._draft(self.item_zero_stock.name, 6)

        with fx.as_user(self.vendedora):
            ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        self.assertEqual(_material_requests_for(so_name), [])
