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
from frappe.contacts.doctype.address.address import get_default_address
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.utils import cint

from erpnext.selling.doctype.customer.customer import parse_full_name

from fabergray_erp.api.bodega import _require_login

_ALLOWED_CUSTOMER_FIELDS = {"customer_name", "access_nombre_comercial", "tax_id", "customer_type"}

# Commit 22.7 -- Contact/Address, mismo boundary que _ALLOWED_CUSTOMER_FIELDS:
# exactamente estas claves, cualquier otra se rechaza (nunca se ignora en
# silencio). address_type/country/is_primary_*/links nunca son
# client-writable -- se derivan server-side (ver _apply_address_payload()).
_ALLOWED_CONTACT_FIELDS = {"mobile_no", "phone"}
_ALLOWED_ADDRESS_FIELDS = {"address_line1", "city", "state"}

_DEFAULT_ADDRESS_TYPE = "Billing"  # mismo default que el quick-entry nativo de ERPNext
_DEFAULT_ADDRESS_COUNTRY = "Colombia"  # única operación de este negocio

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


def _primary_contact_name(doc):
    """The one Contact this Customer's data should be read from/written
    to when several may be linked -- reuses ERPNext's own resolution
    exactly: Customer.customer_primary_contact when set (the native field
    that exists for this specific purpose), falling back to
    frappe.contacts.doctype.contact.contact.get_default_contact() (native
    helper: prefers Contact.is_primary_contact=1 among every Contact
    linked via Dynamic Link, else the first one) only when it isn't.
    Never an arbitrary pick of our own."""
    return doc.customer_primary_contact or get_default_contact("Customer", doc.name)


def _primary_address_name(doc):
    """Same reasoning as _primary_contact_name(), for Address --
    Customer.customer_primary_address first, else ERPNext's own
    get_default_address() (prefers Address.is_primary_address=1)."""
    return doc.customer_primary_address or get_default_address("Customer", doc.name)


@frappe.whitelist()
def get_customer_detail(name):
    """Full read-only detail for one Customer, via frappe.get_doc() +
    check_permission("read") -- real record-level permission, not a
    hand-picked field list bypassing it. Address/Contact are resolved only
    as far as the primary Contact/Address (_primary_contact_name()/
    _primary_address_name() -- Commit 22.7, reusing ERPNext's own
    customer_primary_contact/customer_primary_address +
    get_default_contact()/get_default_address() fallback), and only if the
    caller actually has Address/Contact read permission (explicit
    frappe.has_permission() check before resolving/querying anything --
    frappe.get_list() raises PermissionError outright for a doctype the
    caller has zero permission on, it does not just filter rows, so this
    guard is required, not optional): a caller with Customer-only
    permission simply sees null there, never a PermissionError from an
    incidental lookup."""
    _require_login()

    doc = frappe.get_doc("Customer", name)
    doc.check_permission("read")

    contact = None
    if frappe.has_permission("Contact", "read"):
        contact_name = _primary_contact_name(doc)
        if contact_name:
            rows = frappe.get_list(
                "Contact",
                filters={"name": contact_name},
                fields=["name", "first_name", "last_name", "email_id", "mobile_no", "phone"],
                limit_page_length=1,
            )
            contact = rows[0] if rows else None

    address = None
    if frappe.has_permission("Address", "read"):
        address_name = _primary_address_name(doc)
        if address_name:
            rows = frappe.get_list(
                "Address",
                filters={"name": address_name},
                fields=["name", "address_line1", "city", "state", "country"],
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


def _parse_scoped_payload(payload, allowed_fields, label):
    """Same shape/contract as _parse_customer_payload() -- a JSON-or-dict
    payload filtered through its own allowlist, any key outside it
    rejected outright (never silently dropped). `payload=None` (the
    contact/address argument omitted entirely) is treated as an empty
    payload, not an error -- update_customer() callers that only want to
    touch Customer fields must keep working exactly as before this
    commit."""
    if payload is None:
        return {}
    payload = frappe.parse_json(payload) if isinstance(payload, str) else payload
    if not isinstance(payload, dict):
        frappe.throw(_("Formato de datos de {0} inválido.").format(label))

    disallowed = set(payload.keys()) - allowed_fields
    if disallowed:
        frappe.throw(_("Campos no permitidos en {0}: {1}").format(label, ", ".join(sorted(disallowed))))

    return payload


def _set_primary_phone_row(contact, phone_value, flag_field):
    """Finds the existing phone_nos row currently flagged
    is_primary_phone/is_primary_mobile_no and updates it in place;
    appends a new row only when none is flagged yet. Never appends a
    second row for the same flag -- Contact.validate()'s own set_primary()
    (frappe/contacts/doctype/contact/contact.py) throws if more than one
    row ever carries the same primary flag, and this is also exactly what
    keeps a repeated save() idempotent (Commit 22.7 test: guardar dos
    veces los mismos datos nunca duplica el Contact ni su teléfono)."""
    for row in contact.phone_nos:
        if row.get(flag_field):
            row.phone = phone_value
            return
    contact.append("phone_nos", {"phone": phone_value, flag_field: 1})


def _apply_contact_payload(doc, payload):
    """doc: Customer, already loaded, not yet saved. payload: parsed dict,
    already filtered through _ALLOWED_CONTACT_FIELDS. Idempotent
    create-or-update against the Customer's own primary Contact
    (_primary_contact_name() -- never an arbitrary one when several are
    linked): updates it in place if one already exists, creates exactly
    one new Contact (same native shape as erpnext.selling.doctype.
    customer.customer.make_contact() -- Dynamic Link to this Customer,
    is_primary_contact=1, first_name/last_name (Individual, via that same
    module's parse_full_name()) or company_name (Company/Partnership) so
    Contact.autoname() has something real to build a name from) only if
    the caller actually supplied a phone and none exists yet. Real
    permission checked explicitly (frappe.has_permission(doc=...,
    throw=True)) before either path -- server-side, never inferred from
    the caller having Customer permission alone."""
    mobile_no = (payload.get("mobile_no") or "").strip()
    phone = (payload.get("phone") or "").strip()
    if not mobile_no and not phone:
        return  # both empty: no Contact touched, none created

    contact_name = _primary_contact_name(doc)

    if contact_name:
        frappe.has_permission("Contact", "write", doc=contact_name, throw=True)
        contact = frappe.get_doc("Contact", contact_name)
    else:
        frappe.has_permission("Contact", "create", throw=True)
        contact = frappe.new_doc("Contact")
        contact.append("links", {"link_doctype": "Customer", "link_name": doc.name})
        contact.is_primary_contact = 1
        if doc.customer_type == "Individual":
            first, middle, last = parse_full_name(doc.customer_name)
            contact.first_name = first
            contact.middle_name = middle
            contact.last_name = last
        else:
            contact.company_name = doc.customer_name

    if mobile_no:
        _set_primary_phone_row(contact, mobile_no, "is_primary_mobile_no")
    if phone:
        _set_primary_phone_row(contact, phone, "is_primary_phone")

    contact.save()  # real permission, no ignore_permissions

    if not doc.customer_primary_contact:
        doc.customer_primary_contact = contact.name


def _apply_address_payload(doc, payload):
    """Same reasoning as _apply_contact_payload(), for Address. New
    Address gets address_type="Billing" (ERPNext's own quick-entry
    default -- not exposed in this form, mandatory on the native doctype)
    and country="Colombia" (this business's only country of operation,
    per Commit 22.7's brief -- never client-writable; an existing
    Address's own country is left untouched, only new ones get it)."""
    address_line1 = (payload.get("address_line1") or "").strip()
    city = (payload.get("city") or "").strip()
    state = (payload.get("state") or "").strip()
    if not address_line1 and not city and not state:
        return  # all empty: no Address touched, none created

    address_name = _primary_address_name(doc)

    if address_name:
        frappe.has_permission("Address", "write", doc=address_name, throw=True)
        address = frappe.get_doc("Address", address_name)
    else:
        if not address_line1 or not city:
            frappe.throw(_("Dirección y ciudad son obligatorias para crear una dirección."))
        frappe.has_permission("Address", "create", throw=True)
        address = frappe.new_doc("Address")
        address.append("links", {"link_doctype": "Customer", "link_name": doc.name})
        address.address_title = doc.customer_name
        address.address_type = _DEFAULT_ADDRESS_TYPE
        address.country = _DEFAULT_ADDRESS_COUNTRY
        address.is_primary_address = 1
        address.is_shipping_address = 1

    if address_line1:
        address.address_line1 = address_line1
    if city:
        address.city = city
    if state:
        address.state = state

    address.save()  # real permission, no ignore_permissions

    if not doc.customer_primary_address:
        doc.customer_primary_address = address.name


@frappe.whitelist()
def update_customer(name, customer=None, contact=None, address=None):
    """customer: dict (or JSON string) with any subset of
    _ALLOWED_CUSTOMER_FIELDS -- only the keys actually present are
    changed, everything else on the Customer (including
    access_id_cliente, disabled, customer_group, territory, ...) is left
    exactly as it was. Never frappe.get_doc(...).update(payload)/
    .update(dict) -- each allowed field is assigned individually so the
    allowlist enforced by _parse_customer_payload() is the only thing
    that ever reaches the document, not implicit dict-merge behaviour.

    contact/address (Commit 22.7): optional dicts (or JSON strings),
    filtered through their own allowlists (_ALLOWED_CONTACT_FIELDS/
    _ALLOWED_ADDRESS_FIELDS) by _parse_scoped_payload() -- same boundary
    pattern as `customer`, never merged into it. Applied via
    _apply_contact_payload()/_apply_address_payload() BEFORE doc.save():
    if either raises (bad payload, missing permission, native validation),
    the exception propagates out of this whitelisted method before
    doc.save() is ever reached, so the Customer's own field changes above
    are never persisted either -- one request, one DB transaction, no
    frappe.db.commit() anywhere in this module (Frappe's own request
    lifecycle commits only after this method returns without raising, and
    rolls back everything -- including an already-saved Contact/Address in
    the same request -- the moment it doesn't)."""
    _require_login()

    doc = frappe.get_doc("Customer", name)
    doc.check_permission("write")

    payload = _parse_customer_payload(customer if customer is not None else {})
    contact_payload = _parse_scoped_payload(contact, _ALLOWED_CONTACT_FIELDS, "contacto")
    address_payload = _parse_scoped_payload(address, _ALLOWED_ADDRESS_FIELDS, "dirección")

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

    _apply_contact_payload(doc, contact_payload)
    _apply_address_payload(doc, address_payload)

    # Editing an Address that is already this Customer's
    # customer_primary_address triggers ERPNext's own native
    # ERPNextAddress.on_update() mixin (erpnext.accounts.custom.address),
    # which does frappe.db.set_value("Customer", ..., "primary_address",
    # ...) as a side effect -- bumping this same Customer's `modified`
    # timestamp in the DB out from under the in-memory `doc` this
    # function loaded at the top. Re-syncing doc.modified from the DB
    # right before save() (not doc.reload(), which would discard every
    # field this function just assigned) is what tells Frappe's own
    # optimistic-locking check (Document.check_if_latest()) this is that
    # same known, expected side effect -- not a real concurrent edit --
    # so a normal "Editar Address existente" call doesn't spuriously
    # raise TimestampMismatchError.
    doc.modified = frappe.db.get_value("Customer", doc.name, "modified")

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
