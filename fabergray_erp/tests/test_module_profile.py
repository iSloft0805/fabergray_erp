# -*- coding: utf-8 -*-
"""Home Fabrigray -- "Fabrigray Operativo" Module Profile (fixtures/
module_profile.json), the automatic-assignment hook and after_migrate
backfill (fabergray_erp/user_hooks.py). See that module's own docstring
for the mechanism writeup (why after_migrate and not a [post_model_sync]
patch, why validate() and not on_update()+db_set(), why this is purely
navigational and never a Doctype permission).

Every test here uses real throwaway User documents going through the real
save()/insert() lifecycle -- never frappe.db.set_value() to fake the
outcome -- so the actual hook wiring in hooks.py is what's under test,
not a reimplementation of its logic.
"""

import frappe
from frappe.desk.desktop import get_workspaces
from frappe.tests import IntegrationTestCase

from fabergray_erp.tests import fixtures as fx
from fabergray_erp.user_hooks import (
	MODULE_PROFILE_NAME,
	backfill_module_profile_for_operational_users,
)

_EXPECTED_BLOCKED_MODULES = {
	"Accounts",
	"Assets",
	"Buying",
	"CRM",
	"Core",
	"Email",
	"Integrations",
	"Manufacturing",
	"Setup",
	"Printing",
	"Projects",
	"Quality Management",
	"Selling",
	"Stock",
	"Subcontracting",
	"Support",
	"Website",
	"Automation",
}


class TestModuleProfileFixture(IntegrationTestCase):
	def test_module_profile_exists_exactly_once(self):
		self.assertEqual(frappe.db.count("Module Profile", {"name": MODULE_PROFILE_NAME}), 1)

	def test_blocked_modules_match_exactly(self):
		"""Exact internal module_name values, resolved from the real site
		(tabModule Def) before writing the fixture -- not guessed from
		visual labels. "Fabrigray ERP" itself is never in this list."""
		blocked = set(
			frappe.get_all("Block Module", filters={"parent": MODULE_PROFILE_NAME}, pluck="module")
		)
		self.assertEqual(blocked, _EXPECTED_BLOCKED_MODULES)
		self.assertNotIn("Fabrigray ERP", blocked)


class TestModuleProfileHook(IntegrationTestCase):
	"""apply_default_module_profile() -- doc_events["User"]["validate"]."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def test_new_user_with_each_operational_role_gets_the_profile(self):
		for i, role in enumerate(["Vendedora", "Bodega", "Jefe de Bodega", "Facturación"]):
			with self.subTest(role=role):
				email = f"fg23-hook-{i}@example.com"
				user = self.world.user(email, [role])
				self.assertEqual(frappe.db.get_value("User", user, "module_profile"), MODULE_PROFILE_NAME)
				self.assertEqual(
					set(frappe.get_all("Block Module", filters={"parent": user}, pluck="module")),
					_EXPECTED_BLOCKED_MODULES,
				)

	def test_system_manager_only_never_gets_the_profile(self):
		user = self.world.user("fg23-hook-sysmgr@example.com", ["System Manager"])
		self.assertFalse(frappe.db.get_value("User", user, "module_profile"))

	def test_administrator_is_never_touched(self):
		before = frappe.db.get_value("User", "Administrator", "module_profile")
		# Re-save Administrator (a real, harmless no-op edit) to exercise the
		# hook against that exact account, not just skip it by assumption.
		admin = frappe.get_doc("User", "Administrator")
		admin.save()
		self.assertEqual(frappe.db.get_value("User", "Administrator", "module_profile"), before)

	def test_system_manager_wins_over_an_operational_role_on_the_same_user(self):
		"""Brief's own explicit exception: System Manager + an operational
		role together must NOT receive the profile."""
		user = self.world.user("fg23-hook-dual@example.com", ["Vendedora", "System Manager"])
		self.assertFalse(frappe.db.get_value("User", user, "module_profile"))

	def test_manual_module_profile_choice_is_never_overwritten(self):
		"""A pre-existing module_profile (manual or otherwise) is left
		exactly as-is -- the hook only ever acts when it's empty."""
		manual_profile = frappe.get_doc(
			{"doctype": "Module Profile", "module_profile_name": "FG23 Perfil Manual"}
		)
		manual_profile.insert()
		self.world.track_existing("Module Profile", manual_profile.name)

		user_doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": "fg23-hook-manual@example.com",
				"first_name": "FG23 Manual",
				"send_welcome_email": 0,
				"module_profile": manual_profile.name,
			}
		)
		user_doc.insert()
		self.world.track_existing("User", user_doc.name)
		user_doc.append("roles", {"role": "Vendedora"})
		user_doc.save()
		self.assertEqual(frappe.db.get_value("User", user_doc.name, "module_profile"), manual_profile.name)

	def test_disabled_user_is_never_touched(self):
		user = self.world.user("fg23-hook-disabled@example.com", ["Vendedora"])
		doc = frappe.get_doc("User", user)
		doc.enabled = 0
		# Clear the profile the hook already applied on creation, to prove
		# a *disabled* user is skipped even if otherwise eligible.
		doc.module_profile = None
		doc.save()
		self.assertFalse(frappe.db.get_value("User", user, "module_profile"))

	def test_applying_the_profile_never_changes_real_doctype_permissions(self):
		"""The core guarantee: module_profile is navigation-only. Compares
		the exact same has_permission() checks a real Vendedora session
		already relies on (Commit 18.1/18.2), before and after the profile
		lands on her own User record via the real hook."""
		user = self.world.user("fg23-hook-permcheck@example.com", ["Vendedora"])
		self.assertEqual(frappe.db.get_value("User", user, "module_profile"), MODULE_PROFILE_NAME)

		with fx.as_user(user):
			self.assertTrue(frappe.has_permission("Sales Order", "create"))
			self.assertTrue(frappe.has_permission("Sales Order", "read"))
			self.assertTrue(frappe.has_permission("Quotation", "create"))
			self.assertFalse(frappe.has_permission("Pick List", "read"))
			self.assertFalse(frappe.has_permission("Reporte de Faltante", "read"))

	def test_workspace_list_shrinks_to_exactly_fabrigray_erp(self):
		"""The actual point of this whole commit: once the profile lands,
		Fabrigray ERP is not just first -- it's the *only* Workspace left,
		so there is nothing else for the user to pick."""
		user = self.world.user("fg23-hook-workspace@example.com", ["Bodega"])
		with fx.as_user(user):
			names = [p["name"] for p in get_workspaces()["pages"]]
		self.assertEqual(names, ["Fabrigray ERP"])

	def test_system_manager_keeps_standard_workspaces_visible(self):
		user = self.world.user("fg23-hook-sysmgr-ws@example.com", ["System Manager"])
		with fx.as_user(user):
			names = {p["name"] for p in get_workspaces()["pages"]}
		# Untouched standard Workspaces must still be reachable -- not an
		# exhaustive list, just proof more than "Fabrigray ERP alone" shows.
		self.assertIn("Home", names)
		self.assertIn("Fabrigray ERP", names)
		self.assertGreater(len(names), 1)


class TestModuleProfileBackfill(IntegrationTestCase):
	"""backfill_module_profile_for_operational_users() -- hooks.py's
	after_migrate. Every real user on this site was already backfilled by
	the actual `bench migrate` this commit ran (not re-tested here with
	hardcoded real emails -- see this file's own module docstring: no
	production/personal data belongs in an automated test). These tests
	build their own throwaway "legacy user" state instead, simulating a
	User created before this hook ever existed."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.TestWorld()
		cls.addClassCleanup(cls.world.cleanup)

	def _legacy_user(self, email, roles):
		"""A user the real hook already touched (module_profile got set on
		insert) -- rolled back to look exactly like a pre-existing user
		from before this hook existed, via a plain db write (not a second
		.save(), so this is not re-testing the hook itself)."""
		user = self.world.user(email, roles)
		frappe.db.set_value("User", user, "module_profile", None)
		frappe.db.delete("Block Module", {"parent": user})
		return user

	def test_backfill_applies_to_a_legacy_operational_user(self):
		user = self._legacy_user("fg23-backfill-1@example.com", ["Bodega"])
		self.assertFalse(frappe.db.get_value("User", user, "module_profile"))

		backfill_module_profile_for_operational_users()

		self.assertEqual(frappe.db.get_value("User", user, "module_profile"), MODULE_PROFILE_NAME)
		self.assertEqual(
			set(frappe.get_all("Block Module", filters={"parent": user}, pluck="module")),
			_EXPECTED_BLOCKED_MODULES,
		)

	def test_backfill_never_touches_a_legacy_system_manager(self):
		user = self._legacy_user("fg23-backfill-sysmgr@example.com", ["System Manager"])
		backfill_module_profile_for_operational_users()
		self.assertFalse(frappe.db.get_value("User", user, "module_profile"))

	def test_backfill_never_overwrites_a_manual_choice(self):
		manual_profile = frappe.get_doc(
			{"doctype": "Module Profile", "module_profile_name": "FG23 Perfil Manual Backfill"}
		)
		manual_profile.insert()
		self.world.track_existing("Module Profile", manual_profile.name)

		user = self.world.user("fg23-backfill-manual@example.com", ["Vendedora"])
		frappe.db.set_value("User", user, "module_profile", manual_profile.name)
		backfill_module_profile_for_operational_users()
		self.assertEqual(frappe.db.get_value("User", user, "module_profile"), manual_profile.name)

	def test_backfill_is_idempotent_across_two_consecutive_runs(self):
		user = self._legacy_user("fg23-backfill-idempotent@example.com", ["Facturación"])
		backfill_module_profile_for_operational_users()
		first = frappe.db.get_value("User", user, "modified")
		backfill_module_profile_for_operational_users()
		second = frappe.db.get_value("User", user, "modified")
		self.assertEqual(first, second, "a second run must not re-save an already-backfilled user")
