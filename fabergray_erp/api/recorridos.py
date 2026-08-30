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
from frappe.utils import cint, flt, now_datetime, nowdate

from erpnext import get_default_company

from fabergray_erp import geocoding
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

#: Every status Recorrido.status can actually hold (its own DocType JSON
#: Select options) -- the whitelist get_routes()'s own status filter
#: validates against, Commit 24.2. Kept separate from ACTIVE_ROUTE_STATUSES
#: above: that tuple means something specific ("still going to happen, so
#: still holds its Pick Lists"), this one is just "a real status value",
#: used by a completely different concern (listing/filtering, not
#: eligibility).
LISTABLE_ROUTE_STATUSES = ("Borrador", "Planificado", "En Ruta", "Completado", "Cancelado")


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


def _resolve_stop_geolocation(customer_address):
	"""Commit 24.3's own central geolocation-snapshot helper (brief
	section 7). Given the Address name already resolved for a stop
	(possibly None, e.g. a customer with no primary address on file),
	returns the geographic snapshot dict a Recorrido Parada needs:
	latitude/longitude/geolocation_status/geolocation_source.

	Never calls an external provider -- see fabergray_erp.geocoding.
	geocode_address()'s own docstring for why; this only reads whatever
	coordinates already exist on the Address record right now, through
	fabergray_erp.geocoding.is_valid_coordinate_pair(), the same single
	validity rule set_address_geolocation() below enforces when those
	coordinates are first written. A missing Address, missing
	coordinates, or invalid coordinates all resolve to
	geolocation_status="Pendiente" -- this function never raises, so
	create_route()/update_route_stops() can never fail because of it
	(brief section 8's own explicit requirement).

	An Address whose OWN fg_geocoding_status is "Revisar"/"Error" (a
	future, 24.4+ provider flagging a low-confidence or failed lookup)
	propagates that same status onto the stop rather than miscategorizing
	it as a plain "Pendiente" -- neither value is ever produced by this
	commit's own code today (nothing sets Address.fg_geocoding_status to
	anything but "Pendiente"/"Geolocalizado"), but the DocType's own
	Select already reserves the options, so this passthrough costs
	nothing and needs no later revisit."""
	pending = {"latitude": None, "longitude": None, "geolocation_status": "Pendiente", "geolocation_source": None}
	if not customer_address:
		return pending

	row = frappe.db.get_value(
		"Address",
		customer_address,
		["fg_latitude", "fg_longitude", "fg_geocoding_status", "fg_geocoding_source"],
		as_dict=True,
	)
	if not row:
		return pending

	if geocoding.is_valid_coordinate_pair(row.fg_latitude, row.fg_longitude):
		return {
			"latitude": row.fg_latitude,
			"longitude": row.fg_longitude,
			"geolocation_status": "Geolocalizado",
			"geolocation_source": row.fg_geocoding_source or None,
		}

	if row.fg_geocoding_status in ("Revisar", "Error"):
		return {**pending, "geolocation_status": row.fg_geocoding_status}

	return pending


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

	geo = _resolve_stop_geolocation(customer_address)

	return {
		"sales_order": sales_order,
		"commercial_name": root_commercial_name(sales_order) if sales_order else None,
		"customer": pl.customer,
		"customer_name": pl.customer_name,
		"customer_address": customer_address,
		"address_display": address_display,
		"item_count": item_count,
		"total_qty": total_qty,
		"latitude": geo["latitude"],
		"longitude": geo["longitude"],
		"geolocation_status": geo["geolocation_status"],
		"geolocation_source": geo["geolocation_source"],
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


def _batch_resolve_address_previews(customers):
	"""Commit 24.2's own bounded address PREVIEW for a whole page of
	get_available_orders() rows -- {customer: (customer_address,
	address_display)}. Deliberately a preview shortcut, not the
	authoritative resolution: reads Customer.customer_primary_address
	directly rather than going through api.clientes._primary_address_name()
	's own get_default_address() fallback (that fallback stays exactly
	where Commit 24.1 already put it -- _resolve_pick_list_snapshot(), the
	one place that produces the real, PERSISTED snapshot at
	create_route()/update_route_stops() time, and get_available_order_detail(),
	unchanged here). A customer with no customer_primary_address simply
	shows no address preview in the list -- correctness never depends on
	this shortcut, because the real snapshot is (re)computed properly the
	moment a Pick List is actually turned into a stop.

	Bounded to exactly one Customer query, plus one get_address_display()
	call per DISTINCT address on the page -- never one query per Pick List
	row, and never unbounded by how many Pick Lists exist in the system
	(page_length is already capped at 100 by the caller). Repeat customers
	on the same page (a frequent real case) cost nothing extra: this is
	also why get_address_display() itself resolves via
	frappe.get_cached_doc(), not a fresh read every call. See
	test_get_available_orders_address_preview_query_count_is_bounded."""
	customers = sorted({c for c in customers if c})
	if not customers:
		return {}

	primary_address_by_customer = {
		row.name: row.customer_primary_address
		for row in frappe.get_list(
			"Customer",
			filters={"name": ["in", customers]},
			fields=["name", "customer_primary_address"],
		)
		if row.customer_primary_address
	}

	display_by_address = {}
	previews = {}
	for customer, address_name in primary_address_by_customer.items():
		if address_name not in display_by_address:
			display_by_address[address_name] = get_address_display(address_name)
		previews[customer] = (address_name, display_by_address[address_name])
	return previews


@frappe.whitelist()
def get_available_orders(txt=None, start=0, page_length=20):
	"""Paginated Pick Lists eligible for a NEW Recorrido: submitted,
	fg_invoicing_status=Facturado, this site's own default company, and
	not already claimed by an active (Borrador/Planificado/En Ruta)
	Recorrido. Company is ALWAYS resolved server-side via
	erpnext.get_default_company() -- never accepted from the client, the
	same convention api.jefe_bodega already established, so there is no
	parameter a caller could use to ask for another company's data at all.

	Commit 24.2 -- the Recorridos UI's own "pedidos disponibles" cards show
	an address preview per row (see _batch_resolve_address_previews() right
	above), so customer_address/address_display are now included here too,
	resolved in a bounded, batched way for the current page only -- never
	the per-row N+1 Commit 24.1 originally avoided by leaving this out."""
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
	address_previews = _batch_resolve_address_previews([pl.customer for pl in page_rows])

	results = []
	for pl in page_rows:
		sales_order = sales_order_by_pl.get(pl.name)
		customer_address, address_display = address_previews.get(pl.customer, (None, None))
		results.append(
			{
				"pick_list": pl.name,
				"sales_order": sales_order,
				"commercial_name": resolve_commercial_name(sales_order),
				"customer": pl.customer,
				"customer_name": pl.customer_name,
				"customer_address": customer_address,
				"address_display": address_display,
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
			"latitude",
			"longitude",
			"geolocation_status",
			"geolocation_source",
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
		"driver_name": frappe.db.get_value("Driver", route.driver, "full_name") if route.driver else None,
		"driver": route.driver,
		"vehicle": route.vehicle,
		"start_address": route.start_address,
		"notes": route.notes,
		"created_by_user": route.created_by_user,
		"total_stops": len(stops),
		"stops": stops,
	}


# ---------------------------------------------------------------------------
# Read: listado de Recorridos (pestañas "Recorridos" / "Historial", 24.2)
# ---------------------------------------------------------------------------


def _parse_status_filter(status):
	"""get_routes()'s own status filter -- accepts a single status string OR
	a JSON-array-of-strings (same client-shape convention as
	_parse_pick_lists()'s `pick_lists` argument, so the "Recorridos" tab can
	ask for ["Borrador", "Planificado", "En Ruta"] and "Historial" for
	["Completado", "Cancelado"] in one call each, instead of the UI making
	N separate requests and merging them itself). None/empty means "every
	status". Rejects an unrecognized status explicitly rather than
	silently ignoring it or matching nothing."""
	if status in (None, ""):
		return None
	if isinstance(status, str) and status.strip().startswith("["):
		status = frappe.parse_json(status)
	if isinstance(status, str):
		status = [status]
	status = [s for s in (status or []) if s]
	for s in status:
		if s not in LISTABLE_ROUTE_STATUSES:
			frappe.throw(_("Estado de recorrido inválido: {0}.").format(s), RouteValidationError)
	return status or None


@frappe.whitelist()
def get_routes(status=None, start=0, page_length=20):
	"""Paginated Recorrido listing for the "Recorridos" (Borrador/
	Planificado/En Ruta) and "Historial" (Completado/Cancelado) tabs
	(Commit 24.2) -- get_route_detail() alone cannot serve either tab
	(it needs one already-known route_name, not "list routes matching a
	status"), so this is a genuinely new capability, not a duplicate of an
	existing one.

	Company ALWAYS resolved server-side via get_default_company() -- never
	accepted from the client, same convention as every other endpoint in
	this module. Returns only name/route_date/status/driver/driver_name/
	vehicle/created_by_user/creation/stop_count -- no economic value
	anywhere. completed_count is deliberately NOT included: Recorrido
	Parada.status only ever holds "Pendiente" in this and every prior
	commit (no code path anywhere sets "Entregado"/"No Entregado" yet --
	that is 24.6+ scope), so a "completed" count here would always read
	zero and only mislead the UI; add it once delivery actually exists.

	driver_name (Driver.full_name -- Driver's own autoname is a naming-
	series code, e.g. "HR-DRI-2026-00001", never something fit to show a
	user) and stop_count are each resolved via exactly one batched query
	for the whole page -- never one query per route -- see
	test_get_routes_query_count_is_bounded. Vehicle needs no such lookup:
	its own autoname IS its license_plate (erpnext.setup.doctype.vehicle's
	own `"autoname": "field:license_plate"`), already display-ready."""
	_require_login()
	frappe.has_permission("Recorrido", "read", throw=True)

	company = get_default_company()
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length) or 20, 1), 100)

	filters = {"company": company}
	statuses = _parse_status_filter(status)
	if statuses:
		filters["status"] = ["in", statuses]

	page_rows = frappe.get_list(
		"Recorrido",
		filters=filters,
		fields=["name", "route_date", "status", "driver", "vehicle", "created_by_user", "creation"],
		order_by="creation desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	total = len(frappe.get_list("Recorrido", filters=filters, pluck="name"))

	names = [r.name for r in page_rows]
	stop_counts = {}
	if names:
		for recorrido in frappe.get_list("Recorrido Parada", filters={"recorrido": ["in", names]}, pluck="recorrido"):
			stop_counts[recorrido] = stop_counts.get(recorrido, 0) + 1

	driver_names = {}
	drivers = {r.driver for r in page_rows if r.driver}
	if drivers:
		driver_names = {
			row.name: row.full_name
			for row in frappe.get_list("Driver", filters={"name": ["in", list(drivers)]}, fields=["name", "full_name"])
		}

	routes = [
		{
			"name": r.name,
			"route_date": r.route_date,
			"status": r.status,
			"driver": r.driver,
			"driver_name": driver_names.get(r.driver),
			"vehicle": r.vehicle,
			"created_by_user": r.created_by_user,
			"creation": r.creation,
			"stop_count": stop_counts.get(r.name, 0),
		}
		for r in page_rows
	]

	return {"routes": routes, "total": total}


@frappe.whitelist()
def get_routes_summary():
	"""Header counters for the Recorridos page (Commit 24.2): pedidos
	disponibles + one count per ACTIVE route status (Borrador/Planificado/
	En Ruta) -- exactly the four KPI cards the brief's own header design
	asks for, nothing else. Company ALWAYS resolved server-side.

	Deliberately its own tiny endpoint rather than folding these counts
	into get_available_orders()/get_routes(): those two already return
	full paginated ROWS for their own tabs, and a caller only wanting the
	header counters (e.g. on first page load, before either tab is even
	open) would otherwise have to pay for a full page of pick_list/route
	rows just to get four integers, or the UI would have to derive the
	counts itself by re-implementing the SAME eligibility filter
	get_available_orders() already encapsulates -- exactly the backend-
	logic duplication this commit is told to avoid. The "available
	orders" count below reuses _eligible_pick_list_filters()/
	_pick_lists_in_active_routes(), the same two building blocks
	get_available_orders() itself uses, so there is exactly one place that
	defines "what counts as an available order".

	Four small COUNT-shaped queries total (one per status plus one for
	available orders) -- cost is bounded by a constant (the number of
	statuses), never by how many Recorridos or Pick Lists exist."""
	_require_login()
	frappe.has_permission("Recorrido", "read", throw=True)
	frappe.has_permission("Pick List", "read", throw=True)

	company = get_default_company()

	assigned = _pick_lists_in_active_routes()
	filters = _eligible_pick_list_filters(company)
	if assigned:
		filters = filters + [["name", "not in", list(assigned)]]
	available_orders = len(frappe.get_list("Pick List", filters=filters, pluck="name"))

	route_counts = {
		s: len(frappe.get_list("Recorrido", filters={"company": company, "status": s}, pluck="name"))
		for s in ACTIVE_ROUTE_STATUSES
	}

	return {
		"available_orders": available_orders,
		"borrador": route_counts["Borrador"],
		"planificado": route_counts["Planificado"],
		"en_ruta": route_counts["En Ruta"],
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
				"latitude": snap["latitude"],
				"longitude": snap["longitude"],
				"geolocation_status": snap["geolocation_status"],
				"geolocation_source": snap["geolocation_source"],
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
					"latitude": snap["latitude"],
					"longitude": snap["longitude"],
					"geolocation_status": snap["geolocation_status"],
					"geolocation_source": snap["geolocation_source"],
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


# ---------------------------------------------------------------------------
# Geolocalización (Commit 24.3) -- corrección manual de Address, refresco
# de snapshots de Borrador, y el resumen de "listo para calcular ruta".
# NO llama ningún proveedor externo, NO dibuja mapa, NO optimiza orden --
# ver fabergray_erp/geocoding.py's own docstring para el alcance exacto.
# ---------------------------------------------------------------------------


def _address_belongs_to_company(address_name, company):
	"""Company isolation for set_address_geolocation() (brief section 19,
	revised in the turn-4 security audit). Address has no `company` field
	of its own (native Frappe doctype -- section 8 of that audit forbids
	inventing one) -- it links to Customer only through the standard
	Dynamic Link child table, so "does this Address belong to my company"
	has to be answered indirectly through the Customer(s) linked to it.

	A Customer having a Sales Order in `company` is sufficient proof it
	belongs there. But the ABSENCE of one is NOT proof it belongs to some
	OTHER company: a brand-new Customer, correctly created for this
	company's own context, legitimately has zero Sales Orders for a
	while -- rejecting it on that basis alone would block a real
	Fabrigray customer, not stop an attacker. So this only rejects when
	there is *positive* evidence the linked Customer(s) transact
	exclusively with some other company (at least one Sales Order exists
	and none of it is in `company`); a Customer with no Sales Order
	anywhere yet gets the benefit of the doubt.

	`parent_doctype="Address"` is required, not optional: Dynamic Link is
	a child table, which -- as this module's own top docstring already
	established for Pick List Item -- has no independent permission model
	in this Frappe version; without routing the check through its real
	parent (Address), a bare frappe.get_list("Dynamic Link", ...) raises
	PermissionError for every role, confirmed the hard way while writing
	this function's own tests.

	Both Sales Order checks below deliberately use frappe.db.get_value()
	(a raw read), not frappe.get_list(): neither Recorrido nor Gestión de
	Clientes has Sales Order permission (by design -- never part of this
	module's own minimal permission set, see api/recorridos.py's own top
	docstring), and this check's purpose is purely internal ("does a
	Sales Order tie this Address to my company, or to another one"),
	never a document the caller is meant to see the contents of -- exactly
	the same reasoning fabergray_erp.sales_order_naming.
	root_commercial_name() already documents for its own identical choice
	("a raw, single-field read ... never a document permission check")."""
	customers = frappe.get_list(
		"Dynamic Link",
		filters={"parent": address_name, "parenttype": "Address", "link_doctype": "Customer"},
		pluck="link_name",
		parent_doctype="Address",
	)
	if not customers:
		return False

	if frappe.db.get_value(
		"Sales Order", {"customer": ["in", customers], "company": company}, "name"
	):
		return True

	has_sales_order_elsewhere = frappe.db.get_value(
		"Sales Order", {"customer": ["in", customers]}, "name"
	)
	return not has_sales_order_elsewhere


@frappe.whitelist()
def set_address_geolocation(address_name, latitude, longitude, source="Manual", note=None):
	"""The ONE controlled way an operator corrects an Address's
	coordinates (brief section 9/15/16). Manual only in this commit --
	see fabergray_erp/geocoding.py's own docstring for why this never
	invents coordinates from free text and never calls an external
	provider.

	Permissions -- REVISED in the turn-4 security audit. The original
	24.3 design gave Recorrido role its own `write: 1` grant on Address
	so this function's own address.check_permission("write") call would
	pass for Recorrido users. That was proven too broad: it is a
	DocType-level grant, so it also authorized the generic Document API
	(frappe.get_doc("Address", ...).save(), frappe.client.save()) to
	change ANY Address field for ANY Recorrido user, not just the two
	fields this function itself writes -- confirmed by
	test_recorrido_role_cannot_edit_address_line1_directly's own proof
	run against the old grant before this fix.

	Recorrido's Address permission is back to `write: 0`
	(fixtures/custom_docperm.json). Recorrido is a CONSUMER of
	geolocation (reads Address/coordinates/snapshots via
	_resolve_stop_geolocation(), never edits Address), never an
	ADMINISTRATOR of it. Address write is reserved for roles that already
	own that master data natively: Gestión de Clientes (`write: 1` since
	Commit 22.7 -- customer/address management is its whole purpose) and
	System Manager (native full access). No new Custom DocPerm grant was
	added for either -- this function's only remaining job is to *use*
	frappe.has_permission()/check_permission() for real, never
	ignore_permissions, so those two roles' EXISTING permissions become
	the actual authorization boundary. A Recorrido-only or no-role caller
	is rejected here with a real PermissionError, not by convention.

	This function's own fixed signature (only latitude/longitude/source/
	note are ever accepted or written) remains the field-level boundary
	for whichever role IS authorized: Gestión de Clientes can write any
	Address field via Desk already, so nothing new is exposed by giving
	it a working geolocation form too.

	Never ignore_permissions. Never commits the transaction itself --
	Frappe's own request-lifecycle commit covers that, same as every
	other write in this module."""
	_require_login()

	if not frappe.db.exists("Address", address_name):
		frappe.throw(_("La dirección {0} no existe.").format(address_name), frappe.DoesNotExistError)

	frappe.has_permission("Address", "write", throw=True)

	company = get_default_company()
	if not _address_belongs_to_company(address_name, company):
		frappe.throw(
			_("Esta dirección no pertenece a un cliente de esta empresa."),
			frappe.PermissionError,
		)

	if not geocoding.is_valid_coordinate_pair(latitude, longitude):
		frappe.throw(
			_("Las coordenadas ingresadas no son válidas para calcular una ruta."),
			RouteValidationError,
		)

	source = source or "Manual"
	if source not in geocoding.GEOCODING_SOURCES:
		frappe.throw(_("Fuente de geocodificación inválida: {0}.").format(source), RouteValidationError)

	address = frappe.get_doc("Address", address_name)
	address.check_permission("write")
	address.fg_latitude = flt(latitude)
	address.fg_longitude = flt(longitude)
	address.fg_geocoding_status = "Geolocalizado"
	address.fg_geocoding_source = source
	address.fg_geocoded_on = now_datetime()
	address.fg_geocoded_by = frappe.session.user
	address.fg_geocoding_note = note or None
	address.save()

	return {
		"address": address.name,
		"latitude": address.fg_latitude,
		"longitude": address.fg_longitude,
		"geocoding_status": address.fg_geocoding_status,
		"geocoding_source": address.fg_geocoding_source,
	}


@frappe.whitelist()
@_retrying_on_deadlock
def refresh_route_geolocation(route_name):
	"""Re-resolves EVERY Borrador stop's geographic snapshot from its own
	Address's CURRENT coordinates (brief section 10) -- the explicit,
	on-demand step an operator runs after correcting an Address via
	set_address_geolocation(). Never touches identity: stop `name`,
	`sequence`, `pick_list`, `customer`, ... are all left exactly as they
	are, same guarantee update_route_stops() already makes for its own
	kept stops. Only the 4 geo fields (latitude/longitude/
	geolocation_status/geolocation_source) are ever reassigned, via the
	real Document API (never frappe.db.set_value), one .save() per stop.

	Concurrency (brief section 21) -- the exact "User A opens Borrador,
	User B Planifica, User A tries to refresh" race: reads Recorrido.
	status under `for_update=True` (the same native-Frappe row-locking
	primitive _lock_pick_lists() already uses, no raw SQL) before trusting
	it. This alone is sufficient for real mutual exclusion even though
	plan_route() itself never explicitly takes a lock: plan_route()'s own
	route.save() must acquire that same row's write lock to commit its
	UPDATE, so it either commits fully BEFORE this function's own
	for_update read runs (which then correctly observes "Planificado" and
	rejects) or is still in flight (in which case this function's
	for_update read blocks until it resolves, then observes whichever
	state actually won) -- there is no window where either side can act
	on stale, pre-lock status, the same reasoning already documented for
	_locked_assigned_pick_lists()'s own FOR UPDATE read above.

	Planificado/En Ruta/Completado/Cancelado (brief section 22): rejected
	outright, never silently updated -- a Planificado route's own geo
	snapshot is frozen history from the moment it left Borrador, exactly
	like update_route_stops() already refuses to edit its stops at all
	past that point."""
	_require_login()
	frappe.has_permission("Recorrido", "write", throw=True)

	route = frappe.get_doc("Recorrido", route_name)
	route.check_permission("write")

	locked_status = frappe.db.get_value("Recorrido", route_name, "status", for_update=True)
	if locked_status is None:
		frappe.throw(_("El recorrido {0} no existe.").format(route_name), frappe.DoesNotExistError)
	if locked_status != "Borrador":
		frappe.throw(
			_("Solo se puede actualizar la geolocalización de un recorrido en Borrador."),
			RouteNotEditableError,
		)

	stops = frappe.get_list(
		"Recorrido Parada",
		filters={"recorrido": route.name},
		fields=["name", "customer_address"],
	)
	for s in stops:
		geo = _resolve_stop_geolocation(s.customer_address)
		stop_doc = frappe.get_doc("Recorrido Parada", s.name)
		stop_doc.check_permission("write")
		stop_doc.latitude = geo["latitude"]
		stop_doc.longitude = geo["longitude"]
		stop_doc.geolocation_status = geo["geolocation_status"]
		stop_doc.geolocation_source = geo["geolocation_source"]
		stop_doc.save()

	return get_route_detail(route.name)


@frappe.whitelist()
def get_route_geolocation_status(route_name):
	"""Read-only geographic-readiness summary for a route (brief section
	11/12) -- no economic value anywhere. `ready_for_routing` is a pure,
	never-persisted computed signal: true only when there is at least one
	stop AND every single one of them is geolocation_status=
	"Geolocalizado" (never true for an empty route, never true while any
	stop is Pendiente/Revisar/Error). This means "the data is ready for a
	future routing calculation to run on" -- it does NOT mean the route
	is optimized, and no code anywhere sets or reads this value except
	this endpoint computing it fresh on every call."""
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
			"latitude",
			"longitude",
			"geolocation_status",
		],
		order_by="sequence asc",
	)

	resolve_commercial_name = _commercial_name_cache()
	for s in stops:
		s["commercial_name"] = resolve_commercial_name(s.sales_order)

	geolocated_stops = sum(1 for s in stops if s.geolocation_status == "Geolocalizado")

	return {
		"route": route.name,
		"total_stops": len(stops),
		"geolocated_stops": geolocated_stops,
		"pending_stops": len(stops) - geolocated_stops,
		"ready_for_routing": bool(stops) and geolocated_stops == len(stops),
		"stops": stops,
	}
