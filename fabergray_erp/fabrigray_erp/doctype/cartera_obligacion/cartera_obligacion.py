# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from fabergray_erp import cartera_service
from fabergray_erp.fabrigray_erp.doctype.recorrido_parada.recorrido_parada import _normalized


class CarteraObligacion(Document):
	"""Fase 27.1 -- one receivable per delivered Recorrido Parada.

	The controller is the last line of defense for every save path (the
	service, Desk, doc.save(), frappe.client.set_value/insert, System
	Manager included):

	- insert: only `recorrido_parada` is taken from the caller; every other
	  field is re-derived from the delivered stop
	  (cartera_service.derive_obligation_values()), so a fabricated amount,
	  customer or due date can never be inserted. The recorrido_parada
	  unique index prevents a second obligation for the same stop.
	- update: the derived fields are immutable, and paid_amount/
	  outstanding_amount/paid_on/status must equal what the submitted
	  Cartera Pago rows produce (cartera_service.expected_balances()) --
	  they can only follow the payments, never be typed in.
	- delete: never. History must not disappear (on_trash).

	27.3 -- payment_verification and its audit (payment_verified_by/_on,
	payment_rejection_reason) are immutable except for ONE transition,
	written only by cartera_service (private token): "Sin confirmar" ->
	"Confirmado" or "Rechazado", with who/when (and the reason for a
	rejection). Every other path -- Desk, frappe.client, Administrator --
	is refused. Balances keep following the payments."""

	def validate(self):
		if self.is_new():
			self.update(cartera_service.derive_obligation_values(self.recorrido_parada))
			self.update(cartera_service.expected_balances(self, 0, None))
			return

		before = self.get_doc_before_save()
		if not before:
			return

		changed = [
			fieldname
			for fieldname in (
				"recorrido_parada",
				"sales_invoice",
				*cartera_service.DERIVED_FIELDS,
				*cartera_service.VERIFICATION_FIELDS,
			)
			if _changed(self, before, fieldname)
		]
		if any(f in cartera_service.VERIFICATION_FIELDS for f in changed) and (
			cartera_service.authorized_action(self) == cartera_service.ACTION_VERIFICATION
		):
			self._validate_verification_transition(before)
			changed = [f for f in changed if f not in cartera_service.VERIFICATION_FIELDS]
		if changed:
			frappe.throw(
				_("Los datos de origen de la obligación no se pueden modificar ({0}).").format(", ".join(changed)),
				frappe.ValidationError,
			)

		paid, last = cartera_service.submitted_payments(self.name, for_update=True)
		expected = cartera_service.expected_balances(self, paid, last)
		mismatched = [f for f, value in expected.items() if _differs(self, f, value)]
		if mismatched:
			frappe.throw(
				_("El saldo y el estado de la obligación solo cambian con los pagos registrados ({0}).").format(
					", ".join(mismatched)
				),
				frappe.ValidationError,
			)

	def _validate_verification_transition(self, before):
		new = self.payment_verification
		if before.payment_verification != cartera_service.VERIFICATION_UNCONFIRMED or new not in (
			cartera_service.VERIFICATION_CONFIRMED,
			cartera_service.VERIFICATION_REJECTED,
		):
			frappe.throw(_("Transición de verificación inválida."), frappe.ValidationError)
		if self.driver_payment_status != cartera_service.DRIVER_PAID:
			frappe.throw(_("Solo un pago reportado por el conductor se verifica."), frappe.ValidationError)
		if not self.payment_verified_by or not self.payment_verified_on:
			frappe.throw(_("La verificación debe registrar quién y cuándo."), frappe.ValidationError)
		reason = (self.payment_rejection_reason or "").strip()
		if new == cartera_service.VERIFICATION_REJECTED and len(reason) < cartera_service.REASON_MIN_LENGTH:
			frappe.throw(_("El rechazo requiere un motivo."), frappe.ValidationError)
		if new == cartera_service.VERIFICATION_CONFIRMED and reason:
			frappe.throw(_("Una confirmación no lleva motivo de rechazo."), frappe.ValidationError)

	def after_insert(self):
		# Decision V2: driver reported "Pagado" with a proof -> a Conductor
		# payment for the full value (Pagado, "Sin confirmar").
		cartera_service.create_driver_reported_payment(self)

	def on_trash(self):
		frappe.throw(_("Una obligación de cartera no se puede eliminar."), frappe.ValidationError)


def _changed(doc, before, fieldname):
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else "Data"
	return _normalized(fieldtype, doc.get(fieldname)) != _normalized(fieldtype, before.get(fieldname))


def _differs(doc, fieldname, expected):
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else "Data"
	return _normalized(fieldtype, doc.get(fieldname)) != _normalized(fieldtype, expected)
