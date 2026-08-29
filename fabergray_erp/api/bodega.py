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

from fabergray_erp.sales_order_naming import root_commercial_name

OPEN_SHORTAGE_STATUSES = ["Abierto", "En Proceso"]
ALL_SHORTAGE_STATUSES = ["Abierto", "En Proceso", "Resuelto"]


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


def _insert_shortage_report(
	item_code,
	warehouse,
	qty_solicitada,
	qty_disponible,
	detected_by,
	sales_order=None,
	sales_order_item=None,
	material_request=None,
	pick_list=None,
	pick_list_item=None,
	shortage_reason=None,
	resolution_note=None,
	via_fulfillment_engine=False,
):
	"""The single place that inserts a Reporte de Faltante -- the Fulfillment
	Engine extension point (Commit 9). Full contract documented in
	FULFILLMENT_ENGINE_CONTRACT.md at the app root; read that before wiring up
	a new caller.

	Takes already-resolved values, not a Pick List row -- it has no idea
	where they came from, and never will. That is deliberate: this is the
	*only* function in the app that may call
	`frappe.get_doc({"doctype": "Reporte de Faltante", ...}).insert()`. Every
	caller (today: _create_shortage_report() deriving from a Pick List row;
	fabergray_erp.fulfillment.shortage_service deriving from a Sales Order
	Item, Commit 14) is a thin adapter that resolves its own fields and hands
	them here -- never a second, parallel insert path. `pick_list`/
	`pick_list_item` are optional (the doctype itself never required them,
	see Commit 2/7) so a report with detected_by="Fulfillment Engine" and no
	Pick List at all is already valid today, with zero doctype or permission
	changes. `sales_order_item` (Commit 14) is the exact Sales Order Item row
	reference -- optional here too, since a Bodega-derived report may not
	always have a clean one-to-one Sales Order Item (e.g. product bundles).

	shortage_reason's "required when Bodega" rule is enforced by the
	doctype's own validate() (Commit 2), not duplicated here. Creates exactly
	one document; never touches Stock Ledger, Bin, Sales Order, Material
	Request, Purchase Order or Work Order -- no stock reservation, no
	automation, ever.

	`via_fulfillment_engine` (Commit 18.1) -- the ONE, narrow, documented
	exception to "always check real permissions": this is the only shared
	frontier between Bodega's interactive report_shortage() (via
	_create_shortage_report(), which never passes this) and the Fulfillment
	Engine's own automated sync_shortage_reports_for_sales_order() (which
	always passes True). When True, the explicit create-permission check
	below is skipped and the insert itself runs with
	ignore_permissions=True -- exactly the native ERPNext pattern used for
	automated side effects of an already-authorized action (e.g.
	erpnext/accounts/general_ledger.py's `gle.flags.ignore_permissions = 1`
	when a Sales User submits a Sales Invoice they have no GL Entry
	permission for). This function is never `@frappe.whitelist()`-ed and
	this parameter is never read from a client request -- only
	shortage_service.py's own internal call site ever passes True, and only
	after analyze_sales_order()/native Sales Order permission checks have
	already authorized the underlying Sales Order action that triggered it.
	frappe.session.user is never touched -- the resulting document's
	`owner` is still whoever's session is actually running (e.g. the
	Vendedora who submitted the Sales Order), not a substituted identity.
	"""
	if not via_fulfillment_engine:
		frappe.has_permission("Reporte de Faltante", "create", throw=True)

	report = frappe.get_doc(
		{
			"doctype": "Reporte de Faltante",
			"item_code": item_code,
			"warehouse": warehouse,
			"sales_order": sales_order,
			"sales_order_item": sales_order_item,
			"material_request": material_request,
			"pick_list": pick_list,
			"pick_list_item": pick_list_item,
			"qty_solicitada": qty_solicitada,
			"qty_disponible": qty_disponible,
			"detected_by": detected_by,
			"shortage_reason": shortage_reason,
			"resolution_note": resolution_note,
		}
	)
	report.insert(ignore_permissions=via_fulfillment_engine)
	return report.name


def _create_shortage_report(pick_list_doc, row, qty_disponible, shortage_reason=None,
							detected_by="Bodega", resolution_note=None):
	"""Bodega/Pick List adapter over _insert_shortage_report() -- the only
	role of this function is deriving fields from an already-validated Pick
	List row and its parent document; it does not insert anything itself.

	Used today by report_shortage() (detected_by="Bodega", physical
	discrepancy found while picking). Item/Warehouse/Sales Order/Material
	Request are always derived from the validated Pick List row, never
	accepted as free-form input -- this function's entire job is that
	derivation, nothing else. `sales_order_item` is read off the row the
	same way -- Pick List Item.sales_order_item is already a native field
	(set by create_pick_list()'s own mapper), so this costs nothing new and
	gives Bodega-created reports the same exact-line traceability Commit 14
	added for the Fulfillment Engine, without changing anything about when
	or how these reports get created.
	"""
	return _insert_shortage_report(
		item_code=row.item_code,
		warehouse=row.warehouse,
		qty_solicitada=row.stock_qty,
		qty_disponible=qty_disponible,
		detected_by=detected_by,
		sales_order=row.sales_order or None,
		sales_order_item=row.sales_order_item or None,
		material_request=row.material_request or pick_list_doc.material_request or None,
		pick_list=pick_list_doc.name,
		pick_list_item=row.name,
		shortage_reason=shortage_reason,
		resolution_note=resolution_note,
	)


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
	shortage_pick_lists = _open_shortage_pick_lists(names)

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

	# One root_commercial_name() lookup per distinct Sales Order, not per Pick
	# List -- several Pick Lists (or none) can share the same Sales Order.
	commercial_name_cache = {}

	def _commercial_name(sales_order):
		if not sales_order:
			return None
		if sales_order not in commercial_name_cache:
			commercial_name_cache[sales_order] = root_commercial_name(sales_order)
		return commercial_name_cache[sales_order]

	buckets = {"listos": [], "con_faltantes": [], "en_alistamiento": [], "pendientes": []}
	for pl in pick_lists:
		sales_order = sales_order_by_pick_list.get(pl.name)
		entry = {
			"name": pl.name,
			"status": pl.status,
			"purpose": pl.purpose,
			"parent_warehouse": pl.parent_warehouse,
			"customer": pl.customer,
			"fg_started_by": pl.fg_started_by,
			"fg_started_on": pl.fg_started_on,
			"item_count": line_counts.get(pl.name, 0),
			"sales_order": sales_order,
			"commercial_name": _commercial_name(sales_order),
		}
		buckets[_pick_list_bucket(pl, shortage_pick_lists)].append(entry)

	return buckets


def _pick_list_bucket(pl, shortage_pick_lists):
	"""The one place Pick List operational state is decided, for both this
	function's own live queue and api.jefe_bodega's Pick List history view
	(Commit 22.9 -- imported from there, never re-derived): docstatus==1
	(submitted) -> "listos"; else an OPEN Reporte de Faltante linked (per
	`shortage_pick_lists`, e.g. from _open_shortage_pick_lists() below) ->
	"con_faltantes"; else fg_started_by set -> "en_alistamiento"; else ->
	"pendientes". `pl` needs only .docstatus/.name/.fg_started_by."""
	if pl.docstatus == 1:
		return "listos"
	if pl.name in shortage_pick_lists:
		return "con_faltantes"
	if pl.fg_started_by:
		return "en_alistamiento"
	return "pendientes"


def _open_shortage_pick_lists(names):
	"""Pick Lists (from `names`) that currently have an OPEN Reporte de
	Faltante linked -- the exact relation get_queue() above already uses
	inline; factored out here so api.jefe_bodega's Pick List history view
	(Commit 22.9) can compute the same _pick_list_bucket() input without
	re-deriving the query. One batched IN query, never one per Pick List."""
	if not names:
		return set()
	return set(
		frappe.get_list(
			"Reporte de Faltante",
			filters={"pick_list": ["in", names], "status": ["in", OPEN_SHORTAGE_STATUSES]},
			pluck="pick_list",
		)
	)


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
				# PENDING (tracked separately as "Bodega UOM normalization"):
				# qty_solicitada/qty_alistada/qty_disponible below are all in
				# Stock UOM (native Pick List Item convention), but "uom"
				# here is the transactional UOM, not stock_uom -- they only
				# look consistent while conversion_factor == 1. Deliberately
				# not touched by the qty-stepper coalescing bugfix.
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
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
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


@frappe.whitelist()
def get_shortages(status=None):
	"""Reporte de Faltante records visible to the current Bodega user, for
	the "Faltantes" tab.

	Same read pattern as api.jefe_bodega.get_open_shortage_reports() (that
	function stays untouched -- this is Bodega's own version, not a call
	into the Jefe de Bodega module, which would be the wrong direction:
	jefe_bodega.py already imports from api.bodega, never the reverse).
	`frappe.get_list` (not `get_all`) so Role Permissions AND User
	Permissions apply automatically -- Reporte de Faltante has its own
	`warehouse` field, so the same per-Warehouse User Permission that
	already scopes Pick List/get_queue() scopes this list too, with no
	extra filtering written here.

	item_name is resolved by reading the linked Pick List (a child-table
	lookup, exempt from its own permission model, after check_permission
	("read") on that specific Pick List) -- never by querying the Item
	doctype directly for a bare item_code, exactly like jefe_bodega.py's
	version does it. Bodega's own read permission on Item (granted
	alongside get_inventory() below) is unused here on purpose: resolving
	item_name via the already-authorized Pick List keeps this endpoint
	working the same way even for a report whose Pick List still exists
	but whose item happens to sit outside whatever Item-level visibility
	rules a future change might add.
	"""
	_require_login()
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	filters = {}
	if status:
		if status not in ALL_SHORTAGE_STATUSES:
			frappe.throw(_("Estado de faltante inválido: {0}").format(status))
		filters["status"] = status

	reports = frappe.get_list(
		"Reporte de Faltante",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"warehouse",
			"sales_order",
			"pick_list",
			"pick_list_item",
			"qty_solicitada",
			"qty_disponible",
			"qty_faltante",
			"shortage_reason",
			"status",
			"reported_by",
			"reported_on",
		],
		order_by="reported_on desc",
		limit_page_length=0,
	)

	pick_list_cache = {}

	def _pick_list(pick_list_name):
		if pick_list_name not in pick_list_cache:
			doc = frappe.get_doc("Pick List", pick_list_name)
			doc.check_permission("read")
			pick_list_cache[pick_list_name] = doc
		return pick_list_cache[pick_list_name]

	commercial_name_cache = {}

	def _commercial_name(sales_order):
		if not sales_order:
			return None
		if sales_order not in commercial_name_cache:
			commercial_name_cache[sales_order] = root_commercial_name(sales_order)
		return commercial_name_cache[sales_order]

	for report in reports:
		item_name = None
		if report.pick_list and report.pick_list_item:
			try:
				pl_doc = _pick_list(report.pick_list)
				rows = pl_doc.get("locations", {"name": report.pick_list_item})
				if rows:
					item_name = rows[0].item_name
			except frappe.PermissionError:
				# Report references a Pick List this user can no longer read
				# (e.g. outside their Warehouse User Permission any more).
				# Fall back to the report's own item_code rather than
				# failing the whole list or reading Item data instead.
				item_name = None

		report["item_name"] = item_name or report.item_code
		report["reported_by_fullname"] = frappe.utils.get_fullname(report.reported_by)
		report["commercial_name"] = _commercial_name(report.sales_order)

	return reports


@frappe.whitelist()
def get_inventory():
	"""Read-only stock snapshot for the "Inventario" tab (Más section).

	Bin already had read=1 for the Bodega role before this commit (see
	get_pick_list()'s own use of get_actual_qty(), which reads Bin
	underneath). Item did not -- reading item_name/stock_uom for display is
	the one new permission this feature needed (Custom DocPerm, read=1
	only, same shape already granted to Vendedora -- no write/create/
	delete). `frappe.get_list` on both, never `get_all`, never
	`ignore_permissions` -- Role Permissions AND the existing per-Warehouse
	User Permission (already applied to Bin/Pick List, unchanged here)
	scope the Bin rows automatically. Read-only end to end: no Stock
	Entry, no Bin write, no adjustment path exists anywhere in this
	function or is reachable from it.
	"""
	_require_login()
	frappe.has_permission("Bin", "read", throw=True)
	frappe.has_permission("Item", "read", throw=True)

	bins = frappe.get_list(
		"Bin",
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty"],
		order_by="item_code asc",
		limit_page_length=0,
	)
	if not bins:
		return []

	item_codes = list({b.item_code for b in bins})
	items = frappe.get_list(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=["name", "item_name", "stock_uom"],
		limit_page_length=0,
	)
	item_by_code = {i.name: i for i in items}

	result = []
	for b in bins:
		item = item_by_code.get(b.item_code)
		if not item:
			# Bin row for an item Bodega cannot (or can no longer) read via
			# Item -- skip rather than show a code with no name.
			continue
		result.append(
			{
				"item_code": b.item_code,
				"item_name": item.item_name,
				"uom": item.stock_uom,
				"warehouse": b.warehouse,
				"actual_qty": flt(b.actual_qty),
				"reserved_qty": flt(b.reserved_qty),
				"available_qty": flt(b.actual_qty) - flt(b.reserved_qty),
			}
		)
	return result
