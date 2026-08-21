# -*- coding: utf-8 -*-
"""Commit 18.5b -- modify_submitted_sales_order() / get_modification_status()
/ get_order_for_modification(): letting a Vendedora add/remove products and
change quantities on one of her own already-submitted Sales Orders, via
ERPNext's own native cancel+amend, gated by `modification_blockers_for()`
(fulfillment/modification_service.py).

Central themes tested here: (1) the gate blocks exactly the cases the
approved audit named -- Bodega having started picking, real picked_qty, a
submitted Pick List, a submitted Material Request, a linked Purchase Order
-- and does not block anything else; (2) a genuinely allowed modification
recalculates the Fulfillment Engine's own artifacts exactly as a fresh
submit would (old shortage/Material Request resolved-or-gone, new ones
reflect the new quantities, live stock is read fresh); (3) the commercial
identity ("PEDIDO-N") survives across one or more amendments even though
the technical document name does not; (4) if_owner=1 isolation and the
zero-economic-data boundary hold for the three new endpoints exactly like
every other function in this module.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.material_request.material_request import make_purchase_order

from fabergray_erp.api import bodega as bodega_api
from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_ECONOMIC_KEYS = {
	"rate",
	"price_list_rate",
	"amount",
	"net_rate",
	"net_amount",
	"base_rate",
	"base_amount",
	"total",
	"grand_total",
	"net_total",
	"base_grand_total",
	"base_net_total",
	"discount_percentage",
	"discount_amount",
	"taxes",
	"margin_rate_or_amount",
}


class TestSalesOrderModification(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG18-5b Customer")
		cls.vendedora_a = cls.world.user("fg18-5b-vendedora-a@example.com", ["Vendedora"])
		cls.vendedora_b = cls.world.user("fg18-5b-vendedora-b@example.com", ["Vendedora"])

	def _submit_order(self, item, qty):
		with fx.as_user(self.vendedora_a):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": item.name, "qty": qty}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		return result["name"]

	def _pick_list_for(self, so_name):
		names = frappe.get_all(
			"Pick List Item", filters={"sales_order": so_name, "docstatus": ["!=", 2]}, pluck="parent", distinct=True
		)
		return names[0] if names else None

	# =====================================================================
	# Allowed modification -- happy path, recalculation
	# =====================================================================

	def test_modification_allowed_when_nothing_touched_recalculates_correctly(self):
		wh = self.world.warehouse("FG18-5b Happy")
		item_a = self.world.item("FG18-5b-HAPPY-A", default_warehouse=wh.name)
		item_b = self.world.item("FG18-5b-HAPPY-B", default_warehouse=wh.name)
		self.world.stock_up_real(item_a.name, wh.name, 20)
		self.world.stock_up_real(item_b.name, wh.name, 20)

		so_name = self._submit_order(item_a, 5)

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertTrue(status["modifiable"])
			self.assertEqual(status["blockers"], [])

			prefill = ventas.get_order_for_modification(so_name)
			self.assertEqual(prefill["items"][0]["item_code"], item_a.name)

			result = ventas.modify_submitted_sales_order(
				name=so_name,
				customer=self.customer.name,
				items=[{"item_code": item_a.name, "qty": 8}, {"item_code": item_b.name, "qty": 3}],
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		self.assertEqual(result["commercial_name"], so_name)
		self.assertNotEqual(result["name"], so_name)  # native amend -> new technical name

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 2)  # original, cancelled
		amended = frappe.get_doc("Sales Order", result["name"])
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(amended.amended_from, so_name)
		self.assertEqual({(r.item_code, flt(r.qty)) for r in amended.items}, {(item_a.name, 8.0), (item_b.name, 3.0)})

		pl_name = self._pick_list_for(result["name"])
		self.assertIsNotNone(pl_name, "amended order should get a fresh Pick List covering the new quantities")
		pl_qtys = {row.item_code: flt(row.stock_qty) for row in frappe.get_all(
			"Pick List Item", filters={"parent": pl_name}, fields=["item_code", "stock_qty"]
		)}
		self.assertEqual(pl_qtys, {item_a.name: 8.0, item_b.name: 3.0})

	def test_modification_response_and_status_never_contain_economic_data(self):
		wh = self.world.warehouse("FG18-5b NoEcon")
		item = self.world.item("FG18-5b-NOECON-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		so_name = self._submit_order(item, 4)

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertEqual(set(status.keys()), {"modifiable", "blockers"})

			prefill = ventas.get_order_for_modification(so_name)
			self.assertFalse(_ECONOMIC_KEYS & set(prefill.keys()))
			for row in prefill["items"]:
				self.assertFalse(_ECONOMIC_KEYS & set(row.keys()))

			result = ventas.modify_submitted_sales_order(
				name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 6}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		self.assertEqual(set(result.keys()), {"name", "commercial_name"})

	# =====================================================================
	# Blocked -- Bodega started / picked_qty / submitted Pick List
	# =====================================================================

	def test_modification_blocked_when_bodega_started_picking(self):
		wh = self.world.warehouse("FG18-5b Started")
		item = self.world.item("FG18-5b-STARTED-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		bodega_user = self.world.user("fg18-5b-bodega-started@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		so_name = self._submit_order(item, 5)
		pl_name = self._pick_list_for(so_name)

		with fx.as_user(bodega_user):
			bodega_api.start_picking(pl_name)
		frappe.db.commit()  # fixtures + started Pick List survive any later rollback

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertFalse(status["modifiable"])
			self.assertIn("bodega_started", status["blockers"])

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)  # untouched -- never cancelled

	def test_modification_blocked_by_picked_qty_even_without_fg_started_by(self):
		wh = self.world.warehouse("FG18-5b PickedQty")
		item = self.world.item("FG18-5b-PICKEDQTY-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		bodega_user = self.world.user("fg18-5b-bodega-pickedqty@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		so_name = self._submit_order(item, 5)
		pl_name = self._pick_list_for(so_name)
		row_name = frappe.get_all("Pick List Item", filters={"parent": pl_name}, pluck="name")[0]

		with fx.as_user(bodega_user):
			bodega_api.set_picked_qty(pl_name, row_name, 2)  # no start_picking() call at all
		frappe.db.commit()

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertIn("picked_qty", status["blockers"])
			self.assertNotIn("bodega_started", status["blockers"])  # isolates the two signals

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

	def test_modification_blocked_by_submitted_pick_list(self):
		wh = self.world.warehouse("FG18-5b SubmittedPL")
		item = self.world.item("FG18-5b-SUBMITTEDPL-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		bodega_user = self.world.user("fg18-5b-bodega-submittedpl@example.com", ["Bodega"])
		self.world.warehouse_user_permission(bodega_user, wh.name)

		so_name = self._submit_order(item, 5)
		pl_name = self._pick_list_for(so_name)

		with fx.as_user(bodega_user):
			bodega_api.start_picking(pl_name)
			row = bodega_api.get_pick_list(pl_name)["rows"][0]
			bodega_api.set_picked_qty(pl_name, row["row_name"], row["qty_solicitada"])
			bodega_api.finish_picking(pl_name)  # submits the Pick List
		frappe.db.commit()

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertIn("pick_list_submitted", status["blockers"])

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Pick List", pl_name, "docstatus"), 1)  # untouched

	# =====================================================================
	# Blocked -- submitted Material Request / related Purchase Order
	# =====================================================================

	def test_modification_blocked_by_submitted_material_request(self):
		wh = self.world.warehouse("FG18-5b SubmittedMR")
		item = self.world.item(
			"FG18-5b-SUBMITTEDMR-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		# No stock at all -- create_and_submit_sales_order()'s own on_submit
		# hook already produces a draft, Engine-created Material Request
		# (Commit 19.1/19.2); submitting THAT draft (Compras reviewing it)
		# is what must block modification here.
		so_name = self._submit_order(item, 6)

		mr_name = frappe.get_all(
			"Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
		)[0]
		frappe.get_doc("Material Request", mr_name).submit()
		frappe.db.commit()

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertIn("material_request_submitted", status["blockers"])

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)

	def test_modification_blocked_by_related_purchase_order(self):
		wh = self.world.warehouse("FG18-5b RelatedPO")
		item = self.world.item(
			"FG18-5b-RELATEDPO-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		supplier_name = "FG18-5b Test Supplier"
		if not frappe.db.exists("Supplier", supplier_name):
			doc = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}
			)
			doc.insert()
			self.world.track_existing("Supplier", doc.name)

		so_name = self._submit_order(item, 6)
		mr_name = frappe.get_all(
			"Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
		)[0]
		frappe.get_doc("Material Request", mr_name).submit()  # native precondition for make_purchase_order()

		po = make_purchase_order(mr_name)
		po.supplier = supplier_name
		po.insert()
		po.submit()
		self.world.track_existing("Purchase Order", po.name)
		frappe.db.commit()

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertIn("purchase_order_linked", status["blockers"])

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "docstatus"), 1)  # untouched

	def test_modification_blocked_by_downstream_delivery_note(self):
		"""Delivery Note created directly off the Sales Order (native
		make_delivery_note(), no Pick List involved at all) -- the
		defensive, likely-redundant-in-this-app's-real-flow check."""
		wh = self.world.warehouse("FG18-5b DeliveryNote")
		item = self.world.item("FG18-5b-DN-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)

		so_name = self._submit_order(item, 4)

		dn = make_delivery_note(so_name)
		dn.insert()
		dn.submit()
		self.world.track_existing("Delivery Note", dn.name)
		frappe.db.commit()

		with fx.as_user(self.vendedora_a):
			status = ventas.get_modification_status(so_name)
			self.assertIn("downstream_document", status["blockers"])

			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

	# =====================================================================
	# Permissions
	# =====================================================================

	def test_another_vendedora_cannot_modify_it(self):
		wh = self.world.warehouse("FG18-5b OtherVendedora")
		item = self.world.item("FG18-5b-OTHER-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 10)
		so_name = self._submit_order(item, 4)

		with fx.as_user(self.vendedora_b):
			with self.assertRaises(frappe.PermissionError):
				ventas.get_modification_status(so_name)
			with self.assertRaises(frappe.PermissionError):
				ventas.get_order_for_modification(so_name)
			with self.assertRaises(frappe.PermissionError):
				ventas.modify_submitted_sales_order(
					name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
				)

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)

	# =====================================================================
	# Commercial identity across amendments
	# =====================================================================

	def test_modification_chain_of_multiple_amendments_resolves_commercial_root(self):
		wh = self.world.warehouse("FG18-5b Chain")
		item = self.world.item("FG18-5b-CHAIN-ITEM", default_warehouse=wh.name)
		self.world.stock_up_real(item.name, wh.name, 30)

		so_name = self._submit_order(item, 5)

		with fx.as_user(self.vendedora_a):
			first = ventas.modify_submitted_sales_order(
				name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 7}]
			)
		self.world.track_existing("Sales Order", first["name"])
		self.world.track_existing_pick_lists_and_reports_for(first["name"])
		self.assertEqual(first["commercial_name"], so_name)

		with fx.as_user(self.vendedora_a):
			second = ventas.modify_submitted_sales_order(
				name=first["name"], customer=self.customer.name, items=[{"item_code": item.name, "qty": 9}]
			)
		self.world.track_existing("Sales Order", second["name"])
		self.world.track_existing_pick_lists_and_reports_for(second["name"])
		self.assertEqual(second["commercial_name"], so_name)  # still the very first name, two amends later

		with fx.as_user(self.vendedora_a):
			detail = ventas.get_order_detail(second["name"])
		self.assertEqual(detail["commercial_name"], so_name)
		self.assertEqual(detail["name"], second["name"])

		# Exactly one card for the whole chain, labelled with the root name,
		# holding the LATEST version's data -- no separate card for the
		# original or the first amendment.
		with fx.as_user(self.vendedora_a):
			orders = ventas.get_my_orders()
		matching = [o for o in orders if o["commercial_name"] == so_name]
		self.assertEqual(len(matching), 1)
		self.assertEqual(matching[0]["name"], second["name"])
		self.assertEqual(flt(matching[0]["total_qty"]), 9.0)

	# =====================================================================
	# Downstream artifact recalculation
	# =====================================================================

	def test_modification_regenerates_shortage_report_and_material_request(self):
		wh = self.world.warehouse("FG18-5b Regen")
		item = self.world.item(
			"FG18-5b-REGEN-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 3)

		so_name = self._submit_order(item, 10)  # 3 available, 7 short

		old_report = frappe.get_all("Reporte de Faltante", filters={"sales_order": so_name}, pluck="name")[0]
		old_mr = frappe.get_all(
			"Material Request Item", filters={"sales_order": so_name}, pluck="parent", distinct=True
		)[0]
		self.assertEqual(frappe.get_doc("Reporte de Faltante", old_report).status, "Abierto")

		with fx.as_user(self.vendedora_a):
			result = ventas.modify_submitted_sales_order(
				name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 3}]  # now fully covered
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		# Old shortage report: resolved automatically by the standard
		# on_cancel cleanup (Commit 17) -- kept as history, not deleted.
		self.assertEqual(frappe.get_doc("Reporte de Faltante", old_report).status, "Resuelto")
		# Old draft Material Request: this Sales Order was its only row ->
		# removed entirely (Commit 19.3's "removed" case).
		self.assertFalse(frappe.db.exists("Material Request", old_mr))

		# New version, now fully covered by stock -- no new shortage, no new MR.
		new_reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")
		self.assertEqual(new_reports, [])
		new_mrs = frappe.get_all(
			"Material Request Item", filters={"sales_order": result["name"]}, pluck="parent", distinct=True
		)
		self.assertEqual(new_mrs, [])

	def test_modification_uses_current_stock_not_original(self):
		wh = self.world.warehouse("FG18-5b FreshStock")
		item = self.world.item(
			"FG18-5b-FRESHSTOCK-ITEM", default_material_request_type="Purchase", default_warehouse=wh.name
		)
		self.world.stock_up_real(item.name, wh.name, 2)

		so_name = self._submit_order(item, 10)  # 2 available, 8 short at creation time

		pl_name = self._pick_list_for(so_name)
		original_pl_qty = flt(
			frappe.get_all("Pick List Item", filters={"parent": pl_name}, pluck="stock_qty")[0]
		)
		self.assertEqual(original_pl_qty, 2.0)

		# More stock arrives before the modification.
		self.world.stock_up(item.name, wh.name, 10)

		with fx.as_user(self.vendedora_a):
			result = ventas.modify_submitted_sales_order(
				name=so_name, customer=self.customer.name, items=[{"item_code": item.name, "qty": 10}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])

		new_pl_name = self._pick_list_for(result["name"])
		new_pl_qty = flt(frappe.get_all("Pick List Item", filters={"parent": new_pl_name}, pluck="stock_qty")[0])
		self.assertEqual(new_pl_qty, 10.0)  # fully covered by the stock that arrived in between

		reports = frappe.get_all("Reporte de Faltante", filters={"sales_order": result["name"]}, pluck="name")
		self.assertEqual(reports, [])
