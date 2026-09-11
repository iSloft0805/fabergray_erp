# -*- coding: utf-8 -*-
"""Commit 25.13 -- Cotizaciones: mandatory billing review by Facturación
before a Quotation may ever be converted to a Sales Order.

`fg_billing_review_status` (Custom Field, Select: ""/"Borrador"/"Pendiente
de Facturación"/"Aprobada"/"Devuelta") is a workflow layered ON TOP OF the
native Quotation docstatus/status -- every Quotation created through
`create_and_submit_quotation()` is already `docstatus=1` the moment it
exists (Commit 20.3 never left a real Draft), so "editable" here always
means going through `modify_submitted_quotation()`'s cancel+amend (this
commit's own new function, mirroring `modify_submitted_sales_order()`,
Commit 18.5), never a plain `.save()` on the still-submitted document.

Same convention as test_ventas_cancellation_reason.py: one class per
server-side concern, built directly against `api.cotizaciones` with
`fx.TestWorld`, plus a static UI-contract class at the bottom reading
cotizaciones.js/facturacion.js as text (this app has no JS test runner).
"""

import os
import re
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_COTIZACIONES_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
)
_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)
_FACTURACION_CSS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.css"
)


@contextmanager
def _stock_settings_default_warehouse(warehouse):
	"""Same sanctioned, test-only exception test_ventas_warehouse_fallback.py's
	own helper already establishes -- temporarily sets Stock Settings.
	default_warehouse, always restored afterward, even if the block raises."""
	ss = frappe.get_single("Stock Settings")
	original = ss.default_warehouse
	ss.default_warehouse = warehouse
	ss.save()
	frappe.db.commit()
	try:
		yield
	finally:
		ss.reload()
		ss.default_warehouse = original
		ss.save()
		frappe.db.commit()


class TestBillingReviewFlow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.item = cls.world.item("FG2513-ITEM")
		cls.other_item = cls.world.item("FG2513-OTHER-ITEM")
		cls.customer = cls.world.customer("FG2513 Customer")

		cls.vendedora = cls.world.user("fg2513-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2513-facturacion@example.com", ["Facturación"])
		cls.no_role_user = cls.world.user("fg2513-norole@example.com", [])

		# Section 3 review fix -- the real role matrix, not inferred from
		# Sales Invoice.read alone.
		cls.bodega = cls.world.user("fg2513-bodega@example.com", ["Bodega"])
		cls.jefe_bodega = cls.world.user("fg2513-jefebodega@example.com", ["Jefe de Bodega"])
		cls.recorrido = cls.world.user("fg2513-recorrido@example.com", ["Recorrido"])
		cls.gestion_clientes = cls.world.user("fg2513-gestioncli@example.com", ["Gestión de Clientes"])
		# Jefe de Bodega alone must NOT pass -- but the SAME user ALSO
		# holding Facturación must, because of the Facturación role, never
		# because of Jefe de Bodega itself.
		cls.jefe_bodega_y_facturacion = cls.world.user(
			"fg2513-jefebodega-facturacion@example.com", ["Jefe de Bodega", "Facturación"]
		)

	def _quotation(self):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 2}]
			)
		self.world.track_existing("Quotation", result["name"])
		return result["name"]

	def _historical_quotation(self):
		"""A Quotation that never went through create_and_submit_quotation()
		at all -- `fg_billing_review_status` stays at its schema default
		(`""`), simulating one created before this commit."""
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": fx.COMPANY,
				"items": [{"item_code": self.item.name, "qty": 3}],
			}
		)
		qtn.insert()
		qtn.submit()
		self.world.track_existing("Quotation", qtn.name)
		return qtn.name

	def _sent_quotation(self):
		name = self._quotation()
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(name)
		return name

	def _approved_quotation(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			cotizaciones.approve_quotation_billing(name)
		return name

	def _returned_quotation(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(name, reason="Precio incorrecto")
		return name

	# A. cotización nueva queda Borrador
	def test_a_new_quotation_starts_as_borrador(self):
		name = self._quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Borrador")
		self.assertIsNone(frappe.db.get_value("Quotation", name, "fg_billing_reviewed_by"))
		self.assertIsNone(frappe.db.get_value("Quotation", name, "fg_billing_reviewed_on"))

	# B. Vendedora puede enviar a Facturación
	def test_b_vendedora_can_send_to_billing(self):
		name = self._quotation()
		with fx.as_user(self.vendedora):
			result = cotizaciones.send_quotation_to_billing(name)
		self.assertEqual(result["fg_billing_review_status"], "Pendiente de Facturación")

	# C. envío inválido se rechaza
	def test_c_invalid_send_is_rejected(self):
		# Not submitted (docstatus 0) -- create directly, never submit.
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": fx.COMPANY,
				"items": [{"item_code": self.item.name, "qty": 1}],
			}
		)
		with fx.as_user(self.vendedora):
			qtn.insert()
		self.world.track_existing("Quotation", qtn.name)

		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.send_quotation_to_billing(qtn.name)

		# Already Pendiente -- cannot re-send.
		name = self._sent_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.send_quotation_to_billing(name)

		# Already Aprobada -- cannot re-send directly.
		approved_name = self._approved_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.send_quotation_to_billing(approved_name)

	# D. enviada queda Pendiente
	def test_d_sent_quotation_is_pending(self):
		name = self._sent_quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Pendiente de Facturación")
		self.assertIsNone(frappe.db.get_value("Quotation", name, "fg_billing_review_note"))

	# E. aparece en bandeja Facturación
	def test_e_appears_in_billing_tray(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			pending = [q["name"] for q in cotizaciones.get_pending_billing_review_quotations()]
		self.assertIn(name, pending)

	# F. otra Company no aparece
	def test_f_other_company_does_not_appear_in_tray(self):
		other_customer = self.world.customer("FG2513 Other Company Customer")
		other_item = self.world.item("FG2513-OTHER-COMPANY-ITEM")
		other_qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": other_item.name, "qty": 1, "rate": 100}],
			}
		)
		other_qtn.insert()
		self.world.track_existing("Quotation", other_qtn.name)
		other_qtn.submit()
		other_qtn.fg_billing_review_status = "Pendiente de Facturación"
		other_qtn.save()

		with fx.as_user(self.facturacion):
			pending = [q["name"] for q in cotizaciones.get_pending_billing_review_quotations()]
		self.assertNotIn(other_qtn.name, pending)

	# G. Vendedora no puede aprobar
	def test_g_vendedora_cannot_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)

	# also: Vendedora cannot browse the tray or return either -- same gate.
	def test_g2_vendedora_cannot_browse_tray_or_return(self):
		name = self._sent_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_pending_billing_review_quotations()
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.return_quotation_from_billing(name, reason="motivo")
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_quotation_billing_detail(name)

	# H. Facturación puede aprobar
	def test_h_facturacion_can_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			result = cotizaciones.approve_quotation_billing(name, note="Todo correcto")
		self.assertEqual(result["fg_billing_review_status"], "Aprobada")

	# I. aprobada guarda reviewed_by
	def test_i_approval_stores_reviewed_by(self):
		name = self._approved_quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_reviewed_by"), self.facturacion)

	# J. aprobada guarda reviewed_on
	def test_j_approval_stores_reviewed_on(self):
		name = self._approved_quotation()
		self.assertIsNotNone(frappe.db.get_value("Quotation", name, "fg_billing_reviewed_on"))

	def test_j2_approval_increments_revision_and_cannot_approve_twice(self):
		name = self._approved_quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_revision"), 1)
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.approve_quotation_billing(name)

	# K. Facturación puede devolver
	def test_k_facturacion_can_return(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			result = cotizaciones.return_quotation_from_billing(name, reason="Cantidad mal capturada")
		self.assertEqual(result["fg_billing_review_status"], "Devuelta")

	# L. devolución exige reason
	def test_l_return_requires_reason(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.return_quotation_from_billing(name)
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.return_quotation_from_billing(name, reason="   ")

	# M. Devuelta muestra observación
	def test_m_returned_quotation_shows_the_observation(self):
		name = self._returned_quotation()
		detail_key = "fg_billing_review_note"
		with fx.as_user(self.vendedora):
			detail = cotizaciones.get_quotation_detail(name)
		self.assertEqual(detail[detail_key], "Precio incorrecto")
		with fx.as_user(self.vendedora):
			mine = {q["name"]: q for q in cotizaciones.get_my_quotations()}
		self.assertEqual(mine[name][detail_key], "Precio incorrecto")

	# N. Devuelta puede corregirse/re-enviarse
	def test_n_returned_quotation_can_be_corrected_and_resent(self):
		name = self._returned_quotation()

		with fx.as_user(self.vendedora):
			result = cotizaciones.modify_submitted_quotation(
				name=name,
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 5}],
			)
		new_name = result["name"]
		self.world.track_existing("Quotation", new_name)

		self.assertEqual(frappe.db.get_value("Quotation", new_name, "fg_billing_review_status"), "Borrador")
		self.assertEqual(frappe.db.get_value("Quotation", new_name, "amended_from"), name)

		with fx.as_user(self.vendedora):
			resend = cotizaciones.send_quotation_to_billing(new_name)
		self.assertEqual(resend["fg_billing_review_status"], "Pendiente de Facturación")

	# O. disponibilidad ERP es read-only / P. no modifica Bin / Q. no Stock Entry / R. no Material Request / S. no Pick List
	def test_o_to_s_availability_read_only_and_creates_no_side_effects(self):
		name = self._sent_quotation()

		before_bin = frappe.db.sql("select modified from `tabBin` where item_code=%s", (self.item.name,))
		before_stock_entries = frappe.db.count("Stock Entry")
		before_material_requests = frappe.db.count("Material Request")
		before_pick_lists = frappe.db.count("Pick List")

		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)

		self.assertTrue(detail["items"])
		item0 = detail["items"][0]
		self.assertIn("available_qty", item0)
		self.assertIn("shortage_qty", item0)
		self.assertIn("requested_qty", item0)
		self.assertIn("warehouse", item0)
		self.assertIn("price_list", item0)

		after_bin = frappe.db.sql("select modified from `tabBin` where item_code=%s", (self.item.name,))
		self.assertEqual(before_bin, after_bin)
		self.assertEqual(frappe.db.count("Stock Entry"), before_stock_entries)
		self.assertEqual(frappe.db.count("Material Request"), before_material_requests)
		self.assertEqual(frappe.db.count("Pick List"), before_pick_lists)

	# T. no crea Sales Order al aprobar
	def test_t_approval_never_creates_a_sales_order(self):
		before = frappe.db.count("Sales Order")
		self._approved_quotation()
		self.assertEqual(frappe.db.count("Sales Order"), before)

	# U. price reference no expone costos
	def test_u_price_reference_never_exposes_buying_or_valuation_data(self):
		priced_item = self.world.item("FG2513-PRICED-ITEM")
		selling_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": priced_item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 150,
			}
		).insert()
		self.world.track_existing("Item Price", selling_price.name)
		buying_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": priced_item.name,
				"price_list": "Standard Buying",
				"buying": 1,
				"price_list_rate": 40,
			}
		).insert()
		self.world.track_existing("Item Price", buying_price.name)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": priced_item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])

		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(result["name"])

		item0 = detail["items"][0]
		self.assertEqual(item0["reference_rate"], 150)
		forbidden_keys = {"valuation_rate", "standard_rate", "last_purchase_rate", "buying_rate"}
		self.assertFalse(forbidden_keys & set(item0.keys()))

	# V. cotización no aprobada no puede crear pedido (guard-only, section 13's own decision --
	# no real conversion endpoint exists yet in this app)
	def test_v_unapproved_quotation_guard_blocks(self):
		name = self._sent_quotation()
		with self.assertRaises(frappe.ValidationError):
			cotizaciones.assert_quotation_approved_for_conversion(name)

		historical_name = self._historical_quotation()
		with self.assertRaises(frappe.ValidationError):
			cotizaciones.assert_quotation_approved_for_conversion(historical_name)

	# W. cotización aprobada sí puede continuar (guard-only)
	def test_w_approved_quotation_guard_passes(self):
		name = self._approved_quotation()
		cotizaciones.assert_quotation_approved_for_conversion(name)  # must not raise

	# X. modificar aprobada invalida aprobación
	def test_x_modifying_an_approved_quotation_invalidates_the_approval(self):
		name = self._approved_quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Aprobada")

		with fx.as_user(self.vendedora):
			result = cotizaciones.modify_submitted_quotation(
				name=name,
				customer=self.customer.name,
				items=[{"item_code": self.item.name, "qty": 9}],
			)
		new_name = result["name"]
		self.world.track_existing("Quotation", new_name)

		self.assertEqual(frappe.db.get_value("Quotation", new_name, "fg_billing_review_status"), "Borrador")
		self.assertIsNone(frappe.db.get_value("Quotation", new_name, "fg_billing_reviewed_by"))
		self.assertIsNone(frappe.db.get_value("Quotation", new_name, "fg_billing_reviewed_on"))
		# the OLD approved document is untouched (it's cancelled by the amend, never edited in place)
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "Aprobada")

	def test_x2_editing_is_blocked_entirely_while_pending(self):
		name = self._sent_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				cotizaciones.modify_submitted_quotation(
					name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
				)

	# Y. históricos vacíos no rompen UI (server half: response is well-formed, no crash)
	def test_y_historical_quotation_without_review_status_does_not_break_reads(self):
		name = self._historical_quotation()
		self.assertEqual(frappe.db.get_value("Quotation", name, "fg_billing_review_status"), "")

		with fx.as_user(self.vendedora):
			detail = cotizaciones.get_quotation_detail(name)
			mine = {q["name"]: q for q in cotizaciones.get_my_quotations()}

		self.assertIsNone(detail["fg_billing_review_status"])
		self.assertIsNone(mine[name]["fg_billing_review_status"])

	# Z. permisos y Company isolation (single-doc actions)
	def test_z_unauthorized_user_has_no_access(self):
		name = self._sent_quotation()
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.send_quotation_to_billing(name)
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_quotation_billing_detail(name)

	def test_z2_company_isolation_on_single_document_actions(self):
		other_customer = self.world.customer("FG2513 Other Company Customer Z")
		other_item = self.world.item("FG2513-OTHER-COMPANY-ITEM-Z")
		other_qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": other_item.name, "qty": 1, "rate": 100}],
			}
		)
		other_qtn.insert()
		self.world.track_existing("Quotation", other_qtn.name)
		other_qtn.submit()
		other_qtn.fg_billing_review_status = "Pendiente de Facturación"
		other_qtn.save()

		with fx.as_user(self.facturacion):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(other_qtn.name)
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.get_quotation_billing_detail(other_qtn.name)

	# =====================================================================
	# Review fix, section 1 -- warehouse resolution (letters A-F are THIS
	# review request's own lettering, distinct from the original A-Z test
	# list above). Same 4-tier native precedence
	# test_ventas_warehouse_fallback.py's own Cases A-D already prove for
	# _validate_and_build_item_rows() -- unit-tested directly against
	# _resolve_billing_review_warehouse() here (A-D: no document insert
	# needed, this is a pure read-only resolver) plus two full end-to-end
	# checks through get_quotation_billing_detail() itself (E/F).
	# =====================================================================

	def test_wh_a_line_warehouse_wins_over_item_default(self):
		line_wh = self.world.warehouse("FG2513 WH Line")
		default_wh = self.world.warehouse("FG2513 WH Default For A")
		item = self.world.item("FG2513-WH-A-ITEM", default_warehouse=default_wh.name)

		resolved = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, line_wh.name)
		self.assertEqual(resolved, line_wh.name)

	def test_wh_b_no_line_warehouse_uses_item_default(self):
		default_wh = self.world.warehouse("FG2513 WH Default For B")
		item = self.world.item("FG2513-WH-B-ITEM", default_warehouse=default_wh.name)

		resolved = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, None)
		self.assertEqual(resolved, default_wh.name)

	def test_wh_c_stock_settings_fallback_when_nothing_else_resolves(self):
		item = self.world.item("FG2513-WH-C-ITEM")  # no Item Default at all
		self.assertEqual(item.item_defaults, [])
		fallback_wh = self.world.warehouse("FG2513 WH Stock Settings Fallback")

		with _stock_settings_default_warehouse(fallback_wh.name):
			resolved = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, None)
		self.assertEqual(resolved, fallback_wh.name)

		# and resolves to None (never a guess) when nothing at all is configured
		with _stock_settings_default_warehouse(None):
			resolved_none = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, None)
		self.assertIsNone(resolved_none)

	def test_wh_d_never_crosses_company(self):
		item = self.world.item("FG2513-WH-D-ITEM")  # no Item Default

		# D1: a line warehouse belonging to a DIFFERENT Company is ignored,
		# falls through to the rest of the chain (which also resolves
		# nothing here -> None), never returned as-is.
		other_company_wh = "Finished Goods - _TC"  # belongs to _Test Company
		resolved = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, other_company_wh)
		self.assertIsNone(resolved)
		self.assertNotEqual(resolved, other_company_wh)

		# D2: Stock Settings.default_warehouse belonging to a different
		# Company is ignored the same way (same native check
		# test_ventas_warehouse_fallback.py's own Case F already proves).
		with _stock_settings_default_warehouse(other_company_wh):
			resolved2 = cotizaciones._resolve_billing_review_warehouse(item.name, fx.COMPANY, None)
		self.assertIsNone(resolved2)

	def test_wh_e_actual_qty_belongs_exactly_to_the_resolved_warehouse(self):
		wh_a = self.world.warehouse("FG2513 WH E Alpha")
		wh_b = self.world.warehouse("FG2513 WH E Beta")
		item = self.world.item("FG2513-WH-E-ITEM", default_warehouse=wh_a.name)
		self.world.stock_up(item.name, wh_a.name, 40)
		self.world.stock_up(item.name, wh_b.name, 999)  # a decoy -- must never be read

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 5}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])

		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(result["name"])

		item0 = detail["items"][0]
		self.assertEqual(item0["warehouse"], wh_a.name)
		self.assertEqual(item0["available_qty"], 40)  # never wh_b's 999

	def test_wh_f_shortage_is_computed_correctly(self):
		wh = self.world.warehouse("FG2513 WH F")
		item = self.world.item("FG2513-WH-F-ITEM", default_warehouse=wh.name)
		self.world.stock_up(item.name, wh.name, 3)

		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])

		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(result["name"])

		item0 = detail["items"][0]
		self.assertEqual(item0["requested_qty"], 10)
		self.assertEqual(item0["available_qty"], 3)
		self.assertEqual(item0["shortage_qty"], 7)

	# =====================================================================
	# Review fix, section 2 -- Price List (letters G-K, this review
	# request's own lettering).
	# =====================================================================

	def _quotation_with_price_list(self, item_code, price_list, qty=1):
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": fx.COMPANY,
				"selling_price_list": price_list,
				"items": [{"item_code": item_code, "qty": qty}],
			}
		)
		with fx.as_user(self.vendedora):
			qtn.insert()
			qtn.fg_billing_review_status = "Borrador"
			qtn.save()
			qtn.submit()
		self.world.track_existing("Quotation", qtn.name)
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(qtn.name)
		return qtn.name

	def test_pl_g_uses_the_quotations_own_selling_price_list(self):
		item = self.world.item("FG2513-PL-G-ITEM")
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 111,
			}
		).insert()
		self.world.track_existing("Item Price", frappe.db.get_value("Item Price", {"item_code": item.name}))

		name = self._quotation_with_price_list(item.name, "Standard Selling")
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)
		self.assertEqual(detail["items"][0]["price_list"], "Standard Selling")
		self.assertEqual(detail["items"][0]["reference_rate"], 111)

	def test_pl_h_standard_selling_works(self):
		# Same as test_pl_g -- kept as its own letter per the review's own
		# checklist, proving the default path (no custom Price List
		# involved at all) still resolves correctly.
		item = self.world.item("FG2513-PL-H-ITEM")
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 222,
			}
		).insert()
		self.world.track_existing("Item Price", frappe.db.get_value("Item Price", {"item_code": item.name}))

		name = self._quotation_with_price_list(item.name, "Standard Selling")
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)
		self.assertEqual(detail["items"][0]["reference_rate"], 222)

	def test_pl_i_another_selling_price_list_uses_its_own_item_price(self):
		other_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "FG2513 Other Selling List",
				"selling": 1,
				"currency": "INR",
			}
		)
		other_list.insert()
		self.world.track_existing("Price List", other_list.name)

		item = self.world.item("FG2513-PL-I-ITEM")
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 100,
			}
		).insert()
		self.world.track_existing("Item Price", frappe.db.get_value("Item Price", {"item_code": item.name, "price_list": "Standard Selling"}))
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": other_list.name,
				"selling": 1,
				"currency": "INR",
				"price_list_rate": 777,
			}
		).insert()
		self.world.track_existing(
			"Item Price", frappe.db.get_value("Item Price", {"item_code": item.name, "price_list": other_list.name})
		)

		name = self._quotation_with_price_list(item.name, other_list.name)
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)
		self.assertEqual(detail["items"][0]["price_list"], other_list.name)
		self.assertEqual(detail["items"][0]["reference_rate"], 777)  # never the Standard Selling 100

	def test_pl_j_never_falls_back_to_standard_selling_silently(self):
		other_list = frappe.get_doc(
			{"doctype": "Price List", "price_list_name": "FG2513 No Item Price List", "selling": 1, "currency": "INR"}
		)
		other_list.insert()
		self.world.track_existing("Price List", other_list.name)

		item = self.world.item("FG2513-PL-J-ITEM")
		# Standard Selling DOES have a price -- must never be picked up.
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 555,
			}
		).insert()
		self.world.track_existing("Item Price", frappe.db.get_value("Item Price", {"item_code": item.name}))
		# The quotation's own list (other_list) has NO Item Price at all.

		name = self._quotation_with_price_list(item.name, other_list.name)
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)
		self.assertEqual(detail["items"][0]["price_list"], other_list.name)
		self.assertIsNone(detail["items"][0]["reference_rate"])
		self.assertIsNone(detail["items"][0]["rate_difference"])

	def test_pl_k_no_cost_data_exposed_regardless_of_price_list(self):
		name = self._sent_quotation()
		with fx.as_user(self.facturacion):
			detail = cotizaciones.get_quotation_billing_detail(name)
		item0 = detail["items"][0]
		forbidden_keys = {"valuation_rate", "standard_rate", "last_purchase_rate", "buying_rate"}
		self.assertFalse(forbidden_keys & set(item0.keys()))

	# =====================================================================
	# Review fix, section 3 -- authorization by REAL role, not inferred
	# from Sales Invoice.read alone.
	# =====================================================================

	def test_auth_bodega_cannot_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.bodega):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)

	def test_auth_jefe_de_bodega_alone_cannot_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.jefe_bodega):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)

	def test_auth_recorrido_cannot_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.recorrido):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)

	def test_auth_gestion_de_clientes_cannot_approve(self):
		name = self._sent_quotation()
		with fx.as_user(self.gestion_clientes):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones.approve_quotation_billing(name)

	def test_auth_jefe_de_bodega_plus_facturacion_can_approve(self):
		"""The SAME user holding both roles passes -- because of the
		Facturación role, never because of Jefe de Bodega itself."""
		name = self._sent_quotation()
		with fx.as_user(self.jefe_bodega_y_facturacion):
			result = cotizaciones.approve_quotation_billing(name)
		self.assertEqual(result["fg_billing_review_status"], "Aprobada")

	def test_auth_administrator_bypass_preserved(self):
		name = self._sent_quotation()
		with fx.as_user("Administrator"):
			result = cotizaciones.approve_quotation_billing(name)
		self.assertEqual(result["fg_billing_review_status"], "Aprobada")

	def test_auth_role_check_function_directly(self):
		"""Unit-level pin on the gate itself -- the exact matrix the
		review asked for, independent of any one endpoint."""
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones._require_facturacion_role()
		with fx.as_user(self.bodega):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones._require_facturacion_role()
		with fx.as_user(self.jefe_bodega):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones._require_facturacion_role()
		with fx.as_user(self.recorrido):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones._require_facturacion_role()
		with fx.as_user(self.gestion_clientes):
			with self.assertRaises(frappe.PermissionError):
				cotizaciones._require_facturacion_role()
		with fx.as_user(self.facturacion):
			cotizaciones._require_facturacion_role()  # must not raise
		with fx.as_user(self.jefe_bodega_y_facturacion):
			cotizaciones._require_facturacion_role()  # must not raise
		with fx.as_user("Administrator"):
			cotizaciones._require_facturacion_role()  # must not raise

	# =====================================================================
	# Review fix, section 4 -- editing while Pendiente / Devuelta (letters
	# L-O, this review request's own lettering).
	# =====================================================================

	# L. pending no editable por Vendedora desde API
	def test_pending_l_not_editable_via_api_with_the_exact_message(self):
		name = self._sent_quotation()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError) as ctx:
				cotizaciones.modify_submitted_quotation(
					name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
				)
		self.assertIn("La cotización está siendo revisada por Facturación.", str(ctx.exception))

	# N. Devuelta vuelve a ser editable
	def test_pending_n_returned_is_editable_again(self):
		name = self._returned_quotation()
		with fx.as_user(self.vendedora):
			result = cotizaciones.modify_submitted_quotation(
				name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 4}]
			)
		self.world.track_existing("Quotation", result["name"])
		self.assertEqual(frappe.db.get_value("Quotation", result["name"], "fg_billing_review_status"), "Borrador")

	# O. Aprobada modificada por una vía legítima invalida aprobación --
	# already proven end-to-end by test_x_modifying_an_approved_quotation_
	# invalidates_the_approval() above; this pins the exact same guarantee
	# unit-style, directly against _reset_billing_review_on_edit().
	def test_pending_o_reset_helper_always_clears_approval_fields(self):
		qtn = frappe._dict(
			fg_billing_review_status="Aprobada",
			fg_billing_reviewed_by="someone@example.com",
			fg_billing_reviewed_on="2026-01-01 00:00:00",
		)
		cotizaciones._reset_billing_review_on_edit(qtn)
		self.assertEqual(qtn.fg_billing_review_status, "Borrador")
		self.assertIsNone(qtn.fg_billing_reviewed_by)
		self.assertIsNone(qtn.fg_billing_reviewed_on)


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_ventas_confirm_button_contract.py's/test_ventas_
	cancellation_reason.py's own."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


class TestBillingReviewUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cotizaciones_js = _read(_COTIZACIONES_JS_PATH)
		cls.facturacion_js = _read(_FACTURACION_JS_PATH)

	# M/N/O/P -- UI abre diálogo / reason obligatoria / Otro exige detalle
	# (mirrors the confirm_cancel_order() dialog contract already pinned in
	# test_ventas_cancellation_reason.py, applied here to Facturación's own
	# devolver dialog)
	def test_return_dialog_exists_and_requires_a_reason(self):
		body = _method_body(self.facturacion_js, "open_return_quotation_dialog")
		self.assertIn("new frappe.ui.Dialog(", body)
		self.assertIn("reqd: 1", body)

	def test_approve_action_calls_the_approve_endpoint(self):
		self.assertIn("approve_quotation_billing", self.facturacion_js)

	def test_return_action_calls_the_return_endpoint(self):
		self.assertIn("return_quotation_from_billing", self.facturacion_js)

	def test_tray_lists_only_pending_quotations(self):
		self.assertIn("get_pending_billing_review_quotations", self.facturacion_js)

	def test_send_to_billing_button_exists_in_cotizaciones(self):
		self.assertIn("send_quotation_to_billing", self.cotizaciones_js)

	def test_modify_submitted_quotation_is_wired_for_returned_state(self):
		self.assertIn("modify_submitted_quotation", self.cotizaciones_js)

	# Y (client half) -- historical/null review status never rendered as a raw null
	def test_historical_review_status_has_a_fallback_label(self):
		self.assertIn("Borrador", self.cotizaciones_js)

	# M (review fix, section 4) -- Pendiente shows VER only, no EDITAR/
	# ENVIAR action -- render_quotation_card_actions()'s own early-return
	# branch for billing_status === "Pendiente de Facturación".
	def test_pending_m_ui_shows_no_edit_actions_while_pending(self):
		body = _method_body(self.cotizaciones_js, "render_quotation_card_actions")
		pending_check_pos = body.index('billing_status === "Pendiente de Facturación"')
		early_return_block = body[pending_check_pos : body.index("}", pending_check_pos)]
		self.assertIn("return", early_return_block)
		self.assertNotIn("fg-quotation-card-modify", early_return_block)
		self.assertNotIn("fg-quotation-card-send-billing", early_return_block)

	# AA -- Ventas' own always-enabled Confirmar/Guardar button is untouched
	# by this commit (regression pin, narrow -- the exhaustive suite is
	# test_ventas_confirm_button_contract.py).
	def test_aa_ventas_confirm_button_contract_untouched_by_this_commit(self):
		ventas_js_path = os.path.join(
			frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js"
		)
		ventas_js = _read(ventas_js_path)
		self.assertNotIn('.fg-confirm-btn").prop("disabled", true)', ventas_js)


# Commit 25.13.1's own dedicated regex for a *bare*, dangerous global CSS
# selector -- a line whose selector list is exactly `svg`/`img`/`button`/
# `table`/`.modal`/`.modal-dialog`/`.modal-content`/`.modal-body`/
# `.modal-footer`/`.modal-header` (optionally comma-chained with other
# equally-bare ones), never one of those prefixed by any class (e.g.
# `.fg-fact-billing-review-dialog .modal-footer` is safe and must NOT
# match). This is deliberately narrow: it is not a general CSS parser, it
# exists only to catch the exact class of regression section 10 warns
# against.
_BARE_DANGEROUS_SELECTOR_RE = re.compile(
	r"(?m)^(?:svg|img|button|table|\.modal|\.modal-dialog|\.modal-content|\.modal-body|\.modal-footer|\.modal-header)"
	r"(?:\s*,\s*(?:svg|img|button|table|\.modal|\.modal-dialog|\.modal-content|\.modal-body|\.modal-footer|\.modal-header))*"
	r"\s*\{"
)


class TestBillingReviewDialogVisualContract(IntegrationTestCase):
	"""Commit 25.13.1 -- static contract pins for the "Revisar cotización"
	dialog's visual fix. Same convention as every other class in this file
	(this app has no JS test runner): read facturacion.js/facturacion.css
	as text, assert on them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.facturacion_js = _read(_FACTURACION_JS_PATH)
		cls.facturacion_css = _read(_FACTURACION_CSS_PATH)
		cls.open_dialog_body = _method_body(cls.facturacion_js, "open_billing_review_dialog")
		cls.render_body_body = _method_body(cls.facturacion_js, "render_billing_review_dialog_body")
		cls.render_row_body = _method_body(cls.facturacion_js, "render_billing_review_row")

	# A. modal tiene root class scoped
	def test_a_dialog_wrapper_carries_its_own_scoped_root_class(self):
		self.assertIn('dialog.$wrapper.addClass("fg-fact-billing-review-dialog")', self.open_dialog_body)
		# and CSS actually styles under that exact root, not the Page's own
		# `.fg-facturacion` (the root cause this whole fix exists for).
		self.assertIn(".fg-fact-billing-review-dialog .modal-dialog", self.facturacion_css)
		self.assertNotIn(".fg-facturacion .fg-fact-billing-review-dialog", self.facturacion_css)

	# B. no existen SVG sin clase/tamaño controlado
	def test_b_every_icon_call_produces_a_classed_sized_svg(self):
		# icon() (shared helper, bottom of the file) always emits `class="fg-icon ...`
		self.assertIn('class="fg-icon', self.facturacion_js)
		# and this dialog explicitly sizes that class -- never left to the
		# SVG's own raw intrinsic size.
		start = self.facturacion_css.index(".fg-fact-billing-review-dialog .fg-icon {")
		end = self.facturacion_css.index("}", start)
		icon_rule_body = self.facturacion_css[start:end]
		self.assertIn("width:", icon_rule_body)
		self.assertIn("height:", icon_rule_body)

	# C. CSS del modal no agrega reglas globales svg/img/button
	def test_c_no_bare_dangerous_global_selector_anywhere_in_the_file(self):
		matches = _BARE_DANGEROUS_SELECTOR_RE.findall(self.facturacion_css)
		self.assertEqual(matches, [], f"bare/global selector(s) found: {matches}")

	# D. desktop usa tabla estructurada
	def test_d_desktop_table_is_a_real_css_grid(self):
		start = self.facturacion_css.index(".fg-fact-billing-review-dialog .fg-fact-billing-review-thead,")
		end = self.facturacion_css.index("}", start)
		self.assertIn("display: grid", self.facturacion_css[start:end])
		self.assertIn("grid-template-columns", self.facturacion_css[start:end])

	# E. mobile tiene representación responsive
	def test_e_mobile_collapses_the_same_row_to_a_stacked_card(self):
		media_start = self.facturacion_css.index("@media (max-width: 860px)")
		media_block = self.facturacion_css[media_start:]
		self.assertIn(".fg-fact-billing-review-dialog .fg-fact-billing-review-thead {\n\t\tdisplay: none;", media_block)
		self.assertIn("grid-template-columns: 1fr;", media_block)
		self.assertIn("data-label", self.render_row_body)

	# F. footer tiene 3 acciones
	def test_f_footer_has_exactly_three_actions(self):
		self.assertIn("primary_action:", self.open_dialog_body)  # Aprobar
		self.assertIn("secondary_action_label: __(\"Cerrar\")", self.open_dialog_body)  # Cerrar
		self.assertIn("dialog.add_custom_action(", self.open_dialog_body)  # Devolver a vendedora

	# G. botones no usan layout gigante
	def test_g_footer_buttons_use_a_normal_fixed_height(self):
		start = self.facturacion_css.index(".fg-fact-billing-review-dialog .btn-modal-secondary,")
		end = self.facturacion_css.index("}", start)
		button_rule = self.facturacion_css[start:end]
		self.assertIn("height: 40px", button_rule)
		self.assertNotIn("width: 50%", self.facturacion_css)

	# H. reference_rate null muestra texto descriptivo
	def test_h_null_reference_rate_shows_descriptive_text(self):
		self.assertIn("Sin precio de referencia", self.render_row_body)
		self.assertNotIn('"N/D"', self.render_row_body)

	# I. warehouse null muestra "Almacén sin definir"
	def test_i_null_warehouse_shows_the_exact_label(self):
		self.assertIn("Almacén sin definir", self.render_row_body)

	# J. disponibilidad suficiente identificable
	def test_j_sufficient_availability_has_its_own_badge_class(self):
		self.assertIn("fg-badge--billing-avail-ok", self.render_row_body)
		self.assertIn(".fg-badge--billing-avail-ok {", self.facturacion_css)

	# K. faltante identificable
	def test_k_shortage_has_its_own_badge_class(self):
		self.assertIn("fg-badge--billing-avail-short", self.render_row_body)
		self.assertIn(".fg-badge--billing-avail-short {", self.facturacion_css)
		self.assertIn("has_shortage", self.render_row_body)

	# L. funcionalidad Aprobar sigue conectada
	def test_l_approve_is_still_wired(self):
		self.assertIn("approve_quotation_billing", self.facturacion_js)
		self.assertIn("confirm_approve_from_dialog", self.open_dialog_body)

	# M. funcionalidad Devolver sigue conectada
	def test_m_return_is_still_wired(self):
		self.assertIn("open_return_quotation_dialog", self.open_dialog_body)
		self.assertIn("return_quotation_from_billing", self.facturacion_js)

	# N. Cerrar funciona
	def test_n_close_is_still_wired(self):
		self.assertIn("secondary_action: () => dialog.hide()", self.open_dialog_body)

	# O. diálogo de razón al devolver sigue funcionando
	def test_o_return_reason_dialog_still_required(self):
		body = _method_body(self.facturacion_js, "open_return_quotation_dialog")
		self.assertIn("new frappe.ui.Dialog(", body)
		self.assertIn("reqd: 1", body)
