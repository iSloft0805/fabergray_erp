# -*- coding: utf-8 -*-
"""Jefe de Bodega -- "Validar pedidos con faltantes resueltos".

Completes, in bulk, the picking that was waiting on Reportes de Faltante
resolved TODAY, but only where the stock is really there. The report being
Resuelto is never taken as proof of stock: availability is re-read under
lock for every Pick List.

Scope (decided, not inferred):
- reports whose status changed to "Resuelto" today. Reporte de Faltante
  has no resolved_on field; the transition is read from Frappe's Version
  log (track_changes=1, every resolving code path saves the document) with
  api.reporte_faltantes._resolution_dates() -- the same source the PDF
  report already uses. No historical sweep.
- target Pick List per report:
  * original Pick List still a draft -> that same Pick List;
  * original submitted and fully issued -> its remainder Pick List (draft
    rows of the same Sales Order line), created through
    remainder_service.ensure_remaining_pick_list() when missing
    (idempotent: it subtracts what open Pick Lists already claim);
  * original submitted WITHOUT a complete Material Issue (legacy) -> it is
    sent to COMPLETAR PEDIDO's own guard, which refuses with
    IncompleteStockIssueError: ERROR / revisión administrativa, never
    repaired here;
  * cancelled -> ignored.

Per Pick List (one transaction each, committed on its own, deadlock retry
scoped to it): api.bodega._complete_pick_list() -- the very body of
COMPLETAR PEDIDO -- with a `prepare` step that, on the locked and re-read
draft, refuses unless EVERY line can end up fully picked:
- no Reporte de Faltante Abierto/En Proceso on the Pick List;
- every line not covered by a report resolved today already fully picked
  by Bodega;
- no line to fill in a non-picking warehouse (Devoluciones/Cuarentena);
- operational stock: Bin.actual_qty minus what OTHER draft Pick Lists hold
  physically picked and not yet issued, must cover every pending unit of
  this Pick List per item/warehouse.
Only then are the resolved lines filled up to their requested stock_qty
(and fg_started_by/fg_started_on set to the caller when nobody started the
Pick List). Then the unchanged COMPLETAR PEDIDO path runs: its guards,
validate_stock_for_issue(), submit + ONE Material Issue under its savepoint.
Reportes de Faltante are never modified.
"""

import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, nowdate

from fabergray_erp.api.bodega import OPEN_SHORTAGE_STATUSES, _complete_pick_list, _lock_manual_picking
from fabergray_erp.fulfillment.remainder_service import ensure_remaining_pick_list
from fabergray_erp.fulfillment.stock_issue_service import (
	IncompleteStockIssueError,
	InsufficientStockForIssueError,
	pending_issue_rows,
)
from fabergray_erp.sales_order_naming import root_commercial_name
from fabergray_erp.warehouses import non_picking_warehouses

ROLES = ("Jefe de Bodega", "System Manager")
RESOLVED_STATUS = "Resuelto"
MAX_PICK_LISTS_PER_RUN = 50

STATUS_COMPLETED = "COMPLETADO"
STATUS_STILL_SHORT = "AÚN CON FALTANTES"
STATUS_ALREADY_COMPLETED = "YA COMPLETADO"
STATUS_ERROR = "ERROR"

#: Structured detail other paths leave on frappe.local.response; a refused
#: Pick List must not leak it into the bulk response.
_RESPONSE_SIDE_CHANNELS = ("fg_stock_shortages", "fg_incomplete_stock_issue")


class ResolvedShortageStillShortError(frappe.ValidationError):
	"""The Pick List cannot be fully completed yet; nothing was written.
	`shortages` = [{item_code, warehouse, required_qty, available_qty,
	shortage_qty}] (same shape as validate_stock_for_issue()), `reasons` =
	readable non-stock causes."""

	shortages = ()
	reasons = ()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_resolved_shortage_orders(limit=MAX_PICK_LISTS_PER_RUN):
	"""Process up to `limit` Pick Lists; returns {summary, results,
	remaining_count}. Commits per Pick List (see module docstring)."""
	frappe.only_for(ROLES)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	targets, early_results = _collect_targets(_reports_resolved_today())
	names = list(targets)
	batch, remaining = names[:limit], names[limit:]

	results = list(early_results)
	for name in batch:
		results.append(_process_isolated(name, targets[name]))

	summary = {
		"completed": sum(1 for r in results if r["status"] == STATUS_COMPLETED),
		"still_short": sum(1 for r in results if r["status"] == STATUS_STILL_SHORT),
		"already_completed": sum(1 for r in results if r["status"] == STATUS_ALREADY_COMPLETED),
		"errors": sum(1 for r in results if r["status"] == STATUS_ERROR),
	}
	return {"summary": summary, "results": results, "remaining_count": len(remaining)}


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _reports_resolved_today():
	"""Reportes de Faltante (readable by the caller) whose last transition to
	Resuelto happened today, site date, with their Pick List link."""
	from fabergray_erp.api.reporte_faltantes import _resolution_dates

	today = getdate(nowdate())
	version = frappe.qb.DocType("Version")
	touched_today = (
		frappe.qb.from_(version)
		.select(version.docname)
		.distinct()
		.where(version.ref_doctype == "Reporte de Faltante")
		.where(version.creation >= f"{today.isoformat()} 00:00:00")
		.where(version.creation <= f"{today.isoformat()} 23:59:59.999999")
		.run(pluck=True)
	)
	if not touched_today:
		return []

	resolved_on = _resolution_dates(touched_today)
	names = sorted(name for name, ts in resolved_on.items() if getdate(ts) == today)
	if not names:
		return []

	return frappe.get_list(
		"Reporte de Faltante",
		filters=[
			["name", "in", names],
			["status", "=", RESOLVED_STATUS],
			["pick_list", "is", "set"],
			["pick_list_item", "is", "set"],
		],
		fields=["name", "pick_list", "pick_list_item", "sales_order", "sales_order_item"],
		order_by="name asc",
		limit_page_length=0,
	)


def _collect_targets(reports):
	"""({pick_list: {"rows": set(Pick List Item names to fill), "reports":
	[...]}} in first-seen order, [early ERROR results]). Remainder Pick
	Lists are created here, each in its own committed transaction, BEFORE
	any Pick List is completed: ensure_remaining_pick_list() locks Sales
	Order -> Pick List Item, a different order than COMPLETAR PEDIDO's
	Bin -> Pick List, so the two never nest."""
	targets = {}
	errors = []

	def add(pick_list, rows, report):
		entry = targets.setdefault(pick_list, {"rows": set(), "reports": []})
		entry["rows"].update(rows)
		entry["reports"].append(report.name)

	for report in reports:
		docstatus = frappe.db.get_value("Pick List", report.pick_list, "docstatus")
		if docstatus == 0:
			add(report.pick_list, {report.pick_list_item}, report)
			continue
		if docstatus != 1:
			continue  # cancelled / deleted

		original = frappe.get_doc("Pick List", report.pick_list)
		if pending_issue_rows(original):
			# Legacy: submitted without a complete Material Issue. COMPLETAR
			# PEDIDO's guard refuses it (ERROR); never repaired here.
			add(report.pick_list, set(), report)
			continue

		if not report.sales_order_item:
			continue

		remainder_rows = _draft_remainder_rows(report)
		if not remainder_rows:
			try:
				_retrying_on_deadlock(ensure_remaining_pick_list)(report.name)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				if report.pick_list not in {r["pick_list"] for r in errors}:
					errors.append(_result(report.pick_list, STATUS_ERROR, detail=_error_text(e)))
				continue
			remainder_rows = _draft_remainder_rows(report)

		for pick_list, rows in remainder_rows.items():
			add(pick_list, rows, report)

	return targets, errors


def _draft_remainder_rows(report):
	"""{draft Pick List: {row names}} holding this report's Sales Order line
	outside its original Pick List."""
	rows = frappe.get_all(
		"Pick List Item",
		filters={
			"sales_order_item": report.sales_order_item,
			"parenttype": "Pick List",
			"docstatus": 0,
			"parent": ["!=", report.pick_list],
		},
		fields=["name", "parent"],
		order_by="parent asc, idx asc",
	)
	by_pick_list = defaultdict(set)
	for row in rows:
		by_pick_list[row.parent].add(row.name)
	return dict(by_pick_list)


# ---------------------------------------------------------------------------
# One Pick List, isolated
# ---------------------------------------------------------------------------


def _process_isolated(pick_list, target):
	"""One Pick List in its own transaction: commit on success, rollback on
	any refusal, and never let its messages leak into the bulk response."""
	message_log = frappe.local.message_log
	logged = len(message_log)
	try:
		result = _retrying_on_deadlock(_process_pick_list)(pick_list, target)
		frappe.db.commit()
		return result
	except ResolvedShortageStillShortError as e:
		frappe.db.rollback()
		return _result(pick_list, STATUS_STILL_SHORT, shortages=list(e.shortages), detail="; ".join(e.reasons))
	except InsufficientStockForIssueError as e:
		frappe.db.rollback()
		return _result(pick_list, STATUS_STILL_SHORT, shortages=list(getattr(e, "shortages", ()) or ()))
	except IncompleteStockIssueError as e:
		frappe.db.rollback()
		return _result(
			pick_list,
			STATUS_ERROR,
			detail=_("Finalizado sin salida de inventario completa: requiere revisión administrativa. {0}").format(
				_error_text(e)
			),
		)
	except Exception as e:
		frappe.db.rollback()
		return _result(pick_list, STATUS_ERROR, detail=_error_text(e))
	finally:
		del message_log[logged:]
		for key in _RESPONSE_SIDE_CHANNELS:
			frappe.local.response.pop(key, None)


def _process_pick_list(pick_list, target):
	frappe.get_doc("Pick List", pick_list).check_permission("read")

	outcome = _complete_pick_list(
		pick_list, prepare=lambda pl: _prepare(pl, target["rows"]), system_action=True
	)
	if outcome["already_completed"]:
		return _result(pick_list, STATUS_ALREADY_COMPLETED, stock_entry=outcome["stock_entry"])

	frappe.get_doc("Pick List", pick_list).add_comment(
		"Comment",
		_("Completado por {0} con «Validar pedidos con faltantes resueltos» (faltantes: {1}).").format(
			frappe.session.user, ", ".join(target["reports"])
		),
	)
	return _result(pick_list, STATUS_COMPLETED, stock_entry=outcome["stock_entry"])


def _prepare(pl, rows_to_fill):
	"""COMPLETAR PEDIDO's `prepare` hook: decide on the locked, re-read draft
	and only then change it in memory. Raises ResolvedShortageStillShortError
	(nothing changed) unless every line can end up fully picked."""
	precision = frappe.get_precision("Pick List Item", "picked_qty") or 6
	locations = pl.get("locations") or []
	reasons = []

	open_reports = _open_reports_for(pl)
	if open_reports:
		reasons.append(
			_("Faltantes aún abiertos o en proceso: {0}").format(", ".join(sorted(open_reports)))
		)

	excluded = non_picking_warehouses(pl.company)
	target_qty = {}
	for row in locations:
		requested = flt(row.stock_qty, precision)
		if row.name in rows_to_fill:
			if row.warehouse in excluded:
				reasons.append(
					_("Fila {0} ({1}): el almacén {2} no es de alistamiento.").format(row.idx, row.item_code, row.warehouse)
				)
			target_qty[row.name] = max(requested, flt(row.picked_qty, precision))
		elif flt(row.picked_qty, precision) < requested:
			reasons.append(
				_("Fila {0} ({1}): Bodega aún no la alista por completo ({2} de {3}).").format(
					row.idx, row.item_code, flt(row.picked_qty, precision), requested
				)
			)

	shortages = _operational_shortages(pl, target_qty)

	if reasons or shortages:
		exc = ResolvedShortageStillShortError(
			"; ".join(reasons) or _("No hay stock suficiente para completar el pedido.")
		)
		exc.shortages = shortages
		exc.reasons = reasons
		raise exc

	for row in locations:
		if row.name in target_qty:
			row.picked_qty = target_qty[row.name]
	_lock_manual_picking(pl)
	if not pl.fg_started_by:
		pl.fg_started_by = frappe.session.user
		pl.fg_started_on = now_datetime()


def _open_reports_for(pl):
	"""Reportes Abierto/En Proceso on this Pick List or any of its rows --
	frappe.get_all on purpose: a guard must see them all, not only the ones
	the caller's warehouse permissions happen to show."""
	row_names = [row.name for row in pl.get("locations") or []]
	found = set(
		frappe.get_all(
			"Reporte de Faltante",
			filters={"pick_list": pl.name, "status": ["in", OPEN_SHORTAGE_STATUSES]},
			pluck="name",
		)
	)
	if row_names:
		found.update(
			frappe.get_all(
				"Reporte de Faltante",
				filters={"pick_list_item": ["in", row_names], "status": ["in", OPEN_SHORTAGE_STATUSES]},
				pluck="name",
			)
		)
	return found


def _operational_shortages(pl, target_qty):
	"""Per item/warehouse of this Pick List: what it will issue once filled
	(picked - delivered - transferred, with the target picked_qty) against
	Bin.actual_qty minus what OTHER draft Pick Lists hold physically picked
	and not yet issued. Submitted/cancelled Pick Lists, issued units and
	rows with nothing pending never count as held. Bin rows are already
	locked by COMPLETAR PEDIDO (lock_stock_for_pick_list())."""
	required = defaultdict(float)
	for row in pl.get("locations") or []:
		picked = target_qty.get(row.name, flt(row.picked_qty))
		pending = picked - flt(row.delivered_qty) - flt(row.transferred_qty)
		if pending > 0:
			required[(row.item_code, row.warehouse)] += pending

	precision = frappe.get_precision("Bin", "actual_qty") or 6
	shortages = []
	for (item_code, warehouse), qty in sorted(required.items()):
		actual = flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty", for_update=True)
		)
		held = _held_by_other_draft_pick_lists(item_code, warehouse, pl.name)
		available = flt(max(actual - held, 0), precision)
		required_qty = flt(qty, precision)
		if required_qty > available:
			shortages.append(
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"required_qty": required_qty,
					"available_qty": available,
					"shortage_qty": flt(required_qty - available, precision),
				}
			)
	return shortages


def _held_by_other_draft_pick_lists(item_code, warehouse, exclude_pick_list):
	held = frappe.db.sql(
		"""
		select sum(pli.picked_qty - pli.delivered_qty - pli.transferred_qty)
		from `tabPick List Item` pli
		inner join `tabPick List` pl on pl.name = pli.parent
		where pli.parenttype = 'Pick List'
			and pli.item_code = %(item_code)s and pli.warehouse = %(warehouse)s
			and pl.docstatus = 0 and pl.name != %(exclude)s
			and pli.picked_qty - pli.delivered_qty - pli.transferred_qty > 0
		""",
		{"item_code": item_code, "warehouse": warehouse, "exclude": exclude_pick_list},
	)
	return flt(held[0][0]) if held else 0.0


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------


def _result(pick_list, status, stock_entry=None, shortages=None, detail=None):
	sales_order = frappe.db.get_value(
		"Pick List Item", {"parent": pick_list, "parenttype": "Pick List", "sales_order": ["is", "set"]}, "sales_order"
	)
	customer_name = None
	if sales_order:
		customer_name = frappe.db.get_value("Sales Order", sales_order, "customer_name")
	shortages = shortages or []
	parts = [detail] if detail else []
	parts += [
		_("{0} en {1}: requerido {2}, disponible {3}, faltan {4}").format(
			s["item_code"], s["warehouse"], s["required_qty"], s["available_qty"], s["shortage_qty"]
		)
		for s in shortages
	]
	# Stable shape: every key always present, whatever the status.
	return {
		"pick_list": pick_list,
		"sales_order": sales_order,
		"pedido": root_commercial_name(sales_order) if sales_order else None,
		"customer": customer_name,
		"status": status,
		"material_issue": stock_entry,
		"shortages": shortages,
		"detail": "; ".join(parts),
	}


def _error_text(exc):
	text = str(exc) or type(exc).__name__
	return re.sub(r"<[^>]+>", " ", text).strip()


def _retrying_on_deadlock(fn):
	from fabergray_erp.api.recorridos import _retrying_on_deadlock as retrying

	return retrying(fn)
