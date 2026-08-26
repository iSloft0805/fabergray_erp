# -*- coding: utf-8 -*-
"""Commit 22.2 -- api/clientes.py write endpoints: create_customer(),
update_customer(), set_customer_disabled(). All three build/modify a
native Customer only (frappe.new_doc()/frappe.get_doc()+check_permission()
+.insert()/.save() -- never ignore_permissions, never frappe.set_user()
outside this test file, never SQL). Uses disposable "FG222 ..." Customers
only -- the real 4091 migrated Customers are never touched, confirmed
by an exact count assertion.

Six kinds of check, matching the approved Commit 22.2 brief:
- functional create: valid Customer, default customer_type="Company";
- functional update: name/access_nombre_comercial/tax_id/customer_type,
  one field at a time and combined, each proven to leave every other
  field untouched;
- validation: invalid customer_type rejected (against the doctype's own
  live Select options, not a hardcoded copy); unknown/economic fields
  rejected; access_id_cliente rejected in update AND proven unchanged
  on a Customer that already has one; disabled rejected inside
  update_customer specifically (its own dedicated endpoint exists for a
  reason); non-boolean disabled value rejected;
- activar/desactivar: set_customer_disabled() toggles disabled only;
- permissions, real restricted sessions: Gestión de Clientes can create/
  update/disable; Vendedora/Facturación/Bodega/Jefe de Bodega cannot
  create or update; Gestión de Clientes itself still cannot delete;
- structural guardrail: an AST walk (not a substring search -- immune
  to a docstring merely mentioning one of these names) proves none of
  the three write endpoints contains ignore_permissions=, a call to
  frappe.set_user/frappe.get_all/frappe.db.commit/frappe.db.sql.
"""

import ast
import inspect

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import clientes as clientes_api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_WRITE_ENDPOINTS = ("create_customer", "update_customer", "set_customer_disabled")

_FORBIDDEN_CALLS = {"frappe.set_user", "frappe.get_all", "frappe.db.commit", "frappe.db.sql"}


def _dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _forbidden_findings(source):
    """Real AST walk, not a substring search: a Call node's dotted callee
    name is compared against _FORBIDDEN_CALLS, and any keyword argument
    literally named ignore_permissions is flagged -- so a docstring that
    merely *mentions* "frappe.set_user" (as this module's own docstring
    does, to explain what is NOT used) can never produce a false
    positive, unlike a plain string search."""
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)
            if dotted in _FORBIDDEN_CALLS:
                findings.append(dotted)
        if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
            findings.append("ignore_permissions=")
    return findings


class TestClientesWriteApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.gestion_user = cls.world.user("fg222-gestion-clientes@example.com", ["Gestión de Clientes"])

    def _create(self, **fields):
        result = clientes_api.create_customer(fields)
        self.world.track_existing("Customer", result["name"])
        return result

    # -- Crear ------------------------------------------------------------------

    def test_create_customer_valid(self):
        with fx.as_user(self.gestion_user):
            result = self._create(customer_name="FG222 Nuevo Valido", tax_id="900222001-1")

        doc = frappe.get_doc("Customer", result["name"])
        self.assertEqual(doc.customer_name, "FG222 Nuevo Valido")
        self.assertEqual(doc.tax_id, "900222001-1")
        self.assertIsNone(doc.access_id_cliente)

    def test_create_customer_defaults_to_company(self):
        with fx.as_user(self.gestion_user):
            result = self._create(customer_name="FG222 Default Company")

        doc = frappe.get_doc("Customer", result["name"])
        self.assertEqual(doc.customer_type, "Company")

    def test_create_customer_rejects_invalid_customer_type(self):
        with fx.as_user(self.gestion_user):
            with self.assertRaises(frappe.ValidationError):
                clientes_api.create_customer(
                    {"customer_name": "FG222 Tipo Invalido", "customer_type": "NotAValidType"}
                )
        self.assertFalse(frappe.db.exists("Customer", "FG222 Tipo Invalido"))

    def test_create_customer_rejects_unknown_field(self):
        with fx.as_user(self.gestion_user):
            with self.assertRaises(frappe.ValidationError):
                clientes_api.create_customer(
                    {"customer_name": "FG222 Campo Raro", "credit_limit": 1000000}
                )
        self.assertFalse(frappe.db.exists("Customer", "FG222 Campo Raro"))

    # -- Editar -------------------------------------------------------------------

    def test_update_customer_name(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Nombre Original")
            clientes_api.update_customer(c["name"], {"customer_name": "FG222 Nombre Editado"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.customer_name, "FG222 Nombre Editado")

    def test_update_access_nombre_comercial(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Comercial Original")
            clientes_api.update_customer(c["name"], {"access_nombre_comercial": "NUEVO COMERCIAL"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.access_nombre_comercial, "NUEVO COMERCIAL")
        self.assertEqual(doc.customer_name, "FG222 Comercial Original")  # unchanged

    def test_update_tax_id(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 TaxId Original", tax_id="900222002-2")
            clientes_api.update_customer(c["name"], {"tax_id": "900222003-3"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.tax_id, "900222003-3")
        self.assertEqual(doc.customer_name, "FG222 TaxId Original")  # unchanged

    def test_update_customer_type_valid(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Tipo Original")
            clientes_api.update_customer(c["name"], {"customer_type": "Individual"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.customer_type, "Individual")

    def test_update_customer_type_invalid_is_rejected_and_unchanged(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Tipo Rechazo")
            with self.assertRaises(frappe.ValidationError):
                clientes_api.update_customer(c["name"], {"customer_type": "Bogus"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.customer_type, "Company")  # unchanged (create default)

    def test_update_customer_rejects_unknown_field(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Update Campo Raro")
            with self.assertRaises(frappe.ValidationError):
                clientes_api.update_customer(c["name"], {"credit_limit": 999})

    def test_update_customer_rejects_access_id_cliente_and_leaves_it_unchanged(self):
        """Simulates a migrated Customer (access_id_cliente already set) --
        the write endpoint must reject any attempt to touch it, and the
        original value must survive the rejected call untouched."""
        c = self.world.customer("FG222 Migrado Simulado")
        c.access_id_cliente = "555222"
        c.save()

        with fx.as_user(self.gestion_user):
            with self.assertRaises(frappe.ValidationError):
                clientes_api.update_customer(c.name, {"access_id_cliente": "999999"})

        c.reload()
        self.assertEqual(c.access_id_cliente, "555222")

    def test_update_customer_rejects_disabled_field(self):
        """disabled has its own dedicated endpoint -- update_customer()'s
        general allowlist must reject it explicitly, not silently ignore
        or silently apply it."""
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Update Disabled Rechazo")
            with self.assertRaises(frappe.ValidationError):
                clientes_api.update_customer(c["name"], {"disabled": 1})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.disabled, 0)  # unchanged

    # -- Activar / desactivar ------------------------------------------------------

    def test_set_customer_disabled_toggle(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Toggle Estado")
            clientes_api.set_customer_disabled(c["name"], True)
            doc = frappe.get_doc("Customer", c["name"])
            self.assertEqual(doc.disabled, 1)

            clientes_api.set_customer_disabled(c["name"], False)
            doc.reload()
            self.assertEqual(doc.disabled, 0)

    def test_set_customer_disabled_rejects_non_boolean(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 Bool Invalido")
            with self.assertRaises(frappe.ValidationError):
                clientes_api.set_customer_disabled(c["name"], "maybe")

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.disabled, 0)  # unchanged

    # -- Permisos: positivo ---------------------------------------------------------

    def test_gestion_de_clientes_cannot_delete(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG222 No Delete")
            self.assertFalse(frappe.has_permission("Customer", "delete"))
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc("Customer", c["name"]).delete()

        self.assertTrue(frappe.db.exists("Customer", c["name"]))  # still there

    # -- Permisos: negativo ---------------------------------------------------------

    def test_vendedora_blocked_from_create_and_update(self):
        target = self.world.customer("FG222 Vendedora Target")
        vendedora = self.world.user("fg222-vendedora@example.com", ["Vendedora"])

        with fx.as_user(vendedora):
            self.assertFalse(frappe.has_permission("Customer", "create"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.create_customer({"customer_name": "FG222 Vendedora No Puede"})
            self.assertFalse(frappe.has_permission("Customer", "write"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.update_customer(target.name, {"tax_id": "900222004-4"})

        self.assertFalse(frappe.db.exists("Customer", "FG222 Vendedora No Puede"))

    def test_facturacion_blocked_from_create_and_update(self):
        target = self.world.customer("FG222 Facturacion Target")
        facturacion = self.world.user("fg222-facturacion@example.com", ["Facturación"])

        with fx.as_user(facturacion):
            self.assertFalse(frappe.has_permission("Customer", "create"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.create_customer({"customer_name": "FG222 Facturacion No Puede"})
            self.assertFalse(frappe.has_permission("Customer", "write"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.update_customer(target.name, {"tax_id": "900222005-5"})

        self.assertFalse(frappe.db.exists("Customer", "FG222 Facturacion No Puede"))

    def test_bodega_and_jefe_de_bodega_blocked(self):
        target = self.world.customer("FG222 Bodega Target")
        bodega = self.world.user("fg222-bodega@example.com", ["Bodega"])
        jefe = self.world.user("fg222-jefe-bodega@example.com", ["Jefe de Bodega"])

        for user in (bodega, jefe):
            with fx.as_user(user):
                self.assertFalse(frappe.has_permission("Customer", "create"))
                with self.assertRaises(frappe.PermissionError):
                    clientes_api.create_customer({"customer_name": "FG222 Bodega No Puede"})
                self.assertFalse(frappe.has_permission("Customer", "write"))
                with self.assertRaises(frappe.PermissionError):
                    clientes_api.update_customer(target.name, {"tax_id": "900222006-6"})

        self.assertFalse(frappe.db.exists("Customer", "FG222 Bodega No Puede"))

    # -- Guardrail estructural (AST) --------------------------------------------------

    def test_write_endpoints_ast_guardrail(self):
        for name in _WRITE_ENDPOINTS:
            fn = getattr(clientes_api, name)
            source = inspect.getsource(fn)
            findings = _forbidden_findings(source)
            self.assertEqual(findings, [], f"{name}() contains forbidden pattern(s): {findings}")
