# -*- coding: utf-8 -*-
"""Commit 25.8.2 -- tests for fabergray_erp.quick_order.scoring /
fabergray_erp.quick_order.matcher (pure functions, no DB, no fixtures,
synthetic candidates only -- see this commit's own report, section "22.
NO catalogo real todavia": Commit 25.8.3 is what feeds this from the real
~2794-item catalog, not this file).

Same IntegrationTestCase-for-discovery-only convention as
test_quick_order_parser.py / test_geocoding.py -- nothing here reads or
writes anything through frappe.
"""

import ast
import inspect

from frappe.tests import IntegrationTestCase

from fabergray_erp.quick_order import matcher, scoring
from fabergray_erp.quick_order.matcher import build_candidate, match_order_line, match_order_text, suggested_item
from fabergray_erp.quick_order.parser import parse_order_line, parse_order_text

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


# Commit 25.8.2 brief, section 3 -- the base synthetic catalog every test
# below matches against, plus BOLSA NEGRA 80X100 (ambiguity/measure-conflict
# coverage) and a second guante color/talla pair (talla + color conflict
# coverage) exactly as the brief's own section 3 anticipates ("Agregar
# candidatos adicionales si son necesarios para probar ambigüedad").
def _synthetic_candidates():
	return [
		build_candidate("GLV-NIT-NEG-L", "GUANTE NITRILO NEGRO TALLA L"),
		build_candidate("GLV-NIT-NEG-M", "GUANTE NITRILO NEGRO TALLA M"),
		build_candidate("GLV-LAT-BLA-L", "GUANTE LATEX BLANCO TALLA L"),
		build_candidate("BOL-NEG-7090", "BOLSA NEGRA 70X90"),
		build_candidate("BOL-NEG-80100", "BOLSA NEGRA 80X100"),
		build_candidate("DESENG-GAL", "DESENGRASANTE 1 GALON"),
	]


class TestQuickOrderStaysPureText(IntegrationTestCase):
	"""T./R./S. -- static guardrails, same convention as
	test_quick_order_parser.py's own TestQuickOrderStaysPureText, extended to
	scoring.py/matcher.py."""

	def test_scoring_and_matcher_never_import_frappe(self):
		for module in (scoring, matcher):
			tree = ast.parse(inspect.getsource(module))
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					names = [alias.name.split(".")[0] for alias in node.names]
					self.assertNotIn("frappe", names, f"{module.__name__} imports frappe")
				elif isinstance(node, ast.ImportFrom):
					root = (node.module or "").split(".")[0]
					self.assertNotEqual(root, "frappe", f"{module.__name__} imports from frappe")

	def test_candidate_never_carries_economic_or_stock_fields(self):
		"""R./S. -- Candidate's own field set is a closed, explicit allowlist
		(item_code/item_name/normalized_name/tokens/description/item_group/
		stock_uom) -- no rate/price/valuation/cost field could ever leak
		through it, by construction, the same guarantee api/ventas.py's own
		get_item_info() gives (Commit 25.8 audit, section C)."""
		candidate = build_candidate("X-1", "GUANTE NITRILO NEGRO TALLA L", description="d", item_group="g", stock_uom="Unidad")
		self.assertEqual(
			set(candidate.keys()),
			{"item_code", "item_name", "normalized_name", "tokens", "description", "item_group", "stock_uom"},
		)
		forbidden = {"rate", "price", "price_list_rate", "valuation_rate", "standard_rate", "cost", "amount", "stock", "qty_disponible"}
		self.assertEqual(forbidden & set(candidate.keys()), set())

	def test_candidate_tokens_reuse_the_exact_parser_pipeline(self):
		"""Sanity/documentation test: a Candidate's tokens come from the same
		normalize_text()/extract_tokens() pipeline parse_order_line() uses --
		this is what makes their tokens directly comparable at all.

		Commit 25.8.4 -- expected tokens dict now also carries the
		"presentation" key (see parser.extract_tokens()'s own docstring);
		"GUANTE NITRILO NEGRO TALLA L" has no presentation word in it, so it
		comes back empty, {"primary": None, "contained": []}."""
		candidate = build_candidate("GLV-NIT-NEG-L", "GUANTE NITRILO NEGRO TALLA L")
		self.assertEqual(candidate["normalized_name"], "guante nitrilo negro talla l")
		self.assertEqual(
			candidate["tokens"],
			{
				"generic": ["guante", "nitrilo"],
				"measure": [],
				"size": ["l"],
				"color": ["negro"],
				"presentation": {"primary": None, "contained": []},
			},
		)


class TestQuickOrderRealWorldCases(IntegrationTestCase):
	"""A-H from the Commit 25.8.2 brief's own section 19."""

	def test_a_2_cajas_guantes_talla_l_negro(self):
		result = match_order_line(parse_order_line("2 cajas guantes talla L negro"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "GLV-NIT-NEG-L")
		self.assertEqual(result["candidates"][0]["conflicts"], [])
		self.assertFalse(result["ambiguous"])

	def test_b_guantes_talla_m_negro(self):
		result = match_order_line(parse_order_line("guantes talla M negro"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "GLV-NIT-NEG-M")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_c_guantes_talla_l_blanco(self):
		result = match_order_line(parse_order_line("guantes talla L blanco"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "GLV-LAT-BLA-L")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_d_1_bolsa_negra_70_por_90(self):
		result = match_order_line(parse_order_line("1 bolsa negra 70 por 90"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "BOL-NEG-7090")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_e_bolsa_negra_80x100(self):
		result = match_order_line(parse_order_line("bolsa negra 80x100"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "BOL-NEG-80100")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_f_3_galones_desengrasante(self):
		result = match_order_line(parse_order_line("3 galones desengrasante"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "DESENG-GAL")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_g_guantes_negros_is_ambiguous(self):
		"""No talla was specified -- both NEGRO variants (L and M) score
		close enough to trigger the ambiguity flag (section 17): the whole
		point is that the UI must never auto-pick one over the other here."""
		result = match_order_line(parse_order_line("guantes negros"), _synthetic_candidates())
		top_codes = {c["item_code"] for c in result["candidates"][:2]}
		self.assertEqual(top_codes, {"GLV-NIT-NEG-L", "GLV-NIT-NEG-M"})
		self.assertTrue(result["ambiguous"])
		self.assertIsNotNone(result["score_margin"])
		self.assertLess(result["score_margin"], scoring.AMBIGUITY_MARGIN_THRESHOLD)
		# blanco/latex must be strictly behind both negro variants
		self.assertNotEqual(result["candidates"][0]["item_code"], "GLV-LAT-BLA-L")

	def test_h_producto_inexistente_returns_no_candidates(self):
		result = match_order_line(parse_order_line("producto inexistente completamente"), _synthetic_candidates())
		self.assertEqual(result["candidates"], [])
		self.assertIsNone(result["score_margin"])
		self.assertFalse(result["ambiguous"])


class TestQuickOrderExplicitConflicts(IntegrationTestCase):
	"""I/J/K -- score_candidate() itself, isolated from ranking, so the
	conflict entry's own shape is asserted directly."""

	def test_i_talla_l_vs_talla_m_is_an_explicit_conflict(self):
		line = parse_order_line("guantes talla L negro")
		candidate = build_candidate("GLV-NIT-NEG-M", "GUANTE NITRILO NEGRO TALLA M")
		result = scoring.score_candidate(line, candidate)
		self.assertIn({"category": "size", "order_value": ["l"], "candidate_value": ["m"]}, result["conflicts"])

	def test_j_negro_vs_blanco_is_an_explicit_conflict(self):
		line = parse_order_line("guantes talla L negro")
		candidate = build_candidate("GLV-LAT-BLA-L", "GUANTE LATEX BLANCO TALLA L")
		result = scoring.score_candidate(line, candidate)
		self.assertIn({"category": "color", "order_value": ["negro"], "candidate_value": ["blanco"]}, result["conflicts"])

	def test_k_70x90_vs_80x100_is_an_explicit_conflict(self):
		line = parse_order_line("bolsa negra 70x90")
		candidate = build_candidate("BOL-NEG-80100", "BOLSA NEGRA 80X100")
		result = scoring.score_candidate(line, candidate)
		self.assertIn(
			{"category": "measure", "order_value": ["70x90"], "candidate_value": ["80x100"]}, result["conflicts"]
		)

	def test_matched_generic_word_never_offsets_an_explicit_conflict(self):
		"""Section 4's own rule, checked directly on the score: sharing
		"guante" can never make a talla-L order prefer the talla-M candidate
		over the talla-L one."""
		line = parse_order_line("guantes talla L negro")
		correct = scoring.score_candidate(line, build_candidate("GLV-NIT-NEG-L", "GUANTE NITRILO NEGRO TALLA L"))
		wrong_size = scoring.score_candidate(line, build_candidate("GLV-NIT-NEG-M", "GUANTE NITRILO NEGRO TALLA M"))
		self.assertGreater(correct["score"], wrong_size["score"])
		self.assertGreaterEqual(correct["score"] - wrong_size["score"], scoring.SIZE_CONFLICT_PENALTY // 2)


class TestQuickOrderTypoTolerance(IntegrationTestCase):
	def test_l_typo_guate_still_finds_guante_without_ignoring_talla_color(self):
		result = match_order_line(parse_order_line("guate negro talla l"), _synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "GLV-NIT-NEG-L")
		# the talla-M candidate must still lose despite sharing the same typo'd token
		wrong_size = next(c for c in result["candidates"] if c["item_code"] == "GLV-NIT-NEG-M")
		self.assertLess(wrong_size["score"], result["candidates"][0]["score"])
		self.assertTrue(any(c["category"] == "size" for c in wrong_size["conflicts"]))

	def test_typo_never_reaches_exact_match_score(self):
		"""Auxiliary means auxiliary: a fuzzy generic match must always score
		strictly lower than the identical line typed without the typo."""
		clean = match_order_line(parse_order_line("guante negro talla l"), _synthetic_candidates())
		typo = match_order_line(parse_order_line("guate negro talla l"), _synthetic_candidates())
		self.assertLess(typo["candidates"][0]["score"], clean["candidates"][0]["score"])

	def test_unrelated_words_never_fuzzy_match(self):
		"""No fuzzy global indiscriminado (section 18): "producto" must never
		fuzzy-match "desengrasante" just because both are longish words."""
		exact, fuzzy = scoring.generic_token_matches(["producto"], ["desengrasante"])
		self.assertEqual(exact, set())
		self.assertEqual(fuzzy, [])


class TestQuickOrderLimitsAndOrdering(IntegrationTestCase):
	def test_m_never_returns_more_than_limit(self):
		result = match_order_line(parse_order_line("guantes"), _synthetic_candidates(), limit=2)
		self.assertLessEqual(len(result["candidates"]), 2)

	def test_n_candidates_sorted_score_descending(self):
		result = match_order_line(parse_order_line("guantes negros"), _synthetic_candidates())
		scores = [c["score"] for c in result["candidates"]]
		self.assertEqual(scores, sorted(scores, reverse=True))

	def test_o_score_always_between_0_and_100(self):
		for candidate in _synthetic_candidates():
			for text in ("2 cajas guantes talla L negro", "producto inexistente", "3 galones desengrasante"):
				result = scoring.score_candidate(parse_order_line(text), candidate)
				self.assertGreaterEqual(result["score"], 0)
				self.assertLessEqual(result["score"], 100)

	def test_default_limit_is_five(self):
		result = match_order_line(parse_order_line("guantes"), _synthetic_candidates())
		self.assertLessEqual(len(result["candidates"]), 5)

	def test_match_order_text_preserves_line_order_and_count(self):
		parsed_lines = parse_order_text(
			"2 cajas guantes talla L negro\n1 bolsa negra 70 por 90\n3 galones desengrasante"
		)
		results = match_order_text(parsed_lines, _synthetic_candidates())
		self.assertEqual(len(results), 3)
		self.assertEqual(results[0]["candidates"][0]["item_code"], "GLV-NIT-NEG-L")
		self.assertEqual(results[1]["candidates"][0]["item_code"], "BOL-NEG-7090")
		self.assertEqual(results[2]["candidates"][0]["item_code"], "DESENG-GAL")


class TestQuickOrderConfidenceAndAmbiguity(IntegrationTestCase):
	def test_p_confidence_thresholds_are_centralized(self):
		self.assertEqual(scoring.classify_confidence(100), "high")
		self.assertEqual(scoring.classify_confidence(scoring.CONFIDENCE_HIGH_THRESHOLD), "high")
		self.assertEqual(scoring.classify_confidence(scoring.CONFIDENCE_HIGH_THRESHOLD - 1), "medium")
		self.assertEqual(scoring.classify_confidence(scoring.CONFIDENCE_MEDIUM_THRESHOLD), "medium")
		self.assertEqual(scoring.classify_confidence(scoring.CONFIDENCE_MEDIUM_THRESHOLD - 1), "low")
		self.assertEqual(scoring.classify_confidence(0), "low")

	def test_q_score_margin_matches_top_two_candidates(self):
		result = match_order_line(parse_order_line("guantes negros"), _synthetic_candidates())
		self.assertEqual(
			result["score_margin"], result["candidates"][0]["score"] - result["candidates"][1]["score"]
		)

	def test_q_unambiguous_case_has_a_wide_margin(self):
		"""Test A's own line is the negative case for ambiguity: a clean,
		fully-specified order should have a comfortable margin over its
		nearest (conflicting) rival."""
		result = match_order_line(parse_order_line("2 cajas guantes talla L negro"), _synthetic_candidates())
		self.assertFalse(result["ambiguous"])
		self.assertGreaterEqual(result["score_margin"], scoring.AMBIGUITY_MARGIN_THRESHOLD)

	def test_suggested_item_preselects_only_unambiguous_high_confidence(self):
		clean = match_order_line(parse_order_line("2 cajas guantes talla L negro"), _synthetic_candidates())
		suggestion = suggested_item(clean)
		self.assertIsNotNone(suggestion)
		self.assertTrue(suggestion["preselected"])
		self.assertEqual(suggestion["item_code"], "GLV-NIT-NEG-L")

	def test_suggested_item_never_preselects_an_ambiguous_line(self):
		ambiguous = match_order_line(parse_order_line("guantes negros"), _synthetic_candidates())
		self.assertIsNone(suggested_item(ambiguous))

	def test_suggested_item_is_none_for_no_candidates(self):
		empty = match_order_line(parse_order_line("producto inexistente completamente"), _synthetic_candidates())
		self.assertIsNone(suggested_item(empty))

	def test_suggested_item_never_preselects_low_confidence(self):
		suggestion = suggested_item(
			{"candidates": [{"item_code": "X", "score": 10, "confidence": "low"}], "ambiguous": False}
		)
		self.assertIsNone(suggestion)

	def test_suggested_item_marks_medium_as_not_preselected(self):
		suggestion = suggested_item(
			{"candidates": [{"item_code": "X", "score": 75, "confidence": "medium"}], "ambiguous": False}
		)
		self.assertIsNotNone(suggestion)
		self.assertFalse(suggestion["preselected"])


class TestQuickOrderNoEconomicOrStockDependency(IntegrationTestCase):
	"""R./S., driven end-to-end through the real matching pipeline (not just
	the Candidate structure check in TestQuickOrderStaysPureText above)."""

	def test_full_match_result_never_contains_price_or_stock_keys(self):
		result = match_order_line(parse_order_line("2 cajas guantes talla L negro"), _synthetic_candidates())
		forbidden = {"rate", "price", "price_list_rate", "valuation_rate", "standard_rate", "cost", "amount", "qty_disponible"}
		for candidate_result in result["candidates"]:
			self.assertEqual(forbidden & set(candidate_result.keys()), set())
