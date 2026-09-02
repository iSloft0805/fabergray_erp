# -*- coding: utf-8 -*-
"""Commit 25.8.1 -- tests for fabergray_erp.quick_order.normalizer /
fabergray_erp.quick_order.parser (pure functions, no DB, no fixtures).

IntegrationTestCase only for this app's own consistent test-discovery
convention (see test_geocoding.py's own docstring for the same reasoning) --
nothing in this file reads or writes anything through frappe. No Item, no
Sales Order, no cart, no Item Alias is ever created here; this commit's
scope is text interpretation only (see quick_order/parser.py's own module
docstring).
"""

import ast
import inspect

from frappe.tests import IntegrationTestCase

from fabergray_erp.quick_order import normalizer, parser
from fabergray_erp.quick_order.parser import UOM_ALIASES, parse_order_line, parse_order_text

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestQuickOrderStaysPureText(IntegrationTestCase):
	"""Static guardrail (same convention as test_regression.py's own
	as_dict()/get_order_detail() check): normalizer.py/parser.py must never
	import frappe at all -- not `frappe.db`, not `frappe` itself -- so this
	commit can never accidentally grow a DB dependency, an Item lookup, or a
	Sales Order write without a test catching it here first."""

	def test_normalizer_and_parser_never_import_frappe(self):
		for module in (normalizer, parser):
			tree = ast.parse(inspect.getsource(module))
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					names = [alias.name.split(".")[0] for alias in node.names]
					self.assertNotIn("frappe", names, f"{module.__name__} imports frappe")
				elif isinstance(node, ast.ImportFrom):
					root = (node.module or "").split(".")[0]
					self.assertNotEqual(root, "frappe", f"{module.__name__} imports from frappe")


class TestQuickOrderQuantity(IntegrationTestCase):
	# A. cantidad entera ----------------------------------------------------
	def test_qty_integer(self):
		line = parse_order_line("2 cajas guantes talla L negro")
		self.assertEqual(line["qty"], 2)
		self.assertIsInstance(line["qty"], int)

	# B. cantidad decimal punto ----------------------------------------------
	def test_qty_decimal_dot(self):
		line = parse_order_line("2.5 galones desengrasante")
		self.assertEqual(line["qty"], 2.5)

	# C. cantidad decimal coma -----------------------------------------------
	def test_qty_decimal_comma(self):
		line = parse_order_line("2,5 galones desengrasante")
		self.assertEqual(line["qty"], 2.5)

	# D. cantidad ausente -> 1 ------------------------------------------------
	def test_qty_absent_defaults_to_one(self):
		line = parse_order_line("guantes negros")
		self.assertEqual(line["qty"], 1)
		self.assertIsInstance(line["qty"], int)
		self.assertIsNone(line["detected_uom"])


class TestQuickOrderUom(IntegrationTestCase):
	def test_every_uom_alias_is_recognized(self):
		"""Drives every raw key UOM_ALIASES itself declares through the real
		parser -- keeps the regex in parser.py and the dict in sync (see
		_UOM_WORD_RE's own docstring comment)."""
		for raw, canonical in UOM_ALIASES.items():
			with self.subTest(raw=raw):
				line = parse_order_line(f"3 {raw} algo")
				self.assertEqual(line["detected_uom"], canonical)
				self.assertEqual(line["qty"], 3)

	# E. caja/cajas -----------------------------------------------------------
	def test_uom_caja_singular_and_plural(self):
		self.assertEqual(parse_order_line("1 caja guantes")["detected_uom"], "caja")
		self.assertEqual(parse_order_line("2 cajas guantes")["detected_uom"], "caja")

	# F. unidad/und/unidades ----------------------------------------------------
	def test_uom_unidad_variants(self):
		self.assertEqual(parse_order_line("1 unidad ambientador")["detected_uom"], "unidad")
		self.assertEqual(parse_order_line("3 unidades ambientador")["detected_uom"], "unidad")
		self.assertEqual(parse_order_line("5 und ambientador lavanda")["detected_uom"], "unidad")
		self.assertEqual(parse_order_line("5 und. ambientador lavanda")["detected_uom"], "unidad")

	# G. galón/galones ----------------------------------------------------------
	def test_uom_galon_variants(self):
		self.assertEqual(parse_order_line("1 galon desengrasante")["detected_uom"], "galon")
		self.assertEqual(parse_order_line("3 galones desengrasante")["detected_uom"], "galon")
		# accent on the raw text must not matter -- strip_accents runs first
		self.assertEqual(parse_order_line("1 galón desengrasante")["detected_uom"], "galon")

	# H. paquete ----------------------------------------------------------------
	def test_uom_paquete_variants(self):
		self.assertEqual(parse_order_line("1 paquete bolsas transparentes")["detected_uom"], "paquete")
		self.assertEqual(parse_order_line("2 paquetes bolsas transparentes")["detected_uom"], "paquete")

	# I. bolsa --------------------------------------------------------------------
	def test_uom_bolsa_variants(self):
		self.assertEqual(parse_order_line("1 bolsa negra 70x90")["detected_uom"], "bolsa")
		self.assertEqual(parse_order_line("2 bolsas negras")["detected_uom"], "bolsa")

	# J. par/pares ------------------------------------------------------------------
	def test_uom_par_variants(self):
		self.assertEqual(parse_order_line("1 par guantes")["detected_uom"], "par")
		self.assertEqual(parse_order_line("2 pares guantes amarillos talla 9")["detected_uom"], "par")

	def test_unrecognized_uom_word_is_never_guessed(self):
		"""No inventar (Commit 25.8 audit, section 5) -- a word that isn't in
		UOM_ALIASES leaves detected_uom as None, it is never fuzzily mapped
		to the closest known unit.

		Commit 25.8.4 -- switched the example word from "rollos" to
		"zunchos": "rollo"/"rollos" is now a real, confirmed PRESENTATION
		word (parser.PRESENTATION_ALIASES, added this same commit -- see
		test_quick_order_presentation.py), so it no longer lands in
		`generic` -- that is intentional, correct behaviour, not a
		regression of THIS test's actual point, which is about
		`detected_uom` never guessing. "zunchos" (strapping) is in neither
		UOM_ALIASES nor PRESENTATION_ALIASES, so it still proves the
		original point untouched."""
		line = parse_order_line("2 zunchos papel higienico")
		self.assertIsNone(line["detected_uom"])
		self.assertEqual(line["qty"], 2)
		self.assertIn("zunchos", line["tokens"]["generic"])


class TestQuickOrderMeasures(IntegrationTestCase):
	# K. 70 por 90 -> 70x90 -------------------------------------------------------
	def test_measure_por_normalizes_to_x(self):
		line = parse_order_line("1 bolsa negra 70 por 90")
		self.assertIn("70x90", line["tokens"]["measure"])

	# L. 70 x 90 -> 70x90 -----------------------------------------------------------
	def test_measure_spaced_x_normalizes(self):
		line = parse_order_line("1 bolsa negra 70 x 90")
		self.assertIn("70x90", line["tokens"]["measure"])

	# M. 70*90 -> 70x90 ---------------------------------------------------------------
	def test_measure_asterisk_normalizes(self):
		line = parse_order_line("1 bolsa negra 70*90")
		self.assertIn("70x90", line["tokens"]["measure"])

	# N. 70x90 no se interpreta como qty ------------------------------------------------
	def test_measure_is_never_read_as_quantity(self):
		line = parse_order_line("bolsa negra 70x90")
		self.assertEqual(line["qty"], 1)
		self.assertIn("70x90", line["tokens"]["measure"])

	def test_leading_measure_is_never_read_as_quantity(self):
		"""A line that happens to START with the first number of a
		measurement ("70 x 90 bolsa negra") must not be read as qty=70
		either -- the guard in _extract_quantity_and_uom() covers this, not
		just the more common "measure comes after the product name" case
		above."""
		line = parse_order_line("70 x 90 bolsa negra")
		self.assertEqual(line["qty"], 1)
		self.assertIsNone(line["detected_uom"])


class TestQuickOrderTokens(IntegrationTestCase):
	# O. talla L --------------------------------------------------------------------------
	def test_size_letter(self):
		line = parse_order_line("2 cajas guantes talla L negro")
		self.assertEqual(line["tokens"]["size"], ["l"])
		self.assertNotIn("talla", line["tokens"]["generic"])
		self.assertNotIn("l", line["tokens"]["generic"])

	# P. talla 9 ----------------------------------------------------------------------------
	def test_size_number(self):
		line = parse_order_line("2 pares guantes amarillos talla 9")
		self.assertEqual(line["tokens"]["size"], ["9"])

	# Q. color negro --------------------------------------------------------------------------
	def test_color_negro(self):
		self.assertEqual(parse_order_line("guantes negros")["tokens"]["color"], ["negro"])
		self.assertEqual(
			parse_order_line("2 cajas guantes talla L negro")["tokens"]["color"], ["negro"]
		)

	# R. color amarillo -----------------------------------------------------------------------
	def test_color_amarillo(self):
		line = parse_order_line("2 pares guantes amarillos talla 9")
		self.assertEqual(line["tokens"]["color"], ["amarillo"])
		self.assertNotIn("amarillos", line["tokens"]["generic"])

	def test_generic_token_is_whatever_is_left(self):
		line = parse_order_line("3 galones desengrasante")
		self.assertEqual(line["tokens"]["generic"], ["desengrasante"])
		self.assertEqual(line["tokens"]["size"], [])
		self.assertEqual(line["tokens"]["color"], [])
		self.assertEqual(line["tokens"]["measure"], [])


class TestQuickOrderTextHandling(IntegrationTestCase):
	# S. tildes ------------------------------------------------------------------------------
	def test_accents_are_stripped(self):
		self.assertEqual(normalizer.strip_accents("GALÓN"), "GALON")
		self.assertEqual(normalizer.normalize_text("BOTÓN"), "boton")
		# an accented product word must not block color/generic matching
		line = parse_order_line("2 cajas jabón en polvo")
		self.assertIn("jabon", line["tokens"]["generic"])

	# T. espacios extra -----------------------------------------------------------------------
	def test_extra_whitespace_is_collapsed(self):
		line = parse_order_line("   2    cajas   guantes    talla   L   negro   ")
		self.assertEqual(line["qty"], 2)
		self.assertEqual(line["detected_uom"], "caja")
		self.assertEqual(line["tokens"]["size"], ["l"])
		self.assertEqual(line["tokens"]["color"], ["negro"])

	def test_empty_line_still_returns_a_structured_result(self):
		"""parse_order_line() itself never skips a blank line -- that
		filtering is parse_order_text()'s own job (test U below).

		Commit 25.8.4 -- expected tokens dict now also carries the
		"presentation" key (see extract_tokens()'s own docstring); an empty
		line naturally produces an empty one, {"primary": None, "contained": []}."""
		line = parse_order_line("")
		self.assertEqual(line["source_text"], "")
		self.assertEqual(line["qty"], 1)
		self.assertIsNone(line["detected_uom"])
		self.assertEqual(
			line["tokens"],
			{
				"generic": [],
				"measure": [],
				"size": [],
				"color": [],
				"presentation": {"primary": None, "contained": []},
			},
		)


class TestParseOrderText(IntegrationTestCase):
	# U. líneas vacías --------------------------------------------------------------------------
	def test_blank_lines_are_skipped(self):
		text = "2 cajas guantes talla L negro\n\n   \n1 galon desengrasante"
		lines = parse_order_text(text)
		self.assertEqual(len(lines), 2)
		self.assertEqual(lines[0]["source_text"], "2 cajas guantes talla L negro")
		self.assertEqual(lines[1]["source_text"], "1 galon desengrasante")

	# V. varias líneas ----------------------------------------------------------------------------
	def test_multiple_lines_preserve_original_order(self):
		text = "\n".join(
			[
				"2 cajas guantes talla L negro",
				"1 bolsa negra 70 por 90",
				"3 galones desengrasante",
			]
		)
		lines = parse_order_text(text)
		self.assertEqual(len(lines), 3)
		self.assertEqual(lines[0]["qty"], 2)
		self.assertEqual(lines[0]["detected_uom"], "caja")
		self.assertEqual(lines[1]["qty"], 1)
		self.assertIn("70x90", lines[1]["tokens"]["measure"])
		self.assertEqual(lines[2]["qty"], 3)
		self.assertEqual(lines[2]["detected_uom"], "galon")

	def test_parse_order_text_of_empty_string_returns_empty_list(self):
		self.assertEqual(parse_order_text(""), [])
		self.assertEqual(parse_order_text("   \n  \n"), [])


class TestQuickOrderRealWorldCases(IntegrationTestCase):
	"""Every line from the Commit 25.8 audit's own section 7 ("casos reales
	importantes"), each asserted end-to-end in one place."""

	def test_2_cajas_guantes_talla_l_negro(self):
		line = parse_order_line("2 cajas guantes talla L negro")
		self.assertEqual(line["qty"], 2)
		self.assertEqual(line["detected_uom"], "caja")
		self.assertEqual(line["product_text"], "guantes talla l negro")
		self.assertEqual(line["tokens"]["generic"], ["guantes"])
		self.assertEqual(line["tokens"]["size"], ["l"])
		self.assertEqual(line["tokens"]["color"], ["negro"])

	def test_1_bolsa_negra_70_por_90(self):
		line = parse_order_line("1 bolsa negra 70 por 90")
		self.assertEqual(line["qty"], 1)
		self.assertEqual(line["detected_uom"], "bolsa")
		self.assertEqual(line["tokens"]["color"], ["negro"])
		self.assertIn("70x90", line["tokens"]["measure"])

	def test_3_galones_desengrasante(self):
		line = parse_order_line("3 galones desengrasante")
		self.assertEqual(line["qty"], 3)
		self.assertEqual(line["detected_uom"], "galon")
		self.assertEqual(line["product_text"], "desengrasante")
		self.assertEqual(line["tokens"]["generic"], ["desengrasante"])

	def test_2_paquetes_bolsas_transparentes(self):
		line = parse_order_line("2 paquetes bolsas transparentes")
		self.assertEqual(line["qty"], 2)
		self.assertEqual(line["detected_uom"], "paquete")
		self.assertEqual(line["tokens"]["color"], ["transparente"])
		self.assertIn("bolsas", line["tokens"]["generic"])

	def test_5_und_ambientador_lavanda(self):
		line = parse_order_line("5 und ambientador lavanda")
		self.assertEqual(line["qty"], 5)
		self.assertEqual(line["detected_uom"], "unidad")
		self.assertEqual(line["tokens"]["generic"], ["ambientador", "lavanda"])

	def test_guantes_negros(self):
		line = parse_order_line("guantes negros")
		self.assertEqual(line["qty"], 1)
		self.assertIsNone(line["detected_uom"])
		self.assertEqual(line["tokens"]["generic"], ["guantes"])
		self.assertEqual(line["tokens"]["color"], ["negro"])

	def test_bolsa_negra_70x90(self):
		line = parse_order_line("bolsa negra 70x90")
		self.assertEqual(line["qty"], 1)
		self.assertIsNone(line["detected_uom"])
		self.assertEqual(line["tokens"]["generic"], ["bolsa"])
		self.assertEqual(line["tokens"]["color"], ["negro"])
		self.assertIn("70x90", line["tokens"]["measure"])

	def test_1_galon_desengrasante(self):
		line = parse_order_line("1 galon desengrasante")
		self.assertEqual(line["qty"], 1)
		self.assertEqual(line["detected_uom"], "galon")

	def test_2_pares_guantes_amarillos_talla_9(self):
		line = parse_order_line("2 pares guantes amarillos talla 9")
		self.assertEqual(line["qty"], 2)
		self.assertEqual(line["detected_uom"], "par")
		self.assertEqual(line["tokens"]["generic"], ["guantes"])
		self.assertEqual(line["tokens"]["color"], ["amarillo"])
		self.assertEqual(line["tokens"]["size"], ["9"])
