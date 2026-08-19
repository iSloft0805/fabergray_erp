# Copyright (c) 2026, Fabrigray SAS and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.tests import fixtures as fx

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestReportedeFaltante(IntegrationTestCase):
	"""Commit 8 -- Section 3: detected_by / shortage_reason contract, the
	server-side guarantees of Reporte de Faltante creation, and (Commit 9)
	the generic _insert_shortage_report() core plus the _create_shortage_report()
	Pick List adapter built on top of it. Documented in
	FULFILLMENT_ENGINE_CONTRACT.md and verified here.
	"""

	@classmethod
	def setUpClass(cls):
		# IntegrationTestCase auto-detects cls.doctype = "Reporte de Faltante"
		# from this file's location and would otherwise call
		# make_test_records(), which recursively walks every Link field
		# (item_code -> Item, ...) trying to import each linked doctype's own
		# test module to discover *its* dependencies. Importing
		# erpnext.stock.doctype.item.test_item pulls in the legacy
		# erpnext.tests.utils.ERPNextTestSuite, whose module-level setup code
		# is broken in this erpnext version (LinkValidationError: Could not
		# find Parent Department: All Departments) -- a pre-existing erpnext
		# test-utility bug unrelated to this app, not something to patch in
		# apps/erpnext. Pre-seeding the doctype into frappe.local.test_objects
		# short-circuits that call; every fixture this suite needs is built
		# explicitly via fabergray_erp.tests.fixtures instead.
		frappe.local.test_objects.setdefault("Reporte de Faltante", [])
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG8 Faltante")
		cls.item = cls.world.item("FG8-FALTANTE-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Faltante")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 10, cls.customer.name)
		cls.pl = cls.world.pick_list_for(so, cls.wh.name)

	def test_bodega_detection_requires_shortage_reason(self):
		with self.assertRaises(frappe.ValidationError):
			self.world.shortage_report(
				item_code=self.item.name,
				warehouse=self.wh.name,
				qty_solicitada=10,
				qty_disponible=4,
				detected_by="Bodega",
			)

		# same data, with a reason: must succeed
		doc = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=10,
			qty_disponible=4,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
		)
		self.assertTrue(doc.name)

	def test_fulfillment_engine_detection_does_not_require_shortage_reason(self):
		"""Per the Commit 7 contract: detected_by="Fulfillment Engine" is
		reserved for an upstream, non-physical detection and is not required
		to give a shortage_reason -- no code path sets this today, but the
		validation rule itself must already allow it."""
		doc = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=10,
			qty_disponible=6,
			detected_by="Fulfillment Engine",
		)
		self.assertTrue(doc.name)
		self.assertFalse(doc.shortage_reason)

	def test_qty_faltante_is_always_computed_server_side(self):
		doc = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=10,
			qty_disponible=4,
			qty_faltante=999,  # deliberately wrong, must be ignored
			detected_by="Bodega",
			shortage_reason="Otro",
		)
		self.assertEqual(doc.qty_faltante, 6)

	def test_reported_by_and_reported_on_are_autofilled(self):
		doc = self.world.shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=10,
			qty_disponible=4,
			detected_by="Bodega",
			shortage_reason="Otro",
		)
		self.assertEqual(doc.reported_by, frappe.session.user)
		self.assertIsNotNone(doc.reported_on)

	def test_create_shortage_report_derives_fields_from_pick_list_row(self):
		"""_create_shortage_report() (api/bodega.py) must never accept
		item/warehouse/order data as free-form input -- everything comes
		from the validated Pick List row itself."""
		row = self.pl.get("locations")[0]
		name = bodega._create_shortage_report(
			pick_list_doc=self.pl,
			row=row,
			qty_disponible=2,
			shortage_reason="Producto dañado",
			detected_by="Bodega",
		)
		self.world.track_existing("Reporte de Faltante", name)

		doc = frappe.get_doc("Reporte de Faltante", name)
		self.assertEqual(doc.item_code, row.item_code)
		self.assertEqual(doc.warehouse, row.warehouse)
		self.assertEqual(doc.pick_list, self.pl.name)
		self.assertEqual(doc.pick_list_item, row.name)
		self.assertEqual(doc.qty_solicitada, row.stock_qty)

	def test_insert_shortage_report_core_accepts_fulfillment_engine_without_pick_list(self):
		"""Commit 9: the generic core (_insert_shortage_report) must accept
		detected_by="Fulfillment Engine" with pick_list=None and
		pick_list_item=None -- proving a future engine can create a Reporte
		de Faltante before any Pick List exists, through the same single
		insert path _create_shortage_report() uses, with zero doctype or
		permission change. This does NOT implement automatic detection --
		it only demonstrates the core function's contract directly."""
		name = bodega._insert_shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=10,
			qty_disponible=4,
			detected_by="Fulfillment Engine",
			sales_order=None,
			material_request=None,
			pick_list=None,
			pick_list_item=None,
		)
		self.world.track_existing("Reporte de Faltante", name)

		doc = frappe.get_doc("Reporte de Faltante", name)
		self.assertEqual(doc.detected_by, "Fulfillment Engine")
		self.assertFalse(doc.pick_list)
		self.assertFalse(doc.pick_list_item)
		self.assertFalse(doc.shortage_reason)  # not required for this detected_by
		self.assertEqual(doc.qty_faltante, 6)  # still computed server-side

	def test_insert_shortage_report_core_links_sales_order_without_pick_list(self):
		"""The realistic future-engine shape: it knows the Sales Order (and
		would know the Sales Order Item) it's looking at, but there is no
		Pick List yet -- sales_order must round-trip while pick_list stays
		empty."""
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 5, self.customer.name)
		name = bodega._insert_shortage_report(
			item_code=self.item.name,
			warehouse=self.wh.name,
			qty_solicitada=5,
			qty_disponible=0,
			detected_by="Fulfillment Engine",
			sales_order=so.name,
			pick_list=None,
			pick_list_item=None,
		)
		self.world.track_existing("Reporte de Faltante", name)

		doc = frappe.get_doc("Reporte de Faltante", name)
		self.assertEqual(doc.sales_order, so.name)
		self.assertFalse(doc.pick_list)
		self.assertFalse(doc.pick_list_item)
