# -*- coding: utf-8 -*-
"""Commit 8 -- Section 5: regression guardrails.

Two kinds of check on purpose:
- static (read the source, look for forbidden patterns) so a future edit to
  api/bodega.py or api/jefe_bodega.py that reintroduces ignore_permissions,
  frappe.get_all, or a hand-rolled docstatus flip fails CI immediately, even
  before any behavioural test would catch it;
- live, exercising the real flow once more end-to-end to confirm
  per_picked/submit still go through ERPNext's own native mechanisms and not
  a reimplementation of them.
"""

import ast
import inspect
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega, jefe_bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

IGNORE_PERMISSIONS_PATTERN = re.compile(r"ignore_permissions\s*=\s*(True|1)\b")


class _CallCollector(ast.NodeVisitor):
	def __init__(self):
		self.dotted_calls = []

	def visit_Call(self, node):
		self.dotted_calls.append(self._dotted_name(node.func))
		self.generic_visit(node)

	@staticmethod
	def _dotted_name(node):
		parts = []
		while isinstance(node, ast.Attribute):
			parts.append(node.attr)
			node = node.value
		if isinstance(node, ast.Name):
			parts.append(node.id)
		return ".".join(reversed(parts))


def _dotted_calls_in(module) -> list[str]:
	tree = ast.parse(inspect.getsource(module))
	collector = _CallCollector()
	collector.visit(tree)
	return collector.dotted_calls


class TestStaticGuardrails(IntegrationTestCase):
	"""No Pick List/Reporte de Faltante data needed -- these read source code."""

	def test_bodega_api_never_sets_ignore_permissions_true(self):
		source = inspect.getsource(bodega)
		self.assertIsNone(
			IGNORE_PERMISSIONS_PATTERN.search(source),
			"api/bodega.py must never use ignore_permissions=True in production code",
		)

	def test_jefe_bodega_api_never_sets_ignore_permissions_true(self):
		source = inspect.getsource(jefe_bodega)
		self.assertIsNone(
			IGNORE_PERMISSIONS_PATTERN.search(source),
			"api/jefe_bodega.py must never use ignore_permissions=True in production code",
		)

	def test_jefe_bodega_api_never_calls_get_all(self):
		calls = _dotted_calls_in(jefe_bodega)
		self.assertNotIn(
			"frappe.get_all",
			calls,
			"api/jefe_bodega.py must never call frappe.get_all -- use frappe.get_list "
			"or get_doc()+check_permission() so permissions/User Permissions apply",
		)

	def test_finish_picking_uses_native_submit_not_a_manual_docstatus_flip(self):
		source = inspect.getsource(bodega.finish_picking)
		self.assertIn("pl.submit()", source)
		self.assertNotRegex(
			source,
			r"docstatus\s*=\s*1",
			"finish_picking() must submit the Pick List through Document.submit(), "
			"never by assigning docstatus directly",
		)


class TestFullFlowRegression(IntegrationTestCase):
	"""Live regression: the same full-pick flow as Section 2, focused
	specifically on confirming per_picked and Pick List submission still run
	through ERPNext's own native mechanisms end to end."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG8 Regression")
		cls.item = cls.world.item("FG8-REGRESSION-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Regression")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)
		cls.bodega_user = cls.world.user("fg8-bodega-regression@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)

	def test_per_picked_and_submit_go_through_native_erpnext_flow(self):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, 10, self.customer.name)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 0.0)
		pl = self.world.pick_list_for(so, self.wh.name)

		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			row = bodega.get_pick_list(pl.name)["rows"][0]
			bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)

		pl_doc = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl_doc.docstatus, 1)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_picked")), 100.0)
