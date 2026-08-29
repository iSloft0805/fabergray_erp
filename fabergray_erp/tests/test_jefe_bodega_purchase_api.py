# -*- coding: utf-8 -*-
"""Commit 22.8 -- api.jefe_bodega.receive_shortage_purchase()/
get_shortage_purchase_status(): "Registrar compra recibida" from the
Reporte de Faltante flow, via a native, submitted Stock Entry
(purpose="Material Receipt") -- never a direct Bin/Stock Ledger Entry/GL
Entry write, never frappe.db.commit(), never a hardcoded or auto-configured
accounting account.

This site's real chart of accounts has no resolvable Difference Account for
Stock Entry today (Commit 22.8's own audit: Company.stock_adjustment_account
unset, no Item Default/Item Group Default expense_account for any real
Item) -- every test that expects a SUCCESSFUL receipt wraps itself in
fx.company_defaults(stock_adjustment_account=<a throwaway test-only
"Stock Adjustment"-type Account, fixtures.TestWorld.stock_difference_
account()>) (the same transient-real-account mechanism Commit 19.4/22.6
already use for an identical native gap), restoring the site's real
unconfigured state afterwards. test_missing_receipt_account_produces_
functional_error is the
one test that deliberately does NOT do this, proving the real, current
production gap is caught with a clear functional error rather than crashing
or silently guessing an account.

Twenty-six kinds of check, matching the approved Commit 22.8 brief (24
write-side + 2 read-endpoint groups): permissions (Jefe de Bodega/System
Manager allowed, Bodega denied); qty/purchase_rate/warehouse/shortage
validation; the account-safety gap; native stock movement + valuation
(incoming rate, Bin increase); Standard Buying Item Price create-or-update,
never duplicated; partial receipt leaves En Proceso with correct
received/remaining; a second receipt completes it (Resuelto); two receipts
stay independently traceable; a cancelled Stock Entry stops counting;
Pick List.picked_qty/Sales Order.delivered_qty are never touched; a full
accounting-error rollback; structural guardrails (no direct Bin/Stock
Ledger Entry/GL Entry write, no frappe.db.commit(), no ignore_permissions/
frappe.set_user/frappe.get_all/frappe.db.sql anywhere in the two whitelisted
functions); and idempotency (a Resuelto report rejects a second receipt
outright, so a double-submit landing after the first already completed the
full qty_faltante can never register twice)."""

import ast
import inspect

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import jefe_bodega as api
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_WHITELISTED_FUNCTIONS = ("receive_shortage_purchase", "get_shortage_purchase_status")

_FORBIDDEN_CALLS = {"frappe.set_user", "frappe.get_all", "frappe.db.commit", "frappe.db.sql"}

#: Literal doctype names this flow must never write directly -- the whole
#: point of using a native Stock Entry is that ERPNext, not this module,
#: ever creates one of these three.
_FORBIDDEN_DIRECT_WRITE_DOCTYPES = ("Bin", "Stock Ledger Entry", "GL Entry")


def _dotted_name(node):
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return ".".join(reversed(parts))
	return None


def _forbidden_findings(source):
	"""Real AST walk (not a substring search) -- see
	test_clientes_write_api.py's own _forbidden_findings() for the exact
	same technique/reasoning, reused here."""
	tree = ast.parse(source)
	findings = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			dotted = _dotted_name(node.func)
			if dotted in _FORBIDDEN_CALLS:
				findings.append(dotted)
			if dotted in ("frappe.get_doc", "frappe.new_doc") and node.args:
				arg = node.args[0]
				literal = None
				if isinstance(arg, ast.Constant):
					literal = arg.value
				elif isinstance(arg, ast.Dict):
					for k, v in zip(arg.keys, arg.values):
						if (
							isinstance(k, ast.Constant)
							and k.value == "doctype"
							and isinstance(v, ast.Constant)
						):
							literal = v.value
				if literal in _FORBIDDEN_DIRECT_WRITE_DOCTYPES:
					findings.append(f"direct write: {literal}")
		if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
			findings.append("ignore_permissions=")
	return findings


class TestJefeDeBodegaPurchaseApi(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.warehouse = cls.world.warehouse("FG228 Purchase Receipt Wh")
		cls.jefe = cls.world.user("fg228-jefe@example.com", ["Jefe de Bodega"])
		cls.bodega_user = cls.world.user("fg228-bodega@example.com", ["Bodega"])
		cls.sysmgr = cls.world.user("fg228-sysmgr@example.com", ["System Manager"])
		# See fixtures.TestWorld.stock_difference_account()'s own docstring:
		# fx.STOCK_ADJUSTMENT_ACCOUNT (account_type="Stock") is exactly what
		# Stock Reconciliation needs, but Stock Entry's own
		# validate_difference_account() explicitly rejects a Stock-type
		# Difference Account -- this is a distinct, throwaway
		# "Stock Adjustment"-type account, shared read-only across every
		# test below via fx.company_defaults(), never Company's own real
		# (unset) stock_adjustment_account.
		cls.difference_account = cls.world.stock_difference_account()

	def _item(self, suffix):
		return self.world.item(f"FG228-{suffix}-ITEM")

	def _shortage(self, item_code, warehouse=None, qty_solicitada=15, qty_disponible=0):
		doc = frappe.get_doc(
			{
				"doctype": "Reporte de Faltante",
				"item_code": item_code,
				"warehouse": warehouse or self.warehouse.name,
				"qty_solicitada": qty_solicitada,
				"qty_disponible": qty_disponible,
				"detected_by": "Bodega",
				"shortage_reason": "Compra pendiente",
			}
		)
		doc.insert()
		self.world.track_existing("Reporte de Faltante", doc.name)
		return doc

	def _bin_qty(self, item_code, warehouse=None):
		return frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse or self.warehouse.name}, "actual_qty"
		) or 0

	# -- 1/2/23. Permisos -----------------------------------------------------

	def test_jefe_de_bodega_can_receive_purchase(self):
		item = self._item("JEFE-OK")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)

		self.assertTrue(frappe.db.exists("Stock Entry", result["stock_entry"]))
		self.world.track_existing("Stock Entry", result["stock_entry"])

	def test_bodega_cannot_receive_purchase(self):
		item = self._item("BODEGA-DENY")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.bodega_user):
				self.assertFalse(frappe.has_permission("Stock Entry", "create"))
				with self.assertRaises(frappe.PermissionError):
					api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)

		self.assertEqual(frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name}), [])

	def test_system_manager_can_receive_purchase(self):
		item = self._item("SYSMGR-OK")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.sysmgr):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])
		self.assertEqual(result["status"], "Resuelto")

	# -- 3/4/5/6/7. Validaciones -----------------------------------------------

	def test_non_positive_qty_rejected(self):
		item = self._item("QTY-NEG")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				with self.assertRaises(api.NonPositiveReceiptQuantityError):
					api.receive_shortage_purchase(report.name, qty=0, purchase_rate=3500)
				with self.assertRaises(api.NonPositiveReceiptQuantityError):
					api.receive_shortage_purchase(report.name, qty=-5, purchase_rate=3500)

		self.assertEqual(self._bin_qty(item.name), 0)

	def test_non_positive_purchase_rate_rejected(self):
		item = self._item("RATE-NEG")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				with self.assertRaises(api.NonPositivePurchaseRateError):
					api.receive_shortage_purchase(report.name, qty=10, purchase_rate=0)

		self.assertEqual(self._bin_qty(item.name), 0)

	def test_invalid_warehouse_rejected(self):
		item = self._item("WH-INVALID")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				with self.assertRaises(api.InvalidReceiptWarehouseError):
					api.receive_shortage_purchase(
						report.name, qty=10, purchase_rate=3500, warehouse="FG228 No Existe - FG"
					)

	def test_different_warehouse_rejected(self):
		"""This flow completes ONE specific shortage -- a real, existing,
		DIFFERENT Warehouse than the report's own must still be rejected
		(MismatchedReceiptWarehouseError), never silently redirected there.
		The exact risk this closes: resolving a Producto Terminado shortage
		while accidentally receiving stock into another Warehouse (e.g.
		Devoluciones)."""
		other_warehouse = self.world.warehouse("FG228 Otro Almacen")
		item = self._item("WH-DIFERENTE")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				with self.assertRaises(api.MismatchedReceiptWarehouseError):
					api.receive_shortage_purchase(
						report.name, qty=10, purchase_rate=3500, warehouse=other_warehouse.name
					)

		self.assertEqual(frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name}), [])
		self.assertEqual(self._bin_qty(item.name, other_warehouse.name), 0)

	def test_explicit_matching_warehouse_still_works(self):
		"""Passing the report's own warehouse explicitly (what the UI always
		does, even though the field is read-only) must not be confused with
		overriding it -- same outcome as omitting the argument entirely."""
		item = self._item("WH-EXPLICITO")
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(
					report.name, qty=15, purchase_rate=3500, warehouse=self.warehouse.name
				)
		self.world.track_existing("Stock Entry", result["stock_entry"])
		self.assertEqual(result["status"], "Resuelto")

	# -- Sobre-recepción: qty > remaining_qty ----------------------------------

	def test_over_receipt_rejected(self):
		"""remaining_qty=5, qty=10 attempted -- this endpoint completes a
		specific shortage, it does not log a general purchase, so this must
		be rejected outright with the exact remaining amount in the
		message, never silently truncated or accepted."""
		item = self._item("SOBRE-RECEPCION")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				first = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
				self.world.track_existing("Stock Entry", first["stock_entry"])
				self.assertEqual(first["remaining_qty"], 5)

				with self.assertRaises(api.ExcessReceiptQuantityError) as ctx:
					api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3600)
				self.assertIn("5", str(ctx.exception))

		entries = frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name})
		self.assertEqual(len(entries), 1)  # the rejected over-receipt created nothing

		prices = frappe.get_list(
			"Item Price", filters={"item_code": item.name, "price_list": "Standard Buying"}, fields=["price_list_rate"]
		)
		self.assertEqual(len(prices), 1)
		self.assertEqual(prices[0].price_list_rate, 3500)  # unchanged by the rejected attempt

		report.reload()
		self.assertEqual(report.status, "En Proceso")  # unchanged by the rejected attempt

	def test_exact_remaining_qty_still_completes_it(self):
		"""Boundary check: qty == remaining_qty (not >) must succeed and
		resolve the report -- the cap rejects only strictly-over, never the
		exact completing amount."""
		item = self._item("EXACTO-RESTANTE")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				first = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
				self.world.track_existing("Stock Entry", first["stock_entry"])

				second = api.receive_shortage_purchase(report.name, qty=5, purchase_rate=3600)
				self.world.track_existing("Stock Entry", second["stock_entry"])

		self.assertEqual(second["remaining_qty"], 0)
		self.assertEqual(second["status"], "Resuelto")

	def test_nonexistent_shortage_report_rejected(self):
		with fx.as_user(self.jefe):
			with self.assertRaises(frappe.DoesNotExistError):
				api.receive_shortage_purchase("FALT-2026-99999-NOPE", qty=10, purchase_rate=3500)

	def test_already_resolved_shortage_rejected(self):
		item = self._item("YA-RESUELTO")
		report = self._shortage(item.name)
		report.status = "Resuelto"
		report.resolution_note = "Cerrado manualmente para el test."
		report.save()

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				with self.assertRaises(api.ShortageAlreadyResolvedError):
					api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)

		self.assertEqual(frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name}), [])

	# -- 8/9. Movimiento nativo + incoming rate --------------------------------

	def test_submitted_stock_entry_increases_stock_with_correct_rate(self):
		item = self._item("MOVIMIENTO")
		report = self._shortage(item.name, qty_solicitada=10, qty_disponible=0)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.purpose, "Material Receipt")
		self.assertEqual(entry.fg_shortage_report, report.name)
		self.assertEqual(entry.items[0].basic_rate, 3500)
		self.assertEqual(self._bin_qty(item.name), 10)

		sle = frappe.get_list(
			"Stock Ledger Entry",
			filters={"voucher_no": entry.name, "voucher_type": "Stock Entry"},
			fields=["incoming_rate", "actual_qty"],
		)
		self.assertEqual(len(sle), 1)
		self.assertEqual(sle[0].incoming_rate, 3500)
		self.assertEqual(sle[0].actual_qty, 10)

	# -- 10/11/12. Standard Buying Item Price ----------------------------------

	def test_standard_buying_created_when_missing(self):
		item = self._item("PRICE-CREATE")
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": item.name, "price_list": "Standard Buying"}))
		report = self._shortage(item.name)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		prices = frappe.get_list(
			"Item Price", filters={"item_code": item.name, "price_list": "Standard Buying"}, fields=["name", "price_list_rate"]
		)
		self.assertEqual(len(prices), 1)
		self.assertEqual(prices[0].price_list_rate, 3500)
		self.world.track_existing("Item Price", prices[0].name)

	def test_standard_buying_updated_when_existing(self):
		item = self._item("PRICE-UPDATE")
		existing = frappe.get_doc(
			{"doctype": "Item Price", "item_code": item.name, "price_list": "Standard Buying", "price_list_rate": 1000}
		)
		existing.insert()
		self.world.track_existing("Item Price", existing.name)

		report = self._shortage(item.name)
		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=4200)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		existing.reload()
		self.assertEqual(existing.price_list_rate, 4200)

	def test_standard_buying_never_duplicated_across_two_receipts(self):
		item = self._item("PRICE-NODUP")
		report = self._shortage(item.name, qty_solicitada=20)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				r1 = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
				r2 = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3700)
		self.world.track_existing("Stock Entry", r1["stock_entry"])
		self.world.track_existing("Stock Entry", r2["stock_entry"])

		prices = frappe.get_list("Item Price", filters={"item_code": item.name, "price_list": "Standard Buying"})
		self.assertEqual(len(prices), 1)
		self.world.track_existing("Item Price", prices[0].name)

	# -- 13/14/15. Recepción parcial + trazabilidad ----------------------------

	def test_partial_receipt_leaves_en_proceso(self):
		item = self._item("PARCIAL")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		self.assertEqual(result["received_qty"], 10)
		self.assertEqual(result["remaining_qty"], 5)
		self.assertEqual(result["status"], "En Proceso")

		report.reload()
		self.assertEqual(report.status, "En Proceso")
		self.assertEqual(report.qty_faltante, 15)  # original snapshot, untouched

	def test_second_receipt_completes_pending_and_resolves(self):
		item = self._item("COMPLETA")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				r1 = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
				r2 = api.receive_shortage_purchase(report.name, qty=5, purchase_rate=3600)
		self.world.track_existing("Stock Entry", r1["stock_entry"])
		self.world.track_existing("Stock Entry", r2["stock_entry"])

		self.assertEqual(r2["received_qty"], 15)
		self.assertEqual(r2["remaining_qty"], 0)
		self.assertEqual(r2["status"], "Resuelto")

		report.reload()
		self.assertEqual(report.status, "Resuelto")
		self.assertIn("recepción de compra", report.resolution_note)

	def test_two_receipts_are_traced_independently(self):
		item = self._item("TRAZA")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				r1 = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
				r2 = api.receive_shortage_purchase(report.name, qty=5, purchase_rate=3600)
		self.world.track_existing("Stock Entry", r1["stock_entry"])
		self.world.track_existing("Stock Entry", r2["stock_entry"])

		with fx.as_user(self.jefe):
			status = api.get_shortage_purchase_status(report.name)

		self.assertEqual(len(status["receipts"]), 2)
		by_entry = {r["stock_entry"]: r for r in status["receipts"]}
		self.assertEqual(by_entry[r1["stock_entry"]]["qty"], 10)
		self.assertEqual(by_entry[r1["stock_entry"]]["purchase_rate"], 3500)
		self.assertEqual(by_entry[r2["stock_entry"]]["qty"], 5)
		self.assertEqual(by_entry[r2["stock_entry"]]["purchase_rate"], 3600)
		self.assertEqual(status["received_qty"], 15)
		self.assertEqual(status["remaining_qty"], 0)

	# -- 16. Stock Entry cancelado deja de contar ------------------------------

	def test_cancelled_stock_entry_stops_counting(self):
		item = self._item("CANCELADO")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=10, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		entry.cancel()

		with fx.as_user(self.jefe):
			status = api.get_shortage_purchase_status(report.name)

		self.assertEqual(status["receipts"], [])
		self.assertEqual(status["received_qty"], 0)
		self.assertEqual(status["remaining_qty"], 15)

	# -- 17. Al completar se marca Resuelto (mismo caso que 14, verificado aparte) --

	def test_full_receipt_in_one_call_resolves_immediately(self):
		item = self._item("COMPLETO-UNA-VEZ")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		self.assertEqual(result["status"], "Resuelto")
		report.reload()
		self.assertEqual(report.status, "Resuelto")

	# -- 18/19. No altera Pick List / Sales Order ------------------------------

	def test_does_not_modify_pick_list_or_sales_order(self):
		item = self.world.item("FG228-PLSO-ITEM", default_warehouse=self.warehouse.name)
		self.world.stock_up(item.name, self.warehouse.name, 20)
		customer = self.world.customer("FG228 Purchase Receipt Customer")
		so = self.world.submitted_sales_order(item.name, self.warehouse.name, 15, customer.name)
		pl = self.world.pick_list_for(so, self.warehouse.name)
		pl.locations[0].picked_qty = 4
		pl.save()

		report = self._shortage(
			item.name, warehouse=self.warehouse.name, qty_solicitada=15, qty_disponible=0
		)
		report.sales_order = so.name
		report.pick_list = pl.name
		report.pick_list_item = pl.locations[0].name
		report.save()

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
		self.world.track_existing("Stock Entry", result["stock_entry"])

		pl.reload()
		self.assertEqual(pl.locations[0].picked_qty, 4)  # unchanged

		so.reload()
		self.assertEqual(so.items[0].delivered_qty, 0)  # unchanged

	# -- 20. Error contable produce rollback completo ---------------------------

	def test_missing_receipt_account_produces_functional_error(self):
		"""Deliberately NOT wrapped in fx.company_defaults() -- this is the
		real, current, unconfigured state of the site (Commit 22.8's own
		audit), proving the endpoint fails with a clear functional error
		instead of ERPNext's own internal message, a hardcoded account, or
		Company.stock_adjustment_account being silently configured."""
		item = self._item("SIN-CUENTA")
		report = self._shortage(item.name)

		with fx.as_user(self.jefe):
			with self.assertRaises(api.MissingReceiptAccountError):
				api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)

		self.assertEqual(frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name}), [])
		self.assertEqual(
			frappe.get_list("Item Price", filters={"item_code": item.name, "price_list": "Standard Buying"}), []
		)
		report.reload()
		self.assertEqual(report.status, "Abierto")
		self.assertIsNone(frappe.db.get_value("Company", "fabrigraysas", "stock_adjustment_account"))

	# -- 21/22. Guardrails estructurales ----------------------------------------

	def test_no_forbidden_calls_or_direct_bin_sle_gl_writes(self):
		for name in _WHITELISTED_FUNCTIONS:
			fn = getattr(api, name)
			source = inspect.getsource(fn)
			findings = _forbidden_findings(source)
			self.assertEqual(findings, [], f"{name}() contains forbidden pattern(s): {findings}")

	# -- 23. Ya cubierto arriba (test_bodega_cannot_receive_purchase) -----------

	# -- 24. Idempotencia / doble submit -----------------------------------------

	def test_cannot_register_twice_once_fully_resolved(self):
		item = self._item("DOBLE-SUBMIT")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.company_defaults(stock_adjustment_account=self.difference_account.name):
			with fx.as_user(self.jefe):
				result = api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)
				self.world.track_existing("Stock Entry", result["stock_entry"])

				with self.assertRaises(api.ShortageAlreadyResolvedError):
					api.receive_shortage_purchase(report.name, qty=15, purchase_rate=3500)

		entries = frappe.get_list("Stock Entry", filters={"fg_shortage_report": report.name})
		self.assertEqual(len(entries), 1)  # the double-submit never created a second one

	# -- Endpoint de lectura ------------------------------------------------------

	def test_get_status_before_any_receipt(self):
		item = self._item("STATUS-VACIO")
		report = self._shortage(item.name, qty_solicitada=15)

		with fx.as_user(self.jefe):
			status = api.get_shortage_purchase_status(report.name)

		self.assertEqual(status["received_qty"], 0)
		self.assertEqual(status["remaining_qty"], 15)
		self.assertEqual(status["receipts"], [])
		self.assertEqual(status["status"], "Abierto")

	def test_get_status_reflects_permission(self):
		item = self._item("STATUS-PERM")
		report = self._shortage(item.name)

		with fx.as_user(self.bodega_user):
			# Bodega has native read on Reporte de Faltante -- the read
			# endpoint itself is not restricted the way the write one is.
			status = api.get_shortage_purchase_status(report.name)
		self.assertEqual(status["shortage_report"], report.name)
