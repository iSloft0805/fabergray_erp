# -*- coding: utf-8 -*-
"""Approved architecture (see this session's own audit): a Reporte de
Faltante is never resolved via Material Request -> Purchase Order ->
Purchase Receipt -- this app has no Proveedores/Supplier module, and
Commit 22.8 already built the real "Registrar compra" resolution path as a
native, submitted Stock Entry (purpose="Material Receipt") linked to the
report via the Custom Field `fg_shortage_report`
(api.jefe_bodega.receive_shortage_purchase()). That function and its 27
existing tests (test_jefe_bodega_purchase_api.py) are untouched by this
file.

What this file adds: end-to-end coverage that was missing -- the whole
chain exercised through the REAL Commit 25.4 entry points a Vendedora and
Bodega actually use (ventas.create_draft_sales_order() ->
ventas.confirm_order() -> the full-demand Pick List -> bodega.
report_shortage() -> jefe_bodega.receive_shortage_purchase()), instead of
a hand-built Sales Order/Pick List bypassing the on_submit hook (as every
existing shortage-purchase test does via fx.multi_item_sales_order()'s own
without_sales_order_hook()). It proves the pieces actually fit together:
the same Pick List a Vendedora's confirm produced, with a genuinely
zero-stock line, carries all the way through a Bodega-reported shortage
and a Jefe de Bodega purchase receipt back to a completed pick -- with no
duplicate Pick List and no duplicate Reporte de Faltante anywhere in that
chain.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.api import jefe_bodega
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


class TestShortageResolutionFlow(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)

        cls.wh = cls.world.warehouse("FG254 Shortage Flow")
        cls.item = cls.world.item(
            "FG254-SF-ITEM", default_material_request_type="Purchase", default_warehouse=cls.wh.name
        )
        cls.customer = cls.world.customer("FG254 Shortage Flow Customer")

        cls.vendedora = cls.world.user("fg254sf-vendedora@example.com", ["Vendedora"])
        cls.bodega_user = cls.world.user("fg254sf-bodega@example.com", ["Bodega"])
        cls.jefe = cls.world.user("fg254sf-jefe@example.com", ["Jefe de Bodega"])
        cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
        cls.difference_account = cls.world.stock_difference_account()

    def _confirmed_order_pick_list(self, qty=10):
        """The real chain, start to finish: Draft -> Confirmar (Commit
        25.4) -> the resulting full-demand Pick List -- never a hand-built
        Sales Order/Pick List. The item has zero real stock, so the Pick
        List row is entirely "faltante" by construction."""
        with fx.as_user(self.vendedora):
            draft = ventas.create_draft_sales_order(
                customer=self.customer.name, items=[{"item_code": self.item.name, "qty": qty}]
            )
            so_name = draft["name"]
            self.world.track_existing("Sales Order", so_name)
            ventas.confirm_order(so_name)
        self.world.track_existing_pick_lists_and_reports_for(so_name)

        pick_lists = _pick_lists_for(so_name)
        self.assertEqual(len(pick_lists), 1, "confirm_order() should produce exactly one Pick List")
        return so_name, pick_lists[0]

    def _report_full_shortage(self, pick_list_name, qty):
        with fx.as_user(self.bodega_user):
            bodega.start_picking(pick_list_name)
            row = bodega.get_pick_list(pick_list_name)["rows"][0]
            bodega.set_picked_qty(pick_list_name, row["row_name"], 0)
            result = bodega.report_shortage(
                pick_list=pick_list_name,
                row_name=row["row_name"],
                qty_disponible=0,
                shortage_reason="Compra pendiente",
            )
        self.world.track_existing("Reporte de Faltante", result["name"])
        return result["name"], row["row_name"]

    # -- Flujo Bodega: 14/15 (16 deferred to Commit 25.5, see below) -------------

    def test_14_bodega_registers_shortage_manually_through_the_real_chain(self):
        so_name, pl_name = self._confirmed_order_pick_list(qty=8)
        report_name, _row = self._report_full_shortage(pl_name, qty=8)

        report = frappe.get_doc("Reporte de Faltante", report_name)
        self.assertEqual(report.detected_by, "Bodega")
        self.assertEqual(report.item_code, self.item.name)
        self.assertEqual(report.warehouse, self.wh.name)
        self.assertEqual(report.sales_order, so_name)
        self.assertEqual(report.status, "Abierto")

    def test_15_exactly_one_report_is_created_per_call(self):
        _so_name, pl_name = self._confirmed_order_pick_list(qty=5)
        report_name, row_name = self._report_full_shortage(pl_name, qty=5)

        matching = frappe.get_all(
            "Reporte de Faltante", filters={"pick_list": pl_name, "pick_list_item": row_name}, pluck="name"
        )
        self.assertEqual(matching, [report_name])

    # Item 16 of the original checklist ("repetir la acción no genera
    # duplicado accidental") is deliberately NOT covered here. It documents
    # a pre-existing gap in api.bodega.report_shortage() that is explicitly
    # out of scope for 25.3/25.4 -- the approved plan resolves it as its
    # own, independent Commit 25.5, so no test coupled to today's
    # not-yet-deduplicated behaviour belongs in this commit.

    # -- Flujo Compra (arquitectura aprobada: Stock Entry, no MR/PO/PR): 17-24 --

    def test_17_deciding_to_purchase_creates_the_resolution_artifact(self):
        """"genera el Material Request correspondiente" under the approved
        architecture = genera el Stock Entry correspondiente, únicamente
        cuando Jefe de Bodega decide comprar -- nunca antes."""
        _so_name, pl_name = self._confirmed_order_pick_list(qty=4)
        report_name, _row = self._report_full_shortage(pl_name, qty=4)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=4, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        self.assertTrue(frappe.db.exists("Stock Entry", result["stock_entry"]))
        self.assertEqual(
            frappe.db.get_value("Stock Entry", result["stock_entry"], "fg_shortage_report"), report_name
        )

    def test_18_no_resolution_artifact_exists_before_the_decision(self):
        _so_name, pl_name = self._confirmed_order_pick_list(qty=4)
        report_name, _row = self._report_full_shortage(pl_name, qty=4)

        linked_entries = frappe.get_all("Stock Entry", filters={"fg_shortage_report": report_name}, pluck="name")
        self.assertEqual(linked_entries, [])

    def test_19_purchase_updates_the_correct_shortage_report(self):
        so_name, pl_name = self._confirmed_order_pick_list(qty=7)
        report_name, _row = self._report_full_shortage(pl_name, qty=7)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=7, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        report = frappe.get_doc("Reporte de Faltante", report_name)
        self.assertEqual(report.status, "Resuelto")
        self.assertEqual(report.sales_order, so_name)  # traceability preserved

    def test_20_purchase_does_not_create_a_second_pick_list(self):
        so_name, pl_name = self._confirmed_order_pick_list(qty=9)
        report_name, _row = self._report_full_shortage(pl_name, qty=9)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=9, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        self.assertEqual(_pick_lists_for(so_name), [pl_name])

    def test_21_purchase_does_not_create_a_second_shortage_report(self):
        so_name, pl_name = self._confirmed_order_pick_list(qty=9)
        report_name, _row = self._report_full_shortage(pl_name, qty=9)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=9, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        self.assertEqual(_reports_for(so_name), [report_name])

    def test_22_partial_receipt_leaves_shortage_open_with_correct_remaining(self):
        _so_name, pl_name = self._confirmed_order_pick_list(qty=10)
        report_name, _row = self._report_full_shortage(pl_name, qty=10)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=6, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        report = frappe.get_doc("Reporte de Faltante", report_name)
        self.assertEqual(report.status, "En Proceso")
        self.assertEqual(result["remaining_qty"], 4.0)

    def test_23_full_receipt_resolves_and_closes_the_shortage(self):
        _so_name, pl_name = self._confirmed_order_pick_list(qty=10)
        report_name, _row = self._report_full_shortage(pl_name, qty=10)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=10, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        self.assertEqual(result["status"], "Resuelto")
        self.assertEqual(result["remaining_qty"], 0.0)

    def test_24_repeating_the_resolution_call_once_resolved_is_rejected_not_duplicated(self):
        """Idempotency for this architecture: once a report is fully
        Resuelto, a repeated call is rejected outright
        (ShortageAlreadyResolvedError) rather than silently accepted and
        producing a second Stock Entry -- the same guarantee the old
        "repetir el hook de Purchase Receipt es idempotente" checklist item
        asked for, expressed the way this architecture actually enforces
        it."""
        _so_name, pl_name = self._confirmed_order_pick_list(qty=3)
        report_name, _row = self._report_full_shortage(pl_name, qty=3)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                first = jefe_bodega.receive_shortage_purchase(report_name, qty=3, purchase_rate=1000)
                self.world.track_existing("Stock Entry", first["stock_entry"])

                with self.assertRaises(jefe_bodega.ShortageAlreadyResolvedError):
                    jefe_bodega.receive_shortage_purchase(report_name, qty=3, purchase_rate=1000)

        entries = frappe.get_all("Stock Entry", filters={"fg_shortage_report": report_name}, pluck="name")
        self.assertEqual(entries, [first["stock_entry"]])  # still exactly one

    # -- Continuidad real del alistamiento tras la compra ------------------------

    def test_pick_list_continues_and_completes_after_shortage_resolved_by_purchase(self):
        """The actual point of PASO 3: once Jefe de Bodega registers the
        purchase (Stock Entry increases real stock), Bodega picks the very
        same row on the very same Pick List the confirm produced -- no new
        Pick List, no second shortage report -- and finishing it succeeds."""
        so_name, pl_name = self._confirmed_order_pick_list(qty=5)
        report_name, row_name = self._report_full_shortage(pl_name, qty=5)

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                result = jefe_bodega.receive_shortage_purchase(report_name, qty=5, purchase_rate=1000)
        self.world.track_existing("Stock Entry", result["stock_entry"])

        with fx.as_user(self.bodega_user):
            bodega.set_picked_qty(pl_name, row_name, 5)
            finish = bodega.finish_picking(pl_name)

        self.assertEqual(finish["docstatus"], 1)
        self.assertEqual(frappe.utils.flt(frappe.db.get_value("Sales Order", so_name, "per_picked")), 100.0)
        # Still exactly one Pick List, one Reporte de Faltante for this order.
        self.assertEqual(len(_pick_lists_for(so_name)), 1)
        self.assertEqual(_reports_for(so_name), [report_name])
