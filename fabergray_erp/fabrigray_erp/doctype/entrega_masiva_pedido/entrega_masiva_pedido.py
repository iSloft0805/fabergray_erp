# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EntregaMasivaPedido(Document):
	"""Per-order result of an Entrega Masiva. Same protection as the batch:
	only the service (private token) inserts or updates it; never deleted."""

	def validate(self):
		from fabergray_erp import entrega_masiva_service

		if not entrega_masiva_service.is_authorized(self):
			frappe.throw(
				_("El resultado de una entrega masiva solo lo registra el servicio de entrega masiva."),
				frappe.PermissionError,
			)

	def on_trash(self):
		frappe.throw(_("El resultado de una entrega masiva no se puede eliminar."), frappe.ValidationError)
