# -*- coding: utf-8 -*-
"""Commit 25.26 -- buscador rápido de productos en Cotizaciones, lado
servidor.

Cubre:
  - la búsqueda base, que sigue siendo `ventas.search_items()` sin cambios
    (Cotizaciones la reutiliza tal cual);
  - el endpoint por lotes `cotizaciones.get_quick_search_item_details()`:
    guardas, tope de 20, whitelist exacta de campos, stock informativo,
    precio público de VENTA (o SIN PRECIO), y que nunca expone precio de
    compra/valuation_rate/last_purchase_rate/margen;
  - que la Vendedora sigue SIN permiso general sobre Item Price;
  - que la creación de la Quotation sigue siendo {item_code, qty} y que el
    flujo de descuentos de Facturación (FULL/10/15/20/25, no acumulativo)
    funciona igual sobre una cotización capturada con este buscador.

El contrato de la UI (teclado, + AGREGAR, una llamada batch, etc.) está en
test_cotizaciones_quick_search_ui_contract.py.
"""

import ast
import inspect
import json
import os

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp import pricing
from fabergray_erp.api import cotizaciones, ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_DETAIL_KEYS = {"item_code", "qty_disponible", "public_price", "has_public_price"}

_COST_OR_ECONOMIC_KEYS = {
	"rate",
	"price_list_rate",
	"valuation_rate",
	"last_purchase_rate",
	"standard_rate",
	"buying_price",
	"cost",
	"margin",
	"margin_rate_or_amount",
	"margin_type",
	"discount_percentage",
	"price_list",
	"currency",
}


class _QuickSearchBase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.warehouse = cls.world.warehouse("FG2526 WH")
		cls.customer = cls.world.customer("FG2526 Customer")
		cls.vendedora = cls.world.user("fg2526-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2526-facturacion@example.com", ["Facturación"])
		cls.bodega = cls.world.user("fg2526-bodega@example.com", ["Bodega"])

	@classmethod
	def _item(cls, code, stock=None, **kwargs):
		item = cls.world.item(code, default_warehouse=cls.warehouse.name, **kwargs)
		if stock is not None:
			cls.world.stock_up(item.name, cls.warehouse.name, stock)
		return item

	@classmethod
	def _price(cls, item_code, rate, price_list="Standard Selling"):
		"""Item Price fixture. 0/negative rates are forced with a raw
		db.set_value after insert (fixture setup only) -- the point is to
		prove the endpoint never treats such a row as a valid price, not to
		test Item Price's own validation."""
		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate if rate > 0 else 1,
			}
		)
		doc.insert()
		cls.world.track_existing("Item Price", doc.name)
		if rate <= 0:
			frappe.db.set_value("Item Price", doc.name, "price_list_rate", rate)
		return doc

	def _details(self, codes, user=None):
		with fx.as_user(user or self.vendedora):
			rows = cotizaciones.get_quick_search_item_details(codes)
		return {r["item_code"]: r for r in rows}


class TestQuickSearchBaseSearch(_QuickSearchBase):
	"""1-7: la búsqueda base es ventas.search_items(), reutilizada tal cual."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.active = cls._item("FG2526-DESENG-001")
		frappe.db.set_value("Item", cls.active.name, "item_name", "FG2526 DESENGRASANTE GALON")
		cls.disabled = cls._item("FG2526-DESENG-OFF")
		frappe.db.set_value("Item", cls.disabled.name, "disabled", 1)
		cls.not_sales = cls._item("FG2526-DESENG-NOSALE")
		frappe.db.set_value("Item", cls.not_sales.name, "is_sales_item", 0)

	def _search(self, txt):
		with fx.as_user(self.vendedora):
			return [r["item_code"] for r in ventas.search_items(txt)]

	def test_01_search_by_item_code(self):
		self.assertIn(self.active.name, self._search("FG2526-DESENG-001"))

	def test_02_search_by_item_name(self):
		self.assertIn(self.active.name, self._search("FG2526 DESENGRASANTE GALON"))

	def test_03_partial_search_compatible_with_ventas(self):
		self.assertIn(self.active.name, self._search("DESENGRAS"))
		self.assertIn(self.active.name, self._search("deseng-00"))

	def test_04_active_item_appears(self):
		self.assertIn(self.active.name, self._search("FG2526-DESENG"))

	def test_05_disabled_item_does_not_appear(self):
		self.assertNotIn(self.disabled.name, self._search("FG2526-DESENG"))

	def test_06_non_sales_item_does_not_appear(self):
		self.assertNotIn(self.not_sales.name, self._search("FG2526-DESENG"))

	def test_07_search_returns_at_most_20(self):
		for i in range(21):
			self._item(f"FG2526-LIM-{i:02d}")
		self.assertEqual(len(self._search("FG2526-LIM-")), 20)

	def test_37_ventas_search_items_semantics_unchanged(self):
		source = inspect.getsource(ventas.search_items)
		self.assertIn('filters={"disabled": 0, "is_sales_item": 1, "has_variants": 0}', source)
		self.assertIn('or_filters = [["item_code", "like", f"%{txt}%"], ["item_name", "like", f"%{txt}%"]]', source)
		self.assertIn('fields=["item_code", "item_name", "description", "stock_uom", "image"]', source)
		self.assertIn("limit_page_length=20", source)


class TestQuickSearchItemDetails(_QuickSearchBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.priced = cls._item("FG2526-PRICED", stock=12)
		cls._price(cls.priced.name, 25000)
		cls._price(cls.priced.name, 9999, price_list="Standard Buying")
		frappe.db.set_value("Item", cls.priced.name, "last_purchase_rate", 8888)

		cls.zero_stock = cls._item("FG2526-ZEROSTOCK", stock=0)
		cls._price(cls.zero_stock.name, 18500)

		cls.no_price = cls._item("FG2526-NOPRICE", stock=3)
		cls.zero_price = cls._item("FG2526-ZEROPRICE", stock=3)
		cls._price(cls.zero_price.name, 0)
		cls.negative_price = cls._item("FG2526-NEGPRICE", stock=3)
		cls._price(cls.negative_price.name, -5)
		cls.buying_only = cls._item("FG2526-BUYONLY", stock=3)
		cls._price(cls.buying_only.name, 7777, price_list="Standard Buying")

		cls.disabled = cls._item("FG2526-DET-OFF")
		frappe.db.set_value("Item", cls.disabled.name, "disabled", 1)
		cls.not_sales = cls._item("FG2526-DET-NOSALE")
		frappe.db.set_value("Item", cls.not_sales.name, "is_sales_item", 0)

	# -- contrato de respuesta ------------------------------------------------

	def test_response_is_exact_whitelist_per_item(self):
		rows = self._details([self.priced.name, self.no_price.name])
		for row in rows.values():
			self.assertEqual(set(row.keys()), _DETAIL_KEYS)

	def test_accepts_json_string_like_frappe_call_sends(self):
		rows = self._details(json.dumps([self.priced.name]))
		self.assertIn(self.priced.name, rows)

	def test_empty_list_returns_empty_without_querying(self):
		self.assertEqual(self._details([]), {})

	def test_08_rejects_more_than_20_codes(self):
		codes = [f"FG2526-X-{i:02d}" for i in range(21)]
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.get_quick_search_item_details(codes)

	def test_duplicate_codes_count_once_toward_limit(self):
		rows = self._details([self.priced.name] * 25)
		self.assertEqual(list(rows), [self.priced.name])

	def test_rejects_non_list_payload(self):
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.get_quick_search_item_details('{"a": 1}')

	def test_nonexistent_disabled_and_non_sales_items_are_omitted(self):
		rows = self._details([self.priced.name, "FG2526-DOES-NOT-EXIST", self.disabled.name, self.not_sales.name])
		self.assertEqual(set(rows), {self.priced.name})

	# -- stock ----------------------------------------------------------------

	def test_09_positive_stock(self):
		self.assertEqual(self._details([self.priced.name])[self.priced.name]["qty_disponible"], 12)

	def test_10_zero_stock_is_returned_as_zero(self):
		row = self._details([self.zero_stock.name])[self.zero_stock.name]
		self.assertEqual(row["qty_disponible"], 0)
		self.assertTrue(row["has_public_price"])

	def test_11_zero_stock_does_not_block_quotation(self):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.zero_stock.name, "qty": 5}]
			)
		self.world.track_existing("Quotation", result["name"])
		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(qtn.docstatus, 1)
		self.assertEqual(qtn.items[0].qty, 5)

	def test_details_call_has_no_stock_side_effects(self):
		before = {
			dt: frappe.db.count(dt) for dt in ("Pick List", "Stock Entry", "Bin", "Stock Reservation Entry")
		}
		self._details([self.priced.name, self.zero_stock.name])
		after = {dt: frappe.db.count(dt) for dt in before}
		self.assertEqual(before, after)

	# -- precio público -------------------------------------------------------

	def test_12_public_price_is_standard_selling_rate(self):
		row = self._details([self.priced.name])[self.priced.name]
		self.assertEqual(row["public_price"], 25000)
		self.assertTrue(row["has_public_price"])

	def test_12b_same_source_as_facturacion_reference(self):
		with fx.as_user(self.facturacion):
			reference = pricing.reference_selling_rates([self.priced.name], "Standard Selling")
		self.assertEqual(self._details([self.priced.name])[self.priced.name]["public_price"], reference[self.priced.name])

	def test_13_zero_price_is_sin_precio(self):
		row = self._details([self.zero_price.name])[self.zero_price.name]
		self.assertIsNone(row["public_price"])
		self.assertFalse(row["has_public_price"])

	def test_14_negative_price_is_sin_precio(self):
		row = self._details([self.negative_price.name])[self.negative_price.name]
		self.assertIsNone(row["public_price"])
		self.assertFalse(row["has_public_price"])

	def test_15_missing_item_price_is_sin_precio(self):
		row = self._details([self.no_price.name])[self.no_price.name]
		self.assertIsNone(row["public_price"])
		self.assertFalse(row["has_public_price"])

	def test_invalid_default_price_list_means_sin_precio_never_buying(self):
		"""If Selling Settings ever pointed at a buying list, the endpoint
		must refuse it (SIN PRECIO), never read that list."""
		original = frappe.db.get_single_value("Selling Settings", "selling_price_list")
		frappe.db.set_single_value("Selling Settings", "selling_price_list", "Standard Buying")
		try:
			rows = self._details([self.priced.name, self.buying_only.name])
		finally:
			frappe.db.set_single_value("Selling Settings", "selling_price_list", original)
		for row in rows.values():
			self.assertIsNone(row["public_price"])
			self.assertFalse(row["has_public_price"])

	# -- no exposición de costos ----------------------------------------------

	def _all_values(self, rows):
		return [v for row in rows.values() for v in row.values()]

	def test_16_never_exposes_buying_price(self):
		rows = self._details([self.priced.name, self.buying_only.name])
		self.assertNotIn(9999, self._all_values(rows))
		self.assertNotIn(7777, self._all_values(rows))
		self.assertIsNone(rows[self.buying_only.name]["public_price"])

	def test_17_never_exposes_valuation_rate(self):
		rows = self._details([self.priced.name])
		self.assertNotIn("valuation_rate", rows[self.priced.name])
		# fx.stock_up() seeds Bin.valuation_rate = 100
		self.assertNotIn(100, self._all_values(rows))

	def test_18_never_exposes_last_purchase_rate(self):
		rows = self._details([self.priced.name])
		self.assertNotIn("last_purchase_rate", rows[self.priced.name])
		self.assertNotIn(8888, self._all_values(rows))

	def test_19_never_exposes_margin_or_other_economic_keys(self):
		rows = self._details([self.priced.name, self.zero_stock.name, self.no_price.name])
		for row in rows.values():
			self.assertFalse(_COST_OR_ECONOMIC_KEYS & set(row.keys()))

	# -- roles ----------------------------------------------------------------

	def test_20_authorized_roles(self):
		self.assertIn(self.priced.name, self._details([self.priced.name], user=self.vendedora))
		self.assertIn(self.priced.name, self._details([self.priced.name], user="Administrator"))

	def test_21_unauthorized_roles_rejected(self):
		for user in (self.bodega, self.facturacion):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError):
					cotizaciones.get_quick_search_item_details([self.priced.name])

	def test_guest_rejected(self):
		with fx.as_user("Guest"):
			with self.assertRaises((frappe.AuthenticationError, frappe.PermissionError)):
				cotizaciones.get_quick_search_item_details([self.priced.name])

	# -- Item Price sigue cerrado para la Vendedora ---------------------------

	def test_36_vendedora_has_no_general_item_price_permission(self):
		self.assertFalse(frappe.has_permission("Item Price", "read", user=self.vendedora))
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				pricing.reference_selling_rates([self.priced.name], "Standard Selling")

	def test_36b_no_item_price_docperm_fixture_for_vendedora(self):
		path = os.path.join(frappe.get_app_path("fabergray_erp"), "fixtures", "custom_docperm.json")
		with open(path, encoding="utf-8") as f:
			rows = json.load(f)
		self.assertFalse([r for r in rows if r.get("parent") == "Item Price" and r.get("role") == "Vendedora"])

	def test_privileged_read_is_confined_to_pricing_helper(self):
		"""No api/* module hardcodes ignore_permissions=True (Commit 18.1
		guardrail stays intact); in pricing.py only public_selling_rates()
		does, and the endpoint reaches Item Price only through it."""

		def privileged_functions(module):
			found = []
			for node in ast.walk(ast.parse(inspect.getsource(module))):
				if isinstance(node, ast.FunctionDef):
					for sub in ast.walk(node):
						if (
							isinstance(sub, ast.keyword)
							and sub.arg == "ignore_permissions"
							and isinstance(sub.value, ast.Constant)
							and sub.value.value is True
						):
							found.append(node.name)
			return found

		self.assertEqual(privileged_functions(cotizaciones), [])
		self.assertEqual(privileged_functions(pricing), ["public_selling_rates"])
		self.assertIn("public_selling_rates(codes)", inspect.getsource(cotizaciones.get_quick_search_item_details))
		self.assertIn("ignore_permissions=False", inspect.getsource(pricing.reference_selling_rates))

	def test_existing_get_item_info_contract_untouched(self):
		with fx.as_user(self.vendedora):
			info = cotizaciones.get_item_info(self.priced.name)
		self.assertEqual(set(info.keys()), {"item_code", "item_name", "description", "stock_uom", "image"})


class TestQuickSearchQuotationFlow(_QuickSearchBase):
	"""34-35 y 38: el buscador no cambia lo que se envía ni el flujo de
	descuentos de Facturación."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.item = cls._item("FG2526-FLOW", stock=0)
		cls._price(cls.item.name, 100000)

	def _captured_quotation(self, qty=3):
		# Same sequence the UI runs: search -> batch details -> create with {item_code, qty}.
		with fx.as_user(self.vendedora):
			found = [r["item_code"] for r in ventas.search_items("FG2526-FLOW")]
			self.assertIn(self.item.name, found)
			details = cotizaciones.get_quick_search_item_details(found)
			self.assertTrue(details)
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": qty}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		return result["name"]

	def _apply(self, name, mode):
		with fx.as_user(self.facturacion):
			result = cotizaciones.apply_quotation_price_mode(name, mode)
		self.world.track_existing("Quotation", result["name"])
		return result["name"]

	def _rate(self, name):
		return frappe.get_doc("Quotation", name).items[0].rate

	def test_35_create_still_rejects_economic_fields(self):
		for field in ("rate", "price_list_rate", "discount_percentage", "public_price"):
			with fx.as_user(self.vendedora):
				with self.assertRaises(frappe.ValidationError):
					cotizaciones.create_and_submit_quotation(
						customer=self.customer.name,
						items=[{"item_code": self.item.name, "qty": 1, field: 1}],
					)

	def test_34_allowed_item_fields_unchanged(self):
		self.assertEqual(cotizaciones._ALLOWED_ITEM_FIELDS, {"item_code", "qty"})

	def test_38_price_modes_after_quick_capture(self):
		name = self._captured_quotation()
		expected = {"FULL": 100000, "DISCOUNT_10": 90000, "DISCOUNT_15": 85000, "DISCOUNT_20": 80000, "DISCOUNT_25": 75000}
		for mode in ("DISCOUNT_10", "DISCOUNT_15", "DISCOUNT_20", "DISCOUNT_25", "FULL"):
			name = self._apply(name, mode)
			self.assertEqual(self._rate(name), expected[mode], mode)

	def test_38b_discounts_are_not_cumulative(self):
		name = self._captured_quotation()
		name = self._apply(name, "DISCOUNT_10")
		self.assertEqual(self._rate(name), 90000)
		name = self._apply(name, "DISCOUNT_20")
		self.assertEqual(self._rate(name), 80000)  # never 72.000

	def test_38c_facturacion_price_mode_endpoint_still_gated(self):
		name = self._captured_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_10")


class TestCotizacionRapidaServer(_QuickSearchBase):
	"""Commit 25.26 (ampliación) -- "Cotización rápida" reutiliza tal cual
	`ventas.parse_quick_order` (mismo endpoint whitelisted, llamado desde
	cotizaciones.js con call_ventas()) y termina en el MISMO
	create_and_submit_quotation con {item_code, qty}. Nada del lado servidor
	cambió para esto; estos tests fijan ese contrato."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.deseng = cls._item("FG2526Q-DESENG", stock=0)
		frappe.db.set_value("Item", cls.deseng.name, "item_name", "ZQXDESENG FGCR GALON")
		cls._price(cls.deseng.name, 25000)
		cls.escoba = cls._item("FG2526Q-ESCOBA", stock=4)
		frappe.db.set_value("Item", cls.escoba.name, "item_name", "ZQXESCOBA FGCR INDUSTRIAL")
		cls._price(cls.escoba.name, 18500)
		cls.guante_m = cls._item("FG2526Q-GUANTE-M")
		frappe.db.set_value("Item", cls.guante_m.name, "item_name", "ZQXGUANTE FGCR NITRILO TALLA M")
		cls.guante_l = cls._item("FG2526Q-GUANTE-L")
		frappe.db.set_value("Item", cls.guante_l.name, "item_name", "ZQXGUANTE FGCR NITRILO TALLA L")

	def setUp(self):
		from fabergray_erp.quick_order import catalog as quick_order_catalog

		quick_order_catalog.invalidate_catalog_cache()
		self.addCleanup(quick_order_catalog.invalidate_catalog_cache)

	def _parse(self, text, user=None):
		with fx.as_user(user or self.vendedora):
			return ventas.parse_quick_order(text)["lines"]

	def test_interprets_quantities_and_finds_products(self):
		lines = self._parse("5 zqxdeseng fgcr galon\n3 zqxescoba fgcr industrial")
		self.assertEqual([l["qty"] for l in lines], [5, 3])
		self.assertEqual(lines[0]["candidates"][0]["item_code"], self.deseng.name)
		self.assertEqual(lines[1]["candidates"][0]["item_code"], self.escoba.name)
		# Preselection stays the server's own rule (high AND not ambiguous);
		# anything else needs an explicit pick in the UI.
		for line in lines:
			pre = line["preselected_item"]
			if pre:
				self.assertEqual(line["confidence"], "high")
				self.assertFalse(line["ambiguous"])

	def test_not_found_line_is_never_preselected(self):
		line = self._parse("2 qwzzkx inexistente vbnmrt")[0]
		self.assertEqual(line["qty"], 2)
		self.assertIsNone(line["preselected_item"])
		self.assertNotIn(self.deseng.name, {c["item_code"] for c in line["candidates"]})

	def test_ambiguous_line_is_never_preselected(self):
		line = self._parse("4 zqxguante fgcr nitrilo")[0]
		self.assertIsNone(line["preselected_item"])
		codes = {c["item_code"] for c in line["candidates"]}
		self.assertTrue({self.guante_m.name, self.guante_l.name} <= codes)

	def test_same_input_same_interpretation_as_pedido_rapido(self):
		"""Cotizaciones calls the very same endpoint Ventas does, so a given
		text must interpret identically for both pages' users."""
		text = "5 zqxdeseng fgcr galon\n4 zqxguante fgcr nitrilo\n2 qwzzkx inexistente"
		self.assertEqual(self._parse(text), self._parse(text, user=self.vendedora))
		js_path = os.path.join(
			frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
		)
		with open(js_path, encoding="utf-8") as f:
			self.assertIn('this.call_ventas("parse_quick_order", { text: text })', f.read())

	def test_price_in_text_never_sets_rate(self):
		line = self._parse("5 zqxdeseng fgcr galon $1000")[0]
		self.assertEqual(line["qty"], 5)
		for candidate in line["candidates"]:
			self.assertFalse(_COST_OR_ECONOMIC_KEYS & set(candidate.keys()))
		item = line["preselected_item"] or line["candidates"][0]
		self.assertEqual(item["item_code"], self.deseng.name)
		# The page only ever sends {item_code, qty}; ERPNext prices it.
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item["item_code"], "qty": line["qty"]}]
			)
		self.world.track_existing("Quotation", result["name"])
		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(qtn.items[0].rate, 25000)
		self.assertNotEqual(qtn.items[0].rate, 1000)

	def test_merged_manual_plus_quick_quantity_is_one_line(self):
		"""Cart had 2 (manual) + Cotización rápida 5 -> the page sends one
		{item_code, qty: 7} line through the same create endpoint."""
		line = self._parse("5 zqxdeseng fgcr galon")[0]
		merged_qty = 2 + line["qty"]
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.deseng.name, "qty": merged_qty}]
			)
		self.world.track_existing("Quotation", result["name"])
		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(len(qtn.items), 1)
		self.assertEqual(qtn.items[0].qty, 7)

	def test_zero_stock_product_from_quick_quote_can_be_quoted(self):
		line = self._parse("5 zqxdeseng fgcr galon")[0]
		self.assertEqual(self._details([self.deseng.name])[self.deseng.name]["qty_disponible"], 0)
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name,
				items=[{"item_code": line["candidates"][0]["item_code"], "qty": line["qty"]}],
			)
		self.world.track_existing("Quotation", result["name"])
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "docstatus"), 1)

	def test_parse_is_read_only(self):
		before = {dt: frappe.db.count(dt) for dt in ("Quotation", "Sales Order", "Pick List", "Stock Entry", "Bin")}
		self._parse("5 zqxdeseng fgcr galon\n3 zqxescoba fgcr industrial")
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_permissions_unchanged(self):
		self.assertTrue(self._parse("5 zqxdeseng fgcr galon"))
		with fx.as_user("Guest"):
			with self.assertRaises((frappe.AuthenticationError, frappe.PermissionError)):
				ventas.parse_quick_order("5 zqxdeseng fgcr galon")
		with fx.as_user(self.bodega):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.create_and_submit_quotation(
					customer=self.customer.name, items=[{"item_code": self.deseng.name, "qty": 1}]
				)


class TestCotizacionRapidaOfficialFormat(_QuickSearchBase):
	"""Commit 25.26 -- the official Cotización rápida format IS Pedido
	rápido's: quantity FIRST, then the product, one per line. Same parser,
	same endpoint -> same interpretation. No second syntax."""

	def setUp(self):
		from fabergray_erp.quick_order import catalog as quick_order_catalog

		quick_order_catalog.invalidate_catalog_cache()
		self.addCleanup(quick_order_catalog.invalidate_catalog_cache)

	def _parse(self, text):
		with fx.as_user(self.vendedora):
			return ventas.parse_quick_order(text)["lines"]

	def test_5_desengrasante_1_galon_is_qty_5(self):
		line = self._parse("5 DESENGRASANTE 1 GALON")[0]
		self.assertEqual(line["qty"], 5)
		self.assertEqual(line["source_text"], "5 DESENGRASANTE 1 GALON")

	def test_3_escoba_industrial_is_qty_3(self):
		self.assertEqual(self._parse("3 ESCOBA INDUSTRIAL")[0]["qty"], 3)

	def test_multi_line_official_example(self):
		lines = self._parse("5 DESENGRASANTE 1 GALON\n3 ESCOBA INDUSTRIAL\n10 HIPOCLORITO GALON")
		self.assertEqual([l["qty"] for l in lines], [5, 3, 10])

	def test_cotizacion_rapida_equals_pedido_rapido_for_same_input(self):
		"""Both pages call fabergray_erp.api.ventas.parse_quick_order and
		nothing else, so the same text yields the same response -- lines,
		quantities, candidates, confidence, ambiguity, preselection."""
		text = "5 DESENGRASANTE 1 GALON\n3 ESCOBA INDUSTRIAL\n10 HIPOCLORITO GALON"
		pedido_rapido = self._parse(text)  # what Ventas' interpret_quick_order() receives
		cotizacion_rapida = self._parse(text)  # what Cotizaciones' process_quick_quote() receives
		self.assertEqual(pedido_rapido, cotizacion_rapida)

		page_dir = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page")
		with open(os.path.join(page_dir, "ventas", "ventas.js"), encoding="utf-8") as f:
			ventas_js = f.read()
		with open(os.path.join(page_dir, "cotizaciones", "cotizaciones.js"), encoding="utf-8") as f:
			cotizaciones_js = f.read()
		# Ventas: this.call() with method_prefix "fabergray_erp.api.ventas.";
		# Cotizaciones: call_ventas() with the same prefix.
		self.assertIn('this.method_prefix = "fabergray_erp.api.ventas.";', ventas_js)
		self.assertIn('this.call("parse_quick_order", { text: text })', ventas_js)
		self.assertIn('this.ventas_method_prefix = "fabergray_erp.api.ventas.";', cotizaciones_js)
		self.assertIn('this.call_ventas("parse_quick_order", { text: text })', cotizaciones_js)

	def test_quantity_at_end_is_not_a_second_syntax(self):
		"""Pins current parser behaviour: a trailing "x5" is NOT a quantity
		(qty defaults to 1). 25.26 deliberately adds no second format."""
		line = self._parse("DESENGRASANTE 1 GALON x5")[0]
		self.assertEqual(line["qty"], 1)

	def test_visible_example_parses_with_quantity_first(self):
		"""The example shown in the Cotización rápida help/placeholder must
		be valid input for the shared parser, quantity first."""
		import re

		from fabergray_erp.quick_order import parser

		js_path = os.path.join(
			frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
		)
		with open(js_path, encoding="utf-8") as f:
			m = re.search(r'const QUICK_QUOTE_EXAMPLE = "([^"]+)";', f.read())
		self.assertIsNotNone(m)
		example_lines = m.group(1).split("\\n")
		self.assertEqual(len(example_lines), 3)
		for text in example_lines:
			leading = re.match(r"^(\d+)\s", text)
			self.assertIsNotNone(leading, text)
			self.assertEqual(parser.parse_order_line(text)["qty"], int(leading.group(1)), text)
