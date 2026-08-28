// Home Fabrigray -- landing fix for operational users (production
// incident, post-7c9c1e7). See fabergray_erp/boot.py's own docstring for
// the full mechanism this closes: frappe.views.Workspace.
// get_page_to_show() (frappe/public/js/frappe/views/workspace/
// workspace.js) prefers a per-browser localStorage.current_page cached
// from ANY earlier visit over frappe.boot.workspaces.pages[0] -- so once
// a browser has ever rendered a standard ERPNext workspace (e.g. "Home"),
// every future bare-route landing (login, the app logo, a bare /app)
// keeps showing that cached workspace forever, even though the
// server-side data (Module Profile "Fabrigray Operativo" blocking every
// other Workspace) is already correct. Confirmed live: /app/fabrigray-erp
// (an explicit route) already worked while a bare landing in the same
// browser kept showing the standard desktop.
//
// This script only pre-seeds that fallback default -- it never routes or
// redirects anything itself:
// - get_page_to_show() only ever consults localStorage.current_page when
//   the current route has no explicit page segment (route[1] falsy). Any
//   explicit route -- /app/clientes, /app/inventario, /app/bodega,
//   /app/fabrigray-erp, a Form, a List -- always wins there regardless of
//   this value, so deep links are never intercepted and there is no
//   redirect loop to cause: this file never calls frappe.set_route() or
//   touches window.location.
// - runs once per full page load, synchronously, before frappe.
//   start_app()'s this.set_route() (frappe/public/js/frappe/desk.js)
//   fires -- frappe.boot is already assigned inline in desk.html before
//   this bundled script executes, so frappe.boot.fg_home is available.
//
// frappe.boot.fg_home (fabergray_erp/boot.py::set_home_page(), the same
// boot_session hook that sets home_page) is computed server-side once per
// boot -- no extra request needed here -- and mirrors
// fabergray_erp/user_hooks.py's own OPERATIONAL_ROLES/System Manager/
// Administrator eligibility rule exactly, so Administrator and any System
// Manager without that profile are never touched: their own
// localStorage.current_page (and their standard Desk navigation) is left
// completely alone.
(function () {
	if (typeof frappe === "undefined" || !frappe.boot) return;

	var fg_home = frappe.boot.fg_home;
	if (!fg_home || !fg_home.is_operational || !fg_home.workspace) return;

	try {
		localStorage.current_page = fg_home.workspace;
		localStorage.is_current_page_public = "true";
	} catch (e) {
		// localStorage unavailable (private browsing / disabled storage) --
		// Workspace.get_page_to_show()'s own workspaces[0] fallback still
		// resolves to fg_home.workspace on its own (it is the only entry
		// left for an operational user, Module Profile), just without
		// repairing any previously-poisoned cache in this browser.
	}
})();
