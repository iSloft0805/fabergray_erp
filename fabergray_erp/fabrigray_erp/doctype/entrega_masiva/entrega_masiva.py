# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EntregaMasiva(Document):
	"""Audit record of one administrative bulk-delivery batch (Recorridos).

	Every insert and every change must carry the private token of
	fabergray_erp.entrega_masiva_service (a Python object: JSON from Desk,
	frappe.client or REST can never produce it), so the record -- who, when,
	criteria, reason, counts -- cannot be fabricated or edited by hand, System
	Manager included. It is never deleted."""

	def validate(self):
		from fabergray_erp import entrega_masiva_service

		if not entrega_masiva_service.is_authorized(self):
			frappe.throw(
				_("Las entregas masivas solo las registra el servicio de entrega masiva."), frappe.PermissionError
			)

	def on_trash(self):
		frappe.throw(_("Una entrega masiva no se puede eliminar."), frappe.ValidationError)
