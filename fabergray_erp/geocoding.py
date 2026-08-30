# -*- coding: utf-8 -*-
"""fabergray_erp/geocoding.py -- Commit 24.3's provider-agnostic geocoding
seam, now (Commit 24.4) wired to a real provider: Google Maps Platform's
Geocoding API. Still the ONE place any HTTP call to a geocoding provider
is made anywhere in this app -- api/recorridos.py never imports
`requests` or knows Google's URL/response shape, exactly per Commit 24.4's
own brief section 2 ("api/recorridos.py NO debe implementar directamente
HTTP de Google").

Scope, explicitly, still only two families of thing:

1. is_valid_coordinate_pair() (Commit 24.3) -- the ONE central
   coordinate-validity rule every caller in this app uses, unchanged.

2. geocode_address()/build_geocoding_address() (Commit 24.4) -- turn an
   Address-shaped object into request text, call Google, and return one
   normalized dict every caller in api/recorridos.py works with, never
   Google's raw response shape. `geocode_address(address_text,
   provider=None)`'s own `provider` parameter is the seam Commit 24.3's
   docstring already promised ("the ONLY code that needs to change is
   this one function's body") -- it defaults to `_google_geocode_address`
   (the real HTTP call) and exists so tests can inject a fake transport
   with zero real internet access (brief section 19's own "CERO llamadas
   reales a Internet en tests"), never so callers pick a provider at
   runtime (there is exactly one today).

Status taxonomy, load-bearing for every caller (brief sections 6/7/12):
this module draws a hard line between an ADDRESS-level outcome (Google
looked at this specific address and either found it or didn't -- "OK" or
"ZERO_RESULTS", always returned as a normal dict, never raised) and a
PROVIDER-level failure (the request itself could not be trusted --
missing API key, network/timeout, malformed JSON, non-200 HTTP, or one of
Google's own OVER_QUERY_LIMIT/REQUEST_DENIED/INVALID_REQUEST/
UNKNOWN_ERROR statuses -- always raised as GeocodingProviderError, never
returned as if it were a normal result). This distinction is why a batch
caller (api.recorridos.geocode_route_pending_addresses()) can tell "this
one address genuinely has no match" (mark it Error, keep processing the
rest) apart from "the provider itself is broken right now" (record the
failure, but never silently write a misleading Error onto every other
Address in the batch just because a quota ran out mid-run).

Credentials (brief section 3): the API key is read ONLY server-side, from
`frappe.conf.get("fg_google_maps_api_key")` (Frappe's own conf layer over
site_config.json -- per-site, never common_site_config.json, never a
Custom Field, never a fixture, never hardcoded here or in any JS). If
missing, this throws a generic, operator-safe message
("Google Maps no está configurado para este sitio.") that never names the
config key or reveals whether the problem is a missing key vs. an invalid
one. The key itself is never interpolated into any exception message,
never logged, and never appears in this module's own return values.
"""

import math

import frappe
import requests
from frappe import _

#: The four geocoding sources this app recognizes -- shared by both
#: Address.fg_geocoding_source and Recorrido Parada.geolocation_source so
#: the two Select fields' options can never drift apart. "Manual"
#: (Commit 24.3) and "Google" (Commit 24.4) are the only two anything in
#: this app actually writes; "Mapbox"/"Otro" remain reserved so the
#: DocType schema needs no further migration if a second provider is ever
#: wired in.
GEOCODING_SOURCES = ("Manual", "Google", "Mapbox", "Otro")

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

#: Single-shot HTTP timeout, no retries (brief section 6's own "Timeout
#: razonable: 5-10 segundos" / "No retries infinitos"). A retry loop here
#: would also fight brief section 15's own concurrency design, which
#: already assumes exactly one HTTP round-trip per Address per call.
GOOGLE_GEOCODE_TIMEOUT_SECONDS = 8

#: Statuses Google's own API contract documents as PROVIDER-level failures
#: -- never evidence about the address itself, always raised, never
#: returned as a normal result (see the module docstring's own status
#: taxonomy).
_GOOGLE_PROVIDER_FAILURE_STATUSES = {
	"OVER_QUERY_LIMIT",
	"REQUEST_DENIED",
	"INVALID_REQUEST",
	"UNKNOWN_ERROR",
}

#: Operator-safe messages for each provider-failure status -- never
#: mentions quotas/keys/internal config names, matching brief section 6's
#: own "No mostrar tracebacks técnicos al usuario."
_GOOGLE_STATUS_MESSAGES = {
	"OVER_QUERY_LIMIT": "Se alcanzó el límite de solicitudes a Google Maps. Intenta nuevamente más tarde.",
	"REQUEST_DENIED": "Google Maps no está disponible para este sitio en este momento.",
	"INVALID_REQUEST": "No fue posible interpretar la dirección para buscarla en Google Maps.",
	"UNKNOWN_ERROR": "Google Maps tuvo un error temporal. Intenta nuevamente.",
}

#: An empty/no-result normalized dict -- returned for ZERO_RESULTS and for
#: any Google status this module does not explicitly recognize as either
#: "OK" or a provider failure (defensive: never treated as a hard
#: failure, never mistaken for a real coordinate).
_EMPTY_RESULT_TEMPLATE = {
	"latitude": None,
	"longitude": None,
	"formatted_address": None,
	"provider": "Google",
	"place_id": None,
	"partial_match": False,
	"country_long_name": None,
	"country_short_name": None,
}


class GeocodingProviderError(frappe.ValidationError):
	"""Raised for any PROVIDER-level failure (brief sections 3/6/7): a
	missing/invalid API key, a network/timeout/HTTP-transport failure, a
	malformed response body, or one of Google's own documented
	provider-failure statuses. NEVER raised for a normal address-level
	outcome (OK/ZERO_RESULTS/an unrecognized status) -- those are always
	returned as a normal dict, see this module's own docstring. The
	message is always operator-safe; the API key is never part of it."""


def is_valid_coordinate_pair(latitude, longitude):
	"""The one central coordinate-validity rule (brief section 4/23,
	unchanged since Commit 24.3). True only for a pair genuinely useful
	for routing:

	- both values parse as finite floats (rejects None, "", non-numeric
	  strings, NaN, +/-Infinity);
	- -90 <= latitude <= 90;
	- -180 <= longitude <= 180;
	- NOT the (0, 0) "null island" sentinel -- in practice always a sign
	  a field was left blank/zeroed, never a real address this
	  business delivers to.

	Never raises -- any unparseable input simply returns False, so
	callers can use this directly as a guard without their own
	try/except."""
	try:
		lat = float(latitude)
		lon = float(longitude)
	except (TypeError, ValueError):
		return False

	if math.isnan(lat) or math.isinf(lat) or math.isnan(lon) or math.isinf(lon):
		return False

	if not (-90 <= lat <= 90):
		return False

	if not (-180 <= lon <= 180):
		return False

	if lat == 0 and lon == 0:
		return False

	return True


def build_geocoding_address(address_doc):
	"""Commit 24.4, brief section 5 -- the ONE place an Address-shaped
	object (a real `frappe.get_doc("Address", ...)`, or any object/dict
	supporting `.get(fieldname)`) is turned into the free-text string
	Google's Geocoding API expects. Reads exactly address_line1/
	address_line2/city/state/country/pincode, in that order, joined by
	", " -- empty/None fields are skipped entirely, never emitted as a
	bare comma or a literal "None". Never reads or sends customer_name,
	any Sales Order/Pick List reference, phone, email, or any internal
	note -- Google only ever sees what a delivery address actually is.

	`country` is whatever the Address' own Link field resolves to (the
	Country doctype's own name, e.g. "Colombia") -- never hardcoded,
	never assumed to be Colombia for every Address just because that is
	this business' common case today."""
	parts = []
	for fieldname in ("address_line1", "address_line2", "city", "state", "country", "pincode"):
		value = address_doc.get(fieldname)
		if value and str(value).strip():
			parts.append(str(value).strip())
	return ", ".join(parts)


def _google_api_key():
	"""Brief section 3 -- server-side only, per-site (site_config.json via
	frappe.conf, never common_site_config.json). The failure message never
	names the config key or says "missing" vs "invalid" -- both look
	identical to the caller, on purpose."""
	api_key = frappe.conf.get("fg_google_maps_api_key")
	if not api_key:
		frappe.throw(_("Google Maps no está configurado para este sitio."), GeocodingProviderError)
	return api_key


def _google_geocode_address(address_text, api_key):
	"""The ONE function in this entire app that makes an HTTP request to
	Google (brief section 2's own "provider interno"). Returns Google's
	own parsed JSON body verbatim (`geocode_address()` normalizes it) --
	raises GeocodingProviderError for every transport-level failure
	(timeout, connection error, any other requests exception, a non-200
	HTTP status, or a body that does not parse as JSON), never for a
	well-formed response carrying any `status` value, including Google's
	own error statuses -- interpreting those is `geocode_address()`'s own
	job, not this function's. The API key is sent to Google (as the API
	itself requires) but never appears in any exception message, log
	line, or return value here."""
	try:
		response = requests.get(
			GOOGLE_GEOCODE_URL,
			params={"address": address_text, "key": api_key},
			timeout=GOOGLE_GEOCODE_TIMEOUT_SECONDS,
		)
	except requests.exceptions.Timeout:
		raise GeocodingProviderError(_("Google Maps no respondió a tiempo. Intenta nuevamente."))
	except requests.exceptions.ConnectionError:
		raise GeocodingProviderError(
			_("No fue posible conectar con Google Maps. Verifica la conexión del servidor.")
		)
	except requests.exceptions.RequestException:
		raise GeocodingProviderError(_("Ocurrió un error al conectar con Google Maps."))

	if response.status_code != 200:
		raise GeocodingProviderError(_("Google Maps respondió con un error inesperado."))

	try:
		return response.json()
	except ValueError:
		raise GeocodingProviderError(_("Google Maps devolvió una respuesta inválida."))


def _extract_country_component(address_components):
	"""Brief section 10 -- Google's `address_components` is a flat list of
	{long_name, short_name, types[]} objects; the one with type
	"country" carries both the full name (e.g. "Colombia") and the
	ISO-3166-1 alpha-2 code (e.g. "CO"). Returns (None, None) if Google's
	response carries no such component at all (never raises -- an
	Address whose country cannot be determined this way is a "Revisar"
	case for the caller to decide, not this function's problem)."""
	for component in address_components or []:
		if "country" in (component.get("types") or []):
			return component.get("long_name"), component.get("short_name")
	return None, None


def _normalize_google_response(payload):
	"""Turns Google's raw JSON body into this module's one normalized
	shape (brief section 2). Raises GeocodingProviderError for any
	provider-failure status (see the module docstring's own taxonomy);
	returns a dict for every address-level outcome, OK included, so
	callers never branch on HTTP status codes or Google's own field
	names directly."""
	status = payload.get("status") or "UNKNOWN_ERROR"

	if status in _GOOGLE_PROVIDER_FAILURE_STATUSES:
		message = _GOOGLE_STATUS_MESSAGES.get(status, _GOOGLE_STATUS_MESSAGES["UNKNOWN_ERROR"])
		raise GeocodingProviderError(_(message))

	results = payload.get("results") or []
	if status != "OK" or not results:
		# ZERO_RESULTS, or any other status this module does not
		# recognize -- always a normal, non-raising outcome (defensive:
		# an unrecognized status is never treated as a hard failure, and
		# never mistaken for a real coordinate).
		return dict(_EMPTY_RESULT_TEMPLATE, status=status if status != "OK" else "ZERO_RESULTS")

	result = results[0]
	location = (result.get("geometry") or {}).get("location") or {}
	country_long, country_short = _extract_country_component(result.get("address_components"))

	return {
		"latitude": location.get("lat"),
		"longitude": location.get("lng"),
		"formatted_address": result.get("formatted_address"),
		"provider": "Google",
		"status": "OK",
		"place_id": result.get("place_id"),
		"partial_match": bool(result.get("partial_match")),
		"country_long_name": country_long,
		"country_short_name": country_short,
	}


def geocode_address(address_text, provider=None):
	"""Commit 24.4's real implementation of Commit 24.3's own seam.
	`address_text` is already-built request text (see
	build_geocoding_address()) -- this function never has or needs the
	Address document itself, keeping this module ignorant of Frappe
	permissions/company isolation by construction (api/recorridos.py's
	job entirely).

	`provider`, if given, replaces `_google_geocode_address` (signature
	`provider(address_text, api_key) -> dict`) -- the ONLY seam this
	app's tests use to reach "CERO llamadas reales a Internet en tests"
	(brief section 19): every other line in this function, including
	`_normalize_google_response()`'s own interpretation of the result,
	still runs for real against whatever the fake provider returns.

	Never raises for an empty/blank `address_text` (an Address with no
	populated line/city/state/country/pincode at all) -- returns the same
	normalized "no result" shape as ZERO_RESULTS (never the raised
	INVALID_REQUEST a malformed-but-non-empty request could get back from
	Google itself -- this is an address-level "nothing to search for",
	not a provider-level failure), without spending an HTTP call or API
	quota on a request Google could not usefully answer either way."""
	api_key = _google_api_key()

	if not address_text or not address_text.strip():
		return dict(_EMPTY_RESULT_TEMPLATE, status="ZERO_RESULTS")

	fetch = provider or _google_geocode_address
	payload = fetch(address_text, api_key)
	return _normalize_google_response(payload)
