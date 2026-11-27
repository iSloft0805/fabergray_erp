# -*- coding: utf-8 -*-
"""HOTFIX cantidades divididas -- protección de la estructura y conservación
de cantidades de los Pick Lists de pedidos FabriGray.

Incident (reproduced): a Sales Order line of 4 with 3 in stock becomes two
Pick List rows (3 + a 1-unit top-up row). With the shortage reported and
nothing picked on the top-up row, COMPLETAR PEDIDO refused and told the user
to "adjust the Pick List"; any Bodega/Facturación user could then delete the
row in Desk/REST, complete with 3 and invoice 3 -- the 4th unit vanished
from every document (the Sales Order line kept 4, no Pick List carried the
remainder, the shortage report pointed at a deleted row).

Two independent layers, both enforced server-side on every save path:

1. STRUCTURE (before_save / before_update_after_submit / on_trash). Once a
   Delivery Pick List linked to a Sales Order exists, its rows are fixed:
   no row added or removed, and none of PROTECTED_ROW_FIELDS (product,
   Sales Order line, requested quantities, units, factor, warehouse,
   commercial references) changes -- unless the document carries this
   module's private service token (a Python object on doc.flags: JSON from
   Desk, REST or frappe.client can never produce it, so not even System
   Manager or Administrator can bypass it by editing). Deleting such a
   Pick List requires an explicit service context (allow_delete()).
   Picking (picked_qty), invoicing checks/prices and the other fg_* fields
   are not structure and keep their own guards.

2. CONSERVATION against the Sales Order line -- does NOT rely on the
   previous version of the document. For every Sales Order line in the
   Pick List, what all live Pick Lists claim (draft: stock_qty; submitted:
   picked_qty -- the same measure the fulfillment engine uses) is compared
   with the line's demand (Sales Order Item.stock_qty):
   - more than the demand -> duplicated quantities (checked on EVERY save,
     so a second Pick List for the same line cannot be created either);
   - less than the demand and not covered by an OPEN Reporte de Faltante of
     that line whose Pick List row still exists -> lost quantities (checked
     before COMPLETAR PEDIDO and before marking as invoiced).
   A pre-hotfix order whose row was already deleted is therefore refused
   with an administrative error instead of being completed/invoiced short;
   nothing is corrected automatically (see report_inconsistent_open_orders).

Legitimate writers (all keep working): the three Pick List builders of
fulfillment.pick_list_service (authorize() before insert; the empty-draft
cleanup and the Sales Order cancellation cleanup delete inside
allow_delete()), Bodega picking/shortages/COMPLETAR PEDIDO (picked_qty and
fg_started_* only), Jefe de Bodega resolutions (picked_qty), Facturación
(checks/prices/number/payment fields), the shortage-invoice authorization
fields, and ERPNext's own transferred/delivered quantity updates.
"""

from contextlib import contextmanager

import json

import frappe
from frappe import _
from frappe.utils import flt

#: Row fields that define WHAT was ordered and WHERE it is picked.
PROTECTED_ROW_FIELDS = (
	"item_code",
	"sales_order",
	"sales_order_item",
	"qty",
	"stock_qty",
	"uom",
	"stock_uom",
	"conversion_factor",
	"warehouse",
	"material_request",
	"material_request_item",
	"product_bundle_item",
)
OPEN_REPORT_STATUSES = ("Abierto", "En Proceso")
TOLERANCE = 1e-6

_SERVICE_TOKEN = object()
_DELETE_ALLOWED = "fg_pick_list_delete_allowed"


class PickListStructureError(frappe.PermissionError):
	"""A direct change to the rows/quantities of an order's Pick List."""


class PickListQuantityError(frappe.ValidationError):
	"""Quantities duplicated or lost against the Sales Order line."""


# ---------------------------------------------------------------------------
# Service token
# ---------------------------------------------------------------------------


def authorize(doc):
	doc.flags.fg_pick_list_structure_token = _SERVICE_TOKEN
	return doc


def is_authorized(doc):
	return doc.flags.get("fg_pick_list_structure_token") is _SERVICE_TOKEN


@contextmanager
def allow_delete(*names):
	"""Service context for deleting order Pick Lists (frappe.delete_doc loads
	its own document, so the token cannot travel on it). Lives on
	frappe.local only -- never settable from a request."""
	previous = getattr(frappe.local, _DELETE_ALLOWED, None)
	setattr(frappe.local, _DELETE_ALLOWED, set(previous or ()) | set(names))
	try:
		yield
	finally:
		setattr(frappe.local, _DELETE_ALLOWED, previous)


def _delete_allowed(name):
	return name in (getattr(frappe.local, _DELETE_ALLOWED, None) or ())


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def is_order_pick_list(doc):
	"""A Delivery Pick List with at least one row linked to a Sales Order --
	now or before this save."""
	if (doc.get("purpose") or "Delivery") != "Delivery":
		return False
	if any(row.get("sales_order") for row in doc.get("locations") or []):
		return True
	before = doc.get_doc_before_save() if not doc.is_new() else None
	return bool(before and any(row.get("sales_order") for row in before.get("locations") or []))


# ---------------------------------------------------------------------------
# Layer 1 -- structure
# ---------------------------------------------------------------------------


def _norm(value):
	if value is None or value == "":
		return None
	if isinstance(value, (int, float)):
		return flt(value, 9)
	return str(value)


def structure_changes(doc, before):
	"""Human-readable list of structural differences between `before` and
	`doc` (empty if none)."""
	changes = []
	old_rows = {row.name: row for row in before.get("locations") or []}
	new_rows = {row.name: row for row in doc.get("locations") or [] if row.name}
	added = [row for row in doc.get("locations") or [] if not row.name or row.name not in old_rows]
	removed = [row for name, row in old_rows.items() if name not in new_rows]
	for row in removed:
		changes.append(_("se eliminó la fila #{0} ({1}, {2})").format(row.idx, row.item_code, flt(row.stock_qty)))
	for row in added:
		changes.append(_("se agregó una fila ({0}, {1})").format(row.item_code, flt(row.stock_qty)))
	for name, row in new_rows.items():
		old = old_rows.get(name)
		if not old:
			continue
		for field in PROTECTED_ROW_FIELDS:
			if _norm(row.get(field)) != _norm(old.get(field)):
				changes.append(
					_("fila #{0} ({1}): {2} {3} → {4}").format(row.idx, row.item_code, field, old.get(field), row.get(field))
				)
	return changes


def guard_pick_list_structure(doc, method=None):
	"""Pick List `before_save` + `before_update_after_submit` (hooks.py). In
	before_save on purpose: it runs AFTER the native before_save, which on a
	Pick List without pick_manually re-allocates rows -- the document checked
	is the one actually written."""
	if not is_order_pick_list(doc):
		return
	if not doc.is_new() and not is_authorized(doc):
		before = doc.get_doc_before_save()
		if before:
			changes = structure_changes(doc, before)
			if changes:
				frappe.throw(
					_(
						"Las filas y cantidades de un pedido no se pueden modificar directamente "
						"({0}). Para gestionar un faltante usa REPORTAR FALTANTE en Bodega y las "
						"opciones autorizadas de Jefe de Bodega."
					).format("; ".join(changes[:5])),
					PickListStructureError,
				)
	assert_no_duplicated_quantities(doc)


def _has_open_bodega_reports(doc):
	"""An open shortage report of Bodega points at one of this Pick List's
	rows. Deleting the Pick List would orphan it -- the incident's pattern
	(and an orphan report breaks the Centro de Faltantes)."""
	if doc.name and frappe.db.exists("Reporte de Faltante", {"pick_list": doc.name, "status": ["in", OPEN_REPORT_STATUSES]}):
		return True
	names = [r.name for r in doc.get("locations") or [] if r.get("name")]
	return bool(names) and bool(
		frappe.db.exists("Reporte de Faltante", {"pick_list_item": ["in", names], "status": ["in", OPEN_REPORT_STATUSES]})
	)


def guard_pick_list_delete(doc, method=None):
	"""Pick List `on_trash`. A draft of an order may be deleted as ERPNext
	allows natively (with its own checks, e.g. an active stock reservation):
	the order simply has no Pick List again -- a visible pending order, never
	a silent short completion. While open Bodega shortage reports point at
	its rows, only the system deletes it (Sales Order cancellation)."""
	if not is_order_pick_list(doc):
		return
	if is_authorized(doc) or _delete_allowed(doc.name):
		return
	if not _has_open_bodega_reports(doc):
		return
	frappe.throw(
		_(
			"Este Pick List tiene faltantes reportados por Bodega y no se puede eliminar directamente; "
			"las cantidades del pedido y sus faltantes se perderían. Usa las opciones autorizadas de "
			"Jefe de Bodega."
		),
		PickListStructureError,
	)


# ---------------------------------------------------------------------------
# Layer 2 -- conservation against the Sales Order line
# ---------------------------------------------------------------------------


def _row_claim(docstatus, row):
	"""What a row holds of its Sales Order line, in stock units: a draft
	holds its requested stock_qty, a submitted one what was really picked."""
	return flt(row.get("picked_qty")) if docstatus == 1 else flt(row.get("stock_qty"))


def _line_facts(sales_order_items):
	if not sales_order_items:
		return {}
	return {
		r.name: r
		for r in frappe.db.sql(
			"""SELECT soi.name, soi.parent, soi.item_code, soi.item_name, soi.stock_qty, soi.qty, soi.uom,
				soi.conversion_factor, so.docstatus
			FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
			WHERE soi.name IN %s""",
			(tuple(sales_order_items),),
			as_dict=True,
		)
	}


def _other_claims(sales_order_items, exclude_pick_list):
	"""Claims of every OTHER live Pick List on these lines (locking read --
	the latest committed rows, never a stale snapshot)."""
	if not sales_order_items:
		return {}
	rows = frappe.db.sql(
		"""SELECT pli.sales_order_item, pl.docstatus, pli.stock_qty, pli.picked_qty
		FROM `tabPick List Item` pli JOIN `tabPick List` pl ON pl.name = pli.parent
		WHERE pli.sales_order_item IN %s AND pl.docstatus < 2 AND pl.name != %s
		FOR UPDATE""",
		(tuple(sales_order_items), exclude_pick_list or ""),
		as_dict=True,
	)
	claims = {}
	for r in rows:
		claims[r.sales_order_item] = claims.get(r.sales_order_item, 0.0) + _row_claim(r.docstatus, r)
	return claims


def _claims(doc):
	lines = sorted({row.sales_order_item for row in doc.get("locations") or [] if row.get("sales_order_item")})
	claims = _other_claims(lines, doc.name if not doc.is_new() else None)
	for row in doc.get("locations") or []:
		if row.get("sales_order_item"):
			claims[row.sales_order_item] = claims.get(row.sales_order_item, 0.0) + _row_claim(doc.docstatus, row)
	return lines, claims


def assert_no_duplicated_quantities(doc):
	lines, claims = _claims(doc)
	facts = _line_facts(lines)
	problems = []
	for soi in lines:
		line = facts.get(soi)
		if not line:
			continue
		demand = flt(line.stock_qty)
		if claims.get(soi, 0.0) > demand + TOLERANCE:
			problems.append(
				_("{0}: el pedido tiene {1} y los Pick Lists sumarían {2}").format(
					line.item_name or line.item_code, demand, flt(claims[soi], 6)
				)
			)
	if problems:
		frappe.throw(
			_("Cantidades duplicadas frente al pedido: {0}.").format("; ".join(problems)), PickListQuantityError
		)


def _open_report_coverage(sales_order_items):
	"""{soi: (covered_qty, orphan_reports)} -- open shortage reports of each
	line. A report whose Pick List row no longer exists is an ORPHAN (its
	row was deleted, typically before this hotfix): it covers nothing."""
	if not sales_order_items:
		return {}
	reports = frappe.db.sql(
		"""SELECT r.name, r.sales_order_item, r.pick_list_item, r.qty_faltante,
			EXISTS(SELECT 1 FROM `tabPick List Item` pli JOIN `tabPick List` pl ON pl.name = pli.parent
				WHERE pli.name = r.pick_list_item AND pl.docstatus < 2) AS row_exists
		FROM `tabReporte de Faltante` r
		WHERE r.sales_order_item IN %s AND r.status IN %s""",
		(tuple(sales_order_items), OPEN_REPORT_STATUSES),
		as_dict=True,
	)
	coverage = {}
	for r in reports:
		covered, orphans = coverage.get(r.sales_order_item, (0.0, []))
		if r.pick_list_item and not r.row_exists:
			orphans = orphans + [r.name]
		else:
			covered += flt(r.qty_faltante)
		coverage[r.sales_order_item] = (covered, orphans)
	return coverage


def _removed_evidence(pick_list_name):
	"""{soi: stock qty} that this Pick List's own change history (Version,
	track_changes is on for Pick List) shows as REMOVED from each Sales
	Order line: removed rows and rows whose stock_qty went down, net of rows
	added in the same saves (a native re-allocation removes and re-adds rows
	for the same quantity -- that nets to 0)."""
	evidence = {}
	if not pick_list_name:
		return evidence
	for data in frappe.get_all(
		"Version", filters={"ref_doctype": "Pick List", "docname": pick_list_name}, pluck="data", order_by="creation asc"
	):
		try:
			changes = json.loads(data or "{}")
		except ValueError:
			continue
		for sign, key in ((1, "removed"), (-1, "added")):
			for entry in changes.get(key) or []:
				if len(entry) == 2 and entry[0] == "locations" and isinstance(entry[1], dict):
					soi = entry[1].get("sales_order_item")
					if soi:
						evidence[soi] = evidence.get(soi, 0.0) + sign * flt(entry[1].get("stock_qty"))
		for entry in changes.get("row_changed") or []:
			if len(entry) == 4 and entry[0] == "locations":
				fields = {f[0]: f for f in entry[3] if len(f) == 3}
				if "stock_qty" in fields:
					_f, old, new = fields["stock_qty"]
					soi = frappe.db.get_value("Pick List Item", entry[2], "sales_order_item")
					if soi:
						evidence[soi] = evidence.get(soi, 0.0) + flt(old) - flt(new)
	return evidence


#: Categories of a Sales Order line seen from one Pick List (line_states()).
LINE_OK = "conservada"
LINE_PENDING = "pendiente_legitima"  # covered by an open shortage report
LINE_LOST = "perdida_con_evidencia"  # deleted rows / orphan shortage report
LINE_UNEXPLAINED = "diferencia_sin_evidencia"  # short, no report, no deletion trace
LINE_DUPLICATED = "duplicada"
BLOCKING_CATEGORIES = (LINE_LOST, LINE_UNEXPLAINED, LINE_DUPLICATED)


def line_states(doc):
	"""Read-only classification of each Sales Order line of `doc`:
	- conservada: the live Pick Lists claim exactly the demand;
	- pendiente_legitima: short, and the gap is covered by open shortage
	  reports (Bodega reported it; the authorized flows resolve it);
	- perdida_con_evidencia: short, with a deletion trace (rows/quantities
	  removed in this Pick List's own history, or an open report whose row
	  no longer exists);
	- diferencia_sin_evidencia: short with NO report and NO trace. The
	  absence of history is never taken as proof of integrity: the
	  authorized flows always put the whole demand on a Pick List
	  (full-demand / remainder builders), so an unexplained gap is refused
	  exactly like a proven loss;
	- duplicada: more than the demand claimed."""
	lines, claims = _claims(doc)
	facts = _line_facts(lines)
	coverage = _open_report_coverage(lines)
	removed = _removed_evidence(doc.name if not doc.is_new() else None)
	states = []
	for soi in lines:
		line = facts.get(soi)
		if not line:
			states.append({"sales_order_item": soi, "category": LINE_UNEXPLAINED, "message": _("la línea {0} del pedido no existe").format(soi)})
			continue
		label = line.item_name or line.item_code
		demand = flt(line.stock_qty)
		claimed = flt(claims.get(soi, 0.0), 6)
		covered, orphans = coverage.get(soi, (0.0, []))
		missing = flt(demand - claimed - covered, 6)
		state = {
			"sales_order_item": soi, "item_code": line.item_code, "demand": demand, "claimed": claimed,
			"covered_by_reports": flt(covered, 6), "missing": max(missing, 0.0), "orphan_reports": orphans,
			"removed_in_history": flt(removed.get(soi, 0.0), 6),
		}
		if claimed > demand + TOLERANCE:
			state["category"] = LINE_DUPLICATED
			state["message"] = _("{0}: cantidades duplicadas (pedido {1}, en Pick Lists {2})").format(label, demand, claimed)
		elif missing > TOLERANCE:
			evidence = bool(orphans) or removed.get(soi, 0.0) > TOLERANCE
			state["category"] = LINE_LOST if evidence else LINE_UNEXPLAINED
			if evidence:
				detail = _(" (reporte de faltante sin fila: {0})").format(", ".join(orphans)) if orphans else _(" (filas eliminadas del Pick List)")
				state["message"] = _("{0}: faltan {1} unidad(es) perdidas con evidencia de eliminación{2}").format(label, missing, detail)
			else:
				state["message"] = _(
					"{0}: faltan {1} unidad(es) frente al pedido sin Pick List, sin faltante reportado y sin "
					"historial que lo explique (diferencia sin evidencia suficiente)"
				).format(label, missing)
		elif claimed < demand - TOLERANCE:
			state["category"] = LINE_PENDING
			state["message"] = _("{0}: {1} pendiente(s) cubierto(s) por faltante reportado").format(label, flt(demand - claimed, 6))
		else:
			state["category"] = LINE_OK
			state["message"] = ""
		states.append(state)
	return states


def conversion_problems(doc):
	problems = []
	for row in doc.get("locations") or []:
		if row.get("sales_order_item") and flt(row.get("conversion_factor")) > 0:
			expected = flt(flt(row.qty) * flt(row.conversion_factor), 6)
			if abs(expected - flt(row.stock_qty)) > 1e-4:
				problems.append(
					(row.sales_order_item, _("{0}: conversión inconsistente en la fila #{1} ({2} {3} × {4} ≠ {5})").format(
						row.item_code, row.idx, flt(row.qty), row.uom, flt(row.conversion_factor), flt(row.stock_qty)
					))
				)
	return problems


def conservation_problems(doc):
	"""[(sales_order_item, message)] that block COMPLETAR / FACTURAR:
	duplicated, lost (with evidence), unexplained differences and
	inconsistent conversions. Read-only."""
	problems = [(st["sales_order_item"], st["message"]) for st in line_states(doc) if st["category"] in BLOCKING_CATEGORIES]
	return problems + conversion_problems(doc)


def assert_quantities_conserved(doc, stage):
	"""Before COMPLETAR PEDIDO / MARCAR COMO FACTURADO. A failing order is
	never corrected automatically: it needs the administrative recovery
	procedure."""
	problems = conservation_problems(doc)
	if problems:
		frappe.throw(
			_(
				"El pedido no puede {0} de forma segura: {1}. No se corrige automáticamente; "
				"comunícalo al administrador para aplicar el procedimiento de recuperación."
			).format(stage, "; ".join(msg for _soi, msg in problems)),
			PickListQuantityError,
		)


# ---------------------------------------------------------------------------
# Read-only detection of pre-hotfix inconsistent open orders
# ---------------------------------------------------------------------------


def report_inconsistent_open_orders(limit=500):
	"""READ-ONLY (bench execute): live Pick Lists of open orders whose
	quantities are duplicated, lost (deleted rows, orphan shortage reports)
	or inconsistently converted. Nothing is modified."""
	names = frappe.db.sql_list(
		"""SELECT DISTINCT pl.name FROM `tabPick List` pl
		JOIN `tabPick List Item` pli ON pli.parent = pl.name
		JOIN `tabSales Order` so ON so.name = pli.sales_order
		WHERE pl.docstatus < 2 AND so.docstatus = 1 AND so.status NOT IN ('Closed', 'Completed')
		ORDER BY pl.name LIMIT %s""",
		(int(limit),),
	)
	out = []
	for name in names:
		doc = frappe.get_doc("Pick List", name)
		for st in line_states(doc):
			if st["category"] in BLOCKING_CATEGORIES:
				out.append({"pick_list": name, "docstatus": doc.docstatus, "invoicing": doc.get("fg_invoicing_status"), "sales_order_item": st["sales_order_item"], "category": st["category"], "problem": st["message"]})
		for soi, message in conversion_problems(doc):
			out.append({"pick_list": name, "docstatus": doc.docstatus, "invoicing": doc.get("fg_invoicing_status"), "sales_order_item": soi, "category": "conversion_inconsistente", "problem": message})
	return out
