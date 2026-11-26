# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CarteraLote(Document):
	"""Audit record of one bulk Cartera batch (v1: authorization of the
	driver-reported payments). Every insert and change must carry the
	private token of fabergray_erp.cartera_masiva_service (a Python object:
	JSON from Desk, frappe.client or REST can never produce it), so who,
	when, filters, reason and counts can never be fabricated or edited by
	hand, System Manager and Administrator included. Never deleted."""

	def validate(self):
		from fabergray_erp import cartera_masiva_service

		if not cartera_masiva_service.is_authorized(self):
			frappe.throw(_("Los lotes de cartera solo los registra el servicio de cartera masiva."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Un lote de cartera no se puede eliminar."), frappe.ValidationError)
