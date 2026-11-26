# -*- coding: utf-8 -*-
"""Cartera masiva (fase A) -- volume: 100 and 1000 driver-reported
payments authorized through the real chain (Bodega -> Facturación ->
Recorridos deliver_stop "Pagado" + comprobante -> api.cartera_masiva), with
timings printed for the report. Slow on purpose (1000 real deliveries with
evidence files); kept apart from the functional suite."""

import time

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp import cartera_masiva_service as service
from fabergray_erp.tests.test_cartera_masiva import CarteraMasivaTestMixin

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestCarteraMasivaVolume(CarteraMasivaTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_cartera_world("FGCMV")

	def _volume(self, n):
		day = self._new_day()
		t0 = time.monotonic()
		obs = self._reported(n, day, customers=5)
		frappe.db.commit()
		t_seed = time.monotonic() - t0

		snapshot, counts = self._snapshot(obs), self._counts()
		t1 = time.monotonic()
		preview = self._preview(day, page_length=50)
		t_preview = time.monotonic() - t1
		self.assertEqual(preview["summary"]["eligible"], n)
		self.assertEqual(len(preview["rows"]), min(n, 50))
		self.assertEqual(len(preview["eligible_index"]), n)
		expected_cents = sum(r[2] for r in preview["eligible_index"])

		t2 = time.monotonic()
		result = self._authorize(day)
		t_run = time.monotonic() - t2

		self.assertEqual((result["status"], result["authorized"], result["skipped"], result["failed"]), (service.STATUS_DONE, n, 0, 0))
		self.assertEqual(round(result["authorized_amount"] * 100), expected_cents)
		self.assertEqual(sum(c["count"] for c in result["by_customer"]), n)
		self.assertEqual(len(result["by_customer"]), 5)
		self.assertEqual(
			frappe.db.count("Cartera Obligacion", {"name": ["in", obs], "payment_verification": "Confirmado", "payment_verified_batch": result["name"]}),
			n,
		)
		self.assertEqual(frappe.db.count(service.LINE_DOCTYPE, {"cartera_lote": result["name"], "result": service.RESULT_AUTHORIZED}), n)
		self.assertEqual(self._snapshot(obs), snapshot)  # no balance, date or payment changed
		self.assertEqual(self._counts(), counts)  # zero payments, entries, stock
		print(
			f"\n[autorización masiva {n} cobros] creación de datos de prueba: {t_seed:.1f}s | "
			f"vista previa (clasificación + totales exactos): {t_preview:.2f}s | "
			f"autorización (confirmación + proceso): {t_run:.2f}s ({t_run / n * 1000:.1f} ms/cobro) | "
			f"clientes: {len(result['by_customer'])}"
		)

	def test_volume_100(self):
		self._volume(100)

	def test_volume_1000(self):
		self._volume(1000)
