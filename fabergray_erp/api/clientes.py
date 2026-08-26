# -*- coding: utf-8 -*-
"""Commit 22.1 (read) + 22.2 (write) -- API layer for the future Page
Clientes (Commit 22.3), used by the new "Gestión de Clientes" role (Commit
22.1's own setup: Role + Custom DocPerm on Customer -- read/write/create,
no delete -- versioned in fixtures/role.json + custom_docperm.json, the
same mechanism already established for Bodega/Jefe de Bodega/Vendedora/
Facturación). Vendedora's own existing Customer/Address/Contact read
permission is untouched -- this is a new, separate role, not an extension
of hers.

Same conventions as api/ventas.py/bodega.py/facturacion.py: every access
goes through frappe.get_list()/frappe.get_doc()+check_permission()/
frappe.has_permission(throw=True), never frappe.get_all() (which bypasses
permissions), never ignore_permissions=True, never frappe.set_user()
outside of tests, never frappe.db.sql()/frappe.db.commit() (Frappe's own
request lifecycle commits after a whitelisted method returns).

Commit 22.2 (create_customer/update_customer/set_customer_disabled)
mirrors api/ventas.py's own Commit 18.5 design exactly: one JSON-or-dict
payload, filtered through a single shared allowlist helper
(_parse_customer_payload(), the same role _validate_and_build_item_rows()
plays there) shared by create and update -- one security boundary, not
two. Allowlist is exactly {customer_name, access_nombre_comercial, tax_id,
customer_type} -- access_id_cliente (the Fase 1 migration's own
idempotency key) and disabled (its own narrower, dedicated endpoint below)
are never in it, so sending either raises immediately, same as any other
disallowed key -- never silently dropped, never silently ignored.
customer_type is validated against the Customer doctype's own live
`options` (frappe.get_meta(), not a hardcoded copy that could drift),
defaulting to "Company" only when the caller omits it from
create_customer() entirely.

Scope, exactly as approved in the Fase 2 audit: Customer only (native
doctype -- no custom Client doctype, no delete endpoint). Address/Contact
are surfaced only as far as the primary links Customer itself already
stores (customer_primary_contact/customer_primary_address) -- no
Address/Contact list or CRUD here, deferred as approved."""

import frappe
from frappe import _
from frappe.utils import cint

from fabergray_erp.api.bodega import _require_login

_ALLOWED_CUSTOMER_FIELDS = {"customer_name", "access_nombre_comercial", "tax_id", "customer_type"}

_STATUS_FILTERS = {
    "all": {},
    "active": {"disabled": 0},
    "inactive": {"disabled": 1},
}

_LIST_FIELDS = [
    "name",
    "customer_name",
    "access_nombre_comercial",
    "tax_id",
    "disabled",
    "customer_primary_contact",
]


@frappe.whitelist()
def get_dashboard_summary():
    """KPI counts for the Page Clientes dashboard header.

    "Clientes nuevos este mes" was explicitly rejected in the Fase 2 audit
    approval: every migrated Customer's `creation` timestamp is the Access
    migration's own run date (2026-08-26), not the real historical
    registration date -- FechaSistema was deliberately not migrated. A
    "new this month" KPI computed from `creation` right now would report
    ~4087 clients as "new", which is misleading, not informative.
    Replaced (approved) by "Clientes con datos incompletos": Customer
    missing `tax_id` (Documento) or `access_nombre_comercial` (Nombre
    Comercial) -- the two identity-field gaps the Fase 1 migration audit
    already measured, not a newly invented metric."""
    _require_login()
    frappe.has_permission("Customer", "read", throw=True)

    activos = frappe.get_list("Customer", filters={"disabled": 0}, pluck="name")
    inactivos = frappe.get_list("Customer", filters={"disabled": 1}, pluck="name")
    total = frappe.get_list("Customer", pluck="name")
    incompletos = frappe.get_list(
        "Customer",
        or_filters=[["tax_id", "is", "not set"], ["access_nombre_comercial", "is", "not set"]],
        pluck="name",
    )

    return {
        "activos": len(activos),
        "inactivos": len(inactivos),
        "total": len(total),
        "datos_incompletos": len(incompletos),
    }


@frappe.whitelist()
def search_customers(txt="", status="all", start=0, page_length=20):
    """List + search, combined: called with txt="" for the plain paginated
    list, with txt set for search-as-you-type -- same underlying query
    either way, one function instead of duplicating it.

    status: "all" | "active" | "inactive" -- an unrecognized value falls
    back to "all" (never silently returns an empty result for a typo)."""
    _require_login()
    frappe.has_permission("Customer", "read", throw=True)

    filters = dict(_STATUS_FILTERS.get(status, {}))
    or_filters = None
    if txt:
        or_filters = [
            ["customer_name", "like", f"%{txt}%"],
            ["access_nombre_comercial", "like", f"%{txt}%"],
            ["tax_id", "like", f"%{txt}%"],
            ["name", "like", f"%{txt}%"],
        ]

    customers = frappe.get_list(
        "Customer",
        filters=filters,
        or_filters=or_filters,
        fields=_LIST_FIELDS,
        order_by="customer_name asc",
        limit_start=cint(start),
        limit_page_length=cint(page_length),
    )
    total = frappe.get_list("Customer", filters=filters, or_filters=or_filters, pluck="name")

    return {"customers": customers, "total": len(total)}


@frappe.whitelist()
def get_customer_detail(name):
    """Full read-only detail for one Customer, via frappe.get_doc() +
    check_permission("read") -- real record-level permission, not a
    hand-picked field list bypassing it. Address/Contact are resolved only
    as far as the primary links Customer already stores, and only if the
    caller actually has Address/Contact read permission (explicit
    frappe.has_permission() check before querying -- frappe.get_list()
    raises PermissionError outright for a doctype the caller has zero
    permission on, it does not just filter rows, so this guard is
    required, not optional): a caller with Customer-only permission (this
    commit's "Gestión de Clientes" role, scoped to Customer alone) simply
    sees null there, never a PermissionError from an incidental lookup."""
    _require_login()

    doc = frappe.get_doc("Customer", name)
    doc.check_permission("read")

    contact = None
    if doc.customer_primary_contact and frappe.has_permission("Contact", "read"):
        rows = frappe.get_list(
            "Contact",
            filters={"name": doc.customer_primary_contact},
            fields=["name", "first_name", "last_name", "email_id", "mobile_no", "phone"],
            limit_page_length=1,
        )
        contact = rows[0] if rows else None

    address = None
    if doc.customer_primary_address and frappe.has_permission("Address", "read"):
        rows = frappe.get_list(
            "Address",
            filters={"name": doc.customer_primary_address},
            fields=["name", "address_line1", "city", "country"],
            limit_page_length=1,
        )
        address = rows[0] if rows else None

    return {
        "name": doc.name,
        "customer_name": doc.customer_name,
        "access_nombre_comercial": doc.access_nombre_comercial,
        "tax_id": doc.tax_id,
        "customer_type": doc.customer_type,
        "customer_group": doc.customer_group,
        "territory": doc.territory,
        "disabled": doc.disabled,
        "access_id_cliente": doc.access_id_cliente,
        "creation": doc.creation,
        "contact": contact,
        "address": address,
    }


# ---------------------------------------------------------------------------
# Commit 22.2 -- write endpoints
# ---------------------------------------------------------------------------


def _valid_customer_types():
    """Reads the Customer doctype's own live `customer_type` Select
    options (frappe.get_meta()) -- never a hardcoded copy that could
    silently drift from the real doctype."""
    options = frappe.get_meta("Customer").get_field("customer_type").options or ""
    return [v for v in options.split("\n") if v]


def _validate_customer_type(customer_type):
    valid = _valid_customer_types()
    if customer_type not in valid:
        frappe.throw(
            _("customer_type inválido: {0}. Valores permitidos: {1}").format(
                customer_type, ", ".join(valid)
            )
        )


def _parse_customer_payload(customer):
    """Shared by create_customer()/update_customer() -- the one place a
    Customer field payload from the client is parsed and filtered.
    Rejects (never silently drops) any key outside
    _ALLOWED_CUSTOMER_FIELDS -- explicitly including access_id_cliente
    (the migration's own idempotency key, must never be client-writable)
    and disabled (its own dedicated, narrower endpoint, set_customer_
    disabled() below)."""
    payload = frappe.parse_json(customer) if isinstance(customer, str) else customer
    if not isinstance(payload, dict):
        frappe.throw(_("Formato de datos de cliente inválido."))

    disallowed = set(payload.keys()) - _ALLOWED_CUSTOMER_FIELDS
    if disallowed:
        frappe.throw(_("Campos no permitidos: {0}").format(", ".join(sorted(disallowed))))

    if payload.get("customer_type") is not None:
        _validate_customer_type(payload["customer_type"])

    return payload


def _to_bool(value):
    """Strict boolean parsing for set_customer_disabled() -- accepts real
    bool, 0/1 (int or str), "true"/"false" (any case); rejects anything
    else outright rather than coercing an arbitrary truthy value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true"):
            return True
        if v in ("0", "false"):
            return False
    frappe.throw(_("disabled debe ser booleano (true/false/0/1), recibido: {0}").format(value))


@frappe.whitelist()
def create_customer(customer):
    """customer: dict (or JSON string) with any subset of
    _ALLOWED_CUSTOMER_FIELDS. customer_name is required; customer_type
    defaults to "Company" only when omitted entirely (an explicit empty/
    invalid value is rejected, never silently replaced). access_id_cliente
    is never set here -- a Customer created through this endpoint is, by
    definition, not one of the Access-migrated records, so it stays null,
    exactly as approved."""
    _require_login()
    frappe.has_permission("Customer", "create", throw=True)

    payload = _parse_customer_payload(customer)
    if not payload.get("customer_name"):
        frappe.throw(_("customer_name es obligatorio."))

    doc = frappe.new_doc("Customer")
    doc.customer_name = payload["customer_name"]
    doc.customer_type = payload.get("customer_type") or "Company"
    doc.access_nombre_comercial = payload.get("access_nombre_comercial")
    doc.tax_id = payload.get("tax_id")
    doc.insert()  # real permission, no ignore_permissions

    return {"name": doc.name}


@frappe.whitelist()
def update_customer(name, customer):
    """customer: dict (or JSON string) with any subset of
    _ALLOWED_CUSTOMER_FIELDS -- only the keys actually present are
    changed, everything else on the Customer (including
    access_id_cliente, disabled, customer_group, territory, ...) is left
    exactly as it was. Never frappe.get_doc(...).update(payload)/
    .update(dict) -- each allowed field is assigned individually so the
    allowlist enforced by _parse_customer_payload() is the only thing
    that ever reaches the document, not implicit dict-merge behaviour."""
    _require_login()

    doc = frappe.get_doc("Customer", name)
    doc.check_permission("write")

    payload = _parse_customer_payload(customer)

    if "customer_name" in payload:
        if not payload["customer_name"]:
            frappe.throw(_("customer_name no puede quedar vacío."))
        doc.customer_name = payload["customer_name"]
    if "access_nombre_comercial" in payload:
        doc.access_nombre_comercial = payload["access_nombre_comercial"]
    if "tax_id" in payload:
        doc.tax_id = payload["tax_id"]
    if "customer_type" in payload:
        doc.customer_type = payload["customer_type"]  # already validated above

    doc.save()  # real permission, no ignore_permissions

    return {"name": doc.name}


@frappe.whitelist()
def set_customer_disabled(name, disabled):
    """The only endpoint allowed to touch Customer.disabled -- deliberately
    separate from update_customer() (whose allowlist explicitly excludes
    it), one narrow purpose, one obviously-named function."""
    _require_login()

    doc = frappe.get_doc("Customer", name)
    doc.check_permission("write")

    doc.disabled = 1 if _to_bool(disabled) else 0
    doc.save()  # real permission, no ignore_permissions

    return {"name": doc.name, "disabled": doc.disabled}
