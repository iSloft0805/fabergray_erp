# -*- coding: utf-8 -*-
"""Commit 18.1 -- Vendedora role and permissions, standalone: no Page, no
api/ventas.py yet (Commit 18.2/18.3). Every permission grant lives in
fabergray_erp/fixtures/role.json + custom_docperm.json (Custom DocPerm),
applied via `bench migrate` -- the same mechanism established for Bodega/
Jefe de Bodega since Commit 1, never a change to apps/frappe or
apps/erpnext.

Six kinds of check, all required by the approved Commit 18.1 brief:
- positive: what Vendedora CAN do (Customer/Item/Address/Contact read,
  Sales Order create/read/write/submit);
- negative: what Vendedora explicitly CANNOT do (Item Price, Price List,
  Pick List, Reporte de Faltante, Stock Entry, Purchase Order, Material
  Request, Work Order);
- if_owner isolation: two distinct Vendedora users, confirmed neither can
  read the other's Sales Order;
- regression: Bodega/Jefe de Bodega permissions are unchanged by this
  commit;
- the real end-to-end scenario the minimal-elevation mechanism exists
  for: a Vendedora, with zero permission on Pick List/Reporte de
  Faltante, submits her own Sales Order and the Fulfillment Engine still
  correctly creates both, visible to Bodega/Jefe de Bodega through their
  own normal APIs, while she personally remains unable to read either --
  plus confirmation that Bodega's own interactive report_shortage() still
  checks real permissions, the Engine's internal functions are not
  whitelisted (so nothing above could ever be invoked directly by a
  client), and Commit 16's transactional rollback guarantee still holds
  with the new ignore_permissions=True writes in place.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega as bodega_api
from fabergray_erp.api import jefe_bodega as jefe_bodega_api
from fabergray_erp.fulfillment import cancellation_service, pick_list_service, shortage_service
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_DENIED_DOCTYPES = (
	"Item Price",
	"Price List",
	"Pick List",
	"Reporte de Faltante",
	"Stock Entry",
	"Purchase Order",
	"Material Request",
	"Work Order",
)


class TestVendedoraPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.vendedora_a = cls.world.user("fg18-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg18-vendedora-b@example.com", ["Vendedora"])

	def _raw_sales_order(self, customer, item, warehouse, qty=1):
		delivery_date = add_days(nowdate(), 7)
		return frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": warehouse,
				"items": [
					{
						"item_code": item,
						"warehouse": warehouse,
						"qty": qty,
						"rate": 100,
						"delivery_date": delivery_date,
					}
				],
			}
		)

	# -- Positivos: lo que Vendedora SÍ puede hacer -------------------------------

	def test_vendedora_can_read_customer(self):
		customer = self.world.customer("FG18 Perm Customer")
		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Customer", "read", doc=customer.name))
			frappe.get_doc("Customer", customer.name)  # must not raise

	def test_vendedora_can_read_item(self):
		item = self.world.item("FG18-PERM-ITEM")
		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Item", "read", doc=item.name))
			frappe.get_doc("Item", item.name)

	def test_vendedora_can_read_address_and_contact(self):
		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Address", "read"))
			self.assertTrue(frappe.has_permission("Contact", "read"))

	def test_vendedora_can_create_read_write_submit_her_own_sales_order(self):
		wh = self.world.warehouse("FG18 Perm")
		item = self.world.item("FG18-PERM-SO-ITEM")
		customer = self.world.customer("FG18 Perm SO Customer")

		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Sales Order", "create"))
			so = self._raw_sales_order(customer.name, item.name, wh.name)
			so.insert()
			self.world.track_existing("Sales Order", so.name)

			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so.name))
			self.assertTrue(frappe.has_permission("Sales Order", "write", doc=so.name))
			so.customer_po_no = "PO-TEST"  # any writable field, proves write=1 works end to end
			so.save()

			self.assertTrue(frappe.has_permission("Sales Order", "submit", doc=so.name))
			so.submit()  # Commit 16's hook fires -- same as any other submit in this suite

		self.world.track_existing_pick_lists_and_reports_for(so.name)

	# -- Negativos: lo que Vendedora NUNCA puede hacer ----------------------------

	def test_vendedora_cannot_read_item_price(self):
		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Item Price", "read"))

	def test_vendedora_cannot_read_price_list(self):
		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Price List", "read"))

	def test_vendedora_cannot_read_pick_list(self):
		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Pick List", "read"))

	def test_vendedora_cannot_read_reporte_de_faltante(self):
		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read"))

	def test_vendedora_has_no_access_to_stock_purchase_manufacturing_doctypes(self):
		with fx.as_user(self.vendedora_a):
			for doctype in _DENIED_DOCTYPES:
				self.assertFalse(frappe.has_permission(doctype, "read"), f"{doctype}: read should be denied")
				self.assertFalse(frappe.has_permission(doctype, "write"), f"{doctype}: write should be denied")
				self.assertFalse(frappe.has_permission(doctype, "create"), f"{doctype}: create should be denied")

	# -- Aislamiento if_owner entre dos Vendedoras distintas -----------------------

	def test_vendedora_cannot_read_another_vendedoras_sales_order(self):
		wh = self.world.warehouse("FG18 Isolation")
		item = self.world.item("FG18-ISOLATION-ITEM")
		customer = self.world.customer("FG18 Isolation Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Sales Order", "read", doc=so_a.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Sales Order", so_a.name).check_permission("read")
			# and her own list view must not surface it either
			self.assertEqual(frappe.get_list("Sales Order", filters={"name": so_a.name}, pluck="name"), [])

	def test_vendedora_cannot_write_another_vendedoras_sales_order(self):
		wh = self.world.warehouse("FG18 IsolationWrite")
		item = self.world.item("FG18-ISOLATIONWRITE-ITEM")
		customer = self.world.customer("FG18 IsolationWrite Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Sales Order", "write", doc=so_a.name))

	# -- Regresión: Bodega/Jefe de Bodega mantienen exactamente sus permisos -----

	def test_bodega_and_jefe_de_bodega_permissions_are_unchanged(self):
		bodega_user = self.world.user("fg18-bodega-regress@example.com", ["Bodega"])
		jefe_user = self.world.user("fg18-jefe-regress@example.com", ["Jefe de Bodega"])

		with fx.as_user(bodega_user):
			self.assertTrue(frappe.has_permission("Pick List", "read"))
			self.assertTrue(frappe.has_permission("Pick List", "write"))
			self.assertFalse(frappe.has_permission("Pick List", "create"))
			self.assertTrue(frappe.has_permission("Material Request", "read"))
			self.assertFalse(frappe.has_permission("Material Request", "write"))
			self.assertTrue(frappe.has_permission("Bin", "read"))
			self.assertTrue(frappe.has_permission("Sales Order", "read"))
			self.assertFalse(frappe.has_permission("Sales Order", "write"))
			self.assertFalse(frappe.has_permission("Warehouse", "read"))  # only Jefe de Bodega has this
			self.assertTrue(frappe.has_permission("Reporte de Faltante", "create"))

		with fx.as_user(jefe_user):
			self.assertTrue(frappe.has_permission("Pick List", "read"))
			self.assertFalse(frappe.has_permission("Pick List", "write"))
			self.assertTrue(frappe.has_permission("Warehouse", "read"))
			self.assertFalse(frappe.has_permission("Warehouse", "write"))
			self.assertTrue(frappe.has_permission("Reporte de Faltante", "write"))
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "create"))

	# -- E2E real: Vendedora somete, el Engine crea, ella sigue sin acceso -------

	def test_vendedora_can_submit_and_engine_creates_correct_artifacts_without_granting_her_access(self):
		"""The real scenario the Commit 18.1 minimal-elevation mechanism
		exists for. Covers, in one flow (the checks are inherently
		sequential on the same Sales Order, so splitting them into
		separate test methods would only duplicate setup):

		- Vendedora submits her own Sales Order (create+submit succeed
		  under her own, permission-restricted session -- no PermissionError,
		  no frappe.set_user anywhere);
		- the Fulfillment Engine creates a Pick List (for the available
		  portion) and a Reporte de Faltante (for the shortfall) correctly
		  -- real quantities, not just "no exception";
		- Sales Order.owner == the real Vendedora (session was never
		  swapped, so is the Pick List's/report's owner);
		- she can still read her own Sales Order;
		- a different Vendedora (if_owner=1) cannot read it;
		- the Pick List appears correctly in get_queue() for Bodega;
		- the Reporte de Faltante appears correctly for Jefe de Bodega via
		  get_open_shortage_reports();
		- she personally still cannot read either the Pick List or the
		  Reporte de Faltante the Engine created on her own order's behalf.
		"""
		wh = self.world.warehouse("FG18 E2E")
		item = self.world.item("FG18-E2E-ITEM", default_material_request_type="Purchase")
		customer = self.world.customer("FG18 E2E Customer")
		self.world.stock_up_real(item.name, wh.name, 3)  # partial: 3 of 8 ordered

		bodega_user = self.world.user("fg18-e2e-bodega@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)
		jefe_user = self.world.user("fg18-e2e-jefe@example.com", ["Jefe de Bodega"])

		with fx.as_user(self.vendedora_a):
			so = self._raw_sales_order(customer.name, item.name, wh.name, qty=8)
			so.insert()
			self.world.track_existing("Sales Order", so.name)
			so.submit()  # real hook, real Engine, her own restricted session throughout

		self.world.track_existing_pick_lists_and_reports_for(so.name)

		so.reload()
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.owner, self.vendedora_a)  # frappe.session.user was never touched

		with fx.as_user(self.vendedora_a):
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so.name))
			frappe.get_doc("Sales Order", so.name)  # must not raise

		with fx.as_user(self.vendedora_b):
			self.assertFalse(frappe.has_permission("Sales Order", "read", doc=so.name))

		pick_lists = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(pl.get("locations")[0].stock_qty, 3.0)
		self.assertEqual(pl.owner, self.vendedora_a)  # created under her own session

		reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": so.name}, pluck="name")
		self.assertEqual(len(reports), 1)
		report = frappe.get_doc("Reporte de Faltante", reports[0])
		self.assertEqual(report.qty_faltante, 5.0)
		self.assertEqual(report.detected_by, "Fulfillment Engine")
		self.assertEqual(report.shortage_reason, "Compra pendiente")
		self.assertEqual(report.owner, self.vendedora_a)

		with fx.as_user(bodega_user):
			queue = bodega_api.get_queue()
		self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

		with fx.as_user(jefe_user):
			open_reports = jefe_bodega_api.get_open_shortage_reports()
		self.assertIn(report.name, [r["name"] for r in open_reports])

		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Pick List", "read", doc=pl.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Pick List", pl.name).check_permission("read")
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read", doc=report.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Reporte de Faltante", report.name).check_permission("read")

	# -- La frontera compartida sigue exigiendo permisos reales por defecto ------

	def test_shared_insert_function_still_requires_real_permission_by_default(self):
		"""_insert_shortage_report() without via_fulfillment_engine=True
		(the default -- the only way _create_shortage_report()/Bodega's
		interactive report_shortage() ever calls it) must still enforce
		the real create-permission check exactly as before Commit 18.1,
		proving the elevation is confined to the Engine's own explicit
		opt-in and does not leak into the interactive path."""
		from fabergray_erp.api.bodega import _insert_shortage_report

		wh = self.world.warehouse("FG18 BodegaCheck")
		item = self.world.item("FG18-BODEGACHECK-ITEM")

		with fx.as_user(self.vendedora_a):  # confirmed zero Reporte de Faltante permission
			with self.assertRaises(frappe.PermissionError):
				_insert_shortage_report(
					item_code=item.name,
					warehouse=wh.name,
					qty_solicitada=5,
					qty_disponible=0,
					detected_by="Fulfillment Engine",
				)  # via_fulfillment_engine defaults to False

	# -- Las funciones internas del Engine no son whitelisted ---------------------

	def test_engine_internal_functions_are_not_whitelisted(self):
		"""Guardrail: nothing that can run with ignore_permissions=True (or
		via_fulfillment_engine=True) is reachable directly by a client.
		`@frappe.whitelist()` does not tag the function itself -- it adds
		it to the module-level `frappe.whitelisted` set, checked by
		`frappe.is_whitelisted()` on every `/api/method/...` dispatch --
		so membership in that exact set is the real, authoritative check,
		not an attribute guess."""
		from fabergray_erp.api.bodega import _insert_shortage_report

		for fn in (
			pick_list_service.create_pick_list_for_available_stock,
			pick_list_service._create_pick_list_ignoring_permissions,
			cancellation_service.cleanup_fulfillment_for_cancelled_sales_order,
			shortage_service.sync_shortage_reports_for_sales_order,
			_insert_shortage_report,
		):
			self.assertNotIn(
				fn,
				frappe.whitelisted,
				f"{fn.__qualname__} must never be @frappe.whitelist()-ed",
			)

	# -- Atomicidad del Commit 16 sigue funcionando con ignore_permissions=True --

	def test_rollback_still_works_with_ignore_permissions_writes(self):
		"""Re-exercises Commit 16's own rollback guarantee (see
		test_sales_order_hook.py) specifically to prove the Commit 18.1
		elevation didn't quietly change transaction semantics --
		ignore_permissions only skips a permission check, it has nothing
		to do with commit/rollback, but this is proven here rather than
		assumed."""
		from unittest.mock import patch

		wh = self.world.warehouse("FG18 Rollback")
		item = self.world.item("FG18-ROLLBACK-ITEM")
		customer = self.world.customer("FG18 Rollback Customer")
		self.world.stock_up_real(item.name, wh.name, 5)

		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 7),
				"set_warehouse": wh.name,
				"items": [
					{
						"item_code": item.name,
						"warehouse": wh.name,
						"qty": 5,
						"rate": 100,
						"delivery_date": add_days(nowdate(), 7),
					}
				],
			}
		)
		so.insert()
		self.world.track_existing("Sales Order", so.name)
		frappe.db.commit()  # fixtures + draft SO survive the rollback below

		with patch(
			"fabergray_erp.fulfillment.engine.sync_shortage_reports_for_sales_order",
			side_effect=RuntimeError("Commit 18.1 intentional failure"),
		):
			with self.assertRaises(RuntimeError):
				so.submit()

		frappe.db.rollback()

		so.reload()
		self.assertEqual(so.docstatus, 0)
		self.assertEqual(
			frappe.db.sql("""select count(*) from `tabPick List Item` where sales_order=%s""", so.name)[0][0], 0
		)
		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 0)

		# confirm it can still complete cleanly afterward
		so.submit()
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		self.assertEqual(
			frappe.db.sql("""select count(*) from `tabPick List Item` where sales_order=%s""", so.name)[0][0], 1
		)
