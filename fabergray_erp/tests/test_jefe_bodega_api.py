# -*- coding: utf-8 -*-
"""Commit 8 -- Section 4: fabergray_erp.api.jefe_bodega.* against a small,
self-built world covering every bucket (pendiente, en_alistamiento,
con_faltantes, listos) plus a second Warehouse for User Permission isolation.
"""

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega, jefe_bodega
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestJefeDeBodegaAPI(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh_a = cls.world.warehouse("FG8 Jefe API A")
		cls.wh_b = cls.world.warehouse("FG8 Jefe API B")
		cls.item = cls.world.item("FG8-JEFE-API-ITEM")
		cls.customer = cls.world.customer("FG8 Test Customer Jefe API")
		cls.world.stock_up(cls.item.name, cls.wh_a.name, 1000)
		cls.world.stock_up(cls.item.name, cls.wh_b.name, 1000)

		bodega_user = cls.world.user("fg8-bodega-jefeapi@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(bodega_user, cls.wh_a.name)
		cls.world.warehouse_user_permission(bodega_user, cls.wh_b.name)

		def new_pl(warehouse):
			so = cls.world.submitted_sales_order(cls.item.name, warehouse, 10, cls.customer.name)
			return cls.world.pick_list_for(so, warehouse)

		cls.pl_pendiente = new_pl(cls.wh_a.name)
		cls.pl_en_alistamiento = new_pl(cls.wh_a.name)
		cls.pl_con_faltantes = new_pl(cls.wh_a.name)
		cls.pl_listos = new_pl(cls.wh_a.name)
		cls.pl_con_faltantes_resuelto = new_pl(cls.wh_a.name)
		cls.pl_b_en_alistamiento = new_pl(cls.wh_b.name)

		with fx.as_user(bodega_user):
			bodega.start_picking(cls.pl_en_alistamiento.name)

			bodega.start_picking(cls.pl_con_faltantes.name)
			row = bodega.get_pick_list(cls.pl_con_faltantes.name)["rows"][0]
			bodega.set_picked_qty(cls.pl_con_faltantes.name, row["row_name"], 4)
			open_report = bodega.report_shortage(
				pick_list=cls.pl_con_faltantes.name,
				row_name=row["row_name"],
				qty_disponible=4,
				shortage_reason="Stock insuficiente",
			)
			cls.world.track_existing("Reporte de Faltante", open_report["name"])

			bodega.start_picking(cls.pl_listos.name)
			for row in bodega.get_pick_list(cls.pl_listos.name)["rows"]:
				bodega.set_picked_qty(cls.pl_listos.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(cls.pl_listos.name)

			bodega.start_picking(cls.pl_con_faltantes_resuelto.name)
			row = bodega.get_pick_list(cls.pl_con_faltantes_resuelto.name)["rows"][0]
			bodega.set_picked_qty(cls.pl_con_faltantes_resuelto.name, row["row_name"], 4)
			resuelto_report = bodega.report_shortage(
				pick_list=cls.pl_con_faltantes_resuelto.name,
				row_name=row["row_name"],
				qty_disponible=4,
				shortage_reason="Stock insuficiente",
			)
			cls.world.track_existing("Reporte de Faltante", resuelto_report["name"])

			bodega.start_picking(cls.pl_b_en_alistamiento.name)

		# Resolve one report as Administrator (routine status update, not a
		# permission escalation -- Jefe de Bodega already has write access to
		# existing reports, this just avoids an extra role switch in setup).
		resolved = frappe.get_doc("Reporte de Faltante", resuelto_report["name"])
		resolved.status = "Resuelto"
		resolved.resolution_note = "FG8 test setup"
		resolved.save()

		cls.jefe_a = cls.world.user("fg8-jefe-api-a@example.com", ["Jefe de Bodega"])
		cls.world.warehouse_user_permission(cls.jefe_a, cls.wh_a.name)
		cls.jefe_b = cls.world.user("fg8-jefe-api-b@example.com", ["Jefe de Bodega"])
		cls.world.warehouse_user_permission(cls.jefe_b, cls.wh_b.name)

	def test_get_summary_matches_get_queue_buckets_exactly(self):
		with fx.as_user(self.jefe_a):
			summary = jefe_bodega.get_summary()
			queue = bodega.get_queue()
			open_reports = frappe.get_list("Reporte de Faltante", filters={"status": "Abierto"})

		self.assertEqual(summary["pendientes"], len(queue["pendientes"]))
		self.assertEqual(summary["en_alistamiento"], len(queue["en_alistamiento"]))
		self.assertEqual(summary["con_faltantes"], len(queue["con_faltantes"]))
		self.assertEqual(summary["listos"], len(queue["listos"]))
		self.assertEqual(summary["faltantes_abiertos"], len(open_reports))

	def test_get_open_shortage_reports_returns_only_abierto(self):
		resuelto_name = frappe.db.get_value(
			"Reporte de Faltante", {"pick_list": self.pl_con_faltantes_resuelto.name}
		)
		abierto_name = frappe.db.get_value(
			"Reporte de Faltante", {"pick_list": self.pl_con_faltantes.name}
		)

		with fx.as_user(self.jefe_a):
			names = [r["name"] for r in jefe_bodega.get_open_shortage_reports()]

		self.assertIn(abierto_name, names)
		self.assertNotIn(resuelto_name, names)

	def test_get_active_pick_lists_exact_progress_and_permissions(self):
		with fx.as_user(self.jefe_a):
			active = jefe_bodega.get_active_pick_lists()
		by_name = {pl["name"]: pl for pl in active}

		self.assertIn(self.pl_en_alistamiento.name, by_name)
		entry = by_name[self.pl_en_alistamiento.name]
		self.assertEqual(entry["items_totales"], 1)
		self.assertEqual(entry["items_completos"], 0)  # nothing picked yet

	def test_pick_list_with_open_shortage_is_not_double_counted(self):
		with fx.as_user(self.jefe_a):
			queue = bodega.get_queue()
			active = jefe_bodega.get_active_pick_lists()

		self.assertIn(self.pl_con_faltantes.name, [pl["name"] for pl in queue["con_faltantes"]])
		self.assertNotIn(self.pl_con_faltantes.name, [pl["name"] for pl in queue["en_alistamiento"]])
		self.assertNotIn(self.pl_con_faltantes.name, [pl["name"] for pl in active])

	def test_resolved_shortage_pick_list_is_en_alistamiento_not_con_faltantes(self):
		"""A Pick List whose only shortage report is Resuelto (not Abierto)
		must not be treated as con_faltantes -- it's back to en_alistamiento
		and must show up in Alistamientos activos again."""
		with fx.as_user(self.jefe_a):
			queue = bodega.get_queue()
			active_names = [pl["name"] for pl in jefe_bodega.get_active_pick_lists()]

		self.assertIn(
			self.pl_con_faltantes_resuelto.name, [pl["name"] for pl in queue["en_alistamiento"]]
		)
		self.assertNotIn(
			self.pl_con_faltantes_resuelto.name, [pl["name"] for pl in queue["con_faltantes"]]
		)
		self.assertIn(self.pl_con_faltantes_resuelto.name, active_names)

	def test_user_permission_by_warehouse_isolates_jefe_results(self):
		with fx.as_user(self.jefe_a):
			active_a = [pl["name"] for pl in jefe_bodega.get_active_pick_lists()]
		with fx.as_user(self.jefe_b):
			active_b = [pl["name"] for pl in jefe_bodega.get_active_pick_lists()]

		self.assertIn(self.pl_en_alistamiento.name, active_a)
		self.assertNotIn(self.pl_b_en_alistamiento.name, active_a)

		self.assertIn(self.pl_b_en_alistamiento.name, active_b)
		self.assertNotIn(self.pl_en_alistamiento.name, active_b)
