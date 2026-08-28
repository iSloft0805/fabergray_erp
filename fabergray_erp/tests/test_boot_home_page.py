# -*- coding: utf-8 -*-
"""Home Fabrigray -- boot_session hook (fabergray_erp/boot.py) that forces
the modern Workspaces renderer as the Desk landing view for every System
User, instead of the legacy "Desktop" module grid frappe.boot.
add_home_page() (frappe/boot.py) always falls back to on this site. See
fabergray_erp/boot.py's own docstring for the full mechanism/root cause.

Every "home_page"/"workspaces" assertion here goes through the real
frappe.boot.get_bootinfo() (never a hand-rolled dict) -- the same function
frappe.sessions.get() calls on a cache miss. No test in this file ever
reads or sets desktop:home_page (the Default `add_home_page()` consults):
that Default was proven, in this commit's own audit trail, incapable of
holding a Workspace name -- set_home_page() works entirely downstream of
it, overwriting whatever add_home_page() already put in bootinfo, and does
not depend on that Default's value at all.
"""

import frappe
from frappe.boot import get_bootinfo
from frappe.tests import IntegrationTestCase

from fabergray_erp.boot import set_home_page
from fabergray_erp.tests import fixtures as fx

_OPERATIONAL_ROLES = ["Vendedora", "Bodega", "Jefe de Bodega", "Facturación"]


class TestBootSessionHookComposition(IntegrationTestCase):
	"""Frappe merges every installed app's boot_session hook into one list
	(frappe.append_hook(), frappe/__init__.py) rather than one app's hook
	replacing another's -- confirmed live (bench execute frappe.get_hooks
	--kwargs '{"hook": "boot_session"}'), not inferred from reading the
	loader alone. These tests re-confirm the same thing from inside the
	suite, so a future change that accidentally clobbers erpnext's hook
	(e.g. declaring boot_session as a list and dropping the merge, or a
	hooks.py typo) fails here instead of only showing up as a silently
	missing sysdefaults key in production.
	"""

	def test_both_boot_session_hooks_are_registered(self):
		hooks = frappe.get_hooks("boot_session")
		self.assertIn("erpnext.startup.boot.boot_session", hooks)
		self.assertIn("fabergray_erp.boot.set_home_page", hooks)

	def test_erpnext_hook_is_not_replaced_ours_is_additive(self):
		"""Not load-bearing for correctness (set_home_page() is a flat,
		order-independent overwrite) -- documents the real
		frappe.get_installed_apps() order this site resolves today
		(frappe, erpnext, fabergray_erp) and, more importantly, that both
		entries survive side by side in the merged list rather than one
		overwriting the other."""
		hooks = frappe.get_hooks("boot_session")
		self.assertEqual(len(hooks), len(set(hooks)), "duplicate boot_session entries")
		self.assertLess(
			hooks.index("erpnext.startup.boot.boot_session"),
			hooks.index("fabergray_erp.boot.set_home_page"),
		)

	def test_erpnext_boot_session_still_actually_executes(self):
		"""Proves erpnext's own hook is not just listed but genuinely
		still runs end-to-end: territory/customer_group are two of the
		sysdefaults keys erpnext.startup.boot.boot_session (apps/erpnext/
		erpnext/startup/boot.py) sets unconditionally for every non-Guest
		session -- if our hook had somehow displaced erpnext's in the
		list (e.g. both apps registering under the exact same list index
		due to a hooks-loading regression), these keys would go missing."""
		bootinfo = get_bootinfo()
		self.assertIn("territory", bootinfo.sysdefaults)
		self.assertIn("customer_group", bootinfo.sysdefaults)


class TestBootHomePagePerRole(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	@staticmethod
	def _bootinfo_for(user):
		"""Real frappe.boot.get_bootinfo(), bypassing the per-user Redis
		bootinfo/desktop_icons caches (frappe.sessions.get(),
		frappe.desk.doctype.desktop_icon.desktop_icon.get_desktop_icons())
		so each call reflects the current hook wiring, not a value cached
		by an earlier test or an earlier manual session."""
		frappe.cache.hdel("desktop_icons", user)
		frappe.cache.delete_key("bootinfo", user=user)
		with fx.as_user(user):
			return get_bootinfo()

	def test_each_operational_role_lands_on_workspaces_with_only_fabrigray_erp(self):
		"""The actual point of this whole commit: every operational role
		gets bootinfo.home_page == "Workspaces" (not "desktop"), and the
		Workspaces renderer it lands on has nothing to pick from but
		Fabrigray ERP -- the "Fabrigray Operativo" Module Profile
		(user_hooks.py) already proved this list shrinks correctly in
		test_module_profile.py; this test proves the *landing view*
		itself now actually reaches that filtered list instead of the
		unrelated legacy Desktop grid."""
		for i, role in enumerate(_OPERATIONAL_ROLES):
			with self.subTest(role=role):
				user = self.world.user(f"fg-boothp-{i}@example.com", [role])
				bootinfo = self._bootinfo_for(user)
				self.assertEqual(bootinfo.home_page, "Workspaces")
				names = [p["name"] for p in bootinfo.workspaces["pages"]]
				self.assertEqual(names, ["Fabrigray ERP"])

	def test_administrator_lands_on_workspaces_and_keeps_standard_workspaces(self):
		"""Administrator never gets the Module Profile (user_hooks.py) --
		this hook is global (every System User), so Administrator must
		also get home_page == "Workspaces", while still keeping every
		standard Workspace reachable (unlike the operational roles)."""
		bootinfo = self._bootinfo_for("Administrator")
		self.assertEqual(bootinfo.home_page, "Workspaces")
		names = {p["name"] for p in bootinfo.workspaces["pages"]}
		self.assertIn("Fabrigray ERP", names)
		self.assertGreater(len(names), 1)

	def test_administrator_fabrigray_erp_is_first_by_sequence(self):
		"""The other half of the "first workspace wins" fallback
		(Workspace.get_page_to_show(), frappe/public/js/frappe/views/
		workspace/workspace.js): for Administrator it's Workspace
		Fabrigray ERP's own sequence_id=0 -- not the Module Profile --
		that keeps it first in frappe.boot.workspaces.pages."""
		bootinfo = self._bootinfo_for("Administrator")
		pages = bootinfo.workspaces["pages"]
		self.assertEqual(pages[0]["name"], "Fabrigray ERP")


class TestSetHomePageUnit(IntegrationTestCase):
	"""The hook function in isolation -- no boot, no session switch."""

	def test_sets_home_page_key_to_workspaces(self):
		bootinfo = frappe._dict()
		set_home_page(bootinfo)
		self.assertEqual(bootinfo["home_page"], "Workspaces")

	def test_overwrites_a_pre_existing_home_page_value(self):
		"""Proves this is an unconditional overwrite, not set-if-empty --
		exactly what's needed to override add_home_page()'s "desktop"
		fallback (frappe/boot.py), which has already run and set
		bootinfo["home_page"] by the time boot_session hooks fire
		(get_bootinfo() calls add_home_page() before the boot_session
		loop -- confirmed by reading frappe/boot.py's own call order)."""
		bootinfo = frappe._dict(home_page="desktop")
		set_home_page(bootinfo)
		self.assertEqual(bootinfo["home_page"], "Workspaces")


class TestWorkspacesDispatchKeyGuardrail(IntegrationTestCase):
	"""Structural guardrail, not a boot/session test. "Workspaces" is not a
	documented public Frappe contract -- it is the literal client dispatch
	key frappe/public/js/frappe/views/workspace/workspace.js registers as

		frappe.standard_pages["Workspaces"] = function () { ... }

	and frappe/public/js/frappe/views/pageview.js's pageview.show() checks
	`frappe.standard_pages[name]` *before* ever attempting a `Page`
	doctype fetch -- that check is what makes fabergray_erp.boot.
	set_home_page()'s "Workspaces" value actually short-circuit into
	frappe.views.Workspace instead of a failed Page lookup. If a future
	Frappe upgrade renames or removes that registration, this exact
	regression (silently landing back on the legacy "Desktop" grid) comes
	back with no server-side signal at all, since bootinfo.home_page would
	still say "Workspaces" -- only the client would stop recognizing it.
	This test reads frappe's own source directly so that mismatch is
	caught here, in CI, instead of discovered by a Jefe de Bodega staring
	at Accounting/Buying/Manufacturing again."""

	def test_standard_pages_workspaces_key_still_exists_in_frappe_source(self):
		path = frappe.get_app_path(
			"frappe", "public", "js", "frappe", "views", "workspace", "workspace.js"
		)
		with open(path, encoding="utf-8") as f:
			source = f.read()
		self.assertIn(
			'frappe.standard_pages["Workspaces"]',
			source,
			"frappe's workspace.js no longer registers the \"Workspaces\" "
			"standard-page dispatch key -- fabergray_erp.boot."
			"set_home_page()'s bootinfo.home_page value is now dead. "
			"Update fabergray_erp/boot.py to match whatever key "
			"frappe.views.pageview.show() (frappe/public/js/frappe/views/"
			"pageview.js) now checks against frappe.standard_pages.",
		)
