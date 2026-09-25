# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime, nowdate

from fabergray_erp import cartera_service

PAYMENT_METHODS = cartera_service.PAYMENT_METHODS

_WRITE_ACTIONS = (cartera_service.ACTION_CARTERA_PAYMENT, cartera_service.ACTION_DRIVER_PAYMENT)
_CANCEL_ACTIONS = (cartera_service.ACTION_DRIVER_REJECTION, cartera_service.ACTION_TEARDOWN)


class CarteraPago(Document):
	"""Fase 27.1 -- one payment received against a Cartera Obligacion.

	Submittable on purpose: SUBMIT = the payment is registered (Frappe then
	forbids any edit natively); CANCEL = annulled (System Manager only, the
	record and its history stay). Never deleted, never amended.

	Money rules, enforced here for every path (Desk included):
	- amount > 0, company/customer/currency always copied from the
	  obligation, recorded_by/recorded_on always the server's;
	- no overpayment: before_submit locks the obligation row and re-reads
	  the other submitted payments with a locking read, so two users paying
	  the same obligation at once are serialized -- the second one sees the
	  first payment and is rejected if it would exceed the balance;
	- on_submit/on_cancel recompute the obligation's balance and status.

	source "Conductor" is reserved for the payment the system derives from a
	driver's "Pagado + comprobante" report (cartera_service.
	create_driver_reported_payment()): at most one per obligation, for the
	full value, and only when that report really exists.

	Payment is only RECORDED in Cartera: no Payment Entry/GL is created
	(accounting_status stays "Sin contabilizar" -- future reconciliation).

	27.3 -- closed to direct writes. Insert, save, submit and cancel require
	the private service token (cartera_service._authorize()): Desk,
	frappe.client, REST and a bare doc.insert()/submit()/cancel() are
	refused for EVERY user, Administrator included. The only writers are
	cartera_service.register_cartera_payment() (source Cartera),
	create_driver_reported_payment() (source Conductor) and
	reject_driver_payment() (cancel of the driver payment, with reason).
	Anulación de cobros manuales: out of 27.3 (no cancel path exists)."""

	def validate(self):
		action = cartera_service.authorized_action(self)
		if action not in _WRITE_ACTIONS:
			frappe.throw(
				_("Los cobros de cartera solo se registran desde la Page Cartera."), frappe.PermissionError
			)
		if self.amended_from:
			frappe.throw(_("Un pago de cartera no se puede enmendar; registra un pago nuevo."), frappe.ValidationError)

		obligation = self._obligation()
		if obligation.status == cartera_service.STATUS_ANULADA:
			frappe.throw(_("La obligación {0} está anulada.").format(obligation.name), frappe.ValidationError)
		if obligation.amount_source != cartera_service.AMOUNT_SOURCE_INVOICE:
			frappe.throw(
				_("La obligación {0} todavía no tiene un valor facturado definido.").format(obligation.name),
				frappe.ValidationError,
			)

		self.company = obligation.company
		self.customer = obligation.customer
		self.currency = obligation.currency

		if self.is_new():
			self.recorded_by = frappe.session.user
			self.recorded_on = now_datetime()
		else:
			before = self.get_doc_before_save()
			if before and (self.recorded_by != before.recorded_by or str(self.recorded_on) != str(before.recorded_on)):
				frappe.throw(_("El registro del pago no se puede modificar."), frappe.ValidationError)

		precision = self.precision("amount")
		if abs(flt(self.amount) - flt(self.amount, precision)) > 1e-9:
			# Never rounded silently (27.3 decision 6).
			frappe.throw(_("El valor recibido admite como máximo 2 decimales."), frappe.ValidationError)
		self.amount = flt(self.amount, precision)
		if self.amount <= 0:
			frappe.throw(_("El valor recibido debe ser mayor que cero."), frappe.ValidationError)

		if not self.payment_date:
			frappe.throw(_("La fecha del pago es obligatoria."), frappe.ValidationError)
		if getdate(self.payment_date) > getdate(nowdate()):
			frappe.throw(_("La fecha del pago no puede estar en el futuro."), frappe.ValidationError)

		if self.source == cartera_service.PAYMENT_SOURCE_DRIVER:
			if action != cartera_service.ACTION_DRIVER_PAYMENT:
				frappe.throw(
					_("Un pago con origen Conductor solo lo crea la entrega del conductor."), frappe.PermissionError
				)
			self._validate_driver_payment(obligation)
		elif self.source == cartera_service.PAYMENT_SOURCE_CARTERA:
			if action != cartera_service.ACTION_CARTERA_PAYMENT:
				frappe.throw(_("Origen del pago inválido."), frappe.PermissionError)
			if self.payment_method not in PAYMENT_METHODS:
				frappe.throw(_("Selecciona el medio de pago."), frappe.ValidationError)
			if not (self.client_request_id or "").startswith(cartera_service.CARTERA_REQUEST_PREFIX):
				frappe.throw(_("Identificador de solicitud inválido."), frappe.ValidationError)
			error = cartera_service.collectable_error(obligation)
			if error:
				frappe.throw(error, cartera_service.CarteraStateError)
		else:
			frappe.throw(_("Origen del pago inválido."), frappe.ValidationError)

		if self.accounting_status not in (None, "", "Sin contabilizar") or self.payment_entry:
			frappe.throw(
				_("Cartera no contabiliza pagos todavía; el estado contable es 'Sin contabilizar'."),
				frappe.ValidationError,
			)
		self.accounting_status = "Sin contabilizar"
		self._validate_payment_proof()

	def _validate_payment_proof(self):
		"""H4 -- payment_proof is empty or EXACTLY the private File attached
		to this payment in this field (created by the service). Any other
		URL (another stop's proof, a public file...) is refused."""
		if not self.payment_proof:
			return
		if self.source != cartera_service.PAYMENT_SOURCE_CARTERA or self.is_new():
			frappe.throw(_("Comprobante inválido."), frappe.ValidationError)
		if not frappe.db.exists(
			"File",
			{
				"attached_to_doctype": cartera_service.PAYMENT_DOCTYPE,
				"attached_to_name": self.name,
				"attached_to_field": "payment_proof",
				"file_url": self.payment_proof,
				"is_private": 1,
			},
		):
			frappe.throw(_("El comprobante no corresponde a este pago."), frappe.ValidationError)

	def _obligation(self):
		if not self.cartera_obligacion or not frappe.db.exists(
			cartera_service.OBLIGATION_DOCTYPE, self.cartera_obligacion
		):
			frappe.throw(_("La obligación de cartera no existe."), frappe.ValidationError)
		return frappe.get_doc(cartera_service.OBLIGATION_DOCTYPE, self.cartera_obligacion)

	def _validate_driver_payment(self, obligation):
		if not (
			obligation.driver_payment_status == cartera_service.DRIVER_PAID and obligation.driver_payment_proof
		):
			frappe.throw(
				_("Solo un pago reportado por el conductor con comprobante puede registrarse con origen Conductor."),
				frappe.ValidationError,
			)
		if flt(self.amount) != flt(obligation.invoice_amount, self.precision("amount")):
			frappe.throw(_("El pago del conductor debe ser por el valor facturado completo."), frappe.ValidationError)
		other = frappe.db.get_value(
			cartera_service.PAYMENT_DOCTYPE,
			{
				"cartera_obligacion": obligation.name,
				"source": cartera_service.PAYMENT_SOURCE_DRIVER,
				"docstatus": ["!=", 2],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if other:
			frappe.throw(_("Esta obligación ya tiene el pago reportado por el conductor."), frappe.ValidationError)
		# H3 -- the report must still be unresolved: once Cartera confirmed
		# or rejected it, no Conductor payment can be created again, and the
		# key "conductor:<parada>" stays reserved by the original one.
		if obligation.payment_verification != cartera_service.VERIFICATION_UNCONFIRMED:
			frappe.throw(_("El reporte del conductor ya fue resuelto por Cartera."), frappe.ValidationError)
		if self.client_request_id != f"{cartera_service.DRIVER_REQUEST_PREFIX}{obligation.recorrido_parada}":
			frappe.throw(_("Identificador de solicitud inválido."), frappe.ValidationError)

	def before_submit(self):
		# Row lock on the obligation first, then a LOCKING read of the other
		# submitted payments: concurrent submitters are serialized here and
		# the second one always sees the first one's payment.
		cartera_service.lock_obligation(self.cartera_obligacion)
		invoice_amount = frappe.db.get_value(cartera_service.OBLIGATION_DOCTYPE, self.cartera_obligacion, "invoice_amount")
		precision = self.precision("amount")
		paid_by_others, _last = cartera_service.submitted_payments(
			self.cartera_obligacion, exclude=self.name, for_update=True
		)
		outstanding = flt(flt(invoice_amount, precision) - paid_by_others, precision)
		if flt(self.amount, precision) > outstanding:
			frappe.throw(
				_("El valor recibido ({0}) supera el saldo pendiente ({1}).").format(
					frappe.format_value(self.amount, {"fieldtype": "Currency"}),
					frappe.format_value(outstanding, {"fieldtype": "Currency"}),
				),
				frappe.ValidationError,
			)

	def on_submit(self):
		cartera_service.recompute_obligation(self.cartera_obligacion)

	def before_cancel(self):
		"""H5 -- Frappe does NOT run validate() on cancel: this is the only
		gate. Only the service's driver-payment rejection (or test teardown)
		may cancel, always with a reason."""
		action = cartera_service.authorized_action(self)
		if action not in _CANCEL_ACTIONS:
			frappe.throw(_("Un pago de cartera no se puede anular directamente."), frappe.PermissionError)
		if action == cartera_service.ACTION_DRIVER_REJECTION and self.source != cartera_service.PAYMENT_SOURCE_DRIVER:
			frappe.throw(_("Solo el pago reportado por el conductor se puede rechazar."), frappe.PermissionError)
		self.cancellation_reason = cartera_service.clean_text(
			self.cancellation_reason,
			_("Motivo de anulación"),
			cartera_service.REASON_MAX_LENGTH,
			min_length=cartera_service.REASON_MIN_LENGTH,
			required=True,
		)

	def before_update_after_submit(self):
		frappe.throw(_("Un pago de cartera registrado no se puede modificar."), frappe.PermissionError)

	def on_cancel(self):
		cartera_service.recompute_obligation(self.cartera_obligacion)

	def on_trash(self):
		frappe.throw(_("Un pago de cartera no se puede eliminar; solo anular."), frappe.ValidationError)
