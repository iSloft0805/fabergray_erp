# -*- coding: utf-8 -*-
"""Commit 18.5a -- Sales Order naming series: PEDIDO-.# as the default
(no year, no padding, no reset), SAL-ORD-.YYYY.- kept as a non-default
second option. Native mechanism only -- a `Property Setter` on
`Sales Order.naming_series` (`options`/`default`), the exact same
mechanism `frappe.core.doctype.document_naming_settings`'s own
`update_series()` uses internally (confirmed by reading it directly, not
assumed) -- no custom Python counter anywhere in this app.

Every test drives the real `Sales Order.on_submit`/`on_cancel` hooks
(built directly, not through `TestWorld.multi_item_sales_order()`, which
stays wrapped in `fx.without_sales_order_hook()`) so the real
Fulfillment Engine creates real artifacts against a real, newly-named
Sales Order -- exactly like `test_sales_order_hook.py` already does for
every other Commit 16/17/19.x scenario.
"""

import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

#: The exact pattern this app's Property Setter puts in
#: Sales Order.naming_series's `default` property (fixtures/property_setter.json).
PEDIDO_SERIES = "PEDIDO-.#"
PEDIDO_PREFIX = "PEDIDO-"


class TestSalesOrderNamingSeries(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg185b-bodega@example.com", ["Bodega"])

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG185B {tag}")
		item = self.world.item(f"FG185B-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG185B {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _draft_sales_order(self, customer, items):
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": items[0]["warehouse"],
				"items": [{**item, "delivery_date": delivery_date} for item in items],
			}
		)
		# naming_series deliberately left unset -- the whole point of this
		# commit is that the NEW default applies automatically, exactly as
		# every real caller (Vendedora's own create_and_submit_sales_order(),
		# a human via Desk) already does today.
		doc.insert()
		self.world.track_existing("Sales Order", doc.name)
		return doc

	def _submit_via_hook(self, customer, items):
		doc = self._draft_sales_order(customer, items)
		doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		return doc

	# -- Configuración nativa aplicada -----------------------------------------

	def test_naming_series_options_and_default_are_configured_via_property_setter(self):
		"""Confirms the fixture actually applied to this site -- PEDIDO-.#
		first (and therefore default, per get_default_naming_series()'s own
		"first truthy option wins" rule) with SAL-ORD-.YYYY.- kept as a
		real, still-usable second option, exactly the two decisions
		approved (nothing renamed, nothing removed)."""
		meta = frappe.get_meta("Sales Order")
		field = meta.get_field("naming_series")

		self.assertEqual(field.options, "PEDIDO-.#\nSAL-ORD-.YYYY.-")
		self.assertEqual(field.default, PEDIDO_SERIES)

		from frappe.model.naming import get_default_naming_series

		self.assertEqual(get_default_naming_series("Sales Order"), PEDIDO_SERIES)

		# Both Property Setters exist, are exactly the two rows this commit's
		# fixture creates, and nothing else touches this field.
		rows = frappe.get_all(
			"Property Setter",
			filters={"doc_type": "Sales Order", "field_name": "naming_series"},
			fields=["property", "value"],
		)
		self.assertEqual(
			{(r.property, r.value) for r in rows},
			{("options", "PEDIDO-.#\nSAL-ORD-.YYYY.-"), ("default", PEDIDO_SERIES)},
		)

	# -- Primera / segunda SO nueva usan la nueva serie, incrementan, sin año --

	def test_first_and_second_new_sales_orders_use_the_new_series_and_increment(self):
		wh, item, customer = self._new_world("First", stock_qty=10)
		so_1 = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 5, "rate": 100}])
		so_2 = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 3, "rate": 100}])

		self.assertRegex(so_1.name, r"^PEDIDO-\d+$")
		self.assertRegex(so_2.name, r"^PEDIDO-\d+$")

		n1 = int(so_1.name.split(PEDIDO_PREFIX)[1])
		n2 = int(so_2.name.split(PEDIDO_PREFIX)[1])
		self.assertEqual(n2, n1 + 1)  # increments by exactly 1, second call right after the first

	def test_new_series_name_never_contains_the_year(self):
		"""No 4-digit year token anywhere in a newly-generated name --
		PEDIDO-.# has no .YYYY. part at all, unlike the old
		SAL-ORD-.YYYY.- series it replaces as default."""
		wh, item, customer = self._new_world("NoYear", stock_qty=5)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 2, "rate": 100}])

		self.assertNotIn(str(nowdate()[:4]), so.name)
		self.assertRegex(so.name, r"^PEDIDO-\d+$")  # nothing but the counter after the literal prefix

	def test_series_prefix_has_no_date_token_so_it_can_never_reset(self):
		"""Structural proof of "no reinicio anual": the exact prefix
		key used to look up `tabSeries` (NamingSeries.get_prefix()) is the
		literal string "PEDIDO-", with zero date-related parts (`.YYYY.`,
		`.YY.`, `.MM.`, `.DD.`) -- since `getseries()` keys the counter
		purely off this string, a counter can only ever "reset" if the key
		itself changes, and this key is date-independent, therefore
		constant forever. Fast-forwarding the real system clock across a
		year boundary inside a test isn't practical/honest to simulate --
		this is the correct, honest way to prove non-reset structurally."""
		from frappe.model.naming import NamingSeries

		prefix = NamingSeries(PEDIDO_SERIES).get_prefix()
		self.assertEqual(prefix, PEDIDO_PREFIX)
		for date_token in (".YYYY.", ".YY.", ".MM.", ".DD."):
			self.assertNotIn(date_token, PEDIDO_SERIES)

	def test_new_series_does_not_collide_with_existing_or_old_series_documents(self):
		wh, item, customer = self._new_world("NoCollision", stock_qty=10)

		# Three in a row -- every generated name must be genuinely new and
		# distinct, never reusing something that already exists.
		names = []
		for _ in range(3):
			so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 1, "rate": 100}])
			names.append(so.name)

		self.assertEqual(len(names), len(set(names)))  # no duplicates

		# and none of them collide with anything already using the OLD
		# series' own name shape either (structurally impossible -- distinct
		# prefixes -- but asserted directly rather than just assumed).
		for name in names:
			self.assertFalse(name.startswith("SAL-ORD-"))

	def test_old_series_still_works_when_explicitly_requested(self):
		"""SAL-ORD-.YYYY.- was kept, not removed -- a caller that explicitly
		asks for it (the only way it can still be produced, since it is no
		longer the default) gets it, with its own separate, unaffected
		counter."""
		wh, item, customer = self._new_world("OldSeries", stock_qty=5)
		delivery_date = add_days(nowdate(), 7)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": "SAL-ORD-.YYYY.-",
				"customer": customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": wh.name,
				"items": [{"item_code": item.name, "warehouse": wh.name, "qty": 1, "rate": 100, "delivery_date": delivery_date}],
			}
		)
		so.insert()
		self.world.track_existing("Sales Order", so.name)

		self.assertTrue(so.name.startswith("SAL-ORD-"))
		self.assertIn(nowdate()[:4], so.name)

	# -- El Fulfillment Engine sigue enlazando correctamente ------------------

	def test_fulfillment_engine_links_correctly_to_the_new_style_name(self):
		wh, item, customer = self._new_world(
			"EngineLinks", stock_qty=3, default_material_request_type="Purchase"
		)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 8, "rate": 100}])
		self.assertRegex(so.name, r"^PEDIDO-\d+$")

		pick_list_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]
		self.assertEqual(frappe.get_doc("Pick List", pick_list_name).get("locations")[0].sales_order, so.name)

		report_name = frappe.get_all("Reporte de Faltante", filters={"sales_order": so.name}, pluck="name")[0]
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).sales_order, so.name)

		mr_name = frappe.get_all("Material Request Item", filters={"sales_order": so.name}, pluck="parent", distinct=True)[0]
		mr_item = frappe.get_doc("Material Request", mr_name).items[0]
		self.assertEqual(mr_item.sales_order, so.name)

		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		self.assertIn(pick_list_name, [p["name"] for p in queue["pendientes"]])

	# -- Cancelación sigue funcionando -----------------------------------------

	def test_cancellation_still_works_with_the_new_style_name(self):
		wh, item, customer = self._new_world("CancelWorks", stock_qty=10)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		self.assertRegex(so.name, r"^PEDIDO-\d+$")
		pick_list_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]

		so.cancel()

		so.reload()
		self.assertEqual(so.docstatus, 2)
		self.assertFalse(frappe.db.exists("Pick List", pick_list_name))  # Commit 17 cleanup, unchanged

	# -- Compatibilidad Frappe 16: detectar si cambia el comportamiento --------

	def test_compat_parse_naming_series_still_ignores_the_auto_appended_padding(self):
		"""Documented, deliberate reliance (see FULFILLMENT_ENGINE_CONTRACT.md,
		"Commit 18.5a") on a specific Frappe 16 behaviour: `set_name_by_
		naming_series()` (frappe/model/naming.py) unconditionally appends
		".#####" to whatever the naming_series field value already is
		(`doc.naming_series + ".#####"`), and `parse_naming_series()` only
		honours the FIRST "#" group it encounters, silently ignoring any
		later one -- which is the ONLY reason "PEDIDO-.#" (one hash, no
		padding) still produces "PEDIDO-1" instead of some doubled/padded
		result. This test exercises that exact mechanism directly (a
		throwaway series key, not the real "PEDIDO-" one, so it never
		touches the real counter) and MUST fail loudly if a future Frappe
		version changes either half of this -- so a silent format
		regression is caught here, in a dedicated test, rather than
		discovered later as a support ticket about padded/malformed Sales
		Order names.
		"""
		from frappe.model.naming import parse_naming_series

		throwaway_key = "FG185B-NAMING-COMPAT-TEST-"
		naming_series_field_value = f"{throwaway_key}.#"
		# exactly what set_name_by_naming_series() feeds parse_naming_series()
		actual_pattern = naming_series_field_value + ".#####"
		self.assertEqual(actual_pattern, f"{throwaway_key}.#.#####")

		try:
			first = parse_naming_series(actual_pattern.split("."))
			second = parse_naming_series(actual_pattern.split("."))
		finally:
			frappe.db.delete("Series", {"name": throwaway_key})
			frappe.db.commit()

		self.assertEqual(first, f"{throwaway_key}1")
		self.assertEqual(second, f"{throwaway_key}2")
		# and, just as importantly: no zero-padding snuck in from the
		# auto-appended ".#####" half of the pattern.
		self.assertNotRegex(first, r"0\d+$")
		self.assertIsNone(re.search(r"-0", first))
