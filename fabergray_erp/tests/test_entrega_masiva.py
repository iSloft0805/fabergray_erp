# -*- coding: utf-8 -*-
"""Entrega masiva de pedidos (Recorridos) -- administrative regularization
of deliveries that already happened physically.

Real chain everywhere (Bodega picking -> Facturación -> Recorridos ->
api.entrega_masiva), isolated TestWorld data and test invoice series. Every
order a test creates is moved (test setup) to its own invoice day in 2023,
so no real order -- nor any leftover test data on this site -- ever falls
inside a test's date range."""

import threading
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from fabergray_erp import cartera_service, entrega_masiva_service as service
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.api import entrega_masiva as api
from fabergray_erp.api import recorridos
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base
from fabergray_erp.tests.test_recorridos_deliver_stop import _photo_jpeg, _signature_png

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

REG = service.ROUTE_KIND_REGULARIZATION
ACCOUNTING = ("Stock Ledger Entry", "Stock Entry", "GL Entry", "Payment Entry", "Journal Entry", "Sales Invoice", "Cartera Pago")


class EntregaMasivaTestMixin:
	"""Shared setup/helpers (also used by the volume suite)."""

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

	@classmethod
	def _setup_world(cls, label):
		cls.world = fx.TestWorld(real_stock=True)
		cls.addClassCleanup(cls.world.cleanup)
		ns = cls.world.namespace
		low = ns.lower()
		cls.wh = cls.world.warehouse(f"{label} WH {ns}")
		cls.item = cls.world.item(f"{label}-ITEM-{ns}")
		cls.customer = cls.world.customer(f"{label} Customer {ns}")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100000, rate=50)
		cls.bodega_user = cls.world.user(f"{label.lower()}-bodega-{low}@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"{label.lower()}-fact-{low}@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user(f"{label.lower()}-rec-{low}@example.com", ["Recorrido"])
		cls.admin_user = cls.world.user(f"{label.lower()}-sm-{low}@example.com", ["System Manager"])
		cls.cartera_user = cls.world.user(f"{label.lower()}-cart-{low}@example.com", ["Cartera"])
		cls._seq = 0
		cls._evidence_file_names = []
		cls.addClassCleanup(cls._delete_evidence_files)
		# A unique, otherwise empty, 2023 window for this class's invoice days.
		cls.base_day = add_days("2023-01-01", int(ns[:4], 16) % 300)
		cls._day_seq = 0

	_delete_evidence_files = classmethod(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__)

	def setUp(self):
		super().setUp()
		self.invoice_terms = None
		self.invoice_issuer = None
		self.historical_invoice_terms = False

	# -- data -------------------------------------------------------------------
	def _new_day(self):
		type(self)._day_seq += 1
		return str(add_days(self.base_day, type(self)._day_seq * 2))

	def _move_invoice_day(self, pick_list, day, hour="10:00:00"):
		# Test setup: an order invoiced on `day` (never a real order's date).
		frappe.db.set_value("Pick List", pick_list, "fg_invoiced_on", f"{day} {hour}", update_modified=False)

	def _orders(self, n, day, terms=None, historical=False):
		self.invoice_terms, self.historical_invoice_terms = terms, historical
		names = []
		for _i in range(n):
			_so, pl = self._facturado_pick_list(customer=self.customer)
			self._move_invoice_day(pl.name, day)
			names.append(pl.name)
		self.invoice_terms, self.historical_invoice_terms = None, False
		return names

	def _route_en_ruta(self, n_stops, day):
		route = self._en_ruta(n_stops=n_stops)
		for s in route["stops"]:
			self._move_invoice_day(s["pick_list"], day)
		return route

	# -- api --------------------------------------------------------------------
	def _preview(self, day_from, day_to=None, user=None, **kw):
		kw.setdefault("date_mode", service.DATE_MODE_COMMON)
		if kw["date_mode"] == service.DATE_MODE_COMMON:
			kw.setdefault("effective_date", nowdate())
		with fx.as_user(user or self.admin_user):
			return api.get_bulk_delivery_preview(date_from=day_from, date_to=day_to or day_from, refresh=1, **kw)

	def _confirm(self, day_from, day_to=None, user=None, track=True, **kw):
		kw.setdefault("date_mode", service.DATE_MODE_COMMON)
		if kw["date_mode"] == service.DATE_MODE_COMMON:
			kw.setdefault("effective_date", nowdate())
		kw.setdefault("reason", "Regularización de pedidos entregados (prueba)")
		kw.setdefault("physical_delivery_confirmed", 1)
		kw.setdefault("client_request_id", str(uuid.uuid4()))
		if "pick_lists" not in kw:
			kw.setdefault("select_all", 1)
		with fx.as_user(user or self.admin_user):
			result = api.confirm_bulk_delivery(date_from=day_from, date_to=day_to or day_from, **kw)
		if track:
			self._track_batch(result["name"])
		return result

	def _track_batch(self, batch_name):
		"""Teardown tracking of everything a batch created (batch, results,
		administrative routes, regularized stops, their obligations)."""
		tracked = {n for _d, n in self.world._created}
		def track(doctype, name):
			if name and name not in tracked:
				self.world.track_existing(doctype, name)
				tracked.add(name)
		track("Entrega Masiva", batch_name)
		for item in frappe.get_all("Entrega Masiva Pedido", filters={"entrega_masiva": batch_name}, pluck="name"):
			track("Entrega Masiva Pedido", item)
		for route in frappe.get_all("Recorrido", filters={"entrega_masiva": batch_name}, pluck="name"):
			track("Recorrido", route)
		for stop in frappe.get_all("Recorrido Parada", filters={"entrega_masiva": batch_name}, pluck="name"):
			track("Recorrido Parada", stop)
			for ob in frappe.get_all("Cartera Obligacion", filters={"recorrido_parada": stop}, pluck="name"):
				track("Cartera Obligacion", ob)

	def _items(self, batch_name):
		return {
			i.pick_list: i
			for i in frappe.get_all(
				"Entrega Masiva Pedido",
				filters={"entrega_masiva": batch_name},
				fields=["pick_list", "result", "message", "recorrido", "recorrido_parada", "cartera_obligacion", "effective_delivery_date"],
			)
		}

	def _stop_of(self, pick_list, delivered=True):
		filters = {"pick_list": pick_list}
		if delivered:
			filters["status"] = "Entregado"
		names = frappe.get_all("Recorrido Parada", filters=filters, pluck="name")
		return frappe.get_doc("Recorrido Parada", names[0]) if names else None

	def _obligations(self, pick_list):
		return frappe.get_all("Cartera Obligacion", filters={"pick_list": pick_list}, pluck="name")

	def _accounting_counts(self):
		return {dt: frappe.db.count(dt) for dt in ACCOUNTING}


class TestEntregaMasiva(EntregaMasivaTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_world("FGEM")

	# =====================================================================
	# Preview: read-only, criterion, reasons
	# =====================================================================

	def test_01_preview_is_read_only_and_explains_every_order(self):
		day = self._new_day()
		free = self._orders(2, day, terms=("Crédito", 30))
		en_ruta = self._route_en_ruta(1, day)
		draft = self._route(n_stops=1, plan=False)
		planned = self._route(n_stops=1)
		for route in (draft, planned):
			self._move_invoice_day(route["stops"][0]["pick_list"], day)
		delivered = self._route_en_ruta(1, day)
		self._deliver(delivered["name"], delivered["stops"][0]["name"], payment_status="Crédito")

		before = {dt: frappe.db.count(dt) for dt in ("Recorrido", "Recorrido Parada", "Cartera Obligacion", "Entrega Masiva")}
		preview = self._preview(day)
		after = {dt: frappe.db.count(dt) for dt in before}
		self.assertEqual(after, before)

		self.assertEqual(preview["criterion"], service.DATE_CRITERION)
		summary = preview["summary"]
		self.assertEqual(summary["found"], 6)
		self.assertEqual(summary["eligible"], 3)  # 2 free + 1 in an En Ruta route
		self.assertEqual(summary["from_route_in_progress"], 1)
		reasons = {r["code"]: r["count"] for r in summary["excluded_by_reason"]}
		self.assertEqual(reasons, {"en_recorrido_borrador": 1, "en_recorrido_planificado": 1, "ya_entregado": 1})
		eligible = {r["pick_list"]: r for r in preview["rows"]}
		self.assertEqual(set(eligible), set(free) | {en_ruta["stops"][0]["pick_list"]})
		self.assertGreater(summary["total_amount"], 0)
		self.assertEqual(summary["total_amount"], round(sum(r["amount"] for r in eligible.values()), 2))
		excluded = self._preview(day, view="excluded")["rows"]
		done = next(r for r in excluded if r["reason_code"] == "ya_entregado")
		self.assertIn("Obligación de Cartera válida", done["reason_detail"])

	def test_02_classification_of_every_exclusion(self):
		"""Synthetic candidates: cancelled, not completed (shortages),
		cancelled order, inconsistent obligation, inconsistent routes, invoice
		after the effective date, and the informative stock warning."""
		def cand(**kw):
			c = frappe._dict(
				name="PL-X", company="C", docstatus=1, sales_order="SO-X", so_docstatus=1, fg_invoiced_on="2023-03-01 10:00:00",
				stops=[], obligations=[], stock_issue=True,
			)
			c.update(kw)
			return c

		eff = getdate("2023-03-05")
		stop = lambda st, rs, kind=None: frappe._dict(name="S", status=st, recorrido="R", route_status=rs, route_kind=kind)  # noqa: E731
		cases = (
			(cand(company="OTRA"), "otra_empresa"),
			(cand(docstatus=2), "pick_list_cancelado"),
			(cand(docstatus=0), "pick_list_no_completado"),
			(cand(so_docstatus=2), "pedido_cancelado"),
			(cand(sales_order=None), "pedido_cancelado"),
			(cand(stops=[stop("Entregado", "Completado")]), "ya_entregado"),
			(cand(obligations=[frappe._dict(name="CART-1", recorrido_parada="S0")]), "obligacion_inconsistente"),
			(cand(stops=[stop("Pendiente", "Borrador")]), "en_recorrido_borrador"),
			(cand(stops=[stop("Pendiente", "Planificado")]), "en_recorrido_planificado"),
			(cand(stops=[stop("Pendiente", "En Ruta", REG)]), "en_regularizacion"),
			(cand(stops=[stop("No Entregado", "En Ruta")]), "recorridos_inconsistentes"),
			(cand(stops=[stop("Pendiente", "En Ruta"), stop("Pendiente", "Planificado")]), "recorridos_inconsistentes"),
			(cand(fg_invoiced_on="2023-03-09 10:00:00"), "factura_posterior"),
		)
		for c, expected in cases:
			self.assertEqual(service.classify(c, service.DATE_MODE_COMMON, eff, "C")[0], expected, expected)
		ok = service.classify(cand(stock_issue=False, stops=[stop("Pendiente", "Completado")]), service.DATE_MODE_COMMON, eff, "C")
		self.assertIsNone(ok[0])
		self.assertEqual(set(ok[2]), {"sin_salida_inventario", "parada_en_recorrido_cerrado"})
		row = service._row(cand(obligations=[frappe._dict(name="CART-1", recorrido_parada="S0")]), service.classify(
			cand(obligations=[frappe._dict(name="CART-1", recorrido_parada="S0")]), service.DATE_MODE_COMMON, eff, "C"
		))
		self.assertTrue(row["needs_review"])

	# =====================================================================
	# Deliveries
	# =====================================================================

	def test_03_single_order_regularized_end_to_end(self):
		day = self._new_day()
		(pl,) = self._orders(1, day, terms=("Crédito", 30))
		due = frappe.db.get_value("Pick List", pl, "fg_invoice_due_date")
		before = self._accounting_counts()
		result = self._confirm(day)
		self.assertEqual(result["status"], service.STATUS_DONE)
		self.assertEqual((result["delivered"], result["skipped"], result["failed"]), (1, 0, 0))
		self.assertEqual(self._accounting_counts(), before)  # no inventory/accounting movement

		stop = self._stop_of(pl)
		route = frappe.get_doc("Recorrido", stop.recorrido)
		self.assertEqual(route.route_kind, REG)
		self.assertEqual(route.entrega_masiva, result["name"])
		self.assertEqual(route.status, "Completado")
		self.assertFalse(route.driver or route.vehicle)
		self.assertEqual(stop.delivery_mode, service.DELIVERY_MODE_REGULARIZATION)
		self.assertFalse(stop.delivery_photo or stop.customer_signature or stop.payment_proof)
		self.assertEqual(stop.payment_status, "Crédito")
		self.assertEqual(getdate(stop.effective_delivery_date), getdate(nowdate()))
		self.assertEqual(str(stop.delivered_on)[11:19], "00:00:00")  # declared day, not a verified hour
		self.assertTrue(stop.regularized_on)
		(ob,) = self._obligations(pl)
		ob = frappe.get_doc("Cartera Obligacion", ob)
		self.assertEqual(getdate(ob.due_date), getdate(due))  # exactly Facturación's due date
		self.assertEqual(ob.invoice_payment_method, "Crédito")
		self.assertEqual(ob.status, "Pendiente")
		self.assertEqual(ob.paid_amount, 0)
		with fx.as_user(self.cartera_user):
			detail = cartera_api.get_obligation_detail(ob.name)
		self.assertFalse(detail["delivery_time_verified"])
		self.assertEqual(detail["payments"], [])

	def test_04_payment_terms_and_custom_due_dates_are_preserved(self):
		day = self._new_day()
		credit = {d: self._orders(1, day, terms=("Crédito", d))[0] for d in (15, 45, 90)}
		(cash,) = self._orders(1, day, terms=("Contado", 0))
		(historical,) = self._orders(1, day, historical=True)
		(custom,) = self._orders(1, day, terms=("Transferencia", 20))
		# Test setup: an explicit due date WITHOUT payment method must still win.
		frappe.db.set_value("Pick List", custom, "fg_invoice_payment_method", None, update_modified=False)
		custom_due = frappe.db.get_value("Pick List", custom, "fg_invoice_due_date")
		result = self._confirm(day)
		self.assertEqual(result["delivered"], 6)
		for days, pl in credit.items():
			ob = frappe.get_doc("Cartera Obligacion", self._obligations(pl)[0])
			self.assertEqual(getdate(ob.due_date), getdate(frappe.db.get_value("Pick List", pl, "fg_invoice_due_date")), days)
			self.assertEqual(self._stop_of(pl).payment_status, "Crédito")
		cash_ob = frappe.get_doc("Cartera Obligacion", self._obligations(cash)[0])
		self.assertEqual(self._stop_of(cash).payment_status, "Pendiente por Pago")
		self.assertEqual((cash_ob.status, cash_ob.paid_amount), ("Pendiente", 0))  # never marked paid
		hist_ob = frappe.get_doc("Cartera Obligacion", self._obligations(historical)[0])
		self.assertEqual(self._stop_of(historical).payment_status, "Pendiente por Pago")
		self.assertIsNone(hist_ob.due_date)  # no invented due date
		self.assertEqual(hist_ob.due_date_source, cartera_service.DUE_SOURCE_LEGACY)
		custom_ob = frappe.get_doc("Cartera Obligacion", self._obligations(custom)[0])
		self.assertEqual(getdate(custom_ob.due_date), getdate(custom_due))
		self.assertEqual(custom_ob.due_date_source, cartera_service.DUE_SOURCE_INVOICE)
		self.assertEqual(frappe.db.count("Cartera Pago", {"cartera_obligacion": ["in", [cash_ob.name, hist_ob.name]]}), 0)

	def test_05_in_place_delivery_never_closes_a_route_with_pending_stops(self):
		day = self._new_day()
		route = self._route_en_ruta(2, day)
		first, second = (s["pick_list"] for s in route["stops"])
		result = self._confirm(day, pick_lists=[first])
		self.assertEqual(result["delivered"], 1)
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "En Ruta")
		stop = self._stop_of(first)
		self.assertEqual(stop.recorrido, route["name"])  # delivered in its own route
		self.assertEqual(stop.delivery_mode, service.DELIVERY_MODE_REGULARIZATION)
		self.assertEqual(frappe.db.get_value("Recorrido Parada", self._stop_of(second, delivered=False).name, "status"), "Pendiente")
		self._confirm(day, pick_lists=[second])
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Completado")

	def test_06_ten_orders_mixed_assigned_and_unassigned(self):
		day = self._new_day()
		free = self._orders(8, day, terms=("Crédito", 30))
		route = self._route_en_ruta(2, day)
		result = self._confirm(day)
		self.assertEqual((result["total_selected"], result["delivered"]), (10, 10))
		for pl in free + [s["pick_list"] for s in route["stops"]]:
			self.assertEqual(len(self._obligations(pl)), 1, pl)
			self.assertIsNotNone(self._stop_of(pl))
		admin_routes = result["administrative_routes"]
		self.assertEqual(len(admin_routes), 1)
		self.assertEqual(frappe.db.count("Recorrido Parada", {"recorrido": admin_routes[0]}), 8)
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Completado")

	# =====================================================================
	# Selection, idempotency, retries
	# =====================================================================

	def test_07_select_all_across_pages_minus_exclusions(self):
		day = self._new_day()
		orders = self._orders(7, day, terms=("Contado", 0))
		pages = [self._preview(day, page=p, page_length=3) for p in (1, 2, 3)]
		self.assertEqual([len(p["rows"]) for p in pages], [3, 3, 1])
		self.assertEqual(pages[0]["total_in_view"], 7)
		self.assertTrue(pages[0]["has_more"])
		self.assertFalse(pages[2]["has_more"])
		seen = [r["pick_list"] for p in pages for r in p["rows"]]
		self.assertEqual(sorted(seen), sorted(orders))
		left_out = [pages[0]["rows"][1]["pick_list"], pages[2]["rows"][0]["pick_list"]]
		result = self._confirm(day, select_all=1, excluded_pick_lists=left_out)
		self.assertEqual(result["delivered"], 5)
		for pl in orders:
			self.assertEqual(bool(self._stop_of(pl)), pl not in left_out, pl)

	def test_08_idempotent_request_and_no_double_delivery(self):
		day = self._new_day()
		orders = self._orders(3, day)
		request_id = str(uuid.uuid4())
		first = self._confirm(day, client_request_id=request_id)
		again = self._confirm(day, client_request_id=request_id, track=False)
		self.assertTrue(again["already_submitted"])
		self.assertEqual(again["name"], first["name"])
		with self.assertRaises(service.EntregaMasivaError):  # everything is already delivered
			self._confirm(day, track=False)
		for pl in orders:
			self.assertEqual(len(self._obligations(pl)), 1)
			self.assertEqual(frappe.db.count("Recorrido Parada", {"pick_list": pl, "status": "Entregado"}), 1)

	def test_09_cartera_failure_rolls_back_that_order_only_and_retry_works(self):
		day = self._new_day()
		orders = self._orders(3, day, terms=("Crédito", 30))
		victim = orders[1]
		original = cartera_service.ensure_obligation_for_stop

		def failing(stop_name):
			if frappe.db.get_value("Recorrido Parada", stop_name, "pick_list") == victim:
				raise RuntimeError("Cartera no disponible (prueba)")
			return original(stop_name)

		with patch.object(cartera_service, "ensure_obligation_for_stop", side_effect=failing):
			result = self._confirm(day)
		self.assertEqual((result["delivered"], result["failed"]), (2, 1))
		self.assertEqual(result["status"], service.STATUS_DONE_WITH_ERRORS)
		self.assertIsNone(self._stop_of(victim))  # delivery reverted with Cartera
		self.assertFalse(self._obligations(victim))
		self.assertFalse(frappe.db.exists("Recorrido Parada", {"pick_list": victim}))  # stop removed from the admin route
		(route,) = result["administrative_routes"]
		self.assertEqual(frappe.db.get_value("Recorrido", route, "status"), "Completado")
		self.assertEqual(self._items(result["name"])[victim].result, service.RESULT_FAILED)
		self.assertIn("Cartera no disponible", self._items(result["name"])[victim].message)
		retry = self._confirm(day)
		self.assertEqual(retry["delivered"], 1)
		self.assertEqual(len(self._obligations(victim)), 1)

	def test_10_no_empty_administrative_route(self):
		day = self._new_day()
		self._orders(2, day)
		with patch.object(cartera_service, "ensure_obligation_for_stop", side_effect=RuntimeError("caída total (prueba)")):
			result = self._confirm(day)
		self.assertEqual((result["delivered"], result["failed"]), (0, 2))
		self.assertEqual(result["administrative_routes"], [])
		self.assertFalse(frappe.db.exists("Recorrido", {"entrega_masiva": result["name"]}))

	def test_11_worker_death_and_safe_recovery(self):
		day = self._new_day()
		orders = self._orders(5, day)
		calls = {"n": 0}
		original = service.process_chunk

		def dies_on_second_chunk(batch_name, item_names):
			calls["n"] += 1
			if calls["n"] == 2:
				original(batch_name, item_names)
				raise SystemExit("worker killed (prueba)")  # BaseException: nothing catches it
			return original(batch_name, item_names)

		frappe.flags.entrega_masiva_defer_job = True
		try:
			result = self._confirm(day)
		finally:
			frappe.flags.entrega_masiva_defer_job = False
		batch = frappe.get_doc("Entrega Masiva", result["name"])
		with patch.object(service, "CHUNK_SIZE", 2), patch.object(service, "process_chunk", side_effect=dies_on_second_chunk):
			with fx.as_user(self.admin_user), self.assertRaises(SystemExit):
				service.run_batch(batch.name, batch.worker_token)
		frappe.db.rollback()  # the dead worker's open chunk never commits
		self._track_batch(batch.name)
		status = frappe.get_doc("Entrega Masiva", batch.name)
		self.assertEqual(status.status, service.STATUS_RUNNING)
		self.assertEqual(status.delivered_count, 2)  # chunk 1 committed, chunk 2 lost
		self.assertEqual(status.active_company_lock, status.company)  # lock still held
		other_day = self._new_day()
		self._orders(1, other_day)
		with self.assertRaises(service.ActiveBatchError):
			self._confirm(other_day, track=False)
		with fx.as_user(self.admin_user), self.assertRaises(service.EntregaMasivaError):
			api.resume_bulk_delivery(batch.name)  # not stale yet
		# Test setup: the worker has been silent longer than STALE_AFTER_MINUTES.
		frappe.db.set_value("Entrega Masiva", batch.name, "heartbeat_on", add_days(nowdate(), -1), update_modified=False)
		with fx.as_user(self.admin_user):
			self.assertTrue(api.get_bulk_delivery_status(batch.name)["can_resume"])
			resumed = api.resume_bulk_delivery(batch.name)
		self._track_batch(batch.name)
		self.assertEqual((resumed["status"], resumed["delivered"]), (service.STATUS_DONE, 5))
		self.assertIsNone(frappe.db.get_value("Entrega Masiva", batch.name, "active_company_lock"))
		for pl in orders:
			self.assertEqual(len(self._obligations(pl)), 1, pl)
			self.assertEqual(frappe.db.count("Recorrido Parada", {"pick_list": pl, "status": "Entregado"}), 1, pl)

	def test_11b_old_worker_stops_after_takeover(self):
		day = self._new_day()
		self._orders(2, day)
		frappe.flags.entrega_masiva_defer_job = True
		try:
			result = self._confirm(day)
		finally:
			frappe.flags.entrega_masiva_defer_job = False
		old_token = frappe.db.get_value("Entrega Masiva", result["name"], "worker_token")
		frappe.db.set_value("Entrega Masiva", result["name"], "heartbeat_on", add_days(nowdate(), -1), update_modified=False)
		with fx.as_user(self.admin_user):
			api.resume_bulk_delivery(result["name"])  # new token, runs inline
			service.run_batch(result["name"], old_token)  # the old worker wakes up: no-op
		self._track_batch(result["name"])
		self.assertEqual(frappe.db.get_value("Entrega Masiva", result["name"], "delivered_count"), 2)

	def test_11c_close_releases_the_lock_without_processing(self):
		day = self._new_day()
		orders = self._orders(2, day)
		frappe.flags.entrega_masiva_defer_job = True
		try:
			result = self._confirm(day)
		finally:
			frappe.flags.entrega_masiva_defer_job = False
		frappe.db.set_value("Entrega Masiva", result["name"], "heartbeat_on", add_days(nowdate(), -1), update_modified=False)
		with fx.as_user(self.admin_user):
			closed = api.close_bulk_delivery(result["name"])
		self.assertEqual((closed["status"], closed["skipped"], closed["delivered"]), (service.STATUS_CLOSED, 2, 0))
		self.assertIsNone(frappe.db.get_value("Entrega Masiva", result["name"], "active_company_lock"))
		for pl in orders:
			self.assertIsNone(self._stop_of(pl))

	# =====================================================================
	# Dates
	# =====================================================================

	def test_12_invoice_date_mode_uses_each_order_own_day(self):
		day1, day2 = self._new_day(), self._new_day()
		(a,) = self._orders(1, day1)
		(b,) = self._orders(1, day2)
		with self.assertRaises(service.EntregaMasivaError):  # explicit confirmation required
			self._confirm(day1, day2, date_mode=service.DATE_MODE_INVOICE, track=False)
		result = self._confirm(day1, day2, date_mode=service.DATE_MODE_INVOICE, invoice_date_confirmed=1)
		self.assertEqual(result["delivered"], 2)
		for pl, day in ((a, day1), (b, day2)):
			stop = self._stop_of(pl)
			self.assertEqual(getdate(stop.effective_delivery_date), getdate(day))
			self.assertEqual(getdate(stop.delivered_on), getdate(day))
			self.assertNotEqual(getdate(stop.regularized_on), getdate(day))  # real execution time kept apart
			ob = frappe.get_doc("Cartera Obligacion", self._obligations(pl)[0])
			self.assertEqual(getdate(ob.delivery_date), getdate(day))

	def test_13_date_boundaries_and_validation(self):
		d1, d2, d3 = self._new_day(), self._new_day(), self._new_day()
		(first,) = self._orders(1, d1)
		self._move_invoice_day(first, d1, "00:00:00")
		(last,) = self._orders(1, d2)
		self._move_invoice_day(last, d2, "23:59:59")
		(outside,) = self._orders(1, d3)
		rows = {r["pick_list"] for r in self._preview(d1, d2)["rows"]}
		self.assertIn(first, rows)
		self.assertIn(last, rows)
		self.assertNotIn(outside, rows)
		for kwargs in (
			{"day_from": d2, "day_to": d1},
			{"day_from": d1, "day_to": str(add_days(nowdate(), 1))},
			{"day_from": "2021-01-01", "day_to": "2023-12-31"},
			{"day_from": d1, "effective_date": str(add_days(nowdate(), 1))},
		):
			with self.assertRaises(service.EntregaMasivaError, msg=kwargs):
				self._preview(kwargs.pop("day_from"), kwargs.pop("day_to", None), **kwargs)
		# Common date before the invoice -> excluded with its reason.
		preview = self._preview(d3, effective_date=str(add_days(d3, -1)))
		self.assertEqual(preview["summary"]["eligible"], 0)
		self.assertEqual(preview["summary"]["excluded_by_reason"][0]["code"], "factura_posterior")

	# =====================================================================
	# Concurrency with individual deliveries
	# =====================================================================

	def test_14_individual_then_bulk_and_bulk_then_individual(self):
		day = self._new_day()
		route = self._route_en_ruta(2, day)
		s1, s2 = route["stops"]
		self._deliver(route["name"], s1["name"], payment_status="Crédito")
		preview = self._preview(day, view="excluded")
		self.assertIn(s1["pick_list"], {r["pick_list"] for r in preview["rows"]})
		self._confirm(day)  # delivers s2 only
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Completado")
		with fx.as_user(self.recorrido_user):
			again = recorridos._deliver_stop(
				route["name"], s2["name"], None, _photo_jpeg(), _signature_png(), report={"payment_status": "Crédito"}
			)
		self.assertTrue(again["already_completed"])
		self.assertEqual(len(self._obligations(s2["pick_list"])), 1)

	def test_15_concurrent_individual_and_bulk_deliver_once(self):
		day = self._new_day()
		route = self._route_en_ruta(1, day)
		(stop,) = route["stops"]
		frappe.db.commit()
		site, results, errors = frappe.local.site, {}, []
		barrier = threading.Barrier(2)
		admin, driver = self.admin_user, self.recorrido_user
		photo, signature = _photo_jpeg(), _signature_png()

		def individual():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(driver)
				barrier.wait()
				results["individual"] = recorridos._deliver_stop(
					route["name"], stop["name"], None, photo, signature, report={"payment_status": "Crédito"}
				)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				errors.append(("individual", repr(e)))
			finally:
				frappe.destroy()

		def bulk():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(admin)
				barrier.wait()
				results["bulk"] = api.confirm_bulk_delivery(
					date_from=day, date_to=day, date_mode=service.DATE_MODE_COMMON, effective_date=nowdate(),
					reason="Concurrencia (prueba)", physical_delivery_confirmed=1, select_all=1,
					client_request_id=str(uuid.uuid4()),
				)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				errors.append(("bulk", repr(e)))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=individual), threading.Thread(target=bulk)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(120)
		frappe.db.rollback()
		if "bulk" in results:
			self._track_batch(results["bulk"]["name"])
		self._track_delivery_files(stop["name"])
		# Whatever the interleaving: delivered exactly once, one obligation.
		self.assertEqual(frappe.db.count("Recorrido Parada", {"pick_list": stop["pick_list"], "status": "Entregado"}), 1)
		self.assertEqual(len(self._obligations(stop["pick_list"])), 1)
		mode = frappe.db.get_value("Recorrido Parada", stop["name"], "delivery_mode")
		if mode == service.DELIVERY_MODE_REGULARIZATION:
			self.assertTrue(results["individual"]["already_completed"], errors)
		else:
			self.assertTrue(frappe.db.get_value("Recorrido Parada", stop["name"], "delivery_photo"))
			bulk_items = self._items(results["bulk"]["name"]) if "bulk" in results else {}
			self.assertNotEqual(bulk_items.get(stop["pick_list"], frappe._dict(result=None)).result, service.RESULT_DELIVERED)

	# =====================================================================
	# Security
	# =====================================================================

	def test_16_only_system_manager(self):
		day = self._new_day()
		for user in (self.recorrido_user, self.facturacion_user, self.cartera_user, self.bodega_user):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					api.get_bulk_delivery_preview(date_from=day, date_to=day, effective_date=nowdate())
				with self.assertRaises(frappe.PermissionError, msg=user):
					api.confirm_bulk_delivery(
						date_from=day, date_to=day, effective_date=nowdate(), reason="x" * 20,
						physical_delivery_confirmed=1, select_all=1, client_request_id=str(uuid.uuid4()),
					)
		for fn in (api.confirm_bulk_delivery, api.resume_bulk_delivery, api.close_bulk_delivery):
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func.get(fn), ["POST"])
		for fn in (service.run_batch, service.process_chunk, service.resume, service.close, service.create_batch):
			self.assertNotIn(fn, frappe.whitelisted)

	def test_17_reason_and_physical_confirmation_required(self):
		day = self._new_day()
		self._orders(1, day)
		for kw in ({"reason": "corto"}, {"physical_delivery_confirmed": 0}, {"client_request_id": "x"}):
			with self.assertRaises(service.EntregaMasivaError, msg=kw):
				self._confirm(day, track=False, **kw)
		self.assertFalse(frappe.db.exists("Entrega Masiva", {"date_from": day}))

	def test_18_rest_cannot_fake_the_administrative_mode(self):
		day = self._new_day()
		route = self._route_en_ruta(1, day)
		(stop,) = route["stops"]
		with fx.as_user(self.admin_user):
			doc = frappe.get_doc("Recorrido Parada", stop["name"])
			doc.update({
				"status": "Entregado", "delivery_mode": service.DELIVERY_MODE_REGULARIZATION, "entrega_masiva": None,
				"delivered_on": f"{nowdate()} 00:00:00", "delivered_by": self.admin_user, "payment_status": "Crédito",
				"effective_delivery_date": nowdate(), "regularized_on": frappe.utils.now_datetime(),
			})
			with self.assertRaises(frappe.PermissionError):
				doc.save()
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				frappe.client.set_value("Recorrido Parada", stop["name"], "delivery_mode", service.DELIVERY_MODE_REGULARIZATION)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Recorrido", "company": self.world_company(), "route_date": nowdate(), "route_kind": REG}).insert()
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Entrega Masiva", "company": self.world_company(), "status": "En cola", "reason": "falsa entrega",
					"date_from": day, "date_to": day, "date_mode": service.DATE_MODE_COMMON}).insert()
		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop["name"], "status"), "Pendiente")

	def world_company(self):
		from erpnext import get_default_company

		return get_default_company()

	def test_19_finished_administrative_deliveries_are_frozen(self):
		day = self._new_day()
		(pl,) = self._orders(1, day)
		result = self._confirm(day)
		stop = self._stop_of(pl)
		route_name = stop.recorrido
		with fx.as_user(self.admin_user):
			for field, value in (("effective_delivery_date", "2023-01-01"), ("payment_status", "Pagado"), ("delivery_mode", "Conductor")):
				doc = frappe.get_doc("Recorrido Parada", stop.name)
				doc.set(field, value)
				with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=field):
					doc.save()
			route = frappe.get_doc("Recorrido", route_name)
			route.notes = "editado"
			with self.assertRaises(frappe.PermissionError):
				route.save()
			with self.assertRaises(frappe.ValidationError):
				frappe.delete_doc("Recorrido Parada", stop.name)
			for endpoint in (recorridos.start_route, recorridos.cancel_route, recorridos.plan_route):
				with self.assertRaises(recorridos.RouteNotEditableError):
					endpoint(route_name)
			with self.assertRaises(recorridos.RouteNotEditableError):
				recorridos.update_route_stops(route_name, pick_lists=[pl])
			# No stop can be slipped into (or moved into) an administrative route.
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Recorrido Parada", "recorrido": route_name, "sequence": 99, "pick_list": pl, "status": "Pendiente"}).insert()
		operational = self._route_en_ruta(1, day)
		(other_stop,) = operational["stops"]
		for user in (self.admin_user, self.recorrido_user):
			with fx.as_user(user):
				doc = frappe.get_doc("Recorrido Parada", other_stop["name"])
				doc.recorrido = route_name
				with self.assertRaises(frappe.PermissionError, msg=user):
					doc.save()
		self.assertEqual(frappe.db.get_value("Recorrido Parada", other_stop["name"], "recorrido"), operational["name"])
		self.assertEqual(frappe.db.count("Recorrido Parada", {"recorrido": route_name}), 1)
		with fx.as_user(self.admin_user):
			item = frappe.get_doc("Entrega Masiva Pedido", {"entrega_masiva": result["name"], "pick_list": pl})
			item.result = "Fallido"
			with self.assertRaises(frappe.PermissionError):
				item.save()
			batch = frappe.get_doc("Entrega Masiva", result["name"])
			batch.reason = "otro motivo cualquiera"
			with self.assertRaises(frappe.PermissionError):
				batch.save()
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				frappe.delete_doc("Entrega Masiva", result["name"])
		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop.name, "payment_status"), "Pendiente por Pago")

	def test_20_operational_routes_keep_their_evidence_requirements(self):
		day = self._new_day()
		route = self._route_en_ruta(1, day)
		(stop,) = route["stops"]
		with fx.as_user(self.recorrido_user):
			doc = frappe.get_doc("Recorrido Parada", stop["name"])
			doc.update({"status": "Entregado", "delivered_on": frappe.utils.now_datetime(), "delivered_by": self.recorrido_user, "payment_status": "Crédito"})
			with self.assertRaises(frappe.ValidationError):  # no photo/signature
				doc.save()
			doc = frappe.get_doc("Recorrido Parada", stop["name"])
			doc.update({"status": "Entregado", "delivered_on": frappe.utils.now_datetime(), "delivered_by": self.recorrido_user,
				"payment_status": "Crédito", "delivery_photo": "/private/files/x.jpg", "customer_signature": "/private/files/y.png",
				"effective_delivery_date": nowdate()})
			with self.assertRaises(frappe.PermissionError):  # regularization data in a normal delivery
				doc.save()

	def test_21_no_inventory_or_accounting_movement(self):
		day = self._new_day()
		self._orders(4, day, terms=("Crédito", 30))
		route = self._route_en_ruta(1, day)
		before = self._accounting_counts()
		stock_before = frappe.db.sql("SELECT ROUND(SUM(actual_qty), 3) FROM `tabBin`")[0][0]
		result = self._confirm(day)
		self.assertEqual(result["delivered"], 5)
		self.assertEqual(self._accounting_counts(), before)
		self.assertEqual(frappe.db.sql("SELECT ROUND(SUM(actual_qty), 3) FROM `tabBin`")[0][0], stock_before)
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "Completado")

	def test_22_active_batch_blocks_a_second_one(self):
		day = self._new_day()
		self._orders(1, day)
		frappe.flags.entrega_masiva_defer_job = True
		try:
			result = self._confirm(day)
			with self.assertRaises(service.ActiveBatchError):
				day2 = self._new_day()
				self._orders(1, day2)
				self._confirm(day2, track=False)
		finally:
			frappe.flags.entrega_masiva_defer_job = False
		with fx.as_user(self.admin_user):
			self.assertEqual(api.get_active_bulk_delivery()["name"], result["name"])
			batch = frappe.get_doc("Entrega Masiva", result["name"])
			service.run_batch(batch.name, batch.worker_token)
		self._track_batch(result["name"])
		with fx.as_user(self.admin_user):
			self.assertIsNone(api.get_active_bulk_delivery())


class TestEntregaMasivaUiContract(IntegrationTestCase):
	def test_ui_contract(self):
		import os
		import re

		path = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "recorridos", "recorridos.js")
		with open(path) as f:
			js = f.read()
		self.assertIn('if (!frappe.user.has_role("System Manager")) return "";', js)
		for text in (
			"Entregar todos los pedidos",
			"Fecha inicial",
			"Fecha final",
			"Fecha efectiva de entrega",
			"Buscar pedidos",
			"Seleccionar todos los resultados (todas las páginas)",
			"Motivo de la regularización (obligatorio)",
			"Confirmo que estos pedidos fueron entregados físicamente.",
			"Confirmo que la fecha de facturación de cada pedido corresponde a su entrega real.",
			"Esta operación no se puede deshacer desde la interfaz.",
			"Confirmar entrega masiva",
			"Reanudar",
		):
			self.assertIn(text, js)
		called = set(re.findall(r'em_call\("([a-z_]+)"', js))
		self.assertEqual(
			called,
			{
				"get_active_bulk_delivery",
				"get_bulk_delivery_preview",
				"confirm_bulk_delivery",
				"get_bulk_delivery_status",
			},
		)
		for name in called | {"resume_bulk_delivery", "close_bulk_delivery"}:
			self.assertIn(getattr(api, name), frappe.whitelisted, name)
		self.assertIn('bulk_recovery("resume_bulk_delivery")', js)
		self.assertIn('bulk_recovery("close_bulk_delivery")', js)
