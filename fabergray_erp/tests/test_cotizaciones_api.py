# -*- coding: utf-8 -*-
"""Commits 20.2-20.3 -- Fase 5 (Cotizaciones): api/cotizaciones.py.
`TestCotizacionesApi` (Commit 20.2) covers the four read-only endpoints;
Quotation fixtures there are built as raw
`frappe.get_doc({"doctype": "Quotation", ...})` documents, same pattern
`test_cotizaciones_permissions.py` (Commit 20.1) already established.
`TestCreateAndSubmitQuotation` (Commit 20.3, below) covers
`create_and_submit_quotation()` itself.

Central theme, tested from several angles, same policy as
`test_ventas_api.py`: Vendedora never sees or sends a price/discount/tax/
total on a Quotation either -- every response here is checked against a
strict key allowlist and against `_ECONOMIC_KEYS`, and every economic
field name a line could carry is proven rejected, not silently dropped.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

_COTIZACIONES_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
)

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
		"""Disposable, purpose-built role with zero Item grants -- not
		"Jefe de Bodega" (Commit 22.4 gave that role its own, unrelated
		Item read=1 for api/inventario.py, so it can no longer serve as a
		"definitely no Item permission" example)."""
		role = frappe.get_doc({"doctype": "Role", "role_name": "FG20 No Item Permission Test Role", "desk_access": 1})
		role.insert()
		self.world.track_existing("Role", role.name)

		no_item_user = self.world.user("fg20-2-noitem@example.com", [role.name])
		with fx.as_user(no_item_user):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_item_info(self.item.name)

	# -- get_quotation_summary ----------------------------------------------

	def test_get_quotation_summary_buckets_correctly(self):
		"""Commit 25.16 -- BUGFIX: `pendientes`/`aprobadas` used to be
		derived from native `Quotation.status` here (`"Open"`/`"Ordered"`),
		which this test used to assert directly (`pendientes >= 1` right
		after a raw submit, `aprobadas == 0` always, "no conversion phase
		yet"). Both assertions encoded the exact bug fixed by this commit --
		a raw submitted-but-never-sent-to-Facturación Quotation is
		`fg_billing_review_status` Borrador, and must NOT be counted in
		either bucket any more (see `test_j_borrador_never_counts_in_either_
		kpi` below for the same assertion, `cotizaciones_hoy`/`vencidas`
		stay native-status-derived, unchanged, still asserted here)."""
		with fx.as_user(self.vendedora_a):
			before = cotizaciones.get_quotation_summary()

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

		self.assertEqual(
			set(summary.keys()),
			{
				"cotizaciones_hoy",
				"pendientes",
				"aprobadas",
				"vencidas",
				# Commit 25.13 -- fg_billing_review_status buckets, distinct
				# from the native-status ones above.
				"borradores_facturacion",
				"pendientes_facturacion",
				"aprobadas_facturacion",
				"devueltas_facturacion",
			},
		)
		self.assertEqual(summary["cotizaciones_hoy"], before["cotizaciones_hoy"] + 2)
		self.assertEqual(summary["vencidas"], before["vencidas"] + 1)
		# Neither qtn_today nor qtn_expired was ever sent to Facturación --
		# both stay Borrador, neither one may inflate pendientes/aprobadas.
		self.assertEqual(summary["pendientes"], before["pendientes"])
		self.assertEqual(summary["aprobadas"], before["aprobadas"])

	def test_get_quotation_summary_reflects_every_vendedoras_quotations(self):
		"""Commit 25.1: "el rol controla el área, no el owner" --
		get_quotation_summary() is company-wide, not per-owner (was
		assertEqual(summary_b[...], 0) pre-25.1: A's Quotation now counts
		in B's own summary too). Commit 25.16 -- `pendientes` is now
		fg_billing_review_status-derived (see that function's own
		docstring), so this test must actually send the Quotation to
		Facturación to move the needle, not merely submit it."""
		with fx.as_user(self.vendedora_b):
			before_b = cotizaciones.get_quotation_summary()

		with fx.as_user(self.vendedora_a):
			qtn = self._raw_quotation(self.customer.name, self.item.name, submit=True)
			cotizaciones.send_quotation_to_billing(qtn.name)

		with fx.as_user(self.vendedora_b):
			after_b = cotizaciones.get_quotation_summary()
		self.assertEqual(after_b["cotizaciones_hoy"], before_b["cotizaciones_hoy"] + 1)
		self.assertEqual(after_b["pendientes"], before_b["pendientes"] + 1)

	# -- get_my_quotations ----------------------------------------------------

	def test_vendedora_sees_every_company_quotation_including_others(self):
		"""Commit 25.1: get_my_quotations() shows every Quotation of this
		Company, regardless of who created it (was assertNotIn on the
		other Vendedora's quotation pre-25.1)."""
		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(self.customer.name, self.item.name, terms="Entrega en 5 días")

		with fx.as_user(self.vendedora_b):
			qtn_b = self._raw_quotation(self.customer.name, self.item.name)

		with fx.as_user(self.vendedora_a):
			mine = cotizaciones.get_my_quotations()
		names = [q["name"] for q in mine]
		self.assertIn(qtn_a.name, names)
		self.assertIn(qtn_b.name, names)

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
			# Commit 25.15 -- lifecycle flag only (never economic), lets the
			# UI tell a live Aprobada Quotation apart from a stale,
			# superseded one whose review status is frozen at "Aprobada".
			"docstatus",
			"item_count",
			"total_qty",
			"observations",
			# Commit 25.13
			"fg_billing_review_status",
			"fg_billing_review_note",
			# Commit 25.17 -- {"name", "status"} of the linked Sales Order
			# (or None), never an economic field.
			"sales_order",
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
			# Commit 25.13
			"fg_billing_review_status",
			"fg_billing_review_note",
			# Commit 25.17 -- same convention as get_my_quotations() above.
			"sales_order",
		}
		allowed_item = {"item_code", "item_name", "qty", "stock_uom"}
		self.assertTrue(set(detail.keys()).issubset(allowed_top))
		self.assertFalse(_ECONOMIC_KEYS & set(detail.keys()))
		for row in detail["items"]:
			self.assertTrue(set(row.keys()).issubset(allowed_item))
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

	def test_get_quotation_detail_shared_across_vendedoras(self):
		"""Commit 25.1: get_quotation_detail() is readable by any Vendedora
		of the same Company (was assertRaises(PermissionError) pre-25.1)."""
		with fx.as_user(self.vendedora_a):
			qtn_a = self._raw_quotation(self.customer.name, self.item.name)

		with fx.as_user(self.vendedora_b):
			detail = cotizaciones.get_quotation_detail(qtn_a.name)
		self.assertEqual(detail["name"], qtn_a.name)


# Every field create_and_submit_quotation() must reject if a line tries to
# send it -- the exact list from the approved Commit 20.3 brief.
_FORBIDDEN_ITEM_FIELDS = [
	"rate",
	"price_list_rate",
	"amount",
	"net_rate",
	"net_amount",
	"discount_percentage",
	"discount_amount",
	"margin_type",
	"margin_rate_or_amount",
	"currency",
	"conversion_rate",
	"taxes",
	"total",
	"grand_total",
]


class TestCreateAndSubmitQuotation(IntegrationTestCase):
	"""Commit 20.3 -- create_and_submit_quotation()."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG20-3 Main")
		cls.item = cls.world.item("FG20-3-ITEM", default_warehouse=cls.wh.name)
		cls.customer = cls.world.customer("FG20-3 Customer")

		cls.vendedora_a = cls.world.user("fg20-3-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg20-3-vendedora-b@example.com", ["Vendedora"])

	@staticmethod
	def _side_effect_counts():
		return {
			"Sales Order": frappe.db.count("Sales Order"),
			"Pick List": frappe.db.count("Pick List"),
			"Reporte de Faltante": frappe.db.count("Reporte de Faltante"),
			"Material Request": frappe.db.count("Material Request"),
		}

	# -- Camino feliz: crea y somete, owner correcto, response allowlist --------

	def test_vendedora_can_create_and_submit_her_own_quotation(self):
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 3}],
				terms="Válida por 15 días",
			)
		self.world.track_existing("Quotation", result["name"])

		self.assertEqual(
			set(result.keys()),
			{
				"name",
				"status",
				"customer",
				"customer_name",
				"transaction_date",
				"valid_till",
				"item_count",
				"total_qty",
			},
		)
		self.assertFalse(_ECONOMIC_KEYS & set(result.keys()))

		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(qtn.docstatus, 1)  # Quotation queda docstatus=1
		self.assertEqual(qtn.owner, self.vendedora_a)  # owner queda siendo la Vendedora
		self.assertEqual(qtn.party_name, self.customer.name)
		self.assertEqual(qtn.items[0].qty, 3)
		self.assertEqual(qtn.terms, "Válida por 15 días")

	def test_another_vendedora_can_read_it_afterward(self):
		"""Commit 25.1: shared visibility (was assertRaises(PermissionError)
		pre-25.1)."""
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])

		with fx.as_user(self.vendedora_b):
			detail = cotizaciones.get_quotation_detail(result["name"])
		self.assertEqual(detail["name"], result["name"])

	# -- Inventario: stock 0 nunca bloquea --------------------------------------

	def test_zero_stock_does_not_block_submission(self):
		self.world.stock_up(self.item.name, self.wh.name, 0)  # Bin.actual_qty explícitamente 0

		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 50}]
			)
		self.world.track_existing("Quotation", result["name"])

		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(qtn.docstatus, 1)
		self.assertEqual(qtn.items[0].qty, 50)

	# -- Pricing: ERPNext calcula internamente, nunca sale en la respuesta ------

	def test_erpnext_computes_a_real_price_but_it_never_appears_in_the_response(self):
		price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		if not frappe.db.exists("Item Price", {"item_code": self.item.name, "price_list": price_list}):
			ip = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": self.item.name,
					"price_list": price_list,
					"price_list_rate": 250,
				}
			)
			ip.insert()
			self.world.track_existing("Item Price", ip.name)

		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.world.track_existing("Quotation", result["name"])

		self.assertFalse(_ECONOMIC_KEYS & set(result.keys()))

		qtn = frappe.get_doc("Quotation", result["name"])
		self.assertEqual(qtn.items[0].rate, 250)  # ERPNext sí calculó un precio válido
		self.assertEqual(qtn.grand_total, 500)

	# -- Ningún efecto secundario: nada de SO/Pick List/Faltante/MR -------------

	def test_no_side_effects_created(self):
		before = self._side_effect_counts()

		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 5}]
			)
		self.world.track_existing("Quotation", result["name"])

		after = self._side_effect_counts()
		self.assertEqual(before, after, "create_and_submit_quotation() must never create a Sales Order, "
		"Pick List, Reporte de Faltante or Material Request")

	# -- Inyección de campos económicos: rechazo explícito, uno por uno ---------

	def test_injecting_forbidden_economic_fields_is_rejected(self):
		for field in _FORBIDDEN_ITEM_FIELDS:
			with self.subTest(field=field):
				with fx.as_user(self.vendedora_a):
					with self.assertRaises(frappe.ValidationError):
						cotizaciones.create_and_submit_quotation(
							customer=self.customer.name,
							items=[{"item_code": self.item.name, "qty": 1, field: 999}],
						)

	def test_injecting_an_unknown_field_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.create_and_submit_quotation(
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "some_unexpected_field": "x"}],
				)

	def test_missing_item_code_or_qty_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"item_code": self.item.name}])
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=[{"qty": 1}])

	def test_zero_or_negative_qty_is_rejected(self):
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.create_and_submit_quotation(
					customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 0}]
				)


class TestUpdateDraftQuotation(IntegrationTestCase):
	"""Commit 20.6 -- get_editable_quotation()/update_draft_quotation().
	Only docstatus==0 (Draft) may ever be edited -- same "Draft only"
	boundary Commit 18.5 already established for Sales Order. A real Draft
	Quotation is never produced by create_and_submit_quotation() itself
	(it always submits in the same call) -- exactly like
	test_ventas_api.py's own `_draft_so()`, `_draft_quotation()` below
	builds one directly, mirroring the real-world case this feature exists
	for: `.insert()` succeeded but `.submit()` never ran (a network drop,
	an exception between the two calls, etc.), leaving an orphaned Draft
	the Vendedora can fix or complete later.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.item = cls.world.item("FG20-6-ITEM")
		cls.other_item = cls.world.item("FG20-6-OTHER-ITEM")
		cls.customer = cls.world.customer("FG20-6 Customer")
		cls.other_customer = cls.world.customer("FG20-6 Other Customer")

		cls.vendedora_a = cls.world.user("fg20-6-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg20-6-vendedora-b@example.com", ["Vendedora"])

	def _draft_quotation(self, as_user, items=None, terms=None, valid_till=None):
		items = items or [{"item_code": self.item.name, "qty": 1}]
		doc_dict = {
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": self.customer.name,
			"company": fx.COMPANY,
			"items": items,
		}
		if terms:
			doc_dict["terms"] = terms
		if valid_till:
			doc_dict["valid_till"] = valid_till
		with fx.as_user(as_user):
			qtn = frappe.get_doc(doc_dict)
			qtn.insert()
		self.world.track_existing("Quotation", qtn.name)
		return qtn

	# -- Lectura para edición: get_editable_quotation() ------------------------

	def test_vendedora_can_read_her_own_draft_for_editing(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			editable = cotizaciones.get_editable_quotation(qtn.name)
		self.assertEqual(editable["name"], qtn.name)
		self.assertEqual(editable["status"], "Draft")

	def test_another_vendedora_can_read_and_edit_it_while_draft(self):
		"""Commit 25.1: a Draft Quotation is editable by any Vendedora of
		the same Company, not just its creator (was assertRaises
		(PermissionError) on both calls pre-25.1)."""
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_b):
			editable = cotizaciones.get_editable_quotation(qtn.name)
			self.assertEqual(editable["name"], qtn.name)
			result = cotizaciones.update_draft_quotation(
				name=qtn.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.assertEqual(result["name"], qtn.name)
		qtn.reload()
		self.assertEqual(qtn.items[0].qty, 2)

	# -- Ediciones permitidas ---------------------------------------------------

	def test_update_draft_quotation_updates_customer(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.update_draft_quotation(
				name=qtn.name,
				customer=self.other_customer.name,
				items=[{"item_code": self.item.name, "qty": 1}],
			)
		self.assertEqual(result["name"], qtn.name)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(qtn.party_name, self.other_customer.name)

	def test_update_draft_quotation_adds_a_product(self):
		qtn = self._draft_quotation(self.vendedora_a, items=[{"item_code": self.item.name, "qty": 2}])
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name,
				customer=self.customer.name,
				items=[
					{"item_code": self.item.name, "qty": 2},
					{"item_code": self.other_item.name, "qty": 5},
				],
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(len(qtn.items), 2)
		self.assertEqual({d.item_code for d in qtn.items}, {self.item.name, self.other_item.name})

	def test_update_draft_quotation_removes_a_product(self):
		qtn = self._draft_quotation(
			self.vendedora_a,
			items=[
				{"item_code": self.item.name, "qty": 2},
				{"item_code": self.other_item.name, "qty": 5},
			],
		)
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(len(qtn.items), 1)
		self.assertEqual(qtn.items[0].item_code, self.item.name)

	def test_update_draft_quotation_changes_qty(self):
		qtn = self._draft_quotation(self.vendedora_a, items=[{"item_code": self.item.name, "qty": 1}])
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 42}]
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(qtn.items[0].qty, 42)

	def test_update_draft_quotation_changes_valid_till(self):
		qtn = self._draft_quotation(self.vendedora_a)
		new_valid_till = add_days(nowdate(), 30)
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name,
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 1}],
				valid_till=new_valid_till,
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(str(qtn.valid_till), new_valid_till)

	def test_update_draft_quotation_changes_terms(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name,
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 1}],
				terms="Condiciones actualizadas",
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertEqual(qtn.terms, "Condiciones actualizadas")

	def test_update_draft_quotation_keeps_docstatus_zero(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			cotizaciones.update_draft_quotation(
				name=qtn.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		qtn.reload()
		self.assertEqual(qtn.docstatus, 0)
		self.assertNotEqual(qtn.status, "Cancelled")

	# -- Estados no editables: Submitted / Cancelled -----------------------------

	def test_editing_a_submitted_quotation_fails(self):
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.get_editable_quotation(result["name"])
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.update_draft_quotation(
					name=result["name"], customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
				)

	def test_editing_a_cancelled_quotation_fails(self):
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		qtn = frappe.get_doc("Quotation", result["name"])
		qtn.cancel()

		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.get_editable_quotation(result["name"])
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.update_draft_quotation(
					name=result["name"], customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
				)

	# -- Rechazo de campos económicos y desconocidos (mismo allowlist que 20.3) --

	def test_update_draft_quotation_rejects_forbidden_economic_fields(self):
		qtn = self._draft_quotation(self.vendedora_a)
		for field in _FORBIDDEN_ITEM_FIELDS:
			with self.subTest(field=field):
				with fx.as_user(self.vendedora_a):
					with self.assertRaises(frappe.ValidationError):
						cotizaciones.update_draft_quotation(
							name=qtn.name,
							customer=self.customer.name,
							items=[{"item_code": self.item.name, "qty": 1, field: 999}],
						)

	def test_update_draft_quotation_rejects_unknown_field(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.update_draft_quotation(
					name=qtn.name,
					customer=self.customer.name,
					items=[{"item_code": self.item.name, "qty": 1, "some_unexpected_field": "x"}],
				)

	# -- Ninguna respuesta trae datos económicos ---------------------------------

	def test_get_editable_quotation_response_never_contains_economic_data(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			editable = cotizaciones.get_editable_quotation(qtn.name)
		self.assertFalse(_ECONOMIC_KEYS & set(editable.keys()))
		for row in editable.get("items", []):
			self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

	def test_update_draft_quotation_response_never_contains_economic_data(self):
		qtn = self._draft_quotation(self.vendedora_a)
		with fx.as_user(self.vendedora_a):
			result = cotizaciones.update_draft_quotation(
				name=qtn.name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.assertEqual(set(result.keys()), {"name"})
		self.assertFalse(_ECONOMIC_KEYS & set(result.keys()))


# -- Commit 25.16 -- BUGFIX: Page Cotizaciones showed a card at once
# "APROBADA POR FACTURACIÓN" (billing_review_status_meta() strip, correct)
# AND "PENDIENTE" (the card's own top-right badge, wrong -- that badge, and
# get_quotation_summary()'s own "pendientes"/"aprobadas" KPI counts, used to
# read native Quotation.status, which never leaves "Open" in this app
# because Quotation -> Sales Order conversion has never been implemented).
# fg_billing_review_status is now the one source of truth for all three --
# the top badge, the two KPI numbers, and the two KPI click-through filters.
# Section 15's own lettered test list (A-X) is covered across this class
# (server-side classification/KPI/amendment/regression) and
# TestQuotationCardBadgeUiContract below (JS-source static checks, this app
# has no JS test runner -- same convention as test_cotizaciones_pdf.py's own
# TestQuotationPdfUiContract).
class TestQuotationClassificationKpi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.item = cls.world.item("FG2516-ITEM")
		cls.customer = cls.world.customer("FG2516 Customer")

		cls.vendedora = cls.world.user("fg2516-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2516-facturacion@example.com", ["Facturación"])

	def _priced_item(self, item_code, rate=200, price_list="Standard Selling"):
		"""Only needed for the amendment tests (K/L/M) -- apply_quotation_
		price_mode() resolves its reference rate off a real Item Price on
		the Quotation's own selling_price_list, same pattern
		test_cotizaciones_price_mode.py already establishes."""
		item = self.world.item(item_code)
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": price_list,
				"selling": 1,
				"price_list_rate": rate,
			}
		)
		price.insert()
		self.world.track_existing("Item Price", price.name)
		return item

	def _new_quotation(self, item_name=None):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item_name or self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		return result["name"]

	def _send_to_billing(self, name):
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(name)

	def _approve(self, name):
		with fx.as_user(self.facturacion):
			cotizaciones.approve_quotation_billing(name)

	def _return(self, name, reason="Ajustar precio"):
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(name, reason=reason)

	# -- E/F: Aprobada suma Aprobadas, nunca Pendientes ----------------------

	def test_e_f_aprobada_suma_aprobadas_nunca_pendientes(self):
		name = self._new_quotation()
		self._send_to_billing(name)
		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()
		self._approve(name)
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()

		self.assertEqual(after["aprobadas"], before["aprobadas"] + 1)
		self.assertEqual(after["pendientes"], before["pendientes"] - 1)

	# -- G/H: Pendiente suma Pendientes, nunca Aprobadas ---------------------

	def test_g_h_pendiente_suma_pendientes_nunca_aprobadas(self):
		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()
		name = self._new_quotation()
		self._send_to_billing(name)
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()

		self.assertEqual(after["pendientes"], before["pendientes"] + 1)
		self.assertEqual(after["aprobadas"], before["aprobadas"])

	# -- I: Devuelta no suma ninguno ------------------------------------------

	def test_i_devuelta_no_suma_ninguno(self):
		name = self._new_quotation()
		self._send_to_billing(name)
		with fx.as_user(self.vendedora):
			pending_summary = cotizaciones.get_quotation_summary()
		self._return(name)
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()

		self.assertEqual(after["pendientes"], pending_summary["pendientes"] - 1)
		self.assertEqual(after["aprobadas"], pending_summary["aprobadas"])

	# -- J: Borrador no suma ninguno -------------------------------------------

	def test_j_borrador_no_suma_ninguno(self):
		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()
		self._new_quotation()  # submitted, never sent to Facturación
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()

		self.assertEqual(after["pendientes"], before["pendientes"])
		self.assertEqual(after["aprobadas"], before["aprobadas"])

	# -- K/L/M: amendments -- only the vigente version ever counts ----------

	def test_k_l_m_amendment_kpi_and_classification(self):
		"""Mirrors the real COTIZACION-3 -> ... -> COTIZACION-3-5 chain: an
		Aprobada Quotation gets price-adjusted (apply_quotation_price_mode()
		cancels the original, docstatus=2, and creates a new, still-Pendiente
		amendment), then that amendment is approved. Section 6's own
		"amendment" concern -- the OLD, now-cancelled version must never
		double-count, only the current one may."""
		item = self._priced_item("FG2516-PRICED-ITEM")
		original_name = self._new_quotation(item_name=item.name)
		self._send_to_billing(original_name)

		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()

		with fx.as_user(self.facturacion):
			adjusted = cotizaciones.apply_quotation_price_mode(original_name, "DISCOUNT_10")
		new_name = adjusted["name"]
		self.world.track_existing("Quotation", new_name)
		self.assertNotEqual(new_name, original_name)
		self._approve(new_name)

		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()
			my_quotations = {q["name"]: q for q in cotizaciones.get_my_quotations(limit=500)}

		# K/L -- the old, now-cancelled original: docstatus=2, never counted.
		self.assertEqual(my_quotations[original_name]["docstatus"], 2)
		# M -- only the new, vigente, approved amendment counts: `before` was
		# captured while the ORIGINAL was still Pendiente (already in that
		# bucket) -- apply_quotation_price_mode() cancels it (leaves
		# Pendientes) and creates a new amendment that stays Pendiente too
		# (net Pendientes unchanged at that point), then approve_quotation_
		# billing() moves that new, vigente amendment out of Pendientes and
		# into Aprobadas -- net delta from `before`: aprobadas +1,
		# pendientes -1, never +2/-0 (which would mean the old, cancelled
		# original was still being counted somewhere).
		self.assertEqual(after["aprobadas"], before["aprobadas"] + 1)
		self.assertEqual(after["pendientes"], before["pendientes"] - 1)

	# -- N/O: filtro Aprobadas/Pendientes -- data correctness ----------------
	#
	# cotizaciones.js has no JS test runner (see module docstring) -- these
	# assert that get_my_quotations()'s own `docstatus`/
	# fg_billing_review_status fields are correct and sufficient for
	# quotation_matches_filter() (cotizaciones.js) to filter correctly,
	# replicating that exact, tiny predicate here in Python against the
	# real server response.

	@staticmethod
	def _matches_pendientes(q):
		return q["docstatus"] != 2 and (q.get("fg_billing_review_status") or "Borrador") == "Pendiente de Facturación"

	@staticmethod
	def _matches_aprobadas(q):
		return q["docstatus"] != 2 and (q.get("fg_billing_review_status") or "Borrador") == "Aprobada"

	def test_n_filtro_aprobadas_devuelve_unicamente_aprobadas_vigentes(self):
		item = self._priced_item("FG2516-FILTER-ITEM")
		original_name = self._new_quotation(item_name=item.name)
		self._send_to_billing(original_name)
		with fx.as_user(self.facturacion):
			adjusted = cotizaciones.apply_quotation_price_mode(original_name, "DISCOUNT_15")
		new_name = adjusted["name"]
		self.world.track_existing("Quotation", new_name)
		self._approve(new_name)

		with fx.as_user(self.vendedora):
			my_quotations = {q["name"]: q for q in cotizaciones.get_my_quotations(limit=500)}

		self.assertTrue(self._matches_aprobadas(my_quotations[new_name]))
		self.assertFalse(self._matches_aprobadas(my_quotations[original_name]))  # docstatus=2 -- excluded
		self.assertFalse(self._matches_pendientes(my_quotations[new_name]))

	def test_o_filtro_pendientes_devuelve_unicamente_pendientes_vigentes(self):
		approved_name = self._new_quotation()
		self._send_to_billing(approved_name)
		self._approve(approved_name)

		pending_name = self._new_quotation()
		self._send_to_billing(pending_name)

		with fx.as_user(self.vendedora):
			my_quotations = {q["name"]: q for q in cotizaciones.get_my_quotations(limit=500)}

		self.assertTrue(self._matches_pendientes(my_quotations[pending_name]))
		self.assertFalse(self._matches_pendientes(my_quotations[approved_name]))
		self.assertFalse(self._matches_aprobadas(my_quotations[pending_name]))

	# -- P/Q: real-shape scenarios --------------------------------------------

	def test_p_single_approved_quotation_like_cotizacion_4_classifies_correctly(self):
		"""A single Quotation, never amended, sent + approved -- the
		COTIZACION-4 shape: must read Aprobada, vigente, counted in
		Aprobadas, never in Pendientes."""
		name = self._new_quotation()
		self._send_to_billing(name)
		self._approve(name)

		with fx.as_user(self.vendedora):
			summary = cotizaciones.get_quotation_summary()
			q = next(row for row in cotizaciones.get_my_quotations(limit=500) if row["name"] == name)

		self.assertEqual(q["fg_billing_review_status"], "Aprobada")
		self.assertEqual(q["docstatus"], 1)
		self.assertTrue(self._matches_aprobadas(q))
		self.assertGreaterEqual(summary["aprobadas"], 1)

	def test_q_amended_approved_quotation_like_cotizacion_3_5_classifies_correctly(self):
		"""A price-adjusted-then-approved amendment -- the
		COTIZACION-3 -> COTIZACION-3-5 shape: only the current, vigente
		amendment reads Aprobada/counts; the superseded original does not."""
		item = self._priced_item("FG2516-Q-ITEM")
		original_name = self._new_quotation(item_name=item.name)
		self._send_to_billing(original_name)
		with fx.as_user(self.facturacion):
			adjusted = cotizaciones.apply_quotation_price_mode(original_name, "DISCOUNT_20")
		new_name = adjusted["name"]
		self.world.track_existing("Quotation", new_name)
		self._approve(new_name)

		with fx.as_user(self.vendedora):
			quotations = {row["name"]: row for row in cotizaciones.get_my_quotations(limit=500)}

		self.assertEqual(quotations[new_name]["fg_billing_review_status"], "Aprobada")
		self.assertEqual(quotations[new_name]["docstatus"], 1)
		self.assertEqual(quotations[original_name]["docstatus"], 2)
		self.assertTrue(self._matches_aprobadas(quotations[new_name]))
		self.assertFalse(self._matches_aprobadas(quotations[original_name]))

	# -- T/U: PDF regression -- still works exactly as Commit 25.15 left it --

	def test_t_u_pdf_still_works_for_aprobada_after_this_commit(self):
		"""This commit touches only classification (badge/KPI/filters) --
		the PDF security gate (`_assert_quotation_pdf_eligible()`) reads
		`fg_billing_review_status`/`docstatus` directly from the document,
		completely independent of get_quotation_summary()/get_my_quotations(),
		so nothing here could plausibly regress it -- asserted anyway, per
		section 14's own explicit requirement. `download_pdf` itself is
		monkeypatched to a no-op, same as test_cotizaciones_pdf.py's own
		test_ao_download_sets_the_expected_filename -- this environment has
		no wkhtmltopdf executable, the point here is only that
		download_fabrigray_quotation_pdf() reaches and calls the native
		pipeline at all (i.e. _assert_quotation_pdf_eligible() did not
		reject an Aprobada Quotation), not the real PDF bytes."""
		name = self._new_quotation()
		self._send_to_billing(name)
		self._approve(name)

		with fx.as_user(self.facturacion):
			url = cotizaciones.get_fabrigray_quotation_pdf_view_url(name)
			self.assertIn(name, url)

			from frappe.utils import print_format as print_format_module

			original = print_format_module.download_pdf
			print_format_module.download_pdf = lambda **kwargs: None
			try:
				cotizaciones.download_fabrigray_quotation_pdf(name)  # must not raise
			finally:
				print_format_module.download_pdf = original
			self.assertEqual(frappe.local.response.filename, f"Cotizacion-Fabrigray-{name}.pdf")

	# -- V: editar una Aprobada conserva el comportamiento 25.13 -------------

	def test_v_editing_an_approved_quotation_keeps_invalidating_the_approval(self):
		name = self._new_quotation()
		self._send_to_billing(name)
		self._approve(name)

		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()
			result = cotizaciones.modify_submitted_quotation(
				name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()
			new_qtn = frappe.get_doc("Quotation", result["name"])

		self.assertEqual(new_qtn.get("fg_billing_review_status"), "Borrador")  # _reset_billing_review_on_edit()
		self.assertEqual(after["aprobadas"], before["aprobadas"] - 1)
		self.assertEqual(after["pendientes"], before["pendientes"])

	# -- W/X: Vencidas / Cotizaciones de hoy -- unchanged by this commit -----

	def test_w_vencidas_still_derives_from_valid_till_never_from_billing_review(self):
		name = self._new_quotation()
		self._send_to_billing(name)
		self._approve(name)  # Aprobada...
		frappe.db.set_value(
			"Quotation", name, {"valid_till": add_days(nowdate(), -1), "status": "Expired"}
		)  # ...AND vencida at once -- section 10's own explicit scenario.

		with fx.as_user(self.vendedora):
			summary = cotizaciones.get_quotation_summary()
			q = next(row for row in cotizaciones.get_my_quotations(limit=500) if row["name"] == name)

		self.assertEqual(q["status"], "Expired")
		self.assertEqual(q["fg_billing_review_status"], "Aprobada")  # never overwritten by expiry
		self.assertGreaterEqual(summary["vencidas"], 1)
		self.assertTrue(self._matches_aprobadas(q))  # still counted in Aprobadas despite being expired

	def test_x_cotizaciones_de_hoy_still_derives_from_transaction_date(self):
		with fx.as_user(self.vendedora):
			before = cotizaciones.get_quotation_summary()
		self._new_quotation()  # transaction_date defaults to today, no billing review involved
		with fx.as_user(self.vendedora):
			after = cotizaciones.get_quotation_summary()
		self.assertEqual(after["cotizaciones_hoy"], before["cotizaciones_hoy"] + 1)


# -- A/B/C/D, R/S: static UI-contract checks on cotizaciones.js itself -- no
# JS test runner in this app (same convention as test_cotizaciones_pdf.py's
# own TestQuotationPdfUiContract/test_cotizaciones_billing_review.py's own
# UI-contract classes).
class TestQuotationCardBadgeUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		with open(_COTIZACIONES_JS_PATH, encoding="utf-8") as f:
			cls.js = f.read()

	@staticmethod
	def _function_body(source, function_name):
		m = re.search(r"\nfunction " + re.escape(function_name) + r"\([^)]*\)\s*\{", source)
		assert m, f"function {function_name!r} not found"
		start = m.end()
		next_decl = re.search(r"\n(function |const )[a-zA-Z_]", source[start:])
		end = start + next_decl.start() if next_decl else len(source)
		return source[start:end]

	def test_a_aprobada_maps_to_the_aprobada_badge(self):
		body = self._function_body(self.js, "quotation_review_badge_meta")
		self.assertRegex(body, r'Aprobada:\s*\{\s*label:\s*__\("Aprobada"\)')

	def test_b_top_badge_is_never_sourced_from_native_quotation_status_anymore(self):
		"""The exact regression this commit fixes: the top-right badge span
		must read from quotation_review_badge_meta()'s own output, never
		quotation_status_meta(q.status) (which is what produced a
		contradictory "Pendiente" on an Aprobada card)."""
		top_row = re.search(r'fg-quotation-card-id.*?\n\s*<span class="fg-badge fg-badge--\$\{([a-zA-Z_.]+)\}', self.js, re.S)
		self.assertIsNotNone(top_row)
		self.assertEqual(top_row.group(1), "top_badge.mod")
		self.assertNotIn("const status = quotation_status_meta(q.status)", self.js)

	def test_c_pendiente_maps_to_the_pendiente_badge(self):
		body = self._function_body(self.js, "quotation_review_badge_meta")
		self.assertRegex(body, r'"Pendiente de Facturación":\s*\{\s*label:\s*__\("Pendiente"\)')

	def test_d_devuelta_maps_to_the_devuelta_badge(self):
		body = self._function_body(self.js, "quotation_review_badge_meta")
		self.assertRegex(body, r'Devuelta:\s*\{\s*label:\s*__\("Devuelta"\)')

	def test_r_con_pedido_generado_no_longer_appears(self):
		self.assertNotIn("Con pedido generado", self.js)

	def test_s_por_facturacion_appears_as_the_aprobadas_kpi_subtitle(self):
		self.assertRegex(self.js, r'key:\s*"aprobadas".*?sub:\s*__\("Por Facturación"\)')

	def test_cancelled_amendment_never_shows_a_stale_aprobada_or_pendiente_badge(self):
		body = self._function_body(self.js, "quotation_review_badge_meta")
		# docstatus===2/Cancelled must be the FIRST check in the function --
		# confirmed by requiring it to appear before the fg_billing_review_
		# status map lookup in source order.
		cancelled_pos = body.index('mod: "review-cancelled"')
		map_pos = body.index("const map = {")
		self.assertLess(cancelled_pos, map_pos)

	def test_pendientes_filter_uses_billing_review_status_not_native_status(self):
		# quotation_matches_filter is a method, not a top-level function --
		# extracted by hand here since _function_body() only matches
		# column-0 `function` declarations.
		m = re.search(r"quotation_matches_filter\(q, filter\)\s*\{", self.js)
		self.assertIsNotNone(m)
		start = m.end()
		end = self.js.index("\n\t}", start)
		method_body = self.js[start:end]
		self.assertIn('"Pendiente de Facturación"', method_body)
		self.assertIn('"Aprobada"', method_body)
		self.assertNotIn('q.status === "Open"', method_body)
		self.assertNotIn('"Ordered", "Partially Ordered"', method_body)
