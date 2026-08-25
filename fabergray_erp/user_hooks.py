# -*- coding: utf-8 -*-
"""fabergray_erp/user_hooks.py -- Home Fabrigray Desk-navigation profile,
applied automatically to every operational-role User.

Purely navigational: `User.module_profile` only ever controls which
Workspaces/Dashboards a session sees in the Desk sidebar
(frappe/desk/desktop.py::get_workspaces(), frappe/utils/modules.py::
get_modules_from_all_apps_for_user()) -- confirmed by an exhaustive grep of
apps/frappe for every call site of get_blocked_modules()/block_modules:
none appear in frappe/permissions.py or any has_permission() code path.
Applying this profile never changes what a user can read, write, submit
or cancel on any Doctype.

Two entry points below, sharing one eligibility rule
(_qualifies_for_default_module_profile) so there is only one place that
rule can ever drift:

- apply_default_module_profile() -- doc_events["User"]["validate"]
  (hooks.py). Catches every User created or edited from now on.
- backfill_module_profile_for_operational_users() -- hooks.py's
  after_migrate, not a [post_model_sync] Frappe patch. Confirmed live by
  reading frappe/migrate.py: Migration.run_schema_updates() runs
  [post_model_sync] patches BEFORE Migration.post_schema_updates() calls
  sync_fixtures() -- a patch would fire before the "Fabrigray Operativo"
  Module Profile fixture this relies on even exists, and
  User.validate_allowed_modules() would raise DoesNotExistError trying to
  resolve it. after_migrate runs last, after fixtures are synced, so the
  record always exists by the time this runs. Idempotent by construction
  (only ever touches a User whose module_profile is still empty) --
  running it on every migrate is a deliberate second safety net, not a
  workaround for it not being a one-time patch.

Recursion note: apply_default_module_profile() only ever mutates the
in-memory doc during its own validate() cycle -- it never calls .save()
itself, so there is no save -> hook -> save loop to worry about, and no
frappe.db.set_value()/db_set()/frappe.db.commit() of any kind is needed
here. The one subtlety: Document.hook()'s own compose() (frappe/model/
document.py) always runs a doctype's own controller method (User.validate()
-> validate_allowed_modules(), which syncs the `block_modules` child table
get_blocked_modules() actually reads) BEFORE any doc_events hook for that
same event -- so merely setting `doc.module_profile` here would leave
`block_modules` stale until the *next* save. This re-invokes
validate_allowed_modules() explicitly (Frappe's own method, not
reimplemented) so the effect is immediate, the same save that grants the
profile.
"""

import frappe

MODULE_PROFILE_NAME = "Fabrigray Operativo"

#: Kept in sync with hooks.py's fixtures "Role" filter and every Page's own
#: roles -- Compras/Producción/Despachos join this set once their own role
#: and Page exist, never before.
OPERATIONAL_ROLES = {"Vendedora", "Bodega", "Jefe de Bodega", "Facturación"}


def _qualifies_for_default_module_profile(doc) -> bool:
	"""doc: a User document (loaded, with its `roles` child table
	populated). Every rule from the brief, in order:
	- must be enabled;
	- never the literal Administrator account;
	- never a session that already holds System Manager, even alongside an
	  operational role -- System Manager wins as the administrative
	  exception, per explicit instruction;
	- must hold at least one operational role;
	- never overwrites an already-chosen module_profile (manual or
	  previously automatic) -- this function is only ever consulted when
	  it's still empty."""
	if not doc.enabled:
		return False
	if doc.name == "Administrator":
		return False
	if doc.module_profile:
		return False

	roles = {r.role for r in doc.get("roles") or []}
	if "System Manager" in roles:
		return False

	return bool(roles & OPERATIONAL_ROLES)


def apply_default_module_profile(doc, method=None):
	"""doc_events["User"]["validate"]."""
	if not _qualifies_for_default_module_profile(doc):
		return
	doc.module_profile = MODULE_PROFILE_NAME
	doc.validate_allowed_modules()


def backfill_module_profile_for_operational_users():
	"""hooks.py's after_migrate -- see this module's own docstring for why
	this is not a [post_model_sync] Frappe patch. Never touches a User
	whose module_profile already has any value, automatic or manual."""
	candidates = frappe.get_all(
		"User", filters={"enabled": 1, "module_profile": ["in", ["", None]]}, pluck="name"
	)

	updated = []
	for name in candidates:
		doc = frappe.get_doc("User", name)
		if not _qualifies_for_default_module_profile(doc):
			continue
		doc.module_profile = MODULE_PROFILE_NAME
		doc.save()
		updated.append(name)

	if updated:
		print(
			f"Fabrigray: applied '{MODULE_PROFILE_NAME}' module profile to "
			f"{len(updated)} existing user(s): {', '.join(updated)}"
		)
