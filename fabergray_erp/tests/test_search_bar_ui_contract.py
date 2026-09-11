# -*- coding: utf-8 -*-
"""Commit 25.20 -- static contract tests for the unified "Buscar por
cliente o fecha..." search bar's CLIENT side, across all 6 operational
modules (Bodega, Ventas, Cotizaciones, Recorrido, Facturación, Jefe de
Bodega/Centro de Faltantes).

Same convention as test_ventas_confirm_button_contract.py/
test_quick_order_ui_contract.py: this app has no JS test runner, so these
read the real source files as text and assert on them -- nothing here
executes JavaScript. The actual matching/parsing ALGORITHM is exercised
for real in test_search_utils.py (Python twin) and this commit's own
test_operational_search.py (backend endpoints, server-side modules);
this file only pins that:

- section 7's shared-helper rule (fg_search.js is the ONE place the
  algorithm lives, loaded globally, referenced -- not reimplemented --
  by every client-side-filtering page);
- section 8's client-vs-server audit decision is wired consistently: a
  client-side page calls `fabergray_erp.search.matches_operational_search`,
  a server-side page sends a `txt` param to its own endpoint instead;
- section 10 (clear button restores the current filter, never just the
  search) and section 11 (a real "no results" empty state, never a blank
  screen) markup exists on every page that owns a NEW-this-commit search
  bar, or that its own pre-existing equivalent (Bodega, whose existing
  empty-state copy was deliberately left untouched -- see api/bodega.py
  and bodega.js's own commit history) still stands.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

_APP_PATH = frappe.get_app_path("fabergray_erp")

_PAGE_JS = {
	"ventas": os.path.join(_APP_PATH, "fabrigray_erp", "page", "ventas", "ventas.js"),
	"cotizaciones": os.path.join(_APP_PATH, "fabrigray_erp", "page", "cotizaciones", "cotizaciones.js"),
	"bodega": os.path.join(_APP_PATH, "fabrigray_erp", "page", "bodega", "bodega.js"),
	"facturacion": os.path.join(_APP_PATH, "fabrigray_erp", "page", "facturacion", "facturacion.js"),
	"recorridos": os.path.join(_APP_PATH, "fabrigray_erp", "page", "recorridos", "recorridos.js"),
	"centro_faltantes": os.path.join(_APP_PATH, "fabrigray_erp", "page", "centro_faltantes", "centro_faltantes.js"),
}

_FG_SEARCH_JS = os.path.join(_APP_PATH, "public", "js", "fg_search.js")
_HOOKS_PY = os.path.join(_APP_PATH, "hooks.py")


def _read(path):
	with open(path, encoding="utf-8") as f:
		return f.read()


class TestSharedSearchHelperIsTheOnlyImplementation(IntegrationTestCase):
	"""Section 7 -- "no seis algoritmos distintos"."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.fg_search_js = _read(_FG_SEARCH_JS)
		cls.hooks_py = _read(_HOOKS_PY)

	def test_fg_search_js_exposes_the_three_shared_functions(self):
		self.assertIn('frappe.provide("fabergray_erp.search")', self.fg_search_js)
		self.assertIn("normalize_text", self.fg_search_js)
		self.assertIn("normalize_date", self.fg_search_js)
		self.assertIn("matches_operational_search", self.fg_search_js)
		# Actually assigned onto the namespace, not just referenced in a
		# comment somewhere.
		m = re.search(r"fabergray_erp\.search\s*=\s*\{([^}]*)\}", self.fg_search_js)
		self.assertIsNotNone(m)
		exported = m.group(1)
		for name in ("normalize_text", "normalize_date", "matches_operational_search"):
			self.assertIn(name, exported)

	def test_fg_search_js_is_globally_loaded_via_app_include_js(self):
		"""Every page depends on `fabergray_erp.search` existing without
		importing it itself -- must be a global app_include_js asset, same
		mechanism fg_shell.css already uses."""
		m = re.search(r"app_include_js\s*=\s*(\[[^\]]*\]|[\"'][^\"']*[\"'])", self.hooks_py, re.S)
		self.assertIsNotNone(m)
		self.assertIn("fg_search.js", m.group(1))

	def test_no_page_reimplements_its_own_accent_folding_or_date_parsing(self):
		"""Section 7's own explicit ban -- a page may CALL
		fabergray_erp.search.*, never redefine normalize_text/normalize_date/
		its own NFD-stripping regex locally."""
		for module, path in _PAGE_JS.items():
			js = _read(path)
			self.assertNotIn(
				"normalize(\"NFD\")",
				js,
				f"{module}.js re-implements accent-folding locally instead of calling fabergray_erp.search",
			)
			self.assertNotRegex(
				js,
				r"function\s+normalize_search_date\s*\(",
				f"{module}.js defines its own normalize_search_date() instead of calling fabergray_erp.search.normalize_date",
			)


class TestEveryModuleHasASearchBarWiredToASource(IntegrationTestCase):
	"""Section 8's own audit -- exactly one of these two wiring shapes must
	be present per module, matching this commit's own documented per-module
	decision (see each api/*.py's own Commit 25.20 docstring comments)."""

	# module -> ("client" pages call the shared JS matcher; "server" pages
	# thread a `txt` param through to their own whitelisted endpoint).
	_EXPECTED_MODE = {
		"ventas": "client",
		"cotizaciones": "client",
		"bodega": "client",
		"facturacion": "both",  # billing-review queue: client: pick-list queue: server
		"recorridos": "server",
		"centro_faltantes": "server",
	}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = {module: _read(path) for module, path in _PAGE_JS.items()}

	def test_client_side_modules_call_the_shared_matcher(self):
		for module in ("ventas", "cotizaciones", "bodega"):
			self.assertIn(
				"fabergray_erp.search.matches_operational_search",
				self.js[module],
				f"{module}.js is client-side (section 8) but never calls the shared matcher",
			)

	def test_facturacion_uses_both_shapes_for_its_two_queues(self):
		js = self.js["facturacion"]
		self.assertIn("fabergray_erp.search.matches_operational_search", js)
		# The pick-list (Pendientes de Facturar) queue is server-paginated
		# -- its search must be a `txt` round trip, not a client filter.
		self.assertIn("txt:", js)

	def test_server_side_modules_send_txt_to_their_own_endpoint(self):
		for module in ("recorridos", "centro_faltantes"):
			self.assertIn(
				"txt:",
				self.js[module],
				f"{module}.js is server-side (section 8) but never threads txt: to its endpoint call",
			)

	def test_search_bar_markup_present_on_every_module(self):
		"""fg-search-input is the shared, styled input class (fg_shell.css,
		scoped under .fg-shell) -- every module wrapped in .fg-shell uses it
		directly. Bodega (own `<div class="fg-bodega">` wrapper, not
		.fg-shell -- see bodega.js's own constructor) and Centro de
		Faltantes (own pre-existing fg-cf-search-input, left untouched by
		this commit, see its own docstring) each keep their own local input
		class instead."""
		local_input_class = {
			"bodega": "fg-orders-search",
			"centro_faltantes": "fg-cf-search-input",
		}
		for module, js in self.js.items():
			if module in local_input_class:
				self.assertIn(
					f'class="{local_input_class[module]}"',
					js,
					f"{module}.js has no {local_input_class[module]} markup",
				)
			else:
				self.assertIn(
					'class="fg-search-input',
					js,
					f"{module}.js has no .fg-search-input markup",
				)

	def test_clear_button_markup_present_on_every_module(self):
		"""Bodega's own top-level wrapper is `<div class="fg-bodega">`, not
		`.fg-shell` (see bodega.js's own constructor) -- fg_shell.css's
		`.fg-shell .fg-search-clear` rule never applies there, so Bodega's
		two updated search bars (Pedidos/Historial) use a local
		`.fg-bodega-search-clear` class with its own bodega.css rule
		instead, same visual shape. Every other module reuses the shared
		class directly."""
		for module, js in self.js.items():
			clear_class = "fg-bodega-search-clear" if module == "bodega" else "fg-search-clear"
			self.assertIn(
				clear_class,
				js,
				f"{module}.js has no clear-button markup (section 10)",
			)


class TestEmptyStateNeverBlank(IntegrationTestCase):
	"""Section 11 -- a real explanatory message, never a blank screen, once
	a search yields nothing. Bodega deliberately keeps its own pre-existing
	copy (documented scope decision, this commit's own report item) rather
	than switching to the shared render_search_empty_html() string."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = {module: _read(path) for module, path in _PAGE_JS.items()}

	def test_shared_empty_state_copy_present_where_the_shared_helper_is_used(self):
		for module in ("ventas", "cotizaciones", "facturacion", "recorridos", "centro_faltantes"):
			js = self.js[module]
			self.assertIn("No se encontraron resultados", js, f"{module}.js is missing the shared empty-state heading")
			self.assertIn(
				"Prueba buscando por nombre del cliente o fecha.",
				js,
				f"{module}.js is missing the shared empty-state hint",
			)

	def test_bodega_keeps_its_own_pre_existing_non_blank_empty_states(self):
		js = self.js["bodega"]
		self.assertIn("No hay pedidos que coincidan.", js)
		self.assertIn("No hay alistamientos finalizados que coincidan.", js)


class TestPlaceholderTextIsDescriptiveOfWhatEachModuleActuallySearches(IntegrationTestCase):
	"""Section 3's literal placeholder ("Buscar por cliente o fecha...")
	is used verbatim on every page whose search is ONLY customer+date
	(Ventas/Cotizaciones/Recorridos/Facturación's billing-review queue) --
	the two modules whose real, audited search surface is wider (Bodega:
	+pedido; Facturación's pick-list queue: +Pick List; Centro de
	Faltantes: +Item/Pedido) say so explicitly in their own placeholder
	rather than silently under-documenting what they actually match,
	while still starting from "cliente" / "fecha" as required by section
	2's own minimum."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.js = {module: _read(path) for module, path in _PAGE_JS.items()}

	def test_pure_customer_and_date_modules_use_the_exact_required_placeholder(self):
		for module in ("ventas", "cotizaciones", "recorridos"):
			self.assertIn("Buscar por cliente o fecha...", self.js[module])

	def test_wider_scope_modules_still_mention_cliente_and_fecha(self):
		for module in ("bodega", "facturacion", "centro_faltantes"):
			js = self.js[module]
			self.assertIn("cliente", js.lower())
			self.assertIn(
				"fecha",
				js.lower(),
				f"{module}.js's placeholder text must still mention 'fecha' per section 2's minimum",
			)
