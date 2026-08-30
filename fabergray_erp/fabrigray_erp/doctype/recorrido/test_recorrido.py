# Copyright (c) 2026, Fabrigray SAS and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestRecorrido(IntegrationTestCase):
	"""Commit 24.1 -- the real functional/business-rule coverage for the
	whole Recorrido flow (create_route/plan_route/cancel_route/
	update_route_stops, eligibility, double-assignment locking, company
	isolation) lives in fabergray_erp/tests/test_recorridos_api.py, exactly
	like test_facturacion_invoicing_status.py does for Pick List's own
	fg_invoicing_status flow -- this file only proves the doctype itself
	can be created directly by Administrator, same minimal-smoke role every
	other doctype-level test file in this app plays alongside its real
	suite in fabergray_erp/tests/."""

	@classmethod
	def setUpClass(cls):
		# See test_reporte_de_faltante.py's own identical guard for why this
		# is needed: IntegrationTestCase auto-detects cls.doctype from this
		# file's location and would otherwise call make_test_records(),
		# which walks every Link field's own test module recursively and
		# hits a pre-existing, unrelated erpnext test-utility bug.
		frappe.local.test_objects.setdefault("Recorrido", [])
		super().setUpClass()

	def test_can_be_created_directly(self):
		doc = frappe.get_doc(
			{
				"doctype": "Recorrido",
				"company": frappe.db.get_single_value("Global Defaults", "default_company")
				or frappe.get_all("Company", limit=1, pluck="name")[0],
			}
		)
		doc.insert()
		self.assertEqual(doc.status, "Borrador")
		self.assertEqual(doc.created_by_user, frappe.session.user)
		frappe.delete_doc("Recorrido", doc.name, force=True)
