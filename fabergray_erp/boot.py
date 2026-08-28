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
"""

def set_home_page(bootinfo):
	"""boot_session hook -- see module docstring for the full mechanism."""
	bootinfo["home_page"] = "Workspaces"
