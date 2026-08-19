# -*- coding: utf-8 -*-
"""Thin API layer for the future Bodega Page.

Every function here is a thin wrapper around standard Pick List / Reporte de
Faltante behaviour -- it does not reimplement ERPNext's picking, submission or
Sales Order status logic. Permissions (Role Permissions + User Permissions,
including the per-Warehouse scoping set up for the Bodega role) are always
checked through frappe's own mechanisms (`frappe.get_list`, `doc.check_permission`,
`frappe.has_permission`) -- nothing here uses `ignore_permissions`.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from erpnext.stock.doctype.pick_list.pick_list import get_actual_qty

OPEN_SHORTAGE_STATUSES = ["Abierto", "En Proceso"]


def _require_login():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(_("Debes iniciar sesión."), frappe.AuthenticationError)


def _get_pick_list_row(pick_list_doc, row_name):
	"""Fetch a Pick List Item row and confirm it actually belongs to this Pick List.

	Never trust a `row_name` supplied by the caller without checking parentage --
	this is what stops one user from pointing a call at a row that lives on a
	Pick List they don't have access to.
	"""
	rows = pick_list_doc.get("locations", {"name": row_name})
	if not rows:
		frappe.throw(
			_("La fila {0} no pertenece al Pick List {1}.").format(row_name, pick_list_doc.name)
		)
	return rows[0]


def _lock_manual_picking(pick_list_doc):
	"""Ensure a Pick List's rows never get silently rebuilt out from under us.

	Pick List's own `before_save()` calls `set_item_locations()` whenever
	`pick_manually` is falsy, which *removes and recreates* (new row `name`,
	new auto-suggested warehouse/location) every row whose `picked_qty` is
	still 0 -- including rows nobody has touched yet. Since our whole API is
	built around a stable `row_name` handed to the UI by get_pick_list() and
	later passed back into set_picked_qty()/report_shortage(), that rebuild
	would invalidate row identities out from under the caller the first time
	anyone saves the document. `pick_manually` exists precisely to opt out of
	that auto-relocation and hand control to the picker -- which is exactly
	our case -- so we set it (idempotently) before the first write instead of
	inventing our own row-stability logic."""
	if not pick_list_doc.pick_manually:
		pick_list_doc.pick_manually = 1


def _get_shortage_report_rows(pick_list_item_names, statuses=None):
	"""Return the set of Pick List Item row names that already have a linked
	Reporte de Faltante (optionally filtered by status). Uses frappe.get_list so
	the result is scoped to what the current user is allowed to read."""
	if not pick_list_item_names:
		return set()

	filters = {"pick_list_item": ["in", pick_list_item_names]}
	if statuses:
		filters["status"] = ["in", statuses]

	return set(
		frappe.get_list("Reporte de Faltante", filters=filters, pluck="pick_list_item")
	)


def _create_shortage_report(pick_list_doc, row, qty_disponible, shortage_reason=None,
							detected_by="Bodega", resolution_note=None):
	"""Shared creator for Reporte de Faltante -- the Fulfillment Engine extension point.

	Used today by report_shortage() (detected_by="Bodega", physical discrepancy
	found while picking). Deliberately kept generic (detected_by is a parameter,
	not hardcoded): a future Fulfillment Engine reuses this exact function with
	detected_by="Fulfillment Engine" instead of duplicating this logic. Full
	contract documented in FULFILLMENT_ENGINE_CONTRACT.md at the app root
	(Commit 7) -- read that before wiring up a caller. Building the engine
	itself is out of scope of that commit too; only this shared entry point
	and its contract are.

	Known limitation (documented, not solved here): this function requires an
	already-loaded Pick List document and one of its `locations` rows, so it
	only covers a shortage detected against an existing Pick List. A shortage
	detected upstream -- e.g. at Material Request stage, before any Pick List
	exists -- cannot go through this signature yet; that needs a deliberate
	signature extension in a later commit, not a bypass of this function.

	Item/Warehouse/Sales Order/Material Request are always derived from the
	validated Pick List row, never accepted as free-form input, and
	shortage_reason's "required when Bodega" rule is enforced by the doctype's
	own validate() (Commit 2), not duplicated here. Creates exactly one document;
	never touches Stock Ledger, Bin, Sales Order, Material Request, Purchase
	Order or Work Order -- no stock reservation, no automation, ever.
	"""
	frappe.has_permission("Reporte de Faltante", "create", throw=True)

	report = frappe.get_doc(
		{
			"doctype": "Reporte de Faltante",
			"item_code": row.item_code,
			"warehouse": row.warehouse,
			"sales_order": row.sales_order or None,
			"material_request": row.material_request or pick_list_doc.material_request or None,
			"pick_list": pick_list_doc.name,
			"pick_list_item": row.name,
			"qty_solicitada": row.stock_qty,
			"qty_disponible": qty_disponible,
			"detected_by": detected_by,
			"shortage_reason": shortage_reason,
			"resolution_note": resolution_note,
		}
	)
	report.insert()
	return report.name


@frappe.whitelist()
def get_queue():
	"""Pick Lists the current user may read, bucketed for the future Bodega Page.

	Uses `frappe.get_list` (not `get_all`) so Role Permissions AND User
	Permissions -- in particular a per-Warehouse User Permission on the Bodega
	role -- are applied by frappe itself: a user restricted to one Warehouse
	simply never sees rows whose `parent_warehouse` isn't theirs. No manual
	warehouse filtering is written here.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	pick_lists = frappe.get_list(
		"Pick List",
		filters={"docstatus": ["in", [0, 1]]},
		fields=[
			"name",
			"docstatus",
			"status",
			"purpose",
			"parent_warehouse",
			"customer",
			"fg_started_by",
			"fg_started_on",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=0,
	)

	names = [pl.name for pl in pick_lists]
	shortage_pick_lists = set()
	if names:
		shortage_pick_lists = set(
			frappe.get_list(
				"Reporte de Faltante",
				filters={"pick_list": ["in", names], "status": ["in", OPEN_SHORTAGE_STATUSES]},
				pluck="pick_list",
			)
		)

	# Line count / sales_order per Pick List, for card display. Pick List Item is a
	# child table with no permission model of its own -- access is governed entirely
	# by the parent, and `names` here is already the permission-filtered result of
	# the frappe.get_list("Pick List", ...) call above, so frappe.get_all (which
	# skips the -- inapplicable -- child-table permission check) is safe.
	line_counts = {}
	sales_order_by_pick_list = {}
	if names:
		for row in frappe.get_all(
			"Pick List Item", filters={"parent": ["in", names]}, fields=["parent", "sales_order"]
		):
			line_counts[row.parent] = line_counts.get(row.parent, 0) + 1
			if row.sales_order and row.parent not in sales_order_by_pick_list:
				sales_order_by_pick_list[row.parent] = row.sales_order

	buckets = {"listos": [], "con_faltantes": [], "en_alistamiento": [], "pendientes": []}
	for pl in pick_lists:
		entry = {
			"name": pl.name,
			"status": pl.status,
			"purpose": pl.purpose,
			"parent_warehouse": pl.parent_warehouse,
			"customer": pl.customer,
			"fg_started_by": pl.fg_started_by,
			"fg_started_on": pl.fg_started_on,
			"item_count": line_counts.get(pl.name, 0),
			"sales_order": sales_order_by_pick_list.get(pl.name),
		}
		if pl.docstatus == 1:
			buckets["listos"].append(entry)
		elif pl.name in shortage_pick_lists:
			buckets["con_faltantes"].append(entry)
		elif pl.fg_started_by:
			buckets["en_alistamiento"].append(entry)
		else:
			buckets["pendientes"].append(entry)

	return buckets


@frappe.whitelist()
def get_pick_list(name):
	"""Minimal, UI-ready view of one Pick List -- not the full document.

	Availability per row is read live from Bin via ERPNext's own
	`get_actual_qty` helper (same one Pick List's own code uses), not cached
	from whatever was on the row when it was created.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", name)
	pl.check_permission("read")

	row_names = [row.name for row in pl.get("locations")]
	shortage_rows = _get_shortage_report_rows(row_names)

	rows = []
	for row in pl.get("locations"):
		rows.append(
			{
				"row_name": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"description": row.description,
				"uom": row.uom,
				"warehouse": row.warehouse,
				"qty_solicitada": row.stock_qty,
				"qty_alistada": row.picked_qty,
				"qty_disponible": flt(get_actual_qty(row.item_code, row.warehouse)),
				"has_shortage_report": row.name in shortage_rows,
			}
		)

	sales_order = next((row.sales_order for row in pl.get("locations") if row.sales_order), None)

	return {
		"name": pl.name,
		"docstatus": pl.docstatus,
		"status": pl.status,
		"purpose": pl.purpose,
		"parent_warehouse": pl.parent_warehouse,
		"customer": pl.customer,
		"sales_order": sales_order,
		"fg_started_by": pl.fg_started_by,
		"fg_started_on": pl.fg_started_on,
		"rows": rows,
	}


@frappe.whitelist()
def start_picking(name):
	"""Mark a Pick List as started by the current user.

	Concurrency: frappe already detects two overlapping writers on the same
	document (Document.check_if_latest loads the row with `FOR UPDATE` and
	compares the `modified` timestamp), raising TimestampMismatchError -- no
	manual locking is added here. What IS added on top of that native
	mechanism is the explicit "already started" check below, so that even in
	the non-concurrent case (second employee opens it a minute later) the
	original fg_started_by/fg_started_on are never silently overwritten.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", name)
	pl.check_permission("write")

	if pl.docstatus != 0:
		frappe.throw(_("Solo se puede iniciar el alistamiento de un Pick List en borrador."))

	if pl.fg_started_by:
		frappe.throw(
			_("Este Pick List ya fue iniciado por {0} el {1}.").format(
				pl.fg_started_by, pl.fg_started_on
			)
		)

	pl.fg_started_by = frappe.session.user
	pl.fg_started_on = now_datetime()
	_lock_manual_picking(pl)

	try:
		pl.save()
	except frappe.exceptions.TimestampMismatchError:
		frappe.throw(
			_("Otro usuario ya está iniciando este Pick List. Actualiza la pantalla e intenta de nuevo."),
			exc=frappe.exceptions.TimestampMismatchError,
		)

	return {"name": pl.name, "fg_started_by": pl.fg_started_by, "fg_started_on": pl.fg_started_on}


@frappe.whitelist()
def set_picked_qty(name, row_name, qty):
	"""Set the picked quantity for one Pick List Item row.

	`row_name` is validated against this specific Pick List (see
	_get_pick_list_row) so a row from a different Pick List can never be
	targeted through this call. The over-delivery ceiling reuses ERPNext's own
	Stock Settings.over_delivery_receipt_allowance instead of inventing a new
	threshold, and the physical-stock ceiling is enforced by Pick List's own
	`validate_stock_qty()` when `doc.save()` runs -- not duplicated here.
	`_lock_manual_picking` is called before saving in case this is the first
	write on the document (see its docstring for why that matters).
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", name)
	pl.check_permission("write")

	if pl.docstatus != 0:
		frappe.throw(_("No se puede modificar un Pick List que ya fue finalizado o cancelado."))

	row = _get_pick_list_row(pl, row_name)

	qty = flt(qty)
	if qty < 0:
		frappe.throw(_("La cantidad alistada no puede ser negativa."))

	if row.stock_qty:
		allowance_pct = 100 + flt(
			frappe.get_single_value("Stock Settings", "over_delivery_receipt_allowance")
		)
		if (qty / flt(row.stock_qty)) * 100 > allowance_pct:
			frappe.throw(
				_(
					"La cantidad alistada ({0}) supera lo permitido para la fila {1} "
					"(máximo {2}% de lo solicitado, según la tolerancia configurada en Stock Settings)."
				).format(qty, row.idx, allowance_pct)
			)

	row.picked_qty = qty
	_lock_manual_picking(pl)

	try:
		pl.save()
	except frappe.exceptions.TimestampMismatchError:
		frappe.throw(
			_("Otro usuario modificó este Pick List al mismo tiempo. Actualiza y vuelve a intentar."),
			exc=frappe.exceptions.TimestampMismatchError,
		)

	updated_row = _get_pick_list_row(pl, row_name)
	return {"row_name": row_name, "picked_qty": updated_row.picked_qty}


@frappe.whitelist()
def report_shortage(pick_list, row_name, qty_disponible, shortage_reason, resolution_note=None):
	"""Report a physical shortage found while picking (detected_by="Bodega").

	Item/Warehouse/Sales Order are always derived from `row_name` after
	confirming it belongs to `pick_list` -- never accepted as raw input.
	shortage_reason's "required" rule lives in the doctype itself (Commit 2),
	not duplicated here. Only ever creates one Reporte de Faltante; never
	touches Stock Ledger, Bin or any inventory record.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", pick_list)
	pl.check_permission("write")

	if pl.docstatus == 2:
		frappe.throw(_("No se puede reportar un faltante sobre un Pick List cancelado."))

	row = _get_pick_list_row(pl, row_name)

	qty_disponible = flt(qty_disponible)
	if qty_disponible < 0:
		frappe.throw(_("La cantidad disponible no puede ser negativa."))

	report_name = _create_shortage_report(
		pick_list_doc=pl,
		row=row,
		qty_disponible=qty_disponible,
		shortage_reason=shortage_reason,
		detected_by="Bodega",
		resolution_note=resolution_note,
	)
	return {"name": report_name}


@frappe.whitelist()
def finish_picking(name):
	"""Submit a Pick List through its standard ERPNext submit flow.

	Does not invent a new status: `pl.submit()` is what runs Pick List's own
	`before_submit`/`on_submit` (including `update_sales_order_picking_status`,
	which is what actually updates the Sales Order's `per_picked`). What this
	function adds on top is a pre-submit guard, because Pick List's own
	`validate_picked_items()` silently sets `picked_qty = stock_qty` for any
	row left at exactly 0 (unless `scan_mode` is on) -- i.e. ERPNext's own
	submit would quietly report a fully-zero, reported shortage as "fully
	picked". So:

	- any row picked short of what was requested MUST already have a Reporte
	  de Faltante against it (created via report_shortage) -- otherwise this
	  throws and refuses to finish, so a shortfall can never reach submit
	  undocumented;
	- a row picked at exactly 0 -- even with a Reporte de Faltante -- still
	  blocks finishing, because there is no safe way to submit it without
	  ERPNext's own logic overwriting that 0 into "fully picked". Rows picked
	  partially (>0 but < requested) are safe and go through unchanged, since
	  that native auto-fill only triggers on an untouched 0.

	This means: if the pick is complete, or every shortfall is a disclosed
	partial pick, per_picked ends up correct via ERPNext's native mechanism.
	A pick list can never look "completely alistado" via this API when it
	physically isn't.
	"""
	_require_login()

	pl = frappe.get_doc("Pick List", name)
	pl.check_permission("write")

	if pl.docstatus != 0:
		frappe.throw(_("Este Pick List ya fue finalizado o cancelado."))

	if not pl.fg_started_by:
		frappe.throw(_("No se puede finalizar un alistamiento que no ha sido iniciado."))

	if not pl.get("locations"):
		frappe.throw(_("El Pick List no tiene líneas para alistar."))

	row_names = [row.name for row in pl.get("locations")]
	reported_rows = _get_shortage_report_rows(row_names)

	undisclosed_rows = []
	zero_qty_rows = []
	for row in pl.get("locations"):
		shortfall = flt(row.stock_qty) - flt(row.picked_qty)
		if shortfall <= 0:
			continue
		if row.name not in reported_rows:
			undisclosed_rows.append(row.idx)
		elif flt(row.picked_qty) == 0:
			zero_qty_rows.append(row.idx)

	if undisclosed_rows:
		frappe.throw(
			_(
				"No se puede finalizar: la(s) fila(s) {0} tienen menos cantidad alistada de la "
				"solicitada y no tienen un Reporte de Faltante. Alista la cantidad disponible o "
				"reporta el faltante antes de terminar."
			).format(", ".join(str(idx) for idx in undisclosed_rows))
		)

	if zero_qty_rows:
		frappe.throw(
			_(
				"La(s) fila(s) {0} quedaron en 0 unidades alistadas. Un Pick List no puede "
				"finalizarse así: al enviarlo, ERPNext asumiría automáticamente que se alistó "
				"toda la cantidad solicitada, lo cual falsearía el registro. Ajusta el Pick List "
				"o espera a resolver el faltante antes de finalizar esa fila."
			).format(", ".join(str(idx) for idx in zero_qty_rows))
		)

	try:
		pl.submit()
	except frappe.exceptions.TimestampMismatchError:
		frappe.throw(
			_("Otro usuario modificó este Pick List al mismo tiempo. Actualiza y vuelve a intentar."),
			exc=frappe.exceptions.TimestampMismatchError,
		)

	sales_orders = {row.sales_order for row in pl.get("locations") if row.sales_order}
	per_picked_by_so = {
		so: frappe.db.get_value("Sales Order", so, "per_picked") for so in sales_orders
	}

	return {
		"name": pl.name,
		"docstatus": pl.docstatus,
		"status": pl.status,
		"per_picked_by_sales_order": per_picked_by_so,
	}
