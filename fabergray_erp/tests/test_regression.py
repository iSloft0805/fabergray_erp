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

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega, cotizaciones, jefe_bodega, ventas
from fabergray_erp.fulfillment import shortage_service
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


def _is_true_literal(node) -> bool:
	return isinstance(node, ast.Constant) and node.value in (True, 1)


def _hardcodes_ignore_permissions_true(module) -> bool:
	"""AST-based (not text/regex) so mentioning `ignore_permissions=True` in
	a docstring -- to document a parameterized exception elsewhere, exactly
	as api/bodega.py's own _insert_shortage_report() now deliberately does
	(Commit 18.1) -- can never produce a false positive. Only a literal
	`True`/`1` constant passed as `ignore_permissions=...` to a call, or
	assigned to a `.ignore_permissions` attribute, counts as a real
	violation. `ignore_permissions=via_fulfillment_engine` (a Name, not a
	Constant) does not match -- its value is controlled by the callee's own
	non-whitelisted, non-client-reachable logic, never hardcoded here."""
	tree = ast.parse(inspect.getsource(module))
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			for kw in node.keywords:
				if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
					return True
		elif isinstance(node, ast.Assign):
			for target in node.targets:
				if isinstance(target, ast.Attribute) and target.attr == "ignore_permissions" and _is_true_literal(
					node.value
				):
					return True
	return False


def _passes_via_fulfillment_engine_true(module) -> bool:
	"""AST-based check for the other half of the Commit 18.1 boundary:
	`via_fulfillment_engine=True` (a literal, hardcoded True) must never
	appear in an interactive API module -- only shortage_service.py's own
	internal call to _insert_shortage_report() may pass it, and only as a
	literal there because that call site *is* the Fulfillment Engine, not
	a caller of it."""
	tree = ast.parse(inspect.getsource(module))
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			for kw in node.keywords:
				if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
					return True
	return False


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


def _dotted_calls_in_tree(tree) -> list[str]:
	"""Same as `_dotted_calls_in()` above, but for an already-parsed AST
	(e.g. of a single function's source, via `ast.parse(inspect.getsource(fn))`)
	rather than a whole module -- Commit 18.5's guardrail checks individual
	`api/ventas.py` functions, not the whole module, so it needs this
	narrower entry point."""
	collector = _CallCollector()
	collector.visit(tree)
	return collector.dotted_calls


class TestStaticGuardrails(IntegrationTestCase):
	"""No Pick List/Reporte de Faltante data needed -- these read source code."""

	def test_bodega_api_never_sets_ignore_permissions_true(self):
		"""api/bodega.py may take a `via_fulfillment_engine` parameter on
		_insert_shortage_report() (Commit 18.1) that ultimately controls
		`ignore_permissions=via_fulfillment_engine` -- but never a
		hardcoded, literal `ignore_permissions=True` of its own."""
		self.assertFalse(
			_hardcodes_ignore_permissions_true(bodega),
			"api/bodega.py must never hardcode ignore_permissions=True in production code",
		)

	def test_jefe_bodega_api_never_sets_ignore_permissions_true(self):
		self.assertFalse(
			_hardcodes_ignore_permissions_true(jefe_bodega),
			"api/jefe_bodega.py must never hardcode ignore_permissions=True in production code",
		)

	def test_no_interactive_api_can_enable_fulfillment_bypass(self):
		"""Commit 18.1 guardrail #9: no interactive, whitelisted-function
		module may hardcode `ignore_permissions=True` or
		`via_fulfillment_engine=True` -- the only two knobs that unlock the
		Fulfillment Engine's internal permission bypass. Checked against
		api/bodega.py, api/jefe_bodega.py and (Commit 18.2) api/ventas.py --
		none of them ever reaches the bypass directly; only Sales
		Order.submit()'s own on_submit hook does."""
		for module in (bodega, jefe_bodega, ventas, cotizaciones):
			self.assertFalse(
				_hardcodes_ignore_permissions_true(module),
				f"{module.__name__} must never hardcode ignore_permissions=True",
			)
			self.assertFalse(
				_passes_via_fulfillment_engine_true(module),
				f"{module.__name__} must never hardcode via_fulfillment_engine=True",
			)

		# and no @frappe.whitelist()-decorated function anywhere accepts
		# either name as one of its own parameters, so a client could never
		# supply the value over HTTP even indirectly.
		for module in (bodega, jefe_bodega, ventas, cotizaciones):
			tree = ast.parse(inspect.getsource(module))
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef):
					continue
				is_whitelisted = any(
					(isinstance(d, ast.Call) and _CallCollector._dotted_name(d.func) == "frappe.whitelist")
					or (isinstance(d, ast.Attribute) and _CallCollector._dotted_name(d) == "frappe.whitelist")
					for d in node.decorator_list
				)
				if not is_whitelisted:
					continue
				param_names = {a.arg for a in node.args.args + node.args.kwonlyargs}
				self.assertNotIn("ignore_permissions", param_names, f"{module.__name__}.{node.name}")
				self.assertNotIn("via_fulfillment_engine", param_names, f"{module.__name__}.{node.name}")

	def test_jefe_bodega_api_never_calls_get_all(self):
		calls = _dotted_calls_in(jefe_bodega)
		self.assertNotIn(
			"frappe.get_all",
			calls,
			"api/jefe_bodega.py must never call frappe.get_all -- use frappe.get_list "
			"or get_doc()+check_permission() so permissions/User Permissions apply",
		)

	def test_ventas_api_never_calls_get_all_or_set_user(self):
		"""Commit 18.2 structural guardrail: api/ventas.py must always read
		through Vendedora's own, real, if_owner-restricted permissions --
		frappe.get_all() (which forces ignore_permissions=True internally,
		see frappe/__init__.py) and frappe.set_user() (which would swap her
		out of her own session) are exactly the two mechanisms that could
		silently defeat that. Checked by AST, same as the other guardrails
		in this file, so mentioning either name in a docstring/comment can
		never produce a false positive."""
		calls = _dotted_calls_in(ventas)
		self.assertNotIn(
			"frappe.get_all",
			calls,
			"api/ventas.py must never call frappe.get_all -- use frappe.get_list so "
			"if_owner and Role Permissions are actually applied",
		)
		self.assertNotIn(
			"frappe.set_user",
			calls,
			"api/ventas.py must never call frappe.set_user -- Vendedora's own session "
			"must be used for every read and write",
		)

	def test_cotizaciones_api_never_calls_get_all_or_set_user(self):
		"""Commit 20.2 structural guardrail, same reasoning as
		`test_ventas_api_never_calls_get_all_or_set_user` above: api/
		cotizaciones.py must always read through Vendedora's own, real,
		if_owner-restricted Quotation permission (Commit 20.1) --
		frappe.get_all() and frappe.set_user() are exactly the two
		mechanisms that could silently defeat that."""
		calls = _dotted_calls_in(cotizaciones)
		self.assertNotIn(
			"frappe.get_all",
			calls,
			"api/cotizaciones.py must never call frappe.get_all -- use frappe.get_list so "
			"if_owner and Role Permissions are actually applied",
		)
		self.assertNotIn(
			"frappe.set_user",
			calls,
			"api/cotizaciones.py must never call frappe.set_user -- Vendedora's own session "
			"must be used for every read and write",
		)

	def test_commit_20_2_cotizaciones_endpoints_never_leak_economic_data_or_bypass_permissions(self):
		"""Commit 20.2 guardrail, same shape as the Commit 18.4/18.5 guardrails
		above: get_item_info()/get_quotation_summary()/get_my_quotations()/
		get_quotation_detail() must never return an economic field and must
		never gain a bypass (no `.as_dict()`, no hardcoded
		`ignore_permissions=True`, no `frappe.get_all`, no `frappe.set_user`)
		-- checked statically so a future edit that starts forwarding
		`rate`/`amount`/`grand_total`/etc. fails here immediately, before any
		behavioural test would catch it.
		"""
		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
		}
		functions_with_own_return_dict = [
			cotizaciones.get_item_info,
			cotizaciones.get_quotation_summary,
			cotizaciones.get_my_quotations,
			cotizaciones.get_quotation_detail,
		]

		for fn in functions_with_own_return_dict:
			source = inspect.getsource(fn)
			tree = ast.parse(source)

			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr == "as_dict":
					self.fail(f"{fn.__name__}() must never call .as_dict() -- builds/forwards a safe dict only")

			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					for kw in node.keywords:
						if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never hardcode ignore_permissions=True")
						if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never pass via_fulfillment_engine=True")

			calls = _dotted_calls_in_tree(tree)
			self.assertNotIn("frappe.get_all", calls, f"{fn.__name__}() must never call frappe.get_all")
			self.assertNotIn("frappe.set_user", calls, f"{fn.__name__}() must never call frappe.set_user")

			found_keys = set()
			for node in ast.walk(tree):
				if isinstance(node, ast.Dict):
					for key in node.keys:
						if isinstance(key, ast.Constant) and isinstance(key.value, str):
							found_keys.add(key.value)
			self.assertTrue(found_keys, f"expected at least one dict literal key in {fn.__name__}()")
			self.assertFalse(
				economic_keys & found_keys, f"{fn.__name__}() returns economic key(s): {economic_keys & found_keys}"
			)

	def test_commit_20_3_create_and_submit_quotation_never_leaks_or_accepts_economic_fields(self):
		"""Commit 20.3 guardrail: create_and_submit_quotation() and its
		helper _validate_and_build_quotation_item_rows() must never leak an
		economic field (no `.as_dict()`, response allowlisted), must never
		gain a bypass (no `frappe.get_all`/`frappe.set_user`/hardcoded
		`ignore_permissions=True`/`via_fulfillment_engine=True`/`db_set`/
		`frappe.db.set_value` used to skip a validation), and must keep
		actually enforcing `_ALLOWED_ITEM_FIELDS == {"item_code", "qty"}` --
		both that the constant itself hasn't silently widened, and that the
		validator function still references it (proving the allowlist is
		wired into the rejection logic, not just declared and unused).
		"""
		self.assertEqual(
			cotizaciones._ALLOWED_ITEM_FIELDS,
			{"item_code", "qty"},
			"cotizaciones._ALLOWED_ITEM_FIELDS must stay exactly {item_code, qty}",
		)

		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
			"currency",
			"conversion_rate",
		}

		functions = [cotizaciones.create_and_submit_quotation, cotizaciones._validate_and_build_quotation_item_rows]

		for fn in functions:
			source = inspect.getsource(fn)
			tree = ast.parse(source)

			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr == "as_dict":
					self.fail(f"{fn.__name__}() must never call .as_dict() -- builds/forwards a safe dict only")
				if isinstance(node, ast.Attribute) and node.attr == "db_set":
					self.fail(f"{fn.__name__}() must never call .db_set() -- never skip a native validation")

			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					for kw in node.keywords:
						if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never hardcode ignore_permissions=True")
						if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never pass via_fulfillment_engine=True")

			calls = _dotted_calls_in_tree(tree)
			self.assertNotIn("frappe.get_all", calls, f"{fn.__name__}() must never call frappe.get_all")
			self.assertNotIn("frappe.set_user", calls, f"{fn.__name__}() must never call frappe.set_user")
			self.assertNotIn(
				"frappe.db.set_value", calls, f"{fn.__name__}() must never call frappe.db.set_value"
			)

		# create_and_submit_quotation()'s own RETURN dict must be built from
		# an allowlisted, non-economic set of keys only -- scoped to the
		# `return {...}` statement specifically, not the whole function body
		# (which also contains the frappe.get_doc({"doctype": "Quotation",
		# ...}) construction dict, a different, legitimately-broader set of
		# keys that must not be confused with the response).
		tree = ast.parse(inspect.getsource(cotizaciones.create_and_submit_quotation))
		return_dicts = [
			node.value
			for node in ast.walk(tree)
			if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
		]
		self.assertEqual(len(return_dicts), 1, "expected exactly one return {...} statement")
		found_keys = set()
		for key in return_dicts[0].keys:
			if isinstance(key, ast.Constant) and isinstance(key.value, str):
				found_keys.add(key.value)
		allowed_keys = {
			"name",
			"status",
			"customer",
			"customer_name",
			"transaction_date",
			"valid_till",
			"item_count",
			"total_qty",
		}
		self.assertTrue(found_keys, "expected at least one dict literal key in create_and_submit_quotation()")
		self.assertTrue(
			found_keys.issubset(allowed_keys),
			f"create_and_submit_quotation() returns unexpected key(s): {found_keys - allowed_keys}",
		)
		self.assertFalse(economic_keys & found_keys)

		# _validate_and_build_quotation_item_rows() must actually reference
		# _ALLOWED_ITEM_FIELDS -- proves the allowlist above is wired into
		# the real rejection logic, not merely declared and unused.
		validator_tree = ast.parse(inspect.getsource(cotizaciones._validate_and_build_quotation_item_rows))
		referenced = any(
			isinstance(node, ast.Name) and node.id == "_ALLOWED_ITEM_FIELDS" for node in ast.walk(validator_tree)
		)
		self.assertTrue(
			referenced,
			"_validate_and_build_quotation_item_rows() must reference _ALLOWED_ITEM_FIELDS to actually enforce it",
		)

	def test_commit_20_6_update_draft_quotation_never_leaks_or_bypasses(self):
		"""Commit 20.6 guardrail, same shape as the Commit 20.3 one above:
		get_editable_quotation()/update_draft_quotation() must never leak
		an economic field (no `.as_dict()`), must never gain a bypass (no
		`frappe.get_all`/`frappe.set_user`/hardcoded
		`ignore_permissions=True`/`via_fulfillment_engine=True`/`db_set`/
		`frappe.db.set_value` used to skip a validation), and
		update_draft_quotation()'s own response must stay a minimal,
		non-economic `{"name": ...}` -- proven by scoping the check to its
		actual `Return` statement, not the whole function body (which also
		contains the `qtn.set("items", [])`/`.append()` loop -- not a dict
		literal, but scoped the same defensive way the Commit 20.3 guardrail
		had to be fixed to do, after that exact class of bug was caught
		there)."""
		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
			"currency",
			"conversion_rate",
		}

		functions = [cotizaciones.get_editable_quotation, cotizaciones.update_draft_quotation]
		for fn in functions:
			tree = ast.parse(inspect.getsource(fn))

			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr == "as_dict":
					self.fail(f"{fn.__name__}() must never call .as_dict() -- builds/forwards a safe dict only")
				if isinstance(node, ast.Attribute) and node.attr == "db_set":
					self.fail(f"{fn.__name__}() must never call .db_set() -- never skip a native validation")

			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					for kw in node.keywords:
						if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never hardcode ignore_permissions=True")
						if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never pass via_fulfillment_engine=True")

			calls = _dotted_calls_in_tree(tree)
			self.assertNotIn("frappe.get_all", calls, f"{fn.__name__}() must never call frappe.get_all")
			self.assertNotIn("frappe.set_user", calls, f"{fn.__name__}() must never call frappe.set_user")
			self.assertNotIn(
				"frappe.db.set_value", calls, f"{fn.__name__}() must never call frappe.db.set_value"
			)

		# update_draft_quotation()'s own RETURN dict must be built from an
		# allowlisted, non-economic set of keys only -- scoped to the
		# `return {...}` statement specifically (Commit 20.3's own fix
		# applied from the start here, not discovered again the hard way).
		tree = ast.parse(inspect.getsource(cotizaciones.update_draft_quotation))
		return_dicts = [
			node.value
			for node in ast.walk(tree)
			if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
		]
		self.assertEqual(len(return_dicts), 1, "expected exactly one return {...} statement")
		found_keys = set()
		for key in return_dicts[0].keys:
			if isinstance(key, ast.Constant) and isinstance(key.value, str):
				found_keys.add(key.value)
		self.assertEqual(found_keys, {"name"})
		self.assertFalse(economic_keys & found_keys)

		# get_editable_quotation() must never build its own return dict --
		# it must forward get_quotation_detail()'s own already-guardrailed
		# response verbatim, not construct a second, parallel shape.
		editable_tree = ast.parse(inspect.getsource(cotizaciones.get_editable_quotation))
		editable_dict_literals = [node for node in ast.walk(editable_tree) if isinstance(node, ast.Dict)]
		self.assertEqual(
			editable_dict_literals,
			[],
			"get_editable_quotation() must never build its own dict literal -- "
			"it must return get_quotation_detail()'s response as-is",
		)

	def test_get_order_detail_never_calls_as_dict_and_only_returns_allowlisted_keys(self):
		"""Commit 18.4 guardrail: get_order_detail() must build its response
		dict field by field (never `so.as_dict()`/`row.as_dict()`, both of
		which carry every economic field on the document) and must never
		gain a dict-literal key outside this fixed allowlist -- so a future
		edit that starts forwarding `rate`/`amount`/`grand_total`/etc. fails
		here immediately, before any behavioural test would catch it."""
		source = inspect.getsource(ventas.get_order_detail)
		tree = ast.parse(source)

		for node in ast.walk(tree):
			if isinstance(node, ast.Attribute) and node.attr == "as_dict":
				self.fail(
					"get_order_detail() must build its response dict field by field, "
					"never via so.as_dict()/row.as_dict()"
				)

		allowed_keys = {
			"name",
			"commercial_name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"status",
			"item_count",
			"total_qty",
			"observations",
			"items",
			"item_code",
			"item_name",
			"qty",
			"stock_uom",
		}
		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
		}

		found_keys = set()
		for node in ast.walk(tree):
			if isinstance(node, ast.Dict):
				for key in node.keys:
					if isinstance(key, ast.Constant) and isinstance(key.value, str):
						found_keys.add(key.value)

		self.assertTrue(found_keys, "expected at least one dict literal key in get_order_detail()")
		self.assertTrue(
			found_keys.issubset(allowed_keys),
			f"get_order_detail() returns unexpected key(s): {found_keys - allowed_keys}",
		)
		self.assertFalse(economic_keys & found_keys)

	def test_commit_18_5_endpoints_never_leak_economic_data_or_bypass_permissions(self):
		"""Commit 18.5 guardrail: get_editable_order()/update_draft_sales_
		order()/delete_draft_sales_order()/cancel_sales_order() must never
		return an economic field and must never gain a bypass (no
		`.as_dict()`, no hardcoded `ignore_permissions=True`, no
		`frappe.get_all`, no `frappe.set_user`) -- checked statically so a
		future edit that starts forwarding `rate`/`amount`/`grand_total`/
		etc., or that reaches for a bypass, fails here immediately, before
		any behavioural test would catch it. `test_ventas_api_never_calls_
		get_all_or_set_user`/`test_no_interactive_api_can_enable_
		fulfillment_bypass` already scan the whole `ventas` module
		(these four functions included) for `get_all`/`set_user`/
		`ignore_permissions=True`/`via_fulfillment_engine=True` -- this
		test adds the return-shape/`.as_dict()` check those two do not
		cover, and re-confirms the bypass check narrowly scoped to just
		these four functions for a precise failure message.
		"""
		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
		}
		# get_editable_order() returns get_order_detail()'s own dict verbatim
		# (already covered by the guardrail above) -- its own body has no
		# dict literal of its own, only "name"/"status" pass through docstatus
		# checks below, so it is included here only for the bypass half of
		# this test, not the key-allowlist half.
		functions_with_own_return_dict = [ventas.update_draft_sales_order, ventas.delete_draft_sales_order, ventas.cancel_sales_order]
		all_four = [ventas.get_editable_order] + functions_with_own_return_dict

		for fn in all_four:
			source = inspect.getsource(fn)
			tree = ast.parse(source)

			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr == "as_dict":
					self.fail(f"{fn.__name__}() must never call .as_dict() -- builds/forwards a safe dict only")

			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					for kw in node.keywords:
						if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never hardcode ignore_permissions=True")
						if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never pass via_fulfillment_engine=True")

			calls = _dotted_calls_in_tree(tree)
			self.assertNotIn("frappe.get_all", calls, f"{fn.__name__}() must never call frappe.get_all")
			self.assertNotIn("frappe.set_user", calls, f"{fn.__name__}() must never call frappe.set_user")

		for fn in functions_with_own_return_dict:
			tree = ast.parse(inspect.getsource(fn))
			found_keys = set()
			for node in ast.walk(tree):
				if isinstance(node, ast.Dict):
					for key in node.keys:
						if isinstance(key, ast.Constant) and isinstance(key.value, str):
							found_keys.add(key.value)
			self.assertTrue(found_keys, f"expected at least one dict literal key in {fn.__name__}()")
			self.assertFalse(
				economic_keys & found_keys, f"{fn.__name__}() returns economic key(s): {economic_keys & found_keys}"
			)

		# cancel_sales_order() must trigger cleanup only via so.cancel()'s own
		# hooks -- never by calling a Fulfillment Engine internal directly
		# (the same standing rule create_and_submit_sales_order() already
		# follows for submit -- Commit 16's "don't call the Engine directly
		# if submit/cancel already does").
		cancel_calls = _dotted_calls_in_tree(ast.parse(inspect.getsource(ventas.cancel_sales_order)))
		for forbidden in (
			"process_sales_order",
			"sync_shortage_reports_for_sales_order",
			"sync_material_requests_for_sales_order",
			"cleanup_fulfillment_for_cancelled_sales_order",
		):
			self.assertNotIn(
				forbidden,
				cancel_calls,
				f"cancel_sales_order() must not call {forbidden}() directly -- so.cancel()'s own hooks must do it",
			)

	def test_commit_18_5b_modification_endpoints_never_leak_economic_data_or_bypass_permissions(self):
		"""Commit 18.5b guardrail: get_modification_status()/get_order_for_
		modification()/modify_submitted_sales_order() must never return an
		economic field and must never gain a bypass (no `.as_dict()`, no
		hardcoded `ignore_permissions=True`, no `frappe.get_all`, no
		`frappe.set_user`). Same shape as
		`test_commit_18_5_endpoints_never_leak_economic_data_or_bypass_
		permissions` above, applied to the three new functions.
		"""
		economic_keys = {
			"rate",
			"price_list_rate",
			"amount",
			"net_rate",
			"net_amount",
			"base_rate",
			"base_amount",
			"total",
			"grand_total",
			"net_total",
			"base_grand_total",
			"base_net_total",
			"discount_percentage",
			"discount_amount",
			"taxes",
			"margin_rate_or_amount",
		}
		functions_with_own_return_dict = [ventas.get_modification_status, ventas.modify_submitted_sales_order]
		all_three = functions_with_own_return_dict + [ventas.get_order_for_modification]

		for fn in all_three:
			source = inspect.getsource(fn)
			tree = ast.parse(source)

			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr == "as_dict":
					self.fail(f"{fn.__name__}() must never call .as_dict() -- builds/forwards a safe dict only")

			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					for kw in node.keywords:
						if kw.arg == "ignore_permissions" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never hardcode ignore_permissions=True")
						if kw.arg == "via_fulfillment_engine" and _is_true_literal(kw.value):
							self.fail(f"{fn.__name__}() must never pass via_fulfillment_engine=True")

			calls = _dotted_calls_in_tree(tree)
			self.assertNotIn("frappe.get_all", calls, f"{fn.__name__}() must never call frappe.get_all")
			self.assertNotIn("frappe.set_user", calls, f"{fn.__name__}() must never call frappe.set_user")

		for fn in functions_with_own_return_dict:
			tree = ast.parse(inspect.getsource(fn))
			found_keys = set()
			for node in ast.walk(tree):
				if isinstance(node, ast.Dict):
					for key in node.keys:
						if isinstance(key, ast.Constant) and isinstance(key.value, str):
							found_keys.add(key.value)
			self.assertTrue(found_keys, f"expected at least one dict literal key in {fn.__name__}()")
			self.assertFalse(
				economic_keys & found_keys, f"{fn.__name__}() returns economic key(s): {economic_keys & found_keys}"
			)

		# modify_submitted_sales_order() must trigger cleanup/re-processing
		# only via so.cancel()/amended.submit()'s own native hooks -- never
		# by calling a Fulfillment Engine internal directly (same standing
		# rule as create_and_submit_sales_order()/cancel_sales_order()).
		modify_calls = _dotted_calls_in_tree(ast.parse(inspect.getsource(ventas.modify_submitted_sales_order)))
		for forbidden in (
			"process_sales_order",
			"sync_shortage_reports_for_sales_order",
			"sync_material_requests_for_sales_order",
			"cleanup_fulfillment_for_cancelled_sales_order",
		):
			self.assertNotIn(
				forbidden,
				modify_calls,
				f"modify_submitted_sales_order() must not call {forbidden}() directly -- "
				"so.cancel()/amended.submit()'s own hooks must do it",
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

	def test_only_one_reporte_de_faltante_insert_path_exists(self):
		"""Commit 9: _insert_shortage_report() must be the only place in
		api/bodega.py that builds a Reporte de Faltante doc for .insert() --
		_create_shortage_report() (and any future Fulfillment Engine adapter)
		must derive fields and delegate to it, never insert a second, parallel
		copy of this doctype. AST-based (not a text/regex search) so mentioning
		this exact dict shape in a docstring -- as _insert_shortage_report()'s
		own docstring deliberately does, to spell out the rule -- can never
		produce a false positive here.
		"""
		tree = ast.parse(inspect.getsource(bodega))
		matches = []
		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and _CallCollector._dotted_name(node.func) == "frappe.get_doc"):
				continue
			if not node.args or not isinstance(node.args[0], ast.Dict):
				continue
			for key, value in zip(node.args[0].keys, node.args[0].values, strict=False):
				if (
					isinstance(key, ast.Constant)
					and key.value == "doctype"
					and isinstance(value, ast.Constant)
					and value.value == "Reporte de Faltante"
				):
					matches.append(node.lineno)

		self.assertEqual(
			len(matches),
			1,
			f"Found {len(matches)} frappe.get_doc({{'doctype': 'Reporte de Faltante', ...}}) call(s) "
			f"in api/bodega.py at line(s) {matches} -- there must be exactly one, inside "
			"_insert_shortage_report()",
		)

	def test_shortage_service_never_inserts_reporte_de_faltante_directly(self):
		"""Commit 14: sync_shortage_reports_for_sales_order() must go through
		_insert_shortage_report() (Commit 9's one approved insert path) or a
		plain .save() on an existing report it already found -- never build a
		second, parallel Reporte de Faltante doc of its own. Same AST check as
		the bodega.py guardrail above, applied to the new module, expecting
		zero matches here instead of exactly one."""
		tree = ast.parse(inspect.getsource(shortage_service))
		matches = []
		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and _CallCollector._dotted_name(node.func) == "frappe.get_doc"):
				continue
			if not node.args or not isinstance(node.args[0], ast.Dict):
				continue
			for key, value in zip(node.args[0].keys, node.args[0].values, strict=False):
				if (
					isinstance(key, ast.Constant)
					and key.value == "doctype"
					and isinstance(value, ast.Constant)
					and value.value == "Reporte de Faltante"
				):
					matches.append(node.lineno)

		self.assertEqual(
			matches,
			[],
			f"fulfillment/shortage_service.py must never build a Reporte de Faltante doc "
			f"directly -- found at line(s) {matches}",
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
