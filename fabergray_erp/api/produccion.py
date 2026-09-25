# -*- coding: utf-8 -*-
"""api/produccion.py -- Fase 28.3: Page Producción, dashboard operativo de
SOLO LECTURA sobre la fundación de 28.1/28.2 (Work Order nativo enlazado a
Reporte de Faltante por production_service).

Every endpoint is GET-only (frappe.whitelist(methods=["GET"])) and only
READS: no Work Order, Stock Entry, BOM, Item, Reporte de Faltante, Bin or
any other document is created or modified here, nothing is reserved and
nothing is committed. Manufacturing actions arrive in 28.4.

Access (every endpoint): logged in + role Producción, Jefe de Producción
or System Manager + the site's company, resolved server-side and checked
against the caller's allowed companies (permission_conditions.
_allowed_companies(), the same boundary Cartera/Ventas apply). The roles
have NO DocPerm on Work Order/BOM/Item/Stock Entry/Reporte de Faltante
(28.2 test_new_roles_exist_without_broad_permissions): this module reads
with narrow, parameterized, company-scoped SQL instead of opening the
doctypes. The Page's own roles are only a convenience; this is the real
boundary.

Population (the only Work Orders this Page ever shows): a SUBMITTED
(docstatus 1) Work Order of the resolved company that at least one
Reporte de Faltante of the same company (its warehouse's company) links
through work_order with procurement_route = "Manufacture". Historical
ERPNext Work Orders without such a report, Purchase-routed shortages,
drafts and cancelled Work Orders (docstatus 2 -- a cancellation also
unlinks every non-resolved report, production_service.
on_work_order_cancel) never appear.

Estado operativo -- ONE value per order, from ERPNext's native status
(work_order.py get_status()) and produced_qty. Audited: with
skip_transfer=1 (every 28.2 order) ERPNext sets "In Process" right after
submit, before anything is produced, so the native string alone can't
say "en producción":

  completada     status = Completed (native: produced_qty +
                 process_loss_qty >= qty)
  detenida       status = Stopped
  cerrada        status = Closed
  en_produccion  any other submitted status AND produced_qty > 0
  pendiente      any other submitted status AND produced_qty = 0

Materiales -- a separate flag, only for open orders (pendiente /
en_produccion); an order never appears twice, it just carries both. Per
component (Work Order Item):

  necesario pendiente = max(required_qty - consumed_qty, 0)
      (consumed_qty is ERPNext's own figure: SUM(transfer_qty) of the
      submitted Manufacture / Material Consumption entries of this order,
      work_order.py update_consumed_qty_for_required_items(); with
      skip_transfer there is no Material Transfer to WIP, so the
      component is consumed straight from its source_warehouse)
  disponible = Bin.actual_qty of (item_code, source_warehouse), read LIVE
      -- never Work Order Item.available_qty_at_source_warehouse, a
      snapshot ERPNext stores at save time. Physical stock of that
      warehouse: it does not discount what OTHER open orders will consume
      (each order shows the warehouse's real stock).
  falta = max(necesario pendiente - max(disponible, 0), 0)

  and the order's material level, worst first:
  config   a component still needed whose source warehouse is missing,
           does not exist, belongs to another company, is a group or is
           disabled ("CONFIGURACIÓN INCOMPLETA"; its stock is never read)
  falta    a component still needed with disponible <= 0
  parcial  a component still needed with 0 < disponible < necesario
  ok       every component covered (or already consumed)

FALTA MATERIAL (KPI and tab) = open orders whose level is not ok.

Quantities never mixed: qty (planned), produced_qty (real production),
allocated = SUM(production_qty_allocated) of ALL reports linked to the
order (the same figure production_service._allocated_on() uses for
capacity), available_lot = max(qty - allocated, 0).

COMPLETADAS HOY: completada AND the posting_date of its LAST submitted
"Manufacture" Stock Entry (the native production evidence; ERPNext
derives Work Order.actual_end_date from the same entries) is today (the
site's calendar day, frappe.utils.nowdate()). Work Order.modified is
never used as a production date. 28.3 creates no Manufacture entry, so
locally this KPI is 0 until 28.4.

The same SQL (_base_sql()) feeds the KPIs, the tabs, the cards and the
detail's state, so they can never disagree.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from erpnext import get_default_company

from fabergray_erp.api.bodega import _require_login
from fabergray_erp.permission_conditions import _allowed_companies

PRODUCTION_ROLES = ("Producción", "Jefe de Producción", "System Manager")

DEFAULT_PAGE_LENGTH = 20
MAX_PAGE_LENGTH = 50
MAX_SEARCH_LENGTH = 140

#: Float tolerance for quantity comparisons (SQL and Python alike).
EPS = 0.000001

#: Tabs -- constant SQL fragments over the derived row `t` (never caller
#: input).
TABS = {
	"todas": "",
	"pendientes": "t.state = 'pendiente'",
	"en_produccion": "t.state = 'en_produccion'",
	"falta_material": "t.material_level IN ('parcial', 'falta', 'config')",
	"completadas": "t.state = 'completada'",
}

MATERIAL_FILTERS = {
	"": "",
	"ok": "t.material_level = 'ok'",
	"parcial": "t.material_level = 'parcial'",
	"falta": "t.material_level = 'falta'",
	"config": "t.material_level = 'config'",
}

#: Fecha de la orden = Work Order.planned_start_date (native; 28.2 sets it
#: when the order is created), relative to the site's today.
DATE_RANGES = {"": None, "today": 0, "7d": 6, "30d": 29}

_STATE_SQL = """
	CASE
		WHEN w.status = 'Completed' THEN 'completada'
		WHEN w.status = 'Stopped' THEN 'detenida'
		WHEN w.status = 'Closed' THEN 'cerrada'
		WHEN w.produced_qty > %(eps)s THEN 'en_produccion'
		ELSE 'pendiente'
	END
"""

_MATERIAL_LEVEL_SQL = """
	CASE
		WHEN w.status IN ('Completed', 'Stopped', 'Closed') THEN NULL
		WHEN COALESCE(m.bad_warehouse, 0) > 0 THEN 'config'
		WHEN COALESCE(m.zero_stock, 0) > 0 THEN 'falta'
		WHEN COALESCE(m.partial_stock, 0) > 0 THEN 'parcial'
		ELSE 'ok'
	END
"""

#: A component's source warehouse is usable only if it exists, is of the
#: resolved company, is not a group and is not disabled.
_VALID_WAREHOUSE_SQL = (
	"(wh.name IS NOT NULL AND wh.company = %(company)s AND wh.is_group = 0 AND wh.disabled = 0)"
)
_PENDING_SQL = "GREATEST(wi.required_qty - wi.consumed_qty, 0)"

#: Per-order material aggregate, restricted to the company's submitted
#: orders (never scans other companies' Work Order Items).
_MATERIALS_SQL = f"""
	SELECT
		wi.parent AS work_order,
		SUM(CASE WHEN {_PENDING_SQL} > %(eps)s AND NOT {_VALID_WAREHOUSE_SQL} THEN 1 ELSE 0 END) AS bad_warehouse,
		SUM(CASE WHEN {_PENDING_SQL} > %(eps)s AND {_VALID_WAREHOUSE_SQL}
			AND COALESCE(b.actual_qty, 0) <= %(eps)s THEN 1 ELSE 0 END) AS zero_stock,
		SUM(CASE WHEN {_PENDING_SQL} > %(eps)s AND {_VALID_WAREHOUSE_SQL}
			AND COALESCE(b.actual_qty, 0) > %(eps)s
			AND COALESCE(b.actual_qty, 0) < {_PENDING_SQL} - %(eps)s THEN 1 ELSE 0 END) AS partial_stock
	FROM `tabWork Order Item` wi
	INNER JOIN `tabWork Order` wo ON wo.name = wi.parent AND wo.company = %(company)s AND wo.docstatus = 1
	LEFT JOIN `tabWarehouse` wh ON wh.name = wi.source_warehouse
	LEFT JOIN `tabBin` b ON b.item_code = wi.item_code AND b.warehouse = wi.source_warehouse
	WHERE wi.parenttype = 'Work Order' AND wi.parentfield = 'required_items'
	GROUP BY wi.parent
"""

#: Reports of the resolved company only (Reporte de Faltante has no company
#: field: its warehouse's company is the boundary).
_COMPANY_REPORT_SQL = """
	SELECT r.name, r.work_order, r.procurement_route, r.production_qty_allocated, r.reported_on,
		r.sales_order
	FROM `tabReporte de Faltante` r
	INNER JOIN `tabWarehouse` rw ON rw.name = r.warehouse AND rw.company = %(company)s
	WHERE r.work_order IS NOT NULL AND r.work_order != ''
"""

_IN_POPULATION_SQL = """EXISTS (
	SELECT 1 FROM `tabReporte de Faltante` pr
	INNER JOIN `tabWarehouse` prw ON prw.name = pr.warehouse AND prw.company = %(company)s
	WHERE pr.work_order = w.name AND pr.procurement_route = 'Manufacture'
)"""

_SEARCH_SQL = """(
	w.name LIKE %(search)s OR w.production_item LIKE %(search)s OR w.item_name LIKE %(search)s
	OR EXISTS (
		SELECT 1 FROM `tabReporte de Faltante` sr
		INNER JOIN `tabWarehouse` srw ON srw.name = sr.warehouse AND srw.company = %(company)s
		LEFT JOIN `tabSales Order` sso ON sso.name = sr.sales_order AND sso.company = %(company)s
		WHERE sr.work_order = w.name AND (
			sso.name LIKE %(search)s OR sso.customer LIKE %(search)s OR sso.customer_name LIKE %(search)s
		)
	)
)"""

#: Open orders first, oldest need first; then stopped; then completed and
#: closed, most recent production first. Ties -> name (unique).
_ORDER_BY = """
	ORDER BY
		CASE t.state WHEN 'en_produccion' THEN 1 WHEN 'pendiente' THEN 1 WHEN 'detenida' THEN 2
			WHEN 'completada' THEN 3 ELSE 4 END ASC,
		CASE WHEN t.state IN ('en_produccion', 'pendiente') THEN t.oldest_reported_on END ASC,
		CASE WHEN t.state NOT IN ('en_produccion', 'pendiente') THEN t.last_manufacture_date END DESC,
		t.planned_start_date ASC,
		t.name ASC
"""


# ---------------------------------------------------------------------------
# Access / context
# ---------------------------------------------------------------------------


def _require_production_access():
	_require_login()
	frappe.only_for(PRODUCTION_ROLES)


def _company():
	"""The site's company, never a value sent by the client, and only if
	the caller may operate in it."""
	company = get_default_company()
	allowed = _allowed_companies()
	if not company or (allowed is not None and company not in allowed):
		frappe.throw(_("No tienes acceso a la producción de esta empresa."), frappe.PermissionError)
	return company


def _today():
	return getdate(nowdate())


def _like_pattern(search):
	escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return f"%{escaped}%"


def _base_sql(extra_conditions=""):
	"""One row per Work Order of the population, with its derived state,
	material level and quantity aggregates. `extra_conditions` is always a
	constant fragment built in this module (never caller input)."""
	return f"""
		SELECT
			w.name, w.production_item, w.item_name, w.bom_no, w.qty, w.produced_qty, w.process_loss_qty,
			w.status, w.stock_uom, w.fg_warehouse, w.planned_start_date,
			COALESCE(a.allocated, 0) AS allocated,
			a.oldest_reported_on,
			p.last_manufacture_date,
			{_STATE_SQL} AS state,
			{_MATERIAL_LEVEL_SQL} AS material_level
		FROM `tabWork Order` w
		LEFT JOIN ({_MATERIALS_SQL}) m ON m.work_order = w.name
		LEFT JOIN (
			SELECT cr.work_order, SUM(cr.production_qty_allocated) AS allocated,
				MIN(cr.reported_on) AS oldest_reported_on
			FROM ({_COMPANY_REPORT_SQL}) cr
			GROUP BY cr.work_order
		) a ON a.work_order = w.name
		LEFT JOIN (
			SELECT se.work_order, MAX(se.posting_date) AS last_manufacture_date
			FROM `tabStock Entry` se
			WHERE se.company = %(company)s AND se.docstatus = 1 AND se.purpose = 'Manufacture'
				AND se.work_order IS NOT NULL
			GROUP BY se.work_order
		) p ON p.work_order = w.name
		WHERE w.company = %(company)s AND w.docstatus = 1 AND {_IN_POPULATION_SQL}
			{extra_conditions}
	"""


def _params(company, **extra):
	return {"company": company, "eps": EPS, "today": _today(), **extra}


def _serialize_order(row):
	qty = flt(row.qty)
	produced = flt(row.produced_qty)
	allocated = flt(row.allocated)
	return {
		"name": row.name,
		"production_item": row.production_item,
		"item_name": row.item_name or row.production_item,
		"bom_no": row.bom_no,
		"stock_uom": row.stock_uom,
		"qty": qty,
		"produced_qty": produced,
		"pending_qty": max(qty - produced, 0.0),
		"allocated_qty": allocated,
		"available_lot_qty": max(qty - allocated, 0.0),
		"native_status": row.status,
		"state": row.state,
		"material_level": row.material_level,
		"planned_start_date": str(row.planned_start_date) if row.planned_start_date else None,
		"last_manufacture_date": str(row.last_manufacture_date) if row.last_manufacture_date else None,
		"oldest_reported_on": str(row.oldest_reported_on) if row.oldest_reported_on else None,
	}


def _origin_by_order(work_orders, company):
	"""{work_order: {oldest report's sales order + customer + reported_on,
	orders_count}} for one page of cards -- one query, never one per card.
	Sales Orders of another company are never exposed."""
	if not work_orders:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT r.work_order, r.reported_on, so.name AS sales_order, so.customer, so.customer_name
		FROM `tabReporte de Faltante` r
		INNER JOIN `tabWarehouse` rw ON rw.name = r.warehouse AND rw.company = %(company)s
		LEFT JOIN `tabSales Order` so ON so.name = r.sales_order AND so.company = %(company)s
		WHERE r.work_order IN %(work_orders)s
		ORDER BY r.reported_on ASC, r.name ASC
		""",
		{"company": company, "work_orders": tuple(work_orders)},
		as_dict=True,
	)
	out = {}
	for row in rows:
		entry = out.setdefault(row.work_order, {"oldest": None, "orders": set()})
		if row.sales_order:
			entry["orders"].add(row.sales_order)
		if entry["oldest"] is None:
			entry["oldest"] = row
	return {
		wo: {
			"oldest_sales_order": e["oldest"].sales_order if e["oldest"] else None,
			"oldest_customer": e["oldest"].customer if e["oldest"] else None,
			"oldest_customer_name": (e["oldest"].customer_name or e["oldest"].customer) if e["oldest"] else None,
			"oldest_reported_on": str(e["oldest"].reported_on) if e["oldest"] and e["oldest"].reported_on else None,
			"orders_count": len(e["orders"]),
		}
		for wo, e in out.items()
	}


# ---------------------------------------------------------------------------
# Endpoints (GET only, read only)
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["GET"])
def get_production_dashboard():
	"""The four KPIs, aggregated server-side over the company's whole
	population (never over the cards the UI happens to have loaded).

	- pendientes: state pendiente;
	- en_produccion: state en_produccion;
	- falta_material: open orders whose material level is not ok;
	- completadas_hoy: state completada whose last submitted Manufacture
	  Stock Entry has posting_date = today (site's day)."""
	_require_production_access()
	company = _company()
	params = _params(company)
	row = frappe.db.sql(
		f"""
		SELECT
			COALESCE(SUM(t.state = 'pendiente'), 0) AS pendientes,
			COALESCE(SUM(t.state = 'en_produccion'), 0) AS en_produccion,
			COALESCE(SUM(t.material_level IN ('parcial', 'falta', 'config')), 0) AS falta_material,
			COALESCE(SUM(t.state = 'completada' AND t.last_manufacture_date = %(today)s), 0) AS completadas_hoy,
			COUNT(*) AS total
		FROM ({_base_sql()}) t
		""",
		params,
		as_dict=True,
	)[0]
	return {
		"company": company,
		"today": str(params["today"]),
		"kpis": {key: cint(row[key]) for key in ("pendientes", "en_produccion", "falta_material", "completadas_hoy")},
		"total": cint(row.total),
	}


@frappe.whitelist(methods=["GET"])
def get_production_orders(tab="todas", search="", material="", date_range="", page=1, page_length=DEFAULT_PAGE_LENGTH):
	"""One page of production cards: tab + material + date filters and the
	search applied in SQL (two queries: COUNT + page, plus one for the
	page's origin data)."""
	_require_production_access()
	company = _company()

	tab = tab or "todas"
	material = material or ""
	date_range = date_range or ""
	if tab not in TABS:
		frappe.throw(_("Filtro de producción inválido."), frappe.ValidationError)
	if material not in MATERIAL_FILTERS:
		frappe.throw(_("Filtro de materiales inválido."), frappe.ValidationError)
	if date_range not in DATE_RANGES:
		frappe.throw(_("Filtro de fecha inválido."), frappe.ValidationError)
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length) or DEFAULT_PAGE_LENGTH, 1), MAX_PAGE_LENGTH)
	search = (search or "").strip()[:MAX_SEARCH_LENGTH]

	params = _params(company, limit=page_length, offset=(page - 1) * page_length)
	inner = ""
	if search:
		params["search"] = _like_pattern(search)
		inner += f" AND {_SEARCH_SQL}"
	if DATE_RANGES[date_range] is not None:
		params["date_from"] = add_days(params["today"], -DATE_RANGES[date_range])
		inner += " AND DATE(w.planned_start_date) >= %(date_from)s"
	outer = [c for c in (TABS[tab], MATERIAL_FILTERS[material]) if c]
	where = ("WHERE " + " AND ".join(outer)) if outer else ""
	source = f"({_base_sql(inner)}) t"

	total = cint(frappe.db.sql(f"SELECT COUNT(*) FROM {source} {where}", params)[0][0])
	rows = frappe.db.sql(
		f"SELECT t.* FROM {source} {where} {_ORDER_BY} LIMIT %(limit)s OFFSET %(offset)s",
		params,
		as_dict=True,
	)
	origin = _origin_by_order([r.name for r in rows], company)
	items = []
	for row in rows:
		item = _serialize_order(row)
		item.update(
			origin.get(row.name)
			or {
				"oldest_sales_order": None,
				"oldest_customer": None,
				"oldest_customer_name": None,
				"orders_count": 0,
			}
		)
		items.append(item)
	return {
		"items": items,
		"page": page,
		"page_size": page_length,
		"total": total,
		"has_more": page * page_length < total,
		"tab": tab,
		"material": material,
		"date_range": date_range,
		"search": search,
	}


@frappe.whitelist(methods=["GET"])
def get_production_order_detail(work_order):
	"""DETALLE DE PRODUCCIÓN: the order (same state/material level as its
	card), every associated report (pedidos / faltantes) and every raw
	material with live availability."""
	_require_production_access()
	company = _company()
	if not work_order or not isinstance(work_order, str):
		frappe.throw(_("Orden de producción inválida."), frappe.ValidationError)
	header = frappe.db.get_value("Work Order", work_order, ["company", "docstatus"], as_dict=True)
	if not header:
		frappe.throw(_("La orden de producción no existe."), frappe.DoesNotExistError)
	if header.company != company:
		frappe.throw(_("No tienes acceso a documentos de otra empresa."), frappe.PermissionError)

	params = _params(company, work_order=work_order)
	rows = frappe.db.sql(f"SELECT t.* FROM ({_base_sql(' AND w.name = %(work_order)s')}) t", params, as_dict=True)
	if not rows:
		frappe.throw(_("Esta orden no pertenece al tablero de producción."), frappe.DoesNotExistError)
	order = _serialize_order(rows[0])
	order["fg_warehouse"] = _company_warehouse(rows[0].fg_warehouse, company)
	order["reports"], order["reports_allocated_total"] = _order_reports(work_order, company)
	order["materials"] = _order_materials(work_order, company)
	return order


def _company_warehouse(warehouse, company):
	"""The warehouse name only when it belongs to `company`."""
	if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") == company:
		return warehouse
	return None


def _order_reports(work_order, company):
	rows = frappe.db.sql(
		"""
		SELECT r.name, r.item_code, i.item_name, r.qty_faltante, r.production_qty_allocated, r.status,
			r.reported_on, pl.name AS pick_list, so.name AS sales_order, so.customer, so.customer_name
		FROM `tabReporte de Faltante` r
		INNER JOIN `tabWarehouse` rw ON rw.name = r.warehouse AND rw.company = %(company)s
		LEFT JOIN `tabItem` i ON i.name = r.item_code
		LEFT JOIN `tabSales Order` so ON so.name = r.sales_order AND so.company = %(company)s
		LEFT JOIN `tabPick List` pl ON pl.name = r.pick_list AND pl.company = %(company)s
		WHERE r.work_order = %(work_order)s
		ORDER BY r.reported_on ASC, r.name ASC
		""",
		{"company": company, "work_order": work_order},
		as_dict=True,
	)
	reports = [
		{
			"name": r.name,
			"customer": r.customer,
			"customer_name": r.customer_name or r.customer,
			"sales_order": r.sales_order,
			"pick_list": r.pick_list,
			"item_code": r.item_code,
			"item_name": r.item_name or r.item_code,
			"qty_faltante": flt(r.qty_faltante),
			"production_qty_allocated": flt(r.production_qty_allocated),
			"status": r.status,
			"reported_on": str(r.reported_on) if r.reported_on else None,
		}
		for r in rows
	]
	return reports, sum(r["production_qty_allocated"] for r in reports)


def material_row_level(pending, warehouse_valid, available):
	"""Python twin of the per-component rule in _MATERIALS_SQL."""
	if pending <= EPS:
		return "consumido"
	if not warehouse_valid:
		return "config"
	if flt(available) <= EPS:
		return "falta"
	if flt(available) < pending - EPS:
		return "parcial"
	return "ok"


def _order_materials(work_order, company):
	rows = frappe.db.sql(
		"""
		SELECT wi.idx, wi.item_code, COALESCE(NULLIF(wi.item_name, ''), i.item_name, wi.item_code) AS item_name,
			COALESCE(NULLIF(wi.stock_uom, ''), i.stock_uom) AS stock_uom, wi.source_warehouse,
			wi.required_qty, wi.consumed_qty,
			wh.name AS wh_name, wh.company AS wh_company, wh.is_group AS wh_is_group, wh.disabled AS wh_disabled
		FROM `tabWork Order Item` wi
		LEFT JOIN `tabItem` i ON i.name = wi.item_code
		LEFT JOIN `tabWarehouse` wh ON wh.name = wi.source_warehouse
		WHERE wi.parent = %(work_order)s AND wi.parenttype = 'Work Order' AND wi.parentfield = 'required_items'
		ORDER BY wi.idx ASC
		""",
		{"work_order": work_order},
		as_dict=True,
	)
	materials = []
	for r in rows:
		if not r.source_warehouse:
			warehouse_problem = "sin_bodega"
		elif not r.wh_name:
			warehouse_problem = "no_existe"
		elif r.wh_company != company:
			warehouse_problem = "otra_empresa"
		elif cint(r.wh_is_group) or cint(r.wh_disabled):
			warehouse_problem = "invalida"
		else:
			warehouse_problem = None
		valid = warehouse_problem is None
		# Stock is read only for a valid warehouse of this company.
		available = (
			flt(frappe.db.get_value("Bin", {"item_code": r.item_code, "warehouse": r.source_warehouse}, "actual_qty"))
			if valid
			else None
		)
		required = flt(r.required_qty)
		consumed = flt(r.consumed_qty)
		pending = max(required - consumed, 0.0)
		shortfall = max(pending - max(available or 0.0, 0.0), 0.0) if pending > EPS else 0.0
		materials.append(
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"stock_uom": r.stock_uom,
				# Another company's warehouse name is never exposed.
				"source_warehouse": r.source_warehouse if valid or warehouse_problem == "invalida" else None,
				"warehouse_problem": warehouse_problem,
				"required_qty": required,
				"consumed_qty": consumed,
				"pending_qty": pending,
				"available_qty": available,
				"shortfall_qty": shortfall,
				"level": material_row_level(pending, valid, available),
			}
		)
	return materials
