# -*- coding: utf-8 -*-
"""Facturación -> Cartera: a NEW Cartera Obligacion takes its due date from
the invoiced Pick List (fg_invoice_payment_method / fg_invoice_due_date).

One due date shared by Facturación, the commercial PDF and Cartera; credit
days counted from the invoicing date; never recomputed from the delivery.
Historical Pick Lists (no due date) keep the previous rule. No payment is
ever created from the payment method; the discrepancy alert is informative.

Deliveries go through the real api.recorridos.deliver_stop(), reusing the
Recorridos/Cartera helper FUNCTIONS (borrowed, never inherited), exactly like
test_cartera_foundation.py."""

import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service, invoice_issuers
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.permission_conditions import cartera_company_has_permission
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_cartera_foundation as foundation
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

INVOICE = cartera_service.DUE_SOURCE_INVOICE
LEGACY = cartera_service.DUE_SOURCE_LEGACY


class TestCarteraInvoiceDueDate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld(real_stock=True)
		cls.addClassCleanup(cls.world.cleanup)

		ns = cls.world.namespace
		cls.wh = cls.world.warehouse(f"FGCID WH {ns}")
		cls.item = cls.world.item(f"FGCID-ITEM-{ns}")
		cls.customer = cls.world.customer(f"FGCID Customer {ns}")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)

		low = ns.lower()
		cls.bodega_user = cls.world.user(f"fgcid-bodega-{low}@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fgcid-facturacion-{low}@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user(f"fgcid-recorrido-{low}@example.com", ["Recorrido"])
		cls.cartera_user = cls.world.user(f"fgcid-cartera-{low}@example.com", ["Cartera"])
		cls.other_cartera_user = cls.world.user(f"fgcid-cartera-otra-{low}@example.com", ["Cartera"])
		cls.world.company_user_permission(cls.other_cartera_user, "_Test Company")
		cls.no_role_user = cls.world.user(f"fgcid-norole-{low}@example.com", [])
		cls._seq = 0

		cls._evidence_file_names = []
		cls.addClassCleanup(cls._delete_evidence_files)
		cls.addClassCleanup(cls._delete_cartera_leftovers)

	_delete_evidence_files = classmethod(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__)
	_delete_cartera_leftovers = classmethod(foundation.TestCarteraFoundation._delete_cartera_leftovers.__func__)

	# -- Borrowed helpers ------------------------------------------------------
	_facturado_pick_list = base.TestRecorridosApi._facturado_pick_list
	_set_customer_primary_address = base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = base.TestRecorridosApi._geocode_customer_address
	_track_route = base.TestRecorridosApi._track_route
	_create_route = base.TestRecorridosApi._create_route
	_plan_route = base.TestRecorridosApi._plan_route
	_driver = base.TestRecorridosApi._driver
	_unique = start_base.TestRecorridosStartRoute._unique
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_route = start_base.TestRecorridosStartRoute._route
	_start = start_base.TestRecorridosStartRoute._start
	_en_ruta = deliver_base.TestRecorridosDeliverStop._en_ruta
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files

	def setUp(self):
		self.invoice_terms = None  # helper default: Contado, same day
		self.invoice_issuer = None  # helper default: integrandoMAS
		self.historical_invoice_terms = False

	# -- Local helpers -----------------------------------------------------------
	def _delivered(self, terms=None, issuer=None, historical=False, before_delivery=None, **report):
		"""Invoice with `terms` = (method, days), put it on a started route,
		optionally run `before_delivery(pick_list)` (test setup), deliver.
		Returns (stop, obligation doc, pick list name)."""
		self.invoice_terms = terms
		self.invoice_issuer = issuer
		self.historical_invoice_terms = historical
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		pick_list = frappe.db.get_value("Recorrido Parada", stop_name, "pick_list")
		if before_delivery:
			before_delivery(pick_list)
		report.setdefault("payment_status", "Pendiente por Pago")
		self._deliver(route["name"], stop_name, **report)
		name = frappe.db.get_value("Cartera Obligacion", {"recorrido_parada": stop_name}, "name")
		self.assertTrue(name, "the delivery must create its obligation")
		return frappe.get_doc("Recorrido Parada", stop_name), frappe.get_doc("Cartera Obligacion", name), pick_list

	def _pl(self, pick_list):
		return frappe.db.get_value(
			"Pick List",
			pick_list,
			["fg_invoice_payment_method", "fg_invoice_due_date", "fg_invoiced_on", "fg_invoice_issuer"],
			as_dict=True,
		)

	def _detail(self, obligation, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_obligation_detail(obligation)

	def _card(self, obligation, user=None):
		with fx.as_user(user or self.cartera_user):
			items = cartera_api.get_obligations(search=frappe.db.get_value("Cartera Obligacion", obligation, "pick_list"))[
				"items"
			]
		return next(i for i in items if i["name"] == obligation)

	def _payments(self, obligation):
		return frappe.get_all("Cartera Pago", filters={"cartera_obligacion": obligation}, pluck="name")

	# =====================================================================
	# 1. Credit invoices: 15/30/45/60/90 days
	# =====================================================================

	def test_01_credit_terms_copy_the_invoice_due_date_exactly(self):
		for days in (15, 30, 45, 60, 90):
			_stop, ob, pick_list = self._delivered(terms=("Crédito", days))
			pl = self._pl(pick_list)
			self.assertEqual(getdate(ob.due_date), getdate(pl.fg_invoice_due_date), days)
			self.assertEqual(getdate(ob.due_date), add_days(getdate(pl.fg_invoiced_on), days), days)
			self.assertEqual(ob.credit_days, days)
			self.assertEqual(ob.invoice_payment_method, "Crédito")
			self.assertEqual(ob.due_date_source, INVOICE)
			self.assertEqual(ob.status, "Pendiente")
			detail = self._detail(ob.name)
			self.assertEqual(detail["due_date"], str(getdate(pl.fg_invoice_due_date)))
			self.assertEqual(detail["invoice_date"], str(getdate(pl.fg_invoiced_on)))
			self.assertEqual(detail["credit_days"], days)
			self.assertEqual(detail["days_overdue"], 0)
			self.assertFalse(detail["immediate_payment"])
			self.assertIsNone(detail["terms_discrepancy"])
			self.assertEqual(detail["bucket"], "por_vencer")

	def test_02_driver_credit_never_recomputes_from_delivery(self):
		"""Old rule would be delivery + 30; the invoice says 45 -> 45 wins."""
		stop, ob, pick_list = self._delivered(terms=("Crédito", 45), payment_status="Crédito")
		self.assertEqual(getdate(ob.due_date), getdate(self._pl(pick_list).fg_invoice_due_date))
		self.assertNotEqual(getdate(ob.due_date), add_days(getdate(stop.delivered_on), 30))
		self.assertEqual(ob.credit_days, 45)
		self.assertIsNone(self._detail(ob.name)["terms_discrepancy"])  # Crédito vs Crédito: compatible

	# =====================================================================
	# 2. Immediate payment without confirmed collection
	# =====================================================================

	def test_03_immediate_payment_is_never_a_received_payment(self):
		doctypes = ("Cartera Pago", "Payment Entry", "GL Entry", "Journal Entry", "Sales Invoice")
		for method in ("Contado", "Efectivo", "Transferencia"):
			before = {}

			def snapshot(_pick_list):
				# Measured AFTER picking/invoicing (picking's own Material
				# Issue posts stock GL by design): delivery + obligation only.
				before.update({dt: frappe.db.count(dt) for dt in doctypes})

			_stop, ob, pick_list = self._delivered(terms=(method, 0), before_delivery=snapshot)
			self.assertEqual(ob.invoice_payment_method, method)
			self.assertEqual(ob.credit_days, 0)
			self.assertEqual(getdate(ob.due_date), getdate(self._pl(pick_list).fg_invoiced_on))
			self.assertEqual(ob.status, "Pendiente")
			self.assertEqual(flt(ob.paid_amount), 0)
			self.assertEqual(flt(ob.outstanding_amount), flt(ob.invoice_amount))
			self.assertFalse(ob.payment_verification)
			self.assertEqual(self._payments(ob.name), [], method)
			after = {dt: frappe.db.count(dt) for dt in before}
			self.assertEqual(after, before, method)
			detail = self._detail(ob.name)
			self.assertTrue(detail["immediate_payment"], method)
			self.assertEqual(detail["paid_amount"], 0)
			self.assertTrue(detail["can_register_payment"])  # still collected through Cartera

	def test_04_driver_paid_report_keeps_the_existing_confirmation_flow(self):
		"""Contado + driver "Pagado" WITHOUT proof stays Por Validar (decision
		V2, unchanged): the payment method never confirms anything."""
		_stop, ob, _pl = self._delivered(terms=("Contado", 0), payment_status="Pagado")
		self.assertEqual(ob.status, "Por Validar")
		self.assertEqual(ob.payment_verification, "Sin confirmar")
		self.assertEqual(self._payments(ob.name), [])

	# =====================================================================
	# 3. Delivery after the due date
	# =====================================================================

	def test_05_delivery_after_due_date_is_overdue_not_recomputed(self):
		billing = add_days(nowdate(), -10)
		due = add_days(nowdate(), -4)

		def invoiced_ten_days_ago(pick_list):
			# Test setup (time travel): invoiced 10 days ago with a 6-day term.
			frappe.db.set_value(
				"Pick List",
				pick_list,
				{"fg_invoiced_on": f"{billing} 09:00:00", "fg_invoice_due_date": due},
				update_modified=False,
			)

		stop, ob, _pl = self._delivered(terms=("Crédito", 6), before_delivery=invoiced_ten_days_ago)
		self.assertEqual(getdate(stop.delivered_on), getdate(nowdate()))
		self.assertEqual(getdate(ob.due_date), getdate(due))
		self.assertEqual(ob.credit_days, 6)
		detail = self._detail(ob.name)
		self.assertEqual(detail["bucket"], "vencido")
		self.assertEqual(detail["days_overdue"], 4)
		self.assertEqual(detail["invoice_date"], str(getdate(billing)))
		card = self._card(ob.name)
		self.assertEqual(card["days_overdue"], 4)
		self.assertEqual(card["invoice_date"], str(getdate(billing)))

	# =====================================================================
	# 4. Exact match PDF <-> Cartera
	# =====================================================================

	def test_06_pdf_and_cartera_show_the_same_due_date(self):
		for issuer, terms in (("integrandoMAS", ("Crédito", 30)), ("ecoluminar", ("Transferencia", 0)), ("3CORAZONESCAFES", ("Crédito", 60))):
			_stop, ob, pick_list = self._delivered(terms=terms, issuer=issuer)
			with fx.as_user(self.facturacion_user):
				html = frappe.get_print(
					"Pick List", pick_list, print_format=invoice_issuers.invoice_print_format(issuer), as_pdf=False
				)
			values = {
				label: re.search(re.escape(label) + r"</td>\s*<td[^>]*>\s*([^<]*?)\s*</td>", html).group(1)
				for label in ("FORMA DE PAGO", "FECHA DE FACTURA", "VENCIMIENTO")
			}
			detail = self._detail(ob.name)
			fmt = lambda iso: getdate(iso).strftime("%d/%m/%Y")  # noqa: E731
			self.assertEqual(values["VENCIMIENTO"], fmt(detail["due_date"]), issuer)
			self.assertEqual(values["FECHA DE FACTURA"], fmt(detail["invoice_date"]), issuer)
			self.assertEqual(values["FORMA DE PAGO"], detail["invoice_payment_method"], issuer)
			self.assertEqual(ob.invoice_issuer, issuer)

	# =====================================================================
	# 5. Historical compatibility
	# =====================================================================

	def test_07_historical_pick_list_keeps_the_previous_rule(self):
		stop, ob, _pl = self._delivered(historical=True, payment_status="Crédito")
		self.assertEqual(ob.due_date_source, LEGACY)
		self.assertFalse(ob.invoice_payment_method)
		self.assertEqual(ob.credit_days, cartera_service.DEFAULT_CREDIT_DAYS)
		self.assertEqual(getdate(ob.due_date), add_days(getdate(stop.delivered_on), 30))
		detail = self._detail(ob.name)
		self.assertFalse(detail["due_from_invoice"])
		self.assertFalse(detail["immediate_payment"])
		self.assertIsNone(detail["terms_discrepancy"])

		_stop, ob2, _pl2 = self._delivered(historical=True, payment_status="Pendiente por Pago")
		self.assertEqual(ob2.due_date_source, LEGACY)
		self.assertIsNone(ob2.due_date)
		self.assertEqual(ob2.credit_days, 0)

	def test_08_pre_integration_obligation_is_never_recalculated(self):
		"""An obligation created before this integration (empty source) stays
		exactly as it is -- reads never fill it, saves never change it."""
		_stop, ob, _pl = self._delivered(historical=True, payment_status="Crédito")
		# Test setup: what an obligation created before the integration looks like.
		frappe.db.set_value(
			"Cartera Obligacion", ob.name, {"due_date_source": None, "invoice_payment_method": None}, update_modified=False
		)
		before = frappe.db.get_value("Cartera Obligacion", ob.name, ["due_date", "credit_days", "modified"], as_dict=True)
		detail = self._detail(ob.name)
		self.assertFalse(detail["due_from_invoice"])
		self.assertIsNone(detail["terms_discrepancy"])
		after = frappe.db.get_value("Cartera Obligacion", ob.name, ["due_date", "credit_days", "modified"], as_dict=True)
		self.assertEqual(after, before)
		self.assertFalse(frappe.db.get_value("Cartera Obligacion", ob.name, "due_date_source"))

	def test_09_new_fields_are_immutable(self):
		_stop, ob, _pl = self._delivered(terms=("Crédito", 30))
		for field, value in (
			("invoice_payment_method", "Contado"),
			("due_date_source", LEGACY),
			("due_date", add_days(nowdate(), 90)),
			("credit_days", 90),
		):
			doc = frappe.get_doc("Cartera Obligacion", ob.name)
			doc.set(field, value)
			with self.assertRaises(frappe.ValidationError, msg=field):
				doc.save(ignore_permissions=True)
		fresh = frappe.get_doc("Cartera Obligacion", ob.name)
		self.assertEqual((fresh.invoice_payment_method, fresh.due_date_source, fresh.credit_days), ("Crédito", INVOICE, 30))

	def test_10_insert_ignores_supplied_terms(self):
		"""Only recorrido_parada comes from the caller: supplied terms are
		re-derived from the Pick List (controller, unchanged)."""
		_stop, ob, _pl = self._delivered(terms=("Crédito", 15))
		frappe.delete_doc("Cartera Obligacion", ob.name, ignore_permissions=True, force=True, ignore_on_trash=True)
		doc = frappe.get_doc(
			{
				"doctype": "Cartera Obligacion",
				"recorrido_parada": ob.recorrido_parada,
				"invoice_payment_method": "Contado",
				"due_date": add_days(nowdate(), 300),
				"due_date_source": LEGACY,
				"credit_days": 300,
			}
		).insert(ignore_permissions=True)
		self.world.track_existing("Cartera Obligacion", doc.name)
		self.assertEqual((doc.invoice_payment_method, doc.credit_days, doc.due_date_source), ("Crédito", 15, INVOICE))
		self.assertEqual(getdate(doc.due_date), getdate(ob.due_date))

	# =====================================================================
	# 6. Idempotent creation
	# =====================================================================

	def test_11_creation_stays_idempotent(self):
		_stop, ob, _pl = self._delivered(terms=("Crédito", 30))
		name, created = cartera_service.ensure_obligation_for_stop(ob.recorrido_parada)
		self.assertEqual((name, created), (ob.name, False))
		result = cartera_service.sync_missing_obligations()
		self.assertNotIn(ob.name, result["obligations"])
		for extra in result["obligations"]:
			self.world.track_existing("Cartera Obligacion", extra)
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": ob.recorrido_parada}), 1)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc({"doctype": "Cartera Obligacion", "recorrido_parada": ob.recorrido_parada}).insert(
				ignore_permissions=True
			)

	# =====================================================================
	# 7. Issuer and company separation, permissions
	# =====================================================================

	def test_12_each_issuer_keeps_its_own_terms(self):
		obligations = {}
		for issuer, terms in (("integrandoMAS", ("Crédito", 15)), ("ecoluminar", ("Crédito", 45)), ("3CORAZONESCAFES", ("Efectivo", 0))):
			_stop, ob, pick_list = self._delivered(terms=terms, issuer=issuer)
			obligations[issuer] = (ob, pick_list)
		for issuer, (ob, pick_list) in obligations.items():
			pl = self._pl(pick_list)
			self.assertEqual(ob.invoice_issuer, issuer)
			self.assertEqual(pl.fg_invoice_issuer, issuer)
			self.assertEqual(getdate(ob.due_date), getdate(pl.fg_invoice_due_date), issuer)
			self.assertEqual(ob.invoice_payment_method, pl.fg_invoice_payment_method, issuer)
		self.assertEqual(obligations["ecoluminar"][0].credit_days, 45)
		self.assertEqual(obligations["3CORAZONESCAFES"][0].credit_days, 0)
		# The test series are this TestWorld's own -- never the real ones.
		for issuer in obligations:
			self.assertIn("FG-FACT-TEST-", self.world.invoice_numbering[issuer]["series"])

	def test_13_company_isolation_and_permissions(self):
		_stop, ob, _pl = self._delivered(terms=("Crédito", 30))
		self.assertFalse(cartera_company_has_permission(ob, "read", user=self.other_cartera_user))
		for user in (self.other_cartera_user, self.no_role_user, self.facturacion_user, self.recorrido_user):
			with fx.as_user(user), self.assertRaises(frappe.PermissionError, msg=user):
				cartera_api.get_obligation_detail(ob.name)
		self.assertEqual(self._detail(ob.name)["invoice_payment_method"], "Crédito")
		self.assertEqual(ob.company, get_default_company())
		# Cartera still cannot write the obligation nor the Pick List.
		self.assertFalse(frappe.has_permission("Cartera Obligacion", "write", doc=ob, user=self.cartera_user))
		self.assertFalse(frappe.has_permission("Pick List", "write", user=self.cartera_user))

	# =====================================================================
	# 8. Discrepancies (informative only)
	# =====================================================================

	def test_14_immediate_invoice_with_driver_credit_is_flagged_not_changed(self):
		_stop, ob, pick_list = self._delivered(terms=("Contado", 0), payment_status="Crédito")
		# Data untouched: the invoice's date and term, not delivery + 30.
		self.assertEqual(getdate(ob.due_date), getdate(self._pl(pick_list).fg_invoice_due_date))
		self.assertEqual(ob.credit_days, 0)
		self.assertEqual(ob.driver_payment_status, "Crédito")
		modified = frappe.db.get_value("Cartera Obligacion", ob.name, "modified")
		detail = self._detail(ob.name)
		self.assertIn("pago inmediato", detail["terms_discrepancy"])
		self.assertIn("Contado", detail["terms_discrepancy"])
		self.assertIsNotNone(self._card(ob.name)["terms_discrepancy"])
		self.assertEqual(frappe.db.get_value("Cartera Obligacion", ob.name, "modified"), modified)

	def test_15_compatible_reports_are_not_flagged(self):
		for terms, status in ((("Contado", 0), "Pendiente por Pago"), (("Crédito", 30), "Pagado"), (("Crédito", 30), "Crédito")):
			_stop, ob, _pl = self._delivered(terms=terms, payment_status=status)
			self.assertIsNone(self._detail(ob.name)["terms_discrepancy"], (terms, status))

	def test_16_discrepancy_rule_unit(self):
		row = frappe._dict(due_date_source=INVOICE, invoice_payment_method="Transferencia", credit_days=0)
		for status, flagged in (("Crédito", True), ("Pagado", False), ("Pendiente por Pago", False), (None, False)):
			row.driver_payment_status = status
			self.assertEqual(bool(cartera_service.invoice_terms_discrepancy(row)), flagged, status)
		row.update(due_date_source=LEGACY, driver_payment_status="Crédito")
		self.assertIsNone(cartera_service.invoice_terms_discrepancy(row))
		self.assertFalse(cartera_service.is_immediate_payment(row))

	# =====================================================================
	# 9. UI contract
	# =====================================================================

	def test_17_ui_contract(self):
		import os

		path = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cartera", "cartera.js")
		with open(path) as f:
			js = f.read()
		for label in (
			"CONDICIONES DE LA FACTURA",
			"FORMA DE PAGO",
			"FECHA DE FACTURACIÓN",
			"VENCIMIENTO",
			"PLAZO",
			"DÍAS DE MORA",
			"PAGO INMEDIATO",
			"CONDICIÓN DISCREPANTE",
			"Alerta informativa: no modifica la obligación",
		):
			self.assertIn(label, js)
		# No new write calls from the Page.
		self.assertNotIn("set_invoice_payment_terms", js)
