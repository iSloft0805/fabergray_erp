# -*- coding: utf-8 -*-
"""fabergray_erp/entrega_masiva_service.py -- Entrega masiva de pedidos
(regularización administrativa de entregas ya ocurridas físicamente).

What a bulk delivery does to ONE order is exactly what a valid individual
delivery does (api.recorridos._deliver_stop()), minus the evidence nobody
captured, and nothing else:

- the order's stop reaches "Entregado" through the ORM, so
  RecorridoParada.validate() checks the transition and on_update() runs
  reconcile_recorrido_status() -- a route closes ONLY when every one of its
  stops is delivered, operational or administrative alike;
- its Cartera Obligacion is created by the existing
  cartera_service.ensure_obligation_for_stop() -- invoice payment method and
  due date honoured, historical rule for invoices without them, idempotent
  (unique index per stop), never a payment;
- no Stock Entry, Stock Ledger Entry, GL Entry, Payment Entry or Sales
  Invoice is ever created or changed: stock left at COMPLETAR PEDIDO
  (stock_issue_service), and a delivery never moves it.

Atomic per order: the stop's delivery and its obligation run inside one
savepoint; if Cartera fails, that order's delivery is rolled back too and
the order is reported Fallido (it stays eligible for a retry).

The missing evidence is replaced by an explicit, permanent identity, never
by fake data:
- Recorrido Parada.delivery_mode = "Regularización administrativa", plus the
  batch, the declared effective DAY (effective_delivery_date) and the REAL
  time it was registered (regularized_on). delivered_on carries the declared
  day at 00:00: that time is not a verified hour, and no screen shows it;
- orders without an active route are delivered inside an administrative
  regularization Recorrido (route_kind) -- no driver, vehicle or coordinates
  are invented. It only exists if at least one of its orders was delivered.
Both are reachable ONLY through this module's private token (a Python
object, never producible from JSON): the controllers refuse them from Desk,
frappe.client and REST, System Manager included, and every operational route
keeps its evidence requirements untouched.

Execution: a background job (queue "long") processes the batch in chunks of
CHUNK_SIZE orders, one committed transaction per chunk, so it scales to
thousands and an interruption loses at most the uncommitted chunk (rolled
back by the database) -- the batch is then resumed from its pending orders
without ever delivering one twice. A per-company unique lock allows a
single active batch; a batch whose worker died (no heartbeat for
STALE_AFTER_MINUTES) can be resumed or closed safely. The worker token
guarantees that a resumed batch is never processed by two workers at once.

System Manager only (api/entrega_masiva.py). Writes run as that user with
real permissions; the only ignore_permissions in the whole path is the
existing one inside cartera_service (documented there).
"""

import uuid

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.fabrigray_erp.doctype.recorrido_parada.recorrido_parada import (
	DELIVERY_MODE_REGULARIZATION,
)

ROUTE_KIND_REGULARIZATION = "Regularización administrativa"

DATE_MODE_COMMON = "Fecha común"
DATE_MODE_INVOICE = "Fecha de facturación de cada pedido"
DATE_MODES = (DATE_MODE_COMMON, DATE_MODE_INVOICE)
DATE_CRITERION = "Fecha de facturación del pedido (hora del sitio)"

STATUS_QUEUED = "En cola"
STATUS_RUNNING = "En proceso"
STATUS_INTERRUPTED = "Interrumpido"
STATUS_DONE = "Completado"
STATUS_DONE_WITH_ERRORS = "Completado con errores"
STATUS_CLOSED = "Cerrado"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_INTERRUPTED)

RESULT_PENDING = "Pendiente"
RESULT_DELIVERED = "Entregado"
RESULT_SKIPPED = "Omitido"
RESULT_FAILED = "Fallido"

ORIGIN_NO_ROUTE = "Sin recorrido"
ORIGIN_EN_ROUTE = "Recorrido en ruta"

CHUNK_SIZE = 50
STALE_AFTER_MINUTES = 10
MAX_RANGE_DAYS = 366
MAX_BATCH_ORDERS = 5000
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500
PREVIEW_CACHE_SECONDS = 600
JOB_TIMEOUT_SECONDS = 4 * 3600

ACTIVE_ROUTE_STATUSES = ("Borrador", "Planificado", "En Ruta")

#: Why an order cannot be delivered in bulk. Shown as-is in the preview and
#: stored on the per-order result -- nothing is ever excluded silently.
EXCLUSION_REASONS = {
	"ya_entregado": "Ya entregado",
	"obligacion_inconsistente": "Tiene obligación de Cartera pero ninguna entrega registrada (inconsistencia: requiere revisión)",
	"en_recorrido_borrador": "Asignado a un recorrido en Borrador (no se desasigna automáticamente)",
	"en_recorrido_planificado": "Asignado a un recorrido Planificado (no se desasigna automáticamente)",
	"en_regularizacion": "En otra entrega masiva en curso",
	"recorridos_inconsistentes": "Asignado a más de un recorrido activo o con una parada no entregable (requiere revisión)",
	"pick_list_cancelado": "Pick List cancelado",
	"pick_list_no_completado": "Pedido no completado en Bodega (p. ej. facturado con faltantes pendientes)",
	"pedido_cancelado": "Orden de venta cancelada o inexistente",
	"factura_posterior": "Facturado después de la fecha efectiva de entrega",
	"otra_empresa": "Pertenece a otra empresa",
	"no_facturado": "No está facturado",
}

#: Informative only -- never block, never correct anything.
WARNINGS = {
	"sin_salida_inventario": "Sin salida de inventario registrada (histórico): solo informativo, no se genera ni corrige inventario",
	"parada_en_recorrido_cerrado": "Tiene una parada Pendiente en un recorrido Completado/Cancelado (histórico)",
}


class EntregaMasivaError(frappe.ValidationError):
	pass


class ActiveBatchError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# Private service token
# ---------------------------------------------------------------------------

_SERVICE_TOKEN = object()


def authorize(doc):
	doc.flags.entrega_masiva_token = _SERVICE_TOKEN
	return doc


def is_authorized(doc):
	return doc.flags.get("entrega_masiva_token") is _SERVICE_TOKEN


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def require_access():
	"""System Manager only (v1), plus real write permission on Recorrido and
	Recorrido Parada, and the site's company -- never one sent by a client."""
	from fabergray_erp.api.bodega import _require_login
	from fabergray_erp.permission_conditions import _allowed_companies

	_require_login()
	frappe.only_for("System Manager")
	frappe.has_permission("Recorrido", "write", throw=True)
	frappe.has_permission("Recorrido Parada", "write", throw=True)
	company = get_default_company()
	allowed = _allowed_companies()
	if not company or (allowed is not None and company not in allowed):
		frappe.throw(_("No tienes acceso a los recorridos de esta empresa."), frappe.PermissionError)
	return company


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def _parse_date(value, label):
	try:
		return getdate(value) if value else None
	except Exception:
		frappe.throw(_("{0}: fecha inválida.").format(label), EntregaMasivaError)


def validate_params(date_from, date_to, date_mode, effective_date):
	"""Normalized (date_from, date_to, date_mode, effective_date). Inclusive
	range by invoice date; no future effective date (site time zone)."""
	today = getdate(nowdate())
	date_from = _parse_date(date_from, _("Fecha inicial"))
	date_to = _parse_date(date_to, _("Fecha final"))
	if not date_from or not date_to:
		frappe.throw(_("Indica la fecha inicial y la fecha final."), EntregaMasivaError)
	if date_from > date_to:
		frappe.throw(_("La fecha inicial no puede ser posterior a la fecha final."), EntregaMasivaError)
	if date_to > today:
		frappe.throw(_("La fecha final no puede ser futura."), EntregaMasivaError)
	if (date_to - date_from).days > MAX_RANGE_DAYS:
		frappe.throw(_("El rango no puede superar {0} días.").format(MAX_RANGE_DAYS), EntregaMasivaError)
	date_mode = date_mode or DATE_MODE_COMMON
	if date_mode not in DATE_MODES:
		frappe.throw(_("Modalidad de fecha inválida."), EntregaMasivaError)
	if date_mode == DATE_MODE_COMMON:
		effective_date = _parse_date(effective_date, _("Fecha efectiva de entrega"))
		if not effective_date:
			frappe.throw(_("Indica la fecha efectiva de entrega."), EntregaMasivaError)
		if effective_date > today:
			frappe.throw(_("La fecha efectiva de entrega no puede ser futura."), EntregaMasivaError)
	else:
		effective_date = None
	return date_from, date_to, date_mode, effective_date


# ---------------------------------------------------------------------------
# Candidates and classification (shared by preview, confirm and each chunk)
# ---------------------------------------------------------------------------


def _chunks(values, size):
	values = list(values)
	for i in range(0, len(values), size):
		yield values[i : i + size]


def load_candidates(company, date_from=None, date_to=None, pick_lists=None):
	"""Every Facturado Pick List of `company` invoiced in [date_from,
	date_to] (or exactly `pick_lists`), with every fact classification needs,
	batch-loaded (a fixed number of queries per 500 orders, never one per
	order). Read through frappe.get_list: real permissions apply."""
	filters = [["fg_invoicing_status", "=", "Facturado"], ["company", "=", company]]
	if pick_lists is not None:
		if not pick_lists:
			return []
		filters.append(["name", "in", list(pick_lists)])
	else:
		filters += [
			["fg_invoiced_on", ">=", f"{date_from} 00:00:00"],
			["fg_invoiced_on", "<=", f"{date_to} 23:59:59.999999"],
		]
	rows = frappe.get_list(
		"Pick List",
		filters=filters,
		fields=[
			"name",
			"docstatus",
			"company",
			"customer",
			"customer_name",
			"fg_invoiced_on",
			"fg_invoice_payment_method",
			"fg_invoice_due_date",
			"fg_invoice_issuer",
		],
		order_by="fg_invoiced_on asc, name asc",
		limit_page_length=0,
	)
	if not rows:
		return []
	by_name = {r.name: frappe._dict(r, stops=[], obligations=[], sales_order=None, so_docstatus=None, stock_issue=False) for r in rows}
	names = list(by_name)

	for part in _chunks(names, 500):
		for item in frappe.get_list(
			"Pick List Item",
			filters={"parent": ["in", part]},
			fields=["parent", "sales_order", "idx"],
			order_by="parent asc, idx asc",
			parent_doctype="Pick List",
			limit_page_length=0,
		):
			c = by_name[item.parent]
			if c.sales_order is None and item.sales_order:
				c.sales_order = item.sales_order
		for stop in frappe.get_list(
			"Recorrido Parada",
			filters={"pick_list": ["in", part]},
			fields=["name", "pick_list", "status", "recorrido"],
			limit_page_length=0,
		):
			by_name[stop.pick_list].stops.append(stop)
		for ob in frappe.get_list(
			"Cartera Obligacion",
			filters={"pick_list": ["in", part]},
			fields=["name", "pick_list", "recorrido_parada", "status"],
			limit_page_length=0,
		):
			by_name[ob.pick_list].obligations.append(ob)
		for issue in frappe.get_list(
			"Stock Entry",
			filters={"pick_list": ["in", part], "purpose": "Material Issue", "docstatus": 1},
			fields=["pick_list"],
			limit_page_length=0,
		):
			by_name[issue.pick_list].stock_issue = True

	sales_orders = sorted({c.sales_order for c in by_name.values() if c.sales_order})
	so_status, so_names = {}, {}
	for part in _chunks(sales_orders, 500):
		for so in frappe.get_list(
			"Sales Order", filters={"name": ["in", part]}, fields=["name", "docstatus"], limit_page_length=0
		):
			so_status[so.name] = so.docstatus
	routes = sorted({s.recorrido for c in by_name.values() for s in c.stops})
	route_info = {}
	for part in _chunks(routes, 500):
		for r in frappe.get_list(
			"Recorrido",
			filters={"name": ["in", part]},
			fields=["name", "status", "route_kind", "entrega_masiva"],
			limit_page_length=0,
		):
			route_info[r.name] = r

	from fabergray_erp.sales_order_naming import root_commercial_name

	for c in by_name.values():
		c.so_docstatus = so_status.get(c.sales_order)
		if c.sales_order and c.sales_order not in so_names:
			so_names[c.sales_order] = root_commercial_name(c.sales_order)
		c.commercial_name = so_names.get(c.sales_order) if c.sales_order else None
		for s in c.stops:
			info = route_info.get(s.recorrido) or frappe._dict()
			s.route_status = info.get("status")
			s.route_kind = info.get("route_kind")
	return [by_name[n] for n in names]


def classify(c, date_mode, effective_date, company=None):
	"""(code_or_None, detail, warnings, origin, origin_route, origin_stop,
	effective_delivery_date). code None = eligible. Pure: reads only `c`."""
	warnings = []
	if not c.stock_issue:
		warnings.append("sin_salida_inventario")
	invoice_date = getdate(c.fg_invoiced_on) if c.fg_invoiced_on else None
	effective = invoice_date if date_mode == DATE_MODE_INVOICE else getdate(effective_date) if effective_date else None
	result = lambda code, detail=None, **kw: (  # noqa: E731
		code,
		detail,
		warnings,
		kw.get("origin"),
		kw.get("origin_route"),
		kw.get("origin_stop"),
		effective,
	)

	if company and c.company != company:
		return result("otra_empresa")
	if c.docstatus == 2:
		return result("pick_list_cancelado")
	if c.docstatus != 1:
		return result("pick_list_no_completado")
	if not c.sales_order or c.so_docstatus != 1:
		return result("pedido_cancelado")

	delivered = [s for s in c.stops if s.status == "Entregado"]
	if delivered:
		valid = [o.name for o in c.obligations if o.recorrido_parada in {s.name for s in delivered}]
		detail = (
			_("Obligación de Cartera válida: {0}").format(", ".join(valid))
			if valid
			else _("Sin obligación de Cartera todavía (el conciliador de Cartera la crea)")
		)
		return result("ya_entregado", detail)
	if c.obligations:
		return result("obligacion_inconsistente", ", ".join(o.name for o in c.obligations))

	active = [s for s in c.stops if s.route_status in ACTIVE_ROUTE_STATUSES]
	if any(s.route_status in ("Completado", "Cancelado") and s.status == "Pendiente" for s in c.stops):
		warnings.append("parada_en_recorrido_cerrado")
	if len(active) > 1:
		return result("recorridos_inconsistentes", ", ".join(s.recorrido for s in active))
	origin, origin_route, origin_stop = ORIGIN_NO_ROUTE, None, None
	if active:
		stop = active[0]
		if stop.route_status == "Borrador":
			return result("en_recorrido_borrador", stop.recorrido)
		if stop.route_status == "Planificado":
			return result("en_recorrido_planificado", stop.recorrido)
		if stop.route_kind == ROUTE_KIND_REGULARIZATION:
			return result("en_regularizacion", stop.recorrido)
		if stop.status != "Pendiente":
			return result("recorridos_inconsistentes", stop.recorrido)
		origin, origin_route, origin_stop = ORIGIN_EN_ROUTE, stop.recorrido, stop.name

	if not invoice_date or not effective or effective < invoice_date:
		return result("factura_posterior", _("Facturado el {0}").format(invoice_date))
	return result(None, None, origin=origin, origin_route=origin_route, origin_stop=origin_stop)


def _row(c, classification, amount=None, amount_available=False):
	code, detail, warnings, origin, origin_route, _stop, effective = classification
	return {
		"pick_list": c.name,
		"sales_order": c.sales_order,
		"commercial_name": c.commercial_name or c.sales_order,
		"customer": c.customer,
		"customer_name": c.customer_name or c.customer,
		"invoice_date": str(getdate(c.fg_invoiced_on)) if c.fg_invoiced_on else None,
		"payment_method": c.fg_invoice_payment_method or None,
		"due_date": str(getdate(c.fg_invoice_due_date)) if c.fg_invoice_due_date else None,
		"issuer": c.fg_invoice_issuer or None,
		"eligible": code is None,
		"reason_code": code,
		"reason": _(EXCLUSION_REASONS[code]) if code else None,
		"reason_detail": detail,
		"needs_review": code in ("obligacion_inconsistente", "recorridos_inconsistentes"),
		"warnings": [{"code": w, "label": _(WARNINGS[w])} for w in warnings],
		"origin": origin,
		"origin_recorrido": origin_route,
		"effective_delivery_date": str(effective) if effective and code is None else None,
		"amount": flt(amount, 2) if amount_available else None,
		"amount_available": bool(amount_available),
	}


def _amount(c):
	amount, source, _currency = cartera_service.compute_invoice_amount(c.name, c.company)
	return amount, source == cartera_service.AMOUNT_SOURCE_INVOICE


def build_preview(company, date_from, date_to, date_mode, effective_date):
	"""Read-only: every candidate classified, eligible ones with their
	commercial-invoice amount (the same total as the PDF and Cartera)."""
	rows = []
	for c in load_candidates(company, date_from, date_to):
		classification = classify(c, date_mode, effective_date, company)
		if classification[0] is None:
			amount, available = _amount(c)
			rows.append(_row(c, classification, amount, available))
		else:
			rows.append(_row(c, classification))
	return rows


def summarize(rows):
	eligible = [r for r in rows if r["eligible"]]
	excluded = [r for r in rows if not r["eligible"]]
	by_reason = {}
	for r in excluded:
		entry = by_reason.setdefault(
			r["reason_code"], {"code": r["reason_code"], "label": r["reason"], "count": 0, "needs_review": r["needs_review"]}
		)
		entry["count"] += 1
	warnings = {}
	for r in eligible:
		for w in r["warnings"]:
			warnings.setdefault(w["code"], {"code": w["code"], "label": w["label"], "count": 0})["count"] += 1
	return {
		"found": len(rows),
		"eligible": len(eligible),
		"excluded": len(excluded),
		"total_amount": flt(sum(r["amount"] or 0 for r in eligible), 2),
		"amount_unavailable": sum(1 for r in eligible if not r["amount_available"]),
		"from_route_in_progress": sum(1 for r in eligible if r["origin"] == ORIGIN_EN_ROUTE),
		"excluded_by_reason": sorted(by_reason.values(), key=lambda e: -e["count"]),
		"warnings": sorted(warnings.values(), key=lambda e: -e["count"]),
	}


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------


def stale_cutoff():
	return add_to_date(now_datetime(), minutes=-STALE_AFTER_MINUTES)


def is_stale(batch):
	if batch.status not in (STATUS_QUEUED, STATUS_RUNNING):
		return False
	last = batch.heartbeat_on or batch.started_on or batch.queued_on or batch.creation
	return bool(last) and get_datetime(last) < stale_cutoff()


def active_batch(company):
	name = frappe.db.get_value("Entrega Masiva", {"active_company_lock": company}, "name")
	return frappe.get_doc("Entrega Masiva", name) if name else None


def create_batch(company, params, selected, reason, physical_confirmed, invoice_date_confirmed, client_request_id):
	"""Inserts the batch (unique active lock per company) and one Pendiente
	result per selected order, then enqueues the job. `selected`: list of
	(candidate, classification, amount, amount_available) -- already
	revalidated by the caller in this same request."""
	date_from, date_to, date_mode, effective_date = params
	batch = frappe.get_doc(
		{
			"doctype": "Entrega Masiva",
			"company": company,
			"status": STATUS_QUEUED,
			"requested_by": frappe.session.user,
			"reason": reason,
			"date_criterion": DATE_CRITERION,
			"date_from": date_from,
			"date_to": date_to,
			"date_mode": date_mode,
			"effective_delivery_date": effective_date,
			"physical_delivery_confirmed": 1 if physical_confirmed else 0,
			"invoice_date_confirmed": 1 if invoice_date_confirmed else 0,
			"total_selected": len(selected),
			"total_amount": flt(sum(a or 0 for _c, _k, a, ok in selected if ok), 2),
			"queued_on": now_datetime(),
			"heartbeat_on": now_datetime(),
			"client_request_id": client_request_id,
			"active_company_lock": company,
			"worker_token": uuid.uuid4().hex,
		}
	)
	frappe.db.savepoint("em_batch_insert")
	try:
		authorize(batch).insert()
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point="em_batch_insert")
		existing = frappe.db.get_value("Entrega Masiva", {"client_request_id": client_request_id}, "name")
		if existing:
			return frappe.get_doc("Entrega Masiva", existing), False
		running = active_batch(company)
		frappe.throw(
			_("Ya hay una entrega masiva activa ({0}). Espera a que termine o reanúdala si se interrumpió.").format(
				running.name if running else "?"
			),
			ActiveBatchError,
		)

	for c, classification, amount, available in selected:
		code, detail, warnings, origin, origin_route, _stop, effective = classification
		authorize(
			frappe.get_doc(
				{
					"doctype": "Entrega Masiva Pedido",
					"entrega_masiva": batch.name,
					"pick_list": c.name,
					"sales_order": c.sales_order,
					"commercial_name": c.commercial_name,
					"customer": c.customer,
					"customer_name": c.customer_name,
					"invoice_date": getdate(c.fg_invoiced_on) if c.fg_invoiced_on else None,
					"invoice_payment_method": c.fg_invoice_payment_method or None,
					"amount": amount if available else 0,
					"effective_delivery_date": effective,
					"origin": origin or ORIGIN_NO_ROUTE,
					"origin_recorrido": origin_route,
					"stock_issue_warning": 1 if "sin_salida_inventario" in warnings else 0,
					"result": RESULT_PENDING,
				}
			)
		).insert()
	enqueue(batch)
	return batch, True


def enqueue(batch):
	batch.db_set("job_id", f"entrega_masiva::{batch.name}::{batch.worker_token[:8]}", update_modified=False)
	in_test = bool(frappe.flags.in_test or getattr(frappe, "in_test", False))
	if in_test and frappe.flags.get("entrega_masiva_defer_job"):
		# Tests that drive run_batch() by hand (interruption/recovery): no
		# job is queued at all, never a stray job left in Redis.
		return
	frappe.enqueue(
		"fabergray_erp.entrega_masiva_service.run_batch",
		queue="long",
		timeout=JOB_TIMEOUT_SECONDS,
		job_id=batch.job_id,
		enqueue_after_commit=True,
		now=in_test,
		batch_name=batch.name,
		worker_token=batch.worker_token,
	)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _lock_batch(batch_name):
	row = frappe.db.get_value(
		"Entrega Masiva", batch_name, ["name", "status", "worker_token"], as_dict=True, for_update=True
	)
	return row


def run_batch(batch_name, worker_token):
	"""Background job. Processes pending orders chunk by chunk, committing
	each chunk. Stops at once if the batch was taken over (resume) or closed.
	An unexpected crash marks the batch Interrumpido (resumable); a killed
	worker leaves it En proceso with a stale heartbeat (also resumable)."""
	try:
		_run_batch(batch_name, worker_token)
	except Exception:
		frappe.db.rollback()
		mark_interrupted(batch_name, frappe.get_traceback(), worker_token=worker_token)
		frappe.db.commit()
		frappe.log_error(title=_("Entrega masiva {0} interrumpida").format(batch_name))


def _run_batch(batch_name, worker_token):
	row = _lock_batch(batch_name)
	if not row or row.worker_token != worker_token or row.status not in (STATUS_QUEUED, STATUS_RUNNING):
		frappe.db.rollback()
		return
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	batch.status = STATUS_RUNNING
	batch.started_on = batch.started_on or now_datetime()
	batch.heartbeat_on = now_datetime()
	authorize(batch).save()
	frappe.db.commit()

	while True:
		row = _lock_batch(batch_name)
		if not row or row.worker_token != worker_token or row.status != STATUS_RUNNING:
			frappe.db.rollback()
			return
		pending = frappe.get_list(
			"Entrega Masiva Pedido",
			filters={"entrega_masiva": batch_name, "result": RESULT_PENDING},
			pluck="name",
			order_by="name asc",
			limit_page_length=CHUNK_SIZE,
		)
		if not pending:
			break
		_run_chunk_safely(batch_name, pending)
		_refresh_counters(batch_name, heartbeat=True)
		frappe.db.commit()

	_finish(batch_name)
	frappe.db.commit()


def _run_chunk_safely(batch_name, item_names):
	"""A deadlock retries the whole chunk (InnoDB already rolled the
	transaction back); any other chunk-level error falls back to one order
	per transaction so a single bad order can never block the rest."""
	for attempt in range(3):
		try:
			process_chunk(batch_name, item_names)
			return
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			_lock_batch(batch_name)
			continue
		except Exception:
			frappe.db.rollback()
			_lock_batch(batch_name)
			break
	if len(item_names) == 1:
		frappe.db.rollback()
		_lock_batch(batch_name)
		_mark_items(item_names, RESULT_FAILED, _("Error inesperado: ver el registro de errores."))
		frappe.log_error(title=_("Entrega masiva {0}: pedido fallido").format(batch_name))
		return
	for name in item_names:
		_run_chunk_safely(batch_name, [name])
		frappe.db.commit()
		_lock_batch(batch_name)


def _mark_items(item_names, result, message, **values):
	for name in item_names:
		item = frappe.get_doc("Entrega Masiva Pedido", name, for_update=True)
		if item.result != RESULT_PENDING:
			continue
		item.update({"result": result, "message": message, "processed_on": now_datetime(), **values})
		authorize(item).save()


def _discard_messages_since(count):
	log = frappe.local.message_log or []
	del log[count:]


def process_chunk(batch_name, item_names):
	"""One transaction: revalidates every order under the same locks
	deliver_stop() uses, then delivers each eligible order atomically
	(delivery + Cartera in one savepoint)."""
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	items = []
	for name in sorted(item_names):
		item = frappe.get_doc("Entrega Masiva Pedido", name, for_update=True)
		if item.result == RESULT_PENDING:
			items.append(item)
	if not items:
		return

	from fabergray_erp.api import recorridos

	# Fresh facts for these orders (a chunk starts a new transaction).
	candidates = {c.name: c for c in load_candidates(batch.company, pick_lists=[i.pick_list for i in items])}
	in_place, unassigned = [], []
	for item in items:
		c = candidates.get(item.pick_list)
		if not c:
			_finish_item(item, RESULT_SKIPPED, _(EXCLUSION_REASONS["no_facturado"]))
			continue
		classification = classify(c, batch.date_mode, batch.effective_delivery_date, batch.company)
		code = classification[0]
		if code:
			_finish_item(item, RESULT_SKIPPED, _reason_text(code, classification[1]))
			continue
		target = (item, c, classification)
		(in_place if classification[3] == ORIGIN_EN_ROUTE else unassigned).append(target)

	if unassigned:
		_deliver_unassigned(batch, unassigned, recorridos)
	for target in in_place:
		_deliver_in_place(batch, target, recorridos)


def _reason_text(code, detail):
	text = _(EXCLUSION_REASONS[code])
	return f"{text} ({detail})" if detail else text


def _finish_item(item, result, message, **values):
	item.update({"result": result, "message": message, "processed_on": now_datetime(), **values})
	authorize(item).save()


def _payment_status_for(c):
	"""What the stop records as the payment situation. Never "Pagado":
	nobody confirmed money. Crédito only when the invoice says so; immediate
	invoices and historical ones without terms stay "Pendiente por Pago"."""
	return "Crédito" if c.fg_invoice_payment_method == "Crédito" else "Pendiente por Pago"


def _deliver_stop_regularized(stop, c, batch, effective):
	stop.delivery_mode = DELIVERY_MODE_REGULARIZATION
	stop.entrega_masiva = batch.name
	stop.effective_delivery_date = effective
	stop.delivered_on = get_datetime(f"{effective} 00:00:00")
	stop.regularized_on = now_datetime()
	stop.delivered_by = frappe.session.user
	stop.payment_status = _payment_status_for(c)
	stop.delivery_note = _("Regularización administrativa (entrega masiva {0}).").format(batch.name)
	stop.status = "Entregado"
	authorize(stop).save()
	obligation, _created = cartera_service.ensure_obligation_for_stop(stop.name)
	return obligation


def _deliver_in_place(batch, target, recorridos):
	"""An order whose stop is Pendiente in an OPERATIONAL En Ruta route:
	delivered in that same stop, under the same lock order as deliver_stop()
	(route row, stop row, Pick List); the route closes only if this was its
	last pending stop."""
	item, c, classification = target
	effective = classification[6]
	stop_name = classification[5]
	route_name = classification[4]
	messages = len(frappe.local.message_log or [])
	savepoint = f"em_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		route_status = frappe.db.get_value("Recorrido", route_name, "status", for_update=True)
		locked = frappe.db.get_value(
			"Recorrido Parada", stop_name, ["name", "status", "recorrido"], as_dict=True, for_update=True
		)
		if locked and locked.status == "Entregado":
			frappe.db.rollback(save_point=savepoint)
			_finish_item(item, RESULT_SKIPPED, _(EXCLUSION_REASONS["ya_entregado"]), recorrido=route_name, recorrido_parada=stop_name)
			return
		if route_status != "En Ruta" or not locked or locked.status != "Pendiente" or locked.recorrido != route_name:
			frappe.db.rollback(save_point=savepoint)
			_finish_item(item, RESULT_SKIPPED, _("El recorrido o la parada cambiaron de estado; revisa el pedido."))
			return
		pick_list = frappe.get_doc("Pick List", c.name)
		assigned = recorridos._lock_and_get_assigned([c.name], exclude_route=route_name)
		recorridos._validate_pick_list_eligible(pick_list, batch.company, assigned)
		stop = frappe.get_doc("Recorrido Parada", stop_name)
		obligation = _deliver_stop_regularized(stop, c, batch, effective)
	except frappe.QueryDeadlockError:
		raise
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		_discard_messages_since(messages)
		_finish_item(item, RESULT_FAILED, _short_error(e), recorrido=route_name)
		return
	frappe.db.release_savepoint(savepoint)
	_finish_item(
		item,
		RESULT_DELIVERED,
		_("Entregado en su recorrido en ruta."),
		recorrido=route_name,
		recorrido_parada=stop_name,
		cartera_obligacion=obligation,
	)


def _deliver_unassigned(batch, targets, recorridos):
	"""Orders without an active route: one administrative regularization
	route for this chunk, created only if at least one order is delivered."""
	names = [c.name for _i, c, _k in targets]
	recorridos._lock_pick_lists(names)
	assigned = recorridos._locked_assigned_pick_lists(names)
	ready = []
	for item, c, classification in targets:
		if c.name in assigned:
			_finish_item(item, RESULT_SKIPPED, _("Asignado a un recorrido o entregado mientras se procesaba."))
		else:
			ready.append((item, c, classification))
	if not ready:
		return

	route_savepoint = f"em_route_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(route_savepoint)
	route = frappe.get_doc(
		{
			"doctype": "Recorrido",
			"company": batch.company,
			"route_date": nowdate(),
			"status": "Borrador",
			"route_kind": ROUTE_KIND_REGULARIZATION,
			"entrega_masiva": batch.name,
			"notes": _(
				"Regularización administrativa de entregas ya realizadas (entrega masiva {0}). Sin conductor, vehículo ni coordenadas."
			).format(batch.name),
		}
	)
	authorize(route).insert()
	stops = {}
	for seq, (item, c, _k) in enumerate(ready, start=1):
		pl = frappe.get_doc("Pick List", c.name)
		snap = recorridos._resolve_pick_list_snapshot(pl)
		stop = authorize(frappe.get_doc(
			{
				"doctype": "Recorrido Parada",
				"recorrido": route.name,
				"sequence": seq,
				"pick_list": c.name,
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
		)).insert()
		stops[c.name] = stop.name
	for status in ("Planificado", "En Ruta"):
		route = frappe.get_doc("Recorrido", route.name)
		route.status = status
		authorize(route).save()

	outcomes = []
	for item, c, classification in ready:
		messages = len(frappe.local.message_log or [])
		savepoint = f"em_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		try:
			stop = frappe.get_doc("Recorrido Parada", stops[c.name])
			obligation = _deliver_stop_regularized(stop, c, batch, classification[6])
		except frappe.QueryDeadlockError:
			raise
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			_discard_messages_since(messages)
			outcomes.append((item, c, False, _short_error(e), None))
			continue
		frappe.db.release_savepoint(savepoint)
		outcomes.append((item, c, True, None, obligation))

	delivered = [o for o in outcomes if o[2]]
	if not delivered:
		# Never leave an administrative route without a delivered order.
		frappe.db.rollback(save_point=route_savepoint)
		for item, _c, _ok, error, _ob in outcomes:
			_finish_item(item, RESULT_FAILED, error)
		return

	for item, c, ok, error, _ob in outcomes:
		if not ok:
			# The failed order's stop leaves the administrative route (its
			# delivery was already rolled back), so the route never closes with
			# an undelivered stop and the order stays eligible for a retry.
			stop = frappe.get_doc("Recorrido Parada", stops[c.name])
			authorize(stop).delete()
	recorridos.reconcile_recorrido_status(route.name)
	frappe.db.release_savepoint(route_savepoint)

	for item, c, ok, error, obligation in outcomes:
		if ok:
			_finish_item(
				item,
				RESULT_DELIVERED,
				_("Entregado mediante recorrido de regularización administrativa."),
				recorrido=route.name,
				recorrido_parada=stops[c.name],
				cartera_obligacion=obligation,
			)
		else:
			_finish_item(item, RESULT_FAILED, error)


def _short_error(e):
	text = frappe.utils.strip_html(str(e) or e.__class__.__name__).strip()
	return (text or e.__class__.__name__)[:400]


def _refresh_counters(batch_name, heartbeat=False):
	counts = {
		result: frappe.db.count("Entrega Masiva Pedido", {"entrega_masiva": batch_name, "result": result})
		for result in (RESULT_PENDING, RESULT_DELIVERED, RESULT_SKIPPED, RESULT_FAILED)
	}
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	batch.delivered_count = counts[RESULT_DELIVERED]
	batch.skipped_count = counts[RESULT_SKIPPED]
	batch.failed_count = counts[RESULT_FAILED]
	batch.processed_count = counts[RESULT_DELIVERED] + counts[RESULT_SKIPPED] + counts[RESULT_FAILED]
	if heartbeat:
		batch.heartbeat_on = now_datetime()
	authorize(batch).save()
	return batch, counts


def _finish(batch_name):
	batch, counts = _refresh_counters(batch_name)
	if counts[RESULT_PENDING]:
		return batch
	batch.status = STATUS_DONE_WITH_ERRORS if counts[RESULT_FAILED] else STATUS_DONE
	batch.finished_on = now_datetime()
	batch.active_company_lock = None
	authorize(batch).save()
	return batch


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def resume(batch_name):
	"""A batch whose worker died (stale heartbeat) or that was marked
	Interrumpido is re-enqueued with a NEW worker token: the old worker, if
	it ever wakes up, stops at its next chunk. Pending orders only --
	everything already committed is never redone."""
	row = _lock_batch(batch_name)
	if not row:
		frappe.throw(_("La entrega masiva no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	if not (batch.status == STATUS_INTERRUPTED or is_stale(batch)):
		frappe.throw(_("Solo se puede reanudar una entrega masiva interrumpida o sin avance reciente."), EntregaMasivaError)
	batch.status = STATUS_QUEUED
	batch.worker_token = uuid.uuid4().hex
	batch.heartbeat_on = now_datetime()
	batch.last_error = _("Reanudada por {0} el {1}.").format(frappe.session.user, now_datetime())
	authorize(batch).save()
	enqueue(batch)
	return batch


def close(batch_name):
	"""Releases the company lock of an interrupted/stale batch WITHOUT
	processing its pending orders (marked Omitido "lote cerrado"). Delivered
	orders stay delivered."""
	row = _lock_batch(batch_name)
	if not row:
		frappe.throw(_("La entrega masiva no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	if not (batch.status == STATUS_INTERRUPTED or is_stale(batch)):
		frappe.throw(_("Solo se puede cerrar una entrega masiva interrumpida o sin avance reciente."), EntregaMasivaError)
	batch.worker_token = uuid.uuid4().hex  # any old worker stops
	authorize(batch).save()
	pending = frappe.get_list(
		"Entrega Masiva Pedido", filters={"entrega_masiva": batch_name, "result": RESULT_PENDING}, pluck="name", limit_page_length=0
	)
	_mark_items(pending, RESULT_SKIPPED, _("Lote cerrado sin procesar este pedido."))
	batch, _counts = _refresh_counters(batch_name)
	batch.status = STATUS_CLOSED
	batch.finished_on = now_datetime()
	batch.active_company_lock = None
	authorize(batch).save()
	return batch


def mark_interrupted(batch_name, error, worker_token=None):
	row = _lock_batch(batch_name)
	if not row or (worker_token and row.worker_token != worker_token):
		return
	batch = frappe.get_doc("Entrega Masiva", batch_name)
	if batch.status in (STATUS_QUEUED, STATUS_RUNNING):
		batch.status = STATUS_INTERRUPTED
		batch.last_error = (error or "")[:1000]
		authorize(batch).save()
