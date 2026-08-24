# -*- coding: utf-8 -*-
"""Commit 20.2 -- Fase 5 (Cotizaciones): api/cotizaciones.py's four
read-only endpoints (get_item_info, get_quotation_summary,
get_my_quotations, get_quotation_detail). No create_and_submit_quotation()
yet (Commit 20.3) -- Quotation fixtures needed here are built as raw
`frappe.get_doc({"doctype": "Quotation", ...})` documents, same pattern
`test_cotizaciones_permissions.py` (Commit 20.1) already established.

Central theme, tested from several angles, same policy as
`test_ventas_api.py`: Vendedora never sees a price/discount/tax/total on
a Quotation either -- every response here is checked against a strict
key allowlist and against `_ECONOMIC_KEYS`.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_ECONOMIC_KEYS = {
	"rate",
	"price_list_rate",
	"amount",
	"net_rate",
	"net_amount",
	"base_rate",
	"base_amount",
	"total",
	"grand_total",
	"net_total",
	"base_grand_total",
	"base_net_total",
	"discount_percentage",
	"discount_amount",
	"taxes",
	"margin_rate_or_amount",
}


class TestCotizacionesApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.item = cls.world.item("FG20-2-ITEM")
		cls.customer = cls.world.customer("FG20-2 Customer")

		cls.vendedora_a = cls.world.user("fg20-2-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg20-2-vendedora-b@example.com", ["Vendedora"])

	def _raw_quotation(self, customer, item, qty=1, terms=None, submit=False):
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": customer,
				"company": fx.COMPANY,
				"items": [{"item_code": item, "qty": qty}],
			}
		)
		if terms:
			qtn.terms = terms
		qtn.insert()
		self.world.track_existing("Quotation", qtn.name)
		if submit:
			qtn.submit()
		return qtn

	# -- get_item_info -----------------------------------------------------

	def test_get_item_info_has_a_strict_response_allowlist(self):
		with fx.as_user(self.vendedora_a):
			info = cotizaciones.get_item_info(self.item.name)

		self.assertEqual(set(info.keys()), {"item_code", "item_name", "description", "stock_uom", "image"})
		self.assertEqual(info["item_code"], self.item.name)
		self.assertFalse(_ECONOMIC_KEYS & set(info.keys()))
		self.assertNotIn("qty_disponible", info)  # inventory is out of scope entirely for Cotizaciones

	def test_get_item_info_denies_unreadable_item(self):
		jefe_user = self.world.user("fg20-2-jefe@example.com", ["Jefe de Bodega"])
		with fx.as_user(jefe_user):  # confirmed zero Item permission (only Vendedora/Bodega have it)
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_item_info(self.item.name)

	# -- get_quotation_summary ----------------------------------------------

	def test_get_quotation_summary_buckets_correctly(self):
		with fx.as_user(self.vendedora_a):
			qtn_today = self._raw_quotation(self.customer.name, self.item.name, submit=True)
			qtn_today.reload()
			self.assertEqual(qtn_today.status, "Open")  # submitted, not yet ordered/lost

			qtn_expired = self._raw_quotation(self.customer.name, self.item.name, submit=True)
			# Mirrors what the native daily job (set_expired_status()) does --
			# a direct UPDATE, not doc.save() -- since validate_valid_till()
			# would otherwise reject a valid_till before transaction_date.
			frappe.db.set_value(
				"Quotation", qtn_expired.name, {"valid_till": add_days(nowdate(), -1), "status": "Expired"}
			)

			summary = cotizaciones.get_quotation_summary()

		self.assertEqual(set(summary.keys()), {"cotizaciones_hoy", "pendientes", "aprobadas", "vencidas"})
		self.assertGreaterEqual(summary["cotizaciones_hoy"], 2)
		self.assertGreaterEqual(summary["pendientes"], 1)
		self.assertEqual(summary["aprobadas"], 0)  # no conversion phase yet -- always 0 until built
		self.assertGreaterEqual(summary["vencidas"], 1)

	def test_get_quotation_summary_respects_if_owner(self):
		with fx.as_user(self.vendedora_a):
			self._raw_quotation(self.customer.name, self.item.name, submit=True)

		with fx.as_user(self.vendedora_b):
			summary_b = cotizaciones.get_quotation_summary()
		self.assertEqual(summary_b["cotizaciones_hoy"], 0)
		self.assertEqual(summary_b["pendientes"], 0)

	# -- get_my_quotations ----------------------------------------------------

	def test_vendedora_only_gets_her_own_quotations(self):
		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(self.customer.name, self.item.name, terms="Entrega en 5 días")

		with fx.as_user(self.vendedora_b):
			qtn_b = self._raw_quotation(self.customer.name, self.item.name)

		with fx.as_user(self.vendedora_a):
			mine = cotizaciones.get_my_quotations()
		names = [q["name"] for q in mine]
		self.assertIn(qtn_a.name, names)
		self.assertNotIn(qtn_b.name, names)

	def test_get_my_quotations_response_never_contains_economic_data(self):
		with fx.as_user(self.vendedora_a):
			self._raw_quotation(self.customer.name, self.item.name, qty=4, terms="Condiciones de pago: contado")
			mine = cotizaciones.get_my_quotations()

		allowed = {
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"valid_till",
			"status",
			"item_count",
			"total_qty",
			"observations",
		}
		for row in mine:
			self.assertTrue(set(row.keys()).issubset(allowed), row.keys())
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

		match = next(q for q in mine if q["total_qty"] == 4)
		self.assertEqual(match["observations"], "Condiciones de pago: contado")
		self.assertEqual(match["item_count"], 1)
		self.assertEqual(match["customer"], self.customer.name)

	# -- get_quotation_detail --------------------------------------------------

	def test_get_quotation_detail_returns_items_without_economic_fields(self):
		with fx.as_user(self.vendedora_a):
			qtn = self._raw_quotation(self.customer.name, self.item.name, qty=7, terms="Válida por 15 días")
			detail = cotizaciones.get_quotation_detail(qtn.name)

		self.assertEqual(detail["name"], qtn.name)
		self.assertEqual(detail["observations"], "Válida por 15 días")
		self.assertEqual(len(detail["items"]), 1)
		self.assertEqual(detail["items"][0]["item_code"], self.item.name)
		self.assertEqual(detail["items"][0]["qty"], 7)

		allowed_top = {
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"valid_till",
			"status",
			"item_count",
			"total_qty",
			"observations",
			"items",
		}
		allowed_item = {"item_code", "item_name", "qty", "stock_uom"}
		self.assertTrue(set(detail.keys()).issubset(allowed_top))
		self.assertFalse(_ECONOMIC_KEYS & set(detail.keys()))
		for row in detail["items"]:
			self.assertTrue(set(row.keys()).issubset(allowed_item))
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

	def test_get_quotation_detail_enforces_if_owner(self):
		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(self.customer.name, self.item.name)

		with fx.as_user(self.vendedora_b):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_quotation_detail(qtn_a.name)
