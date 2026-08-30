# -*- coding: utf-8 -*-
"""Commit 24.3 -- fabergray_erp/geocoding.py's own unit tests (pure
functions, no DB, no fixtures). Commit 24.4 adds the Google provider
layer's own tests -- every one of them injects `provider=`/monkeypatches
`requests.get` with a fake, so this file makes ZERO real HTTP calls
(brief section 19's own "CERO llamadas reales a Internet en tests").
IntegrationTestCase only for this app's own consistent test-discovery
convention, not because anything here needs a site (Commit 24.4's own
tests use frappe.conf directly, still no real DB writes)."""

import frappe
import requests
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

	def test_geocoding_sources_tuple_matches_manual(self):
		self.assertIn("Manual", geocoding.GEOCODING_SOURCES)
		self.assertIn("Google", geocoding.GEOCODING_SOURCES)


class TestBuildGeocodingAddress(IntegrationTestCase):
	"""Commit 24.4, brief section 5 -- build_geocoding_address(). No DB, no
	fixtures needed: a plain dict already satisfies the `.get(fieldname)`
	contract this function relies on."""

	def test_build_address_all_fields(self):
		address = {
			"address_line1": "Cra 27 #45-21",
			"address_line2": "",
			"city": "Bucaramanga",
			"state": "Santander",
			"country": "Colombia",
			"pincode": "",
		}
		self.assertEqual(
			geocoding.build_geocoding_address(address), "Cra 27 #45-21, Bucaramanga, Santander, Colombia"
		)

	def test_build_address_ignores_empty_fields(self):
		address = {"address_line1": "Cra 27 #45-21", "city": "Bucaramanga", "state": None, "country": "Colombia"}
		self.assertEqual(geocoding.build_geocoding_address(address), "Cra 27 #45-21, Bucaramanga, Colombia")

	def test_build_address_never_includes_customer_or_internal_fields(self):
		"""Even if the caller accidentally hands this a full Address-like
		dict carrying unrelated keys, only the six allowed fieldnames are
		ever read -- nothing here iterates address.keys()."""
		address = {
			"address_line1": "Cra 27 #45-21",
			"city": "Bucaramanga",
			"customer_name": "Should Never Appear",
			"phone": "3000000000",
			"email_id": "someone@example.com",
		}
		text = geocoding.build_geocoding_address(address)
		self.assertEqual(text, "Cra 27 #45-21, Bucaramanga")
		self.assertNotIn("Should Never Appear", text)
		self.assertNotIn("3000000000", text)

	def test_build_address_all_empty_returns_empty_string(self):
		self.assertEqual(geocoding.build_geocoding_address({}), "")


def _fake_provider(payload):
	"""Test helper -- a `provider(address_text, api_key)` callable that
	returns a fixed payload and never touches the network, matching
	geocode_address()'s own documented injection seam (brief section 19's
	own "CERO llamadas reales a Internet en tests")."""

	def provider(address_text, api_key):
		return payload

	return provider


def _raising_provider(exc):
	def provider(address_text, api_key):
		raise exc

	return provider


class TestGeocodeAddressGoogle(IntegrationTestCase):
	"""Commit 24.4 -- geocode_address()'s real Google implementation.
	Every test injects `provider=` -- zero real HTTP calls anywhere in
	this class (brief section 19)."""

	def setUp(self):
		super().setUp()
		frappe.conf["fg_google_maps_api_key"] = "test-key-not-real"
		self.addCleanup(lambda: frappe.conf.pop("fg_google_maps_api_key", None))

	def test_missing_api_key_raises_safe_error(self):
		frappe.conf.pop("fg_google_maps_api_key", None)
		with self.assertRaises(geocoding.GeocodingProviderError) as ctx:
			geocoding.geocode_address("Cra 27 #45-21, Bucaramanga, Colombia")
		message = str(ctx.exception)
		self.assertIn("Google Maps no está configurado", message)
		self.assertNotIn("fg_google_maps_api_key", message)

	def test_empty_address_text_never_calls_provider(self):
		calls = []

		def provider(address_text, api_key):
			calls.append(address_text)
			return {"status": "OK", "results": []}

		result = geocoding.geocode_address("", provider=provider)
		self.assertEqual(result["status"], "ZERO_RESULTS")
		self.assertEqual(calls, [])

	def test_ok_valid_result(self):
		payload = {
			"status": "OK",
			"results": [
				{
					"formatted_address": "Cra 27 #45-21, Bucaramanga, Santander, Colombia",
					"place_id": "abc123",
					"partial_match": False,
					"geometry": {"location": {"lat": 7.119349, "lng": -73.1227416}},
					"address_components": [
						{"long_name": "Colombia", "short_name": "CO", "types": ["country", "political"]}
					],
				}
			],
		}
		result = geocoding.geocode_address("Cra 27 #45-21, Bucaramanga, Colombia", provider=_fake_provider(payload))
		self.assertEqual(result["status"], "OK")
		self.assertAlmostEqual(result["latitude"], 7.119349, places=5)
		self.assertAlmostEqual(result["longitude"], -73.1227416, places=5)
		self.assertEqual(result["formatted_address"], "Cra 27 #45-21, Bucaramanga, Santander, Colombia")
		self.assertEqual(result["place_id"], "abc123")
		self.assertFalse(result["partial_match"])
		self.assertEqual(result["provider"], "Google")
		self.assertEqual(result["country_long_name"], "Colombia")
		self.assertEqual(result["country_short_name"], "CO")

	def test_ok_result_with_invalid_coordinates_still_returned_for_caller_to_reject(self):
		"""geocode_address() itself never calls is_valid_coordinate_pair()
		-- that is the caller's own job (api.recorridos._geocode_one_address()).
		A (0, 0) "null island" result from Google is passed through
		as-is."""
		payload = {
			"status": "OK",
			"results": [{"geometry": {"location": {"lat": 0, "lng": 0}}, "formatted_address": "Null Island"}],
		}
		result = geocoding.geocode_address("some address", provider=_fake_provider(payload))
		self.assertEqual(result["status"], "OK")
		self.assertFalse(geocoding.is_valid_coordinate_pair(result["latitude"], result["longitude"]))

	def test_zero_results(self):
		payload = {"status": "ZERO_RESULTS", "results": []}
		result = geocoding.geocode_address("dirección inexistente", provider=_fake_provider(payload))
		self.assertEqual(result["status"], "ZERO_RESULTS")
		self.assertIsNone(result["latitude"])
		self.assertIsNone(result["longitude"])

	def test_request_denied_raises(self):
		payload = {"status": "REQUEST_DENIED", "error_message": "This API key is not authorized"}
		with self.assertRaises(geocoding.GeocodingProviderError) as ctx:
			geocoding.geocode_address("some address", provider=_fake_provider(payload))
		self.assertNotIn("test-key-not-real", str(ctx.exception))

	def test_over_query_limit_raises(self):
		payload = {"status": "OVER_QUERY_LIMIT"}
		with self.assertRaises(geocoding.GeocodingProviderError):
			geocoding.geocode_address("some address", provider=_fake_provider(payload))

	def test_invalid_request_raises(self):
		payload = {"status": "INVALID_REQUEST"}
		with self.assertRaises(geocoding.GeocodingProviderError):
			geocoding.geocode_address("some address", provider=_fake_provider(payload))

	def test_unknown_error_raises(self):
		payload = {"status": "UNKNOWN_ERROR"}
		with self.assertRaises(geocoding.GeocodingProviderError):
			geocoding.geocode_address("some address", provider=_fake_provider(payload))

	def test_unrecognized_status_treated_as_zero_results(self):
		"""Defensive -- a Google status this module does not explicitly
		list is never treated as OK (a false Geolocalizado) nor raised as
		a hard failure -- it degrades to the same "no result" shape as
		ZERO_RESULTS."""
		payload = {"status": "SOME_FUTURE_STATUS_NOT_YET_DOCUMENTED", "results": []}
		result = geocoding.geocode_address("some address", provider=_fake_provider(payload))
		self.assertIsNone(result["latitude"])

	def test_timeout_raises_provider_error(self):
		"""Exercises the REAL `_google_geocode_address()` transport layer
		(no `provider=` override) with a monkeypatched `requests.get` --
		this is the function that actually translates a
		requests.exceptions.Timeout into GeocodingProviderError; a
		`provider=` override would bypass that translation entirely,
		which is exactly why this test does not use one."""
		original_get = requests.get
		try:
			requests.get = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout("timed out"))
			with self.assertRaises(geocoding.GeocodingProviderError):
				geocoding.geocode_address("some address")
		finally:
			requests.get = original_get

	def test_connection_error_raises_provider_error(self):
		original_get = requests.get
		try:
			requests.get = lambda *a, **k: (_ for _ in ()).throw(
				requests.exceptions.ConnectionError("no route to host")
			)
			with self.assertRaises(geocoding.GeocodingProviderError):
				geocoding.geocode_address("some address")
		finally:
			requests.get = original_get

	def test_malformed_response_raises_provider_error(self):
		"""provider() returning something that is not even a dict (e.g. a
		string a broken transport layer might hand back) must not crash
		with an unhandled AttributeError -- _google_geocode_address()
		itself already guards the real HTTP path (response.json() failure
		-> GeocodingProviderError); this exercises the same guarantee via
		the injection seam using a provider that raises exactly what a
		real malformed-JSON failure would."""
		provider = _raising_provider(geocoding.GeocodingProviderError("Google Maps devolvió una respuesta inválida."))
		with self.assertRaises(geocoding.GeocodingProviderError):
			geocoding.geocode_address("some address", provider=provider)

	def test_api_key_never_in_normalized_result(self):
		payload = {
			"status": "OK",
			"results": [{"geometry": {"location": {"lat": 4.6, "lng": -74.0}}, "formatted_address": "x"}],
		}
		result = geocoding.geocode_address("some address", provider=_fake_provider(payload))
		self.assertNotIn("test-key-not-real", str(result))

	def test_real_google_geocode_address_never_raises_for_ok_status_over_http_layer(self):
		"""_google_geocode_address() itself (the real HTTP function, not
		geocode_address()'s own injection seam) is exercised here with a
		fake `requests.get` at the transport boundary -- still zero real
		network access -- to confirm the two-layer split (HTTP transport
		vs status interpretation) both work when wired together for
		real."""

		class _FakeResponse:
			status_code = 200

			def json(self):
				return {"status": "ZERO_RESULTS", "results": []}

		original_get = requests.get
		try:
			requests.get = lambda *a, **k: _FakeResponse()
			result = geocoding.geocode_address("some address")
			self.assertEqual(result["status"], "ZERO_RESULTS")
		finally:
			requests.get = original_get
