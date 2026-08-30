# -*- coding: utf-8 -*-
"""Commit 24.3 -- fabergray_erp/geocoding.py's own unit tests. Pure
functions, no DB, no fixtures -- IntegrationTestCase only for this app's
own consistent test-discovery convention, not because anything here
needs a site."""

from frappe.tests import IntegrationTestCase

from fabergray_erp import geocoding

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestGeocoding(IntegrationTestCase):
	# -- is_valid_coordinate_pair() (brief section 4/24) --------------------

	def test_valid_coordinates_accepted(self):
		self.assertTrue(geocoding.is_valid_coordinate_pair(4.710989, -74.072092))
		self.assertTrue(geocoding.is_valid_coordinate_pair(-33.45, -70.66))

	def test_latitude_above_90_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(90.0001, 0))
		self.assertFalse(geocoding.is_valid_coordinate_pair(91, 0))

	def test_latitude_below_negative_90_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(-90.0001, 0))
		self.assertFalse(geocoding.is_valid_coordinate_pair(-91, 0))

	def test_longitude_above_180_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, 180.0001))
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, 181))

	def test_longitude_below_negative_180_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, -180.0001))
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, -181))

	def test_boundary_values_accepted(self):
		"""Exactly +/-90 and +/-180 are valid boundary values, not
		off-by-one rejected."""
		self.assertTrue(geocoding.is_valid_coordinate_pair(90, 180))
		self.assertTrue(geocoding.is_valid_coordinate_pair(-90, -180))

	def test_null_island_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, 0))
		self.assertFalse(geocoding.is_valid_coordinate_pair(0.0, 0.0))
		self.assertFalse(geocoding.is_valid_coordinate_pair("0", "0"))

	def test_nan_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(float("nan"), 0))
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, float("nan")))

	def test_infinity_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair(float("inf"), 0))
		self.assertFalse(geocoding.is_valid_coordinate_pair(0, float("-inf")))

	def test_invalid_strings_rejected(self):
		self.assertFalse(geocoding.is_valid_coordinate_pair("not-a-number", -74))
		self.assertFalse(geocoding.is_valid_coordinate_pair(4.6, "not-a-number"))
		self.assertFalse(geocoding.is_valid_coordinate_pair(None, -74))
		self.assertFalse(geocoding.is_valid_coordinate_pair(4.6, None))
		self.assertFalse(geocoding.is_valid_coordinate_pair("", ""))

	def test_numeric_strings_accepted(self):
		"""Address.fg_latitude/fg_longitude arrive as real Python floats
		from frappe.db.get_value(), but whitelisted-function arguments
		(set_address_geolocation()) can arrive as strings over HTTP --
		this must accept both the same way the rest of this app's own
		cint()/flt() helpers do."""
		self.assertTrue(geocoding.is_valid_coordinate_pair("4.710989", "-74.072092"))

	# -- geocode_address() (brief section 17/27) -----------------------------

	def test_geocode_address_returns_none_no_provider_configured(self):
		"""No external provider is wired in this commit -- confirmed by
		this always returning None regardless of input, never making an
		HTTP request."""
		self.assertIsNone(geocoding.geocode_address(None))
		self.assertIsNone(geocoding.geocode_address({"name": "whatever"}))

	def test_geocoding_sources_tuple_matches_manual(self):
		self.assertIn("Manual", geocoding.GEOCODING_SOURCES)
