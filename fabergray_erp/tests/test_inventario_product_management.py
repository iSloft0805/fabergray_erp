# -*- coding: utf-8 -*-
"""Commit 25.21 -- api/inventario.py's 4 new product-management endpoints:
create_inventory_item/deactivate_inventory_item/reactivate_inventory_item/
delete_inventory_item. Uses the native Item doctype end to end -- no
parallel catalog anywhere.

Same fixture conventions as test_inventario_api.py (fx.TestWorld, no
ERPNext test-utils, no ignore_permissions except TestWorld's own teardown)
-- deliberately a SEPARATE file rather than appended to
test_inventario_api.py, so this commit's own tests stay isolated from the
Commit 22.4/22.6 suite's own run_id-suffixed fixtures.

Letters A-T (section 24 of the brief) map onto the classes below:
- TestCreateInventoryItem: A, B, C, D, E, F, G
- TestDeactivateReactivateInventoryItem: H, I, J
- TestDeleteInventoryItem: K, L, M, N, O, P, Q
- TestInactiveFilterListing: R, S
- T (permisos intactos) is exercised throughout -- every write endpoint
  has its own Bodega-denied test, not just create_inventory_item's.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega, inventario as api, jefe_bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestCreateInventoryItem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		seed_item = cls.world.item("FG2521-SEED-ITEM")
		cls.item_group = seed_item.item_group
		cls.stock_uom = seed_item.stock_uom

		cls.jefe_user = cls.world.user("fg2521-jefe@example.com", ["Jefe de Bodega"])
		cls.sysmanager_user = cls.world.user("fg2521-sysmanager@example.com", ["System Manager"])
		cls.bodega_user = cls.world.user("fg2521-bodega@example.com", ["Bodega"])

	def _create(self, item_code, **kwargs):
		result = api.create_inventory_item(item_code=item_code, **kwargs)
		self.world.track_existing("Item", result["item_code"])
		return result

	# A. Jefe de Bodega puede crear Item.
	def test_a_jefe_de_bodega_can_create_item(self):
		with fx.as_user(self.jefe_user):
			result = self._create(
				"FG2521-CREATE-A",
				item_name="Desengrasante 1 Galón",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(result["item_code"], "FG2521-CREATE-A")
		self.assertTrue(frappe.db.exists("Item", "FG2521-CREATE-A"))

	def test_a_system_manager_can_create_item(self):
		with fx.as_user(self.sysmanager_user):
			result = self._create(
				"FG2521-CREATE-A2",
				item_name="Desinfectante 1 Litro",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(result["item_code"], "FG2521-CREATE-A2")

	# B. Bodega normal no puede crear.
	def test_b_bodega_cannot_create_item(self):
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				api.create_inventory_item(
					item_code="FG2521-CREATE-B",
					item_name="No debería crearse",
					item_group=self.item_group,
					stock_uom=self.stock_uom,
				)
		self.assertFalse(frappe.db.exists("Item", "FG2521-CREATE-B"))

	# C. duplicate item_code rechazado.
	def test_c_duplicate_item_code_rejected(self):
		with fx.as_user(self.jefe_user):
			self._create(
				"FG2521-CREATE-C",
				item_name="Original",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
			with self.assertRaises(api.DuplicateItemCodeError):
				api.create_inventory_item(
					item_code="FG2521-CREATE-C",
					item_name="Duplicado",
					item_group=self.item_group,
					stock_uom=self.stock_uom,
				)

	# D. Item Group inválido rechazado.
	def test_d_invalid_item_group_rejected(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.InvalidItemGroupError):
				api.create_inventory_item(
					item_code="FG2521-CREATE-D",
					item_name="Sin grupo válido",
					item_group="FG2521 Grupo Que No Existe",
					stock_uom=self.stock_uom,
				)
		self.assertFalse(frappe.db.exists("Item", "FG2521-CREATE-D"))

	# E. UOM inválida rechazada.
	def test_e_invalid_uom_rejected(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.InvalidUOMError):
				api.create_inventory_item(
					item_code="FG2521-CREATE-E",
					item_name="Sin UOM válida",
					item_group=self.item_group,
					stock_uom="FG2521 UOM Que No Existe",
				)
		self.assertFalse(frappe.db.exists("Item", "FG2521-CREATE-E"))

	def test_empty_item_code_rejected(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.ValidationError):
				api.create_inventory_item(
					item_code="   ",
					item_name="Sin código",
					item_group=self.item_group,
					stock_uom=self.stock_uom,
				)

	def test_empty_item_name_rejected(self):
		with fx.as_user(self.jefe_user):
			with self.assertRaises(frappe.ValidationError):
				api.create_inventory_item(
					item_code="FG2521-CREATE-NONAME",
					item_name="   ",
					item_group=self.item_group,
					stock_uom=self.stock_uom,
				)
		self.assertFalse(frappe.db.exists("Item", "FG2521-CREATE-NONAME"))

	# F. nuevo Item queda activo.
	def test_f_new_item_is_active_by_default(self):
		with fx.as_user(self.jefe_user):
			self._create(
				"FG2521-CREATE-F",
				item_name="Debe quedar activo",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(frappe.db.get_value("Item", "FG2521-CREATE-F", "disabled"), 0)

	# G. nuevo Item stock item por defecto.
	def test_g_new_item_is_stock_item_by_default(self):
		with fx.as_user(self.jefe_user):
			result = self._create(
				"FG2521-CREATE-G",
				item_name="Producto de inventario por defecto",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(result["is_stock_item"], 1)

	def test_g_is_stock_item_can_be_explicitly_disabled(self):
		with fx.as_user(self.jefe_user):
			result = self._create(
				"FG2521-CREATE-G2",
				item_name="No maneja inventario",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
				is_stock_item=0,
			)
		self.assertEqual(result["is_stock_item"], 0)

	# Section 21 -- crear producto no crea stock inicial ni toca Bin.
	def test_create_never_creates_stock_or_bin_rows(self):
		with fx.as_user(self.jefe_user):
			self._create(
				"FG2521-CREATE-NOSTOCK",
				item_name="Sin stock inicial",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(
			frappe.get_all("Bin", filters={"item_code": "FG2521-CREATE-NOSTOCK"}, pluck="name"),
			[],
		)

	# Section 20 -- crear producto no crea Item Price.
	def test_create_never_creates_item_price(self):
		with fx.as_user(self.jefe_user):
			self._create(
				"FG2521-CREATE-NOPRICE",
				item_name="Sin precio inicial",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.assertEqual(
			frappe.get_all("Item Price", filters={"item_code": "FG2521-CREATE-NOPRICE"}, pluck="name"),
			[],
		)


class TestDeactivateReactivateInventoryItem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.jefe_user = cls.world.user("fg2521-jefe-toggle@example.com", ["Jefe de Bodega"])
		cls.bodega_user = cls.world.user("fg2521-bodega-toggle@example.com", ["Bodega"])

	# H. Jefe Bodega puede desactivar. I. desactivar pone disabled=1.
	def test_h_i_jefe_de_bodega_can_deactivate_and_it_sets_disabled_1(self):
		item = self.world.item("FG2521-TOGGLE-HI")
		self.assertEqual(item.disabled, 0)
		with fx.as_user(self.jefe_user):
			result = api.deactivate_inventory_item(item.name)
		self.assertEqual(result["disabled"], 1)
		self.assertEqual(frappe.db.get_value("Item", item.name, "disabled"), 1)

	# J. reactivar pone disabled=0.
	def test_j_reactivate_sets_disabled_0(self):
		item = self.world.item("FG2521-TOGGLE-J")
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
			result = api.reactivate_inventory_item(item.name)
		self.assertEqual(result["disabled"], 0)
		self.assertEqual(frappe.db.get_value("Item", item.name, "disabled"), 0)

	def test_deactivate_is_idempotent(self):
		item = self.world.item("FG2521-TOGGLE-IDEMP")
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
			result = api.deactivate_inventory_item(item.name)
		self.assertEqual(result["disabled"], 1)

	# T. permisos intactos -- Bodega no puede desactivar ni reactivar.
	def test_t_bodega_cannot_deactivate(self):
		item = self.world.item("FG2521-TOGGLE-BODEGA-DEACT")
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				api.deactivate_inventory_item(item.name)
		self.assertEqual(frappe.db.get_value("Item", item.name, "disabled"), 0)

	def test_t_bodega_cannot_reactivate(self):
		item = self.world.item("FG2521-TOGGLE-BODEGA-REACT")
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				api.reactivate_inventory_item(item.name)
		self.assertEqual(frappe.db.get_value("Item", item.name, "disabled"), 1)

	def test_toggle_never_touches_stock(self):
		wh = self.world.warehouse("FG2521 Toggle Wh")
		item = self.world.item("FG2521-TOGGLE-STOCK")
		self.world.stock_up(item.name, wh.name, 42)
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh.name}, "actual_qty"), 42)


class TestDeleteInventoryItem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2521 Delete Wh")
		cls.customer = cls.world.customer("FG2521 Delete Customer")

		cls.jefe_user = cls.world.user("fg2521-jefe-delete@example.com", ["Jefe de Bodega"])
		cls.bodega_user = cls.world.user("fg2521-bodega-delete@example.com", ["Bodega"])

	def _raw_quotation(self, item_code, submit=True):
		qtn = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": self.customer.name,
				"company": fx.COMPANY,
				"items": [{"item_code": item_code, "qty": 1}],
			}
		)
		qtn.insert()
		self.world.track_existing("Quotation", qtn.name)
		if submit:
			qtn.submit()
		return qtn

	# K. Item sin uso puede eliminarse.
	def test_k_item_without_any_usage_can_be_deleted(self):
		item = self.world.item("FG2521-DELETE-K")
		with fx.as_user(self.jefe_user):
			result = api.delete_inventory_item(item.name)
		self.assertTrue(result["deleted"])
		self.assertFalse(frappe.db.exists("Item", item.name))

	# L. Item con Sales Order no puede eliminarse. No stock_up() here on
	# purpose -- Sales Order submission never requires Bin stock (this
	# app's own test_ventas_source_never_checks_actual_qty_or_similar_
	# before_submit already establishes that), so this isolates the
	# LinkExistsError path cleanly from the separate stock check (P below).
	def test_l_item_with_sales_order_cannot_be_deleted(self):
		item = self.world.item("FG2521-DELETE-L")
		so = self.world.submitted_sales_order(item.name, self.wh.name, 5, self.customer.name)
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasDependenciesError):
				api.delete_inventory_item(item.name)
		# Q -- the Sales Order itself must survive untouched, never cascaded.
		self.assertTrue(frappe.db.exists("Sales Order", so.name))
		self.assertTrue(frappe.db.exists("Item", item.name))

	# M. Item con Quotation no puede eliminarse.
	def test_m_item_with_quotation_cannot_be_deleted(self):
		item = self.world.item("FG2521-DELETE-M")
		qtn = self._raw_quotation(item.name)
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasDependenciesError):
				api.delete_inventory_item(item.name)
		self.assertTrue(frappe.db.exists("Quotation", qtn.name))
		self.assertTrue(frappe.db.exists("Item", item.name))

	# N. Item con Pick List no puede eliminarse. Stock is needed only so
	# create_pick_list() actually produces a location row (a Pick List
	# with zero available stock at creation time gets zero rows --
	# confirmed by direct testing of create_pick_list()'s own
	# set_item_locations() behaviour), then reset back to 0 right after,
	# so the deletion attempt below isolates the Pick List Item link from
	# the separate stock check (P below) -- otherwise this test would
	# just be re-proving P, not N.
	def test_n_item_with_pick_list_cannot_be_deleted(self):
		item = self.world.item("FG2521-DELETE-N")
		self.world.stock_up(item.name, self.wh.name, 1000)
		so = self.world.submitted_sales_order(item.name, self.wh.name, 5, self.customer.name)
		pl = self.world.pick_list_for(so, self.wh.name)
		self.world.stock_up(item.name, self.wh.name, 0)
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasDependenciesError):
				api.delete_inventory_item(item.name)
		self.assertTrue(frappe.db.exists("Pick List", pl.name))
		self.assertTrue(frappe.db.exists("Item", item.name))

	# O. Item con Stock Entry no puede eliminarse.
	def _stock_entry(self, entry_type, item_code, qty, account):
		"""Stock Entry nativo, sometido (Material Receipt / Material Issue)
		-- nunca Bin/SLE/actual_qty manipulados directamente."""
		warehouse_field = "t_warehouse" if entry_type == "Material Receipt" else "s_warehouse"
		doc = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": entry_type,
				"company": fx.COMPANY,
				"items": [
					{
						"item_code": item_code,
						warehouse_field: self.wh.name,
						"qty": qty,
						"basic_rate": 1000,
						"expense_account": account,
						"cost_center": fx.COST_CENTER,
					}
				],
			}
		)
		doc.insert()
		doc.submit()
		self.world.track_existing("Stock Entry", doc.name)
		return doc

	# O. CASO B -- stock actual 0 pero con movimientos históricos reales:
	# entrada y salida por Stock Entry normal; el historial permanece.
	def test_o_item_with_zero_stock_but_history_cannot_be_deleted(self):
		item = self.world.item("FG2521-DELETE-O")
		account = self.world.stock_difference_account().name
		receipt = self._stock_entry("Material Receipt", item.name, 3, account)
		issue = self._stock_entry("Material Issue", item.name, 3, account)

		self.assertEqual(frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": self.wh.name}, "actual_qty"), 0)
		history = frappe.db.count("Stock Ledger Entry", {"item_code": item.name, "is_cancelled": 0})
		self.assertEqual(history, 2)

		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasDependenciesError) as ctx:
				api.delete_inventory_item(item.name)
		self.assertIn("Puedes desactivarlo", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Item", item.name))
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": item.name, "is_cancelled": 0}), history)
		self.assertEqual(frappe.db.get_value("Stock Entry", receipt.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Stock Entry", issue.name, "docstatus"), 1)

	# CASO A con movimiento real: stock actual > 0 por Stock Entry -> se
	# rechaza por existencias ANTES de mirar dependencias.
	def test_o2_item_with_stock_from_stock_entry_raises_stock_error(self):
		item = self.world.item("FG2521-DELETE-O2")
		account = self.world.stock_difference_account().name
		self._stock_entry("Material Receipt", item.name, 3, account)
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasStockError):
				api.delete_inventory_item(item.name)
		self.assertTrue(frappe.db.exists("Item", item.name))

	# P. Item con stock != 0 no puede eliminarse -- via Bin directly (Stock
	# Reconciliation), no Sales Order/Pick List/other link involved at all,
	# so this specifically exercises the explicit, MORE SPECIFIC stock
	# check (ItemHasStockError), not the generic LinkExistsError fallback.
	def test_p_item_with_nonzero_stock_cannot_be_deleted(self):
		item = self.world.item("FG2521-DELETE-P")
		self.world.stock_up(item.name, self.wh.name, 10)
		with fx.as_user(self.jefe_user):
			with self.assertRaises(api.ItemHasStockError):
				api.delete_inventory_item(item.name)
		self.assertTrue(frappe.db.exists("Item", item.name))

	# T. permisos intactos -- Bodega no puede eliminar.
	def test_t_bodega_cannot_delete(self):
		item = self.world.item("FG2521-DELETE-BODEGA")
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				api.delete_inventory_item(item.name)
		self.assertTrue(frappe.db.exists("Item", item.name))

	def test_delete_never_uses_raw_sql_or_ignore_permissions(self):
		"""Static confirmation, not just behavioural: the source itself
		never calls frappe.db.delete/frappe.db.sql or passes
		ignore_permissions=True anywhere in delete_inventory_item()."""
		import inspect

		source = inspect.getsource(api.delete_inventory_item)
		self.assertNotIn("frappe.db.delete", source)
		self.assertNotIn("frappe.db.sql", source)
		self.assertNotIn("ignore_permissions=True", source)
		self.assertNotIn("ignore_permissions = True", source)
		self.assertNotIn("force", source.split('"""')[-1])  # nunca delete_doc(..., force=...)
		self.assertNotIn("ignore_links", source)


class TestInactiveFilterListing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.jefe_user = cls.world.user("fg2521-jefe-filter@example.com", ["Jefe de Bodega"])

	# R. inactivos aparecen en filtro INACTIVOS.
	def test_r_disabled_item_appears_in_disabled_filter(self):
		item = self.world.item("FG2521-FILTER-R")
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
			result = api.get_inventory_items(txt=item.name, status="disabled")
		self.assertIn(item.name, [r["item_code"] for r in result["items"]])

	# S. activos no aparecen en INACTIVOS.
	def test_s_active_item_does_not_appear_in_disabled_filter(self):
		item = self.world.item("FG2521-FILTER-S")
		with fx.as_user(self.jefe_user):
			result = api.get_inventory_items(txt=item.name, status="disabled")
		self.assertNotIn(item.name, [r["item_code"] for r in result["items"]])

	def test_disabled_item_does_not_appear_in_active_filter(self):
		item = self.world.item("FG2521-FILTER-ACTIVEEXCL")
		with fx.as_user(self.jefe_user):
			api.deactivate_inventory_item(item.name)
			result = api.get_inventory_items(txt=item.name, status="active")
		self.assertNotIn(item.name, [r["item_code"] for r in result["items"]])

	def test_active_item_appears_in_active_filter(self):
		item = self.world.item("FG2521-FILTER-ACTIVEINCL")
		with fx.as_user(self.jefe_user):
			result = api.get_inventory_items(txt=item.name, status="active")
		self.assertIn(item.name, [r["item_code"] for r in result["items"]])


class TestItemCreationOptions(IntegrationTestCase):
	"""get_item_creation_options(): catálogo controlado de Item Group/UOM
	para "+ NUEVO PRODUCTO", sin Custom DocPerm sobre esos DocTypes."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		sfx = frappe.generate_hash(length=5)
		seed_item = cls.world.item(f"FG2521-OPT-{sfx}")
		cls.item_group = seed_item.item_group
		cls.stock_uom = seed_item.stock_uom
		slugs = {
			"Jefe de Bodega": "jefe",
			"System Manager": "sm",
			"Bodega": "bodega",
			"Vendedora": "vendedora",
			"Facturación": "facturacion",
			"Recorrido": "recorrido",
		}
		cls.users = {role: cls.world.user(f"fg2521-opt-{sfx}-{slug}@example.com", [role]) for role, slug in slugs.items()}
		disabled_uom = frappe.get_doc({"doctype": "UOM", "uom_name": f"FG2521 Off {sfx}", "enabled": 0}).insert()
		cls.world.track_existing("UOM", disabled_uom.name)
		cls.disabled_uom = disabled_uom.name
		cls.parent_group = frappe.db.get_value("Item Group", {"is_group": 1}, "name")

	def test_managers_get_only_leaf_groups_and_enabled_uoms(self):
		for role in ("Jefe de Bodega", "System Manager"):
			with fx.as_user(self.users[role]):
				options = api.get_item_creation_options()
			self.assertEqual(set(options), {"item_groups", "uoms"})
			self.assertIn(self.item_group, options["item_groups"])
			self.assertIn(self.stock_uom, options["uoms"])
			self.assertNotIn(self.parent_group, options["item_groups"])
			self.assertNotIn(self.disabled_uom, options["uoms"])
			# orden de la BD (collation *_ci: sin mayúsculas ni tildes)
			self.assertEqual(options["item_groups"], sorted(options["item_groups"], key=_collation_key))
			self.assertEqual(options["uoms"], sorted(options["uoms"], key=_collation_key))
			self.assertTrue(all(isinstance(v, str) for v in options["item_groups"] + options["uoms"]))

	def test_other_roles_rejected(self):
		for role in ("Bodega", "Vendedora", "Facturación", "Recorrido"):
			with fx.as_user(self.users[role]):
				with self.assertRaises(frappe.PermissionError, msg=role):
					api.get_item_creation_options()

	def test_works_without_any_item_group_or_uom_permission(self):
		"""El endpoint no depende de permisos sobre Item Group/UOM: aunque
		frappe.has_permission los negara, las opciones y la creación siguen
		funcionando (la lectura está encapsulada en el endpoint)."""
		from unittest.mock import patch

		real = frappe.has_permission

		def deny_catalogs(doctype=None, *args, **kwargs):
			if doctype in ("Item Group", "UOM"):
				return False
			return real(doctype, *args, **kwargs)

		with patch.object(frappe, "has_permission", side_effect=deny_catalogs):
			with fx.as_user(self.users["Jefe de Bodega"]):
				options = api.get_item_creation_options()
				result = api.create_inventory_item(
					item_code=f"FG2521-OPT-NOPERM-{frappe.generate_hash(length=4)}",
					item_name="Sin permiso de catálogo",
					item_group=options["item_groups"][0],
					stock_uom=self.stock_uom,
				)
		self.world.track_existing("Item", result["item_code"])
		self.assertTrue(frappe.db.exists("Item", result["item_code"]))

	def test_create_rejects_parent_group_and_disabled_uom(self):
		with fx.as_user(self.users["Jefe de Bodega"]):
			with self.assertRaises(api.InvalidItemGroupError):
				api.create_inventory_item(
					item_code="FG2521-OPT-PARENT", item_name="x", item_group=self.parent_group, stock_uom=self.stock_uom
				)
			with self.assertRaises(api.InvalidUOMError):
				api.create_inventory_item(
					item_code="FG2521-OPT-OFFUOM", item_name="x", item_group=self.item_group, stock_uom=self.disabled_uom
				)
		self.assertFalse(frappe.db.exists("Item", "FG2521-OPT-PARENT"))
		self.assertFalse(frappe.db.exists("Item", "FG2521-OPT-OFFUOM"))

	def test_non_manager_roles_get_no_administrative_item_capability(self):
		item = self.world.item(f"FG2521-OPT-ADM-{frappe.generate_hash(length=4)}")
		for role in ("Bodega", "Vendedora", "Facturación", "Recorrido"):
			with fx.as_user(self.users[role]):
				with self.assertRaises(frappe.PermissionError, msg=role):
					api.create_inventory_item(
						item_code="FG2521-OPT-DENY", item_name="x", item_group=self.item_group, stock_uom=self.stock_uom
					)
				for fn in (api.deactivate_inventory_item, api.reactivate_inventory_item, api.delete_inventory_item):
					with self.assertRaises(frappe.PermissionError, msg=f"{role} {fn.__name__}"):
						fn(item.name)
		self.assertTrue(frappe.db.exists("Item", item.name))
		self.assertEqual(frappe.db.get_value("Item", item.name, "disabled"), 0)

	def test_create_never_creates_a_warehouse(self):
		before = frappe.db.count("Warehouse")
		with fx.as_user(self.users["Jefe de Bodega"]):
			result = api.create_inventory_item(
				item_code=f"FG2521-OPT-NOWH-{frappe.generate_hash(length=4)}",
				item_name="Sin almacén",
				item_group=self.item_group,
				stock_uom=self.stock_uom,
			)
		self.world.track_existing("Item", result["item_code"])
		self.assertEqual(frappe.db.count("Warehouse"), before)


class TestNativeCatalogPermissionsPreserved(IntegrationTestCase):
	"""Regresión: 25.21 NO introduce Custom DocPerm sobre Item Group/UOM --
	en Frappe, cualquier Custom DocPerm de un DocType hace que se ignore TODA
	su matriz nativa (frappe.permissions.get_valid_perms), así que un solo
	registro dejaría sin acceso a Sales User/Stock User/Item Manager/..."""

	_FIXTURES = ("custom_docperm.json", "system_manager_custom_docperm.json", "native_restore_custom_docperm.json")

	def test_no_fixture_adds_custom_docperm_for_item_group_or_uom(self):
		import json
		import os

		fixtures_dir = os.path.join(frappe.get_app_path("fabergray_erp"), "fixtures")
		for filename in self._FIXTURES:
			with open(os.path.join(fixtures_dir, filename), encoding="utf-8") as f:
				parents = {row["parent"] for row in json.load(f)}
			self.assertFalse(parents & {"Item Group", "UOM"}, filename)

	def test_hooks_never_export_item_group_or_uom_docperms(self):
		for entry in frappe.get_hooks("fixtures"):
			if isinstance(entry, dict) and entry.get("dt") == "Custom DocPerm":
				flat = json_dump(entry.get("filters"))
				self.assertNotIn("Item Group", flat)
				self.assertNotIn('"UOM"', flat)

	def test_native_item_group_and_uom_matrix_still_defined(self):
		expected = {
			"Item Group": {"Accounts User", "Item Manager", "Purchase User", "Sales User", "Stock Manager", "Stock User"},
			"UOM": {"Item Manager", "Sales Manager", "Sales User", "Stock Manager", "Stock User"},
		}
		for doctype, roles in expected.items():
			readers = set(frappe.get_all("DocPerm", filters={"parent": doctype, "read": 1}, pluck="role"))
			self.assertTrue(roles <= readers, (doctype, roles - readers))


def json_dump(value):
	import json

	return json.dumps(value, ensure_ascii=False)


def _collation_key(value):
	import unicodedata

	return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold()
