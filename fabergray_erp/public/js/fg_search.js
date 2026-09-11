// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt
//
// Commit 25.20 -- shared search helper for the unified "Buscar por
// cliente o fecha..." bar across Bodega/Ventas/Cotizaciones/Recorridos/
// Facturación/Jefe de Bodega. Loaded globally via `app_include_js`
// (hooks.py), same mechanism `fg_shell.css` already uses for shared,
// page-agnostic styling -- section 7's own explicit "no crear seis
// algoritmos distintos", so this is the ONE place the text-normalize/
// date-parse/match algorithm is implemented, never copied.
//
// `normalize_date()` mirrors `fabergray_erp/search_utils.py`'s own
// `normalize_search_date()` exactly (same three formats, same month
// table) for the pages that filter client-side -- server-side pages
// (Facturación/Recorridos/Centro de Faltantes) send the raw query text to
// their own endpoint instead, which parses it with the Python twin of
// this function; client-side normalization here is never sent over the
// wire.

frappe.provide("fabergray_erp.search");

(function () {
	const SPANISH_MONTHS = {
		enero: 1,
		febrero: 2,
		marzo: 3,
		abril: 4,
		mayo: 5,
		junio: 6,
		julio: 7,
		agosto: 8,
		septiembre: 9,
		setiembre: 9,
		octubre: 10,
		noviembre: 11,
		diciembre: 12,
	};

	function pad(n) {
		return String(n).padStart(2, "0");
	}

	function pack(year, month, day) {
		if (month < 1 || month > 12 || day < 1 || day > 31) return null;
		return `${year}-${pad(month)}-${pad(day)}`;
	}

	// Case-insensitive, accent-insensitive ("razonable" per section 3 --
	// NFD-decompose then strip combining diacritics, the standard JS
	// technique), collapsed whitespace.
	function normalize_text(value) {
		return (value || "")
			.toString()
			.normalize("NFD")
			.replace(/[̀-ͯ]/g, "")
			.toLowerCase()
			.trim()
			.replace(/\s+/g, " ");
	}

	// "YYYY-MM-DD" | "DD/MM/YYYY" | "DD-MM-YYYY" | "11 septiembre 2026" |
	// "11 de septiembre de 2026" -> ISO "YYYY-MM-DD", or null when the
	// query does not look like a date at all (a plain customer-name
	// search, most of the time).
	function normalize_date(value) {
		if (!value) return null;
		const v = value.trim().toLowerCase();

		let m = v.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
		if (m) return pack(parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10));

		m = v.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/);
		if (m) return pack(parseInt(m[3], 10), parseInt(m[2], 10), parseInt(m[1], 10));

		m = v.match(/^(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)\s+(?:de\s+)?(\d{4})$/);
		if (m) {
			const month = SPANISH_MONTHS[m[2]];
			if (month) return pack(parseInt(m[3], 10), month, parseInt(m[1], 10));
		}

		return null;
	}

	// The one matcher every client-side search (Ventas/Cotizaciones/
	// Bodega) calls -- `record` is a plain data row already fetched from
	// its own get_my_orders()/get_my_quotations()/get_queue() response,
	// `text_fields` the customer-ish keys to substring-match, `date_fields`
	// the real Date/Datetime keys to compare a parsed query against
	// (section 4's own "no buscar solo contra texto visual" -- this always
	// compares the ISO-normalized query against the record's own real
	// date value, sliced to its first 10 characters, never a formatted
	// display string). An empty query always matches (nothing to filter
	// on yet).
	function matches_operational_search(record, query, options) {
		const opts = options || {};
		const text_fields = opts.text_fields || [];
		const date_fields = opts.date_fields || [];

		const q = normalize_text(query);
		if (!q) return true;

		const haystack = text_fields
			.map((f) => record[f])
			.filter(Boolean)
			.map(normalize_text)
			.join(" ");
		if (haystack.includes(q)) return true;

		const q_date = normalize_date(query);
		if (q_date) {
			for (let i = 0; i < date_fields.length; i++) {
				const raw = record[date_fields[i]];
				if (raw && String(raw).slice(0, 10) === q_date) return true;
			}
		}

		return false;
	}

	fabergray_erp.search = {
		normalize_text,
		normalize_date,
		matches_operational_search,
	};
})();
