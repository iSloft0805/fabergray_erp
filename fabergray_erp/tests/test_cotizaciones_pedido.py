# -*- coding: utf-8 -*-
"""Commit 25.17 -- "ENVIAR A PEDIDOS": converts an Aprobada, vigente
Quotation into a real Sales Order via ERPNext's own native
`erpnext.selling.doctype.quotation.quotation.make_sales_order()` mapper --
`cotizaciones.create_sales_order_from_quotation()` never hand-builds a
mapping of its own. See that function's own module-level comment
(api/cotizaciones.py, right above it) for the full audit of what the
native mapper already does (rate/discount preservation, native
`prevdoc_docname`/`quotation_item` link, Customer reuse, tax copy) --
these tests exercise that behaviour through the real endpoint, never by
re-deriving the mapper's own internals here.

Same convention as test_cotizaciones_pdf.py/test_cotizaciones_billing_
review.py: one class per concern, `fx.TestWorld` fixtures, plus a static
UI-contract class at the bottom reading cotizaciones.js as text (no JS
test runner in this app).
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from fabergray_erp.api import bodega as bodega_api
from fabergray_erp.api import cotizaciones
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_COTIZACIONES_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"
)


class TestCreateSalesOrderFromQuotation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.customer = cls.world.customer("FG2517 Customer")
		cls.vendedora = cls.world.user("fg2517-vendedora@example.com", ["Vendedora"])
		cls.facturacion = cls.world.user("fg2517-facturacion@example.com", ["Facturación"])
		cls.bodega_user = cls.world.user("fg2517-bodega@example.com", ["Bodega"])

	def _priced_item(self, item_code, rate=200, price_list="Standard Selling", **kwargs):
		"""apply_quotation_price_mode()/the mapper's own reference rate both
		need a real Item Price on the Quotation's own selling_price_list --
		same pattern test_cotizaciones_price_mode.py already establishes.
		`default_material_request_type="Manufacture"` (test_ventas_api.py's
		own `test_e2e_zero_stock_...` convention) keeps zero-stock tests
		below from ever triggering an automatic Material Request for an
		unrelated reason (a "Purchase" default would).

		`default_warehouse`, unless the caller already passed one: this
		site's own `Stock Settings.default_warehouse` is genuinely unset
		(confirmed live -- `test_cotizaciones_billing_review.py`'s own
		`_stock_settings_default_warehouse()` only ever sets it temporarily,
		for ITS OWN one test), so every item here needs its own explicit
		Item Default warehouse or `make_sales_order()`'s own native
        `validate_warehouse()` rejects the row outright -- exactly the same
		requirement `create_and_submit_sales_order()`'s own item fixtures
		already have to satisfy (fixtures.py's own `item()` docstring)."""
		if "default_warehouse" not in kwargs:
			kwargs["default_warehouse"] = self.world.warehouse(f"{item_code} WH").name
		item = self.world.item(item_code, default_material_request_type="Manufacture", **kwargs)
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": price_list,
				"selling": 1,
				"price_list_rate": rate,
			}
		)
		price.insert()
		self.world.track_existing("Item Price", price.name)
		return item

	def _new_quotation(self, item, qty=1, extra_row=None):
		items = [{"item_code": item.name, "qty": qty}]
		if extra_row:
			items.append(extra_row)
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(customer=self.customer.name, items=items)
		self.world.track_existing("Quotation", result["name"])
		return result["name"]

	def _send_to_billing(self, name):
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(name)

	def _approve(self, name):
		with fx.as_user(self.facturacion):
			cotizaciones.approve_quotation_billing(name)

	def _return(self, name, reason="Ajustar precio"):
		with fx.as_user(self.facturacion):
			cotizaciones.return_quotation_from_billing(name, reason=reason)

	def _approved_quotation(self, item, qty=1):
		name = self._new_quotation(item, qty=qty)
		self._send_to_billing(name)
		self._approve(name)
		return name

	def _send_to_pedidos(self, name, user=None):
		with fx.as_user(user or self.vendedora):
			return cotizaciones.create_sales_order_from_quotation(name)

	# -- A-E: eligibility --------------------------------------------------

	def test_a_aprobada_puede_generar_pedido(self):
		item = self._priced_item("FG2517-A-ITEM")
		name = self._approved_quotation(item)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		self.assertEqual(result["quotation"], name)
		self.assertFalse(result["already_exists"])
		self.assertTrue(frappe.db.exists("Sales Order", result["sales_order"]))

	def test_b_pendiente_no_puede(self):
		item = self._priced_item("FG2517-B-ITEM")
		name = self._new_quotation(item)
		self._send_to_billing(name)
		with self.assertRaises(frappe.ValidationError):
			self._send_to_pedidos(name)

	def test_c_devuelta_no_puede(self):
		item = self._priced_item("FG2517-C-ITEM")
		name = self._new_quotation(item)
		self._send_to_billing(name)
		self._return(name)
		with self.assertRaises(frappe.ValidationError):
			self._send_to_pedidos(name)

	def test_d_borrador_no_puede(self):
		item = self._priced_item("FG2517-D-ITEM")
		name = self._new_quotation(item)  # never sent to Facturación -- stays Borrador
		with self.assertRaises(frappe.ValidationError):
			self._send_to_pedidos(name)

	def test_e_cancelada_no_puede(self):
		item = self._priced_item("FG2517-E-ITEM")
		name = self._approved_quotation(item)
		frappe.get_doc("Quotation", name).cancel()
		with self.assertRaises(frappe.ValidationError):
			self._send_to_pedidos(name)

	# -- F/G: amendments -----------------------------------------------------

	def test_f_g_amendment_old_cannot_new_vigente_can(self):
		"""Mirrors COTIZACION-3 -> ... -> COTIZACION-3-5: a price-mode
		amendment cancels the original and creates a new one -- only the
		new, vigente, approved version may ever generate a pedido."""
		item = self._priced_item("FG2517-FG-ITEM")
		original_name = self._new_quotation(item)
		self._send_to_billing(original_name)
		with fx.as_user(self.facturacion):
			adjusted = cotizaciones.apply_quotation_price_mode(original_name, "DISCOUNT_10")
		new_name = adjusted["name"]
		self.world.track_existing("Quotation", new_name)
		self._approve(new_name)

		# F -- the old, now-cancelled (docstatus=2) original can never
		self.assertEqual(frappe.db.get_value("Quotation", original_name, "docstatus"), 2)
		with self.assertRaises(frappe.ValidationError):
			self._send_to_pedidos(original_name)

		# G -- the new, vigente, approved amendment can
		result = self._send_to_pedidos(new_name)
		self.world.track_existing("Sales Order", result["sales_order"])
		self.assertEqual(result["quotation"], new_name)

	# -- H-M: data preservation -----------------------------------------------

	def test_h_i_j_k_conserva_customer_items_qty_y_rate_aprobado(self):
		item = self._priced_item("FG2517-HIJK-ITEM", rate=300)
		name = self._approved_quotation(item, qty=4)
		qtn = frappe.get_doc("Quotation", name)
		approved_rate = qtn.items[0].rate

		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		so = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertEqual(so.customer, qtn.party_name)  # H
		self.assertEqual(len(so.items), 1)  # I
		self.assertEqual(so.items[0].item_code, item.name)
		self.assertEqual(so.items[0].qty, 4)  # J
		self.assertEqual(so.items[0].rate, approved_rate)  # K -- exact approved rate, not re-fetched

	def test_l_conserva_descuentos_sin_recomputarlos(self):
		"""A DISCOUNT_20 quotation's persisted rate is 80% of the reference
		-- the Sales Order must carry that EXACT number and discount
		percentage, never a fresh price-list lookup, never the discount
		re-applied a second time (which would compound to 64%)."""
		item = self._priced_item("FG2517-L-ITEM", rate=500)
		name = self._new_quotation(item)
		self._send_to_billing(name)
		with fx.as_user(self.facturacion):
			adjusted = cotizaciones.apply_quotation_price_mode(name, "DISCOUNT_20")
		amended_name = adjusted["name"]
		self.world.track_existing("Quotation", amended_name)
		self._approve(amended_name)

		qtn = frappe.get_doc("Quotation", amended_name)
		approved_rate = qtn.items[0].rate
		approved_discount = qtn.items[0].discount_percentage
		self.assertEqual(approved_rate, 400)  # 500 * 0.8, sanity check on the fixture itself

		result = self._send_to_pedidos(amended_name)
		self.world.track_existing("Sales Order", result["sales_order"])
		so = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertEqual(so.items[0].rate, approved_rate)
		self.assertEqual(so.items[0].discount_percentage, approved_discount)
		self.assertNotEqual(so.items[0].rate, 500 * 0.8 * 0.8)  # never compounded to 64%

	def test_m_conserva_taxes_mediante_mapper_nativo(self):
		"""No Company Sales Taxes and Charges Template is configured on
		this site (confirmed across every prior PDF/price-mode commit's own
		review) -- so both Quotation and the mapped Sales Order carry zero
		tax rows here, and the assertion that matters is that the mapper's
		native `Sales Taxes and Charges` table mapping ran at all (no
		exception, `taxes` is a real, present list, not a missing field)."""
		item = self._priced_item("FG2517-M-ITEM")
		name = self._approved_quotation(item)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		so = frappe.get_doc("Sales Order", result["sales_order"])

		qtn = frappe.get_doc("Quotation", name)
		self.assertEqual(len(so.get("taxes") or []), len(qtn.get("taxes") or []))
		self.assertEqual(so.total_taxes_and_charges, qtn.total_taxes_and_charges)

	# -- N-P: native link + idempotency ---------------------------------------

	def test_n_crea_relacion_quotation_sales_order(self):
		item = self._priced_item("FG2517-N-ITEM")
		name = self._approved_quotation(item)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		so = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertEqual(so.items[0].prevdoc_docname, name)  # native link, no Custom Field
		self.assertEqual(so.items[0].quotation_item, frappe.get_doc("Quotation", name).items[0].name)

	def test_o_p_doble_llamada_crea_solo_uno_y_retorna_el_mismo(self):
		item = self._priced_item("FG2517-OP-ITEM")
		name = self._approved_quotation(item)

		first = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", first["sales_order"])
		second = self._send_to_pedidos(name)  # simulates a double click / retry

		self.assertFalse(first["already_exists"])
		self.assertTrue(second["already_exists"])  # P
		self.assertEqual(first["sales_order"], second["sales_order"])  # P

		linked_sales_orders = frappe.get_all(
			"Sales Order Item",
			filters={"prevdoc_docname": name, "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(len(linked_sales_orders), 1)  # O -- exactly one Sales Order, never two

	# -- Q/R: Ventas / Bodega -------------------------------------------------

	def test_q_pedido_aparece_elegible_en_ventas(self):
		from fabergray_erp.api import ventas

		item = self._priced_item("FG2517-Q-ITEM")
		name = self._approved_quotation(item)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])

		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders(limit=500)
			detail = ventas.get_order_detail(result["sales_order"])

		order_row = next(o for o in orders if o["name"] == result["sales_order"])
		self.assertEqual(order_row["quotation"], name)
		self.assertEqual(detail["quotation"], name)

	def test_r_pedido_entra_al_flujo_existente_hacia_bodega(self):
		"""Same assertion shape as test_ventas_api.py's own
		test_e2e_zero_stock_creates_full_demand_pick_list_no_automatic_
		shortage() -- proves `.submit()` alone (inside create_sales_order_
		from_quotation()) reached the exact same Sales Order.on_submit ->
		Fulfillment Engine hook any other Sales Order in this app goes
		through, with zero special-casing."""
		wh = self.world.warehouse("FG2517 R Warehouse")
		item = self._priced_item("FG2517-R-ITEM", default_warehouse=wh.name)
		self.world.warehouse_user_permission(self.bodega_user, wh.name)

		name = self._approved_quotation(item, qty=5)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		self.world.track_existing_pick_lists_and_reports_for(result["sales_order"])

		pick_lists = frappe.get_all(
			"Pick List Item",
			filters={"sales_order": result["sales_order"], "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(len(pick_lists), 1)

		with fx.as_user(self.bodega_user):
			queue = bodega_api.get_queue()
		self.assertIn(pick_lists[0], [p["name"] for p in queue["pendientes"]])

	# -- S/T/U: no stock validation, no Material Request, no Bin write -------

	def test_s_t_u_no_requiere_stock_ni_crea_material_request_ni_toca_bin(self):
		wh = self.world.warehouse("FG2517 STU Warehouse")
		item = self._priced_item("FG2517-STU-ITEM", default_warehouse=wh.name)
		# Deliberately never call stock_up/stock_up_real -- zero real stock.

		bin_before = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh.name}, "actual_qty")
		mr_rows_before = frappe.db.count("Material Request Item", {"item_code": item.name})

		name = self._approved_quotation(item, qty=7)
		result = self._send_to_pedidos(name)  # must NOT raise for lack of stock
		self.world.track_existing("Sales Order", result["sales_order"])
		self.world.track_existing_pick_lists_and_reports_for(result["sales_order"])

		pick_lists = frappe.get_all(
			"Pick List Item",
			filters={"sales_order": result["sales_order"], "docstatus": ["!=", 2]},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(len(pick_lists), 1)  # S -- full-demand Pick List created despite zero stock
		rows = frappe.get_doc("Pick List", pick_lists[0]).get("locations")
		self.assertEqual(sum(row.stock_qty for row in rows), 7.0)

		mr_rows_after = frappe.db.count("Material Request Item", {"item_code": item.name})
		self.assertEqual(mr_rows_after, mr_rows_before)  # T -- no Material Request

		# U -- Bin.actual_qty stays zero -- ERPNext's own Pick List/Sales
		# Order machinery may auto-vivify a Bin row the first time a
		# warehouse+item combination is referenced (standard framework
		# behavior, not something this endpoint does deliberately), so
		# `None -> 0.0` here is expected and fine; what must NEVER happen
		# is a real quantity appearing where there was none.
		bin_after = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh.name}, "actual_qty")
		self.assertEqual(flt(bin_before), flt(bin_after))
		self.assertEqual(flt(bin_after), 0.0)

	# -- V: security -----------------------------------------------------------

	def test_v_endpoint_accepts_only_quotation_name_never_items_or_rates(self):
		"""There is no `items`/`customer`/`rate`/`discount` parameter on
		create_sales_order_from_quotation() for a caller to even attempt to
		pass -- confirmed here by calling it with an extra kwarg the way a
		crafted request would, and by re-reading the persisted Sales Order
		rate afterward: it can only ever be what the Quotation itself
		already carried."""
		item = self._priced_item("FG2517-V-ITEM", rate=999)
		name = self._approved_quotation(item)
		approved_rate = frappe.get_doc("Quotation", name).items[0].rate

		with self.assertRaises(TypeError):
			with fx.as_user(self.vendedora):
				cotizaciones.create_sales_order_from_quotation(
					quotation_name=name, rate=1, items=[{"item_code": item.name, "qty": 999}]
				)

		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])
		so = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(so.items[0].rate, approved_rate)
		self.assertEqual(so.items[0].qty, 1)

	def test_v2_only_sales_order_create_permission_required_no_ignore_permissions(self):
		"""No new role/permission concept was introduced -- the same Sales
		Order Custom DocPerm grant create_and_submit_sales_order() already
		relies on (api/ventas.py) is what this endpoint checks too. A
		disposable role with zero Sales Order grant is denied outright."""
		role = frappe.get_doc({"doctype": "Role", "role_name": "FG2517 No Sales Order Role", "desk_access": 1})
		role.insert()
		self.world.track_existing("Role", role.name)
		no_role_user = self.world.user("fg2517-norole@example.com", [role.name])

		item = self._priced_item("FG2517-V2-ITEM")
		name = self._approved_quotation(item)
		with self.assertRaises(frappe.PermissionError):
			self._send_to_pedidos(name, user=no_role_user)

	# -- Z: PDF regression -------------------------------------------------

	def test_z_pdf_sigue_funcionando_despues_de_generar_el_pedido(self):
		"""This commit touches classification/pedido creation only --
		_assert_quotation_pdf_eligible() reads fg_billing_review_status/
		docstatus directly from the Quotation, completely independent of
		whether a Sales Order now exists for it (section 13's own explicit
		"fg_billing_review_status = Aprobada nunca cambia")."""
		item = self._priced_item("FG2517-Z-ITEM")
		name = self._approved_quotation(item)
		result = self._send_to_pedidos(name)
		self.world.track_existing("Sales Order", result["sales_order"])

		with fx.as_user(self.facturacion):
			url = cotizaciones.get_fabrigray_quotation_pdf_view_url(name)
			self.assertIn(name, url)

			from frappe.utils import print_format as print_format_module

			original = print_format_module.download_pdf
			print_format_module.download_pdf = lambda **kwargs: None
			try:
				cotizaciones.download_fabrigray_quotation_pdf(name)
			finally:
				print_format_module.download_pdf = original
			self.assertEqual(frappe.local.response.filename, f"Cotizacion-Fabrigray-{name}.pdf")

		self.assertEqual(frappe.get_doc("Quotation", name).get("fg_billing_review_status"), "Aprobada")


class TestPedidoUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		with open(_COTIZACIONES_JS_PATH, encoding="utf-8") as f:
			cls.js = f.read()

	# -- W: button appears only for Aprobada without an existing pedido ------

	def test_w_boton_solo_para_aprobada_sin_pedido(self):
		m = re.search(r"const aprobada_vigente = [^\n]+", self.js)
		self.assertIsNotNone(m)
		self.assertIn('billing_status === "Aprobada"', m.group())
		self.assertIn("q.docstatus !== 2", m.group())

		pedido_block = re.search(r"const pedido_btn = aprobada_vigente[\s\S]*?fg-quotation-card-cta[\s\S]*?: \"\";", self.js)
		self.assertIsNotNone(pedido_block)
		self.assertIn("q.sales_order", pedido_block.group())
		self.assertIn("ENVIAR A PEDIDOS", pedido_block.group())

	# -- X: after generating, ENVIAR A PEDIDOS disappears ---------------------

	def test_x_despues_de_generar_desaparece_enviar_a_pedidos(self):
		pedido_block = re.search(r"const pedido_btn = aprobada_vigente[\s\S]*?fg-quotation-card-cta[\s\S]*?: \"\";", self.js)
		self.assertIsNotNone(pedido_block)
		body = pedido_block.group()
		# The branch that renders "ENVIAR A PEDIDOS" is reached only via
		# the `: (...)` (falsy q.sales_order) side of `q.sales_order ? ... : ...`
		# -- confirmed by requiring "ENVIAR A PEDIDOS" to appear AFTER the
		# `q.sales_order` ternary check in source order.
		self.assertLess(body.index("q.sales_order"), body.index("ENVIAR A PEDIDOS"))

	# -- Y: VER PEDIDO appears once a Sales Order exists ----------------------

	def test_y_aparece_ver_pedido(self):
		pedido_block = re.search(r"const pedido_btn = aprobada_vigente[\s\S]*?fg-quotation-card-cta[\s\S]*?: \"\";", self.js)
		self.assertIsNotNone(pedido_block)
		self.assertIn("VER PEDIDO", pedido_block.group())
		self.assertIn("fg-quotation-card-view-pedido", pedido_block.group())

	def test_endpoint_is_wired_to_the_real_backend_method(self):
		self.assertIn("create_sales_order_from_quotation", self.js)

	def test_confirm_dialog_never_mentions_a_money_total(self):
		"""Commit 25.17 review decision: the confirmation dialog shows
		Cliente/Referencias/Unidades, deliberately NEVER a Total/price --
		this whole module's own standing economic-data policy applies to
		this screen too."""
		m = re.search(r"confirm_send_to_pedidos\(name\) \{[\s\S]*?\n\t\}\n", self.js)
		self.assertIsNotNone(m)
		body = m.group()
		self.assertNotIn("Total", body)
		self.assertNotIn("grand_total", body)
		# q.total_qty (a unit count, not money) is legitimately shown --
		# only a bare `q.total` (the economic field) would be disallowed.
		self.assertIsNone(re.search(r"q\.total(?!_qty)", body))
