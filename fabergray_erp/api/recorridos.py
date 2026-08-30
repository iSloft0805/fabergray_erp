# -*- coding: utf-8 -*-
"""api/recorridos.py -- Commit 24.1: base data model + backend for the new
Recorridos (delivery-route) module. NO maps/Google Maps/Waze/Mapbox,
geocoding, route optimization, GPS/tracking, signature, photo, delivery
proof, novedades or driver UI in this commit -- those are 24.2+. This
commit is model + backend + permissions + tests only.

Audit performed before writing any code here:

1. No existing "Recorrido"/"Ruta"/"Delivery Route"/"Delivery Stop" concept
   anywhere in this app (grepped the whole tree, nothing found).

2. ERPNext's native "Delivery Trip" (erpnext.stock.doctype.delivery_trip,
   is_submittable=1) was audited and explicitly NOT reused: its own child
   table "Delivery Stop" carries a `grand_total` Currency field and a
   `delivery_note` Link -- Recorridos is deliberately non-economic
   ("SIN valores económicos", brief section 7) and must never create or
   reference a Delivery Note (section 18/19 guardrails). Delivery Trip's
   own controller (`update_delivery_notes()`, called from validate()/
   on_update()/on_submit()/on_cancel()) actively writes onto real Delivery
   Note documents -- fighting that behavior to keep it out of our flow
   would be more code than building a small, purpose-built doctype, and
   would leave a permanent trap for a future contributor who touches
   Delivery Trip for an unrelated reason. Confirmed via direct read of
   erpnext/stock/doctype/delivery_trip/delivery_trip.py.

3. ERPNext's native "Driver" and "Vehicle" (erpnext.setup.doctype.*) WERE
   reused directly as Link targets for Recorrido.driver/.vehicle: both are
   plain, non-submittable master-data doctypes with no accounting or
   Delivery Note entanglement (confirmed via their own JSON + controllers).
   Their native permissions (Fleet Manager/Delivery User/Delivery
   Manager/System Manager) don't include any role this app uses, so a
   minimal read-only Custom DocPerm grant for the new "Recorrido" role was
   added on both (fixtures/custom_docperm.json) -- the same
   minimal-grant-on-a-native-doctype pattern Facturación/Vendedora already
   established for Item/Customer/Account/Address.

4. Address: reused api.clientes._primary_address_name() (Customer.
   customer_primary_address, falling back to ERPNext's own
   get_default_address()) and frappe.contacts.doctype.address.address.
   get_address_display() for the snapshot text -- exactly the same
   resolution Gestión de Clientes already uses, not reimplemented.

5. Architecture decision -- "Recorrido Parada" is a STANDALONE DocType,
   NOT a Table (child) field on Recorrido, despite the brief's own
   "stops - Table -> Recorrido Parada" suggestion. Reasoning, checked
   empirically rather than assumed:

   - Child tables have no independent permission model in this Frappe
     version -- proven directly in Commit 23.0, where a bare
     frappe.get_list("Pick List Item", ...) raised PermissionError for
     every role (zero DocPerm/Custom DocPerm rows exist for any child
     doctype) until parent_doctype="Pick List" was passed so the check
     routed through the PARENT's own permission instead. A Link field on
     some future "Entrega Evidencia"/"Entrega Novedad"/"Posición
     Recorrido" doctype pointing at a Recorrido Parada CHILD row would hit
     exactly the same wall, except worse: those future doctypes have no
     natural "parent" of their own to route the permission check through.
   - Section 21 of the brief explicitly needs Recorrido Parada to have a
     stable, independently addressable identity so future commits (24.7
     evidence/photos, 24.8 novedades, 24.9 GPS positions) can each Link
     back to ONE specific stop -- all three are one-to-(potentially-)many
     relationships FROM a single parada, which a child row (itself
     already "inside" a parent's own child table) cannot cleanly host a
     child-of-a-child for in Frappe.
   - As a standalone doctype, Recorrido Parada gets: a real `name`,
     genuine DocPerm rows (see its own JSON), and ordinary frappe.get_list/
     get_doc/check_permission semantics everywhere -- no workaround
     needed, ever, for it or anything that will reference it later.
   - Cost of this choice: reordering/adding/removing stops is now plain
     CRUD against a normal doctype (see update_route_stops() below)
     instead of child-table row manipulation -- if anything, simpler, not
     more complex, and existing stop rows keep their own stable identity
     across a reorder (an actual traceability win, not just a permission
     workaround).

Same permission policy as every other interactive module in this app: only
frappe.get_list()/frappe.get_doc()+check_permission()/frappe.has_permission()
-- nothing here uses frappe.get_all(), ignore_permissions or
frappe.set_user(). No `frappe.db.commit()` anywhere in this module --
Frappe's own request-lifecycle commit/rollback provides atomicity, and
double-assignment protection (see _lock_pick_lists()) relies on that same
lifecycle holding row locks for the duration of one request.

Nothing in this module ever creates or modifies: Sales Invoice, Purchase
Invoice, Payment Entry, GL Entry, Journal Entry, Delivery Note, Stock
Entry, Bin, Stock Ledger Entry, Pick List Item.qty/picked_qty/
delivered_qty, or Sales Order.qty/delivered_qty/per_delivered/per_billed
-- see test_recorridos_api.py's own guardrail tests for the executable
version of this claim.
"""

import functools

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_address_display
from frappe.utils import cint, flt, nowdate

from erpnext import get_default_company

from fabergray_erp.api.bodega import _require_login
from fabergray_erp.api.clientes import _primary_address_name
from fabergray_erp.api.facturacion import FG_INVOICING_FACTURADO, _sales_order_of
from fabergray_erp.sales_order_naming import root_commercial_name

#: A Pick List assigned to a Recorrido in any of these statuses blocks it
#: from being assigned to a second one -- Borrador/Planificado/En Ruta are
#: all "still going to happen" states. Completado/Cancelado release it.
#: Extensibility note (brief section 6): once delivery is implemented
#: (24.6+), a Completado route whose stop actually reached
#: status="Entregado" must NOT make the Pick List available again --
#: that is a SEPARATE, not-yet-needed rule this module deliberately keeps
#: out of ACTIVE_ROUTE_STATUSES (which only ever governs "still in
#: progress", not "was this ever delivered"). When 24.6+ needs it, add a
#: second, explicitly named helper (e.g. _pick_lists_already_delivered())
#: rather than overloading this one.
ACTIVE_ROUTE_STATUSES = ("Borrador", "Planificado", "En Ruta")


class PickListNotEligibleError(frappe.ValidationError):
	pass


class PickListAlreadyAssignedError(frappe.ValidationError):
	pass


class RouteNotEditableError(frappe.ValidationError):
	pass


class RouteValidationError(frappe.ValidationError):
	pass


def _eligible_pick_list_filters(company):
	"""Single source of truth for "is this Pick List eligible for
	Recorridos at all" at the DB-filter level -- docstatus/status/company.
	Does NOT check double-assignment (that needs the locked, request-time
	_pick_lists_in_active_routes() set, not a plain filter) -- reused by
	get_available_orders() and get_available_order_detail()."""
	return [
		["docstatus", "=", 1],
		["fg_invoicing_status", "=", FG_INVOICING_FACTURADO],
		["company", "=", company],
	]


def _pick_lists_in_active_routes(exclude_route=None):
	"""Pick List names currently claimed by a Borrador/Planificado/En Ruta
	Recorrido -- the one query every eligibility check in this module goes
	through, never duplicated ad hoc. `exclude_route`: a route may keep its
	own already-assigned Pick Lists when re-validating itself (planning,
	editing its own stops)."""
	filters = {"status": ["in", ACTIVE_ROUTE_STATUSES]}
	if exclude_route:
		filters["name"] = ["!=", exclude_route]
	active_routes = frappe.get_list("Recorrido", filters=filters, pluck="name")
	if not active_routes:
		return set()
	return set(
		frappe.get_list(
			"Recorrido Parada",
			filters={"recorrido": ["in", active_routes]},
			pluck="pick_list",
		)
	)


def _lock_pick_lists(names):
	"""The double-assignment race guard (brief section 9), part 1 of 2:
	acquires a real row-level lock on every Pick List in `names`, held for
	the rest of this request (Frappe's own request-lifecycle commit/
	rollback releases it -- nothing here calls frappe.db.commit()). A
	second concurrent request trying to lock the SAME Pick List blocks
	until the first one finishes. frappe.db.get_value(..., for_update=True)
	is the native Frappe API for this -- no raw SQL. Locks are acquired in
	SORTED name order (not request order) so two concurrent multi-Pick-
	List requests can never deadlock each other by locking the same two
	rows in opposite order. Also validates existence as a side effect (a
	missing Pick List returns None here).

	Blocking here is necessary but NOT sufficient by itself -- see
	_locked_assigned_pick_lists() below for part 2, the actual reason a
	plain frappe.get_list() re-check after this lock would still be
	wrong."""
	for name in sorted(set(names)):
		if not frappe.db.get_value("Pick List", name, "name", for_update=True):
			frappe.throw(_("El Pick List {0} no existe.").format(name), frappe.DoesNotExistError)


def _locked_assigned_pick_lists(pick_list_names, exclude_route=None):
	"""The double-assignment race guard, part 2 of 2 -- and the one that
	actually closes the race. MUST be called AFTER _lock_pick_lists() has
	already blocked-and-woken for the same `pick_list_names`.

	Confirmed by a real two-thread reproduction during this commit's own
	testing that a plain frappe.get_list() re-check here is NOT enough,
	even after _lock_pick_lists() correctly blocks a concurrent request:
	by the time this function runs, the current request has already
	issued earlier, ordinary (non-locking) reads elsewhere (loading the
	calling user, permission checks, frappe.get_doc() on the target
	Recorrido, ...) -- under MariaDB's default REPEATABLE READ isolation,
	the FIRST such plain read in a transaction fixes that transaction's
	own consistent-read snapshot for every later plain SELECT, no matter
	how long that transaction then blocks waiting on a row lock. So a
	plain frappe.get_list("Recorrido Parada", ...) issued right after
	_lock_pick_lists() unblocks can still return the PRE-lock, stale
	state, silently missing a Recorrido Parada another request committed
	while this one was waiting -- exactly the double-assignment bug this
	whole mechanism exists to prevent.

	`SELECT ... FOR UPDATE` reads always return the latest committed row
	regardless of the transaction's snapshot age, which is what actually
	fixes this. frappe.get_list()/DatabaseQuery has no for_update
	parameter (confirmed: not present in frappe.model.qb_query), so this
	is the raw-SQL exception brief section 9 itself anticipates ("no
	escribir SQL directo... salvo locks ... justificadas") -- a single,
	narrow, parameterized query, not a general-purpose helper."""
	if not pick_list_names:
		return set()
	names = list(set(pick_list_names))
	name_placeholders = ", ".join(["%s"] * len(names))
	status_placeholders = ", ".join(["%s"] * len(ACTIVE_ROUTE_STATUSES))
	params = [*names, *ACTIVE_ROUTE_STATUSES]
	exclude_clause = ""
	if exclude_route:
		exclude_clause = "AND r.name != %s"
		params.append(exclude_route)
	rows = frappe.db.sql(
		f"""
		SELECT rp.pick_list AS pick_list
		FROM `tabRecorrido Parada` rp
		INNER JOIN `tabRecorrido` r ON r.name = rp.recorrido
		WHERE rp.pick_list IN ({name_placeholders})
		  AND r.status IN ({status_placeholders})
		  {exclude_clause}
		FOR UPDATE
		""",
		tuple(params),
		as_dict=True,
	)
	return {r.pick_list for r in rows}


def _lock_and_get_assigned(pick_list_names, exclude_route=None):
	"""_lock_pick_lists() + _locked_assigned_pick_lists(), the one call
	every write-path caller (create_route/update_route_stops/plan_route)
	makes. See @_retrying_on_deadlock below for why a transient deadlock
	around this whole area is handled at the WHOLE-endpoint level, not
	locally here -- a deadlock confirmed via real testing to also strike
	later, during the actual Recorrido Parada .insert(), well after this
	function has already returned."""
	_lock_pick_lists(pick_list_names)
	return _locked_assigned_pick_lists(pick_list_names, exclude_route=exclude_route)


def _retrying_on_deadlock(fn):
	"""Wraps a whole write endpoint so a transient InnoDB deadlock causes a
	clean, transparent retry of the WHOLE function body -- never a raw
	deadlock error reaching the end user.

	Why this is needed, confirmed by a real two-thread reproduction during
	this commit's own testing (not theoretical): _lock_pick_lists()'s row
	lock on the Pick List genuinely serializes two concurrent requests
	targeting the same one -- the second blocks until the first commits,
	exactly as intended. But InnoDB's own gap-locking (needed to prevent
	phantom inserts under REPEATABLE READ) means the two transactions can
	still end up in a lock-wait cycle that MariaDB's deadlock detector
	resolves by killing one of them -- surfacing as a real
	frappe.QueryDeadlockError at the later Recorrido Parada .insert(),
	past the point _lock_and_get_assigned() itself already returned
	cleanly. This is expected, standard InnoDB behavior under write
	contention on a shared index range, not a flaw in the locking logic.

	frappe.QueryDeadlockError is exactly what frappe.db.sql() itself
	raises once its own internal is_deadlocked(e) check (the same one
	frappe/app.py's own top-level error handler uses) already confirmed
	the raw driver error was a real deadlock -- catching that specific
	exception class here, not a second is_deadlocked() call of our own
	against the (by then already-wrapped) QueryDeadlockError, whose own
	.args[0] is the original exception object, not a raw MySQL error
	code -- confirmed empirically: an earlier version of this decorator
	called is_deadlocked() again here and it silently never matched,
	letting every deadlock re-raise unretried.

	frappe.db.rollback() discards the whole half-finished transaction so
	the retry starts from a clean slate, re-running every validation from
	scratch. By the retry, the other transaction has always already
	committed, so this one either succeeds cleanly or correctly raises
	PickListAlreadyAssignedError this time -- never a raw deadlock."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		last_error = None
		for _attempt in range(3):
			try:
				return fn(*args, **kwargs)
			except frappe.QueryDeadlockError as e:
				last_error = e
				frappe.db.rollback()
		raise last_error

	return wrapper


def _resolve_pick_list_snapshot(pl):
	"""Everything a Recorrido Parada needs, resolved from the real,
	already-permission-checked Pick List doc `pl` -- never from client
	input. No rate/amount/grand_total/account anywhere: Recorridos is
	logistics, not accounting."""
	sales_order = _sales_order_of(pl)

	item_count = 0
	total_qty = 0.0
	for row in frappe.get_list(
		"Pick List Item",
		filters={"parent": pl.name},
		fields=["picked_qty"],
		parent_doctype="Pick List",
	):
		item_count += 1
		total_qty += flt(row.picked_qty)

	customer_address = None
	address_display = None
	if pl.customer:
		customer_doc = frappe.get_doc("Customer", pl.customer)
		customer_doc.check_permission("read")
		address_name = _primary_address_name(customer_doc)
		if address_name:
			customer_address = address_name
			address_display = get_address_display(address_name)

	return {
		"sales_order": sales_order,
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
		"customer": pl.customer,
		"customer_name": pl.customer_name,
		"customer_address": customer_address,
		"address_display": address_display,
		"item_count": item_count,
		"total_qty": total_qty,
	}


def _validate_pick_list_eligible(pl, company, already_assigned):
	"""Raises a clear, functional (Spanish) error if the already-loaded
	Pick List `pl` cannot be attached to a Recorrido for `company` right
	now. `already_assigned`: the already-computed set from either
	_pick_lists_in_active_routes() (plain reads, listing-only callers) or
	_locked_assigned_pick_lists() (locking reads, write-path callers) --
	never recomputed per Pick List, so a multi-stop request only ever pays
	for this query once."""
	if pl.docstatus != 1:
		frappe.throw(_("El Pick List {0} no está sometido.").format(pl.name), PickListNotEligibleError)
	if pl.fg_invoicing_status != FG_INVOICING_FACTURADO:
		frappe.throw(_("El Pick List {0} todavía no está facturado.").format(pl.name), PickListNotEligibleError)
	if pl.company != company:
		frappe.throw(_("El Pick List {0} pertenece a otra empresa.").format(pl.name), PickListNotEligibleError)
	if pl.name in already_assigned:
		frappe.throw(
			_("El pedido {0} ya está asignado a otro recorrido activo.").format(pl.name),
			PickListAlreadyAssignedError,
		)


def _parse_pick_lists(pick_lists):
	"""create_route()/update_route_stops() both accept `pick_lists` as
	either a real list (server-to-server call) or a JSON string (the
	shape frappe.call() sends from the browser for a list argument) --
	same normalization every other list-accepting endpoint in this app
	does. Rejects duplicates within the same request explicitly (brief's
	own "Pick List duplicado en la misma solicitud" requirement) rather
	than silently deduplicating."""
	if isinstance(pick_lists, str):
		pick_lists = frappe.parse_json(pick_lists)
	pick_lists = [p for p in (pick_lists or []) if p]
	if len(pick_lists) != len(set(pick_lists)):
		frappe.throw(_("Hay Pick List duplicados en la solicitud."), RouteValidationError)
	return pick_lists


def _commercial_name_cache():
	cache = {}

	def resolve(sales_order):
		if not sales_order:
			return None
		if sales_order not in cache:
			cache[sales_order] = root_commercial_name(sales_order)
		return cache[sales_order]

	return resolve


# ---------------------------------------------------------------------------
# Read: pedidos disponibles para recorrido
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_available_orders(txt=None, start=0, page_length=20):
	"""Paginated Pick Lists eligible for a NEW Recorrido: submitted,
	fg_invoicing_status=Facturado, this site's own default company, and
	not already claimed by an active (Borrador/Planificado/En Ruta)
	Recorrido. Company is ALWAYS resolved server-side via
	erpnext.get_default_company() -- never accepted from the client, the
	same convention api.jefe_bodega already established, so there is no
	parameter a caller could use to ask for another company's data at all.

	Deliberately does NOT resolve address here (see
	get_available_order_detail() for that): with up to 100 rows per page,
	resolving Customer.customer_primary_address + rendering
	get_address_display() for each would be a real per-row N+1 for data
	the future "pedidos disponibles" list (24.2) mainly needs for
	filtering/selection, not for a live address preview -- that happens
	once, lazily, for one specific Pick List, via the detail endpoint
	below (and again, permanently, as this Pick List's own snapshot the
	moment it actually becomes a parada)."""
	_require_login()
	frappe.has_permission("Recorrido", "read", throw=True)
	frappe.has_permission("Pick List", "read", throw=True)

	company = get_default_company()
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length) or 20, 1), 100)
	txt = (txt or "").strip()

	assigned = _pick_lists_in_active_routes()

	filters = _eligible_pick_list_filters(company)
	if assigned:
		filters = filters + [["name", "not in", list(assigned)]]

	or_filters = None
	if txt:
		or_filters = [
			["name", "like", f"%{txt}%"],
			["customer_name", "like", f"%{txt}%"],
			["customer", "like", f"%{txt}%"],
		]

	page_rows = frappe.get_list(
		"Pick List",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "customer", "customer_name", "modified"],
		order_by="modified desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	total = len(frappe.get_list("Pick List", filters=filters, or_filters=or_filters, pluck="name"))

	names = [r.name for r in page_rows]
	item_counts, total_qtys, sales_order_by_pl = {}, {}, {}
	if names:
		for row in frappe.get_list(
			"Pick List Item",
			filters={"parent": ["in", names]},
			fields=["parent", "sales_order", "picked_qty"],
			parent_doctype="Pick List",
		):
			item_counts[row.parent] = item_counts.get(row.parent, 0) + 1
			total_qtys[row.parent] = total_qtys.get(row.parent, 0.0) + flt(row.picked_qty)
			if row.sales_order and row.parent not in sales_order_by_pl:
				sales_order_by_pl[row.parent] = row.sales_order

	resolve_commercial_name = _commercial_name_cache()

	results = []
	for pl in page_rows:
		sales_order = sales_order_by_pl.get(pl.name)
		results.append(
			{
				"pick_list": pl.name,
				"sales_order": sales_order,
				"commercial_name": resolve_commercial_name(sales_order),
				"customer": pl.customer,
				"customer_name": pl.customer_name,
				"item_count": item_counts.get(pl.name, 0),
				"total_qty": total_qtys.get(pl.name, 0.0),
			}
		)

	return {"pick_lists": results, "total": total}


@frappe.whitelist()
def get_available_order_detail(pick_list):
	"""Single-Pick-List detail, brief section 7: pick_list/sales_order/
	customer/customer_name/address/item_count/total_qty -- no economic
	value anywhere. Re-validates eligibility (does NOT assume the caller
	only ever requests a Pick List that was in get_available_orders()'s
	own last response) so a client cannot use this to peek at a Pick List
	that is draft/not-Facturado/another company's."""
	_require_login()
	frappe.has_permission("Recorrido", "read", throw=True)
	frappe.has_permission("Pick List", "read", throw=True)

	pl = frappe.get_doc("Pick List", pick_list)
	pl.check_permission("read")

	company = get_default_company()
	assigned = _pick_lists_in_active_routes()
	_validate_pick_list_eligible(pl, company, assigned)

	snapshot = _resolve_pick_list_snapshot(pl)
	return {"pick_list": pl.name, **snapshot}


# ---------------------------------------------------------------------------
# Read: detalle de un Recorrido ya creado
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_route_detail(route_name):
	"""Route header + its stops, ordered by sequence. total_stops is
	computed here from the very query this function already has to run to
	return the stops themselves -- not persisted on Recorrido (see this
	module's own top docstring: no field earns a persisted counter until a
	real hot path needs one; every current caller already pays for this
	query anyway)."""
	_require_login()
	frappe.has_permission("Recorrido", "read", throw=True)

	route = frappe.get_doc("Recorrido", route_name)
	route.check_permission("read")

	stops = frappe.get_list(
		"Recorrido Parada",
		filters={"recorrido": route.name},
		fields=[
			"name",
			"sequence",
			"pick_list",
			"sales_order",
			"customer",
			"customer_name",
			"customer_address",
			"address_display",
			"item_count",
			"total_qty",
			"status",
		],
		order_by="sequence asc",
	)

	resolve_commercial_name = _commercial_name_cache()
	for s in stops:
		s["commercial_name"] = resolve_commercial_name(s.sales_order)

	return {
		"name": route.name,
		"company": route.company,
		"route_date": route.route_date,
		"status": route.status,
		"driver": route.driver,
		"vehicle": route.vehicle,
		"start_address": route.start_address,
		"notes": route.notes,
		"created_by_user": route.created_by_user,
		"total_stops": len(stops),
		"stops": stops,
	}


# ---------------------------------------------------------------------------
# Write: crear Recorrido
# ---------------------------------------------------------------------------


@frappe.whitelist()
@_retrying_on_deadlock
def create_route(route_date=None, pick_lists=None, driver=None, vehicle=None, start_address=None, notes=None):
	"""The one route-creation endpoint. Validates permissions, every Pick
	List (existence, docstatus, fg_invoicing_status, company, double-
	assignment -- all under real row locks, see _lock_pick_lists()),
	resolves every stop's data server-side, then creates the Recorrido and
	its Recorrido Parada rows in sequence 1..N matching the order
	`pick_lists` was given in. Never submits anything (Recorrido isn't
	submittable) and starts life as status="Borrador" always."""
	_require_login()
	frappe.has_permission("Recorrido", "create", throw=True)
	frappe.has_permission("Pick List", "read", throw=True)

	pick_lists = _parse_pick_lists(pick_lists)
	if not pick_lists:
		frappe.throw(_("Debes incluir al menos un Pick List."), RouteValidationError)

	company = get_default_company()

	assigned = _lock_and_get_assigned(pick_lists)

	snapshots = {}
	for name in pick_lists:
		pl = frappe.get_doc("Pick List", name)
		pl.check_permission("read")
		_validate_pick_list_eligible(pl, company, assigned)
		snapshots[name] = _resolve_pick_list_snapshot(pl)

	route = frappe.get_doc(
		{
			"doctype": "Recorrido",
			"company": company,
			"route_date": route_date or nowdate(),
			"status": "Borrador",
			"driver": driver or None,
			"vehicle": vehicle or None,
			"start_address": start_address or None,
			"notes": notes or None,
		}
	)
	route.insert()

	for idx, name in enumerate(pick_lists, start=1):
		snap = snapshots[name]
		frappe.get_doc(
			{
				"doctype": "Recorrido Parada",
				"recorrido": route.name,
				"sequence": idx,
				"pick_list": name,
				"sales_order": snap["sales_order"],
				"customer": snap["customer"],
				"customer_name": snap["customer_name"],
				"customer_address": snap["customer_address"],
				"address_display": snap["address_display"],
				"item_count": snap["item_count"],
				"total_qty": snap["total_qty"],
				"status": "Pendiente",
			}
		).insert()

	return get_route_detail(route.name)


# ---------------------------------------------------------------------------
# Write: editar paradas (solo Borrador)
# ---------------------------------------------------------------------------


@frappe.whitelist()
@_retrying_on_deadlock
def update_route_stops(route_name, pick_lists=None):
	"""Full-replacement semantics: `pick_lists` is the COMPLETE desired
	list of Pick Lists, in the desired order -- covers add/remove/reorder
	in one call rather than three separate endpoints, matching the brief's
	own framing ("añadir ... quitar ... reordenar" as one editing
	capability). A stop whose Pick List stays in the new list KEEPS its
	existing Recorrido Parada row (same `name`, only `sequence` touched if
	its position changed) -- deleted and recreated only for Pick Lists
	genuinely leaving/entering the route, preserving the identity
	guarantee this module's own architecture note promises. Only allowed
	while status="Borrador"."""
	_require_login()
	frappe.has_permission("Recorrido", "write", throw=True)
	frappe.has_permission("Pick List", "read", throw=True)

	route = frappe.get_doc("Recorrido", route_name)
	route.check_permission("write")

	if route.status != "Borrador":
		frappe.throw(
			_("Solo se pueden editar las paradas mientras el recorrido está en Borrador."),
			RouteNotEditableError,
		)

	pick_lists = _parse_pick_lists(pick_lists)

	assigned = _lock_and_get_assigned(pick_lists, exclude_route=route.name)

	existing_stops = {
		s.pick_list: s
		for s in frappe.get_list(
			"Recorrido Parada",
			filters={"recorrido": route.name},
			fields=["name", "pick_list", "sequence"],
		)
	}

	snapshots = {}
	for name in pick_lists:
		pl = frappe.get_doc("Pick List", name)
		pl.check_permission("read")
		_validate_pick_list_eligible(pl, route.company, assigned)
		if name not in existing_stops:
			snapshots[name] = _resolve_pick_list_snapshot(pl)

	# Remove stops for Pick Lists no longer requested.
	for pl_name, stop in existing_stops.items():
		if pl_name not in pick_lists:
			stop_doc = frappe.get_doc("Recorrido Parada", stop.name)
			stop_doc.check_permission("delete")
			stop_doc.delete()

	# Re-sequence kept stops, insert new ones -- both via the real Document
	# API (never frappe.db.set_value) so permission/validate still apply.
	for idx, name in enumerate(pick_lists, start=1):
		if name in existing_stops:
			if existing_stops[name].sequence != idx:
				stop_doc = frappe.get_doc("Recorrido Parada", existing_stops[name].name)
				stop_doc.check_permission("write")
				stop_doc.sequence = idx
				stop_doc.save()
		else:
			snap = snapshots[name]
			frappe.get_doc(
				{
					"doctype": "Recorrido Parada",
					"recorrido": route.name,
					"sequence": idx,
					"pick_list": name,
					"sales_order": snap["sales_order"],
					"customer": snap["customer"],
					"customer_name": snap["customer_name"],
					"customer_address": snap["customer_address"],
					"address_display": snap["address_display"],
					"item_count": snap["item_count"],
					"total_qty": snap["total_qty"],
					"status": "Pendiente",
				}
			).insert()

	return get_route_detail(route.name)


# ---------------------------------------------------------------------------
# Write: transiciones de estado (Borrador -> Planificado -> Cancelado)
# ---------------------------------------------------------------------------


@frappe.whitelist()
@_retrying_on_deadlock
def plan_route(route_name):
	"""Borrador -> Planificado. Re-validates everything from scratch
	(sequences, every Pick List's own eligibility, double-assignment)
	rather than trusting whatever update_route_stops() last confirmed --
	time may have passed and another route may have claimed a Pick List
	since. No GPS, no real dispatch, no optimization: only a status
	change."""
	_require_login()
	frappe.has_permission("Recorrido", "write", throw=True)

	route = frappe.get_doc("Recorrido", route_name)
	route.check_permission("write")

	if route.status != "Borrador":
		frappe.throw(_("Solo se puede planificar un recorrido en Borrador."), RouteNotEditableError)

	stops = frappe.get_list(
		"Recorrido Parada",
		filters={"recorrido": route.name},
		fields=["name", "pick_list", "sequence"],
		order_by="sequence asc",
	)
	if not stops:
		frappe.throw(_("El recorrido no tiene paradas."), RouteValidationError)

	sequences = [cint(s.sequence) for s in stops]
	if len(sequences) != len(set(sequences)):
		frappe.throw(_("Hay paradas con el mismo orden (sequence duplicada)."), RouteValidationError)
	if any(s <= 0 for s in sequences):
		frappe.throw(_("El orden de una parada debe ser mayor que cero."), RouteValidationError)

	pick_list_names = [s.pick_list for s in stops]
	assigned = _lock_and_get_assigned(pick_list_names, exclude_route=route.name)

	for s in stops:
		pl = frappe.get_doc("Pick List", s.pick_list)
		pl.check_permission("read")
		_validate_pick_list_eligible(pl, route.company, assigned)

	route.status = "Planificado"
	route.save()

	return get_route_detail(route.name)


@frappe.whitelist()
def cancel_route(route_name):
	"""Borrador/Planificado -> Cancelado. Never allowed from En Ruta or
	Completado (brief section 12). Never deletes the Recorrido -- history
	is kept. Releases its Pick Lists implicitly: once status is Cancelado,
	_pick_lists_in_active_routes() simply stops including this route (its
	status is no longer in ACTIVE_ROUTE_STATUSES), no separate "release"
	step needed on the paradas themselves."""
	_require_login()
	frappe.has_permission("Recorrido", "write", throw=True)

	route = frappe.get_doc("Recorrido", route_name)
	route.check_permission("write")

	if route.status not in ("Borrador", "Planificado"):
		frappe.throw(
			_("No se puede cancelar un recorrido en estado {0}.").format(route.status),
			RouteNotEditableError,
		)

	route.status = "Cancelado"
	route.save()

	return get_route_detail(route.name)
