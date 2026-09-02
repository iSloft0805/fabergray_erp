app_name = "fabergray_erp"
app_title = "Fabrigray ERP"
app_publisher = "Fabrigray SAS"
app_description = "Sistema de gestión empresarial personalizado para Fabrigray SAS"
app_email = "desarollo@fabrigraysas.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fabergray_erp",
# 		"logo": "/assets/fabergray_erp/logo.png",
# 		"title": "Fabrigray ERP",
# 		"route": "/fabergray_erp",
# 		"has_permission": "fabergray_erp.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# Commit 6: shared, page-agnostic design system (design tokens + generic
# components) extracted from the approved /app/bodega look, so the Jefe de
# Bodega Page can reuse it without depending on bodega.css. Scoped entirely
# under .fg-shell -- see public/css/fg_shell.css.
app_include_css = "/assets/fabergray_erp/css/fg_shell.css"
# Home Fabrigray -- landing fix for operational users (production
# incident, post-7c9c1e7). See fabergray_erp/public/js/fg_home_landing.js
# and fabergray_erp/boot.py's own docstrings for the full mechanism.
app_include_js = "/assets/fabergray_erp/js/fg_home_landing.js"

# include js, css files in header of web template
# web_include_css = "/assets/fabergray_erp/css/fabergray_erp.css"
# web_include_js = "/assets/fabergray_erp/js/fabergray_erp.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fabergray_erp/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fabergray_erp/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
#
# NOT used for the Fabrigray home launcher -- confirmed live (see
# frappe/www/login.py) that this hook is only ever consulted for
# user_type == "Website User"; every real user in this app (Vendedora,
# Bodega, Jefe de Bodega, Facturación, System Manager, Administrator) is a
# System User, whose post-login redirect never reaches this code path at
# all. The real mechanism is three pieces working together:
# 1) boot_session (below) forces every session's Desk landing into the
#    modern Workspaces renderer (frappe.standard_pages["Workspaces"]) --
#    confirmed live that without this, add_home_page() (frappe/boot.py)
#    always falls back to the legacy "Desktop" page instead (no Page
#    doctype record named "workspace"/a Workspace's own name exists, on
#    this site or in stock Frappe), whose module grid is permission-
#    checked by a mechanism that never respects blocked_modules -- see
#    fabergray_erp/boot.py's own docstring for the full chain;
# 2) the Workspace "Fabrigray ERP" itself (fabrigray_erp/workspace/
#    fabrigray_erp/fabrigray_erp.json), sequence_id=0, which wins
#    frappe/public/js/frappe/views/workspace/workspace.js's own
#    get_page_to_show() fallback (first workspace in frappe.boot.workspaces,
#    already role/module-filtered server-side);
# 3) the "Fabrigray Operativo" Module Profile (below, applied automatically
#    by fabergray_erp/user_hooks.py) blocks every standard module for
#    operational users, so "Fabrigray ERP" isn't just first -- it's the
#    only Workspace left for them to see at all. Administrator/System
#    Manager never get that profile, so they keep the full standard Desk
#    (Fabrigray ERP still shows first there too, via sequence_id=0 alone).
# role_home_page = {
# 	"Role": "home_page"
# }

# Forces the Desk landing into the modern Workspaces renderer for every
# System User on every login -- additive to erpnext's own boot_session
# (erpnext.startup.boot.boot_session): Frappe merges every installed app's
# boot_session hook into one list and runs all of them (frappe.
# append_hook(), frappe/__init__.py), it does not replace/override.
# See fabergray_erp/boot.py's own docstring for the full mechanism and why
# this is the only fix point available (desktop:home_page itself cannot
# hold a Workspace name -- confirmed live, add_home_page() only ever
# resolves it as a Page doctype name).
boot_session = "fabergray_erp.boot.set_home_page"

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fabergray_erp.utils.jinja_methods",
# 	"filters": "fabergray_erp.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fabergray_erp.install.before_install"
# after_install = "fabergray_erp.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "fabergray_erp.uninstall.before_uninstall"
# after_uninstall = "fabergray_erp.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fabergray_erp.utils.before_app_install"
# after_app_install = "fabergray_erp.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fabergray_erp.utils.before_app_uninstall"
# after_app_uninstall = "fabergray_erp.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "fabergray_erp.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fabergray_erp.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# Commit 25.1 -- "el rol controla el área, no el owner": role-level Custom
# DocPerm on Sales Order/Quotation for Vendedora is now shared (if_owner=0),
# so Company isolation has to be enforced here instead -- see
# fabergray_erp/permission_conditions.py's own module docstring for why a
# list-level hook alone is not enough and api/ventas.py|cotizaciones.py's
# own assert_same_company() calls cover the single-document gap.
permission_query_conditions = {
	"Sales Order": "fabergray_erp.permission_conditions.sales_order_permission_query_conditions",
	"Quotation": "fabergray_erp.permission_conditions.quotation_permission_query_conditions",
}

# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# Commit 16/17: connects the Fulfillment Engine to the real Sales Order
# submit/cancel flow. Both handlers are one-line delegations -- see
# fulfillment/sales_order_hooks.py, which deliberately contains no
# fulfillment logic of its own, and FULFILLMENT_ENGINE_CONTRACT.md,
# "Commit 16"/"Commit 17" for the full transactional/concurrency/
# cancellation writeup.
doc_events = {
	"Sales Order": {
		"on_submit": "fabergray_erp.fulfillment.sales_order_hooks.on_submit",
		"on_cancel": "fabergray_erp.fulfillment.sales_order_hooks.on_cancel",
	},
	"Purchase Receipt": {
		"on_submit": "fabergray_erp.fulfillment.purchase_receipt_hooks.on_submit",
	},
	# Home Fabrigray -- Desk-navigation profile only (never a Doctype
	# permission). See fabergray_erp/user_hooks.py's own module docstring
	# for why this is safe against a save -> hook -> save loop.
	"User": {
		"validate": "fabergray_erp.user_hooks.apply_default_module_profile",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"fabergray_erp.tasks.all"
# 	],
# 	"daily": [
# 		"fabergray_erp.tasks.daily"
# 	],
# 	"hourly": [
# 		"fabergray_erp.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fabergray_erp.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fabergray_erp.tasks.monthly"
# 	],
# }

# Fixtures
# --------
# Roles y permisos propios de fabergray_erp (Bodega/Jefe de Bodega/Vendedora/
# Facturación), versionados en fabergray_erp/fixtures/. No se exportan
# roles/permisos de otras apps.

fixtures = [
	{
		"dt": "Role",
		"filters": [
			[
				"name",
				"in",
				["Bodega", "Jefe de Bodega", "Vendedora", "Facturación", "Gestión de Clientes", "Recorrido"],
			]
		],
	},
	{
		"dt": "Custom DocPerm",
		"filters": [
			[
				"role",
				"in",
				["Bodega", "Jefe de Bodega", "Vendedora", "Facturación", "Gestión de Clientes", "Recorrido"],
			]
		],
	},
	{
		# Commit 22.4 -- "System Manager" is a standard Frappe role (never
		# exported via the "Role" fixture above, which is only for this
		# app's own custom roles), but its Item/Bin/Item Price/Stock
		# Ledger Entry read grants for api/inventario.py ARE this app's
		# own Custom DocPerm rows and must be versioned too -- confirmed
		# via a real has_permission() audit that native ERPNext grants
		# System Manager nothing on any of these doctypes (item.json/
		# customer.json's own shipped permissions never list it), the
		# same latent gap every other Page.roles "+ System Manager" entry
		# in this app already has, just made explicit here instead of
		# silently relying on Administrator's own blanket bypass.
		#
		# Deliberately a SEPARATE fixture entry (own filename via
		# `prefix`, not folded into the block above): filtering the main
		# Custom DocPerm export by role="System Manager" alone also
		# matched unrelated, pre-existing Custom DocPerm rows granted to
		# System Manager by other ERPNext localizations (UAE VAT/South
		# Africa VAT) that have nothing to do with this app -- scoping by
		# `parent` too keeps this export to exactly the doctypes this
		# app actually grants System Manager on.
		#
		# Commit 22.8 -- "Stock Entry" added: same reasoning, for the
		# Material Receipt flow behind receive_shortage_purchase().
		#
		# Commit 24.4 -- "Address" added, and this one is NOT the same
		# "System Manager never had any native grant here" story as the
		# rows above. Address's OWN native permissions
		# (frappe/contacts/doctype/address/address.json) DO list System
		# Manager (read=1/write=1) -- but Address has carried Custom
		# DocPerm rows of its own since at least Commit 22.7 (Vendedora/
		# Gestión de Clientes), which -- confirmed live via
		# frappe.permissions.get_valid_perms("Address") -- means Frappe
		# has been silently ignoring ALL of Address's native permission
		# rows for EVERY role ever since, System Manager included. The
		# exact "Reporte de Faltante incident" class this app's own
		# native_restore fixture entry below already exists to fix for
		# Item Price/Stock Ledger Entry/Stock Reconciliation/Stock Entry
		# -- just never previously noticed for Address, because every
		# geolocation test before this commit's own real, non-
		# Administrator System Manager test user either used
		# Administrator (which bypasses every permission check outright)
		# or Gestión de Clientes (which already has its own explicit
		# Custom DocPerm row).
		#
		# Deliberately NOT a native_restore entry, though: Address's
		# OTHER masked native grants (Sales User/Purchase User/
		# Maintenance User/Accounts User/"All", all read=1/write=1) are
		# NOT replicated here. Restoring "All" -- literally every user on
		# the site -- would silently reopen the exact vulnerability the
		# turn-4 security audit spent an entire commit closing (Recorrido
		# and every other role regaining Address write through "All").
		# Only the ONE grant this commit's own brief explicitly requires
		# ("Autorizados: Gestión de Clientes / System Manager") is added.
		# Whether to restore the other four roles is a separate product
		# decision, flagged in this commit's own report, not resolved
		# here.
		# Commit 25.1 -- "Sales Order"/"Quotation" added. Exact same latent
		# gap, only now actually noticed empirically rather than assumed:
		# a Sales Order Custom DocPerm exists since Commit 18.1
		# (Facturación/Bodega/Jefe de Bodega/Vendedora), Quotation's since
		# Commit 20.1 (Vendedora) -- neither ever had its own System
		# Manager row, so a real (non-Administrator) System Manager test
		# user genuinely could not read either doctype through this app's
		# own permission model until this commit, confirmed live while
		# writing test_administrator_and_system_manager_see_every_company_
		# sales_order (test_ventas_permissions.py): frappe.has_permission()
		# for a fresh System-Manager-only user returned False before this
		# row was added. ventas.json/cotizaciones.json's own Page.roles
		# have listed "System Manager" since Commit 18.3/20.5 -- this
		# closes the gap those Page grants always implied but never
		# actually backed with a real Custom DocPerm row.
		"dt": "Custom DocPerm",
		"prefix": "system_manager",
		"filters": [
			["role", "=", "System Manager"],
			[
				"parent",
				"in",
				[
					"Item",
					"Bin",
					"Item Price",
					"Stock Ledger Entry",
					"Stock Reconciliation",
					"Warehouse",
					"Stock Entry",
					"Address",
					"Sales Order",
					"Quotation",
				],
			],
		],
	},
	{
		# Commit 22.4, part 2 -- Item Price and Stock Ledger Entry had ZERO
		# Custom DocPerm rows before this commit. Confirmed live: the
		# moment ANY Custom DocPerm row exists for a doctype, Frappe
		# ignores ALL of that doctype's own native DocPerm rows for EVERY
		# role (frappe.permissions.get_valid_perms()/
		# get_doctypes_with_custom_docperms() -- the exact mechanism that
		# already caused the Reporte de Faltante incident, see
		# test_facturacion_permissions.py). Real users on this site hold
		# Item Price's native roles (Sales Master Manager, Purchase
		# Master Manager) and Stock Ledger Entry's (Stock User, Accounts
		# Manager) -- granting Bodega/Jefe de Bodega/System Manager
		# access via Custom DocPerm would have silently masked all four.
		# Unlike Reporte de Faltante (an app-owned doctype, fixed by
		# adding a native DocPerm row to its own json), these are core
		# ERPNext doctypes this app does not own and must never edit --
		# so the fix is to replicate their exact native permission rows
		# (field for field, from item_price.json/stock_ledger_entry.json)
		# as Custom DocPerm rows of their own, restoring the exact same
		# effective access instead of leaving it lost.
		#
		# Commit 22.6 -- same incident, same fix, for Stock Reconciliation:
		# it had ZERO Custom DocPerm rows before this commit (confirmed
		# live), so granting Jefe de Bodega/System Manager a Custom DocPerm
		# there would have silently masked "Stock Manager"'s native full
		# access (39 real users on this site). Replicated field-for-field
		# from stock_reconciliation.json's own "Stock Manager" row.
		#
		# Commit 22.8 -- same incident, same fix, for Stock Entry (the
		# Material Receipt flow behind receive_shortage_purchase()): zero
		# Custom DocPerm rows before this commit, real native access for
		# Stock User/Manufacturing User/Manufacturing Manager/Stock
		# Manager (stock_entry.json's own shipped permissions), replicated
		# field-for-field the same way. "Manufacturing User"/"Manufacturing
		# Manager" added to the role list here (not needed by the three
		# doctypes above, which have no native grant for either).
		"dt": "Custom DocPerm",
		"prefix": "native_restore",
		"filters": [
			[
				"role",
				"in",
				[
					"Sales Master Manager",
					"Purchase Master Manager",
					"Stock User",
					"Accounts Manager",
					"Stock Manager",
					"Manufacturing User",
					"Manufacturing Manager",
				],
			],
			["parent", "in", ["Item Price", "Stock Ledger Entry", "Stock Reconciliation", "Stock Entry"]],
		],
	},
	{
		"dt": "Custom Field",
		"filters": [
			[
				"fieldname",
				"in",
				[
					"fg_started_by",
					"fg_started_on",
					"fg_observations",
					"fg_created_by_fulfillment_engine",
					# Migración de datos legados Access -> Customer/Item (identidad
					# estable de sincronización, no una etiqueta cosmética). Ver
					# fabergray_erp/migration_piloto/README.md para el importador
					# que las usa como clave de idempotencia.
					"access_id_cliente",
					"access_nombre_comercial",
					"access_id_producto",
					# Commit 22.6 -- Stock Reconciliation adjustment reason,
					# free text captured on the doc that carries the actual
					# stock movement (native mechanism, no custom doctype).
					"fg_adjustment_reason",
					# Commit 22.8 -- Stock Entry (Material Receipt) <->
					# Reporte de Faltante traceability. See
					# api/jefe_bodega.py's receive_shortage_purchase().
					"fg_shortage_report",
					"fg_purchase_reference",
					# Commit 23.0 -- Pick List's own OPERATIONAL invoicing
					# state (never contable): api/facturacion.py's
					# mark_as_invoiced().
					"fg_invoicing_status",
					"fg_invoiced_on",
					"fg_invoiced_by",
				],
			]
		],
	},
	{
		# Commit 23.0 (correction) -- per-item invoicing checklist,
		# api/facturacion.py's set_invoicing_item_checked()/
		# get_invoicing_detail(). Same reasoning as the Pick List-level
		# fields above, on the child doctype instead.
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Pick List Item-fg_invoicing_checked",
					"Pick List Item-fg_invoicing_checked_on",
					"Pick List Item-fg_invoicing_checked_by",
				],
			]
		],
	},
	{
		# Commit 18.5a -- Sales Order naming series (PEDIDO-.# as default,
		# SAL-ORD-.YYYY.- kept as a non-default second option). Commit 20.4
		# -- same mechanism for Quotation (COTIZACION-.# as default,
		# SAL-QTN-.YYYY.- kept as a non-default second option). Native
		# Document Naming Settings mechanism (frappe.custom.doctype.
		# property_setter) -- no custom Python counter, either doctype.
		"dt": "Property Setter",
		"filters": [["doc_type", "in", ["Sales Order", "Quotation"]], ["field_name", "=", "naming_series"]],
	},
	{
		# Home Fabrigray -- Desk-navigation profile applied automatically to
		# every operational-role User (fabergray_erp/user_hooks.py). Blocks
		# only standard Workspace/Dashboard navigation (frappe/desk/
		# desktop.py::get_workspaces(), frappe/utils/modules.py) -- never a
		# Doctype permission. Filtered by exact name so no foreign Module
		# Profile is ever exported.
		"dt": "Module Profile",
		"filters": [["name", "=", "Fabrigray Operativo"]],
	},
	{
		# Home visual -- Custom HTML Block rendered inside the Workspace
		# "Fabrigray ERP" (fabrigray_erp/workspace/fabrigray_erp/
		# fabrigray_erp.json), replacing the five native Shortcut blocks
		# as the landing launcher. Deliberately unfiltered by role
		# (roles=[] on the record itself, confirmed live via
		# is_custom_block_permitted() -- frappe/desk/desktop.py): every
		# System User sees the same five cards regardless of role; the
		# real access control is unchanged and lives entirely in
		# Page.is_permitted() on the destination route when a card is
		# clicked, exactly like before this block existed. Custom HTML
		# Block has no app/module/standard field of its own (unlike Page/
		# Workspace), so it cannot export-to-file the way those do --
		# this fixture, filtered by exact name, is the only versioning
		# mechanism available for it.
		"dt": "Custom HTML Block",
		"filters": [["name", "=", "Fabrigray Home"]],
	},
]

# Migration
# ---------
#
# Home Fabrigray -- backfill "Fabrigray Operativo" onto every existing
# operational-role User. Deliberately NOT a [post_model_sync] patch --
# confirmed live by reading frappe/migrate.py: those run BEFORE
# sync_fixtures(), i.e. before the Module Profile fixture above even
# exists yet. after_migrate runs last, and the function itself is
# idempotent (see fabergray_erp/user_hooks.py's own docstring) -- safe to
# run on every migrate, not just once.
after_migrate = "fabergray_erp.user_hooks.backfill_module_profile_for_operational_users"

# Testing
# -------

# before_tests = "fabergray_erp.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
#
# Commit 25.9 -- Pick List's own native validate_stock_qty() (erpnext core)
# throws "Insufficient Stock" whenever picked_qty > Bin.actual_qty, which is
# the wrong rule for Bodega's physical-count flow (picked_qty must be
# bounded by what was REQUESTED, not by what the ERP's own live stock
# figure says -- see fulfillment/pick_list_mixin.py's own module docstring
# for the full incident writeup and why this is the correct fix point:
# no core file touched, no global negative-stock bypass, scoped to Pick
# List only).
extend_doctype_class = {
	"Pick List": "fabergray_erp.fulfillment.pick_list_mixin.PickListPhysicalCountMixin"
}

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fabergray_erp.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fabergray_erp.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fabergray_erp.utils.before_request"]
# after_request = ["fabergray_erp.utils.after_request"]

# Job Events
# ----------
# before_job = ["fabergray_erp.utils.before_job"]
# after_job = ["fabergray_erp.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fabergray_erp.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

