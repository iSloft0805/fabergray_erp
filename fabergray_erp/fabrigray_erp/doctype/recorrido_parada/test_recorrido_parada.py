# Copyright (c) 2026, Fabrigray SAS and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestRecorridoParada(IntegrationTestCase):
	"""Commit 24.1 -- real functional coverage lives in
	fabergray_erp/tests/test_recorridos_api.py. See test_recorrido.py's own
	identical docstring/guard for why."""

	@classmethod
	def setUpClass(cls):
		frappe.local.test_objects.setdefault("Recorrido Parada", [])
		super().setUpClass()
