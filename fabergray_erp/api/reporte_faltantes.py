# -*- coding: utf-8 -*-
"""api/reporte_faltantes.py -- Hotfix "Reporte PDF de faltantes" (Jefe de
Bodega): every Reporte de Faltante reported within a period, as JSON (page
summary) and as an A4-landscape PDF. READ-ONLY: nothing here writes any
document -- it never resolves a shortage, never moves stock.

Audit performed before writing any code here:

1. Source: only "Reporte de Faltante" and its real links (sales_order,
   item_code, warehouse, reported_by). Never rebuilt from Pick List.
   Customer comes from the linked Sales Order (the report has no customer
   field of its own -- same resolution api.jefe_bodega.get_shortage_center()
   already uses); the item name from Item.

2. Report date: `reported_on` (Datetime, reqd, set server-side by
   ReportedeFaltante.set_missing_detection_fields()).

3. Resolution date: Reporte de Faltante has NO resolved_on field. It is
   NOT approximated with `modified` (any later save moves it). Instead the
   DocType has track_changes=1 and every code path that resolves a
   shortage (api.jefe_bodega receive flow, production_service,
   fulfillment.shortage_service/cancellation_service) does it with a real
   `.save()` -- no db.set_value -- so Frappe's own Version log records the
   exact moment `status` changed to "Resuelto". _resolution_dates() reads
   that transition. No new field, no migration, and it also covers every
   report resolved before this change. A report whose Version row is
   missing shows "—", never a guessed date.

4. Company isolation: the DocType has no `company` field. Its Warehouse
   does (reqd Link), so the report belongs to its warehouse's Company --
   the same rule api.jefe_bodega.route_shortage() enforces. The Company is
   ALWAYS the site's (get_default_company()), checked against the caller's
   allowed companies (permission_conditions._allowed_companies()); the
   client never sends one. Sales Orders are additionally filtered by that
   company when resolving the customer.

5. PDF: Frappe's native frappe.utils.pdf.get_pdf() -- the same engine
   behind the Facturación Print Format -- over a Jinja template in this app
   (templates/reporte_faltantes_pdf.html). A Print Format cannot be used:
   it renders ONE document, this is a multi-document report.

Roles: Jefe de Bodega and System Manager (frappe.only_for). Bodega is NOT
enabled -- a product decision, reported, not made here.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, format_date, get_first_day, getdate, nowdate

from erpnext import get_default_company

from fabergray_erp.api.bodega import _batch_item_names, _require_login
from fabergray_erp.permission_conditions import _allowed_companies
from fabergray_erp.sales_order_naming import root_commercial_name

REPORT_ROLES = ("Jefe de Bodega", "System Manager")

STATUS_OPEN = "Abierto"
STATUS_IN_PROGRESS = "En Proceso"
STATUS_RESOLVED = "Resuelto"
SHORTAGE_STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED)

PERIOD_TODAY = "hoy"
PERIOD_LAST_7 = "7_dias"
PERIOD_LAST_30 = "30_dias"
PERIOD_THIS_MONTH = "este_mes"
PERIOD_CUSTOM = "rango"
PERIODS = (PERIOD_TODAY, PERIOD_LAST_7, PERIOD_LAST_30, PERIOD_THIS_MONTH, PERIOD_CUSTOM)

#: A custom range is bounded so one request can never ask for years of data.
MAX_CUSTOM_RANGE_DAYS = 366

PDF_TEMPLATE = "fabergray_erp/templates/reporte_faltantes_pdf.html"
PDF_OPTIONS = {"orientation": "Landscape", "page-size": "A4"}


class InvalidReportPeriodError(frappe.ValidationError):
	pass


def resolve_period(period, from_date=None, to_date=None, today=None):
	"""(from_date, to_date) as dates, both inclusive, from the SITE's date
	(frappe.utils.nowdate() -- the site timezone), never the browser's.
	Only the custom range uses the dates the client sends."""
	today = getdate(today or nowdate())
	if period == PERIOD_TODAY:
		return today, today
	if period == PERIOD_LAST_7:
		return getdate(add_days(today, -6)), today
	if period == PERIOD_LAST_30:
		return getdate(add_days(today, -29)), today
	if period == PERIOD_THIS_MONTH:
		return getdate(get_first_day(today)), today
	if period == PERIOD_CUSTOM:
		if not from_date or not to_date:
			frappe.throw(_("Indica la fecha desde y la fecha hasta del rango."), InvalidReportPeriodError)
		try:
			start, end = getdate(from_date), getdate(to_date)
		except Exception:
			frappe.throw(_("Rango de fechas inválido."), InvalidReportPeriodError)
		if start > end:
			frappe.throw(_("La fecha desde no puede ser posterior a la fecha hasta."), InvalidReportPeriodError)
		if date_diff(end, start) + 1 > MAX_CUSTOM_RANGE_DAYS:
			frappe.throw(
				_("El rango no puede superar {0} días.").format(MAX_CUSTOM_RANGE_DAYS), InvalidReportPeriodError
			)
		return start, end
	frappe.throw(_("Período inválido: {0}.").format(period), InvalidReportPeriodError)


def _validate_status(status):
	if not status:
		return None
	if status not in SHORTAGE_STATUSES:
		frappe.throw(_("Estado inválido: {0}.").format(status), InvalidReportPeriodError)
	return status


def _authorized_company():
	"""Login + role + the site's Company, which the caller must be allowed to
	operate in. Never a company sent by the client."""
	_require_login()
	frappe.only_for(REPORT_ROLES)
	frappe.has_permission("Reporte de Faltante", "read", throw=True)
	company = get_default_company()
	allowed = _allowed_companies()
	if not company or (allowed is not None and company not in allowed):
		frappe.throw(_("No tienes acceso a faltantes de esta empresa."), frappe.PermissionError)
	return company


def _company_warehouses(company):
	return frappe.get_list("Warehouse", filters={"company": company}, pluck="name")


def _resolution_dates(report_names):
	"""{report: datetime} -- when each report's `status` last changed to
	"Resuelto", read from Frappe's own Version log (see module docstring,
	point 3). One query for the whole report, never one per row. Read with
	frappe.qb, not frappe.get_list: Version has no DocPerm for Jefe de
	Bodega, and this only reads the audit trail of reports the caller was
	already authorized to list above -- nothing else is returned."""
	if not report_names:
		return {}
	version = frappe.qb.DocType("Version")
	rows = (
		frappe.qb.from_(version)
		.select(version.docname, version.creation, version.data)
		.where(version.ref_doctype == "Reporte de Faltante")
		.where(version.docname.isin(list(report_names)))
		.orderby(version.creation)
		.run(as_dict=True)
	)
	resolved = {}
	for row in rows:
		try:
			changed = (json.loads(row.data or "{}") or {}).get("changed") or []
		except ValueError:
			continue
		for change in changed:
			if len(change) >= 3 and change[0] == "status" and change[2] == STATUS_RESOLVED:
				resolved[row.docname] = row.creation  # last transition wins (ordered by creation)
	return resolved


def _build_report(period, from_date=None, to_date=None, status=None):
	company = _authorized_company()
	start, end = resolve_period(period, from_date, to_date)
	status = _validate_status(status)

	summary = {"total_reports": 0, "open": 0, "in_progress": 0, "resolved": 0, "total_shortage_qty": 0.0}
	result = {
		"period": period,
		"from_date": start.isoformat(),
		"to_date": end.isoformat(),
		"status": status,
		"company": company,
		"summary": summary,
		"items": [],
	}

	warehouses = _company_warehouses(company)
	if not warehouses:
		return result

	filters = [
		["warehouse", "in", warehouses],
		["reported_on", "between", [f"{start.isoformat()} 00:00:00", f"{end.isoformat()} 23:59:59.999999"]],
	]
	if status:
		filters.append(["status", "=", status])

	rows = frappe.get_list(
		"Reporte de Faltante",
		filters=filters,
		fields=[
			"name",
			"reported_on",
			"sales_order",
			"item_code",
			"warehouse",
			"qty_solicitada",
			"qty_disponible",
			"qty_faltante",
			"status",
			"shortage_reason",
			"reported_by",
			"resolution_note",
		],
		order_by="reported_on desc, name desc",
	)
	if not rows:
		return result

	sales_orders = {r.sales_order for r in rows if r.sales_order}
	so_by_name = {}
	if sales_orders:
		so_by_name = {
			so.name: so
			for so in frappe.get_list(
				"Sales Order",
				filters={"name": ["in", list(sales_orders)], "company": company},
				fields=["name", "customer", "customer_name"],
			)
		}
	item_names = _batch_item_names([r.item_code for r in rows])
	resolved_on = _resolution_dates([r.name for r in rows if r.status == STATUS_RESOLVED])
	reporter_names = {}

	for r in rows:
		so = so_by_name.get(r.sales_order)
		if r.reported_by and r.reported_by not in reporter_names:
			reporter_names[r.reported_by] = frappe.utils.get_fullname(r.reported_by)
		result["items"].append(
			{
				"name": r.name,
				"reported_on": r.reported_on,
				"sales_order": r.sales_order if so else None,
				"order_number": root_commercial_name(r.sales_order) if so else None,
				"customer": so.customer if so else None,
				"customer_name": so.customer_name if so else None,
				"item_code": r.item_code,
				"item_name": item_names.get(r.item_code) or None,
				"warehouse": r.warehouse,
				"qty_solicitada": flt(r.qty_solicitada),
				"qty_disponible": flt(r.qty_disponible),
				"qty_faltante": flt(r.qty_faltante),
				"status": r.status,
				"shortage_reason": r.shortage_reason or None,
				"reported_by": r.reported_by,
				"reported_by_name": reporter_names.get(r.reported_by),
				"resolved_on": resolved_on.get(r.name) if r.status == STATUS_RESOLVED else None,
				"resolution_note": (r.resolution_note or "").strip() or None,
			}
		)
		summary["total_reports"] += 1
		summary["total_shortage_qty"] += flt(r.qty_faltante)
		if r.status == STATUS_OPEN:
			summary["open"] += 1
		elif r.status == STATUS_IN_PROGRESS:
			summary["in_progress"] += 1
		elif r.status == STATUS_RESOLVED:
			summary["resolved"] += 1

	summary["total_shortage_qty"] = flt(summary["total_shortage_qty"], 3)
	return result


@frappe.whitelist()
def get_shortage_report(period, from_date=None, to_date=None, status=None):
	"""Read-only JSON for the Reporte de Faltantes page: {from_date, to_date,
	summary: {total_reports, open, in_progress, resolved, total_shortage_qty},
	items: [...]}. See the module docstring for sources and security."""
	return _build_report(period, from_date, to_date, status)


def _fmt_qty(value):
	value = flt(value, 3)
	return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0").rstrip(".")


def _format_generated_on(value):
	# Not format_datetime(value, "dd/mm/yyyy HH:mm"): its babel pattern reads
	# "mm" as MINUTES, which printed the minute in place of the month.
	return f"{format_date(value, 'dd/mm/yyyy')} {value.strftime('%H:%M')}"


def render_shortage_report_html(report):
	"""The PDF's HTML (also what a test inspects). Pure presentation."""
	status_labels = {STATUS_OPEN: "ABIERTO", STATUS_IN_PROGRESS: "EN PROCESO", STATUS_RESOLVED: "RESUELTO"}
	status_filter_labels = {None: "Todos", STATUS_OPEN: "Abiertos", STATUS_IN_PROGRESS: "En proceso", STATUS_RESOLVED: "Resueltos"}
	return frappe.render_template(
		PDF_TEMPLATE,
		{
			"report": report,
			"period_label": f"{format_date(report['from_date'], 'dd/mm/yyyy')} - {format_date(report['to_date'], 'dd/mm/yyyy')}",
			"status_filter_label": status_filter_labels.get(report["status"], report["status"]),
			"status_labels": status_labels,
			"fmt_qty": _fmt_qty,
			"fmt_date": lambda value: format_date(value, "dd/mm/yyyy") if value else "—",
			"generated_on": _format_generated_on(frappe.utils.now_datetime()),
			"generated_by": frappe.utils.get_fullname(frappe.session.user),
		},
	)


def shortage_report_filename(report):
	return f"Reporte-Faltantes-{report['from_date']}-a-{report['to_date']}.pdf"


@frappe.whitelist()
def download_shortage_report_pdf(period, from_date=None, to_date=None, status=None, preview=0):
	"""GENERAR PDF (download) / VISTA PREVIA (preview=1, same PDF shown
	inline in the browser tab). Same validation as get_shortage_report();
	the PDF is produced by Frappe's native get_pdf(), A4 landscape."""
	from frappe.utils.pdf import get_pdf

	report = _build_report(period, from_date, to_date, status)
	html = render_shortage_report_html(report)
	frappe.local.response.filename = shortage_report_filename(report)
	frappe.local.response.filecontent = get_pdf(html, dict(PDF_OPTIONS))
	frappe.local.response.type = "pdf" if cint(preview) else "download"
