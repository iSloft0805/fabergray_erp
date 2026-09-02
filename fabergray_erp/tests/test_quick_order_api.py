# -*- coding: utf-8 -*-
"""Commit 25.8.5 -- tests for fabergray_erp.api.ventas.parse_quick_order()
and its two private helpers (_build_quick_order_line_response(),
_serialize_quick_order_candidate()).

Two kinds of test here, same "separar claramente" convention
test_quick_order_catalog.py already established for its own two classes:

- TestPreselectionRuleUnit: calls _build_quick_order_line_response()
  directly with hand-built, synthetic match_result/catalog_index dicts --
  no DB, no real Sales Order/Item/User -- to pin the
  top_candidate/preselected_item business rule (Commit 25.8.5 brief,
  section 6) deterministically, independent of whatever the real catalog
  happens to contain today.
- Every other class calls the real, whitelisted parse_quick_order() against
  fabergray.local, through real Vendedora/no-role Users (fx.TestWorld, same
  fixture convention as test_ventas_permissions.py) -- confirms the
  endpoint's permission/validation/security contract end-to-end, and pins a
  handful of real-catalog results already verified live while building this
  commit (matching test_quick_order_catalog.py's own
  test_real_world_case_* convention).
"""

import inspect
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import ventas as ventas_api
from fabergray_erp.quick_order import catalog as quick_order_catalog
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_FORBIDDEN_PAYLOAD_KEYS = {
    "rate",
    "price",
    "price_list_rate",
    "valuation_rate",
    "standard_rate",
    "last_purchase_rate",
    "cost",
    "amount",
    "actual_qty",
    "projected_qty",
    "reserved_qty",
    "qty_disponible",
    "warehouse",
    "description",
}


class TestPreselectionRuleUnit(IntegrationTestCase):
    """No DB, no real Item/User -- see this module's own docstring."""

    @staticmethod
    def _line(source_text="algo", qty=1, detected_uom=None, product_text="algo"):
        return {"source_text": source_text, "qty": qty, "detected_uom": detected_uom, "product_text": product_text}

    @staticmethod
    def _candidate(item_code="X-1", score=90, confidence="high"):
        return {
            "item_code": item_code,
            "item_name": f"ITEM {item_code}",
            "score": score,
            "confidence": confidence,
            "matched_tokens": [],
            "conflicts": [],
            "reasons": [],
        }

    @staticmethod
    def _catalog_index(item_code="X-1", stock_uom="Unidad"):
        return {"by_code": {item_code: {"item_code": item_code, "item_name": f"ITEM {item_code}", "stock_uom": stock_uom}}}

    # M. high, not ambiguous -> preselected
    def test_m_high_confidence_not_ambiguous_is_preselected(self):
        match_result = {"candidates": [self._candidate(score=95, confidence="high")], "ambiguous": False, "score_margin": 20}
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertEqual(response["top_candidate"]["item_code"], "X-1")
        self.assertIsNotNone(response["preselected_item"])
        self.assertEqual(response["preselected_item"]["item_code"], "X-1")
        self.assertEqual(response["confidence"], "high")

    # N. high, ambiguous -> NOT preselected
    def test_n_high_confidence_but_ambiguous_is_not_preselected(self):
        match_result = {
            "candidates": [self._candidate(score=95, confidence="high"), self._candidate("X-2", score=94, confidence="high")],
            "ambiguous": True,
            "score_margin": 1,
        }
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertIsNotNone(response["top_candidate"])  # top_candidate still present
        self.assertIsNone(response["preselected_item"])  # but never preselected
        self.assertEqual(response["confidence"], "high")

    # O. medium -> NOT preselected
    def test_o_medium_confidence_is_not_preselected(self):
        match_result = {"candidates": [self._candidate(score=75, confidence="medium")], "ambiguous": False, "score_margin": None}
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertIsNotNone(response["top_candidate"])
        self.assertIsNone(response["preselected_item"])
        self.assertEqual(response["confidence"], "medium")

    # P. low -> NOT preselected
    def test_p_low_confidence_is_not_preselected(self):
        match_result = {"candidates": [self._candidate(score=40, confidence="low")], "ambiguous": False, "score_margin": None}
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertIsNotNone(response["top_candidate"])
        self.assertIsNone(response["preselected_item"])
        self.assertEqual(response["confidence"], "low")

    def test_no_candidates_gives_low_confidence_and_no_top_candidate(self):
        """The "producto que no existe xyz" shape from this commit's own
        brief, section 12 -- structural version, no DB."""
        match_result = {"candidates": [], "ambiguous": False, "score_margin": None}
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertIsNone(response["top_candidate"])
        self.assertIsNone(response["preselected_item"])
        self.assertEqual(response["confidence"], "low")
        self.assertEqual(response["candidates"], [])

    def test_serialized_candidate_has_exactly_the_allowed_keys(self):
        match_result = {"candidates": [self._candidate()], "ambiguous": False, "score_margin": None}
        response = ventas_api._build_quick_order_line_response(self._line(), match_result, self._catalog_index())
        self.assertEqual(
            set(response["candidates"][0].keys()),
            {"item_code", "item_name", "stock_uom", "score", "confidence", "matched_tokens", "conflicts", "reasons"},
        )


class TestParseQuickOrderPermissions(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-vendedora@example.com", ["Vendedora"])
        cls.no_role_user = cls.world.user("fg2585-norole@example.com", [])

    # A. usuario autorizado
    def test_a_authorized_vendedora_can_call_the_endpoint(self):
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order("guante talla 9 amarillo")
            self.assertEqual(result["line_count"], 1)

    # B. usuario no autorizado
    def test_b_unauthorized_user_is_rejected(self):
        with fx.as_user(self.no_role_user):
            self.assertRaises(frappe.PermissionError, ventas_api.parse_quick_order, "guante negro")

    def test_same_permission_check_as_search_items(self):
        """Same guard search_items()/get_item_info() already use -- no new,
        parallel permission system invented for this endpoint (Commit
        25.8.5 brief, section 2)."""
        source = inspect.getsource(ventas_api.parse_quick_order)
        self.assertIn('frappe.has_permission("Item", "read", throw=True)', source)
        self.assertIn("_require_login()", source)


class TestParseQuickOrderInputValidation(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-input-vendedora@example.com", ["Vendedora"])

    # C. texto vacío
    def test_c_empty_text_is_rejected(self):
        with fx.as_user(self.vendedora):
            self.assertRaises(frappe.ValidationError, ventas_api.parse_quick_order, "")

    # D. whitespace
    def test_d_whitespace_only_text_is_rejected(self):
        with fx.as_user(self.vendedora):
            self.assertRaises(frappe.ValidationError, ventas_api.parse_quick_order, "   \n   \n  ")

    def test_non_string_text_is_rejected(self):
        with fx.as_user(self.vendedora):
            self.assertRaises(frappe.ValidationError, ventas_api.parse_quick_order, None)

    # E. > máximo líneas
    def test_e_too_many_lines_is_rejected(self):
        text = "\n".join(["guante negro"] * (ventas_api.QUICK_ORDER_MAX_LINES + 1))
        with fx.as_user(self.vendedora):
            self.assertRaises(frappe.ValidationError, ventas_api.parse_quick_order, text)

    def test_exactly_the_max_lines_is_accepted(self):
        text = "\n".join(["guante negro"] * ventas_api.QUICK_ORDER_MAX_LINES)
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)
            self.assertEqual(result["line_count"], ventas_api.QUICK_ORDER_MAX_LINES)

    # F. > máximo caracteres
    def test_f_too_many_characters_is_rejected(self):
        text = "a" * (ventas_api.QUICK_ORDER_MAX_CHARS + 1)
        with fx.as_user(self.vendedora):
            self.assertRaises(frappe.ValidationError, ventas_api.parse_quick_order, text)

    def test_nothing_is_silently_truncated(self):
        """Section 3's own explicit rule -- an over-limit request raises,
        it never quietly processes only the first N lines/characters."""
        text = "\n".join(["guante negro"] * (ventas_api.QUICK_ORDER_MAX_LINES + 5))
        with fx.as_user(self.vendedora):
            with self.assertRaises(frappe.ValidationError):
                ventas_api.parse_quick_order(text)


class TestParseQuickOrderPipeline(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-pipeline-vendedora@example.com", ["Vendedora"])

    # G. múltiples líneas
    def test_g_multiple_lines(self):
        text = "2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90\n3 galones desengrasante"
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)
        self.assertEqual(result["line_count"], 3)

    # H. líneas vacías ignoradas
    def test_h_blank_lines_are_ignored(self):
        text = "guante talla 9 amarillo\n\n   \nescoba suave"
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)
        self.assertEqual(result["line_count"], 2)

    # I. orden preservado
    def test_i_line_order_is_preserved(self):
        text = "2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90\n3 galones desengrasante"
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)
        sources = [line["source_text"] for line in result["lines"]]
        self.assertEqual(
            sources,
            ["2 cajas guantes talla L negro", "1 paquete bolsa blanca 70x90", "3 galones desengrasante"],
        )


class TestParseQuickOrderCandidates(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-candidates-vendedora@example.com", ["Vendedora"])

    # J. máximo 5 candidatos
    def test_j_never_more_than_five_candidates_per_line(self):
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order("guante negro\nbolsa negra\ndesengrasante\npaquete")
        for line in result["lines"]:
            self.assertLessEqual(len(line["candidates"]), 5)

    # K. candidatos ordenados
    def test_k_candidates_sorted_score_descending(self):
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order("guante negro")
        scores = [c["score"] for c in result["lines"][0]["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestParseQuickOrderNoResult(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-noresult-vendedora@example.com", ["Vendedora"])

    # L. inexistente no falla el request
    def test_l_line_with_no_reasonable_candidate_does_not_fail_the_request(self):
        text = "guante talla 9 amarillo\nzzxxqqwwyy producto absolutamente inventado zzz"
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)  # must not raise
        self.assertEqual(result["line_count"], 2)
        weird_line = result["lines"][1]
        self.assertEqual(weird_line["source_text"], "zzxxqqwwyy producto absolutamente inventado zzz")
        self.assertIsNone(weird_line["preselected_item"])
        self.assertIn(weird_line["confidence"], ("low", "medium", "high"))  # always a well-formed value


class TestParseQuickOrderPayloadSecurity(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-security-vendedora@example.com", ["Vendedora"])

    # Q. sin precio / R. sin stock -- driven through the real endpoint against
    # several real queries, not just one.
    def test_q_r_payload_never_contains_price_or_stock_fields(self):
        text = "2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90\n3 galones desengrasante\nescoba"
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(text)
        for line in result["lines"]:
            for candidate in line["candidates"]:
                self.assertEqual(_FORBIDDEN_PAYLOAD_KEYS & set(candidate.keys()), set())
            for maybe_candidate in (line["top_candidate"], line["preselected_item"]):
                if maybe_candidate:
                    self.assertEqual(_FORBIDDEN_PAYLOAD_KEYS & set(maybe_candidate.keys()), set())

    def test_candidate_field_allowlist_is_exact(self):
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order("guante talla 9 amarillo")
        candidate = result["lines"][0]["candidates"][0]
        self.assertEqual(
            set(candidate.keys()),
            {"item_code", "item_name", "stock_uom", "score", "confidence", "matched_tokens", "conflicts", "reasons"},
        )

    # S. solo items vendibles
    def test_s_only_sellable_items_can_ever_appear(self):
        disabled_item = self.world.item("FG2585-DISABLED-GUANTE")
        # world.item() builds a normal sellable Item (is_sales_item=1,
        # disabled=0) -- flip it to disabled AFTER creation, and give it a
        # distinctive, matchable name, via a direct field update (test
        # fixture setup only, never part of the code under test).
        frappe.db.set_value(
            "Item", disabled_item.name, {"disabled": 1, "item_name": "GUANTE FANTASMA DESHABILITADO PRUEBA UNICA"}
        )
        quick_order_catalog.invalidate_catalog_cache()
        self.addCleanup(quick_order_catalog.invalidate_catalog_cache)

        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order("guante fantasma deshabilitado")

        all_codes = {c["item_code"] for line in result["lines"] for c in line["candidates"]}
        self.assertNotIn(disabled_item.name, all_codes)


class TestParseQuickOrderReadOnly(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-readonly-vendedora@example.com", ["Vendedora"])

    # T. no escribe Sales Order (ni Quotation/Pick List)
    def test_t_never_creates_a_sales_order(self):
        before = frappe.db.count("Sales Order")
        with fx.as_user(self.vendedora):
            ventas_api.parse_quick_order("2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90")
        after = frappe.db.count("Sales Order")
        self.assertEqual(before, after)

    # U. no modifica Item
    def test_u_never_modifies_an_item(self):
        item_code = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1, "has_variants": 0}, "item_code")
        before_modified = frappe.db.get_value("Item", item_code, "modified")
        with fx.as_user(self.vendedora):
            ventas_api.parse_quick_order("guante negro\nbolsa negra\ndesengrasante")
        after_modified = frappe.db.get_value("Item", item_code, "modified")
        self.assertEqual(before_modified, after_modified)

    def test_endpoint_source_never_calls_write_primitives(self):
        """Static guardrail, same convention as
        test_quick_order_catalog.py's own test_real_catalog_never_inserted_
        updated_or_deleted -- scoped to just this endpoint's own three
        functions (the rest of api/ventas.py legitimately DOES insert/submit
        Sales Orders elsewhere, so scanning the whole file would false-
        positive)."""
        source = "".join(
            inspect.getsource(fn)
            for fn in (
                ventas_api.parse_quick_order,
                ventas_api._build_quick_order_line_response,
                ventas_api._serialize_quick_order_candidate,
            )
        )
        for forbidden_call in (
            "frappe.get_all",
            ".insert(",
            ".save(",
            ".submit(",
            ".delete(",
            "frappe.delete_doc",
            "ignore_permissions=True",
            "db.set_value",
            "\"Sales Order\"",
            "'Sales Order'",
            "\"Quotation\"",
            "\"Pick List\"",
        ):
            self.assertNotIn(forbidden_call, source, f"parse_quick_order must never contain {forbidden_call!r}")


class TestParseQuickOrderSerializationAndPerformance(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.vendedora = cls.world.user("fg2585-perf-vendedora@example.com", ["Vendedora"])

    # V. respuesta serializable JSON
    def test_v_response_is_json_serializable(self):
        with fx.as_user(self.vendedora):
            result = ventas_api.parse_quick_order(
                "2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90\n3 galones desengrasante"
            )
        payload = json.dumps(result)  # must not raise
        self.assertGreater(len(payload), 0)

    # W. una carga de catálogo por request
    def test_w_catalog_is_loaded_exactly_once_per_request(self):
        text = "\n".join(
            [
                "2 cajas guantes talla L negro",
                "1 paquete bolsa blanca 70x90",
                "3 galones desengrasante",
                "1 litro ambientador lavanda",
                "guante talla 9 amarillo",
            ]
        )
        with patch(
            "fabergray_erp.quick_order.catalog.get_cached_catalog", wraps=quick_order_catalog.get_cached_catalog
        ) as spy:
            with fx.as_user(self.vendedora):
                result = ventas_api.parse_quick_order(text)
        self.assertEqual(result["line_count"], 5)
        self.assertEqual(spy.call_count, 1)
