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
from frappe.utils import flt

from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults

from fabergray_erp.api.bodega import OPEN_SHORTAGE_STATUSES, _require_login, get_queue
from fabergray_erp.api.inventario import PRICE_LIST_BUYING, _upsert_item_price

RESOLVED_STATUS = "Resuelto"
IN_PROGRESS_STATUS = "En Proceso"
MATERIAL_RECEIPT_PURPOSE = "Material Receipt"

DEFAULT_RESOLUTION_NOTE = _("Faltante completado mediante recepción de compra.")


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
	for report in reports:
		report["item_name"] = _resolve_item_name(
			report.pick_list, report.pick_list_item, report.item_code, pick_list_cache
		)
		report["reported_by_fullname"] = frappe.utils.get_fullname(report.reported_by)

	return reports


def _resolve_item_name(pick_list, pick_list_item, item_code, pick_list_cache=None):
	"""Reporte de Faltante does not store item_name, only item_code. Rather
	than querying the Item doctype (Jefe de Bodega has no permission on
	Item) or using frappe.get_all on the Pick List Item child table
	(forbidden by policy for this API), the item name is read off the
	linked Pick List document itself -- after confirming read access to
	that specific Pick List -- exactly the same pattern _get_pick_list_row()
	already uses in api/bodega.py. `pick_list_cache` (optional, a plain
	dict) lets a caller iterating many reports load the same Pick List only
	once; get_shortage_purchase_status() below (a single report) omits it."""
	if not (pick_list and pick_list_item):
		return item_code

	cache = pick_list_cache if pick_list_cache is not None else {}
	try:
		if pick_list not in cache:
			doc = frappe.get_doc("Pick List", pick_list)
			doc.check_permission("read")
			cache[pick_list] = doc
		rows = cache[pick_list].get("locations", {"name": pick_list_item})
		return rows[0].item_name if rows else item_code
	except frappe.PermissionError:
		# Report references a Pick List this user can no longer read (e.g.
		# outside their Warehouse User Permission). Fall back to item_code
		# rather than failing the whole call or fetching Item data without
		# permission.
		return item_code


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
