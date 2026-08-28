# -*- coding: utf-8 -*-
"""Home Fabrigray -- forces the modern Workspaces renderer as the Desk
landing view, via the native `boot_session` hook (hooks.py).

Root cause this works around (confirmed live, no fix possible on the
`desktop:home_page` Default itself -- see this commit's audit): frappe.boot.
add_home_page() (frappe/boot.py) always resolves `desktop:home_page` as a
`Page` doctype name (`frappe.get_doc("Page", home_page)`). No Page named
"workspace"/"Workspaces"/a Workspace's own name exists on this site (or in
stock Frappe), so that lookup always raises DoesNotExistError and
add_home_page() always falls back to the literal string "desktop" --
routing every user, on every login, into the legacy "Desktop" page
(frappe/desk/page/desktop/). That page's icon grid is built from
Workspace Sidebar items auto-generated per Module Def
(auto_generate_sidebar_from_module(), frappe/desk/doctype/workspace_sidebar/
workspace_sidebar.py) and permission-checked against native Page/Report/
Dashboard role restrictions -- a completely different mechanism from
get_workspaces()'s blocked_modules filtering, and never wired to respect
it. That is why the standard ERPNext module grid (Organization/Accounting/
Buying/...) kept showing for every operational user even after the
"Fabrigray Operativo" Module Profile correctly emptied
frappe.boot.workspaces down to ["Fabrigray ERP"] (user_hooks.py).

boot_session hooks run *after* add_home_page() inside get_bootinfo()
(frappe/boot.py: add_home_page() at line ~75, the `for method in
hooks.boot_session` loop at line ~99) and receive the live `bootinfo` dict
by reference, so this can simply overwrite the value add_home_page()
already (wrongly) set -- no Page doctype record involved at all.

"Workspaces" is not a Workspace name and not a Page: it is the literal
dispatch key frappe.public/js/frappe/views/workspace/workspace.js
registers on the client --
    frappe.standard_pages["Workspaces"] = function () { ... }
frappe.views.pageview.show() (frappe/public/js/frappe/views/pageview.js)
checks `frappe.standard_pages[name]` *before* ever attempting a `Page`
doctype fetch, so this key short-circuits straight into
frappe.views.Workspace -- the same renderer /app/fabrigray-erp already
uses, reading the same already-filtered frappe.boot.workspaces.pages.
Workspace.get_page_to_show() (same file) falls back to
`this.workspaces[0].name` when there is nothing cached in
localStorage.current_page -- the first workspace in boot.workspaces.pages,
which is "Fabrigray ERP" for every operational role (only entry left,
Module Profile) and for Administrator/System Manager (sequence_id=0,
untouched).

Compatibility guardrail: "Workspaces" is an internal client dispatch key,
not a documented public contract. test_boot_home_page.py::
test_standard_pages_workspaces_key_still_exists_in_frappe_source asserts
the literal string is still registered in frappe's own workspace.js on
every test run, so a future Frappe upgrade that renames it fails a test
here instead of silently reintroducing the legacy Desktop landing in
production.

Registered in hooks.py as a second, additive `boot_session` entry --
Frappe merges every installed app's `boot_session` hook into one list
(frappe.append_hook(), frappe/__init__.py) and runs all of them in
frappe.get_installed_apps() order (frappe, erpnext, fabergray_erp on this
site); this does not replace or skip erpnext.startup.boot.boot_session,
confirmed by test_boot_home_page.py::
TestBootSessionHookComposition.

Landing fix (production incident, post-7c9c1e7) -- "Workspaces" above only
selects the *renderer*, never *which* Workspace it shows on a bare route
(login, the app logo, a bare /app): that decision is
Workspace.get_page_to_show()'s job (workspace.js), and it prefers a
per-browser localStorage.current_page cached from ANY earlier visit over
workspaces[0] -- confirmed live via /app/fabrigray-erp working while a
bare landing kept showing the standard ERPNext desktop in the same
browser. public/js/fg_home_landing.js (app_include_js, hooks.py) closes
that gap client-side by pre-seeding localStorage.current_page itself, but
only for users who actually belong to the operational Fabrigray
environment -- it needs that eligibility computed somewhere, and
`fg_home` below is where. Mirrors fabergray_erp/user_hooks.py's own
OPERATIONAL_ROLES/System-Manager/Administrator rule exactly (imported,
not re-typed) so the two can never drift apart: the same rule that grants
"Fabrigray Operativo" is the one that decides who gets this landing fix,
computed once per boot_session call (already running for every session
regardless), no extra request needed client-side.
"""

import frappe

from fabergray_erp.user_hooks import OPERATIONAL_ROLES

#: The one Workspace this app ships (fabergray_erp/fabrigray_erp/workspace/
#: fabrigray_erp/fabrigray_erp.json) -- see that file's own `name`.
FG_HOME_WORKSPACE = "Fabrigray ERP"


def set_home_page(bootinfo):
	"""boot_session hook -- see module docstring for the full mechanism."""
	bootinfo["home_page"] = "Workspaces"
	bootinfo["fg_home"] = {
		"is_operational": _is_operational_session(),
		"workspace": FG_HOME_WORKSPACE,
	}


def _is_operational_session() -> bool:
	"""True only for the exact same sessions user_hooks.py's
	_qualifies_for_default_module_profile() would grant "Fabrigray
	Operativo" to: never Administrator/Guest, never a System Manager (even
	alongside an operational role), only an actual OPERATIONAL_ROLES
	holder. Uses frappe.get_roles() (session-cached already) instead of
	loading a User doc -- boot_session has no doc to reuse, and this only
	ever needs the current session's own roles."""
	user = frappe.session.user
	if user in ("Administrator", "Guest"):
		return False
	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return False
	return bool(roles & OPERATIONAL_ROLES)
