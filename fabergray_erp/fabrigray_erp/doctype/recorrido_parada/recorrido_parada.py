# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


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
			frappe.throw(
				_("Solo se pueden eliminar paradas de un recorrido en estado Borrador."),
				frappe.ValidationError,
			)
