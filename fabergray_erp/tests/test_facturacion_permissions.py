# -*- coding: utf-8 -*-
"""Commit 21.1 -- permission tests for the new Facturación role.

Scope of Commit 21.1 (per the approved brief): create the Facturación role
and grant EXACTLY 6 Custom DocPerm rows, no if_owner (Facturación is a shared
queue, not a per-owner one like Vendedora's Sales Order/Quotation):

- Sales Invoice: create/read/write/submit/cancel
- Pick List: read
- Sales Order: read
- Customer: read
- Item: read
- Account: select

**Amended in Commit 21.2** with read-only access to `Reporte de Faltante` --
discovered live while building api/facturacion.py:
`get_facturacion_summary()`'s `con_incidencia` bucket and
`get_pending_pick_lists()`'s `has_open_shortage` indicator both require
reading it, which 21.1's exact-6 grant did not include. Presented to the
user as an explicit choice before adding it.

**Critical discovery made while adding it, fixed within this same commit --
not a Custom DocPerm row:** `Reporte de Faltante` is a doctype fabergray_erp
itself owns (Commit 2), so unlike every other Facturación/Vendedora/Bodega
grant so far (all on core Frappe/ERPNext doctypes), its permissions live
natively in its own `reporte_de_faltante.json` `permissions` array. Adding a
Custom DocPerm row for it instead (the first attempt) triggered a real,
confirmed-live Frappe mechanism (`frappe.permissions.get_valid_perms()`,
`get_doctypes_with_custom_docperms()`): once ANY Custom DocPerm row exists
for a given doctype, ALL of that doctype's own native DocPerm rows are
ignored for EVERY role, not just the one being granted -- this silently
wiped out Bodega's and Jefe de Bodega's own pre-existing, load-bearing
Reporte de Faltante permissions (System Manager's too), confirmed by a real
regression in test_bodega_permissions.py before the fix. Corrected by
reverting the Custom DocPerm row entirely and instead adding one native
permission row (`{"read": 1, "print": 1, "report": 1, "role":
"Facturación"}`, no write/create/delete) directly to
`reporte_de_faltante.json`, alongside System Manager/Bodega/Jefe de
Bodega's existing rows -- the correct, idiomatic mechanism for an own-app
doctype (see [[fabrigray-permission-pattern]]: Custom DocPerm is for
doctypes this app does NOT own). `fixtures/custom_docperm.json` therefore
still holds exactly the same 6 Facturación rows as Commit 21.1 -- unchanged.

No api/facturacion.py Page, no other permission grant beyond these 6 Custom
DocPerm rows + 1 native DocPerm row. This file proves all of it works and
that nothing beyond it was accidentally opened up, especially the 5
doctypes the 21.1 brief explicitly says NOT to grant unless a real test
fails (Item Price, Cost Center, Sales Taxes and Charges Template, Item Tax
Template, Payment Terms Template) -- none did, so none are granted.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

# Per the brief: never grant these unless a real test fails specifically
# because of one -- none did in this commit.
_STILL_DENIED_DOCTYPES = (
	"Item Price",
	"Cost Center",
	"Sales Taxes and Charges Template",
	"Item Tax Template",
	"Payment Terms Template",
)

# Doctypes Facturación can read but never write/create/submit/cancel/delete.
_READ_ONLY_DOCTYPES = ("Pick List", "Sales Order", "Customer", "Item", "Reporte de Faltante")


class TestFacturacionRoleExists(IntegrationTestCase):
	def test_role_exists(self):
		self.assertTrue(frappe.db.exists("Role", "Facturación"))

	def test_exactly_six_custom_docperm_rows_for_facturacion(self):
		"""Unchanged from Commit 21.1 -- Reporte de Faltante access (Commit
		21.2) is a native DocPerm row on the doctype's own JSON, never a
		Custom DocPerm row (see this module's docstring for why)."""
		rows = frappe.get_all(
			"Custom DocPerm", filters={"role": "Facturación"}, fields=["parent", "if_owner"]
		)
		self.assertEqual(len(rows), 6, rows)
		for row in rows:
			self.assertEqual(row.if_owner, 0, f"{row.parent} must not be if_owner -- shared queue")
		parents = {row.parent for row in rows}
		self.assertEqual(
			parents, {"Sales Invoice", "Pick List", "Sales Order", "Customer", "Item", "Account"}
		)

	def test_reporte_de_faltante_grant_is_native_docperm_not_custom_docperm(self):
		"""The specific regression this commit found and fixed: Reporte de
		Faltante must have ZERO Custom DocPerm rows at all -- one existing
		for any role/doctype combination would mask every native DocPerm
		row on that doctype for every role (Bodega/Jefe de Bodega/System
		Manager included), per frappe.permissions.get_valid_perms()."""
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Reporte de Faltante"}))
		native = frappe.get_all(
			"DocPerm", filters={"parent": "Reporte de Faltante", "role": "Facturación"}, fields=["read", "write"]
		)
		self.assertEqual(len(native), 1)
		self.assertEqual(native[0].read, 1)
		self.assertEqual(native[0].write, 0)


class TestFacturacionPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG21 Facturación Perm")
		cls.item = cls.world.item("FG21-PERM-ITEM")
		cls.customer = cls.world.customer("FG21 Test Customer Perm")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 5, cls.customer.name)
		cls.pl = cls.world.pick_list_for(so, cls.wh.name)
		cls.so = so
		cls.report = cls.world.shortage_report(
			item_code=cls.item.name,
			warehouse=cls.wh.name,
			pick_list=cls.pl.name,
			qty_solicitada=5,
			qty_disponible=3,
			detected_by="Bodega",
			shortage_reason="Stock insuficiente",
		)

		cls.facturacion_a = cls.world.user("fg21-facturacion-a@example.com", ["Facturación"])
		cls.facturacion_b = cls.world.user("fg21-facturacion-b@example.com", ["Facturación"])

	# -- Positive: the 7 granted permissions ------------------------------

	def test_can_read_pick_list(self):
		with fx.as_user(self.facturacion_a):
			frappe.get_doc("Pick List", self.pl.name).check_permission("read")

	def test_can_write_and_submit_pick_list_for_invoicing_status(self):
		"""Commit 23.0: Facturación's Custom DocPerm on Pick List gained
		write=1 AND submit=1 (was read-only) -- the one permission change
		that commit made, and explicitly not an accounting one (this role
		still has zero Account permission, see
		test_has_select_on_account_not_read below). submit=1 is required
		alongside write=1 because Frappe's own
		Document.check_docstatus_transition() calls check_permission
		("submit") for ANY save on an already-submitted document, even one
		that only touches allow_on_submit fields -- exactly what
		api.facturacion.mark_as_invoiced() does. See that module's own top
		docstring for the full architectural explanation."""
		with fx.as_user(self.facturacion_a):
			self.assertTrue(frappe.has_permission("Pick List", "write"))
			self.assertTrue(frappe.has_permission("Pick List", "submit"))

	def test_can_read_sales_order(self):
		with fx.as_user(self.facturacion_a):
			frappe.get_doc("Sales Order", self.so.name).check_permission("read")

	def test_can_read_customer(self):
		with fx.as_user(self.facturacion_a):
			frappe.get_doc("Customer", self.customer.name).check_permission("read")

	def test_can_read_item(self):
		with fx.as_user(self.facturacion_a):
			frappe.get_doc("Item", self.item.name).check_permission("read")

	def test_can_read_reporte_de_faltante(self):
		with fx.as_user(self.facturacion_a):
			frappe.get_doc("Reporte de Faltante", self.report.name).check_permission("read")

	def test_has_select_on_account_not_read(self):
		with fx.as_user(self.facturacion_a):
			self.assertTrue(frappe.has_permission("Account", "select"))
			self.assertFalse(frappe.has_permission("Account", "read"))

	def test_can_create_read_write_submit_cancel_sales_invoice(self):
		with fx.as_user(self.facturacion_a):
			self.assertTrue(frappe.has_permission("Sales Invoice", "create"))
			self.assertTrue(frappe.has_permission("Sales Invoice", "read"))
			self.assertTrue(frappe.has_permission("Sales Invoice", "write"))
			self.assertTrue(frappe.has_permission("Sales Invoice", "submit"))
			self.assertTrue(frappe.has_permission("Sales Invoice", "cancel"))

	def test_sales_invoice_is_not_if_owner_scoped(self):
		"""Facturación is a shared queue -- one Facturación user must be able
		to read/write a Sales Invoice created by ANOTHER Facturación user,
		unlike Vendedora's if_owner=1 Sales Order/Quotation."""
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"items": [
					{"item_code": self.item.name, "qty": 1, "rate": 100, "warehouse": self.wh.name}
				],
			}
		)
		with fx.as_user(self.facturacion_a):
			si.insert()
		self.world.track_existing("Sales Invoice", si.name)

		with fx.as_user(self.facturacion_b):
			doc = frappe.get_doc("Sales Invoice", si.name)
			doc.check_permission("read")
			doc.check_permission("write")

	# -- Negative: read-only doctypes stay read-only -----------------------
	# (Pick List moved out of this section in Commit 23.0 -- see
	# test_can_write_and_submit_pick_list_for_invoicing_status above.)

	def test_cannot_write_sales_order(self):
		with fx.as_user(self.facturacion_a):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Sales Order", self.so.name).check_permission("write")

	def test_cannot_write_customer(self):
		with fx.as_user(self.facturacion_a):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Customer", self.customer.name).check_permission("write")

	def test_cannot_write_item(self):
		with fx.as_user(self.facturacion_a):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Item", self.item.name).check_permission("write")

	def test_cannot_write_reporte_de_faltante(self):
		with fx.as_user(self.facturacion_a):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Reporte de Faltante", self.report.name).check_permission("write")

	def test_cannot_delete_sales_invoice(self):
		with fx.as_user(self.facturacion_a):
			self.assertFalse(frappe.has_permission("Sales Invoice", "delete"))

	# -- Negative: explicitly withheld doctypes -----------------------------

	def test_denied_doctypes_stay_denied(self):
		with fx.as_user(self.facturacion_a):
			for doctype in _STILL_DENIED_DOCTYPES:
				with self.subTest(doctype=doctype):
					self.assertFalse(frappe.has_permission(doctype, "read"))

	# -- Regression: other roles unaffected ---------------------------------

	def test_bodega_jefe_vendedora_permissions_unchanged(self):
		# Spot checks, not a full re-run of every other role's suite --
		# those have their own dedicated test files.
		self.assertTrue(frappe.db.exists("Custom DocPerm", {"parent": "Pick List", "role": "Bodega", "write": 1}))
		# Commit 25.1 -- "el rol controla el área, no el owner" flipped
		# Sales Order/Vendedora's if_owner from 1 to 0; still the same one
		# Custom DocPerm row (create/read/write/submit/cancel/delete), just
		# no longer owner-scoped -- see test_ventas_permissions.py's own
		# suite for the full before/after story.
		self.assertTrue(
			frappe.db.exists("Custom DocPerm", {"parent": "Sales Order", "role": "Vendedora", "if_owner": 0})
		)
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Sales Invoice", "role": "Vendedora"}))
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Sales Invoice", "role": "Bodega"}))

		# The specific regression this commit found and fixed: Bodega/Jefe
		# de Bodega's own, pre-existing (native, Commit 2) Reporte de
		# Faltante permissions must still be intact.
		bodega_perm = frappe.get_all(
			"DocPerm", filters={"parent": "Reporte de Faltante", "role": "Bodega"}, fields=["read", "write", "create"]
		)
		self.assertEqual(len(bodega_perm), 1)
		self.assertEqual((bodega_perm[0].read, bodega_perm[0].write, bodega_perm[0].create), (1, 1, 1))

		jefe_perm = frappe.get_all(
			"DocPerm", filters={"parent": "Reporte de Faltante", "role": "Jefe de Bodega"}, fields=["read", "write"]
		)
		self.assertEqual(len(jefe_perm), 1)
		self.assertEqual((jefe_perm[0].read, jefe_perm[0].write), (1, 1))

		with fx.as_user(self.world.user("fg21-bodega-regression@example.com", ["Bodega"])):
			frappe.has_permission("Reporte de Faltante", "read", throw=True)
			frappe.has_permission("Reporte de Faltante", "write", throw=True)
