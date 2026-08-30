# -*- coding: utf-8 -*-
"""fabergray_erp/geocoding.py -- Commit 24.3's own tiny, provider-agnostic
geocoding seam.

Scope, explicitly: this module does two things and nothing else --
(1) is_valid_coordinate_pair(), the ONE central coordinate-validity rule
every caller in this app must use (api/recorridos.py's
_resolve_stop_geolocation()/set_address_geolocation(), and this module's
own tests), so "what counts as a usable coordinate" is defined in exactly
one place; (2) geocode_address(), the single seam a REAL geocoding
provider (Google/Mapbox/other) plugs into in a future commit.

geocode_address() always returns None today -- no HTTP request, no API
key, no provider configured anywhere in this commit (brief sections 17/
18/27: no Google, no Mapbox, no Waze, no external request of any kind
yet). The only way a coordinate gets set in Commit 24.3 is manual entry
through api.recorridos.set_address_geolocation(). This function exists
now, already wired into _resolve_stop_geolocation()'s own call sites
conceptually (nothing calls it yet, since there is nothing for it to do),
so that when a future commit adds a real provider, the ONLY code that
needs to change is this one function's body -- api/recorridos.py, the
Recorrido/Recorrido Parada DocTypes, and every existing test stay exactly
as they are. Deliberately not over-built: no provider registry, no
plugin system, no config schema -- brief section 17's own "no crear
complejidad vacía".

Credentials note (brief section 18): when a real provider is eventually
wired in here, its API key belongs in site_config.json (frappe.conf) or a
Password-fieldtype Settings DocType -- never hardcoded in this file, a
fixture, or any JS. Nothing in this commit needs a credential at all, so
none exists anywhere in this module or elsewhere in the app.
"""

import math

#: The four geocoding sources this app recognizes today, shared by both
#: Address.fg_geocoding_source and Recorrido Parada.geolocation_source so
#: the two Select fields' options can never drift apart. "Manual" is the
#: only one Commit 24.3 itself ever writes; the other three exist so the
#: DocType schema does not need another migration the day a real provider
#: is wired in.
GEOCODING_SOURCES = ("Manual", "Google", "Mapbox", "Otro")


def is_valid_coordinate_pair(latitude, longitude):
	"""The one central coordinate-validity rule (brief section 4/23).
	True only for a pair genuinely useful for routing:

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


def geocode_address(address_doc):
	"""The seam a real provider connects to in a future commit -- given an
	already-loaded, already-permission-checked Address document, return
	(latitude, longitude, source) or None if it cannot (or, as in this
	commit, simply does not yet try to) resolve coordinates for it.

	Always returns None in Commit 24.3: no provider is configured, no
	HTTP request is ever made here. Every caller already treats "no
	result" as an entirely normal, handled outcome (the stop/Address
	stays geolocation_status="Pendiente"), so plugging in a real provider
	later is a one-function change, not a caller-by-caller migration."""
	return None
