# -*- coding: utf-8 -*-
"""Commit 25.10 -- Ventas: cancelled Sales Orders no longer stay mixed into
the main "Pedidos" list.

Real bug this fixes: `get_my_orders()` used to return every Sales Order
regardless of `docstatus`, so a cancelled order kept showing up in the
default dashboard list right alongside active ones -- the Page's own
"Cancelados" KPI chip already existed as a client-side filter over that
same mixed list, but nothing server-side ever actually separated the two.

Business rule under test throughout: cancellation NEVER deletes anything
(native `docstatus=2`, no soft-delete flag of this app's own invention);
"active" means `docstatus != 2`; "cancelled" means `docstatus == 2`; an
order superseded by a later amendment (Commit 18.5's own cancel+amend
"modify" flow) is not a real, asesora-facing cancellation and stays out of
BOTH buckets except as its still-live successor -- see
`get_my_orders()`'s own docstring for the full reasoning, unchanged by
this commit.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from fabergray_erp.api import ventas
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class TestVentasCancelledOrders(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2510 Cancelled Orders")
		cls.item = cls.world.item("FG2510-CANCELLED-ITEM", default_warehouse=cls.wh.name)
		cls.customer = cls.world.customer("FG2510 Cancelled Orders Customer")
		cls.world.stock_up(cls.item.name, cls.wh.name, 1000)

		cls.vendedora = cls.world.user("fg2510-vendedora@example.com", ["Vendedora"])
		cls.no_role_user = cls.world.user("fg2510-norole@example.com", [])

	def _active_order(self):
		with fx.as_user(self.vendedora):
			result = ventas.create_and_submit_sales_order(
				customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Sales Order", result["name"])
		self.world.track_existing_pick_lists_and_reports_for(result["name"])
		return result["name"]

	def _cancelled_order(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name)
		return name

	# K. listado normal excluye docstatus=2
	def test_k_default_view_excludes_cancelled(self):
		cancelled_name = self._cancelled_order()
		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders()
		self.assertNotIn(cancelled_name, [o["name"] for o in orders])

	def test_k_explicit_active_view_excludes_cancelled(self):
		cancelled_name = self._cancelled_order()
		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders(view="active")
		self.assertNotIn(cancelled_name, [o["name"] for o in orders])

	# L. vista cancelados solo devuelve docstatus=2
	def test_l_cancelled_view_returns_only_cancelled(self):
		active_name = self._active_order()
		cancelled_name = self._cancelled_order()
		with fx.as_user(self.vendedora):
			cancelled_orders = ventas.get_my_orders(view="cancelled")
		names = [o["name"] for o in cancelled_orders]
		self.assertIn(cancelled_name, names)
		self.assertNotIn(active_name, names)
		for name in names:
			self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)

	# Commit 25.10.1 -- strict contract: an unrecognized `view` value raises
	# outright, it is never silently coerced to "active" (the brief's own
	# explicit preference, section 6: "contrato estricto sobre fallback
	# silencioso" for a brand-new parameter this app fully controls the one
	# caller of).
	def test_unrecognized_view_value_raises_validation_error(self):
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				ventas.get_my_orders(view="literally-anything-else")

	def test_default_view_is_still_active_when_omitted(self):
		"""The strict check only rejects an explicitly-wrong value -- the
		parameter itself still defaults to "active" when the caller omits
		it entirely (every existing caller of get_my_orders() before this
		commit never passed `view` at all)."""
		active_name = self._active_order()
		cancelled_name = self._cancelled_order()
		with fx.as_user(self.vendedora):
			orders = ventas.get_my_orders()
		names = [o["name"] for o in orders]
		self.assertIn(active_name, names)
		self.assertNotIn(cancelled_name, names)

	# M. cancelado no se borra físicamente
	def test_m_cancelled_order_is_never_physically_deleted(self):
		name = self._cancelled_order()
		self.assertTrue(frappe.db.exists("Sales Order", name))
		doc = frappe.get_doc("Sales Order", name)
		self.assertEqual(doc.docstatus, 2)

	# N. cancelado desaparece de activos tras cancel_order()
	def test_n_order_disappears_from_active_after_cancel(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			before = [o["name"] for o in ventas.get_my_orders(view="active")]
		self.assertIn(name, before)

		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name)
			after = [o["name"] for o in ventas.get_my_orders(view="active")]
		self.assertNotIn(name, after)

	# O. cancelado aparece en Cancelados
	def test_o_order_appears_in_cancelled_after_cancel(self):
		name = self._active_order()
		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name)
			cancelled = [o["name"] for o in ventas.get_my_orders(view="cancelled")]
		self.assertIn(name, cancelled)

	# P. permisos siguen aplicando
	def test_p_unauthorized_user_cannot_call_either_view(self):
		with fx.as_user(self.no_role_user):
			with self.assertRaises(frappe.PermissionError):
				ventas.get_my_orders(view="active")
			with self.assertRaises(frappe.PermissionError):
				ventas.get_my_orders(view="cancelled")

	# Q. Company isolation sigue aplicando (incluso en la vista cancelados)
	def test_q_company_isolation_applies_to_cancelled_view_too(self):
		"""Same "_Test Company" pattern test_ventas_permissions.py's own
		test_vendedora_cannot_see_sales_order_from_another_company()
		already establishes -- built directly (not through the Vendedora
		create path), submitted, then cancelled, all as Administrator; a
		Vendedora scoped to `fabrigraysas` must never see it in EITHER
		view, this commit's own `docstatus` filter is additive to
		permission_query_conditions, never a replacement for it."""
		other_customer = self.world.customer("FG2510 Other Company Customer")
		other_item = self.world.item("FG2510-OTHER-COMPANY-ITEM")
		other_so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": other_customer.name,
				"company": "_Test Company",
				"currency": "INR",
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 7),
				"items": [
					{
						"item_code": other_item.name,
						"warehouse": "Finished Goods - _TC",
						"qty": 1,
						"rate": 100,
						"delivery_date": add_days(nowdate(), 7),
					}
				],
			}
		)
		other_so.insert()
		self.world.track_existing("Sales Order", other_so.name)
		other_so.submit()
		other_so.cancel()

		with fx.as_user(self.vendedora):
			cancelled = [o["name"] for o in ventas.get_my_orders(view="cancelled")]
		self.assertNotIn(other_so.name, cancelled)

	# R. cancelado no tiene acciones editar/confirmar (server-side re-check:
	# ninguno de los endpoints de escritura acepta un pedido cancelado)
	def test_r_cancelled_order_rejects_edit_and_modify_attempts(self):
		name = self._cancelled_order()
		with fx.as_user(self.vendedora):
			with self.assertRaises(frappe.ValidationError):
				ventas.update_draft_sales_order(
					name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
				)
			with self.assertRaises(frappe.ValidationError):
				ventas.modify_submitted_sales_order(
					name=name, customer=self.customer.name, items=[{"item_code": self.item.name, "qty": 1}]
				)
			with self.assertRaises(Exception):
				ventas.cancel_sales_order(name)  # already cancelled -- cannot cancel again

	# S. contadores activos excluyen cancelados si existen contadores
	def test_s_pedidos_hoy_counter_excludes_orders_cancelled_today(self):
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()["pedidos_hoy"]

		name = self._active_order()  # transaction_date defaults to today
		with fx.as_user(self.vendedora):
			after_active = ventas.get_sales_summary()["pedidos_hoy"]
		self.assertEqual(after_active, before + 1)

		with fx.as_user(self.vendedora):
			ventas.cancel_sales_order(name)
			after_cancel = ventas.get_sales_summary()["pedidos_hoy"]
		self.assertEqual(after_cancel, before)

	def test_s_cancelados_counter_still_reflects_cancelled_orders(self):
		with fx.as_user(self.vendedora):
			before = ventas.get_sales_summary()["cancelados"]

		self._cancelled_order()
		with fx.as_user(self.vendedora):
			after = ventas.get_sales_summary()["cancelados"]
		self.assertEqual(after, before + 1)
