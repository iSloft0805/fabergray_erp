# -*- coding: utf-8 -*-
"""Commit 20.4 -- Fase 5 (Cotizaciones): Quotation naming series.
COTIZACION-.# as the default (no year, no padding, no reset),
SAL-QTN-.YYYY.- (Quotation's own original native series) kept as a
non-default second option. Native mechanism only -- two `Property
Setter` records on `Quotation.naming_series` (`fixtures/
property_setter.json`, applied via `bench migrate`), the exact same
mechanism `test_sales_order_naming_series.py` (Commit 18.5a) already
proved for `PEDIDO-.#` -- no custom Python counter anywhere in this app,
either doctype.

**Frappe 16 compatibility caveat, same one Commit 18.5a documented for
PEDIDO-.#, deliberately NOT re-tested here as a second copy of the exact
same mechanism-level assertion:** the reason `COTIZACION-.#` (one hash,
no padding) produces `COTIZACION-1` instead of `COTIZACION-00001` is a
Frappe-internal, prefix-agnostic behaviour --
`set_name_by_naming_series()` (`frappe/model/naming.py`) unconditionally
appends `.#####` to whatever `naming_series` already is, and
`parse_naming_series()` only honours the FIRST `#`-group it finds,
silently discarding the auto-appended one. This is exactly what
`test_sales_order_naming_series.py`'s own
`test_compat_parse_naming_series_still_ignores_the_auto_appended_padding`
already exercises directly, against a throwaway series key -- the
mechanism is identical regardless of which doctype/prefix uses it, so
re-running the identical Frappe-internals assertion a second time here,
against a second throwaway key, would prove nothing new. What THIS file
proves instead, concretely, against the real `COTIZACION-` key itself
(not a throwaway one): `test_first_and_second_new_quotations_use_the_new_
series_and_increment` and `test_new_series_name_never_contains_the_year`
assert the real, no-padding, no-year shape end to end. If a future Frappe
version ever changes the shared mechanism, the Sales Order suite's
dedicated compat test still catches it first (same root cause, same
fix), and both this file's shape assertions and that one would need to be
revisited together.

Every test drives `create_and_submit_quotation()` or a raw
`frappe.get_doc({"doctype": "Quotation", ...})` with `naming_series` left
unset (the whole point of this commit: the new default applies
automatically), never `api.cotizaciones.py` itself modified -- confirmed
unchanged (see Commit 20.4's own scope note).
"""

import re

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

#: The exact pattern this app's Property Setter puts in
#: Quotation.naming_series's `default` property (fixtures/property_setter.json).
COTIZACION_SERIES = "COTIZACION-.#"
COTIZACION_PREFIX = "COTIZACION-"


class TestQuotationNamingSeries(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.item = cls.world.item("FG204-ITEM")
		cls.customer = cls.world.customer("FG204 Customer")
		cls.vendedora = cls.world.user("fg204-vendedora@example.com", ["Vendedora"])

	def _raw_quotation(self, naming_series=None, submit=True):
		doc_dict = {
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": self.customer.name,
			"company": fx.COMPANY,
			"items": [{"item_code": self.item.name, "qty": 1}],
		}
		if naming_series:
			doc_dict["naming_series"] = naming_series
		doc = frappe.get_doc(doc_dict)
		# naming_series deliberately left unset in the default case -- the
		# whole point of this commit is that the NEW default applies
		# automatically, exactly as create_and_submit_quotation() (the real
		# caller) already does.
		doc.insert()
		self.world.track_existing("Quotation", doc.name)
		if submit:
			doc.submit()
		return doc

	def _submit_via_api(self):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		return result

	# -- Configuración nativa aplicada -----------------------------------------

	def test_naming_series_options_and_default_are_configured_via_property_setter(self):
		"""Confirms the fixture actually applied to this site --
		COTIZACION-.# first (and therefore default, per
		get_default_naming_series()'s own "first truthy option wins" rule)
		with SAL-QTN-.YYYY.- (Quotation's own original native series) kept
		as a real, still-usable second option -- nothing renamed, nothing
		removed."""
		meta = frappe.get_meta("Quotation")
		field = meta.get_field("naming_series")

		self.assertEqual(field.options, "COTIZACION-.#\nSAL-QTN-.YYYY.-")
		self.assertEqual(field.default, COTIZACION_SERIES)

		from frappe.model.naming import get_default_naming_series

		self.assertEqual(get_default_naming_series("Quotation"), COTIZACION_SERIES)

		rows = frappe.get_all(
			"Property Setter",
			filters={"doc_type": "Quotation", "field_name": "naming_series"},
			fields=["property", "value"],
		)
		self.assertEqual(
			{(r.property, r.value) for r in rows},
			{("options", "COTIZACION-.#\nSAL-QTN-.YYYY.-"), ("default", COTIZACION_SERIES)},
		)

	# -- Primera / segunda Quotation nueva usan la nueva serie, incrementan, sin año

	def test_first_and_second_new_quotations_use_the_new_series_and_increment(self):
		qtn_1 = self._raw_quotation()
		qtn_2 = self._raw_quotation()

		self.assertRegex(qtn_1.name, r"^COTIZACION-\d+$")
		self.assertRegex(qtn_2.name, r"^COTIZACION-\d+$")

		n1 = int(qtn_1.name.split(COTIZACION_PREFIX)[1])
		n2 = int(qtn_2.name.split(COTIZACION_PREFIX)[1])
		self.assertEqual(n2, n1 + 1)  # increments by exactly 1, second call right after the first

		# and no zero-padding snuck in either.
		self.assertIsNone(re.search(r"-0", qtn_1.name))

	def test_new_series_name_never_contains_the_year(self):
		"""No 4-digit year token anywhere in a newly-generated name --
		COTIZACION-.# has no .YYYY. part at all, unlike SAL-QTN-.YYYY.-,
		the series it replaces as default."""
		from frappe.utils import nowdate

		qtn = self._raw_quotation()
		self.assertNotIn(str(nowdate()[:4]), qtn.name)
		self.assertRegex(qtn.name, r"^COTIZACION-\d+$")  # nothing but the counter after the literal prefix

	def test_series_prefix_has_no_date_token_so_it_can_never_reset(self):
		"""Structural proof of "sin reinicio anual": the exact prefix key
		used to look up `tabSeries` (NamingSeries.get_prefix()) is the
		literal string "COTIZACION-", with zero date-related parts
		(`.YYYY.`, `.YY.`, `.MM.`, `.DD.`) -- since `getseries()` keys the
		counter purely off this string, it can only ever "reset" if the key
		itself changes, and this key is date-independent, therefore
		constant forever."""
		from frappe.model.naming import NamingSeries

		prefix = NamingSeries(COTIZACION_SERIES).get_prefix()
		self.assertEqual(prefix, COTIZACION_PREFIX)
		for date_token in (".YYYY.", ".YY.", ".MM.", ".DD."):
			self.assertNotIn(date_token, COTIZACION_SERIES)

	def test_new_series_does_not_collide_with_existing_or_old_series_documents(self):
		names = [self._raw_quotation().name for _ in range(3)]
		self.assertEqual(len(names), len(set(names)))  # no duplicates
		for name in names:
			self.assertFalse(name.startswith("SAL-QTN-"))

	def test_old_series_still_works_when_explicitly_requested(self):
		"""SAL-QTN-.YYYY.- was kept, not removed -- a caller that explicitly
		asks for it (the only way it can still be produced, since it is no
		longer the default) gets it, with its own separate, unaffected
		counter."""
		from frappe.utils import nowdate

		qtn = self._raw_quotation(naming_series="SAL-QTN-.YYYY.-")
		self.assertTrue(qtn.name.startswith("SAL-QTN-"))
		self.assertIn(nowdate()[:4], qtn.name)

	# -- create_and_submit_quotation() usa la nueva serie automáticamente ------

	def test_create_and_submit_quotation_uses_the_new_series_automatically(self):
		"""api/cotizaciones.py is unmodified in this commit -- neither
		create_and_submit_quotation() nor _validate_and_build_quotation_
		item_rows() ever sets naming_series, so this proves the new
		default reaches the real API endpoint purely through the native
		mechanism, not through any code change in that file."""
		result = self._submit_via_api()
		self.assertRegex(result["name"], r"^COTIZACION-\d+$")
		self.assertEqual(result["status"], "Open")

	def test_read_endpoints_show_the_real_new_name(self):
		result = self._submit_via_api()

		with fx.as_user(self.vendedora):
			mine = cotizaciones.get_my_quotations()
			detail = cotizaciones.get_quotation_detail(result["name"])

		self.assertIn(result["name"], [q["name"] for q in mine])
		self.assertEqual(detail["name"], result["name"])
		self.assertRegex(detail["name"], r"^COTIZACION-\d+$")

	# -- api/cotizaciones.py no fue tocado en este commit -----------------------

	def test_cotizaciones_api_module_was_not_modified_by_this_commit(self):
		"""Structural guardrail for the user's explicit "No modificar
		api/cotizaciones.py" instruction: the module must not reference
		"COTIZACION" anywhere in its own source -- the naming series change
		is applied entirely through fixtures/property_setter.json, never
		through a hardcoded string in application code."""
		import inspect

		source = inspect.getsource(cotizaciones)
		self.assertNotIn("COTIZACION", source)
		self.assertNotIn("naming_series", source)
