# -*- coding: utf-8 -*-
"""Commit 22.1 -- api/clientes.py: the three read-only endpoints behind the
future Page Clientes (Commit 22.3), plus this commit's own Role setup
("Gestión de Clientes": read/write/create, no delete, on Customer --
Vendedora's existing Customer/Address/Contact read permission is
untouched).

Four kinds of check, matching the approved Commit 22.1 brief:
- functional: dashboard counts, search (text/status/pagination), detail
  (including primary-contact resolution) are actually correct, not just
  "no exception" -- every assertion is a delta against a captured
  baseline, since Customer counts are global (not owner-scoped like Sales
  Order) and this site already has ~4091 real migrated Customers;
- positive/negative permissions: a real "Gestión de Clientes" session can
  call every endpoint; a real session with zero Customer permission
  (Bodega) is denied by all three, under its own restricted session --
  never frappe.set_user() inside api/clientes.py itself;
- regression: Vendedora/Facturación/Bodega/Jefe de Bodega's existing
  Customer permissions are byte-for-byte unchanged by this commit;
- structural guardrails: api/clientes.py exposes exactly these three
  whitelisted functions (nothing that writes), and its source contains no
  insert()/save()/submit()/delete()/ignore_permissions/frappe.set_user()/
  frappe.get_all()/frappe.db.sql -- the scope promises enforced as code,
  not just as a docstring claim.
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import clientes as clientes_api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_FORBIDDEN_SOURCE_SNIPPETS = (
    ".insert(",
    ".save(",
    ".submit(",
    ".delete(",
    "ignore_permissions",
    "frappe.set_user",
    "frappe.get_all(",
    "frappe.db.sql",
)


class TestClientesApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.gestion_user = cls.world.user("fg221-gestion-clientes@example.com", ["Gestión de Clientes"])

    def _contact_for(self, customer_name, first_name):
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": first_name,
                "links": [{"link_doctype": "Customer", "link_name": customer_name}],
            }
        )
        contact.insert()
        self.world.track_existing("Contact", contact.name)
        return contact

    # -- Funcional: dashboard -------------------------------------------------

    def test_dashboard_summary_reflects_real_data(self):
        before = clientes_api.get_dashboard_summary()

        complete = self.world.customer("FG221 Activo Completo")
        complete.tax_id = "900221001-1"
        complete.access_nombre_comercial = "ACTIVO COMPLETO"
        complete.save()

        inactive = self.world.customer("FG221 Inactivo Completo")
        inactive.tax_id = "900221002-2"
        inactive.access_nombre_comercial = "INACTIVO COMPLETO"
        inactive.disabled = 1
        inactive.save()

        # deliberately leaves tax_id and access_nombre_comercial unset
        self.world.customer("FG221 Incompleto")

        with fx.as_user(self.gestion_user):
            after = clientes_api.get_dashboard_summary()

        self.assertEqual(after["total"], before["total"] + 3)
        self.assertEqual(after["activos"], before["activos"] + 2)
        self.assertEqual(after["inactivos"], before["inactivos"] + 1)
        self.assertEqual(after["datos_incompletos"], before["datos_incompletos"] + 1)

    # -- Funcional: búsqueda / lista -------------------------------------------

    def test_search_by_name_and_tax_id(self):
        c = self.world.customer("FG221 Search Target")
        c.tax_id = "900221333-4"
        c.save()

        with fx.as_user(self.gestion_user):
            by_name = clientes_api.search_customers(txt="FG221 Search Target")
            by_tax_id = clientes_api.search_customers(txt="900221333-4")

        self.assertIn(c.name, [r["name"] for r in by_name["customers"]])
        self.assertIn(c.name, [r["name"] for r in by_tax_id["customers"]])

    def test_search_status_filter_and_pagination(self):
        a = self.world.customer("FG221 Status A")
        b = self.world.customer("FG221 Status B")
        b.disabled = 1
        b.save()

        with fx.as_user(self.gestion_user):
            active_only = clientes_api.search_customers(txt="FG221 Status", status="active")
            inactive_only = clientes_api.search_customers(txt="FG221 Status", status="inactive")
            page1 = clientes_api.search_customers(txt="FG221 Status", page_length=1, start=0)

        active_names = [r["name"] for r in active_only["customers"]]
        inactive_names = [r["name"] for r in inactive_only["customers"]]
        self.assertIn(a.name, active_names)
        self.assertNotIn(b.name, active_names)
        self.assertIn(b.name, inactive_names)
        self.assertNotIn(a.name, inactive_names)
        self.assertEqual(len(page1["customers"]), 1)
        self.assertEqual(page1["total"], 2)

    # -- Funcional: detalle -----------------------------------------------------

    def test_customer_detail_fields(self):
        c = self.world.customer("FG221 Detail Target")
        c.tax_id = "900221444-5"
        c.access_nombre_comercial = "DETALLE COMERCIAL"
        c.access_id_cliente = "9990221"
        c.save()

        with fx.as_user(self.gestion_user):
            detail = clientes_api.get_customer_detail(c.name)

        self.assertEqual(detail["customer_name"], "FG221 Detail Target")
        self.assertEqual(detail["tax_id"], "900221444-5")
        self.assertEqual(detail["access_nombre_comercial"], "DETALLE COMERCIAL")
        self.assertEqual(detail["access_id_cliente"], "9990221")

    def test_customer_detail_resolves_contact_when_caller_has_permission(self):
        """As Administrator (the default IntegrationTestCase session, real
        native Contact/Address permission) -- proves the resolution code
        path itself is correct, not just that it degrades gracefully."""
        c = self.world.customer("FG221 Detail WithContact")
        contact = self._contact_for(c.name, "Juana")
        c.customer_primary_contact = contact.name
        c.save()

        detail = clientes_api.get_customer_detail(c.name)

        self.assertIsNotNone(detail["contact"])
        self.assertEqual(detail["contact"]["name"], contact.name)
        self.assertEqual(detail["contact"]["first_name"], "Juana")
        self.assertIsNone(detail["address"])

    def test_customer_detail_omits_contact_when_caller_lacks_contact_permission(self):
        """A caller with Customer read permission but none on Contact must
        not get a PermissionError just because get_customer_detail() tried
        to resolve a primary contact -- it must come back null instead.

        Commit 22.7 gave "Gestión de Clientes" its own Contact/Address
        Custom DocPerm (read/write/create, so the Page's "Editar cliente"
        modal actually works for that role -- see fixtures/
        custom_docperm.json), so that role itself no longer demonstrates
        "Customer read without Contact read". This ad hoc role (Customer
        read only, no Custom DocPerm row of its own on Contact) is
        constructed fresh here to keep testing the same guarantee: Contact
        and Address already have at least one Custom DocPerm row
        (Vendedora's), which masks every native DocPerm for every OTHER
        role (frappe.permissions.get_valid_perms() -- same mechanism this
        app's own Commit 22.4 documents for Item Price/Stock Ledger
        Entry), so a role with no explicit row of its own gets exactly
        zero Contact permission, same as before this commit."""
        role = frappe.get_doc({"doctype": "Role", "role_name": "FG221 Cliente Sin Contacto", "desk_access": 1})
        role.insert()
        self.world.track_existing("Role", role.name)
        docperm = frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": "Customer",
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": role.name,
                "read": 1,
            }
        )
        docperm.insert()
        self.world.track_existing("Custom DocPerm", docperm.name)
        no_contact_user = self.world.user("fg221-sin-contacto@example.com", [role.name])

        c = self.world.customer("FG221 Detail NoContactPerm")
        contact = self._contact_for(c.name, "Marta")
        c.customer_primary_contact = contact.name
        c.save()

        with fx.as_user(no_contact_user):
            self.assertFalse(frappe.has_permission("Contact", "read"))
            detail = clientes_api.get_customer_detail(c.name)  # must not raise

        self.assertIsNone(detail["contact"])

    def test_customer_detail_without_primary_contact_returns_none(self):
        c = self.world.customer("FG221 No Contact Target")

        with fx.as_user(self.gestion_user):
            detail = clientes_api.get_customer_detail(c.name)

        self.assertIsNone(detail["contact"])
        self.assertIsNone(detail["address"])

    # -- Positivo: el nuevo rol puede leer --------------------------------------

    def test_gestion_de_clientes_role_has_read_create_write_no_delete(self):
        with fx.as_user(self.gestion_user):
            self.assertTrue(frappe.has_permission("Customer", "read"))
            self.assertTrue(frappe.has_permission("Customer", "write"))
            self.assertTrue(frappe.has_permission("Customer", "create"))
            self.assertFalse(frappe.has_permission("Customer", "delete"))

    # -- Negativo: sin permiso sobre Customer, las tres funciones se niegan -----

    def test_user_without_customer_permission_is_denied(self):
        c = self.world.customer("FG221 NoPerm Target")
        bodega_user = self.world.user("fg221-bodega-noperm@example.com", ["Bodega"])

        with fx.as_user(bodega_user):
            self.assertFalse(frappe.has_permission("Customer", "read"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.get_dashboard_summary()
            with self.assertRaises(frappe.PermissionError):
                clientes_api.search_customers()
            with self.assertRaises(frappe.PermissionError):
                clientes_api.get_customer_detail(c.name)

    # -- Regresión: los roles existentes no cambian ------------------------------

    def test_vendedora_and_facturacion_customer_permission_unchanged(self):
        vendedora = self.world.user("fg221-vendedora-regress@example.com", ["Vendedora"])
        facturacion = self.world.user("fg221-facturacion-regress@example.com", ["Facturación"])

        with fx.as_user(vendedora):
            self.assertTrue(frappe.has_permission("Customer", "read"))
            self.assertFalse(frappe.has_permission("Customer", "write"))
            self.assertFalse(frappe.has_permission("Customer", "create"))

        with fx.as_user(facturacion):
            self.assertTrue(frappe.has_permission("Customer", "read"))
            self.assertFalse(frappe.has_permission("Customer", "write"))
            self.assertFalse(frappe.has_permission("Customer", "create"))

    def test_bodega_and_jefe_de_bodega_still_have_no_customer_permission(self):
        bodega = self.world.user("fg221-bodega-regress2@example.com", ["Bodega"])
        jefe = self.world.user("fg221-jefe-regress2@example.com", ["Jefe de Bodega"])

        with fx.as_user(bodega):
            self.assertFalse(frappe.has_permission("Customer", "read"))
        with fx.as_user(jefe):
            self.assertFalse(frappe.has_permission("Customer", "read"))

    # -- Guardrails estructurales -------------------------------------------------

    def test_module_exposes_exactly_the_read_and_write_endpoints(self):
        """Updated by Commit 22.2: api/clientes.py is no longer read-only
        -- create_customer()/update_customer()/set_customer_disabled()
        were added there (their own AST guardrail lives in
        test_clientes_write_api.py). This guardrail now only proves the
        module's public surface is exactly these six, nothing more."""
        own_functions = {
            name
            for name, fn in inspect.getmembers(clientes_api, inspect.isfunction)
            if fn.__module__ == clientes_api.__name__ and not name.startswith("_")
        }
        self.assertEqual(
            own_functions,
            {
                "get_dashboard_summary",
                "search_customers",
                "get_customer_detail",
                "create_customer",
                "update_customer",
                "set_customer_disabled",
            },
        )
        for name in own_functions:
            self.assertIn(
                getattr(clientes_api, name), frappe.whitelisted, f"{name} must be @frappe.whitelist()-ed"
            )

    def test_module_source_never_writes_or_bypasses_permissions(self):
        """Checks each function body individually (not the whole module)
        so the module-level docstring's own prose -- which names these
        same forbidden snippets to explain what this module does NOT do --
        can't produce a false positive."""
        for fn in (
            clientes_api.get_dashboard_summary,
            clientes_api.search_customers,
            clientes_api.get_customer_detail,
        ):
            source = inspect.getsource(fn)
            for forbidden in _FORBIDDEN_SOURCE_SNIPPETS:
                self.assertNotIn(
                    forbidden, source, f"{fn.__name__}() must not contain {forbidden!r}"
                )
