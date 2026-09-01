# -*- coding: utf-8 -*-
"""Supervision + purchase-receipt API for the Jefe de Bodega Page.

Commit 6-8: every read function here is a thin, read-only wrapper. The four
Pick List states (pendientes/en_alistamiento/con_faltantes/listos) are never
re-bucketed here -- get_queue() from api.bodega is the single source of
truth, imported and called directly.

Same permission policy as api/bodega.py: only frappe.get_list / frappe.get_doc
+ doc.check_permission() / frappe.has_permission(), so Role Permissions AND
User Permissions (including per-Warehouse scoping) are applied by frappe
itself. No frappe.get_all, no ignore_permissions, no manual bypass -- child
table data (Pick List Item / Pick List.locations) is only ever read off a
Pick List document after check_permission("read") has been called on that
specific document, exactly like get_pick_list() already does in api/bodega.py.

Commit 22.8 -- receive_shortage_purchase()/get_shortage_purchase_status():
"Registrar compra recibida" from the Reporte de Faltante flow. Audited
against ERPNext v16 directly (not assumed) before writing a line of this:
Purchase Receipt requires a real Supplier (reqd=1, purchase_receipt.json)
this app has no Proveedores module for yet, and its own on_submit is already
wired (fulfillment/purchase_receipt_hooks.py) to the Fulfillment Engine's
process_sales_order() -- entangling an ad-hoc, no-PO receipt with that would
mean either a fabricated Supplier or an unapproved side effect, both
rejected. Stock Entry purpose="Material Receipt" needs neither: no Supplier,
no PO, untouched by any Fulfillment Engine hook. The one real native
requirement (Stock Entry Item.expense_account, "Difference Account",
mandatory whenever perpetual inventory is enabled -- confirmed live: it is,
for this Company) is never picked or configured here -- see
_resolve_receipt_expense_account()'s own docstring.

This is a native, submitted Stock Entry -- Bin.actual_qty, Stock Ledger
Entry and GL Entry are all ERPNext's own consequence of that submit, never
written here directly. Standard Buying Item Price is updated afterwards
through api.inventario._upsert_item_price() (Commit 22.6's own helper,
reused as-is, not reimplemented). valuation_rate is never touched anywhere
in this module -- ERPNext's own valuation method computes it from the
Stock Entry's basic_rate.

Traceability (received_qty/remaining_qty per Reporte de Faltante, possibly
split across more than one receipt): derived by querying submitted Stock
Entry documents through the new Custom Field `fg_shortage_report` (Link,
read_only, set only by receive_shortage_purchase() below), never by reading
Bin/Warehouse current stock (which could include stock from unrelated
movements) and never duplicated into a new field/child table on Reporte de
Faltante itself -- see _shortage_receipts()'s own docstring for why this was
preferred over a child table.

Concurrency: the exact same native row-lock idiom already proven in
fulfillment/shortage_service.py's own sync_shortage_reports_for_sales_order()
(a plain `SELECT ... FOR UPDATE` on the document about to be read-then-
written, held for the rest of the transaction) -- reused here on the
Reporte de Faltante row itself, not invented. Two concurrent calls for the
same report serialize; the second one sees the first's already-persisted
outcome (report.status, received_qty) once it proceeds.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from erpnext import get_default_company
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults

from fabergray_erp.api.bodega import (
	OPEN_SHORTAGE_STATUSES,
	_batch_item_names,
	_open_shortage_pick_lists,
	_pick_list_bucket,
	_require_login,
	get_queue,
)
from fabergray_erp.api.inventario import PRICE_LIST_BUYING, _selling_rates, _upsert_item_price
from fabergray_erp.sales_order_naming import root_commercial_name

RESOLVED_STATUS = "Resuelto"
IN_PROGRESS_STATUS = "En Proceso"
OPEN_STATUS = "Abierto"
MATERIAL_RECEIPT_PURPOSE = "Material Receipt"

DEFAULT_RESOLUTION_NOTE = _("Faltante completado mediante recepción de compra.")

#: Commit 22.9 -- Page Jefe de Bodega's 4 quick-access modules
#: (jefe-pick-lists / centro-faltantes / almacenes). Bounded fetch for the
#: Python-side status filter in get_pick_list_history() (status is not a
#: plain column -- see _pick_list_bucket()), same reasoning as api.clientes'
#: own INCOMPLETE_FETCH_CAP for its "incomplete" tab.
PICK_LIST_HISTORY_FETCH_CAP = 1000
PICK_LIST_HISTORY_STATES = ("listos", "con_faltantes", "en_alistamiento", "pendientes")

#: Commit 22.9 -- server-side page_length ceilings, independent of
#: whatever a caller asks for: a request for 1000000 rows is silently
#: clamped, never honored literally. Warehouse items gets a wider ceiling
#: than the other two (a single Warehouse's own catalog, the same
#: reasoning PICK_LIST_HISTORY_FETCH_CAP above already documents for a
#: different bounded-fetch case).
MAX_PAGE_LENGTH = 100
MAX_PAGE_LENGTH_WAREHOUSE_ITEMS = 200


def _clamp_pagination(start, page_length, ceiling):
	"""start/page_length, sanitized the same way for every paginated
	endpoint in this module: never negative, never above `ceiling`
	regardless of what the caller asked for."""
	start = max(cint(start), 0)
	page_length = cint(page_length) or ceiling
	page_length = min(max(page_length, 1), ceiling)
	return start, page_length


def _validate_date_param(value, label):
	"""Server-side date validation for date_from/date_to -- a malformed
	value must produce a clear functional error here, never an
	unparameterized string reaching the query builder to fail as a raw
	DB error later. frappe.utils.getdate() already raises for anything
	that isn't a real date; normalized back to "YYYY-MM-DD" so every
	caller (a datetime.date, "2026-01-01", or frappe's own datepicker
	string) produces the exact same filter value."""
	if not value:
		return None
	try:
		return frappe.utils.getdate(value).isoformat()
	except Exception:
		frappe.throw(_("Fecha inválida para {0}: {1}").format(label, value))


def _validate_own_company_warehouse(warehouse, company):
	"""Server-side Warehouse validation shared by get_pick_list_history()
	and get_warehouse_items(): a Warehouse belonging to another Company
	(this site's own other, demo companies -- see get_warehouse_summary()'s
	own docstring) is rejected outright, the same way an invalid name is --
	never silently ignored, never trusted just because the caller passed
	something that exists."""
	if warehouse and not frappe.db.exists("Warehouse", {"name": warehouse, "company": company}):
		frappe.throw(_("Almacén inválido: {0}").format(warehouse))
PICK_LIST_HISTORY_FIELDS = [
	"name",
	"docstatus",
	"status",
	"purpose",
	"parent_warehouse",
	"customer",
	"fg_started_by",
	"fg_started_on",
	"modified",
	"modified_by",
	"creation",
]


# -- Commit 22.8: named exceptions, same convention api/inventario.py's own
# write endpoints already use (OpeningStockRequiredError,
# MissingOpeningAccountError, ...) -- a test (or a future caller) can
# assertRaises() the exact functional reason, never just "something failed".
class NonPositiveReceiptQuantityError(frappe.ValidationError):
	pass


class NonPositivePurchaseRateError(frappe.ValidationError):
	pass


class InvalidReceiptWarehouseError(frappe.ValidationError):
	pass


class MismatchedReceiptWarehouseError(frappe.ValidationError):
	pass


class InvalidReceiptItemError(frappe.ValidationError):
	pass


class ShortageAlreadyResolvedError(frappe.ValidationError):
	pass


class ExcessReceiptQuantityError(frappe.ValidationError):
	pass


class MissingReceiptAccountError(frappe.ValidationError):
	pass


@frappe.whitelist()
def get_summary():
	"""Counts for the 5 KPI cards. Single round-trip for the whole row.

	The four Pick List counts come straight out of get_queue()'s buckets --
	no re-implementation of the bucketing rule. "Faltantes abiertos" is a
	plain, permission-scoped count on Reporte de Faltante -- Abierto AND En
	Proceso (Commit 22.8: a partially-received shortage still needs
	attention, it must not silently drop off this count the moment its
	first receipt lands)."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	queue = get_queue()
	faltantes_abiertos = frappe.get_list(
		"Reporte de Faltante", filters={"status": ["in", OPEN_SHORTAGE_STATUSES]}, pluck="name"
	)

	return {
		"pendientes": len(queue["pendientes"]),
		"en_alistamiento": len(queue["en_alistamiento"]),
		"con_faltantes": len(queue["con_faltantes"]),
		"listos": len(queue["listos"]),
		"faltantes_abiertos": len(faltantes_abiertos),
	}


@frappe.whitelist()
def get_open_shortage_reports():
	"""Open (Abierto or En Proceso, Commit 22.8 -- see get_summary()'s own
	docstring) Reporte de Faltante records for the "Requieren atención"
	section.

	Reporte de Faltante does not store item_name, only item_code. Rather than
	querying the Item doctype (Jefe de Bodega has no permission on Item) or
	using frappe.get_all on the Pick List Item child table (forbidden by
	policy for this API), the item name is read off the linked Pick List
	document itself -- after confirming read access to that specific Pick
	List -- exactly the same pattern _get_pick_list_row() already uses in
	api/bodega.py. Pick List docs are cached per request so a batch of
	reports referencing the same Pick List only loads it once.
	"""
	_require_login()
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	reports = frappe.get_list(
		"Reporte de Faltante",
		filters={"status": ["in", OPEN_SHORTAGE_STATUSES]},
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
			"reported_by",
			"reported_on",
		],
		order_by="reported_on desc",
		limit_page_length=0,
	)

	pick_list_cache = {}
	item_name_by_code = _batch_item_names([r.item_code for r in reports])
	for report in reports:
		report["item_name"] = _resolve_item_name(
			report.pick_list, report.pick_list_item, report.item_code, pick_list_cache, item_name_by_code
		)
		report["reported_by_fullname"] = frappe.utils.get_fullname(report.reported_by)

	return reports


def _resolve_item_name(pick_list, pick_list_item, item_code, pick_list_cache=None, item_name_by_code=None):
	"""Reporte de Faltante does not store item_name, only item_code. The
	item name is read PRIMARILY off the linked Pick List document itself
	(after confirming read access to that specific Pick List -- exactly
	the same pattern _get_pick_list_row() already uses in api/bodega.py)
	-- never frappe.get_all on the Pick List Item child table (forbidden
	by policy for this API). `pick_list_cache` (optional, a plain dict)
	lets a caller iterating many reports load the same Pick List only
	once; get_shortage_purchase_status()/receive_shortage_purchase()
	below (single-report callers) omit it.

	Commit 25.3 -- FALLBACK added: a report the Fulfillment Engine
	creates at `actual_qty=0` never has a Pick List at all (confirmed
	live -- create_pick_list_for_available_stock() returns None before
	building anything when nothing is theoretically available), so this
	always fell through to the bare item_code for exactly those reports
	-- the root cause of "Faltante 0002" instead of the real product
	name. Item now has read=1 for every role that reaches this function
	(Jefe de Bodega/Facturación/System Manager, Custom DocPerm as of a
	later commit than the one that first wrote this docstring) -- no
	longer the permission gap the original comment assumed. `item_name_
	by_code`, if given, must already be a fully-resolved dict (built via
	_batch_item_names() ONCE for the whole page by the caller -- never a
	query issued from inside this function, so iterating many reports
	never becomes N+1); when omitted (the two single-report callers), a
	single frappe.db.get_value() read is used instead. item_code remains
	the final, last-resort fallback if the Item itself cannot be found
	either -- this function never raises."""
	if pick_list and pick_list_item:
		cache = pick_list_cache if pick_list_cache is not None else {}
		try:
			if pick_list not in cache:
				doc = frappe.get_doc("Pick List", pick_list)
				doc.check_permission("read")
				cache[pick_list] = doc
			rows = cache[pick_list].get("locations", {"name": pick_list_item})
			if rows:
				return rows[0].item_name
		except frappe.PermissionError:
			# Report references a Pick List this user can no longer read
			# (e.g. outside their Warehouse User Permission). Fall through
			# to the Item-based lookup below rather than failing the call.
			pass

	if item_name_by_code is not None:
		return item_name_by_code.get(item_code) or item_code
	return frappe.db.get_value("Item", item_code, "item_name") or item_code


@frappe.whitelist()
def get_active_pick_lists():
	"""Pick Lists currently being picked, for the "Alistamientos activos" section.

	The bucket itself is get_queue()["en_alistamiento"] -- not recomputed here.
	items_completos/items_totales are exact counts, read off doc.locations
	after check_permission("read") on each individual authorized Pick List
	(en_alistamiento is expected to be a small list in this first version, so
	one get_doc per row is preferred over any batched/child-table query that
	would bypass the parent's permission check). If a Pick List has no rows
	at all, both fields are returned as null so the UI never fabricates a
	fraction.
	"""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	active = get_queue()["en_alistamiento"]

	for pl in active:
		doc = frappe.get_doc("Pick List", pl["name"])
		doc.check_permission("read")
		rows = doc.get("locations") or []
		total = len(rows)
		done = sum(1 for row in rows if flt(row.picked_qty) >= flt(row.stock_qty))

		pl["items_completos"] = done if total else None
		pl["items_totales"] = total if total else None
		pl["fg_started_by_fullname"] = (
			frappe.utils.get_fullname(pl["fg_started_by"]) if pl.get("fg_started_by") else None
		)

	return active


# ---------------------------------------------------------------------------
# Commit 22.8 -- Recepción de compra desde Reporte de Faltante
# ---------------------------------------------------------------------------


def _resolve_receipt_expense_account(item_code, company):
	"""Read-only replication of Stock Entry's own native Difference Account
	resolution chain (erpnext/stock/doctype/stock_entry/stock_entry.py's
	get_item_details(): Item Default.expense_account for this company ->
	Item Group Default.expense_account, native, inherited up the tree via
	get_item_group_defaults() -> Company.stock_adjustment_account) -- reused
	verbatim (get_item_group_defaults is imported, not reimplemented), never
	set, never picked, never defaulted to anything of this module's own
	choosing. Only ever used to answer one question before attempting the
	real Stock Entry: would ERPNext's own validate_difference_account()
	find something here, or would it throw? Audited live against this site's
	real data (Commit 22.8's own audit): Company.stock_adjustment_account is
	unset, no Item Default/Item Group Default expense_account exists for any
	real Item here either -- so today this always returns falsy, and
	receive_shortage_purchase() must stop with a clear functional error
	instead of letting ERPNext's own internal error surface or silently
	guessing an account (e.g. "Apertura Temporal", or configuring
	Company.stock_adjustment_account) -- both explicitly rejected."""
	item_default = frappe.db.get_value(
		"Item Default", {"parent": item_code, "company": company}, "expense_account"
	)
	if item_default:
		return item_default

	item_group_defaults = get_item_group_defaults(item_code, company)
	if item_group_defaults.get("expense_account"):
		return item_group_defaults["expense_account"]

	return frappe.db.get_value("Company", company, "stock_adjustment_account")


def _shortage_receipts(shortage_report):
	"""Every submitted Material Receipt Stock Entry linked to
	`shortage_report` via the native Custom Field `fg_shortage_report` --
	the traceability mechanism Commit 22.8 chose over a child table on
	Reporte de Faltante itself (investigated both, see this module's own
	top docstring): a submitted document ERPNext already keeps forever
	(with its own audit trail, its own cancellation semantics) is a more
	robust source of truth than a duplicated quantity this module would
	otherwise have to keep in sync by hand on every submit/cancel. Never
	reads Bin/current Warehouse stock for this -- that could include stock
	from any other, unrelated movement.

	frappe.get_list (permission-respecting, this module's own established
	rule) + one frappe.get_doc().check_permission("read") per matching
	Stock Entry -- same "batch id lookup, then check_permission per
	document" idiom get_active_pick_lists() above already uses, preferred
	here too over a raw child-table read that would bypass each document's
	own permission check. Every Stock Entry this flow creates has exactly
	one Item row (its own invariant, enforced by receive_shortage_purchase()
	building it that way) -- doc.items[0] is always that one row.

	Cancelled Stock Entries (docstatus=2) never match the `docstatus=1`
	filter -- they stop counting as received the moment they're cancelled,
	with no extra bookkeeping required.

	Returns (receipts, received_qty) -- receipts is a list of dicts (posting
	order), received_qty is the plain sum of their qty."""
	names = frappe.get_list(
		"Stock Entry",
		filters={
			"fg_shortage_report": shortage_report,
			"purpose": MATERIAL_RECEIPT_PURPOSE,
			"docstatus": 1,
		},
		pluck="name",
		order_by="posting_date asc, posting_time asc, creation asc",
	)

	receipts = []
	received_qty = 0.0
	for name in names:
		doc = frappe.get_doc("Stock Entry", name)
		doc.check_permission("read")
		row = doc.items[0]
		qty = flt(row.qty)
		receipts.append(
			{
				"stock_entry": doc.name,
				"posting_date": doc.posting_date,
				"qty": qty,
				"purchase_rate": flt(row.basic_rate),
				"amount": qty * flt(row.basic_rate),
				"purchase_reference": doc.get("fg_purchase_reference"),
			}
		)
		received_qty += qty

	return receipts, received_qty


@frappe.whitelist()
def get_shortage_purchase_status(shortage_report):
	"""Read-only: received_qty/remaining_qty/receipts[] for one Reporte de
	Faltante, for the "Registrar compra recibida" modal (initial load) and
	for the post-submit confirmation screen. remaining_qty is
	max(qty_faltante - received_qty, 0) -- qty_faltante is this report's own
	original shortage amount, a Read Only field this module never writes to
	(see receive_shortage_purchase()'s own docstring for why qty_solicitada/
	qty_disponible/qty_faltante are deliberately left untouched by every
	receipt), so it stays a stable reference point across any number of
	partial receipts.

	receipts/received_qty are resolved only if the caller actually has
	Stock Entry read permission (explicit frappe.has_permission() check
	first -- frappe.get_list() raises PermissionError outright for a
	doctype the caller has zero permission on, same reasoning
	api.clientes.get_customer_detail() already documents for Contact/
	Address): a caller with Reporte de Faltante read only (e.g. Bodega,
	which has no Stock Entry permission at all, by design) still gets a
	valid response, simply with an empty receipts[] and received_qty=0,
	never a PermissionError from this incidental lookup."""
	_require_login()

	report = frappe.get_doc("Reporte de Faltante", shortage_report)
	report.check_permission("read")

	if frappe.has_permission("Stock Entry", "read"):
		receipts, received_qty = _shortage_receipts(report.name)
	else:
		receipts, received_qty = [], 0.0
	remaining_qty = max(flt(report.qty_faltante) - received_qty, 0.0)

	return {
		"shortage_report": report.name,
		"item_code": report.item_code,
		"item_name": _resolve_item_name(report.pick_list, report.pick_list_item, report.item_code),
		"warehouse": report.warehouse,
		"sales_order": report.sales_order,
		"pick_list": report.pick_list,
		"qty_solicitada": report.qty_solicitada,
		"qty_disponible": report.qty_disponible,
		"qty_faltante": report.qty_faltante,
		"shortage_reason": report.shortage_reason,
		"status": report.status,
		"received_qty": received_qty,
		"remaining_qty": remaining_qty,
		"receipts": receipts,
	}


@frappe.whitelist()
def receive_shortage_purchase(
	shortage_report, qty, purchase_rate, warehouse=None, purchase_reference=None, note=None
):
	"""Registers merchandise that physically arrived from a purchase against
	one Reporte de Faltante -- exclusively via a native, submitted Stock
	Entry (purpose="Material Receipt"); see this module's own top docstring
	for the full Stock Entry vs Purchase Receipt audit. Never writes
	Bin.actual_qty, a Stock Ledger Entry or a GL Entry directly -- those are
	entirely ERPNext's own consequence of entry.submit() below.

	Authorization: gated on real Stock Entry create/submit permission
	(Commit 22.8's own Custom DocPerm grant -- Jefe de Bodega/System
	Manager/the native Stock User-family roles; Bodega has none, by
	design), never a hardcoded role-name check -- the same
	frappe.has_permission(throw=True) convention every other write endpoint
	in this app uses. Reporte de Faltante's own write permission is also
	required (both Bodega and Jefe de Bodega already have it natively) --
	Stock Entry permission is what actually excludes Bodega here.

	Concurrency: a `SELECT ... FOR UPDATE` lock on the Reporte de Faltante
	row (this module's own top docstring: same idiom as
	shortage_service.py's sync_shortage_reports_for_sales_order(), not
	invented) is acquired before anything else, serializing two concurrent
	calls against the same report. A report already Resuelto rejects any
	further call outright (ShortageAlreadyResolvedError) -- the same guard
	that stops a report from ever being "received into" twice once its
	full original qty_faltante has already been covered by earlier
	receipts (this call or an earlier one), covering the accidental
	double-submit case without inventing a separate idempotency key: this
	is a real, physical "merchandise arrived" event, not a value that could
	ever legitimately need silent deduplication by payload alone.

	qty_solicitada/qty_disponible/qty_faltante on the report are never
	touched -- see get_shortage_purchase_status()'s own docstring for why
	qty_faltante must stay a stable original-shortage reference point.
	Only `status`/`resolution_note` change here: En Proceso while
	received_qty > 0 and remaining_qty > 0 (a genuine partial receipt),
	Resuelto once remaining_qty reaches 0 -- and even then,
	resolution_note is only ever set automatically when the field is
	still empty, never overwriting an existing human note (this report's
	own, or a previous receipt's).

	warehouse is always the report's own warehouse -- this flow exists to
	complete ONE specific shortage, not to record a general purchase
	receipt, so a caller passing a different Warehouse is rejected outright
	(MismatchedReceiptWarehouseError), never silently redirected there or
	silently ignored. Prevents the real, named risk this was written to
	close: resolving a Producto Terminado shortage while accidentally
	receiving stock into Devoluciones. Passing the report's own warehouse
	explicitly (what the UI always does) or omitting it entirely both work
	identically. A distinct future flow, not this one, is the right place
	for "receive into a different Warehouse".

	qty is capped at this call's own remaining_qty (computed from
	_shortage_receipts() BEFORE this receipt, under the same row lock) --
	over-receiving beyond what this specific shortage still needs is
	rejected (ExcessReceiptQuantityError) with the exact remaining amount
	in the message, not silently truncated or accepted. This endpoint
	completes a shortage, it does not log a general purchase.

	purchase_reference/note map onto this Stock Entry's own
	fg_purchase_reference (Commit 22.8's Custom Field) and native `remarks`
	field respectively -- neither is a new field invented where a native
	one already existed (remarks already did).

	Standard Buying Item Price is updated (or created exactly once) via
	api.inventario._upsert_item_price() -- Commit 22.6's own helper, reused
	verbatim, after entry.submit() succeeds -- purchase_rate is always
	"the last real commercial purchase price", per this commit's own brief.
	Never touches valuation_rate anywhere (Bin's, the Stock Ledger's, or
	Item's) -- ERPNext's own valuation method computes it from the Stock
	Entry's basic_rate.

	Atomic by construction, not by any explicit transaction call: every
	write (Stock Entry insert+submit, Item Price upsert, Reporte de
	Faltante save) happens in this one function body, no frappe.db.commit()
	anywhere in this module -- an exception at any point (a bad qty/rate,
	a missing account, a native validation failure) propagates out of this
	whitelisted method before its own request-lifecycle commit, and
	Frappe rolls back everything written so far in the same request,
	including an already-submitted Stock Entry."""
	_require_login()
	frappe.has_permission("Stock Entry", "create", throw=True)
	frappe.has_permission("Stock Entry", "submit", throw=True)

	# Lock first (harmless no-op if the row doesn't exist -- frappe.get_doc()
	# right after is what actually raises DoesNotExistError for that case).
	frappe.db.get_value("Reporte de Faltante", shortage_report, "name", for_update=True)
	report = frappe.get_doc("Reporte de Faltante", shortage_report)
	report.check_permission("write")

	qty = flt(qty)
	purchase_rate = flt(purchase_rate)

	if qty <= 0:
		frappe.throw(_("La cantidad recibida debe ser mayor que cero."), NonPositiveReceiptQuantityError)
	if purchase_rate <= 0:
		frappe.throw(
			_("El valor de compra unitario debe ser mayor que cero."), NonPositivePurchaseRateError
		)

	if report.status == RESOLVED_STATUS:
		frappe.throw(
			_(
				"Este Reporte de Faltante ya está Resuelto -- no se puede registrar otra "
				"compra sobre él."
			),
			ShortageAlreadyResolvedError,
		)

	if not frappe.db.exists("Item", report.item_code):
		frappe.throw(_("Item inválido: {0}").format(report.item_code), InvalidReceiptItemError)

	candidate_warehouse = warehouse or report.warehouse
	if not frappe.db.exists("Warehouse", candidate_warehouse):
		frappe.throw(_("Almacén inválido: {0}").format(candidate_warehouse), InvalidReceiptWarehouseError)
	if candidate_warehouse != report.warehouse:
		frappe.throw(
			_(
				"Este flujo solo puede recibir mercancía en el almacén del Reporte de "
				"Faltante ({0})."
			).format(report.warehouse),
			MismatchedReceiptWarehouseError,
		)
	warehouse = report.warehouse

	received_qty_before = _shortage_receipts(report.name)[1]
	remaining_before = max(flt(report.qty_faltante) - received_qty_before, 0.0)
	if qty > remaining_before:
		frappe.throw(
			_("La cantidad recibida supera el faltante pendiente ({0}).").format(remaining_before),
			ExcessReceiptQuantityError,
		)

	company = frappe.db.get_value("Warehouse", warehouse, "company")

	if not _resolve_receipt_expense_account(report.item_code, company):
		frappe.throw(
			_(
				"No existe una cuenta contable configurada para registrar esta recepción. "
				"Contacta al administrador."
			),
			MissingReceiptAccountError,
		)

	entry = frappe.new_doc("Stock Entry")
	entry.purpose = MATERIAL_RECEIPT_PURPOSE
	entry.company = company
	entry.set_stock_entry_type()  # native helper -- stock_entry_type is mandatory, never auto-set on a plain new_doc()
	entry.fg_shortage_report = report.name
	if purchase_reference:
		entry.fg_purchase_reference = purchase_reference
	if note:
		entry.remarks = note
	entry.append(
		"items",
		{
			"item_code": report.item_code,
			"t_warehouse": warehouse,
			"qty": qty,
			"basic_rate": purchase_rate,
		},
	)
	entry.insert()  # real permission, no ignore_permissions
	entry.submit()  # real permission, no ignore_permissions

	_upsert_item_price(report.item_code, PRICE_LIST_BUYING, purchase_rate)

	receipts, received_qty = _shortage_receipts(report.name)
	remaining_qty = max(flt(report.qty_faltante) - received_qty, 0.0)

	if remaining_qty <= 0:
		report.status = RESOLVED_STATUS
		if not (report.resolution_note or "").strip():
			report.resolution_note = DEFAULT_RESOLUTION_NOTE
	elif received_qty > 0:
		report.status = IN_PROGRESS_STATUS
	report.save()  # real permission, no ignore_permissions

	# Read-only, post-submit -- current_stock is just what the confirmation
	# screen shows the user, never used to derive received_qty/remaining_qty
	# above (see _shortage_receipts()'s own docstring for why: current Bin
	# stock can include other, unrelated movements).
	current_stock = flt(frappe.db.get_value("Bin", {"item_code": report.item_code, "warehouse": warehouse}, "actual_qty"))

	return {
		"stock_entry": entry.name,
		"item_code": report.item_code,
		"item_name": _resolve_item_name(report.pick_list, report.pick_list_item, report.item_code),
		"qty": qty,
		"purchase_rate": purchase_rate,
		"amount": qty * purchase_rate,
		"warehouse": warehouse,
		"current_stock": current_stock,
		"received_qty": received_qty,
		"remaining_qty": remaining_qty,
		"status": report.status,
		"receipts": receipts,
	}


# ---------------------------------------------------------------------------
# Commit 22.9 -- Módulos visuales de Jefe de Bodega: Pick Lists (resumen
# operativo/historial), Reportes de Faltante (centro de faltantes/compras),
# Almacenes. Inventario reuses /app/inventario as-is (api/inventario.py),
# no new endpoint for it. Every list here is real, DB-level paginated
# (limit_start/limit_page_length) except where the filter itself isn't a
# plain column (get_pick_list_history()'s own `status` -- see its
# docstring) -- and every per-row derived field (shortage flags, line
# counts/qty sums, received_qty, selling rates) is resolved with ONE
# batched query for the whole page, never one per row -- same "bulk read,
# never N+1" rule get_queue()/api.inventario.py's own docstrings already
# state as a standing rule for this app.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_pick_list_history_summary():
	"""KPI row for the Pick Lists view: Total hoy / Listos / Con faltantes /
	En alistamiento / Completados -- always TODAY, independent of
	get_pick_list_history()'s own filters (a separate, always-current-day
	snapshot, the same relationship api.clientes' get_dashboard_summary()
	has to its own search_customers()). Uses the exact same
	_pick_list_bucket() rule get_pick_list_history() uses below -- never a
	parallel definition of "con faltantes"/"en alistamiento".

	Completados: of today's "listos" (docstatus==1), the ones whose native
	`status` is "Completed" -- Pick List's own field (pick_list.py's
	get_transfer_status()/update_status()), never a status this module
	invents. For a purpose="Delivery" Pick List (this app's only kind),
	ERPNext derives it from the linked Sales Order's own delivery
	tracking -- if this app's flow doesn't yet reach that (no Delivery
	Note wired up), this count is honestly 0, not fabricated."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	today = nowdate()
	rows = frappe.get_list(
		"Pick List",
		filters=[
			["docstatus", "in", [0, 1]],
			["company", "=", get_default_company()],
			["creation", ">=", f"{today} 00:00:00"],
			["creation", "<=", f"{today} 23:59:59"],
		],
		fields=["name", "docstatus", "status", "fg_started_by"],
		limit_page_length=0,
	)

	shortage_pick_lists = _open_shortage_pick_lists([r.name for r in rows])

	counts = {"listos": 0, "con_faltantes": 0, "en_alistamiento": 0, "pendientes": 0}
	completados = 0
	for pl in rows:
		bucket = _pick_list_bucket(pl, shortage_pick_lists)
		counts[bucket] += 1
		if bucket == "listos" and pl.status == "Completed":
			completados += 1

	return {
		"total_hoy": len(rows),
		"listos": counts["listos"],
		"con_faltantes": counts["con_faltantes"],
		"en_alistamiento": counts["en_alistamiento"],
		"completados": completados,
	}


def _pick_lists_matching_sales_order(txt):
	"""Pick List names whose own Pick List Item rows reference a Sales
	Order matching `txt` -- lets "buscar por Pedido" work even though
	`sales_order` lives only on the child table, not on Pick List itself.
	frappe.get_all (child table, no permission model of its own -- exactly
	get_queue()'s own documented reasoning): this only narrows a filter,
	the actual Pick List fetch afterwards is still a permission-checked
	frappe.get_list()."""
	if not txt:
		return []
	return frappe.get_all(
		"Pick List Item", filters={"sales_order": ["like", f"%{txt}%"]}, pluck="parent", distinct=True
	)


@frappe.whitelist()
def get_pick_list_history(status=None, date_from=None, date_to=None, warehouse=None, txt=None, start=0, page_length=20):
	"""Paginated Pick List history for Jefe de Bodega's "resumen operativo"
	-- draft AND submitted Pick Lists (never only the currently-open queue
	get_queue() shows), filtered by creation date range/Warehouse/free
	text, bucketed with the exact same _pick_list_bucket() rule get_queue()
	itself uses (imported from api.bodega, never re-derived).

	status: one of PICK_LIST_HISTORY_STATES, or falsy/unrecognized for
	"todos" (same convention as api.clientes.search_customers()'s own
	status handling). Since bucket membership depends on a linked Reporte
	de Faltante (not a plain Pick List column), filtering by it can't be
	pushed to the database the way date/warehouse/txt are: this fetches up
	to PICK_LIST_HISTORY_FETCH_CAP date/warehouse/txt-matching rows,
	buckets them, filters by status and paginates the result in Python --
	the exact same bounded-fetch-then-filter pattern api.clientes'
	"incomplete" tab already uses for an equivalent problem (a status with
	no native column). Without a status filter, pagination is a plain,
	efficient DB-level limit_start/limit_page_length query instead.

	Per-row item_count/qty_requerida/qty_alistada/shortage_count/
	commercial_name are resolved with ONE batched Pick List Item query and
	ONE batched Reporte de Faltante count query, scoped to only the
	page actually being returned -- never one query per Pick List."""
	_require_login()
	frappe.has_permission("Pick List", "read", throw=True)

	start, page_length = _clamp_pagination(start, page_length, MAX_PAGE_LENGTH)
	txt = (txt or "").strip()
	status = (status or "").strip()
	if status not in PICK_LIST_HISTORY_STATES:
		status = None

	company = get_default_company()
	_validate_own_company_warehouse(warehouse, company)
	date_from = _validate_date_param(date_from, _("fecha desde"))
	date_to = _validate_date_param(date_to, _("fecha hasta"))

	filters = [["docstatus", "in", [0, 1]], ["company", "=", company]]
	if date_from:
		filters.append(["creation", ">=", f"{date_from} 00:00:00"])
	if date_to:
		filters.append(["creation", "<=", f"{date_to} 23:59:59"])
	if warehouse:
		filters.append(["parent_warehouse", "=", warehouse])

	or_filters = None
	if txt:
		or_filters = [["name", "like", f"%{txt}%"], ["customer", "like", f"%{txt}%"]]
		matching_by_so = _pick_lists_matching_sales_order(txt)
		if matching_by_so:
			or_filters.append(["name", "in", matching_by_so])

	if status:
		wide = frappe.get_list(
			"Pick List",
			filters=filters,
			or_filters=or_filters,
			fields=PICK_LIST_HISTORY_FIELDS,
			order_by="creation desc",
			limit_page_length=PICK_LIST_HISTORY_FETCH_CAP,
		)
		shortage_pick_lists = _open_shortage_pick_lists([r.name for r in wide])
		matching = [r for r in wide if _pick_list_bucket(r, shortage_pick_lists) == status]
		total = len(matching)
		page_rows = matching[start : start + page_length]
	else:
		page_rows = frappe.get_list(
			"Pick List",
			filters=filters,
			or_filters=or_filters,
			fields=PICK_LIST_HISTORY_FIELDS,
			order_by="creation desc",
			limit_start=start,
			limit_page_length=page_length,
		)
		total = len(
			frappe.get_list("Pick List", filters=filters, or_filters=or_filters, pluck="name")
		)
		shortage_pick_lists = _open_shortage_pick_lists([r.name for r in page_rows])

	names = [r.name for r in page_rows]

	line_counts, qty_requerida, qty_alistada, sales_order_by_pick_list = {}, {}, {}, {}
	if names:
		for row in frappe.get_all(
			"Pick List Item",
			filters={"parent": ["in", names]},
			fields=["parent", "sales_order", "stock_qty", "picked_qty"],
		):
			line_counts[row.parent] = line_counts.get(row.parent, 0) + 1
			qty_requerida[row.parent] = qty_requerida.get(row.parent, 0) + flt(row.stock_qty)
			qty_alistada[row.parent] = qty_alistada.get(row.parent, 0) + flt(row.picked_qty)
			if row.sales_order and row.parent not in sales_order_by_pick_list:
				sales_order_by_pick_list[row.parent] = row.sales_order

	shortage_counts = {}
	if names:
		for row in frappe.get_list(
			"Reporte de Faltante",
			filters={"pick_list": ["in", names]},
			fields=["pick_list", {"COUNT": "name", "as": "cnt"}],
			group_by="pick_list",
		):
			shortage_counts[row.pick_list] = row.cnt

	commercial_name_cache = {}

	def _commercial_name(sales_order):
		if not sales_order:
			return None
		if sales_order not in commercial_name_cache:
			commercial_name_cache[sales_order] = root_commercial_name(sales_order)
		return commercial_name_cache[sales_order]

	results = []
	for pl in page_rows:
		sales_order = sales_order_by_pick_list.get(pl.name)
		bucket = _pick_list_bucket(pl, shortage_pick_lists)
		results.append(
			{
				"name": pl.name,
				"docstatus": pl.docstatus,
				"state": bucket,
				"is_completed": bucket == "listos" and pl.status == "Completed",
				"purpose": pl.purpose,
				"parent_warehouse": pl.parent_warehouse,
				"customer": pl.customer,
				"sales_order": sales_order,
				"commercial_name": _commercial_name(sales_order),
				"item_count": line_counts.get(pl.name, 0),
				"qty_requerida": qty_requerida.get(pl.name, 0.0),
				"qty_alistada": qty_alistada.get(pl.name, 0.0),
				"shortage_count": shortage_counts.get(pl.name, 0),
				"fg_started_by": pl.fg_started_by,
				"fg_started_on": pl.fg_started_on,
				"modified": pl.modified,
				"modified_by": pl.modified_by,
			}
		)

	return {"pick_lists": results, "total": total}


SHORTAGE_CENTER_STATUSES = (OPEN_STATUS, IN_PROGRESS_STATUS, RESOLVED_STATUS)


@frappe.whitelist()
def get_shortage_center_summary():
	"""KPI row for the Centro de Faltantes: Faltantes abiertos / En
	proceso / Compras recibidas hoy / Resueltos hoy. "Compras recibidas
	hoy" counts submitted Material Receipt Stock Entries linked via
	fg_shortage_report (Commit 22.8) with posting_date today -- the same
	native relation get_shortage_center()/_bulk_received_qty() below use,
	never a parallel count. "Resueltos hoy" uses `modified` as the closest
	native proxy for "when it became Resuelto" (Reporte de Faltante has no
	dedicated resolved_on field, and this app's own convention -- see
	receive_shortage_purchase()'s docstring -- is to reuse a native field
	rather than invent one for this)."""
	_require_login()
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	abiertos = len(frappe.get_list("Reporte de Faltante", filters={"status": OPEN_STATUS}, pluck="name"))
	en_proceso = len(
		frappe.get_list("Reporte de Faltante", filters={"status": IN_PROGRESS_STATUS}, pluck="name")
	)

	today = nowdate()
	resueltos_hoy = len(
		frappe.get_list(
			"Reporte de Faltante",
			filters=[
				["status", "=", RESOLVED_STATUS],
				["modified", ">=", f"{today} 00:00:00"],
				["modified", "<=", f"{today} 23:59:59"],
			],
			pluck="name",
		)
	)

	compras_recibidas_hoy = 0
	if frappe.has_permission("Stock Entry", "read"):
		compras_recibidas_hoy = len(
			frappe.get_list(
				"Stock Entry",
				filters=[
					["purpose", "=", MATERIAL_RECEIPT_PURPOSE],
					["docstatus", "=", 1],
					["fg_shortage_report", "is", "set"],
					["posting_date", "=", today],
				],
				pluck="name",
			)
		)

	return {
		"abiertos": abiertos,
		"en_proceso": en_proceso,
		"compras_recibidas_hoy": compras_recibidas_hoy,
		"resueltos_hoy": resueltos_hoy,
	}


def _bulk_received_qty(report_names):
	"""received_qty for many Reporte de Faltante at once -- one query to
	Stock Entry (fg_shortage_report IN [...], docstatus=1) plus one to
	Stock Entry Detail (child table, same "parent already permission-
	filtered above" reasoning _shortage_receipts() and get_queue() both
	already document), never one frappe.get_doc() per report the way
	_shortage_receipts() does for a single report's own detail view (that
	function stays as-is, unchanged, for get_shortage_purchase_status()
	-- this is a separate, list-oriented aggregate, not a replacement)."""
	if not report_names:
		return {}
	entries = frappe.get_list(
		"Stock Entry",
		filters={
			"fg_shortage_report": ["in", report_names],
			"purpose": MATERIAL_RECEIPT_PURPOSE,
			"docstatus": 1,
		},
		fields=["name", "fg_shortage_report"],
	)
	if not entries:
		return {}
	entry_to_report = {e.name: e.fg_shortage_report for e in entries}
	detail_rows = frappe.get_all(
		"Stock Entry Detail", filters={"parent": ["in", list(entry_to_report)]}, fields=["parent", "qty"]
	)
	totals = {}
	for row in detail_rows:
		report_name = entry_to_report.get(row.parent)
		if report_name:
			totals[report_name] = totals.get(report_name, 0.0) + flt(row.qty)
	return totals


@frappe.whitelist()
def get_shortage_center(status=None, txt=None, start=0, page_length=20):
	"""Paginated Reporte de Faltante list for the Centro de Faltantes,
	with received_qty/remaining_qty per row (Commit 22.8's own relation,
	via _bulk_received_qty() -- never recomputed differently here).

	status: "Abierto" | "En Proceso" | "Resuelto", or falsy/unrecognized
	for "TODOS" -- a plain native column, so this is a real DB-level
	filter+pagination, no bounded-fetch-then-filter needed (unlike Pick
	List's own computed state)."""
	_require_login()
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	start, page_length = _clamp_pagination(start, page_length, MAX_PAGE_LENGTH)
	txt = (txt or "").strip()
	status = status if status in SHORTAGE_CENTER_STATUSES else None

	filters = {"status": status} if status else {}
	or_filters = None
	if txt:
		or_filters = [
			["name", "like", f"%{txt}%"],
			["item_code", "like", f"%{txt}%"],
			["sales_order", "like", f"%{txt}%"],
		]

	rows = frappe.get_list(
		"Reporte de Faltante",
		filters=filters,
		or_filters=or_filters,
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
			"reported_on",
		],
		order_by="reported_on desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	total = len(frappe.get_list("Reporte de Faltante", filters=filters, or_filters=or_filters, pluck="name"))

	names = [r.name for r in rows]
	received_by_report = _bulk_received_qty(names) if frappe.has_permission("Stock Entry", "read") else {}

	pick_list_cache = {}
	item_name_by_code = _batch_item_names([r.item_code for r in rows])
	results = []
	for r in rows:
		received_qty = received_by_report.get(r.name, 0.0)
		remaining_qty = max(flt(r.qty_faltante) - received_qty, 0.0)
		results.append(
			{
				"name": r.name,
				"item_code": r.item_code,
				"item_name": _resolve_item_name(
					r.pick_list, r.pick_list_item, r.item_code, pick_list_cache, item_name_by_code
				),
				"warehouse": r.warehouse,
				"sales_order": r.sales_order,
				"qty_solicitada": r.qty_solicitada,
				"qty_disponible": r.qty_disponible,
				"qty_faltante": r.qty_faltante,
				"shortage_reason": r.shortage_reason,
				"status": r.status,
				"received_qty": received_qty,
				"remaining_qty": remaining_qty,
			}
		)

	return {"reports": results, "total": total}


@frappe.whitelist()
def get_warehouse_summary():
	"""Almacenes view: top metrics + one card per real, operational
	Warehouse. Only non-group, non-disabled Warehouses -- a Warehouse
	Group organizes the tree natively but is never itself an inventory
	leaf (ERPNext never posts a Bin row against one). Scoped to this
	site's own default Company (erpnext.get_default_company(), never a
	hardcoded name) -- this site's database also carries other companies'
	demo Warehouses (ERPNext's own stock demo fixtures, e.g. "Finished
	Goods - _TC"), which are not real for Fabrigray and must never appear
	here. Read-only: never writes Bin, one grouped query for the
	per-Warehouse stats (never one per Warehouse), one grouped query for
	the global distinct-item count (never summed from the per-Warehouse
	rows, which would double-count an Item stocked in more than one
	Warehouse)."""
	_require_login()
	frappe.has_permission("Warehouse", "read", throw=True)
	frappe.has_permission("Bin", "read", throw=True)

	company = get_default_company()

	warehouses = frappe.get_list(
		"Warehouse",
		filters={"is_group": 0, "disabled": 0, "company": company},
		fields=["name", "warehouse_name"],
		order_by="warehouse_name asc",
		limit_page_length=0,
	)

	bin_stats = frappe.get_list(
		"Bin",
		filters={"actual_qty": [">", 0], "company": company},
		fields=[
			"warehouse",
			{"COUNT": "name", "as": "items_with_stock"},
			{"SUM": "actual_qty", "as": "total_qty"},
			{"SUM": "stock_value", "as": "total_value"},
		],
		group_by="warehouse",
		limit_page_length=0,
	)
	stats_by_warehouse = {r.warehouse: r for r in bin_stats}

	distinct_items_with_stock = len(
		frappe.get_list(
			"Bin",
			filters={"actual_qty": [">", 0], "company": company},
			fields=["item_code"],
			group_by="item_code",
			limit_page_length=0,
		)
	)

	rows = []
	total_units = 0.0
	total_value = 0.0
	for wh in warehouses:
		stats = stats_by_warehouse.get(wh.name)
		items_with_stock = cint(stats.items_with_stock) if stats else 0
		total_qty = flt(stats.total_qty) if stats else 0.0
		wh_value = flt(stats.total_value) if stats else 0.0
		total_units += total_qty
		total_value += wh_value
		rows.append(
			{
				"name": wh.name,
				"warehouse_name": wh.warehouse_name,
				"items_with_stock": items_with_stock,
				"total_qty": total_qty,
			}
		)

	return {
		"warehouses": rows,
		"active_warehouses": len(warehouses),
		"items_with_stock": distinct_items_with_stock,
		"total_units": total_units,
		# "Stock total distribuido" -- native Bin.stock_value (ERPNext's own
		# qty * valuation_rate, never recomputed here), distinct from
		# total_units (a quantity): the monetary value of inventory spread
		# across every real Warehouse.
		"total_stock_value": total_value,
	}


@frappe.whitelist()
def get_warehouse_items(warehouse, txt=None, start=0, page_length=50):
	"""Read-only per-Warehouse item breakdown for the Almacenes drill-down
	-- api.inventario's own get_inventory_items() sums actual_qty across
	every Warehouse (a different, already-answered question); this is the
	one-Warehouse view it doesn't provide. Bin.actual_qty is read exactly
	as ERPNext already computed it -- never recalculated, never written.
	Items with actual_qty<=0 in this Warehouse are excluded by default
	(qty=0 "no aparece", per this commit's own brief) -- there is no way
	to ask for them through this endpoint, on purpose (a different,
	broader question than "what does this Warehouse actually hold").

	One Bin query (bounded by how many distinct Items this one Warehouse
	actually holds, not the whole company's catalog) + one paginated Item
	query + one bulk Item Price lookup for exactly the page being
	returned -- never one query per Item.

	Scoped to this site's own default Company exactly like
	get_warehouse_summary() above (never just on that first screen): a
	Warehouse belonging to another company (this site's other, demo
	companies) is rejected outright, the same InvalidReceiptWarehouseError-
	style guard -- get_warehouse_summary()'s own list only ever offers
	real, own-Company Warehouses, but this endpoint is independently
	callable with an arbitrary Warehouse name and must not trust that the
	caller only ever passes what that list showed."""
	_require_login()
	company = get_default_company()
	_validate_own_company_warehouse(warehouse, company)
	if not warehouse:
		frappe.throw(_("Almacén requerido."))

	doc = frappe.get_doc("Warehouse", warehouse)
	doc.check_permission("read")
	frappe.has_permission("Bin", "read", throw=True)
	frappe.has_permission("Item", "read", throw=True)

	start, page_length = _clamp_pagination(start, page_length, MAX_PAGE_LENGTH_WAREHOUSE_ITEMS)
	txt = (txt or "").strip()

	bin_rows = frappe.get_list(
		"Bin",
		filters={"warehouse": warehouse, "actual_qty": [">", 0], "company": company},
		fields=["item_code", "actual_qty"],
		limit_page_length=0,
	)
	qty_by_item = {r.item_code: flt(r.actual_qty) for r in bin_rows}
	item_codes = list(qty_by_item.keys())

	if not item_codes:
		return {
			"warehouse": warehouse,
			"warehouse_name": doc.warehouse_name,
			"items": [],
			"total": 0,
			"total_products": 0,
			"total_qty": 0.0,
		}

	filters = [["name", "in", item_codes]]
	or_filters = [["item_code", "like", f"%{txt}%"], ["item_name", "like", f"%{txt}%"]] if txt else None

	page_items = frappe.get_list(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=["name as item_code", "item_name", "item_group", "stock_uom"],
		order_by="item_code asc",
		limit_start=start,
		limit_page_length=page_length,
	)
	total = len(frappe.get_list("Item", filters=filters, or_filters=or_filters, pluck="name"))

	rates = _selling_rates([r.item_code for r in page_items])

	items = [
		{
			"item_code": r.item_code,
			"item_name": r.item_name,
			"item_group": r.item_group,
			"stock_uom": r.stock_uom,
			"actual_qty": qty_by_item.get(r.item_code, 0.0),
			"selling_rate": rates.get(r.item_code),
		}
		for r in page_items
	]

	return {
		"warehouse": warehouse,
		"warehouse_name": doc.warehouse_name,
		"items": items,
		"total": total,
		"total_products": len(item_codes),
		"total_qty": sum(qty_by_item.values()),
	}
