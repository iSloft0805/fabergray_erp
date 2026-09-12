# -*- coding: utf-8 -*-
"""Commit 25.22 -- Customer classification by "Empresa / Tipo de
facturación" (`Customer.fg_customer_company_type`, Custom Field --
`CUSTOMER_COMPANY_TYPES` in api/clientes.py is the one closed set it may
ever hold: IVA / integrandoMAS / ecoluminar / fabrigraySAS / amore).

Covers every scenario in the approved brief's own "13. Tests obligatorios"
list (A-P):

  A-E  a new Customer accepts each of the 5 valid values
  F    an invalid value is rejected (create AND update)
  G    a new Customer requires a classification (create_customer() only
       -- never the Custom Field's own `reqd`, see api/clientes.py's own
       module docstring for why)
  H    a historical Customer with no value stays fully legible
  I    editing persists a classification change
  J/L  Facturación (Pick List/Sales Order path) resolves the real
       classification, live, from Customer
  K    Cotización en Facturación resolves it too, same way
  M    an unclassified Customer surfaces as `None` ("Sin clasificar" in
       the UI), never a guessed value
  N    Facturación has no write access to Customer through this or any
       other flow
  O    Gestión de Clientes' own permission boundary (can; Vendedora/
       Facturación cannot)
  P    Sales Order/Quotation carry no snapshot field of their own --
       dynamic resolution only

Plus a static UI-contract class (facturacion.js/clientes.js read as text,
same technique test_cotizaciones_price_mode.py's own
TestPriceModeUiContract already established -- no JS test runner in this
app).

Section 11's OPTIONAL recommendation (blocking invoice generation for an
unclassified Customer) is deliberately NOT implemented in this commit --
audited and reported instead, per the brief's own explicit "no
implementar el bloqueo sin auditar primero... reportar si puede hacerse
de forma segura" -- so there is nothing to test for it here."""

import os

import frappe
from frappe.tests import IntegrationTestCase

from fabergray_erp.api import bodega
from fabergray_erp.api import clientes as clientes_api
from fabergray_erp.api import cotizaciones, facturacion
from fabergray_erp.tests import fixtures as fx

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_CLIENTES_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "clientes", "clientes.js"
)
_FACTURACION_JS_PATH = os.path.join(
	frappe.get_app_path("fabergray_erp"), "fabrigray_erp", "page", "facturacion", "facturacion.js"
)


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


# ---------------------------------------------------------------------------
# A-G -- creation
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeCreate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.gestion_user = cls.world.user("fg2522-create-gestion@example.com", ["Gestión de Clientes"])

	def _create(self, **fields):
		with fx.as_user(self.gestion_user):
			result = clientes_api.create_customer(fields)
		self.world.track_existing("Customer", result["name"])
		return result

	def test_a_accepts_iva(self):
		result = self._create(customer_name="FG2522 Create IVA", fg_customer_company_type="IVA")
		self.assertEqual(frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "IVA")

	def test_b_accepts_integrandomas(self):
		result = self._create(
			customer_name="FG2522 Create IntegrandoMAS", fg_customer_company_type="integrandoMAS"
		)
		self.assertEqual(
			frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "integrandoMAS"
		)

	def test_c_accepts_ecoluminar(self):
		result = self._create(customer_name="FG2522 Create Ecoluminar", fg_customer_company_type="ecoluminar")
		self.assertEqual(
			frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "ecoluminar"
		)

	def test_d_accepts_fabrigraysas(self):
		result = self._create(
			customer_name="FG2522 Create FabrigraySAS", fg_customer_company_type="fabrigraySAS"
		)
		self.assertEqual(
			frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "fabrigraySAS"
		)

	def test_e_accepts_amore(self):
		result = self._create(customer_name="FG2522 Create Amore", fg_customer_company_type="amore")
		self.assertEqual(frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "amore")

	# F. valor inválido rechazado
	def test_f_invalid_value_rejected(self):
		with fx.as_user(self.gestion_user):
			with self.assertRaises(frappe.ValidationError):
				clientes_api.create_customer(
					{"customer_name": "FG2522 Create Invalido", "fg_customer_company_type": "NOEXISTE"}
				)
		self.assertFalse(frappe.db.exists("Customer", "FG2522 Create Invalido"))

	# G. nuevo cliente requiere clasificación
	def test_g_missing_value_rejected_for_a_new_customer(self):
		with fx.as_user(self.gestion_user):
			with self.assertRaises(frappe.ValidationError):
				clientes_api.create_customer({"customer_name": "FG2522 Create Sin Clasificar"})
		self.assertFalse(frappe.db.exists("Customer", "FG2522 Create Sin Clasificar"))

	def test_empty_string_is_also_rejected_as_missing(self):
		with fx.as_user(self.gestion_user):
			with self.assertRaises(frappe.ValidationError):
				clientes_api.create_customer(
					{"customer_name": "FG2522 Create Vacio", "fg_customer_company_type": ""}
				)
		self.assertFalse(frappe.db.exists("Customer", "FG2522 Create Vacio"))

	def test_customer_type_stays_independent_of_this_feature(self):
		"""Section 15/19 -- this is a NEW, separate classification, never a
		replacement for the native `customer_type` field."""
		result = self._create(customer_name="FG2522 Create Independiente", fg_customer_company_type="IVA")
		doc = frappe.get_doc("Customer", result["name"])
		self.assertEqual(doc.customer_type, "Company")
		self.assertEqual(doc.fg_customer_company_type, "IVA")


# ---------------------------------------------------------------------------
# H/I -- historical Customers + edit
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeHistoricalAndEdit(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.gestion_user = cls.world.user("fg2522-edit-gestion@example.com", ["Gestión de Clientes"])

	# H. cliente histórico vacío sigue siendo legible
	def test_h_historical_customer_without_value_is_readable(self):
		"""`fx.TestWorld.customer()` -- like every one of the ~4091 real
		migrated Customers -- never goes through create_customer(), so it
		carries no classification at all, exactly the historical case."""
		c = self.world.customer("FG2522 Historico Sin Clasificar")
		with fx.as_user(self.gestion_user):
			detail = clientes_api.get_customer_detail(c.name)
		self.assertIsNone(detail["fg_customer_company_type"])
		self.assertEqual(detail["customer_name"], "FG2522 Historico Sin Clasificar")

	# I. edición persiste cambio
	def test_i_update_persists_change(self):
		c = self.world.customer("FG2522 Editar Clasificacion")
		with fx.as_user(self.gestion_user):
			clientes_api.update_customer(c.name, customer={"fg_customer_company_type": "integrandoMAS"})
		self.assertEqual(
			frappe.db.get_value("Customer", c.name, "fg_customer_company_type"), "integrandoMAS"
		)

		# Section 5 -- "permitir cambiarlo entre las 5 opciones".
		with fx.as_user(self.gestion_user):
			clientes_api.update_customer(c.name, customer={"fg_customer_company_type": "amore"})
		self.assertEqual(frappe.db.get_value("Customer", c.name, "fg_customer_company_type"), "amore")

	def test_update_rejects_invalid_value_and_leaves_it_unchanged(self):
		c = self.world.customer("FG2522 Editar Invalido")
		c.fg_customer_company_type = "IVA"
		c.save()
		with fx.as_user(self.gestion_user):
			with self.assertRaises(frappe.ValidationError):
				clientes_api.update_customer(c.name, customer={"fg_customer_company_type": "NOEXISTE"})
		self.assertEqual(frappe.db.get_value("Customer", c.name, "fg_customer_company_type"), "IVA")

	def test_update_without_the_key_leaves_classification_unchanged(self):
		c = self.world.customer("FG2522 Editar Omitido")
		c.fg_customer_company_type = "ecoluminar"
		c.save()
		with fx.as_user(self.gestion_user):
			clientes_api.update_customer(c.name, customer={"tax_id": "900222999-1"})
		self.assertEqual(frappe.db.get_value("Customer", c.name, "fg_customer_company_type"), "ecoluminar")
		self.assertEqual(frappe.db.get_value("Customer", c.name, "tax_id"), "900222999-1")

	def test_editing_an_unrelated_field_never_forces_classification(self):
		"""Section 6 -- editing a historical, unclassified Customer for
		anything else (tax_id here) must never be blocked by this feature,
		and must never invent a classification as a side effect."""
		c = self.world.customer("FG2522 Editar Sin Forzar")
		with fx.as_user(self.gestion_user):
			clientes_api.update_customer(c.name, customer={"tax_id": "900222998-2"})
		self.assertEqual(frappe.db.get_value("Customer", c.name, "tax_id"), "900222998-2")
		# The raw DB value for a never-set Select is "" (Frappe's own
		# default), not NULL -- get_customer_detail()/every Facturación
		# read normalizes that to None via `or None` (see
		# api/clientes.py's own get_customer_detail()), so this checks the
		# falsy-ness that normalization relies on, not a literal None.
		self.assertFalse(frappe.db.get_value("Customer", c.name, "fg_customer_company_type"))


# ---------------------------------------------------------------------------
# J/L/M/N -- Facturación (Pick List / Sales Order path)
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeFacturacion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.wh = cls.world.warehouse("FG2522 Fact WH")
		cls.item = cls.world.item("FG2522-FACT-ITEM")
		cls.world.stock_up_real(cls.item.name, cls.wh.name, 100, rate=50)

		cls.customer_classified = cls.world.customer("FG2522 Fact Classified")
		cls.customer_classified.fg_customer_company_type = "fabrigraySAS"
		cls.customer_classified.save()

		cls.customer_unclassified = cls.world.customer("FG2522 Fact Unclassified")

		cls.bodega_user = cls.world.user("fg2522-fact-bodega@example.com", ["Bodega"])
		cls.world.warehouse_user_permission(cls.bodega_user, cls.wh.name)
		cls.facturacion_user = cls.world.user("fg2522-fact-facturacion@example.com", ["Facturación"])

	def _submitted_pick_list(self, customer, qty=3):
		so = self.world.submitted_sales_order(self.item.name, self.wh.name, qty, customer.name, rate=100)
		pl = self.world.pick_list_for(so, self.wh.name)
		with fx.as_user(self.bodega_user):
			bodega.start_picking(pl.name)
			for row in bodega.get_pick_list(pl.name)["rows"]:
				bodega.set_picked_qty(pl.name, row["row_name"], row["qty_solicitada"])
			bodega.finish_picking(pl.name)
		return frappe.get_doc("Pick List", pl.name)

	# J. Facturación devuelve clasificación correcta (tray + modal)
	def test_j_invoicing_queue_and_detail_return_classification(self):
		pl = self._submitted_pick_list(self.customer_classified)
		with fx.as_user(self.facturacion_user):
			queue = facturacion.get_invoicing_queue()
			detail = facturacion.get_invoicing_detail(pl.name)

		row = next(r for r in queue["pick_lists"] if r["name"] == pl.name)
		self.assertEqual(row["fg_customer_company_type"], "fabrigraySAS")
		self.assertEqual(detail["fg_customer_company_type"], "fabrigraySAS")

	# L. Sales Order en Facturación muestra clasificación -- Facturación's
	# own Pick List/Sales Order pair is the only place a Sales Order
	# surfaces in this Page; there is no separate Sales-Order-only view.
	def test_l_sales_order_linked_pick_list_shows_classification(self):
		pl = self._submitted_pick_list(self.customer_classified)
		with fx.as_user(self.facturacion_user):
			detail = facturacion.get_invoicing_detail(pl.name)
		self.assertIsNotNone(detail["sales_order"])
		self.assertEqual(detail["fg_customer_company_type"], "fabrigraySAS")

	# M. cliente sin clasificación -- None server-side ("Sin clasificar" +
	# advertencia en la UI), nunca un valor inventado.
	def test_m_unclassified_customer_returns_none_never_a_guess(self):
		pl = self._submitted_pick_list(self.customer_unclassified)
		with fx.as_user(self.facturacion_user):
			queue = facturacion.get_invoicing_queue()
			detail = facturacion.get_invoicing_detail(pl.name)

		row = next(r for r in queue["pick_lists"] if r["name"] == pl.name)
		self.assertIsNone(row["fg_customer_company_type"])
		self.assertIsNone(detail["fg_customer_company_type"])

	# N. Facturación no puede editar Customer mediante este flujo, ni
	# ningún otro -- su Custom DocPerm es read=1/write=0 (fixtures/
	# custom_docperm.json, sin cambios en este commit).
	def test_n_facturacion_has_no_write_permission_on_customer(self):
		with fx.as_user(self.facturacion_user):
			self.assertFalse(frappe.has_permission("Customer", "write"))
			with self.assertRaises(frappe.PermissionError):
				doc = frappe.get_doc("Customer", self.customer_classified.name)
				doc.fg_customer_company_type = "amore"
				doc.save()

		self.assertEqual(
			frappe.db.get_value("Customer", self.customer_classified.name, "fg_customer_company_type"),
			"fabrigraySAS",
		)

	def test_reading_the_facturacion_queue_never_writes_to_customer(self):
		pl = self._submitted_pick_list(self.customer_classified)
		before = frappe.db.get_value("Customer", self.customer_classified.name, "modified")
		with fx.as_user(self.facturacion_user):
			facturacion.get_invoicing_queue()
			facturacion.get_invoicing_detail(pl.name)
		after = frappe.db.get_value("Customer", self.customer_classified.name, "modified")
		self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# K -- Cotizaciones en Facturación
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeCotizacionesEnFacturacion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

		cls.item = cls.world.item("FG2522-QTN-ITEM")
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": cls.item.name,
				"price_list": "Standard Selling",
				"selling": 1,
				"price_list_rate": 100,
			}
		)
		price.insert()
		cls.world.track_existing("Item Price", price.name)

		cls.customer = cls.world.customer("FG2522 QTN Classified")
		cls.customer.fg_customer_company_type = "ecoluminar"
		cls.customer.save()

		cls.customer_unclassified = cls.world.customer("FG2522 QTN Unclassified")

		cls.vendedora = cls.world.user("fg2522-qtn-vendedora@example.com", ["Vendedora"])
		cls.facturacion_user = cls.world.user("fg2522-qtn-facturacion@example.com", ["Facturación"])

	def _pending_quotation(self, customer):
		with fx.as_user(self.vendedora):
			result = cotizaciones.create_and_submit_quotation(
				customer=customer.name, items=[{"item_code": self.item.name, "qty": 1}]
			)
		self.world.track_existing("Quotation", result["name"])
		with fx.as_user(self.vendedora):
			cotizaciones.send_quotation_to_billing(result["name"])
		return result["name"]

	# K. Cotización en Facturación muestra clasificación (tray + modal)
	def test_k_quotation_billing_tray_and_detail_show_classification(self):
		name = self._pending_quotation(self.customer)
		with fx.as_user(self.facturacion_user):
			pending = cotizaciones.get_pending_billing_review_quotations()
			detail = cotizaciones.get_quotation_billing_detail(name)

		row = next(q for q in pending if q["name"] == name)
		self.assertEqual(row["fg_customer_company_type"], "ecoluminar")
		self.assertEqual(detail["fg_customer_company_type"], "ecoluminar")

	def test_unclassified_quotation_customer_returns_none(self):
		name = self._pending_quotation(self.customer_unclassified)
		with fx.as_user(self.facturacion_user):
			detail = cotizaciones.get_quotation_billing_detail(name)
		self.assertIsNone(detail["fg_customer_company_type"])

	def test_billing_review_never_writes_to_customer(self):
		name = self._pending_quotation(self.customer)
		before = frappe.db.get_value("Customer", self.customer.name, "modified")
		with fx.as_user(self.facturacion_user):
			cotizaciones.get_pending_billing_review_quotations()
			cotizaciones.get_quotation_billing_detail(name)
		after = frappe.db.get_value("Customer", self.customer.name, "modified")
		self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# P -- no duplication onto Sales Order/Quotation
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeNoDuplication(IntegrationTestCase):
	# P. no se duplican datos en Sales Order/Quotation sin necesidad --
	# section 12's own explicit "preferir fuente dinámica": neither
	# doctype carries a Custom Field of its own for this, Facturación
	# resolves it fresh from Customer every time (see the two test classes
	# above, which never set anything on Sales Order/Quotation directly).
	def test_p_sales_order_and_quotation_carry_no_snapshot_field(self):
		self.assertFalse(frappe.get_meta("Sales Order").has_field("fg_customer_company_type"))
		self.assertFalse(frappe.get_meta("Quotation").has_field("fg_customer_company_type"))
		self.assertFalse(frappe.get_meta("Pick List").has_field("fg_customer_company_type"))

	def test_customer_itself_is_the_only_doctype_carrying_the_field(self):
		self.assertTrue(frappe.get_meta("Customer").has_field("fg_customer_company_type"))


# ---------------------------------------------------------------------------
# O -- permisos Gestión de Clientes
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypePermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)
		cls.gestion_user = cls.world.user("fg2522-perm-gestion@example.com", ["Gestión de Clientes"])
		cls.vendedora = cls.world.user("fg2522-perm-vendedora@example.com", ["Vendedora"])
		cls.facturacion_user = cls.world.user("fg2522-perm-facturacion@example.com", ["Facturación"])
		cls.bodega_user = cls.world.user("fg2522-perm-bodega@example.com", ["Bodega"])

	def test_o_gestion_de_clientes_can_create_and_edit_classification(self):
		with fx.as_user(self.gestion_user):
			result = clientes_api.create_customer(
				{"customer_name": "FG2522 Perm OK", "fg_customer_company_type": "IVA"}
			)
		self.world.track_existing("Customer", result["name"])

		with fx.as_user(self.gestion_user):
			clientes_api.update_customer(result["name"], customer={"fg_customer_company_type": "amore"})
		self.assertEqual(frappe.db.get_value("Customer", result["name"], "fg_customer_company_type"), "amore")

	def test_o_vendedora_and_facturacion_cannot_create_customer(self):
		"""Vendedora already has Customer READ (unchanged by this commit --
		section 15's own "auditar si ya puede leer Customer" -- confirmed
		against fixtures/custom_docperm.json: read=1/write=0, same as
		Facturación) but neither role gained create/write through this
		feature."""
		for user in (self.vendedora, self.facturacion_user, self.bodega_user):
			with fx.as_user(user):
				with self.assertRaises(frappe.PermissionError):
					clientes_api.create_customer(
						{"customer_name": "FG2522 Perm Blocked", "fg_customer_company_type": "IVA"}
					)
		self.assertFalse(frappe.db.exists("Customer", "FG2522 Perm Blocked"))

	def test_vendedora_can_still_read_the_classification_she_could_already_read_customer(self):
		c = self.world.customer("FG2522 Perm Vendedora Lee")
		c.fg_customer_company_type = "amore"
		c.save()
		with fx.as_user(self.vendedora):
			self.assertTrue(frappe.has_permission("Customer", "read"))
			doc = frappe.get_doc("Customer", c.name)
			doc.check_permission("read")
			self.assertEqual(doc.fg_customer_company_type, "amore")


# ---------------------------------------------------------------------------
# Static UI contract -- facturacion.js / clientes.js read as text.
# ---------------------------------------------------------------------------
class TestCustomerCompanyTypeUiContract(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.clientes_js = _read(_CLIENTES_JS_PATH)
		cls.facturacion_js = _read(_FACTURACION_JS_PATH)

	def test_clientes_js_mirrors_the_closed_set(self):
		self.assertIn(
			'const CUSTOMER_COMPANY_TYPES = ["IVA", "integrandoMAS", "ecoluminar", "fabrigraySAS", "amore"];',
			self.clientes_js,
		)

	def test_clientes_js_field_is_mandatory_only_on_create(self):
		self.assertIn('fieldname: "fg_customer_company_type"', self.clientes_js)
		self.assertIn("reqd: is_edit ? 0 : 1", self.clientes_js)

	def test_clientes_js_never_sends_a_blank_classification(self):
		self.assertIn("if (values.fg_customer_company_type) {", self.clientes_js)

	def test_clientes_js_shows_missing_classification_explicitly(self):
		self.assertIn("Sin clasificar", self.clientes_js)

	def test_clientes_js_renders_a_badge_in_list_and_detail(self):
		self.assertEqual(self.clientes_js.count("render_company_type_badge("), 3)  # def + card + detail

	def test_facturacion_js_renders_the_badge_in_every_required_place(self):
		# def + both queue cards + both review dialogs = 5.
		self.assertEqual(self.facturacion_js.count("render_company_type_badge("), 5)

	def test_facturacion_js_shows_missing_classification_with_a_warning(self):
		self.assertIn("Sin clasificar", self.facturacion_js)
		self.assertIn(
			"Este cliente no tiene Empresa / Tipo de facturación definido. "
			"Actualízalo en Gestión de Clientes antes de facturar.",
			self.facturacion_js,
		)

	def test_facturacion_js_never_calls_a_customer_write_endpoint(self):
		"""Section 10/12 -- Facturación only ever READS the classification
		(get_invoicing_queue/get_invoicing_detail via `this.call(...)`,
		get_pending_billing_review_quotations/get_quotation_billing_detail
		via `this.call_cotizaciones(...)`) -- it never calls
		update_customer()/create_customer()/set_customer_disabled()."""
		for forbidden in ("update_customer", "create_customer", "set_customer_disabled"):
			self.assertNotIn(forbidden, self.facturacion_js)
