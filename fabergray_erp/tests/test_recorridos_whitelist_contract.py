# -*- coding: utf-8 -*-
"""Hotfix 26.1.1 -- whitelist contract for api.recorridos.

Regression being locked down: Commit 25.20 inserted
`_routes_matching_search()` between `@frappe.whitelist()` and
`get_routes()`, silently moving the decorator onto the private helper.
Every existing test in test_recorridos_api.py calls `recorridos.get_routes()`
directly in Python, which never goes through `frappe.is_whitelisted()`, so
the suite stayed green while the real UI failed with "Método no permitido".

These tests check the contract from the outside: membership in
`frappe.whitelisted`, every method recorridos.js calls remotely, no private
function exposed, and one real dispatch through `frappe.handler.execute_cmd()`
(the same entry point /api/method/<cmd> uses) so a missing decorator fails
here exactly like it fails in the browser."""

import inspect
import os
import re
from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import recorridos
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_MODULE_PATH = "fabergray_erp.api.recorridos"
_RECORRIDOS_JS = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "recorridos", "recorridos.js"
)


def _module_functions():
	return {
		name: fn
		for name, fn in inspect.getmembers(recorridos, inspect.isfunction)
		if fn.__module__ == recorridos.__name__
	}


def _js_remote_methods():
	"""Every api.recorridos method recorridos.js calls remotely: both the
	`this.call("x")`/`this.call_route_write("x")` shorthand (resolved against
	the page's own `method_prefix`) and any fully-qualified literal."""
	with open(_RECORRIDOS_JS, encoding="utf-8") as f:
		source = f.read()

	prefix = re.search(r'this\.method_prefix\s*=\s*"([^"]+)"', source)
	assert prefix and prefix.group(1) == _MODULE_PATH + ".", "recorridos.js method_prefix changed"

	methods = set(re.findall(r'this\.call(?:_route_write)?\(\s*"([A-Za-z0-9_]+)"', source))
	methods.update(re.findall(re.escape(_MODULE_PATH) + r"\.([A-Za-z0-9_]+)", source))
	return methods


class TestRecorridosWhitelistContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.recorrido_user = cls.world.user("fg2611-recorrido@example.com", ["Recorrido"])

	# A.
	def test_get_routes_is_whitelisted(self):
		self.assertIn(recorridos.get_routes, frappe.whitelisted)

	# B.
	def test_routes_matching_search_is_not_whitelisted(self):
		self.assertNotIn(recorridos._routes_matching_search, frappe.whitelisted)

	# C.
	def test_every_method_called_from_recorridos_js_is_whitelisted(self):
		methods = _js_remote_methods()
		# Guard against the regex silently matching nothing.
		self.assertIn("get_routes", methods)
		self.assertIn("create_route", methods)

		functions = _module_functions()
		for name in sorted(methods):
			self.assertIn(name, functions, f"recorridos.js calls {_MODULE_PATH}.{name}, which does not exist")
			self.assertIn(
				functions[name], frappe.whitelisted, f"recorridos.js calls {name}, which is not @frappe.whitelist()-ed"
			)

	# D.
	def test_no_private_function_is_whitelisted(self):
		exposed = sorted(
			name for name, fn in _module_functions().items() if name.startswith("_") and fn in frappe.whitelisted
		)
		self.assertEqual(exposed, [], f"private functions must never be @frappe.whitelist()-ed: {exposed}")

	# E.
	def _execute_cmd(self, method, **form_dict):
		"""Dispatch through frappe.handler.execute_cmd() -- the real RPC path
		that runs frappe.is_whitelisted() and the HTTP-method check -- with a
		minimal POST request stand-in, restoring the previous request/form_dict."""
		from frappe.handler import execute_cmd

		previous_request = getattr(frappe.local, "request", None)
		previous_form_dict = frappe.local.form_dict
		frappe.local.request = SimpleNamespace(method="POST")
		frappe.local.form_dict = frappe._dict(form_dict)
		try:
			return execute_cmd(f"{_MODULE_PATH}.{method}")
		finally:
			frappe.local.form_dict = previous_form_dict
			if previous_request is None:
				del frappe.local.request
			else:
				frappe.local.request = previous_request

	def test_get_routes_callable_through_rpc_handler(self):
		with fx.as_user(self.recorrido_user):
			result = self._execute_cmd(
				"get_routes", status='["Borrador", "Planificado", "En Ruta"]', start=0, page_length=20, txt=""
			)
		self.assertIn("routes", result)
		self.assertIn("total", result)

	def test_routes_matching_search_rejected_by_rpc_handler(self):
		with fx.as_user(self.recorrido_user):
			with self.assertRaises(frappe.PermissionError):
				self._execute_cmd("_routes_matching_search", txt="x", company="_Test Company")
