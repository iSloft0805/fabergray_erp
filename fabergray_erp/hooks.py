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
# app_include_js = "/assets/fabergray_erp/js/fabergray_erp.js"

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
# all. The real mechanism is two pieces working together:
# 1) the Workspace "Fabrigray ERP" itself (fabrigray_erp/workspace/
#    fabrigray_erp/fabrigray_erp.json), sequence_id=0, which wins
#    frappe/public/js/frappe/views/workspace/workspace.js's own
#    get_page_to_show() fallback (first workspace in frappe.boot.workspaces,
#    already role/module-filtered server-side);
# 2) the "Fabrigray Operativo" Module Profile (below, applied automatically
#    by fabergray_erp/user_hooks.py) blocks every standard module for
#    operational users, so "Fabrigray ERP" isn't just first -- it's the
#    only Workspace left for them to see at all. Administrator/System
#    Manager never get that profile, so they keep the full standard Desk
#    (Fabrigray ERP still shows first there too, via sequence_id=0 alone).
# role_home_page = {
# 	"Role": "home_page"
# }

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

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
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
	{"dt": "Role", "filters": [["name", "in", ["Bodega", "Jefe de Bodega", "Vendedora", "Facturación"]]]},
	{
		"dt": "Custom DocPerm",
		"filters": [["role", "in", ["Bodega", "Jefe de Bodega", "Vendedora", "Facturación"]]],
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
# extend_doctype_class = {
# 	"Task": "fabergray_erp.custom.task.CustomTaskMixin"
# }

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

