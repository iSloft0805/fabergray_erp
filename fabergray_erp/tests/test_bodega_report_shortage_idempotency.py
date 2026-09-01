# -*- coding: utf-8 -*-
"""Commit 25.5 -- idempotent warehouse shortage reporting.

Audit finding this closes: api.bodega.report_shortage() had no guard
against being called twice for the same Pick List Item row (double
click, HTTP retry, timeout+retry, accidental repeat) -- each call
unconditionally inserted a brand-new Reporte de Faltante, so a retry
silently doubled the reported shortage in Centro de Faltantes.

Operational identity chosen: `pick_list_item` (the exact Pick List Item
row's own `name`) -- not item_code+warehouse (would block legitimate,
unrelated shortages for the same product on a different order/Pick
List), not sales_order_item alone (an amended/re-picked order gets a
brand new Pick List Item row, and correctly deserves its own new
report). Confirmed, not assumed, that no other consumer in this app
(_get_shortage_report_rows(), used by both get_pick_list()'s
has_shortage_report and finish_picking()'s undisclosed-shortfall check)
ever expects a second report for the same row once the first exists,
regardless of status -- so idempotency here is status-blind on purpose,
matching that already-established model rather than inventing a
narrower "only while open" rule.

Mechanism: a real MariaDB UNIQUE INDEX on Reporte de
Faltante.pick_list_item (reporte_de_faltante.json, `"unique": 1`,
confirmed live to exclude NULL -- a Fulfillment-Engine report, which
never sets this field, is unaffected), backing a cheap pre-check in
report_shortage() itself. Two connections is the level 4/24 tests
above reproduce genuine concurrency for -- see
test_concurrent_calls_do_not_duplicate_the_report below.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega
from fabergray_erp.api import jefe_bodega
from fabergray_erp.api.bodega import _create_shortage_report, _get_pick_list_row
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestReportShortageIdempotency(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)

        cls.wh = cls.world.warehouse("FG255 Idempotent Wh")
        cls.wh_b = cls.world.warehouse("FG255 Idempotent Wh B")
        cls.item = cls.world.item(
            "FG255-IDEMPOTENT-ITEM", default_material_request_type="Purchase", default_warehouse=cls.wh.name
        )
        # Native create_pick_list()'s own before_save() -> set_item_locations()
        # drops any row with zero available stock entirely -- world.pick_list_for()
        # uses that native mapper as-is (unlike this app's own full-demand adapter,
        # Commit 25.4), so every test below needs the item to genuinely have stock
        # for its Pick List row to exist at all.
        cls.world.stock_up(cls.item.name, cls.wh.name, 1000)
        cls.world.stock_up(cls.item.name, cls.wh_b.name, 1000)
        cls.customer = cls.world.customer("FG255 Idempotent Customer")

        cls.bodega_user = cls.world.user("fg255-bodega@example.com", ["Bodega"])
        cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
        cls.world.warehouse_user_permission(cls.bodega_user, cls.wh_b.name)
        cls.jefe = cls.world.user("fg255-jefe@example.com", ["Jefe de Bodega"])
        cls.no_role_user = cls.world.user("fg255-norole@example.com", [])
        cls.difference_account = cls.world.stock_difference_account()

    def _pick_list_with_row(self, qty=10, warehouse=None, customer=None, item=None):
        warehouse = warehouse or self.wh
        so = self.world.submitted_sales_order(
            (item or self.item).name, warehouse.name, qty, (customer or self.customer).name
        )
        pl = self.world.pick_list_for(so, warehouse.name)
        with fx.as_user(self.bodega_user):
            bodega.start_picking(pl.name)
        return pl

    def _row_name(self, pl, idx=0):
        with fx.as_user(self.bodega_user):
            rows = bodega.get_pick_list(pl.name)["rows"]
        return rows[idx]["row_name"]

    def _report_count_for_row(self, row_name):
        return frappe.db.count("Reporte de Faltante", {"pick_list_item": row_name})

    # -- 1/2/3/4/5. Idempotencia básica -----------------------------------------

    def test_first_call_creates_exactly_one_report(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            result = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
        self.world.track_existing("Reporte de Faltante", result["name"])

        self.assertEqual(self._report_count_for_row(row_name), 1)
        self.assertFalse(result["already_exists"])

    def test_second_identical_call_returns_the_same_report(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            second = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertEqual(first["name"], second["name"])
        self.assertTrue(second["already_exists"])

    def test_count_stays_at_one_after_repeated_calls(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            for _ in range(3):
                bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertEqual(self._report_count_for_row(row_name), 1)

    def test_already_exists_false_on_first_call(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            result = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
        self.world.track_existing("Reporte de Faltante", result["name"])

        self.assertIs(result["already_exists"], False)

    def test_already_exists_true_on_second_call(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            second = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertIs(second["already_exists"], True)

    # -- 6. Cantidad distinta en la segunda llamada ------------------------------

    def test_different_qty_on_second_call_does_not_create_or_overwrite(self):
        pl = self._pick_list_with_row(qty=10)
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            second = bodega.report_shortage(pl.name, row_name, qty_disponible=8, shortage_reason="Otro")

        self.assertEqual(second["name"], first["name"])
        self.assertTrue(second["already_exists"])
        self.assertEqual(second["qty_faltante"], 7.0)  # the ORIGINAL 10-3, never recomputed as 10-8
        self.assertEqual(self._report_count_for_row(row_name), 1)

        report = frappe.get_doc("Reporte de Faltante", first["name"])
        self.assertEqual(flt(report.qty_disponible), 3.0)  # untouched by the second, different call
        self.assertEqual(flt(report.qty_faltante), 7.0)
        self.assertEqual(report.shortage_reason, "Stock insuficiente")  # not overwritten to "Otro"

    # -- 7/8/9. La deduplicación no es global -- solo por línea operacional -----

    def test_same_item_different_pick_list_gets_its_own_report(self):
        customer_b = self.world.customer("FG255 Idempotent Customer B")
        pl_1 = self._pick_list_with_row(qty=6)
        pl_2 = self._pick_list_with_row(qty=4, customer=customer_b)
        row_1 = self._row_name(pl_1)
        row_2 = self._row_name(pl_2)
        self.assertNotEqual(row_1, row_2)

        with fx.as_user(self.bodega_user):
            r1 = bodega.report_shortage(pl_1.name, row_1, qty_disponible=1, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r1["name"])
            r2 = bodega.report_shortage(pl_2.name, row_2, qty_disponible=1, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r2["name"])

        self.assertNotEqual(r1["name"], r2["name"])
        self.assertFalse(r1["already_exists"])
        self.assertFalse(r2["already_exists"])  # NOT blocked by row_1's report despite the same item_code

    def test_same_item_two_distinct_lines_in_one_order_are_not_confused(self):
        so = self.world.multi_item_sales_order(
            self.customer.name,
            [
                {"item_code": self.item.name, "warehouse": self.wh.name, "qty": 5, "rate": 100},
                {"item_code": self.item.name, "warehouse": self.wh.name, "qty": 5, "rate": 100},
            ],
        )
        pl = self.world.pick_list_for(so, self.wh.name)
        with fx.as_user(self.bodega_user):
            bodega.start_picking(pl.name)
            rows = bodega.get_pick_list(pl.name)["rows"]
        self.assertEqual(len(rows), 2)
        row_a, row_b = rows[0]["row_name"], rows[1]["row_name"]
        self.assertNotEqual(row_a, row_b)

        with fx.as_user(self.bodega_user):
            r_a = bodega.report_shortage(pl.name, row_a, qty_disponible=1, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r_a["name"])
            r_b = bodega.report_shortage(pl.name, row_b, qty_disponible=2, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r_b["name"])

        self.assertNotEqual(r_a["name"], r_b["name"])
        self.assertFalse(r_a["already_exists"])
        self.assertFalse(r_b["already_exists"])
        self.assertEqual(frappe.db.get_value("Reporte de Faltante", r_a["name"], "qty_disponible"), 1.0)
        self.assertEqual(frappe.db.get_value("Reporte de Faltante", r_b["name"], "qty_disponible"), 2.0)

    def test_same_item_different_warehouse_is_not_confused(self):
        so = self.world.multi_item_sales_order(
            self.customer.name,
            [
                {"item_code": self.item.name, "warehouse": self.wh.name, "qty": 5, "rate": 100},
                {"item_code": self.item.name, "warehouse": self.wh_b.name, "qty": 5, "rate": 100},
            ],
        )
        pl_a = self.world.pick_list_for(so, self.wh.name)
        pl_b = self.world.pick_list_for(so, self.wh_b.name)
        with fx.as_user(self.bodega_user):
            bodega.start_picking(pl_a.name)
            bodega.start_picking(pl_b.name)
            row_a = bodega.get_pick_list(pl_a.name)["rows"][0]["row_name"]
            row_b = bodega.get_pick_list(pl_b.name)["rows"][0]["row_name"]

        with fx.as_user(self.bodega_user):
            r_a = bodega.report_shortage(pl_a.name, row_a, qty_disponible=1, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r_a["name"])
            r_b = bodega.report_shortage(pl_b.name, row_b, qty_disponible=1, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", r_b["name"])

        self.assertNotEqual(r_a["name"], r_b["name"])
        self.assertFalse(r_a["already_exists"])
        self.assertFalse(r_b["already_exists"])
        self.assertEqual(frappe.db.get_value("Reporte de Faltante", r_a["name"], "warehouse"), self.wh.name)
        self.assertEqual(frappe.db.get_value("Reporte de Faltante", r_b["name"], "warehouse"), self.wh_b.name)

    # -- 10. Ciclo de vida: reporte Resuelto -------------------------------------

    def test_resolved_report_is_still_returned_not_duplicated(self):
        """Determined by audit, not assumed: get_pick_list()'s own
        has_shortage_report and finish_picking()'s own undisclosed-
        shortfall check both treat ANY status (including Resuelto) as
        "this row was already reported" -- there is no code path in this
        app that expects or benefits from a second report on the same
        row once the first resolves. So a Resuelto report is returned
        exactly like an open one, never superseded by a new document.

        Dedicated item+warehouse (not the class-level self.item/self.wh
        every other test here shares): receive_shortage_purchase() posts
        a REAL Stock Entry, and ERPNext's own stock ledger reposting then
        recalculates Bin.actual_qty for this exact item+warehouse from
        real Stock Ledger Entry history alone -- wiping out
        stock_up()'s synthetic, unbacked seed value (confirmed live: the
        shared Bin dropped from 1000 to 5, the real receipt's own qty,
        the moment this ran against the shared fixtures) and starving
        every other test's own world.pick_list_for() call, whose native
        create_pick_list() mapper silently drops any row with zero
        available stock. A throwaway item+warehouse here contains that
        interaction to this one test only."""
        wh = self.world.warehouse("FG255 Resolved Lifecycle Wh")
        item = self.world.item("FG255-RESOLVED-LIFECYCLE-ITEM", default_warehouse=wh.name)
        self.world.stock_up(item.name, wh.name, 1000)
        self.world.warehouse_user_permission(self.bodega_user, wh.name)

        pl = self._pick_list_with_row(qty=5, warehouse=wh, item=item)
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=0, shortage_reason="Compra pendiente")
        self.world.track_existing("Reporte de Faltante", first["name"])

        with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
            with fx.as_user(self.jefe):
                receipt = jefe_bodega.receive_shortage_purchase(first["name"], qty=5, purchase_rate=1000)
        self.world.track_existing("Stock Entry", receipt["stock_entry"])
        self.assertEqual(frappe.db.get_value("Reporte de Faltante", first["name"], "status"), "Resuelto")

        with fx.as_user(self.bodega_user):
            second = bodega.report_shortage(pl.name, row_name, qty_disponible=0, shortage_reason="Compra pendiente")

        self.assertEqual(second["name"], first["name"])
        self.assertTrue(second["already_exists"])
        self.assertEqual(second["status"], "Resuelto")
        self.assertEqual(self._report_count_for_row(row_name), 1)

    # -- 11. Permisos -------------------------------------------------------------

    def test_user_without_permission_is_still_rejected(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.no_role_user):
            with self.assertRaises(frappe.PermissionError):
                bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertEqual(self._report_count_for_row(row_name), 0)

    # -- 12/13/14. Un retry no toca inventario ni compras ------------------------

    def test_retries_create_no_material_request(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)
        so_name = pl.get("locations")[0].sales_order

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertEqual(frappe.get_all("Material Request Item", filters={"sales_order": so_name}), [])

    def test_retries_create_no_stock_entry(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        self.assertEqual(frappe.get_all("Stock Entry", filters={"fg_shortage_report": first["name"]}), [])

    def test_retries_do_not_change_bin_actual_qty(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)
        before = flt(frappe.db.get_value("Bin", {"item_code": self.item.name, "warehouse": self.wh.name}, "actual_qty"))

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        after = flt(frappe.db.get_value("Bin", {"item_code": self.item.name, "warehouse": self.wh.name}, "actual_qty"))
        self.assertEqual(before, after)

    # -- 15. Centro de Faltantes sigue viendo exactamente un registro -----------

    def test_centro_de_faltantes_still_sees_exactly_one_row_after_retries(self):
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)

        with fx.as_user(self.bodega_user):
            first = bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            self.world.track_existing("Reporte de Faltante", first["name"])
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")
            bodega.report_shortage(pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente")

        with fx.as_user(self.jefe):
            center = jefe_bodega.get_shortage_center(page_length=100)
        matching = [r for r in center["reports"] if r["name"] == first["name"]]
        self.assertEqual(len(matching), 1)

        with fx.as_user(self.bodega_user):
            own_view = bodega.get_shortages()
        matching_own = [r for r in own_view if r["name"] == first["name"]]
        self.assertEqual(len(matching_own), 1)

    # -- Concurrencia -------------------------------------------------------------

    def test_direct_insert_path_raises_unique_validation_error_on_duplicate(self):
        """Tests the actual atomicity mechanism directly, bypassing
        report_shortage()'s own pre-check on purpose -- this is what
        genuinely closes the race two truly concurrent requests would
        hit (both already past the pre-check before either commits): a
        second, low-level insert attempt for the identical pick_list_item
        must be rejected by the real MariaDB unique index, not merely by
        application logic that a race could still run past."""
        pl = self._pick_list_with_row()
        row = _get_pick_list_row(pl, self._row_name(pl))

        first_name = _create_shortage_report(
            pick_list_doc=pl, row=row, qty_disponible=3, shortage_reason="Stock insuficiente", detected_by="Bodega"
        )
        self.world.track_existing("Reporte de Faltante", first_name)

        with self.assertRaises(frappe.UniqueValidationError):
            _create_shortage_report(
                pick_list_doc=pl, row=row, qty_disponible=3, shortage_reason="Stock insuficiente", detected_by="Bodega"
            )

        self.assertEqual(self._report_count_for_row(row.name), 1)

    def test_concurrent_calls_from_two_connections_do_not_duplicate_the_report(self):
        """Real two-connection proof, using IntegrationTestCase's own
        primary_connection()/secondary_connection() (same infrastructure
        test_shortage_service.py's own concurrency test uses). The
        primary connection's report_shortage() call is committed first
        (the realistic shape of two nearly-simultaneous requests, one of
        which always finishes microseconds before the other) -- the
        secondary connection's own call, running with no knowledge of
        the primary's outcome beyond what is now actually committed,
        must detect the committed report via its own pre-check and
        return it rather than racing past a stale read. Combined with
        test_direct_insert_path_raises_unique_validation_error_on_duplicate
        above (the case where neither has committed yet), both halves of
        the real race window are covered."""
        pl = self._pick_list_with_row()
        row_name = self._row_name(pl)
        frappe.db.commit()  # fixtures + started Pick List visible to the secondary connection

        try:
            with self.primary_connection():
                with fx.as_user(self.bodega_user):
                    primary_result = bodega.report_shortage(
                        pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente"
                    )
                self.assertFalse(primary_result["already_exists"])
                frappe.db.commit()

            with self.secondary_connection():
                with fx.as_user(self.bodega_user):
                    secondary_result = bodega.report_shortage(
                        pl.name, row_name, qty_disponible=3, shortage_reason="Stock insuficiente"
                    )
                frappe.db.commit()
        finally:
            pass

        self.world.track_existing("Reporte de Faltante", primary_result["name"])
        self.assertEqual(secondary_result["name"], primary_result["name"])
        self.assertTrue(secondary_result["already_exists"])
        self.assertEqual(self._report_count_for_row(row_name), 1)
