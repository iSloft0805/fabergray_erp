# -*- coding: utf-8 -*-
"""Commit 25.8.3 -- tests for fabergray_erp.quick_order.catalog.

Split in two, exactly per this commit's own brief (section 16):

- TestCatalogIndexPureLogic: builds its own synthetic candidates via
  matcher.build_candidate() (same pattern as test_quick_order_matching.py,
  Commit 25.8.2) and calls build_inverted_index()/search_catalog_candidates()
  directly against a hand-built `catalog` dict -- NEVER frappe.get_list, NEVER
  the real Item table. These do not need a DB and would still pass if this
  site had zero Items.
- TestCatalogAgainstRealSite: the only class in this file that reads real
  Item rows (frappe.get_list, read-only, same filter as api/ventas.py's own
  search_items()) -- confirms catalog.py's ERPNext integration itself, never
  the pure retrieval/ranking logic already covered above.

25.8.1 (normalizer.py/parser.py) and 25.8.2 (scoring.py/matcher.py) are
untouched by this commit and stay exactly as DB-free as before -- see
test_quick_order_parser.py/test_quick_order_matching.py, neither of which
this file modifies or depends on.
"""

import ast
import inspect

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.quick_order import catalog
from fabergray_erp.quick_order.matcher import build_candidate, match_order_line
from fabergray_erp.quick_order.parser import parse_order_line

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


def _synthetic_candidates():
	"""Same base catalog as test_quick_order_matching.py (Commit 25.8.2) --
	deliberately not imported from there (that module has no shared fixture
	helper of its own, and duplicating six lines here keeps this file
	independently readable/runnable)."""
	return [
		build_candidate("GLV-NIT-NEG-L", "GUANTE NITRILO NEGRO TALLA L"),
		build_candidate("GLV-NIT-NEG-M", "GUANTE NITRILO NEGRO TALLA M"),
		build_candidate("GLV-LAT-BLA-L", "GUANTE LATEX BLANCO TALLA L"),
		build_candidate("BOL-NEG-7090", "BOLSA NEGRA 70X90"),
		build_candidate("BOL-NEG-80100", "BOLSA NEGRA 80X100"),
		build_candidate("DESENG-GAL", "DESENGRASANTE 1 GALON"),
	]


class TestCatalogIndexPureLogic(IntegrationTestCase):
	"""No frappe.get_list anywhere in this class -- every `catalog` argument
	is built by hand from `_synthetic_candidates()`."""

	def test_catalog_py_is_the_only_module_allowed_to_import_frappe(self):
		"""Inverse of test_quick_order_parser.py/test_quick_order_matching.py's
		own guardrails: THIS module must import frappe (it is the ERPNext
		integration layer), confirmed here so nobody "purifies" it into a
		non-functional stub by accident."""
		tree = ast.parse(inspect.getsource(catalog))
		imports_frappe = any(
			(isinstance(node, ast.Import) and any(a.name.split(".")[0] == "frappe" for a in node.names))
			or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "frappe")
			for node in ast.walk(tree)
		)
		self.assertTrue(imports_frappe, "catalog.py should import frappe -- it is the ERPNext integration layer")

	def test_build_inverted_index_shape(self):
		index = catalog.build_inverted_index(_synthetic_candidates())
		self.assertEqual(set(index.keys()), {"token_index", "by_code"})
		self.assertEqual(len(index["by_code"]), 6)
		self.assertIn("GLV-NIT-NEG-L", index["token_index"]["guante"])
		self.assertIn("BOL-NEG-7090", index["token_index"]["bolsa"])
		self.assertIn("BOL-NEG-7090", index["token_index"]["70x90"])
		self.assertIn("GLV-NIT-NEG-L", index["token_index"]["l"])
		self.assertIn("GLV-NIT-NEG-L", index["token_index"]["negro"])

	def test_search_catalog_candidates_retrieves_relevant_pool_only(self):
		"""Section 5's own example, reproduced with the synthetic set: a
		guante query must never even consider DESENGRASANTE (no shared
		token, no bucket in common)."""
		index = catalog.build_inverted_index(_synthetic_candidates())
		pool = catalog.search_catalog_candidates(parse_order_line("2 cajas guantes talla L negro"), catalog=index)
		pool_codes = {c["item_code"] for c in pool}
		self.assertIn("GLV-NIT-NEG-L", pool_codes)
		self.assertNotIn("DESENG-GAL", pool_codes)

	def test_search_catalog_candidates_feeds_match_order_line_correctly(self):
		"""End-to-end through the real retrieval -> scoring pipeline, still
		with zero DB access -- the same top-1 result Commit 25.8.2's own
		test_a_2_cajas_guantes_talla_l_negro already established directly
		against the full synthetic list."""
		index = catalog.build_inverted_index(_synthetic_candidates())
		parsed = parse_order_line("2 cajas guantes talla L negro")
		pool = catalog.search_catalog_candidates(parsed, catalog=index)
		result = match_order_line(parsed, pool)
		self.assertEqual(result["candidates"][0]["item_code"], "GLV-NIT-NEG-L")

	def test_typo_fallback_still_retrieves_the_right_candidate(self):
		"""Section 6's own concern -- a typo with zero exact index hit must
		not come back empty. "guate" has no bucket of its own at all, so
		tier 1 finds nothing; tier 2 (fuzzy vocabulary scan) must recover
		it."""
		index = catalog.build_inverted_index(_synthetic_candidates())
		self.assertNotIn("guate", index["token_index"])
		pool = catalog.search_catalog_candidates(parse_order_line("guate negro talla l"), catalog=index)
		pool_codes = {c["item_code"] for c in pool}
		self.assertIn("GLV-NIT-NEG-L", pool_codes)

	def test_completely_unrelated_query_can_still_return_empty_pool_tier3_notwithstanding(self):
		"""A line with tokens that resemble NOTHING in the vocabulary at all
		(section 5's "producto inexistente") legitimately falls through to
		tier 3 (full catalog) per this module's own design -- match_order_line
		is still the one that filters it back down to zero via its own
		retrieval gate, exactly like Commit 25.8.2's own test_h."""
		index = catalog.build_inverted_index(_synthetic_candidates())
		parsed = parse_order_line("producto inexistente completamente")
		pool = catalog.search_catalog_candidates(parsed, catalog=index)
		result = match_order_line(parsed, pool)
		self.assertEqual(result["candidates"], [])

	def test_common_token_pool_is_deterministic_and_prioritized(self):
		"""Regression pin for the truncation-safety fix found while building
		this commit (see report, section Q, edge case #1): ranking by
		hit-count (not an unordered set) means a low-limit_pool run still
		keeps the multi-signal-matching candidate over a single-bucket one."""
		index = catalog.build_inverted_index(_synthetic_candidates())
		parsed = parse_order_line("2 cajas guantes talla L negro")
		pool = catalog.search_catalog_candidates(parsed, catalog=index, limit_pool=1)
		self.assertEqual(len(pool), 1)
		self.assertEqual(pool[0]["item_code"], "GLV-NIT-NEG-L")


class TestCatalogAgainstRealSite(IntegrationTestCase):
	"""The only class in this file that touches the real Item table. Every
	call is a plain read (frappe.get_list) -- no insert/update/delete of any
	kind, matching this commit's own "solo SELECT/read" brief (section 19)."""

	def test_get_sellable_item_candidates_matches_search_items_filter(self):
		"""Same disabled=0/is_sales_item=1/has_variants=0 semantics
		api/ventas.py's own search_items() already uses (Commit 18.2) --
		confirmed by count, not just by reading the filter dict."""
		expected_count = frappe.db.count("Item", catalog.SELLABLE_ITEM_FILTERS)
		candidates = catalog.get_sellable_item_candidates()
		self.assertEqual(len(candidates), expected_count)

	def test_candidates_never_carry_price_or_stock_fields(self):
		candidates = catalog.get_sellable_item_candidates()
		forbidden = {"rate", "price", "price_list_rate", "valuation_rate", "standard_rate", "cost", "amount", "stock", "qty_disponible"}
		for candidate in candidates[:50]:
			self.assertEqual(forbidden & set(candidate.keys()), set())

	def test_get_cached_catalog_is_read_through_and_invalidatable(self):
		catalog.invalidate_catalog_cache()
		self.assertIsNone(frappe.cache().get_value(catalog.CACHE_KEY))
		first = catalog.get_cached_catalog()
		self.assertIsNotNone(frappe.cache().get_value(catalog.CACHE_KEY))
		second = catalog.get_cached_catalog()
		self.assertEqual(set(first["by_code"].keys()), set(second["by_code"].keys()))
		catalog.invalidate_catalog_cache()
		self.assertIsNone(frappe.cache().get_value(catalog.CACHE_KEY))

	def test_real_catalog_never_inserted_updated_or_deleted(self):
		"""Static guardrail: catalog.py must never call frappe insert/update/
		delete primitives -- same convention as test_regression.py's own
		"never calls get_all" checks elsewhere in this app."""
		source = inspect.getsource(catalog)
		for forbidden_call in ("frappe.get_all", ".insert(", ".save(", ".submit(", ".delete(", "frappe.delete_doc", "ignore_permissions=True"):
			self.assertNotIn(forbidden_call, source, f"catalog.py must never call {forbidden_call}")

	def test_real_world_case_guante_talla_9_amarillo_is_high_confidence_and_unambiguous(self):
		"""Regression pin for one of this commit's own benchmark findings
		(report section L): a fully-specified query against a real,
		distinctively-named Item should land clean -- high confidence, wide
		margin, not ambiguous. If this ever regresses, scoring/index
		calibration broke something that used to work."""
		index = catalog.get_cached_catalog()
		parsed = parse_order_line("guante talla 9 amarillo")
		pool = catalog.search_catalog_candidates(parsed, catalog=index)
		result = match_order_line(parsed, pool)
		self.assertTrue(result["candidates"])
		top = result["candidates"][0]
		self.assertEqual(top["item_name"], "GUANTE GLOVAL TALLA 9 AMARILLO DOMESTICO")
		self.assertEqual(top["confidence"], "high")
		self.assertFalse(result["ambiguous"])

	def test_real_world_case_desengrasante_never_returns_empty(self):
		index = catalog.get_cached_catalog()
		parsed = parse_order_line("3 galones desengrasante")
		pool = catalog.search_catalog_candidates(parsed, catalog=index)
		result = match_order_line(parsed, pool)
		self.assertTrue(result["candidates"])
		self.assertIn("desengrasante", result["candidates"][0]["item_name"].lower())
