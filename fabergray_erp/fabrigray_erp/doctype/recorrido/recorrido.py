# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

#: Fase 26.2 -- every status change a Recorrido may make through ANY save
#: path (api.recorridos endpoints, Desk, frappe.client.set_value/save).
#: Staying in the same status is always allowed. There is deliberately no
#: En Ruta -> Planificado rollback and no cancellation from En Ruta.
#: Hotfix cierre automático -- En Ruta -> Completado, only once every stop
#: is Entregado (api.recorridos.reconcile_recorrido_status()).
ALLOWED_STATUS_TRANSITIONS = {
	"Borrador": {"Borrador", "Planificado", "Cancelado"},
	"Planificado": {"Planificado", "En Ruta", "Cancelado"},
	"En Ruta": {"En Ruta", "Completado"},
	"Cancelado": {"Cancelado"},
	"Completado": {"Completado"},
}

#: Statuses in which started_on may hold a value -- empty before leaving.
STARTED_STATUSES = ("En Ruta", "Completado")


class Recorrido(Document):
	"""Commit 24.1 -- base model; the workflow itself (eligibility checks,
	double-assignment locking, stop resolution) lives in api/recorridos.py's
	own whitelisted functions.

	Fase 26.2 -- this controller is now also the last line of defense for
	the route's lifecycle, so a generic doc.save()/frappe.client.set_value()
	(Desk included, System Manager included) cannot skip what the
	endpoints enforce:

	- a new Recorrido always starts as Borrador, with no started_on;
	- status only moves along ALLOWED_STATUS_TRANSITIONS;
	- entering En Ruta requires a driver, at least one stop and a valid
	  coordinate snapshot on every stop (the same
	  api.recorridos._assert_route_ready_to_start() start_route() uses),
	  and sets started_on if the caller didn't;
	- started_on is empty before En Ruta and immutable once set;
	- entering Completado requires every stop Entregado and sets
	  completed_on; completed_on is empty before and immutable once set;
	- company is immutable after insert.

	`_doc_before_save` is loaded by Frappe with for_update=True (check_if_
	latest()), so every comparison below is against the real, locked DB row,
	never a stale in-memory copy.

	Not covered here on purpose: frappe.db.set_value() (server-side code
	only, never reachable from a client) and Recorrido Parada's own fields
	(pending the permissions phase)."""

	def validate(self):
		if not self.created_by_user:
			self.created_by_user = frappe.session.user

		if self.is_new():
			self._validate_new()
			return

		before = self.get_doc_before_save()
		if not before:
			return

		self._validate_regularization_unchanged(before)
		self._validate_company_unchanged(before)
		self._validate_status_transition(before)
		self._validate_started_on(before)
		self._validate_completed_on(before)

	def _validate_new(self):
		if (self.status or "Borrador") != "Borrador":
			frappe.throw(_("Un recorrido nuevo siempre inicia en Borrador."), frappe.ValidationError)
		if self.started_on:
			frappe.throw(_("Un recorrido nuevo no puede tener fecha de inicio."), frappe.ValidationError)
		if self.completed_on:
			frappe.throw(_("Un recorrido nuevo no puede tener fecha de finalización."), frappe.ValidationError)
		if self._is_regularization() or self.entrega_masiva:
			# Entrega masiva -- only the service (private token) creates an
			# administrative regularization route; never Desk/REST/the API.
			from fabergray_erp import entrega_masiva_service

			if not (self._is_regularization() and entrega_masiva_service.is_authorized(self) and self.entrega_masiva):
				frappe.throw(
					_("Los recorridos de regularización administrativa solo los crea el servicio de entrega masiva."),
					frappe.PermissionError,
				)

	def _is_regularization(self):
		from fabergray_erp import entrega_masiva_service

		return self.route_kind == entrega_masiva_service.ROUTE_KIND_REGULARIZATION

	def _validate_regularization_unchanged(self, before):
		"""route_kind/entrega_masiva never change. A regularization route
		accepts, without the service token, only what reconcile_recorrido_
		status() does to every route (status -> Completado + completed_on);
		any other edit (driver, notes, dates...) is refused."""
		from fabergray_erp import entrega_masiva_service

		for fieldname in ("route_kind", "entrega_masiva"):
			if (self.get(fieldname) or None) != (before.get(fieldname) or None):
				frappe.throw(
					_("No se puede cambiar el tipo de recorrido ni su entrega masiva ({0}).").format(fieldname),
					frappe.PermissionError,
				)
		if not self._is_regularization() or entrega_masiva_service.is_authorized(self):
			return
		allowed = {"status", "completed_on", "modified", "modified_by"}
		for df in self.meta.fields:
			if df.fieldname in allowed or df.fieldtype in ("Section Break", "Column Break", "Tab Break"):
				continue
			if (self.get(df.fieldname) or None) != (before.get(df.fieldname) or None):
				frappe.throw(
					_("Un recorrido de regularización administrativa no se puede modificar ({0}).").format(df.fieldname),
					frappe.PermissionError,
				)
		if self.status != before.status and self.status != "Completado":
			frappe.throw(
				_("Un recorrido de regularización administrativa no se puede modificar (status)."), frappe.PermissionError
			)

	def _validate_company_unchanged(self, before):
		if before.company and self.company != before.company:
			frappe.throw(_("No se puede cambiar la empresa de un recorrido."), frappe.ValidationError)

	def _validate_status_transition(self, before):
		previous = before.status
		allowed = ALLOWED_STATUS_TRANSITIONS.get(previous, {previous})
		if self.status not in allowed:
			frappe.throw(
				_("Cambio de estado no permitido: {0} → {1}.").format(_(previous), _(self.status)),
				frappe.ValidationError,
			)

		if previous != "En Ruta" and self.status == "En Ruta":
			# Lazy import: api.recorridos imports this app's other API modules;
			# keeping it out of module load avoids any import cycle.
			from fabergray_erp.api.recorridos import _assert_route_ready_to_start

			if self._is_regularization():
				# Entrega masiva: no driver/coordinates exist for a delivery that
				# already happened -- allowed only under the service token
				# (checked in _validate_regularization_unchanged()), and still
				# never without at least one stop.
				if not frappe.db.exists("Recorrido Parada", {"recorrido": self.name}):
					frappe.throw(_("El recorrido no tiene paradas."), frappe.ValidationError)
			else:
				_assert_route_ready_to_start(self)
			if not self.started_on:
				self.started_on = frappe.utils.now_datetime()

		if previous != "Completado" and self.status == "Completado":
			from fabergray_erp.api.recorridos import _assert_route_ready_to_complete

			_assert_route_ready_to_complete(self)
			if not self.completed_on:
				self.completed_on = frappe.utils.now_datetime()

	def _validate_completed_on(self, before):
		if before.completed_on:
			if not self.completed_on or get_datetime(self.completed_on) != get_datetime(before.completed_on):
				frappe.throw(
					_("La fecha de finalización del recorrido no se puede modificar."), frappe.ValidationError
				)
			return

		if self.completed_on and self.status != "Completado":
			frappe.throw(
				_("La fecha de finalización solo se registra al completar el recorrido."), frappe.ValidationError
			)

	def _validate_started_on(self, before):
		if before.started_on:
			if not self.started_on or get_datetime(self.started_on) != get_datetime(before.started_on):
				frappe.throw(
					_("La fecha de inicio del recorrido no se puede modificar."), frappe.ValidationError
				)
			return

		if self.started_on and self.status not in STARTED_STATUSES:
			frappe.throw(
				_("La fecha de inicio solo se registra al iniciar el recorrido."), frappe.ValidationError
			)
