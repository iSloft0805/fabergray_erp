# -*- coding: utf-8 -*-
"""Fase 2 de la migración Access (Address/Contact/Contact Phone/Contact
Email) -- fabergray_erp/migration_piloto/customer_contacts.py.

Every test builds its own throwaway Customer(s) via TestWorld (never real
production data, never the real Excel) and calls the module's own
functions directly against in-memory row dicts shaped exactly like a row
load_xlsx() would produce -- so these tests exercise the real matching/
idempotency/validation logic without ever touching a real .xlsx file.
"""

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.migration_piloto import customer_contacts as cc
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

COUNTRY = "Colombia"


def _row(**overrides):
    base = {
        "IdCliente": None,
        "Documento": None,
        "Nombre": "Cliente de Prueba",
        "Nombre contacto": None,
        "Cargo contacto": None,
        "Dirección": None,
        "IdBarrio": None,
        "Ciudad": None,
        "País": None,
        "Telefono1": None,
        "Telefono2": None,
        "Celular": None,
        "Dirección correo": None,
        "DirCorreoElectrónico2": None,
        "Habilitado": True,
        "Nombre Comercial": None,
    }
    base.update(overrides)
    return base


class TestMigrationCustomerContacts(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = fx.TestWorld()
        cls.addClassCleanup(cls.world.cleanup)

    def _customer(self, name, access_id=None, tax_id=None):
        doc = self.world.customer(name)
        if access_id is not None:
            doc.db_set("access_id_cliente", access_id)
        if tax_id is not None:
            doc.db_set("tax_id", tax_id)
        return doc

    # -- Matching -----------------------------------------------------------

    def test_match_by_access_id_cliente(self):
        cust = self._customer("FG256 Match AccessId", access_id="9001")
        by_access_id, by_tax_id = cc.load_customer_index()

        row = _row(IdCliente=9001, Documento="000000000")  # Documento deliberately wrong/unused
        matched, match_type = cc.match_customer(row, by_access_id, by_tax_id)

        self.assertEqual(match_type, "access_id")
        self.assertEqual(matched.name, cust.name)

    def test_fallback_match_by_tax_id(self):
        cust = self._customer("FG256 Match TaxId", access_id="9002", tax_id="900123456-7")
        by_access_id, by_tax_id = cc.load_customer_index()

        # IdCliente does not match anything on purpose -- forces the tax_id fallback.
        row = _row(IdCliente=999999, Documento="900.123.456-7")
        matched, match_type = cc.match_customer(row, by_access_id, by_tax_id)

        self.assertEqual(match_type, "tax_id")
        self.assertEqual(matched.name, cust.name)

    def test_ambiguous_tax_id_resolved_by_name(self):
        self._customer("FG256 Ambiguo Uno", access_id="9101", tax_id="800111222-3")
        cust_b = self._customer("FG256 Ambiguo Dos", access_id="9102", tax_id="800111222-3")
        by_access_id, by_tax_id = cc.load_customer_index()

        row = _row(IdCliente=999998, Documento="800111222-3", Nombre="fg256 ambiguo dos")  # case/space differ on purpose
        matched, match_type = cc.match_customer(row, by_access_id, by_tax_id)

        self.assertEqual(match_type, "tax_id_and_name")
        self.assertEqual(matched.name, cust_b.name)

    def test_ambiguous_tax_id_not_resolved(self):
        self._customer("FG256 Ambiguo3 A", access_id="9111", tax_id="800333444-5")
        self._customer("FG256 Ambiguo3 B", access_id="9112", tax_id="800333444-5")
        by_access_id, by_tax_id = cc.load_customer_index()

        row = _row(IdCliente=999997, Documento="800333444-5", Nombre="Nombre que no coincide con ninguno")
        matched, match_type = cc.match_customer(row, by_access_id, by_tax_id)

        self.assertIsNone(matched)
        self.assertEqual(match_type, "ambiguous")

    def test_unmatched_row(self):
        by_access_id, by_tax_id = cc.load_customer_index()
        # A deliberately unique-looking Documento -- "000000000" and similar
        # all-same-digit placeholders are real, pre-existing values in this
        # site's actual Customer data (a common Access "unknown" filler),
        # so a genuinely unmatched test needs a value that cannot collide.
        row = _row(IdCliente=999996, Documento="FG256-NUNCA-EXISTE-999996")
        matched, match_type = cc.match_customer(row, by_access_id, by_tax_id)
        self.assertIsNone(matched)
        self.assertEqual(match_type, "unmatched")

    # -- Address idempotencia ------------------------------------------------

    def test_address_idempotent_same_customer_same_address(self):
        cust = self._customer("FG256 Addr Idem", access_id="9201")
        row = _row(IdCliente=9201, Dirección="Calle 10 # 20-30", Ciudad="Bucaramanga", País=COUNTRY)

        report1 = cc._dry_run_report(
            [row], *cc.load_customer_index(), country_fallback=None, city_id_map=None
        )
        self.assertEqual(report1["addresses_would_create"], 1)

        with fx.as_user("Administrator"):
            doc, outcome = cc._create_address(cust, row, country_fallback=None, city_id_map=None)
        self.assertEqual(outcome, "created")
        self.world.track_existing("Address", doc.name)

        report2 = cc._dry_run_report(
            [row], *cc.load_customer_index(), country_fallback=None, city_id_map=None
        )
        self.assertEqual(report2["addresses_would_create"], 0)
        self.assertEqual(report2["addresses_already_exist"], 1)

    def test_multiple_legitimate_addresses_for_same_customer(self):
        cust = self._customer("FG256 Addr Multi", access_id="9202")
        row1 = _row(IdCliente=9202, Dirección="Calle 10 # 20-30", Ciudad="Bucaramanga", País=COUNTRY)
        doc1, outcome1 = cc._create_address(cust, row1, country_fallback=None, city_id_map=None)
        self.world.track_existing("Address", doc1.name)
        self.assertEqual(outcome1, "created")

        row2 = _row(IdCliente=9202, Dirección="Carrera 27 # 45-12", Ciudad="Bucaramanga", País=COUNTRY)
        cache = {}
        existing = cc.find_matching_address(cust.name, row2["Dirección"], cache)
        self.assertIsNone(existing)  # different content -- must NOT be treated as a duplicate
        doc2, outcome2 = cc._create_address(cust, row2, country_fallback=None, city_id_map=None)
        self.world.track_existing("Address", doc2.name)
        self.assertEqual(outcome2, "created")
        self.assertNotEqual(doc1.name, doc2.name)

        addresses = cc.existing_addresses_for_customer(cust.name)
        self.assertEqual(len(addresses), 2)

    def test_same_address_content_not_deduplicated_across_different_customers(self):
        cust_a = self._customer("FG256 Addr CrossA", access_id="9203")
        cust_b = self._customer("FG256 Addr CrossB", access_id="9204")
        same_line = "Avenida Siempre Viva 742"

        row_a = _row(IdCliente=9203, Dirección=same_line, Ciudad="Bucaramanga", País=COUNTRY)
        doc_a, _ = cc._create_address(cust_a, row_a, country_fallback=None, city_id_map=None)
        self.world.track_existing("Address", doc_a.name)

        cache = {}
        existing_for_b = cc.find_matching_address(cust_b.name, same_line, cache)
        self.assertIsNone(existing_for_b)  # cust_a's address must never satisfy cust_b's lookup

    def test_numeric_city_value_is_never_invented(self):
        cust = self._customer("FG256 Ciudad Numerica", access_id="9205")
        row = _row(IdCliente=9205, Dirección="Calle Falsa 123", Ciudad="49", País=COUNTRY)

        self.assertTrue(cc.is_numeric_city_value(row["Ciudad"]))
        self.assertIsNone(cc.resolve_city(row, city_id_map=None))  # no equivalence table given -> unresolved

        doc, outcome = cc._create_address(cust, row, country_fallback=None, city_id_map=None)
        self.assertIsNone(doc)
        self.assertEqual(outcome, "blocked_missing_city")
        self.assertEqual(frappe.get_all("Address", filters={"city": "49"}), [])  # never stored as a "city"

        # An approved equivalence table resolves it correctly -- still never guessed on its own.
        resolved = cc.resolve_city(row, city_id_map={"49": "Bogotá"})
        self.assertEqual(resolved, "Bogotá")

    def test_country_never_invented_without_explicit_fallback(self):
        cust = self._customer("FG256 Pais Vacio", access_id="9206")
        row = _row(IdCliente=9206, Dirección="Calle Falsa 456", Ciudad="Bucaramanga", País=None)

        doc, outcome = cc._create_address(cust, row, country_fallback=None, city_id_map=None)
        self.assertIsNone(doc)
        self.assertEqual(outcome, "blocked_missing_country")

        doc2, outcome2 = cc._create_address(cust, row, country_fallback="Colombia", city_id_map=None)
        self.world.track_existing("Address", doc2.name)
        self.assertEqual(outcome2, "created")
        self.assertEqual(doc2.country, "Colombia")

    # -- Contact: teléfonos ---------------------------------------------------

    def test_phone1_phone2_mobile_all_created(self):
        row = _row(Telefono1="315-2273268", Telefono2="6436131", Celular="3011234567")
        phones = cc.collect_phones(row)
        self.assertEqual([p for p, _m in phones], ["315-2273268", "6436131", "3011234567"])
        self.assertEqual([m for _p, m in phones], [False, False, True])

    def test_repeated_phone_is_not_duplicated(self):
        row = _row(Telefono1="315 227 3268", Celular="(315)-2273268")  # same number, different formatting
        phones = cc.collect_phones(row)
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0][0], "315 227 3268")  # first occurrence's own formatting is kept, never rewritten

    def test_contact_created_with_deduplicated_phones(self):
        cust = self._customer("FG256 Contact Phones", access_id="9301")
        row = _row(
            IdCliente=9301,
            **{"Nombre contacto": "Janeth"},
            Telefono1="315-2273268",
            Telefono2="",
            Celular="315-2273268",
        )
        contact = cc._create_contact(cust, row)
        self.world.track_existing("Contact", contact.name)
        self.assertEqual(len(contact.phone_nos), 1)

    # -- Contact: emails --------------------------------------------------------

    def test_valid_email_is_accepted(self):
        self.assertEqual(cc.clean_valid_email("janeth@example.com"), "janeth@example.com")
        row = _row(**{"Dirección correo": "janeth@example.com"})
        self.assertEqual(cc.collect_emails(row), ["janeth@example.com"])
        self.assertEqual(cc.invalid_emails_in_row(row), [])

    def test_invalid_email_is_rejected_and_reported(self):
        self.assertIsNone(cc.clean_valid_email("no-es-un-correo"))
        row = _row(**{"Dirección correo": "no-es-un-correo"})
        self.assertEqual(cc.collect_emails(row), [])
        self.assertEqual(cc.invalid_emails_in_row(row), ["no-es-un-correo"])

    # -- Segundo run no duplica --------------------------------------------------

    def test_second_run_does_not_duplicate_address_or_contact(self):
        cust = self._customer("FG256 Segundo Run", access_id="9401")
        row = _row(
            IdCliente=9401,
            **{"Nombre contacto": "Ana"},
            Dirección="Diagonal 15 # 8-20",
            Ciudad="Bucaramanga",
            País=COUNTRY,
            Telefono1="6000000",
        )

        def run_once():
            by_access_id, by_tax_id = cc.load_customer_index()
            return cc._migrate_real(
                [row], by_access_id, by_tax_id, country_fallback=None, city_id_map=None,
                commit_every=10, progress_every=10, log=lambda msg: None,
            )

        counters1, errors1 = run_once()
        self.assertEqual(errors1, [])
        self.assertEqual(counters1["addresses_created"], 1)
        self.assertEqual(counters1["contacts_created"], 1)

        addr_names = [a.name for a in cc.existing_addresses_for_customer(cust.name)]
        for name in addr_names:
            self.world.track_existing("Address", name)
        contact_links = frappe.get_all(
            "Dynamic Link", filters={"link_doctype": "Customer", "link_name": cust.name, "parenttype": "Contact"}, pluck="parent"
        )
        for name in contact_links:
            self.world.track_existing("Contact", name)

        counters2, errors2 = run_once()
        self.assertEqual(errors2, [])
        self.assertEqual(counters2["addresses_created"], 0)
        self.assertEqual(counters2["addresses_already_exist"], 1)
        self.assertEqual(counters2["contacts_created"], 0)
        self.assertEqual(counters2["contacts_already_exist"], 1)

        self.assertEqual(len(cc.existing_addresses_for_customer(cust.name)), 1)
        self.assertEqual(len(contact_links), 1)

    # -- dry_run no modifica BD --------------------------------------------------

    def test_dry_run_makes_no_writes(self):
        cust = self._customer("FG256 Dry Run NoOp", access_id="9501")
        row = _row(
            IdCliente=9501,
            **{"Nombre contacto": "Carlos", "Cargo contacto": "Gerente"},
            Dirección="Transversal 9 # 3-45",
            Ciudad="Bucaramanga",
            País=COUNTRY,
            Telefono1="6111111",
            **{"Dirección correo": "carlos@example.com"},
        )

        addresses_before = frappe.db.count("Address")
        contacts_before = frappe.db.count("Contact")
        dynamic_links_before = frappe.db.count("Dynamic Link")

        report = cc._dry_run_report([row], *cc.load_customer_index(), country_fallback=None, city_id_map=None)

        self.assertEqual(report["addresses_would_create"], 1)
        self.assertEqual(report["contacts_would_create"], 1)
        self.assertEqual(frappe.db.count("Address"), addresses_before)
        self.assertEqual(frappe.db.count("Contact"), contacts_before)
        self.assertEqual(frappe.db.count("Dynamic Link"), dynamic_links_before)
        self.assertIsNone(frappe.db.get_value("Customer", cust.name, "customer_primary_address"))
        self.assertIsNone(frappe.db.get_value("Customer", cust.name, "customer_primary_contact"))

    # -- migrate_addresses=False: garantía dura, no sugerencia -------------------

    def test_migrate_addresses_false_creates_no_address_and_needs_no_city_or_country(self):
        """Fila con dirección Y datos de contacto, pero país/ciudad ambos
        ausentes (ni country_fallback ni city_id_map se pasan) -- con
        migrate_addresses=False esto debe migrar el Contact sin problema y
        sin tocar Address para nada, aunque los datos de dirección serían
        inválidos para crear un Address hoy."""
        cust = self._customer("FG256 AddrOff", access_id="9601")
        row = _row(
            IdCliente=9601,
            **{"Nombre contacto": "Pedro"},
            Dirección="Calle sin ciudad ni pais 99",
            Ciudad="49",  # numérico, sin city_id_map -- no debería importar
            País=None,  # vacío, sin country_fallback -- no debería importar
            Telefono1="7000000",
        )

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counters["addresses_created"], 0)
        self.assertEqual(counters["addresses_blocked_missing_city"], 0)
        self.assertEqual(counters["addresses_blocked_missing_country"], 0)
        self.assertEqual(counters["contacts_created"], 1)

        contact_name = cc.existing_contact_name_for_customer(cust.name)
        self.world.track_existing("Contact", contact_name)

        # No Address of any kind was created or linked to this Customer.
        self.assertEqual(cc.existing_addresses_for_customer(cust.name), [])
        self.assertIsNone(frappe.db.get_value("Customer", cust.name, "customer_primary_address"))
        # But the Contact side went through normally.
        self.assertEqual(frappe.db.get_value("Customer", cust.name, "customer_primary_contact"), contact_name)

    def test_migrate_contacts_only_granular_counters(self):
        cust = self._customer("FG256 Granular", access_id="9602")
        row = _row(
            IdCliente=9602,
            **{"Nombre contacto": "Laura"},
            Telefono1="7111111",
            Telefono2="7222222",
            Celular="7333333",
            **{"Dirección correo": "laura@example.com", "DirCorreoElectrónico2": "no-es-valido"},
        )

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["phones_created"], 3)
        self.assertEqual(counters["emails_created"], 1)
        self.assertEqual(counters["emails_invalid_skipped"], 1)
        self.assertEqual(counters["dynamic_links_created"], 1)
        self.assertEqual(counters["primary_contacts_set"], 1)
        self.assertEqual(counters["primary_contacts_preserved"], 0)

        self.world.track_existing("Contact", cc.existing_contact_name_for_customer(cust.name))

    def test_primary_contact_not_overwritten_when_already_set(self):
        cust = self._customer("FG256 PrimaryPreserved", access_id="9603")
        other_contact = frappe.new_doc("Contact")
        other_contact.first_name = "Contacto Manual"
        other_contact.append("links", {"link_doctype": "Customer", "link_name": cust.name})
        other_contact.insert()
        self.world.track_existing("Contact", other_contact.name)
        cust.db_set("customer_primary_contact", other_contact.name)

        row = _row(IdCliente=9603, **{"Nombre contacto": "Otro Nombre"}, Telefono1="7444444")
        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )
        self.assertEqual(errors, [])
        # Customer already had ANY Contact linked -- this migration must not
        # create a second one, and must never overwrite the primary.
        self.assertEqual(counters["contacts_created"], 0)
        self.assertEqual(counters["contacts_already_exist"], 1)
        self.assertEqual(counters["primary_contacts_preserved"], 1)
        self.assertEqual(counters["primary_contacts_set"], 0)
        self.assertEqual(frappe.db.get_value("Customer", cust.name, "customer_primary_contact"), other_contact.name)

    def test_a_failed_row_never_assigns_a_previous_rows_contact_as_primary(self):
        """Regression (test F): found live during the real fabergray.local
        run -- a row whose own Contact insert genuinely fails must NEVER
        end up with customer_primary_contact pointing at some OTHER,
        unrelated customer's own Contact -- which is exactly what
        happened for 13 real customers before this fix, because
        contact_name wasn't reset per row and silently kept the previous
        successful row's value after savepoint(catch=Exception) swallowed
        the failure. An invalid-phone-only row no longer fails at all
        (see test_invalid_phone_only_with_name_recovers_contact below) --
        this test forces a genuine, still-real failure mode (mocked at
        _create_contact itself) to keep proving the reset independently
        of what specifically can fail."""
        cust_ok = self._customer("FG256 BugRegresion OK", access_id="9701")
        cust_fail = self._customer("FG256 BugRegresion Fail", access_id="9702")

        row_ok = _row(IdCliente=9701, **{"Nombre contacto": "Contacto Valido"}, Telefono1="7555555")
        row_fail = _row(IdCliente=9702, **{"Nombre contacto": "Fallara Genuinamente"})

        real_create_contact = cc._create_contact

        def _fail_for_9702(customer, row):
            if row.get("IdCliente") == 9702:
                raise RuntimeError("fallo genuino simulado")
            return real_create_contact(customer, row)

        by_access_id, by_tax_id = cc.load_customer_index()
        with mock.patch.object(cc, "_create_contact", side_effect=_fail_for_9702):
            counters, errors = cc._migrate_real(
                [row_ok, row_fail], by_access_id, by_tax_id,
                country_fallback=None, city_id_map=None,
                commit_every=10, progress_every=10, log=lambda msg: None,
                migrate_addresses=False, migrate_contacts=True,
            )

        # Track before asserting -- so a future assertion failure never
        # leaves an untracked, orphaned Contact behind for a later test
        # run to accidentally "inherit" via this same deterministic name.
        ok_contact = cc.existing_contact_name_for_customer(cust_ok.name)
        if ok_contact:
            self.world.track_existing("Contact", ok_contact)

        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["errors"], 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["IdCliente"], 9702)

        self.assertIsNotNone(ok_contact)
        self.assertEqual(frappe.db.get_value("Customer", cust_ok.name, "customer_primary_contact"), ok_contact)

        # The failed row's own Customer must have NO Contact and NO
        # primary_contact of any kind -- specifically never cust_ok's own.
        self.assertIsNone(cc.existing_contact_name_for_customer(cust_fail.name))
        self.assertIsNone(frappe.db.get_value("Customer", cust_fail.name, "customer_primary_contact"))

    # -- Validación granular de teléfonos (sección 1/2/3 de este turno) --------

    def test_invalid_phone_only_with_name_recovers_contact(self):
        """Test A + parte de F: Telefono1="EXT 138" + Nombre contacto
        válido -> Contact SÍ se crea, el teléfono inválido simplemente no
        se inserta, y no se cuenta ningún error."""
        cust = self._customer("FG256 SoloExtConNombre", access_id="9801")
        row = _row(IdCliente=9801, **{"Nombre contacto": "Maria"}, Telefono1="EXT 138")

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counters["errors"], 0)
        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["invalid_phones_skipped"], 1)
        self.assertEqual(counters["contacts_recovered_from_invalid_phone_rows"], 1)
        self.assertEqual(counters["phones_created"], 0)

        contact_name = cc.existing_contact_name_for_customer(cust.name)
        self.world.track_existing("Contact", contact_name)
        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual(contact.first_name, "Maria")
        self.assertEqual(len(contact.phone_nos), 0)

    def test_invalid_phone_only_with_valid_email_recovers_contact(self):
        """Test B: Telefono1="EXT 138" + email válido -> Contact se crea
        con el email, sin el teléfono."""
        cust = self._customer("FG256 SoloExtConEmail", access_id="9802")
        row = _row(IdCliente=9802, Telefono1="EXT 138", **{"Dirección correo": "maria@example.com"})

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["emails_created"], 1)
        self.assertEqual(counters["invalid_phones_skipped"], 1)

        contact_name = cc.existing_contact_name_for_customer(cust.name)
        self.world.track_existing("Contact", contact_name)
        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual([e.email_id for e in contact.email_ids], ["maria@example.com"])
        self.assertEqual(len(contact.phone_nos), 0)

    def test_invalid_phone1_valid_phone2_only_valid_one_inserted(self):
        """Test C: Telefono1 inválido + Telefono2 válido -> Contact
        creado, solo Telefono2 insertado."""
        cust = self._customer("FG256 Tel1InvalidoTel2Valido", access_id="9803")
        row = _row(IdCliente=9803, **{"Nombre contacto": "Pedro"}, Telefono1="EXT 200", Telefono2="6111111")

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["phones_created"], 1)
        self.assertEqual(counters["invalid_phones_skipped"], 1)

        contact_name = cc.existing_contact_name_for_customer(cust.name)
        self.world.track_existing("Contact", contact_name)
        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual([p.phone for p in contact.phone_nos], ["6111111"])

    def test_invalid_phone1_valid_mobile_only_mobile_inserted(self):
        """Test D: Telefono1 inválido + Celular válido -> Contact creado,
        el celular sí se inserta (marcado is_primary_mobile_no)."""
        cust = self._customer("FG256 Tel1InvalidoCelularValido", access_id="9804")
        row = _row(IdCliente=9804, **{"Nombre contacto": "Sofia"}, Telefono1="EXT 300", Celular="3001234567")

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(counters["contacts_created"], 1)
        self.assertEqual(counters["phones_created"], 1)

        contact_name = cc.existing_contact_name_for_customer(cust.name)
        self.world.track_existing("Contact", contact_name)
        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual(len(contact.phone_nos), 1)
        self.assertEqual(contact.phone_nos[0].phone, "3001234567")
        self.assertEqual(contact.phone_nos[0].is_primary_mobile_no, 1)

    def test_only_invalid_phone_and_nothing_else_skips_without_creating_empty_contact(self):
        """Test E: solo "EXT 138" y ningún otro dato útil -> Contact NO se
        crea, contact_rows_skipped_no_valid_data += 1, sin error."""
        cust = self._customer("FG256 SoloExtNadaMas", access_id="9805")
        row = _row(IdCliente=9805, Telefono1="EXT 138")

        by_access_id, by_tax_id = cc.load_customer_index()
        counters, errors = cc._migrate_real(
            [row], by_access_id, by_tax_id,
            country_fallback=None, city_id_map=None,
            commit_every=10, progress_every=10, log=lambda msg: None,
            migrate_addresses=False, migrate_contacts=True,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counters["errors"], 0)
        self.assertEqual(counters["contacts_created"], 0)
        self.assertEqual(counters["contact_rows_skipped_no_valid_data"], 1)
        self.assertEqual(counters["invalid_phones_skipped"], 1)
        self.assertIsNone(cc.existing_contact_name_for_customer(cust.name))
        self.assertIsNone(frappe.db.get_value("Customer", cust.name, "customer_primary_contact"))
