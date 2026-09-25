# -*- coding: utf-8 -*-
"""Fase 27.3 -- Cobros + validación del pago del conductor.

REGISTRAR COBRO (api.cartera.register_payment / cartera_service.
register_cartera_payment), idempotency, concurrency (H1: payments of other
obligations are never locked), the Cartera proof upload + get_payment_proof
(IDOR), CONFIRMAR / RECHAZAR the driver's payment or report, the Desk
lock-down of Cartera Pago/Obligacion (H2/H3/H4/H5), KPIs after every action
and ERP integrity.

Obligations are born from REAL deliveries (api.recorridos.deliver_stop()),
reusing the Recorridos/Cartera helpers -- borrowed, never inherited."""

import base64
import io
import os
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate
from PIL import Image

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.api import facturacion
from fabergray_erp.api import recorridos
from fabergray_erp.fabrigray_erp.doctype.cartera_pago import cartera_pago as pago_controller
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_cartera_foundation as foundation
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base
from fabergray_erp.tests.test_recorridos_deliver_stop import _photo_jpeg, _uploads

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

XSS = '<img src=x onerror="alert(1)">'


def _image(fmt, size=(900, 600), exif=None):
	image = Image.new("RGB", size, (int(time.time() * 1000) % 255, 120, 40))
	output = io.BytesIO()
	kwargs = {"exif": exif.tobytes()} if exif is not None else {}
	image.save(output, format=fmt, **kwargs)
	return output.getvalue()


class TestCarteraCobros(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG273 WH")
		cls.item = cls.world.item("FG273-ITEM")
		cls.customer = cls.world.customer("FG273 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg273-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg273-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg273-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg273-recorrido-b@example.com", ["Recorrido"])
		cls.cartera_user = cls.world.user("fg273-cartera@example.com", ["Cartera"], full_name="Ana Cartera")
		cls.cartera_user_b = cls.world.user("fg273-cartera-b@example.com", ["Cartera"])
		cls.vendedora_user = cls.world.user("fg273-vendedora@example.com", ["Vendedora"])
		cls.no_role_user = cls.world.user("fg273-norole@example.com", [])
		cls.system_manager_user = cls.world.user("fg273-sysmanager@example.com", ["System Manager"])
		cls.denied_users = (cls.no_role_user, cls.vendedora_user, cls.bodega_user, cls.facturacion_user, cls.recorrido_user)
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
	_obligation_of = foundation.TestCarteraFoundation._obligation_of
	_ob = foundation.TestCarteraFoundation._ob
	_track_obligation = foundation.TestCarteraFoundation._track_obligation

	# -- Local helpers -----------------------------------------------------------
	def _delivered(self, **report):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, **report)
		obligation = self._obligation_of(stop_name)
		self._track_obligation(obligation)
		return obligation

	def _credit(self):
		return self._delivered(payment_status="Crédito")

	def _paid_with_proof(self):
		return self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg())

	def _paid_without_proof(self):
		return self._delivered(payment_status="Pagado")

	def _register(self, obligation, amount, user=None, request_id=None, proof=None, **fields):
		"""Through the real endpoint (multipart when a proof is given)."""
		values = {
			"payment_date": nowdate(),
			"payment_method": "Transferencia",
			"reference": None,
			"notes": None,
		}
		values.update(fields)
		with fx.as_user(user or self.cartera_user):
			with _uploads(payment_proof=proof):
				res = cartera_api.register_payment(
					obligation,
					amount=amount,
					client_request_id=request_id or str(uuid.uuid4()),
					**values,
				)
		self._track_payments(obligation)
		return res

	def _track_payments(self, obligation):
		for name in frappe.get_all("Cartera Pago", filters={"cartera_obligacion": obligation}, pluck="name"):
			if ("Cartera Pago", name) not in self.world._created:
				self.world.track_existing("Cartera Pago", name)
		for file_name in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Cartera Pago",
				"attached_to_name": ["in", frappe.get_all("Cartera Pago", filters={"cartera_obligacion": obligation}, pluck="name") or [""]],
			},
			pluck="name",
		):
			if file_name not in self._evidence_file_names:
				self._evidence_file_names.append(file_name)

	def _confirm(self, obligation, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.confirm_driver_payment(obligation)

	def _reject(self, obligation, reason="El dinero no llegó a la empresa", user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.reject_driver_payment(obligation, reason=reason)

	def _payments(self, obligation, **filters):
		return frappe.get_all(
			"Cartera Pago",
			filters={"cartera_obligacion": obligation, **filters},
			fields=["name", "amount", "docstatus", "source", "payment_proof", "cancellation_reason", "modified_by", "client_request_id"],
			order_by="creation asc",
		)

	def _kpis(self):
		with fx.as_user(self.cartera_user):
			k = cartera_api.get_dashboard()["kpis"]
		return {key: (flt(v["amount"], 2), v["count"]) for key, v in k.items()}

	def _thread(self, target, results, key, user, *args):
		site = frappe.local.site

		def run():
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user)
			try:
				results[key] = ("ok", target(*args))
				frappe.db.commit()
			except frappe.ValidationError as e:
				frappe.db.rollback()
				results[key] = ("error", f"{type(e).__name__}: {e}")
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		return threading.Thread(target=run)

	def _reconnect(self):
		frappe.init(site=frappe.local.site)
		frappe.connect()
		frappe.set_user("Administrator")

	# =====================================================================
	# REGISTRAR COBRO
	# =====================================================================

	def test_partial_then_total_then_zero_balance(self):
		ob = self._credit()
		total = flt(self._ob(ob).invoice_amount, 2)
		part = flt(total * 0.4, 2)
		res = self._register(ob, part)
		self.assertFalse(res["result"]["already_registered"])
		self.assertEqual(res["result"]["previous_outstanding"], total)
		self.assertEqual(res["result"]["new_outstanding"], flt(total - part, 2))
		self.assertFalse(res["result"]["is_full_payment"])
		self.assertEqual(res["detail"]["status"], "Pendiente")
		self.assertEqual(res["detail"]["outstanding_amount"], flt(total - part, 2))
		self.assertIn("kpis", res["dashboard"])

		res = self._register(ob, flt(total - part, 2), user=self.cartera_user_b, payment_method="Efectivo")
		self.assertTrue(res["result"]["is_full_payment"])
		doc = self._ob(ob)
		self.assertEqual((doc.status, flt(doc.paid_amount, 2), flt(doc.outstanding_amount)), ("Pagado", total, 0))
		self.assertEqual(getdate(doc.paid_on), getdate(nowdate()))
		self.assertFalse(res["detail"]["can_register_payment"])

		# Saldo cero: any further payment is refused.
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "saldo pendiente"):
			self._register(ob, 1)
		self.assertEqual(len(self._payments(ob, docstatus=1)), 2)

	def test_payment_rows_are_server_derived(self):
		ob = self._credit()
		quarter = flt(flt(self._ob(ob).invoice_amount) / 4, 2)
		res = self._register(ob, quarter, reference=f"TRX {XSS} 123", notes=f"Nota {XSS}")
		payment = frappe.get_doc("Cartera Pago", res["result"]["payment"])
		self.assertEqual(payment.source, "Cartera")
		self.assertEqual(payment.company, get_default_company())
		self.assertEqual(payment.customer, self._ob(ob).customer)
		self.assertEqual(payment.currency, "COP")
		self.assertEqual(payment.recorded_by, self.cartera_user)
		self.assertEqual(payment.accounting_status, "Sin contabilizar")
		self.assertEqual(payment.docstatus, 1)
		self.assertTrue(payment.client_request_id.startswith("cartera:"))
		self.assertNotIn("<", payment.reference)
		self.assertNotIn("<", payment.notes)
		self.assertIn("123", payment.reference)

		# Economic fields sent by a client are ignored by the real dispatcher.
		with fx.as_user(self.cartera_user):
			res = self._execute_cmd(
				"register_payment",
				obligation_name=ob,
				amount=str(quarter),
				payment_date=nowdate(),
				payment_method="Efectivo",
				client_request_id=str(uuid.uuid4()),
				source="Conductor",
				company="_Test Company",
				recorded_by="Administrator",
				accounting_status="Contabilizado",
				customer="_Test Customer",
				currency="USD",
			)
		self._track_payments(ob)
		payment = frappe.get_doc("Cartera Pago", res["result"]["payment"])
		self.assertEqual(
			(payment.source, payment.company, payment.recorded_by, payment.accounting_status, payment.currency),
			("Cartera", get_default_company(), self.cartera_user, "Sin contabilizar", "COP"),
		)

	def test_input_validations(self):
		ob = self._credit()
		total = flt(self._ob(ob).invoice_amount, 2)
		cases = (
			({"amount": "0"}, "mayor que cero"),
			({"amount": "-10"}, "mayor que cero"),
			({"amount": "12.345"}, "2 decimales"),
			({"amount": "abc"}, "número válido"),
			({"amount": ""}, "obligatorio"),
			({"amount": str(total + 1)}, "supera el saldo"),
			({"payment_date": add_days(nowdate(), 1)}, "futuro"),
			({"payment_date": "2026-13-45"}, "no es válida"),
			({"payment_method": "Bitcoin"}, "medio de pago"),
			({"payment_method": ""}, "medio de pago"),
			({"reference": "x" * 141}, "140"),
			({"notes": "x" * 501}, "500"),
		)
		for fields, message in cases:
			values = {"amount": str(flt(total / 4, 2)), **fields}
			with self.assertRaisesRegex(frappe.ValidationError, message, msg=str(fields)):
				self._register(ob, values.pop("amount"), **values)
		for bad_id in ("", "no-es-uuid", "conductor:REC-PAR-1", "cartera:" + str(uuid.uuid4())):
			with self.assertRaisesRegex(frappe.ValidationError, "solicitud", msg=bad_id):
				self._register(ob, "10", request_id=bad_id or " ")
		# Exactly 2 decimals and a date before the delivery are accepted.
		self._register(ob, "10.55", payment_date=add_days(self._ob(ob).delivery_date, -3))
		self.assertEqual(flt(self._ob(ob).paid_amount, 2), 10.55)

	def test_unknown_amount_and_unresolved_reports_block_payments(self):
		def refuse(pl, so):
			frappe.throw("factura parcial con impuestos", facturacion.InvoicePdfNotEligibleError)

		route = self._en_ruta()
		stop = route["stops"][0]["name"]
		with patch.object(facturacion, "_build_invoice_lines_and_totals", side_effect=refuse):
			self._deliver(route["name"], stop, payment_status="Crédito")
		unknown = self._obligation_of(stop)
		self._track_obligation(unknown)
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "valor facturado"):
			self._register(unknown, "10")

		# Pagado + comprobante (saldo 0, Sin confirmar) and Pagado sin
		# comprobante (Por Validar, saldo completo): both must be resolved first.
		with_proof = self._paid_with_proof()
		without_proof = self._paid_without_proof()
		for ob in (with_proof, without_proof):
			self.assertFalse(self._detail(ob)["can_register_payment"])
			with self.assertRaisesRegex(cartera_service.CarteraStateError, "Confirma o rechaza"):
				self._register(ob, "10")
		self.assertEqual(self._payments(without_proof), [])

	# =====================================================================
	# Idempotency
	# =====================================================================

	def test_same_request_id_same_data_returns_the_same_payment(self):
		ob = self._credit()
		quarter = str(flt(flt(self._ob(ob).invoice_amount) / 4, 2))
		request_id = str(uuid.uuid4())
		first = self._register(ob, quarter, request_id=request_id, proof=_photo_jpeg(), reference="R-1")
		# Double click / HTTP retry: same id and data (even re-sending the image).
		again = self._register(ob, quarter, request_id=request_id, proof=_photo_jpeg(), reference="R-1")
		self.assertTrue(again["result"]["already_registered"])
		self.assertEqual(again["result"]["payment"], first["result"]["payment"])
		self.assertEqual(len(self._payments(ob)), 1)
		self.assertEqual(
			frappe.db.count("File", {"attached_to_doctype": "Cartera Pago", "attached_to_name": first["result"]["payment"]}), 1
		)
		self.assertEqual(flt(self._ob(ob).paid_amount, 2), flt(quarter, 2))
		stored = frappe.db.get_value("Cartera Pago", first["result"]["payment"], "client_request_id")
		self.assertEqual(stored, f"cartera:{request_id}")

		# Same id, other data -> explicit conflict, nothing written.
		for other in (
			{"reference": "R-2"},
			{"reference": "R-1", "payment_method": "Efectivo"},
			{"reference": "R-1", "notes": "otra"},
			{"reference": "R-1", "payment_date": add_days(nowdate(), -1)},
		):
			with self.assertRaises(cartera_service.CarteraRequestConflictError, msg=str(other)):
				self._register(ob, quarter, request_id=request_id, **other)
		with self.assertRaises(cartera_service.CarteraRequestConflictError):
			self._register(ob, "1", request_id=request_id, reference="R-1")
		# The same id on ANOTHER obligation is also a conflict.
		other_ob = self._credit()
		with self.assertRaises(cartera_service.CarteraRequestConflictError):
			self._register(other_ob, quarter, request_id=request_id, reference="R-1")
		self.assertEqual(len(self._payments(ob)), 1)
		self.assertEqual(self._payments(other_ob), [])

	def test_unique_race_returns_the_winner_as_idempotent(self):
		ob = self._credit()
		quarter = flt(flt(self._ob(ob).invoice_amount) / 4, 2)
		request_id = str(uuid.uuid4())
		frappe.db.commit()

		results = {}

		def call():
			return cartera_api._register_payment_tx(ob, quarter, nowdate(), "Transferencia", None, None, request_id, None)

		threads = [self._thread(call, results, k, self.cartera_user) for k in ("a", "b")]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		self._reconnect()
		self._track_payments(ob)

		outcomes = [results.get("a"), results.get("b")]
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual({o[1]["payment"] for o in outcomes}, {self._payments(ob)[0].name})
		self.assertEqual(sorted(o[1]["already_registered"] for o in outcomes), [False, True])
		self.assertEqual(len(self._payments(ob)), 1)

		# The service-level savepoint path: a duplicate insert on the unique
		# index (the lookup "missed" it) resolves to the stored payment.
		with patch.object(cartera_service, "_payment_by_request_id", side_effect=[None, self._payments(ob)[0].name]):
			with fx.as_user(self.cartera_user):
				res = cartera_service.register_cartera_payment(ob, quarter, nowdate(), "Transferencia", client_request_id=request_id)
		self.assertTrue(res["already_registered"])
		self.assertEqual(len(self._payments(ob)), 1)

	# =====================================================================
	# Concurrency (H1)
	# =====================================================================

	def test_payment_index_and_locking_read_are_per_obligation(self):
		indexes = frappe.db.sql(
			"""SELECT COLUMN_NAME FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabCartera Pago' AND COLUMN_NAME = 'cartera_obligacion'"""
		)
		self.assertTrue(indexes)
		plan = frappe.db.sql(
			"EXPLAIN SELECT amount, payment_date FROM `tabCartera Pago` WHERE cartera_obligacion = %s AND docstatus = 1 FOR UPDATE",
			("X",),
			as_dict=True,
		)[0]
		self.assertNotEqual(plan.type, "ALL")
		self.assertEqual(plan.key, "cartera_obligacion_index")

	def test_two_obligations_never_block_each_other(self):
		"""H1. A registers a payment and keeps its transaction OPEN (all its
		row locks held). Meanwhile B takes EVERY lock the payment flow takes
		for ANOTHER obligation -- the obligation row, the locking read of its
		submitted payments and of its driver payments -- with a 3 s lock
		timeout: all must be granted at once. Before the index, the payments
		read scanned the whole table, hit A's new row and waited.

		The one shared lock left is Frappe's own naming series row
		(tabSeries "CPAG-YYYY-"), taken by every insert until commit: asserted
		here explicitly so the remaining wait is documented, not hidden. With
		real (millisecond) transactions it is a brief queue, never a timeout:
		part 2 runs both payments truly concurrently and both commit."""
		ob_a, ob_b = self._credit(), self._credit()
		amount_a = flt(flt(self._ob(ob_a).invoice_amount) / 2, 2)
		amount_b = flt(flt(self._ob(ob_b).invoice_amount) / 2, 2)
		frappe.db.commit()

		site = frappe.local.site
		a_registered, release_a = threading.Event(), threading.Event()
		results = {}

		def run_a():
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(self.cartera_user)
			try:
				res = cartera_api._register_payment_tx(ob_a, amount_a, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None)
				a_registered.set()
				release_a.wait(30)
				frappe.db.commit()
				results["a"] = ("ok", res)
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results["a"] = ("exception", repr(e))
				a_registered.set()
			finally:
				frappe.destroy()

		def run_b():
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 3")
			frappe.set_user(self.cartera_user_b)
			try:
				started = time.monotonic()
				cartera_service.lock_obligation(ob_b)
				cartera_service.submitted_payments(ob_b, for_update=True)
				cartera_service._active_driver_payments(ob_b)
				elapsed = time.monotonic() - started
				series = f"CPAG-{getdate(nowdate()).year}-"
				try:
					frappe.db.sql("SELECT current FROM `tabSeries` WHERE name = %s FOR UPDATE", (series,))
					series_blocked = False
				except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
					series_blocked = True
				frappe.db.rollback()
				results["b"] = ("ok", elapsed, series_blocked)
			except Exception as e:
				frappe.db.rollback()
				results["b"] = ("exception", repr(e))
			finally:
				frappe.destroy()

		thread_a = threading.Thread(target=run_a)
		thread_a.start()
		self.assertTrue(a_registered.wait(30))
		thread_b = threading.Thread(target=run_b)
		thread_b.start()
		thread_b.join(timeout=30)
		a_still_open = thread_a.is_alive() and "a" not in results
		release_a.set()
		thread_a.join(timeout=30)
		self._reconnect()
		self._track_payments(ob_a)

		self.assertTrue(a_still_open)
		self.assertEqual(results["b"][0], "ok", results.get("b"))
		self.assertLess(results["b"][1], 1.0)  # no wait on A's payment rows
		self.assertTrue(results["b"][2])  # only Frappe's naming series is shared
		self.assertEqual(results["a"][0], "ok", results.get("a"))

		# Part 2 -- two real payments on two obligations at the same time.
		ob_c = self._credit()
		frappe.db.commit()
		concurrent = {}
		threads = [
			self._thread(
				lambda: cartera_api._register_payment_tx(ob_b, amount_b, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None),
				concurrent,
				"b",
				self.cartera_user_b,
			),
			self._thread(
				lambda: cartera_api._register_payment_tx(ob_c, amount_b, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None),
				concurrent,
				"c",
				self.cartera_user,
			),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		self._reconnect()
		self._track_payments(ob_b)
		self._track_payments(ob_c)
		self.assertEqual({k: v[0] for k, v in concurrent.items()}, {"b": "ok", "c": "ok"}, concurrent)
		self.assertEqual(flt(self._ob(ob_a).paid_amount, 2), amount_a)
		self.assertEqual(flt(self._ob(ob_b).paid_amount, 2), amount_b)
		self.assertEqual(flt(self._ob(ob_c).paid_amount, 2), amount_b)

	def test_same_obligation_is_serialized_never_overpaid(self):
		ob = self._credit()
		total = flt(self._ob(ob).invoice_amount, 2)
		share = flt(total * 0.6, 2)
		frappe.db.commit()
		results = {}

		def call():
			return cartera_api._register_payment_tx(ob, share, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None)

		threads = [
			self._thread(call, results, "a", self.cartera_user),
			self._thread(call, results, "b", self.cartera_user_b),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		self._reconnect()
		self._track_payments(ob)

		outcomes = [results.get("a"), results.get("b")]
		self.assertEqual(sorted(o[0] for o in outcomes), ["error", "ok"], outcomes)
		self.assertIn("supera el saldo", next(o[1] for o in outcomes if o[0] == "error"))
		doc = self._ob(ob)
		self.assertEqual(flt(doc.paid_amount, 2), share)
		self.assertEqual(flt(doc.outstanding_amount, 2), flt(total - share, 2))

	def test_deadlock_is_retried_a_bounded_number_of_times(self):
		ob = self._credit()
		quarter = flt(flt(self._ob(ob).invoice_amount) / 4, 2)
		frappe.db.commit()
		real = cartera_service.register_cartera_payment
		calls = {"n": 0}

		def flaky(*args, **kwargs):
			calls["n"] += 1
			if calls["n"] == 1:
				raise frappe.QueryDeadlockError("deadlock simulado")
			return real(*args, **kwargs)

		with patch.object(cartera_service, "register_cartera_payment", side_effect=flaky):
			with fx.as_user(self.cartera_user):
				res = cartera_api._register_payment_tx(ob, quarter, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None)
		self._track_payments(ob)
		self.assertEqual(calls["n"], 2)
		self.assertEqual(len(self._payments(ob)), 1)
		self.assertEqual(res["payment"], self._payments(ob)[0].name)

		# A permanent deadlock is NOT hidden forever: 3 attempts, then raised.
		with patch.object(cartera_service, "register_cartera_payment", side_effect=frappe.QueryDeadlockError("x")) as always:
			with fx.as_user(self.cartera_user):
				with self.assertRaises(frappe.QueryDeadlockError):
					cartera_api._register_payment_tx(ob, quarter, nowdate(), "Efectivo", None, None, str(uuid.uuid4()), None)
		self.assertEqual(always.call_count, 3)

	# =====================================================================
	# Proof upload + get_payment_proof
	# =====================================================================

	def _payment_file(self, payment):
		return frappe.get_doc(
			"File", {"attached_to_doctype": "Cartera Pago", "attached_to_name": payment, "attached_to_field": "payment_proof"}
		)

	def test_proof_formats_are_reencoded_private_and_exactly_attached(self):
		exif = Image.Exif()
		exif[0x010F] = "FG273 Phone"
		exif[0x0112] = 6  # rotate 90 on display
		for fmt, content in (
			("JPEG", _image("JPEG", size=(900, 600), exif=exif)),
			("PNG", _image("PNG")),
			("WEBP", _image("WEBP")),
		):
			ob = self._credit()
			quarter = str(flt(flt(self._ob(ob).invoice_amount) / 4, 2))
			res = self._register(ob, quarter, proof=content)
			payment = frappe.get_doc("Cartera Pago", res["result"]["payment"])
			file_doc = self._payment_file(payment.name)
			self.assertEqual(file_doc.is_private, 1, fmt)
			self.assertEqual(payment.payment_proof, file_doc.file_url, fmt)
			self.assertTrue(file_doc.file_url.startswith("/private/files/"), fmt)
			stored = Image.open(io.BytesIO(file_doc.get_content(encodings=())))
			self.assertEqual(stored.format, "JPEG", fmt)
			self.assertEqual(dict(stored.getexif()), {}, fmt)
			if fmt == "JPEG":
				self.assertEqual(stored.size, (600, 900))  # EXIF orientation applied
			self.assertEqual(res["detail"]["payments"][0]["proof_kind"], "payment")
			with fx.as_user(self.cartera_user):
				served = cartera_api.get_payment_proof(payment.name)
			self.assertEqual(served["content_type"], "image/jpeg")
			self.assertEqual(base64.b64decode(served["data"]), file_doc.get_content(encodings=()))

	def test_invalid_proofs_are_rejected_before_anything_is_written(self):
		ob = self._credit()
		for content, message in (
			(b"%PDF-1.4 not an image", "no es una imagen"),
			(os.urandom(2048), "no es una imagen"),
			(b"\xff" * (recorridos.DELIVERY_PHOTO_MAX_BYTES + 1), "tamaño"),
			(_image("GIF"), "formato"),
		):
			with self.assertRaisesRegex(frappe.ValidationError, message):
				self._register(ob, "10", proof=content)
		self.assertEqual(self._payments(ob), [])
		self.assertEqual(flt(self._ob(ob).paid_amount), 0)

	def test_rollback_leaves_no_payment_file_row_or_file_on_disk(self):
		ob = self._credit()
		frappe.db.commit()
		created = []
		original_on_submit = pago_controller.CarteraPago.on_submit

		def failing_on_submit(doc):
			file_doc = self._payment_file(doc.name)
			created.append((doc.name, file_doc.name, file_doc.get_full_path()))
			raise RuntimeError("fallo simulado después del comprobante")

		with patch.object(pago_controller.CarteraPago, "on_submit", failing_on_submit):
			with self.assertRaises(RuntimeError):
				self._register(ob, "10", proof=_photo_jpeg())
		self.assertEqual(len(created), 1)
		payment, file_name, path = created[0]
		self.assertTrue(os.path.exists(path))
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("Cartera Pago", payment))
		self.assertFalse(frappe.db.exists("File", file_name))
		self.assertFalse(os.path.exists(path), f"orphaned file left on disk: {path}")
		self.assertEqual(flt(self._ob(ob).paid_amount), 0)
		self.assertIs(pago_controller.CarteraPago.on_submit, original_on_submit)

	def test_payment_proof_must_be_the_payments_own_private_file(self):
		"""H4: a payment_proof pointing at any other File is refused."""
		donor = self._paid_with_proof()
		foreign_url = frappe.db.get_value("Cartera Obligacion", donor, "driver_payment_proof")
		ob = self._credit()
		doc = frappe.get_doc(
			{
				"doctype": "Cartera Pago",
				"cartera_obligacion": ob,
				"amount": 10,
				"payment_date": nowdate(),
				"payment_method": "Efectivo",
				"client_request_id": f"cartera:{uuid.uuid4()}",
				"payment_proof": foreign_url,
			}
		)
		cartera_service._authorize(doc, cartera_service.ACTION_CARTERA_PAYMENT)
		with fx.as_user("Administrator"):
			with self.assertRaisesRegex(frappe.ValidationError, "Comprobante"):
				doc.insert(ignore_permissions=True)
		# An existing payment: submitted rows can't be modified at all.
		res = self._register(ob, "10", proof=_photo_jpeg())
		with fx.as_user("Administrator"):
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				frappe.client.set_value("Cartera Pago", res["result"]["payment"], "payment_proof", foreign_url)
		self.assertNotEqual(frappe.db.get_value("Cartera Pago", res["result"]["payment"], "payment_proof"), foreign_url)

	def test_get_payment_proof_idor(self):
		ob = self._credit()
		res = self._register(ob, "10", proof=_photo_jpeg())
		payment = res["result"]["payment"]
		file_doc = self._payment_file(payment)
		url = file_doc.file_url

		def refused(label, exc=frappe.PermissionError, user=None):
			with fx.as_user(user or self.cartera_user):
				with self.assertRaises(exc, msg=label):
					cartera_api.get_payment_proof(payment)

		for user in self.denied_users:
			refused(user, user=user)
		with fx.as_user(self.system_manager_user):
			self.assertEqual(cartera_api.get_payment_proof(payment)["content_type"], "image/jpeg")

		for field, bad in (("attached_to_field", "otro"), ("attached_to_name", "CPAG-X"), ("is_private", 0)):
			original = file_doc.get(field)
			frappe.db.set_value("File", file_doc.name, field, bad, update_modified=False)
			try:
				refused(field)
			finally:
				frappe.db.set_value("File", file_doc.name, field, original, update_modified=False)

		driver_ob = self._paid_with_proof()
		driver_url = frappe.db.get_value("Cartera Obligacion", driver_ob, "driver_payment_proof")
		frappe.db.set_value("Cartera Pago", payment, "payment_proof", driver_url, update_modified=False)
		try:
			refused("payment_proof pointing at another file")
		finally:
			frappe.db.set_value("Cartera Pago", payment, "payment_proof", url, update_modified=False)

		for doctype, name in (("Cartera Pago", payment), ("Cartera Obligacion", ob)):
			frappe.db.set_value(doctype, name, "company", "_Test Company", update_modified=False)
			try:
				refused(f"company {doctype}")
				refused(f"company {doctype} SM", user=self.system_manager_user)
			finally:
				frappe.db.set_value(doctype, name, "company", get_default_company(), update_modified=False)

		driver_payment = self._payments(driver_ob)[0].name
		with fx.as_user(self.cartera_user):
			with self.assertRaises(frappe.DoesNotExistError):
				cartera_api.get_payment_proof(driver_payment)  # served by get_driver_payment_proof
			with self.assertRaises(frappe.DoesNotExistError):
				cartera_api.get_payment_proof("CPAG-NO-EXISTE")
			no_proof = self._register(ob, "10")["result"]["payment"]
			with self.assertRaises(frappe.DoesNotExistError):
				cartera_api.get_payment_proof(no_proof)
			# Extra client arguments (a path/url) are ignored by the dispatcher.
			served = self._execute_cmd("get_payment_proof", "GET", payment_name=payment, file_url=driver_url, path="/etc/passwd")
		self.assertEqual(base64.b64decode(served["data"]), file_doc.get_content(encodings=()))
		import inspect

		self.assertEqual(list(inspect.signature(cartera_api.get_payment_proof).parameters), ["payment_name"])

	# =====================================================================
	# CONFIRMAR pago del conductor
	# =====================================================================

	def test_confirm_driver_payment(self):
		ob = self._paid_with_proof()
		before = self._kpis()
		driver_payment = self._payments(ob)[0]
		res = self._confirm(ob)
		self.assertFalse(res["result"]["already_done"])
		doc = self._ob(ob)
		self.assertEqual(doc.payment_verification, "Confirmado")
		self.assertEqual(doc.payment_verified_by, self.cartera_user)
		self.assertTrue(doc.payment_verified_on)
		self.assertFalse(doc.payment_rejection_reason)
		self.assertEqual((doc.status, flt(doc.outstanding_amount), flt(doc.paid_amount, 2)), ("Pagado", 0, flt(doc.invoice_amount, 2)))
		payments = self._payments(ob)
		self.assertEqual(len(payments), 1)
		self.assertEqual((payments[0].name, payments[0].docstatus, flt(payments[0].amount)), (driver_payment.name, 1, flt(driver_payment.amount)))
		self.assertFalse(res["detail"]["por_confirmar"])
		self.assertFalse(res["detail"]["can_confirm_driver_payment"])
		self.assertFalse(res["detail"]["can_register_payment"])  # saldo 0

		after = self._kpis()
		self.assertEqual(after["por_confirmar"][1], before["por_confirmar"][1] - 1)
		self.assertEqual(after["por_confirmar"][0], flt(before["por_confirmar"][0] - flt(doc.invoice_amount), 2))
		self.assertEqual(after["cartera_actual"], before["cartera_actual"])
		self.assertEqual(after["cobrado_mes"], before["cobrado_mes"])
		self.assertEqual(res["dashboard"]["kpis"]["por_confirmar"]["count"], after["por_confirmar"][1])

		# Double confirmation -> already_done, nothing changes.
		again = self._confirm(ob)
		self.assertTrue(again["result"]["already_done"])
		self.assertEqual(len(self._payments(ob)), 1)
		# Reject after confirm -> conflict.
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "confirmado"):
			self._reject(ob)
		# Saldo 0: another payment is refused.
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "saldo pendiente"):
			self._register(ob, "1")

	def test_confirm_requires_a_driver_payment_and_access(self):
		without_proof = self._paid_without_proof()
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "no hay pago que confirmar"):
			self._confirm(without_proof)
		credit = self._credit()
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "por confirmar"):
			self._confirm(credit)

		ob = self._paid_with_proof()
		for user in self.denied_users:
			with self.assertRaises(frappe.PermissionError, msg=user):
				self._confirm(ob, user=user)
		frappe.db.set_value("Cartera Obligacion", ob, "company", "_Test Company", update_modified=False)
		try:
			for user in (self.cartera_user, self.system_manager_user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					self._confirm(ob, user=user)
		finally:
			frappe.db.set_value("Cartera Obligacion", ob, "company", get_default_company(), update_modified=False)
		with patch.object(cartera_api, "_allowed_companies", return_value=["_Test Company"]):
			with self.assertRaises(frappe.PermissionError):
				self._confirm(ob)
		self.assertEqual(self._ob(ob).payment_verification, "Sin confirmar")
		# System Manager may confirm.
		self._confirm(ob, user=self.system_manager_user)
		self.assertEqual(self._ob(ob).payment_verified_by, self.system_manager_user)

	# =====================================================================
	# RECHAZAR -- con comprobante (caso A)
	# =====================================================================

	def test_reject_driver_payment_with_proof(self):
		ob = self._paid_with_proof()
		invoice = flt(self._ob(ob).invoice_amount, 2)
		driver_payment = self._payments(ob)[0].name
		before = self._kpis()

		with self.assertRaisesRegex(frappe.ValidationError, "al menos 5"):
			self._reject(ob, reason="no")
		with self.assertRaisesRegex(frappe.ValidationError, "obligatorio"):
			self._reject(ob, reason="   ")
		with self.assertRaisesRegex(frappe.ValidationError, "500"):
			self._reject(ob, reason="x" * 501)

		res = self._reject(ob, reason=f"Cliente no pagó {XSS}")
		self.assertEqual(res["result"]["cancelled_payment"], driver_payment)
		doc = self._ob(ob)
		self.assertEqual(doc.payment_verification, "Rechazado")
		self.assertEqual(doc.payment_verified_by, self.cartera_user)
		self.assertTrue(doc.payment_verified_on)
		self.assertNotIn("<", doc.payment_rejection_reason)
		self.assertIn("Cliente no pagó", doc.payment_rejection_reason)
		self.assertEqual((doc.status, flt(doc.paid_amount), flt(doc.outstanding_amount, 2)), ("Pendiente", 0, invoice))
		self.assertIsNone(doc.paid_on)

		payment = frappe.get_doc("Cartera Pago", driver_payment)
		self.assertEqual(payment.docstatus, 2)
		self.assertEqual(payment.cancellation_reason, doc.payment_rejection_reason)
		self.assertEqual(payment.modified_by, self.cartera_user)  # who annulled it
		self.assertEqual(flt(payment.amount, 2), invoice)  # never edited

		history = {p["name"]: p for p in res["detail"]["payments"]}
		self.assertTrue(history[driver_payment]["cancelled"])
		self.assertEqual(history[driver_payment]["cancelled_by_name"], "Ana Cartera")
		self.assertEqual(history[driver_payment]["cancellation_reason"], doc.payment_rejection_reason)
		self.assertTrue(res["detail"]["can_register_payment"])
		self.assertFalse(res["detail"]["can_reject_driver_report"])

		after = self._kpis()
		self.assertEqual(after["cartera_actual"][0], flt(before["cartera_actual"][0] + invoice, 2))
		self.assertEqual(after["cobrado_mes"][0], flt(before["cobrado_mes"][0] - invoice, 2))
		self.assertEqual(after["por_confirmar"][1], before["por_confirmar"][1] - 1)

		# Double reject -> already_done; confirm after reject -> conflict.
		self.assertTrue(self._reject(ob)["result"]["already_done"])
		with self.assertRaisesRegex(cartera_service.CarteraStateError, "rechazado"):
			self._confirm(ob)

		# H3: the driver payment can never come back.
		self.assertEqual(cartera_service.create_driver_reported_payment(doc), driver_payment)
		clone = frappe.get_doc(
			{
				"doctype": "Cartera Pago",
				"cartera_obligacion": ob,
				"amount": invoice,
				"payment_date": nowdate(),
				"source": "Conductor",
				"client_request_id": f"conductor:{doc.recorrido_parada}-2",
			}
		)
		cartera_service._authorize(clone, cartera_service.ACTION_DRIVER_PAYMENT)
		with fx.as_user("Administrator"):
			with self.assertRaisesRegex(frappe.ValidationError, "resuelto"):
				clone.insert(ignore_permissions=True)
		self.assertEqual(len(self._payments(ob, source="Conductor")), 1)

		# The debt is collectable again: Registrar Cobro works.
		part = flt(invoice * 0.4, 2)
		res = self._register(ob, part)
		self.assertEqual(res["detail"]["status"], "Pendiente")
		self.assertEqual(res["detail"]["outstanding_amount"], flt(invoice - part, 2))

	# =====================================================================
	# RECHAZAR -- sin comprobante (caso B)
	# =====================================================================

	def test_reject_report_without_proof(self):
		ob = self._paid_without_proof()
		before_doc = self._ob(ob)
		self.assertEqual((before_doc.status, before_doc.payment_verification), ("Por Validar", "Sin confirmar"))
		invoice = flt(before_doc.invoice_amount, 2)
		detail = self._detail(ob)
		self.assertTrue(detail["can_reject_driver_report"])
		self.assertFalse(detail["can_confirm_driver_payment"])
		self.assertFalse(detail["driver_report_has_payment"])
		before = self._kpis()

		res = self._reject(ob, reason="No se recibió efectivo del conductor")
		self.assertIsNone(res["result"]["cancelled_payment"])
		doc = self._ob(ob)
		self.assertEqual(
			(doc.status, doc.payment_verification, flt(doc.paid_amount), flt(doc.outstanding_amount, 2)),
			("Pendiente", "Rechazado", 0, invoice),
		)
		self.assertEqual(doc.payment_verified_by, self.cartera_user)
		self.assertEqual(doc.payment_rejection_reason, "No se recibió efectivo del conductor")
		self.assertEqual(self._payments(ob), [])
		after = self._kpis()
		self.assertEqual(after["cartera_actual"], before["cartera_actual"])  # balance was already owed
		self.assertEqual(after["cobrado_mes"], before["cobrado_mes"])
		self.assertTrue(res["detail"]["can_register_payment"])

		res = self._register(ob, invoice)
		self.assertEqual(res["detail"]["status"], "Pagado")
		self.assertEqual(self._ob(ob).payment_verification, "Rechazado")  # the report stays rejected

	def test_confirm_and_reject_at_the_same_time(self):
		ob = self._paid_with_proof()
		frappe.db.commit()
		results = {}
		threads = [
			self._thread(cartera_service.confirm_driver_payment, results, "confirm", self.cartera_user, ob),
			self._thread(cartera_service.reject_driver_payment, results, "reject", self.cartera_user_b, ob, "No llegó el dinero"),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		self._reconnect()
		self._track_payments(ob)

		outcomes = {k: v[0] for k, v in results.items()}
		self.assertEqual(sorted(outcomes.values()), ["error", "ok"], results)
		doc = self._ob(ob)
		payments = self._payments(ob)
		if outcomes["confirm"] == "ok":
			self.assertEqual((doc.payment_verification, doc.status, payments[0].docstatus), ("Confirmado", "Pagado", 1))
		else:
			self.assertEqual((doc.payment_verification, doc.status, payments[0].docstatus), ("Rechazado", "Pendiente", 2))
		self.assertIn("CarteraStateError", next(v[1] for v in results.values() if v[0] == "error"))

	# =====================================================================
	# Desk / direct writes (H2, H3, H5)
	# =====================================================================

	def test_direct_writes_are_refused_for_everyone(self):
		ob = self._credit()
		quarter = flt(flt(self._ob(ob).invoice_amount) / 4, 2)
		payment = self._register(ob, str(quarter))["result"]["payment"]

		for user in (self.cartera_user, self.system_manager_user):
			for ptype in ("create", "write", "submit", "cancel", "delete"):
				self.assertFalse(frappe.has_permission("Cartera Pago", ptype, user=user), f"{user} {ptype}")
			for ptype in ("create", "write", "delete"):
				self.assertFalse(frappe.has_permission("Cartera Obligacion", ptype, user=user), f"{user} {ptype}")
			self.assertTrue(frappe.has_permission("Cartera Pago", "read", user=user))

		def new_payment(**extra):
			return {
				"doctype": "Cartera Pago",
				"cartera_obligacion": ob,
				"amount": 1,
				"payment_date": nowdate(),
				"payment_method": "Efectivo",
				"client_request_id": f"cartera:{uuid.uuid4()}",
				**extra,
			}

		for user in (self.cartera_user, self.system_manager_user, "Administrator"):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=f"insert {user}"):
					frappe.get_doc(new_payment()).insert()
				with self.assertRaises(frappe.PermissionError, msg=f"client.insert {user}"):
					frappe.client.insert(new_payment())
				with self.assertRaises(frappe.PermissionError, msg=f"client.insert submitted {user}"):
					frappe.client.insert(new_payment(docstatus=1))
				# A forged "flags" payload cannot produce the private token.
				with self.assertRaises(frappe.PermissionError, msg=f"forged flags {user}"):
					frappe.client.insert(
						new_payment(flags={"cartera_service_token": True, "cartera_service_action": "cartera_payment"})
					)
				with self.assertRaises(frappe.PermissionError, msg=f"cancel {user}"):
					frappe.get_doc("Cartera Pago", payment).cancel()
				with self.assertRaises(frappe.PermissionError, msg=f"client.cancel {user}"):
					frappe.client.cancel("Cartera Pago", payment)
				with self.assertRaises((frappe.PermissionError, frappe.ValidationError), msg=f"set_value {user}"):
					frappe.client.set_value("Cartera Pago", payment, "amount", 1)
				with self.assertRaises((frappe.PermissionError, frappe.ValidationError), msg=f"delete {user}"):
					frappe.delete_doc("Cartera Pago", payment)
		# Administrator: a bare authorized-looking document still needs the
		# token for submit too.
		with fx.as_user("Administrator"):
			doc = frappe.get_doc(new_payment())
			cartera_service._authorize(doc, cartera_service.ACTION_CARTERA_PAYMENT)
			doc.insert()
			doc.flags.cartera_service_token = None
			with self.assertRaises(frappe.PermissionError):
				doc.submit()
		self._track_payments(ob)
		self.assertEqual(frappe.db.get_value("Cartera Pago", payment, "docstatus"), 1)
		self.assertEqual(flt(self._ob(ob).paid_amount, 2), quarter)

		# Verification fields: only the service transition may write them.
		driver_ob = self._paid_with_proof()
		for field, value in (
			("payment_verification", "Confirmado"),
			("payment_verified_by", "Administrator"),
			("payment_rejection_reason", "inventado"),
		):
			with fx.as_user("Administrator"):
				with self.assertRaises(frappe.ValidationError, msg=field):
					frappe.client.set_value("Cartera Obligacion", driver_ob, field, value)
		self.assertEqual(self._ob(driver_ob).payment_verification, "Sin confirmar")

	def test_endpoints_deny_other_roles_and_other_companies(self):
		ob = self._credit()
		driver_ob = self._paid_with_proof()
		calls = (
			lambda: cartera_api.register_payment(ob, amount="1", payment_date=nowdate(), payment_method="Efectivo", client_request_id=str(uuid.uuid4())),
			lambda: cartera_api.confirm_driver_payment(driver_ob),
			lambda: cartera_api.reject_driver_payment(driver_ob, reason="motivo válido"),
		)
		for user in self.denied_users:
			for call in calls:
				with fx.as_user(user):
					with self.assertRaises(frappe.PermissionError, msg=user):
						call()
		with fx.as_user("Guest"):
			for call in calls:
				with self.assertRaises((frappe.AuthenticationError, frappe.PermissionError)):
					call()
		frappe.db.set_value("Cartera Obligacion", ob, "company", "_Test Company", update_modified=False)
		try:
			for user in (self.cartera_user, self.system_manager_user):
				with fx.as_user(user):
					with self.assertRaises(frappe.PermissionError, msg=user):
						calls[0]()
		finally:
			frappe.db.set_value("Cartera Obligacion", ob, "company", get_default_company(), update_modified=False)
		self.assertEqual(self._payments(ob), [])
		self.assertEqual(self._ob(driver_ob).payment_verification, "Sin confirmar")
		for name in ("register_payment", "confirm_driver_payment", "reject_driver_payment"):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[getattr(cartera_api, name)], ["POST"])

	# =====================================================================
	# KPIs by bucket / ERP integrity
	# =====================================================================

	def test_payments_move_vencida_and_por_vencer(self):
		credit = self._credit()
		before = self._kpis()
		part = flt(flt(self._ob(credit).invoice_amount) / 4, 2)
		self._register(credit, str(part))
		after = self._kpis()
		self.assertEqual(after["por_vencer"][0], flt(before["por_vencer"][0] - part, 2))
		self.assertEqual(after["vencida"], before["vencida"])
		self.assertEqual(after["cobrado_mes"][0], flt(before["cobrado_mes"][0] + part, 2))
		self.assertEqual(after["cartera_actual"][0], flt(before["cartera_actual"][0] - part, 2))

		# The same credit seen after its due date is overdue: a payment lowers VENCIDA.
		due = getdate(self._ob(credit).due_date)
		with patch.object(cartera_api, "_today", return_value=add_days(due, 1)):
			shifted_before = self._kpis()
			self._register(credit, str(part))
			shifted_after = self._kpis()
		self.assertEqual(shifted_after["vencida"][0], flt(shifted_before["vencida"][0] - part, 2))

	def test_no_accounting_or_operational_documents_touched(self):
		counted = ("Payment Entry", "Sales Invoice", "Journal Entry", "GL Entry", "Delivery Note", "Stock Ledger Entry")
		credit = self._credit()
		with_proof = self._paid_with_proof()
		to_reject = self._paid_with_proof()
		no_proof = self._paid_without_proof()
		obligations = (credit, with_proof, to_reject, no_proof)
		pick_lists = [self._ob(o).pick_list for o in obligations]
		orders = [self._ob(o).sales_order for o in obligations]
		snapshot = lambda: (  # noqa: E731
			{dt: frappe.db.count(dt) for dt in counted},
			frappe.get_all("Pick List", filters={"name": ["in", pick_lists]}, fields=["name", "modified", "status"], order_by="name"),
			frappe.get_all("Pick List Item", filters={"parent": ["in", pick_lists]}, fields=["name", "modified", "picked_qty"], order_by="name"),
			frappe.get_all("Sales Order", filters={"name": ["in", orders]}, fields=["name", "modified", "status", "per_billed"], order_by="name"),
			frappe.get_all("Bin", filters={"warehouse": self.wh.name}, fields=["name", "modified", "actual_qty", "reserved_qty"], order_by="name"),
		)
		before = snapshot()
		self._register(credit, str(flt(flt(self._ob(credit).invoice_amount) / 2, 2)), proof=_photo_jpeg())
		self._confirm(with_proof)
		self._reject(to_reject)
		self._reject(no_proof)
		self._register(no_proof, str(flt(self._ob(no_proof).invoice_amount, 2)))
		self.assertEqual(snapshot(), before)
		for o in obligations:
			for p in self._payments(o):
				self.assertEqual(frappe.db.get_value("Cartera Pago", p.name, "accounting_status"), "Sin contabilizar")
				self.assertFalse(frappe.db.get_value("Cartera Pago", p.name, "payment_entry"))

	# -- helpers used above ------------------------------------------------
	def _detail(self, obligation, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_obligation_detail(obligation)

	def _execute_cmd(self, method, http_method="POST", **form_dict):
		from frappe.handler import execute_cmd

		previous_request = getattr(frappe.local, "request", None)
		previous_form_dict = frappe.local.form_dict
		frappe.local.request = SimpleNamespace(method=http_method, host=None, files={})
		frappe.local.form_dict = frappe._dict(form_dict)
		try:
			return execute_cmd(f"fabergray_erp.api.cartera.{method}")
		finally:
			frappe.local.form_dict = previous_form_dict
			if previous_request is None:
				del frappe.local.request
			else:
				frappe.local.request = previous_request
