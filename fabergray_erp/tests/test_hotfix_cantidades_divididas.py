# -*- coding: utf-8 -*-
"""HOTFIX cantidades divididas -- reproduces the incident and proves it is
fixed, through the real chain (Sales Order submit path -> Pick List ->
Bodega -> Material Issue -> Facturación -> PDF -> Cartera -> Recorridos) on
isolated TestWorld data. The Bodega force-complete policy is restored after
every test that changes it."""

import json
import threading
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from fabergray_erp import cartera_service, commercial_lines
from fabergray_erp.api import bodega, facturacion, recorridos
from fabergray_erp.fulfillment import bodega_settings, engine, pick_list_guard, shortage_invoice
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

NO_MONEY = ("Payment Entry", "Journal Entry", "GL Entry", "Sales Invoice")


class TestHotfixCantidadesDivididas(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld(real_stock=True)
		cls.addClassCleanup(cls.world.cleanup)
		ns = cls.world.namespace
		low = ns.lower()
		cls.wh = cls.world.warehouse(f"FGHF A {ns}")
		cls.wh_b = cls.world.warehouse(f"FGHF B {ns}")
		cls.customer = cls.world.customer(f"FGHF Cliente {ns}")
		cls.bodega_user = cls.world.user(f"fghf-bodega-{low}@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user(f"fghf-fact-{low}@example.com", ["Facturación"])
		cls.sm_user = cls.world.user(f"fghf-sm-{low}@example.com", ["System Manager"])
		cls._seq = 0

	# -- helpers -----------------------------------------------------------------
	def _item(self, stock_uom="Nos", box=None):
		type(self)._seq += 1
		item = self.world.item(f"ESC-OBA-TEST-{self.world.namespace}-{self._seq}", stock_uom=stock_uom)
		if box:
			doc = frappe.get_doc("Item", item.name)
			doc.append("uoms", {"uom": "Box", "conversion_factor": box})
			doc.save()
		return item

	def _order(self, lines):
		items = []
		for line in lines:
			row = {"item_code": line["item_code"], "qty": line["qty"], "rate": line.get("rate", 100), "warehouse": self.wh.name}
			if line.get("uom"):
				row["uom"] = line["uom"]
			if line.get("discount_percentage"):
				row["price_list_rate"] = line["rate"]
				row["discount_percentage"] = line["discount_percentage"]
				row["rate"] = flt(line["rate"] * (1 - line["discount_percentage"] / 100), 2)
			items.append(row)
		so = self.world.multi_item_sales_order(self.customer.name, items)
		engine.process_sales_order_for_confirmation(so)  # exactly what Ventas' submit hook runs
		self.world.track_existing_pick_lists_and_reports_for(so.name)
		names = sorted({r.parent for r in frappe.get_all("Pick List Item", filters={"sales_order": so.name}, fields=["parent"])})
		return frappe.get_doc("Sales Order", so.name), [frappe.get_doc("Pick List", n) for n in names]

	@contextmanager
	def _force(self, enabled):
		before = bodega_settings.force_complete_enabled()
		if before != enabled:
			bodega_settings.set_force_complete(enabled)
		try:
			yield
		finally:
			if bodega_settings.force_complete_enabled() != before:
				bodega_settings.set_force_complete(before)

	def _rows(self, pl):
		return frappe.get_doc("Pick List", pl.name).get("locations")

	def _group(self, pl):
		with fx.as_user(self.bodega_user):
			detail = bodega.get_pick_list(pl.name)
		self.assertEqual(len(detail["groups"]), 1, detail["groups"])
		return detail["groups"][0]

	def _pick_all(self, pl, force=True, available=None):
		group = self._group(pl)
		with self._force(force), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			bodega.set_group_picked_qty(pl.name, group["group"], group["qty_solicitada"] if available is None else available)
			if available is not None and available < group["qty_solicitada"]:
				self._track_reports(bodega.report_group_shortage(pl.name, group["group"], available, "Stock insuficiente"))
			result = bodega.finish_picking(pl.name)
		for se in frappe.get_all("Stock Entry", filters={"pick_list": pl.name}, pluck="name"):
			self.world.track_existing("Stock Entry", se)
		return result

	def _invoice(self, pl, issuer="integrandoMAS"):
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for g in detail["groups"]:
				facturacion.set_invoicing_group_checked(pl.name, g["group"], 1)
			facturacion.mark_as_invoiced(pl.name, issuer, payment_method="Contado", due_date=nowdate())
		return detail

	def _lines(self, pl, so):
		return facturacion._build_invoice_lines_and_totals(frappe.get_doc("Pick List", pl.name), frappe.get_doc("Sales Order", so.name))

	def _issued(self, pl):
		return frappe.db.sql(
			"""select d.qty, d.uom, d.transfer_qty from `tabStock Entry Detail` d
			join `tabStock Entry` se on se.name = d.parent where se.pick_list = %s and se.docstatus = 1 order by d.idx""",
			(pl.name,),
			as_dict=True,
		)

	def _track_reports(self, result):
		for r in result["reports"]:
			self.world.track_existing("Reporte de Faltante", r if isinstance(r, str) else r["name"])
		return result

	def _report_short(self, pl):
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			g = bodega.get_pick_list(pl.name)["groups"][0]
			bodega.set_group_picked_qty(pl.name, g["group"], 3)
			self._track_reports(bodega.report_group_shortage(pl.name, g["group"], 3, "Stock insuficiente"))

	def _split_4_3(self):
		item = self._item()
		self.world.stock_up(item.name, self.wh.name, 3)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 4, "rate": 100}])
		return item, so, pl

	# =====================================================================
	# The original incident
	# =====================================================================

	def test_01_split_3_plus_1_is_one_commercial_line(self):
		item, so, pl = self._split_4_3()
		rows = self._rows(pl)
		self.assertEqual([(flt(r.stock_qty), r.uom, flt(r.conversion_factor), r.stock_uom) for r in rows], [(3, "Nos", 1, "Nos"), (1, "Nos", 1, "Nos")])
		self.assertEqual({r.sales_order_item for r in rows}, {so.items[0].name})
		g = self._group(pl)
		self.assertEqual((g["qty_solicitada"], g["qty_disponible"], g["qty_faltante"], len(g["assignments"])), (4, 3, 1, 2))
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		entry = next(e for bucket in queue.values() if isinstance(bucket, list) for e in bucket if e["name"] == pl.name)
		self.assertEqual(entry["item_count"], 1)  # an internal split is not a second product

	def test_02_rows_cannot_be_deleted_by_anyone(self):
		_item, _so, pl = self._split_4_3()
		for user in (self.bodega_user, self.facturacion_user, self.sm_user, "Administrator"):
			with fx.as_user(user):
				doc = frappe.get_doc("Pick List", pl.name)
				doc.remove(doc.get("locations")[1])
				doc.flags.ignore_permissions = True
				with self.assertRaises(pick_list_guard.PickListStructureError, msg=user):
					doc.save()
		# REST / frappe.client: the whole document without the row -- as
		# Administrator (every permission): the guard still refuses. (System
		# Manager alone has no write on Pick List here.)
		with fx.as_user("Administrator"):
			data = frappe.get_doc("Pick List", pl.name).as_dict()
			data["locations"] = data["locations"][:1]
			with self.assertRaises(pick_list_guard.PickListStructureError):
				frappe.client.save(data)
		self._report_short(pl)
		with fx.as_user("Administrator"):
			with self.assertRaises(pick_list_guard.PickListStructureError):
				frappe.client.delete("Pick List", pl.name)  # would orphan Bodega's shortage report
		self.assertEqual([flt(r.stock_qty) for r in self._rows(pl)], [3, 1])

	def test_03_quantities_references_and_duplicates_are_protected(self):
		item, so, pl = self._split_4_3()
		other = self._item()
		cases = {
			"reduce stock_qty": lambda r: r.update({"stock_qty": 0.5, "qty": 0.5}),
			"change item": lambda r: r.update({"item_code": other.name}),
			"change sales order line": lambda r: r.update({"sales_order_item": "otra-linea"}),
			"change uom": lambda r: r.update({"uom": "Box"}),
			"change factor": lambda r: r.update({"conversion_factor": 6}),
			"change warehouse": lambda r: r.update({"warehouse": self.wh_b.name}),
		}
		for label, change in cases.items():
			with fx.as_user("Administrator"):
				doc = frappe.get_doc("Pick List", pl.name)
				change(doc.get("locations")[1])
				with self.assertRaises(pick_list_guard.PickListStructureError, msg=label):
					doc.save()
		with fx.as_user("Administrator"):
			doc = frappe.get_doc("Pick List", pl.name)
			dup = doc.get("locations")[1].as_dict()
			for f in ("name", "idx"):
				dup.pop(f, None)
			doc.append("locations", dup)
			with self.assertRaises(pick_list_guard.PickListStructureError):
				doc.save()
		# A second Pick List claiming the same order line again (crafted via
		# Desk/REST with explicit rows; ERPNext's own "Create Pick List"
		# already finds nothing left to allocate) -> duplicated quantities.
		with fx.as_user("Administrator"):
			second = frappe.get_doc(
				{
					"doctype": "Pick List",
					"company": pl.company,
					"purpose": "Delivery",
					"pick_manually": 1,
					"customer": pl.customer,
					"locations": [
						{
							"item_code": item.name, "warehouse": self.wh.name, "sales_order": so.name,
							"sales_order_item": so.items[0].name, "qty": 4, "stock_qty": 4, "conversion_factor": 1,
							"uom": "Nos", "stock_uom": "Nos",
						}
					],
				}
			)
			with self.assertRaises(pick_list_guard.PickListQuantityError):
				second.insert()
		self.assertEqual([flt(r.stock_qty) for r in self._rows(pl)], [3, 1])

	def test_04_shortage_without_authorization_blocks_with_authorized_options(self):
		_item, _so, pl = self._split_4_3()
		with self._force(False), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			g = self._group(pl)
			bodega.set_group_picked_qty(pl.name, g["group"], 3)
			result = self._track_reports(bodega.report_group_shortage(pl.name, g["group"], 3, "Stock insuficiente"))
			self.assertEqual(len(result["reports"]), 1)  # only the unpicked internal row is short
			with self.assertRaises(frappe.ValidationError) as ctx:
				bodega.finish_picking(pl.name)
		message = str(ctx.exception)
		self.assertIn("Opciones autorizadas", message)
		self.assertNotIn("Ajusta", message)
		self.assertIn("No elimines productos", message)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "docstatus"), 0)
		self.assertEqual([(flt(r.stock_qty), flt(r.picked_qty)) for r in self._rows(pl)], [(3, 3), (1, 0)])

	def test_05_invoice_with_authorized_shortage_is_one_line_of_4(self):
		_item, so, pl = self._split_4_3()
		shortage_invoice.set_authorization(frappe.get_doc("Pick List", pl.name))
		detail = self._invoice(pl)
		self.assertEqual([(g["qty"], g["uom"]) for g in detail["groups"]], [(4, "Nos")])
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["amount"]) for l in lines], [(4, 400)])
		self.assertEqual(totals["total"], 400)
		pl = frappe.get_doc("Pick List", pl.name)
		self.assertEqual(pl.fg_invoice_calc_version, commercial_lines.CALC_VERSION_COMMERCIAL)
		self.assertEqual(json.loads(pl.fg_invoice_snapshot)["totals"]["total"], 400)

	def test_06_force_complete_issues_and_invoices_4(self):
		_item, so, pl = self._split_4_3()
		result = self._pick_all(pl, force=True)
		money = {dt: frappe.db.count(dt) for dt in NO_MONEY}  # invoicing never posts entries
		self.assertTrue(result["completed_without_stock"])
		self.assertEqual([(flt(d.qty), d.uom, flt(d.transfer_qty)) for d in self._issued(pl)], [(3, "Nos", 3), (1, "Nos", 1)])
		self._invoice(pl)
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["uom"], l["amount"]) for l in lines], [(4, "Nos", 400)])
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 400)
		snap = recorridos._resolve_pick_list_snapshot(frappe.get_doc("Pick List", pl.name))
		self.assertEqual((snap["item_count"], snap["total_qty"]), (1, 4))
		self.assertEqual({dt: frappe.db.count(dt) for dt in NO_MONEY}, money)

	def test_07_totally_missing_product(self):
		item = self._item()
		so, (pl,) = self._order([{"item_code": item.name, "qty": 4}])
		self.assertEqual([(flt(r.stock_qty), r.stock_uom) for r in self._rows(pl)], [(4, "Nos")])
		self.assertEqual(self._group(pl)["qty_faltante"], 4)

	# =====================================================================
	# Commercial lines: prices, discounts, units, decimals, 4+3+3
	# =====================================================================

	def test_08_three_internal_rows_4_3_3_are_one_line_of_10(self):
		item = self._item()
		self.world.stock_up(item.name, self.wh.name, 4)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 10, "rate": 100}])
		self.assertEqual([flt(r.stock_qty) for r in self._rows(pl)], [4, 6])  # 4 in stock + one top-up
		self._pick_all(pl, force=True)
		self._invoice(pl)
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["amount"]) for l in lines], [(10, 1000)])
		# The builder also merges an n-way split (e.g. 4 + 3 + 3) of one line.
		fake = frappe._dict(name="x", fg_invoicing_status=None, locations=[
			frappe._dict(name=f"r{i}", idx=i, item_code=item.name, sales_order_item=so.items[0].name, stock_qty=q, picked_qty=q,
				fg_invoice_rate=0, fg_invoice_public_rate=0, uom="Nos", item_name=item.name, get=lambda f, d=None: None)
			for i, q in enumerate((4, 3, 3), start=1)
		])
		groups = commercial_lines.group_rows(fake.locations)
		self.assertEqual(len(groups), 1)
		self.assertEqual(sum(flt(r.stock_qty) for r in groups[0][1]), 10)

	def test_09_same_product_different_prices_and_discounts_stay_separate(self):
		item = self._item()
		self.world.stock_up(item.name, self.wh.name, 20)
		so, (pl,) = self._order([
			{"item_code": item.name, "qty": 2, "rate": 100},
			{"item_code": item.name, "qty": 2, "rate": 80},
			{"item_code": item.name, "qty": 2, "rate": 100, "discount_percentage": 10},
		])
		self._pick_all_multi(pl)
		self._invoice(pl)
		lines, totals = self._lines(pl, so)
		self.assertEqual(sorted((l["qty"], l["rate"], l["amount"]) for l in lines), [(2, 80, 160), (2, 90, 180), (2, 100, 200)])
		self.assertEqual(totals["total"], 540)

	def _pick_all_multi(self, pl):
		with self._force(True), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for g in bodega.get_pick_list(pl.name)["groups"]:
				bodega.set_group_picked_qty(pl.name, g["group"], g["qty_solicitada"])
			bodega.finish_picking(pl.name)
		for se in frappe.get_all("Stock Entry", filters={"pick_list": pl.name}, pluck="name"):
			self.world.track_existing("Stock Entry", se)

	def test_10_two_boxes_of_6_invoice_1200_not_7200(self):
		item = self._item(stock_uom="Unidad", box=6)
		self.world.stock_up(item.name, self.wh.name, 12)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
		self._pick_all(pl, force=False)
		self.assertEqual(sum(flt(d.transfer_qty) for d in self._issued(pl)), 12)  # 12 units leave stock
		self._invoice(pl)
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["uom"], l["rate"], l["amount"]) for l in lines], [(2, "Box", 600, 1200)])
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 1200)

	def test_11_boxes_with_partial_availability(self):
		item = self._item(stock_uom="Unidad", box=6)
		self.world.stock_up(item.name, self.wh.name, 8)  # 1 box + 2 loose units
		so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
		rows = self._rows(pl)
		self.assertEqual([(flt(r.qty), r.uom, flt(r.conversion_factor), flt(r.stock_qty), r.stock_uom) for r in rows],
			[(1, "Box", 6, 6, "Unidad"), (1, "Box", 6, 6, "Unidad")])  # the top-up keeps the line's unit
		self._pick_all(pl, force=True)
		self.assertEqual([(d.uom, flt(d.transfer_qty)) for d in self._issued(pl)], [("Unidad", 6), ("Unidad", 6)])
		self._invoice(pl)
		lines, _totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["uom"], l["amount"]) for l in lines], [(2, "Box", 1200)])

	def test_12_decimal_quantities(self):
		item = self._item(stock_uom="Unidad")
		self.world.stock_up(item.name, self.wh.name, 3.25)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 4.5, "rate": 100}])
		self.assertEqual([(flt(r.stock_qty), r.stock_uom) for r in self._rows(pl)], [(3.25, "Unidad"), (1.25, "Unidad")])
		self._pick_all(pl, force=True)  # failed before the hotfix (top-up row in UOM "Nos")
		self.assertEqual([(d.uom, flt(d.transfer_qty)) for d in self._issued(pl)], [("Unidad", 3.25), ("Unidad", 1.25)])
		self._invoice(pl)
		lines, _totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["amount"]) for l in lines], [(4.5, 450)])

	def test_13_indivisible_unit_is_refused_not_rounded(self):
		with self.assertRaises(commercial_lines.CommercialQuantityError):
			commercial_lines.to_commercial_qty(7, 6, "Box", label="Caja")
		self.assertEqual(commercial_lines.to_commercial_qty(12, 6, "Box"), 2)
		self.assertEqual(commercial_lines.to_commercial_qty(1.25, 1, "Unidad"), 1.25)

	# =====================================================================
	# PDF (three issuers), historical invoices, Cartera
	# =====================================================================

	def test_14_pdf_of_the_three_issuers(self):
		from fabergray_erp import invoice_issuers

		for issuer in ("integrandoMAS", "ecoluminar", "3CORAZONESCAFES"):
			item = self._item(stock_uom="Unidad", box=6)
			self.world.stock_up(item.name, self.wh.name, 12)
			so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
			self._pick_all(pl, force=False)
			self._invoice(pl, issuer=issuer)
			with fx.as_user(self.facturacion_user):
				html = frappe.get_print("Pick List", pl.name, invoice_issuers.invoice_print_format(issuer), no_letterhead=1)
			self.assertIn("1.200", html, issuer)
			self.assertNotIn("7.200", html, issuer)

	def test_15_historical_invoices_keep_their_amounts(self):
		item = self._item(stock_uom="Unidad", box=6)
		self.world.stock_up(item.name, self.wh.name, 12)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
		self._pick_all(pl, force=False)
		self._invoice(pl)
		# Test setup: the same invoice as if it had been issued BEFORE the
		# hotfix (no calculation version, no snapshot -- every historical one).
		frappe.db.set_value("Pick List", pl.name, {"fg_invoice_calc_version": 0, "fg_invoice_snapshot": None}, update_modified=False)
		lines, totals = self._lines(pl, so)
		self.assertEqual(totals["total"], 7200)  # reprint = what was issued then
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 7200)  # a later obligation too

	def test_16_snapshot_and_version_are_immutable(self):
		_item, _so, pl = self._split_4_3()
		self._pick_all(pl, force=True)
		self._invoice(pl)
		for field, value in (("fg_invoice_snapshot", "{}"), ("fg_invoice_calc_version", 0)):
			with fx.as_user(self.sm_user):
				doc = frappe.get_doc("Pick List", pl.name)
				doc.set(field, value)
				with self.assertRaises(frappe.PermissionError, msg=field):
					doc.save()

	# =====================================================================
	# Pre-hotfix inconsistent orders
	# =====================================================================

	def test_17_pre_hotfix_deleted_row_is_refused_not_completed_short(self):
		_item, _so, pl = self._split_4_3()
		with self._force(False), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			g = self._group(pl)
			bodega.set_group_picked_qty(pl.name, g["group"], 3)
			self._track_reports(bodega.report_group_shortage(pl.name, g["group"], 3, "Stock insuficiente"))
		# Test setup: the row deleted the way it happened BEFORE this hotfix.
		row = self._rows(pl)[1]
		frappe.db.delete("Pick List Item", {"name": row.name})
		with self._force(False), fx.as_user(self.bodega_user), self.assertRaises(pick_list_guard.PickListQuantityError) as ctx:
			bodega.finish_picking(pl.name)
		self.assertIn("faltan 1", str(ctx.exception))
		problems = pick_list_guard.report_inconsistent_open_orders()
		self.assertTrue(any(p["pick_list"] == pl.name for p in problems))
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "docstatus"), 0)  # nothing corrected automatically

	def test_18_pre_hotfix_short_order_cannot_be_invoiced(self):
		_item, _so, pl = self._split_4_3()
		shortage_invoice.set_authorization(frappe.get_doc("Pick List", pl.name))
		# Test setup: the row removed through a normal save, as Desk allowed
		# BEFORE this hotfix (the token stands in for the absent guard); the
		# Pick List's own change history records the removal.
		doc = frappe.get_doc("Pick List", pl.name)
		doc.remove(doc.get("locations")[1])
		pick_list_guard.authorize(doc)
		doc.save(ignore_version=False)  # production always records the Version (tests skip it by default)
		with self.assertRaises(pick_list_guard.PickListQuantityError):
			self._invoice(pl)
		self.assertNotEqual(frappe.db.get_value("Pick List", pl.name, "fg_invoicing_status"), "Facturado")

	# =====================================================================
	# Bodega / Facturación group actions and concurrency
	# =====================================================================

	def test_19_group_actions_distribute_over_internal_rows(self):
		_item, _so, pl = self._split_4_3()
		g = self._group(pl)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			bodega.set_group_picked_qty(pl.name, g["group"], 4)
			self.assertEqual([flt(r.picked_qty) for r in self._rows(pl)], [3, 1])
			bodega.set_group_picked_qty(pl.name, g["group"], 2)
			self.assertEqual([flt(r.picked_qty) for r in self._rows(pl)], [2, 0])
			with self.assertRaises(frappe.ValidationError):
				bodega.set_group_picked_qty(pl.name, g["group"], 5)
		shortage_invoice.set_authorization(frappe.get_doc("Pick List", pl.name))
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			(fg,) = detail["groups"]
			self.assertEqual((fg["qty"], fg["rows"]), (4, 2))
			result = facturacion.set_invoicing_group_checked(pl.name, fg["group"], 1)
			self.assertEqual((result["checked_items"], result["total_items"]), (1, 1))  # one commercial line
			self.assertTrue(all(r.fg_invoicing_checked for r in self._rows(pl)))  # both internal rows
			pricing = facturacion.set_invoice_line_price(pl.name, self._rows(pl)[0].name, 95)
		self.assertEqual({flt(r.fg_invoice_rate) for r in self._rows(pl)}, {95})  # the price is the line's
		self.assertTrue(pricing["changed"])

	def test_20_concurrent_picking_and_direct_edit(self):
		_item, _so, pl = self._split_4_3()
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
		g = self._group(pl)
		frappe.db.commit()
		site, outcome = frappe.local.site, {}
		barrier = threading.Barrier(2)
		bodega_user, sm_user = self.bodega_user, self.sm_user

		def pick():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(bodega_user)
				barrier.wait()
				bodega.set_group_picked_qty(pl.name, g["group"], 4)
				frappe.db.commit()
				outcome["pick"] = "ok"
			except Exception as e:
				frappe.db.rollback()
				outcome["pick"] = e.__class__.__name__
			finally:
				frappe.destroy()

		def delete_row():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			try:
				frappe.set_user(sm_user)
				doc = frappe.get_doc("Pick List", pl.name)
				doc.remove(doc.get("locations")[1])
				barrier.wait()
				doc.save()
				frappe.db.commit()
				outcome["delete"] = "saved"
			except Exception as e:
				frappe.db.rollback()
				outcome["delete"] = e.__class__.__name__
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=pick), threading.Thread(target=delete_row)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(120)
		frappe.db.rollback()
		self.assertNotEqual(outcome.get("delete"), "saved", outcome)
		rows = self._rows(pl)
		self.assertEqual([flt(r.stock_qty) for r in rows], [3, 1])
		self.assertIn(sum(flt(r.picked_qty) for r in rows), (0, 4), outcome)  # the pick is whole or not at all

	def test_21_counters_show_commercial_products_not_internal_rows(self):
		_item, _so, pl = self._split_4_3()
		shortage_invoice.set_authorization(frappe.get_doc("Pick List", pl.name))
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual((detail["total_items"], detail["checked_items"]), (1, 0))
			result = facturacion.set_invoicing_group_checked(pl.name, detail["groups"][0]["group"], 1)
			self.assertEqual((result["total_items"], result["checked_items"], result["progress_percent"]), (1, 1, 100))
		# Recorridos: one commercial product, 4 units (once picked/complete).
		self._pick_all(pl, force=True)
		snap = recorridos._resolve_pick_list_snapshot(frappe.get_doc("Pick List", pl.name))
		self.assertEqual((snap["item_count"], snap["total_qty"]), (1, 4))

	def test_22_native_capped_pick_list_is_refused_as_unexplained(self):
		"""ERPNext's native "Create Pick List" caps the row to the stock on
		hand (6 of 10) -- no report, no history: an unexplained difference,
		refused at COMPLETAR PEDIDO (outside the authorized flows)."""
		from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

		item = self._item()
		self.world.stock_up(item.name, self.wh.name, 6)
		so = self.world.multi_item_sales_order(self.customer.name, [{"item_code": item.name, "qty": 10, "rate": 100, "warehouse": self.wh.name}])
		pl = create_pick_list(so.name)
		pl.parent_warehouse = self.wh.name
		pl.insert()
		self.world.track_existing("Pick List", pl.name)
		(state,) = pick_list_guard.line_states(frappe.get_doc("Pick List", pl.name))
		self.assertEqual((state["category"], state["missing"]), (pick_list_guard.LINE_UNEXPLAINED, 4))
		with self._force(True), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			g = bodega.get_pick_list(pl.name)["groups"][0]
			bodega.set_group_picked_qty(pl.name, g["group"], 6)
			with self.assertRaises(pick_list_guard.PickListQuantityError):
				bodega.finish_picking(pl.name)  # not even with "Completar sin importar inventario"
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "docstatus"), 0)

	def test_23_clean_draft_can_be_deleted_reported_one_cannot(self):
		_item, _so, pl = self._split_4_3()
		self._report_short(pl)
		with self.assertRaises(pick_list_guard.PickListStructureError):
			frappe.delete_doc("Pick List", pl.name)
		_item2, _so2, clean = self._split_4_3()
		frappe.delete_doc("Pick List", clean.name)  # nothing started: native behaviour
		self.assertFalse(frappe.db.exists("Pick List", clean.name))

	def test_24_three_of_four_without_history_or_report(self):
		"""Order of 4, Pick List holds 3 -- no deletion trace, no shortage
		report. Absence of history is not proof of integrity: it is an
		unexplained difference; neither COMPLETAR (even with the force
		policy) nor FACTURAR (even authorized with shortages) goes through."""
		_item, so, pl = self._split_4_3()
		row = self._rows(pl)[1]
		frappe.db.delete("Pick List Item", {"name": row.name})  # test setup: no Version, no report
		self.assertFalse(frappe.db.exists("Version", {"ref_doctype": "Pick List", "docname": pl.name}))
		self.assertFalse(frappe.db.exists("Reporte de Faltante", {"sales_order": so.name}))
		(state,) = pick_list_guard.line_states(frappe.get_doc("Pick List", pl.name))
		self.assertEqual((state["category"], state["demand"], state["claimed"], state["missing"]), (pick_list_guard.LINE_UNEXPLAINED, 4, 3, 1))
		with self._force(True), fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			bodega.set_group_picked_qty(pl.name, self._group(pl)["group"], 3)
			with self.assertRaises(pick_list_guard.PickListQuantityError) as ctx:
				bodega.finish_picking(pl.name)
		self.assertIn("diferencia sin evidencia suficiente", str(ctx.exception))
		shortage_invoice.set_authorization(frappe.get_doc("Pick List", pl.name))
		with self.assertRaises(pick_list_guard.PickListQuantityError):
			self._invoice(pl)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "docstatus"), 0)
		self.assertNotEqual(frappe.db.get_value("Pick List", pl.name, "fg_invoicing_status"), "Facturado")
		self.assertTrue(any(p["pick_list"] == pl.name and p["category"] == pick_list_guard.LINE_UNEXPLAINED
			for p in pick_list_guard.report_inconsistent_open_orders()))

	def test_24b_reported_partial_pick_is_legitimate_pending(self):
		"""The same 3 of 4, but disclosed by Bodega (REPORTAR FALTANTE on a
		full-quantity row): legitimate pending demand -- completes with 3,
		the open report keeps the 4th visible."""
		item = self._item()
		self.world.stock_up(item.name, self.wh.name, 4)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 4, "rate": 100}])
		self.assertEqual([flt(r.stock_qty) for r in self._rows(pl)], [4])
		self._pick_all(pl, force=False, available=3)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "docstatus"), 1)
		(state,) = pick_list_guard.line_states(frappe.get_doc("Pick List", pl.name))
		self.assertEqual((state["category"], state["claimed"], state["covered_by_reports"]), (pick_list_guard.LINE_PENDING, 3, 1))

	def test_25_deleting_a_clean_draft_and_recovering_through_ventas(self):
		"""A draft without open reports may be deleted (native). Then: the
		units are pending again and free for picking, no stock moved, nothing
		can be invoiced, a native capped re-creation is refused, and the
		authorized flow (Ventas > Modificar pedido) gives the order back ONE
		full Pick List -- no loss, no duplicate."""
		from erpnext.selling.doctype.sales_order.sales_order import create_pick_list

		from fabergray_erp.api import ventas
		from fabergray_erp.fulfillment.analyzer import analyze_sales_order

		item = self.world.item(f"ESC-OBA-TEST-{self.world.namespace}-R", default_warehouse=self.wh.name)
		self.world.stock_up(item.name, self.wh.name, 3)
		price = frappe.get_doc({"doctype": "Item Price", "item_code": item.name, "price_list": "Standard Selling", "selling": 1, "price_list_rate": 100}).insert()
		self.world.track_existing("Item Price", price.name)  # Ventas prices from the price list
		so, (pl,) = self._order([{"item_code": item.name, "qty": 4, "rate": 100}])
		(line,) = analyze_sales_order(so.name)["lines"]
		self.assertEqual(line["qty_available_for_pick"], 0)  # the draft holds the 3 on hand
		ledger = {dt: frappe.db.count(dt) for dt in ("Stock Ledger Entry", "GL Entry", "Stock Entry", "Stock Reservation Entry")}

		frappe.delete_doc("Pick List", pl.name)  # clean draft: nothing started, no report
		self.assertFalse(frappe.db.exists("Pick List Item", {"sales_order": so.name}))
		(line,) = analyze_sales_order(so.name)["lines"]
		self.assertEqual((line["qty_remaining"], line["qty_available_for_pick"]), (4, 3))  # pending again, stock free
		self.assertEqual({dt: frappe.db.count(dt) for dt in ledger}, ledger)  # no stock / GL movement
		self.assertEqual(flt(frappe.db.get_value("Sales Order Item", so.items[0].name, "picked_qty")), 0)

		# A native re-creation caps to the 3 on hand -> refused at completion.
		native = create_pick_list(so.name)
		native.parent_warehouse = self.wh.name
		native.insert()
		self.world.track_existing("Pick List", native.name)
		(state,) = pick_list_guard.line_states(native)
		self.assertEqual(state["category"], pick_list_guard.LINE_UNEXPLAINED)
		frappe.delete_doc("Pick List", native.name)

		# Authorized recovery: Ventas > Modificar pedido (native cancel + amend).
		result = ventas.modify_submitted_sales_order(name=so.name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 4}])
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 2)
		rows = frappe.get_all("Pick List Item", filters={"sales_order": result["name"]}, fields=["parent", "stock_qty"])
		self.assertEqual(len({r.parent for r in rows}), 1)  # exactly one Pick List
		self.assertEqual(sum(flt(r.stock_qty) for r in rows), 4)  # the whole demand, not duplicated
		recovered = frappe.get_doc("Pick List", rows[0].parent)
		self.assertEqual(pick_list_guard.conservation_problems(recovered), [])
		self.assertFalse(frappe.db.exists("Pick List Item", {"sales_order": so.name}))  # nothing left on the cancelled order
		self._pick_all(recovered, force=True)
		self._invoice(recovered)
		lines, totals = self._lines(recovered, frappe.get_doc("Sales Order", result["name"]))
		self.assertEqual([(l["qty"], l["amount"]) for l in lines], [(4, 400)])

	def test_26_pick_list_with_open_report_reference_cannot_be_deleted(self):
		"""A report referencing the Pick List itself (not only a row) also
		blocks deletion -- an orphan reference breaks the Centro de Faltantes."""
		_item, so, pl = self._split_4_3()
		rep = frappe.get_doc({"doctype": "Reporte de Faltante", "item_code": _item.name, "warehouse": self.wh.name,
			"sales_order": so.name, "pick_list": pl.name, "qty_solicitada": 4, "qty_disponible": 3,
			"detected_by": "Bodega", "shortage_reason": "Stock insuficiente"}).insert()
		self.world.track_existing("Reporte de Faltante", rep.name)
		with self.assertRaises(pick_list_guard.PickListStructureError):
			frappe.delete_doc("Pick List", pl.name)

	def test_27_snapshot_fields_protected_and_generated_at_issue(self):
		"""fg_invoice_calc_version / fg_invoice_snapshot: no direct write
		(Desk/REST/frappe.client, as Administrator) before or after issuing;
		mark_as_invoiced() writes them once, with the exact lines/totals
		issued; a later change of the order never alters the reprint."""
		item = self._item(stock_uom="Unidad", box=6)
		self.world.stock_up(item.name, self.wh.name, 12)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
		self._pick_all(pl, force=False)  # submitted -> before_update_after_submit path
		with fx.as_user("Administrator"):
			for field, value in (("fg_invoice_calc_version", 2), ("fg_invoice_snapshot", '{"totals": {"total": 1}}')):
				with self.assertRaises(frappe.PermissionError, msg=f"before issue: {field}"):
					frappe.client.set_value("Pick List", pl.name, field, value)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, ["fg_invoice_calc_version", "fg_invoice_snapshot"]), (0, None))

		expected_lines, expected_totals = facturacion._build_commercial_invoice_lines(frappe.get_doc("Pick List", pl.name), frappe.get_doc("Sales Order", so.name))
		self._invoice(pl)
		version, raw = frappe.db.get_value("Pick List", pl.name, ["fg_invoice_calc_version", "fg_invoice_snapshot"])
		snap = json.loads(raw)
		self.assertEqual((version, snap["version"]), (commercial_lines.CALC_VERSION_COMMERCIAL, commercial_lines.CALC_VERSION_COMMERCIAL))
		self.assertEqual([(l["qty"], l["uom"], l["rate"], l["amount"]) for l in snap["lines"]], [(2, "Box", 600, 1200)])
		self.assertEqual([(l["qty"], l["amount"]) for l in snap["lines"]], [(l["qty"], l["amount"]) for l in expected_lines])
		self.assertEqual(snap["totals"]["total"], expected_totals["total"])
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 1200)

		with fx.as_user("Administrator"):
			for field, value in (("fg_invoice_calc_version", 0), ("fg_invoice_snapshot", None), ("fg_invoice_snapshot", "{}")):
				with self.assertRaises(frappe.PermissionError, msg=f"after issue: {field}={value!r}"):
					frappe.client.set_value("Pick List", pl.name, field, value)
		self.assertEqual(frappe.db.get_value("Pick List", pl.name, "fg_invoice_snapshot"), raw)  # unchanged

		# Test setup: the order's price changed afterwards at db level -- the
		# issued invoice (reprint and Cartera) keeps what was issued.
		frappe.db.set_value("Sales Order Item", so.items[0].name, "rate", 999)
		lines, totals = self._lines(pl, so)
		self.assertEqual((lines[0]["amount"], totals["total"]), (1200, 1200))
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 1200)

	def test_28_public_price_and_price_modes_in_the_line_unit(self):
		"""Item Price is per stock unit ($100 / Unidad); the line is sold in
		boxes of 6. Facturación's public price, PRECIO COMPLETO and -10% work
		per BOX ($600 / $540), so 2 boxes invoice $1.200 / $1.080 -- never
		$200 (unit price applied per box)."""
		item = self._item(stock_uom="Unidad", box=6)
		price = frappe.get_doc({"doctype": "Item Price", "item_code": item.name, "price_list": "Standard Selling",
			"selling": 1, "price_list_rate": 100, "uom": "Unidad"}).insert()
		self.world.track_existing("Item Price", price.name)
		self.world.stock_up(item.name, self.wh.name, 12)
		so, (pl,) = self._order([{"item_code": item.name, "qty": 2, "uom": "Box", "rate": 600}])
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "selling_price_list"), "Standard Selling")
		self._pick_all(pl, force=False)
		with fx.as_user(self.facturacion_user):
			pricing = facturacion.get_invoicing_pricing(pl.name)
			self.assertEqual({flt(l["public_rate"]) for l in pricing["lines"]}, {600})
			facturacion.apply_invoice_price_mode(pl.name, "FULL")
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["qty"], l["uom"], l["rate"], l["amount"]) for l in lines], [(2, "Box", 600, 1200)])
		with fx.as_user(self.facturacion_user):
			facturacion.apply_invoice_price_mode(pl.name, "DISCOUNT_10")
		lines, totals = self._lines(pl, so)
		self.assertEqual([(l["rate"], l["amount"]) for l in lines], [(540, 1080)])
		self._invoice(pl)
		self.assertEqual(cartera_service.compute_invoice_amount(pl.name, pl.company)[0], 1080)
		self.assertEqual(json.loads(frappe.db.get_value("Pick List", pl.name, "fg_invoice_snapshot"))["totals"]["total"], 1080)
