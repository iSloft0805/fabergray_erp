# -*- coding: utf-8 -*-
"""Commit 19.4 -- tests for fulfillment.purchase_receipt_hooks.on_submit,
REWRITTEN for Commit 25.4's approved architecture (see this session's own
audit -- ventas/faltante-resolution session, "Ventas no decide faltantes").

Reality this file now represents, confirmed live before writing a line of
it (not assumed):

1. The ROUTINE flow (Draft -> Confirmar pedido -> Bodega -> "Registrar
   compra") never creates a Material Request, Purchase Order or Purchase
   Receipt at all -- the approved resolution mechanism for a Bodega-
   reported shortage is a native Stock Entry (purpose="Material Receipt"),
   built by api.jefe_bodega.receive_shortage_purchase() (Commit 22.8,
   already fully covered by its own 27 tests in
   test_jefe_bodega_purchase_api.py, plus the real Draft->Confirmar->
   Bodega->Compra chain end to end in test_shortage_resolution_flow.py).
   So under the real, current flow, a Purchase Receipt Item row's
   `sales_order` is never populated by anything this app does -- this
   hook is a live no-op today. test_on_submit_is_a_noop_for_a_real_
   purchase_receipt_unrelated_to_any_sales_order below proves that
   directly against a real, native Purchase Receipt.

2. process_sales_order() (Commit 15) itself is completely UNCHANGED --
   still correct, still the composition test_engine.py already verifies
   in isolation. What changed is only that Sales Order.on_submit no
   longer calls it (Commit 25.4 wired process_sales_order_for_
   confirmation() instead -- see fulfillment/sales_order_hooks.py). So
   every scenario below that still needs a Material Request/Purchase
   Order/Purchase Receipt to exist builds its own Sales Order OUTSIDE the
   real hook (fx.without_sales_order_hook(), the same helper every test
   file written before Commit 16 already uses for this exact reason) and
   then calls process_sales_order() explicitly -- deliberately modelling
   the one legitimate remaining use of this pipeline: an admin-triggered
   manual reprocess of an order the routine Commit 25.4 flow never
   touched, per process_sales_order()'s own docstring ("e.g. a future
   admin 'reprocess' action"). Every assertion in those tests is
   unchanged from before this rewrite -- only the setup fixture changed,
   because only the setup fixture's assumption (Sales Order.submit()
   itself producing the Material Request) stopped being true.

3. test_reprocessing_a_confirmed_order_via_the_legacy_pipeline_is_a_safe_
   no_op below is new: it proves, empirically (not by inspection), that
   if process_sales_order() is ever manually called on a Sales Order that
   WAS confirmed through the real Commit 25.4 flow (so it already has a
   full-demand Pick List), it creates no duplicate Pick List, no
   duplicate/parallel Reporte de Faltante, and no Material Request -- the
   full-demand Pick List's own "already claimed by an open Pick List"
   accounting (pick_list_service._qty_already_claimed_by_open_pick_
   lists_for_so_item(), reused as-is by sync_shortage_reports_for_sales_
   order()) neutralizes it by construction. This is a real regression
   guard, not a formality: it is what makes it safe that this whole
   pipeline was left in place (deprecated, a candidate for removal, not
   redesigned) rather than deleted.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.stock.doctype.material_request.material_request import make_purchase_order

from fabergray_erp.api import bodega
from fabergray_erp.api import ventas
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
		"""Kept the original name (every existing scenario below still calls
		it) but NOT the original mechanism: Commit 25.4 changed what Sales
		Order.on_submit does (see this file's own module docstring, point
		2), so this no longer produces a Material Request just by
		submitting. It now explicitly models the one legitimate remaining
		use of this pipeline -- an admin/legacy manual reprocess of an
		order the real Commit 25.4 hook never touched -- by submitting
		OUTSIDE that hook (fx.without_sales_order_hook()) and then calling
		process_sales_order() (Commit 15, itself completely unchanged)
		directly, exactly once, exactly like the real
		fulfillment.purchase_receipt_hooks.on_submit does for each row it
		finds."""
		doc = self._draft_sales_order(customer, items)
		with fx.without_sales_order_hook():
			doc.submit()
		process_sales_order(doc.name)
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
		"""Commit 25.4 note: her REAL submit path (ventas.confirm_order(),
		via process_sales_order_for_confirmation()) never creates a
		Material Request at all any more, so this scenario -- "does
		reprocessing triggered somewhere downstream of a Vendedora's own
		action leak her any new permission" -- can only still arise from
		this file's own legacy/admin reprocess pipeline (module docstring,
		point 2), never from her routine order-confirmation flow itself.
		The guarantee under test is unchanged: her role must still never
		read Purchase Receipt/Purchase Order/Material Request, regardless
		of what an admin does with a Sales Order afterward."""
		wh, item, customer = self._new_world("VendedoraNoNewPerm", stock_qty=3)
		vendedora = self.world.user("fg194-vendedora@example.com", ["Vendedora"])
		item_defaults_item = self.world.item(
			"FG194-VENDEDORA-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item_defaults_item.name, wh.name, 3)

		so = self._submit_via_hook(customer.name, [{"item_code": item_defaults_item.name, "warehouse": wh.name, "qty": 10, "rate": 100}])
		mr_name = self._mr_for(so.name)

		# Compras/Stock (an already-privileged Administrator session in this
		# test, exactly like every other Purchase Order/Purchase Receipt in
		# this suite) receives the goods -- Vendedora is never the one
		# submitting a Purchase Receipt, so this scenario never even asks
		# whether her own session needs a new bypass, unlike Commit 18.1/19.1's
		# concern for her own Sales Order submit.
		self._receive_via_po_and_pr(mr_name)

		self.assertEqual(frappe.get_doc("Reporte de Faltante", self._reports_for(so.name)[0]).status, "Resuelto")

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

	# -- Nuevas (rewrite): la realidad del flujo actual --------------------------

	def test_distinct_sales_orders_for_deduplicates_and_ignores_rows_without_one(self):
		"""Pure unit test of the one real piece of logic this hook still
		owns -- purchase_receipt_hooks._distinct_sales_orders_for() -- with
		no DB dependency at all, so it stays meaningful regardless of
		whether this pipeline has a live trigger in this app or not."""
		from types import SimpleNamespace

		from fabergray_erp.fulfillment.purchase_receipt_hooks import _distinct_sales_orders_for

		doc = SimpleNamespace(
			items=[
				SimpleNamespace(sales_order="SO-A"),
				SimpleNamespace(sales_order=None),
				SimpleNamespace(sales_order="SO-B"),
				SimpleNamespace(sales_order="SO-A"),  # duplicate -- must not appear twice
				SimpleNamespace(sales_order=""),  # falsy -- must be skipped, not treated as a name
			]
		)
		self.assertEqual(_distinct_sales_orders_for(doc), ["SO-A", "SO-B"])

	def test_on_submit_is_a_noop_for_a_real_purchase_receipt_unrelated_to_any_sales_order(self):
		"""The real, current shape of a Purchase Receipt in this app: a
		plain, standalone purchase (Compras buying general stock), never
		derived from a Material Request tied to a Sales Order -- exactly
		what api.jefe_bodega.receive_shortage_purchase() deliberately does
		NOT produce either (it inserts a Stock Entry, never a Purchase
		Receipt at all). process_sales_order() must never be reached for
		this, proven directly against the real hook function, not
		inferred."""
		wh = self.world.warehouse("FG194 UnrelatedNoop")
		item = self.world.item("FG194-UNRELATED-NOOP-ITEM")

		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": self.supplier,
				"company": fx.COMPANY,
				"schedule_date": nowdate(),
				"items": [
					{"item_code": item.name, "qty": 5, "rate": 10, "schedule_date": nowdate(), "warehouse": wh.name}
				],
			}
		)
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)

		pr = make_purchase_receipt(po.name)
		pr.posting_date = nowdate()
		pr.insert()

		with patch("fabergray_erp.fulfillment.purchase_receipt_hooks.process_sales_order") as spy:
			pr.submit()
			self.world.track_existing("Purchase Receipt", pr.name)
			spy.assert_not_called()

	def test_reprocessing_a_confirmed_order_via_the_legacy_pipeline_is_a_safe_no_op(self):
		"""Empirically verified during this session's audit, not assumed:
		manually running the old, unchanged process_sales_order() against a
		Sales Order that WAS confirmed through the real Commit 25.4 flow
		(ventas.confirm_order() -> a full-demand Pick List already exists)
		creates nothing new and touches nothing -- the exact same
		"already claimed by an open Pick List" accounting sync_shortage_
		reports_for_sales_order()/create_pick_list_for_available_stock()
		already used for Commit 13's own integration case (see
		shortage_service.py's own docstring) applies here too, since the
		full-demand Pick List's rows are indistinguishable from any other
		open Pick List's rows to that shared accounting. This is what
		makes it safe to leave fulfillment/purchase_receipt_hooks.py and
		process_sales_order() in place (deprecated, a removal candidate,
		not deleted this session) instead of having to delete them
		immediately to avoid a real hazard."""
		wh = self.world.warehouse("FG194 SafeNoOp")
		item = self.world.item(
			"FG194-SAFE-NOOP-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		customer = self.world.customer("FG194 SafeNoOp Customer")
		vendedora = self.world.user("fg194-safenoop-vendedora@example.com", ["Vendedora"])

		with fx.as_user(vendedora):
			draft = ventas.create_draft_sales_order(customer=customer.name, items=[{"item_code": item.name, "qty": 10}])
			so_name = draft["name"]
			self.world.track_existing("Sales Order", so_name)
			ventas.confirm_order(so_name)
		self.world.track_existing_pick_lists_and_reports_for(so_name)

		pick_lists_before = self._pick_lists_for(so_name)
		self.assertEqual(len(pick_lists_before), 1)

		result = process_sales_order(so_name)  # e.g. someone runs the legacy pipeline on this order by mistake
		self.world.track_existing_pick_lists_and_reports_for(so_name)

		self.assertIsNone(result["pick_list"])
		self.assertEqual(result["shortages"]["created"], [])
		self.assertEqual(result["shortages"]["updated"], [])
		self.assertEqual(result["purchasing"]["created"], [])
		self.assertEqual(self._pick_lists_for(so_name), pick_lists_before)  # no second Pick List
		self.assertEqual(self._reports_for(so_name), [])  # no Reporte de Faltante at all
		self.assertEqual(
			frappe.get_all("Material Request Item", filters={"sales_order": so_name}, pluck="parent"), []
		)
