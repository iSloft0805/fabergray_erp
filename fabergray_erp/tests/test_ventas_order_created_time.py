# -*- coding: utf-8 -*-
"""Hotfix 25.26.3 -- hora de creación del pedido en Ventas.

Fuente única: `Sales Order.creation` del pedido VIGENTE (nunca `modified`,
nunca la cadena `amended_from`). `get_my_orders()` (tarjetas) y
`get_order_detail()` (VER PEDIDO) solo agregan `creation`; ventas.js la
formatea con UN helper (`format_created_time()` / `format_order_date_with_
time()`) usando `frappe.datetime.convert_to_user_tz()` -- la zona horaria
efectiva de Frappe, sin offsets manuales.

Los casos de formato (12:00 AM / 9:05 AM / 12:30 PM / 3:42 PM, sin
segundos, fallback limpio) ejecutan el helper REAL extraído de ventas.js
en Node con el mismo moment-timezone que distribuye Frappe -- no una
réplica en Python.
"""

import json
import os
import re
import shutil
import subprocess

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime

from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_VENTAS_JS = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.js")
_VENTAS_CSS = os.path.join(frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "ventas", "ventas.css")
_FRAPPE_APP_DIR = os.path.dirname(frappe.get_app_path("frappe"))


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


def _helper_source(js):
	"""The exact helper block from ventas.js: CREATED_TIME_FORMAT +
	format_created_time() + format_order_date_with_time()."""
	start = js.index("const CREATED_TIME_FORMAT")
	end = js.index("\nfunction format_qty(")
	return js[start:end]


class TestCreatedTimeServer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		sfx = frappe.generate_hash(length=5)
		cls.wh = cls.world.warehouse(f"FG25263 {sfx} WH")
		cls.item = cls.world.item(f"FG25263-{sfx}-A")
		cls.customer = cls.world.customer(f"FG25263 {sfx} Cliente")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		cls.vendedora = cls.world.user(f"fg25263-{sfx}-vendedora@example.com", ["Vendedora"])

	def _order(self):
		return self.world.submitted_sales_order(self.item.name, self.wh.name, 2, self.customer.name)

	def _active(self, name):
		with fx.as_user(self.vendedora):
			return {o["name"]: o for o in ventas.get_my_orders(limit=200)}[name]

	def test_01_02_07_active_list_returns_the_sales_order_creation(self):
		so = self._order()
		row = self._active(so.name)
		self.assertIn("creation", row)
		self.assertEqual(get_datetime(row["creation"]), get_datetime(frappe.db.get_value("Sales Order", so.name, "creation")))

	def test_08_cancelled_list_returns_creation(self):
		so = self._order()
		so.reload()
		so.cancel()
		with fx.as_user(self.vendedora):
			rows = {o["name"]: o for o in ventas.get_my_orders(limit=200, view="cancelled")}
		self.assertEqual(
			get_datetime(rows[so.name]["creation"]), get_datetime(frappe.db.get_value("Sales Order", so.name, "creation"))
		)

	def test_03_04_modified_is_never_the_source(self):
		so = self._order()
		creation = get_datetime(frappe.db.get_value("Sales Order", so.name, "creation"))
		frappe.db.set_value(
			"Sales Order", so.name, "modified", add_to_date(creation, days=3, hours=5), update_modified=False
		)
		row = self._active(so.name)
		self.assertEqual(get_datetime(row["creation"]), creation)
		self.assertNotEqual(get_datetime(row["creation"]), get_datetime(frappe.db.get_value("Sales Order", so.name, "modified")))
		with fx.as_user(self.vendedora):
			detail = ventas.get_order_detail(so.name)
		self.assertEqual(get_datetime(detail["creation"]), creation)

	def test_05_06_transaction_and_delivery_dates_untouched(self):
		so = self._order()
		row = self._active(so.name)
		self.assertEqual(str(row["transaction_date"]), str(so.transaction_date))
		self.assertEqual(str(row["delivery_date"]), str(so.delivery_date))

	def test_22_order_detail_returns_creation(self):
		so = self._order()
		with fx.as_user(self.vendedora):
			detail = ventas.get_order_detail(so.name)
		self.assertEqual(get_datetime(detail["creation"]), get_datetime(frappe.db.get_value("Sales Order", so.name, "creation")))
		self.assertEqual(str(detail["transaction_date"]), str(so.transaction_date))
		self.assertEqual(str(detail["delivery_date"]), str(so.delivery_date))

	def test_24_uses_own_creation_never_amended_from(self):
		import inspect

		for fn in (ventas.get_my_orders, ventas.get_order_detail):
			source = inspect.getsource(fn)
			self.assertIn('"creation": so.creation', source)
			self.assertNotRegex(source, r'"creation":[^\n]*amended_from')
			self.assertNotRegex(source, r'"creation":[^\n]*modified')

	def test_26_reading_does_not_modify_the_sales_order(self):
		so = self._order()
		before = frappe.db.get_value("Sales Order", so.name, ["modified", "creation", "transaction_date", "delivery_date"], as_dict=True)
		self._active(so.name)
		with fx.as_user(self.vendedora):
			ventas.get_order_detail(so.name)
		after = frappe.db.get_value("Sales Order", so.name, ["modified", "creation", "transaction_date", "delivery_date"], as_dict=True)
		self.assertEqual(before, after)

	def test_27_no_custom_field_created(self):
		"""Sales Order keeps exactly its three pre-existing Custom Fields --
		the time comes from the native `creation`, nothing is stored."""
		expected = {"fg_observations", "fg_cancellation_reason", "fg_cancellation_note"}
		path = os.path.join(frappe.get_app_path("fabergray_erp"), "fixtures", "custom_field.json")
		fixture = {f["fieldname"] for f in json.loads(_read(path)) if f["dt"] == "Sales Order"}
		self.assertEqual(fixture, expected)
		in_db = set(
			frappe.get_all("Custom Field", filters={"dt": "Sales Order", "fieldname": ["like", "fg\\_%"]}, pluck="fieldname")
		)
		self.assertEqual(in_db, expected)


class TestCreatedTimeUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = _read(_VENTAS_JS)
		cls.css = _read(_VENTAS_CSS)
		cls.helper = _helper_source(cls.js)
		cls.card = _method_body(cls.js, "render_order_card")
		cls.detail = _method_body(cls.js, "render_order_detail_overlay")

	def test_09_10_11_card_uses_creation_keeps_transaction_date(self):
		self.assertIn("format_order_date_with_time(o.transaction_date, o.creation)", self.card)
		self.assertNotIn("str_to_user(o.transaction_date)", self.card)  # now inside the shared helper
		self.assertIn("frappe.datetime.str_to_user(transaction_date)", self.helper)
		self.assertIn("`${date} · ${time}`", self.helper)

	def test_12_delivery_date_has_no_time(self):
		for body, obj in ((self.card, "o"), (self.detail, "detail")):
			self.assertIn(f"{obj}.delivery_date ? frappe.datetime.str_to_user({obj}.delivery_date)", body)
			self.assertNotRegex(body, r"delivery_date[^\n]*creation")

	def test_13_14_helper_uses_frappe_timezone_no_manual_offsets(self):
		self.assertIn("frappe.datetime.convert_to_user_tz(creation, false)", self.helper)
		for forbidden in ("America/Bogota", "utcOffset", "getTimezoneOffset", "-05:00", "new Date(", ".utc(", "* 60"):
			self.assertNotIn(forbidden, self.helper)

	def test_15_20_format_is_12h_without_seconds(self):
		self.assertIn('const CREATED_TIME_FORMAT = "h:mm A";', self.helper)
		self.assertNotIn(":ss", self.helper)

	def test_21_missing_creation_has_clean_fallback(self):
		self.assertIn('if (!creation) return "";', self.helper)
		self.assertIn("m && m.isValid() ?", self.helper)
		self.assertIn("return time ? `${date} · ${time}` : date;", self.helper)

	def test_22_23_detail_reuses_the_same_helper(self):
		self.assertIn("format_order_date_with_time(detail.transaction_date, detail.creation)", self.detail)
		self.assertEqual(self.js.count("function format_created_time("), 1)
		self.assertEqual(self.js.count("function format_order_date_with_time("), 1)
		self.assertEqual(self.js.count("format_order_date_with_time("), 3)  # definition + card + detail

	def test_24_no_modified_or_amended_from_in_helper(self):
		for forbidden in ("modified", "amended_from"):
			self.assertNotIn(forbidden, self.helper)

	def test_25_create_payload_unchanged(self):
		body = _method_body(self.js, "build_order_payload")
		self.assertIn(".map((l) => ({ item_code: l.item_code, qty: l.qty }))", body)
		self.assertNotIn("creation", body)

	def test_28_responsive_meta_keeps_flex_wrap(self):
		for selector in (".fg-ventas .fg-order-card-meta", ".fg-ventas .fg-order-detail-meta"):
			rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css).group(1)
			self.assertIn("flex-wrap: wrap", rule, selector)
			self.assertNotIn("white-space: nowrap", rule, selector)
			self.assertNotRegex(rule, r"(?<!-)width:\s*\d", selector)


class TestCreatedTimeFormatExecuted(IntegrationTestCase):
	"""16-21: runs the REAL helper from ventas.js under Node with Frappe's own
	moment-timezone and a stubbed frappe.datetime.convert_to_user_tz()
	reproducing Frappe's system->user conversion."""

	def _run(self, cases, system_tz="America/Bogota", user_tz="America/Bogota"):
		node = shutil.which("node")
		if not node or not os.path.isdir(os.path.join(_FRAPPE_APP_DIR, "node_modules", "moment-timezone")):
			self.skipTest("node / moment-timezone not available")
		helper = _helper_source(_read(_VENTAS_JS))
		script = f"""
const moment = require("moment-timezone");
moment.suppressDeprecationWarnings = true;
global.frappe = {{
	boot: {{ time_zone: {{ system: {json.dumps(system_tz)}, user: {json.dumps(user_tz)} }} }},
	datetime: {{
		convert_to_user_tz(date, format) {{
			const d = moment.tz(date, frappe.boot.time_zone.system).clone().tz(frappe.boot.time_zone.user);
			return format === false ? d : d.format();
		}},
		str_to_user(v) {{ return moment(v, "YYYY-MM-DD").format("DD-MM-YYYY"); }},
	}},
}};
{helper}
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(([d, c]) => format_order_date_with_time(d, c))));
"""
		out = subprocess.run([node, "-e", script], cwd=_FRAPPE_APP_DIR, capture_output=True, text=True, timeout=60)
		self.assertEqual(out.returncode, 0, out.stderr)
		return json.loads(out.stdout.strip().splitlines()[-1])

	def test_16_to_20_twelve_hour_format(self):
		result = self._run(
			[
				["2026-09-23", "2026-09-23 00:00:00.000000"],
				["2026-09-23", "2026-09-23 09:05:12.123456"],
				["2026-09-23", "2026-09-23 12:30:59"],
				["2026-09-23", "2026-09-23 15:42:07.5"],
			]
		)
		self.assertEqual(
			result,
			["23-09-2026 · 12:00 AM", "23-09-2026 · 9:05 AM", "23-09-2026 · 12:30 PM", "23-09-2026 · 3:42 PM"],
		)
		for value in result:
			self.assertNotRegex(value, r"\d:\d\d:\d\d")  # no seconds

	def test_21_missing_or_invalid_creation_shows_only_the_date(self):
		result = self._run([["2026-09-23", None], ["2026-09-23", ""], ["2026-09-23", "basura"]])
		self.assertEqual(result, ["23-09-2026", "23-09-2026", "23-09-2026"])
		for value in result:
			self.assertNotIn("undefined", value)
			self.assertNotIn("Invalid", value)
			self.assertNotIn("·", value)

	def test_13_follows_frappe_system_to_user_conversion(self):
		"""Stored in the system zone, shown in the user's -- purely through
		convert_to_user_tz(), whatever those zones are."""
		self.assertEqual(
			self._run([["2026-09-23", "2026-09-24 02:12:00"]], system_tz="Asia/Kolkata", user_tz="America/Bogota"),
			["23-09-2026 · 3:42 PM"],
		)
