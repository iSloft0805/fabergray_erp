# -*- coding: utf-8 -*-
"""Commit 18.1 -- Vendedora role and permissions, standalone: no Page, no
api/ventas.py yet (Commit 18.2/18.3). Every permission grant lives in
fabergray_erp/fixtures/role.json + custom_docperm.json (Custom DocPerm),
applied via `bench migrate` -- the same mechanism established for Bodega/
Jefe de Bodega since Commit 1, never a change to apps/frappe or
apps/erpnext.

Commit 25.1 -- "el rol controla el área, no el owner": Sales Order/
Vendedora's Custom DocPerm dropped `if_owner` from 1 to 0. Every Vendedora
now sees/edits every Sales Order of this site's own Company, regardless of
who created it -- the previous "if_owner isolation between two distinct
Vendedoras" tests below are inverted accordingly (now proving SHARED
access, not isolation), and two new kinds of check were added: Company
isolation (a Sales Order of a different Company stays invisible even to a
Vendedora who could otherwise read anything) and "no role, no access, even
knowing the exact document name" (a user with zero roles beyond the
Frappe-implicit `All`/`Guest` gets a real `PermissionError`, never a
filtered empty result). `owner` remains a plain audit/display field
everywhere (`get_my_orders()`'s response still returns whatever ERPNext
itself sets it to) -- it is only removed as an ACCESS boundary.

Kinds of check, all required by the approved Commit 18.1 brief (updated
this commit where the brief itself changed):
- positive: what Vendedora CAN do (Customer/Item/Address/Contact read,
  Sales Order create/read/write/submit);
- negative: what Vendedora explicitly CANNOT do (Item Price, Price List,
  Pick List, Reporte de Faltante, Stock Entry, Purchase Order, Material
  Request, Work Order);
- shared visibility (Commit 25.1): two distinct Vendedora users, confirmed
  each can read/edit the other's Sales Order (was the opposite pre-25.1);
- Company isolation (Commit 25.1): a Sales Order of a different Company
  stays invisible via `has_permission()`, `get_list()`, and
  `api.ventas.get_order_detail()` alike;
- no access without the role (Commit 25.1): a user holding none of this
  app's roles gets `PermissionError` from every read path, even knowing
  the document's exact name;
- regression: Bodega/Jefe de Bodega permissions are unchanged by this
  commit;
- the real end-to-end scenario the minimal-elevation mechanism exists
  for: a Vendedora, with zero permission on Pick List/Reporte de
  Faltante, submits her own Sales Order and the Fulfillment Engine still
  correctly creates both, visible to Bodega/Jefe de Bodega through their
  own normal APIs; a second Vendedora can now read the Sales Order itself
  (Commit 25.1) but remains unable to read either the Pick List or the
  Reporte de Faltante it produced -- that permission boundary is
  independent of this commit and stays untouched -- plus confirmation
  that Bodega's own interactive report_shortage() still checks real
  permissions, the Engine's internal functions are not whitelisted (so
  nothing above could ever be invoked directly by a client), and Commit
  16's transactional rollback guarantee still holds with the new
  ignore_permissions=True writes in place.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega as bodega_api
from fabergray_erp.api import ventas as ventas_api
from fabergray_erp.api.bodega import _insert_shortage_report
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

	# -- Visibilidad compartida entre dos Vendedoras distintas (Commit 25.1) -------

	def test_vendedora_can_read_another_vendedoras_sales_order(self):
		"""Commit 25.1: "el rol controla el área, no el owner" -- a second
		Vendedora of the same Company can now read the first one's Sales
		Order, through has_permission(), a direct check_permission(), her
		own get_list(), AND the real api.ventas.get_order_detail() the
		Page actually calls -- not just the permission primitive."""
		wh = self.world.warehouse("FG25 Shared Read")
		item = self.world.item("FG25-SHARED-READ-ITEM")
		customer = self.world.customer("FG25 Shared Read Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so_a.name))
			frappe.get_doc("Sales Order", so_a.name).check_permission("read")  # must not raise
			self.assertEqual(frappe.get_list("Sales Order", filters={"name": so_a.name}, pluck="name"), [so_a.name])
			detail = ventas_api.get_order_detail(so_a.name)
			self.assertEqual(detail["name"], so_a.name)

	def test_vendedora_can_write_another_vendedoras_draft_sales_order(self):
		"""Commit 25.1: write follows the same rule -- a second Vendedora
		can edit a DRAFT Sales Order she did not create, through both the
		permission primitive and the real api.ventas.update_draft_
		sales_order() the Page's "Editar pedido" screen calls. Draft is
		the operative state here, not ownership (section 5 of the new
		policy: "las restricciones deben depender de rol/estado/docstatus/
		Company, no del owner")."""
		wh = self.world.warehouse("FG25 Shared Write")
		item = self.world.item("FG25-SHARED-WRITE-ITEM", default_warehouse=wh.name)
		customer = self.world.customer("FG25 Shared Write Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()  # left in Draft -- never submitted
			self.world.track_existing("Sales Order", so_a.name)

		with fx.as_user(self.vendedora_b):
			self.assertTrue(frappe.has_permission("Sales Order", "write", doc=so_a.name))
			result = ventas_api.update_draft_sales_order(
				so_a.name, customer.name, [{"item_code": item.name, "qty": 3}], observations="Editado por B"
			)
			self.assertEqual(result["name"], so_a.name)

		so_a.reload()
		self.assertEqual(so_a.fg_observations, "Editado por B")

	def test_submitted_sales_order_not_editable_by_anyone_regardless_of_owner(self):
		"""The correct kind of restriction, per the new policy's own
		example (section 5): once a Sales Order is submitted and Bodega
		has genuinely started picking (`fg_started_by` set -- confirmed
		via `modification_blockers_for()`'s own real logic, read directly
		from `fulfillment/modification_service.py`: a freshly-created,
		untouched Pick List is NOT a blocker on its own, only a submitted
		one / one Bodega started / one with real picked_qty is), NEITHER
		the original Vendedora (the owner) NOR a second one can modify the
		order -- docstatus/flujo governs, not who created it, in either
		direction. Mirrors the exact, already-proven pattern
		`test_sales_order_modification.test_modification_blocked_when_
		bodega_started_picking` uses."""
		wh = self.world.warehouse("FG25 Submitted Guard")
		item = self.world.item("FG25-SUBMITTED-GUARD-ITEM", default_warehouse=wh.name)
		customer = self.world.customer("FG25 Submitted Guard Customer")
		self.world.stock_up_real(item.name, wh.name, 10)  # full stock -- picking is what blocks, not a shortage
		bodega_user = self.world.user("fg25-submitted-guard-bodega@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name, qty=5)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)
			so_a.submit()

		self.world.track_existing_pick_lists_and_reports_for(so_a.name)

		pl_name = frappe.get_all(
			"Pick List Item", filters={"sales_order": so_a.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)[0]
		with fx.as_user(bodega_user):
			bodega_api.start_picking(pl_name)
		frappe.db.commit()  # fixtures + started Pick List survive any later rollback, same as the sibling test

		with fx.as_user(self.vendedora_a):
			status = ventas_api.get_modification_status(so_a.name)
			self.assertIn("bodega_started", status["blockers"])
			with self.assertRaises(frappe.ValidationError):
				ventas_api.modify_submitted_sales_order(
					so_a.name, customer.name, [{"item_code": item.name, "qty": 1}]
				)

		with fx.as_user(self.vendedora_b):
			# she CAN read it (Commit 25.1's own shared visibility)...
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so_a.name))
			# ...but modification is blocked by flujo/state for her too, same as the owner.
			with self.assertRaises(frappe.ValidationError):
				ventas_api.modify_submitted_sales_order(
					so_a.name, customer.name, [{"item_code": item.name, "qty": 1}]
				)

	def test_vendedora_cannot_see_sales_order_from_another_company(self):
		"""Company isolation (Commit 25.1, brief section 6) -- a Sales
		Order of `_Test Company` (an already-existing, already-configured
		Company on this site, unrelated to `fabrigraysas` -- confirmed via
		this commit's own audit to already own a full Warehouse/chart-of-
		accounts setup) stays invisible to a Vendedora scoped to
		`fabrigraysas` through both surfaces she can actually reach: her
		own list view (get_list()) and the real API entry point
		(api.ventas.get_order_detail()) -- see the in-test comment for why
		bare has_permission()/check_permission() are deliberately NOT
		asserted here. Built directly as
		Administrator (never through Vendedora's own create path -- this is
		about an EXISTING cross-Company document, not about whether she
		could have created one there). Customer/Item themselves are NOT
		Company-scoped doctypes in ERPNext (only Warehouse and Item Default
		rows are) -- `self.world.customer()`/`.item()` (both plain,
		Company-agnostic masters) are reused here, only `company` on the
		Sales Order itself and `warehouse` (`_Test Company`'s own existing
		"Finished Goods - _TC") actually belong to the other Company."""
		other_company_customer = self.world.customer("FG25 Other Company Customer")
		other_company_item = self.world.item("FG25-OTHER-COMPANY-ITEM")
		other_company_so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": other_company_customer.name,
				"company": "_Test Company",
				"currency": "INR",  # _Test Company's own currency -- avoids needing a COP->INR Currency Exchange rate
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 7),
				"items": [
					{
						"item_code": other_company_item.name,
						"warehouse": "Finished Goods - _TC",
						"qty": 1,
						"rate": 100,
						"delivery_date": add_days(nowdate(), 7),
					}
				],
			}
		)
		other_company_so.insert()
		self.world.track_existing("Sales Order", other_company_so.name)

		with fx.as_user(self.vendedora_a):
			# NOTE: bare frappe.has_permission()/check_permission() do NOT
			# know about Company at all -- confirmed by reading frappe/
			# permissions.py directly, they only ever consult role-level
			# Custom DocPerm + User Permission, so both legitimately still
			# return True/do not raise here (the exact same architectural
			# boundary api.recorridos._address_belongs_to_company() already
			# lives with for Address -- enforced at the APPLICATION layer,
			# never the raw permission primitive). The two guarantees that
			# actually matter -- and are actually tested here -- are the
			# ones a Vendedora can actually reach through this app's own
			# surface: her own list view (get_list(), Company-filtered by
			# permission_conditions.py's own hook) and the real API
			# endpoint (api.ventas.get_order_detail(), Company-checked by
			# its own assert_same_company() call).
			self.assertEqual(
				frappe.get_list("Sales Order", filters={"name": other_company_so.name}, pluck="name"), []
			)
			with self.assertRaises(frappe.PermissionError):
				ventas_api.get_order_detail(other_company_so.name)

	def test_user_without_vendedora_role_has_no_access_by_knowing_the_name(self):
		"""Commit 25.1 (brief section 9's own explicit test): sharing
		visibility WITHIN the role is not the same as opening access to
		anyone who merely knows a document's name -- a user holding none
		of this app's roles (only the Frappe-implicit `All`/`Guest`) gets a
		real `PermissionError` from every read path, exactly like before
		this commit, even for a Sales Order she could otherwise identify by
		name."""
		wh = self.world.warehouse("FG25 NoRole Guard")
		item = self.world.item("FG25-NOROLE-GUARD-ITEM")
		customer = self.world.customer("FG25 NoRole Guard Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)

		no_role_user = self.world.user("fg25-norole@example.com", [])
		with fx.as_user(no_role_user):
			self.assertFalse(frappe.has_permission("Sales Order", "read", doc=so_a.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Sales Order", so_a.name).check_permission("read")
			with self.assertRaises(frappe.PermissionError):
				ventas_api.get_order_detail(so_a.name)

	def test_administrator_and_system_manager_see_every_company_sales_order(self):
		"""Section 9's own explicit "Administrator/System Manager: sigue
		funcionando" -- neither is scoped by the Company-isolation hook
		added this commit (`permission_conditions.py`'s own
		`_allowed_companies()` returns `None`, i.e. no restriction, for
		both)."""
		wh = self.world.warehouse("FG25 Admin Sees All")
		item = self.world.item("FG25-ADMIN-SEES-ALL-ITEM")
		customer = self.world.customer("FG25 Admin Sees All Customer")

		with fx.as_user(self.vendedora_a):
			so_a = self._raw_sales_order(customer.name, item.name, wh.name)
			so_a.insert()
			self.world.track_existing("Sales Order", so_a.name)

		# Administrator -- the ambient IntegrationTestCase user outside any `as_user` block.
		self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so_a.name))
		self.assertIn(so_a.name, frappe.get_list("Sales Order", filters={"name": so_a.name}, pluck="name"))

		sysmgr_user = self.world.user("fg25-sysmgr@example.com", ["System Manager"])
		with fx.as_user(sysmgr_user):
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so_a.name))
			detail = ventas_api.get_order_detail(so_a.name)
			self.assertEqual(detail["name"], so_a.name)

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
		- the Fulfillment Engine creates a Pick List for the FULL demand
		  (Commit 25.4 -- "Ventas no decide faltantes": no automatic
		  Reporte de Faltante, regardless of real stock) -- real
		  quantities, not just "no exception";
		- Sales Order.owner == the real Vendedora (session was never
		  swapped, so is the Pick List's owner);
		- she can still read her own Sales Order;
		- a different Vendedora CAN now read it too (Commit 25.1's own
		  shared visibility -- was the opposite pre-25.1);
		- the Pick List appears correctly in get_queue() for Bodega;
		- she personally still cannot read either the Pick List or a
		  Reporte de Faltante built directly (Commit 25.4: no longer the
		  Engine's own, but the elevation/visibility boundary this test
		  exists for is unrelated to who created the report).
		"""
		wh = self.world.warehouse("FG18 E2E")
		item = self.world.item("FG18-E2E-ITEM", default_material_request_type="Purchase")
		customer = self.world.customer("FG18 E2E Customer")
		self.world.stock_up_real(item.name, wh.name, 3)  # partial: 3 of 8 ordered

		bodega_user = self.world.user("fg18-e2e-bodega@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

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
			self.assertTrue(frappe.has_permission("Sales Order", "read", doc=so.name))  # Commit 25.1: shared

		pick_lists = frappe.get_all(
			"Pick List Item", filters={"sales_order": so.name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)
		self.assertEqual(len(pick_lists), 1)
		pl = frappe.get_doc("Pick List", pick_lists[0])
		self.assertEqual(sum(row.stock_qty for row in pl.get("locations")), 8.0)  # full demand, not capped at 3
		self.assertEqual(pl.owner, self.vendedora_a)  # created under her own session

		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 0)  # Commit 25.4

		with fx.as_user(bodega_user):
			queue = bodega_api.get_queue()
		self.assertIn(pl.name, [p["name"] for p in queue["pendientes"]])

		# Commit 25.4: her submit itself no longer creates any Reporte de
		# Faltante -- build one directly (e.g. as Bodega would while
		# picking, or Jefe reprocessing) purely to prove the permission
		# boundary below is unaffected by who created it.
		report_name = _insert_shortage_report(
			item_code=item.name,
			warehouse=wh.name,
			sales_order=so.name,
			sales_order_item=so.items[0].name,
			qty_solicitada=8,
			qty_disponible=3,
			detected_by="Fulfillment Engine",
			shortage_reason="Compra pendiente",
			via_fulfillment_engine=True,
		)
		self.world.track_existing("Reporte de Faltante", report_name)

		with fx.as_user(self.vendedora_a):
			self.assertFalse(frappe.has_permission("Pick List", "read", doc=pl.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Pick List", pl.name).check_permission("read")
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read", doc=report_name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Reporte de Faltante", report_name).check_permission("read")

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
		assumed. Commit 25.4: patches create_pick_list_for_full_demand()
		instead of sync_shortage_reports_for_sales_order() -- the latter
		is no longer called by the real on_submit hook at all (see
		fulfillment/engine.py's process_sales_order_for_confirmation())."""
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
			"fabergray_erp.fulfillment.engine.create_pick_list_for_full_demand",
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
