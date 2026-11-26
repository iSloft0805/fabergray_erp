# -*- coding: utf-8 -*-
"""Cartera masiva -- fase A: autorización masiva de los cobros reportados
por el conductor ("Autorizar cobros y pagos").

Real chain everywhere (Bodega -> Facturación -> Recorridos deliver_stop with
"Pagado" + comprobante -> Cartera Obligacion + Cartera Pago Conductor ->
api.cartera_masiva), isolated TestWorld data and test invoice series. Every
obligation a test creates has its Pick List moved (test setup) to its own
invoice day in 2023, so no real record -- nor any leftover test data on this
site -- ever falls inside a test's range. Historical obligations are never
touched: every test that uses another date type also filters by its own
test customers."""

import threading
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp import cartera_masiva_service as service
from fabergray_erp import cartera_service
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.api import cartera_masiva as api
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_cartera_foundation as foundation
from fabergray_erp.tests.test_entrega_masiva import EntregaMasivaTestMixin
from fabergray_erp.tests.test_recorridos_deliver_stop import _photo_jpeg

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

UNTOUCHED = (
	"Cartera Pago",
	"Payment Entry",
	"Journal Entry",
	"GL Entry",
	"Sales Invoice",
	"Stock Ledger Entry",
	"Stock Entry",
	"Stock Reconciliation",
)
REASON = "Autorización de cobros revisados (prueba)"


class CarteraMasivaTestMixin(EntregaMasivaTestMixin):
	"""Shared setup/helpers (also used by the volume suite)."""

	_obligation_of = foundation.TestCarteraFoundation._obligation_of

	@classmethod
	def _setup_cartera_world(cls, label):
		cls._setup_world(label)
		cls._customer_pool = []
		cls.addClassCleanup(cls._delete_cartera_leftovers)

	_delete_cartera_leftovers = classmethod(foundation.TestCarteraFoundation._delete_cartera_leftovers.__func__)

	def _customers(self, n):
		while len(type(self)._customer_pool) < n:
			type(self)._customer_pool.append(self._stop_customer())
		return type(self)._customer_pool[:n]

	def _reported(self, n, day, customers=1, proof=True, payment_status="Pagado", route_size=50, fresh_customers=None):
		"""`n` obligations born from REAL deliveries reported by the driver
		(default "Pagado" + comprobante -> POR CONFIRMAR), invoiced (test
		setup) on `day`. Returns their names, in creation order.
		`fresh_customers`: customers used by this call only (isolation for
		filters that are not the invoice day)."""
		pool = fresh_customers or self._customers(customers)
		names, created = [], 0
		while created < n:
			size = min(route_size, n - created)
			pick_lists = []
			for i in range(size):
				_so, pl = self._facturado_pick_list(customer=pool[(created + i) % len(pool)])
				pick_lists.append(pl.name)
			driver = self._driver(self._unique("Conductor")).name
			with fx.as_user(self.recorrido_user):
				route = self._create_route(pick_lists=pick_lists, driver=driver)
				route = self._plan_route(route["name"])
			route = self._start(route["name"])
			for stop in route["stops"]:
				self._deliver(
					route["name"], stop["name"], payment_status=payment_status, payment_proof=_photo_jpeg() if proof else None
				)
				self._move_invoice_day(stop["pick_list"], day)
				names.append(self._obligation_of(stop["name"]))
			created += size
		return names

	def _filters(self, day, day_to=None, **kw):
		kw.setdefault("date_type", service.DATE_INVOICE)
		return {"date_from": day, "date_to": day_to or day, **kw}

	def _preview(self, day, day_to=None, user=None, **kw):
		view = kw.pop("view", "eligible")
		page = kw.pop("page", 1)
		page_length = kw.pop("page_length", 50)
		with fx.as_user(user or self.admin_user):
			return api.get_bulk_authorization_preview(
				view=view, page=page, page_length=page_length, refresh=1, **self._filters(day, day_to, **kw)
			)

	def _authorize(self, day, day_to=None, user=None, track=True, **kw):
		filters = self._filters(day, day_to, **{k: kw.pop(k) for k in list(kw) if k in (
			"date_type", "customers", "issuer", "invoice_payment_method", "status", "search")})
		kw.setdefault("reason", REASON)
		kw.setdefault("review_confirmed", 1)
		kw.setdefault("client_request_id", str(uuid.uuid4()))
		if "obligations" not in kw:
			kw.setdefault("select_all", 1)
		with fx.as_user(user or self.admin_user):
			result = api.confirm_bulk_authorization(**filters, **kw)
		if track:
			self._track_lote(result["name"])
		return result

	def _track_lote(self, batch_name):
		tracked = {n for _d, n in self.world._created}
		if batch_name not in tracked:
			self.world.track_existing(service.BATCH_DOCTYPE, batch_name)
		for line in frappe.get_all(service.LINE_DOCTYPE, filters={"cartera_lote": batch_name}, pluck="name"):
			if line not in tracked:
				self.world.track_existing(service.LINE_DOCTYPE, line)

	def _lines(self, batch_name):
		return {
			l.cartera_obligacion: l
			for l in frappe.get_all(
				service.LINE_DOCTYPE,
				filters={"cartera_lote": batch_name},
				fields=["cartera_obligacion", "result", "message", "amount", "outstanding_before", "outstanding_after", "driver_payment"],
			)
		}

	def _snapshot(self, obligations):
		"""Every money/date/payment fact of these obligations and their
		payments -- must be identical before and after an authorization."""
		obs = frappe.get_all(
			"Cartera Obligacion",
			filters={"name": ["in", obligations]},
			fields=["name", "invoice_amount", "paid_amount", "outstanding_amount", "status", "due_date", "paid_on", "credit_days"],
			order_by="name asc",
		)
		pays = frappe.get_all(
			"Cartera Pago",
			filters={"cartera_obligacion": ["in", obligations]},
			fields=["name", "amount", "payment_date", "docstatus", "source", "modified", "accounting_status"],
			order_by="name asc",
		)
		return [tuple(o.values()) for o in obs], [tuple(p.values()) for p in pays]

	def _counts(self):
		return {dt: frappe.db.count(dt) for dt in UNTOUCHED}

	def _verification(self, name):
		return frappe.db.get_value(
			"Cartera Obligacion", name, ["payment_verification", "payment_verified_by", "payment_verified_batch"], as_dict=True
		)

	def _deferred(self, day, **kw):
		frappe.flags.cartera_masiva_defer_job = True
		try:
			return self._authorize(day, **kw)
		finally:
			frappe.flags.cartera_masiva_defer_job = False

	def _make_stale(self, batch_name):
		# Test setup: the worker has been silent longer than STALE_AFTER_MINUTES.
		frappe.db.set_value(service.BATCH_DOCTYPE, batch_name, "heartbeat_on", add_days(nowdate(), -1), update_modified=False)


class TestCarteraMasiva(CarteraMasivaTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_cartera_world("FGCM")

	# =====================================================================
	# Preview: read-only, classification, totals
	# =====================================================================

	def test_01_preview_is_read_only_and_explains_every_report(self):
		day = self._new_day()
		eligible = self._reported(2, day, customers=2)
		(no_proof,) = self._reported(1, day, proof=False)
		(confirmed,) = self._reported(1, day)
		(rejected,) = self._reported(1, day)
		(credit,) = self._reported(1, day, proof=False, payment_status="Crédito")
		with fx.as_user(self.admin_user):
			cartera_service.confirm_driver_payment(confirmed)
			cartera_service.reject_driver_payment(rejected, "Comprobante ilegible (prueba)")
		all_obs = [*eligible, no_proof, confirmed, rejected, credit]
		before = (self._snapshot(all_obs), self._counts(), frappe.db.count(service.BATCH_DOCTYPE))
		p = self._preview(day)
		self.assertEqual(before, (self._snapshot(all_obs), self._counts(), frappe.db.count(service.BATCH_DOCTYPE)))

		s = p["summary"]
		self.assertEqual((s["found"], s["eligible"], s["excluded"]), (5, 2, 3))  # the Crédito one is not a reported payment
		self.assertEqual({r["obligation"] for r in p["rows"]}, set(eligible))
		amounts = {r["obligation"]: r["amount"] for r in p["rows"]}
		invoice = {o: frappe.db.get_value("Cartera Obligacion", o, "invoice_amount") for o in eligible}
		self.assertEqual(amounts, invoice)
		self.assertEqual(s["total_amount"], sum(invoice.values()))
		self.assertEqual(s["customers"], 2)
		self.assertEqual(sum(c["amount"] for c in s["by_customer"]), s["total_amount"])
		self.assertEqual(sum(c["count"] for c in s["by_customer"]), 2)
		self.assertEqual({r[0] for r in p["eligible_index"]}, set(eligible))
		codes = {r["code"]: r["count"] for r in s["excluded_by_reason"]}
		self.assertEqual(codes, {"sin_comprobante": 1, "ya_confirmado": 1, "rechazado": 1})

		excluded = self._preview(day, view="excluded")
		reasons = {r["obligation"]: r["reason_code"] for r in excluded["rows"]}
		self.assertEqual(reasons, {no_proof: "sin_comprobante", confirmed: "ya_confirmado", rejected: "rechazado"})
		self.assertTrue(all(r["reason"] and not r["eligible"] and r["amount"] == 0 for r in excluded["rows"]))
		self.assertIn("próximamente", dict((r["obligation"], r["reason"]) for r in excluded["rows"])[no_proof])

	# =====================================================================
	# Authorization: 1 and 10
	# =====================================================================

	def test_02_single_authorization_end_to_end(self):
		day = self._new_day()
		(ob,) = self._reported(1, day)
		before_snapshot, before_counts = self._snapshot([ob]), self._counts()
		self.assertEqual(self._verification(ob).payment_verification, "Sin confirmar")
		result = self._authorize(day)

		self.assertEqual((result["status"], result["authorized"], result["skipped"], result["failed"]), (service.STATUS_DONE, 1, 0, 0))
		self.assertEqual(result["mode"], service.MODE_AUTHORIZATION)
		v = self._verification(ob)
		self.assertEqual((v.payment_verification, v.payment_verified_by, v.payment_verified_batch), ("Confirmado", self.admin_user, result["name"]))
		self.assertEqual(self._snapshot([ob]), before_snapshot)  # no money, date or payment changed
		self.assertEqual(self._counts(), before_counts)  # zero new payments / entries / stock
		batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
		self.assertIsNone(batch.active_company_lock)
		self.assertEqual((batch.requested_by, batch.reason, batch.review_confirmed), (self.admin_user, REASON, 1))
		self.assertTrue(batch.started_on and batch.finished_on and batch.queued_on)
		self.assertEqual(batch.authorized_amount, batch.total_amount)
		(line,) = self._lines(result["name"]).values()
		self.assertEqual((line.result, line.outstanding_before, line.outstanding_after), (service.RESULT_AUTHORIZED, 0, 0))
		self.assertEqual(line.driver_payment, frappe.db.get_value("Cartera Pago", {"cartera_obligacion": ob}, "name"))
		# The obligation is no longer POR CONFIRMAR on the Cartera page; the
		# detail shows the batch.
		with fx.as_user(self.admin_user):
			detail = cartera_api.get_obligation_detail(ob)
		self.assertFalse(detail["can_confirm_driver_payment"])
		self.assertEqual(detail["payment_verified_batch"], result["name"])

	def test_03_ten_obligations_several_customers_exact_totals(self):
		day = self._new_day()
		obs = self._reported(10, day, customers=3)
		before_snapshot, before_counts = self._snapshot(obs), self._counts()
		p = self._preview(day)
		result = self._authorize(day)
		self.assertEqual((result["authorized"], result["skipped"], result["failed"]), (10, 0, 0))
		self.assertEqual(result["authorized_amount"], p["summary"]["total_amount"])
		self.assertEqual(
			sorted((c["customer"], c["count"], c["amount"]) for c in result["by_customer"]),
			sorted((c["customer"], c["count"], c["amount"]) for c in p["summary"]["by_customer"]),
		)
		self.assertEqual({self._verification(o).payment_verified_batch for o in obs}, {result["name"]})
		self.assertEqual((self._snapshot(obs), self._counts()), (before_snapshot, before_counts))

	# =====================================================================
	# Selection
	# =====================================================================

	def test_04_select_all_across_pages_minus_exclusions(self):
		day = self._new_day()
		obs = self._reported(7, day)
		page1 = self._preview(day, page_length=3)
		page3 = self._preview(day, page_length=3, page=3)
		self.assertEqual((len(page1["rows"]), len(page3["rows"]), page1["total_in_view"]), (3, 1, 7))
		self.assertEqual(len(page1["eligible_index"]), 7)
		skip = [page1["rows"][0]["obligation"], page3["rows"][0]["obligation"]]
		result = self._authorize(day, excluded_obligations=skip)
		self.assertEqual(result["authorized"], 5)
		for o in obs:
			self.assertEqual(self._verification(o).payment_verification, "Sin confirmar" if o in skip else "Confirmado", o)

	def test_05_explicit_selection_reports_what_is_no_longer_eligible(self):
		day = self._new_day()
		a, b, c = self._reported(3, day)
		with fx.as_user(self.admin_user):
			cartera_service.reject_driver_payment(b, "Rechazado antes de confirmar (prueba)")
		result = self._authorize(day, obligations=[a, b, "CART-NO-EXISTE"])
		self.assertEqual(result["authorized"], 1)
		self.assertEqual({n["obligation"] for n in result["not_eligible"]}, {b, "CART-NO-EXISTE"})
		self.assertEqual(self._verification(c).payment_verification, "Sin confirmar")
		self.assertEqual(self._verification(b).payment_verification, "Rechazado")

	# =====================================================================
	# Filters
	# =====================================================================

	def test_06_filters_issuer_method_customers_search_status(self):
		day = self._new_day()
		(a,) = self._reported(1, day)  # integrandoMAS, Contado
		self.invoice_issuer, self.invoice_terms = "ecoluminar", ("Crédito", 30)
		try:
			(b,) = self._reported(1, day, fresh_customers=[self._stop_customer()])
		finally:
			self.invoice_issuer, self.invoice_terms = None, None
		self.historical_invoice_terms = True
		try:
			(c,) = self._reported(1, day)
		finally:
			self.historical_invoice_terms = False
		(d,) = self._reported(1, day, proof=False)

		def eligible(**kw):
			return {r["obligation"] for r in self._preview(day, **kw)["rows"]}

		self.assertEqual(eligible(), {a, b, c})
		self.assertEqual(eligible(issuer="ecoluminar"), {b})
		self.assertEqual(eligible(invoice_payment_method="Crédito"), {b})
		self.assertEqual(eligible(invoice_payment_method="Contado"), {a})
		self.assertEqual(eligible(invoice_payment_method=service.NO_PAYMENT_METHOD), {c})
		customer_b = frappe.db.get_value("Cartera Obligacion", b, "customer")
		self.assertEqual(eligible(customers=[customer_b]), {b})
		self.assertEqual(eligible(search=frappe.db.get_value("Cartera Obligacion", a, "pick_list")), {a})
		payment_c = frappe.db.get_value("Cartera Pago", {"cartera_obligacion": c}, "name")
		self.assertEqual(eligible(search=payment_c), {c})
		self.assertEqual(eligible(status="sin_comprobante"), set())
		excluded = self._preview(day, view="excluded", status="sin_comprobante")
		self.assertEqual({r["obligation"] for r in excluded["rows"]}, {d})
		self.assertEqual(self._preview(day, status="confirmado")["summary"]["found"], 0)
		# The batch only takes what the filters say, revalidated server-side.
		result = self._authorize(day, issuer="ecoluminar")
		self.assertEqual(result["authorized"], 1)
		self.assertEqual(self._verification(b).payment_verified_batch, result["name"])
		self.assertEqual(self._verification(a).payment_verification, "Sin confirmar")

	def test_07_date_types_are_inclusive_and_never_invented(self):
		day1, day2, day3 = self._new_day(), self._new_day(), self._new_day()
		own = [self._stop_customer()]
		(a,) = self._reported(1, day1, fresh_customers=own)
		(b,) = self._reported(1, day2, fresh_customers=own)
		(c,) = self._reported(1, day3, fresh_customers=own)
		customers = sorted({frappe.db.get_value("Cartera Obligacion", o, "customer") for o in (a, b, c)})

		def found(date_from, date_to, **kw):
			return {r["obligation"] for r in self._preview(date_from, date_to, **kw)["rows"]}

		self.assertEqual(found(day1, day2), {a, b})  # invoice date, inclusive bounds
		self.assertEqual(found(day2, day2), {b})
		self.assertEqual(found(day3, add_days(day3, 1)), {c})
		today = nowdate()
		# Delivery and reported-payment dates are today (real deliveries);
		# restricted to this class's own customers.
		for date_type in (service.DATE_DELIVERY, service.DATE_PAYMENT):
			self.assertEqual(found(today, today, date_type=date_type, customers=customers), {a, b, c}, date_type)
			self.assertEqual(found(add_days(today, -3), add_days(today, -1), date_type=date_type, customers=customers), set())
		due = frappe.db.get_value("Cartera Obligacion", a, "due_date")
		self.assertIn(a, found(due, due, date_type=service.DATE_DUE, customers=customers))
		# An obligation without a due date is never placed in a due-date range.
		frappe.db.set_value("Cartera Obligacion", a, "due_date", None, update_modified=False)  # test setup
		try:
			p = self._preview(add_days(today, -300), today, date_type=service.DATE_DUE, customers=customers)
			self.assertNotIn(a, {r["obligation"] for r in p["rows"]})
			self.assertGreaterEqual(p["summary"]["missing_date"], 1)
		finally:
			frappe.db.set_value("Cartera Obligacion", a, "due_date", due, update_modified=False)

	def test_08_parameter_validation(self):
		day = self._new_day()
		cases = (
			{"date_from": None},
			{"date_from": "2023-02-10", "date_to": "2023-02-01"},
			{"date_from": "2022-01-01", "date_to": "2023-06-01"},  # > 366 days
			{"date_type": "otra"},
			{"status": "inventado"},
			{"invoice_payment_method": "Bitcoin"},
			{"customers": "no-es-json"},
			{"search": "x" * 200},
		)
		for case in cases:
			args = {"date_type": service.DATE_INVOICE, "date_from": day, "date_to": day, **case}
			with fx.as_user(self.admin_user), self.assertRaises(service.CarteraMasivaError, msg=case):
				api.get_bulk_authorization_preview(**args)

	# =====================================================================
	# Idempotency, lock, recovery
	# =====================================================================

	def test_09_same_request_twice_is_one_batch(self):
		day = self._new_day()
		self._reported(2, day)
		request_id = str(uuid.uuid4())
		first = self._authorize(day, client_request_id=request_id)
		second = self._authorize(day, client_request_id=request_id)
		self.assertEqual(first["name"], second["name"])
		self.assertTrue(second["already_submitted"])
		self.assertEqual(frappe.db.count(service.LINE_DOCTYPE, {"cartera_lote": first["name"]}), 2)

	def test_10_one_active_batch_per_company_across_modes(self):
		day = self._new_day()
		self._reported(1, day)
		result = self._deferred(day)
		other = self._new_day()
		self._reported(1, other)
		with self.assertRaises(service.ActiveBatchError):
			self._authorize(other, track=False)
		with fx.as_user(self.admin_user):
			self.assertEqual(api.get_active_bulk_authorization()["name"], result["name"])
		# Registrar pagos (fase B) cannot be started at all.
		with self.assertRaises(service.CarteraMasivaError):
			service.create_batch(service.require_access(), {}, [frappe._dict()], REASON, str(uuid.uuid4()), mode=service.MODE_PAYMENTS)
		with fx.as_user(self.admin_user):
			options = api.get_bulk_authorization_options()
		self.assertEqual({m["key"]: m["enabled"] for m in options["modes"]}, {service.MODE_AUTHORIZATION: True, service.MODE_PAYMENTS: False})
		# Finish the pending one: the lock is released.
		batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
		with fx.as_user(self.admin_user):
			service.run_batch(batch.name, batch.worker_token)
		self.assertIsNone(frappe.db.get_value(service.BATCH_DOCTYPE, batch.name, "active_company_lock"))
		self.assertEqual(self._authorize(other)["authorized"], 1)

	def test_11_worker_death_and_safe_recovery(self):
		day = self._new_day()
		obs = self._reported(5, day)
		calls = {"n": 0}
		original = service.process_chunk

		def dies_on_second_chunk(batch_name, line_names):
			calls["n"] += 1
			if calls["n"] == 2:
				original(batch_name, line_names)
				raise SystemExit("worker killed (prueba)")  # BaseException: nothing catches it
			return original(batch_name, line_names)

		result = self._deferred(day)
		batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
		with patch.object(service, "CHUNK_SIZE", 2), patch.object(service, "process_chunk", side_effect=dies_on_second_chunk):
			with fx.as_user(self.admin_user), self.assertRaises(SystemExit):
				service.run_batch(batch.name, batch.worker_token)
		frappe.db.rollback()  # the dead worker's open chunk never commits
		status = frappe.get_doc(service.BATCH_DOCTYPE, batch.name)
		self.assertEqual((status.status, status.authorized_count), (service.STATUS_RUNNING, 2))
		self.assertEqual(status.active_company_lock, status.company)
		self.assertEqual(sum(self._verification(o).payment_verification == "Confirmado" for o in obs), 2)
		with fx.as_user(self.admin_user), self.assertRaises(service.CarteraMasivaError):
			api.resume_bulk_authorization(batch.name)  # not stale yet
		self._make_stale(batch.name)
		before = self._counts()
		with fx.as_user(self.admin_user):
			self.assertTrue(api.get_bulk_authorization_status(batch.name)["can_resume"])
			resumed = api.resume_bulk_authorization(batch.name)
		self.assertEqual((resumed["status"], resumed["authorized"], resumed["skipped"]), (service.STATUS_DONE, 5, 0))
		self.assertEqual(self._counts(), before)
		self.assertEqual({self._verification(o).payment_verified_batch for o in obs}, {batch.name})
		self.assertIsNone(frappe.db.get_value(service.BATCH_DOCTYPE, batch.name, "active_company_lock"))

	def test_12_old_worker_stops_after_takeover(self):
		day = self._new_day()
		self._reported(2, day)
		result = self._deferred(day)
		old_token = frappe.db.get_value(service.BATCH_DOCTYPE, result["name"], "worker_token")
		self._make_stale(result["name"])
		with fx.as_user(self.admin_user):
			api.resume_bulk_authorization(result["name"])  # new token, runs inline
			service.run_batch(result["name"], old_token)  # the old worker wakes up: no-op
		self.assertEqual(frappe.db.get_value(service.BATCH_DOCTYPE, result["name"], "authorized_count"), 2)
		self.assertEqual(
			frappe.db.count(service.LINE_DOCTYPE, {"cartera_lote": result["name"], "result": service.RESULT_AUTHORIZED}), 2
		)

	def test_13_close_releases_the_lock_without_processing(self):
		day = self._new_day()
		obs = self._reported(2, day)
		result = self._deferred(day)
		with fx.as_user(self.admin_user), self.assertRaises(service.CarteraMasivaError):
			api.close_bulk_authorization(result["name"])  # still alive
		self._make_stale(result["name"])
		with fx.as_user(self.admin_user):
			closed = api.close_bulk_authorization(result["name"])
		self.assertEqual((closed["status"], closed["skipped"], closed["authorized"]), (service.STATUS_CLOSED, 2, 0))
		self.assertIsNone(frappe.db.get_value(service.BATCH_DOCTYPE, result["name"], "active_company_lock"))
		for o in obs:
			self.assertEqual(self._verification(o).payment_verification, "Sin confirmar")

	def test_14_failure_of_one_obligation_rolls_back_only_that_one(self):
		day = self._new_day()
		a, b, c = self._reported(3, day)
		original = cartera_service.confirm_driver_payment

		def fails_for_b(name, batch=None):
			result = original(name, batch=batch)
			if name == b:
				raise RuntimeError("fallo simulado después de confirmar (prueba)")
			return result

		with patch.object(cartera_service, "confirm_driver_payment", side_effect=fails_for_b):
			result = self._authorize(day)
		self.assertEqual((result["status"], result["authorized"], result["failed"]), (service.STATUS_DONE_WITH_ERRORS, 2, 1))
		self.assertEqual(self._verification(b).payment_verification, "Sin confirmar")  # rolled back
		self.assertIsNone(self._verification(b).payment_verified_batch)
		self.assertEqual({self._verification(o).payment_verification for o in (a, c)}, {"Confirmado"})
		self.assertEqual(self._lines(result["name"])[b].result, service.RESULT_FAILED)
		for log in frappe.get_all("Error Log", filters={"reference_name": b}, pluck="name"):
			self.world.track_existing("Error Log", log)  # teardown: the simulated failure's log
		self.assertIsNone(frappe.db.get_value(service.BATCH_DOCTYPE, result["name"], "active_company_lock"))
		retry = self._authorize(day)  # the failed one is still eligible
		self.assertEqual((retry["authorized"], self._verification(b).payment_verified_batch), (1, retry["name"]))

	# =====================================================================
	# Concurrency with the individual actions
	# =====================================================================

	def test_15_individual_actions_after_confirm_are_respected(self):
		day = self._new_day()
		a, b, c = self._reported(3, day)
		result = self._deferred(day)
		with fx.as_user(self.admin_user):
			cartera_service.confirm_driver_payment(a)  # individual CONFIRMAR first
			cartera_service.reject_driver_payment(b, "Comprobante falso (prueba)")  # individual RECHAZAR first
		batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
		with fx.as_user(self.admin_user):
			service.run_batch(batch.name, batch.worker_token)
		lines = self._lines(batch.name)
		self.assertEqual({o: lines[o].result for o in (a, b, c)}, {a: "Omitido", b: "Omitido", c: "Autorizado"})
		self.assertIsNone(self._verification(a).payment_verified_batch)  # the individual confirmation stays individual
		self.assertEqual(self._verification(b).payment_verification, "Rechazado")  # never confirmed afterwards
		self.assertEqual(frappe.db.get_value("Cartera Obligacion", b, "status"), "Pendiente")
		# And the other way round: an authorized one can no longer be rejected.
		with fx.as_user(self.admin_user), self.assertRaises(cartera_service.CarteraStateError):
			cartera_service.reject_driver_payment(c, "Rechazo tardío (prueba)")

	def test_16_concurrent_individual_reject_and_bulk_authorize(self):
		day = self._new_day()
		(ob,) = self._reported(1, day)
		result = self._deferred(day)
		frappe.db.commit()
		batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
		site, errors, outcome = frappe.local.site, [], {}
		barrier = threading.Barrier(2)
		admin = self.admin_user

		def worker():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(admin)
				barrier.wait()
				service.run_batch(batch.name, batch.worker_token)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				errors.append(("worker", repr(e)))
			finally:
				frappe.destroy()

		def reject():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(admin)
				barrier.wait()
				outcome["reject"] = cartera_service.reject_driver_payment(ob, "Rechazo concurrente (prueba)")
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				outcome["reject_error"] = e.__class__.__name__
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=worker), threading.Thread(target=reject)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(120)
		frappe.db.rollback()
		self.assertFalse(errors, errors)
		v = self._verification(ob)
		line = self._lines(batch.name)[ob]
		if v.payment_verification == "Confirmado":
			self.assertEqual((line.result, v.payment_verified_batch, outcome.get("reject_error")), ("Autorizado", batch.name, "CarteraStateError"))
		else:
			self.assertEqual((v.payment_verification, line.result), ("Rechazado", "Omitido"))
			self.assertIsNone(v.payment_verified_batch)
		# Whatever the interleaving: at most one decision, money coherent.
		pays = frappe.get_all("Cartera Pago", filters={"cartera_obligacion": ob}, fields=["docstatus"])
		self.assertEqual(len(pays), 1)
		status = frappe.db.get_value("Cartera Obligacion", ob, "status")
		self.assertEqual(status, "Pagado" if v.payment_verification == "Confirmado" else "Pendiente")

	# =====================================================================
	# Security
	# =====================================================================

	def test_17_only_system_manager(self):
		day = self._new_day()
		for user in (self.cartera_user, self.recorrido_user, self.facturacion_user, self.bodega_user):
			with fx.as_user(user):
				for call in (
					lambda: api.get_bulk_authorization_options(),
					lambda: api.search_bulk_customers(""),
					lambda: api.get_bulk_authorization_preview(**self._filters(day)),
					lambda: api.confirm_bulk_authorization(
						**self._filters(day), reason=REASON, review_confirmed=1, select_all=1, client_request_id=str(uuid.uuid4())
					),
					lambda: api.get_bulk_authorization_history(),
					lambda: api.get_active_bulk_authorization(),
				):
					with self.assertRaises(frappe.PermissionError, msg=user):
						call()
		for fn in (api.confirm_bulk_authorization, api.resume_bulk_authorization, api.close_bulk_authorization):
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func.get(fn), ["POST"])
		for fn in (service.run_batch, service.process_chunk, service.resume, service.close, service.create_batch, service.load_candidates):
			self.assertNotIn(fn, frappe.whitelisted)

	def test_18_reason_and_administrative_confirmation_required(self):
		day = self._new_day()
		(ob,) = self._reported(1, day)
		for kw, exc in (
			({"reason": "corto"}, frappe.ValidationError),
			({"reason": "<b></b>   "}, frappe.ValidationError),
			({"review_confirmed": 0}, service.CarteraMasivaError),
			({"review_confirmed": "quizás"}, service.CarteraMasivaError),
			({"client_request_id": "x"}, service.CarteraMasivaError),
		):
			with self.assertRaises(exc, msg=kw):
				self._authorize(day, track=False, **kw)
		self.assertFalse(frappe.db.exists(service.BATCH_DOCTYPE, {"date_from": day}))
		self.assertEqual(self._verification(ob).payment_verification, "Sin confirmar")

	def test_19_rest_and_desk_cannot_fake_batches_or_traceability(self):
		day = self._new_day()
		(ob,) = self._reported(1, day)
		result = self._authorize(day)
		(other,) = self._reported(1, self._new_day())
		for user in (self.admin_user, "Administrator"):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					frappe.get_doc({
						"doctype": service.BATCH_DOCTYPE, "company": frappe.db.get_value("Cartera Obligacion", ob, "company"),
						"mode": service.MODE_AUTHORIZATION, "status": "En cola", "reason": "lote falso de prueba",
					}).insert(ignore_permissions=True)
				with self.assertRaises(frappe.PermissionError, msg=user):
					frappe.get_doc({
						"doctype": service.LINE_DOCTYPE, "cartera_lote": result["name"], "cartera_obligacion": other, "result": "Autorizado",
					}).insert(ignore_permissions=True)
				batch = frappe.get_doc(service.BATCH_DOCTYPE, result["name"])
				batch.reason = "motivo cambiado"
				with self.assertRaises(frappe.PermissionError, msg=user):
					batch.save(ignore_permissions=True)
				line = frappe.get_doc(service.LINE_DOCTYPE, {"cartera_lote": result["name"]})
				line.result = "Fallido"
				with self.assertRaises(frappe.PermissionError, msg=user):
					line.save(ignore_permissions=True)
				with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=user):
					frappe.delete_doc(service.BATCH_DOCTYPE, result["name"])
				with self.assertRaises(frappe.ValidationError, msg=user):
					frappe.delete_doc(service.LINE_DOCTYPE, line.name, ignore_permissions=True)
				# payment_verified_batch: never writable by hand, nor clearable.
				for target, value in ((other, result["name"]), (ob, None)):
					doc = frappe.get_doc("Cartera Obligacion", target)
					doc.payment_verified_batch = value
					with self.assertRaises(frappe.ValidationError, msg=(user, target)):
						doc.save(ignore_permissions=True)
				with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=user):
					frappe.client.set_value("Cartera Obligacion", other, "payment_verified_batch", result["name"])
		self.assertIsNone(self._verification(other).payment_verified_batch)
		self.assertEqual(self._verification(ob).payment_verified_batch, result["name"])
		# Even the service cannot stamp a batch that is not RUNNING (this one is Completado).
		with fx.as_user(self.admin_user), self.assertRaises(frappe.ValidationError):
			cartera_service.confirm_driver_payment(other, batch=result["name"])
		self.assertEqual(self._verification(other).payment_verification, "Sin confirmar")

	def test_20_zero_payments_balances_entries_or_stock(self):
		day = self._new_day()
		obs = self._reported(4, day, customers=2)
		snapshot, counts = self._snapshot(obs), self._counts()
		bins = frappe.db.sql("select round(sum(actual_qty), 6), round(sum(reserved_qty), 6) from tabBin")
		series = frappe.db.sql("select name, current from tabSeries where name like 'FG-FACT%%' order by name")
		result = self._authorize(day)
		self.assertEqual(result["authorized"], 4)
		self.assertEqual(self._snapshot(obs), snapshot)
		self.assertEqual(self._counts(), counts)
		self.assertEqual(frappe.db.sql("select round(sum(actual_qty), 6), round(sum(reserved_qty), 6) from tabBin"), bins)
		self.assertEqual(frappe.db.sql("select name, current from tabSeries where name like 'FG-FACT%%' order by name"), series)
		self.assertEqual(
			set(frappe.get_all("Cartera Pago", filters={"cartera_obligacion": ["in", obs]}, pluck="accounting_status")),
			{"Sin contabilizar"},
		)

	def test_21_history_and_status_payloads(self):
		day = self._new_day()
		self._reported(2, day, customers=2)
		result = self._authorize(day)
		with fx.as_user(self.admin_user):
			history = api.get_bulk_authorization_history()
			status = api.get_bulk_authorization_status(result["name"])
		self.assertIn(result["name"], [h["name"] for h in history["items"]])
		row = next(h for h in history["items"] if h["name"] == result["name"])
		self.assertEqual((row["mode"], row["authorized_count"], row["requested_by"]), (service.MODE_AUTHORIZATION, 2, self.admin_user))
		self.assertEqual(len(status["by_customer"]), 2)
		self.assertEqual(sum(c["amount"] for c in status["by_customer"]), status["authorized_amount"])
		self.assertTrue(status["finished"])
		self.assertEqual(status["date_type"], "Fecha de facturación")


class TestCarteraMasivaUiContract(IntegrationTestCase):
	def test_ui_contract(self):
		import os
		import re

		path = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cartera", "cartera.js")
		with open(path) as f:
			js = f.read()
		self.assertIn('if (!frappe.user.has_role("System Manager")) return "";', js)
		for text in (
			"Autorizar cobros y pagos",
			"Autorizar cobros",
			"Registrar pagos",
			"Próximamente",
			"Tipo de fecha",
			"Fecha inicial",
			"Fecha final",
			"Clientes",
			"Emisor",
			"Forma de pago de la factura",
			"Buscar cobros",
			"Seleccionar todos los resultados (todas las páginas)",
			"Total por cliente",
			"Motivo de la autorización (obligatorio)",
			"Confirmo que revisé estos cobros y sus comprobantes.",
			"Esta operación no se puede deshacer desde la interfaz.",
			"Confirmar autorización",
			"Reanudar",
			"Historial de lotes",
		):
			self.assertIn(text, js)
		called = set(re.findall(r'cm_call\("([a-z_]+)"', js))
		self.assertEqual(
			called,
			{
				"get_bulk_authorization_options",
				"search_bulk_customers",
				"get_bulk_authorization_preview",
				"confirm_bulk_authorization",
				"get_bulk_authorization_status",
				"get_bulk_authorization_history",
			},
		)
		for name in called | {"resume_bulk_authorization", "close_bulk_authorization"}:
			self.assertIn(getattr(api, name), frappe.whitelisted, name)
		self.assertIn('cm_recovery("resume_bulk_authorization")', js)
		self.assertIn('cm_recovery("close_bulk_authorization")', js)
