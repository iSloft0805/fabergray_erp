# -*- coding: utf-8 -*-
"""Facturación con faltantes -- "Validar pedidos con faltantes resueltos"
mode "allow_invoice_with_shortage" (fulfillment.shortage_invoice,
fulfillment.resolved_shortage_service, api.facturacion, api.bodega,
api.recorridos).

The reference order throughout: A ordered 5 / picked 5 (in stock), B
ordered 2 / picked 0 (no stock), Reporte de Faltante of B Resuelto. Real
Bodega flow, real Facturación endpoints, real stock."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, flt

from fabergray_erp.api import bodega, facturacion, jefe_bodega, recorridos
from fabergray_erp.fulfillment import resolved_shortage_service as rss
from fabergray_erp.fulfillment import shortage_invoice, stock_issue_service
from fabergray_erp.fulfillment.remainder_service import ensure_remaining_pick_list
from fabergray_erp.tests import fixtures as fx
from fabergray_erp.tests.test_inventario_out_stock_issue import RATE, _actual, _stock_issues
from fabergray_erp.tests.test_resolved_shortage_validation import _ResolvedShortageMixin

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

STRICT = rss.MODE_STRICT_STOCK
ALLOW = rss.MODE_ALLOW_INVOICE_WITH_SHORTAGE
ISSUER = "integrandoMAS"
ORDER_RATE = 5000  # _StockIssueMixin._order()


class TestShortageInvoiceAuthorization(_ResolvedShortageMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._setup_resolved_world("FGSIA")
		cls.recorrido_user = cls.world.user(f"fgsia-recorrido-{cls.tag.lower()}@example.com", ["Recorrido"])
		frappe.db.commit()

	# -- helpers ----------------------------------------------------------

	def _short_order(self):
		"""A 5/5 in stock, B 0/2 without stock, B's report Resuelto."""
		item_a = self._item(self.wh_liq, stock=5)
		item_b = self._item(self.wh_liq)
		so, pl = self._order((item_a, self.wh_liq, 5), (item_b, self.wh_liq, 2))
		self._pick(pl, {item_b: 0})
		report = self._report_for(pl, item_b)
		self._resolve(report.name)
		return item_a, item_b, so, pl, report

	def _authorized(self):
		item_a, item_b, so, pl, report = self._short_order()
		row = self._row(self._run(mode=ALLOW), pl)
		self.assertEqual(row["status"], rss.STATUS_AUTHORIZED, row)
		return item_a, item_b, so, pl, report

	def _invoice(self, pl):
		with fx.as_user(self.facturacion_user):
			for item in facturacion.get_invoicing_detail(pl)["items"]:
				facturacion.set_invoicing_item_checked(pl, item["row_name"], 1)
			return facturacion.mark_as_invoiced(pl, ISSUER)

	def _invoiced(self):
		item_a, item_b, so, pl, report = self._authorized()
		self._invoice(pl)
		frappe.db.commit()
		return item_a, item_b, so, pl, report

	def _doc(self, pl):
		return frappe.get_doc("Pick List", pl)

	def _row_of(self, pl, item_code):
		return next(r for r in self._doc(pl).locations if r.item_code == item_code)

	def _sle_count(self, *items):
		return frappe.db.count("Stock Ledger Entry", {"item_code": ["in", list(items)], "is_cancelled": 0})

	def _comments(self, pl):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Pick List", "reference_name": pl, "comment_type": "Comment"},
			pluck="content",
			order_by="creation asc",
		)

	def _queue_entry(self, pl, status="Pendiente"):
		with fx.as_user(self.facturacion_user):
			queue = facturacion.get_invoicing_queue(status=status, txt=pl)
		rows = [r for r in queue["pick_lists"] if r["name"] == pl]
		return rows[0] if rows else None

	def _available_for_routes(self, pl):
		with fx.as_user(self.recorrido_user):
			result = recorridos.get_available_orders(txt=pl)
		rows = result.get("orders") or result.get("pick_lists") or result.get("results") or []
		return [r for r in rows if r.get("name") == pl or r.get("pick_list") == pl]

	def _restock(self, item, qty):
		self.world.stock_up_real(item, self.wh_liq, qty, rate=RATE)

	def _finish_by_bodega(self, pl, picked_by_item):
		with fx.as_user(self.bodega_user):
			for row in bodega.get_pick_list(pl)["rows"]:
				if row["item_code"] in picked_by_item:
					bodega.set_picked_qty(pl, row["row_name"], picked_by_item[row["item_code"]])
			return bodega.finish_picking(pl)

	# =====================================================================
	# Modes
	# =====================================================================

	def test_01_mode1_full_stock_completes_normally(self):
		item, _so, pl, _report = self._draft_with_resolved_shortage()
		row = self._row(self._run(mode=STRICT), pl)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		doc = self._doc(pl)
		self.assertEqual(doc.docstatus, 1)
		self.assertFalse(shortage_invoice.is_authorized(doc))
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(_actual(item, self.wh_liq), 0)

	def test_02_mode1_short_is_still_short_and_changes_nothing(self):
		item_a, item_b, _so, pl, _report = self._short_order()
		row = self._row(self._run(mode=STRICT), pl)
		self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
		doc = self._doc(pl)
		self.assertEqual(doc.docstatus, 0)
		self.assertFalse(shortage_invoice.is_authorized(doc))
		self.assertEqual(self._picked(pl), [5, 0])
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(self._comments(pl), [])

	def test_03_mode2_short_authorizes_without_moving_stock(self):
		item_a, item_b, so, pl, _report = self._short_order()
		sle_before = self._sle_count(item_a, item_b)

		row = self._row(self._run(mode=ALLOW), pl)

		self.assertEqual(row["status"], rss.STATUS_AUTHORIZED, row)
		self.assertIn(f"{item_b} — Pendiente: 2 · Disponible: 0", row["detail"])
		self.assertIsNone(row["material_issue"])
		doc = self._doc(pl)
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual(cint(doc.fg_shortage_invoice_authorized), 1)
		self.assertEqual(doc.fg_shortage_invoice_authorized_by, self.jefe)
		self.assertTrue(doc.fg_shortage_invoice_authorized_on)
		self.assertEqual(self._picked(pl), [5, 0])
		self.assertEqual([flt(r.transferred_qty) for r in doc.locations], [0, 0])
		self.assertEqual(_stock_issues(pl), [])
		self.assertEqual(self._sle_count(item_a, item_b), sle_before)
		self.assertEqual((_actual(item_a, self.wh_liq), _actual(item_b, self.wh_liq)), (5, 0))
		(note,) = self._comments(pl)
		self.assertIn(f"Autorizado para facturar con faltantes por {self.jefe}", note)
		self.assertIn(f"Pick List: {pl} · Orden de Venta: {so.name}", note)
		self.assertIn(f"- {item_a} — Pedido: 5 · Alistado: 5 · Pendiente: 0 · Disponible: 5", note)
		self.assertIn(f"- {item_b} — Pedido: 2 · Alistado: 0 · Pendiente: 2 · Disponible: 0", note)

	def test_04_mode2_full_stock_completes_without_authorization(self):
		item, _so, pl, _report = self._draft_with_resolved_shortage()
		row = self._row(self._run(mode=ALLOW), pl)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		doc = self._doc(pl)
		self.assertEqual(doc.docstatus, 1)
		self.assertFalse(shortage_invoice.is_authorized(doc))
		self.assertIsNone(doc.fg_shortage_invoice_authorized_by)
		self.assertEqual(len(_stock_issues(pl)), 1)

	def test_05_06_open_or_in_process_report_blocks_mode2(self):
		for status in ("Abierto", "En Proceso"):
			with self.subTest(status=status):
				item_a = self._item(self.wh_liq, stock=5)
				item_b = self._item(self.wh_liq)
				_so, pl = self._order((item_a, self.wh_liq, 5), (item_b, self.wh_liq, 2))
				self._pick(pl, {item_b: 0})
				report = self._report_for(pl, item_b)
				frappe.db.set_value("Reporte de Faltante", report.name, "status", status)
				self._mine(report.name)

				row = self._row(self._run(mode=ALLOW), pl)

				self.assertEqual(row["status"], rss.STATUS_STILL_SHORT, row)
				self.assertIn(
					f"{report.name} sigue Abierto/En Proceso. Resuélvelo antes de autorizar Facturación.", row["detail"]
				)
				self.assertFalse(shortage_invoice.is_authorized(self._doc(pl)))
				self.assertEqual(frappe.db.get_value("Reporte de Faltante", report.name, "status"), status)
				self.assertEqual(self._comments(pl), [])

	def test_07_bodega_cannot_use_mode2(self):
		_a, _b, _so, pl, _report = self._short_order()
		frappe.db.commit()
		with fx.as_user(self.bodega_user):
			with self.assertRaises(frappe.PermissionError):
				jefe_bodega.validate_resolved_shortage_orders(mode=ALLOW)
			with self.assertRaises(frappe.PermissionError):
				rss.validate_resolved_shortage_orders(mode=ALLOW)
		self.assertFalse(shortage_invoice.is_authorized(self._doc(pl)))

	def test_08_jefe_can_use_mode2_and_only_two_modes_exist(self):
		_a, _b, _so, pl, _report = self._short_order()
		row = self._row(self._run(mode=ALLOW), pl)  # through api.jefe_bodega
		self.assertEqual(row["status"], rss.STATUS_AUTHORIZED)
		for bad in ("force", "allow", "STRICT_STOCK", "1"):
			with self.subTest(mode=bad), fx.as_user(self.jefe):
				with self.assertRaises(frappe.ValidationError):
					jefe_bodega.validate_resolved_shortage_orders(mode=bad)
				with self.assertRaises(frappe.ValidationError):
					rss.validate_resolved_shortage_orders(mode=bad)

	def test_bodega_cannot_write_the_authorization_itself(self):
		_a, _b, _so, pl, _report = self._short_order()
		with fx.as_user(self.bodega_user):
			doc = self._doc(pl)
			doc.fg_shortage_invoice_authorized = 1
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		self.assertFalse(shortage_invoice.is_authorized(self._doc(pl)))

	# =====================================================================
	# Facturación
	# =====================================================================

	def test_09_facturacion_sees_only_the_authorized_draft(self):
		_a, _b, _so, pl, _report = self._short_order()
		self.assertIsNone(self._queue_entry(pl))
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(facturacion.PickListNotReadyForInvoicingError):
				facturacion.get_invoicing_detail(pl)
			with self.assertRaises(facturacion.PickListNotReadyForInvoicingError):
				facturacion.mark_as_invoiced(pl, ISSUER)
			before = facturacion.get_invoicing_summary()["pendientes"]

		self._run(mode=ALLOW)

		entry = self._queue_entry(pl)
		self.assertIsNotNone(entry)
		self.assertTrue(entry["authorized_with_shortage"])
		self.assertEqual(entry["fg_invoicing_status"], "Pendiente")
		with fx.as_user(self.facturacion_user):
			self.assertEqual(facturacion.get_invoicing_summary()["pendientes"], before + 1)

	def test_10_23_invoice_bills_the_full_order(self):
		item_a, item_b, so, pl, _report = self._authorized()

		entry = self._queue_entry(pl)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl)
			pricing = facturacion.get_invoicing_pricing(pl)

		self.assertEqual(entry["total_qty"], 7)  # 5 + 2, not picked 5 + 0
		self.assertTrue(detail["authorized_with_shortage"])
		self.assertEqual(detail["shortage_authorized_by"], self.jefe)
		lines = {i["item_code"]: i for i in detail["items"]}
		self.assertEqual(
			{k: (v["qty"], v["ordered_qty"], v["picked_qty"], v["pending_qty"]) for k, v in lines.items()},
			{item_a: (5, 5, 5, 0), item_b: (2, 2, 0, 2)},
		)
		self.assertEqual(detail["total_qty"], 7)
		self.assertEqual([line["qty"] for line in pricing["lines"]], [5, 2])
		self.assertEqual(pricing["totals"]["total"], 7 * ORDER_RATE)

	def test_11_pdf_and_detail_use_the_same_quantities(self):
		_a, _b, so, pl, _report = self._invoiced()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl)
			pricing = facturacion.get_invoicing_pricing(pl)
			lines, totals = facturacion._build_invoice_lines_and_totals(self._doc(pl), frappe.get_doc("Sales Order", so.name))
		self.assertEqual([line["qty"] for line in lines], [i["qty"] for i in detail["items"]])
		self.assertEqual([line["qty"] for line in lines], [5, 2])
		self.assertEqual(totals["total"], pricing["totals"]["total"])
		self.assertEqual(totals["total"], 7 * ORDER_RATE)

	def test_12_second_facturar_is_already_invoiced_while_still_draft(self):
		_a, _b, _so, pl, _report = self._invoiced()
		number = frappe.db.get_value("Pick List", pl, "fg_invoice_number")
		self.assertEqual(self._doc(pl).docstatus, 0)
		with fx.as_user(self.facturacion_user), self.assertRaises(facturacion.AlreadyInvoicedError):
			facturacion.mark_as_invoiced(pl, ISSUER)
		self.assertEqual(frappe.db.get_value("Pick List", pl, "fg_invoice_number"), number)

	# =====================================================================
	# Frozen once invoiced
	# =====================================================================

	def test_13_invoiced_draft_is_commercially_frozen(self):
		item_a, item_b, _so, pl, _report = self._invoiced()
		other_item = self._item(self.wh_var)
		changes = {
			"item_code": lambda d: setattr(d.locations[1], "item_code", other_item),
			"warehouse": lambda d: setattr(d.locations[1], "warehouse", self.wh_var),
			"stock_qty": lambda d: (setattr(d.locations[1], "qty", 1), setattr(d.locations[1], "stock_qty", 1)),
			"sales_order_item": lambda d: setattr(d.locations[1], "sales_order_item", d.locations[0].sales_order_item),
			"add row": lambda d: d.append(
				"locations", {"item_code": item_b, "warehouse": self.wh_liq, "qty": 1, "stock_qty": 1, "conversion_factor": 1}
			),
			"remove row": lambda d: d.remove(d.locations[1]),
			"picked decreases": lambda d: setattr(d.locations[0], "picked_qty", 4),
		}
		for label, change in changes.items():
			with self.subTest(change=label):
				doc = self._doc(pl)
				change(doc)
				with self.assertRaises(shortage_invoice.ShortageInvoiceFrozenError):
					doc.save()
				frappe.db.rollback()
		with self.assertRaises(shortage_invoice.ShortageInvoiceFrozenError):
			frappe.delete_doc("Pick List", pl)
		frappe.db.rollback()
		self.assertTrue(frappe.db.exists("Pick List", pl))
		self.assertEqual(self._picked(pl), [5, 0])

	def test_14_picked_qty_can_still_go_up(self):
		_a, item_b, _so, pl, _report = self._invoiced()
		self._restock(item_b, 1)
		with fx.as_user(self.bodega_user):
			bodega.set_picked_qty(pl, self._row_of(pl, item_b).name, 1)
		self.assertEqual(self._picked(pl), [5, 1])

	def test_15_partial_completion_of_an_invoiced_draft_is_refused(self):
		item_a, item_b, so, pl, _report = self._invoiced()
		self._restock(item_b, 1)

		with self.assertRaises(shortage_invoice.ShortageInvoicePartialCompletionError):
			self._finish_by_bodega(pl, {item_b: 1})
		frappe.db.rollback()
		self.assertEqual(self._doc(pl).docstatus, 0)
		self.assertEqual(_stock_issues(pl), [])

		# Whatever path submits: the before_submit guard holds too.
		doc = self._doc(pl)
		doc.locations[1].picked_qty = 1
		doc.flags.ignore_permissions = True
		with self.assertRaises(shortage_invoice.ShortageInvoicePartialCompletionError):
			with stock_issue_service.controlled_pick_list_submit(pl):
				doc.submit()
		frappe.db.rollback()
		self.assertEqual(self._doc(pl).docstatus, 0)
		self.assertEqual(self._pick_lists_of(so.name), {pl})

	def test_16_24_full_physical_completion_issues_once_and_never_reinvoices(self):
		item_a, item_b, so, pl, _report = self._invoiced()
		number = frappe.db.get_value("Pick List", pl, "fg_invoice_number")
		sle_before = self._sle_count(item_a, item_b)
		self.assertEqual((_actual(item_a, self.wh_liq), _actual(item_b, self.wh_liq)), (5, 0))
		with fx.as_user(self.facturacion_user):
			facturados = facturacion.get_invoicing_summary()["facturados"]

		self._restock(item_b, 2)
		result = self._finish_by_bodega(pl, {item_b: 2})

		doc = self._doc(pl)
		self.assertEqual(doc.docstatus, 1)
		self.assertFalse(result["already_completed"])
		for row in doc.locations:
			self.assertEqual((flt(row.picked_qty), flt(row.transferred_qty)), (flt(row.stock_qty), flt(row.stock_qty)))
		self.assertEqual(len(_stock_issues(pl)), 1)
		# Inventory moved only now, once, by the real Material Issue (+ the restock).
		self.assertEqual(self._sle_count(item_a, item_b), sle_before + 1 + 2)
		self.assertEqual((_actual(item_a, self.wh_liq), _actual(item_b, self.wh_liq)), (0, 0))
		self.assertEqual(doc.fg_invoicing_status, "Facturado")
		self.assertEqual(doc.fg_invoice_number, number)
		self.assertTrue(shortage_invoice.is_authorized(doc))  # kept as audit
		with fx.as_user(self.facturacion_user):
			self.assertEqual(facturacion.get_invoicing_summary()["facturados"], facturados)
			detail = facturacion.get_invoicing_detail(pl)
		self.assertFalse(detail["authorized_with_shortage"])
		self.assertEqual([i["qty"] for i in detail["items"]], [5, 2])
		self.assertEqual(self._pick_lists_of(so.name), {pl})

	def test_button_mode1_completes_an_invoiced_draft_once_stock_arrives(self):
		_a, item_b, so, pl, _report = self._invoiced()
		self._restock(item_b, 2)
		row = self._row(self._run(mode=STRICT), pl)
		self.assertEqual(row["status"], rss.STATUS_COMPLETED, row)
		self.assertEqual(self._doc(pl).docstatus, 1)
		self.assertEqual(len(_stock_issues(pl)), 1)
		self.assertEqual(self._pick_lists_of(so.name), {pl})

	def test_17_no_second_invoiceable_remainder(self):
		_a, _b, so, pl, report = self._invoiced()
		self.assertIsNone(ensure_remaining_pick_list(report.name))
		self.assertEqual(sorted((r.kind, r.pick_list) for r in self._scoped_candidates()), [(rss.KIND_DRAFT, pl)])
		second = self._run(mode=ALLOW)
		self.assertEqual(self._row(second, pl)["status"], rss.STATUS_AUTHORIZED)
		self.assertEqual(self._pick_lists_of(so.name), {pl})

	# =====================================================================
	# Recorridos
	# =====================================================================

	def test_18_19_recorridos_only_after_physical_completion(self):
		_a, item_b, _so, pl, _report = self._invoiced()
		self.assertEqual(self._available_for_routes(pl), [])

		self._restock(item_b, 2)
		self._finish_by_bodega(pl, {item_b: 2})

		self.assertEqual(len(self._available_for_routes(pl)), 1)

	# =====================================================================
	# Revocation, idempotency
	# =====================================================================

	def test_20_revoke_before_invoicing(self):
		item_a, _b, _so, pl, _report = self._authorized()
		with fx.as_user(self.jefe):
			self.assertEqual(jefe_bodega.revoke_shortage_invoice_authorization(pl), {"pick_list": pl})
		doc = self._doc(pl)
		self.assertEqual(
			(doc.fg_shortage_invoice_authorized, doc.fg_shortage_invoice_authorized_by, doc.fg_shortage_invoice_authorized_on),
			(0, None, None),
		)
		self.assertEqual(self._picked(pl), [5, 0])
		self.assertEqual(_actual(item_a, self.wh_liq), 5)
		self.assertIn(f"revocada por {self.jefe}", self._comments(pl)[-1])
		self.assertIsNone(self._queue_entry(pl))

	def test_21_revoke_after_invoicing_is_refused(self):
		_a, _b, _so, pl, _report = self._invoiced()
		with fx.as_user(self.bodega_user), self.assertRaises(frappe.PermissionError):
			jefe_bodega.revoke_shortage_invoice_authorization(pl)
		with fx.as_user(self.jefe), self.assertRaises(shortage_invoice.ShortageInvoiceRevokeError):
			jefe_bodega.revoke_shortage_invoice_authorization(pl)
		self.assertTrue(shortage_invoice.is_authorized(self._doc(pl)))

	def test_22_repeating_the_authorization_writes_nothing(self):
		_a, _b, _so, pl, _report = self._authorized()
		before = frappe.db.get_value(
			"Pick List", pl, ["modified", "fg_shortage_invoice_authorized_on"], as_dict=True
		)
		comments = self._comments(pl)

		row = self._row(self._run(mode=ALLOW), pl)

		self.assertEqual(row["status"], rss.STATUS_AUTHORIZED)
		self.assertIn("Ya estaba autorizado", row["detail"])
		self.assertEqual(
			frappe.db.get_value("Pick List", pl, ["modified", "fg_shortage_invoice_authorized_on"], as_dict=True),
			before,
		)
		self.assertEqual(self._comments(pl), comments)

	# =====================================================================
	# UI contracts (no JS runner in this app)
	# =====================================================================

	def test_ui_contracts(self):
		def read(page):
			path = frappe.get_app_path("fabergray_erp", "fabrigray_erp", "page", page, f"{page}.js")
			with open(path, encoding="utf-8") as f:
				return f.read()

		factura = read("facturacion")
		self.assertIn('__("AUTORIZADO CON FALTANTES")', factura)
		self.assertIn("r.authorized_with_shortage", factura)
		self.assertIn("item.pending_qty", factura)
		jefe = read("jefe_pick_lists")
		self.assertIn('"revoke_shortage_invoice_authorization"', jefe)
		self.assertIn(
			"Este pedido ya fue facturado con faltantes. Debe completar físicamente todas las cantidades "
			"antes de finalizar el alistamiento.",
			jefe,
		)
		self.assertIn("detail.invoiced_with_shortage", read("bodega"))
		_a, _b, _so, pl, _report = self._invoiced()
		with fx.as_user(self.bodega_user):
			view = bodega.get_pick_list(pl)
		self.assertTrue(view["invoiced_with_shortage"])
		self.assertEqual(view["invoiced_with_shortage_message"], shortage_invoice.MSG_INVOICED_WITH_SHORTAGE)
