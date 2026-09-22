# -*- coding: utf-8 -*-
"""Commit 25.25 -- precios de factura por línea de Pick List (opción B).

El precio final de Facturación vive en Pick List Item (fg_invoice_rate/
fg_invoice_price_mode/fg_invoice_public_rate); el Sales Order conserva su
precio original. Cubre los 39 casos del brief: precio público real, modos
FULL/10/15/20/25 sin descuento acumulativo, precio especial, permisos,
precio negociado del pedido, reemplazo de especiales solo con
replace_special, totales, congelamiento, PDF, protección after-submit,
Version, contrato de get_invoicing_detail y atomicidad.

Nombres con sufijo aleatorio: un setUpClass interrumpido no deja registros
que choquen con la siguiente corrida.
"""

import inspect
import json
import os
from unittest.mock import patch

import frappe
from frappe.client import set_value
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp import pricing
from fabergray_erp.api import bodega, cotizaciones, facturacion, inventario
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

PRICE_LIST = "Standard Selling"
SPECIAL = facturacion.INVOICE_PRICE_MODE_SPECIAL
ORDER = facturacion.INVOICE_PRICE_MODE_ORDER


class TestFacturacionInvoicePricing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FG2525 {cls.sfx} WH")
		cls.customer = cls.world.customer(f"FG2525 {cls.sfx} Cliente")

		cls.bodega_user = cls.world.user(f"fg2525-{cls.sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fg2525-{cls.sfx}-facturacion@example.com", ["Facturación"])
		cls.jefe_user = cls.world.user(f"fg2525-{cls.sfx}-jefe@example.com", ["Jefe de Bodega"])
		cls.vendedora_user = cls.world.user(f"fg2525-{cls.sfx}-vendedora@example.com", ["Vendedora"])
		cls._counter = 0

	# -- helpers --------------------------------------------------------------

	def _item(self, public_rate=None):
		"""Item con stock real y, opcionalmente, precio público en Standard
		Selling."""
		type(self)._counter += 1
		item = self.world.item(f"FG2525-{self.sfx}-{self._counter}")
		if public_rate is not None:
			price = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": item.name,
					"price_list": PRICE_LIST,
					"selling": 1,
					"price_list_rate": public_rate,
				}
			).insert()
			self.world.track_existing("Item Price", price.name)
		self.world.stock_up_real(item.name, self.wh.name, 500, rate=10)
		return item

	def _sales_order(self, lines, discount_amount=0):
		"""lines: [(item, qty, so_rate)]. discount_amount>0 agrega un
		descuento global (apply_discount_on Grand Total)."""
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"selling_price_list": PRICE_LIST,
				"set_warehouse": self.wh.name,
				"items": [
					{
						"item_code": item.name,
						"warehouse": self.wh.name,
						"qty": qty,
						"rate": rate,
						"delivery_date": delivery_date,
					}
					for item, qty, rate in lines
				],
			}
		)
		if discount_amount:
			doc.apply_discount_on = "Grand Total"
			doc.discount_amount = discount_amount
		# ERPNext crea un Item Price automáticamente (con el rate del pedido)
		# para una línea sin precio de lista si esta opción está activa --
		# desactivada solo aquí para poder probar "producto sin precio
		# público" y no dejar Item Price sin rastrear.
		with fx.stock_settings(auto_insert_price_list_rate_if_missing=0):
			doc.insert()
			with fx.without_sales_order_hook():
				doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		self.world._track(doc)
		return doc

	def _pick_list(self, lines, discount_amount=0):
		so = self._sales_order(lines, discount_amount=discount_amount)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, pl.name

	def _rows(self, pl_name):
		return {row.item_code: row for row in frappe.get_doc("Pick List", pl_name).locations}

	def _as_fact(self, fn, *args, **kwargs):
		with fx.as_user(self.facturacion_user):
			return fn(*args, **kwargs)

	def _pricing(self, pl_name):
		return self._as_fact(facturacion.get_invoicing_pricing, pl_name)

	def _line(self, pricing_payload, item):
		return next(line for line in pricing_payload["lines"] if line["item_code"] == item.name)

	def _apply(self, pl_name, mode, replace_special=0):
		return self._as_fact(facturacion.apply_invoice_price_mode, pl_name, mode, replace_special=replace_special)

	def _set_price(self, pl_name, item, rate):
		row = self._rows(pl_name)[item.name]
		return self._as_fact(facturacion.set_invoice_line_price, pl_name, row.name, rate)

	def _invoice(self, pl_name, issuer="integrandoMAS"):
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl_name)
			facturacion.set_invoice_issuer(pl_name, issuer)

	def _pdf_html(self, pl_name):
		with fx.as_user(self.facturacion_user):
			return frappe.get_print(
				"Pick List", pl_name, print_format=facturacion.INVOICE_PDF_PRINT_FORMAT_NAME, as_pdf=False
			)

	def _versions(self, pl_name):
		return frappe.get_all(
			"Version",
			filters={"ref_doctype": "Pick List", "docname": pl_name},
			fields=["name", "owner", "creation", "data"],
			order_by="creation desc",
		)

	# -- 1. precio público real ---------------------------------------------------

	def test_01_public_price_comes_from_item_price(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		line = self._line(self._pricing(pl_name), item)
		self.assertEqual(line["public_rate"], 45000)
		self.assertEqual(line["order_rate"], 45000)
		self.assertEqual(line["final_rate"], 45000)

	# -- 2-6. modos ---------------------------------------------------------------

	def _assert_mode(self, mode, expected):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		result = self._apply(pl_name, mode)
		self.assertFalse(result["requires_confirmation"])
		row = self._rows(pl_name)[item.name]
		self.assertEqual(row.fg_invoice_rate, expected)
		self.assertEqual(row.fg_invoice_public_rate, 100000)
		self.assertEqual(row.fg_invoice_price_mode, pricing.PRICE_MODE_LABELS[mode])
		self.assertEqual(self._line(result["pricing"], item)["final_rate"], expected)

	def test_02_full_equals_public_price(self):
		self._assert_mode("FULL", 100000)

	def test_03_discount_10(self):
		self._assert_mode("DISCOUNT_10", 90000)

	def test_04_discount_15(self):
		self._assert_mode("DISCOUNT_15", 85000)

	def test_05_discount_20(self):
		self._assert_mode("DISCOUNT_20", 80000)

	def test_06_discount_25(self):
		self._assert_mode("DISCOUNT_25", 75000)

	# -- 7. sin descuento acumulativo ---------------------------------------------

	def test_07_discounts_never_compound(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		self._apply(pl_name, "DISCOUNT_10")
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 90000)
		self._apply(pl_name, "DISCOUNT_20")
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 80000)  # nunca 72.000

	# -- 8-10. precio manual -------------------------------------------------------

	def test_08_valid_manual_price(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 2, 45000)])
		result = self._set_price(pl_name, item, "41500")
		row = self._rows(pl_name)[item.name]
		self.assertEqual(row.fg_invoice_rate, 41500)
		self.assertEqual(row.fg_invoice_price_mode, SPECIAL)
		self.assertEqual(row.fg_invoice_public_rate, 45000)
		line = self._line(result["pricing"], item)
		self.assertTrue(line["is_special"])
		self.assertEqual(line["amount"], 83000)

	def test_09_manual_price_zero_or_negative_rejected(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		for bad in (0, "0", -1, "-41500", "0.00"):
			with self.assertRaises(facturacion.InvalidInvoicePriceError, msg=repr(bad)):
				self._set_price(pl_name, item, bad)
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 0)

	def test_10_non_numeric_or_imprecise_price_rejected(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		for bad in ("abc", "", None, True, "41.500,00", "nan", "inf", "Infinity", "12.345"):
			with self.assertRaises(facturacion.InvalidInvoicePriceError, msg=repr(bad)):
				self._set_price(pl_name, item, bad)
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 0)

	# -- 11. producto sin Item Price -----------------------------------------------

	def test_11_product_without_item_price(self):
		item = self._item(None)
		_, pl_name = self._pick_list([(item, 1, 30000)])
		result = self._pricing(pl_name)
		line = self._line(result, item)
		self.assertIsNone(line["public_rate"])
		self.assertEqual(line["final_rate"], 30000)  # el precio del pedido sigue siendo válido
		self.assertEqual(line["price_mode"], ORDER)
		self.assertIn(item.name, result["missing_public_items"])
		self.assertIsNone(result["totals"]["public_subtotal"])  # nunca un subtotal inventado
		self.assertIsNone(result["totals"]["adjustment"])

	# -- 12-15. permisos -------------------------------------------------------------

	def test_12_facturacion_user_allowed(self):
		item = self._item(50000)
		_, pl_name = self._pick_list([(item, 1, 50000)])
		self.assertTrue(self._pricing(pl_name)["lines"])
		self.assertFalse(self._apply(pl_name, "DISCOUNT_10")["requires_confirmation"])
		self.assertTrue(self._set_price(pl_name, item, 47000)["changed"])

	def _assert_rejected_for(self, user):
		item = self._item(50000)
		_, pl_name = self._pick_list([(item, 1, 50000)])
		row = self._rows(pl_name)[item.name]
		with fx.as_user(user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.get_invoicing_pricing(pl_name)
			with self.assertRaises(frappe.PermissionError):
				facturacion.apply_invoice_price_mode(pl_name, "DISCOUNT_10")
			with self.assertRaises(frappe.PermissionError):
				facturacion.set_invoice_line_price(pl_name, row.name, 40000)
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 0)

	def test_13_bodega_rejected(self):
		self._assert_rejected_for(self.bodega_user)

	def test_14_jefe_de_bodega_rejected(self):
		self._assert_rejected_for(self.jefe_user)

	def test_15_vendedora_rejected(self):
		self._assert_rejected_for(self.vendedora_user)

	# -- 16/36. precio negociado del pedido -------------------------------------------

	def test_16_36_negotiated_order_price_is_kept_on_open(self):
		"""Cotización -> Sales Order con precio negociado (85.000) mientras el
		público vigente es 100.000: abrir Facturación no lo sobrescribe ni lo
		convierte en precio especial."""
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 85000)])
		first = self._pricing(pl_name)
		self._pricing(pl_name)  # abrir de nuevo tampoco escribe nada
		line = self._line(first, item)
		self.assertEqual(line["public_rate"], 100000)
		self.assertEqual(line["order_rate"], 85000)
		self.assertEqual(line["final_rate"], 85000)
		self.assertEqual(line["price_mode"], "Descuento 15%")  # coincide exactamente con un modo
		self.assertFalse(line["is_special"])
		row = self._rows(pl_name)[item.name]
		self.assertEqual(row.fg_invoice_rate, 0)
		self.assertFalse(row.fg_invoice_price_mode)

		other = self._item(100000)
		_, other_pl = self._pick_list([(other, 1, 83000)])
		self.assertEqual(self._line(self._pricing(other_pl), other)["price_mode"], ORDER)

	# -- 17/18. general sobre varias líneas + especial individual ----------------------

	def test_17_general_discount_on_several_lines(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 2, 50000)])
		self._apply(pl_name, "DISCOUNT_10")
		rows = self._rows(pl_name)
		self.assertEqual(rows[a.name].fg_invoice_rate, 90000)
		self.assertEqual(rows[b.name].fg_invoice_rate, 45000)

	def test_18_individual_special_after_general(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000)])
		self._apply(pl_name, "DISCOUNT_10")
		result = self._set_price(pl_name, b, 42000)
		self.assertEqual(self._line(result["pricing"], a)["price_mode"], "Descuento 10%")
		self.assertEqual(self._line(result["pricing"], a)["final_rate"], 90000)
		self.assertEqual(self._line(result["pricing"], b)["price_mode"], SPECIAL)
		self.assertEqual(self._line(result["pricing"], b)["final_rate"], 42000)

	# -- 19/37/38. reemplazo de especiales ----------------------------------------------

	def test_19_37_general_change_without_flag_modifies_nothing(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000)])
		self._apply(pl_name, "DISCOUNT_10")
		self._set_price(pl_name, b, 42000)
		before = {code: (row.fg_invoice_rate, row.fg_invoice_price_mode) for code, row in self._rows(pl_name).items()}
		versions_before = len(self._versions(pl_name))

		result = self._apply(pl_name, "DISCOUNT_20")
		self.assertTrue(result["requires_confirmation"])
		self.assertEqual(result["special_items"], [b.name])
		self.assertFalse(result["changed"])
		after = {code: (row.fg_invoice_rate, row.fg_invoice_price_mode) for code, row in self._rows(pl_name).items()}
		self.assertEqual(after, before)
		self.assertEqual(len(self._versions(pl_name)), versions_before)

	def test_38_confirmed_general_change_replaces_specials(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000)])
		self._set_price(pl_name, b, 42000)
		result = self._apply(pl_name, "DISCOUNT_20", replace_special=1)
		self.assertFalse(result["requires_confirmation"])
		rows = self._rows(pl_name)
		self.assertEqual(rows[a.name].fg_invoice_rate, 80000)
		self.assertEqual(rows[b.name].fg_invoice_rate, 40000)
		self.assertEqual(rows[b.name].fg_invoice_price_mode, "Descuento 20%")
		self.assertFalse(result["pricing"]["has_special"])

	# -- 20. totales -----------------------------------------------------------------------

	def test_20_totals_are_qty_times_final_price(self):
		a, b = self._item(45000), self._item(10000)
		_, pl_name = self._pick_list([(a, 2, 45000), (b, 3, 10000)])
		self._set_price(pl_name, a, 41500)
		result = self._pricing(pl_name)
		self.assertEqual(self._line(result, a)["amount"], 83000)
		self.assertEqual(self._line(result, b)["amount"], 30000)
		self.assertEqual(result["totals"]["total"], 113000)
		self.assertEqual(result["totals"]["public_subtotal"], 120000)
		self.assertEqual(result["totals"]["adjustment"], -7000)

	# -- 21-23. PDF --------------------------------------------------------------------------

	def test_21_22_pdf_reflects_final_price_and_total(self):
		a, b = self._item(45000), self._item(10000)
		_, pl_name = self._pick_list([(a, 2, 45000), (b, 1, 10000)])
		self._set_price(pl_name, a, 41500)
		self._invoice(pl_name)
		html = self._pdf_html(pl_name)
		self.assertIn("$ 41.500", html)
		self.assertIn("$ 83.000", html)
		self.assertIn("$ 93.000", html)  # 83.000 + 10.000
		self.assertNotIn("$ 45.000", html)
		self.assertNotIn("$ 100.000", html)

	def test_23_regenerating_pdf_does_not_change_prices(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		self._apply(pl_name, "DISCOUNT_10")
		self._invoice(pl_name)
		before = self._rows(pl_name)[item.name]
		versions_before = len(self._versions(pl_name))
		first, second = self._pdf_html(pl_name), self._pdf_html(pl_name)
		self.assertIn("$ 40.500", first)
		self.assertIn("$ 40.500", second)
		after = self._rows(pl_name)[item.name]
		self.assertEqual(
			(after.fg_invoice_rate, after.fg_invoice_price_mode, after.fg_invoice_public_rate),
			(before.fg_invoice_rate, before.fg_invoice_price_mode, before.fg_invoice_public_rate),
		)
		self.assertEqual(len(self._versions(pl_name)), versions_before)

	# -- 24/25. sin efectos en Cotizaciones / Inventario ----------------------------------

	def test_24_cotizaciones_reuses_the_same_rules_unchanged(self):
		self.assertIs(cotizaciones.PRICE_MODE_DISCOUNTS, pricing.PRICE_MODE_DISCOUNTS)
		self.assertIs(cotizaciones._PRICE_MODE_LABELS, pricing.PRICE_MODE_LABELS)
		self.assertEqual(
			pricing.PRICE_MODE_DISCOUNTS,
			{"FULL": 0, "DISCOUNT_10": 10, "DISCOUNT_15": 15, "DISCOUNT_20": 20, "DISCOUNT_25": 25},
		)
		row = frappe.new_doc("Quotation Item")
		for mode, expected in (("FULL", 100000), ("DISCOUNT_10", 90000), ("DISCOUNT_25", 75000)):
			self.assertEqual(cotizaciones._expected_price_mode_rate(100000, mode, row), expected)
		self.assertNotIn("fg_invoice_", inspect.getsource(cotizaciones))

	def test_25_inventario_untouched(self):
		source = inspect.getsource(inventario)
		self.assertNotIn("fg_invoice_", source)
		self.assertNotIn("fabergray_erp.pricing", source)

	# -- 26. idempotencia -------------------------------------------------------------------

	def test_26_reapplying_same_mode_is_idempotent(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		self.assertTrue(self._apply(pl_name, "DISCOUNT_15")["changed"])
		versions = len(self._versions(pl_name))
		modified = frappe.db.get_value("Pick List", pl_name, "modified")
		self.assertFalse(self._apply(pl_name, "DISCOUNT_15")["changed"])
		self.assertEqual(len(self._versions(pl_name)), versions)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "modified"), modified)

	# -- 27/28. especial > público y sin público ------------------------------------------

	def test_27_special_price_above_public_allowed(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		result = self._set_price(pl_name, item, 48000)
		line = self._line(result["pricing"], item)
		self.assertEqual(line["final_rate"], 48000)
		self.assertEqual(line["price_mode"], SPECIAL)
		self.assertEqual(result["pricing"]["totals"]["adjustment"], 3000)  # ajuste positivo, no "descuento"

	def test_28_no_public_price_plus_manual_price_can_be_invoiced(self):
		item = self._item(None)
		_, pl_name = self._pick_list([(item, 1, 30000)])
		self._set_price(pl_name, item, 28000)
		self._invoice(pl_name)
		row = self._rows(pl_name)[item.name]
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoicing_status"), "Facturado")
		self.assertEqual(row.fg_invoice_rate, 28000)
		self.assertEqual(row.fg_invoice_price_mode, SPECIAL)
		self.assertEqual(row.fg_invoice_public_rate, 0)
		self.assertIn("$ 28.000", self._pdf_html(pl_name))

	# -- 29. descuento general atómico ------------------------------------------------------

	def test_29_general_discount_with_missing_public_price_is_atomic(self):
		priced, unpriced = self._item(100000), self._item(None)
		_, pl_name = self._pick_list([(priced, 1, 100000), (unpriced, 1, 30000)])
		with self.assertRaises(facturacion.MissingPublicPriceError) as ctx:
			self._apply(pl_name, "DISCOUNT_10")
		self.assertIn(unpriced.name, str(ctx.exception))
		for row in self._rows(pl_name).values():
			self.assertEqual(row.fg_invoice_rate, 0)
			self.assertFalse(row.fg_invoice_price_mode)

	# -- 30/31. congelamiento ---------------------------------------------------------------

	def test_30_31_frozen_price_survives_item_price_and_order_changes(self):
		item = self._item(100000)
		so, pl_name = self._pick_list([(item, 1, 100000)])
		self._apply(pl_name, "DISCOUNT_10")
		self._invoice(pl_name)

		price_name = frappe.db.get_value("Item Price", {"item_code": item.name, "price_list": PRICE_LIST})
		frappe.db.set_value("Item Price", price_name, "price_list_rate", 200000)  # test-only: cambio posterior
		so_item = so.items[0].name
		frappe.db.set_value("Sales Order Item", so_item, "rate", 150000)  # test-only: cambio posterior

		line = self._line(self._pricing(pl_name), item)
		self.assertTrue(self._pricing(pl_name)["locked"])
		self.assertEqual(line["final_rate"], 90000)
		self.assertEqual(line["public_rate"], 100000)
		html = self._pdf_html(pl_name)
		self.assertIn("$ 90.000", html)
		self.assertNotIn("$ 150.000", html)
		self.assertNotIn("$ 200.000", html)

	def test_30b_freeze_order_price_when_never_modified(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 85000)])
		self._invoice(pl_name)
		row = self._rows(pl_name)[item.name]
		self.assertEqual(row.fg_invoice_rate, 85000)
		self.assertEqual(row.fg_invoice_public_rate, 100000)
		self.assertEqual(row.fg_invoice_price_mode, "Descuento 15%")

	# -- 32. después de Facturado ------------------------------------------------------------

	def test_32_changes_after_invoiced_rejected(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		self._invoice(pl_name)
		with self.assertRaises(facturacion.InvoicePricingLockedError):
			self._apply(pl_name, "DISCOUNT_10")
		with self.assertRaises(facturacion.InvoicePricingLockedError):
			self._set_price(pl_name, item, 50000)
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 100000)

	# -- 33. manipulación directa ------------------------------------------------------------

	def test_33_direct_after_submit_write_without_flag_rejected(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		for user in (self.bodega_user, self.facturacion_user):
			with fx.as_user(user):
				pl = frappe.get_doc("Pick List", pl_name)
				pl.locations[0].fg_invoice_rate = 1
				with self.assertRaises(frappe.PermissionError, msg=user):
					pl.save()
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				set_value("Pick List Item", self._rows(pl_name)[item.name].name, "fg_invoice_rate", 1)
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_rate, 0)

	def test_33b_draft_pick_list_cannot_carry_invoice_prices(self):
		item = self._item(100000)
		so = self._sales_order([(item, 1, 100000)])
		from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

		draft = create_pick_list(so.name)
		draft.parent_warehouse = self.wh.name
		draft.locations[0].fg_invoice_rate = 1
		with self.assertRaises(frappe.PermissionError):
			draft.insert()

	# -- 34. Version -------------------------------------------------------------------------

	def test_34_version_records_manual_change(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		self._apply(pl_name, "FULL")  # persiste 45.000
		# Frappe no guarda Version dentro de tests (ignore_version =
		# frappe.in_test); en producción sí. Se simula fuera de test SOLO
		# durante este cambio para verificar la trazabilidad real.
		with patch.object(frappe, "in_test", False):
			self._set_price(pl_name, item, 41500)
		versions = self._versions(pl_name)
		for row in versions:
			self.world.track_existing("Version", row.name)
		version = versions[0]
		self.assertEqual(version.owner, self.facturacion_user)
		self.assertTrue(version.creation)
		data = json.loads(version.data)
		changes = [change for entry in data.get("row_changed", []) for change in entry[3]]
		rate_change = next(change for change in changes if change[0] == "fg_invoice_rate")
		# Version guarda los Currency ya formateados (p. ej. "$ 45.000,00").
		self.assertIn("45.000", str(rate_change[1]))
		self.assertIn("41.500", str(rate_change[2]))
		mode_change = next(change for change in changes if change[0] == "fg_invoice_price_mode")
		self.assertEqual(mode_change[1:], ["Precio completo", SPECIAL])

	# -- 35. contrato de get_invoicing_detail -----------------------------------------------

	def test_35_detail_contract_still_has_no_money(self):
		item = self._item(45000)
		_, pl_name = self._pick_list([(item, 1, 45000)])
		self._set_price(pl_name, item, 41500)
		detail = self._as_fact(facturacion.get_invoicing_detail, pl_name)
		self.assertNotIn("grand_total", detail)
		for line in detail["items"]:
			for key in ("rate", "amount", "fg_invoice_rate", "public_rate", "final_rate"):
				self.assertNotIn(key, line)

	# -- 39. sin estados parciales ------------------------------------------------------------

	def test_39_failure_leaves_no_partial_changes(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000)])
		with patch.object(facturacion, "_save_invoice_pricing", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				self._apply(pl_name, "DISCOUNT_10")
		for row in self._rows(pl_name).values():
			self.assertEqual(row.fg_invoice_rate, 0)

		# mark_as_invoiced: una línea sin precio válido bloquea todo, sin
		# congelar ninguna y sin cambiar el estado.
		priced, unpriced = self._item(100000), self._item(None)
		_, other = self._pick_list([(priced, 1, 100000), (unpriced, 1, 30000)])
		frappe.db.set_value(
			"Sales Order Item",
			frappe.get_doc("Pick List", other).locations[1].sales_order_item,
			"rate",
			0,
		)  # test-only: línea sin precio del pedido
		with fx.as_user(self.facturacion_user):
			for detail_item in facturacion.get_invoicing_detail(other)["items"]:
				facturacion.set_invoicing_item_checked(other, detail_item["row_name"], 1)
			with self.assertRaises(facturacion.InvoiceLinePriceMissingError):
				facturacion.mark_as_invoiced(other)
		self.assertNotEqual(frappe.db.get_value("Pick List", other, "fg_invoicing_status"), "Facturado")
		for row in self._rows(other).values():
			self.assertEqual(row.fg_invoice_rate, 0)

	# -- impuestos / descuento global ---------------------------------------------------------

	def test_40_order_discount_keeps_safe_pdf_and_blocks_price_changes(self):
		"""Pedido con descuento global: sin cambios de precio el PDF conserva
		el comportamiento seguro previo; cualquier cambio de precio se rechaza
		ANTES de escribir (0 líneas modificadas)."""
		unchanged = self._item(100000)
		_, pl_ok = self._pick_list([(unchanged, 1, 100000)], discount_amount=1000)
		self._invoice(pl_ok)
		self.assertIn("$ 99.000", self._pdf_html(pl_ok))

		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)], discount_amount=1000)
		self.assertTrue(self._pricing(pl_name)["order_adjustments"])
		with self.assertRaises(facturacion.OrderAdjustmentsPricingError):
			self._apply(pl_name, "DISCOUNT_10")
		with self.assertRaises(facturacion.OrderAdjustmentsPricingError):
			self._set_price(pl_name, item, 90000)
		row = self._rows(pl_name)[item.name]
		self.assertEqual((row.fg_invoice_rate, row.fg_invoice_price_mode or None), (0, None))

	def test_40b_pdf_still_blocks_adjusted_prices_with_order_discount(self):
		"""Defensa en profundidad: aunque un precio congelado distinto al del
		pedido llegara a existir (dato histórico/manipulado, escrito aquí
		con la bandera interna solo para el test), el PDF no prorratea."""
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)], discount_amount=1000)
		self._invoice(pl_name)
		pl = frappe.get_doc("Pick List", pl_name)
		pl.locations[0].fg_invoice_rate = 90000
		with facturacion._invoice_pricing_write():
			pl.save()
		with self.assertRaises(facturacion.InvoicePdfNotEligibleError):
			self._pdf_html(pl_name)

	# -- public_rate <= 0 == SIN PRECIO PÚBLICO ----------------------------------------------

	def test_41_zero_public_price_is_shown_as_missing(self):
		item = self._item(0)
		_, pl_name = self._pick_list([(item, 1, 30000)])
		result = self._pricing(pl_name)
		line = self._line(result, item)
		self.assertIsNone(line["public_rate"])  # la UI lo pinta "SIN PRECIO"
		self.assertIn(item.name, result["missing_public_items"])
		self.assertEqual(line["price_mode"], ORDER)  # nunca "Precio completo" contra base 0

	def test_42_zero_public_price_blocks_every_general_mode(self):
		item = self._item(0)
		_, pl_name = self._pick_list([(item, 1, 30000)])
		for mode in ("FULL", "DISCOUNT_10", "DISCOUNT_15", "DISCOUNT_20", "DISCOUNT_25"):
			with self.assertRaises(facturacion.MissingPublicPriceError, msg=mode):
				self._apply(pl_name, mode)
		row = self._rows(pl_name)[item.name]
		self.assertEqual((row.fg_invoice_rate, row.fg_invoice_price_mode or None), (0, None))

	def test_43_zero_public_price_allows_manual_special_price(self):
		item = self._item(0)
		_, pl_name = self._pick_list([(item, 1, 30000)])
		result = self._set_price(pl_name, item, 27500)
		line = self._line(result["pricing"], item)
		self.assertEqual(line["final_rate"], 27500)
		self.assertEqual(line["price_mode"], SPECIAL)
		self.assertIsNone(line["public_rate"])
		self.assertEqual(self._rows(pl_name)[item.name].fg_invoice_public_rate, 0)

	def test_44_one_zero_public_line_makes_public_subtotal_na(self):
		priced, zero = self._item(100000), self._item(0)
		_, pl_name = self._pick_list([(priced, 1, 100000), (zero, 1, 30000)])
		totals = self._pricing(pl_name)["totals"]
		self.assertIsNone(totals["public_subtotal"])
		self.assertIsNone(totals["adjustment"])
		self.assertEqual(totals["total"], 130000)

	def test_45_percentage_is_never_computed_on_a_zero_or_negative_base(self):
		calls = []
		real = facturacion.discounted_rate

		def spy(base, *args):
			calls.append(base)
			return real(base, *args)

		priced, zero = self._item(100000), self._item(0)
		_, pl_name = self._pick_list([(priced, 1, 90000), (zero, 1, 30000)])
		with patch.object(facturacion, "discounted_rate", side_effect=spy):
			self._pricing(pl_name)
			with self.assertRaises(facturacion.MissingPublicPriceError):
				self._apply(pl_name, "DISCOUNT_10")
			# Facturado con precio público congelado 0 y uno negativo forzado
			self._set_price(pl_name, zero, 28000)
			self._invoice(pl_name)
			pl = frappe.get_doc("Pick List", pl_name)
			for row in pl.locations:
				if row.item_code == zero.name:
					row.fg_invoice_public_rate = -5  # test-only: dato inválido
			with facturacion._invoice_pricing_write():
				pl.save()
			result = self._pricing(pl_name)
		self.assertTrue(calls)  # sí se detectó el modo de la línea con precio público real
		self.assertTrue(all(base > 0 for base in calls), calls)
		line = self._line(result, zero)
		self.assertIsNone(line["public_rate"])
		self.assertIsNone(result["totals"]["public_subtotal"])

	def test_46_negative_or_zero_order_rate_is_never_a_valid_final_price(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		row = self._rows(pl_name)[item.name]
		original = frappe.db.get_value("Sales Order Item", row.sales_order_item, "rate")
		frappe.db.set_value("Sales Order Item", row.sales_order_item, "rate", -10)  # test-only
		try:
			line = self._line(self._pricing(pl_name), item)
			self.assertIsNone(line["final_rate"])
			self.assertIn(item.name, self._pricing(pl_name)["missing_price_items"])
		finally:
			# restaurado siempre: un rate negativo impide cancelar el pedido en la limpieza
			frappe.db.set_value("Sales Order Item", row.sales_order_item, "rate", original)

	# -- atomicidad ------------------------------------------------------------------------------

	def test_47_apply_validates_every_line_before_writing(self):
		"""Una línea con precio público 0 entre varias válidas: 0 líneas
		modificadas y ninguna Version/escritura."""
		a, b, zero = self._item(100000), self._item(50000), self._item(0)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000), (zero, 1, 30000)])
		modified = frappe.db.get_value("Pick List", pl_name, "modified")
		with patch.object(facturacion, "_save_invoice_pricing") as save:
			with self.assertRaises(facturacion.MissingPublicPriceError):
				self._apply(pl_name, "DISCOUNT_10")
			save.assert_not_called()
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "modified"), modified)
		for row in self._rows(pl_name).values():
			self.assertEqual(row.fg_invoice_rate, 0)

	def test_48_mark_as_invoiced_failure_freezes_nothing(self):
		a, b = self._item(100000), self._item(50000)
		_, pl_name = self._pick_list([(a, 1, 100000), (b, 1, 50000)])
		self._apply(pl_name, "DISCOUNT_10")
		frappe.db.set_value("Pick List Item", self._rows(pl_name)[b.name].name, "fg_invoice_rate", 0)  # test-only
		frappe.db.set_value("Sales Order Item", self._rows(pl_name)[b.name].sales_order_item, "rate", 0)  # test-only
		before = {code: (row.fg_invoice_rate, row.fg_invoice_price_mode) for code, row in self._rows(pl_name).items()}
		with fx.as_user(self.facturacion_user):
			for detail_item in facturacion.get_invoicing_detail(pl_name)["items"]:
				facturacion.set_invoicing_item_checked(pl_name, detail_item["row_name"], 1)
			with self.assertRaises(facturacion.InvoiceLinePriceMissingError):
				facturacion.mark_as_invoiced(pl_name)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "fg_invoicing_status"), "Pendiente")
		after = {code: (row.fg_invoice_rate, row.fg_invoice_price_mode) for code, row in self._rows(pl_name).items()}
		self.assertEqual(after, before)

	# -- PDF histórico ----------------------------------------------------------------------------

	def test_49_pdf_uses_frozen_price_without_consulting_item_price(self):
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 100000)])
		self._apply(pl_name, "DISCOUNT_20")
		self._invoice(pl_name)
		with patch.object(facturacion, "reference_selling_rates", side_effect=AssertionError("Item Price")):
			html = self._pdf_html(pl_name)
		self.assertIn("$ 80.000", html)
		self.assertNotIn("$ 100.000", html)

	def test_50_pre_25_25_invoiced_pick_list_falls_back_to_order_rate(self):
		"""Compatibilidad: un Pick List facturado ANTES de 25.25 no tiene
		fg_invoice_rate (0) -> el PDF usa Sales Order.rate, como siempre."""
		item = self._item(100000)
		_, pl_name = self._pick_list([(item, 1, 95000)])
		self._invoice(pl_name)
		frappe.db.set_value("Pick List Item", self._rows(pl_name)[item.name].name, "fg_invoice_rate", 0)  # simula histórico
		html = self._pdf_html(pl_name)
		self.assertIn("$ 95.000", html)


class TestFacturacionInvoicePricingUiContract(IntegrationTestCase):
	"""facturacion.js leído como texto (no hay runner JS en esta app)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		path = os.path.join(
			frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
		)
		with open(path, encoding="utf-8") as f:
			cls.source = f.read()

	def test_ui_uses_the_pricing_endpoints_and_confirms_specials(self):
		for method in ("get_invoicing_pricing", "apply_invoice_price_mode", "set_invoice_line_price"):
			self.assertIn(f'"{method}"', self.source)
		self.assertIn(
			"Este cambio reemplazará los precios especiales establecidos manualmente. ¿Deseas continuar?",
			self.source,
		)
		self.assertIn("replace_special: replace_special ? 1 : 0", self.source)
		self.assertIn("PRECIO ESPECIAL", self.source)
		self.assertIn("SIN PRECIO", self.source)
