# -*- coding: utf-8 -*-
"""Hotfix "Reporte PDF de faltantes" -- api.reporte_faltantes: every Reporte
de Faltante reported within a period (HOY / 7 / 30 días / este mes / rango),
optional status filter, summary, A4-landscape PDF, company isolation, roles
and a read-only integrity check.

Reports are real documents (the DocType's own insert/validate); reported_on
is then placed at exact period boundaries at db level (test setup only).
The report covers every Fabrigray report in the period -- including other
test data on this site -- so assertions look at THIS class's own report
names, and the summary is checked for consistency with the returned rows.
wkhtmltopdf is not installed here: get_pdf() is replaced by a capturer, the
HTML it receives is asserted directly."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, get_datetime, get_first_day, getdate, nowdate

from fabergray_erp.api import reporte_faltantes as rf
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


@contextmanager
def _captured_pdf():
	calls = []

	def fake_get_pdf(html, options=None, output=None):
		calls.append({"html": html, "options": dict(options or {})})
		return b"%PDF-1.4 fake"

	with patch("frappe.utils.pdf.get_pdf", side_effect=fake_get_pdf):
		yield calls


class TestReporteFaltantes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		sfx = frappe.generate_hash(length=5)
		cls.sfx = sfx
		cls.wh = cls.world.warehouse(f"FGRF {sfx} WH")
		cls.item = cls.world.item(f"FGRF-{sfx}-ITEM")
		frappe.db.set_value("Item", cls.item.name, "item_name", f"BASE CUBANA ESCOBA SUAVE DIN {sfx}")
		cls.customer = cls.world.customer(f"FGRF {sfx} Cliente")
		cls.world.stock_up(cls.item.name, cls.wh.name, 100)
		cls.so = cls.world.submitted_sales_order(cls.item.name, cls.wh.name, 3, cls.customer.name)

		cls.jefe = cls.world.user(f"fgrf-{sfx}-jefe@example.com", ["Jefe de Bodega"])
		cls.bodega = cls.world.user(f"fgrf-{sfx}-bodega@example.com", ["Bodega"])
		cls.vendedora = cls.world.user(f"fgrf-{sfx}-vendedora@example.com", ["Vendedora"])
		cls.sysmanager = cls.world.user(f"fgrf-{sfx}-sm@example.com", ["System Manager"])
		cls.other_company_jefe = cls.world.user(f"fgrf-{sfx}-other-jefe@example.com", ["Jefe de Bodega"])
		perm = frappe.get_doc(
			{"doctype": "User Permission", "user": cls.other_company_jefe, "allow": "Company", "for_value": "_Test Company"}
		).insert(ignore_permissions=True)
		cls.world.track_existing("User Permission", perm.name)

		cls.today = getdate(nowdate())
		# Boundary reports: (key, reported_on)
		cls.r = {}
		cls.r["today"] = cls._shortage(f"{cls.today} 10:00:00")
		cls.r["d6_start"] = cls._shortage(f"{add_days(cls.today, -6)} 00:00:00")
		cls.r["d7_end"] = cls._shortage(f"{add_days(cls.today, -7)} 23:59:59")
		cls.r["d29_start"] = cls._shortage(f"{add_days(cls.today, -29)} 00:00:00")
		cls.r["d30_end"] = cls._shortage(f"{add_days(cls.today, -30)} 23:59:59")
		cls.first_of_month = getdate(get_first_day(cls.today))
		cls.r["month_start"] = cls._shortage(f"{cls.first_of_month} 00:00:00")
		cls.r["prev_month_end"] = cls._shortage(f"{add_days(cls.first_of_month, -1)} 23:59:59")

		# Status mix, all reported today.
		cls.r["in_progress"] = cls._shortage(f"{cls.today} 09:00:00", qty_solicitada=5, qty_disponible=2)
		frappe.db.set_value("Reporte de Faltante", cls.r["in_progress"], "status", "En Proceso", update_modified=False)
		# The spec's example: PEDIDO + BASE CUBANA ESCOBA SUAVE DIN, 3 / 0 / 3,
		# resolved through a REAL save (so Frappe's Version log records it).
		cls.r["resolved"] = cls._shortage(f"{cls.today} 08:00:00", qty_solicitada=3, qty_disponible=0)
		doc = frappe.get_doc("Reporte de Faltante", cls.r["resolved"])
		doc.status = "Resuelto"
		doc.resolution_note = "Compra recibida"
		# ignore_version=False: Frappe skips Version rows under frappe.in_test
		# by default (Document.save()); production always records them.
		doc.save(ignore_permissions=True, ignore_version=False)
		cls.resolved_version_on = frappe.db.get_value(
			"Version", {"ref_doctype": "Reporte de Faltante", "docname": cls.r["resolved"]}, "creation", order_by="creation desc"
		)
		# Resolved with NO Version trail (forced at db level): must show no
		# resolution date rather than a guessed one.
		cls.r["resolved_no_trail"] = cls._shortage(f"{cls.today} 07:00:00")
		frappe.db.set_value("Reporte de Faltante", cls.r["resolved_no_trail"], "status", "Resuelto")

		# Another company's report: never visible.
		cls.r["other_company"] = cls._shortage(f"{cls.today} 11:00:00", warehouse="Finished Goods - _TC", sales_order=None)
		frappe.db.commit()

	@classmethod
	def _shortage(cls, reported_on, qty_solicitada=3, qty_disponible=0, warehouse=None, sales_order="default"):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": cls.item.name,
				"warehouse": warehouse or cls.wh.name,
				"sales_order": cls.so.name if sales_order == "default" else sales_order,
				"qty_solicitada": qty_solicitada,
				"qty_disponible": qty_disponible,
				"detected_by": "Bodega",
				"shortage_reason": "Compra pendiente",
			}
		).insert(ignore_permissions=True)
		cls.world.track_existing("Reporte de Faltante", doc.name)
		frappe.db.set_value("Reporte de Faltante", doc.name, "reported_on", reported_on, update_modified=False)
		return doc.name

	def _report(self, period, user=None, **kwargs):
		with fx.as_user(user or self.jefe):
			return rf.get_shortage_report(period, **kwargs)

	def _mine(self, report):
		mine = set(self.r.values())
		return {row["name"]: row for row in report["items"] if row["name"] in mine}

	def _keys(self, report):
		by_name = {v: k for k, v in self.r.items()}
		return {by_name[name] for name in self._mine(report)}

	# =====================================================================
	# Períodos (server-side, inclusivos)
	# =====================================================================

	def test_resolve_period_pure(self):
		today = getdate("2026-09-28")
		self.assertEqual(rf.resolve_period("hoy", today=today), (today, today))
		self.assertEqual(rf.resolve_period("7_dias", today=today), (getdate("2026-09-22"), today))
		self.assertEqual(rf.resolve_period("30_dias", today=today), (getdate("2026-08-30"), today))
		self.assertEqual(rf.resolve_period("este_mes", today=today), (getdate("2026-09-01"), today))
		self.assertEqual(
			rf.resolve_period("rango", "2026-09-22", "2026-09-28", today=today),
			(getdate("2026-09-22"), getdate("2026-09-28")),
		)
		for bad in (("rango", None, "2026-09-28"), ("rango", "2026-09-29", "2026-09-28"), ("rango", "x", "y"), ("ayer", None, None)):
			with self.assertRaises(rf.InvalidReportPeriodError, msg=str(bad)):
				rf.resolve_period(*bad, today=today)
		with self.assertRaises(rf.InvalidReportPeriodError):
			rf.resolve_period("rango", "2024-01-01", "2026-09-28", today=today)

	def test_today(self):
		report = self._report("hoy")
		self.assertEqual((report["from_date"], report["to_date"]), (str(self.today), str(self.today)))
		keys = self._keys(report)
		self.assertTrue({"today", "in_progress", "resolved", "resolved_no_trail"} <= keys, keys)
		self.assertFalse({"d6_start", "d7_end", "other_company"} & keys, keys)

	def test_last_7_days_inclusive(self):
		keys = self._keys(self._report("7_dias"))
		self.assertIn("today", keys)
		self.assertIn("d6_start", keys)  # 6 days ago at 00:00:00 -- inside
		self.assertNotIn("d7_end", keys)  # 7 days ago at 23:59:59 -- outside

	def test_last_30_days_inclusive(self):
		keys = self._keys(self._report("30_dias"))
		self.assertTrue({"today", "d6_start", "d7_end", "d29_start"} <= keys, keys)
		self.assertNotIn("d30_end", keys)

	def test_this_month(self):
		keys = self._keys(self._report("este_mes"))
		self.assertIn("month_start", keys)
		self.assertIn("today", keys)
		self.assertNotIn("prev_month_end", keys)

	def test_custom_range_is_inclusive_on_both_ends(self):
		d7, d6 = add_days(self.today, -7), add_days(self.today, -6)
		keys = self._keys(self._report("rango", from_date=str(d7), to_date=str(d6)))
		self.assertIn("d7_end", keys)  # last second of from_date
		self.assertIn("d6_start", keys)  # first second of to_date
		self.assertNotIn("today", keys)

	def test_period_without_results(self):
		report = self._report("rango", from_date="2020-01-01", to_date="2020-01-31")
		self.assertEqual(report["items"], [])
		self.assertEqual(
			report["summary"], {"total_reports": 0, "open": 0, "in_progress": 0, "resolved": 0, "total_shortage_qty": 0.0}
		)

	# =====================================================================
	# Estado / resumen / orden
	# =====================================================================

	def test_all_statuses_by_default_and_summary_is_consistent(self):
		report = self._report("hoy")
		items, s = report["items"], report["summary"]
		self.assertEqual(s["total_reports"], len(items))
		self.assertEqual(s["open"], sum(1 for i in items if i["status"] == "Abierto"))
		self.assertEqual(s["in_progress"], sum(1 for i in items if i["status"] == "En Proceso"))
		self.assertEqual(s["resolved"], sum(1 for i in items if i["status"] == "Resuelto"))
		self.assertAlmostEqual(s["total_shortage_qty"], sum(i["qty_faltante"] for i in items))
		statuses = {self._mine(report)[self.r[k]]["status"] for k in ("today", "in_progress", "resolved")}
		self.assertEqual(statuses, {"Abierto", "En Proceso", "Resuelto"})

	def test_only_open(self):
		report = self._report("hoy", status="Abierto")
		self.assertTrue(report["items"])
		self.assertTrue(all(i["status"] == "Abierto" for i in report["items"]))
		self.assertEqual(self._keys(report) & {"in_progress", "resolved"}, set())
		self.assertEqual(report["summary"]["open"], report["summary"]["total_reports"])

	def test_only_resolved(self):
		report = self._report("hoy", status="Resuelto")
		self.assertTrue(all(i["status"] == "Resuelto" for i in report["items"]))
		self.assertEqual(self._keys(report) & {"today", "in_progress"}, set())
		self.assertIn("resolved", self._keys(report))

	def test_invalid_status_rejected(self):
		with self.assertRaises(rf.InvalidReportPeriodError):
			self._report("hoy", status="Cerrado")

	def test_order_reported_on_desc_then_name_desc(self):
		items = self._report("30_dias")["items"]
		keys = [(get_datetime(i["reported_on"]), i["name"]) for i in items]
		self.assertEqual(keys, sorted(keys, reverse=True))

	# =====================================================================
	# Datos de la fila
	# =====================================================================

	def test_example_row_appears_exactly_once_with_real_data(self):
		items = self._report("hoy")["items"]
		matches = [i for i in items if i["name"] == self.r["resolved"]]
		self.assertEqual(len(matches), 1)
		row = matches[0]
		self.assertEqual(row["order_number"], self.so.name)
		self.assertEqual(row["sales_order"], self.so.name)
		self.assertEqual(row["customer"], self.customer.name)
		self.assertEqual(row["item_code"], self.item.name)
		self.assertEqual(row["item_name"], f"BASE CUBANA ESCOBA SUAVE DIN {self.sfx}")
		self.assertEqual(row["warehouse"], self.wh.name)
		self.assertEqual((row["qty_solicitada"], row["qty_disponible"], row["qty_faltante"]), (3, 0, 3))
		self.assertEqual(row["status"], "Resuelto")
		self.assertEqual(row["shortage_reason"], "Compra pendiente")
		self.assertEqual(row["resolution_note"], "Compra recibida")
		self.assertEqual(row["reported_by"], "Administrator")

	def test_quantities_come_from_the_report(self):
		row = self._mine(self._report("hoy"))[self.r["in_progress"]]
		self.assertEqual((row["qty_solicitada"], row["qty_disponible"], row["qty_faltante"]), (5, 2, 3))

	def test_resolution_date_comes_from_the_real_status_transition(self):
		mine = self._mine(self._report("hoy"))
		self.assertIsNotNone(self.resolved_version_on)
		self.assertEqual(get_datetime(mine[self.r["resolved"]]["resolved_on"]), get_datetime(self.resolved_version_on))
		# no Version trail -> no date, never `modified`
		self.assertIsNone(mine[self.r["resolved_no_trail"]]["resolved_on"])
		self.assertIsNone(mine[self.r["today"]]["resolved_on"])

	# =====================================================================
	# Seguridad
	# =====================================================================

	def test_roles(self):
		self.assertTrue(self._report("hoy", user=self.sysmanager)["items"])
		for user in (self.bodega, self.vendedora):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self._report("hoy", user=user)
			with fx.as_user(user), _captured_pdf() as calls:
				with self.assertRaises(frappe.PermissionError):
					rf.download_shortage_report_pdf("hoy")
			self.assertEqual(calls, [])

	def test_other_company_reports_never_listed(self):
		for period in ("hoy", "30_dias"):
			names = {i["name"] for i in self._report(period, user=self.sysmanager)["items"]}
			self.assertNotIn(self.r["other_company"], names)

	def test_user_of_another_company_is_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			self._report("hoy", user=self.other_company_jefe)

	def test_company_is_never_taken_from_the_client(self):
		with self.assertRaises(TypeError):
			self._report("hoy", company="_Test Company")

	# =====================================================================
	# PDF
	# =====================================================================

	def test_pdf_download_contains_order_item_and_quantities(self):
		with fx.as_user(self.jefe), _captured_pdf() as calls:
			rf.download_shortage_report_pdf("hoy")
		self.assertEqual(len(calls), 1)
		html, options = calls[0]["html"], calls[0]["options"]
		self.assertEqual(options.get("orientation"), "Landscape")
		self.assertEqual(options.get("page-size"), "A4")
		self.assertEqual(frappe.local.response.type, "download")
		self.assertEqual(frappe.local.response.filename, f"Reporte-Faltantes-{self.today}-a-{self.today}.pdf")
		self.assertEqual(frappe.local.response.filecontent, b"%PDF-1.4 fake")
		for text in (
			"FABRIGRAY",
			"REPORTE DE FALTANTES",
			"TOTAL REPORTADOS",
			"CANTIDAD TOTAL FALTANTE",
			self.so.name,
			self.item.name,
			f"BASE CUBANA ESCOBA SUAVE DIN {self.sfx}",
			"RESUELTO",
			"EN PROCESO",
			"ABIERTO",
			"Compra recibida",
			self.r["resolved"],
		):
			self.assertIn(text, html)
		self.assertNotIn(self.r["other_company"], html)
		d = self.today.strftime("%d/%m/%Y")
		self.assertIn(f"{d} - {d}", html)
		# footer "Generado el": day/month/year, never the minute as month
		self.assertIn(f"Generado el {getdate(nowdate()).strftime('%d/%m/%Y')} ", html)

	def test_generated_on_is_day_month_year_hour_minute(self):
		"""Regression: a babel "dd/mm/yyyy HH:mm" pattern printed the MINUTE
		where the month goes. Minute 58 != month 09 makes a swap visible."""
		self.assertEqual(rf._format_generated_on(get_datetime("2026-09-28 14:58:07")), "28/09/2026 14:58")
		self.assertEqual(rf._format_generated_on(get_datetime("2026-01-05 07:03:00")), "05/01/2026 07:03")

	def test_pdf_preview_is_inline(self):
		with fx.as_user(self.jefe), _captured_pdf():
			rf.download_shortage_report_pdf("7_dias", preview=1)
		self.assertEqual(frappe.local.response.type, "pdf")

	def test_pdf_for_empty_period_says_so(self):
		with fx.as_user(self.jefe), _captured_pdf() as calls:
			rf.download_shortage_report_pdf("rango", from_date="2020-01-01", to_date="2020-01-02")
		self.assertIn("No hay faltantes reportados en este período.", calls[0]["html"])

	# =====================================================================
	# Solo lectura
	# =====================================================================

	def test_report_and_pdf_modify_nothing(self):
		pick_list = self.world.pick_list_for(self.so, self.wh.name)

		def snapshot():
			return {
				"reports": frappe.db.sql(
					"select name, status, modified, resolution_note from `tabReporte de Faltante` order by name"
				),
				"so": frappe.db.get_value("Sales Order", self.so.name, ["modified", "status", "per_delivered"]),
				"pick_list": frappe.db.get_value("Pick List", pick_list.name, ["modified", "docstatus"]),
				"item": frappe.db.get_value("Item", self.item.name, "modified"),
				"warehouse": frappe.db.get_value("Warehouse", self.wh.name, "modified"),
				"bin": frappe.db.sql("select name, actual_qty, reserved_qty, modified from tabBin order by name"),
				"counts": {
					dt: frappe.db.count(dt)
					for dt in ("Material Request", "Stock Entry", "Stock Ledger Entry", "Version", "Reporte de Faltante")
				},
			}

		before = snapshot()
		for period, kwargs in (("hoy", {}), ("30_dias", {"status": "Resuelto"}), ("este_mes", {})):
			self._report(period, **kwargs)
			with fx.as_user(self.jefe), _captured_pdf():
				rf.download_shortage_report_pdf(period, **kwargs)
		self.assertEqual(snapshot(), before)

	# =====================================================================
	# UI (contrato estático; no hay runner JS en esta app)
	# =====================================================================

	def test_page_and_entry_point(self):
		page = frappe.get_doc("Page", "reporte-faltantes")
		self.assertEqual({r.role for r in page.roles}, {"Jefe de Bodega", "System Manager"})
		base = frappe.get_app_path("fabergray_erp", "fabrigray_erp", "page")
		with open(f"{base}/jefe_de_bodega/jefe_de_bodega.js", encoding="utf-8") as f:
			self.assertIn('frappe.set_route("reporte-faltantes")', f.read())
		with open(f"{base}/reporte_faltantes/reporte_faltantes.js", encoding="utf-8") as f:
			js = f.read()
		for text in ("GENERAR PDF", "VISTA PREVIA", "RANGO PERSONALIZADO", "EN PROCESO", "download_shortage_report_pdf", "get_shortage_report"):
			self.assertIn(text, js)
		self.assertNotIn("company:", js)  # never sent by the client
