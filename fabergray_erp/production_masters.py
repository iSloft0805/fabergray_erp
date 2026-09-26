# -*- coding: utf-8 -*-
"""fabergray_erp/production_masters.py -- Fase 28.4A.1: master data of the
first real manufactured product (01216 GALON HIPOCLORITO 13%).

This module only CONFIGURES MASTER DATA through the native ERPNext ORM
(Item, Item Default, BOM). It never manufactures: no Work Order, no Stock
Entry, no stock, no price, no cost. The make/buy decision itself stays
where it is (manufacturing.get_manufacturing_route(): native
Item.default_material_request_type + the Item's default BOM), and the
component source warehouses live in the BOM Items / Item Defaults -- never
in production_service.

Fase 28.4A.3 -- multi-warehouse architecture: 01216 is delivered to its
product-line warehouse (Líquidos - FG), the four chemicals come from
Materia Prima - FG and the container + label from Envases - FG (created
by warehouses.ensure_operational_warehouses(), which must run first). The
28.4A.1 configuration (Producto Terminado / Materias Primas / Material de
Empaque) is recognised as SUPERSEDED and migrated, never treated as a
conflict: Item Defaults still pointing there are updated, and the old
submitted BOM (whose components hardcode the old warehouses and cannot be
edited once submitted) is replaced natively -- a new BOM is created and
submitted as the default, then the old one is cancelled (native cancel:
inactive, not default, history kept) -- only when no Work Order or Stock
Entry uses it. Any other difference is still a conflict.

It is NOT a patch and never runs on migrate. It is run explicitly, once per
site, by a System Manager:

    bench --site <site> execute fabergray_erp.production_masters.ensure_hipoclorito_13_galon \
        --kwargs "{'dry_run': True}"      # plan only, writes nothing
    bench --site <site> execute fabergray_erp.production_masters.ensure_hipoclorito_13_galon

Idempotent: every step first reads what exists and only acts when
something is missing. Running it again on a configured site changes
nothing (every action comes back "ok"). A CONFLICT -- an existing master
that differs from the approved spec (another UOM, another default
warehouse, a different active default BOM, a same-named Item with another
code...) -- is never overwritten: the run stops with
ProductionMasterConflict before writing anything, so a human decides.

The approved formula (1 Unidad of 01216) is data below, verbatim:
chemicals by MASS in Gram (the site's float precision is 2 decimals, so
1672.00 / 2072.19 / 0.36 / 5.45 Gram are all exact, while 0.00036 Kg
would round to 0); no density or litre<->gram conversion is assumed.
"""

import frappe
from frappe import _
from frappe.utils import flt

from erpnext import get_default_company

#: Kept verbatim from the approved formula (Fase 28.4A.1).
HIPOCLORITO_13_GALON = {
	"product": "01216",
	"product_uom": "Unidad",
	"product_warehouse": "Líquidos - FG",
	"bom_quantity": 1,
	"raw_material_group": "Materias Prima",
	"raw_materials": [
		{"item_code": "MP-HIPO-SODIO", "item_name": "HIPOCLORITO DE SODIO PURO", "qty": 1672, "uom": "Gram"},
		{"item_code": "MP-AGUA", "item_name": "AGUA PRODUCCION", "qty": 2072.19, "uom": "Gram"},
		{"item_code": "MP-BICROMATO", "item_name": "BICROMATO", "qty": 0.36, "uom": "Gram"},
		{"item_code": "MP-SODA-LIQ", "item_name": "SODA LIQUIDA", "qty": 5.45, "uom": "Gram"},
	],
	"raw_material_warehouse": "Materia Prima - FG",
	"packaging": [
		{"item_code": "01068", "qty": 1, "uom": "Unidad"},  # ENVASE VACIO GALON REDONDO
		{"item_code": "ACCESS-235684", "qty": 1, "uom": "Unidad"},  # ETIQUETA HIPOCLORITO 13 %
	],
	"packaging_warehouse": "Envases - FG",
	#: Fase 28.4A.1 warehouses, superseded by the multi-warehouse
	#: architecture (Fase 28.4A.3): migrated, never a conflict.
	"superseded_warehouses": {
		"product_warehouse": "Producto Terminado - FG",
		"raw_material_warehouse": "Materias Primas - FG",
		"packaging_warehouse": "Material de Empaque - FG",
	},
}

QTY_PRECISION = 2


class ProductionMasterConflict(frappe.ValidationError):
	pass


def _conflict(message):
	frappe.throw(message, ProductionMasterConflict)


def _components(spec):
	"""[(item_code, qty, uom, source_warehouse)] in BOM order."""
	rows = [
		(r["item_code"], flt(r["qty"], QTY_PRECISION), r["uom"], spec["raw_material_warehouse"])
		for r in spec["raw_materials"]
	]
	rows += [
		(r["item_code"], flt(r["qty"], QTY_PRECISION), r["uom"], spec["packaging_warehouse"])
		for r in spec["packaging"]
	]
	return rows


def _superseded_spec(spec):
	"""The same formula with the superseded (Fase 28.4A.1) warehouses, or
	None when the spec supersedes nothing."""
	superseded = spec.get("superseded_warehouses")
	return {**spec, **superseded} if superseded else None


# ---------------------------------------------------------------------------
# Checks (read only) -- every conflict is detected BEFORE any write
# ---------------------------------------------------------------------------


def _check_warehouse(name, company):
	row = frappe.db.get_value("Warehouse", name, ["company", "is_group", "disabled"], as_dict=True)
	if not row or row.company != company or row.is_group or row.disabled:
		_conflict(_("La bodega {0} no existe o no es una bodega activa y no-grupo de {1}.").format(name, company))


def _check_item_default(item_code, company, warehouse, superseded=None):
	"""An existing default warehouse other than `warehouse` is a conflict,
	unless it is the `superseded` one (migrated by _ensure_item_default)."""
	rows = frappe.get_all(
		"Item Default", filters={"parent": item_code, "parenttype": "Item", "company": company}, fields=["default_warehouse"]
	)
	if rows and rows[0].default_warehouse and rows[0].default_warehouse not in {warehouse, superseded}:
		_conflict(
			_("{0} ya tiene bodega por defecto {1} en {2} (se esperaba {3}).").format(
				item_code, rows[0].default_warehouse, company, warehouse
			)
		)


def _check_raw_material(spec_row, group):
	code, name = spec_row["item_code"], spec_row["item_name"]
	same_name = frappe.db.sql(
		"SELECT name FROM `tabItem` WHERE LOWER(TRIM(item_name)) = LOWER(%s) AND name != %s", (name, code)
	)
	if same_name:
		_conflict(_("Ya existe otro Item llamado {0}: {1}. No se duplica el maestro.").format(name, same_name[0][0]))
	item = frappe.db.get_value(
		"Item", code, ["item_name", "stock_uom", "is_stock_item", "disabled", "item_group"], as_dict=True
	)
	if item and (
		item.stock_uom != spec_row["uom"]
		or not item.is_stock_item
		or item.disabled
		or item.item_name.strip().lower() != name.lower()
	):
		_conflict(_("El Item {0} existe pero no coincide con la especificación aprobada: {1}").format(code, dict(item)))
	return bool(item)


def _check_existing_item(code, uom):
	item = frappe.db.get_value("Item", code, ["stock_uom", "is_stock_item", "disabled"], as_dict=True)
	if not item:
		_conflict(_("El Item {0} no existe.").format(code))
	if item.stock_uom != uom or not item.is_stock_item or item.disabled:
		_conflict(_("El Item {0} no es un Item de stock activo en {1}: {2}").format(code, uom, dict(item)))


def _matching_bom(spec, company):
	"""The submitted, active BOM of the product whose components are
	exactly the spec (same items, qty, uom and source warehouse), or None."""
	wanted = sorted(_components(spec))
	for name in frappe.get_all(
		"BOM",
		filters={"item": spec["product"], "company": company, "docstatus": 1, "is_active": 1},
		pluck="name",
		order_by="creation asc",
	):
		bom = frappe.get_doc("BOM", name)
		have = sorted(
			(r.item_code, flt(r.qty, QTY_PRECISION), r.uom, r.source_warehouse) for r in bom.items
		)
		if have == wanted and flt(bom.quantity) == flt(spec["bom_quantity"]) and not bom.with_operations:
			return bom
	return None


def _bom_in_use(bom_no):
	"""Work Orders / Stock Entries (draft or submitted) that use `bom_no`."""
	return frappe.get_all("Work Order", filters={"bom_no": bom_no, "docstatus": ["<", 2]}, pluck="name") + frappe.get_all(
		"Stock Entry", filters={"bom_no": bom_no, "docstatus": ["<", 2]}, pluck="name"
	)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ensure_hipoclorito_13_galon(company=None, dry_run=False):
	"""Configure (or verify) the master data of 01216. Returns
	{"company", "dry_run", "actions": [(step, target, "ok" | "created" |
	"updated" | "cancelled" | "would create" | "would update" |
	"would cancel")], "bom"}."""
	frappe.only_for("System Manager")
	spec = HIPOCLORITO_13_GALON
	company = company or get_default_company()
	dry_run = bool(dry_run) and dry_run not in ("0", "false", "False")
	actions = []

	# 1. Everything the spec references must already be sound.
	for wh in (spec["product_warehouse"], spec["raw_material_warehouse"], spec["packaging_warehouse"]):
		_check_warehouse(wh, company)
	gram = frappe.db.get_value("UOM", "Gram", ["enabled", "must_be_whole_number"], as_dict=True)
	if not gram or not gram.enabled or gram.must_be_whole_number:
		_conflict(_("La UOM Gram debe existir, estar activa y permitir decimales."))
	if not frappe.db.get_value("Item Group", spec["raw_material_group"], "is_group") == 0:
		_conflict(_("El grupo {0} debe existir y no ser un grupo padre.").format(spec["raw_material_group"]))
	_check_existing_item(spec["product"], spec["product_uom"])
	for row in spec["packaging"]:
		_check_existing_item(row["item_code"], row["uom"])
	existing_raw = {row["item_code"]: _check_raw_material(row, spec["raw_material_group"]) for row in spec["raw_materials"]}
	superseded = spec.get("superseded_warehouses") or {}
	for row in spec["raw_materials"]:
		if existing_raw[row["item_code"]]:
			_check_item_default(
				row["item_code"], company, spec["raw_material_warehouse"], superseded.get("raw_material_warehouse")
			)
	for row in spec["packaging"]:
		_check_item_default(row["item_code"], company, spec["packaging_warehouse"], superseded.get("packaging_warehouse"))
	_check_item_default(spec["product"], company, spec["product_warehouse"], superseded.get("product_warehouse"))
	bom = _matching_bom(spec, company)
	legacy_spec = _superseded_spec(spec)
	legacy_bom = _matching_bom(legacy_spec, company) if legacy_spec else None
	in_use = _bom_in_use(legacy_bom.name) if legacy_bom else []
	if in_use:
		_conflict(
			_("La BOM {0} (bodegas anteriores) está en uso por {1}; no se retira automáticamente.").format(
				legacy_bom.name, ", ".join(in_use)
			)
		)
	other_default = frappe.db.get_value(
		"BOM", {"item": spec["product"], "is_default": 1, "is_active": 1, "docstatus": 1}, "name"
	)
	if other_default and other_default not in {b.name for b in (bom, legacy_bom) if b}:
		_conflict(_("{0} ya tiene otra BOM activa por defecto ({1}).").format(spec["product"], other_default))

	# 2. Raw materials (Gram, stock, not for sale, no price/cost/stock).
	for row in spec["raw_materials"]:
		if existing_raw[row["item_code"]]:
			actions.append(("raw_material", row["item_code"], "ok"))
			continue
		if dry_run:
			actions.append(("raw_material", row["item_code"], "would create"))
			continue
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": row["item_code"],
				"item_name": row["item_name"],
				"description": row["item_name"],
				"item_group": spec["raw_material_group"],
				"stock_uom": row["uom"],
				"is_stock_item": 1,
				"is_sales_item": 0,
				"is_purchase_item": 1,
				"include_item_in_manufacturing": 1,
				"default_material_request_type": "Purchase",
				"opening_stock": 0,
				"item_defaults": [{"company": company, "default_warehouse": spec["raw_material_warehouse"]}],
			}
		)
		item.insert()
		actions.append(("raw_material", row["item_code"], "created"))

	# 3. Item Defaults (one row per company, never a second one).
	targets = [(r["item_code"], spec["raw_material_warehouse"]) for r in spec["raw_materials"]]
	targets += [(r["item_code"], spec["packaging_warehouse"]) for r in spec["packaging"]]
	targets.append((spec["product"], spec["product_warehouse"]))
	for code, warehouse in targets:
		if not frappe.db.exists("Item", code):  # dry run: raw material not created yet
			actions.append(("item_default", code, "would create"))
			continue
		actions.append(("item_default", code, _ensure_item_default(code, company, warehouse, dry_run)))

	# 4. The product is made, not bought (native field only).
	if frappe.db.get_value("Item", spec["product"], "default_material_request_type") == "Manufacture":
		actions.append(("product_policy", spec["product"], "ok"))
	elif dry_run:
		actions.append(("product_policy", spec["product"], "would update"))
	else:
		item = frappe.get_doc("Item", spec["product"])
		item.default_material_request_type = "Manufacture"
		item.save()
		actions.append(("product_policy", spec["product"], "updated"))

	# 5. The BOM: submitted, active, default (native manage_default_bom()
	#    sets Item.default_bom on submit).
	if bom:
		actions.append(("bom", bom.name, "ok"))
	elif dry_run:
		actions.append(("bom", spec["product"], "would create"))
	else:
		bom = _create_bom(spec, company)
		actions.append(("bom", bom.name, "created"))

	# 6. The superseded BOM (old warehouses hardcoded in its submitted
	#    components): native cancel -- inactive, not default, kept as
	#    history -- AFTER the new default BOM exists (native
	#    manage_default_bom() then leaves Item.default_bom on the new one).
	if legacy_bom:
		if dry_run:
			actions.append(("legacy_bom", legacy_bom.name, "would cancel"))
		else:
			frappe.get_doc("BOM", legacy_bom.name).cancel()
			actions.append(("legacy_bom", legacy_bom.name, "cancelled"))

	return {
		"company": company,
		"dry_run": dry_run,
		"actions": actions,
		"bom": bom.name if bom else None,
	}


def _ensure_item_default(item_code, company, warehouse, dry_run):
	item = frappe.get_doc("Item", item_code)
	row = next((d for d in item.item_defaults if d.company == company), None)
	if row and row.default_warehouse == warehouse:
		return "ok"
	if dry_run:
		return "would update" if row else "would create"
	if row:  # a row without warehouse or with the superseded one (conflicts were rejected before)
		row.default_warehouse = warehouse
	else:
		item.append("item_defaults", {"company": company, "default_warehouse": warehouse})
	item.save()
	return "updated" if row else "created"


def _create_bom(spec, company):
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": spec["product"],
			"company": company,
			"quantity": spec["bom_quantity"],
			"currency": frappe.get_cached_value("Company", company, "default_currency"),
			"conversion_rate": 1,
			"rm_cost_as_per": "Valuation Rate",
			"with_operations": 0,
			"is_active": 1,
			"is_default": 1,
			"items": [
				{"item_code": code, "qty": qty, "uom": uom, "conversion_factor": 1, "source_warehouse": warehouse}
				for code, qty, uom, warehouse in _components(spec)
			],
		}
	)
	bom.insert()
	bom.submit()
	return bom
