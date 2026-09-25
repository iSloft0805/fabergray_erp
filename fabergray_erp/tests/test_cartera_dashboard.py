# -*- coding: utf-8 -*-
"""Fase 27.2 -- Page Cartera read API (api/cartera.py): access, company
isolation, the five KPIs, chip filters, search, priority order,
pagination, detail, payment history, driver proof (+ IDOR), sync result,
serialization and date labels against the site's `today`.

Obligations are born from REAL deliveries (api.recorridos.deliver_stop()),
reusing the Recorridos/Cartera test helpers -- borrowed, never inherited.
Every customer of this class carries a random TOKEN, so filters/search/
order/pagination are asserted on exactly this class's rows even when the
database holds other obligations; KPIs are asserted as deltas and against
an independent recomputation over the whole company."""

import base64
import datetime
import inspect
import json
import os
import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, now_datetime, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.api import cartera as cartera_api
from fabergray_erp.api import facturacion
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_cartera_foundation as foundation
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base
from fabergray_erp.tests.test_recorridos_deliver_stop import _photo_jpeg

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

READ_ENDPOINTS = (
	"get_dashboard",
	"get_obligations",
	"get_obligation_detail",
	"get_driver_payment_proof",
	"get_payment_proof",
)
# 27.3 writes: the reconciler (27.1) + REGISTRAR COBRO + confirm/reject.
WRITE_ENDPOINTS = ("sync_missing_obligations", "register_payment", "confirm_driver_payment", "reject_driver_payment")
ALL_ENDPOINTS = (*READ_ENDPOINTS, *WRITE_ENDPOINTS)
_CARTERA_JS = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cartera", "cartera.js")

XSS = '<img src=x onerror="alert(1)">'


class TestCarteraDashboard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		# Fixture customers carry TOKEN; customers created by individual
		# tests carry OTHER_TOKEN, so the fixture set stays exactly 7 rows.
		cls.TOKEN = f"FG272{frappe.generate_hash(length=6).upper()}"
		cls.OTHER_TOKEN = f"FG272X{frappe.generate_hash(length=6).upper()}"
		cls._current_token = cls.OTHER_TOKEN

		cls.wh = cls.world.warehouse("FG272 WH")
		cls.item = cls.world.item("FG272-ITEM")
		cls.customer = cls.world.customer("FG272 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg272-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg272-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg272-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg272-recorrido-b@example.com", ["Recorrido"])
		cls.cartera_user = cls.world.user("fg272-cartera@example.com", ["Cartera"])
		cls.vendedora_user = cls.world.user("fg272-vendedora@example.com", ["Vendedora"])
		cls.jefe_user = cls.world.user("fg272-jefe@example.com", ["Jefe de Bodega"])
		cls.no_role_user = cls.world.user("fg272-norole@example.com", [])
		cls.system_manager_user = cls.world.user("fg272-sysmanager@example.com", ["System Manager"])
		cls.denied_users = (
			cls.no_role_user,
			cls.vendedora_user,
			cls.bodega_user,
			cls.jefe_user,
			cls.facturacion_user,
			cls.recorrido_user,
		)
		cls._seq = 0
		cls._fixture = None

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
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_route = start_base.TestRecorridosStartRoute._route
	_start = start_base.TestRecorridosStartRoute._start
	_en_ruta = deliver_base.TestRecorridosDeliverStop._en_ruta
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files
	_obligation_of = foundation.TestCarteraFoundation._obligation_of
	_ob = foundation.TestCarteraFoundation._ob
	_track_obligation = foundation.TestCarteraFoundation._track_obligation

	def _unique(self, label):
		"""Every customer name of this class carries a token (search key)."""
		type(self)._seq += 1
		return f"{type(self)._current_token} {label} {self._seq} {frappe.generate_hash(length=5)}"

	# -- Local helpers -----------------------------------------------------------
	def _delivered(self, **report):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, **report)
		obligation = self._obligation_of(stop_name)
		self._track_obligation(obligation)
		return obligation

	def _delivered_unavailable_amount(self):
		def refuse(pl, so):
			frappe.throw("factura parcial con impuestos", facturacion.InvoicePdfNotEligibleError)

		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with patch.object(facturacion, "_build_invoice_lines_and_totals", side_effect=refuse):
			self._deliver(route["name"], stop_name, payment_status="Crédito")
		obligation = self._obligation_of(stop_name)
		self._track_obligation(obligation)
		return obligation

	def _delivered_days_ago(self, days, **report):
		"""A delivery whose obligation is created as if delivered `days`
		ago (the stop's delivered_on is back-dated before the obligation
		derives from it)."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with patch.object(cartera_service, "ensure_obligation_after_delivery", return_value=None):
			self._deliver(route["name"], stop_name, **report)
		frappe.db.set_value("Recorrido Parada", stop_name, "delivered_on", add_days(now_datetime(), -days))
		obligation, created = cartera_service.ensure_obligation_for_stop(stop_name)
		self.assertTrue(created)
		self._track_obligation(obligation)
		return obligation

	def _fx(self):
		"""One delivered obligation per state, built once per class."""
		if type(self)._fixture is None:
			type(self)._current_token = self.TOKEN
			try:
				f = self._build_fixture()
			finally:
				type(self)._current_token = self.OTHER_TOKEN
			type(self)._fixture = f
		return type(self)._fixture

	def _build_fixture(self):
		if True:
			f = SimpleNamespace()
			f.pend = self._delivered(payment_status="Pendiente por Pago", payment_note=f"Transfiere {XSS} mañana")
			f.cred = self._delivered(payment_status="Crédito")
			f.paid = self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg(), payment_note="Efectivo")
			f.noproof = self._delivered(payment_status="Pagado")
			f.issues = self._delivered(payment_status="Crédito", has_delivery_issues="1", delivery_issues=f"Faltó {XSS}")
			f.unavail = self._delivered_unavailable_amount()
			f.overdue = self._delivered_days_ago(40, payment_status="Crédito")
			frappe.db.set_value("Customer", self._ob(f.pend).customer, "access_nombre_comercial", f"Tienda {XSS}")
			frappe.db.set_value("Customer", self._ob(f.cred).customer, "access_nombre_comercial", "Ferretería Única 100%")
			f.all = {f.pend, f.cred, f.paid, f.noproof, f.issues, f.unavail, f.overdue}
		return f

	def _list(self, user=None, **kwargs):
		kwargs.setdefault("search", self.TOKEN)
		kwargs.setdefault("page_length", 100)
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_obligations(**kwargs)

	def _names(self, **kwargs):
		return [row["name"] for row in self._list(**kwargs)["items"]]

	def _dashboard(self, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_dashboard()

	def _detail(self, name, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_obligation_detail(name)

	def _proof(self, name, user=None):
		with fx.as_user(user or self.cartera_user):
			return cartera_api.get_driver_payment_proof(name)

	def _pay(self, obligation, amount, payment_date=None):
		"""27.3: REGISTRAR COBRO through the service (the only writer)."""
		with fx.as_user(self.cartera_user):
			result = cartera_service.register_cartera_payment(
				obligation,
				amount,
				payment_date or nowdate(),
				"Transferencia",
				reference=f"REF {XSS}",
				client_request_id=str(uuid.uuid4()),
			)
		self.world.track_existing("Cartera Pago", result["payment"])
		return frappe.get_doc("Cartera Pago", result["payment"])

	def _execute_cmd(self, method, http_method="POST", **form_dict):
		"""Real RPC path (frappe.handler.execute_cmd: whitelist + HTTP method
		check + argument filtering), restoring request/form_dict."""
		from frappe.handler import execute_cmd

		previous_request = getattr(frappe.local, "request", None)
		previous_form_dict = frappe.local.form_dict
		frappe.local.request = SimpleNamespace(method=http_method, host=None)
		frappe.local.form_dict = frappe._dict(form_dict)
		try:
			return execute_cmd(f"fabergray_erp.api.cartera.{method}")
		finally:
			frappe.local.form_dict = previous_form_dict
			if previous_request is None:
				del frappe.local.request
			else:
				frappe.local.request = previous_request

	@staticmethod
	def _kpi(dashboard, key):
		value = dashboard["kpis"][key]
		return flt(value["amount"], 2), value["count"]

	# =====================================================================
	# Access
	# =====================================================================

	def test_page_roles(self):
		page = frappe.get_doc("Page", "cartera")
		self.assertEqual({row.role for row in page.roles}, {"Cartera", "System Manager"})
		for user in (self.cartera_user, self.system_manager_user):
			with fx.as_user(user):
				self.assertTrue(frappe.get_doc("Page", "cartera").is_permitted(), user)
		for user in self.denied_users:
			with fx.as_user(user):
				self.assertFalse(frappe.get_doc("Page", "cartera").is_permitted(), user)

	def test_endpoints_deny_every_other_role(self):
		f = self._fx()
		calls = {
			"get_dashboard": lambda: cartera_api.get_dashboard(),
			"get_obligations": lambda: cartera_api.get_obligations(),
			"get_obligation_detail": lambda: cartera_api.get_obligation_detail(f.paid),
			"get_driver_payment_proof": lambda: cartera_api.get_driver_payment_proof(f.paid),
			"sync_missing_obligations": lambda: cartera_api.sync_missing_obligations(),
		}
		for user in self.denied_users:
			for name, call in calls.items():
				with fx.as_user(user):
					with self.assertRaises(frappe.PermissionError, msg=f"{user} {name}"):
						call()
		with fx.as_user("Guest"):
			for name, call in calls.items():
				with self.assertRaises((frappe.AuthenticationError, frappe.PermissionError), msg=name):
					call()
		# Cartera and System Manager get through.
		for user in (self.cartera_user, self.system_manager_user):
			with fx.as_user(user):
				self.assertIn("kpis", cartera_api.get_dashboard())
				self.assertIn("items", cartera_api.get_obligations(search=self.TOKEN))
				self.assertEqual(cartera_api.get_obligation_detail(f.paid)["name"], f.paid)
				self.assertEqual(cartera_api.get_driver_payment_proof(f.paid)["content_type"], "image/jpeg")

	def test_whitelist_contract(self):
		module_functions = {
			name: fn
			for name, fn in inspect.getmembers(cartera_api, inspect.isfunction)
			if fn.__module__ == cartera_api.__name__
		}
		whitelisted = {name for name, fn in module_functions.items() if fn in frappe.whitelisted}
		# 27.2 adds read endpoints only; the single write is the 27.1 sync.
		self.assertEqual(whitelisted, set(ALL_ENDPOINTS))
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[cartera_api.sync_missing_obligations], ["POST"])
		self.assertEqual(list(inspect.signature(cartera_api.get_driver_payment_proof).parameters), ["obligation_name"])
		self.assertEqual(list(inspect.signature(cartera_api.get_obligation_detail).parameters), ["obligation_name"])

		with open(_CARTERA_JS, encoding="utf-8") as fh:
			source = fh.read()
		prefix = re.search(r'this\.method_prefix\s*=\s*"([^"]+)"', source)
		self.assertEqual(prefix.group(1), "fabergray_erp.api.cartera.")
		js_methods = set(re.findall(r'this\.(?:call|run_action|show_proof)\(\s*"([A-Za-z0-9_]+)"', source))
		js_methods.update(re.findall(r'"fabergray_erp\.api\.cartera\.([A-Za-z0-9_]+)"', source))
		self.assertEqual(js_methods, set(ALL_ENDPOINTS))
		# Every write is POST-only.
		for name in WRITE_ENDPOINTS:
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[getattr(cartera_api, name)], ["POST"], name)

		# Real dispatch through the /api/method entry point.
		with fx.as_user(self.cartera_user):
			self.assertIn("kpis", self._execute_cmd("get_dashboard", http_method="GET"))
			self.assertIn("items", self._execute_cmd("get_obligations", filter="vencidos", search=self.TOKEN))
			with self.assertRaises(frappe.PermissionError):
				self._execute_cmd("_company")
			with self.assertRaises(frappe.PermissionError):
				self._execute_cmd("sync_missing_obligations", http_method="GET")

	def test_company_isolation(self):
		f = self._fx()
		# Caller restricted to another company -> every endpoint refuses.
		with patch.object(cartera_api, "_allowed_companies", return_value=["_Test Company"]):
			for user in (self.cartera_user,):
				with fx.as_user(user):
					for call in (
						lambda: cartera_api.get_dashboard(),
						lambda: cartera_api.get_obligations(),
						lambda: cartera_api.get_obligation_detail(f.cred),
						lambda: cartera_api.get_driver_payment_proof(f.paid),
						lambda: cartera_api.sync_missing_obligations(),
					):
						with self.assertRaises(frappe.PermissionError):
							call()

		# An obligation of another company: invisible in the list, refused
		# by name (hook for Cartera; explicit company check for System
		# Manager too, since the Page only works on the site's company).
		frappe.db.set_value("Cartera Obligacion", f.issues, "company", "_Test Company", update_modified=False)
		try:
			self.assertNotIn(f.issues, self._names())
			for user in (self.cartera_user, self.system_manager_user):
				with self.assertRaises(frappe.PermissionError, msg=user):
					self._detail(f.issues, user=user)
			before = self._kpi(self._dashboard(), "cartera_actual")
		finally:
			frappe.db.set_value("Cartera Obligacion", f.issues, "company", get_default_company(), update_modified=False)
		after = self._kpi(self._dashboard(), "cartera_actual")
		self.assertEqual(flt(after[0] - before[0], 2), flt(self._ob(f.issues).outstanding_amount, 2))

	# =====================================================================
	# KPIs
	# =====================================================================

	def test_kpis_match_an_independent_recomputation(self):
		self._fx()
		dashboard = self._dashboard()
		company = get_default_company()
		today = getdate(nowdate())
		rows = frappe.get_all(
			"Cartera Obligacion",
			filters={"company": company, "status": ["!=", "Anulada"]},
			fields=["name", "outstanding_amount", "due_date", "status", "payment_verification"],
		)
		owing = [r for r in rows if flt(r.outstanding_amount) > 0]
		overdue = [r for r in owing if r.due_date and getdate(r.due_date) < today]
		upcoming = [r for r in owing if not r.due_date or getdate(r.due_date) >= today]
		month_start = today.replace(day=1)
		payments = frappe.get_all(
			"Cartera Pago",
			filters={"company": company, "docstatus": 1, "payment_date": [">=", month_start]},
			fields=["amount", "payment_date", "source", "cartera_obligacion"],
		)
		this_month = [p for p in payments if getdate(p.payment_date).month == today.month]
		unconfirmed = {r.name for r in rows if r.status == "Pagado" and r.payment_verification == "Sin confirmar"}
		driver_payments = frappe.get_all(
			"Cartera Pago",
			filters={"docstatus": 1, "source": "Conductor", "cartera_obligacion": ["in", list(unconfirmed) or [""]]},
			fields=["amount", "cartera_obligacion"],
		)

		def total(items, field):
			return flt(sum(flt(i[field]) for i in items), 2)

		self.assertEqual(self._kpi(dashboard, "cartera_actual"), (total(owing, "outstanding_amount"), len(owing)))
		self.assertEqual(self._kpi(dashboard, "vencida"), (total(overdue, "outstanding_amount"), len(overdue)))
		self.assertEqual(self._kpi(dashboard, "por_vencer"), (total(upcoming, "outstanding_amount"), len(upcoming)))
		self.assertEqual(
			flt(self._kpi(dashboard, "por_vencer")[0] + self._kpi(dashboard, "vencida")[0], 2),
			self._kpi(dashboard, "cartera_actual")[0],
		)
		self.assertEqual(self._kpi(dashboard, "cobrado_mes"), (total(this_month, "amount"), len(this_month)))
		self.assertEqual(
			self._kpi(dashboard, "por_confirmar"),
			(total(driver_payments, "amount"), len({p.cartera_obligacion for p in driver_payments})),
		)
		self.assertEqual(dashboard["company"], company)
		self.assertEqual(dashboard["currency"], "COP")
		self.assertEqual(dashboard["today"], str(today))

	def test_kpi_deltas_per_scenario(self):
		self._fx()
		k0 = self._dashboard()

		# Crédito: +cartera actual, +por vencer, vencida unchanged.
		cred = self._delivered(payment_status="Crédito")
		amount = flt(self._ob(cred).invoice_amount, 2)
		k1 = self._dashboard()
		self.assertEqual(self._kpi(k1, "cartera_actual")[0], flt(self._kpi(k0, "cartera_actual")[0] + amount, 2))
		self.assertEqual(self._kpi(k1, "cartera_actual")[1], self._kpi(k0, "cartera_actual")[1] + 1)
		self.assertEqual(self._kpi(k1, "por_vencer")[0], flt(self._kpi(k0, "por_vencer")[0] + amount, 2))
		self.assertEqual(self._kpi(k1, "vencida"), self._kpi(k0, "vencida"))
		self.assertEqual(self._kpi(k1, "cobrado_mes"), self._kpi(k0, "cobrado_mes"))

		# Pagado + comprobante: saldo 0 -> NOT in cartera actual; the driver
		# payment counts as collected this month and as "por confirmar".
		paid = self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg())
		paid_amount = flt(self._ob(paid).invoice_amount, 2)
		k2 = self._dashboard()
		self.assertEqual(self._kpi(k2, "cartera_actual"), self._kpi(k1, "cartera_actual"))
		self.assertEqual(self._kpi(k2, "cobrado_mes")[0], flt(self._kpi(k1, "cobrado_mes")[0] + paid_amount, 2))
		self.assertEqual(self._kpi(k2, "por_confirmar")[0], flt(self._kpi(k1, "por_confirmar")[0] + paid_amount, 2))
		self.assertEqual(self._kpi(k2, "por_confirmar")[1], self._kpi(k1, "por_confirmar")[1] + 1)

		# Pagado sin comprobante: Por Validar with balance -> cartera actual,
		# never "por confirmar" (nothing was paid).
		noproof = self._delivered(payment_status="Pagado")
		noproof_amount = flt(self._ob(noproof).invoice_amount, 2)
		k3 = self._dashboard()
		self.assertEqual(self._kpi(k3, "cartera_actual")[0], flt(self._kpi(k2, "cartera_actual")[0] + noproof_amount, 2))
		self.assertEqual(self._kpi(k3, "por_confirmar"), self._kpi(k2, "por_confirmar"))

		# A Cartera payment this month + one of an earlier month.
		part = flt(amount / 4, 2)
		self._pay(cred, part)
		self._pay(cred, part, payment_date=add_days(getdate(nowdate()).replace(day=1), -1))
		k4 = self._dashboard()
		self.assertEqual(self._kpi(k4, "cobrado_mes")[0], flt(self._kpi(k3, "cobrado_mes")[0] + part, 2))
		self.assertEqual(self._kpi(k4, "cartera_actual")[0], flt(self._kpi(k3, "cartera_actual")[0] - 2 * part, 2))
		self.assertEqual(self._kpi(k4, "por_confirmar"), self._kpi(k3, "por_confirmar"))
		# A cancelled payment (27.3: only the rejected driver payment) never
		# counts as collected.
		with fx.as_user(self.cartera_user):
			cartera_service.reject_driver_payment(paid, "El dinero no llegó")
		k5 = self._dashboard()
		self.assertEqual(self._kpi(k5, "cobrado_mes")[0], flt(self._kpi(k4, "cobrado_mes")[0] - paid_amount, 2))
		self.assertEqual(self._kpi(k5, "cartera_actual")[0], flt(self._kpi(k4, "cartera_actual")[0] + paid_amount, 2))

	def test_vencida_uses_the_site_today(self):
		f = self._fx()
		cred = self._ob(f.cred)
		overdue = self._ob(f.overdue)
		self.assertLess(getdate(overdue.due_date), getdate(nowdate()))
		self.assertIn(f.overdue, self._names(filter="vencidos"))
		self.assertNotIn(f.cred, self._names(filter="vencidos"))

		# One day after the credit's due date the SAME obligation is overdue.
		later = add_days(getdate(cred.due_date), 1)
		with patch.object(cartera_api, "_today", return_value=getdate(later)):
			self.assertIn(f.cred, self._names(filter="vencidos"))
			row = next(r for r in self._list()["items"] if r["name"] == f.cred)
			self.assertEqual(row["bucket"], "vencido")
			self.assertEqual(row["days_to_due"], -1)
			shifted = self._dashboard()
		self.assertGreaterEqual(
			self._kpi(shifted, "vencida")[0], flt(cred.outstanding_amount + overdue.outstanding_amount, 2)
		)

		# On the due date itself: por vencer ("VENCE HOY"), not overdue.
		with patch.object(cartera_api, "_today", return_value=getdate(cred.due_date)):
			self.assertNotIn(f.cred, self._names(filter="vencidos"))
			row = next(r for r in self._list()["items"] if r["name"] == f.cred)
			self.assertEqual((row["bucket"], row["days_to_due"]), ("por_vencer", 0))

	def test_today_is_the_site_calendar_day_never_the_db_clock(self):
		self.assertEqual(cartera_api._today(), getdate(nowdate()))
		with open(cartera_api.__file__, encoding="utf-8") as fh:
			source = fh.read().upper()
		for forbidden in ("CURDATE(", "CURRENT_DATE", "NOW()", "SYSDATE(", "UTC_DATE("):
			self.assertNotIn(forbidden, source.replace("NOWDATE()", ""), forbidden)

	def test_month_label_and_range(self):
		with patch.object(cartera_api, "_today", return_value=datetime.date(2026, 9, 15)):
			dashboard = self._dashboard()
		self.assertEqual(dashboard["month"], {"label": "SEPTIEMBRE", "start": "2026-09-01", "end": "2026-09-30"})
		with patch.object(cartera_api, "_today", return_value=datetime.date(2028, 2, 3)):
			self.assertEqual(self._dashboard()["month"]["end"], "2028-02-29")

	# =====================================================================
	# Filters / search / order / pagination
	# =====================================================================

	def test_filters(self):
		f = self._fx()
		expected = {
			"todos": f.all,
			"pendientes": {f.pend, f.cred, f.issues, f.overdue},
			"credito": {f.cred, f.issues, f.overdue},
			"vencidos": {f.overdue},
			"pagados": {f.paid},
			"por_validar": {f.noproof, f.unavail},
			"por_confirmar": {f.paid},
		}
		for key, names in expected.items():
			self.assertEqual(set(self._names(filter=key)), names, key)
		with self.assertRaises(frappe.ValidationError):
			self._list(filter="todo; DROP TABLE x")

	def test_por_confirmar_is_one_population_for_kpi_filter_and_rows(self):
		f = self._fx()
		paid = self._ob(f.paid)

		def state():
			dashboard = self._dashboard()
			with fx.as_user(self.cartera_user):
				company_wide = cartera_api.get_obligations(filter="por_confirmar", page_length=100)
			names = [row["name"] for row in company_wide["items"]]
			driver_total = flt(
				sum(
					flt(p.amount)
					for p in frappe.get_all(
						"Cartera Pago",
						filters={"docstatus": 1, "source": "Conductor", "cartera_obligacion": ["in", names or [""]]},
						fields=["amount"],
					)
				),
				2,
			)
			rows = {row["name"]: row for row in self._list()["items"]}
			return self._kpi(dashboard, "por_confirmar"), company_wide["total"], names, driver_total, rows

		def assert_consistent(kpi, total, names, driver_total):
			# KPI and chip can never contradict each other: same count, and
			# the KPI amount is exactly the driver payments of the listed rows.
			self.assertEqual(kpi[1], total)
			self.assertEqual(len(names), total)
			self.assertEqual(kpi[0], driver_total)

		kpi, total, names, driver_total, rows = state()
		assert_consistent(kpi, total, names, driver_total)

		# A. Pagado + comprobante + pago Conductor + "Sin confirmar": KPI and filter.
		self.assertIn(f.paid, names)
		self.assertTrue(rows[f.paid]["por_confirmar"])
		self.assertTrue(self._detail(f.paid)["por_confirmar"])

		# B. Pagado SIN comprobante: Por Validar, full balance, no payment ->
		#    not POR CONFIRMAR (KPI nor filter), only POR VALIDAR.
		noproof = self._ob(f.noproof)
		self.assertEqual((noproof.status, noproof.payment_verification), ("Por Validar", "Sin confirmar"))
		self.assertEqual(flt(noproof.outstanding_amount), flt(noproof.invoice_amount))
		self.assertEqual(frappe.db.count("Cartera Pago", {"cartera_obligacion": f.noproof}), 0)
		self.assertNotIn(f.noproof, names)
		self.assertFalse(rows[f.noproof]["por_confirmar"])
		self.assertFalse(self._detail(f.noproof)["por_confirmar"])
		self.assertIn(f.noproof, self._names(filter="por_validar"))

		# C. A future confirmation (27.3; simulated here -- no action exists
		#    yet) takes the obligation out of POR CONFIRMAR everywhere.
		frappe.db.set_value("Cartera Obligacion", f.paid, "payment_verification", "Confirmado", update_modified=False)
		try:
			kpi_c, total_c, names_c, driver_total_c, rows_c = state()
			assert_consistent(kpi_c, total_c, names_c, driver_total_c)
			self.assertNotIn(f.paid, names_c)
			self.assertEqual(total_c, total - 1)
			self.assertEqual(kpi_c[0], flt(kpi[0] - flt(paid.invoice_amount), 2))
			self.assertFalse(rows_c[f.paid]["por_confirmar"])
			self.assertFalse(self._detail(f.paid)["por_confirmar"])
			self.assertIn(f.paid, self._names(filter="pagados"))
		finally:
			frappe.db.set_value("Cartera Obligacion", f.paid, "payment_verification", "Sin confirmar", update_modified=False)
		self.assertIn(f.paid, self._names(filter="por_confirmar"))

	def test_buckets_and_priority_order(self):
		f = self._fx()
		items = self._list()["items"]
		buckets = {row["name"]: row["bucket"] for row in items}
		self.assertEqual(
			buckets,
			{
				f.overdue: "vencido",
				f.cred: "por_vencer",
				f.issues: "por_vencer",
				f.pend: "pendiente",
				f.noproof: "por_validar",
				f.unavail: "por_validar",
				f.paid: "pagado",
			},
		)
		# vencido -> por vencer (due_date asc, then name) -> pendiente ->
		# por validar (delivery_date asc, then name) -> pagado.
		expected = [f.overdue, *sorted([f.cred, f.issues]), f.pend, *sorted([f.noproof, f.unavail]), f.paid]
		self.assertEqual([row["name"] for row in items], expected)
		# Same order for PENDIENTES.
		self.assertEqual(self._names(filter="pendientes"), [f.overdue, *sorted([f.cred, f.issues]), f.pend])
		# The Python twin of the SQL bucket agrees with the list.
		for row in items:
			self.assertEqual(self._detail(row["name"])["bucket"], row["bucket"], row["name"])

	def test_search(self):
		f = self._fx()
		pend = self._ob(f.pend)
		self.assertEqual(set(self._names()), f.all)
		self.assertEqual(set(self._names(search=self.TOKEN.lower())), f.all)  # case-insensitive
		for value in (pend.customer_name, pend.customer, pend.commercial_name, pend.sales_order, pend.pick_list, pend.recorrido):
			found = self._names(search=value)
			self.assertIn(f.pend, found, value)
			# "PEDIDO-12" may also be a substring of "PEDIDO-120": among this
			# class's rows only the searched one matches.
			self.assertEqual(set(found) & f.all, {f.pend}, value)
		# Customer's own commercial name (access_nombre_comercial).
		self.assertEqual(self._names(search="Ferretería Única"), [f.cred])
		self.assertIn(f.cred, self._names(search="Única 100%"))
		# LIKE wildcards are literal: "%" and "_" never match everything.
		self.assertEqual(self._names(search=f"{self.TOKEN}%Cliente"), [])
		self.assertEqual(self._names(search=f"{self.TOKEN}_"), [])
		# Search combines with the chip filter.
		self.assertEqual(self._names(filter="pagados", search=pend.customer_name), [])
		result = self._list(search=f"  {pend.customer_name}  ")
		self.assertEqual(result["search"], pend.customer_name)

	def test_pagination(self):
		f = self._fx()
		pages = [self._list(page=p, page_length=3) for p in (1, 2, 3)]
		self.assertEqual([p["total"] for p in pages], [7, 7, 7])
		self.assertEqual([len(p["items"]) for p in pages], [3, 3, 1])
		self.assertEqual([p["has_more"] for p in pages], [True, True, False])
		names = [row["name"] for p in pages for row in p["items"]]
		self.assertEqual(len(names), len(set(names)))
		self.assertEqual(names, self._names())
		self.assertEqual(set(names), f.all)
		self.assertEqual(self._list(page=4, page_length=3)["items"], [])
		self.assertEqual(self._list(page_length=1000)["page_length"], cartera_api.MAX_PAGE_LENGTH)
		self.assertEqual(self._list(page=0)["page"], 1)
		default = self._list(page_length=None)
		self.assertEqual(default["page_length"], cartera_api.DEFAULT_PAGE_LENGTH)

	def test_list_is_constant_queries_not_n_plus_one(self):
		self._fx()

		def count_queries(page_length):
			original = frappe.db.sql
			calls = []

			def counting(*args, **kwargs):
				calls.append(args[0] if args else "")
				return original(*args, **kwargs)

			with patch.object(frappe.db, "sql", side_effect=counting):
				self._list(page_length=page_length)
			return len([q for q in calls if "Cartera" in str(q) or "tabCustomer" in str(q)])

		self.assertEqual(count_queries(1), count_queries(7))
		self.assertEqual(count_queries(7), 2)  # COUNT + page

	# =====================================================================
	# Detail / history / issues / serialization
	# =====================================================================

	def test_detail(self):
		f = self._fx()
		ob = self._ob(f.issues)
		d = self._detail(f.issues)
		for field in ("customer", "customer_name", "sales_order", "pick_list", "recorrido", "delivered_by"):
			self.assertEqual(d[field], ob.get(field), field)
		self.assertEqual(d["commercial_name"], ob.commercial_name)
		self.assertEqual(d["delivery_date"], str(getdate(ob.delivery_date)))
		self.assertEqual(d["due_date"], str(getdate(ob.due_date)))
		self.assertEqual(d["credit_days"], 30)
		self.assertEqual(d["invoice_amount"], flt(ob.invoice_amount, 2))
		self.assertEqual(d["paid_amount"], 0)
		self.assertEqual(d["outstanding_amount"], flt(ob.invoice_amount, 2))
		self.assertEqual(d["driver_payment_status"], "Crédito")
		self.assertEqual(d["delivered_by_name"], frappe.utils.get_fullname(ob.delivered_by))
		self.assertEqual(d["days_to_due"], (getdate(ob.due_date) - getdate(nowdate())).days)
		self.assertEqual(d["payments"], [])
		self.assertFalse(d["has_driver_proof"])
		# Faltantes/cambios are shown, never change the money.
		self.assertEqual(d["has_delivery_issues"], 1)
		# (deliver_stop() already strips HTML from the driver's text.)
		self.assertEqual(d["delivery_issues"], ob.delivery_issues)
		self.assertIn("Faltó", d["delivery_issues"])
		self.assertEqual(d["outstanding_amount"], d["invoice_amount"])

		unavailable = self._detail(f.unavail)
		self.assertFalse(unavailable["amount_available"])
		self.assertEqual(unavailable["status"], "Por Validar")

		with self.assertRaises(frappe.DoesNotExistError):
			self._detail("CART-NO-EXISTE")
		with self.assertRaises(frappe.ValidationError):
			self._detail({"name": f.issues})

	def test_driver_reported_payment_history(self):
		f = self._fx()
		d = self._detail(f.paid)
		self.assertEqual((d["status"], d["payment_verification"]), ("Pagado", "Sin confirmar"))
		self.assertTrue(d["has_driver_proof"])
		self.assertEqual(d["driver_payment_note"], "Efectivo")
		self.assertEqual(len(d["payments"]), 1)
		payment = d["payments"][0]
		self.assertEqual(payment["source"], "Conductor")
		self.assertEqual(payment["amount"], d["invoice_amount"])
		self.assertEqual(payment["accounting_status"], "Sin contabilizar")
		self.assertTrue(payment["has_payment_proof"])
		self.assertTrue(payment["proof_is_driver_proof"])
		self.assertFalse(payment["cancelled"])
		self.assertTrue(payment["recorded_by"])
		self.assertTrue(payment["recorded_on"])
		self.assertEqual(payment["payment_date"], d["delivery_date"])

		no_proof = self._detail(f.noproof)
		self.assertEqual((no_proof["status"], no_proof["payment_verification"]), ("Por Validar", "Sin confirmar"))
		self.assertFalse(no_proof["has_driver_proof"])
		self.assertEqual(no_proof["payments"], [])

	def test_history_shows_cancelled_but_not_drafts(self):
		# 27.3: drafts cannot exist at all (the service inserts + submits in
		# one transaction); the cancelled row is the rejected driver payment.
		cred = self._delivered(payment_status="Pagado", payment_proof=_photo_jpeg())
		driver_payment = frappe.get_all("Cartera Pago", filters={"cartera_obligacion": cred}, pluck="name")[0]
		with fx.as_user(self.cartera_user):
			cartera_service.reject_driver_payment(cred, "No llegó el dinero")
		quarter = flt(flt(self._ob(cred).invoice_amount) / 4, 2)
		kept = self._pay(cred, quarter)
		with fx.as_user("Administrator"):
			draft = frappe.get_doc(
				{"doctype": "Cartera Pago", "cartera_obligacion": cred, "amount": quarter, "payment_date": nowdate(),
				 "payment_method": "Efectivo", "client_request_id": f"cartera:{uuid.uuid4()}"}
			)
			with self.assertRaises(frappe.PermissionError):
				draft.insert()
		history = {p["name"]: p for p in self._detail(cred)["payments"]}
		self.assertEqual(set(history), {kept.name, driver_payment})
		cancelled = history[driver_payment]
		self.assertTrue(cancelled["cancelled"])
		self.assertEqual(cancelled["cancellation_reason"], "No llegó el dinero")
		self.assertEqual(cancelled["cancelled_by_name"], frappe.utils.get_fullname(self.cartera_user))
		self.assertTrue(cancelled["cancelled_on"])
		self.assertFalse(history[kept.name]["cancelled"])
		self.assertIsNone(history[kept.name]["cancellation_reason"])
		self.assertEqual(history[kept.name]["source"], "Cartera")
		self.assertEqual(history[kept.name]["payment_method"], "Transferencia")
		# Frappe sanitizes Data on save; the API returns exactly what is stored.
		self.assertEqual(history[kept.name]["reference"], frappe.db.get_value("Cartera Pago", kept.name, "reference"))
		self.assertEqual(history[kept.name]["recorded_by"], self.cartera_user)
		self.assertFalse(history[kept.name]["has_payment_proof"])

	def test_serialization_is_raw_json_without_private_urls(self):
		f = self._fx()
		listing = self._list()
		detail = self._detail(f.pend)
		paid = self._detail(f.paid)
		for payload in (listing, detail, paid, self._dashboard()):
			text = frappe.as_json(payload)
			json.loads(text)
			self.assertNotIn("driver_payment_proof", text)
			self.assertNotIn("/private/files", text)
			self.assertNotIn("/files/", text)
		# Untrusted text travels raw (the client escapes it; never pre-rendered HTML).
		self.assertEqual(detail["driver_payment_note"], self._ob(f.pend).driver_payment_note)
		self.assertEqual(detail["customer_commercial_name"], f"Tienda {XSS}")
		row = next(r for r in listing["items"] if r["name"] == f.pend)
		self.assertEqual(row["customer_commercial_name"], f"Tienda {XSS}")

	def test_date_labels_follow_the_site_today(self):
		f = self._fx()
		ob = self._ob(f.pend)
		fixed = add_days(getdate(ob.delivery_date), 5)
		with patch.object(cartera_api, "_today", return_value=getdate(fixed)):
			row = next(r for r in self._list()["items"] if r["name"] == f.pend)
			credit = next(r for r in self._list()["items"] if r["name"] == f.cred)
		self.assertEqual(row["days_since_delivery"], 5)
		self.assertIsNone(row["days_to_due"])
		self.assertIsNone(row["due_date"])
		self.assertEqual(credit["days_to_due"], 25)

	def test_read_endpoints_write_nothing(self):
		f = self._fx()
		snapshot = lambda: (  # noqa: E731
			frappe.get_all("Cartera Obligacion", filters={"name": ["in", list(f.all)]}, fields=["name", "modified"], order_by="name"),
			frappe.get_all("Cartera Pago", filters={"cartera_obligacion": ["in", list(f.all)]}, fields=["name", "modified"], order_by="name"),
			{dt: frappe.db.count(dt) for dt in ("Payment Entry", "Sales Invoice", "Journal Entry", "GL Entry", "Cartera Pago")},
		)
		before = snapshot()
		self._dashboard()
		self._list()
		for name in f.all:
			self._detail(name)
		self._proof(f.paid)
		self.assertEqual(snapshot(), before)

	# =====================================================================
	# Driver proof -- controlled endpoint + IDOR
	# =====================================================================

	def test_driver_proof_is_served_from_the_exact_private_file(self):
		f = self._fx()
		ob = self._ob(f.paid)
		stop = frappe.get_doc("Recorrido Parada", ob.recorrido_parada)
		file_doc = frappe.get_doc("File", {"file_url": stop.payment_proof, "attached_to_name": stop.name})
		for user in (self.cartera_user, self.system_manager_user):
			res = self._proof(f.paid, user=user)
			self.assertEqual(res["content_type"], "image/jpeg")
			self.assertEqual(base64.b64decode(res["data"]), file_doc.get_content())
			self.assertEqual(set(res), {"obligation", "content_type", "data"})
		# Cartera still has no generic read on the stop or the File.
		self.assertFalse(frappe.has_permission("File", "read", doc=file_doc, user=self.cartera_user))
		self.assertFalse(frappe.has_permission("Recorrido Parada", "read", user=self.cartera_user))
		with self.assertRaises(frappe.DoesNotExistError):
			self._proof(f.noproof)
		with self.assertRaises(frappe.DoesNotExistError):
			self._proof("CART-NO-EXISTE")

	def test_driver_proof_idor(self):
		f = self._fx()
		ob = self._ob(f.paid)
		stop = frappe.get_doc("Recorrido Parada", ob.recorrido_parada)
		proof_file = frappe.get_doc("File", {"file_url": stop.payment_proof, "attached_to_name": stop.name}).name

		def refused(label, exc=frappe.PermissionError):
			with self.assertRaises(exc, msg=label):
				self._proof(f.paid)

		# 1. The obligation points at another private File of the stop
		#    (the delivery photo) -> refused: it is not the stop's proof.
		frappe.db.set_value("Cartera Obligacion", f.paid, "driver_payment_proof", stop.delivery_photo, update_modified=False)
		try:
			refused("obligation url tampered")
		finally:
			frappe.db.set_value("Cartera Obligacion", f.paid, "driver_payment_proof", stop.payment_proof, update_modified=False)

		# 2. Same URL, but the File is no longer the stop's payment_proof field.
		frappe.db.set_value("File", proof_file, "attached_to_field", "delivery_photo", update_modified=False)
		try:
			refused("attached_to_field")
		finally:
			frappe.db.set_value("File", proof_file, "attached_to_field", "payment_proof", update_modified=False)

		# 3. The File is attached to another document.
		frappe.db.set_value("File", proof_file, "attached_to_name", ob.recorrido_parada + "-X", update_modified=False)
		try:
			refused("attached_to_name")
		finally:
			frappe.db.set_value("File", proof_file, "attached_to_name", ob.recorrido_parada, update_modified=False)

		# 4. A public File is never served through this endpoint.
		frappe.db.set_value("File", proof_file, "is_private", 0, update_modified=False)
		try:
			refused("is_private")
		finally:
			frappe.db.set_value("File", proof_file, "is_private", 1, update_modified=False)

		# 5. Obligation of another company.
		frappe.db.set_value("Cartera Obligacion", f.paid, "company", "_Test Company", update_modified=False)
		try:
			refused("company")
			with self.assertRaises(frappe.PermissionError):
				self._proof(f.paid, user=self.system_manager_user)
		finally:
			frappe.db.set_value("Cartera Obligacion", f.paid, "company", get_default_company(), update_modified=False)

		# 6. Extra client arguments (a file URL/name) are ignored by the
		#    real dispatcher: only obligation_name exists.
		with fx.as_user(self.cartera_user):
			res = self._execute_cmd(
				"get_driver_payment_proof", obligation_name=f.paid, file_url=stop.delivery_photo, file_name="x"
			)
		proof_content = frappe.get_doc("File", proof_file).get_content()
		self.assertEqual(base64.b64decode(res["data"]), proof_content)

		# Everything restored: served again.
		self.assertEqual(self._proof(f.paid)["content_type"], "image/jpeg")

	# =====================================================================
	# SINCRONIZAR
	# =====================================================================

	def test_sync_reports_created_existing_and_errors(self):
		self._fx()
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with patch.object(cartera_service, "ensure_obligation_after_delivery", return_value=None):
			self._deliver(route["name"], stop_name, payment_status="Crédito")
		self.assertIsNone(self._obligation_of(stop_name))

		with fx.as_user(self.cartera_user):
			first = cartera_api.sync_missing_obligations()
		self._track_obligation(self._obligation_of(stop_name))
		self.assertIn(self._obligation_of(stop_name), first["obligations"])
		self.assertGreaterEqual(first["created"], 1)
		self.assertEqual(first["failed"], 0)
		self.assertGreaterEqual(first["already_existing"], 7)

		with fx.as_user(self.system_manager_user):
			second = cartera_api.sync_missing_obligations()
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["already_existing"], first["already_existing"] + first["created"])
		self.assertEqual(frappe.db.count("Cartera Obligacion", {"recorrido_parada": stop_name}), 1)
