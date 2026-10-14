# -*- coding: utf-8 -*-
"""Global Bodega policy -- "Completar pedidos sin importar inventario".

Stored server-side in the Single DocType "Fabrigray Bodega Settings"
(force_complete_without_stock, track_changes=1). Never Company, site
config or the browser.

- force_complete_enabled(): FRESH read straight from tabSingles, never the
  document/value cache -- COMPLETAR PEDIDO must see an OFF the moment the
  Jefe sets it.
- set_force_complete(enabled): Jefe de Bodega / System Manager only,
  `enabled` validated strictly, the Singles row locked, then a real save
  (Version log: user, date/time, previous and new value) plus a readable
  Comment. Any other write of the field is refused by the controller.
- state(): what the Bodega / Jefe de Bodega screens show.

Effect (api.bodega._complete_pick_list(), Bodega's COMPLETAR PEDIDO only):
ON skips the "not enough Bin.actual_qty" refusal and lets that Pick List's
own Material Issue take the stock negative (stock_issue_service.
create_stock_issue(allow_shortage=True)). Nothing else changes: not Stock
Settings, not any Item, not any other Stock Entry, not the "Validar pedidos
con faltantes resueltos" button."""

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

DOCTYPE = "Fabrigray Bodega Settings"
FIELD = "force_complete_without_stock"
CHANGE_ROLES = ("Jefe de Bodega", "System Manager")

LABEL_ON = "COMPLETAR SIN INVENTARIO: ACTIVADO"
LABEL_OFF = "COMPLETAR SIN INVENTARIO: DESACTIVADO"
WARNING_ON = (
	"Los pedidos podrán finalizarse aunque no exista inventario suficiente. "
	"Esto puede generar existencias negativas."
)

_WRITE_FLAG = "fg_bodega_settings_write"
_TRUE = {"1", "true"}
_FALSE = {"0", "false"}


class InvalidForceCompleteValueError(frappe.ValidationError):
	pass


def force_complete_enabled(for_update=False):
	"""Current value, read from tabSingles (no cache). `for_update` locks the
	row for the caller's transaction."""
	query = "select value from `tabSingles` where doctype = %s and field = %s"
	if for_update:
		query += " for update"
	row = frappe.db.sql(query, (DOCTYPE, FIELD))
	return bool(cint(row[0][0])) if row else False


def can_change(user=None):
	roles = set(frappe.get_roles(user or frappe.session.user))
	return bool(roles & set(CHANGE_ROLES)) or (user or frappe.session.user) == "Administrator"


def state():
	enabled = force_complete_enabled()
	return {
		"enabled": enabled,
		"label": _(LABEL_ON if enabled else LABEL_OFF),
		"warning": _(WARNING_ON) if enabled else None,
		"can_change": can_change(),
	}


def parse_enabled(value):
	"""Strict: a bool, the ints 0/1, or the strings "0"/"1"/"true"/"false"."""
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str):
		normalized = value.strip().lower()
		if normalized in _TRUE:
			return True
		if normalized in _FALSE:
			return False
	frappe.throw(_("Valor no válido para el modo 'Completar pedidos sin importar inventario'."), InvalidForceCompleteValueError)


@contextmanager
def _settings_write():
	previous = frappe.flags.get(_WRITE_FLAG)
	frappe.flags[_WRITE_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[_WRITE_FLAG] = previous


def set_force_complete(enabled):
	"""Change the policy. Returns state() plus `previous` and `changed`."""
	frappe.only_for(CHANGE_ROLES)
	new = parse_enabled(enabled)

	previous = force_complete_enabled(for_update=True)
	if previous == new:
		return {**state(), "previous": previous, "changed": False}

	doc = frappe.get_doc(DOCTYPE)
	doc.check_permission("write")
	doc.set(FIELD, int(new))
	with _settings_write():
		doc.save(ignore_version=False)  # Version log: who, when, previous -> new
	doc.add_comment(
		"Comment",
		_("Modo 'Completar pedidos sin importar inventario' {0} por {1} el {2} (antes: {3}).").format(
			_("ACTIVADO") if new else _("DESACTIVADO"),
			frappe.session.user,
			frappe.utils.format_datetime(now_datetime()),
			_("ACTIVADO") if previous else _("DESACTIVADO"),
		),
	)
	return {**state(), "previous": previous, "changed": True}


def guard_settings_write(doc):
	"""Controller validate: the policy only changes through set_force_complete()."""
	if frappe.flags.get(_WRITE_FLAG):
		return
	before = doc.get_doc_before_save()
	old = cint(before.get(FIELD)) if before else 0
	if cint(doc.get(FIELD)) != old:
		frappe.throw(
			_("El modo 'Completar pedidos sin importar inventario' solo se cambia desde Bodega / Jefe de Bodega."),
			frappe.PermissionError,
		)
