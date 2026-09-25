# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class ReportedeFaltante(Document):
	"""detected_by/shortage_reason contract (Commit 7): see
	FULFILLMENT_ENGINE_CONTRACT.md at the app root for the full write-up aimed
	at a future Fulfillment Engine. In short: detected_by="Bodega" is a human,
	physical discrepancy found while picking (report_shortage() in
	api/bodega.py) and always requires shortage_reason (enforced below, not
	duplicated by any caller); detected_by="Fulfillment Engine" is reserved for
	an upstream, non-physical detection -- no code path sets it yet -- and is
	not required to give a shortage_reason. Any future creator must go through
	api.bodega._create_shortage_report() rather than inserting this doctype
	directly, so this validation and the Item/Warehouse/order derivation stay
	in one place.
	"""

	def validate(self):
		self.set_missing_detection_fields()
		self.calculate_qty_faltante()
		self.validate_shortage_reason()
		self.validate_routing_fields()

	def validate_routing_fields(self):
		"""Fase 28.2 -- procurement_route / procurement_route_reason /
		work_order / production_qty_allocated are written ONLY by
		fabergray_erp.production_service (private token). Bodega and Jefe
		de Bodega keep their normal write permission on the rest of the
		report, but Desk, frappe.client or any other path can never set or
		change the route, the Work Order link or the allocated quantity."""
		from fabergray_erp import production_service

		if production_service.is_authorized(self):
			return
		if self.is_new():
			if any(self.get(f) for f in production_service.ROUTING_FIELDS):
				frappe.throw(
					_("La ruta de abastecimiento solo la fija el servidor al enrutar el faltante."),
					frappe.PermissionError,
				)
			return
		before = self.get_doc_before_save()
		if not before:
			return
		changed = [
			f
			for f in production_service.ROUTING_FIELDS
			if (flt(self.get(f)) != flt(before.get(f)) if f == "production_qty_allocated" else (self.get(f) or None) != (before.get(f) or None))
		]
		if changed:
			frappe.throw(
				_("La ruta de abastecimiento solo la fija el servidor al enrutar el faltante ({0}).").format(
					", ".join(changed)
				),
				frappe.PermissionError,
			)

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
