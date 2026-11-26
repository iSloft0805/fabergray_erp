# -*- coding: utf-8 -*-
"""Entrega masiva -- volume: 100 and 1000 orders through the real chain
(Bodega -> Facturación -> api.entrega_masiva), with timings printed for the
report. Slow on purpose (1000 real invoiced orders); kept apart from the
functional suite."""

import math
import time

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp import entrega_masiva_service as service
from fabergray_erp.tests.test_entrega_masiva import EntregaMasivaTestMixin

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestEntregaMasivaVolume(EntregaMasivaTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_world("FGEMV")

	def _volume(self, n):
		day = self._new_day()
		t0 = time.monotonic()
		half = n // 2
		credit = self._orders(half, day, terms=("Crédito", 30))
		cash = self._orders(n - half, day, terms=("Contado", 0))
		frappe.db.commit()
		t_seed = time.monotonic() - t0

		before = self._accounting_counts()
		t1 = time.monotonic()
		preview = self._preview(day, page_length=50)
		t_preview = time.monotonic() - t1
		self.assertEqual(preview["summary"]["eligible"], n)
		self.assertEqual(len(preview["rows"]), min(n, 50))

		t2 = time.monotonic()
		result = self._confirm(day)
		t_run = time.monotonic() - t2

		self.assertEqual((result["status"], result["delivered"], result["failed"], result["skipped"]), (service.STATUS_DONE, n, 0, 0))
		self.assertEqual(self._accounting_counts(), before)
		routes = result["administrative_routes"]
		self.assertEqual(len(routes), math.ceil(n / service.CHUNK_SIZE))
		self.assertEqual(
			set(frappe.get_all("Recorrido", filters={"name": ["in", routes]}, pluck="status")), {"Completado"}
		)
		orders = credit + cash
		self.assertEqual(
			frappe.db.count("Recorrido Parada", {"pick_list": ["in", orders], "status": "Entregado"}), n
		)
		obligations = frappe.get_all("Cartera Obligacion", filters={"pick_list": ["in", orders]}, fields=["pick_list", "paid_amount"])
		self.assertEqual(len(obligations), n)
		self.assertEqual(len({o.pick_list for o in obligations}), n)  # exactly one per order
		self.assertEqual(sum(o.paid_amount for o in obligations), 0)  # nothing marked paid
		print(
			f"\n[entrega masiva {n} pedidos] creación de datos de prueba: {t_seed:.1f}s | "
			f"vista previa (clasificación + valor exacto): {t_preview:.2f}s | "
			f"entrega masiva (confirmación + proceso + Cartera): {t_run:.2f}s "
			f"({t_run / n * 1000:.1f} ms/pedido) | recorridos administrativos: {len(routes)}"
		)

	def test_volume_100(self):
		self._volume(100)

	def test_volume_1000(self):
		self._volume(1000)
