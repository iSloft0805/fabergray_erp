# -*- coding: utf-8 -*-
"""fabergray_erp/cartera_masiva_service.py -- Cartera masiva, fase A:
autorización masiva de cobros reportados por el conductor.

What a bulk authorization does to ONE obligation is EXACTLY what the
individual CONFIRMAR PAGO does, and nothing else: it calls
cartera_service.confirm_driver_payment() -- the same lock (obligation row,
then its driver payments), the same checks (unresolved report, exactly one
submitted Conductor payment for the full invoiced value, balance 0, status
Pagado), the same single transition "Sin confirmar" -> "Confirmado" written
through the controller with the private Cartera token. It never creates,
changes or cancels a payment, never changes an amount, a balance or a due
date, and creates no Payment Entry / Journal Entry / GL Entry / stock
movement (Cartera stays "Sin contabilizar"). The only addition is the
traceability field payment_verified_batch (the batch that authorized it).

Population: obligations whose driver reported "Pagado". Eligible only the
POR CONFIRMAR ones (a real driver payment with proof, unresolved). Every
other one is shown EXCLUDED with its reason -- never silently: a "Pagado"
report WITHOUT proof has no payment to confirm (it will be regularized by
"Registrar pagos", fase B), confirmed/rejected ones are already resolved,
and inconsistent ones need review.

Integrity:
- confirm revalidates every selected obligation from the database (never
  trusts the preview or the browser);
- the worker revalidates each obligation again under its row lock, then
  calls the service inside a savepoint: one obligation = one atomic unit
  (a failure rolls back only that obligation); the service never commits;
- the balance is compared before/after under the same lock: any change
  aborts that obligation (defensive -- the service cannot change it);
- concurrent individual CONFIRMAR/RECHAZAR serialize on the same row
  lock: whichever comes second sees the first one's result (already
  confirmed -> Omitido; rejected -> Omitido, never confirmed).

Execution (same pattern as entrega_masiva_service): background job on queue
"long", chunks of CHUNK_SIZE committed one by one, a per-company unique lock
shared by every Cartera Lote mode (one active batch per company), heartbeat
+ worker token so a dead worker's batch is resumed (or closed) without ever
processing an obligation twice, idempotent per client_request_id.

System Manager only (api/cartera_masiva.py). Cartera Lote / Cartera Lote
Linea are read-only DocTypes whose controllers require this module's private
token; ignore_permissions on their own inserts/saves is safe for that reason
(no user input reaches them except the validated reason and filters).
"""

import json
import uuid

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service

BATCH_DOCTYPE = "Cartera Lote"
LINE_DOCTYPE = "Cartera Lote Linea"

MODE_AUTHORIZATION = "Autorización de cobros"
MODE_PAYMENTS = "Registro de pagos"
#: Fase B (Registrar pagos) is NOT implemented: the mode exists in the
#: DocType so the single per-company lock already covers both, but no
#: batch of that mode can be created.
ENABLED_MODES = (MODE_AUTHORIZATION,)

STATUS_QUEUED = "En cola"
STATUS_RUNNING = "En proceso"
STATUS_INTERRUPTED = "Interrumpido"
STATUS_DONE = "Completado"
STATUS_DONE_WITH_ERRORS = "Completado con errores"
STATUS_CLOSED = "Cerrado"
FINISHED_STATUSES = (STATUS_DONE, STATUS_DONE_WITH_ERRORS, STATUS_CLOSED)

RESULT_PENDING = "Pendiente"
RESULT_AUTHORIZED = "Autorizado"
RESULT_SKIPPED = "Omitido"
RESULT_FAILED = "Fallido"

CHUNK_SIZE = 50
STALE_AFTER_MINUTES = 10
MAX_RANGE_DAYS = 366
MAX_BATCH_LINES = 2000
MAX_CUSTOMERS_FILTER = 500
MAX_SEARCH_LENGTH = 140
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500
PREVIEW_CACHE_SECONDS = 600
JOB_TIMEOUT_SECONDS = 4 * 3600

#: Date criterion of the range (inclusive, site time zone). Missing dates are
#: never invented: an obligation without that date simply does not fall in
#: any range (the preview reports how many were left out for that reason).
DATE_INVOICE = "facturacion"
DATE_DELIVERY = "entrega"
DATE_DUE = "vencimiento"
DATE_PAYMENT = "pago"
DATE_TYPES = {
	DATE_INVOICE: "Fecha de facturación",
	DATE_DELIVERY: "Fecha de entrega",
	DATE_DUE: "Fecha de vencimiento",
	DATE_PAYMENT: "Fecha del pago reportado",
}
DEFAULT_DATE_TYPE = DATE_INVOICE

NO_PAYMENT_METHOD = "__sin_forma__"

#: Status filter -- over the classification below.
STATUS_FILTERS = {
	"todos": "Todos",
	"por_confirmar": "Por confirmar",
	"sin_comprobante": "Reportado pagado sin comprobante",
	"confirmado": "Ya confirmados",
	"rechazado": "Rechazados",
	"revision": "Requieren revisión",
}

#: Why an obligation cannot be authorized in bulk -- shown as-is and stored
#: on its line; nothing is ever excluded silently.
EXCLUSION_REASONS = {
	"sin_comprobante": "Reportado como pagado sin comprobante: no hay pago que confirmar (se regulariza en «Registrar pagos», próximamente)",
	"ya_confirmado": "El pago del conductor ya fue confirmado",
	"rechazado": "El reporte del conductor fue rechazado",
	"anulada": "Obligación anulada",
	"sin_valor": "La obligación no tiene un valor facturado definido",
	"sin_verificacion": "Reporte de pago sin estado de verificación (requiere revisión)",
	"multiples_pagos": "Tiene más de un pago del conductor activo (requiere revisión)",
	"no_coincide": "El pago del conductor no coincide con la obligación (requiere revisión)",
	"pago_cambio": "El pago del conductor cambió después de crear el lote",
	"no_encontrada": "La obligación ya no corresponde a esta empresa o no existe",
}
REVIEW_CODES = ("anulada", "sin_valor", "sin_verificacion", "multiples_pagos", "no_coincide")
_STATUS_OF_CODE = {
	None: "por_confirmar",
	"sin_comprobante": "sin_comprobante",
	"ya_confirmado": "confirmado",
	"rechazado": "rechazado",
}


class CarteraMasivaError(frappe.ValidationError):
	pass


class ActiveBatchError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# Private service token
# ---------------------------------------------------------------------------

_SERVICE_TOKEN = object()


def authorize(doc):
	doc.flags.cartera_masiva_token = _SERVICE_TOKEN
	return doc


def is_authorized(doc):
	return doc.flags.get("cartera_masiva_token") is _SERVICE_TOKEN


def _insert(doc):
	"""Read-only DocTypes (DocPerm: read/report only): the token is the
	barrier; ignore_permissions only skips the create DocPerm."""
	authorize(doc).insert(ignore_permissions=True)
	return doc


def _save(doc):
	authorize(doc).save(ignore_permissions=True)
	return doc


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def require_access():
	"""System Manager only (v1), real read permission on Cartera Obligacion
	and the site's company -- never one sent by the client."""
	from fabergray_erp.api.bodega import _require_login
	from fabergray_erp.permission_conditions import _allowed_companies

	_require_login()
	frappe.only_for("System Manager")
	frappe.has_permission(cartera_service.OBLIGATION_DOCTYPE, "read", throw=True)
	company = get_default_company()
	allowed = _allowed_companies()
	if not company or (allowed is not None and company not in allowed):
		frappe.throw(_("No tienes acceso a la cartera de esta empresa."), frappe.PermissionError)
	return company


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _parse_date(value, label):
	try:
		return getdate(value) if value else None
	except Exception:
		frappe.throw(_("{0}: fecha inválida.").format(label), CarteraMasivaError)


def _parse_list(value, label, limit):
	if value in (None, "", []):
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("{0}: lista inválida.").format(label), CarteraMasivaError)
	if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
		frappe.throw(_("{0}: lista inválida.").format(label), CarteraMasivaError)
	if len(value) > limit:
		frappe.throw(_("{0}: demasiados elementos (máximo {1}).").format(label, limit), CarteraMasivaError)
	return sorted({v.strip() for v in value})


def validate_filters(
	date_type=None, date_from=None, date_to=None, customers=None, issuer=None, invoice_payment_method=None, status=None, search=None
):
	"""Normalized, JSON-serializable filters (also the preview cache key and
	what the batch stores). Inclusive range of the chosen date type."""
	date_type = date_type or DEFAULT_DATE_TYPE
	if date_type not in DATE_TYPES:
		frappe.throw(_("Tipo de fecha inválido."), CarteraMasivaError)
	date_from = _parse_date(date_from, _("Fecha inicial"))
	date_to = _parse_date(date_to, _("Fecha final"))
	if not date_from or not date_to:
		frappe.throw(_("Indica la fecha inicial y la fecha final."), CarteraMasivaError)
	if date_from > date_to:
		frappe.throw(_("La fecha inicial no puede ser posterior a la fecha final."), CarteraMasivaError)
	if (date_to - date_from).days > MAX_RANGE_DAYS:
		frappe.throw(_("El rango no puede superar {0} días.").format(MAX_RANGE_DAYS), CarteraMasivaError)
	status = status or "todos"
	if status not in STATUS_FILTERS:
		frappe.throw(_("Estado inválido."), CarteraMasivaError)
	issuer = (issuer or "").strip() or None
	method = (invoice_payment_method or "").strip() or None
	if method and method != NO_PAYMENT_METHOD:
		allowed = frappe.get_meta(cartera_service.OBLIGATION_DOCTYPE).get_field("invoice_payment_method").options.split("\n")
		if method not in [m for m in allowed if m]:
			frappe.throw(_("Forma de pago de la factura inválida."), CarteraMasivaError)
	search = (search or "").strip()
	if len(search) > MAX_SEARCH_LENGTH:
		frappe.throw(_("La búsqueda no puede superar {0} caracteres.").format(MAX_SEARCH_LENGTH), CarteraMasivaError)
	return {
		"date_type": date_type,
		"date_from": str(date_from),
		"date_to": str(date_to),
		"customers": _parse_list(customers, _("Clientes"), MAX_CUSTOMERS_FILTER),
		"issuer": issuer,
		"invoice_payment_method": method,
		"status": status,
		"search": search or None,
	}


def _like(search):
	escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Candidates and classification (shared by preview, confirm and the worker)
# ---------------------------------------------------------------------------

_SOURCE = """
	`tabCartera Obligacion` o
	LEFT JOIN `tabCustomer` c ON c.name = o.customer
	LEFT JOIN `tabPick List` pl ON pl.name = o.pick_list
"""
_FIELDS = """
	o.name, o.company, o.customer, o.customer_name, c.access_nombre_comercial AS customer_commercial_name,
	o.commercial_name, o.sales_order, o.pick_list, o.recorrido, o.invoice_issuer, o.invoice_payment_method,
	o.invoice_amount, o.paid_amount, o.outstanding_amount, o.currency, o.amount_source, o.status,
	o.payment_verification, o.driver_payment_status, o.delivery_date, o.due_date, pl.fg_invoiced_on,
	o.payment_verified_batch
"""
_PAYMENT_DATE_EXISTS = """EXISTS (
	SELECT 1 FROM `tabCartera Pago` dp
	WHERE dp.cartera_obligacion = o.name AND dp.docstatus = 1 AND dp.source = 'Conductor'
		AND dp.payment_date BETWEEN %(date_from)s AND %(date_to)s
)"""


def _date_condition(date_type):
	if date_type == DATE_INVOICE:
		# fg_invoiced_on is a site-time datetime: [from 00:00, to+1 00:00).
		return "pl.fg_invoiced_on >= %(date_from)s AND pl.fg_invoiced_on < %(date_to_next)s"
	if date_type == DATE_DELIVERY:
		return "o.delivery_date BETWEEN %(date_from)s AND %(date_to)s"
	if date_type == DATE_DUE:
		return "o.due_date BETWEEN %(date_from)s AND %(date_to)s"
	return _PAYMENT_DATE_EXISTS


def _missing_date_condition(date_type):
	if date_type == DATE_INVOICE:
		return "pl.fg_invoiced_on IS NULL"
	if date_type == DATE_DELIVERY:
		return "o.delivery_date IS NULL"
	if date_type == DATE_DUE:
		return "o.due_date IS NULL"
	return """NOT EXISTS (
		SELECT 1 FROM `tabCartera Pago` dp
		WHERE dp.cartera_obligacion = o.name AND dp.docstatus = 1 AND dp.source = 'Conductor'
	)"""


def _base_conditions(company, filters):
	conditions = ["o.company = %(company)s", "o.driver_payment_status = %(driver_paid)s"]
	params = {"company": company, "driver_paid": cartera_service.DRIVER_PAID}
	if filters.get("customers"):
		conditions.append("o.customer IN %(customers)s")
		params["customers"] = tuple(filters["customers"])
	if filters.get("issuer"):
		conditions.append("o.invoice_issuer = %(issuer)s")
		params["issuer"] = filters["issuer"]
	method = filters.get("invoice_payment_method")
	if method == NO_PAYMENT_METHOD:
		conditions.append("IFNULL(o.invoice_payment_method, '') = ''")
	elif method:
		conditions.append("o.invoice_payment_method = %(method)s")
		params["method"] = method
	if filters.get("search"):
		params["search"] = _like(filters["search"])
		columns = (
			"o.name",
			"o.customer",
			"o.customer_name",
			"c.access_nombre_comercial",
			"o.commercial_name",
			"o.sales_order",
			"o.pick_list",
			"o.recorrido",
		)
		conditions.append(
			"("
			+ " OR ".join(f"{column} LIKE %(search)s" for column in columns)
			+ """ OR EXISTS (SELECT 1 FROM `tabCartera Pago` sp WHERE sp.cartera_obligacion = o.name
				AND (sp.name LIKE %(search)s OR sp.reference LIKE %(search)s OR sp.notes LIKE %(search)s)))"""
		)
	return conditions, params


def load_candidates(company, filters=None, obligations=None, for_update=False):
	"""Obligations of `company` whose driver reported "Pagado", each with
	its submitted Conductor payments (`driver_payments`). Either by the
	filters (preview/confirm) or by name (worker). Narrow parameterized SQL,
	always scoped to the company explicitly (same convention as
	api/cartera.py); for_update=True makes both reads locking reads, in the
	global lock order (obligation rows, then their payments)."""
	source, fields = _SOURCE, _FIELDS
	if obligations is not None:
		if not obligations:
			return []
		conditions = ["o.company = %(company)s", "o.name IN %(names)s"]
		params = {"company": company, "names": tuple(sorted(obligations))}
		if for_update:
			# Locking read of the obligation rows ONLY (never the joined
			# Customer/Pick List rows): the same rows, in the same order, as
			# cartera_service.lock_obligation(). The joined columns are
			# informative and not needed to classify.
			source = "`tabCartera Obligacion` o"
			fields = _FIELDS.replace("c.access_nombre_comercial AS customer_commercial_name", "NULL AS customer_commercial_name").replace(
				"pl.fg_invoiced_on", "NULL AS fg_invoiced_on"
			)
	else:
		if for_update:
			frappe.throw(_("Lectura con bloqueo solo por nombre de obligación."), CarteraMasivaError)
		conditions, params = _base_conditions(company, filters)
		conditions.append(_date_condition(filters["date_type"]))
		params["date_from"] = filters["date_from"]
		params["date_to"] = filters["date_to"]
		params["date_to_next"] = str(add_days(filters["date_to"], 1))
	lock = "FOR UPDATE" if for_update else ""
	rows = frappe.db.sql(
		f"SELECT {fields} FROM {source} WHERE {' AND '.join(conditions)} ORDER BY o.name ASC {lock}",
		params,
		as_dict=True,
	)
	if not rows:
		return rows
	payments = {}
	names = [r.name for r in rows]
	for i in range(0, len(names), 500):
		for p in frappe.db.sql(
			f"""
			SELECT name, cartera_obligacion, amount, payment_date
			FROM `tabCartera Pago`
			WHERE cartera_obligacion IN %(names)s AND docstatus = 1 AND source = %(source)s
			ORDER BY name ASC {lock}
			""",
			{"names": tuple(names[i : i + 500]), "source": cartera_service.PAYMENT_SOURCE_DRIVER},
			as_dict=True,
		):
			payments.setdefault(p.cartera_obligacion, []).append(p)
	for r in rows:
		r.driver_payments = payments.get(r.name, [])
	return rows


def count_missing_date(company, filters):
	"""How many driver-reported obligations matching every other filter have
	NO date of the chosen type (never invented, so never in any range)."""
	conditions, params = _base_conditions(company, filters)
	conditions.append(_missing_date_condition(filters["date_type"]))
	return cint(frappe.db.sql(f"SELECT COUNT(*) FROM {_SOURCE} WHERE {' AND '.join(conditions)}", params)[0][0])


def classify(c):
	"""(reason_code or None, detail). Mirrors cartera_service.
	confirm_driver_payment() check by check, in the same order, so the
	preview never promises what the service would refuse."""
	if c.status == cartera_service.STATUS_ANULADA:
		return "anulada", None
	if c.payment_verification == cartera_service.VERIFICATION_CONFIRMED:
		return "ya_confirmado", c.payment_verified_batch or None
	if c.payment_verification == cartera_service.VERIFICATION_REJECTED:
		return "rechazado", None
	if c.payment_verification != cartera_service.VERIFICATION_UNCONFIRMED:
		return "sin_verificacion", None
	payments = c.get("driver_payments") or []
	if c.amount_source != cartera_service.AMOUNT_SOURCE_INVOICE:
		return ("sin_valor", None) if payments else ("sin_comprobante", None)
	if not payments:
		return "sin_comprobante", None
	if len(payments) != 1:
		return "multiples_pagos", ", ".join(p.name for p in payments)
	payment = payments[0]
	if (
		flt(payment.amount, 2) != flt(c.invoice_amount, 2)
		or flt(c.outstanding_amount, 2) != 0
		or c.status != cartera_service.STATUS_PAGADO
	):
		return "no_coincide", payment.name
	return None, None


def _reason_text(code, detail=None):
	text = _(EXCLUSION_REASONS[code])
	return f"{text} ({detail})" if detail else text


def _row(c, code, detail):
	payment = (c.get("driver_payments") or [None])[0] if len(c.get("driver_payments") or []) == 1 else None
	return {
		"obligation": c.name,
		"customer": c.customer,
		"customer_name": c.customer_name,
		"customer_commercial_name": c.customer_commercial_name or None,
		"commercial_name": c.commercial_name or c.sales_order,
		"sales_order": c.sales_order,
		"pick_list": c.pick_list,
		"recorrido": c.recorrido,
		"invoice_issuer": c.invoice_issuer or None,
		"invoice_payment_method": c.invoice_payment_method or None,
		"invoice_date": str(getdate(c.fg_invoiced_on)) if c.fg_invoiced_on else None,
		"delivery_date": str(getdate(c.delivery_date)) if c.delivery_date else None,
		"due_date": str(getdate(c.due_date)) if c.due_date else None,
		"payment_date": str(getdate(payment.payment_date)) if payment and payment.payment_date else None,
		"driver_payment": payment.name if payment else None,
		"currency": c.currency,
		"invoice_amount": flt(c.invoice_amount, 2),
		"outstanding_amount": flt(c.outstanding_amount, 2),
		# The money this authorization covers: the driver's payment (only
		# eligible rows have exactly one, equal to the invoiced value).
		"amount": flt(payment.amount, 2) if payment and code is None else 0,
		"status": c.status,
		"payment_verification": c.payment_verification or None,
		"eligible": code is None,
		"reason_code": code,
		"reason": _reason_text(code) if code else None,
		"reason_detail": detail,
		"needs_review": code in REVIEW_CODES,
		"status_filter": _STATUS_OF_CODE.get(code, "revision"),
	}


def build_preview(company, filters):
	"""Every classified row matching the filters (status filter applied on
	the classification). Read-only."""
	rows = []
	for c in load_candidates(company, filters):
		code, detail = classify(c)
		row = _row(c, code, detail)
		if filters["status"] != "todos" and row["status_filter"] != filters["status"]:
			continue
		rows.append(row)
	return rows


def _cents(value):
	return int(round(flt(value, 2) * 100))


def summarize(rows):
	"""Exact totals in integer cents (never float sums)."""
	eligible = [r for r in rows if r["eligible"]]
	by_reason, by_customer = {}, {}
	for r in rows:
		if r["eligible"]:
			entry = by_customer.setdefault(
				r["customer"],
				{
					"customer": r["customer"],
					"customer_name": r["customer_name"],
					"customer_commercial_name": r["customer_commercial_name"],
					"count": 0,
					"cents": 0,
				},
			)
			entry["count"] += 1
			entry["cents"] += _cents(r["amount"])
		else:
			reason = by_reason.setdefault(
				r["reason_code"],
				{"code": r["reason_code"], "label": _(EXCLUSION_REASONS[r["reason_code"]]), "count": 0, "needs_review": r["needs_review"]},
			)
			reason["count"] += 1
	customers = sorted(by_customer.values(), key=lambda e: (-e["cents"], e["customer_name"] or ""))
	for e in customers:
		e["amount"] = e.pop("cents") / 100
	return {
		"found": len(rows),
		"eligible": len(eligible),
		"excluded": len(rows) - len(eligible),
		"total_amount": sum(_cents(r["amount"]) for r in eligible) / 100,
		"customers": len(customers),
		"by_customer": customers,
		"excluded_by_reason": sorted(by_reason.values(), key=lambda e: -e["count"]),
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
	name = frappe.db.get_value(BATCH_DOCTYPE, {"active_company_lock": company}, "name")
	return frappe.get_doc(BATCH_DOCTYPE, name) if name else None


def create_batch(company, filters, selected, reason, client_request_id, mode=MODE_AUTHORIZATION):
	"""Inserts the batch (unique active lock per company, shared by every
	mode) and one Pendiente line per selected obligation, then enqueues the
	job. `selected`: candidates already revalidated by the caller in this
	same request (classification None). Returns (batch, created)."""
	if mode not in ENABLED_MODES:
		frappe.throw(_("La modalidad «{0}» todavía no está disponible.").format(mode), CarteraMasivaError)
	if not selected:
		frappe.throw(_("Ninguna de las obligaciones seleccionadas se puede autorizar."), CarteraMasivaError)
	if len(selected) > MAX_BATCH_LINES:
		frappe.throw(
			_("Un lote admite como máximo {0} cobros; divide el rango.").format(MAX_BATCH_LINES), CarteraMasivaError
		)
	currencies = {c.currency for c in selected}
	total_cents = sum(_cents(c.driver_payments[0].amount) for c in selected)
	batch = frappe.get_doc(
		{
			"doctype": BATCH_DOCTYPE,
			"company": company,
			"mode": mode,
			"status": STATUS_QUEUED,
			"requested_by": frappe.session.user,
			"reason": reason,
			"review_confirmed": 1,
			"date_type": DATE_TYPES[filters["date_type"]],
			"date_from": filters["date_from"],
			"date_to": filters["date_to"],
			"filters_json": json.dumps(filters, ensure_ascii=False, sort_keys=True),
			"total_selected": len(selected),
			"currency": next(iter(currencies)) if len(currencies) == 1 else None,
			"total_amount": total_cents / 100,
			"queued_on": now_datetime(),
			"heartbeat_on": now_datetime(),
			"client_request_id": client_request_id,
			"active_company_lock": company,
			"worker_token": uuid.uuid4().hex,
		}
	)
	frappe.db.savepoint("cm_batch_insert")
	try:
		_insert(batch)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point="cm_batch_insert")
		existing = frappe.db.get_value(BATCH_DOCTYPE, {"client_request_id": client_request_id}, "name")
		if existing:
			return frappe.get_doc(BATCH_DOCTYPE, existing), False
		running = active_batch(company)
		frappe.throw(
			_("Ya hay un lote de cartera activo ({0}). Espera a que termine o reanúdalo si se interrumpió.").format(
				running.name if running else "?"
			),
			ActiveBatchError,
		)

	for c in selected:
		payment = c.driver_payments[0]
		_insert(
			frappe.get_doc(
				{
					"doctype": LINE_DOCTYPE,
					"cartera_lote": batch.name,
					"cartera_obligacion": c.name,
					"driver_payment": payment.name,
					"result": RESULT_PENDING,
					"customer": c.customer,
					"customer_name": c.customer_name,
					"commercial_name": c.commercial_name or c.sales_order,
					"pick_list": c.pick_list,
					"invoice_issuer": c.invoice_issuer,
					"invoice_payment_method": c.invoice_payment_method,
					"currency": c.currency,
					"amount": flt(payment.amount, 2),
					"outstanding_before": flt(c.outstanding_amount, 2),
				}
			)
		)
	enqueue(batch)
	return batch, True


def enqueue(batch):
	batch.db_set("job_id", f"cartera_lote::{batch.name}::{batch.worker_token[:8]}", update_modified=False)
	in_test = bool(frappe.flags.in_test or getattr(frappe, "in_test", False))
	if in_test and frappe.flags.get("cartera_masiva_defer_job"):
		# Tests that drive run_batch() by hand (interruption/recovery).
		return
	frappe.enqueue(
		"fabergray_erp.cartera_masiva_service.run_batch",
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
	return frappe.db.get_value(
		BATCH_DOCTYPE, batch_name, ["name", "status", "worker_token", "requested_by"], as_dict=True, for_update=True
	)


def run_batch(batch_name, worker_token):
	"""Background job. Processes the pending lines chunk by chunk, one
	committed transaction per chunk. Stops at once if the batch was taken
	over (resume) or closed. A crash marks it Interrumpido (resumable); a
	killed worker leaves it En proceso with a stale heartbeat (resumable)."""
	try:
		_run_batch(batch_name, worker_token)
	except Exception:
		frappe.db.rollback()
		mark_interrupted(batch_name, frappe.get_traceback(), worker_token=worker_token)
		frappe.db.commit()
		frappe.log_error(title=_("Lote de cartera {0} interrumpido").format(batch_name))


def _run_batch(batch_name, worker_token):
	row = _lock_batch(batch_name)
	if not row or row.worker_token != worker_token or row.status not in (STATUS_QUEUED, STATUS_RUNNING):
		frappe.db.rollback()
		return
	if "System Manager" not in frappe.get_roles(frappe.session.user):
		# The job runs as the requesting user: a user who lost the role
		# while the batch was queued never gets it processed.
		frappe.throw(_("El usuario que solicitó el lote ya no es System Manager."), frappe.PermissionError)
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	batch.status = STATUS_RUNNING
	batch.started_on = batch.started_on or now_datetime()
	batch.heartbeat_on = now_datetime()
	_save(batch)
	frappe.db.commit()

	while True:
		row = _lock_batch(batch_name)
		if not row or row.worker_token != worker_token or row.status != STATUS_RUNNING:
			frappe.db.rollback()
			return
		pending = frappe.get_all(
			LINE_DOCTYPE,
			filters={"cartera_lote": batch_name, "result": RESULT_PENDING},
			pluck="name",
			order_by="name asc",
			limit=CHUNK_SIZE,
		)
		if not pending:
			break
		_run_chunk_safely(batch_name, pending)
		_refresh_counters(batch_name, heartbeat=True)
		frappe.db.commit()

	_finish(batch_name)
	frappe.db.commit()


def _run_chunk_safely(batch_name, line_names):
	"""A deadlock retries the whole chunk (InnoDB already rolled the
	transaction back); any other chunk-level error falls back to one line
	per transaction so a single bad obligation never blocks the rest."""
	for _attempt in range(3):
		try:
			process_chunk(batch_name, line_names)
			return
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			_lock_batch(batch_name)
			continue
		except Exception:
			frappe.db.rollback()
			_lock_batch(batch_name)
			break
	if len(line_names) == 1:
		frappe.db.rollback()
		_lock_batch(batch_name)
		_mark_lines(line_names, RESULT_FAILED, _("Error inesperado: ver el registro de errores."))
		frappe.log_error(title=_("Lote de cartera {0}: obligación fallida").format(batch_name))
		return
	for name in line_names:
		_run_chunk_safely(batch_name, [name])
		frappe.db.commit()
		_lock_batch(batch_name)


def _mark_lines(line_names, result, message):
	for name in line_names:
		line = frappe.get_doc(LINE_DOCTYPE, name, for_update=True)
		if line.result != RESULT_PENDING:
			continue
		_finish_line(line, result, message)


def _finish_line(line, result, message, **values):
	line.update({"result": result, "message": message, "processed_on": now_datetime(), **values})
	_save(line)


def _discard_messages_since(count):
	log = frappe.local.message_log or []
	del log[count:]


def _short_error(e):
	text = frappe.utils.strip_html(str(e) or e.__class__.__name__).strip()
	return (text or e.__class__.__name__)[:400]


def process_chunk(batch_name, line_names):
	"""One transaction. Each pending line, in obligation-name order (the
	global lock order), inside its own savepoint:
	1. lock the obligation row and its driver payments (locking reads) and
	   revalidate it exactly like the preview;
	2. the driver payment must still be the one the batch selected;
	3. cartera_service.confirm_driver_payment(name, batch) -- the individual
	   CONFIRMAR PAGO, unchanged;
	4. the balance, paid amount and status must be identical afterwards and
	   the payment still submitted and untouched -- otherwise roll back."""
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	lines = []
	for name in line_names:
		line = frappe.get_doc(LINE_DOCTYPE, name, for_update=True)
		if line.result == RESULT_PENDING:
			lines.append(line)
	for line in sorted(lines, key=lambda l: l.cartera_obligacion):
		_authorize_line(batch, line)


def _payment_snapshot(name):
	return frappe.db.get_value(
		cartera_service.PAYMENT_DOCTYPE, name, ["docstatus", "amount", "payment_date", "source", "modified"], as_dict=True
	)


def _authorize_line(batch, line):
	messages = len(frappe.local.message_log or [])
	savepoint = f"cm_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		candidates = load_candidates(batch.company, obligations=[line.cartera_obligacion], for_update=True)
		if not candidates:
			frappe.db.rollback(save_point=savepoint)
			_finish_line(line, RESULT_SKIPPED, _reason_text("no_encontrada"))
			return
		c = candidates[0]
		code, detail = classify(c)
		if code:
			frappe.db.rollback(save_point=savepoint)
			_finish_line(line, RESULT_SKIPPED, _reason_text(code, detail), outstanding_after=flt(c.outstanding_amount, 2))
			return
		if c.driver_payments[0].name != line.driver_payment:
			frappe.db.rollback(save_point=savepoint)
			_finish_line(line, RESULT_SKIPPED, _reason_text("pago_cambio", c.driver_payments[0].name))
			return

		before = (flt(c.invoice_amount, 2), flt(c.paid_amount, 2), flt(c.outstanding_amount, 2), c.status, c.due_date)
		payment_before = _payment_snapshot(line.driver_payment)
		result = cartera_service.confirm_driver_payment(c.name, batch=batch.name)
		after_doc = frappe.db.get_value(
			cartera_service.OBLIGATION_DOCTYPE,
			c.name,
			["invoice_amount", "paid_amount", "outstanding_amount", "status", "due_date", "payment_verification", "payment_verified_batch"],
			as_dict=True,
		)
		after = (
			flt(after_doc.invoice_amount, 2),
			flt(after_doc.paid_amount, 2),
			flt(after_doc.outstanding_amount, 2),
			after_doc.status,
			after_doc.due_date,
		)
		if (
			result.get("already_done")
			or after != before
			or _payment_snapshot(line.driver_payment) != payment_before
			or after_doc.payment_verification != cartera_service.VERIFICATION_CONFIRMED
			or after_doc.payment_verified_batch != batch.name
		):
			frappe.throw(_("La autorización no dejó la obligación en el estado esperado."), cartera_service.CarteraStateError)
	except frappe.QueryDeadlockError:
		raise
	except cartera_service.CarteraStateError as e:
		frappe.db.rollback(save_point=savepoint)
		_discard_messages_since(messages)
		_finish_line(line, RESULT_SKIPPED, _short_error(e))
		return
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		_discard_messages_since(messages)
		_finish_line(line, RESULT_FAILED, _short_error(e))
		frappe.log_error(
			title=_("Lote de cartera {0}: no se pudo autorizar {1}").format(batch.name, line.cartera_obligacion),
			reference_doctype=cartera_service.OBLIGATION_DOCTYPE,
			reference_name=line.cartera_obligacion,
		)
		return
	frappe.db.release_savepoint(savepoint)
	_finish_line(line, RESULT_AUTHORIZED, _("Pago del conductor confirmado."), outstanding_after=after[2])


def _refresh_counters(batch_name, heartbeat=False):
	counts = {
		r.result: (cint(r.n), _cents(r.amount))
		for r in frappe.db.sql(
			"""
			SELECT result, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount
			FROM `tabCartera Lote Linea` WHERE cartera_lote = %s GROUP BY result
			""",
			(batch_name,),
			as_dict=True,
		)
	}
	get = lambda result: counts.get(result, (0, 0))  # noqa: E731
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	batch.authorized_count = get(RESULT_AUTHORIZED)[0]
	batch.authorized_amount = get(RESULT_AUTHORIZED)[1] / 100
	batch.skipped_count = get(RESULT_SKIPPED)[0]
	batch.failed_count = get(RESULT_FAILED)[0]
	batch.processed_count = batch.authorized_count + batch.skipped_count + batch.failed_count
	if heartbeat:
		batch.heartbeat_on = now_datetime()
	_save(batch)
	return batch, get(RESULT_PENDING)[0]


def _finish(batch_name):
	batch, pending = _refresh_counters(batch_name)
	if pending:
		return batch
	batch.status = STATUS_DONE_WITH_ERRORS if batch.failed_count else STATUS_DONE
	batch.finished_on = now_datetime()
	batch.active_company_lock = None
	_save(batch)
	return batch


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def resume(batch_name):
	"""A batch whose worker died (stale heartbeat) or that was marked
	Interrumpido is re-enqueued with a NEW worker token: the old worker, if
	it ever wakes up, stops at its next chunk. Pending lines only --
	everything already committed is never redone."""
	if not _lock_batch(batch_name):
		frappe.throw(_("El lote no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	if not (batch.status == STATUS_INTERRUPTED or is_stale(batch)):
		frappe.throw(_("Solo se puede reanudar un lote interrumpido o sin avance reciente."), CarteraMasivaError)
	batch.status = STATUS_QUEUED
	batch.worker_token = uuid.uuid4().hex
	batch.heartbeat_on = now_datetime()
	batch.last_error = _("Reanudado por {0} el {1}.").format(frappe.session.user, now_datetime())
	_save(batch)
	enqueue(batch)
	return batch


def close(batch_name):
	"""Releases the company lock of an interrupted/stale batch WITHOUT
	processing its pending lines (Omitido "lote cerrado"). Authorized lines
	stay authorized."""
	if not _lock_batch(batch_name):
		frappe.throw(_("El lote no existe."), frappe.DoesNotExistError)
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	if not (batch.status == STATUS_INTERRUPTED or is_stale(batch)):
		frappe.throw(_("Solo se puede cerrar un lote interrumpido o sin avance reciente."), CarteraMasivaError)
	batch.worker_token = uuid.uuid4().hex  # any old worker stops
	_save(batch)
	pending = frappe.get_all(LINE_DOCTYPE, filters={"cartera_lote": batch_name, "result": RESULT_PENDING}, pluck="name")
	_mark_lines(pending, RESULT_SKIPPED, _("Lote cerrado sin procesar esta obligación."))
	batch, _pending = _refresh_counters(batch_name)
	batch.status = STATUS_CLOSED
	batch.finished_on = now_datetime()
	batch.active_company_lock = None
	_save(batch)
	return batch


def mark_interrupted(batch_name, error, worker_token=None):
	row = _lock_batch(batch_name)
	if not row or (worker_token and row.worker_token != worker_token):
		return
	batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
	if batch.status in (STATUS_QUEUED, STATUS_RUNNING):
		batch.status = STATUS_INTERRUPTED
		batch.last_error = (error or "")[:1000]
		_save(batch)
