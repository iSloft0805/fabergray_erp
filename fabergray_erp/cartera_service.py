# -*- coding: utf-8 -*-
"""fabergray_erp/cartera_service.py -- Fase 27.1: Cartera foundation.

System service (NOT an interactive api/ module): creates and maintains
Cartera Obligacion / Cartera Pago records that are 100% derived from data the
app already owns. It sits next to fulfillment/* and pricing.py -- the other
system services -- and, like them, is the one place allowed to use
`ignore_permissions=True`, each use documented at the call site.

Why a service with that exception (approved decision V3, Fase 27 audit):
an obligation is born when a DRIVER confirms a delivery (api.recorridos.
deliver_stop()), and the Recorrido role has -- deliberately -- no permission
on Sales Order or on Cartera. The obligation holds no user input at all:
Cartera Obligacion.validate() re-derives every field from the delivered
Recorrido Parada (only `recorrido_parada` is ever taken from the caller), so
granting Recorrido create-rights on Cartera would be strictly worse.

Where the money comes from (Fase 27 audit, section A): there is no Sales
Invoice in this flow and no persisted invoice total. The value is the SAME
total the customer received on the commercial PDF, computed by
api.facturacion._build_invoice_lines_and_totals() (frozen fg_invoice_rate
since Commit 25.25, SO rounded/grand total when the Pick List covers the
whole order) and frozen on the obligation at creation. It is never
recomputed from current Item prices. When that function refuses (partial
invoice + order taxes/discount, multi-order Pick List...), the obligation is
still created -- "Por Validar", amount_source "Sin valor calculable" -- so it
is never lost (decision V1).

Creation paths, all idempotent (the Cartera Obligacion.recorrido_parada
unique index is the final guarantee against duplicates):

1. ensure_obligation_after_delivery() -- called at the end of deliver_stop(),
   inside a savepoint: a Cartera failure is rolled back to the savepoint and
   logged, and the physical delivery still commits.
2. sync_missing_obligations() -- reconciler for any Entregado stop without an
   obligation (scheduler + manual trigger). Also the backfill.

Payment is only REPORTED here: nothing in this module creates Payment
Entry, Sales Invoice, Journal Entry or GL Entry, or touches Sales Order,
Pick List, Customer or stock.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from fabergray_erp.api import facturacion

OBLIGATION_DOCTYPE = "Cartera Obligacion"
PAYMENT_DOCTYPE = "Cartera Pago"

STATUS_POR_VALIDAR = "Por Validar"
STATUS_PENDIENTE = "Pendiente"
STATUS_PAGADO = "Pagado"
STATUS_ANULADA = "Anulada"

VERIFICATION_UNCONFIRMED = "Sin confirmar"

AMOUNT_SOURCE_INVOICE = "Factura comercial"
AMOUNT_SOURCE_UNAVAILABLE = "Sin valor calculable"

DRIVER_PAID = "Pagado"
DRIVER_CREDIT = "Crédito"

PAYMENT_SOURCE_CARTERA = "Cartera"
PAYMENT_SOURCE_DRIVER = "Conductor"

#: Approved decision: every "Crédito" delivery gets 30 days for now. Stored
#: on each obligation (credit_days), so a future per-customer term never
#: rewrites existing obligations.
DEFAULT_CREDIT_DAYS = 30

#: Fields a Cartera Obligacion takes from its delivered stop and never
#: changes afterwards.
DERIVED_FIELDS = (
	"recorrido",
	"pick_list",
	"sales_order",
	"commercial_name",
	"company",
	"customer",
	"customer_name",
	"invoice_issuer",
	"invoice_amount",
	"currency",
	"amount_source",
	"credit_days",
	"due_date",
	"delivered_on",
	"delivery_date",
	"delivered_by",
	"driver_payment_status",
	"driver_payment_note",
	"driver_payment_proof",
	"has_delivery_issues",
	"delivery_issues",
	"payment_verification",
)

#: Fields only the balance recomputation may change.
BALANCE_FIELDS = ("paid_amount", "outstanding_amount", "paid_on", "status")

_SAVEPOINT = "fg_cartera_obligation"


class ObligationSourceError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def compute_invoice_amount(pick_list_name, company):
	"""(amount, amount_source, currency) -- the commercial-PDF total of this
	Pick List, or (None, AMOUNT_SOURCE_UNAVAILABLE, currency) when it cannot
	be computed without inventing a fiscal rule. Reads through get_doc
	(system context, no permission check): see module docstring."""
	pl = frappe.get_doc("Pick List", pick_list_name)
	locations = pl.get("locations") or []
	sales_orders = {row.sales_order for row in locations if row.sales_order}
	if not locations or len(sales_orders) != 1 or any(not row.sales_order for row in locations):
		return None, AMOUNT_SOURCE_UNAVAILABLE, None

	so = frappe.get_doc("Sales Order", next(iter(sales_orders)))
	if so.docstatus != 1 or so.company != company:
		return None, AMOUNT_SOURCE_UNAVAILABLE, so.currency

	message_count = len(frappe.local.message_log or [])
	try:
		_lines, totals = facturacion._build_invoice_lines_and_totals(pl, so)
	except frappe.ValidationError:
		_discard_messages_since(message_count)
		return None, AMOUNT_SOURCE_UNAVAILABLE, so.currency

	# _build_invoice_lines_and_totals() already rounds to the money precision (2).
	amount = flt(totals["total"], 2)
	if amount <= 0:
		return None, AMOUNT_SOURCE_UNAVAILABLE, so.currency
	return amount, AMOUNT_SOURCE_INVOICE, so.currency


def derive_obligation_values(recorrido_parada):
	"""Every field of a new obligation, from the delivered stop. Raises
	ObligationSourceError if the stop is not a real, delivered stop."""
	if not recorrido_parada or not frappe.db.exists("Recorrido Parada", recorrido_parada):
		frappe.throw(_("La parada {0} no existe.").format(recorrido_parada), ObligationSourceError)

	stop = frappe.get_doc("Recorrido Parada", recorrido_parada)
	if stop.status != "Entregado" or not stop.delivered_on:
		frappe.throw(
			_("Solo una parada Entregada genera una obligación de cartera ({0}).").format(stop.name),
			ObligationSourceError,
		)

	company = frappe.db.get_value("Recorrido", stop.recorrido, "company")
	if not company:
		frappe.throw(_("El recorrido de la parada {0} no existe.").format(stop.name), ObligationSourceError)

	amount, amount_source, currency = compute_invoice_amount(stop.pick_list, company)
	currency = currency or frappe.get_cached_value("Company", company, "default_currency")

	delivery_date = getdate(stop.delivered_on)
	is_credit = stop.payment_status == DRIVER_CREDIT
	credit_days = DEFAULT_CREDIT_DAYS if is_credit else 0

	return {
		"recorrido": stop.recorrido,
		"pick_list": stop.pick_list,
		"sales_order": stop.sales_order,
		"commercial_name": _commercial_name(stop.sales_order),
		"company": company,
		"customer": stop.customer,
		"customer_name": stop.customer_name,
		"invoice_issuer": frappe.db.get_value("Pick List", stop.pick_list, "fg_invoice_issuer") or None,
		"invoice_amount": amount or 0,
		"currency": currency,
		"amount_source": amount_source,
		"credit_days": credit_days,
		"due_date": add_days(delivery_date, credit_days) if is_credit else None,
		"delivered_on": stop.delivered_on,
		"delivery_date": delivery_date,
		"delivered_by": stop.delivered_by,
		"driver_payment_status": stop.payment_status,
		"driver_payment_note": stop.payment_note,
		"driver_payment_proof": stop.payment_proof,
		"has_delivery_issues": cint(stop.has_delivery_issues),
		"delivery_issues": stop.delivery_issues,
		# Decision V2: a payment REPORTED by the driver stays "Sin
		# confirmar" until Cartera confirms it (Fase 27.2).
		"payment_verification": VERIFICATION_UNCONFIRMED if stop.payment_status == DRIVER_PAID else None,
	}


def _commercial_name(sales_order):
	if not sales_order:
		return None
	from fabergray_erp.sales_order_naming import root_commercial_name

	return root_commercial_name(sales_order)


# ---------------------------------------------------------------------------
# Balances -- always recomputed from submitted Cartera Pago rows
# ---------------------------------------------------------------------------


def submitted_payments(obligation_name, exclude=None, for_update=False):
	"""(total, last_payment_date) of the SUBMITTED payments of an obligation.
	for_update=True turns it into a locking read, which always returns the
	latest committed rows (a plain read inside a REPEATABLE READ transaction
	may return a stale snapshot -- see api.recorridos.
	_locked_assigned_pick_lists()). A narrow parameterized query: frappe.
	get_list() has no FOR UPDATE."""
	if not obligation_name:
		return 0.0, None
	params = [obligation_name]
	exclude_clause = ""
	if exclude:
		exclude_clause = "AND name != %s"
		params.append(exclude)
	lock_clause = "FOR UPDATE" if for_update else ""
	rows = frappe.db.sql(
		f"""
		SELECT amount, payment_date
		FROM `tabCartera Pago`
		WHERE cartera_obligacion = %s AND docstatus = 1 {exclude_clause}
		{lock_clause}
		""",
		tuple(params),
		as_dict=True,
	)
	total = sum(flt(r.amount) for r in rows)
	last = max((getdate(r.payment_date) for r in rows if r.payment_date), default=None)
	return total, last


def expected_balances(doc, paid_total, last_payment_date):
	"""The single rule for paid/outstanding/paid_on/status (decisions V1,
	V2, V4). Pure: same inputs, same answer. "Vencido" is never stored --
	it is derived at read time from outstanding_amount and due_date."""
	precision = cint(doc.precision("invoice_amount")) or 2
	paid = flt(paid_total, precision)

	if doc.status == STATUS_ANULADA:
		return {"paid_amount": paid, "outstanding_amount": 0, "paid_on": None, "status": STATUS_ANULADA}

	if doc.amount_source != AMOUNT_SOURCE_INVOICE:
		# Decision V1: no computable value -> Por Validar, nothing owed yet.
		return {"paid_amount": paid, "outstanding_amount": 0, "paid_on": None, "status": STATUS_POR_VALIDAR}

	outstanding = flt(flt(doc.invoice_amount, precision) - paid, precision)
	if outstanding <= 0:
		return {
			"paid_amount": paid,
			"outstanding_amount": 0,
			"paid_on": last_payment_date,
			"status": STATUS_PAGADO,
		}

	if doc.driver_payment_status == DRIVER_PAID and doc.payment_verification == VERIFICATION_UNCONFIRMED:
		# Decision V2: "Pagado" reported WITHOUT proof -> Por Validar.
		status = STATUS_POR_VALIDAR
	else:
		status = STATUS_PENDIENTE
	return {"paid_amount": paid, "outstanding_amount": outstanding, "paid_on": None, "status": status}


def recompute_obligation(obligation_name):
	"""Re-derives paid/outstanding/paid_on/status from the submitted
	payments, under a row lock on the obligation, and saves it through the
	ORM (Cartera Obligacion.validate() re-checks the same rule).

	ignore_permissions=True: the saved values are purely derived from
	already-validated, submitted Cartera Pago rows -- no user input is
	written. The caller (a payment's on_submit/on_cancel) already passed its
	own permission checks."""
	frappe.db.get_value(OBLIGATION_DOCTYPE, obligation_name, "name", for_update=True)
	doc = frappe.get_doc(OBLIGATION_DOCTYPE, obligation_name)
	paid, last = submitted_payments(obligation_name, for_update=True)
	doc.update(expected_balances(doc, paid, last))
	doc.save(ignore_permissions=True)
	return doc


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_driver_reported_payment(obligation):
	"""Decision V2: the driver reported "Pagado" WITH a proof -> a submitted
	Cartera Pago (source Conductor) for the full value, so the obligation is
	Pagado (payment_verification stays "Sin confirmar" until Cartera
	confirms). The proof image is NOT copied: it stays attached to the stop
	and the obligation keeps its reference (driver_payment_proof).

	Idempotent: client_request_id "conductor:<stop>" is unique.

	ignore_permissions=True: system-generated from the driver's own already-
	validated delivery report (see module docstring)."""
	if not (
		obligation.driver_payment_status == DRIVER_PAID
		and obligation.driver_payment_proof
		and obligation.amount_source == AMOUNT_SOURCE_INVOICE
		and flt(obligation.invoice_amount) > 0
	):
		return None

	request_id = f"conductor:{obligation.recorrido_parada}"
	existing = frappe.db.get_value(PAYMENT_DOCTYPE, {"client_request_id": request_id}, "name")
	if existing:
		return existing

	payment = frappe.get_doc(
		{
			"doctype": PAYMENT_DOCTYPE,
			"cartera_obligacion": obligation.name,
			"amount": obligation.invoice_amount,
			"payment_date": obligation.delivery_date,
			"source": PAYMENT_SOURCE_DRIVER,
			"notes": obligation.driver_payment_note,
			"client_request_id": request_id,
		}
	)
	payment.insert(ignore_permissions=True)
	payment.submit()
	return payment.name


def ensure_obligation_for_stop(recorrido_parada):
	"""(obligation_name, created). Idempotent: an existing obligation for
	this stop is returned as-is; a concurrent insert that loses the race on
	the unique index returns the winner's.

	ignore_permissions=True: only `recorrido_parada` is supplied here --
	Cartera Obligacion.validate() derives everything else from the stop
	(see module docstring)."""
	existing = frappe.db.get_value(OBLIGATION_DOCTYPE, {"recorrido_parada": recorrido_parada}, "name")
	if existing:
		return existing, False

	message_count = len(frappe.local.message_log or [])
	doc = frappe.get_doc({"doctype": OBLIGATION_DOCTYPE, "recorrido_parada": recorrido_parada})
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		_discard_messages_since(message_count)
		winner = frappe.db.get_value(
			OBLIGATION_DOCTYPE, {"recorrido_parada": recorrido_parada}, "name", for_update=True
		)
		if winner:
			return winner, False
		raise
	return doc.name, True


def ensure_obligation_after_delivery(recorrido_parada):
	"""Called by api.recorridos.deliver_stop() right after the stop is saved
	as Entregado, in the SAME transaction -- so a working Cartera makes the
	obligation appear immediately, atomically with the delivery.

	A Cartera failure must never undo a valid physical delivery: everything
	runs inside a savepoint; any error rolls back ONLY to the savepoint, is
	logged (Error Log) and its user-facing messages are discarded, and the
	delivery goes on. sync_missing_obligations() picks the stop up later.

	A deadlock is re-raised on purpose: InnoDB aborts the whole transaction
	on a deadlock (the savepoint no longer exists), so the delivery's own
	@_retrying_on_deadlock must retry everything."""
	message_count = len(frappe.local.message_log or [])
	frappe.db.savepoint(_SAVEPOINT)
	try:
		return ensure_obligation_for_stop(recorrido_parada)[0]
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		_discard_messages_since(message_count)
		frappe.log_error(
			title=_("Cartera: no se pudo crear la obligación de la parada {0}").format(recorrido_parada),
			reference_doctype="Recorrido Parada",
			reference_name=recorrido_parada,
		)
		return None


def delivered_stops_without_obligation(company=None, limit=500):
	"""Entregado stops that still have no obligation -- a read-only anti-join
	(the reconciler's work list)."""
	params = []
	company_clause = ""
	if company:
		company_clause = "AND r.company = %s"
		params.append(company)
	params.append(cint(limit) or 500)
	return frappe.db.sql_list(
		f"""
		SELECT p.name
		FROM `tabRecorrido Parada` p
		INNER JOIN `tabRecorrido` r ON r.name = p.recorrido
		LEFT JOIN `tabCartera Obligacion` o ON o.recorrido_parada = p.name
		WHERE p.status = 'Entregado' AND p.delivered_on IS NOT NULL AND o.name IS NULL
		{company_clause}
		ORDER BY p.delivered_on ASC
		LIMIT %s
		""",
		tuple(params),
	)


def sync_missing_obligations(company=None, limit=500):
	"""The reconciler / backfill: creates the obligation of every Entregado
	stop that has none, each inside its own savepoint so one bad stop never
	blocks the rest. Idempotent -- running it twice creates nothing new."""
	created, failed = [], []
	for stop_name in delivered_stops_without_obligation(company=company, limit=limit):
		message_count = len(frappe.local.message_log or [])
		frappe.db.savepoint(_SAVEPOINT)
		try:
			name, was_created = ensure_obligation_for_stop(stop_name)
			if was_created:
				created.append(name)
		except frappe.QueryDeadlockError:
			raise
		except Exception:
			frappe.db.rollback(save_point=_SAVEPOINT)
			_discard_messages_since(message_count)
			frappe.log_error(
				title=_("Cartera: no se pudo sincronizar la parada {0}").format(stop_name),
				reference_doctype="Recorrido Parada",
				reference_name=stop_name,
			)
			failed.append(stop_name)
	return {"created": len(created), "failed": len(failed), "obligations": created, "failed_stops": failed}


def scheduled_sync_missing_obligations():
	"""hooks.py scheduler entry point (every 15 minutes)."""
	sync_missing_obligations()


def _discard_messages_since(count):
	"""frappe.throw() queues a user-facing message before raising; when this
	service catches that exception on purpose, the message must not leak to
	the driver's delivery response."""
	log = frappe.local.message_log
	if log is not None and len(log) > count:
		del log[count:]
