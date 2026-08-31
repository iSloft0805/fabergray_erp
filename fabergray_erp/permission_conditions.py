# -*- coding: utf-8 -*-
"""fabergray_erp/permission_conditions.py -- Commit 25.1's own "el rol
controla el área, no el owner" mechanism: Company isolation for doctypes
whose Custom DocPerm now grants a shared, non-owner-scoped permission
(Sales Order/Quotation for role Vendedora -- if_owner dropped to 0 in
fixtures/custom_docperm.json this same commit). Without this module, once
if_owner=0, ANY Vendedora would see EVERY Sales Order/Quotation in the
whole site, including a future second Company's -- native Frappe has no
built-in "same Company as me" restriction for a plain Link field; that is
exactly what this module adds back, deliberately, server-side, never
trusting a `company` value the client could send.

Two entry points, both resolving the SAME allowed-company set via the one
shared `_allowed_companies()`:

1. `sales_order_permission_query_conditions()` / `quotation_permission_
   query_conditions()` -- registered in hooks.py's own
   `permission_query_conditions`, applied automatically by Frappe's own
   `frappe.model.db_query.DatabaseQuery` to every LIST query
   (`frappe.get_list()`/`frappe.get_all()`, and the standard Desk list
   view) for that doctype. Confirmed by reading
   `frappe/model/db_query.py:get_permission_query_conditions()` directly:
   this hook always runs, for every user INCLUDING Administrator (no
   built-in Administrator bypass exists at that layer) -- so this
   module's own `_allowed_companies()` must, and does, special-case
   Administrator/System Manager itself.

2. `assert_same_company()` -- the list-level hook above does NOT run for
   a single `frappe.get_doc(...).check_permission()`/`frappe.has_
   permission()` call: confirmed by reading `frappe/permissions.py`
   directly, a single-document permission check only ever consults
   `User Permission` (via `has_user_permission()`), never this app's own
   `permission_query_conditions` hooks. So every function in
   `api/ventas.py`/`api/cotizaciones.py` that loads ONE document by name
   must call this explicitly, immediately after `check_permission()` --
   otherwise a Vendedora who simply knows/guesses another Company's
   Sales Order name could read/write/cancel it once if_owner=0 makes the
   role-level grant unconditional. This is the exact same "the native
   mechanism doesn't cover single-doc company isolation on its own" gap
   `api.recorridos._address_belongs_to_company()` already documents and
   solves for Address -- same shape of problem, same kind of fix, not a
   new pattern invented here.

Company resolution mirrors the existing convention already used
throughout `api/ventas.py`/`api/cotizaciones.py`
(`frappe.defaults.get_global_default("company")`), never a value
supplied by the caller. If a `User Permission` (`allow="Company"`) exists
for the calling user, THOSE companies are used instead -- brief's own
"Si existen User Permissions de Company en el futuro, deben respetarse."
No User Permission exists anywhere in this site today (confirmed by
direct query during this commit's own audit), so this branch is currently
dormant -- forward-compatible, not exercised by any test yet.

Never uses `ignore_permissions`, `frappe.set_user`, `frappe.get_all()` to
bypass a check, or raw write SQL -- `frappe.get_list()` (permission-
respecting) is the only list read here, and `assert_same_company()` only
ever raises, never grants.
"""

import frappe
from frappe import _


def _allowed_companies(user=None):
    """The one place "which Company(ies) may `user` operate in" is
    resolved -- shared by both entry points below so they can never
    drift apart. Returns `None` to mean "no restriction" (Administrator
    or a System Manager -- matches how every other permission boundary
    in this app already treats System Manager as a superset role, e.g.
    Page.roles always lists it alongside the operational role, Custom
    DocPerm already grants it full access on every doctype this app
    touches). Returns `[]` (never a mix of None/empty meaning different
    things) when no Company can be resolved at all -- callers turn that
    into "deny", never "no restriction", so a misconfigured site fails
    closed, not open."""
    user = user or frappe.session.user
    if user == "Administrator" or "System Manager" in frappe.get_roles(user):
        return None

    # frappe.permissions.get_user_permissions() -- NOT frappe.get_list("User
    # Permission", ...) -- confirmed the hard way while writing this
    # commit's own tests: get_list() on "User Permission" is itself
    # permission-checked, and Vendedora (like every role this app grants)
    # has no read permission on that doctype at all, so it raised
    # PermissionError under her own session the moment this ran. The
    # get_user_permissions() helper is the same cached, permission-agnostic
    # accessor Frappe's own core permission engine uses internally for
    # exactly this "what is user X allowed to see" question -- never a
    # document read subject to the caller's own permissions.
    allowed = [row.doc for row in frappe.permissions.get_user_permissions(user).get("Company", [])]
    if allowed:
        return allowed

    default_company = frappe.defaults.get_global_default("company")
    return [default_company] if default_company else []


def _company_permission_query_condition(doctype, user=None):
    companies = _allowed_companies(user)
    if companies is None:
        return ""
    if not companies:
        return "1=0"
    escaped = ", ".join(frappe.db.escape(c) for c in companies)
    return f"`tab{doctype}`.`company` in ({escaped})"


def sales_order_permission_query_conditions(user=None, doctype=None):
    """Registered in hooks.py's `permission_query_conditions["Sales
    Order"]` -- Frappe calls this positionally with `user`, keyword
    `doctype` (see `frappe/model/db_query.py`); `doctype` is unused here
    since this function is only ever registered for "Sales Order"."""
    return _company_permission_query_condition("Sales Order", user)


def quotation_permission_query_conditions(user=None, doctype=None):
    """Same as `sales_order_permission_query_conditions()`, for
    Quotation -- registered separately in hooks.py (one function per
    doctype, both delegating to the same shared helper) so a future
    third doctype never has to guess which of the two to reuse."""
    return _company_permission_query_condition("Quotation", user)


def assert_same_company(doc, user=None):
    """The single-document counterpart to the two `*_permission_query_
    conditions()` functions above -- call this immediately after
    `doc.check_permission(...)` in any function that loads ONE Sales
    Order/Quotation by name, before reading or writing anything on it.
    A no-op (never raises) for Administrator/System Manager, exactly
    like the list-level hook. Raises `frappe.PermissionError` -- the
    same exception class a real `check_permission()` failure would
    raise -- never a bespoke exception type, so callers already handling
    `PermissionError` need no new branch."""
    companies = _allowed_companies(user)
    if companies is None:
        return
    if doc.company not in companies:
        frappe.throw(
            _("No tienes acceso a documentos de otra empresa."),
            frappe.PermissionError,
        )
