# -*- coding: utf-8 -*-
"""fabergray_erp/warehouses.py -- Fase 28.4A.3: infraestructura multi-almacén.

Two things live here, and only here:

1. OPERATIONAL_WAREHOUSES -- the operational warehouses of Fabrigray
   (one per product line, plus Materia Prima and Envases), created by
   ensure_operational_warehouses(): a System Manager helper run explicitly
   once per site (never a patch, never on migrate):

       bench --site <site> execute fabergray_erp.warehouses.ensure_operational_warehouses \
           --kwargs "{'dry_run': True}"      # plan only, writes nothing
       bench --site <site> execute fabergray_erp.warehouses.ensure_operational_warehouses

   Idempotent (a second run reports every warehouse "ok") and
   conflict-safe: an existing warehouse with that name but another
   company, another parent, a group or disabled stops the run with
   WarehouseMasterConflict BEFORE anything is written. Existing warehouses
   (Producto Terminado, Materias Primas, Material de Empaque, Producción
   WIP, Devoluciones, Cuarentena) are never renamed, disabled or touched.

2. non_picking_warehouses() -- the warehouses whose stock is NEVER a
   source for normal picking: Devoluciones and Cuarentena of the company,
   plus any warehouse flagged with the native Warehouse.is_rejected_
   warehouse (ERPNext's own "rejected" concept, which the native Pick List
   already excludes unless consider_rejected_warehouses is set). Today no
   warehouse carries that flag, so the two names are what actually applies;
   flagging Devoluciones/Cuarentena natively later keeps this rule intact.
"""

import frappe
from frappe import _

from erpnext import get_default_company

#: warehouse_name of each operational warehouse; the native name is
#: "<warehouse_name> - <Company.abbr>" (e.g. "Líquidos - FG").
OPERATIONAL_WAREHOUSES = (
	"Líquidos",
	"Varios",
	"Cafetería",
	"Jardinería",
	"Piscina",
	"Materia Prima",
	"Envases",
)

#: Never a picking source (physical stock that is not sellable).
NON_PICKING_WAREHOUSES = ("Devoluciones", "Cuarentena")


ROOT_WAREHOUSE = "Todos los almacenes"


class WarehouseMasterConflict(frappe.ValidationError):
	pass


def warehouse_name(base, company):
	"""Native Warehouse name of `base` in `company` ("<base> - <abbr>")."""
	return f"{base} - {frappe.get_cached_value('Company', company, 'abbr')}"


def non_picking_warehouses(company):
	"""Set of warehouse names of `company` that are never a picking source."""
	names = {warehouse_name(base, company) for base in NON_PICKING_WAREHOUSES}
	names.update(
		frappe.get_all("Warehouse", filters={"company": company, "is_rejected_warehouse": 1}, pluck="name")
	)
	return names


def ensure_operational_warehouses(company=None, dry_run=False):
	"""Create (or verify) OPERATIONAL_WAREHOUSES under the company's root
	warehouse. Returns {"company", "dry_run", "actions": [(warehouse,
	"ok" | "created" | "would create")]}."""
	frappe.only_for("System Manager")
	company = company or get_default_company()
	dry_run = bool(dry_run) and dry_run not in ("0", "false", "False")

	root = warehouse_name(ROOT_WAREHOUSE, company)
	root_row = frappe.db.get_value("Warehouse", root, ["company", "is_group"], as_dict=True)
	if not root_row or root_row.company != company or not root_row.is_group:
		frappe.throw(_("La bodega raíz {0} no existe o no es un grupo de {1}.").format(root, company), WarehouseMasterConflict)

	# 1. Every conflict is detected before any write.
	plan = []
	for base in OPERATIONAL_WAREHOUSES:
		name = warehouse_name(base, company)
		row = frappe.db.get_value(
			"Warehouse", name, ["company", "parent_warehouse", "is_group", "disabled"], as_dict=True
		)
		if row and (row.company != company or row.parent_warehouse != root or row.is_group or row.disabled):
			frappe.throw(
				_("La bodega {0} ya existe con otra configuración: {1}").format(name, dict(row)),
				WarehouseMasterConflict,
			)
		plan.append((base, name, bool(row)))

	# 2. Create what is missing.
	actions = []
	for base, name, exists in plan:
		if exists:
			actions.append((name, "ok"))
		elif dry_run:
			actions.append((name, "would create"))
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": base,
					"company": company,
					"parent_warehouse": root,
					"is_group": 0,
				}
			)
			doc.insert()
			actions.append((doc.name, "created"))

	return {"company": company, "dry_run": dry_run, "actions": actions}
