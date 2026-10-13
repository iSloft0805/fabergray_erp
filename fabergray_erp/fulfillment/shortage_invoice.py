# -*- coding: utf-8 -*-
"""Facturación con faltantes -- the authorization a Jefe de Bodega gives
(api.jefe_bodega.validate_resolved_shortage_orders(), mode
"allow_invoice_with_shortage") so a DRAFT Pick List whose stock is not
complete can be invoiced in full while its picking stays pending.

The authorization lives on Pick List (three Custom Fields, read-only,
no_copy, allow_on_submit):
- fg_shortage_invoice_authorized (Check)
- fg_shortage_invoice_authorized_by (User)
- fg_shortage_invoice_authorized_on (Datetime)
They only change inside _authorization_write() (authorize / revoke here);
any other write -- Desk, frappe.client.set_value, a Bodega save -- is
refused by guard_shortage_invoice_pick_list().

It never moves stock: no submit, no Material Issue, no transferred_qty, no
picked_qty change. Facturación reads it through api.facturacion's own
single "facturable" criterion and billable-quantity function.

Once that draft is Facturado it is commercially frozen (see
is_invoiced_with_shortage()):
- its rows can no longer be added, removed or re-pointed (item, warehouse,
  qty/stock_qty, conversion, Sales Order line), and picked_qty may only go
  up -- never below what was already picked;
- it cannot be deleted;
- it can only be submitted (COMPLETAR PEDIDO) with EVERY line fully
  picked (picked_qty == stock_qty). A partial finish would let the
  remainder builder hand the missing units out again as a new, separately
  invoiceable Pick List -- the same units billed twice. While the draft is
  open its rows claim their whole stock_qty, so no remainder is built for
  them; once fully submitted, picked == stock leaves nothing to rebuild.
Revocation is allowed only before it is Facturado."""

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

FIELD_AUTHORIZED = "fg_shortage_invoice_authorized"
FIELD_AUTHORIZED_BY = "fg_shortage_invoice_authorized_by"
FIELD_AUTHORIZED_ON = "fg_shortage_invoice_authorized_on"
AUTHORIZATION_FIELDS = (FIELD_AUTHORIZED, FIELD_AUTHORIZED_BY, FIELD_AUTHORIZED_ON)

ROLES = ("Jefe de Bodega", "System Manager")

#: Same value as api.facturacion.FG_INVOICING_FACTURADO -- not imported:
#: api.facturacion imports api.bodega, which imports this module.
FACTURADO = "Facturado"

#: Row fields that make up what was invoiced: frozen once Facturado.
FROZEN_ROW_FIELDS = (
	"item_code",
	"warehouse",
	"qty",
	"stock_qty",
	"conversion_factor",
	"uom",
	"stock_uom",
	"sales_order",
	"sales_order_item",
)

MSG_INVOICED_WITH_SHORTAGE = (
	"Este pedido ya fue facturado con faltantes. Debe completar físicamente todas las cantidades "
	"antes de finalizar el alistamiento."
)

MSG_FROZEN = "Este pedido ya fue facturado con faltantes: sus líneas están congeladas."

_WRITE_FLAG = "fg_shortage_invoice_write"


class ShortageInvoiceFrozenError(frappe.ValidationError):
	pass


class ShortageInvoicePartialCompletionError(frappe.ValidationError):
	pass


class ShortageInvoiceRevokeError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def is_authorized(pl):
	return bool(cint(pl.get(FIELD_AUTHORIZED)))


def bills_ordered_qty(pl):
	"""A draft authorized to be invoiced with shortages bills what was
	ordered (stock_qty); every other Pick List bills what was picked."""
	return pl.docstatus == 0 and is_authorized(pl)


def is_invoiced_with_shortage(pl):
	"""Draft, authorized and already Facturado: commercially frozen."""
	return pl.docstatus == 0 and is_authorized(pl) and pl.get("fg_invoicing_status") == FACTURADO


def pending_lines(pl):
	"""[(row, qty still to pick)] -- rows whose picked_qty is below stock_qty."""
	lines = []
	for row in pl.get("locations") or []:
		precision = row.precision("picked_qty") or 6
		pending = flt(flt(row.stock_qty) - flt(row.picked_qty), precision)
		if pending > 0:
			lines.append((row, pending))
	return lines


@contextmanager
def _authorization_write():
	previous = frappe.flags.get(_WRITE_FLAG)
	frappe.flags[_WRITE_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[_WRITE_FLAG] = previous


# ---------------------------------------------------------------------------
# Writes (callers have already checked the role)
# ---------------------------------------------------------------------------


def set_authorization(pl):
	"""Authorize the locked, in-memory draft and save it. System action: the
	caller (Jefe de Bodega / System Manager) holds no Pick List write
	permission. Nothing else changes -- the fields are header fields, the
	rows are not touched."""
	# Same reason as api.bodega._lock_manual_picking(): a later save must
	# never rebuild the rows Facturación is going to bill.
	if not pl.pick_manually:
		pl.pick_manually = 1
	pl.set(FIELD_AUTHORIZED, 1)
	pl.set(FIELD_AUTHORIZED_BY, frappe.session.user)
	pl.set(FIELD_AUTHORIZED_ON, now_datetime())
	pl.flags.ignore_permissions = True
	with _authorization_write():
		pl.save()


def revoke(pick_list):
	"""Clear the authorization of a Pick List not yet Facturado. Returns the
	Pick List name. picked_qty and stock are never touched."""
	frappe.only_for(ROLES)
	pl = frappe.get_doc("Pick List", pick_list, for_update=True)
	pl.check_permission("read")
	if not is_authorized(pl):
		frappe.throw(
			_("El Pick List {0} no tiene autorización para facturar con faltantes.").format(pl.name),
			ShortageInvoiceRevokeError,
		)
	if pl.get("fg_invoicing_status") == FACTURADO:
		frappe.throw(
			_("El Pick List {0} ya fue facturado; su autorización con faltantes no puede revocarse.").format(
				pl.name
			),
			ShortageInvoiceRevokeError,
		)
	for fieldname in AUTHORIZATION_FIELDS:
		pl.set(fieldname, 0 if fieldname == FIELD_AUTHORIZED else None)
	pl.flags.ignore_permissions = True
	with _authorization_write():
		pl.save()
	pl.add_comment(
		"Comment",
		_("Autorización para facturar con faltantes revocada por {0} el {1}.").format(
			frappe.session.user, frappe.utils.format_datetime(now_datetime())
		),
	)
	return pl.name


# ---------------------------------------------------------------------------
# Guards (hooks.py, Pick List doc_events)
# ---------------------------------------------------------------------------


def guard_shortage_invoice_pick_list(doc, method=None):
	"""`validate` + `before_update_after_submit`:
	1. the three authorization fields only change inside
	   _authorization_write();
	2. a draft authorized and Facturado BEFORE this save is frozen: same
	   rows, same contractual fields, picked_qty never decreases."""
	before = doc.get_doc_before_save()

	if not frappe.flags.get(_WRITE_FLAG):
		for fieldname in AUTHORIZATION_FIELDS:
			old = before.get(fieldname) if before else None
			new = doc.get(fieldname)
			if fieldname == FIELD_AUTHORIZED:
				old, new = cint(old), cint(new)
			else:
				old, new = old or None, new or None
				if old is not None and new is not None:
					old, new = str(old), str(new)
			if old != new:
				frappe.throw(
					_("La autorización para facturar con faltantes solo puede cambiarse desde Jefe de Bodega."),
					frappe.PermissionError,
				)

	if not before or not is_invoiced_with_shortage(before):
		return

	old_rows = {row.name: row for row in before.get("locations") or []}
	new_rows = {row.name: row for row in doc.get("locations") or []}
	if set(old_rows) != set(new_rows):
		frappe.throw(_("{0} No se pueden añadir ni quitar líneas.").format(_(MSG_FROZEN)), ShortageInvoiceFrozenError)
	for name, row in new_rows.items():
		old = old_rows[name]
		for fieldname in FROZEN_ROW_FIELDS:
			old_value, new_value = old.get(fieldname), row.get(fieldname)
			if isinstance(old_value, (int, float)) or isinstance(new_value, (int, float)):
				changed = flt(old_value, 6) != flt(new_value, 6)
			else:
				changed = (old_value or None) != (new_value or None)
			if changed:
				frappe.throw(
					_("{0} No se puede cambiar {1} de la línea {2}.").format(_(MSG_FROZEN), fieldname, row.idx),
					ShortageInvoiceFrozenError,
				)
		if flt(row.picked_qty, 6) < flt(old.picked_qty, 6):
			frappe.throw(
				_("{0} La cantidad alistada de la línea {1} no puede disminuir.").format(_(MSG_FROZEN), row.idx),
				ShortageInvoiceFrozenError,
			)



def assert_can_finish(pl):
	"""COMPLETAR PEDIDO / submit: a draft invoiced with shortages only
	finishes with every line fully picked."""
	if is_invoiced_with_shortage(pl) and pending_lines(pl):
		frappe.throw(_(MSG_INVOICED_WITH_SHORTAGE), ShortageInvoicePartialCompletionError)


def guard_invoiced_shortage_submit(doc, method=None):
	"""`before_submit`: same rule as assert_can_finish(), whatever path
	submits (defense in depth behind COMPLETAR PEDIDO's own check). Frappe
	has already set docstatus = 1 when before_submit runs, so the draft
	check of is_invoiced_with_shortage() cannot be used here."""
	if is_authorized(doc) and doc.get("fg_invoicing_status") == FACTURADO and pending_lines(doc):
		frappe.throw(_(MSG_INVOICED_WITH_SHORTAGE), ShortageInvoicePartialCompletionError)


def guard_invoiced_pick_list_delete(doc, method=None):
	"""`on_trash`: a draft invoiced with shortages is never deleted -- the
	invoice would be left without its picking."""
	if is_authorized(doc) and doc.get("fg_invoicing_status") == FACTURADO:
		frappe.throw(
			_("El Pick List {0} ya fue facturado; no puede eliminarse.").format(doc.name),
			ShortageInvoiceFrozenError,
		)
