# -*- coding: utf-8 -*-
"""Commit 23.0 -- "Facturación operativa sin Sales Invoice":
api.facturacion.get_invoicing_summary()/get_invoicing_queue()/
get_invoicing_detail()/set_invoicing_item_checked()/mark_as_invoiced(). A
purely OPERATIONAL checkbox on Pick List (fg_invoicing_status/
fg_invoiced_on/fg_invoiced_by) PLUS a per-item review checklist on Pick
List Item (fg_invoicing_checked/fg_invoicing_checked_on/
fg_invoicing_checked_by) -- both sets of Custom Fields, all
allow_on_submit=1 -- deliberately never touching Sales Invoice, GL Entry,
Payment Entry, or any native accounting/delivery/qty field (Sales
Order.per_billed/billing_status, Pick List.delivery_status/per_delivered,
Pick List Item.qty/picked_qty/delivered_qty).

The checklist is a correction to this same commit: the first version let
mark_as_invoiced() flip a Pick List straight to Facturado with no per-item
review, removing real functionality the previous (Sales-Invoice-backed)
flow had -- Commit 21.5's own "VERIFICADO" checklist. That checklist was
audited first and found to be frontend-only, never persisted, so this
correction adds real server-side persistence from scratch rather than
reusing anything (there was nothing to reuse) -- see api/facturacion.py's
own top docstring for the full audit trail.

generate_invoice() (Commit 21.3, api/facturacion.py) is untouched, kept as
legacy -- its own dedicated suite (test_facturacion_generate_invoice.py)
still exercises it; nothing here calls it, and
test_new_flow_never_reaches_the_invoicing_engine below proves that
statically (AST), not just by omission.

Every scenario from the approved correction brief's "Tests nuevos/
actualizados" list (20 items) plus the original Commit 23.0 brief's own 18,
all still exercised (now routed through a completed checklist first)."""

import ast
import inspect
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from fabergray_erp.api import bodega, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_FORBIDDEN_CALLS = {
	"generate_invoice",
	"create_delivery",
	"make_sales_invoice",
	"frappe.db.commit",
	"frappe.get_all",
	"frappe.set_user",
}


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
	"""Real AST walk -- see test_clientes_write_api.py's own
	_forbidden_findings() for the exact same technique. Additionally flags
	any Call whose first argument is the literal string "Sales Invoice"
	(covers frappe.get_doc("Sales Invoice", ...)/frappe.new_doc("Sales
	Invoice") specifically, per the brief's own guardrail list) and any
	literal `ignore_permissions=True`."""
	tree = ast.parse(source)
	findings = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			dotted = _dotted_name(node.func)
			if dotted in _FORBIDDEN_CALLS:
				findings.append(dotted)
			if dotted in ("frappe.get_doc", "frappe.new_doc") and node.args:
				arg = node.args[0]
				if isinstance(arg, ast.Constant) and arg.value == "Sales Invoice":
					findings.append(f"{dotted}('Sales Invoice')")
		if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
			if isinstance(node.value, ast.Constant) and node.value.value in (True, 1):
				findings.append("ignore_permissions=True")
	return findings


@contextmanager
def frappe_monkeypatch(module, attr, value):
	original = getattr(module, attr)
	setattr(module, attr, value)
	try:
		yield
	finally:
		setattr(module, attr, original)


class TestFacturacionInvoicingStatus(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG230 WH")
		cls.item = cls.world.item("FG230-ITEM")
		cls.item2 = cls.world.item("FG230-ITEM-2")
		cls.customer = cls.world.customer("FG230 Customer")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up_real(cls.item2.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg230-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg230-facturacion@example.com", ["Facturación"])
		cls.facturacion_user_b = cls.world.user("fg230-facturacion-b@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user("fg230-vendedora@example.com", ["Vendedora"])

	def _submitted_pick_list(self, qty=5, rate=100):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, self.customer.name, rate=rate)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	def _check_all_items(self, pl_name, user=None):
		"""Reviews every Pick List Item row via the real endpoint (never a
		direct DB write) -- the one helper every test that expects
		mark_as_invoiced() to succeed goes through, since Commit 23.0's
		correction made a complete checklist a hard server-side
		precondition."""
		with fx.as_user(user or self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl_name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl_name, item["row_name"], 1)

	# -- 1/2/3/4. Marcar como facturado + persistencia -------------------------

	def test_ready_pick_list_can_be_marked_invoiced(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			result = facturacion.mark_as_invoiced(pl.name)
		self.assertEqual(result["fg_invoicing_status"], "Facturado")

	def test_persists_fg_invoicing_status(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertEqual(pl.fg_invoicing_status, "Facturado")

	def test_persists_fg_invoiced_on(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		before = frappe.utils.now_datetime()
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertIsNotNone(pl.fg_invoiced_on)
		self.assertGreaterEqual(pl.fg_invoiced_on, frappe.utils.add_to_date(before, seconds=-5))

	def test_persists_fg_invoiced_by(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertEqual(pl.fg_invoiced_by, self.facturacion_user)

	# -- 5. Idempotencia ---------------------------------------------------------

	def test_second_click_does_not_duplicate(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			first = facturacion.mark_as_invoiced(pl.name)
			with self.assertRaises(facturacion.AlreadyInvoicedError):
				facturacion.mark_as_invoiced(pl.name)

		pl.reload()
		self.assertEqual(pl.fg_invoiced_on, first["fg_invoiced_on"])  # never overwritten by the rejected 2nd call
		self.assertEqual(pl.fg_invoiced_by, first["fg_invoiced_by"])

	# -- 6. Permisos ---------------------------------------------------------------

	def test_user_without_permission_is_denied(self):
		_, pl = self._submitted_pick_list()
		with fx.as_user(self.vendedora_user):
			self.assertFalse(frappe.has_permission("Pick List", "write"))
			with self.assertRaises(frappe.PermissionError):
				facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertEqual(pl.fg_invoicing_status, "Pendiente")

	# -- 7/8/9. Nunca Sales Invoice / GL Entry / Payment Entry --------------------

	def test_no_sales_invoice_created(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		before = frappe.db.count("Sales Invoice")
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		self.assertEqual(frappe.db.count("Sales Invoice"), before)
		self.assertEqual(frappe.get_list("Sales Invoice Item", filters={"against_pick_list": pl.name}), [])

	def test_no_gl_entry_created(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		before = frappe.db.count("GL Entry")
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		self.assertEqual(frappe.db.count("GL Entry"), before)

	def test_no_payment_entry_created(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		before = frappe.db.count("Payment Entry")
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		self.assertEqual(frappe.db.count("Payment Entry"), before)

	# -- 10/11. Estado nativo contable/de entrega intacto --------------------------

	def test_sales_order_per_billed_unmodified(self):
		# Read both snapshots from the DB (not the in-memory doc returned by
		# submitted_sales_order(), whose float fields may be uncast None
		# rather than 0.0) so the comparison is apples-to-apples.
		so, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		per_billed_before, billing_status_before = frappe.db.get_value(
			"Sales Order", so.name, ["per_billed", "billing_status"]
		)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		per_billed_after, billing_status_after = frappe.db.get_value(
			"Sales Order", so.name, ["per_billed", "billing_status"]
		)
		self.assertEqual(flt(per_billed_after), flt(per_billed_before))
		self.assertEqual(billing_status_after, billing_status_before)

	def test_pick_list_delivery_status_unmodified(self):
		so, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		delivery_status_before = pl.delivery_status
		per_delivered_before = pl.per_delivered
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertEqual(pl.delivery_status, delivery_status_before)
		self.assertEqual(pl.per_delivered, per_delivered_before)

	# -- 12/13/14. Listados y métricas ----------------------------------------------

	def test_pending_list_excludes_invoiced(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
			pendientes = facturacion.get_invoicing_queue(status="Pendiente", page_length=200)
		self.assertNotIn(pl.name, [r["name"] for r in pendientes["pick_lists"]])

	def test_invoiced_list_includes_invoiced(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
			facturados = facturacion.get_invoicing_queue(status="Facturado", page_length=200)
		self.assertIn(pl.name, [r["name"] for r in facturados["pick_lists"]])

	def test_facturados_hoy_counts_correctly(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			before = facturacion.get_invoicing_summary()
			facturacion.mark_as_invoiced(pl.name)
			after = facturacion.get_invoicing_summary()
		self.assertEqual(after["facturados_hoy"], before["facturados_hoy"] + 1)
		self.assertEqual(after["facturados"], before["facturados"] + 1)
		self.assertEqual(after["pendientes"], before["pendientes"] - 1)

	# -- 15. Lectura devuelve usuario y fecha ----------------------------------------

	def test_read_returns_user_and_date(self):
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)
		with fx.as_user(self.facturacion_user):
			facturacion.mark_as_invoiced(pl.name)
			facturados = facturacion.get_invoicing_queue(status="Facturado", page_length=200)
		row = next(r for r in facturados["pick_lists"] if r["name"] == pl.name)
		self.assertEqual(row["fg_invoiced_by"], self.facturacion_user)
		self.assertIsNotNone(row["fg_invoiced_by_fullname"])
		self.assertIsNotNone(row["fg_invoiced_on"])

	# -- 16. Fallo de save -> rollback normal -----------------------------------------

	def test_save_failure_leaves_nothing_partially_applied(self):
		"""Simulates a real concurrent-edit conflict (the same native
		TimestampMismatchError mechanism Document.check_if_latest() already
		provides -- no custom locking invented here): the in-memory Pick
		List loaded by mark_as_invoiced() becomes stale the moment another
		process touches the row first. `.save()` must then raise and the
		Pick List must remain exactly as it was before the call -- proving
		a failed save is a real, atomic no-op, not a partial field
		commit."""
		_, pl = self._submitted_pick_list()
		self._check_all_items(pl.name)

		original_get_doc = frappe.get_doc

		def stale_get_doc(*args, **kwargs):
			doc = original_get_doc(*args, **kwargs)
			if args and args[0] == "Pick List" and len(args) > 1 and args[1] == pl.name:
				# Another writer touches the row AFTER this function's own
				# frappe.get_doc() already loaded it, but BEFORE .save()
				# runs -- the exact native race check_if_latest() exists
				# to catch.
				frappe.db.set_value("Pick List", pl.name, "modified", frappe.utils.now_datetime(), update_modified=False)
			return doc

		with fx.as_user(self.facturacion_user):
			with frappe_monkeypatch(frappe, "get_doc", stale_get_doc):
				with self.assertRaises(frappe.exceptions.TimestampMismatchError):
					facturacion.mark_as_invoiced(pl.name)

		pl.reload()
		self.assertEqual(pl.fg_invoicing_status, "Pendiente")
		self.assertIsNone(pl.fg_invoiced_on)
		self.assertIsNone(pl.fg_invoiced_by)

	# -- 17/18. Guardrails estructurales -----------------------------------------------

	def test_no_frappe_db_commit_in_new_functions(self):
		for fn in (
			facturacion.get_invoicing_summary,
			facturacion.get_invoicing_queue,
			facturacion.get_invoicing_detail,
			facturacion.set_invoicing_item_checked,
			facturacion.mark_as_invoiced,
		):
			source = inspect.getsource(fn)
			findings = _forbidden_findings(source)
			self.assertEqual(findings, [], f"{fn.__name__}() contains forbidden pattern(s): {findings}")

	def test_new_flow_never_reaches_the_invoicing_engine(self):
		"""The exact guardrail the brief asks for, extended to the whole new
		flow (not just mark_as_invoiced()): none of get_invoicing_detail()/
		set_invoicing_item_checked()/mark_as_invoiced() may ever call
		generate_invoice()/create_delivery()/make_sales_invoice() or
		frappe.get_doc("Sales Invoice", ...)/frappe.new_doc("Sales
		Invoice")."""
		for fn in (facturacion.get_invoicing_detail, facturacion.set_invoicing_item_checked, facturacion.mark_as_invoiced):
			findings = _forbidden_findings(inspect.getsource(fn))
			self.assertEqual(findings, [], f"{fn.__name__}() reaches the invoicing engine: {findings}")


class TestFacturacionInvoicingChecklist(IntegrationTestCase):
	"""Commit 23.0's correction: the per-item review checklist
	(get_invoicing_detail()/set_invoicing_item_checked()) and
	mark_as_invoiced()'s new hard dependency on it. Every scenario from the
	correction brief's own "Tests nuevos/actualizados" list (20 items,
	numbered in each test's own comment)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG231 WH")
		cls.item_a = cls.world.item("FG231-ITEM-A")
		cls.item_b = cls.world.item("FG231-ITEM-B")
		cls.customer = cls.world.customer("FG231 Customer")
		cls.world.stock_up_real(cls.item_a.name, cls.wh.name, 1000, rate=50)
		cls.world.stock_up_real(cls.item_b.name, cls.wh.name, 1000, rate=50)

		cls.bodega_user = cls.world.user("fg231-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg231-facturacion@example.com", ["Facturación"])
		cls.vendedora_user = cls.world.user("fg231-vendedora@example.com", ["Vendedora"])

	def _two_line_pick_list(self, qty_a=4, qty_b=6):
		"""A Pick List with TWO Pick List Item rows -- item_code is never
		unique on that child doctype (confirmed via its own meta, see api/
		facturacion.py's own top docstring), so this exercises the
		multi-row case the brief's own "no colapsar" requirement is about."""
		so = self.world.multi_item_sales_order(
			self.customer.name,
			[
				{"item_code": self.item_a.name, "warehouse": self.wh.name, "qty": qty_a, "rate": 100},
				{"item_code": self.item_b.name, "warehouse": self.wh.name, "qty": qty_b, "rate": 100},
			],
		)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return so, frappe.get_doc("Pick List", pl.name)

	# -- 1. get_invoicing_detail devuelve todos los ítems ---------------------------

	def test_get_invoicing_detail_returns_all_items(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
		self.assertEqual(len(detail["items"]), 2)
		self.assertEqual(detail["total_items"], 2)
		self.assertEqual({i["item_code"] for i in detail["items"]}, {self.item_a.name, self.item_b.name})

	# -- 2. cantidades correctas ------------------------------------------------------

	def test_get_invoicing_detail_quantities_correct(self):
		_, pl = self._two_line_pick_list(qty_a=4, qty_b=6)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
		by_item = {i["item_code"]: i for i in detail["items"]}
		self.assertEqual(flt(by_item[self.item_a.name]["qty"]), 4)
		self.assertEqual(flt(by_item[self.item_b.name]["qty"]), 6)
		self.assertEqual(flt(detail["total_qty"]), 10)
		for item in detail["items"]:
			self.assertNotIn("rate", item)
			self.assertNotIn("amount", item)
		self.assertNotIn("grand_total", detail)

	# -- 3. marcar un ítem checked -----------------------------------------------------

	def test_set_invoicing_item_checked_marks_item(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			row_name = detail["items"][0]["row_name"]
			result = facturacion.set_invoicing_item_checked(pl.name, row_name, 1)
		self.assertEqual(result["checked"], 1)
		self.assertIsNotNone(result["checked_on"])
		self.assertEqual(result["checked_by"], self.facturacion_user)

	# -- 4. desmarcar un ítem -----------------------------------------------------------

	def test_set_invoicing_item_checked_unmarks_item(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			row_name = detail["items"][0]["row_name"]
			facturacion.set_invoicing_item_checked(pl.name, row_name, 1)
			result = facturacion.set_invoicing_item_checked(pl.name, row_name, 0)
		self.assertEqual(result["checked"], 0)
		self.assertIsNone(result["checked_on"])
		self.assertIsNone(result["checked_by"])

	# -- 5/6. progreso persiste / otro reload devuelve progreso --------------------------

	def test_checklist_progress_persists_across_reload(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			row_name = detail["items"][0]["row_name"]
			facturacion.set_invoicing_item_checked(pl.name, row_name, 1)
			# A fresh call, own frappe.get_doc() load -- not the same Python
			# object -- proves the state is on the DB row, not in memory.
			reloaded = facturacion.get_invoicing_detail(pl.name)
		checked_row = next(i for i in reloaded["items"] if i["row_name"] == row_name)
		self.assertEqual(checked_row["checked"], 1)
		self.assertEqual(reloaded["checked_items"], 1)

	# -- 7. checked_items correcto -----------------------------------------------------

	def test_checked_items_count_correct(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["checked_items"], 0)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 1)
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["checked_items"], 1)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][1]["row_name"], 1)
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["checked_items"], 2)

	# -- 8. progress_percent correcto ---------------------------------------------------

	def test_progress_percent_correct(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["progress_percent"], 0)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 1)
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["progress_percent"], 50.0)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][1]["row_name"], 1)
			detail = facturacion.get_invoicing_detail(pl.name)
			self.assertEqual(detail["progress_percent"], 100.0)

	# -- 9. no permite row de otra Pick List --------------------------------------------

	def test_set_invoicing_item_checked_rejects_foreign_row(self):
		_, pl_a = self._two_line_pick_list()
		_, pl_b = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail_b = facturacion.get_invoicing_detail(pl_b.name)
			foreign_row = detail_b["items"][0]["row_name"]
			with self.assertRaises(frappe.DoesNotExistError):
				facturacion.set_invoicing_item_checked(pl_a.name, foreign_row, 1)
		pl_b.reload()
		self.assertFalse(any(cint_bool(r.fg_invoicing_checked) for r in pl_b.locations))

	# -- 10. usuario sin permisos rechazado ----------------------------------------------

	def test_set_invoicing_item_checked_permission_denied(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			row_name = facturacion.get_invoicing_detail(pl.name)["items"][0]["row_name"]
		with fx.as_user(self.vendedora_user):
			with self.assertRaises(frappe.PermissionError):
				facturacion.set_invoicing_item_checked(pl.name, row_name, 1)
		pl.reload()
		self.assertFalse(any(row.fg_invoicing_checked for row in pl.locations if row.name == row_name))

	# -- 11. no modifica cantidades ------------------------------------------------------

	def test_set_invoicing_item_checked_does_not_modify_qty(self):
		_, pl = self._two_line_pick_list(qty_a=4, qty_b=6)
		before = {r.name: (flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty)) for r in pl.locations}
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
		pl.reload()
		after = {r.name: (flt(r.qty), flt(r.picked_qty), flt(r.delivered_qty)) for r in pl.locations}
		self.assertEqual(after, before)

	# -- 12. no crea Sales Invoice --------------------------------------------------------

	def test_set_invoicing_item_checked_creates_no_sales_invoice(self):
		_, pl = self._two_line_pick_list()
		before = frappe.db.count("Sales Invoice")
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 1)
		self.assertEqual(frappe.db.count("Sales Invoice"), before)

	# -- 13. mark_as_invoiced falla con checklist incompleto --------------------------

	def test_mark_as_invoiced_fails_with_incomplete_checklist(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 1)  # only 1 of 2
			with self.assertRaisesRegex(
				facturacion.ChecklistIncompleteError,
				"Debes revisar todos los productos antes de marcar el pedido como facturado.",
			):
				facturacion.mark_as_invoiced(pl.name)
		pl.reload()
		self.assertEqual(pl.fg_invoicing_status, "Pendiente")

	def test_mark_as_invoiced_fails_with_zero_items_checked(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			with self.assertRaises(facturacion.ChecklistIncompleteError):
				facturacion.mark_as_invoiced(pl.name)

	# -- 14. mark_as_invoiced funciona con checklist 100% ------------------------------

	def test_mark_as_invoiced_succeeds_with_complete_checklist(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
			result = facturacion.mark_as_invoiced(pl.name)
		self.assertEqual(result["fg_invoicing_status"], "Facturado")

	# -- 15. después de Facturado no permite editar checklist ---------------------------

	def test_checklist_read_only_after_invoiced(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)

			with self.assertRaises(facturacion.ChecklistReadOnlyError):
				facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 0)

	# -- 16. segundo mark_as_invoiced sigue siendo idempotente/rechazado ----------------

	def test_second_mark_as_invoiced_still_rejected(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
			with self.assertRaises(facturacion.AlreadyInvoicedError):
				facturacion.mark_as_invoiced(pl.name)

	# -- 17. lista Pendientes muestra progreso -------------------------------------------

	def test_pending_queue_shows_progress(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			facturacion.set_invoicing_item_checked(pl.name, detail["items"][0]["row_name"], 1)
			queue = facturacion.get_invoicing_queue(status="Pendiente", page_length=200)
		row = next(r for r in queue["pick_lists"] if r["name"] == pl.name)
		self.assertEqual(row["total_items"], 2)
		self.assertEqual(row["checked_items"], 1)
		self.assertEqual(row["progress_percent"], 50.0)

	# -- 18. Facturados conserva checklist como read-only ---------------------------------

	def test_invoiced_queue_preserves_checklist(self):
		_, pl = self._two_line_pick_list()
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
			for item in detail["items"]:
				facturacion.set_invoicing_item_checked(pl.name, item["row_name"], 1)
			facturacion.mark_as_invoiced(pl.name)
			queue = facturacion.get_invoicing_queue(status="Facturado", page_length=200)
			row = next(r for r in queue["pick_lists"] if r["name"] == pl.name)
			self.assertEqual(row["checked_items"], 2)
			self.assertEqual(row["total_items"], 2)

			final_detail = facturacion.get_invoicing_detail(pl.name)
			self.assertTrue(all(i["checked"] for i in final_detail["items"]))
			with self.assertRaises(facturacion.ChecklistReadOnlyError):
				facturacion.set_invoicing_item_checked(pl.name, final_detail["items"][0]["row_name"], 0)

	# -- 19/20 (module-level guardrails) live in TestFacturacionInvoicingStatus
	# above (test_no_frappe_db_commit_in_new_functions /
	# test_new_flow_never_reaches_the_invoicing_engine), which already
	# include get_invoicing_detail()/set_invoicing_item_checked().


def cint_bool(v):
	return bool(frappe.utils.cint(v))
