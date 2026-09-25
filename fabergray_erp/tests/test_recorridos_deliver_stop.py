# -*- coding: utf-8 -*-
"""Fase 26.3 -- api.recorridos.deliver_stop(): delivery of one stop with a
mandatory photo + customer signature stored as PRIVATE Files attached to the
stop, the Recorrido Parada lifecycle guard, idempotency/concurrency,
rollback without orphans, and the Ventas DELIVERED integration.

Fixtures reuse the real Bodega -> Facturación -> Recorrido chain through
test_recorridos_api.py / test_recorridos_start_route.py helper FUNCTIONS
(borrowed, never inherited -- inheriting a TestCase would re-run its whole
suite inside this module)."""

import io
import os
import random
import threading
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, get_datetime
from PIL import Image, ImageDraw
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from fabergray_erp import geocoding
from fabergray_erp.api import recorridos, ventas
from fabergray_erp.fabrigray_erp.doctype.recorrido_parada import recorrido_parada as parada_controller
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests import test_recorridos_api as base
from fabergray_erp.tests import test_recorridos_start_route as start_base

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


# -- Image fixtures ------------------------------------------------------------
def _photo_jpeg(size=(800, 600), exif=None):
	"""A unique JPEG (random colour + rectangle), so Frappe's content-hash
	dedup never makes two tests share one file on disk."""
	image = Image.new("RGB", size, tuple(random.randint(0, 255) for _ in range(3)))
	draw = ImageDraw.Draw(image)
	x, y = random.randint(0, size[0] // 2), random.randint(0, size[1] // 2)
	draw.rectangle([x, y, x + size[0] // 3, y + size[1] // 3], fill=tuple(random.randint(0, 255) for _ in range(3)))
	output = io.BytesIO()
	kwargs = {"exif": exif.tobytes()} if exif is not None else {}
	image.save(output, format="JPEG", **kwargs)
	return output.getvalue()


def _signature_png(ink=True, background=(255, 255, 255, 0), size=(600, 200)):
	image = Image.new("RGBA", size, background)
	if ink:
		draw = ImageDraw.Draw(image)
		points = [(20 + i * 70, random.randint(40, 160)) for i in range(8)]
		draw.line(points, fill=(16, 24, 40, 255), width=5)
	output = io.BytesIO()
	image.save(output, format="PNG")
	return output.getvalue()


def _multipart_request(method="POST", **files):
	"""A real werkzeug Request carrying multipart files -- exactly what
	deliver_stop() reads from frappe.request.files (and what File.insert()
	expects of frappe.request, e.g. .host)."""
	data = {key: (io.BytesIO(content), f"{key}.bin") for key, content in files.items() if content is not None}
	builder = EnvironBuilder(method=method, data=data if method != "GET" else None, base_url="http://fabergray.local")
	return Request(builder.get_environ())


@contextmanager
def _uploads(method="POST", **files):
	"""Installs a _multipart_request() as frappe.local.request, restoring the
	previous one afterwards."""
	previous = getattr(frappe.local, "request", None)
	frappe.local.request = _multipart_request(method=method, **files)
	try:
		yield
	finally:
		if previous is None:
			del frappe.local.request
		else:
			frappe.local.request = previous


class TestRecorridosDeliverStop(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG263 WH")
		cls.item = cls.world.item("FG263-ITEM")
		cls.item2 = cls.world.item("FG263-ITEM-2")
		cls.customer = cls.world.customer("FG263 Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg263-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg263-facturacion@example.com", ["Facturación"])
		cls.recorrido_user = cls.world.user("fg263-recorrido@example.com", ["Recorrido"])
		cls.recorrido_user_b = cls.world.user("fg263-recorrido-b@example.com", ["Recorrido"])
		cls.no_role_user = cls.world.user("fg263-norole@example.com", [])
		cls.gestion_clientes_user = cls.world.user("fg263-gestion@example.com", ["Gestión de Clientes"])
		cls.system_manager_user = cls.world.user("fg263-sysmanager@example.com", ["System Manager"])
		cls._seq = 0

		# Evidence Files are NOT tracked in world: world.cleanup() deletes with
		# ignore_on_trash=True, which would skip File.on_trash and leave the
		# physical file behind. Registered after world.cleanup so it runs
		# first (class cleanups are LIFO).
		cls._evidence_file_names = []
		cls.addClassCleanup(cls._delete_evidence_files)

	@classmethod
	def _delete_evidence_files(cls):
		frappe.set_user("Administrator")
		for name in cls._evidence_file_names:
			if frappe.db.exists("File", name):
				# Normal delete: File.on_trash removes the file from disk too.
				frappe.delete_doc("File", name, ignore_permissions=True)
		frappe.db.commit()

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

	def _en_ruta(self, n_stops=1):
		route = self._route(n_stops=n_stops)
		return self._start(route["name"])

	def _track_delivery_files(self, stop_name):
		for name in frappe.get_all(
			"File", filters={"attached_to_doctype": "Recorrido Parada", "attached_to_name": stop_name}, pluck="name"
		):
			if name not in self._evidence_file_names:
				self._evidence_file_names.append(name)
		# Fase 27.1 -- a delivery also creates its Cartera Obligacion (and,
		# for "Pagado + comprobante", a submitted Cartera Pago): tracked so
		# world.cleanup() removes them (payments first -- reverse order).
		for obligation in frappe.get_all("Cartera Obligacion", filters={"recorrido_parada": stop_name}, pluck="name"):
			self.world.track_existing("Cartera Obligacion", obligation)
			for payment in frappe.get_all("Cartera Pago", filters={"cartera_obligacion": obligation}, pluck="name"):
				self.world.track_existing("Cartera Pago", payment)

	def _deliver(
		self, route_name, stop_name, photo="default", signature="default", notes=None, user=None, payment_proof=None, **report
	):
		"""`report`: has_delivery_issues/delivery_issues/payment_status/
		payment_note, exactly as the page's FormData sends them. payment_status
		defaults to "Pagado" (pass payment_status=None to omit it)."""
		photo = _photo_jpeg() if photo == "default" else photo
		signature = _signature_png() if signature == "default" else signature
		report.setdefault("payment_status", "Pagado")
		try:
			with fx.as_user(user or self.recorrido_user):
				with _uploads(photo=photo, signature=signature, payment_proof=payment_proof):
					return recorridos.deliver_stop(route_name, stop_name, notes=notes, **report)
		finally:
			self._track_delivery_files(stop_name)

	def _stop(self, stop_name):
		return frappe.get_doc("Recorrido Parada", stop_name)

	def _evidence_files(self, stop_name):
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Recorrido Parada", "attached_to_name": stop_name},
			fields=["name", "file_url", "is_private", "attached_to_field", "owner"],
		)

	# =====================================================================
	# Entrega correcta
	# =====================================================================

	def test_deliver_stop_marks_entregado(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		result = self._deliver(route["name"], stop_name, notes="  Recibió <b>portería</b>  ")

		self.assertFalse(result["already_completed"])
		self.assertEqual(result["delivered_stop"], stop_name)
		stop = self._stop(stop_name)
		self.assertEqual(stop.status, "Entregado")
		self.assertIsNotNone(stop.delivered_on)
		self.assertEqual(stop.delivered_by, self.recorrido_user)
		self.assertIsNone(stop.arrived_on)
		self.assertEqual(stop.delivery_note, "Recibió portería")
		# The route itself is NOT finished by a delivery.
		self.assertEqual(frappe.db.get_value("Recorrido", route["name"], "status"), "En Ruta")
		self.assertIsNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))

	def test_evidence_files_are_private_and_attached_to_the_stop(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)

		stop = self._stop(stop_name)
		files = {f.attached_to_field: f for f in self._evidence_files(stop_name)}
		# A normal delivery (Pagado without proof) creates exactly 2 Files.
		self.assertEqual(len(self._evidence_files(stop_name)), 2)
		self.assertEqual(set(files), {"delivery_photo", "customer_signature"})
		self.assertIsNone(stop.payment_proof)
		for field, file_row in files.items():
			self.assertEqual(file_row.is_private, 1)
			self.assertTrue(file_row.file_url.startswith("/private/files/"), file_row.file_url)
			self.assertEqual(stop.get(field), file_row.file_url)
			self.assertEqual(file_row.owner, self.recorrido_user)
		for value in (stop.delivery_photo, stop.customer_signature):
			self.assertNotIn("data:", value)
			self.assertNotIn("base64", value)

		photo = frappe.get_doc("File", files["delivery_photo"].name)
		signature = frappe.get_doc("File", files["customer_signature"].name)
		self.assertEqual(Image.open(io.BytesIO(photo.get_content())).format, "JPEG")
		self.assertEqual(Image.open(io.BytesIO(signature.get_content())).format, "PNG")

	def test_get_route_detail_exposes_delivery_timestamps_not_file_urls(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		with fx.as_user(self.recorrido_user):
			detail = recorridos.get_route_detail(route["name"])
		stop = detail["stops"][0]
		self.assertIsNotNone(stop["delivered_on"])
		self.assertEqual(stop["delivered_by"], self.recorrido_user)
		self.assertEqual(stop["payment_status"], "Pagado")
		self.assertEqual(stop["has_delivery_issues"], 0)
		# Minimum exposure: no private URLs, no free-text notes.
		for hidden in ("delivery_photo", "customer_signature", "payment_proof", "payment_note", "delivery_issues", "delivery_note"):
			self.assertNotIn(hidden, stop)

	# =====================================================================
	# Evidencia obligatoria / validación de imágenes
	# =====================================================================

	def test_photo_required(self):
		route = self._en_ruta()
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "foto"):
			self._deliver(route["name"], route["stops"][0]["name"], photo=None)
		self.assertEqual(self._stop(route["stops"][0]["name"]).status, "Pendiente")

	def test_signature_required(self):
		route = self._en_ruta()
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "firma"):
			self._deliver(route["name"], route["stops"][0]["name"], signature=None)
		self.assertEqual(self._stop(route["stops"][0]["name"]).status, "Pendiente")

	def test_blank_signature_rejected(self):
		route = self._en_ruta()
		for blank in (_signature_png(ink=False), _signature_png(ink=False, background=(255, 255, 255, 255))):
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "vacía"):
				self._deliver(route["name"], route["stops"][0]["name"], signature=blank)
		self.assertEqual(self._evidence_files(route["stops"][0]["name"]), [])

	def test_signature_must_be_png(self):
		route = self._en_ruta()
		with self.assertRaises(recorridos.DeliveryEvidenceError):
			self._deliver(route["name"], route["stops"][0]["name"], signature=_photo_jpeg())

	def test_fake_mime_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		fake = b"<?php echo 'not an image'; ?>"
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "no es una imagen"):
			self._deliver(route["name"], stop_name, photo=fake)
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "no es una imagen"):
			self._deliver(route["name"], stop_name, signature=fake)
		gif = io.BytesIO()
		Image.new("RGB", (50, 50), (1, 2, 3)).save(gif, format="GIF")
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "formato"):
			self._deliver(route["name"], stop_name, photo=gif.getvalue())
		self.assertEqual(self._evidence_files(stop_name), [])

	def test_oversized_uploads_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "tamaño"):
			self._deliver(route["name"], stop_name, photo=b"\xff" * (recorridos.DELIVERY_PHOTO_MAX_BYTES + 1))
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "tamaño"):
			self._deliver(
				route["name"], stop_name, signature=b"\x89" * (recorridos.DELIVERY_SIGNATURE_MAX_BYTES + 1)
			)
		self.assertEqual(self._stop(stop_name).status, "Pendiente")

	def test_photo_reencoded_oriented_resized_without_exif(self):
		exif = Image.Exif()
		exif[0x0112] = 6  # Orientation: rotate 90 CW
		exif[0x010F] = "FG263 Camera Maker"
		source = _photo_jpeg(size=(3000, 1000), exif=exif)
		self.assertEqual(Image.open(io.BytesIO(source)).getexif().get(0x0112), 6)

		result = Image.open(io.BytesIO(recorridos._normalize_delivery_photo(source)))
		self.assertEqual(result.format, "JPEG")
		# Orientation applied (portrait now) and long side capped at 1600.
		self.assertEqual(result.size, (533, 1600))
		self.assertEqual(dict(result.getexif()), {})

	def test_signature_reencoded_as_opaque_png(self):
		result = Image.open(io.BytesIO(recorridos._normalize_customer_signature(_signature_png())))
		self.assertEqual(result.format, "PNG")
		self.assertEqual(result.mode, "RGB")

	def test_notes_html_stripped_and_length_limited(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "1000"):
			self._deliver(route["name"], stop_name, notes="x" * 1001)
		self._deliver(route["name"], stop_name, notes="<script>alert(1)</script>Ok")
		self.assertNotIn("<", self._stop(stop_name).delivery_note or "")

	# =====================================================================
	# Estado / pertenencia / permisos / Company
	# =====================================================================

	def test_route_must_be_en_ruta(self):
		planned = self._route()
		with self.assertRaises(recorridos.RouteNotEditableError):
			self._deliver(planned["name"], planned["stops"][0]["name"])

		cancelled = self._route()
		with fx.as_user(self.recorrido_user):
			self._cancel_route(cancelled["name"])
		with self.assertRaises(recorridos.RouteNotEditableError):
			self._deliver(cancelled["name"], cancelled["stops"][0]["name"])

		completed = self._en_ruta()
		self._force_route_status(completed["name"], "Completado")
		with self.assertRaises(recorridos.RouteNotEditableError):
			self._deliver(completed["name"], completed["stops"][0]["name"])

		for route in (planned, cancelled, completed):
			self.assertEqual(self._stop(route["stops"][0]["name"]).status, "Pendiente")
			self.assertEqual(self._evidence_files(route["stops"][0]["name"]), [])

	def test_stop_must_be_pendiente(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		frappe.db.set_value("Recorrido Parada", stop_name, "status", "No Entregado")
		with self.assertRaises(recorridos.StopNotDeliverableError):
			self._deliver(route["name"], stop_name)
		self.assertEqual(self._evidence_files(stop_name), [])

	def test_stop_must_belong_to_route(self):
		route_a = self._en_ruta()
		route_b = self._en_ruta()
		with self.assertRaisesRegex(recorridos.StopNotDeliverableError, "no pertenece"):
			self._deliver(route_a["name"], route_b["stops"][0]["name"])
		with self.assertRaisesRegex(recorridos.StopNotDeliverableError, "no pertenece"):
			self._deliver(route_a["name"], "no-existe-parada")
		self.assertEqual(self._stop(route_b["stops"][0]["name"]).status, "Pendiente")

	def test_company_isolation(self):
		other = frappe.get_doc({"doctype": "Recorrido", "company": "_Test Company"})
		other.insert(ignore_permissions=True)
		self.world.track_existing("Recorrido", other.name)
		self._force_route_status(other.name, "En Ruta")
		with self.assertRaises(frappe.PermissionError):
			self._deliver(other.name, "cualquier-parada")

	def test_requires_permission(self):
		route = self._en_ruta()
		with self.assertRaises(frappe.PermissionError):
			self._deliver(route["name"], route["stops"][0]["name"], user=self.no_role_user)
		with self.assertRaises(frappe.PermissionError):
			self._deliver(route["name"], route["stops"][0]["name"], user=self.gestion_clientes_user)
		self.assertEqual(self._stop(route["stops"][0]["name"]).status, "Pendiente")

	def test_pick_list_must_still_be_eligible(self):
		route = self._en_ruta()
		stop = route["stops"][0]
		frappe.db.set_value("Pick List", stop["pick_list"], "fg_invoicing_status", "Pendiente")
		try:
			with self.assertRaises(recorridos.PickListNotEligibleError):
				self._deliver(route["name"], stop["name"])
		finally:
			frappe.db.set_value("Pick List", stop["pick_list"], "fg_invoicing_status", "Facturado")
		self.assertEqual(self._evidence_files(stop["name"]), [])

	def test_get_is_not_allowed_through_rpc(self):
		from frappe.handler import execute_cmd

		route = self._en_ruta()
		previous_form_dict = frappe.local.form_dict
		frappe.local.form_dict = frappe._dict(route_name=route["name"], stop_name=route["stops"][0]["name"])
		try:
			with fx.as_user(self.recorrido_user):
				with _uploads(method="GET", photo=_photo_jpeg(), signature=_signature_png()):
					with self.assertRaises(frappe.PermissionError):
						execute_cmd("fabergray_erp.api.recorridos.deliver_stop")
		finally:
			frappe.local.form_dict = previous_form_dict
		self.assertEqual(self._stop(route["stops"][0]["name"]).status, "Pendiente")

	def test_private_evidence_visibility(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		file_doc = frappe.get_doc("File", self._evidence_files(stop_name)[0].name)
		# Current permissions (approved): Recorrido + System Manager, nobody else.
		self.assertTrue(frappe.has_permission("File", "read", doc=file_doc, user=self.recorrido_user))
		self.assertTrue(frappe.has_permission("File", "read", doc=file_doc, user=self.recorrido_user_b))
		self.assertTrue(frappe.has_permission("File", "read", doc=file_doc, user=self.system_manager_user))
		self.assertFalse(frappe.has_permission("File", "read", doc=file_doc, user=self.no_role_user))
		self.assertFalse(frappe.has_permission("File", "read", doc=file_doc, user=self.gestion_clientes_user))

	# =====================================================================
	# Idempotencia / concurrencia / rollback
	# =====================================================================

	def test_second_call_is_idempotent(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		first = self._stop(stop_name)
		files_before = self._evidence_files(stop_name)

		second = self._deliver(route["name"], stop_name, user=self.recorrido_user_b, notes="otra")
		self.assertTrue(second["already_completed"])
		stop_in_detail = next(s for s in second["stops"] if s["name"] == stop_name)
		self.assertEqual(stop_in_detail["status"], "Entregado")

		after = self._stop(stop_name)
		self.assertEqual(after.delivered_on, first.delivered_on)
		self.assertEqual(after.delivered_by, self.recorrido_user)
		self.assertEqual(after.delivery_photo, first.delivery_photo)
		self.assertEqual(after.customer_signature, first.customer_signature)
		self.assertEqual(after.delivery_note, first.delivery_note)
		self.assertEqual(sorted(f.name for f in self._evidence_files(stop_name)), sorted(f.name for f in files_before))

	def test_rollback_leaves_no_file_rows_or_orphaned_files(self):
		"""A failure AFTER both Files were inserted: after the rollback there is
		no Entregado stop, no File row and no file left on disk (Frappe's own
		File.on_rollback). Setup is committed first so the rollback only undoes
		the failed delivery."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		frappe.db.commit()

		created = []
		original_save = recorridos._save_delivery_evidence_file

		def recording_save(*args, **kwargs):
			file_doc = original_save(*args, **kwargs)
			created.append((file_doc.name, file_doc.get_full_path()))
			return file_doc

		with patch.object(recorridos, "_save_delivery_evidence_file", side_effect=recording_save):
			with patch.object(
				parada_controller.RecorridoParada, "validate", side_effect=RuntimeError("fallo simulado")
			):
				with self.assertRaises(RuntimeError):
					self._deliver(route["name"], stop_name, payment_proof=_photo_jpeg())

		# photo + signature + payment proof were all written before the failure.
		self.assertEqual(len(created), 3)
		for _name, path in created:
			self.assertTrue(os.path.exists(path), path)

		frappe.db.rollback()

		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop_name, "status"), "Pendiente")
		for name, path in created:
			self.assertFalse(frappe.db.exists("File", name))
			self.assertFalse(os.path.exists(path), f"orphaned file left on disk: {path}")

	def test_double_delivery_under_real_concurrency(self):
		"""Two real threads, each on its own DB connection and its own request
		with its own photo/signature, confirm the SAME stop at once: exactly one
		delivers (already_completed=False), the other waits on the locks and
		returns already_completed=True -- and only ONE pair of Files exists."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		frappe.db.commit()

		site = frappe.local.site
		results = {}
		payloads = {key: (_photo_jpeg(), _signature_png()) for key in ("a", "b")}

		def attempt(key, user_email):
			frappe.init(site=site)
			frappe.connect()
			frappe.db.begin()
			frappe.set_user(user_email)
			photo, signature = payloads[key]
			frappe.local.request = _multipart_request(photo=photo, signature=signature)
			try:
				detail = recorridos.deliver_stop(route["name"], stop_name, payment_status="Crédito")
				frappe.db.commit()
				results[key] = ("ok", detail["already_completed"])
			except Exception as e:  # pragma: no cover -- diagnostic only
				frappe.db.rollback()
				results[key] = ("exception", repr(e))
			finally:
				frappe.destroy()

		t1 = threading.Thread(target=attempt, args=("a", self.recorrido_user))
		t2 = threading.Thread(target=attempt, args=("b", self.recorrido_user_b))
		t1.start()
		t2.start()
		t1.join(timeout=60)
		t2.join(timeout=60)

		frappe.init(site=site)
		frappe.connect()
		frappe.set_user("Administrator")
		self._track_delivery_files(stop_name)

		outcomes = [results.get("a"), results.get("b")]
		self.assertTrue(all(o and o[0] == "ok" for o in outcomes), outcomes)
		self.assertEqual(sorted(o[1] for o in outcomes), [False, True], outcomes)
		self.assertEqual(frappe.db.get_value("Recorrido Parada", stop_name, "status"), "Entregado")
		self.assertEqual(len(self._evidence_files(stop_name)), 2)

	# =====================================================================
	# Controlador Recorrido Parada -- ningún atajo genérico
	# =====================================================================

	def test_generic_save_cannot_mark_entregado_without_evidence(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value("Recorrido Parada", stop_name, "status", "Entregado")
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value(
					"Recorrido Parada",
					stop_name,
					{"status": "Entregado", "delivered_on": frappe.utils.now_datetime(), "delivered_by": self.recorrido_user},
				)
		self.assertEqual(self._stop(stop_name).status, "Pendiente")

	def test_generic_save_cannot_deliver_outside_en_ruta_even_with_fields(self):
		route = self._route()
		stop_name = route["stops"][0]["name"]
		with fx.as_user(self.system_manager_user):
			doc = frappe.get_doc("Recorrido Parada", stop_name)
			doc.update(
				{
					"status": "Entregado",
					"delivered_on": frappe.utils.now_datetime(),
					"delivered_by": self.system_manager_user,
					"delivery_photo": "/private/files/x.jpg",
					"customer_signature": "/private/files/y.png",
				}
			)
			with self.assertRaisesRegex(frappe.ValidationError, "En Ruta"):
				doc.save()

	def test_pendiente_cannot_hold_delivery_fields(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		for field, value in (
			("delivery_note", "nota"),
			("delivered_by", self.recorrido_user),
			("delivery_photo", "/private/files/x.jpg"),
			("payment_status", "Pagado"),
			("payment_note", "nota"),
			("payment_proof", "/private/files/x.jpg"),
			("has_delivery_issues", 1),
			("delivery_issues", "faltó algo"),
		):
			with fx.as_user(self.recorrido_user):
				with self.assertRaises(frappe.ValidationError, msg=field):
					frappe.client.set_value("Recorrido Parada", stop_name, field, value)

	def test_no_entregado_transition_not_available_yet(self):
		route = self._en_ruta()
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.ValidationError):
				frappe.client.set_value("Recorrido Parada", route["stops"][0]["name"], "status", "No Entregado")

	def test_new_stop_must_be_pendiente_without_evidence(self):
		route = self._en_ruta()
		for extra in ({"status": "Entregado"}, {"delivery_note": "x"}):
			doc = frappe.get_doc(
				{
					"doctype": "Recorrido Parada",
					"recorrido": route["name"],
					"sequence": 99,
					"pick_list": route["stops"][0]["pick_list"],
					**extra,
				}
			)
			with self.assertRaises(frappe.ValidationError, msg=str(extra)):
				doc.insert(ignore_permissions=True)

	def test_delivered_stop_is_frozen(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		original = self._stop(stop_name)
		changes = (
			("status", "Pendiente"),
			("status", "No Entregado"),
			("delivered_on", "2020-01-01 10:00:00"),
			("delivered_by", self.recorrido_user_b),
			("delivery_photo", "/private/files/otra.jpg"),
			("customer_signature", "/private/files/otra.png"),
			("delivery_note", "cambiada"),
			("sequence", 42),
		)
		for field, value in changes:
			with fx.as_user(self.system_manager_user):
				with self.assertRaises(frappe.ValidationError, msg=field):
					frappe.client.set_value("Recorrido Parada", stop_name, field, value)
		after = self._stop(stop_name)
		for field in ("status", "delivered_on", "delivered_by", "delivery_photo", "customer_signature", "sequence"):
			self.assertEqual(after.get(field), original.get(field), field)

	def test_delivered_stop_noop_save_is_allowed(self):
		"""Frozen means "no change", not "cannot be saved": a Desk save that sends
		the same values back (Datetime as a string) must not be rejected."""
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		with fx.as_user(self.system_manager_user):
			doc = frappe.get_doc("Recorrido Parada", stop_name)
			doc.delivered_on = str(doc.delivered_on)
			doc.save()

	# =====================================================================
	# Fase 26.3 (extensión) -- FALTANTES / CAMBIOS
	# =====================================================================

	def test_issues_no_without_detail_ok(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, has_delivery_issues="0")
		stop = self._stop(stop_name)
		self.assertEqual(stop.has_delivery_issues, 0)
		self.assertFalse(stop.delivery_issues)

	def test_issues_default_is_no(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name)
		self.assertEqual(self._stop(stop_name).has_delivery_issues, 0)

	def test_issues_yes_with_detail_ok_and_html_stripped(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, has_delivery_issues="true", delivery_issues="  <i>Faltó</i> 1 galón  ")
		stop = self._stop(stop_name)
		self.assertEqual(stop.has_delivery_issues, 1)
		self.assertEqual(stop.delivery_issues, "Faltó 1 galón")

	def test_issues_yes_without_detail_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		for detail in (None, "", "   ", "<b> </b>"):
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "faltantes", msg=repr(detail)):
				self._deliver(route["name"], stop_name, has_delivery_issues="1", delivery_issues=detail)
		self.assertEqual(self._stop(stop_name).status, "Pendiente")
		self.assertEqual(self._evidence_files(stop_name), [])

	def test_issues_no_with_detail_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "no hay faltantes"):
			self._deliver(route["name"], stop_name, has_delivery_issues="0", delivery_issues="Faltó algo")
		self.assertEqual(self._stop(stop_name).status, "Pendiente")

	def test_issues_flag_strictly_parsed(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		for invalid in ("si", "yes", "2", "-1", "maybe"):
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "inválido", msg=invalid):
				self._deliver(route["name"], stop_name, has_delivery_issues=invalid, delivery_issues="x")
		for value, expected in (("1", True), ("TRUE", True), (1, True), (True, True), ("0", False), ("false", False), (0, False), (None, False), ("", False)):
			self.assertEqual(recorridos._parse_strict_bool(value, "x"), expected, repr(value))

	def test_issues_max_length(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "1000"):
			self._deliver(route["name"], stop_name, has_delivery_issues="1", delivery_issues="x" * 1001)
		self._deliver(route["name"], stop_name, has_delivery_issues="1", delivery_issues="x" * 1000)
		self.assertEqual(len(self._stop(stop_name).delivery_issues), 1000)

	# =====================================================================
	# Fase 26.3 (extensión) -- ESTADO DEL PAGO / COMPROBANTE
	# =====================================================================

	def test_payment_status_required(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		for missing in (None, ""):
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "obligatorio"):
				self._deliver(route["name"], stop_name, payment_status=missing)
		self.assertEqual(self._stop(stop_name).status, "Pendiente")

	def test_payment_status_invalid_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		for invalid in ("Fiado", "pagado", "Credito", "Pendiente"):
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "inválido", msg=invalid):
				self._deliver(route["name"], stop_name, payment_status=invalid)
		self.assertEqual(self._evidence_files(stop_name), [])

	def test_paid_without_proof_ok(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(route["name"], stop_name, payment_status="Pagado")
		stop = self._stop(stop_name)
		self.assertEqual(stop.payment_status, "Pagado")
		self.assertIsNone(stop.payment_proof)
		self.assertEqual(len(self._evidence_files(stop_name)), 2)

	def test_paid_with_proof_creates_third_private_file(self):
		exif = Image.Exif()
		exif[0x010F] = "FG263 Phone"
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(
			route["name"], stop_name, payment_status="Pagado", payment_proof=_photo_jpeg(exif=exif), payment_note="Efectivo"
		)
		stop = self._stop(stop_name)
		files = {f.attached_to_field: f for f in self._evidence_files(stop_name)}
		self.assertEqual(len(self._evidence_files(stop_name)), 3)
		self.assertEqual(set(files), {"delivery_photo", "customer_signature", "payment_proof"})
		proof = files["payment_proof"]
		self.assertEqual(proof.is_private, 1)
		self.assertTrue(proof.file_url.startswith("/private/files/"))
		self.assertEqual(stop.payment_proof, proof.file_url)
		self.assertEqual(stop.payment_note, "Efectivo")
		content = Image.open(io.BytesIO(frappe.get_doc("File", proof.name).get_content()))
		self.assertEqual(content.format, "JPEG")
		self.assertEqual(dict(content.getexif()), {})

	def test_pending_payment_and_credit_ok_with_notes(self):
		for status, note in (("Pendiente por Pago", "<b>Transfiere</b> mañana"), ("Crédito", "Crédito 30 días")):
			route = self._en_ruta()
			stop_name = route["stops"][0]["name"]
			self._deliver(route["name"], stop_name, payment_status=status, payment_note=note)
			stop = self._stop(stop_name)
			self.assertEqual(stop.payment_status, status)
			self.assertNotIn("<", stop.payment_note)
			self.assertIsNone(stop.payment_proof)
			self.assertEqual(len(self._evidence_files(stop_name)), 2)

	def test_proof_rejected_unless_paid(self):
		for status in ("Pendiente por Pago", "Crédito"):
			route = self._en_ruta()
			stop_name = route["stops"][0]["name"]
			with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "solo se admite", msg=status):
				self._deliver(route["name"], stop_name, payment_status=status, payment_proof=_photo_jpeg())
			self.assertEqual(self._stop(stop_name).status, "Pendiente")
			self.assertEqual(self._evidence_files(stop_name), [])

	def test_payment_note_optional_and_limited(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "1000"):
			self._deliver(route["name"], stop_name, payment_note="x" * 1001)
		self._deliver(route["name"], stop_name, payment_note="   ")
		self.assertIsNone(self._stop(stop_name).payment_note)

	def test_payment_proof_fake_mime_and_size_rejected(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "no es una imagen"):
			self._deliver(route["name"], stop_name, payment_proof=b"%PDF-1.4 not an image")
		with self.assertRaisesRegex(recorridos.DeliveryEvidenceError, "tamaño"):
			self._deliver(route["name"], stop_name, payment_proof=b"\xff" * (recorridos.DELIVERY_PHOTO_MAX_BYTES + 1))
		self.assertEqual(self._evidence_files(stop_name), [])

	def test_report_fields_are_immutable_after_delivery(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		self._deliver(
			route["name"],
			stop_name,
			payment_proof=_photo_jpeg(),
			payment_note="Efectivo",
			has_delivery_issues="1",
			delivery_issues="Faltó 1 galón",
		)
		original = self._stop(stop_name)
		for field, value in (
			("payment_status", "Crédito"),
			("payment_proof", "/private/files/otro.jpg"),
			("payment_note", "cambiada"),
			("has_delivery_issues", 0),
			("delivery_issues", "otro detalle"),
		):
			with fx.as_user(self.system_manager_user):
				with self.assertRaises(frappe.ValidationError, msg=field):
					frappe.client.set_value("Recorrido Parada", stop_name, field, value)
		after = self._stop(stop_name)
		for field in ("payment_status", "payment_proof", "payment_note", "has_delivery_issues", "delivery_issues"):
			self.assertEqual(after.get(field), original.get(field), field)

	def test_generic_save_requires_valid_report(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		base_fields = {
			"status": "Entregado",
			"delivered_on": frappe.utils.now_datetime(),
			"delivered_by": self.system_manager_user,
			"delivery_photo": "/private/files/x.jpg",
			"customer_signature": "/private/files/y.png",
		}
		for extra in (
			{},  # no payment_status
			{"payment_status": "Crédito", "payment_proof": "/private/files/z.jpg"},
			{"payment_status": "Pagado", "has_delivery_issues": 1},
			{"payment_status": "Pagado", "delivery_issues": "detalle sin marcar"},
		):
			with fx.as_user(self.system_manager_user):
				doc = frappe.get_doc("Recorrido Parada", stop_name)
				doc.update({**base_fields, **extra})
				with self.assertRaises(frappe.ValidationError, msg=str(extra)):
					doc.save()
		self.assertEqual(self._stop(stop_name).status, "Pendiente")

	# =====================================================================
	# Integridad -- solo Recorrido Parada cambia
	# =====================================================================

	def test_delivery_keeps_pick_list_sales_order_stock_untouched(self):
		route = self._en_ruta(n_stops=2)
		stop = route["stops"][0]
		so_name = frappe.db.get_value("Recorrido Parada", stop["name"], "sales_order")
		pl_before = frappe.db.get_value("Pick List", stop["pick_list"], ["docstatus", "status", "fg_invoicing_status", "modified"])
		items_before = sorted(
			(flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty))
			for r in frappe.get_all("Pick List Item", filters={"parent": stop["pick_list"]}, fields=["qty", "picked_qty", "delivered_qty"])
		)
		so_before = frappe.db.get_value("Sales Order", so_name, ["status", "per_delivered", "per_billed", "modified"])
		counts = (
			"Stock Ledger Entry",
			"GL Entry",
			"Delivery Note",
			"Sales Invoice",
			"Stock Entry",
			"Delivery Trip",
			"Payment Entry",
			"Journal Entry",
		)
		counts_before = {dt: frappe.db.count(dt) for dt in counts}
		sequences_before = [(s["name"], s["sequence"]) for s in route["stops"]]
		customer = frappe.db.get_value("Recorrido Parada", stop["name"], "customer")
		customer_before = frappe.db.get_value("Customer", customer, "modified")
		credit_before = frappe.get_all("Customer Credit Limit", filters={"parent": customer}, fields=["credit_limit"])

		# Payment is only REPORTED: a "Pagado" delivery with proof + faltantes.
		result = self._deliver(
			route["name"],
			stop["name"],
			payment_proof=_photo_jpeg(),
			has_delivery_issues="1",
			delivery_issues="Faltó 1 galón",
			payment_note="Transferencia",
		)

		self.assertEqual(
			frappe.db.get_value("Pick List", stop["pick_list"], ["docstatus", "status", "fg_invoicing_status", "modified"]),
			pl_before,
		)
		self.assertEqual(
			sorted(
				(flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty))
				for r in frappe.get_all(
					"Pick List Item", filters={"parent": stop["pick_list"]}, fields=["qty", "picked_qty", "delivered_qty"]
				)
			),
			items_before,
		)
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, ["status", "per_delivered", "per_billed", "modified"]), so_before)
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts_before)
		self.assertEqual([(s["name"], s["sequence"]) for s in result["stops"]], sequences_before)
		self.assertEqual(frappe.db.get_value("Customer", customer, "modified"), customer_before)
		self.assertEqual(
			frappe.get_all("Customer Credit Limit", filters={"parent": customer}, fields=["credit_limit"]), credit_before
		)

	def test_delivery_never_calls_google(self):
		route = self._en_ruta()
		with patch.object(geocoding, "geocode_address") as geocode, patch.object(geocoding, "_google_geocode_address") as google:
			self._deliver(route["name"], route["stops"][0]["name"])
		geocode.assert_not_called()
		google.assert_not_called()

	# =====================================================================
	# Siguiente parada derivada / última parada / Ventas
	# =====================================================================

	def test_next_stop_is_derived_after_each_delivery(self):
		route = self._en_ruta(n_stops=2)
		first, second = route["stops"]

		def current(detail):
			pending = sorted((s for s in detail["stops"] if s["status"] == "Pendiente"), key=lambda s: s["sequence"])
			return pending[0]["name"] if pending else None

		self.assertEqual(current(route), first["name"])
		after_first = self._deliver(route["name"], first["name"])
		self.assertEqual(current(after_first), second["name"])

		after_second = self._deliver(route["name"], second["name"])
		self.assertIsNone(current(after_second))
		# Last stop processed: the route stays En Ruta, no completed_on yet.
		self.assertEqual(after_second["status"], "En Ruta")
		self.assertIsNone(frappe.db.get_value("Recorrido", route["name"], "completed_on"))

	def test_ventas_shows_delivered_with_delivery_date(self):
		route = self._en_ruta()
		stop_name = route["stops"][0]["name"]
		so_name = frappe.db.get_value("Recorrido Parada", stop_name, "sales_order")
		self.assertEqual(ventas._resolve_sales_order_logistics_status(so_name)["logistics_status"], "IN_ROUTE")

		self._deliver(route["name"], stop_name)

		logistics = ventas._resolve_sales_order_logistics_status(so_name)
		self.assertEqual(logistics["logistics_status"], "DELIVERED")
		self.assertEqual(logistics["route_name"], route["name"])
		self.assertEqual(
			get_datetime(logistics["delivered_on"]),
			get_datetime(frappe.db.get_value("Recorrido Parada", stop_name, "delivered_on")),
		)
