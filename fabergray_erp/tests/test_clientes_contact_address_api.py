# -*- coding: utf-8 -*-
"""Commit 22.7 -- update_customer()'s Contact/Address extension (Page
Clientes' "Editar cliente" modal, INFORMACIÓN DE CONTACTO section): native
Contact/Address/Dynamic Link only, no duplicated phone/address fields on
Customer. Create-or-update against the Customer's own primary
Contact/Address (api/clientes.py's _primary_contact_name()/
_primary_address_name() -- Customer.customer_primary_contact/
customer_primary_address first, ERPNext's own get_default_contact()/
get_default_address() fallback second, never an arbitrary pick of our
own). Server-side permission on Contact/Address enforced independently of
Customer's own (this commit also adds a Custom DocPerm row granting
"Gestión de Clientes" read/write/create on both -- see
fixtures/custom_docperm.json; before this commit that role had zero
permission there, since Contact/Address already had a Custom DocPerm row
of their own (Vendedora's), which masks every native DocPerm for every
other role -- same masking mechanism this app's own Commit 22.4 already
documents for Item Price/Stock Ledger Entry). Single logical transaction,
no frappe.db.commit() anywhere in api/clientes.py: _apply_contact_payload()/
_apply_address_payload() run before doc.save(), so an exception from
either (bad payload, missing permission, a native mandatory-field
validation) leaves the Customer's own field changes unsaved too.

Fourteen kinds of check, matching the approved Commit 22.7 brief: no
Contact/Address; create teléfono principal; create principal+secundario;
create Address; create both at once; edit an existing Contact/Address in
place (same document reused, not a second one); idempotency (same data
saved twice never duplicates); correct primary chosen among several
Contacts/Addresses; permission denial independent of Customer's own;
empty fields create nothing; a mid-request error leaves the Customer
untouched; and the read endpoint surfaces Contact/Address correctly."""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import clientes as clientes_api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestClientesContactAddressApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)
        cls.gestion_user = cls.world.user("fg227-gestion-clientes@example.com", ["Gestión de Clientes"])

        # Ad hoc role for test 11: Customer read/write/create (same shape
        # as "Gestión de Clientes"'s own grant), deliberately with no
        # Custom DocPerm row of its own on Contact/Address -- Contact and
        # Address both already have at least one Custom DocPerm row
        # (Vendedora's), so any role without an explicit row of its own
        # gets exactly zero permission there (frappe.permissions.
        # get_valid_perms() ignores every native DocPerm the instant ANY
        # Custom DocPerm row exists for a doctype -- confirmed live, same
        # mechanism Commit 22.4 already documents for Item Price/Stock
        # Ledger Entry). This is what makes it possible to test "Customer
        # write granted, Contact/Address not" as a real, independent
        # permission boundary instead of a synthetic mock.
        role = frappe.get_doc(
            {"doctype": "Role", "role_name": "FG227 Cliente Sin Contacto", "desk_access": 1}
        )
        role.insert()
        cls.world.track_existing("Role", role.name)
        docperm = frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": "Customer",
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": role.name,
                "read": 1,
                "write": 1,
                "create": 1,
            }
        )
        docperm.insert()
        cls.world.track_existing("Custom DocPerm", docperm.name)
        cls.no_contact_user = cls.world.user("fg227-sin-contacto@example.com", [role.name])

    def _create(self, **fields):
        result = clientes_api.create_customer(fields)
        self.world.track_existing("Customer", result["name"])
        return result

    def _contact_for(self, customer_name, mobile_no=None, phone=None, is_primary=1):
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "company_name": customer_name,
                "is_primary_contact": is_primary,
                "links": [{"link_doctype": "Customer", "link_name": customer_name}],
            }
        )
        if mobile_no:
            contact.append("phone_nos", {"phone": mobile_no, "is_primary_mobile_no": 1})
        if phone:
            contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
        contact.insert()
        self.world.track_existing("Contact", contact.name)
        return contact

    def _address_for(self, customer_name, address_line1="Calle 1", city="Bogotá", state=None, is_primary=1):
        address = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": customer_name,
                "address_type": "Billing",
                "address_line1": address_line1,
                "city": city,
                "state": state,
                "country": "Colombia",
                "is_primary_address": is_primary,
                "links": [{"link_doctype": "Customer", "link_name": customer_name}],
            }
        )
        address.insert()
        self.world.track_existing("Address", address.name)
        return address

    def _linked_contacts(self, customer_name):
        return frappe.get_all(
            "Contact",
            filters=[
                ["Dynamic Link", "link_doctype", "=", "Customer"],
                ["Dynamic Link", "link_name", "=", customer_name],
            ],
            pluck="name",
        )

    def _linked_addresses(self, customer_name):
        return frappe.get_all(
            "Address",
            filters=[
                ["Dynamic Link", "link_doctype", "=", "Customer"],
                ["Dynamic Link", "link_name", "=", customer_name],
            ],
            pluck="name",
        )

    # -- 1. Cliente sin Contact ni Address ---------------------------------

    def test_customer_without_contact_or_address(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Sin Contacto Ni Address")
            detail = clientes_api.get_customer_detail(c["name"])

        self.assertIsNone(detail["contact"])
        self.assertIsNone(detail["address"])
        self.assertEqual(self._linked_contacts(c["name"]), [])
        self.assertEqual(self._linked_addresses(c["name"]), [])

    # -- 2/3. Crear teléfono -------------------------------------------------

    def test_create_primary_phone_only(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Solo Movil")
            clientes_api.update_customer(c["name"], contact={"mobile_no": "3001112233"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertTrue(doc.customer_primary_contact)
        contact = frappe.get_doc("Contact", doc.customer_primary_contact)
        self.assertEqual(contact.mobile_no, "3001112233")
        self.assertEqual(contact.phone, "")
        self.assertTrue(contact.has_link("Customer", c["name"]))

    def test_create_primary_and_secondary_phone(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Movil Y Fijo")
            clientes_api.update_customer(
                c["name"], contact={"mobile_no": "3002223344", "phone": "6012223344"}
            )

        doc = frappe.get_doc("Customer", c["name"])
        contact = frappe.get_doc("Contact", doc.customer_primary_contact)
        self.assertEqual(contact.mobile_no, "3002223344")
        self.assertEqual(contact.phone, "6012223344")

    # -- 4. Crear Address -----------------------------------------------------

    def test_create_address(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Con Direccion")
            clientes_api.update_customer(
                c["name"],
                address={"address_line1": "Cra 10 # 20-30", "city": "Bogotá", "state": "Cundinamarca"},
            )

        doc = frappe.get_doc("Customer", c["name"])
        self.assertTrue(doc.customer_primary_address)
        address = frappe.get_doc("Address", doc.customer_primary_address)
        self.assertEqual(address.address_line1, "Cra 10 # 20-30")
        self.assertEqual(address.city, "Bogotá")
        self.assertEqual(address.state, "Cundinamarca")
        self.assertEqual(address.country, "Colombia")
        self.assertEqual(address.address_type, "Billing")
        self.assertTrue(address.has_link("Customer", c["name"]))

    # -- 5. Crear Contact + Address simultáneamente ----------------------------

    def test_create_contact_and_address_together(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Ambos")
            clientes_api.update_customer(
                c["name"],
                contact={"mobile_no": "3003334455"},
                address={"address_line1": "Calle 5", "city": "Medellín"},
            )

        doc = frappe.get_doc("Customer", c["name"])
        self.assertTrue(doc.customer_primary_contact)
        self.assertTrue(doc.customer_primary_address)

    # -- 6. Editar Contact existente --------------------------------------------

    def test_edit_existing_contact(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Editar Contacto")
            clientes_api.update_customer(c["name"], contact={"mobile_no": "3004445566"})
            first_contact_name = frappe.get_doc("Customer", c["name"]).customer_primary_contact

            clientes_api.update_customer(c["name"], contact={"mobile_no": "3009998877"})

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.customer_primary_contact, first_contact_name)  # same Contact, not a new one
        contact = frappe.get_doc("Contact", first_contact_name)
        self.assertEqual(contact.mobile_no, "3009998877")
        self.assertEqual(len(self._linked_contacts(c["name"])), 1)

    # -- 7. Editar Address existente --------------------------------------------

    def test_edit_existing_address(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Editar Direccion")
            clientes_api.update_customer(
                c["name"], address={"address_line1": "Calle Vieja", "city": "Cali"}
            )
            first_address_name = frappe.get_doc("Customer", c["name"]).customer_primary_address

            clientes_api.update_customer(
                c["name"], address={"address_line1": "Calle Nueva", "city": "Cali"}
            )

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.customer_primary_address, first_address_name)  # same Address, not a new one
        address = frappe.get_doc("Address", first_address_name)
        self.assertEqual(address.address_line1, "Calle Nueva")
        self.assertEqual(len(self._linked_addresses(c["name"])), 1)

    # -- 8. Idempotencia: mismos datos guardados dos veces ----------------------

    def test_saving_same_data_twice_does_not_duplicate(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Idempotente")
            contact_payload = {"mobile_no": "3005556677"}
            address_payload = {"address_line1": "Calle X", "city": "Bogotá"}
            clientes_api.update_customer(c["name"], contact=contact_payload, address=address_payload)
            clientes_api.update_customer(c["name"], contact=contact_payload, address=address_payload)

        self.assertEqual(len(self._linked_contacts(c["name"])), 1)
        self.assertEqual(len(self._linked_addresses(c["name"])), 1)

    # -- 9. Múltiples Contacts: usar correctamente el principal -----------------

    def test_multiple_contacts_uses_the_primary_one(self):
        c = self.world.customer("FG227 Multi Contact")
        other = self._contact_for(c.name, mobile_no="3009990000", is_primary=0)
        primary = self._contact_for(c.name, mobile_no="3008880000", is_primary=1)
        c.customer_primary_contact = primary.name
        c.save()

        with fx.as_user(self.gestion_user):
            clientes_api.update_customer(c.name, contact={"mobile_no": "3007770000"})

        primary.reload()
        other.reload()
        self.assertEqual(primary.mobile_no, "3007770000")
        self.assertEqual(other.mobile_no, "3009990000")  # untouched

    # -- 10. Múltiples Addresses: usar correctamente la principal ---------------

    def test_multiple_addresses_uses_the_primary_one(self):
        c = self.world.customer("FG227 Multi Address")
        other = self._address_for(c.name, address_line1="Otra Calle", city="Cali", is_primary=0)
        primary = self._address_for(c.name, address_line1="Calle Principal", city="Bogotá", is_primary=1)
        c.customer_primary_address = primary.name
        c.save()

        with fx.as_user(self.gestion_user):
            clientes_api.update_customer(c.name, address={"address_line1": "Calle Editada", "city": "Bogotá"})

        primary.reload()
        other.reload()
        self.assertEqual(primary.address_line1, "Calle Editada")
        self.assertEqual(other.address_line1, "Otra Calle")  # untouched

    # -- 11. Permisos: sin permiso sobre Contact/Address ------------------------

    def test_user_without_contact_address_permission_is_denied(self):
        c = self.world.customer("FG227 Sin Permiso Contacto")

        with fx.as_user(self.no_contact_user):
            self.assertTrue(frappe.has_permission("Customer", "write"))
            self.assertFalse(frappe.has_permission("Contact", "create"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.update_customer(c.name, contact={"mobile_no": "3000001111"})
            self.assertFalse(frappe.has_permission("Address", "create"))
            with self.assertRaises(frappe.PermissionError):
                clientes_api.update_customer(c.name, address={"address_line1": "X", "city": "Y"})

        c.reload()
        self.assertIsNone(c.customer_primary_contact)
        self.assertIsNone(c.customer_primary_address)
        self.assertEqual(self._linked_contacts(c.name), [])
        self.assertEqual(self._linked_addresses(c.name), [])

    # -- 12. Campos vacíos no crean documentos innecesarios ---------------------

    def test_empty_fields_create_nothing(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Campos Vacios")
            clientes_api.update_customer(
                c["name"],
                contact={"mobile_no": "", "phone": ""},
                address={"address_line1": "", "city": "", "state": ""},
            )

        doc = frappe.get_doc("Customer", c["name"])
        self.assertIsNone(doc.customer_primary_contact)
        self.assertIsNone(doc.customer_primary_address)
        self.assertEqual(self._linked_contacts(c["name"]), [])
        self.assertEqual(self._linked_addresses(c["name"]), [])

    # -- 13. Error en Address no deja Customer parcialmente actualizado ---------

    def test_address_error_leaves_customer_untouched(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Error Parcial", tax_id="900227001-1")
            with self.assertRaises(frappe.ValidationError):
                clientes_api.update_customer(
                    c["name"],
                    customer={"tax_id": "900227999-9"},
                    address={"state": "Antioquia"},  # falta address_line1/city -> throw, antes de doc.save()
                )

        doc = frappe.get_doc("Customer", c["name"])
        self.assertEqual(doc.tax_id, "900227001-1")  # unchanged -- doc.save() never ran
        self.assertIsNone(doc.customer_primary_address)
        self.assertEqual(self._linked_contacts(c["name"]), [])
        self.assertEqual(self._linked_addresses(c["name"]), [])

    # -- 14. El endpoint de lectura devuelve Contact/Address correctamente ------

    def test_get_customer_detail_returns_contact_and_address(self):
        with fx.as_user(self.gestion_user):
            c = self._create(customer_name="FG227 Detalle Completo")
            clientes_api.update_customer(
                c["name"],
                contact={"mobile_no": "3006667788", "phone": "6017778899"},
                address={"address_line1": "Calle Detalle", "city": "Barranquilla", "state": "Atlántico"},
            )
            detail = clientes_api.get_customer_detail(c["name"])

        self.assertIsNotNone(detail["contact"])
        self.assertEqual(detail["contact"]["mobile_no"], "3006667788")
        self.assertEqual(detail["contact"]["phone"], "6017778899")
        self.assertIsNotNone(detail["address"])
        self.assertEqual(detail["address"]["address_line1"], "Calle Detalle")
        self.assertEqual(detail["address"]["city"], "Barranquilla")
        self.assertEqual(detail["address"]["state"], "Atlántico")
        self.assertEqual(detail["address"]["country"], "Colombia")
