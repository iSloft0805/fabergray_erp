# -*- coding: utf-8 -*-
"""Commit 25.20 -- fabergray_erp/search_utils.py::normalize_search_date().
Pure function, no DB/fixtures needed -- still IntegrationTestCase, same
convention as every other test file in this app, so it runs through the
same one test runner/discovery path."""

from frappe.tests import IntegrationTestCase

from fabergray_erp.search_utils import normalize_search_date

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestNormalizeSearchDate(IntegrationTestCase):
	# -- D: ISO ------------------------------------------------------------

	def test_d_iso_format(self):
		self.assertEqual(normalize_search_date("2026-09-11"), "2026-09-11")

	def test_d_iso_single_digit_month_day(self):
		self.assertEqual(normalize_search_date("2026-9-1"), "2026-09-01")

	# -- E: DD/MM/YYYY and DD-MM-YYYY ------------------------------------

	def test_e_dmy_slash(self):
		self.assertEqual(normalize_search_date("11/09/2026"), "2026-09-11")

	def test_e_dmy_dash(self):
		self.assertEqual(normalize_search_date("11-09-2026"), "2026-09-11")

	def test_e_dmy_single_digit(self):
		self.assertEqual(normalize_search_date("1/9/2026"), "2026-09-01")

	# -- Spanish long form ("si es fácil con el formateador actual") ------

	def test_spanish_long_form(self):
		self.assertEqual(normalize_search_date("11 septiembre 2026"), "2026-09-11")

	def test_spanish_long_form_with_de(self):
		self.assertEqual(normalize_search_date("11 de septiembre de 2026"), "2026-09-11")

	def test_spanish_long_form_case_insensitive(self):
		self.assertEqual(normalize_search_date("11 SEPTIEMBRE 2026"), "2026-09-11")

	def test_spanish_long_form_accented_month_still_matches_plain_key(self):
		# The month table itself has no accented keys ("febrero" etc. have
		# none) -- confirms no month name in the table needs one.
		self.assertEqual(normalize_search_date("5 febrero 2026"), "2026-02-05")

	# -- F: not a date at all -- must return None, never raise/guess ------

	def test_f_plain_customer_name_returns_none(self):
		self.assertIsNone(normalize_search_date("ABC FUMISERVICES"))

	def test_f_empty_string_returns_none(self):
		self.assertIsNone(normalize_search_date(""))

	def test_f_none_input_returns_none(self):
		self.assertIsNone(normalize_search_date(None))

	def test_f_out_of_range_month_returns_none_not_an_exception(self):
		self.assertIsNone(normalize_search_date("11/13/2026"))

	def test_f_out_of_range_day_returns_none_not_an_exception(self):
		self.assertIsNone(normalize_search_date("32/01/2026"))

	def test_f_unknown_month_name_returns_none(self):
		self.assertIsNone(normalize_search_date("11 monthname 2026"))

	def test_f_partial_numeric_string_returns_none(self):
		self.assertIsNone(normalize_search_date("2026"))
