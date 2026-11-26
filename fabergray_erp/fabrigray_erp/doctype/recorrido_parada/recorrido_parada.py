# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_datetime

#: Fase 26.3 -- fields that only api.recorridos.deliver_stop() may fill, and
#: only in the Pendiente -> Entregado transition. delivery_note (the
#: optional "Observaciones de entrega") and the driver's faltantes/pago
#: report are included: they are part of the delivery record, not
#: free-form stop data.
DELIVERY_FIELDS = (
	"delivered_on",
	"delivered_by",
	"delivery_photo",
	"customer_signature",
	"delivery_note",
	"has_delivery_issues",
	"delivery_issues",
	"payment_status",
	"payment_proof",
	"payment_note",
	# Entrega masiva -- regularización administrativa (see below).
	"delivery_mode",
	"entrega_masiva",
	"effective_delivery_date",
	"regularized_on",
)
DELIVERY_REQUIRED_FIELDS = ("delivered_on", "delivered_by", "delivery_photo", "customer_signature", "payment_status")

#: Entrega masiva -- an ALREADY-OCCURRED delivery registered by
#: fabergray_erp.entrega_masiva_service, never by a driver. Only that service
#: (private token on the document's flags, impossible to produce from JSON)
#: may set it; the normal driver path keeps every requirement above intact.
DELIVERY_MODE_REGULARIZATION = "Regularización administrativa"
REGULARIZATION_FIELDS = ("delivery_mode", "entrega_masiva", "effective_delivery_date", "regularized_on")
REGULARIZATION_REQUIRED_FIELDS = (
	"delivered_on",
	"delivered_by",
	"payment_status",
	"entrega_masiva",
	"effective_delivery_date",
	"regularized_on",
)
#: Never "Pagado": a regularization never claims money was received.
REGULARIZATION_PAYMENT_STATUSES = ("Pendiente por Pago", "Crédito")

#: Fase 26.3 -- payment status REPORTED by the driver (never an accounting
#: confirmation). Shared with api.recorridos.deliver_stop().
PAYMENT_STATUS_PAID = "Pagado"
PAYMENT_STATUSES = (PAYMENT_STATUS_PAID, "Pendiente por Pago", "Crédito")


def _normalized(fieldtype, value):
	"""Type-aware comparison value, so a Desk save that sends a Datetime or
	number back as a string is not mistaken for a change."""
	if value in (None, ""):
		return None
	if fieldtype in ("Datetime", "Date"):
		return get_datetime(value)
	if fieldtype in ("Float", "Currency", "Percent"):
		return flt(value)
	if fieldtype in ("Int", "Check"):
		return cint(value)
	return cstr(value)


class RecorridoParada(Document):
	"""Commit 24.1 -- deliberately a standalone DocType, not a Table field
	on Recorrido. See api/recorridos.py's own top docstring for the full
	architectural reasoning (child tables have no independent permission
	model in this Frappe version -- confirmed empirically in Commit 23.0
	with "Pick List Item" -- and future evidence/novedades/GPS-position
	records will each need to Link back to one specific parada, which a
	child row's own unstable identity cannot support cleanly).

	No validate()/business logic here on purpose: every write path (add/
	remove/reorder a stop, snapshot resolution from the real Pick List)
	goes through api.recorridos, which already re-validates everything
	server-side before ever calling .insert()/.save() on this doctype.

	on_trash() below is the one exception: Recorrido Parada's own DocPerm
	grants "delete" to the "Recorrido" role (see this doctype's own JSON --
	needed so update_route_stops() can legitimately remove a stop while its
	parent Recorrido is still Borrador). That same grant, with nothing else
	in place, would also let anyone with the "Recorrido" role delete a
	parada directly from Desk/API on a route that is already Planificado/En
	Ruta/Completado/Cancelado -- silently destroying the historical record
	of what was actually delivered, with no trace. on_trash() is the
	correct hook for this (not validate(), which never runs on delete; not
	before_delete(), which frappe.model.document.Document does not define
	as a distinct hook -- on_trash is the one Frappe calls, via
	doc.run_method("on_trash"), for every delete path: Desk, frappe.
	delete_doc(), and doc.delete() alike, unless a caller explicitly passes
	ignore_on_trash=True, which nothing in this app ever does). Deliberately
	does NOT check frappe.local.flags or any other "trust the caller" bypass
	-- update_route_stops() itself only ever deletes a stop while its own
	already-loaded `route.status == "Borrador"` check passed moments
	earlier, so this guard re-confirming the SAME fact from the DB is
	redundant-but-harmless for that legitimate path, and is the ONLY thing
	standing between a direct Desk/API delete and a silently-destroyed
	stop."""

	# -- Fase 26.3 -- delivery lifecycle guard -------------------------------
	# Recorrido role holds DocType-level write on this doctype (needed by
	# api.recorridos' own writes), so without this a Desk save / doc.save() /
	# frappe.client.set_value() could mark a stop "Entregado" with no photo
	# or signature -- and Ventas would show it as ENTREGADO. The only
	# intended path is api.recorridos.deliver_stop(); this guard makes every
	# other path follow the same rules:
	#
	# - insert: always Pendiente, with no delivery field;
	# - while Pendiente: no delivery field may hold a value;
	# - Pendiente -> Entregado: only while the Recorrido is En Ruta and with
	#   photo + signature + delivered_on + delivered_by + a valid
	#   payment_status; payment_proof only with "Pagado"; delivery_issues
	#   required exactly when has_delivery_issues;
	# - Entregado: frozen -- no field may change, status included;
	# - No Entregado: not available yet (its own phase).
	#
	# `_doc_before_save` is loaded by Frappe with for_update=True, so the
	# comparison is against the real locked row.

	def validate(self):
		if self.is_new():
			self._validate_regularization_route_untouched(self.recorrido)
			self._validate_new_stop()
			return

		before = self.get_doc_before_save()
		if not before:
			return

		if before.status == "Entregado":
			self._validate_delivered_stop_frozen(before)
			return

		if self.status == "Pendiente":
			self._validate_regularization_route_untouched(self.recorrido, before.recorrido)
			self._validate_no_delivery_fields()
		elif self.status == "Entregado":
			self._validate_delivery_transition()
		else:
			frappe.throw(
				_("Cambio de estado de parada no permitido: {0} → {1}.").format(_(before.status), _(self.status)),
				frappe.ValidationError,
			)

	def _validate_regularization_route_untouched(self, *routes):
		"""Entrega masiva: only the service (token) adds stops to an
		administrative regularization route; Desk/REST can neither insert a
		stop into one nor move a pending stop into or out of one."""
		from fabergray_erp import entrega_masiva_service

		if entrega_masiva_service.is_authorized(self):
			return
		for route in {r for r in routes if r}:
			if frappe.db.get_value("Recorrido", route, "route_kind") == entrega_masiva_service.ROUTE_KIND_REGULARIZATION:
				frappe.throw(
					_("Las paradas de un recorrido de regularización administrativa solo las gestiona la entrega masiva."),
					frappe.PermissionError,
				)

	def _validate_new_stop(self):
		if (self.status or "Pendiente") != "Pendiente":
			frappe.throw(_("Una parada nueva siempre inicia Pendiente."), frappe.ValidationError)
		self._validate_no_delivery_fields()

	def _validate_no_delivery_fields(self):
		filled = [f for f in DELIVERY_FIELDS if self.get(f)]
		if filled:
			frappe.throw(
				_("Una parada Pendiente no puede tener datos de entrega ({0}).").format(", ".join(filled)),
				frappe.ValidationError,
			)

	def _validate_delivery_transition(self):
		route_status = frappe.db.get_value("Recorrido", self.recorrido, "status")
		if route_status != "En Ruta":
			frappe.throw(
				_("Solo se puede entregar una parada de un recorrido En Ruta."), frappe.ValidationError
			)
		if self.delivery_mode == DELIVERY_MODE_REGULARIZATION:
			self._validate_regularization_delivery()
			return
		filled = [f for f in REGULARIZATION_FIELDS if self.get(f) and not (f == "delivery_mode" and self.get(f) == "Conductor")]
		if filled:
			frappe.throw(
				_("Datos de regularización administrativa no permitidos en una entrega normal ({0}).").format(
					", ".join(filled)
				),
				frappe.PermissionError,
			)
		missing = [f for f in DELIVERY_REQUIRED_FIELDS if not self.get(f)]
		if missing:
			frappe.throw(
				_("Faltan datos obligatorios de la entrega: {0}.").format(", ".join(missing)),
				frappe.ValidationError,
			)
		if self.payment_status not in PAYMENT_STATUSES:
			frappe.throw(_("Estado del pago inválido: {0}.").format(self.payment_status), frappe.ValidationError)
		if self.payment_proof and self.payment_status != PAYMENT_STATUS_PAID:
			frappe.throw(
				_("El comprobante de pago solo se admite cuando el estado del pago es Pagado."),
				frappe.ValidationError,
			)
		if cint(self.has_delivery_issues) and not (self.delivery_issues or "").strip():
			frappe.throw(_("Falta el detalle de faltantes / cambios."), frappe.ValidationError)
		if not cint(self.has_delivery_issues) and (self.delivery_issues or "").strip():
			frappe.throw(
				_("Hay detalle de faltantes / cambios sin marcar Faltantes / cambios."), frappe.ValidationError
			)

	def _validate_regularization_delivery(self):
		"""Entrega masiva: no photo/signature (the delivery already happened
		and nobody captured them), but only through the service token, never
		"Pagado", no payment proof, no faltantes, and delivered_on carrying
		exactly the declared day (its 00:00 time is not a verified hour)."""
		from fabergray_erp import entrega_masiva_service

		if not entrega_masiva_service.is_authorized(self):
			frappe.throw(
				_("La regularización administrativa solo la registra el servicio de entrega masiva."),
				frappe.PermissionError,
			)
		missing = [f for f in REGULARIZATION_REQUIRED_FIELDS if not self.get(f)]
		if missing:
			frappe.throw(
				_("Faltan datos obligatorios de la regularización: {0}.").format(", ".join(missing)),
				frappe.ValidationError,
			)
		present = [f for f in ("delivery_photo", "customer_signature", "payment_proof") if self.get(f)]
		if present:
			frappe.throw(
				_("Una regularización administrativa no lleva evidencia del conductor ({0}).").format(", ".join(present)),
				frappe.ValidationError,
			)
		if self.payment_status not in REGULARIZATION_PAYMENT_STATUSES:
			frappe.throw(
				_("Una regularización nunca registra el pedido como pagado ({0}).").format(self.payment_status),
				frappe.ValidationError,
			)
		if cint(self.has_delivery_issues) or (self.delivery_issues or "").strip():
			frappe.throw(_("Una regularización no registra faltantes / cambios."), frappe.ValidationError)
		if get_datetime(self.delivered_on).date() != get_datetime(self.effective_delivery_date).date():
			frappe.throw(_("La fecha de entrega no coincide con la fecha efectiva declarada."), frappe.ValidationError)

	def _validate_delivered_stop_frozen(self, before):
		for df in self.meta.fields:
			if df.fieldtype in no_value_fields:
				continue
			if _normalized(df.fieldtype, self.get(df.fieldname)) != _normalized(
				df.fieldtype, before.get(df.fieldname)
			):
				frappe.throw(
					_("Una parada Entregada no se puede modificar ({0}).").format(df.fieldname),
					frappe.ValidationError,
				)

	def on_update(self):
		# Hotfix cierre automático -- after ANY save that moves a stop to a
		# terminal status, recompute the route's status server-side
		# (deliver_stop() and every other save path alike). Idempotent.
		if self.has_value_changed("status") and self.status in ("Entregado", "No Entregado"):
			# Lazy import, same reason as Recorrido.validate(): no import cycle.
			from fabergray_erp.api.recorridos import reconcile_recorrido_status

			reconcile_recorrido_status(self.recorrido)

	def on_trash(self):
		if not self.recorrido:
			frappe.throw(
				_("No se puede eliminar esta parada: no tiene un recorrido asociado."),
				frappe.ValidationError,
			)

		route_status = frappe.db.get_value("Recorrido", self.recorrido, "status")
		if route_status is None:
			frappe.throw(
				_("No se puede eliminar esta parada: el recorrido asociado ({0}) no existe.").format(
					self.recorrido
				),
				frappe.ValidationError,
			)

		if route_status != "Borrador":
			# Entrega masiva -- a PENDING stop of a regularization route whose
			# order failed is removed by the service (token), so the route
			# never closes with an undelivered stop nor stays open forever.
			from fabergray_erp import entrega_masiva_service

			if (
				self.status == "Pendiente"
				and entrega_masiva_service.is_authorized(self)
				and frappe.db.get_value("Recorrido", self.recorrido, "route_kind")
				== entrega_masiva_service.ROUTE_KIND_REGULARIZATION
			):
				return
			frappe.throw(
				_("Solo se pueden eliminar paradas de un recorrido en estado Borrador."),
				frappe.ValidationError,
			)
