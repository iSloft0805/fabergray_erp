# -*- coding: utf-8 -*-
"""Jefe de Bodega -- "Validar pedidos con faltantes resueltos".

Completes, in bulk, the picking of Pick Lists that were waiting on a
Reporte de Faltante resolved recently, but only where the stock is really
there. The report being Resuelto is never taken as proof of stock:
availability is re-read under lock for every Pick List.

Scope (decided, not inferred):
- reports whose LAST status change to "Resuelto" happened inside the
  window RESOLVED_SHORTAGE_LOOKBACK_DAYS = 3, site date: today, yesterday
  and the day before (today - 2 at 00:00:00 .. today 23:59:59); today - 3
  and earlier are out. Reporte de Faltante has no resolved_on field; the
  transition is read from Frappe's Version log (track_changes=1, every
  resolving code path saves the document) with
  api.reporte_faltantes._resolution_dates() -- the same source the PDF
  report already uses. Never `modified`, never an unrelated edit. No
  historical sweep: the window only exists so orders a first, stricter
  run left short are re-validated after the day changes.
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
draft, re-validates the WHOLE Pick List (not only the resolved line) and
refuses -- changing nothing -- unless every line can end up fully picked:
- no Reporte de Faltante Abierto/En Proceso on the Pick List (stock on the
  shelf never closes the administrative process);
- no line still to fill in a non-picking warehouse (Devoluciones/
  Cuarentena); a line Bodega already picked completely is left alone;
- operational stock: Bin.actual_qty minus what OTHER draft Pick Lists hold
  physically picked and not yet issued, must cover every pending unit of
  this Pick List, aggregated per item_code + warehouse.
A line Bodega has not picked yet is no longer a reason to refuse by itself.
Only when everything fits is every line filled up to its stock_qty (and
fg_started_by/fg_started_on set to the caller when nobody started the Pick
List). Then the unchanged COMPLETAR PEDIDO path runs: its guards,
validate_stock_for_issue(), submit + ONE Material Issue under its savepoint.
A note on the Pick List lists the lines the system filled, so Bodega knows
what to collect physically. Reportes de Faltante are never modified.
"""

import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, escape_html, flt, getdate, now_datetime, nowdate

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
#: Window of the last status change to "Resuelto": today and the 2 previous
#: days (site date). Internal only -- never taken from the request.
RESOLVED_SHORTAGE_LOOKBACK_DAYS = 3

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


def resolution_window(today=None):
	"""(first_day, last_day), both inclusive, site date: today and the
	RESOLVED_SHORTAGE_LOOKBACK_DAYS - 1 previous days."""
	last_day = getdate(today or nowdate())
	return getdate(add_days(last_day, -(RESOLVED_SHORTAGE_LOOKBACK_DAYS - 1))), last_day


def _reports_resolved_today():
	"""Reportes de Faltante (readable by the caller) whose LAST transition to
	Resuelto falls inside resolution_window(), with their Pick List link.
	Version.creation only narrows the candidates (some edit in the window);
	the decision is the date of the last status -> "Resuelto" change, so a
	report resolved before the window and merely edited inside it stays
	out."""
	from fabergray_erp.api.reporte_faltantes import _resolution_dates

	first_day, last_day = resolution_window()
	version = frappe.qb.DocType("Version")
	touched = (
		frappe.qb.from_(version)
		.select(version.docname)
		.distinct()
		.where(version.ref_doctype == "Reporte de Faltante")
		.where(version.creation >= f"{first_day.isoformat()} 00:00:00")
		.where(version.creation <= f"{last_day.isoformat()} 23:59:59.999999")
		.run(pluck=True)
	)
	if not touched:
		return []

	resolved_on = _resolution_dates(touched)
	names = sorted(name for name, ts in resolved_on.items() if first_day <= getdate(ts) <= last_day)
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
	"""({pick_list: {"reports": [resolved reports that made it a candidate]}}
	in first-seen order, [early ERROR results]). Remainder Pick
	Lists are created here, each in its own committed transaction, BEFORE
	any Pick List is completed: ensure_remaining_pick_list() locks Sales
	Order -> Pick List Item, a different order than COMPLETAR PEDIDO's
	Bin -> Pick List, so the two never nest."""
	targets = {}
	errors = []

	def add(pick_list, report):
		targets.setdefault(pick_list, {"reports": []})["reports"].append(report.name)

	for report in reports:
		docstatus = frappe.db.get_value("Pick List", report.pick_list, "docstatus")
		if docstatus == 0:
			add(report.pick_list, report)
			continue
		if docstatus != 1:
			continue  # cancelled / deleted

		original = frappe.get_doc("Pick List", report.pick_list)
		if pending_issue_rows(original):
			# Legacy: submitted without a complete Material Issue. COMPLETAR
			# PEDIDO's guard refuses it (ERROR); never repaired here.
			add(report.pick_list, report)
			continue

		if not report.sales_order_item:
			continue

		remainders = _draft_remainders(report)
		if not remainders:
			try:
				_retrying_on_deadlock(ensure_remaining_pick_list)(report.name)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				if report.pick_list not in {r["pick_list"] for r in errors}:
					errors.append(_result(report.pick_list, STATUS_ERROR, detail=_error_text(e)))
				continue
			remainders = _draft_remainders(report)

		for pick_list in remainders:
			add(pick_list, report)

	return targets, errors


def _draft_remainders(report):
	"""Draft Pick Lists holding this report's Sales Order line outside its
	original Pick List, sorted."""
	return sorted(
		set(
			frappe.get_all(
				"Pick List Item",
				filters={
					"sales_order_item": report.sales_order_item,
					"parenttype": "Pick List",
					"docstatus": 0,
					"parent": ["!=", report.pick_list],
				},
				pluck="parent",
			)
		)
	)


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
		return _result(pick_list, STATUS_STILL_SHORT, shortages=list(e.shortages), detail="\n".join(e.reasons))
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

	filled = []  # fresh per attempt: a deadlock retry re-runs this whole function

	def prepare(pl):
		filled.extend(_prepare(pl))

	outcome = _complete_pick_list(pick_list, prepare=prepare, system_action=True)
	if outcome["already_completed"]:
		return _result(pick_list, STATUS_ALREADY_COMPLETED, stock_entry=outcome["stock_entry"])

	doc = frappe.get_doc("Pick List", pick_list)
	doc.add_comment(
		"Comment",
		_("Completado por {0} con «Validar pedidos con faltantes resueltos» (faltantes: {1}).").format(
			frappe.session.user, ", ".join(target["reports"])
		),
	)
	if filled:
		doc.add_comment("Comment", _auto_fill_note(filled))
	return _result(pick_list, STATUS_COMPLETED, stock_entry=outcome["stock_entry"])


def _auto_fill_note(filled):
	"""What the system marked as picked that Bodega had not: the list Bodega
	uses to collect the goods physically before dispatch. Written only on a
	real completion, so an idempotent re-run never repeats it."""
	lines = "".join(
		"<br>- {0} — {1} — {2}".format(
			escape_html(item_code), _units(qty), escape_html(warehouse)
		)
		for item_code, qty, warehouse in filled
	)
	return _("Validación automática de faltantes resueltos por {0}.<br>Se completaron para alistamiento:{1}").format(
		escape_html(frappe.session.user), lines
	)


def _units(qty):
	qty = flt(qty)
	return _("1 unidad") if qty == 1 else _("{0} unidades").format(_qty(qty))


def _qty(value):
	return "{0:g}".format(flt(value))


def _prepare(pl):
	"""COMPLETAR PEDIDO's `prepare` hook: re-validate the WHOLE locked,
	re-read draft and only then change it in memory. Raises
	ResolvedShortageStillShortError -- with the document untouched -- unless
	every line can end up fully picked. Returns [(item_code, qty filled,
	warehouse)] for the lines it completed."""
	precision = frappe.get_precision("Pick List Item", "picked_qty") or 6
	locations = pl.get("locations") or []
	reasons = [
		_("{0} — faltante todavía Abierto/En Proceso").format(name) for name in sorted(_open_reports_for(pl))
	]

	excluded = non_picking_warehouses(pl.company)
	target_qty = {}
	for row in locations:
		requested = flt(row.stock_qty, precision)
		if requested <= 0 or flt(row.picked_qty, precision) >= requested:
			continue  # nothing to fill: never blocked for its warehouse alone
		if row.warehouse in excluded:
			reasons.append(
				_("{0} — {1}: almacén no válido para alistamiento").format(row.item_code, row.warehouse)
			)
			continue
		target_qty[row.name] = requested

	shortages = _operational_shortages(pl, target_qty)

	if reasons or shortages:
		exc = ResolvedShortageStillShortError(
			"\n".join(reasons) or _("No hay stock suficiente para completar el pedido.")
		)
		exc.shortages = shortages
		exc.reasons = reasons
		raise exc

	filled = []
	for row in locations:
		if row.name in target_qty:
			filled.append((row.item_code, flt(target_qty[row.name] - flt(row.picked_qty), precision), row.warehouse))
			row.picked_qty = target_qty[row.name]
	_lock_manual_picking(pl)
	if not pl.fg_started_by:
		pl.fg_started_by = frappe.session.user
		pl.fg_started_on = now_datetime()
	return filled


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
		_("{0} — {1} · Requerido: {2} · Disponible: {3} · Faltante: {4}").format(
			s["item_code"], s["warehouse"], _qty(s["required_qty"]), _qty(s["available_qty"]), _qty(s["shortage_qty"])
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
		"detail": "\n".join(parts),
	}


def _error_text(exc):
	text = str(exc) or type(exc).__name__
	return re.sub(r"<[^>]+>", " ", text).strip()


def _retrying_on_deadlock(fn):
	from fabergray_erp.api.recorridos import _retrying_on_deadlock as retrying

	return retrying(fn)
