# -*- coding: utf-8 -*-
"""Commit 19.4 -- end-to-end tests for the real Purchase Receipt.on_submit
-> fulfillment.purchase_receipt_hooks.on_submit -> process_sales_order()
hook (hooks.py doc_events).

Every test builds and submits its own Sales Order directly (like
test_sales_order_hook.py, NOT through TestWorld.multi_item_sales_order(),
which stays wrapped in fx.without_sales_order_hook()) so the real
on_submit hook creates the initial Pick List/Reporte de Faltante/Material
Request automatically, then drives a real Material Request -> Purchase
Order -> Purchase Receipt chain through ERPNext's own native mappers
(never a hand-built document skipping them) to exercise the real
Purchase Receipt on_submit hook this commit adds.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.stock.doctype.material_request.material_request import make_purchase_order

from fabergray_erp.api import bodega
from fabergray_erp.fulfillment.engine import process_sales_order
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_SRBNB_ACCOUNT = "281505 - Valores recibidos para terceros - FG"


class TestPurchaseReceiptHooks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.bodega_user = cls.world.user("fg194-bodega@example.com", ["Bodega"])

		cls._company_defaults_cm = fx.company_defaults(stock_received_but_not_billed=_SRBNB_ACCOUNT)
		cls._company_defaults_cm.__enter__()
		cls.addClassCleanup(cls._company_defaults_cm.__exit__, None, None, None)

		cls.supplier = "FG194 Test Supplier"
		if not frappe.db.exists("Supplier", cls.supplier):
			doc = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": cls.supplier, "supplier_group": "All Supplier Groups"}
			)
			doc.insert()
			cls.world.track_existing("Supplier", doc.name)

	def _new_world(self, tag, stock_qty=None, default_material_request_type="Purchase"):
		wh = self.world.warehouse(f"FG194 {tag}")
		item = self.world.item(f"FG194-{tag.upper()}", default_material_request_type=default_material_request_type)
		customer = self.world.customer(f"FG194 {tag} Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		if stock_qty is not None:
			self.world.stock_up_real(item.name, wh.name, stock_qty)
		return wh, item, customer

	def _draft_sales_order(self, customer, items):
		delivery_date = add_days(nowdate(), 7)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": fx.COMPANY,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"set_warehouse": items[0]["warehouse"],
				"items": [{**item, "delivery_date": delivery_date} for item in items],
			}
		)
		doc.insert()
		self.world.track_existing("Sales Order", doc.name)
		return doc

	def _submit_via_hook(self, customer, items):
		doc = self._draft_sales_order(customer, items)
		doc.submit()
		self.world.track_existing_pick_lists_and_reports_for(doc.name)
		return doc

	def _mr_for(self, so_name):
		names = frappe.get_all(
			"Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
		)
		self.assertEqual(len(names), 1, f"expected exactly one Material Request for {so_name}, found {names}")
		return names[0]

	def _pick_lists_for(self, so_name):
		return frappe.get_all(
			"Pick List Item", filters={"sales_order": so_name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)

	def _reports_for(self, so_name):
		return frappe.get_all("Reporte de Faltante", filters={"sales_order": so_name}, pluck="name")

	def _receive_via_po_and_pr(self, mr_name, qty_by_row=None, extra_po_row=None):
		"""Drives MR -> PO -> PR through ERPNext's own native mappers
		(make_purchase_order/make_purchase_receipt) -- never a hand-built
		document skipping them, so `sales_order`/`sales_order_item` reach
		Purchase Receipt Item exactly the way a human's own "Create
		Purchase Order"/"Create Purchase Receipt" buttons would produce
		them. `qty_by_row` (optional {sales_order_item: qty}) caps
		specific PR rows for a partial receipt; `extra_po_row` (optional
		dict) appends one extra, unrelated PO Item row (no
		material_request/sales_order at all) before submitting, for the
		"line without a Sales Order is ignored" scenario."""
		# Compras reviews and submits the Engine's own draft Material
		# Request before creating a Purchase Order from it -- exactly the
		# native precondition make_purchase_order() itself enforces
		# (validation: docstatus=1) and exactly the human step Commit 19.1
		# deliberately left the Engine from ever doing automatically.
		mr = frappe.get_doc("Material Request", mr_name)
		if mr.docstatus == 0:
			mr.submit()

		po = make_purchase_order(mr_name)
		po.supplier = self.supplier
		for d in po.items:
			d.rate = 10
		if extra_po_row:
			po.append("items", extra_po_row)
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)

		pr = make_purchase_receipt(po.name)
		pr.posting_date = nowdate()
		if qty_by_row:
			for d in pr.items:
				if d.sales_order_item in qty_by_row:
					d.qty = qty_by_row[d.sales_order_item]
					d.received_qty = qty_by_row[d.sales_order_item]
		pr.insert()
		pr.submit()
		self.world.track_existing("Purchase Receipt", pr.name)
		return pr

	# -- Caso 1: recepción completa del remanente -> nuevo Pick List + Reporte resuelto --

	def test_full_receipt_creates_new_pick_list_and_resolves_report(self):
		wh, item, customer = self._new_world("Full", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		mr_name = self._mr_for(so.name)
		report_name = self._reports_for(so.name)[0]
		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).qty_faltante, 7.0)
		self.assertEqual(len(self._pick_lists_for(so.name)), 1)

		self._receive_via_po_and_pr(mr_name)  # receives the MR's own remaining qty (7) in full

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 2)  # original (3) + new (7)
		total_claimed = sum(
			flt(frappe.get_doc("Pick List", name).get("locations")[0].stock_qty) for name in pick_lists
		)
		self.assertEqual(total_claimed, 10.0)

		self.assertEqual(frappe.get_doc("Reporte de Faltante", report_name).status, "Resuelto")

		# Material Request: still exactly one, never touched by the Engine
		# itself (Commit 19.1's own standing policy -- this commit does not
		# change it) -- Compras' own submit (a precondition for creating the
		# Purchase Order at all) is the only reason its docstatus changed.
		mr_names_after = frappe.get_all(
			"Material Request Item", filters={"sales_order": so.name}, pluck="parent", distinct=True
		)
		self.assertEqual(mr_names_after, [mr_name])
		self.assertEqual(frappe.db.get_value("Material Request", mr_name, "docstatus"), 1)

	# -- Caso 2: recepción parcial -> Pick List parcial nuevo, Reporte se actualiza, MR no se duplica --

	def test_partial_receipt_creates_partial_pick_list_updates_report_and_does_not_duplicate_mr(self):
		wh, item, customer = self._new_world("Partial", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])

		mr_name = self._mr_for(so.name)
		report_name = self._reports_for(so.name)[0]
		mr = frappe.get_doc("Material Request", mr_name)
		so_item_name = mr.items[0].sales_order_item

		self._receive_via_po_and_pr(mr_name, qty_by_row={so_item_name: 4})  # only 4 of the 7 requested

		pick_lists = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists), 2)
		total_claimed = sum(
			flt(frappe.get_doc("Pick List", n).get("locations")[0].stock_qty) for n in pick_lists
		)
		self.assertEqual(total_claimed, 7.0)  # 3 original + 4 just received

		report = frappe.get_doc("Reporte de Faltante", report_name)
		self.assertEqual(report.status, "Abierto")
		self.assertEqual(report.qty_faltante, 3.0)  # 10 - 7 claimed so far

		# Material Request: no new one -- the existing draft (qty 7) already
		# exceeds the new, smaller shortage (3); Commit 19.1's own approved,
		# documented behaviour (never shrinks an existing draft) applies
		# unchanged here.
		mr_names_after = frappe.get_all(
			"Material Request Item", filters={"sales_order": so.name}, pluck="parent", distinct=True
		)
		self.assertEqual(mr_names_after, [mr_name])
		mr.reload()
		self.assertEqual(mr.docstatus, 1)  # submitted by Compras to create the Purchase Order, not by the Engine
		self.assertEqual(flt(mr.items[0].qty), 7.0)  # qty itself untouched by the Engine

	# -- Caso 3: un Purchase Receipt con líneas de dos Sales Orders distintas --

	def test_purchase_receipt_spanning_two_sales_orders_reprocesses_each_once(self):
		wh_a, item_a, customer_a = self._new_world("MultiA", stock_qty=2)
		so_a = self._submit_via_hook(customer_a.name, [{"item_code": item_a.name, "warehouse": wh_a.name, "qty": 10, "rate": 100}])
		mr_a = self._mr_for(so_a.name)

		wh_b, item_b, customer_b = self._new_world("MultiB", stock_qty=1)
		so_b = self._submit_via_hook(customer_b.name, [{"item_code": item_b.name, "warehouse": wh_b.name, "qty": 6, "rate": 100}])
		mr_b = self._mr_for(so_b.name)

		# Compras reviews and submits both drafts first -- required
		# precondition for make_purchase_order() (docstatus=1), same as
		# _receive_via_po_and_pr()'s own reasoning.
		frappe.get_doc("Material Request", mr_a).submit()
		frappe.get_doc("Material Request", mr_b).submit()

		# Consolidate both Material Requests' needs onto one Purchase Order --
		# a human doing this by hand would use the native mapper for one MR,
		# then add the second MR's own row(s) the same way it would have
		# mapped them (same field shape, same native relations).
		po = make_purchase_order(mr_a)
		po.supplier = self.supplier
		for d in po.items:
			d.rate = 10
		mr_b_doc = frappe.get_doc("Material Request", mr_b)
		po.append(
			"items",
			{
				"item_code": mr_b_doc.items[0].item_code,
				"qty": mr_b_doc.items[0].qty,
				"warehouse": mr_b_doc.items[0].warehouse,
				"schedule_date": mr_b_doc.items[0].schedule_date,
				"rate": 10,
				"material_request": mr_b,
				"material_request_item": mr_b_doc.items[0].name,
				"sales_order": so_b.name,
				"sales_order_item": mr_b_doc.items[0].sales_order_item,
			},
		)
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)

		pr = make_purchase_receipt(po.name)
		pr.posting_date = nowdate()
		pr.insert()

		with patch("fabergray_erp.fulfillment.purchase_receipt_hooks.process_sales_order", wraps=process_sales_order) as spy:
			pr.submit()
			self.world.track_existing("Purchase Receipt", pr.name)

			called_sales_orders = [c.args[0] if c.args else c.kwargs.get("sales_order") for c in spy.call_args_list]

		self.assertEqual(sorted(called_sales_orders), sorted([so_a.name, so_b.name]))
		self.assertEqual(len(called_sales_orders), 2)  # exactly once each

		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(so_a.name)[0]).status, "Resuelto")
		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(so_b.name)[0]).status, "Resuelto")

	# -- Caso 4: varias líneas del mismo PR para la MISMA SO -> se ejecuta una sola vez --

	def test_multiple_rows_for_the_same_so_in_one_receipt_reprocess_it_only_once(self):
		wh = self.world.warehouse("FG194 SameSo")
		customer = self.world.customer("FG194 SameSo Customer")
		self.world.warehouse_user_permission(self.bodega_user, wh.name)
		item_1 = self.world.item("FG194-SAMESO-1", default_material_request_type="Purchase")
		item_2 = self.world.item("FG194-SAMESO-2", default_material_request_type="Purchase")
		self.world.stock_up_real(item_1.name, wh.name, 2)
		self.world.stock_up_real(item_2.name, wh.name, 1)

		so = self._submit_via_hook(
			customer.name,
			[
				{"item_code": item_1.name, "warehouse": wh.name, "qty": 8, "rate": 100},
				{"item_code": item_2.name, "warehouse": wh.name, "qty": 5, "rate": 100},
			],
		)
		mr_name = self._mr_for(so.name)
		mr = frappe.get_doc("Material Request", mr_name)
		self.assertEqual(len(mr.items), 2)  # both lines short, both in one Material Request

		mr.submit()  # Compras reviews and submits the draft first (required precondition)

		po = make_purchase_order(mr_name)
		po.supplier = self.supplier
		for d in po.items:
			d.rate = 10
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)
		self.assertEqual(len(po.items), 2)  # both rows carried through, same sales_order

		pr = make_purchase_receipt(po.name)
		pr.posting_date = nowdate()
		pr.insert()

		with patch("fabergray_erp.fulfillment.purchase_receipt_hooks.process_sales_order", wraps=process_sales_order) as spy:
			pr.submit()
			self.world.track_existing("Purchase Receipt", pr.name)
			self.assertEqual(spy.call_count, 1)  # not once per row

		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(so.name)[0]).status, "Resuelto")

	# -- Caso 5: línea de PR sin Sales Order -> ignorada -------------------------

	def test_purchase_receipt_row_without_sales_order_is_ignored(self):
		wh, item, customer = self._new_world("NoSoRow", stock_qty=2)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		unrelated_item = self.world.item("FG194-UNRELATED-RESTOCK")
		extra_row = {
			"item_code": unrelated_item.name,
			"qty": 5,
			"warehouse": wh.name,
			"schedule_date": getdate(nowdate()),
			"rate": 10,
		}  # no material_request, no sales_order -- a plain, unrelated restock line

		with patch("fabergray_erp.fulfillment.purchase_receipt_hooks.process_sales_order", wraps=process_sales_order) as spy:
			self._receive_via_po_and_pr(mr_name, extra_po_row=extra_row)
			self.assertEqual(spy.call_count, 1)
			self.assertEqual(spy.call_args_list[0].args[0], so.name)

		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(so.name)[0]).status, "Resuelto")

	# -- Caso 6: SO ya completamente atendida -> no crea Pick List adicional ----

	def test_reprocessing_an_already_fully_served_so_creates_nothing_new(self):
		wh, item, customer = self._new_world("AlreadyServed", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		self._receive_via_po_and_pr(mr_name)  # fully covers the order
		pick_lists_after_receipt = self._pick_lists_for(so.name)
		self.assertEqual(len(pick_lists_after_receipt), 2)

		result = process_sales_order(so.name)  # manual reprocess, nothing left to do

		self.assertIsNone(result["pick_list"])
		self.assertEqual(result["shortages"]["created"], [])
		self.assertEqual(result["shortages"]["updated"], [])
		self.assertEqual(result["purchasing"], {"created": [], "lines_requested": []})
		self.assertEqual(self._pick_lists_for(so.name), pick_lists_after_receipt)

	# -- Caso 7: el nuevo Pick List aparece en get_queue() de Bodega -------------

	def test_new_pick_list_from_receipt_appears_in_bodega_queue(self):
		wh, item, customer = self._new_world("Queue", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		self._receive_via_po_and_pr(mr_name)

		pick_lists = self._pick_lists_for(so.name)
		with fx.as_user(self.bodega_user):
			queue = bodega.get_queue()
		queue_names = [p["name"] for p in queue["pendientes"]] + [p["name"] for p in queue["en_alistamiento"]]
		for pl_name in pick_lists:
			self.assertIn(pl_name, queue_names)

	# -- Caso 8 (Commit 18.1's own standard, re-confirmed): Vendedora gana ningún permiso nuevo --

	def test_vendedora_gains_no_new_permission_from_receipt_reprocessing(self):
		from fabergray_erp.api import ventas

		wh, item, customer = self._new_world("VendedoraNoNewPerm", stock_qty=3)
		vendedora = self.world.user("fg194-vendedora@example.com", ["Vendedora"])
		item_defaults_item = self.world.item(
			"FG194-VENDEDORA-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item_defaults_item.name, wh.name, 3)

		with fx.as_user(vendedora):
			result = ventas.create_and_submit_sales_order(
				customer=customer.name, items=[{"item_code": item_defaults_item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		mr_name = self._mr_for(result["name"])

		# Compras/Stock (an already-privileged Administrator session in this
		# test, exactly like every other Purchase Order/Purchase Receipt in
		# this suite) receives the goods -- Vendedora is never the one
		# submitting a Purchase Receipt, so this scenario never even asks
		# whether her own session needs a new bypass, unlike Commit 18.1/19.1's
		# concern for her own Sales Order submit.
		self._receive_via_po_and_pr(mr_name)

		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(result["name"])[0]).status, "Resuelto")

		with fx.as_user(vendedora):
			self.assertFalse(frappe.has_permission("Purchase Receipt", "read"))
			self.assertFalse(frappe.has_permission("Purchase Order", "read"))
			self.assertFalse(frappe.has_permission("Material Request", "read"))

	# -- Caso 9: rollback transaccional si process_sales_order() falla ----------

	def test_intentional_error_during_reprocessing_rolls_back_the_whole_receipt(self):
		wh, item, customer = self._new_world("Rollback", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)
		pick_lists_before = self._pick_lists_for(so.name)
		frappe.get_doc("Material Request", mr_name).submit()  # Compras' own precondition
		frappe.db.commit()  # fixtures + hook-created artifacts survive the rollback below

		po = make_purchase_order(mr_name)
		po.supplier = self.supplier
		for d in po.items:
			d.rate = 10
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)
		frappe.db.commit()

		pr = make_purchase_receipt(po.name)
		pr.posting_date = nowdate()
		pr.insert()
		frappe.db.commit()

		with patch(
			"fabergray_erp.fulfillment.purchase_receipt_hooks.process_sales_order",
			side_effect=RuntimeError("Commit 19.4 intentional failure"),
		):
			with self.assertRaises(RuntimeError):
				pr.submit()

		frappe.db.rollback()

		pr.reload()
		self.assertEqual(pr.docstatus, 0)  # the Purchase Receipt's own submit rolled back too
		self.assertEqual(self._pick_lists_for(so.name), pick_lists_before)  # no new Pick List left behind
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh.name}, "actual_qty"), 3.0
		)  # the stock ledger update rolled back too -- never left at 10

		# confirm the Purchase Receipt is left genuinely submittable afterward.
		pr.submit()
		self.world.track_existing("Purchase Receipt", pr.name)
		self.assertEqual(len(self._pick_lists_for(so.name)), 2)

	# -- Caso 10: idempotencia -- reprocesar (p.ej. una segunda recepción redundante) --

	def test_reprocessing_is_idempotent(self):
		wh, item, customer = self._new_world("Idempotent", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		self._receive_via_po_and_pr(mr_name)
		pick_lists_after_first_receipt = self._pick_lists_for(so.name)

		result_again = process_sales_order(so.name)  # e.g. a stray manual reprocess

		self.assertIsNone(result_again["pick_list"])
		self.assertEqual(self._pick_lists_for(so.name), pick_lists_after_first_receipt)
		self.assertEqual(result_again["purchasing"], {"created": [], "lines_requested": []})

	# -- Caso 11: concurrencia -- el lock existente sigue sirviendo vía este disparador --

	def test_concurrent_reprocessing_of_the_same_so_does_not_duplicate_the_open_report(self):
		"""Extends Commit 16's own two-connection proof
		(test_concurrent_calls_do_not_create_duplicate_open_reports_for_same_line,
		test_shortage_service.py) to this new call path: two
		process_sales_order() calls for the SAME Sales Order, racing on two
		genuinely separate connections, must still not create a duplicate
		Reporte de Faltante -- the exact same SELECT ... FOR UPDATE lock
		sync_shortage_reports_for_sales_order() already takes on the Sales
		Order (Commit 16) protects this call path too, since it is the
        exact same function, not a parallel one. This is NOT a new lock --
		it is a demonstration that no new lock was needed for this commit,
		per the standing instruction not to add one without first proving a
		real, uncovered race."""
		wh, item, customer = self._new_world("Concurrency", stock_qty=3)
		so = self._submit_via_hook(customer.name, [{"item_code": item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		frappe.db.commit()  # fixtures + hook-created artifacts visible to the secondary connection

		report_name_before = self._reports_for(so.name)[0]

		try:
			with self.primary_connection():
				result_primary = process_sales_order(so.name)
				# transaction intentionally left open here -- the row lock
				# sync_shortage_reports_for_sales_order() took on the Sales
				# Order is still held on this connection.

			with self.secondary_connection():
				with self.assertRaises(frappe.QueryTimeoutError):
					frappe.db.get_value("Sales Order", so.name, "name", for_update=True, wait=False)

			with self.primary_connection():
				frappe.db.commit()  # ends primary's transaction -- releases the lock

			with self.secondary_connection():
				result_secondary = process_sales_order(so.name)
				frappe.db.commit()
		finally:
			pass

		self.assertEqual(frappe.db.count("Reporte de Faltante", {"sales_order": so.name}), 1)
