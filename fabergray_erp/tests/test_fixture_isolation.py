# -*- coding: utf-8 -*-
"""fixtures.TestWorld isolation from the site's real documents.

- Reporte de Faltante: inside a TestWorld it is named
  FG-TEST-<ns>-FALT-00001..., never from the real FALT-.YYYY.-.##### series,
  so a test can never reuse a real (or reverted) name that an old document
  -- e.g. a submitted Material Receipt via fg_shortage_report -- still
  points to.
- Invoice numbering: the number stays deterministic (6886...), the UNIQUE
  fg_invoice_number_key lives in the world's test namespace.
Production naming is never touched and the real FALT- series never moves."""

import inspect

import frappe
from frappe.model.naming import parse_naming_series
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from fabergray_erp.api import facturacion, jefe_bodega
from fabergray_erp.fabrigray_erp.doctype.reporte_de_faltante.reporte_de_faltante import ReportedeFaltante
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

PRODUCTION_AUTONAME = "FALT-.YYYY.-.#####"


def _real_series_prefix():
	return parse_naming_series("FALT-.YYYY.-")


def _real_series_current():
	row = frappe.db.sql("select current from `tabSeries` where name = %s", _real_series_prefix())
	return row[0][0] if row else None


class TestFixtureIsolation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.local.test_objects.setdefault("Reporte de Faltante", [])  # see test_reporte_de_faltante.py
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		sfx = cls.world.namespace
		cls.wh = cls.world.warehouse(f"FGISO {sfx}").name
		cls.item = cls.world.item(f"FGISO-{sfx}").name
		frappe.db.commit()

	def _report(self, world=None):
		return (world or self.world).shortage_report(
			item_code=self.item,
			warehouse=self.wh,
			qty_solicitada=10,
			qty_disponible=4,
			detected_by="Fulfillment Engine",
		)

	# 1 -----------------------------------------------------------------------

	def test_production_naming_is_unchanged(self):
		from fabergray_erp.fabrigray_erp.doctype.reporte_de_faltante import reporte_de_faltante

		self.assertEqual(frappe.get_meta("Reporte de Faltante").autoname, PRODUCTION_AUTONAME)
		self.assertEqual(_real_series_prefix(), f"FALT-{nowdate()[:4]}-")
		# The real controller defines no autoname: outside a TestWorld Frappe
		# names a report from the DocType expression above (FALT-2026-00001).
		self.assertNotIn("def autoname", inspect.getsource(reporte_de_faltante))
		self.assertEqual(self.world.production_invoice_number_key("integrandoMAS", 6886), "integrandoMAS-6886")

	def test_cleanup_restores_production_naming(self):
		"""A world started and cleaned up on its own leaves the controller and
		the invoice key exactly as they were before it."""
		before_autoname = vars(ReportedeFaltante).get("autoname")
		before_key = facturacion._invoice_number_key
		world = fx.TestWorld()
		self.assertIsNot(vars(ReportedeFaltante).get("autoname"), before_autoname)
		world.cleanup()
		self.assertIs(vars(ReportedeFaltante).get("autoname"), before_autoname)
		self.assertIs(facturacion._invoice_number_key, before_key)

	# 2 / 3 ------------------------------------------------------------------

	def test_reports_use_the_world_namespace_and_stay_unique(self):
		series_before = _real_series_current()
		first, second = self._report(), self._report()

		prefix = f"FG-TEST-{self.world.namespace}-FALT-"
		self.assertTrue(first.name.startswith(prefix), first.name)
		self.assertTrue(second.name.startswith(prefix), second.name)
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(int(second.name[len(prefix):]), int(first.name[len(prefix):]) + 1)
		self.assertFalse(first.name.startswith("FALT-"))
		self.assertEqual(_real_series_current(), series_before)  # real series never advanced

	# 4 -----------------------------------------------------------------------

	def test_two_worlds_never_collide(self):
		other = fx.TestWorld()
		try:
			mine_in_other = other.shortage_report(
				item_code=self.item, warehouse=self.wh, qty_solicitada=1, qty_disponible=0,
				detected_by="Fulfillment Engine",
			)
		finally:
			other.cleanup()
		mine = self._report()

		self.assertNotEqual(other.namespace, self.world.namespace)
		self.assertTrue(mine_in_other.name.startswith(f"FG-TEST-{other.namespace}-FALT-"))
		self.assertTrue(mine.name.startswith(f"FG-TEST-{self.world.namespace}-FALT-"))
		self.assertNotEqual(mine_in_other.name, mine.name)
		self.assertNotEqual(
			other.invoice_number_key("integrandoMAS", 6886), self.world.invoice_number_key("integrandoMAS", 6886)
		)

	# 5 -----------------------------------------------------------------------

	def test_an_orphan_receipt_never_belongs_to_a_new_test_report(self):
		"""A submitted Material Receipt still pointing to a deleted report
		(what test teardown leaves behind) never counts for a new report:
		the name is never handed out again."""
		old = self._report()
		receipt = frappe.new_doc("Stock Entry")
		receipt.stock_entry_type = "Material Receipt"
		receipt.company = fx.COMPANY
		receipt.fg_shortage_report = old.name
		receipt.append("items", {"item_code": self.item, "t_warehouse": self.wh, "qty": 4, "basic_rate": 100})
		receipt.insert()
		receipt.submit()
		self.world.track_existing("Stock Entry", receipt.name)
		frappe.delete_doc("Reporte de Faltante", old.name, force=True, ignore_permissions=True)
		receipts, received_qty = jefe_bodega._shortage_receipts(old.name)
		self.assertEqual(([r["stock_entry"] for r in receipts], received_qty), ([receipt.name], 4))

		new = self._report()

		self.assertNotEqual(new.name, old.name)
		self.assertEqual(jefe_bodega._shortage_receipts(new.name), ([], 0))

	def test_no_test_report_name_matches_a_name_real_documents_point_to(self):
		"""Site-independent form of the FALT-2026-02444 case: whatever real
		reports old receipts reference, a test report can never take it."""
		referenced = set(
			frappe.get_all(
				"Stock Entry", filters={"fg_shortage_report": ["is", "set"]}, pluck="fg_shortage_report"
			)
		)
		names = {self._report().name for _ in range(3)}
		self.assertFalse(names & referenced)
		self.assertFalse(any(name.startswith("FALT-") for name in names))
