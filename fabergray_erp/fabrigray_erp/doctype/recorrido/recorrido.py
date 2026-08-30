# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class Recorrido(Document):
	"""Commit 24.1 -- base model only. The full Borrador -> Planificado ->
	En Ruta -> Completado/Cancelado workflow (eligibility checks, double-
	assignment locking, stop resolution) lives entirely in
	api/recorridos.py's own whitelisted functions, not here -- this
	controller only enforces the one invariant that has to hold regardless
	of which code path inserts a Recorrido: created_by_user is always the
	real inserting user, never client-suppliable. status transitions are
	deliberately NOT re-validated here (create_route()/plan_route()/
	cancel_route() are the only intended write path in this commit); a
	System Manager editing this doctype directly via Desk is trusted the
	same way this app already trusts System Manager everywhere else."""

	def validate(self):
		if not self.created_by_user:
			self.created_by_user = frappe.session.user
