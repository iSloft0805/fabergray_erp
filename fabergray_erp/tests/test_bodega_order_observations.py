# -*- coding: utf-8 -*-
"""Hotfix 25.26.2 -- "OBSERVACIONES DEL PEDIDO" en Bodega (alistamiento) y
en Jefe de Bodega (VER DETALLE).

`Sales Order.fg_observations` sigue siendo la ÚNICA fuente (Hotfix 25.26.1).
`bodega.get_pick_list()` -- el endpoint que ya alimenta ambas vistas -- solo
AGREGA `order_observations`: una entrada por cada Sales Order distinto del
Pick List (orden de primera aparición), solo con texto real, solo de la
misma Company. Todo lo demás de Bodega (start/set/report/finish, stock,
estados) queda exactamente igual, y esta suite lo comprueba.
"""

import ast
import inspect
import os
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import bodega
from fabergray_erp.sales_order_naming import root_commercial_name
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_PAGE_DIR = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page")
_BODEGA_JS = os.path.join(_PAGE_DIR, "bodega", "bodega.js")
_BODEGA_CSS = os.path.join(_PAGE_DIR, "bodega", "bodega.css")
_JEFE_JS = os.path.join(_PAGE_DIR, "jefe_pick_lists", "jefe_pick_lists.js")
_JEFE_CSS = os.path.join(_PAGE_DIR, "jefe_pick_lists", "jefe_pick_lists.css")

MULTILINE = "Entregar después de las 2:00 p. m.\nCliente solicita 2 galones sin fragancia.\nEmpacar las escobas por separado."
SPECIAL = "Ñandú & Cía. — «urgente» 50% \"comillas\" áéíóú"
JINJA = "Revisar {{ 7*7 }} y {% if 1 %}X{% endif %}"
MALICIOUS = '<script>alert("x")</script><img src=x onerror="alert(1)"> texto'


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


def _method_body(source, method_name):
	"""Same helper as test_cotizaciones_billing_review.py's own."""
	m = re.search(r"\n\t" + re.escape(method_name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"method {method_name!r} not found")
	start = m.end()
	next_method = re.search(r"\n\t[a-zA-Z_]\w*\([^)]*\)\s*\{", source[start:])
	end = start + next_method.start() if next_method else len(source)
	return source[start:end]


def _function_body(source, name):
	m = re.search(r"\nfunction " + re.escape(name) + r"\([^)]*\)\s*\{", source)
	if not m:
		raise AssertionError(f"function {name!r} not found")
	start = m.end()
	nxt = re.search(r"\n(function |const )[a-zA-Z_]", source[start:])
	return source[start : start + nxt.start() if nxt else len(source)]


def _css_rule(css, selector):
	m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
	if not m:
		raise AssertionError(f"CSS rule {selector!r} not found")
	return m.group(1)


class _ObservationsBodegaWorld(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FG25262 {sfx} WH")
		cls.item_a = cls.world.item(f"FG25262-{sfx}-A")
		cls.item_b = cls.world.item(f"FG25262-{sfx}-B")
		cls.customer = cls.world.customer(f"FG25262 {sfx} Cliente")
		cls.world.stock_up(cls.item_a.name, cls.wh.name, 1000)
		cls.world.stock_up(cls.item_b.name, cls.wh.name, 1000)

		cls.bodega_user = cls.world.user(f"fg25262-{sfx}-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.jefe_user = cls.world.user(f"fg25262-{sfx}-jefe@example.com", ["Jefe de Bodega"])
		cls.world.warehouse_user_permission(cls.jefe_user, cls.wh.name)
		cls.vendedora_user = cls.world.user(f"fg25262-{sfx}-vendedora@example.com", ["Vendedora"])

	def _sales_order(self, observations=None, items=None):
		"""fg_observations has no allow_on_submit -> set before submit()."""
		delivery_date = add_days(nowdate(), 7)
		items = items or [(self.item_a.name, 5), (self.item_b.name, 3)]
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": self.wh.name,
				"items": [
					{"item_code": code, "warehouse": self.wh.name, "qty": qty, "rate": 100, "delivery_date": delivery_date}
					for code, qty in items
				],
			}
		)
		if observations is not None:
			doc.fg_observations = observations
		doc.insert()
		with fx.without_sales_order_hook():
			doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		self.world.track_existing("Sales Order", doc.name)
		return doc

	def _pick_list(self, observations=None, items=None):
		so = self._sales_order(observations, items)
		return so, self.world.pick_list_for(so, self.wh.name)

	def _manual_pick_list(self, rows):
		"""A Pick List spanning several Sales Orders -- impossible through
		this app's own Fulfillment Engine (one Pick List per Sales Order)
		but possible from native Desk; built the same way
		test_facturacion_flow.py's own multi-SO guardrail builds it.
		rows: [(sales_order_doc, item_index)]."""
		pl = frappe.get_doc(
			{
				"doctype": "Pick List",
				"company": fx.COMPANY,
				"purpose": "Delivery",
				"parent_warehouse": self.wh.name,
				"locations": [
					{
						"item_code": so.items[idx].item_code,
						"warehouse": self.wh.name,
						"qty": so.items[idx].qty,
						"stock_qty": so.items[idx].qty,
						"conversion_factor": 1,
						"sales_order": so.name,
						"sales_order_item": so.items[idx].name,
					}
					for so, idx in rows
				],
			}
		)
		pl.insert()
		self.world.track_existing("Pick List", pl.name)
		return pl

	def _obs(self, pl_name, user=None):
		with fx.as_user(user or self.bodega_user):
			return bodega.get_pick_list(pl_name)["order_observations"]


class TestGetPickListObservations(_ObservationsBodegaWorld):
	def test_one_order_one_observation(self):
		so, pl = self._pick_list(MULTILINE)
		self.assertEqual(
			self._obs(pl.name),
			[{"sales_order": so.name, "commercial_name": root_commercial_name(so.name), "text": MULTILINE}],
		)

	def test_no_observation_returns_empty_list(self):
		_, pl = self._pick_list(None)
		self.assertEqual(self._obs(pl.name), [])

	def test_whitespace_only_returns_empty_list(self):
		_, pl = self._pick_list("   \n\t   ")
		self.assertEqual(self._obs(pl.name), [])

	def test_order_a_never_shows_in_order_b(self):
		_, pl_a = self._pick_list("Solo para A")
		_, pl_b = self._pick_list("Solo para B")
		self.assertEqual([o["text"] for o in self._obs(pl_a.name)], ["Solo para A"])
		self.assertEqual([o["text"] for o in self._obs(pl_b.name)], ["Solo para B"])

	def test_multiline_preserved(self):
		_, pl = self._pick_list(MULTILINE)
		self.assertEqual(self._obs(pl.name)[0]["text"].split("\n"), MULTILINE.split("\n"))

	def test_trim_only_at_the_ends(self):
		_, pl = self._pick_list("\n  Línea 1\n\n  Línea 3  \n")
		self.assertEqual(self._obs(pl.name)[0]["text"], "Línea 1\n\n  Línea 3")

	def test_long_text_returned_whole(self):
		long_text = "Instrucción operativa muy larga para el alistamiento. " * 120
		_, pl = self._pick_list(long_text)
		self.assertEqual(self._obs(pl.name)[0]["text"], long_text.strip())

	def test_special_characters(self):
		_, pl = self._pick_list(SPECIAL)
		self.assertEqual(self._obs(pl.name)[0]["text"], SPECIAL)

	def test_jinja_stays_literal(self):
		_, pl = self._pick_list(JINJA)
		self.assertEqual(self._obs(pl.name)[0]["text"], JINJA)

	def test_malicious_html_is_already_sanitized_at_rest(self):
		"""Layer 1 (Frappe, on save) strips <script>/handlers; layer 2 is the
		page's own escape (TestBodegaUiContract)."""
		_, pl = self._pick_list(MALICIOUS)
		text = self._obs(pl.name)[0]["text"]
		self.assertNotIn("<script", text)
		self.assertNotIn("onerror", text)

	def test_several_lines_of_same_order_do_not_duplicate(self):
		_, pl = self._pick_list(MULTILINE, items=[(self.item_a.name, 1), (self.item_b.name, 2), (self.item_a.name, 3)])
		self.assertGreaterEqual(len(pl.locations), 2)
		self.assertEqual(len(self._obs(pl.name)), 1)

	def test_two_sales_orders_each_labelled_in_first_appearance_order(self):
		so_1 = self._sales_order("Observación del pedido 1")
		so_2 = self._sales_order("Observación del pedido 2")
		pl = self._manual_pick_list([(so_2, 0), (so_1, 0), (so_2, 1)])
		self.assertEqual(
			self._obs(pl.name),
			[
				{"sales_order": so_2.name, "commercial_name": root_commercial_name(so_2.name), "text": "Observación del pedido 2"},
				{"sales_order": so_1.name, "commercial_name": root_commercial_name(so_1.name), "text": "Observación del pedido 1"},
			],
		)

	def test_order_without_observation_is_omitted_among_several(self):
		so_1 = self._sales_order(None)
		so_2 = self._sales_order("Solo el segundo tiene")
		pl = self._manual_pick_list([(so_1, 0), (so_2, 0)])
		self.assertEqual([o["sales_order"] for o in self._obs(pl.name)], [so_2.name])

	def test_resolved_with_one_query_not_per_row(self):
		_, pl = self._pick_list(MULTILINE, items=[(self.item_a.name, 1), (self.item_b.name, 2)])
		original = frappe.db.get_values
		calls = []

		def spy(doctype, *args, **kwargs):
			# frappe.db.get_value() is itself built on get_values(); count only
			# the observation lookup (the one asking for fg_observations).
			fields = args[1] if len(args) > 1 else kwargs.get("fieldname")
			if doctype == "Sales Order" and "fg_observations" in (fields or []):
				calls.append(args)
			return original(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_values", side_effect=spy):
			self._obs(pl.name)
		self.assertEqual(len(calls), 1)

	def test_sales_order_of_another_company_is_never_exposed(self):
		so, pl = self._pick_list("No debe salir")
		original = frappe.db.get_values

		def other_company(doctype, *args, **kwargs):
			rows = original(doctype, *args, **kwargs)
			fields = args[1] if len(args) > 1 else kwargs.get("fieldname")
			if doctype == "Sales Order" and "fg_observations" in (fields or []):
				for r in rows:
					r.company = "Otra Company"
			return rows

		with patch.object(frappe.db, "get_values", side_effect=other_company):
			self.assertEqual(self._obs(pl.name), [])

	def test_jefe_de_bodega_sees_observations_through_same_endpoint(self):
		so, pl = self._pick_list(MULTILINE)
		self.assertEqual(self._obs(pl.name, user=self.jefe_user)[0]["text"], MULTILINE)

	def test_jefe_without_observation_gets_empty_list(self):
		_, pl = self._pick_list(None)
		self.assertEqual(self._obs(pl.name, user=self.jefe_user), [])

	def test_role_without_pick_list_access_still_denied(self):
		_, pl = self._pick_list(MULTILINE)
		with fx.as_user(self.vendedora_user):
			with self.assertRaises(frappe.PermissionError):
				bodega.get_pick_list(pl.name)

	def test_existing_response_keys_unchanged_plus_new_one(self):
		_, pl = self._pick_list(MULTILINE)
		with fx.as_user(self.bodega_user):
			detail = bodega.get_pick_list(pl.name)
		self.assertEqual(
			set(detail.keys()),
			{
				"name",
				"docstatus",
				"status",
				"purpose",
				"parent_warehouse",
				"warehouses",  # Fase 28.4A.3 -- line warehouses of a multi-warehouse order
				"customer",
				"sales_order",
				"commercial_name",
				"fg_started_by",
				"fg_started_on",
				"order_observations",
				"rows",
			},
		)


class TestReadOnlyAndPickingUnchanged(_ObservationsBodegaWorld):
	def test_reading_modifies_nothing(self):
		so, pl = self._pick_list(MULTILINE)
		so_modified = frappe.db.get_value("Sales Order", so.name, "modified")
		pl_modified = frappe.db.get_value("Pick List", pl.name, "modified")
		bins = frappe.db.sql(
			"select item_code, actual_qty, reserved_qty from tabBin where warehouse=%s order by item_code", self.wh.name
		)
		for _ in range(3):
			self._obs(pl.name)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "modified"), so_modified)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "fg_observations"), MULTILINE)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "modified"), pl_modified)
		self.assertEqual(
			frappe.db.sql(
				"select item_code, actual_qty, reserved_qty from tabBin where warehouse=%s order by item_code", self.wh.name
			),
			bins,
		)

	def test_full_picking_flow_unchanged_with_observations(self):
		_, pl = self._pick_list(MULTILINE, items=[(self.item_a.name, 10), (self.item_b.name, 10)])
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			rows = bodega.get_pick_list(pl.name)["rows"]
			bodega.set_picked_qty(pl.name, rows[0]["row_name"], rows[0]["qty_solicitada"])
			bodega.set_picked_qty(pl.name, rows[1]["row_name"], 4)
			report = bodega.report_shortage(
				pick_list=pl.name,
				row_name=rows[1]["row_name"],
				qty_disponible=4,
				shortage_reason="Stock insuficiente",
			)
			self.world.track_existing("Reporte de Faltante", report["name"])
			result = bodega.finish_picking(pl.name)
			after = bodega.get_pick_list(pl.name)
		self.assertEqual(result["docstatus"], 1)
		self.assertEqual(after["order_observations"][0]["text"], MULTILINE)
		self.assertEqual([r["qty_alistada"] for r in after["rows"]], [10, 4])

	def test_bodega_module_never_writes_fg_observations(self):
		tree = ast.parse(inspect.getsource(bodega))
		for node in ast.walk(tree):
			if isinstance(node, ast.Assign):
				for target in node.targets:
					if isinstance(target, ast.Attribute):
						self.assertNotEqual(target.attr, "fg_observations")
		source = inspect.getsource(bodega)
		self.assertNotRegex(source, r"set_value\([^)]*fg_observations")
		self.assertNotRegex(source, r"db_set\([^)]*fg_observations")

	def test_no_new_whitelisted_endpoint(self):
		whitelisted = sorted(
			node.name
			for node in ast.walk(ast.parse(inspect.getsource(bodega)))
			if isinstance(node, ast.FunctionDef)
			and any("whitelist" in ast.unparse(d) for d in node.decorator_list)
		)
		self.assertEqual(
			whitelisted,
			sorted(
				[
					"get_queue",
					"get_pick_list",
					"start_picking",
					"set_picked_qty",
					"report_shortage",
					"finish_picking",
					"get_shortages",
					"get_inventory",
				]
			),
		)

	def test_permissions_unchanged(self):
		for role in ("Bodega", "Jefe de Bodega"):
			perms = frappe.db.get_value(
				"Custom DocPerm",
				{"parent": "Sales Order", "role": role, "permlevel": 0},
				["read", "write", "create", "submit", "cancel"],
				as_dict=True,
			)
			self.assertEqual((perms.read, perms.write, perms.create, perms.submit, perms.cancel), (1, 0, 0, 0, 0), role)


class TestBodegaUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_BODEGA_JS)
		cls.css = _read(_BODEGA_CSS)
		cls.detail = _method_body(cls.js, "render_detail_html")
		cls.helper = _function_body(cls.js, "render_order_observations_html")

	def test_block_between_detail_top_and_progress_card(self):
		i_top = self.detail.index('<div class="fg-detail-top">')
		i_obs = self.detail.index("${render_order_observations_html(detail.order_observations)}")
		i_progress = self.detail.index('<div class="fg-progress-card">')
		i_items = self.detail.index('<div class="fg-item-cards">')
		i_finish = self.detail.index("fg-finish-bar")
		self.assertTrue(i_top < i_obs < i_progress < i_items < i_finish)
		self.assertEqual(self.detail.count("render_order_observations_html("), 1)

	def test_empty_renders_nothing(self):
		self.assertIn('if (!list.length) return "";', self.helper)
		self.assertIn('(o.text || "").trim()', self.helper)

	def test_heading_once_and_labels_only_for_several_orders(self):
		self.assertEqual(self.js.count('__("OBSERVACIONES DEL PEDIDO")'), 1)
		self.assertIn("const multiple = list.length > 1;", self.helper)
		self.assertRegex(self.helper, r"multiple\s*\?\s*`<div class=\"fg-order-observations-order\">")

	def test_every_server_value_escaped(self):
		self.assertIn("frappe.utils.escape_html(o.text.trim())", self.helper)
		self.assertRegex(self.helper, r"escape_html\(\s*o\.commercial_name \|\| o\.sales_order")

	def test_read_only(self):
		for forbidden in ("<textarea", "<input", "contenteditable", "<button"):
			self.assertNotIn(forbidden, self.helper)

	def test_responsive_and_not_fixed(self):
		block = _css_rule(self.css, ".fg-order-observations")
		self.assertIn("min-width: 0", block)
		self.assertNotIn("position", block)
		text = _css_rule(self.css, ".fg-order-observations-text")
		self.assertIn("white-space: pre-wrap", text)
		self.assertIn("overflow-wrap: anywhere", text)
		self.assertRegex(self.css, r"@media \(max-width: 640px\) \{\s*\.fg-order-observations \{")


class TestJefeUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_JEFE_JS)
		cls.css = _read(_JEFE_CSS)
		cls.dialog = _method_body(cls.js, "render_detail_dialog")
		cls.helper = _function_body(cls.js, "render_order_observations_html")

	def test_reuses_bodega_get_pick_list(self):
		self.assertIn('"fabergray_erp.api.bodega.get_pick_list"', _method_body(self.js, "open_detail"))

	def test_block_before_item_table(self):
		i_info = self.dialog.index('<div class="fg-pl-detail-info">')
		i_obs = self.dialog.index("${render_order_observations_html(detail.order_observations)}")
		i_table = self.dialog.index('<table class="fg-pl-detail-table">')
		self.assertTrue(i_info < i_obs < i_table)

	def test_empty_renders_nothing_and_escapes(self):
		self.assertIn('if (!list.length) return "";', self.helper)
		self.assertIn("frappe.utils.escape_html(o.text.trim())", self.helper)
		self.assertEqual(self.js.count('__("OBSERVACIONES DEL PEDIDO")'), 1)
		for forbidden in ("<textarea", "<input", "contenteditable", "<button"):
			self.assertNotIn(forbidden, self.helper)

	def test_styles_scoped_to_dialog_and_wrapping(self):
		text = _css_rule(self.css, ".fg-pl-detail-dialog .fg-order-observations-text")
		self.assertIn("white-space: pre-wrap", text)
		self.assertIn("overflow-wrap: anywhere", text)
		self.assertNotIn("position", _css_rule(self.css, ".fg-pl-detail-dialog .fg-order-observations"))
