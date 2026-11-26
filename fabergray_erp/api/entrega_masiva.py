# -*- coding: utf-8 -*-
"""api/entrega_masiva.py -- Recorridos: "Entregar todos los pedidos".

Whitelisted layer over fabergray_erp.entrega_masiva_service (see its
docstring for the delivery rules). Every endpoint: logged in, System Manager
(v1), write permission on Recorrido/Recorrido Parada, the site's company
resolved server-side. The preview never writes; confirm revalidates every
order from the database (never trusts the preview or the browser), creates
the audited batch and queues the background job.
"""

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, flt

from fabergray_erp import entrega_masiva_service as service

PREVIEW_PAGE_LENGTH = 50
MAX_PAGE_LENGTH = 200
VIEWS = ("eligible", "excluded")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _strict_bool(value, label):
	if value in (None, "", 0, "0", False, "false", "False"):
		return False
	if value in (1, "1", True, "true", "True"):
		return True
	frappe.throw(_("{0}: valor inválido.").format(label), service.EntregaMasivaError)


def _parse_names(value, label):
	if value in (None, ""):
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("{0}: lista inválida.").format(label), service.EntregaMasivaError)
	if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
		frappe.throw(_("{0}: lista inválida.").format(label), service.EntregaMasivaError)
	if len(value) > service.MAX_BATCH_ORDERS:
		frappe.throw(_("{0}: demasiados pedidos.").format(label), service.EntregaMasivaError)
	return list(dict.fromkeys(value))


def _cache_key(company, params):
	date_from, date_to, date_mode, effective = params
	return f"fg_entrega_masiva_preview::{frappe.session.user}::{company}::{date_from}::{date_to}::{date_mode}::{effective}"


@frappe.whitelist()
def get_bulk_delivery_preview(
	date_from=None, date_to=None, date_mode=None, effective_date=None, view="eligible", page=1, page_length=PREVIEW_PAGE_LENGTH, refresh=0
):
	"""Read-only. Classifies every Facturado order invoiced in the range (the
	date criterion is the invoice date) and returns the summary for ALL of
	them plus one page of the requested view. Cached 10 minutes per user and
	parameters so paging is instant; refresh=1 recomputes. Writes nothing."""
	company = service.require_access()
	params = service.validate_params(date_from, date_to, date_mode, effective_date)
	if view not in VIEWS:
		frappe.throw(_("Vista inválida."), service.EntregaMasivaError)
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length) or PREVIEW_PAGE_LENGTH, 1), MAX_PAGE_LENGTH)

	key = _cache_key(company, params)
	rows = None if cint(refresh) else frappe.cache.get_value(key)
	if rows is None:
		rows = service.build_preview(company, *params)
		frappe.cache.set_value(key, rows, expires_in_sec=service.PREVIEW_CACHE_SECONDS)

	selected_view = [r for r in rows if r["eligible"] == (view == "eligible")]
	start = (page - 1) * page_length
	date_from, date_to, date_mode, effective = params
	return {
		"criterion": service.DATE_CRITERION,
		"date_from": str(date_from),
		"date_to": str(date_to),
		"date_mode": date_mode,
		"effective_date": str(effective) if effective else None,
		"today": frappe.utils.nowdate(),
		"summary": service.summarize(rows),
		"view": view,
		"rows": selected_view[start : start + page_length],
		"page": page,
		"page_length": page_length,
		"total_in_view": len(selected_view),
		"has_more": start + page_length < len(selected_view),
		"active_batch": _active_payload(company),
	}


@frappe.whitelist(methods=["POST"])
def confirm_bulk_delivery(
	date_from=None,
	date_to=None,
	date_mode=None,
	effective_date=None,
	reason=None,
	physical_delivery_confirmed=None,
	invoice_date_confirmed=None,
	select_all=None,
	pick_lists=None,
	excluded_pick_lists=None,
	client_request_id=None,
):
	"""Creates the audited batch and queues it. `select_all=1`: every order
	eligible NOW for the parameters (all pages), minus `excluded_pick_lists`;
	otherwise exactly `pick_lists`. Everything is recomputed here from the
	database: an order that stopped being eligible is reported back, never
	delivered. Idempotent per client_request_id."""
	company = service.require_access()
	params = service.validate_params(date_from, date_to, date_mode, effective_date)
	client_request_id = (client_request_id or "").strip()
	if not _REQUEST_ID.match(client_request_id):
		frappe.throw(_("Solicitud inválida (falta la clave de idempotencia)."), service.EntregaMasivaError)
	existing = frappe.db.get_value("Entrega Masiva", {"client_request_id": client_request_id}, ["name", "company"], as_dict=True)
	if existing:
		if existing.company != company:
			frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
		return {**get_bulk_delivery_status(existing.name), "already_submitted": True}

	reason = (reason or "").strip()
	if len(reason) < service.REASON_MIN_LENGTH:
		frappe.throw(_("Indica el motivo de la regularización (mínimo {0} caracteres).").format(service.REASON_MIN_LENGTH), service.EntregaMasivaError)
	if len(reason) > service.REASON_MAX_LENGTH:
		frappe.throw(_("El motivo no puede superar {0} caracteres.").format(service.REASON_MAX_LENGTH), service.EntregaMasivaError)
	if not _strict_bool(physical_delivery_confirmed, _("Confirmación de entrega física")):
		frappe.throw(_("Confirma que los pedidos fueron entregados físicamente."), service.EntregaMasivaError)
	invoice_confirmed = _strict_bool(invoice_date_confirmed, _("Confirmación de fecha de facturación"))
	if params[2] == service.DATE_MODE_INVOICE and not invoice_confirmed:
		frappe.throw(
			_("Confirma que la fecha de facturación de cada pedido corresponde a su entrega real."), service.EntregaMasivaError
		)
	select_all = _strict_bool(select_all, _("Seleccionar todos"))
	requested = _parse_names(pick_lists, _("Pedidos"))
	excluded = set(_parse_names(excluded_pick_lists, _("Pedidos excluidos")))

	date_from, date_to, date_mode, effective = params
	eligible, not_eligible = {}, []
	candidates = service.load_candidates(company, date_from, date_to)
	for c in candidates:
		classification = service.classify(c, date_mode, effective, company)
		if classification[0] is None:
			eligible[c.name] = (c, classification)
		elif not select_all and c.name in requested:
			not_eligible.append({"pick_list": c.name, "reason": service._reason_text(classification[0], classification[1])})

	if select_all:
		chosen = [name for name in eligible if name not in excluded]
	else:
		if not requested:
			frappe.throw(_("Selecciona al menos un pedido."), service.EntregaMasivaError)
		known = {c.name for c in candidates}
		not_eligible += [
			{"pick_list": name, "reason": _("Fuera del rango o no facturado.")} for name in requested if name not in known
		]
		chosen = [name for name in requested if name in eligible]
	if not chosen:
		frappe.throw(_("Ninguno de los pedidos seleccionados se puede entregar."), service.EntregaMasivaError)
	if len(chosen) > service.MAX_BATCH_ORDERS:
		frappe.throw(
			_("Una entrega masiva admite como máximo {0} pedidos; divide el rango.").format(service.MAX_BATCH_ORDERS),
			service.EntregaMasivaError,
		)

	selected = []
	for name in chosen:
		c, classification = eligible[name]
		amount, available = service._amount(c)
		selected.append((c, classification, amount, available))

	batch, _created = service.create_batch(
		company, params, selected, reason, True, invoice_confirmed, client_request_id
	)
	frappe.cache.delete_value(_cache_key(company, params))
	return {**get_bulk_delivery_status(batch.name), "not_eligible": not_eligible, "already_submitted": False}


def _load_batch(batch_name, company):
	if not batch_name or not isinstance(batch_name, str) or not frappe.db.exists("Entrega Masiva", batch_name):
		frappe.throw(_("La entrega masiva no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	batch.check_permission("read")
	if batch.company != company:
		frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
	return batch


def _active_payload(company):
	batch = service.active_batch(company)
	if not batch:
		return None
	return {"name": batch.name, "status": batch.status, "stale": service.is_stale(batch)}


@frappe.whitelist()
def get_bulk_delivery_status(batch_name):
	"""Progress + final summary. Times are REAL execution times; per-order
	effective delivery days are reported as dates only."""
	company = service.require_access()
	batch = _load_batch(batch_name, company)
	total = cint(batch.total_selected)
	processed = cint(batch.processed_count)
	issues = frappe.get_list(
		"Entrega Masiva Pedido",
		filters={"entrega_masiva": batch.name, "result": ["in", [service.RESULT_SKIPPED, service.RESULT_FAILED]]},
		fields=["pick_list", "commercial_name", "customer_name", "result", "message"],
		order_by="result asc, pick_list asc",
		limit_page_length=500,
	)
	routes = frappe.get_list(
		"Recorrido", filters={"entrega_masiva": batch.name}, pluck="name", order_by="name asc", limit_page_length=0
	)
	return {
		"name": batch.name,
		"status": batch.status,
		"finished": batch.status in (service.STATUS_DONE, service.STATUS_DONE_WITH_ERRORS, service.STATUS_CLOSED),
		"stale": service.is_stale(batch),
		"can_resume": batch.status == service.STATUS_INTERRUPTED or service.is_stale(batch),
		"total_selected": total,
		"processed": processed,
		"delivered": cint(batch.delivered_count),
		"skipped": cint(batch.skipped_count),
		"failed": cint(batch.failed_count),
		"progress_percent": flt(processed * 100.0 / total, 1) if total else 0,
		"total_amount": flt(batch.total_amount, 2),
		"date_mode": batch.date_mode,
		"effective_date": str(batch.effective_delivery_date) if batch.effective_delivery_date else None,
		"date_from": str(batch.date_from),
		"date_to": str(batch.date_to),
		"reason": batch.reason,
		"requested_by": batch.requested_by,
		"queued_on": str(batch.queued_on) if batch.queued_on else None,
		"started_on": str(batch.started_on) if batch.started_on else None,
		"finished_on": str(batch.finished_on) if batch.finished_on else None,
		"administrative_routes": routes,
		"issues": issues,
	}


@frappe.whitelist()
def get_active_bulk_delivery():
	company = service.require_access()
	return _active_payload(company)


@frappe.whitelist(methods=["POST"])
def resume_bulk_delivery(batch_name):
	company = service.require_access()
	_load_batch(batch_name, company)
	service.resume(batch_name)
	return get_bulk_delivery_status(batch_name)


@frappe.whitelist(methods=["POST"])
def close_bulk_delivery(batch_name):
	company = service.require_access()
	_load_batch(batch_name, company)
	service.close(batch_name)
	return get_bulk_delivery_status(batch_name)
