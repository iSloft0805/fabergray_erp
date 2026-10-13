# -*- coding: utf-8 -*-
"""Jefe de Bodega -- "Validar pedidos con faltantes resueltos".

Completes, in bulk, the picking of Pick Lists that are still operationally
pending because of a Reporte de Faltante, but only where the stock is
really there. The report being Resuelto is never taken as proof of stock:
availability is re-read under lock for every Pick List.

Universe (decided, not inferred): orders still in picking or still pending
because of a shortage -- never "every resolved report in history". The
date the report was resolved is NOT a criterion (no window, no Version
log, no `modified`); what matters is whether operational work is left.
_pending_candidates() reads it in ONE statement:
- A: a DRAFT Pick List with a Reporte de Faltante Abierto, En Proceso or
  Resuelto. With an Abierto/En Proceso report it is processed and comes
  back AÚN CON FALTANTES naming that report (never closed here);
- B: a DRAFT remainder -- another draft Pick List holding the Sales Order
  line of a Resuelto report whose original was submitted with a complete
  Material Issue;
- C: a Resuelto report whose original was submitted with a complete
  Material Issue, with no draft remainder, and whose Sales Order line still
  has real demand: stock_qty - delivered_qty - what open Pick Lists already
  claim, the exact formula of
  pick_list_service.create_pick_list_for_remaining_demand() (with the
  Material Issue flow delivered_qty never grows, so delivered_qty alone
  would let every historical order in). The remainder is created with
  remainder_service.ensure_remaining_pick_list() (idempotent) ONLY for the
  C lines that made it into this run's batch;
- E: legacy -- a submitted Pick List with a report and picked - delivered
  - transferred > 0 (no complete Material Issue), not Facturado: ERROR /
  revisión administrativa, read-only, never repaired.
A, B and C exclude Sales Orders that are not submitted or are Closed / On
Hold / Completed (the same block ensure_remaining_pick_list() applies).
Everything else is left out entirely, without a result row: submitted Pick
Lists with a complete Material Issue and nothing left (ready for, or
already through, Facturación), Facturado legacy ones, drafts without any
report, submitted ones whose shortage is still open (nothing to complete
until it is resolved: the remainder is born on resolution), cancelled.
YA COMPLETADO only appears through concurrency: someone else completed the
Pick List between the selection and its lock.

Batch: at most MAX_PICK_LISTS_PER_RUN processable targets (A/B Pick Lists,
C Sales Order lines) per run, cut BEFORE any remainder is created;
`remaining_count` is what is left. E rows do not use that quota: at most
MAX_ERROR_RESULTS are listed, `error_remaining_count` says how many more.

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
from frappe.utils import escape_html, flt, now_datetime

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
#: Legacy (kind E) rows listed per run; they never use the batch quota.
MAX_ERROR_RESULTS = 20

KIND_DRAFT = "A"
KIND_REMAINDER = "B"
KIND_NEEDS_REMAINDER = "C"
KIND_LEGACY = "E"

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
	"""Process up to `limit` targets; returns {summary, results,
	remaining_count, error_remaining_count}. Commits per Pick List (see
	module docstring)."""
	frappe.only_for(ROLES)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)

	work, legacy = _plan(_pending_candidates())
	batch, remaining = work[:limit], work[limit:]
	shown_legacy, hidden_legacy = legacy[:MAX_ERROR_RESULTS], legacy[MAX_ERROR_RESULTS:]

	# Remainders (C) are created only now, for the batch alone.
	targets, results = _collect_targets(batch)
	for name, target in targets.items():
		results.append(_process_isolated(name, target))
	results.extend(_legacy_result(name) for name in shown_legacy)

	summary = {
		"completed": sum(1 for r in results if r["status"] == STATUS_COMPLETED),
		"still_short": sum(1 for r in results if r["status"] == STATUS_STILL_SHORT),
		"already_completed": sum(1 for r in results if r["status"] == STATUS_ALREADY_COMPLETED),
		"errors": sum(1 for r in results if r["status"] == STATUS_ERROR),
	}
	return {
		"summary": summary,
		"results": results,
		"remaining_count": len(remaining),
		"error_remaining_count": len(hidden_legacy),
	}


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

#: One statement, four branches. Every branch starts from Reporte de
#: Faltante.status (indexed) and only follows indexed links (pick_list,
#: Pick List Item.parent / sales_order_item, Sales Order Item.name), so a
#: closed historical order costs an index probe, never a scan of its rows.
_CANDIDATES_SQL = """
select r.name as report, r.pick_list as pick_list, r.sales_order_item as sales_order_item,
	'A' as kind
from `tabReporte de Faltante` r
join `tabPick List` pl on pl.name = r.pick_list and pl.docstatus = 0
left join `tabSales Order` so on so.name = r.sales_order
where r.status in %(active)s
	and {so_open}

union all

select r.name, rem.name, r.sales_order_item, 'B'
from `tabReporte de Faltante` r
join `tabPick List` orig on orig.name = r.pick_list and orig.docstatus = 1
join `tabPick List Item` pli on pli.sales_order_item = r.sales_order_item
	and pli.parenttype = 'Pick List' and pli.parent != r.pick_list
join `tabPick List` rem on rem.name = pli.parent and rem.docstatus = 0
left join `tabSales Order` so on so.name = r.sales_order
where r.status = %(resolved)s
	and {so_open}
	and not exists ({pending_issue})

union all

select r.name, r.pick_list, r.sales_order_item, 'C'
from `tabReporte de Faltante` r
join `tabPick List` orig on orig.name = r.pick_list and orig.docstatus = 1
join `tabSales Order Item` soi on soi.name = r.sales_order_item
join `tabSales Order` so on so.name = soi.parent
join `tabItem` item on item.name = soi.item_code and item.is_stock_item = 1
where r.status = %(resolved)s
	and so.docstatus = 1 and so.status not in ('Closed', 'On Hold', 'Completed', 'Cancelled')
	and ifnull(soi.delivered_by_supplier, 0) = 0
	and not exists ({pending_issue})
	and not exists (
		select 1 from `tabPick List Item` d
		join `tabPick List` dpl on dpl.name = d.parent and dpl.docstatus = 0
		where d.sales_order_item = r.sales_order_item and d.parenttype = 'Pick List'
	)
	and round(
		greatest(soi.stock_qty - soi.delivered_qty, 0) - ifnull((
			select sum(case when c.picked_qty > 0 and c.docstatus = 1
				then c.picked_qty - c.delivered_qty else c.stock_qty end)
			from `tabPick List Item` c
			join `tabPick List` cpl on cpl.name = c.parent
			where c.sales_order_item = r.sales_order_item
				and cpl.status not in ('Completed', 'Cancelled')
				and c.docstatus != 2
		), 0), 6) > 0

union all

select r.name, r.pick_list, r.sales_order_item, 'E'
from `tabReporte de Faltante` r
join `tabPick List` orig on orig.name = r.pick_list and orig.docstatus = 1
where r.status in %(active)s
	and ifnull(orig.fg_invoicing_status, '') != 'Facturado'
	and exists ({pending_issue})

order by report, pick_list
""".format(
	# A Sales Order that is gone, cancelled, Closed, On Hold or Completed
	# leaves nothing to pick (ensure_remaining_pick_list()'s own block).
	so_open="(r.sales_order is null or r.sales_order = '' or (so.docstatus = 1 "
	"and so.status not in ('Closed', 'On Hold', 'Completed', 'Cancelled')))",
	# Same rule as stock_issue_service.pending_issue_rows(): picked and
	# neither delivered nor issued.
	pending_issue="select 1 from `tabPick List Item` e where e.parent = orig.name "
	"and e.parenttype = 'Pick List' and round(e.picked_qty - e.delivered_qty - e.transferred_qty, 6) > 0",
)


def _pending_candidates():
	"""[{report, pick_list, sales_order_item, kind}] -- see module docstring
	for A/B/C/E. The raw statement is then narrowed to the reports the
	caller may read (one get_list, never one call per report)."""
	rows = frappe.db.sql(
		_CANDIDATES_SQL,
		{"active": tuple(OPEN_SHORTAGE_STATUSES) + (RESOLVED_STATUS,), "resolved": RESOLVED_STATUS},
		as_dict=True,
	)
	if not rows:
		return []
	readable = set(
		frappe.get_list(
			"Reporte de Faltante",
			filters={"name": ["in", sorted({r.report for r in rows})]},
			pluck="name",
			limit_page_length=0,
		)
	)
	return [r for r in rows if r.report in readable]


def _plan(candidates):
	"""([(key, entry)] processable A/B/C targets in first-seen order,
	[legacy Pick List names]). A/B are keyed by their Pick List (several
	reports of one Pick List are one target), C by its Sales Order line
	(one remainder each). Nothing is written here."""
	work = {}
	legacy = []
	for row in candidates:
		if row.kind == KIND_LEGACY:
			if row.pick_list not in legacy:
				legacy.append(row.pick_list)
			continue
		if row.kind == KIND_NEEDS_REMAINDER:
			key = ("C", row.sales_order_item)
		else:
			key = ("PL", row.pick_list)
		entry = work.setdefault(key, {"kind": row.kind, "reports": []})
		if row.report not in entry["reports"]:
			entry["reports"].append(row.report)
	return list(work.items()), legacy


def _collect_targets(batch):
	"""({pick_list: {"reports": [...]}} in batch order, [early ERROR
	results]). C remainders are created here, each in its own committed
	transaction, BEFORE any Pick List is completed:
	ensure_remaining_pick_list() locks Sales Order -> Pick List Item, a
	different order than COMPLETAR PEDIDO's Bin -> Pick List, so the two
	never nest. A remainder another process created meanwhile is reused; a
	line whose demand vanished meanwhile simply yields nothing."""
	targets = {}
	errors = []

	def add(pick_list, reports):
		bucket = targets.setdefault(pick_list, {"reports": []})["reports"]
		bucket.extend(r for r in reports if r not in bucket)

	for (key_type, key), entry in batch:
		if key_type == "PL":
			add(key, entry["reports"])
			continue

		report = frappe.db.get_value(
			"Reporte de Faltante",
			entry["reports"][0],
			["name", "pick_list", "sales_order_item"],
			as_dict=True,
		)
		try:
			_retrying_on_deadlock(ensure_remaining_pick_list)(report.name)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			errors.append(_result(report.pick_list, STATUS_ERROR, detail=_error_text(e)))
			continue
		for pick_list in _draft_remainders(report):
			add(pick_list, entry["reports"])

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


def _legacy_result(pick_list):
	"""Kind E, read-only: no lock, no transaction, nothing repaired."""
	lines = "".join(
		"\n{0} — {1}: {2} sin salida de inventario".format(row.item_code, row.warehouse, _units(qty))
		for row, qty in pending_issue_rows(frappe.get_doc("Pick List", pick_list))
	)
	return _result(
		pick_list,
		STATUS_ERROR,
		detail=_("Finalizado sin salida de inventario completa: requiere revisión administrativa.") + lines,
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
