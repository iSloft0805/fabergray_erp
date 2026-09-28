# -*- coding: utf-8 -*-
"""Ajuste UI -- tarjetas "Requieren atención" (dashboard Jefe de Bodega):
api.jefe_bodega.get_open_shortage_reports() now returns the customer of the
linked Sales Order (customer_name, falling back to customer) and the EXACT
report date/time (reported_on -> "DD/MM/YYYY HH:mm", formatted server-side),
and the card renders both, escaped, instead of a relative "hace X minutos".

Names carry a random suffix: an interrupted setUpClass never leaves records
that collide with the next run."""

import os
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime

from fabergray_erp.api import jefe_bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "jefe_de_bodega")

LONG_NAME = "DISTRIBUIDORA NACIONAL DE PRODUCTOS DE ASEO Y LIMPIEZA INSTITUCIONAL DEL ORIENTE SAS"
XSS_NAME = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


class TestJefeBodegaAttentionCards(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FGATT {sfx} WH")
		cls.item = cls.world.item(f"FGATT-{sfx}-ITEM")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		cls.jefe = cls.world.user(f"fgatt-{sfx}-jefe@example.com", ["Jefe de Bodega"])

		def order(customer_name):
			customer = cls.world.customer(f"FGATT {sfx} {frappe.generate_hash(length=4)}")
			so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 3, customer.name)
			if customer_name is not None:
				frappe.db.set_value("Sales Order", so.name, "customer_name", customer_name, update_modified=False)
			return customer, so

		cls.cust_named, cls.so_named = order("ASEO SERVICIOS SAS")
		cls.cust_fallback, cls.so_fallback = order("")  # no customer_name -> customer
		cls.cust_long, cls.so_long = order(LONG_NAME)
		cls.cust_xss, cls.so_xss = order(XSS_NAME)

		cls.r_named = cls._shortage(cls.so_named.name, "2026-09-28 14:32:05")
		cls.r_fallback = cls._shortage(cls.so_fallback.name, "2026-01-05 07:03:00")
		cls.r_long = cls._shortage(cls.so_long.name, "2026-09-27 09:15:00")
		cls.r_xss = cls._shortage(cls.so_xss.name, "2026-09-27 09:16:00")
		cls.r_no_order = cls._shortage(None, "2026-09-27 09:17:00")

	@classmethod
	def _shortage(cls, sales_order, reported_on):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": cls.item.name,
				"warehouse": cls.wh.name,
				"sales_order": sales_order,
				"qty_solicitada": 3,
				"qty_disponible": 0,
				"detected_by": "Bodega",
				"shortage_reason": "Compra pendiente",
			}
		).insert(ignore_permissions=True)
		cls.world.track_existing("Reporte de Faltante", doc.name)
		# reported_on deliberately far from creation/modified (which are "now").
		frappe.db.set_value("Reporte de Faltante", doc.name, "reported_on", reported_on, update_modified=False)
		return doc.name

	def _cards(self):
		with fx.as_user(self.jefe):
			return {r["name"]: r for r in jefe_bodega.get_open_shortage_reports()}

	# -- Cliente ---------------------------------------------------------------

	def test_customer_name_from_linked_sales_order(self):
		card = self._cards()[self.r_named]
		self.assertEqual(card["customer"], self.cust_named.name)
		self.assertEqual(card["customer_name"], "ASEO SERVICIOS SAS")

	def test_fallback_to_customer_when_no_customer_name(self):
		card = self._cards()[self.r_fallback]
		self.assertEqual(card["customer"], self.cust_fallback.name)
		self.assertIsNone(card["customer_name"])  # the UI then shows `customer`

	def test_report_without_sales_order_has_no_customer(self):
		card = self._cards()[self.r_no_order]
		self.assertIsNone(card["customer"])
		self.assertIsNone(card["customer_name"])  # the UI then shows "—"

	def test_long_customer_name_is_returned_whole(self):
		self.assertEqual(self._cards()[self.r_long]["customer_name"], LONG_NAME)

	def test_customers_resolved_in_one_batched_query(self):
		real_get_list = frappe.get_list
		sales_order_lists = []

		def spy(doctype, *args, **kwargs):
			if doctype == "Sales Order":
				sales_order_lists.append(kwargs.get("filters"))
			return real_get_list(doctype, *args, **kwargs)

		with patch.object(frappe, "get_list", side_effect=spy):
			cards = self._cards()
		self.assertGreaterEqual(len(cards), 5)
		self.assertEqual(len(sales_order_lists), 1, sales_order_lists)

	# -- Fecha del reporte -----------------------------------------------------

	def test_date_is_reported_on_formatted_dd_mm_yyyy_hh_mm(self):
		cards = self._cards()
		self.assertEqual(cards[self.r_named]["reported_on_display"], "28/09/2026 14:32")
		# minute (03) != month (01): a month/minute swap would be visible
		self.assertEqual(cards[self.r_fallback]["reported_on_display"], "05/01/2026 07:03")

	def test_date_never_comes_from_modified_or_creation(self):
		card = self._cards()[self.r_named]
		modified, creation = frappe.db.get_value("Reporte de Faltante", self.r_named, ["modified", "creation"])
		self.assertEqual(get_datetime(card["reported_on"]), get_datetime("2026-09-28 14:32:05"))
		for other in (modified, creation):
			self.assertNotEqual(card["reported_on_display"], get_datetime(other).strftime("%d/%m/%Y %H:%M"))

	def test_format_helper(self):
		self.assertEqual(jefe_bodega.format_report_datetime("2026-09-28 14:32:59.123456"), "28/09/2026 14:32")
		self.assertIsNone(jefe_bodega.format_report_datetime(None))

	# -- UI (contrato estático; no hay runner JS en esta app) ---------------------

	@classmethod
	def _card_js(cls):
		with open(os.path.join(_PAGE_DIR, "jefe_de_bodega.js"), encoding="utf-8") as f:
			js = f.read()
		start = js.index("\trender_shortage_card(r) {")
		return js[start : js.index("\n\t}\n", start)]

	def test_card_shows_escaped_customer_with_fallbacks(self):
		card = self._card_js()
		self.assertIn('const cliente = frappe.utils.escape_html(r.customer_name || r.customer || "—");', card)
		self.assertIn('${__("Cliente")}: ${cliente}', card)

	def test_card_shows_exact_report_date_not_relative_time(self):
		card = self._card_js()
		self.assertIn('frappe.utils.escape_html(r.reported_on_display || "—")', card)
		self.assertIn('${__("Fecha reporte")}: ${fecha_reporte}', card)
		self.assertNotIn("comment_when", card)
		self.assertNotIn("modified", card)
		self.assertNotIn("creation", card)

	def test_card_order(self):
		card = self._card_js()
		order = [
			"fg-shortage-card-title",
			"${pedido}",
			'__("Cliente")',
			'__("Bodega")',
			'__("Solicitado")',
			'__("Motivo")',
			'__("Reportado por")',
			'__("Fecha reporte")',
			'__(\n\t\t\t\t\t"VER FALTANTE"',
		]
		positions = [card.index(token) for token in order]
		self.assertEqual(positions, sorted(positions))

	def test_long_names_wrap_without_overflow(self):
		with open(os.path.join(_PAGE_DIR, "jefe_de_bodega.css"), encoding="utf-8") as f:
			css = f.read()
		meta = re.search(r"\.fg-shortage-card-meta \{([^}]*)\}", css).group(1)
		self.assertIn("overflow-wrap: anywhere", meta)
		self.assertNotIn("text-overflow", meta)
		self.assertNotIn("nowrap", meta)
		footer_span = re.search(r"\.fg-shortage-card-footer span \{([^}]*)\}", css).group(1)
		self.assertIn("flex-wrap: wrap", footer_span)
		self.assertIn("overflow-wrap: anywhere", footer_span)
