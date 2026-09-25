# -*- coding: utf-8 -*-
"""Fase 27.1 -- Cartera foundation: Cartera Obligacion / Cartera Pago, their
creation from delivered stops (in-line savepoint + reconciler), immutability,
balances, overpayment, concurrency, permissions and company isolation.

Deliveries go through the real api.recorridos.deliver_stop() (photo +
signature + driver report), reusing test_recorridos_deliver_stop.py helper
FUNCTIONS -- borrowed, never inherited (a TestCase subclass would re-run
that whole suite inside this module)."""

import json
import os
import threading
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.api import facturacion
from fabergray_erp.permission_conditions import (
	cartera_company_has_permission,
	cartera_obligacion_permission_query_conditions,
)
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base
from fabergray_erp.tests.test_recorridos_deliver_stop import _photo_jpeg

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []



class TestCarteraFoundation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG271 WH")
		cls.item = cls.world.item("FG271-ITEM")
		cls.item2 = cls.world.item("FG271-ITEM-2")
		cls.customer = cls.world.customer("FG271 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg271-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg271-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg271-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg271-recorrido-b@example.com", ["Recorrido"])
		cls.cartera_user = cls.world.user("fg271-cartera@example.com", ["Cartera"])
		cls.cartera_user_b = cls.world.user("fg271-cartera-b@example.com", ["Cartera"])
		cls.no_role_user = cls.world.user("fg271-norole@example.com", [])
		cls.system_manager_user = cls.world.user("fg271-sysmanager@example.com", ["System Manager"])
		cls._seq = 0

		cls._evidence_file_names = []
		# Class cleanups run LIFO: Cartera leftovers, then evidence Files, then
		# world.cleanup().
		cls.addClassCleanup(cls._delete_evidence_files)
		cls.addClassCleanup(cls._delete_cartera_leftovers)

	_delete_evidence_files = classmethod(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__)

	@classmethod
	def _delete_cartera_leftovers(cls):
		"""Every obligation/payment of a stop this class created -- including
		the ones the reconciler creates, which no helper tracks. Same
		teardown-only path as world.cleanup() (ignore_on_trash: both
		doctypes refuse deletion in production on purpose)."""
		frappe.set_user("Administrator")
		stops = [name for doctype, name in cls.world._created if doctype == "Recorrido Parada"]
		if not stops:
			return
		for obligation in frappe.get_all("Cartera Obligacion", filters={"recorrido_parada": ["in", stops]}, pluck="name"):
			for payment in frappe.get_all(
				"Cartera Pago", filters={"cartera_obligacion": obligation}, fields=["name", "docstatus"]
			):
				if payment.docstatus == 1:
					cartera_service.authorize_teardown(frappe.get_doc("Cartera Pago", payment.name)).cancel()
				frappe.delete_doc("Cartera Pago", payment.name, ignore_permissions=True, force=True, ignore_on_trash=True)
			frappe.delete_doc("Cartera Obligacion", obligation, ignore_permissions=True, force=True, ignore_on_trash=True)
		frappe.db.commit()

	# -- Borrowed helpers ------------------------------------------------------
	_facturado_pick_list = base.TestRecorridosApi._facturado_pick_list
	_set_customer_primary_address = base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = base.TestRecorridosApi._geocode_customer_address
	_track_route = base.TestRecorridosApi._track_route
	_create_route = base.TestRecorridosApi._create_route
	_plan_route = base.TestRecorridosApi._plan_route
	_driver = base.TestRecorridosApi._driver
	_force_route_status = base.TestRecorridosApi._force_route_status
	_unique = start_base.TestRecorridosStartRoute._unique
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_route = start_base.TestRecorridosStartRoute._route
	_start = start_base.TestRecorridosStartRoute._start
	_en_ruta = deliver_base.TestRecorridosDeliverStop._en_ruta
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files

	# -- Local helpers -----------------------------------------------------------
	def _delivered(self, **report):
		"""A real delivered stop; returns (stop_name, obligation_name|None)."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, **report)
		return stop_name, self._obligation_of(stop_name)

	def _obligation_of(self, stop_name):
		return frappe.db.get_value("Cartera Obligacion", {"recorrido_parada": stop_name}, "name")

	def _ob(self, name):
		return frappe.get_doc("Cartera Obligacion", name)

	def _new_payment(self, obligation, amount, user=None, request_id=None, **fields):
		"""Fase 27.3: a Cartera payment only exists through the service
		(REGISTRAR COBRO); direct ORM inserts are refused by the controller."""
		values = {"payment_date": nowdate(), "payment_method": "Transferencia"}
		values.update(fields)
		with fx.as_user(user or self.cartera_user):
			result = cartera_service.register_cartera_payment(
				obligation,
				amount,
				values["payment_date"],
				values["payment_method"],
				reference=values.get("reference"),
				notes=values.get("notes"),
				client_request_id=request_id or str(uuid.uuid4()),
			)
		self.world.track_existing("Cartera Pago", result["payment"])
		return frappe.get_doc("Cartera Pago", result["payment"])

	def _direct_payment(self, obligation, amount, **fields):
		"""A payment built WITHOUT the service (Desk/frappe.client/bare ORM)."""
		values = {
			"doctype": "Cartera Pago",
			"cartera_obligacion": obligation,
			"amount": amount,
			"payment_date": nowdate(),
			"payment_method": "Transferencia",
			"client_request_id": f"cartera:{uuid.uuid4()}",
		}
		values.update(fields)
		return frappe.get_doc(values)

	def _payments(self, obligation):
		return frappe.get_all(
			"Cartera Pago",
			filters={"cartera_obligacion": obligation},
			fields=["name", "amount", "docstatus", "source", "payment_proof", "payment_date"],
		)

	def _track_obligation(self, name):
		self.world.track_existing("Cartera Obligacion", name)
		for payment in frappe.get_all("Cartera Pago", filters={"cartera_obligacion": name}, pluck="name"):
			self.world.track_existing("Cartera Pago", payment)

	# =====================================================================
	# Creation from the delivery + states (decisions V1/V2/V4)
	# =====================================================================

	def test_credit_delivery_creates_pending_obligation_with_30_days(self):
		stop_name, obligation = self._delivered(payment_status="Crédito")
		self.assertTrue(obligation)
		ob = self._ob(obligation)
		stop = frappe.get_doc("Recorrido Parada", stop_name)
		self.assertEqual(ob.status, "Pendiente")
		self.assertEqual(ob.driver_payment_status, "Crédito")
		self.assertEqual(ob.credit_days, 30)
		self.assertEqual(getdate(ob.delivery_date), getdate(stop.delivered_on))
		self.assertEqual(getdate(ob.due_date), add_days(getdate(stop.delivered_on), 30))
		self.assertGreater(flt(ob.invoice_amount), 0)
		self.assertEqual(flt(ob.outstanding_amount), flt(ob.invoice_amount))
		self.assertEqual(flt(ob.paid_amount), 0)
		self.assertFalse(ob.payment_verification)
		for field in ("recorrido", "pick_list", "sales_order", "customer", "customer_name", "delivered_by"):
			self.assertEqual(ob.get(field), stop.get(field) if field != "recorrido" else stop.recorrido, field)
		self.assertEqual(ob.company, get_default_company())
		self.assertEqual(ob.currency, "COP")
		self.assertEqual(self._payments(obligation), [])

	def test_pending_payment_obligation_has_no_due_date(self):
		_stop, obligation = self._delivered(payment_status="Pendiente por Pago", payment_note="Transfiere mañana")
		ob = self._ob(obligation)
		self.assertEqual(ob.status, "Pendiente")
		self.assertEqual(ob.credit_days, 0)
		self.assertIsNone(ob.due_date)
		self.assertEqual(ob.driver_payment_note, "Transfiere mañana")
		self.assertEqual(ob.amount_source, "Factura comercial")
		self.assertGreater(flt(ob.invoice_amount), 0)
		self.assertEqual(flt(ob.paid_amount), 0)
		self.assertEqual(flt(ob.outstanding_amount), flt(ob.invoice_amount))
		self.assertEqual(self._payments(obligation), [])

	def test_paid_with_proof_is_pagado_unconfirmed_via_driver_payment(self):
		stop_name, obligation = self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg())
		self._track_obligation(obligation)
		ob = self._ob(obligation)
		stop = frappe.get_doc("Recorrido Parada", stop_name)
		self.assertEqual(ob.status, "Pagado")
		self.assertEqual(ob.payment_verification, "Sin confirmar")
		self.assertEqual(flt(ob.paid_amount), flt(ob.invoice_amount))
		self.assertEqual(flt(ob.outstanding_amount), 0)
		self.assertEqual(getdate(ob.paid_on), getdate(ob.delivery_date))
		# The proof is referenced, never copied.
		self.assertEqual(ob.driver_payment_proof, stop.payment_proof)
		payments = self._payments(obligation)
		self.assertEqual(len(payments), 1)
		self.assertEqual(payments[0].source, "Conductor")
		self.assertEqual(payments[0].docstatus, 1)
		self.assertEqual(flt(payments[0].amount), flt(ob.invoice_amount))
		self.assertFalse(payments[0].payment_proof)
		self.assertEqual(
			frappe.db.count("File", {"attached_to_doctype": ["in", ["Cartera Obligacion", "Cartera Pago"]]}), 0
		)
		# The proof stays a PRIVATE File of the stop. In 27.1 Cartera has no
		# access to it (the controlled endpoint arrives in 27.2); a URL guess
		# is not enough.
		proof = frappe.get_doc("File", {"file_url": stop.payment_proof, "attached_to_name": stop_name})
		self.assertEqual(proof.is_private, 1)
		self.assertTrue(proof.file_url.startswith("/private/files/"))
		self.assertTrue(frappe.has_permission("File", "read", doc=proof, user=self.system_manager_user))
		for user in (self.cartera_user, self.no_role_user, self.facturacion_user):
			self.assertFalse(frappe.has_permission("File", "read", doc=proof, user=user), user)

	def test_paid_without_proof_is_por_validar(self):
		_stop, obligation = self._delivered(payment_status="Pagado")
		ob = self._ob(obligation)
		self.assertEqual(ob.status, "Por Validar")
		self.assertEqual(ob.payment_verification, "Sin confirmar")
		self.assertEqual(ob.amount_source, "Factura comercial")
		self.assertGreater(flt(ob.invoice_amount), 0)
		self.assertEqual(flt(ob.paid_amount), 0)
		self.assertEqual(flt(ob.outstanding_amount), flt(ob.invoice_amount))
		self.assertIsNone(ob.paid_on)
		self.assertEqual(self._payments(obligation), [])

	def test_delivery_issues_are_copied(self):
		_stop, obligation = self._delivered(
			payment_status="Crédito", has_delivery_issues="1", delivery_issues="Faltó 1 galón"
		)
		ob = self._ob(obligation)
		self.assertEqual(ob.has_delivery_issues, 1)
		self.assertEqual(ob.delivery_issues, "Faltó 1 galón")
		# Issues never change the debt (a future adjustments flow will).
		self.assertEqual(flt(ob.outstanding_amount), flt(ob.invoice_amount))

	# =====================================================================
	# Value = commercial invoice (PDF) total, frozen
	# =====================================================================

	def test_amount_equals_commercial_invoice_total(self):
		stop_name, obligation = self._delivered(payment_status="Crédito")
		ob = self._ob(obligation)
		pl = frappe.get_doc("Pick List", ob.pick_list)
		so = frappe.get_doc("Sales Order", ob.sales_order)
		_lines, totals = facturacion._build_invoice_lines_and_totals(pl, so)
		self.assertEqual(flt(ob.invoice_amount), flt(totals["total"]))
		frozen = sum(flt(r.picked_qty) * flt(r.fg_invoice_rate) for r in pl.locations if flt(r.picked_qty) > 0)
		self.assertEqual(flt(ob.invoice_amount), flt(frozen, 2))
		self.assertEqual(ob.amount_source, "Factura comercial")

	def test_amount_is_frozen_after_creation(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		before = flt(self._ob(obligation).invoice_amount)
		row = frappe.get_all("Pick List Item", filters={"parent": self._ob(obligation).pick_list}, pluck="name")[0]
		frappe.db.set_value("Pick List Item", row, "fg_invoice_rate", 999999)
		self.assertEqual(flt(self._ob(obligation).invoice_amount), before)

	def test_unavailable_amount_creates_por_validar_without_leaking_messages(self):
		def refuse(pl, so):
			frappe.throw("factura parcial con impuestos", facturacion.InvoicePdfNotEligibleError)

		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		frappe.local.message_log = []
		with patch.object(facturacion, "_build_invoice_lines_and_totals", side_effect=refuse):
			self._deliver(route["name"], stop_name, payment_status="Crédito")
		self.assertEqual(frappe.local.message_log, [])
		ob = self._ob(self._obligation_of(stop_name))
		self.assertEqual(ob.amount_source, "Sin valor calculable")
		self.assertEqual(ob.status, "Por Validar")
		self.assertEqual(flt(ob.invoice_amount), 0)
		self.assertEqual(flt(ob.outstanding_amount), 0)
		with self.assertRaisesRegex(frappe.ValidationError, "valor facturado"):
			self._new_payment(ob.name, 100)

	# =====================================================================
	# Idempotency / uniqueness / concurrency / failure isolation
	# =====================================================================

	def test_one_obligation_per_stop(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, payment_status="Crédito")
		first = self._obligation_of(stop_name)
		self._deliver(route["name"], stop_name, payment_status="Crédito")  # already_completed
		self.assertEqual(cartera_service.ensure_obligation_for_stop(stop_name), (first, False))
		cartera_service.sync_missing_obligations()
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": stop_name}), 1)

	def test_unique_index_rejects_direct_duplicate(self):
		stop_name, _obligation = self._delivered(payment_status="Crédito")
		duplicate = frappe.get_doc({"doctype": "Cartera Obligacion", "recorrido_parada": stop_name})
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			duplicate.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": stop_name}), 1)

	def test_concurrent_creation_yields_one_obligation(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with patch.object(cartera_service, "ensure_obligation_after_delivery", return_value=None):
			self._deliver(route["name"], stop_name, payment_status="Crédito")
		self.assertIsNone(self._obligation_of(stop_name))
		frappe.db.commit()

		site = frappe.local.site
		results = {}

		def attempt(key):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user("Administrator")
			try:
				# Caller-level retry, exactly what deliver_stop()'s own
				# @_retrying_on_deadlock does: a concurrent insert may lose on
				# the naming-series row (tabSeries) and must simply retry.
				for _attempt in range(3):
					try:
						results[key] = ("ok", cartera_service.ensure_obligation_for_stop(stop_name))
						frappe.db.commit()
						break
					except frappe.QueryDeadlockError:
						frappe.db.rollback()
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=attempt, args=(k,)) for k in ("a", "b")]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		obligation = self._obligation_of(stop_name)
		self._track_obligation(obligation)

		outcomes = [results.get("a"), results.get("b")]
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual({o[1][0] for o in outcomes}, {obligation})
		self.assertEqual(sorted(o[1][1] for o in outcomes), [False, True])
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": stop_name}), 1)

	def test_cartera_failure_never_blocks_delivery_and_sync_recovers(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		frappe.local.message_log = []
		with patch.object(cartera_service, "derive_obligation_values", side_effect=RuntimeError("cartera caída")):
			result = self._deliver(
				route["name"],
				stop_name,
				payment_status="Crédito",
				payment_note="Crédito 30 días",
				has_delivery_issues="1",
				delivery_issues="Faltó 1 galón",
			)
		self.assertFalse(result["already_completed"])
		# The whole delivery committed: evidence, status, audit, driver report.
		stop = frappe.get_doc("Recorrido Parada", stop_name)
		self.assertEqual(stop.status, "Entregado")
		self.assertTrue(stop.delivered_on)
		self.assertEqual(stop.delivered_by, self.recorrido_user)
		self.assertTrue(stop.delivery_photo)
		self.assertTrue(stop.customer_signature)
		self.assertEqual(stop.payment_status, "Crédito")
		self.assertEqual(stop.payment_note, "Crédito 30 días")
		self.assertEqual(stop.has_delivery_issues, 1)
		self.assertEqual(stop.delivery_issues, "Faltó 1 galón")
		self.assertEqual(
			frappe.db.count(
				"File",
				{"attached_to_doctype": "Recorrido Parada", "attached_to_name": stop_name, "file_url": ["in", [stop.delivery_photo, stop.customer_signature]]},
			),
			2,
		)
		self.assertIsNone(self._obligation_of(stop_name))
		self.assertEqual(frappe.local.message_log, [])
		error_log = frappe.db.get_value("Error Log", {"reference_name": stop_name, "method": ["like", "Cartera:%"]}, "name")
		self.assertTrue(error_log)
		# The failure above is simulated: its Error Log is a test artifact.
		frappe.delete_doc("Error Log", error_log, ignore_permissions=True, force=True)

		summary = cartera_service.sync_missing_obligations()
		self._track_obligation(self._obligation_of(stop_name))
		self.assertIn(self._obligation_of(stop_name), summary["obligations"])
		self.assertEqual(self._ob(self._obligation_of(stop_name)).status, "Pendiente")
		self.assertEqual(cartera_service.sync_missing_obligations()["created"], 0)
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": stop_name}), 1)

	def test_only_delivered_stops_originate_obligations(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaises(cartera_service.ObligationSourceError):
			cartera_service.ensure_obligation_for_stop(stop_name)
		self.assertNotIn(stop_name, cartera_service.delivered_stops_without_obligation())
		self.assertIsNone(self._obligation_of(stop_name))

	def test_insert_derives_everything_ignoring_supplied_values(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with patch.object(cartera_service, "ensure_obligation_after_delivery", return_value=None):
			self._deliver(route["name"], stop_name, payment_status="Crédito")
		doc = frappe.get_doc(
			{
				"doctype": "Cartera Obligacion",
				"recorrido_parada": stop_name,
				"invoice_amount": 1,
				"customer": self.customer.name,
				"due_date": "2099-01-01",
				"status": "Pagado",
				"paid_amount": 999,
			}
		)
		doc.insert(ignore_permissions=True)
		self._track_obligation(doc.name)
		self.assertNotEqual(flt(doc.invoice_amount), 1)
		self.assertEqual(doc.customer, frappe.db.get_value("Recorrido Parada", stop_name, "customer"))
		self.assertNotEqual(doc.customer, self.customer.name)
		self.assertNotEqual(str(doc.due_date), "2099-01-01")
		self.assertEqual(doc.status, "Pendiente")
		self.assertEqual(flt(doc.paid_amount), 0)

	# =====================================================================
	# Immutability
	# =====================================================================

	def test_obligation_fields_are_immutable(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		original = self._ob(obligation)
		changes = (
			("invoice_amount", 1),
			("customer", "_Test Customer"),
			("due_date", "2099-01-01"),
			("credit_days", 90),
			("delivered_on", "2020-01-01 10:00:00"),
			("recorrido_parada", "otra"),
			("payment_verification", "Confirmado"),
			("payment_verified_by", "Administrator"),
			("payment_verified_on", "2026-01-01 10:00:00"),
			("payment_rejection_reason", "motivo inventado"),
			("sales_invoice", "ACC-SINV-X"),
			("status", "Pagado"),
			("paid_amount", 1),
			("outstanding_amount", 0),
			("paid_on", "2026-01-01"),
		)
		# 27.3: no role holds write any more (PermissionError)...
		for user in (self.system_manager_user, self.cartera_user):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					frappe.client.set_value("Cartera Obligacion", obligation, "invoice_amount", 1)
		# ...and the controller refuses every change even for Administrator,
		# who bypasses DocPerms.
		for field, value in changes:
			with fx.as_user("Administrator"):
				with self.assertRaises(frappe.ValidationError, msg=field):
					frappe.client.set_value("Cartera Obligacion", obligation, field, value)
		after = self._ob(obligation)
		for field, _value in changes:
			self.assertEqual(str(after.get(field)), str(original.get(field)), field)

	def test_obligation_cannot_be_deleted(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		# No role holds delete (PermissionError) and on_trash refuses anyway
		# (ValidationError) -- Administrator, who bypasses DocPerms, proves it.
		for user in (self.system_manager_user, "Administrator"):
			with fx.as_user(user):
				with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=user):
					frappe.delete_doc("Cartera Obligacion", obligation)
		self.assertTrue(frappe.db.exists("Cartera Obligacion", obligation))

	# =====================================================================
	# Payments -- partial, total, overpayment, validations, concurrency
	# =====================================================================

	def test_partial_then_full_payment(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		total = flt(self._ob(obligation).invoice_amount)
		part = flt(total * 0.4, 2)

		first = self._new_payment(obligation, part)
		self.assertEqual(first.recorded_by, self.cartera_user)
		self.assertEqual(first.company, get_default_company())
		ob = self._ob(obligation)
		self.assertEqual(ob.status, "Pendiente")
		self.assertEqual(flt(ob.paid_amount), part)
		self.assertEqual(flt(ob.outstanding_amount), flt(total - part, 2))

		self._new_payment(obligation, flt(total - part, 2), user=self.cartera_user_b, payment_method="Efectivo")
		ob = self._ob(obligation)
		self.assertEqual(ob.status, "Pagado")
		self.assertEqual(flt(ob.outstanding_amount), 0)
		self.assertEqual(getdate(ob.paid_on), getdate(nowdate()))

	def test_overpayment_rejected(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		total = flt(self._ob(obligation).invoice_amount)
		with self.assertRaisesRegex(frappe.ValidationError, "supera el saldo"):
			self._new_payment(obligation, total + 1)
		self._new_payment(obligation, flt(total / 2, 2))
		with self.assertRaisesRegex(frappe.ValidationError, "supera el saldo"):
			self._new_payment(obligation, total)
		self.assertEqual(self._ob(obligation).status, "Pendiente")

	def test_payment_validations(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		total = flt(self._ob(obligation).invoice_amount)
		cases = (
			({"amount": 0}, "mayor que cero"),
			({"amount": -5}, "mayor que cero"),
			({"amount": "12.345"}, "2 decimales"),
			({"payment_date": add_days(nowdate(), 1)}, "futuro"),
			({"payment_method": ""}, "medio de pago"),
			({"payment_method": "Bitcoin"}, "medio de pago"),
		)
		for fields, message in cases:
			values = {"amount": flt(total / 4, 2), **fields}
			with self.assertRaisesRegex(frappe.ValidationError, message, msg=str(fields)):
				self._new_payment(obligation, values.pop("amount"), **values)
		# source/accounting_status are never taken from a caller: the service
		# has no such parameters, and a direct document is refused outright.
		for fields in ({"source": "Conductor"}, {"accounting_status": "Contabilizado"}):
			with fx.as_user("Administrator"):
				with self.assertRaises(frappe.PermissionError, msg=str(fields)):
					self._direct_payment(obligation, flt(total / 4, 2), **fields).insert()
		self.assertEqual(flt(self._ob(obligation).paid_amount), 0)

	def test_recorded_by_is_always_the_server_user(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		payment = self._new_payment(obligation, flt(flt(self._ob(obligation).invoice_amount) / 4, 2))
		self.assertEqual(payment.recorded_by, self.cartera_user)
		self.assertTrue(payment.recorded_on)
		# The only entry point takes no recorded_by/recorded_on/source/company.
		import inspect

		params = set(inspect.signature(cartera_api.register_payment).parameters)
		self.assertFalse(params & {"recorded_by", "recorded_on", "source", "company", "customer", "currency", "accounting_status"})

	def test_payment_cannot_be_deleted_nor_cancelled_by_cartera(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		payment = self._new_payment(obligation, flt(flt(self._ob(obligation).invoice_amount) / 4, 2))
		# 27.3: nobody cancels directly -- Cartera, System Manager nor
		# Administrator (the controller refuses without the service token).
		for user in (self.cartera_user, self.system_manager_user, "Administrator"):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					frappe.get_doc("Cartera Pago", payment.name).cancel()
		for user in (self.cartera_user, self.system_manager_user, "Administrator"):
			with fx.as_user(user):
				with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=user):
					frappe.delete_doc("Cartera Pago", payment.name)
		self.assertEqual(frappe.db.get_value("Cartera Pago", payment.name, "docstatus"), 1)

	def test_cancel_restores_balance_only_through_the_service(self):
		"""27.3: a direct cancel (System Manager, frappe.client) is refused and
		changes nothing; the service's rejection of the driver payment is the
		cancel path, and on_cancel re-derives the balance from what is left."""
		_stop, obligation = self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg())
		self._track_obligation(obligation)
		total = flt(self._ob(obligation).invoice_amount)
		payment = frappe.get_all("Cartera Pago", filters={"cartera_obligacion": obligation}, pluck="name")[0]
		self.assertEqual(self._ob(obligation).status, "Pagado")
		with fx.as_user(self.system_manager_user):
			with self.assertRaises(frappe.PermissionError):
				frappe.client.cancel("Cartera Pago", payment)
		self.assertEqual(self._ob(obligation).status, "Pagado")
		with fx.as_user(self.cartera_user):
			cartera_service.reject_driver_payment(obligation, "No llegó el dinero")
		ob = self._ob(obligation)
		self.assertEqual(ob.status, "Pendiente")
		self.assertEqual(flt(ob.outstanding_amount), total)
		self.assertIsNone(ob.paid_on)
		self.assertEqual(frappe.db.get_value("Cartera Pago", payment, "docstatus"), 2)  # history kept

	def test_client_request_id_is_unique(self):
		"""27.3: the unique index stays the last guarantee; the service turns a
		repeated id into the same payment (or an explicit conflict)."""
		_stop, obligation = self._delivered(payment_status="Crédito")
		quarter = flt(flt(self._ob(obligation).invoice_amount) / 4, 2)
		request_id = str(uuid.uuid4())
		first = self._new_payment(obligation, quarter, request_id=request_id)
		again = self._new_payment(obligation, quarter, request_id=request_id)
		self.assertEqual(again.name, first.name)
		with self.assertRaises(cartera_service.CarteraRequestConflictError):
			self._new_payment(obligation, flt(quarter / 2, 2), request_id=request_id)
		self.assertEqual(frappe.db.count("Cartera Pago", {"client_request_id": f"cartera:{request_id}"}), 1)
		# The DB index itself still refuses a duplicate that bypasses the check.
		clone = self._direct_payment(obligation, quarter, client_request_id=f"cartera:{request_id}")
		cartera_service._authorize(clone, cartera_service.ACTION_CARTERA_PAYMENT)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			clone.insert(ignore_permissions=True)

	def test_concurrent_payments_never_overpay(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		total = flt(self._ob(obligation).invoice_amount)
		share = flt(total * 0.6, 2)
		frappe.db.commit()

		site = frappe.local.site
		results = {}

		def attempt(key, user_email):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user_email)
			try:
				# 27.3: the REGISTRAR COBRO transaction (with its bounded
				# retry on deadlock), exactly as the endpoint runs it.
				result = cartera_api._register_payment_tx(
					obligation, share, nowdate(), "Transferencia", None, None, str(uuid.uuid4()), None
				)
				frappe.db.commit()
				results[key] = ("ok", result["payment"])
			except frappe.ValidationError as e:
				frappe.db.rollback()
				results[key] = ("error", str(e))
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		threads = [
			threading.Thread(target=attempt, args=("a", self.cartera_user)),
			threading.Thread(target=attempt, args=("b", self.cartera_user_b)),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		self._track_obligation(obligation)

		outcomes = [results.get("a"), results.get("b")]
		self.assertEqual(sorted(o[0] for o in outcomes), ["error", "ok"], outcomes)
		self.assertIn("supera el saldo", next(o[1] for o in outcomes if o[0] == "error"))
		ob = self._ob(obligation)
		self.assertEqual(flt(ob.paid_amount), share)
		self.assertEqual(flt(ob.outstanding_amount), flt(total - share, 2))

	# =====================================================================
	# Permissions / company isolation / endpoint
	# =====================================================================

	def test_role_permissions(self):
		_stop, obligation = self._delivered(payment_status="Crédito")
		ob = self._ob(obligation)
		self.assertTrue(frappe.has_permission("Cartera Obligacion", "read", doc=ob, user=self.cartera_user))
		self.assertTrue(frappe.has_permission("Cartera Obligacion", "read", doc=ob, user=self.system_manager_user))
		for user in (self.recorrido_user, self.no_role_user, self.facturacion_user):
			self.assertFalse(frappe.has_permission("Cartera Obligacion", "read", doc=ob, user=user), user)
			self.assertFalse(frappe.has_permission("Cartera Pago", "create", user=user), user)
		self.assertFalse(frappe.has_permission("Cartera Obligacion", "create", user=self.cartera_user))
		self.assertFalse(frappe.has_permission("Cartera Obligacion", "delete", user=self.cartera_user))
		self.assertFalse(frappe.has_permission("Cartera Pago", "cancel", user=self.cartera_user))
		self.assertFalse(frappe.has_permission("Cartera Pago", "delete", user=self.cartera_user))
		# Cartera has no grant on the operational documents.
		for doctype in ("Sales Order", "Pick List", "Recorrido", "Recorrido Parada", "Customer"):
			self.assertFalse(frappe.has_permission(doctype, "write", user=self.cartera_user), doctype)

	def test_company_isolation(self):
		foreign = frappe._dict(doctype="Cartera Obligacion", company="_Test Company")
		own = frappe._dict(doctype="Cartera Obligacion", company=get_default_company())
		self.assertFalse(cartera_company_has_permission(foreign, "read", user=self.cartera_user))
		self.assertTrue(cartera_company_has_permission(own, "read", user=self.cartera_user))
		self.assertTrue(cartera_company_has_permission(foreign, "read", user=self.system_manager_user))
		condition = cartera_obligacion_permission_query_conditions(user=self.cartera_user)
		self.assertIn(get_default_company(), condition)
		self.assertNotIn("_Test Company'", condition)

		_stop, obligation = self._delivered(payment_status="Crédito")
		with fx.as_user(self.cartera_user):
			names = frappe.get_list("Cartera Obligacion", pluck="name", limit_page_length=0)
		self.assertIn(obligation, names)

	def test_sync_endpoint_roles(self):
		self.assertIn(cartera_api.sync_missing_obligations, frappe.whitelisted)
		for user in (self.recorrido_user, self.no_role_user):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError):
					cartera_api.sync_missing_obligations()
		with fx.as_user(self.cartera_user):
			result = cartera_api.sync_missing_obligations()
		self.assertIn("created", result)
		for obligation in result["obligations"]:
			self._track_obligation(obligation)

	# =====================================================================
	# Integrity -- Cartera is operational only
	# =====================================================================

	def test_no_accounting_or_operational_documents_touched(self):
		counts = ("Payment Entry", "Sales Invoice", "Journal Entry", "GL Entry", "Delivery Note", "Stock Ledger Entry")
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		pl_name = route["stops"][0]["pick_list"]
		so_name = frappe.db.get_value("Recorrido Parada", stop_name, "sales_order")
		before_counts = {dt: frappe.db.count(dt) for dt in counts}
		pl_before = frappe.db.get_value("Pick List", pl_name, ["modified", "status", "fg_invoicing_status"])
		pl_items_fields = ["name", "modified", "item_code", "qty", "picked_qty", "fg_invoice_rate"]
		pl_items_before = frappe.get_all("Pick List Item", filters={"parent": pl_name}, fields=pl_items_fields, order_by="name")
		bins_fields = ["name", "modified", "actual_qty", "reserved_qty", "projected_qty"]
		bins_before = frappe.get_all("Bin", filters={"warehouse": self.wh.name}, fields=bins_fields, order_by="name")
		so_before = frappe.db.get_value("Sales Order", so_name, ["modified", "status", "per_billed", "per_delivered"])

		self._deliver(route["name"], stop_name, payment_status="Crédito")
		obligation = self._obligation_of(stop_name)
		total = flt(self._ob(obligation).invoice_amount)
		self._new_payment(obligation, flt(total / 2, 2))
		self._new_payment(obligation, flt(total - flt(total / 2, 2), 2))

		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, before_counts)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, ["modified", "status", "fg_invoicing_status"]), pl_before)
		self.assertEqual(
			frappe.get_all("Pick List Item", filters={"parent": pl_name}, fields=pl_items_fields, order_by="name"),
			pl_items_before,
		)
		self.assertEqual(
			frappe.get_all("Bin", filters={"warehouse": self.wh.name}, fields=bins_fields, order_by="name"), bins_before
		)
		self.assertEqual(
			frappe.db.get_value("Sales Order", so_name, ["modified", "status", "per_billed", "per_delivered"]), so_before
		)
		for payment in self._payments(obligation):
			self.assertEqual(frappe.db.get_value("Cartera Pago", payment.name, "accounting_status"), "Sin contabilizar")

	def test_vencido_is_never_a_stored_status(self):
		options = frappe.get_meta("Cartera Obligacion").get_field("status").options.split("\n")
		self.assertEqual(options, ["Por Validar", "Pendiente", "Pagado", "Anulada"])
		self.assertEqual(frappe.get_meta("Cartera Obligacion").get_field("due_date").fieldtype, "Date")

	def test_role_fixture_and_hooks(self):
		app = frappe.get_app_path("fabergray_erp")
		with open(os.path.join(app, "fixtures", "role.json"), encoding="utf-8") as f:
			self.assertIn("Cartera", [r["name"] for r in json.load(f)])
		hooks = frappe.get_hooks()
		for entry in frappe.get_hooks("fixtures", app_name="fabergray_erp"):
			if isinstance(entry, dict) and entry.get("dt") in ("Role", "Custom DocPerm") and entry.get("filters"):
				roles = entry["filters"][0][2]
				if "Recorrido" in roles:
					self.assertIn("Cartera", roles, entry["dt"])
		self.assertIn(
			"fabergray_erp.cartera_service.scheduled_sync_missing_obligations",
			hooks.get("scheduler_events", {}).get("cron", {}).get("*/15 * * * *", []),
		)
