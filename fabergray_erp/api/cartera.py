# -*- coding: utf-8 -*-
"""api/cartera.py -- Page Cartera (27.1 foundation, 27.2 read API, 27.3
cobros + validación del pago del conductor).

Writes (27.3): register_payment (REGISTRAR COBRO), confirm_driver_payment,
reject_driver_payment -- plus the 27.1 reconciler trigger
(sync_missing_obligations). Each one checks access here and then delegates
to fabergray_erp/cartera_service.py, the ONLY writer of Cartera Pago and of
the verification of Cartera Obligacion (private service token; Desk and
frappe.client are refused by the controllers). None creates Payment
Entry/Sales Invoice/Journal Entry/GL Entry: Cartera is operational, every
payment stays "Sin contabilizar". Every write runs inside a bounded retry
on deadlock (api.recorridos._retrying_on_deadlock, 3 attempts) and answers
with the refreshed detail and KPIs, so the Page never reloads.

Access (every endpoint): logged in + role Cartera or System Manager +
read permission on Cartera Obligacion + the site's company, resolved
server-side and checked against the caller's allowed companies
(permission_conditions._allowed_companies(), the same boundary the
Cartera list/has_permission hooks apply). The Page's own roles are only a
convenience; this is the real boundary.

Aggregations/lists use narrow parameterized SQL on purpose: KPIs are
SUM()s over the whole company (never over the rows the UI happens to have
loaded), and the list needs a computed priority ORDER BY plus one LEFT
JOIN to Customer (access_nombre_comercial) -- neither is expressible with
frappe.get_list(). Every such query is scoped to the resolved company
explicitly.

"today" is ALWAYS frappe.utils.nowdate() (the site's System Settings
time zone), passed to SQL as a parameter -- never a database-clock date
function, which uses the database server's clock and may be a different
calendar day.

Buckets (one rule, shared by the KPIs, the chips, the priority order and
the card/detail badges -- see _BUCKET_SQL and _bucket()):

  vencido      outstanding > 0 AND due_date IS NOT NULL AND due_date < today
  por_vencer   outstanding > 0 AND due_date IS NOT NULL AND due_date >= today
  pendiente    outstanding > 0 AND status = Pendiente (no due_date)
  por_validar  status = Por Validar (any balance not covered above)
  pagado       status = Pagado
  anulada      anything else (status Anulada)

POR CONFIRMAR is one population (POR_CONFIRMAR_SQL), shared by the KPI, the
chip and each row's flag.

Known debt, deliberately left for later phases:
- anulación de cobros manuales (Cartera source): no endpoint, no cancel
  path -- out of 27.3;
- resolving the amount of a "Sin valor calculable" obligation: out of 27.3;
- role Cartera is not in user_hooks.OPERATIONAL_ROLES (no "Fabrigray
  Operativo" module profile), same as Recorrido -- to be evaluated;
- extremely long amounts (14+ chars) shrink their font instead of wrapping
  (page/cartera/cartera.css, .is-long);
- the shared fg_shell.css header wraps to two lines at 360px (all Pages).
"""

import base64
import calendar

import frappe
from frappe import _
from frappe.utils import cint, flt, get_fullname, getdate, nowdate

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.api import recorridos as _recorridos
from fabergray_erp.api.bodega import _require_login
from fabergray_erp.permission_conditions import _allowed_companies

CARTERA_ROLES = ("Cartera", "System Manager")

DEFAULT_PAGE_LENGTH = 20
MAX_PAGE_LENGTH = 100
MAX_SEARCH_LENGTH = 140

#: The driver's payment proof is a re-encoded JPEG (api.recorridos.
#: _normalize_payment_proof()); anything bigger than this is not a file
#: that flow produced.
MAX_PROOF_BYTES = 8 * 1024 * 1024

MONTHS_ES = (
	"ENERO",
	"FEBRERO",
	"MARZO",
	"ABRIL",
	"MAYO",
	"JUNIO",
	"JULIO",
	"AGOSTO",
	"SEPTIEMBRE",
	"OCTUBRE",
	"NOVIEMBRE",
	"DICIEMBRE",
)

#: POR CONFIRMAR -- ONE population, shared by the KPI, the chip and each
#: row's `por_confirmar` flag: a REAL payment registered by the driver
#: (submitted Cartera Pago, source Conductor) that Cartera has not confirmed
#: yet. With the 27.1 model that is status Pagado + payment_verification
#: "Sin confirmar" + that payment. A "Pagado" report WITHOUT proof (Por
#: Validar, full balance, no payment) is NOT here -- it is POR VALIDAR only.
#: Once a future confirmation sets payment_verification to anything else,
#: the obligation leaves this population automatically.
_DRIVER_PAYMENT_EXISTS_SQL = """EXISTS (
	SELECT 1 FROM `tabCartera Pago` dp
	WHERE dp.cartera_obligacion = o.name AND dp.docstatus = 1 AND dp.source = 'Conductor'
)"""
POR_CONFIRMAR_SQL = (
	"o.status = 'Pagado' AND o.payment_verification = 'Sin confirmar' AND " + _DRIVER_PAYMENT_EXISTS_SQL
)

#: Chip filters -- constant SQL fragments (no caller input is ever
#: interpolated; `today` is a bound parameter).
FILTERS = {
	"todos": "",
	"pendientes": "o.outstanding_amount > 0 AND o.status = 'Pendiente'",
	"credito": "o.credit_days > 0 AND o.outstanding_amount > 0",
	"vencidos": "o.outstanding_amount > 0 AND o.due_date IS NOT NULL AND o.due_date < %(today)s",
	"pagados": "o.status = 'Pagado' AND o.outstanding_amount = 0",
	"por_validar": "o.status = 'Por Validar'",
	"por_confirmar": POR_CONFIRMAR_SQL,
}

#: Priority rank (1 = most urgent). Must stay equivalent to _bucket().
_BUCKET_SQL = """
	CASE
		WHEN o.outstanding_amount > 0 AND o.due_date IS NOT NULL AND o.due_date < %(today)s THEN 1
		WHEN o.outstanding_amount > 0 AND o.due_date IS NOT NULL THEN 2
		WHEN o.outstanding_amount > 0 AND o.status = 'Pendiente' THEN 3
		WHEN o.status = 'Por Validar' THEN 4
		WHEN o.status = 'Pagado' THEN 5
		ELSE 6
	END
"""

BUCKETS = {1: "vencido", 2: "por_vencer", 3: "pendiente", 4: "por_validar", 5: "pagado", 6: "anulada"}

#: Deterministic order inside each bucket:
#: vencido/por_vencer -> due_date ASC (most overdue / soonest due first);
#: pendiente/por_validar -> delivery_date ASC (oldest debt first);
#: pagado/anulada -> delivery_date DESC (most recent first);
#: ties -> o.name ASC (unique).
_ORDER_BY = """
	ORDER BY
		bucket_rank ASC,
		CASE WHEN bucket_rank IN (1, 2) THEN o.due_date END ASC,
		CASE WHEN bucket_rank IN (3, 4) THEN o.delivery_date END ASC,
		CASE WHEN bucket_rank IN (5, 6) THEN o.delivery_date END DESC,
		o.name ASC
"""

_LIST_FIELDS = """
	o.name, o.customer, o.customer_name, c.access_nombre_comercial AS customer_commercial_name,
	o.commercial_name, o.sales_order, o.pick_list, o.recorrido,
	o.invoice_amount, o.paid_amount, o.outstanding_amount, o.currency, o.amount_source,
	o.status, o.payment_verification, o.credit_days, o.due_date,
	o.delivery_date, o.delivered_on, o.driver_payment_status, o.has_delivery_issues
"""


# ---------------------------------------------------------------------------
# Access / context helpers
# ---------------------------------------------------------------------------


def _today():
	"""The site's calendar day (System Settings time zone)."""
	return getdate(nowdate())


def _require_cartera_access():
	_require_login()
	frappe.only_for(CARTERA_ROLES)
	frappe.has_permission(cartera_service.OBLIGATION_DOCTYPE, "read", throw=True)


def _company():
	"""The site's company, never a value sent by the client, and only if
	the caller may operate in it."""
	company = get_default_company()
	allowed = _allowed_companies()
	if not company or (allowed is not None and company not in allowed):
		frappe.throw(_("No tienes acceso a la cartera de esta empresa."), frappe.PermissionError)
	return company


def _load_obligation(obligation_name, company):
	"""One obligation the caller may read, of the resolved company."""
	if not obligation_name or not isinstance(obligation_name, str):
		frappe.throw(_("Obligación de cartera inválida."), frappe.ValidationError)
	if not frappe.db.exists(cartera_service.OBLIGATION_DOCTYPE, obligation_name):
		frappe.throw(_("La obligación de cartera no existe."), frappe.DoesNotExistError)
	doc = frappe.get_doc(cartera_service.OBLIGATION_DOCTYPE, obligation_name)
	doc.check_permission("read")
	if doc.company != company:
		frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
	return doc


def _bucket(doc, today):
	"""Python twin of _BUCKET_SQL (same rule, used for a single record)."""
	outstanding = flt(doc.get("outstanding_amount"))
	due_date = getdate(doc.get("due_date")) if doc.get("due_date") else None
	if outstanding > 0 and due_date and due_date < today:
		return "vencido"
	if outstanding > 0 and due_date:
		return "por_vencer"
	if outstanding > 0 and doc.get("status") == cartera_service.STATUS_PENDIENTE:
		return "pendiente"
	if doc.get("status") == cartera_service.STATUS_POR_VALIDAR:
		return "por_validar"
	if doc.get("status") == cartera_service.STATUS_PAGADO:
		return "pagado"
	return "anulada"


def _date_str(value):
	return str(getdate(value)) if value else None


def _datetime_str(value):
	return str(value) if value else None


def _serialize_obligation(row, today, bucket=None):
	"""Fields shared by the card and the detail. Money stays unformatted
	(the client formats); dates are ISO strings; day counts are computed
	here against the site's `today` so every label agrees with the KPIs.

	The private proof URL is NEVER serialized -- only whether one exists;
	the image itself is served by get_driver_payment_proof()."""
	due_date = getdate(row.get("due_date")) if row.get("due_date") else None
	delivery_date = getdate(row.get("delivery_date")) if row.get("delivery_date") else None
	return {
		"name": row.get("name"),
		"customer": row.get("customer"),
		"customer_name": row.get("customer_name"),
		"customer_commercial_name": row.get("customer_commercial_name") or None,
		"commercial_name": row.get("commercial_name") or row.get("sales_order"),
		"sales_order": row.get("sales_order"),
		"pick_list": row.get("pick_list"),
		"recorrido": row.get("recorrido"),
		"invoice_amount": flt(row.get("invoice_amount"), 2),
		"paid_amount": flt(row.get("paid_amount"), 2),
		"outstanding_amount": flt(row.get("outstanding_amount"), 2),
		"currency": row.get("currency"),
		"amount_source": row.get("amount_source"),
		"amount_available": row.get("amount_source") == cartera_service.AMOUNT_SOURCE_INVOICE,
		"status": row.get("status"),
		"payment_verification": row.get("payment_verification") or None,
		"credit_days": cint(row.get("credit_days")),
		"due_date": _date_str(due_date),
		"delivery_date": _date_str(delivery_date),
		"delivered_on": _datetime_str(row.get("delivered_on")),
		"driver_payment_status": row.get("driver_payment_status") or None,
		"has_delivery_issues": cint(row.get("has_delivery_issues")),
		"bucket": bucket or _bucket(row, today),
		"por_confirmar": bool(cint(row.get("por_confirmar"))),
		"days_to_due": (due_date - today).days if due_date else None,
		"days_since_delivery": (today - delivery_date).days if delivery_date else None,
	}


def _like_pattern(search):
	escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_dashboard():
	"""The five KPIs, aggregated server-side over the WHOLE company.

	- cartera_actual: SUM(outstanding) of non-Anulada obligations with
	  outstanding > 0 (Pendiente, Crédito and Por Validar alike; a
	  driver-reported payment that left the balance at 0 is excluded).
	- vencida: the part of cartera_actual with due_date < today.
	- por_vencer: the rest (due_date IS NULL OR due_date >= today), i.e.
	  cartera_actual - vencida, by construction.
	- cobrado_mes: SUM(amount) of SUBMITTED Cartera Pago (any source) whose
	  payment_date falls in the current month; cancelled ones excluded.
	- por_confirmar: the POR_CONFIRMAR_SQL population (the same one the
	  chip lists). The amount is the SUM of their submitted "Conductor"
	  payments -- the money the driver reported (27.1 allows at most one
	  per obligation, for the full value), so nothing is counted twice;
	  confirmed and manual Cartera payments never enter it."""
	_require_cartera_access()
	return _dashboard_payload(_company())


def _dashboard_payload(company):
	today = _today()
	month_start = today.replace(day=1)
	month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
	params = {"company": company, "today": today, "month_start": month_start, "month_end": month_end}

	balance = frappe.db.sql(
		"""
		SELECT
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 THEN o.outstanding_amount END), 0) AS actual_amount,
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 THEN 1 ELSE 0 END), 0) AS actual_count,
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 AND o.due_date IS NOT NULL
				AND o.due_date < %(today)s THEN o.outstanding_amount END), 0) AS overdue_amount,
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 AND o.due_date IS NOT NULL
				AND o.due_date < %(today)s THEN 1 ELSE 0 END), 0) AS overdue_count,
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 AND (o.due_date IS NULL
				OR o.due_date >= %(today)s) THEN o.outstanding_amount END), 0) AS upcoming_amount,
			COALESCE(SUM(CASE WHEN o.outstanding_amount > 0 AND (o.due_date IS NULL
				OR o.due_date >= %(today)s) THEN 1 ELSE 0 END), 0) AS upcoming_count
		FROM `tabCartera Obligacion` o
		WHERE o.company = %(company)s AND o.status != 'Anulada'
		""",
		params,
		as_dict=True,
	)[0]

	collected = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(p.amount), 0) AS amount, COUNT(*) AS count
		FROM `tabCartera Pago` p
		WHERE p.company = %(company)s AND p.docstatus = 1
			AND p.payment_date BETWEEN %(month_start)s AND %(month_end)s
		""",
		params,
		as_dict=True,
	)[0]

	unconfirmed = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(p.amount), 0) AS amount, COUNT(DISTINCT o.name) AS count
		FROM `tabCartera Obligacion` o
		INNER JOIN `tabCartera Pago` p
			ON p.cartera_obligacion = o.name AND p.docstatus = 1 AND p.source = 'Conductor'
		WHERE o.company = %(company)s AND {POR_CONFIRMAR_SQL}
		""".format(POR_CONFIRMAR_SQL=POR_CONFIRMAR_SQL),
		params,
		as_dict=True,
	)[0]

	def kpi(amount, count):
		return {"amount": flt(amount, 2), "count": cint(count)}

	return {
		"company": company,
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		"today": str(today),
		"month": {"label": MONTHS_ES[today.month - 1], "start": str(month_start), "end": str(month_end)},
		"kpis": {
			"cartera_actual": kpi(balance.actual_amount, balance.actual_count),
			"por_vencer": kpi(balance.upcoming_amount, balance.upcoming_count),
			"vencida": kpi(balance.overdue_amount, balance.overdue_count),
			"cobrado_mes": kpi(collected.amount, collected.count),
			"por_confirmar": kpi(unconfirmed.amount, unconfirmed.count),
		},
	}


@frappe.whitelist()
def get_obligations(filter="todos", search="", page=1, page_length=DEFAULT_PAGE_LENGTH):
	"""One page of obligation cards: chip filter + search + priority order
	applied in SQL. Two queries per call (COUNT + page), never one per
	card."""
	_require_cartera_access()
	company = _company()
	today = _today()

	filter = filter or "todos"
	if filter not in FILTERS:
		frappe.throw(_("Filtro de cartera inválido."), frappe.ValidationError)
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length) or DEFAULT_PAGE_LENGTH, 1), MAX_PAGE_LENGTH)
	search = (search or "").strip()[:MAX_SEARCH_LENGTH]

	conditions = ["o.company = %(company)s"]
	params = {"company": company, "today": today, "limit": page_length, "offset": (page - 1) * page_length}
	if FILTERS[filter]:
		conditions.append(FILTERS[filter])
	if search:
		params["search"] = _like_pattern(search)
		conditions.append(
			"("
			+ " OR ".join(
				f"{column} LIKE %(search)s"
				for column in (
					"o.customer_name",
					"o.commercial_name",
					"o.customer",
					"o.sales_order",
					"o.pick_list",
					"o.recorrido",
					"c.access_nombre_comercial",
				)
			)
			+ ")"
		)
	where = " AND ".join(conditions)
	source = "`tabCartera Obligacion` o LEFT JOIN `tabCustomer` c ON c.name = o.customer"

	total = cint(frappe.db.sql(f"SELECT COUNT(*) FROM {source} WHERE {where}", params)[0][0])
	rows = frappe.db.sql(
		f"""
		SELECT {_LIST_FIELDS}, {_BUCKET_SQL} AS bucket_rank, ({POR_CONFIRMAR_SQL}) AS por_confirmar
		FROM {source}
		WHERE {where}
		{_ORDER_BY}
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		params,
		as_dict=True,
	)
	items = [_serialize_obligation(row, today, BUCKETS.get(cint(row.bucket_rank))) for row in rows]
	return {
		"items": items,
		"total": total,
		"page": page,
		"page_length": page_length,
		"has_more": page * page_length < total,
		"filter": filter,
		"search": search,
		"today": str(today),
	}


@frappe.whitelist()
def get_obligation_detail(obligation_name):
	"""A single obligation, its delivery report and its payment history.
	Read-only: nothing here can change a value."""
	_require_cartera_access()
	company = _company()
	return _detail_payload(_load_obligation(obligation_name, company))


def _detail_payload(doc):
	today = _today()
	row = doc.as_dict()
	row["por_confirmar"] = _is_por_confirmar(doc.name)
	data = _serialize_obligation(row, today)
	has_active_driver_payment = bool(
		frappe.db.exists(
			cartera_service.PAYMENT_DOCTYPE,
			{"cartera_obligacion": doc.name, "docstatus": 1, "source": cartera_service.PAYMENT_SOURCE_DRIVER},
		)
	)
	unresolved_report = (
		doc.payment_verification == cartera_service.VERIFICATION_UNCONFIRMED
		and doc.driver_payment_status == cartera_service.DRIVER_PAID
	)
	data.update(
		{
			"today": str(today),
			"payment_verified_by": doc.payment_verified_by or None,
			"payment_verified_by_name": get_fullname(doc.payment_verified_by) if doc.payment_verified_by else None,
			"payment_verified_on": _datetime_str(doc.payment_verified_on),
			"payment_rejection_reason": doc.payment_rejection_reason or None,
			# Action availability, decided HERE (the UI only shows/hides;
			# every endpoint re-checks under lock).
			"can_register_payment": cartera_service.collectable_error(doc) is None,
			"can_confirm_driver_payment": bool(data["por_confirmar"]),
			"can_reject_driver_report": unresolved_report,
			"driver_report_has_payment": has_active_driver_payment,

			"customer_commercial_name": frappe.db.get_value("Customer", doc.customer, "access_nombre_comercial")
			or None,
			"paid_on": _date_str(doc.paid_on),
			"delivered_by": doc.delivered_by,
			"delivered_by_name": get_fullname(doc.delivered_by) if doc.delivered_by else None,
			"driver_payment_note": doc.driver_payment_note or None,
			"has_driver_proof": bool(doc.driver_payment_proof),
			"delivery_issues": doc.delivery_issues or None,
			"invoice_issuer": doc.invoice_issuer or None,
			"payments": _payment_history(doc),
		}
	)
	return data


def _is_por_confirmar(obligation_name):
	"""The same POR_CONFIRMAR_SQL predicate, evaluated for one record (never
	a second, hand-written copy of the rule)."""
	return bool(
		cint(
			frappe.db.sql(
				f"SELECT ({POR_CONFIRMAR_SQL}) FROM `tabCartera Obligacion` o WHERE o.name = %s",
				(obligation_name,),
			)[0][0]
		)
	)


def _payment_history(obligation):
	"""Submitted and cancelled payments (drafts are not payments yet),
	read through get_list (permission + company conditions apply). A
	payment's own proof URL is never returned -- only whether it has one;
	a Conductor payment's proof IS the driver's (served by
	get_driver_payment_proof())."""
	rows = frappe.get_list(
		cartera_service.PAYMENT_DOCTYPE,
		filters={"cartera_obligacion": obligation.name, "docstatus": ["in", [1, 2]]},
		fields=[
			"name",
			"payment_date",
			"amount",
			"currency",
			"payment_method",
			"source",
			"reference",
			"notes",
			"accounting_status",
			"recorded_by",
			"recorded_on",
			"docstatus",
			"payment_proof",
			"cancellation_reason",
			"modified",
			"modified_by",
		],
		order_by="payment_date asc, creation asc",
	)
	history = []
	for row in rows:
		is_driver = row.source == cartera_service.PAYMENT_SOURCE_DRIVER
		history.append(
			{
				"name": row.name,
				"payment_date": _date_str(row.payment_date),
				"amount": flt(row.amount, 2),
				"currency": row.currency,
				"payment_method": row.payment_method or None,
				"source": row.source,
				"reference": row.reference or None,
				"notes": row.notes or None,
				"accounting_status": row.accounting_status or "Sin contabilizar",
				"recorded_by": row.recorded_by,
				"recorded_by_name": get_fullname(row.recorded_by) if row.recorded_by else None,
				"recorded_on": _datetime_str(row.recorded_on),
				"cancelled": row.docstatus == 2,
				# A cancelled payment can never be modified again, so its
				# modified/modified_by ARE who/when it was annulled (27.3).
				"cancellation_reason": row.cancellation_reason if row.docstatus == 2 else None,
				"cancelled_by_name": get_fullname(row.modified_by) if row.docstatus == 2 and row.modified_by else None,
				"cancelled_on": _datetime_str(row.modified) if row.docstatus == 2 else None,
				"has_payment_proof": bool(row.payment_proof) or (is_driver and bool(obligation.driver_payment_proof)),
				"proof_is_driver_proof": is_driver and bool(obligation.driver_payment_proof),
				# "payment" -> get_payment_proof(name); "driver" ->
				# get_driver_payment_proof(obligation); None -> no proof.
				"proof_kind": "driver"
				if is_driver and obligation.driver_payment_proof
				else ("payment" if row.payment_proof and not is_driver else None),
			}
		)
	return history


@frappe.whitelist()
def get_driver_payment_proof(obligation_name):
	"""The driver's payment proof of ONE obligation, as base64 image data.

	The caller only names the obligation; the File is resolved here and
	must be exactly the private File attached to that obligation's own
	delivered stop in field payment_proof, with the same URL the obligation
	references. No URL/file name is ever accepted from the client, and no
	read permission on Recorrido Parada/File is granted to Cartera -- the
	content is read in system context only after all of the above
	matched."""
	_require_cartera_access()
	company = _company()
	doc = _load_obligation(obligation_name, company)

	if not doc.driver_payment_proof:
		frappe.throw(_("Esta obligación no tiene comprobante del conductor."), frappe.DoesNotExistError)

	stop_proof = frappe.db.get_value("Recorrido Parada", doc.recorrido_parada, "payment_proof")
	if not stop_proof or stop_proof != doc.driver_payment_proof:
		frappe.throw(_("El comprobante del conductor no coincide con la entrega."), frappe.PermissionError)

	content_type, data = _read_private_image("Recorrido Parada", doc.recorrido_parada, doc.driver_payment_proof)
	return {"obligation": doc.name, "content_type": content_type, "data": data}


@frappe.whitelist()
def get_payment_proof(payment_name):
	"""The proof of ONE Cartera payment (source Cartera), as base64 image
	data. Same rules as get_driver_payment_proof(): the caller only names
	the payment; its obligation must be readable and of the resolved
	company, and the File must be exactly the private File attached to
	Cartera Pago/<payment>/payment_proof with the URL the payment stores.
	A driver payment's proof is served by get_driver_payment_proof()."""
	_require_cartera_access()
	company = _company()
	if not payment_name or not isinstance(payment_name, str):
		frappe.throw(_("Pago de cartera inválido."), frappe.ValidationError)
	if not frappe.db.exists(cartera_service.PAYMENT_DOCTYPE, payment_name):
		frappe.throw(_("El pago de cartera no existe."), frappe.DoesNotExistError)
	payment = frappe.get_doc(cartera_service.PAYMENT_DOCTYPE, payment_name)
	payment.check_permission("read")
	_load_obligation(payment.cartera_obligacion, company)
	if payment.company != company:
		frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)
	if payment.source != cartera_service.PAYMENT_SOURCE_CARTERA or not payment.payment_proof:
		frappe.throw(_("Este pago no tiene comprobante."), frappe.DoesNotExistError)

	content_type, data = _read_private_image(cartera_service.PAYMENT_DOCTYPE, payment.name, payment.payment_proof)
	return {"payment": payment.name, "content_type": content_type, "data": data}


def _read_private_image(attached_to_doctype, attached_to_name, file_url):
	"""(content_type, base64) of the ONE private File attached to exactly
	that document in field payment_proof with exactly that URL -- read in
	system context only after the caller's own checks. Never a URL/path
	from the client."""
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"attached_to_field": "payment_proof",
			"file_url": file_url,
		},
		fields=["name", "is_private"],
		limit_page_length=2,
	)
	if len(files) != 1 or not cint(files[0].is_private):
		frappe.throw(_("El comprobante no está disponible."), frappe.PermissionError)

	# encodings=(): raw bytes. The default get_content() tries to DECODE the
	# file as utf-8/windows-1250/windows-1252 and a JPEG that happens to
	# decode came back as str -- re-encoding it corrupted the image (latent
	# 27.2 bug, fixed in 27.3).
	content = frappe.get_doc("File", files[0].name).get_content(encodings=())
	if not isinstance(content, bytes):
		frappe.throw(_("El comprobante no es una imagen válida."), frappe.ValidationError)
	content_type = _image_content_type(content)
	if not content_type or len(content) > MAX_PROOF_BYTES:
		frappe.throw(_("El comprobante no es una imagen válida."), frappe.ValidationError)
	return content_type, base64.b64encode(content).decode("ascii")


def _image_content_type(content):
	if content[:3] == b"\xff\xd8\xff":
		return "image/jpeg"
	if content[:8] == b"\x89PNG\r\n\x1a\n":
		return "image/png"
	return None


@frappe.whitelist(methods=["POST"])
def sync_missing_obligations():
	"""SINCRONIZAR: creates the obligation of every Entregado stop of this
	site's company that still has none (the same idempotent reconciler the
	scheduler runs every 15 minutes). Company is always resolved
	server-side. `already_existing` = delivered stops of the company that
	already had their obligation before this run."""
	_require_cartera_access()
	company = _company()
	already_existing = cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM `tabRecorrido Parada` p
			INNER JOIN `tabRecorrido` r ON r.name = p.recorrido
			INNER JOIN `tabCartera Obligacion` o ON o.recorrido_parada = p.name
			WHERE p.status = 'Entregado' AND r.company = %s
			""",
			(company,),
		)[0][0]
	)
	result = cartera_service.sync_missing_obligations(company=company)
	result["already_existing"] = already_existing
	return result


# ---------------------------------------------------------------------------
# 27.3 -- Writes
# ---------------------------------------------------------------------------


def _action_response(obligation_name, company, result):
	"""What the Page needs to refresh without a reload: the obligation's
	detail and the company KPIs (the list is refreshed once, on return)."""
	return {
		"result": result,
		"detail": _detail_payload(frappe.get_doc(cartera_service.OBLIGATION_DOCTYPE, obligation_name)),
		"dashboard": _dashboard_payload(company),
	}


@frappe.whitelist(methods=["POST"])
def register_payment(
	obligation_name,
	amount=None,
	payment_date=None,
	payment_method=None,
	reference=None,
	notes=None,
	client_request_id=None,
):
	"""REGISTRAR COBRO (multipart: optional file "payment_proof").

	The client sends only these fields and the image; company, customer,
	currency, source (Cartera), recorded_by/on, accounting_status and every
	balance are decided by the server. The proof is read ONCE and
	validated/re-encoded (26.3 pipeline: real image bytes JPEG/PNG/WEBP,
	EXIF orientation, max side 1600, JPEG without metadata, 8 MB input)
	before any lock and outside the retried block."""
	_require_cartera_access()
	company = _company()
	proof = _recorridos._read_uploaded_file("payment_proof", _recorridos.DELIVERY_PHOTO_MAX_BYTES)
	proof_jpeg = _recorridos._normalize_payment_proof(proof) if proof else None
	_load_obligation(obligation_name, company)

	result = _register_payment_tx(
		obligation_name, amount, payment_date, payment_method, reference, notes, client_request_id, proof_jpeg
	)
	return _action_response(obligation_name, company, result)


@_recorridos._retrying_on_deadlock
def _register_payment_tx(obligation_name, amount, payment_date, payment_method, reference, notes, client_request_id, proof_jpeg):
	return cartera_service.register_cartera_payment(
		obligation_name,
		amount,
		payment_date,
		payment_method,
		reference=reference,
		notes=notes,
		client_request_id=client_request_id,
		proof_content=proof_jpeg,
	)


@frappe.whitelist(methods=["POST"])
def confirm_driver_payment(obligation_name):
	"""CONFIRMAR PAGO del conductor (only the verification changes)."""
	_require_cartera_access()
	company = _company()
	_load_obligation(obligation_name, company)
	result = _confirm_tx(obligation_name)
	return _action_response(obligation_name, company, result)


@_recorridos._retrying_on_deadlock
def _confirm_tx(obligation_name):
	return cartera_service.confirm_driver_payment(obligation_name)


@frappe.whitelist(methods=["POST"])
def reject_driver_payment(obligation_name, reason=None):
	"""RECHAZAR PAGO / REPORTE del conductor (motivo obligatorio). With a
	driver payment it is cancelled (docstatus 2, kept as history) and the
	debt comes back; without one only the report is resolved."""
	_require_cartera_access()
	company = _company()
	_load_obligation(obligation_name, company)
	result = _reject_tx(obligation_name, reason)
	return _action_response(obligation_name, company, result)


@_recorridos._retrying_on_deadlock
def _reject_tx(obligation_name, reason):
	return cartera_service.reject_driver_payment(obligation_name, reason)
