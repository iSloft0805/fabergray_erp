# -*- coding: utf-8 -*-
"""Commit 25.8.4 -- tests for the `presentation` token category added this
commit to `parser.extract_tokens()`, plus its scoring in `scoring.py` and
its retrieval signal in `matcher.py`/`catalog.py`.

Pure Python throughout -- no DB, matching the letter list in this commit's
own brief, section 18 (A-M, O-R here; N is a documented non-implementation,
see TestPackCountWasNotImplemented; S/T/U are the EXISTING
test_quick_order_parser.py/test_quick_order_matching.py/
test_quick_order_catalog.py suites, re-run as regression, not duplicated
into a new file here).
"""

from frappe.tests import IntegrationTestCase

from fabergray_erp.quick_order import scoring
from fabergray_erp.quick_order.matcher import build_candidate, match_order_line
from fabergray_erp.quick_order.normalizer import normalize_text
from fabergray_erp.quick_order.parser import PRESENTATION_ALIASES, UOM_ALIASES, extract_tokens, parse_order_line

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestNewUomWords(IntegrationTestCase):
	"""A-E: litro/ml/kg/gramos/rollo -- litro/ml/kg/gramos are real,
	confirmed UOM words (Commit 25.8.3's own catalog audit); "rollo" is
	deliberately NOT one (see UOM_ALIASES's own comment) -- it is a
	presentation-only word instead, confirmed by its own test below."""

	# A. litro
	def test_litro_is_a_recognized_uom(self):
		self.assertEqual(parse_order_line("2 litros desengrasante")["detected_uom"], "litro")
		self.assertEqual(parse_order_line("1 litro desengrasante")["detected_uom"], "litro")

	# B. ml
	def test_ml_is_a_recognized_uom(self):
		line = parse_order_line("500 ml desengrasante")
		self.assertEqual(line["qty"], 500)
		self.assertEqual(line["detected_uom"], "ml")

	# C. kg
	def test_kg_is_a_recognized_uom(self):
		self.assertEqual(parse_order_line("2 kg jabon en polvo")["detected_uom"], "kg")

	# D. gramos
	def test_gramos_is_a_recognized_uom(self):
		self.assertEqual(parse_order_line("300 gramos jabon")["detected_uom"], "gramo")
		self.assertEqual(parse_order_line("1 gramo jabon")["detected_uom"], "gramo")

	# E. rollo (presentation, NOT a qty-prefix UOM -- see UOM_ALIASES's own comment)
	def test_rollo_is_presentation_not_uom(self):
		self.assertNotIn("rollo", UOM_ALIASES)
		self.assertNotIn("rollos", UOM_ALIASES)
		line = parse_order_line("2 rollos papel higienico")
		self.assertIsNone(line["detected_uom"])
		self.assertEqual(line["tokens"]["presentation"]["primary"], "rollo")


class TestMedioGalon(IntegrationTestCase):
	# F. medio galon
	def test_medio_galon_is_one_canonical_presentation_token(self):
		tokens = extract_tokens("medio galon desengrasante")
		self.assertEqual(tokens["presentation"]["primary"], "medio_galon")
		self.assertEqual(tokens["generic"], ["desengrasante"])

	def test_medio_galon_is_distinct_from_plain_galon(self):
		"""Section 5's own warning -- "no asumir que medio solo significa
		0.5 de cualquier UOM": kept as two different canonical values on
		purpose, never unified/converted."""
		medio = extract_tokens("medio galon desengrasante")["presentation"]["primary"]
		llano = extract_tokens("galon desengrasante")["presentation"]["primary"]
		self.assertNotEqual(medio, llano)
		self.assertEqual(llano, "galon")

	def test_no_plural_form_of_medio_galon_is_guessed(self):
		"""No real "medio galones" was ever found in the catalog audit (this
		commit's own report, section C) -- so `_MEDIO_GALON_RE` (singular
		"galon" only, `\\bgalon\\b`) never matches it as ONE phrase. It still
		safely falls back to the ordinary single-word pass, which DOES
		recognize "galones" on its own (PRESENTATION_ALIASES already maps
		plural "galones" -> "galon" for the plain, non-"medio" case) --
		"medio" is simply left as an uninterpreted generic word instead of
		being silently folded into a guessed "medio_galon". Never crashes,
		never invents the untested plural phrase."""
		tokens = extract_tokens("medio galones desengrasante")
		self.assertEqual(tokens["presentation"]["primary"], "galon")
		self.assertIn("medio", tokens["generic"])


class TestPresentationTokens(IntegrationTestCase):
	# G. presentación paquete
	def test_presentation_paquete(self):
		tokens = extract_tokens("paquete bolsa negra")
		self.assertEqual(tokens["presentation"]["primary"], "paquete")
		self.assertNotIn("paquete", tokens["generic"])

	# H. presentación caja
	def test_presentation_caja(self):
		tokens = extract_tokens("caja guante nitrilo negro talla l")
		self.assertEqual(tokens["presentation"]["primary"], "caja")
		self.assertNotIn("caja", tokens["generic"])

	# I. primary presentation (position-based)
	def test_primary_presentation_is_the_earliest_match(self):
		tokens = extract_tokens("bulto bolsa 70x90 blanca x 40 paquete x 10 und")
		self.assertEqual(tokens["presentation"]["primary"], "bulto")

	# J. contained presentation
	def test_contained_presentation_is_whatever_comes_after_primary(self):
		tokens = extract_tokens("bulto bolsa 70x90 blanca x 40 paquete x 10 und")
		self.assertIn("paquete", tokens["presentation"]["contained"])
		self.assertNotIn("paquete", tokens["presentation"]["primary"] or "")

	def test_no_presentation_word_leaves_primary_none_and_contained_empty(self):
		tokens = extract_tokens("guante nitrilo negro talla l")
		self.assertEqual(tokens["presentation"], {"primary": None, "contained": []})

	def test_bulto_cunete_garrafa_botella_are_recognized(self):
		"""Confirmed real, all four, in this commit's own catalog audit
		(BULTO 100, CUÑETE 130, GARRAFA 17, BOTELLA 83 hits) before being
		added -- brief section 3: "NO agregar vocabulario sin evidencia
		real"."""
		self.assertEqual(PRESENTATION_ALIASES["bulto"], "bulto")
		self.assertEqual(PRESENTATION_ALIASES["cunete"], "cunete")
		self.assertEqual(PRESENTATION_ALIASES["garrafa"], "garrafa")
		self.assertEqual(PRESENTATION_ALIASES["botella"], "botella")
		# "cuñete" is looked up post strip_accents -- the raw accented form
		# is never a dict key, "cunete" is (Commit 25.8.1's own strip_accents
		# behaviour, unchanged). extract_tokens() itself expects ALREADY
		# normalize_text()-ed input (see its own docstring) -- normalize_text()
		# here first, exactly like parse_order_line() does internally,
		# rather than passing the raw accented text directly.
		tokens = extract_tokens(normalize_text("cuñete desengrasante multiusos"))
		self.assertEqual(tokens["presentation"]["primary"], "cunete")


def _presentation_synthetic_candidates():
	"""A separate, presentation-focused synthetic catalog -- deliberately
	NOT reusing test_quick_order_matching.py's own base 6 (none of those
	have a presentation word in their name at all, by design, so they
	cannot exercise this commit's own scoring path)."""
	return [
		build_candidate("BOL-PAQ-7090", "PAQUETE BOLSA NEGRA 70X90 X 10 UND"),
		build_candidate("BOL-BULTO-7090", "BULTO BOLSA 70X90 NEGRA X 40 PAQUETE X 10 UND"),
		build_candidate("DES-BOTELLA", "BOTELLA DESENGRASANTE MULTIUSOS"),
		build_candidate("DES-GALON", "GALON DESENGRASANTE MULTIUSOS"),
		build_candidate("DES-MEDIO-GALON", "MEDIO GALON DESENGRASANTE MULTIUSOS"),
		build_candidate("PAP-ROLLO", "ROLLO PAPEL HIGIENICO INDUSTRIAL"),
		build_candidate("PAP-PAQUETE", "PAQUETE PAPEL HIGIENICO INDUSTRIAL"),
	]


class TestPresentationScoring(IntegrationTestCase):
	# K. paquete vs bulto (Commit 25.8.3's own real false positive #1,
	# reproduced synthetically per this commit's brief section 12 -- "no
	# hardcodear item_code", so this uses invented codes, never
	# "01942"/"00251").
	def test_k_paquete_beats_bulto_when_paquete_was_requested(self):
		line = parse_order_line("1 paquete bolsa negra 70x90")
		result = match_order_line(line, _presentation_synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "BOL-PAQ-7090")

	def test_k_bulto_is_not_discarded_only_penalized(self):
		"""Section 8's own explicit rule: BULTO must still appear as a
		lower-ranked alternative, never disappear entirely -- and it should
		get partial credit back for CONTAINING "paquete" in its own name."""
		line = parse_order_line("1 paquete bolsa negra 70x90")
		result = match_order_line(line, _presentation_synthetic_candidates())
		codes = [c["item_code"] for c in result["candidates"]]
		self.assertIn("BOL-BULTO-7090", codes)
		bulto_result = next(c for c in result["candidates"] if c["item_code"] == "BOL-BULTO-7090")
		self.assertLess(bulto_result["score"], result["candidates"][0]["score"])
		self.assertTrue(any(c["category"] == "presentation" for c in bulto_result["conflicts"]))

	def test_k_presentation_conflict_alone_never_beats_a_measure_or_color_conflict(self):
		"""Section 17's own priority rule, checked directly: the
		presentation weights must stay small enough that a presentation
		conflict is a softer penalty than a measure/size/color one."""
		self.assertLess(scoring.PRESENTATION_CONFLICT_PENALTY, scoring.COLOR_CONFLICT_PENALTY)
		self.assertLess(scoring.PRESENTATION_CONFLICT_PENALTY, scoring.SIZE_CONFLICT_PENALTY)
		self.assertLess(scoring.PRESENTATION_CONFLICT_PENALTY, scoring.MEASURE_CONFLICT_PENALTY)
		self.assertLess(scoring.PRESENTATION_MATCH_POINTS, scoring.COLOR_MATCH_POINTS)

	# L. botella vs galon
	def test_l_botella_beats_galon_when_botella_was_requested(self):
		line = parse_order_line("botella desengrasante")
		result = match_order_line(line, _presentation_synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "DES-BOTELLA")

	# M. galon vs medio galon
	def test_m_galon_does_not_prefer_medio_galon(self):
		line = parse_order_line("3 galones desengrasante")
		result = match_order_line(line, _presentation_synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "DES-GALON")
		medio_result = next(c for c in result["candidates"] if c["item_code"] == "DES-MEDIO-GALON")
		self.assertLess(medio_result["score"], result["candidates"][0]["score"])
		self.assertTrue(any(c["category"] == "presentation" for c in medio_result["conflicts"]))

	def test_m_medio_galon_query_prefers_medio_galon_candidate(self):
		"""The symmetric case: asking for "medio galon" should prefer the
		medio-galon candidate over the plain galon one -- proves this isn't
		a one-directional bias."""
		line = parse_order_line("medio galon desengrasante")
		result = match_order_line(line, _presentation_synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "DES-MEDIO-GALON")

	def test_rollo_vs_paquete_same_product_different_presentation(self):
		"""A second, independent presentation pair (not bolsa/desengrasante)
		-- confirms the mechanism generalizes."""
		line = parse_order_line("2 rollos papel higienico")
		result = match_order_line(line, _presentation_synthetic_candidates())
		self.assertEqual(result["candidates"][0]["item_code"], "PAP-ROLLO")


class TestPackCountWasNotImplemented(IntegrationTestCase):
	"""N. pack_count -- considered, deliberately declined this commit (see
	report section E for the full reasoning: real "X <n> UND" patterns are
	too heterogeneous -- simple "X 50 UND" vs nested "X 40 PAQ X 10 UND",
	514/2794 real hits, meaningfully different semantics). This test is a
	documentation guardrail, not a feature test: it pins that NO code path
	in this package invents a `pack_count` key, so a future commit that adds
	it does so as a deliberate, visible decision."""

	def test_no_pack_count_key_anywhere_in_extracted_tokens(self):
		tokens = extract_tokens("bulto bolsa 70x90 blanca x 40 paquete x 10 und")
		self.assertNotIn("pack_count", tokens)
		self.assertNotIn("pack_count", tokens["presentation"])

	def test_no_pack_count_key_on_a_parsed_line(self):
		line = parse_order_line("1 paquete bolsa blanca 70x90")
		self.assertNotIn("pack_count", line)


class TestFractionalTalla(IntegrationTestCase):
	# O. talla 7 1/2
	def test_o_talla_7_1_2(self):
		tokens = extract_tokens("guante duralon eterna talla 7 1/2 negro calibre 35")
		self.assertEqual(tokens["size"], ["7 1/2"])
		self.assertNotIn("7", tokens["generic"])

	# P. talla 8 1/2
	def test_p_talla_8_1_2(self):
		tokens = extract_tokens("guante duralon eterna talla 8 1/2 negro calibre 35")
		self.assertEqual(tokens["size"], ["8 1/2"])

	# Q. talla/rango fraccionario
	def test_q_talla_range_with_fraction(self):
		tokens = extract_tokens("guante pantera bicolor talla 8-8 1/2 negro cal 18")
		self.assertEqual(tokens["size"], ["8-8 1/2"])

	def test_fractional_talla_is_never_reduced_to_its_leading_integer(self):
		"""The regression this whole category exists to prevent: "7 1/2"
		must never collapse to "7" -- doing so is what caused Commit
		25.8.3's own false positive #3 ("guante negro talla 7" incorrectly
		matching a real TALLA-7-exact item over the TALLA-7-1/2 one)."""
		tokens_half = extract_tokens("talla 7 1/2")
		tokens_whole = extract_tokens("talla 7")
		self.assertNotEqual(tokens_half["size"], tokens_whole["size"])
		self.assertEqual(tokens_whole["size"], ["7"])

	def test_malformed_legacy_talla_degrades_gracefully_without_guessing(self):
		""""TALLA 7-7/2" (no space before the fraction) is a real, malformed
		legacy Item name (Commit 25.8.4's own audit) -- this module never
		tries to auto-correct it; it partially matches ("7-7") rather than
		crashing or grabbing garbage. Reported as a data-quality issue, not
		"fixed" here (this commit's own brief, section 14)."""
		tokens = extract_tokens("guante pantera bicolor talla 7-7/2 negro cal 18")
		self.assertEqual(tokens["size"], ["7-7"])


class TestBolsaDualRoleStillWorks(IntegrationTestCase):
	"""R. Regression: "bolsa" must still work as a product-type word, not a
	presentation word -- Commit 25.8.2/25.8.3's own established behaviour,
	explicitly preserved (this commit's own brief, section 6)."""

	def test_bolsa_is_not_in_presentation_aliases(self):
		self.assertNotIn("bolsa", PRESENTATION_ALIASES)
		self.assertNotIn("bolsas", PRESENTATION_ALIASES)

	def test_bolsa_negra_70x90_still_resolves_with_no_presentation_signal(self):
		"""Both sides (line and candidate) leave presentation empty for a
		pure bolsa-as-product case -- the mechanism added this commit stays
		fully inactive/neutral here, exactly like before this commit."""
		line = parse_order_line("bolsa negra 70x90")
		self.assertIsNone(scoring.effective_primary_presentation(line))
		candidate = build_candidate("BOL-NEG-7090", "BOLSA NEGRA 70X90")
		self.assertIsNone(candidate["tokens"]["presentation"]["primary"])
		result = match_order_line(line, [candidate])
		self.assertEqual(result["candidates"][0]["item_code"], "BOL-NEG-7090")
		self.assertEqual(result["candidates"][0]["conflicts"], [])

	def test_1_bolsa_negra_70x90_detected_uom_is_not_treated_as_presentation(self):
		""""bolsa" as `detected_uom` (line starts with a quantity) must NOT
		be promoted to a presentation signal -- only genuine presentation
		concepts (caja/paquete/galon/litro/ml/kg/gramo/bulto/...) are."""
		line = parse_order_line("1 bolsa negra 70x90")
		self.assertEqual(line["detected_uom"], "bolsa")
		self.assertIsNone(scoring.effective_primary_presentation(line))
