# -*- coding: utf-8 -*-
"""Hotfix -- cierre automático del Recorrido al procesar la última parada.

api.recorridos.reconcile_recorrido_status() (called from RecorridoParada.
on_update()), the Recorrido controller's En Ruta -> Completado transition,
the delivered-Pick-List exclusion, and the audit/repair helpers.

Fixtures reuse the real Bodega -> Facturación -> Recorrido -> deliver_stop()
chain through test_recorridos_deliver_stop.py's helper FUNCTIONS (borrowed,
never inherited -- inheriting a TestCase would re-run its whole suite)."""

import threading

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import recorridos
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_deliver_stop as deliver_base
from fabergray_erp.tests import test_recorridos_start_route as start_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestRecorridosAutoClose(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG28H WH")
		cls.item = cls.world.item("FG28H-ITEM")
		cls.item2 = cls.world.item("FG28H-ITEM-2")
		cls.customer = cls.world.customer("FG28H Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg28h-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg28h-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg28h-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg28h-recorrido-b@example.com", ["Recorrido"])
		cls.system_manager_user = cls.world.user("fg28h-sysmanager@example.com", ["System Manager"])
		cls._seq = 0

		cls._evidence_file_names = []
		cls.addClassCleanup(cls._delete_evidence_files)

	_delete_evidence_files = classmethod(deliver_base.TestRecorridosDeliverStop._delete_evidence_files.__func__)

	# -- Borrowed helpers (functions only, see module docstring) -------------
	_facturado_pick_list = base.TestRecorridosApi._facturado_pick_list
	_set_customer_primary_address = base.TestRecorridosApi._set_customer_primary_address
	_geocode_customer_address = base.TestRecorridosApi._geocode_customer_address
	_track_route = base.TestRecorridosApi._track_route
	_create_route = base.TestRecorridosApi._create_route
	_plan_route = base.TestRecorridosApi._plan_route
	_cancel_route = base.TestRecorridosApi._cancel_route
	_driver = base.TestRecorridosApi._driver
	_force_route_status = base.TestRecorridosApi._force_route_status
	_unique = start_base.TestRecorridosStartRoute._unique
	_stop_customer = start_base.TestRecorridosStartRoute._stop_customer
	_route = start_base.TestRecorridosStartRoute._route
	_start = start_base.TestRecorridosStartRoute._start
	_en_ruta = deliver_base.TestRecorridosDeliverStop._en_ruta
	_track_delivery_files = deliver_base.TestRecorridosDeliverStop._track_delivery_files
	_deliver = deliver_base.TestRecorridosDeliverStop._deliver

	def _status(self, route_name):
		return frappe.db.get_value("Recorrido", route_name, "status")

	def _active_route_names(self):
		with fx.as_user(self.recorrido_user):
			return [
				r["name"]
				for r in recorridos.get_routes(status=["Borrador", "Planificado", "En Ruta"], page_length=100)["routes"]
			]

	def _history_route_names(self):
		with fx.as_user(self.recorrido_user):
			return [r["name"] for r in recorridos.get_routes(status=["Completado", "Cancelado"], page_length=100)["routes"]]

	def _summary(self):
		with fx.as_user(self.recorrido_user):
			return recorridos.get_routes_summary()

	# =====================================================================
	# 12. Happy path -- la última entrega cierra el recorrido
	# =====================================================================

	def test_last_delivery_closes_route_as_completado(self):
		route = self._en_ruta(n_stops=3)
		first, second, third = route["stops"]
		self._deliver(route["name"], first["name"])
		self._deliver(route["name"], second["name"])
		self.assertEqual(self._status(route["name"]), "En Ruta")
		self.assertIn(route["name"], self._active_route_names())
		en_ruta_before = self._summary()["en_ruta"]

		result = self._deliver(route["name"], third["name"])

		statuses = [s["status"] for s in result["stops"]]
		self.assertEqual(statuses.count("Pendiente"), 0)
		self.assertEqual(statuses.count("Entregado"), 3)
		self.assertEqual(result["status"], "Completado")
		self.assertIsNotNone(result["completed_on"])
		self.assertEqual(self._status(route["name"]), "Completado")
		self.assertIsNotNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))
		# Out of Recorridos activos / KPI EN RUTA, into Historial.
		self.assertNotIn(route["name"], self._active_route_names())
		self.assertEqual(self._summary()["en_ruta"], en_ruta_before - 1)
		self.assertIn(route["name"], self._history_route_names())
		with fx.as_user(self.recorrido_user):
			self.assertEqual(recorridos.get_route_detail(route["name"])["status"], "Completado")

	def test_completed_on_is_after_started_on(self):
		route = self._en_ruta()
		self._deliver(route["name"], route["stops"][0]["name"])
		started_on, completed_on = frappe.db.get_value("Recorrido", route["name"], ["started_on", "completed_on"])
		self.assertGreaterEqual(completed_on, started_on)

	# =====================================================================
	# 13. Todavía queda una parada
	# =====================================================================

	def test_route_stays_en_ruta_while_a_stop_is_pending(self):
		route = self._en_ruta(n_stops=3)
		self._deliver(route["name"], route["stops"][0]["name"])
		result = self._deliver(route["name"], route["stops"][1]["name"])
		self.assertEqual(result["status"], "En Ruta")
		self.assertEqual(self._status(route["name"]), "En Ruta")
		self.assertIsNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))
		self.assertIn(route["name"], self._active_route_names())

		outcome = recorridos.reconcile_recorrido_status(route["name"])
		self.assertFalse(outcome["changed"])
		self.assertEqual((outcome["pending"], outcome["delivered"]), (1, 2))

	# =====================================================================
	# 14. Todas procesadas, alguna con novedad
	# =====================================================================

	def test_not_delivered_stop_never_marks_route_completado(self):
		"""No Entregado has no code path yet (RecorridoParada.validate()
		rejects it), so it is seeded directly. The model has no final status
		for "processed with failed deliveries": the route stays En Ruta and
		is flagged needs_review -- never Completado."""
		route = self._en_ruta(n_stops=2)
		self._deliver(route["name"], route["stops"][0]["name"])
		frappe.db.set_value("Recorrido Parada", route["stops"][1]["name"], "status", "No Entregado")

		outcome = recorridos.reconcile_recorrido_status(route["name"])
		self.assertFalse(outcome["changed"])
		self.assertTrue(outcome["needs_review"])
		self.assertEqual((outcome["pending"], outcome["delivered"], outcome["other_terminal"]), (0, 1, 1))
		self.assertEqual(self._status(route["name"]), "En Ruta")
		self.assertIsNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))

	def test_delivery_with_faltantes_still_closes_route(self):
		"""Faltantes / cambios is reported ON an Entregado stop (the order
		reached the customer); it stays visible on the stop and in Cartera,
		and does not keep the route open."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, has_delivery_issues="1", delivery_issues="Faltó 1 galón")
		self.assertEqual(self._status(route["name"]), "Completado")
		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop_name, "has_delivery_issues"), 1)

	# =====================================================================
	# 15. Idempotencia
	# =====================================================================

	def test_reconcile_is_idempotent(self):
		route = self._en_ruta(n_stops=2)
		for stop in route["stops"]:
			self._deliver(route["name"], stop["name"])
		completed_on, modified = frappe.db.get_value("Recorrido", route["name"], ["completed_on", "modified"])

		for _ in range(2):
			outcome = recorridos.reconcile_recorrido_status(route["name"])
			self.assertFalse(outcome["changed"])
			self.assertEqual(outcome["status"], "Completado")
		self.assertEqual(
			frappe.db.get_value("Recorrido", route["name"], ["completed_on", "modified"]), (completed_on, modified)
		)

	def test_double_tap_on_last_stop_is_idempotent_after_close(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		self.assertEqual(self._status(route["name"]), "Completado")

		again = self._deliver(route["name"], stop_name, user=self.recorrido_user_b)
		self.assertTrue(again["already_completed"])
		self.assertEqual(again["status"], "Completado")

	def test_pending_stop_of_completado_route_still_rejected(self):
		"""The idempotent branch is only for an already-delivered stop."""
		route = self._en_ruta()
		self._force_route_status(route["name"], "Completado")
		with self.assertRaises(recorridos.RouteNotEditableError):
			self._deliver(route["name"], route["stops"][0]["name"])

	# =====================================================================
	# Reparación controlada de recorridos ya inconsistentes
	# =====================================================================

	def test_audit_and_repair_inconsistent_route(self):
		"""The pre-hotfix state: every stop Entregado, route still En Ruta
		(seeded by re-opening a closed route directly, the way the bug left
		REC-2026-00007)."""
		route = self._en_ruta(n_stops=2)
		for stop in route["stops"]:
			self._deliver(route["name"], stop["name"])
		frappe.db.set_value("Recorrido", route["name"], {"status": "En Ruta", "completed_on": None})

		rows = {r["recorrido"]: r for r in recorridos.audit_route_statuses()}
		row = rows[route["name"]]
		self.assertTrue(row["inconsistent"])
		self.assertEqual(row["persisted_status"], "En Ruta")
		self.assertEqual(row["expected_status"], "Completado")
		self.assertEqual((row["total_stops"], row["pending"], row["delivered"]), (2, 0, 2))
		# audit never writes.
		self.assertEqual(self._status(route["name"]), "En Ruta")

		stops_before = frappe.get_all(
			"Recorrido Parada", filters={"recorrido": route["name"]}, fields=["name", "status", "modified"], order_by="name"
		)
		outcome = recorridos.reconcile_recorrido_status(route["name"])
		self.assertTrue(outcome["changed"])
		self.assertEqual((outcome["previous_status"], outcome["status"]), ("En Ruta", "Completado"))
		self.assertEqual(self._status(route["name"]), "Completado")
		self.assertIsNotNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))
		# Only the Recorrido changed.
		self.assertEqual(
			frappe.get_all(
				"Recorrido Parada",
				filters={"recorrido": route["name"]},
				fields=["name", "status", "modified"],
				order_by="name",
			),
			stops_before,
		)
		self.assertNotIn(route["name"], {r["recorrido"] for r in recorridos.audit_route_statuses()})

	def test_reconcile_leaves_other_statuses_alone(self):
		planned = self._route()
		self.assertFalse(recorridos.reconcile_recorrido_status(planned["name"])["changed"])
		self.assertEqual(self._status(planned["name"]), "Planificado")

	# =====================================================================
	# Controlador -- ningún atajo genérico
	# =====================================================================

	def test_controller_rejects_completado_with_pending_stops(self):
		route = self._en_ruta(n_stops=2)
		self._deliver(route["name"], route["stops"][0]["name"])
		with fx.as_user(self.system_manager_user):
			doc = frappe.get_doc("Recorrido", route["name"])
			doc.status = "Completado"
			with self.assertRaisesRegex(recorridos.RouteValidationError, "todas sus paradas"):
				doc.save()
		self.assertEqual(self._status(route["name"]), "En Ruta")

	def test_controller_completed_on_is_immutable_and_only_for_completado(self):
		route = self._en_ruta()
		with fx.as_user(self.system_manager_user):
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value("Recorrido", route["name"], "completed_on", frappe.utils.now_datetime())
		self._deliver(route["name"], route["stops"][0]["name"])
		original = frappe.db.get_value("Recorrido", route["name"], "completed_on")
		for new_value in (frappe.utils.add_to_date(original, hours=-2), None):
			with fx.as_user(self.system_manager_user):
				doc = frappe.get_doc("Recorrido", route["name"])
				doc.completed_on = new_value
				with self.assertRaises(frappe.ValidationError, msg=str(new_value)):
					doc.save()
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "completed_on"), original)

	def test_completado_has_no_exit(self):
		route = self._en_ruta()
		self._deliver(route["name"], route["stops"][0]["name"])
		for target in ("En Ruta", "Cancelado", "Planificado"):
			with fx.as_user(self.system_manager_user):
				doc = frappe.get_doc("Recorrido", route["name"])
				doc.status = target
				with self.assertRaises(frappe.ValidationError, msg=target):
					doc.save()

	# =====================================================================
	# Pick Lists entregados no vuelven a estar disponibles
	# =====================================================================

	def test_delivered_pick_list_not_available_after_close(self):
		route = self._en_ruta()
		pick_list = route["stops"][0]["pick_list"]
		self._deliver(route["name"], route["stops"][0]["name"])
		self.assertEqual(self._status(route["name"]), "Completado")

		with fx.as_user(self.recorrido_user):
			available = recorridos.get_available_orders(page_length=100)
			self.assertNotIn(pick_list, [r["pick_list"] for r in available["pick_lists"]])
			with self.assertRaises(recorridos.PickListAlreadyAssignedError):
				recorridos.get_available_order_detail(pick_list)
			with self.assertRaises(recorridos.PickListAlreadyAssignedError):
				self._create_route(pick_lists=[pick_list])

	# =====================================================================
	# Concurrencia -- las dos últimas paradas entregadas a la vez
	# =====================================================================

	def test_two_last_deliveries_under_real_concurrency_close_route(self):
		"""Two real threads, each on its own DB connection, deliver the two
		remaining stops of the same route at once. Both succeed; whichever
		commits second must see the first one's stop (locking reads) and close
		the route -- never leave it En Ruta with 0 pending."""
		route = self._en_ruta(n_stops=2)
		first, second = route["stops"]
		frappe.db.commit()

		site = frappe.local.site
		results = {}
		payloads = {key: (deliver_base._photo_jpeg(), deliver_base._signature_png()) for key in ("a", "b")}

		def attempt(key, user_email, stop_name):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user_email)
			photo, signature = payloads[key]
			frappe.local.request = deliver_base._multipart_request(photo=photo, signature=signature)
			try:
				detail = recorridos.deliver_stop(route["name"], stop_name, payment_status="Crédito")
				frappe.db.commit()
				results[key] = ("ok", detail["status"])
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		t1 = threading.Thread(target=attempt, args=("a", self.recorrido_user, first["name"]))
		t2 = threading.Thread(target=attempt, args=("b", self.recorrido_user_b, second["name"]))
		t1.start()
		t2.start()
		t1.join(timeout=60)
		t2.join(timeout=60)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		for stop in (first, second):
			self._track_delivery_files(stop["name"])

		outcomes = [results.get("a"), results.get("b")]
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual(sorted(o[1] for o in outcomes), ["Completado", "En Ruta"], outcomes)
		self.assertEqual(self._status(route["name"]), "Completado")
		self.assertIsNotNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))
