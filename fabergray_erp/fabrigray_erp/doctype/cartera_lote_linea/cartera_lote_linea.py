# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CarteraLoteLinea(Document):
	"""Per-obligation result of a Cartera Lote. Same protection as the
	batch: only the service (private token) inserts or updates it; never
	deleted."""

	def validate(self):
		from fabergray_erp import cartera_masiva_service

		if not cartera_masiva_service.is_authorized(self):
			frappe.throw(
				_("El resultado de un lote de cartera solo lo registra el servicio de cartera masiva."), frappe.PermissionError
			)

	def on_trash(self):
		frappe.throw(_("El resultado de un lote de cartera no se puede eliminar."), frappe.ValidationError)
