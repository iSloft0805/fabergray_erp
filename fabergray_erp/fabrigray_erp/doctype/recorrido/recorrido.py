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
