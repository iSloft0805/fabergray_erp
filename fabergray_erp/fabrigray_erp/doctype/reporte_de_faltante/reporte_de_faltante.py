# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class ReportedeFaltante(Document):
	def validate(self):
		self.set_missing_detection_fields()
		self.calculate_qty_faltante()
		self.validate_shortage_reason()

	def set_missing_detection_fields(self):
		if not self.reported_by:
			self.reported_by = frappe.session.user
		if not self.reported_on:
			self.reported_on = now_datetime()

	def calculate_qty_faltante(self):
		self.qty_faltante = max(flt(self.qty_solicitada) - flt(self.qty_disponible), 0)

	def validate_shortage_reason(self):
		if self.detected_by == "Bodega" and not self.shortage_reason:
			frappe.throw(
				_("Motivo del Faltante es obligatorio cuando Detectado Por es Bodega.")
			)
