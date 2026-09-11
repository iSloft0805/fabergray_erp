# -*- coding: utf-8 -*-
"""Commit 25.20 -- shared server-side search helper: parses a free-text
search query into a real ISO date (`YYYY-MM-DD`) when it looks like one,
so `api/bodega.py`/`api/facturacion.py`/`api/recorridos.py`/
`api/jefe_bodega.py` can each match a query like "11/09/2026" against a
document's own real Date/Datetime field (`transaction_date`/`route_date`/
`reported_on`/`fg_invoiced_on`/...) instead of a plain text `like` on a
formatted string -- section 4's own explicit "comparar contra la fecha
real del documento, no solo contra texto visual".

Top-level module (not under api/), same placement convention as
`sales_order_naming.py`/`geocoding.py` -- a small, doctype-agnostic
utility multiple `api/*.py` modules import, never the other way around
(nothing here ever imports an `api/*` module, so no circular-import risk
for any of them).

Mirrors `fabergray_erp/public/js/fg_search.js`'s own `normalize_date()`
exactly (same three formats, same month-name table) -- one algorithm,
implemented twice only because the client and the server are two
different languages, never two different sets of rules. If one changes,
the other must change with it.
"""

import re

_SPANISH_MONTHS = {
	"enero": 1,
	"febrero": 2,
	"marzo": 3,
	"abril": 4,
	"mayo": 5,
	"junio": 6,
	"julio": 7,
	"agosto": 8,
	"septiembre": 9,
	"setiembre": 9,
	"octubre": 10,
	"noviembre": 11,
	"diciembre": 12,
}

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$")
_SPANISH_RE = re.compile(r"^(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)\s+(?:de\s+)?(\d{4})$")


def normalize_search_date(text):
	"""Returns `"YYYY-MM-DD"` if `text` looks like a date in any of the
	three formats section 4 asks for (ISO, DD/MM/YYYY or DD-MM-YYYY,
	Spanish long form), `None` otherwise -- never raises on a plain,
	non-date search term like a customer name. No calendar validation
	beyond field ranges/a real Spanish month name (an out-of-range day/
	month, e.g. "32/13/2026", simply returns `None` -- the caller then
	falls back to its own plain text match, exactly as if the query had
	never looked like a date at all)."""
	if not text:
		return None
	value = text.strip().lower()

	m = _ISO_RE.match(value)
	if m:
		year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
		return _pack(year, month, day)

	m = _DMY_RE.match(value)
	if m:
		day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
		return _pack(year, month, day)

	m = _SPANISH_RE.match(value)
	if m:
		day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
		month = _SPANISH_MONTHS.get(month_name)
		if month:
			return _pack(year, month, day)

	return None


def _pack(year, month, day):
	if not (1 <= month <= 12) or not (1 <= day <= 31):
		return None
	return f"{year:04d}-{month:02d}-{day:02d}"
