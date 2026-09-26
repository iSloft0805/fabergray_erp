# -*- coding: utf-8 -*-
"""fabergray_erp/manufacturing.py -- Fase 28.1: Producción, fundación.

The ONE place that decides whether a product is bought or made
(get_manufacturing_route()) and whether its BOM is operationally usable
(validate_bom_for_production()). Pure reads: this module never writes,
never changes an Item's policy, never creates BOM/Work Order/Stock Entry/
Material Request. fulfillment/analyzer.py delegates to it, so the rule
exists exactly once.

Make/buy authority (approved decision, Fase 28.1): the NATIVE
Item.default_material_request_type --
  "Purchase"     -> bought;
  "Manufacture"  -> made, and only usable with an active, submitted,
                    default BOM of the same company that passes
                    validate_bom_for_production();
  anything else  -> Blocked (never guessed).
No custom make_or_buy field, no Item Group, no name heuristics, and
Purchase is never promoted to Manufacture automatically: the catalog is
migrated product by product, by a human.

validate_bom_for_production() only answers "can ERPNext execute this
BOM": it never judges whether a formula is chemically right (master
data responsibility).
"""

import frappe
from frappe import _
from frappe.utils import flt

from erpnext import get_default_company
from erpnext.stock.get_item_details import get_default_bom

ROUTE_PURCHASE = "Purchase"
ROUTE_MANUFACTURE = "Manufacture"
ROUTE_BLOCKED = "Blocked"

#: Reason kept verbatim from Commit 12 (fulfillment/analyzer.py) -- a
#: Manufacture item without any resolvable BOM.
REASON_MISSING_BOM = "Missing BOM"


def _result(route, bom=None, reason=None, problems=None):
	return {"route": route, "bom": bom, "reason": reason, "problems": problems or []}


def get_manufacturing_route(item_code, company=None):
	"""{route, bom, reason, problems} for one Item.

	- Purchase:    default_material_request_type == "Purchase".
	- Manufacture: default_material_request_type == "Manufacture" AND a
	               default BOM (ERPNext's own get_default_bom(): active,
	               default, submitted, template fallback for variants)
	               that validate_bom_for_production() accepts for `company`.
	- Blocked:     anything else, with the reason ("Missing BOM" when no
	               BOM resolves; the BOM problems otherwise) -- a
	               Manufacture item is never silently downgraded to
	               Purchase.

	`company` defaults to the site's company (the only one this app
	operates in); a BOM of another company never qualifies."""
	company = company or get_default_company()
	if not item_code or not frappe.db.exists("Item", item_code):
		return _result(ROUTE_BLOCKED, reason=f"Item inexistente: {item_code}")

	item = frappe.get_cached_value(
		"Item", item_code, ["default_material_request_type", "disabled", "is_stock_item"], as_dict=True
	)
	policy = item.default_material_request_type

	if policy == ROUTE_PURCHASE:
		return _result(ROUTE_PURCHASE)

	if policy != ROUTE_MANUFACTURE:
		return _result(ROUTE_BLOCKED, reason=f"Unsupported procurement policy: {policy}")

	if item.disabled:
		return _result(ROUTE_BLOCKED, reason="Item deshabilitado")
	if not item.is_stock_item:
		return _result(ROUTE_BLOCKED, reason="Un producto fabricado debe ser de inventario")

	bom_no = get_default_bom(item_code)
	if not bom_no:
		return _result(ROUTE_BLOCKED, reason=REASON_MISSING_BOM)

	problems = validate_bom_for_production(bom_no, item_code, company)
	if problems:
		return _result(ROUTE_BLOCKED, bom=bom_no, reason="BOM no utilizable: " + "; ".join(problems), problems=problems)
	return _result(ROUTE_MANUFACTURE, bom=bom_no)


def validate_bom_for_production(bom_no, item_code, company=None):
	"""Every reason (list of str, empty = usable) why `bom_no` cannot be
	executed by ERPNext to produce `item_code` for `company`:

	- exists, submitted (docstatus 1) and active;
	- produces this item (or its template, get_default_bom()'s own
	  variant fallback);
	- belongs to `company`;
	- quantity > 0 and a UOM equal to the item's stock UOM;
	- at least one component; each component exists, is enabled, is a
	  stock item (it must be consumable from a warehouse), is not the
	  finished product itself, has qty > 0, a valid UOM and a positive
	  conversion factor.

	Formula correctness is NOT judged here."""
	company = company or get_default_company()
	problems = []
	if not bom_no or not frappe.db.exists("BOM", bom_no):
		return [f"BOM inexistente: {bom_no}"]

	bom = frappe.get_doc("BOM", bom_no)
	if bom.docstatus != 1:
		problems.append("la BOM no está enviada (submitted)")
	if not bom.is_active:
		problems.append("la BOM está inactiva")

	template = frappe.db.get_value("Item", item_code, "variant_of")
	if bom.item not in {item_code, template} - {None}:
		problems.append(f"la BOM produce {bom.item}, no {item_code}")
	if bom.company != company:
		problems.append(f"la BOM es de otra empresa ({bom.company})")
	if flt(bom.quantity) <= 0:
		problems.append("la cantidad producida por la BOM debe ser mayor que cero")
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	if bom.uom and stock_uom and bom.uom != stock_uom:
		problems.append(f"la UOM de la BOM ({bom.uom}) no es la UOM de inventario del producto ({stock_uom})")

	rows = bom.get("items") or []
	if not rows:
		problems.append("la BOM no tiene componentes")
	for row in rows:
		label = f"componente {row.idx} ({row.item_code})"
		component = frappe.db.get_value(
			"Item", row.item_code, ["disabled", "is_stock_item", "stock_uom"], as_dict=True
		)
		if not component:
			problems.append(f"{label}: el Item no existe")
			continue
		if row.item_code in {item_code, bom.item}:
			problems.append(f"{label}: un producto no puede ser componente de sí mismo")
		if component.disabled:
			problems.append(f"{label}: el Item está deshabilitado")
		if not component.is_stock_item:
			problems.append(f"{label}: no es un Item de inventario")
		if flt(row.qty) <= 0:
			problems.append(f"{label}: la cantidad debe ser mayor que cero")
		if not row.uom or not frappe.db.exists("UOM", row.uom):
			problems.append(f"{label}: UOM inválida ({row.uom})")
		if flt(row.conversion_factor) <= 0:
			problems.append(f"{label}: factor de conversión inválido")
		if row.stock_uom and row.stock_uom != component.stock_uom:
			problems.append(f"{label}: la UOM de inventario no coincide con la del Item")
	return problems


# ---------------------------------------------------------------------------
# Fase 28.2 / 28.4A.3 -- warehouses (all native configuration, no names in code)
# ---------------------------------------------------------------------------


def _warehouse_problem(warehouse, company, label):
	if not warehouse:
		return f"{label}: sin bodega configurada"
	row = frappe.db.get_value("Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True)
	if not row:
		return f"{label}: la bodega {warehouse} no existe"
	if row.company != company:
		return f"{label}: la bodega {warehouse} es de otra empresa"
	if row.is_group:
		return f"{label}: la bodega {warehouse} es un grupo"
	if row.disabled:
		return f"{label}: la bodega {warehouse} está deshabilitada"
	return None


def resolve_fg_warehouse(item_code, company=None):
	"""(warehouse, problems) -- where `item_code` is delivered when it is
	manufactured for `company` (Fase 28.4A.3: one finished-goods warehouse
	per product line, not one per company). Native configuration only, in
	this order -- the first level that is CONFIGURED wins:

	1. Item Default.default_warehouse (native get_item_defaults());
	2. Item Group Default.default_warehouse (native get_item_group_defaults());
	3. Company.default_fg_warehouse -- fallback for the items not classified
	   into a product-line warehouse yet.

	The winning warehouse is always validated (exists, same company,
	enabled, not a group). An invalid one is a problem, never silently
	skipped to the next level: producing into a warehouse the product does
	not belong to would put the stock where Bodega never picks it."""
	from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
	from erpnext.stock.doctype.item.item import get_item_defaults

	company = company or get_default_company()
	candidates = (
		("Item Default", lambda: get_item_defaults(item_code, company).get("default_warehouse")),
		("Item Group Default", lambda: get_item_group_defaults(item_code, company).get("default_warehouse")),
		("Company.default_fg_warehouse", lambda: frappe.db.get_value("Company", company, "default_fg_warehouse")),
	)
	for source, resolve in candidates:
		warehouse = resolve()
		if warehouse:
			break
	problem = _warehouse_problem(warehouse, company, f"Producto terminado de {item_code} ({source})")
	return warehouse, [problem] if problem else []


def resolve_component_warehouses(bom_no, company=None):
	"""(rows, problems) -- the source warehouse of every BOM component,
	resolved by ERPNext's OWN chain (manufacturing.doctype.bom.bom.
	get_bom_items_as_dict(), the exact function Work Order.set_required_
	items() uses): BOM Item.source_warehouse, else the component's Item
	Default for `company`. A component with none -- or with a warehouse of
	another company, a group or a disabled one -- is a problem, so a Work
	Order is never created with a component that could not be consumed.
	Chemicals and packaging can (and should) come from different
	warehouses; nothing assumes a single source."""
	from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

	company = company or get_default_company()
	items = get_bom_items_as_dict(bom_no, company, qty=1, fetch_exploded=0)
	rows, problems = [], []
	for item in sorted(items.values(), key=lambda d: d.get("idx") or 0):
		warehouse = item.get("source_warehouse") or item.get("default_warehouse")
		rows.append({"item_code": item.item_code, "source_warehouse": warehouse})
		problem = _warehouse_problem(warehouse, company, f"componente {item.item_code}")
		if problem:
			problems.append(problem)
	return rows, problems
