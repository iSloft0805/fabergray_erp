# -*- coding: utf-8 -*-
"""api/cartera_masiva.py -- Cartera: "Autorizar cobros y pagos" (fase A:
autorización masiva de los pagos reportados por el conductor).

Whitelisted layer over fabergray_erp.cartera_masiva_service (see its
docstring for the rules). Every endpoint: logged in, System Manager (v1),
read permission on Cartera Obligacion, the site's company resolved
server-side. The preview never writes; confirm revalidates every obligation
from the database (never trusts the preview or the browser), creates the
audited batch and queues the background job. Writes are POST only.

"Registrar pagos" (fase B) is not available: options report it as
disabled and the service refuses that mode.
"""

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, get_fullname, nowdate

from fabergray_erp import cartera_masiva_service as service
from fabergray_erp import cartera_service

PREVIEW_PAGE_LENGTH = 50
MAX_PAGE_LENGTH = 200
HISTORY_PAGE_LENGTH = 20
VIEWS = ("eligible", "excluded")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
#: The eligible index sent to the browser so the selection totals per
#: client are exact across pages (obligation, customer, cents).
MAX_INDEX = 10000


def _strict_bool(value, label):
	if value in (None, "", 0, "0", False, "false", "False"):
		return False
	if value in (1, "1", True, "true", "True"):
		return True
	frappe.throw(_("{0}: valor inválido.").format(label), service.CarteraMasivaError)


def _filters(date_type, date_from, date_to, customers, issuer, invoice_payment_method, status, search):
	return service.validate_filters(
		date_type=date_type,
		date_from=date_from,
		date_to=date_to,
		customers=customers,
		issuer=issuer,
		invoice_payment_method=invoice_payment_method,
		status=status,
		search=search,
	)


def _cache_key(company, filters):
	digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:24]
	return f"fg_cartera_masiva_preview::{frappe.session.user}::{company}::{digest}"


def _active_payload(company):
	batch = service.active_batch(company)
	if not batch:
		return None
	return {"name": batch.name, "status": batch.status, "mode": batch.mode, "stale": service.is_stale(batch)}


@frappe.whitelist()
def get_bulk_authorization_options():
	"""Static choices + the company's issuers present in Cartera. Read-only."""
	company = service.require_access()
	issuers = frappe.db.sql_list(
		"""
		SELECT DISTINCT invoice_issuer FROM `tabCartera Obligacion`
		WHERE company = %s AND IFNULL(invoice_issuer, '') != '' ORDER BY invoice_issuer
		""",
		(company,),
	)
	methods = [
		m
		for m in frappe.get_meta(cartera_service.OBLIGATION_DOCTYPE).get_field("invoice_payment_method").options.split("\n")
		if m
	]
	return {
		"today": nowdate(),
		"modes": [
			{"key": service.MODE_AUTHORIZATION, "label": _("Autorizar cobros"), "enabled": True},
			{"key": service.MODE_PAYMENTS, "label": _("Registrar pagos"), "enabled": False, "note": _("Próximamente")},
		],
		"date_types": [{"key": k, "label": _(v)} for k, v in service.DATE_TYPES.items()],
		"default_date_type": service.DEFAULT_DATE_TYPE,
		"statuses": [{"key": k, "label": _(v)} for k, v in service.STATUS_FILTERS.items()],
		"issuers": issuers,
		"invoice_payment_methods": [{"key": m, "label": _(m)} for m in methods]
		+ [{"key": service.NO_PAYMENT_METHOD, "label": _("Sin forma de pago (histórico)")}],
		"limits": {
			"max_lines": service.MAX_BATCH_LINES,
			"max_range_days": service.MAX_RANGE_DAYS,
			"reason_min_length": service.REASON_MIN_LENGTH,
			"reason_max_length": service.REASON_MAX_LENGTH,
		},
		"active_batch": _active_payload(company),
	}


@frappe.whitelist()
def search_bulk_customers(search="", limit=20):
	"""Clients with driver-reported payments in this company (for the
	multi-select). Read-only."""
	company = service.require_access()
	search = (search or "").strip()[: service.MAX_SEARCH_LENGTH]
	limit = min(max(cint(limit) or 20, 1), 50)
	params = {"company": company, "paid": cartera_service.DRIVER_PAID, "limit": limit}
	condition = ""
	if search:
		params["search"] = service._like(search)
		condition = "AND (o.customer LIKE %(search)s OR o.customer_name LIKE %(search)s OR c.access_nombre_comercial LIKE %(search)s)"
	return frappe.db.sql(
		f"""
		SELECT o.customer, MAX(o.customer_name) AS customer_name,
			MAX(c.access_nombre_comercial) AS customer_commercial_name, COUNT(*) AS count
		FROM `tabCartera Obligacion` o LEFT JOIN `tabCustomer` c ON c.name = o.customer
		WHERE o.company = %(company)s AND o.driver_payment_status = %(paid)s {condition}
		GROUP BY o.customer
		ORDER BY customer_name ASC
		LIMIT %(limit)s
		""",
		params,
		as_dict=True,
	)


@frappe.whitelist()
def get_bulk_authorization_preview(
	date_type=None,
	date_from=None,
	date_to=None,
	customers=None,
	issuer=None,
	invoice_payment_method=None,
	status=None,
	search=None,
	view="eligible",
	page=1,
	page_length=PREVIEW_PAGE_LENGTH,
	refresh=0,
):
	"""Read-only. Classifies every driver-reported obligation matching the
	filters and returns the exact summary for ALL of them (per client too),
	plus one page of the requested view and the eligible index (exact
	selection totals across pages). Cached 10 minutes per user and filters;
	refresh=1 recomputes. Writes nothing."""
	company = service.require_access()
	filters = _filters(date_type, date_from, date_to, customers, issuer, invoice_payment_method, status, search)
	if view not in VIEWS:
		frappe.throw(_("Vista inválida."), service.CarteraMasivaError)
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length) or PREVIEW_PAGE_LENGTH, 1), MAX_PAGE_LENGTH)

	key = _cache_key(company, filters)
	cached = None if cint(refresh) else frappe.cache.get_value(key)
	if cached is None:
		cached = {
			"rows": service.build_preview(company, filters),
			"missing_date": service.count_missing_date(company, filters),
		}
		frappe.cache.set_value(key, cached, expires_in_sec=service.PREVIEW_CACHE_SECONDS)
	rows = cached["rows"]

	selected_view = [r for r in rows if r["eligible"] == (view == "eligible")]
	start = (page - 1) * page_length
	eligible = [r for r in rows if r["eligible"]]
	return {
		"filters": filters,
		"date_type_label": _(service.DATE_TYPES[filters["date_type"]]),
		"today": nowdate(),
		"summary": {**service.summarize(rows), "missing_date": cint(cached["missing_date"])},
		"view": view,
		"rows": selected_view[start : start + page_length],
		"page": page,
		"page_length": page_length,
		"total_in_view": len(selected_view),
		"has_more": start + page_length < len(selected_view),
		"eligible_index": [[r["obligation"], r["customer"], service._cents(r["amount"])] for r in eligible[:MAX_INDEX]],
		"index_complete": len(eligible) <= MAX_INDEX,
		"max_lines": service.MAX_BATCH_LINES,
		"active_batch": _active_payload(company),
	}


@frappe.whitelist(methods=["POST"])
def confirm_bulk_authorization(
	date_type=None,
	date_from=None,
	date_to=None,
	customers=None,
	issuer=None,
	invoice_payment_method=None,
	status=None,
	search=None,
	reason=None,
	review_confirmed=None,
	select_all=None,
	obligations=None,
	excluded_obligations=None,
	client_request_id=None,
):
	"""Creates the audited batch and queues it. select_all=1: every
	obligation eligible NOW for the filters (all pages) minus
	`excluded_obligations`; otherwise exactly `obligations`. Everything is
	recomputed here from the database: an obligation that stopped being
	eligible is reported back, never authorized. Idempotent per
	client_request_id."""
	company = service.require_access()
	filters = _filters(date_type, date_from, date_to, customers, issuer, invoice_payment_method, status, search)
	client_request_id = (client_request_id or "").strip()
	if not _REQUEST_ID.match(client_request_id):
		frappe.throw(_("Solicitud inválida (falta la clave de idempotencia)."), service.CarteraMasivaError)
	existing = frappe.db.get_value(service.BATCH_DOCTYPE, {"client_request_id": client_request_id}, ["name", "company"], as_dict=True)
	if existing:
		if existing.company != company:
			frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
		return {**get_bulk_authorization_status(existing.name), "already_submitted": True, "not_eligible": []}

	reason = cartera_service.clean_text(
		reason, _("Motivo de la autorización"), service.REASON_MAX_LENGTH, min_length=service.REASON_MIN_LENGTH, required=True
	)
	if not _strict_bool(review_confirmed, _("Confirmación administrativa")):
		frappe.throw(
			_("Confirma que revisaste los cobros y sus comprobantes antes de autorizarlos."), service.CarteraMasivaError
		)
	select_all = _strict_bool(select_all, _("Seleccionar todos"))
	requested = service._parse_list(obligations, _("Obligaciones"), service.MAX_BATCH_LINES * 5)
	excluded = set(service._parse_list(excluded_obligations, _("Obligaciones excluidas"), service.MAX_BATCH_LINES * 5))

	eligible, not_eligible = {}, []
	candidates = service.load_candidates(company, filters)
	for c in candidates:
		code, detail = service.classify(c)
		row_status = service._STATUS_OF_CODE.get(code, "revision")
		if filters["status"] != "todos" and row_status != filters["status"]:
			continue
		if code is None:
			eligible[c.name] = c
		elif not select_all and c.name in requested:
			not_eligible.append({"obligation": c.name, "reason": service._reason_text(code, detail)})

	if select_all:
		chosen = [name for name in eligible if name not in excluded]
	else:
		if not requested:
			frappe.throw(_("Selecciona al menos un cobro."), service.CarteraMasivaError)
		known = {c.name for c in candidates}
		not_eligible += [
			{"obligation": name, "reason": _("Fuera de los filtros o de otra empresa.")} for name in requested if name not in known
		]
		chosen = [name for name in requested if name in eligible]
	if not chosen:
		frappe.throw(_("Ninguno de los cobros seleccionados se puede autorizar."), service.CarteraMasivaError)

	batch, _created = service.create_batch(company, filters, [eligible[n] for n in chosen], reason, client_request_id)
	frappe.cache.delete_value(_cache_key(company, filters))
	return {**get_bulk_authorization_status(batch.name), "not_eligible": not_eligible, "already_submitted": False}


def _load_batch(batch_name, company):
	if not batch_name or not isinstance(batch_name, str) or not frappe.db.exists(service.BATCH_DOCTYPE, batch_name):
		frappe.throw(_("El lote no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc(service.BATCH_DOCTYPE, batch_name)
	batch.check_permission("read")
	if batch.company != company:
		frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
	return batch


@frappe.whitelist()
def get_bulk_authorization_status(batch_name):
	"""Progress + final summary (per client, issues). Times are REAL
	execution times."""
	company = service.require_access()
	batch = _load_batch(batch_name, company)
	total = cint(batch.total_selected)
	processed = cint(batch.processed_count)
	issues = frappe.get_list(
		service.LINE_DOCTYPE,
		filters={"cartera_lote": batch.name, "result": ["in", [service.RESULT_SKIPPED, service.RESULT_FAILED]]},
		fields=["cartera_obligacion", "commercial_name", "customer_name", "amount", "result", "message"],
		order_by="result asc, cartera_obligacion asc",
		limit=500,
	)
	by_customer = frappe.db.sql(
		"""
		SELECT customer, MAX(customer_name) AS customer_name, COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount
		FROM `tabCartera Lote Linea`
		WHERE cartera_lote = %s AND result = %s
		GROUP BY customer ORDER BY amount DESC, customer_name ASC
		""",
		(batch.name, service.RESULT_AUTHORIZED),
		as_dict=True,
	)
	return {
		"name": batch.name,
		"mode": batch.mode,
		"status": batch.status,
		"finished": batch.status in service.FINISHED_STATUSES,
		"stale": service.is_stale(batch),
		"can_resume": batch.status == service.STATUS_INTERRUPTED or service.is_stale(batch),
		"total_selected": total,
		"processed": processed,
		"authorized": cint(batch.authorized_count),
		"skipped": cint(batch.skipped_count),
		"failed": cint(batch.failed_count),
		"progress_percent": flt(processed * 100.0 / total, 1) if total else 0,
		"currency": batch.currency,
		"total_amount": flt(batch.total_amount, 2),
		"authorized_amount": flt(batch.authorized_amount, 2),
		"date_type": batch.date_type,
		"date_from": str(batch.date_from) if batch.date_from else None,
		"date_to": str(batch.date_to) if batch.date_to else None,
		"reason": batch.reason,
		"requested_by": batch.requested_by,
		"requested_by_name": get_fullname(batch.requested_by) if batch.requested_by else None,
		"queued_on": str(batch.queued_on) if batch.queued_on else None,
		"started_on": str(batch.started_on) if batch.started_on else None,
		"finished_on": str(batch.finished_on) if batch.finished_on else None,
		"by_customer": [
			{"customer": r.customer, "customer_name": r.customer_name, "count": cint(r.count), "amount": flt(r.amount, 2)}
			for r in by_customer
		],
		"issues": issues,
	}


@frappe.whitelist()
def get_active_bulk_authorization():
	company = service.require_access()
	return _active_payload(company)


@frappe.whitelist()
def get_bulk_authorization_history(page=1, page_length=HISTORY_PAGE_LENGTH):
	"""Every Cartera Lote of the company, newest first. Read-only."""
	company = service.require_access()
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length) or HISTORY_PAGE_LENGTH, 1), 100)
	filters = {"company": company}
	total = frappe.db.count(service.BATCH_DOCTYPE, filters)
	rows = frappe.get_list(
		service.BATCH_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"mode",
			"status",
			"requested_by",
			"reason",
			"date_type",
			"date_from",
			"date_to",
			"total_selected",
			"authorized_count",
			"skipped_count",
			"failed_count",
			"currency",
			"total_amount",
			"authorized_amount",
			"queued_on",
			"finished_on",
		],
		order_by="creation desc",
		start=(page - 1) * page_length,
		page_length=page_length,
	)
	for r in rows:
		r["requested_by_name"] = get_fullname(r.requested_by) if r.requested_by else None
		for f in ("date_from", "date_to", "queued_on", "finished_on"):
			r[f] = str(r[f]) if r.get(f) else None
		r["total_amount"] = flt(r.total_amount, 2)
		r["authorized_amount"] = flt(r.authorized_amount, 2)
	return {"items": rows, "total": total, "page": page, "page_length": page_length, "has_more": page * page_length < total}


@frappe.whitelist(methods=["POST"])
def resume_bulk_authorization(batch_name):
	company = service.require_access()
	_load_batch(batch_name, company)
	service.resume(batch_name)
	return get_bulk_authorization_status(batch_name)


@frappe.whitelist(methods=["POST"])
def close_bulk_authorization(batch_name):
	company = service.require_access()
	_load_batch(batch_name, company)
	service.close(batch_name)
	return get_bulk_authorization_status(batch_name)
