# -*- coding: utf-8 -*-
"""Commits 20.2-20.6 -- Fase 5 (Cotizaciones). API layer for Page
Cotizaciones (Commit 20.5), used by the Vendedora role. Every read goes
through `frappe.get_list()`/`frappe.get_doc()`+`check_permission()`, and
every write (`create_and_submit_quotation()`, Commit 20.3;
`update_draft_quotation()`, Commit 20.6) goes through a plain
`.insert()`/`.submit()`/`.save()` under Vendedora's own real session --
never `frappe.get_all()`, never `frappe.set_user()`, never
`ignore_permissions=True`, never `db_set`/`frappe.db.set_value` to skip a
validation. Vendedora always operates with her own real, restricted
permission on Quotation (Commit 20.1); Commit 25.1 dropped the original
`if_owner=1` scoping -- "el rol controla el área, no el owner" -- so this
is now shared across every Vendedora of the same Company, Company
isolation enforced by `fabergray_erp/permission_conditions.py` instead.
No Pick List/Reporte de Faltante/Fulfillment Engine/Material Request
involvement at all -- this module has nothing to bypass a permission for,
unlike api/ventas.py. Commit 25.17 is the ONE deliberate exception:
`create_sales_order_from_quotation()`, at the very bottom of this file,
converts an already-Aprobada Quotation into a real Sales Order via
ERPNext's own native `make_sales_order()` mapper (never a hand-built
mapping) -- see that function's own docstring for the full design. It
still creates zero Pick List/Reporte de Faltante/Material Request of its
own: `.submit()` alone triggers the exact same `Sales Order.on_submit`
Fulfillment Engine hook (hooks.py) every OTHER Sales Order in this app
already goes through, api/ventas.py's own `create_and_submit_sales_order()`
included -- there is no second, parallel pipeline.

`search_customers()`/`search_items()` are NOT duplicated here -- both are
already generic, carry nothing Sales-Order-specific, and are already
`@frappe.whitelist()`-ed in `api/ventas.py`. Page Cotizaciones calls
`fabergray_erp.api.ventas.search_customers`/`search_items` directly.

Fundamental rule this whole module exists to enforce (per the user's
explicit approval of the Fase 5 architecture): Vendedora never sees or
sends a price, a discount, a tax amount, or a total on a Quotation either
-- same policy as `api/ventas.py`. Every read function below omits
economic fields entirely from its response (built field by field, never
`.as_dict()`); `create_and_submit_quotation()` explicitly rejects any
economic field the caller tries to send, via a strict per-line allowlist
(`_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}`). ERPNext's own native
pricing engine resolves `rate`/`price_list_rate`/taxes/`grand_total`
server-side, automatically, the moment the Quotation is inserted --
nothing here duplicates or second-guesses that.

Stock/inventory is out of scope entirely for this module, by design (the
user's explicit instruction) -- unlike `api/ventas.py`'s `get_item_info()`,
this module's own `get_item_info()` never reads `Bin`/`get_actual_qty()`.
Commit 25.26 -- the one deliberate exception is
`get_quick_search_item_details()` (buscador rápido de "Nueva cotización"):
it returns an informative, read-only `qty_disponible` and the public
SELLING price (`public_price`) so the Vendedora can quote faster -- see
its own docstring for the narrowly-scoped Item Price read. `get_item_info()`
and every other Vendedora read keep their historical contract untouched,
and the create/update payload is still `{item_code, qty}` only.

Commit 25.13 -- Facturación billing review, added at the bottom of this
module: before this commit a Quotation went straight from "created" to
"whatever a future conversion phase does with it" with no verification of
its own. Every function below `_ALLOWED_ITEM_FIELDS` up to this point is
UNCHANGED and still Vendedora-facing, still never exposes an economic
field -- that boundary is exactly as strict as it was in Commit 20.x,
covered by the exact same `test_regression.py` guardrails. The NEW
functions at the bottom of this file are a deliberate, explicit exception
to that policy: Facturación's entire job is to verify prices/quantities/
ERP availability, so those functions -- and only those, gated by
`_require_facturacion_role()` (an explicit role check: "Facturación", or
System Manager/Administrator per this app's own standing admin-bypass
convention already established in `permission_conditions._allowed_
companies()` -- never inferred from `frappe.has_permission("Sales
Invoice", "read")` alone, corrected during this commit's own review: that
was only ever a proxy, and a role granted Sales Invoice access for an
unrelated reason would have incorrectly passed it) -- legitimately return
`rate`/`amount`/a reference price. Never `valuation_rate`/a Buying Item
Price/any purchase-side cost field -- Facturación reviews SALES pricing
only, see `_reference_selling_rates()`'s own docstring.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime, nowdate

from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.stock.doctype.pick_list.pick_list import get_actual_qty

from fabergray_erp.api.bodega import _require_login
from fabergray_erp.pricing import (
    PRICE_MODE_DISCOUNT_10,
    PRICE_MODE_DISCOUNT_15,
    PRICE_MODE_DISCOUNT_20,
    PRICE_MODE_DISCOUNT_25,
    PRICE_MODE_DISCOUNTS,
    PRICE_MODE_FULL,
    PRICE_MODE_LABELS,
    discounted_rate,
    public_selling_rates,
    reference_selling_rates,
)
from fabergray_erp.api.ventas import DEFAULT_DELIVERY_LEAD_DAYS, _default_warehouse_for_item
from fabergray_erp.permission_conditions import assert_same_company

# The only two fields a Quotation Item line may carry in from the client,
# enforced by `_validate_and_build_quotation_item_rows()` below (Commit
# 20.3) -- any other key (`rate`, `price_list_rate`, `discount_percentage`,
# `discount_amount`, `amount`, `net_rate`, `net_amount`,
# `margin_rate_or_amount`, `margin_type`, `currency`, `conversion_rate`,
# `taxes`, `total`/`grand_total`, or anything else, present or future) is
# rejected outright, never silently dropped.
_ALLOWED_ITEM_FIELDS = {"item_code", "qty"}

# Commit 25.13 -- the one, closed set of `fg_billing_review_status` values
# (Custom Field, fixtures/custom_field.json) -- every function below that
# sets this field uses these constants, never a bare string, so a typo
# can never silently create a new, unrecognized status. Mirrors
# `fg_cancellation_reason`'s own `CANCELLATION_REASONS` closed-enum
# convention in api/ventas.py (Commit 25.12).
BILLING_REVIEW_DRAFT = "Borrador"
BILLING_REVIEW_PENDING = "Pendiente de Facturación"
BILLING_REVIEW_APPROVED = "Aprobada"
BILLING_REVIEW_RETURNED = "Devuelta"

# Commit 25.14 -- the one, closed set of `price_mode` values
# apply_quotation_price_mode() accepts, mapped to their discount
# percentage -- never an arbitrary client-supplied percentage. "FULL"
# maps to `0` deliberately (not a separate code path): ERPNext's own
# `calculate_item_rate()` (erpnext/controllers/taxes_and_totals.py)
# already treats `discount_percentage == 0` as "no discount, rate =
# price_list_rate" -- see `apply_quotation_price_mode()`'s own docstring
# for the full audit of that native mechanism. Keys here double as the
# exact strings persisted to `fg_billing_price_mode`... except that field
# stores the human label (`_PRICE_MODE_LABELS` below), never this raw
# code, to match `fg_cancellation_reason`'s own "Select stores the label
# users actually see" convention (Commit 25.12).
#
# Commit 25.15 review fix, section 6 -- the FINAL closed set is
# FULL/10%/15%/20%/25%. "DISCOUNT_5" (5%) EXISTED ONLY BRIEFLY in this
# commit's own first draft and is REMOVED here -- it never shipped to a
# committed state (this whole feature has been uncommitted, "NO commit",
# since it was first written), so there is no historical data anywhere
# carrying the old "Descuento 5%" label to migrate/backfill. Grepped the
# entire working tree to confirm zero remaining references outside this
# rename itself. NEVER confuse this with IVA/tax percentages (`_build_
# pdf_tax_context()`'s own "5%"/"19%" tax labels, Commit 25.15's PDF work)
# -- those come from a completely different, unrelated source
# (`item_wise_tax_details`, real tax configuration) and are entirely
# untouched by this rename.
#
# The closed set itself (codes, percentages, labels) now lives in
# fabergray_erp/pricing.py, shared with Facturación's invoice pricing --
# re-exported here under the exact same names so nothing that reads
# `cotizaciones.PRICE_MODE_DISCOUNTS`/`_PRICE_MODE_LABELS` changes.
# The Select field's own closed set of options (fixtures/custom_field.json)
# -- `fg_billing_price_mode` stores ONE of these labels, never the raw
# "FULL"/"DISCOUNT_10"/"DISCOUNT_15"/"DISCOUNT_20"/"DISCOUNT_25" code the
# client sends.
_PRICE_MODE_LABELS = PRICE_MODE_LABELS

# Commit 25.13 review fix: the reference price list is READ from the
# Quotation itself (`qtn.selling_price_list`, native field, resolved by
# ERPNext at insert time to whatever Selling Settings/the party's own
# default configures -- "Standard Selling" for every quotation this app
# currently creates, but never hardcoded as the only possibility) --
# see `get_quotation_billing_detail()`'s own docstring. No module-level
# constant here anymore; `api/inventario.py`'s own `_selling_rates()`
# still hardcodes "Standard Selling" for its own, different purpose
# (Inventario is out of scope for this commit either way).


@frappe.whitelist()
def get_item_info(item_code):
    """Operational detail for one product, for the "Nueva Cotización" cart
    line -- an explicitly-built dict of only the allowed keys, never a
    forwarded `get_item_details()` response or `Item.as_dict()` (both of
    which carry pricing fields this function must never expose, by
    construction, not by after-the-fact filtering).

    No `qty_disponible`/`Bin`/`get_actual_qty()` here at all -- unlike
    `api/ventas.py`'s own `get_item_info()`, inventory has no role in a
    Quotation by explicit design (the user's own instruction: "El
    inventario puede incluso omitirse completamente de esta interfaz").
    """
    _require_login()

    item = frappe.get_doc("Item", item_code)
    item.check_permission("read")

    return {
        "item_code": item.item_code,
        "item_name": item.item_name,
        "description": item.description,
        "stock_uom": item.stock_uom,
        "image": item.image,
    }


# Commit 25.26 -- tope del lote de get_quick_search_item_details(): el mismo
# límite de 20 filas que ya devuelve ventas.search_items() (su propio
# `limit_page_length=20`), así una búsqueda del buscador rápido siempre cabe
# en UNA sola llamada de detalle.
QUICK_SEARCH_MAX_ITEMS = 20

# Las ÚNICAS claves que get_quick_search_item_details() devuelve por
# producto. Construidas campo por campo -- nunca un documento serializado.
QUICK_SEARCH_DETAIL_FIELDS = ("item_code", "qty_disponible", "public_price", "has_public_price")


def _parse_quick_search_item_codes(item_codes):
    """Lista de item_codes (JSON o lista) -> lista deduplicada, en el mismo
    orden, de strings no vacíos. Lanza ValidationError si llega algo que no
    sea una lista de strings o si supera QUICK_SEARCH_MAX_ITEMS."""
    if isinstance(item_codes, str):
        item_codes = frappe.parse_json(item_codes)
    if not isinstance(item_codes, (list, tuple)):
        frappe.throw(_("item_codes debe ser una lista de códigos de producto."))

    codes = []
    for code in item_codes:
        if not isinstance(code, str) or not code.strip():
            frappe.throw(_("item_codes debe ser una lista de códigos de producto."))
        if code not in codes:
            codes.append(code)

    if len(codes) > QUICK_SEARCH_MAX_ITEMS:
        frappe.throw(
            _("Máximo {0} productos por consulta.").format(QUICK_SEARCH_MAX_ITEMS)
        )
    return codes


@frappe.whitelist()
def get_quick_search_item_details(item_codes):
    """Commit 25.26 -- detalle por LOTES para el buscador rápido de "Nueva
    cotización": UNA llamada por búsqueda (tras ventas.search_items()), en
    lugar de una llamada get_item_info() por resultado.

    Devuelve, por cada producto válido, EXACTAMENTE
    QUICK_SEARCH_DETAIL_FIELDS:
      - `qty_disponible`: informativo, mismo origen que ventas.get_item_info()
        (`_default_warehouse_for_item()` + `get_actual_qty()`, solo lectura
        de Bin); None si el producto no tiene bodega por defecto. Nunca
        bloquea nada: una Quotation se crea igual con stock 0.
      - `public_price`/`has_public_price`: precio público de VENTA vía
        `pricing.reference_selling_rates()` -- la misma fuente que
        Facturación usa para sus modos de precio. Precio ausente, 0 o
        negativo -> `None`/`False` ("SIN PRECIO"), nunca 0 como válido.

    EXCEPCIÓN DE PERMISOS DOCUMENTADA: la Vendedora no tiene DocPerm sobre
    Item Price y NO se le otorga. El precio sale de
    `pricing.public_selling_rates()` -- la única lectura privilegiada de
    Item Price, acotada allí a la Selling Price List por defecto
    (habilitada, de venta, no de compra; nunca elegida por el cliente) y a
    `price_list_rate` > 0 -- y solo para los item_codes que ya pasaron aquí
    el filtro de producto vendible leído con los permisos reales de la
    usuaria. Nada de valuation_rate/last_purchase_rate/listas de compra/
    márgenes se lee siquiera.

    Productos inexistentes, deshabilitados, no vendibles, plantillas con
    variantes o no legibles para la usuaria simplemente no aparecen en la
    respuesta. Mostrar el precio NO cambia lo que se envía: la Quotation se
    sigue creando solo con {item_code, qty} (`_ALLOWED_ITEM_FIELDS`).
    """
    _require_login()
    _require_vendedora_role()
    frappe.has_permission("Item", "read", throw=True)

    codes = _parse_quick_search_item_codes(item_codes)
    if not codes:
        return []

    # Mismos filtros de "producto vendible" que ventas.search_items(), leídos
    # con los permisos reales de la usuaria (get_list, no get_all).
    valid = {
        row.name
        for row in frappe.get_list(
            "Item",
            filters={"name": ["in", codes], "disabled": 0, "is_sales_item": 1, "has_variants": 0},
            fields=["name"],
            limit_page_length=QUICK_SEARCH_MAX_ITEMS,
        )
    }
    codes = [code for code in codes if code in valid]
    if not codes:
        return []

    rates = public_selling_rates(codes)

    company = frappe.defaults.get_global_default("company")
    result = []
    for code in codes:
        warehouse = _default_warehouse_for_item(code, company)
        rate = flt(rates.get(code))
        has_price = rate > 0
        result.append(
            {
                "item_code": code,
                "qty_disponible": flt(get_actual_qty(code, warehouse)) if warehouse else None,
                "public_price": rate if has_price else None,
                "has_public_price": has_price,
            }
        )
    return result


@frappe.whitelist()
def get_quotation_summary():
    """KPI counts for the Page Cotizaciones dashboard header, scoped to
    this site's own Company (Commit 25.1: no longer to Vendedora's own
    quotations -- if_owner dropped from the Custom DocPerm). Company
    isolation comes from `frappe.get_list()`'s own
    `permission_query_conditions` (see `permission_conditions.py`), never
    a manual filter here; `frappe.get_all()` is never used.

    Commit 25.16 -- BUGFIX, confirmed visually on real data (COTIZACION-4/
    COTIZACION-3-5): before this commit, `pendientes`/`aprobadas` here
    were derived from native `Quotation.status` (`Open`/`Ordered`/
    `Partially Ordered`), which tracks Quotation -> Sales Order conversion
    -- a phase this app has never implemented (explicitly out of scope,
    see `assert_quotation_approved_for_conversion()`'s own module
    position). Native `status` therefore stayed `Open` forever regardless
    of Facturación's own review, so `aprobadas` always read 0 and
    `pendientes` kept counting a Quotation Facturación had ALREADY
    approved -- the exact contradiction the Page Cotizaciones card badge
    showed too (`fg-quotation-card-top`'s badge came from the same native
    `status`, see `cotizaciones.js::render_quotation_card()`). Facturación's
    own review (`fg_billing_review_status`, Commit 25.13) is this app's
    real, only commercial-approval workflow today -- `pendientes` and
    `aprobadas` are now derived from it directly, exactly like the
    `*_facturacion` buckets already were (Commit 25.13 section 15); the
    two are now intentionally identical in value (`pendientes ==
    pendientes_facturacion`, `aprobadas == aprobadas_facturacion`) and
    computed from the SAME query result below, never a second, redundant
    one -- `pendientes`/`aprobadas` simply remain the stable key names the
    dashboard's four KPI cards and `cotizaciones.js`'s own
    `quotation_matches_filter()` already read.

    Every one of the four `fg_billing_review_status` buckets below now
    also filters `docstatus != 2` -- an old, superseded amendment (an
    earlier version `modify_submitted_quotation()`/
    `apply_quotation_price_mode()` cancelled to create a new one, e.g.
    COTIZACION-3 through COTIZACION-3-4 before COTIZACION-3-5) keeps
    whatever `fg_billing_review_status` it read the INSTANT BEFORE
    cancellation frozen on the dead document forever (native Frappe
    amend-chain behavior, confirmed live) -- without this filter, a
    once-approved-then-price-adjusted Quotation would double (or
    triple...) count itself across every one of its own past amendments.
    Only the current, vigente version of a Quotation ever counts here.

    `cotizaciones_hoy` (`transaction_date == hoy`, any status/review) and
    `vencidas` (native `status == "Expired"`, set automatically, daily, by
    ERPNext's own already-active `set_expired_status()` scheduled job) are
    UNCHANGED by this commit -- vigencia (`valid_till`) and revisión
    comercial (`fg_billing_review_status`) are deliberately two separate
    concepts (section 10): a Quotation can read Aprobada AND Expired at
    the same time, and `aprobadas` never excludes an expired one just
    because it is expired (nothing here converts one into the other).
    """
    _require_login()
    frappe.has_permission("Quotation", "read", throw=True)

    cotizaciones_hoy = frappe.get_list("Quotation", filters={"transaction_date": nowdate()}, pluck="name")
    vencidas = frappe.get_list("Quotation", filters={"status": "Expired"}, pluck="name")

    borradores_facturacion = frappe.get_list(
        "Quotation",
        filters={"fg_billing_review_status": ["in", ["", BILLING_REVIEW_DRAFT]], "docstatus": ["!=", 2]},
        pluck="name",
    )
    pendientes_facturacion = frappe.get_list(
        "Quotation",
        filters={"fg_billing_review_status": BILLING_REVIEW_PENDING, "docstatus": ["!=", 2]},
        pluck="name",
    )
    aprobadas_facturacion = frappe.get_list(
        "Quotation",
        filters={"fg_billing_review_status": BILLING_REVIEW_APPROVED, "docstatus": ["!=", 2]},
        pluck="name",
    )
    devueltas_facturacion = frappe.get_list(
        "Quotation",
        filters={"fg_billing_review_status": BILLING_REVIEW_RETURNED, "docstatus": ["!=", 2]},
        pluck="name",
    )

    return {
        "cotizaciones_hoy": len(cotizaciones_hoy),
        "pendientes": len(pendientes_facturacion),
        "aprobadas": len(aprobadas_facturacion),
        "vencidas": len(vencidas),
        "borradores_facturacion": len(borradores_facturacion),
        "pendientes_facturacion": len(pendientes_facturacion),
        "aprobadas_facturacion": len(aprobadas_facturacion),
        "devueltas_facturacion": len(devueltas_facturacion),
    }


@frappe.whitelist()
def get_my_quotations(limit=50):
    """All of Fabrigray's Quotations (Commit 25.1: if_owner dropped from
    Quotation/Vendedora's Custom DocPerm), most recent first --
    operational fields only (number, customer, dates, status, line/unit
    counts, terms), never a rate/amount/total. Every Vendedora sees every
    quotation of this site's own Company, regardless of who created it.
    Company isolation is enforced centrally by `fabergray_erp.
    permission_conditions.quotation_permission_query_conditions()`
    (hooks.py's own `permission_query_conditions`), applied automatically
    by `frappe.get_list()` below. `frappe.get_all()` is never used
    here.

    Per-quotation line/unit counts are read by loading each of her own,
    already-authorized Quotations individually (`frappe.get_doc()` +
    `len(qtn.items)`) rather than a batched query against the Quotation
    Item child table -- same reasoning as `get_my_orders()`: child
    doctypes have no permission model of their own, so a batched
    child-table read would not reliably respect `if_owner`. `total_qty`
    (a native, top-level Quotation field, purely a unit count -- not
    money) supplies the unit count without a child-table read.

    `terms` (a native `Text Editor` field, NOT a Custom Field -- unlike
    Sales Order's `fg_observations`, Quotation already ships this field
    natively) is returned as `observations`, matching the key name Page
    Ventas already uses, so the future Page Cotizaciones (Commit 20.5)
    can reuse the same rendering logic. Covered by the exact same
    if_owner=1 Quotation permission already checked above -- no separate
    permission needed.

    No amend-chain walk-back here (unlike `get_my_orders()`'s
    `commercial_name`/superseded-amendment skip) -- Quotation/Vendedora's
    `amend=0` (Commit 20.1) means she can never amend one of her own
    quotations in the first place, so that whole concern does not apply.
    """
    _require_login()
    frappe.has_permission("Quotation", "read", throw=True)

    names = frappe.get_list(
        "Quotation",
        fields=["name"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=0,
        pluck="name",
    )

    max_quotations = cint(limit) or 50
    quotations = []
    for name in names:
        if len(quotations) >= max_quotations:
            break

        qtn = frappe.get_doc("Quotation", name)
        qtn.check_permission("read")

        quotations.append(
            {
                "name": qtn.name,
                "customer": qtn.party_name,
                "customer_name": qtn.customer_name,
                "transaction_date": qtn.transaction_date,
                "valid_till": qtn.valid_till,
                "status": qtn.status,
                # Commit 25.15 -- needed so the UI can tell a live "Aprobada"
                # Quotation (docstatus 1, VER/DESCARGAR PDF allowed) apart
                # from a stale one an amendment already superseded
                # (docstatus 2, `fg_billing_review_status` frozen at
                # whatever it read the instant BEFORE `modify_submitted_
                # quotation()` cancelled it -- can still read "Aprobada"
                # long after it stopped being the vigente document). Purely
                # a lifecycle flag, never an economic field.
                "docstatus": qtn.docstatus,
                "item_count": len(qtn.items),
                "total_qty": qtn.total_qty,
                "observations": qtn.terms,
                # Commit 25.13 -- null for a Quotation created before this
                # commit (never backfilled/inferred); the UI treats that
                # identically to "Borrador" (no review yet).
                "fg_billing_review_status": qtn.get("fg_billing_review_status") or None,
                "fg_billing_review_note": qtn.get("fg_billing_review_note") or None,
                # Commit 25.17 -- the ONE fact "ENVIAR A PEDIDOS" needs to
                # decide whether to show that button or "PEDIDO CREADO"
                # instead, see `_existing_sales_order_for_quotation()`'s
                # own docstring. Never an economic field -- just the
                # linked Sales Order's own name/status, read off the
                # native `prevdoc_docname` link `make_sales_order()`
                # itself already sets, no custom field involved.
                "sales_order": _existing_sales_order_for_quotation(qtn.name),
            }
        )

    return quotations


@frappe.whitelist()
def get_quotation_detail(name):
    """Line-level detail for any Quotation of this Company -- the "VER
    COTIZACIÓN" view in the future Page Cotizaciones. `check_permission
    ("read")` now only enforces the role-level grant (Commit 25.1 --
    if_owner=0); `assert_same_company()` right after it is what actually
    keeps a Vendedora from reading another Company's quotation by name --
    see `fabergray_erp/permission_conditions.py`'s own module docstring.

    The response is built field by field, never `qtn.as_dict()` or
    `row.as_dict()` (both of which carry every economic field on the
    document) -- there is nothing to filter after the fact because
    nothing economic is ever read into a variable here in the first
    place. See `test_regression.py` for the static guardrail that keeps
    this true if this function is ever touched again.
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("read")
    assert_same_company(qtn)

    return {
        "name": qtn.name,
        "customer": qtn.party_name,
        "customer_name": qtn.customer_name,
        "transaction_date": qtn.transaction_date,
        "valid_till": qtn.valid_till,
        "status": qtn.status,
        "item_count": len(qtn.items),
        "total_qty": qtn.total_qty,
        "observations": qtn.terms,
        # Commit 25.13 -- same null-for-historical convention as
        # get_my_quotations() above.
        "fg_billing_review_status": qtn.get("fg_billing_review_status") or None,
        "fg_billing_review_note": qtn.get("fg_billing_review_note") or None,
        # Commit 25.17 -- same convention as get_my_quotations() above.
        "sales_order": _existing_sales_order_for_quotation(qtn.name),
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty,
                "stock_uom": row.stock_uom,
            }
            for row in qtn.items
        ],
    }


def _validate_and_build_quotation_item_rows(items):
    """The one place a Quotation Item row list is built from
    client-supplied data. Rejects any line carrying a key outside
    `_ALLOWED_ITEM_FIELDS` -- `rate`, `price_list_rate`,
    `discount_percentage`, `discount_amount`, `amount`, `net_rate`,
    `net_amount`, `margin_rate_or_amount`, `margin_type`, `currency`,
    `conversion_rate`, `taxes`, `total`, or anything else, present or
    future -- never silently dropped. No warehouse, no delivery_date, no
    stock check of any kind (unlike `ventas._validate_and_build_item_rows()`)
    -- inventory has no role in a Quotation, by explicit design."""
    items = frappe.parse_json(items) if isinstance(items, str) else items
    if not items:
        frappe.throw(_("La cotización debe tener al menos un producto."))

    qtn_items = []
    for row in items:
        if not isinstance(row, dict):
            frappe.throw(_("Formato de línea de cotización inválido."))

        disallowed = set(row.keys()) - _ALLOWED_ITEM_FIELDS
        if disallowed:
            frappe.throw(
                _("Campos no permitidos en la línea de la cotización: {0}").format(", ".join(sorted(disallowed)))
            )

        if "item_code" not in row or "qty" not in row:
            frappe.throw(_("Cada línea de la cotización debe incluir item_code y qty."))

        item_code = row["item_code"]
        qty = flt(row["qty"])

        if not frappe.db.exists("Item", item_code):
            frappe.throw(_("El producto {0} no existe.").format(item_code))
        if qty <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(item_code))

        qtn_items.append({"item_code": item_code, "qty": qty})

    return qtn_items


def _require_vendedora_role(user=None):
    """Commit 25.14 audit fix (section 4/5 of the "AUDITORÍA FINAL" review)
    -- explicit gate for the 3 functions below that let Vendedora freely
    create/edit a Quotation's own commercial content: `create_and_submit_
    quotation()`, `update_draft_quotation()`, `modify_submitted_
    quotation()`. Before this commit none of the three checked a role
    NAME explicitly -- each relied purely on the underlying Custom DocPerm
    grant (`create`/`write`/`cancel` respectively) via `frappe.has_
    permission()`/`doc.check_permission()`, which was safe as long as
    Vendedora was the only role ever holding any of those three grants on
    Quotation. This same commit (25.14) granted Facturación `cancel: 1`
    and `create: 1` too -- strictly needed ONLY so `apply_quotation_price_
    mode()`'s own controlled cancel+amend could `amended.insert()` (see
    that function's own docstring: Frappe never actually enforces "amend"
    permission anywhere, `create` is the real, unconditional gate
    `Document.insert()` checks). Without this explicit role check, that
    same grant -- plus Facturación's own pre-existing `write: 1` from
    Commit 25.13 (needed only for `approve_quotation_billing()`/`return_
    quotation_from_billing()`'s plain `.save()`) -- would ALSO let a
    Facturación user reach these 3 Vendedora-only write paths directly and
    freely rewrite ANY Quotation's customer/items, far beyond the 3 closed
    price schemes `apply_quotation_price_mode()` allows. Same admin-bypass
    convention as `_require_facturacion_role()` below: Administrator, or
    "System Manager", pass unconditionally; everyone else must hold
    "Vendedora" (a user holding both "Vendedora" and "Facturación" keeps
    her own ordinary rights, deliberately -- this never punishes a dual-
    role user, only a Facturación-only one)."""
    user = user or frappe.session.user
    if user == "Administrator":
        return
    roles = frappe.get_roles(user)
    if "System Manager" in roles or "Vendedora" in roles:
        return
    frappe.throw(_("No tienes permiso para crear o modificar cotizaciones."), frappe.PermissionError)


@frappe.whitelist()
def create_and_submit_quotation(customer, items, valid_till=None, terms=None):
    """The critical operation: build and submit a standard Quotation from
    exactly what Vendedora is allowed to send -- `customer`, an optional
    `valid_till`/`terms`, and, per line, `item_code`/`qty` only -- and let
    `.insert()` run ERPNext's own native pipeline
    (`SellingController.validate()` -> `set_missing_values()` ->
    `calculate_taxes_and_totals()`) to resolve pricing/taxes/totals.
    Nothing in this function reads or writes a price field, queries `Item
    Price`, or calls any inventory helper (`get_actual_qty()`/`Bin`/Pick
    List/the Fulfillment Analyzer) -- a Quotation may be submitted
    regardless of stock level, by explicit design.

    Native field names used to build the document (confirmed against the
    installed ERPNext 16.32.1 `Quotation` doctype, and already proven live
    across every test in Commits 20.1/20.2 -- every one of those tests
    inserts and submits a real Quotation with exactly this shape):
    `quotation_to` (hard-set to `"Customer"`, never accepted from the
    client -- Vendedora only ever quotes an existing Customer),
    `party_name` (Dynamic Link, the actual customer -- there is no plain
    `customer` field on Quotation, unlike Sales Order), `company`,
    `items` (child table `Quotation Item`), `valid_till` (Date, optional,
    native `validate_valid_till()` rejects a value before
    `transaction_date` -- not re-validated here, ERPNext's own check is
    sufficient), `terms` (`Text Editor`, optional, NATIVE field -- not a
    Custom Field, see `get_my_quotations()`'s docstring). `currency`,
    `conversion_rate`, `selling_price_list`, `taxes_and_charges`,
    `order_type`, `transaction_date` are all left unset here and resolve
    to their own native defaults during `.insert()` -- exactly as already
    proven by every Commit 20.1/20.2 test, none of which sets them either.

    Security: each line is checked against `_ALLOWED_ITEM_FIELDS =
    {"item_code", "qty"}` via `_validate_and_build_quotation_item_rows()`
    -- any other key makes this function raise `frappe.ValidationError`
    immediately, before any Quotation is even constructed.

    No `ignore_permissions`, no `frappe.get_all`, no `frappe.set_user`, no
    `db_set`/`frappe.db.set_value` anywhere in this function -- `owner`
    ends up as `frappe.session.user` purely because `.insert()` runs
    under Vendedora's own real, unmodified session, and she can read the
    result back afterward only because of the real `if_owner=1` Custom
    DocPerm grant from Commit 20.1, not because of anything special done
    here.

    Returns only non-economic fields -- never `grand_total`,
    `rounded_total`, `total`, `taxes`, `rate`, or `amount`, all of which
    ERPNext computed internally and this function never reads.
    """
    _require_login()
    _require_vendedora_role()
    frappe.has_permission("Quotation", "create", throw=True)

    company = frappe.defaults.get_global_default("company")
    qtn_items = _validate_and_build_quotation_item_rows(items)

    qtn = frappe.get_doc(
        {
            "doctype": "Quotation",
            "quotation_to": "Customer",
            "party_name": customer,
            "company": company,
            "items": qtn_items,
        }
    )
    if valid_till:
        qtn.valid_till = valid_till
    if terms:
        qtn.terms = terms

    # Commit 25.13 -- every new Quotation starts in the billing-review
    # workflow's own initial state, explicitly, never left null/whatever
    # the field's schema default is -- see this commit's own section on
    # `fg_billing_review_status` for why "Borrador" (not yet sent to
    # Facturación) is distinct from a genuinely historical, pre-Commit-
    # 25.13 Quotation whose status is null (see get_my_quotations()/
    # get_quotation_detail()'s own docstrings for that distinction).
    qtn.fg_billing_review_status = BILLING_REVIEW_DRAFT

    qtn.insert()
    qtn.submit()

    return {
        "name": qtn.name,
        "status": qtn.status,
        "customer": qtn.party_name,
        "customer_name": qtn.customer_name,
        "transaction_date": qtn.transaction_date,
        "valid_till": qtn.valid_till,
        "item_count": len(qtn.items),
        "total_qty": qtn.total_qty,
    }


@frappe.whitelist()
def get_editable_quotation(name):
    """Prefill data for the "Editar cotización" view (Commit 20.6) --
    reuses `get_quotation_detail()`'s own exact response shape verbatim
    (same allowlist, same field-by-field construction, same static
    guardrail in `test_regression.py`), since editing reuses the
    identical "Nueva Cotización" screen just prefilled. `check_permission
    ("read")` + `assert_same_company()` (Commit 25.1) are where access is
    actually enforced -- role + same Company, no longer ownership; the
    `docstatus` check is the read-side half of the same "Draft only"
    boundary `update_draft_quotation()` enforces independently on the
    write side below.
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("read")
    assert_same_company(qtn)

    if qtn.docstatus != 0:
        frappe.throw(_("Solo se pueden editar cotizaciones en borrador."))

    return get_quotation_detail(name)


@frappe.whitelist()
def update_draft_quotation(name, customer, items, valid_till=None, terms=None):
    """Edits one of Vendedora's own Draft Quotations in place (Commit
    20.6) -- `customer`, `items` (`item_code`/`qty` only, via the exact
    same `_validate_and_build_quotation_item_rows()` allowlist
    `create_and_submit_quotation()` uses, Commit 20.3 -- one shared
    security boundary, not two independently-maintained copies), and
    `valid_till`/`terms`. Replaces the entire item list rather than
    patching individual rows -- exactly what "Editar cotización" reusing
    the "Nueva Cotización" screen naturally produces (she rebuilds her
    cart from the prefilled state, the same UI flow as creating a new
    quotation).

    Never submits -- "GUARDAR CAMBIOS" is deliberately not "CREAR
    COTIZACIÓN"; `.save()` alone lets ERPNext's own native pipeline
    (`SellingController.validate()` -> `set_missing_values()` ->
    `calculate_taxes_and_totals()`) re-resolve pricing/taxes/totals
    internally, exactly as it already does on insert -- nothing here
    reads or writes a price field, and the response below never returns
    one either.

    `check_permission("write")` + `assert_same_company()` (Commit 25.1)
    are where access is actually enforced -- role + same Company, no
    longer ownership. `docstatus == 0` is required explicitly, throwing
    a clear, specific message -- ERPNext's
    own docstatus-transition guard would eventually reject writing to a
    submitted document too, but only after doing more work first, and
    with a more generic message.

    Nothing here ever touches the document's naming series field, `owner`,
    `status`, `docstatus`, `currency`, `selling_price_list`, `rate`,
    `taxes`, or any discount/total field -- this function has no
    parameter that could carry any of them in, and
    `_validate_and_build_quotation_item_rows()` rejects
    any attempt to smuggle one through a line.

    Returns only `{"name": qtn.name}` -- no economic field, matching
    `update_draft_sales_order()`'s own minimal response shape (Commit
    18.5). (Deliberately avoids spelling out the Quotation naming series'
    own prefix anywhere in this docstring -- Commit 20.4's own guardrail
    confirms this module's source never contains it.)
    """
    _require_login()
    _require_vendedora_role()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("write")
    assert_same_company(qtn)

    if qtn.docstatus != 0:
        frappe.throw(_("Solo se pueden editar cotizaciones en borrador."))
    _assert_billing_review_not_pending(qtn)

    qtn_items = _validate_and_build_quotation_item_rows(items)

    qtn.party_name = customer
    qtn.set("items", [])
    for row in qtn_items:
        qtn.append("items", row)
    if valid_till is not None:
        qtn.valid_till = valid_till or None
    if terms is not None:
        qtn.terms = terms

    _reset_billing_review_on_edit(qtn)

    qtn.save()  # no ignore_permissions -- her real role+Company write permission already covers this

    return {"name": qtn.name}


# =============================================================================
# Commit 25.13 -- Facturación billing review workflow.
# =============================================================================
# A Quotation now carries its own review lifecycle, independent of
# docstatus/status: fg_billing_review_status is one of the four
# BILLING_REVIEW_* constants above, or null/"" for a historical Quotation
# created before this commit (treated identically to BILLING_REVIEW_DRAFT
# everywhere it is read -- never auto-approved, never backfilled).


def _assert_billing_review_not_pending(qtn):
    """Shared guard, called at the top of every function below that
    changes a Quotation's commercial content (items/customer/qty/rate) --
    section 5's own explicit rule: editing is blocked server-side while
    Facturación is reviewing, never just a hidden/disabled UI button. No
    "retirar de revisión" action exists in this commit -- if a correction
    is genuinely needed while pending, Facturación must return it first
    (`return_quotation_from_billing()`), never a silent edit underneath
    her while she may be looking at it."""
    if qtn.get("fg_billing_review_status") == BILLING_REVIEW_PENDING:
        frappe.throw(_("La cotización está siendo revisada por Facturación."))


def _reset_billing_review_on_edit(qtn):
    """CRITICAL (section 12): a legitimate commercial edit -- items,
    customer, qty, rate, or anything ERPNext's own pricing engine derives
    from those -- must never leave a stale "Aprobada" (or "Devuelta")
    review behind. Called by every write path that can change commercial
    content (update_draft_quotation() above, modify_submitted_quotation()
    below) right before `.save()`/`.insert()`, unconditionally -- going
    from "Borrador" to "Borrador" is a harmless no-op, so this never needs
    an `if` checking the previous value first. `fg_billing_review_note` is
    deliberately left untouched -- the previous review's own note (an
    approval note or a return reason) stays visible as historical context
    until the next explicit send/approve/return action overwrites it; no
    separate history mechanism is built for this (brief section 12's own
    "NO crear historial custom complejo salvo que sea necesario").

    Commit 25.14, section 15's own explicit rule -- a price mode
    Facturación applied (`fg_billing_price_mode`/`_adjusted_by`/`_adjusted_on`)
    is JUST AS stale as an old approval the moment the underlying
    commercial content changes again: whatever discount was calculated
    was calculated against the items/qty that existed at that moment,
    never re-validated against a Vendedora's later edit. Cleared here too,
    unconditionally, same reasoning as the three fields above."""
    qtn.fg_billing_review_status = BILLING_REVIEW_DRAFT
    qtn.fg_billing_reviewed_by = None
    qtn.fg_billing_reviewed_on = None
    qtn.fg_billing_price_mode = None
    qtn.fg_billing_price_adjusted_by = None
    qtn.fg_billing_price_adjusted_on = None


@frappe.whitelist()
def send_quotation_to_billing(quotation_name):
    """Commit 25.13, section 4 -- Vendedora's "ENVIAR A FACTURACIÓN"
    action. Re-validates the Quotation is actually ready (customer, at
    least one item, every qty > 0, every rate a real non-negative number)
    explicitly, at click time, the same "never trust what already got
    this far, check again before the real action" convention
    `confirm_order()` (api/ventas.py, Commit 25.10) already established
    for Sales Order -- even though ERPNext's own native validation already
    ran once at `.insert()`/`.submit()` time.

    Sets ONLY the four billing-review fields this action owns -- never
    touches `items`/`party_name`/`valid_till`/`terms`, so nothing here can
    ever accidentally change what was actually quoted. `.save()` alone
    (never `.submit()`/`.cancel()` -- docstatus never changes): the
    Quotation was already submitted by `create_and_submit_quotation()` or
    `modify_submitted_quotation()`, this action only moves its own,
    separate review status forward. Requires `allow_on_submit=1` on all
    five `fg_billing_review_*` Custom Fields (fixtures/custom_field.json)
    to succeed while docstatus stays 1 -- confirmed by reading
    `frappe/model/base_document.py`'s `_validate_update_after_submit()`
    directly (Commit 25.12's own cancel-action bypass does NOT apply
    here: this is a plain save, `_action == "update_after_submit"`, which
    DOES run that check, unlike a cancel).

    No Sales Order, no Pick List, no stock reservation, no invoice --
    this function calls nothing from api/ventas.py, api/bodega.py, or
    any Fulfillment Engine module.
    """
    _require_login()

    qtn = frappe.get_doc("Quotation", quotation_name)
    qtn.check_permission("write")
    assert_same_company(qtn)

    if qtn.docstatus != 1:
        frappe.throw(_("Solo se pueden enviar a Facturación cotizaciones sometidas."))

    current_status = qtn.get("fg_billing_review_status")
    if current_status == BILLING_REVIEW_PENDING:
        frappe.throw(_("Esta cotización ya está pendiente de revisión de Facturación."))
    if current_status == BILLING_REVIEW_APPROVED:
        frappe.throw(_("Esta cotización ya está aprobada. Corrígela antes de volver a enviarla."))

    if not qtn.party_name:
        frappe.throw(_("La cotización debe tener un cliente."))
    if not qtn.items:
        frappe.throw(_("La cotización debe tener al menos un producto."))
    for row in qtn.items:
        if flt(row.qty) <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(row.item_code))
        if row.rate is None or flt(row.rate) < 0:
            frappe.throw(_("El precio de {0} no es válido.").format(row.item_code))

    qtn.fg_billing_review_status = BILLING_REVIEW_PENDING
    qtn.fg_billing_review_note = None
    qtn.fg_billing_reviewed_by = None
    qtn.fg_billing_reviewed_on = None
    qtn.save()  # no ignore_permissions

    return {"name": qtn.name, "fg_billing_review_status": qtn.fg_billing_review_status}


@frappe.whitelist()
def modify_submitted_quotation(name, customer, items, valid_till=None, terms=None):
    """Commit 25.13, section 11 -- lets Vendedora correct a submitted
    Quotation's own commercial content (customer/items/qty; rate is never
    a parameter here, exactly like every other write in this module --
    ERPNext's own pricing engine re-resolves it) via ERPNext's native
    cancel+amend, the identical mechanism `modify_submitted_sales_order()`
    (api/ventas.py, Commit 18.5) already established for Sales Order --
    never by touching a field or child row of the still-submitted document
    directly (native `Quotation Item.qty`/`rate`/`item_code` have no
    `allow_on_submit`, confirmed by reading
    `erpnext/selling/doctype/quotation_item/quotation_item.json` directly
    -- a plain `.save()` cannot change them on a submitted Quotation at
    all, cancel+amend is the only native way).

    Blocked while `fg_billing_review_status == "Pendiente de Facturación"`
    (section 5) -- `_assert_billing_review_not_pending()`, same guard
    `update_draft_quotation()` above already uses. Available for every
    OTHER status (null/"Borrador"/"Devuelta"/"Aprobada" included --
    section 12's own scenario: a legitimate edit to an already-Aprobada
    Quotation is allowed, and is exactly what must invalidate the old
    approval, never silently keep it).

    Sequence: validate the new `items` payload FIRST (fail fast, nothing
    cancelled yet) -> `qtn.cancel()` (no bypass) -> `frappe.copy_doc(qtn,
    ignore_no_copy=False)` -- clears every `no_copy`-flagged field
    (`fg_billing_review_status`/`_reviewed_by`/`_reviewed_on`/`_note`/
    `_revision`, ALL five Custom Fields, marked `no_copy: 1` in
    fixtures/custom_field.json specifically so this one `copy_doc()` call
    already invalidates the old review as a side effect -- the same
    `no_copy` mechanism `picked_qty`/`delivered_qty`/`billed_amt` already
    rely on for Sales Order's own amend) -> `_reset_billing_review_on_edit()`
    called anyway, explicitly, right after (belt-and-suspenders: this
    function's own correctness must never depend on a Custom Field's
    `no_copy` flag being configured a particular way) -> the new customer/
    items/valid_till/terms are applied -> `.insert()` + `.submit()`.

    No `commercial_name`/root-of-amend-chain concept here (unlike Sales
    Order's `PEDIDO-N`) -- out of scope for this commit; `get_my_quotations()`/
    get_quotation_detail() do not walk `amended_from` chains, matching this
    module's own pre-existing "no amend-chain walk-back" note (now revised
    by granting `amend=1` to Vendedora, Commit 25.13's own fixture change,
    but the chain-walk UI concern itself is explicitly left as a known,
    accepted gap -- a superseded original may still show as its own card
    alongside its amendment, exactly as it would if nothing here called
    get_my_quotations() differently).

    Returns `{"name": amended.name}` only -- no economic field.
    """
    _require_login()
    _require_vendedora_role()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("cancel")
    assert_same_company(qtn)

    if qtn.docstatus != 1:
        frappe.throw(_("Solo se pueden modificar cotizaciones sometidas."))
    _assert_billing_review_not_pending(qtn)

    qtn_items = _validate_and_build_quotation_item_rows(items)  # fail fast -- nothing cancelled yet

    qtn.cancel()  # no ignore_permissions

    amended = frappe.copy_doc(qtn, ignore_no_copy=False)
    # frappe.copy_doc() only clears docstatus when NOT frappe.in_test --
    # same fix modify_submitted_sales_order() already applies, for the
    # identical reason (confirmed live, not assumed): under
    # IntegrationTestCase that flag is always true, so `qtn`'s
    # already-cancelled docstatus=2 would otherwise be copied verbatim.
    amended.docstatus = 0
    amended.amended_from = name
    amended.party_name = customer
    amended.set("items", [])
    for row in qtn_items:
        amended.append("items", row)
    if valid_till is not None:
        amended.valid_till = valid_till or None
    if terms is not None:
        amended.terms = terms

    _reset_billing_review_on_edit(amended)

    amended.insert()
    amended.submit()

    return {"name": amended.name}


def _require_facturacion_role(user=None):
    """Commit 25.13 review fix -- the AUTHORITATIVE check for "is this
    user allowed to act as Facturación on the billing review workflow".

    Explicit role membership only: "Facturación", or System Manager/
    Administrator -- the exact same admin-bypass convention this app's
    own `permission_conditions._allowed_companies()` already establishes
    (`user == "Administrator" or "System Manager" in frappe.get_roles(user)`),
    reused here for consistency rather than invented a second time.

    This function replaces an earlier draft of this gate that checked
    `frappe.has_permission("Sales Invoice", "read", throw=True)` as a
    PROXY for "is Facturación" -- flagged during this commit's own review
    as semantically wrong: that only happens to work because, on this
    site, exactly one role (Facturación) currently holds Sales Invoice
    access. A role granted Sales Invoice permission for an unrelated
    reason in the future would have silently passed the old check. There
    is no existing shared "require this role" helper anywhere else in
    this app to reuse (confirmed by searching every api/*.py module) --
    every other role-exclusive action in this codebase (e.g.
    `api/jefe_bodega.py`'s own `receive_shortage_purchase()`) is instead
    gated by a doctype permission only that role holds; that pattern was
    this function's own first draft, and is exactly what this fix moves
    away from for the billing review actions specifically, since Vendedora
    was ALSO going to end up holding a permission (`write` on Quotation)
    that could have been mistaken for the same kind of proxy.

    Policy (this commit's own explicit matrix): Vendedora -- no. Bodega --
    no. Jefe de Bodega -- no, UNLESS that same user ALSO explicitly holds
    the Facturación role (then it is the Facturación role granting
    access, never Jefe de Bodega itself). Recorrido -- no. Gestión de
    Clientes -- no. Facturación -- yes. System Manager/Administrator --
    yes, preserving this app's normal administrative bypass."""
    user = user or frappe.session.user
    if user == "Administrator":
        return
    roles = frappe.get_roles(user)
    if "System Manager" in roles or "Facturación" in roles:
        return
    frappe.throw(_("No tienes permiso para esta acción de Facturación."), frappe.PermissionError)


def _facturacion_billing_review_gate():
    """Shared entry gate for every Facturación-only function below --
    `_require_facturacion_role()` is the real authorization (see its own
    docstring for the full reasoning); every one of these functions ALSO
    carries a real `check_permission()`/`assert_same_company()` on the
    Quotation itself immediately after -- defense in depth, never a
    substitute for the role check.

    Facturación's Quotation Custom DocPerm (fixtures/custom_docperm.json)
    also grants `submit: 1`, even though nothing here ever calls
    `.submit()` -- confirmed live by reading `frappe/model/document.py`'s
    `check_docstatus_transition()` directly: a plain `.save()` that keeps
    docstatus at 1 (`approve_quotation_billing()`/`return_quotation_from_
    billing()`'s own write path, `_action == "update_after_submit"`)
    calls `check_permission("submit")` internally, not `"write"` -- a
    genuine Frappe quirk, not a design choice of this app's own. Without
    it, both functions raised a real `PermissionError` the first time
    this commit's own tests actually exercised them.

    Commit 25.14 review fix -- that same Custom DocPerm row ALSO now grants
    `cancel: 1`, `amend: 1`, AND `create: 1`: `apply_quotation_price_mode()`
    reuses `modify_submitted_quotation()`'s own native cancel+amend
    mechanism, and `qtn.cancel()` itself calls `check_permission("cancel")`.
    `amend: 1` is granted for consistency with Vendedora's own equivalent
    grant (Commit 25.13's own fixture change) and because it is the
    semantically-correct flag for this capability, but confirmed by reading
    `frappe/permissions.py` and `frappe/model/document.py` directly: Frappe
    never actually checks "amend" permission anywhere in its own code --
    the REAL, unconditionally-enforced gate for `amended.insert()` (any new
    document, `amended_from` set or not) is `document.py`'s own
    `check_permission("create")` inside `insert()`. Without `create: 1`
    here, `apply_quotation_price_mode()` raised a real `PermissionError` at
    `amended.insert()` the first time this commit's own tests actually
    exercised it -- `amend: 1`/`cancel: 1` alone were necessary but not
    sufficient.

    Commit 25.14 "AUDITORÍA FINAL" fix -- that same `create: 1` (plus the
    pre-existing `write: 1`/`cancel: 1`) is coarse: Frappe's declarative
    Custom DocPerm has no way to say "create, but ONLY as a price-mode
    amendment". Two things close that gap, together, deliberately (never
    `ignore_permissions`, never a weaker declarative grant instead):
    `_require_vendedora_role()` now gates the 3 ordinary Vendedora write
    paths (`create_and_submit_quotation()`/`update_draft_quotation()`/
    `modify_submitted_quotation()`) so a Facturación-only user can no
    longer reach THOSE through this app's own API even though the
    underlying doc-level permission would otherwise allow it; and
    `guard_facturacion_quotation_insert()` below (registered as
    Quotation's `before_insert` doc_event, hooks.py) blocks every OTHER
    insert path -- Desk's own native "New Quotation"/"Amend" buttons, or
    a raw `frappe.client.insert` call -- for a Facturación-only user,
    unless it is happening through `apply_quotation_price_mode()`'s own
    ONE controlled `amended.insert()` call (marked by the `frappe.flags.
    fg_billing_price_mode_insert` flag set immediately around it, nowhere
    else in this codebase)."""
    _require_login()
    _require_facturacion_role()


def guard_facturacion_quotation_insert(doc, method=None):
    """Quotation `before_insert` doc_event (hooks.py) -- see
    `_facturacion_billing_review_gate()`'s own docstring above for the
    full reasoning behind why this exists. Administrator/System Manager:
    exempt (this app's standing admin-bypass convention). A user holding
    "Vendedora" (even alongside "Facturación"): exempt -- her own
    ordinary create rights are untouched, this never penalizes a dual-
    role user. Any other role: exempt too -- this hook only restricts
    Facturación specifically, nothing else in this app currently holds
    `create: 1` on Quotation besides Vendedora and Facturación. A
    Facturación-only user: allowed ONLY while `frappe.flags.fg_billing_
    price_mode_insert` is truthy; every other insert attempt is rejected
    here, before a single row is written -- never silently, always a
    real `frappe.PermissionError` naming exactly why."""
    user = frappe.session.user
    if user == "Administrator":
        return
    roles = frappe.get_roles(user)
    if "System Manager" in roles or "Vendedora" in roles:
        return
    if "Facturación" not in roles:
        return
    if frappe.flags.get("fg_billing_price_mode_insert"):
        return
    frappe.throw(
        _(
            "El rol Facturación no puede crear cotizaciones nuevas directamente. "
            "Solo puede ajustar precios de cotizaciones ya enviadas a revisión de Facturación."
        ),
        frappe.PermissionError,
    )


@frappe.whitelist()
def get_quotation_billing_summary():
    """Commit 25.13, section 15 -- the one counter Page Facturación's own
    "COTIZACIONES PENDIENTES" section shows. Company isolation comes from
    `frappe.get_list()`'s own `permission_query_conditions` (already
    registered for Quotation, `permission_conditions.py`), never a manual
    filter here.

    Commit 25.14 review fix -- `docstatus: 1` added explicitly. Before
    this commit, no function ever cancelled a Quotation while its own
    `fg_billing_review_status` still read "Pendiente de Facturación"
    (`modify_submitted_quotation()`'s own `_assert_billing_review_not_
    pending()` guard made that impossible). `apply_quotation_price_mode()`
    (this commit) is the FIRST function that legitimately cancels a
    Quotation that IS still Pendiente (Facturación adjusting its price
    while reviewing it) -- the superseded original is never touched again
    after that cancel, so its own `fg_billing_review_status` field stays
    stuck reading "Pendiente de Facturación" forever, even though it is
    now docstatus=2. Without this filter, that cancelled original would
    double-count here (and duplicate-list in the tray below) alongside
    its own live amended replacement."""
    _facturacion_billing_review_gate()

    pending = frappe.get_list(
        "Quotation", filters={"fg_billing_review_status": BILLING_REVIEW_PENDING, "docstatus": 1}, pluck="name"
    )
    return {"cotizaciones_pendientes": len(pending)}


def _customer_company_types(customer_names):
    """Commit 25.22 -- bulk Customer.fg_customer_company_type lookup for
    Facturación's Quotation views, one query for the whole page -- same
    helper/reasoning as api.facturacion._customer_company_types() (kept
    as its own copy here, not imported, same "each module stays
    independent" convention this whole app already follows for small
    helpers). Guarded by frappe.has_permission("Customer", "read") --
    never assumed -- so a caller without it simply sees None everywhere,
    never a PermissionError from an incidental enrichment lookup.
    Source of truth is always this live read against Customer, never a
    value copied onto Quotation (section 12's own explicit "preferir
    fuente dinámica")."""
    customer_names = [c for c in dict.fromkeys(customer_names) if c]
    if not customer_names or not frappe.has_permission("Customer", "read"):
        return {}
    rows = frappe.get_list(
        "Customer",
        filters={"name": ["in", customer_names]},
        fields=["name", "fg_customer_company_type"],
    )
    return {r.name: (r.fg_customer_company_type or None) for r in rows}


def _customer_company_type(customer_name):
    """Single-Customer counterpart of _customer_company_types() above,
    for get_quotation_billing_detail() below (one Quotation, one
    Customer, at a time)."""
    if not customer_name or not frappe.has_permission("Customer", "read"):
        return None
    return frappe.db.get_value("Customer", customer_name, "fg_customer_company_type") or None


@frappe.whitelist()
def get_pending_billing_review_quotations(limit=50):
    """Commit 25.13, section 6 -- Page Facturación's "COTIZACIONES
    PENDIENTES" tray. Lists ONLY `fg_billing_review_status ==
    "Pendiente de Facturación"` -- never an arbitrary client-supplied
    filter (section 16's own explicit rule). Company isolation is
    enforced the same way `get_my_quotations()` already is: centrally, by
    `frappe.get_list()`'s own `permission_query_conditions`, never
    re-derived here.

    Unlike every Vendedora-facing read in this module, this response DOES
    include `grand_total` -- Facturación's entire job here is reviewing
    commercial value, and this function is gated by
    `_facturacion_billing_review_gate()`, never reachable by Vendedora.
    Still never `valuation_rate`/a cost field of any kind -- `grand_total`
    is the native, already-computed SELLING total, nothing here reads
    Item Price or any buying-side field.

    `docstatus: 1` -- see `get_quotation_billing_summary()`'s own
    docstring (Commit 25.14 review fix) for the full reasoning: without
    it, a Quotation superseded by `apply_quotation_price_mode()`'s own
    cancel+amend would keep showing here as a stale, already-cancelled
    duplicate of its own live replacement.
    """
    _facturacion_billing_review_gate()

    names = frappe.get_list(
        "Quotation",
        filters={"fg_billing_review_status": BILLING_REVIEW_PENDING, "docstatus": 1},
        fields=["name"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=0,
        pluck="name",
    )

    max_quotations = cint(limit) or 50
    quotations = []
    for name in names:
        if len(quotations) >= max_quotations:
            break

        qtn = frappe.get_doc("Quotation", name)
        qtn.check_permission("read")

        quotations.append(
            {
                "name": qtn.name,
                "customer": qtn.party_name,
                "customer_name": qtn.customer_name,
                "transaction_date": qtn.transaction_date,
                "owner": qtn.owner,
                "owner_fullname": frappe.utils.get_fullname(qtn.owner),
                "item_count": len(qtn.items),
                "total_qty": qtn.total_qty,
                "grand_total": qtn.grand_total,
                "fg_billing_review_status": qtn.fg_billing_review_status,
            }
        )

    # Commit 25.22 -- one batched Customer lookup for the whole tray,
    # never one per Quotation (same reasoning as api/facturacion.py's
    # own get_invoicing_queue()).
    company_types = _customer_company_types([q["customer"] for q in quotations])
    for q in quotations:
        q["fg_customer_company_type"] = company_types.get(q["customer"])

    return quotations


def _reference_selling_rates(item_codes, price_list):
    """Bulk lookup of the CURRENT Item Price for exactly these item_codes
    on `price_list` -- one query, never one Item Price read per line, same
    "bulk, never per-row" convention `api/inventario.py`'s own
    `_selling_rates()` already established (that exact function is not
    imported/reused here -- Inventario is out of scope for this commit).

    `price_list` is the CALLER's responsibility to supply -- always
    `qtn.selling_price_list`, the real commercial Price List that Quotation
    was actually priced against (Commit 25.13 review fix: never a
    hardcoded "Standard Selling" constant here anymore -- see
    `get_quotation_billing_detail()`'s own docstring for the full
    reasoning). NEVER reads `valuation_rate`/Standard Buying Item Price/any
    purchase-side field -- Facturación reviews what was SOLD for, compared
    to what the SAME selling catalog currently sells for, nothing about
    what it cost to acquire."""
    return reference_selling_rates(item_codes, price_list)  # shared: fabergray_erp/pricing.py


def _resolve_billing_review_warehouse(item_code, company, line_warehouse=None):
    """Commit 25.13 review fix -- the FULL native ERPNext warehouse
    precedence, not the cheaper "Item Default only" preview
    `api/ventas.py`'s own `_default_warehouse_for_item()` deliberately
    settles for (that function stays untouched, still used for its own,
    documented, lower-stakes purpose -- a pre-submit cart preview, never
    reused here anymore).

    Precedence, confirmed by reading `erpnext/stock/get_item_details.py`'s
    own `get_item_warehouse_()` directly (the real function ERPNext's
    pricing/item-detail pipeline calls during a real Sales Order/Quotation
    `.insert()`) -- this is that SAME precedence, reimplemented read-only
    since there is no document being inserted here to let resolve it
    natively:
      1. `line_warehouse` (the Quotation Item's own `warehouse`, if set --
         Cotizaciones never sets this today, `_validate_and_build_
         quotation_item_rows()` only ever allows `item_code`/`qty`, but a
         Quotation created some other way could carry one) -- ONLY if that
         Warehouse's own `company` matches `company` (never a Warehouse
         belonging to a different Company, no matter what the line says).
      2. Item Default (`get_item_defaults`, native, Company-scoped)
      3. Item Group Default (`get_item_group_defaults`, native,
         Company-scoped)
      4. Brand Default (`get_brand_defaults`, native, Company-scoped)
      5. Stock Settings.default_warehouse -- the one genuine native
         fallback below Company-scoped defaults -- ONLY if that
         Warehouse's own `company` also matches `company` (same guard
         `_validate_and_build_item_rows()`'s own docstring in
         api/ventas.py already documents for the identical native check).

    Company doctype itself has no generic "default_warehouse" field
    (confirmed directly against `erpnext/setup/doctype/company/
    company.json` -- only purpose-specific ones: `default_warehouse_for_
    sales_return`, `default_in_transit_warehouse`, `default_wip_
    warehouse`, `default_fg_warehouse`, `default_scrap_warehouse`, none of
    which apply here), so there is no separate "Company default" step
    between Brand Default and Stock Settings -- Stock Settings.
    default_warehouse (Company-checked) IS that fallback.

    Returns `None` if nothing in the whole chain resolves -- the caller
    renders that as "Sin definir", never guesses a warehouse."""
    if line_warehouse:
        line_company = frappe.db.get_value("Warehouse", line_warehouse, "company")
        if line_company == company:
            return line_warehouse
        # else: falls through to the defaults chain below -- never a
        # warehouse belonging to a different Company.

    item_defaults = get_item_defaults(item_code, company)
    if item_defaults.get("default_warehouse"):
        return item_defaults["default_warehouse"]

    item_group_defaults = get_item_group_defaults(item_code, company)
    if item_group_defaults.get("default_warehouse"):
        return item_group_defaults["default_warehouse"]

    brand_defaults = get_brand_defaults(item_code, company)
    if brand_defaults.get("default_warehouse"):
        return brand_defaults["default_warehouse"]

    stock_settings_warehouse = frappe.get_single_value("Stock Settings", "default_warehouse")
    if stock_settings_warehouse and frappe.get_cached_value("Warehouse", stock_settings_warehouse, "company") == company:
        return stock_settings_warehouse

    return None


def _expected_price_mode_rate(reference_rate, price_mode, row):
    """Hotfix 25.20.4 -- the `rate` a Quotation Item WOULD end up carrying
    if `apply_quotation_price_mode(price_mode)` ran right now, computed
    with exactly the same two-step arithmetic and the same per-field
    precisions ERPNext's own `calculate_item_rate()` uses (confirmed by
    reading `erpnext/controllers/taxes_and_totals.py` directly:
    `discount_amount = flt(rate_with_margin * discount_percentage / 100.0,
    precision("discount_amount"))`, then `rate = flt(rate_with_margin -
    discount_amount, precision("rate"))`, with `rate_with_margin ==
    price_list_rate` for a fresh row that carries no margin/pricing rule
    -- exactly the rows `apply_quotation_price_mode()` builds).

    Reimplementing the formula here (rather than calling ERPNext) is
    deliberate and safe BECAUSE it is only ever used to answer a
    yes/no question -- "would applying this mode change anything?"
    (`has_price_mode_changes()` below). It never writes a rate anywhere:
    `apply_quotation_price_mode()` still sets ONLY `discount_percentage`
    and lets ERPNext derive every rate natively, unchanged by this
    hotfix.

    The base is ALWAYS `reference_rate` -- the CURRENT Item Price on the
    Quotation's own `selling_price_list` -- never the line's already
    discounted `rate`, so a discount can never compound on a previous one
    (Commit 25.15, section 7's own rule, the same base
    `apply_quotation_price_mode()` itself relies on)."""
    return discounted_rate(  # shared: fabergray_erp/pricing.py, identical arithmetic
        reference_rate,
        PRICE_MODE_DISCOUNTS[price_mode],
        row.precision("discount_amount"),
        row.precision("rate"),
    )


def has_price_mode_changes(qtn, price_mode, reference_rates=None):
    """Hotfix 25.20.4 -- THE single source of truth for "would applying
    `price_mode` to `qtn` produce a REAL change in the persisted prices?",
    the one question "APLICAR PRECIOS" is enabled/disabled by.

    Compares, per line, against the DOCUMENT's own persisted economic
    reality -- never against the `fg_billing_price_mode` audit label.
    That label records the last explicit `apply_quotation_price_mode()`
    call and can disagree with the rates (a Quotation amended/re-priced
    another way afterwards, a label written before an Item Price moved, a
    historical document this mechanism never touched). When the label and
    the rates disagree, THE RATES WIN: a Quotation stamped "Descuento
    25%" whose lines still carry the undiscounted `rate` has NOT had 25%
    applied, and Facturación must still be able to apply it.

    Three persisted fields are compared, all three of which
    `apply_quotation_price_mode()` really would rewrite:

      * `rate` vs `_expected_price_mode_rate()` -- the economic number;
      * `price_list_rate` vs the CURRENT `reference_rate` -- a line whose
        stored base drifted from the live Item Price would genuinely be
        rewritten by an apply, even if `rate` happened to match;
      * `discount_percentage` vs `PRICE_MODE_DISCOUNTS[price_mode]`.

    Every comparison is rounded to the field's own Frappe/ERPNext
    precision first (`row.precision(...)`) -- never a raw float equality
    on values that went through a division.

    Returns False (nothing to apply) when a line has no reference price
    on `qtn.selling_price_list`: `apply_quotation_price_mode()` refuses
    that whole call outright, naming the item, so enabling the button
    there would only offer a guaranteed error.
    """
    if price_mode not in PRICE_MODE_DISCOUNTS:
        return False
    if not qtn.items:
        return False

    if reference_rates is None:
        reference_rates = _reference_selling_rates([row.item_code for row in qtn.items], qtn.selling_price_list)

    discount_percentage = PRICE_MODE_DISCOUNTS[price_mode]

    for row in qtn.items:
        reference_rate = reference_rates.get(row.item_code)
        if not reference_rate:
            return False

        rate_precision = row.precision("rate")
        if flt(row.rate, rate_precision) != _expected_price_mode_rate(reference_rate, price_mode, row):
            return True
        if flt(row.price_list_rate, rate_precision) != flt(reference_rate, rate_precision):
            return True

        discount_precision = row.precision("discount_percentage")
        if flt(row.discount_percentage, discount_precision) != flt(discount_percentage, discount_precision):
            return True

    return False


@frappe.whitelist()
def get_quotation_billing_detail(name):
    """Commit 25.13, sections 7/8 -- the full per-line review Facturación
    sees after opening one pending Quotation from the tray: quoted
    price/amount next to the current reference selling price (and their
    difference), plus READ-ONLY ERP availability (requested/available/
    warehouse/shortfall) -- never a physical count, explicitly labelled
    "Disponibilidad ERP" everywhere it is rendered (never "física"), per
    section 8's own explicit instruction: this never substitutes Bodega's
    own later physical check.

    Warehouse resolution (review fix, was "Item Default only" in this
    function's first draft): `_resolve_billing_review_warehouse()` above,
    the FULL native precedence (line warehouse -> Item Default -> Item
    Group Default -> Brand Default -> Stock Settings.default_warehouse),
    every step Company-checked, never a warehouse from another Company.
    Availability itself still reuses `get_actual_qty()` (erpnext.stock.
    doctype.pick_list.pick_list), the same Bin-reading helper
    `api/ventas.py`'s own `get_item_info()` and `api/bodega.py` already
    read qty_disponible through -- one source of truth, never a second
    one invented for this view. Strictly read-only: no `Bin`/`Stock
    Entry`/`Material Request`/reservation of any kind is ever written by
    this function -- it never calls anything but `get_actual_qty()`
    (itself a pure SELECT) and `frappe.get_list()`/`frappe.get_doc()`
    reads.

    Price reference (review fix, was a hardcoded "Standard Selling"
    constant in this function's first draft): reads `qtn.selling_price_list`
    -- the REAL Price List this Quotation was actually priced against
    (native field, resolved by ERPNext itself at `.insert()` time) -- and
    compares every line's quoted `rate` against THAT list's current Item
    Price, never a second, unrelated list. `price_list` is returned
    per-line so Facturación always knows exactly what was compared against
    what.

    Hotfix 25.20.4 adds `can_apply_price_mode` (bool) and
    `price_mode_changes` (`{price_mode: bool}`, one entry per
    `PRICE_MODE_DISCOUNTS` key) to the response -- the single, server-side
    answer to "may APLICAR PRECIOS be pressed for this mode?".  See
    `has_price_mode_changes()` above for why that answer is derived from
    the persisted `rate`/`price_list_rate`/`discount_percentage` and never
    from the `fg_billing_price_mode` audit label.  Still strictly
    read-only: computing them touches nothing but the same
    `_reference_selling_rates()` lookup this function already did.

    Commit 25.22 adds `fg_customer_company_type`, resolved fresh from the
    real Customer (`qtn.party_name`) -- `None` for a historical Customer
    never classified, the dialog renders that as "Sin clasificar" with a
    visible warning (section 9/11's own explicit requirement). Never
    modifies pricing/billing review in any way -- this is display only.
    """
    _facturacion_billing_review_gate()

    qtn = frappe.get_doc("Quotation", name)
    qtn.check_permission("read")
    assert_same_company(qtn)

    item_codes = [row.item_code for row in qtn.items]
    reference_rates = _reference_selling_rates(item_codes, qtn.selling_price_list)

    # Hotfix 25.20.4 -- same eligibility apply_quotation_price_mode() itself
    # enforces, re-derived from the document, never from whatever the tray
    # last showed.
    can_apply_price_mode = qtn.docstatus == 1 and qtn.get("fg_billing_review_status") == BILLING_REVIEW_PENDING

    items = []
    for row in qtn.items:
        reference_rate = reference_rates.get(row.item_code)
        warehouse = _resolve_billing_review_warehouse(row.item_code, qtn.company, row.warehouse)
        available_qty = flt(get_actual_qty(row.item_code, warehouse)) if warehouse else None
        shortage_qty = max(flt(row.qty) - available_qty, 0) if available_qty is not None else None

        items.append(
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty,
                "stock_uom": row.stock_uom,
                "rate": row.rate,
                "amount": row.amount,
                "reference_rate": reference_rate,
                # Hotfix 25.20.4 AUDIT (brief section 8) -- SEMANTICS
                # DELIBERATELY UNCHANGED. `rate_difference` is
                # "Actual - Base": how far the rate ALREADY PERSISTED on
                # this line sits from the CURRENT Item Price on the
                # Quotation's own selling_price_list. It is NOT, and never
                # was, "Actual - Ajustado" (the client-side preview of a
                # mode the user is merely hovering over -- that preview is
                # not persisted, so the server has nothing to difference
                # it against). $0,00 next to a -25% preview is therefore
                # CORRECT, not a bug: it says "this line is still quoted
                # at exactly catalog price" -- which is precisely why
                # APLICAR PRECIOS must be enabled there. The column header
                # in facturacion.js is relabelled "Dif. vs base" by this
                # hotfix so the number can no longer be misread as
                # belonging to the "Ajustado" column beside it; the
                # formula itself is untouched.
                "rate_difference": (flt(row.rate) - flt(reference_rate)) if reference_rate is not None else None,
                "price_list": qtn.selling_price_list,
                "warehouse": warehouse,
                "requested_qty": row.qty,
                "available_qty": available_qty,
                "shortage_qty": shortage_qty,
            }
        )

    return {
        "name": qtn.name,
        "customer": qtn.party_name,
        "customer_name": qtn.customer_name,
        "fg_customer_company_type": _customer_company_type(qtn.party_name),
        "transaction_date": qtn.transaction_date,
        "owner": qtn.owner,
        "owner_fullname": frappe.utils.get_fullname(qtn.owner),
        "status": qtn.status,
        "fg_billing_review_status": qtn.fg_billing_review_status,
        "fg_billing_review_note": qtn.fg_billing_review_note,
        # Commit 25.14 -- null for a Quotation never price-adjusted (never
        # backfilled/inferred) -- the UI derives "which of the 3 modes is
        # currently selected" itself by comparing each line's own
        # `rate`/`reference_rate` above, this is only the persisted
        # AUDIT record of the last explicit apply_quotation_price_mode()
        # call, section 13's own requirement.
        "fg_billing_price_mode": qtn.fg_billing_price_mode or None,
        "fg_billing_price_adjusted_by": qtn.fg_billing_price_adjusted_by or None,
        "fg_billing_price_adjusted_on": qtn.fg_billing_price_adjusted_on or None,
        # Hotfix 25.20.4 -- everything "APLICAR PRECIOS" needs to decide
        # enabled/disabled, computed HERE (server-side, from the document's
        # own persisted rates) so the browser never has to re-derive an
        # economic truth from a preview. `can_apply_price_mode` is the
        # eligibility half (submitted + still Pendiente de Facturación --
        # the exact same two conditions apply_quotation_price_mode() itself
        # re-validates; the role half is already enforced by this
        # function's own _facturacion_billing_review_gate() above, nobody
        # else ever gets this payload at all). `price_mode_changes` is the
        # "would it actually change anything" half, per mode --
        # has_price_mode_changes()'s own answer, never a comparison against
        # the `fg_billing_price_mode` audit label above. The frontend only
        # reads these; it never decides authorization, and
        # apply_quotation_price_mode() re-validates everything regardless.
        "can_apply_price_mode": can_apply_price_mode,
        "price_mode_changes": {
            mode: (can_apply_price_mode and has_price_mode_changes(qtn, mode, reference_rates))
            for mode in PRICE_MODE_DISCOUNTS
        },
        "item_count": len(qtn.items),
        "total_qty": qtn.total_qty,
        # Commit 25.14, section 4/10 -- native Quotation totals, straight
        # off the document (`total` = native subtotal before taxes,
        # `total_taxes_and_charges` = native tax total, `grand_total`/
        # `rounded_total` already existed above) -- ERPNext's own
        # `calculate_taxes_and_totals()` computed every one of these at
        # `.insert()` time, nothing here recomputes any of them.
        "total": qtn.total,
        "total_taxes_and_charges": qtn.total_taxes_and_charges,
        "grand_total": qtn.grand_total,
        "rounded_total": qtn.rounded_total,
        "items": items,
    }


@frappe.whitelist()
def apply_quotation_price_mode(quotation_name, price_mode):
    """Commit 25.14 (Commit 25.15 review fix, section 6 -- final closed set
    corrected to 10%/15%/20%/25%, "Descuento 5%" removed) -- Facturación
    selects one closed price scheme for the WHOLE Quotation ("Precio
    completo"/"Descuento 10%"/"Descuento 15%"/"Descuento 20%"/"Descuento
    25%") and this function applies it, server-side, to every line.

    `price_mode` MUST be one of `PRICE_MODE_DISCOUNTS`'s own keys
    ("FULL"/"DISCOUNT_10"/"DISCOUNT_15"/"DISCOUNT_20"/"DISCOUNT_25") --
    never an arbitrary percentage from the client, checked explicitly
    before anything else runs.

    PRICE BASE (section 2): the CURRENT Item Price on `qtn.selling_
    price_list` -- the real commercial Price List this Quotation was
    actually priced against (same `_reference_selling_rates()` helper
    `get_quotation_billing_detail()` already uses) -- NEVER
    `valuation_rate`/a Buying Item Price/the line's own current `rate`
    (which could already carry a previous discount -- using it as the new
    base would silently compound discounts, exactly what section 2
    forbids). Every line without a valid, non-zero reference price on
    that exact Price List blocks the WHOLE call outright, before any
    write happens, naming the specific item -- section 12's own explicit
    "no inventar precio, no usar 0 en silencio" rule.

    HOW THE DISCOUNT ACTUALLY GETS APPLIED (section 9/10/11) -- this is
    the one part of this function that does real math, and it is
    deliberately NOT "compute adjusted_rate ourselves and write it":
    every new item row is built with `item_code`/`qty`/`discount_
    percentage` only (`discount_percentage` = 0/5/10 straight from
    `PRICE_MODE_DISCOUNTS[price_mode]`) -- `rate`/`price_list_rate` are
    NEVER set here, left exactly as unset as every other write in this
    module already leaves them (`_validate_and_build_quotation_item_rows()`
    never accepts them either). Confirmed by reading `erpnext/controllers/
    taxes_and_totals.py::calculate_item_rate()` directly: for a fresh row
    with no pricing rules and `item.rate` falsy, ERPNext computes
    `item.rate = item.price_list_rate - (item.price_list_rate *
    item.discount_percentage / 100)` itself, using the price_list_rate
    IT resolves natively via `set_missing_item_details()` -> `get_item_
    details()` (the exact same native pipeline that already, correctly,
    handles UOM/`conversion_factor`/currency/`selling_price_list`/
    `transaction_date` for every other write in this module -- section 11
    is satisfied by construction, not by re-deriving any of that here).
    `discount_percentage == 0` (FULL) is not a special case either: that
    branch of `calculate_item_rate()` simply never adds a discount, so
    `rate` resolves straight to `price_list_rate` -- restoring the full
    price. `calculate_item_values()`/`calculate_taxes_and_totals()`
    (also native, also already the exact mechanism this module relies on
    for `create_and_submit_quotation()`) then derive `amount`, `net_rate`,
    `net_amount`, `total`, `net_total`, taxes, `grand_total`, `rounded_
    total` from that `rate` -- nothing here computes any of those by
    hand (section 10's own explicit instruction). This is how "10% never
    compounds on a previous 5%" (section 2's own explicit rule) is
    actually guaranteed: every call resolves `price_list_rate` FRESH from
    Item Price, every time, regardless of what `discount_percentage` the
    line carried a moment ago on the now-cancelled original.

    MECHANISM FOR A SUBMITTED QUOTATION (section 9): `Quotation Item.rate`/
    `discount_percentage`/`price_list_rate` have no `allow_on_submit`
    (confirmed the same way `modify_submitted_quotation()`'s own docstring
    already confirmed it for `qty`/`item_code` -- read directly against
    `erpnext/selling/doctype/quotation_item/quotation_item.json`), so a
    plain `.save()` cannot change them on a submitted document at all.
    This function reuses the EXACT SAME native cancel+amend
    `modify_submitted_quotation()` already established -- no third,
    parallel mechanism -- `qtn.cancel()` (no bypass) -> `frappe.copy_doc()`
    -> rebuild `items` -> `.insert()` + `.submit()`.

    WORKFLOW STATE (section 8/9/14): allowed ONLY while
    `fg_billing_review_status == "Pendiente de Facturación"` (the brief's
    own stated preference -- Aprobada/Borrador/Devuelta are all rejected
    outright, with a clear message) -- and, unlike
    `modify_submitted_quotation()`'s own `_reset_billing_review_on_edit()`
    (which drops back to "Borrador", a VENDEDORA edit invalidating
    review), the amended document here is set BACK to "Pendiente de
    Facturación" explicitly -- Facturación is still mid-review, adjusting
    price IS the review, this must never silently kick the Quotation out
    of her own tray or force the Vendedora to resend it. `fg_billing_
    reviewed_by`/`_reviewed_on` stay null (an approval/return never
    happened yet); never sets `fg_billing_review_status` to "Aprobada" --
    approving is `approve_quotation_billing()`'s own, separate, explicit
    action (section 5's own "NO aprobar automáticamente por ajustar
    precios").

    Returns `{"name": amended.name, "fg_billing_review_status":
    ..., "fg_billing_price_mode": ...}` -- the caller (facturacion.js)
    must re-point its own dialog state at this NEW name; the original
    `quotation_name` is now docstatus=2, superseded.
    """
    _require_login()
    _facturacion_billing_review_gate()

    if price_mode not in PRICE_MODE_DISCOUNTS:
        frappe.throw(_("Selecciona una modalidad de precio válida."))

    qtn = frappe.get_doc("Quotation", quotation_name)
    qtn.check_permission("cancel")
    assert_same_company(qtn)

    if qtn.docstatus != 1:
        frappe.throw(_("Solo se pueden ajustar precios de cotizaciones sometidas."))
    if qtn.get("fg_billing_review_status") != BILLING_REVIEW_PENDING:
        frappe.throw(_("Solo se pueden ajustar precios de cotizaciones pendientes de revisión de Facturación."))

    # Fail fast, before anything is cancelled: every line must have a
    # real, non-zero reference price on the Quotation's OWN Price List --
    # never a guessed/zero price (section 12).
    item_codes = [row.item_code for row in qtn.items]
    reference_rates = _reference_selling_rates(item_codes, qtn.selling_price_list)
    for row in qtn.items:
        if not reference_rates.get(row.item_code):
            frappe.throw(
                _('El producto {0} no tiene precio de referencia en la lista de precios "{1}".').format(
                    row.item_code, qtn.selling_price_list
                )
            )

    discount_percentage = PRICE_MODE_DISCOUNTS[price_mode]
    item_rows = [{"item_code": row.item_code, "qty": row.qty} for row in qtn.items]

    qtn.cancel()  # no ignore_permissions

    amended = frappe.copy_doc(qtn, ignore_no_copy=False)
    amended.docstatus = 0
    amended.amended_from = quotation_name
    amended.set("items", [])
    for row in item_rows:
        amended.append("items", {"item_code": row["item_code"], "qty": row["qty"], "discount_percentage": discount_percentage})

    # Stays Pendiente -- Facturación is still mid-review; never Borrador
    # (that would be _reset_billing_review_on_edit()'s own job, for a
    # VENDEDORA edit, never this one) and never Aprobada (approving is a
    # separate, explicit action).
    amended.fg_billing_review_status = BILLING_REVIEW_PENDING
    amended.fg_billing_reviewed_by = None
    amended.fg_billing_reviewed_on = None
    amended.fg_billing_price_mode = _PRICE_MODE_LABELS[price_mode]
    amended.fg_billing_price_adjusted_by = frappe.session.user
    amended.fg_billing_price_adjusted_on = now_datetime()

    # `fg_billing_price_mode_insert` -- set around this ONE call, nowhere
    # else in this codebase -- is what `guard_facturacion_quotation_
    # insert()`'s own `before_insert` doc_event checks to tell THIS
    # controlled amendment apart from any other insert attempt by a
    # Facturación-only user (Desk's own "New Quotation"/"Amend", a raw API
    # insert). Cleared in `finally` unconditionally, success or failure,
    # so it can never leak into a later, unrelated insert in the same
    # request/worker.
    frappe.flags.fg_billing_price_mode_insert = True
    try:
        amended.insert()
    finally:
        frappe.flags.fg_billing_price_mode_insert = False
    amended.submit()  # triggers ERPNext's own native calculate_taxes_and_totals(), nothing here recomputes it

    return {
        "name": amended.name,
        "fg_billing_review_status": amended.fg_billing_review_status,
        "fg_billing_price_mode": amended.fg_billing_price_mode,
    }


@frappe.whitelist()
def approve_quotation_billing(quotation_name, note=None):
    """Commit 25.13, section 9. Only Facturación (`_facturacion_billing_
    review_gate()`) may approve, only while `fg_billing_review_status ==
    "Pendiente de Facturación"` -- re-derived from the document itself,
    never trusted from whatever the tray last showed. Never creates a
    Sales Order/Pick List/reservation of any kind -- this function's own
    body calls nothing from api/ventas.py or any Fulfillment Engine
    module, matching the brief's own explicit instruction.
    """
    _require_login()
    _facturacion_billing_review_gate()

    qtn = frappe.get_doc("Quotation", quotation_name)
    qtn.check_permission("write")
    assert_same_company(qtn)

    if qtn.get("fg_billing_review_status") != BILLING_REVIEW_PENDING:
        frappe.throw(_("Solo se pueden aprobar cotizaciones pendientes de revisión de Facturación."))

    qtn.fg_billing_review_status = BILLING_REVIEW_APPROVED
    qtn.fg_billing_reviewed_by = frappe.session.user
    qtn.fg_billing_reviewed_on = now_datetime()
    qtn.fg_billing_review_note = (note or "").strip() or None
    qtn.fg_billing_review_revision = cint(qtn.fg_billing_review_revision) + 1
    qtn.save()  # no ignore_permissions

    return {"name": qtn.name, "fg_billing_review_status": qtn.fg_billing_review_status}


@frappe.whitelist()
def return_quotation_from_billing(quotation_name, reason=None):
    """Commit 25.13, section 10. `reason` is ALWAYS required -- rejected
    outright if empty/whitespace-only, server-side (never trusting a
    client-side-only check), matching `cancel_sales_order()`'s own
    `reason`-required convention (Commit 25.12). Only Facturación, only
    while `fg_billing_review_status == "Pendiente de Facturación"`, same
    gate/re-derivation as `approve_quotation_billing()` above.
    """
    _require_login()
    _facturacion_billing_review_gate()

    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Debes indicar un motivo para devolver la cotización."))

    qtn = frappe.get_doc("Quotation", quotation_name)
    qtn.check_permission("write")
    assert_same_company(qtn)

    if qtn.get("fg_billing_review_status") != BILLING_REVIEW_PENDING:
        frappe.throw(_("Solo se pueden devolver cotizaciones pendientes de revisión de Facturación."))

    qtn.fg_billing_review_status = BILLING_REVIEW_RETURNED
    qtn.fg_billing_reviewed_by = frappe.session.user
    qtn.fg_billing_reviewed_on = now_datetime()
    qtn.fg_billing_review_note = reason
    qtn.save()  # no ignore_permissions

    return {"name": qtn.name, "fg_billing_review_status": qtn.fg_billing_review_status}


def assert_quotation_approved_for_conversion(quotation_name):
    """Commit 25.13, section 13 -- reusable server-side guard for a FUTURE
    Quotation -> Sales Order conversion phase. IMPORTANT: no such
    conversion exists anywhere in this app as of this commit (confirmed
    during this commit's own audit -- `get_quotation_summary()`'s own
    docstring already documented it as a "future phase, out of scope";
    nothing in api/ventas.py or this module creates a Sales Order from a
    Quotation). This function is not `@frappe.whitelist()`-ed and is not
    called from anywhere yet -- it exists so that whenever that future
    conversion entry point IS built, it has one, single, already-tested
    guard to call FIRST, before creating anything, rather than trusting a
    hidden/disabled "Crear pedido" button alone (the brief's own explicit
    instruction, section 13: "NO confiar únicamente en ocultar/deshabilitar
    botón"). Raises `frappe.ValidationError` with the exact message the
    brief specifies unless `fg_billing_review_status == "Aprobada"`.
    """
    status = frappe.db.get_value("Quotation", quotation_name, "fg_billing_review_status")
    if status != BILLING_REVIEW_APPROVED:
        frappe.throw(_("La cotización debe ser aprobada por Facturación antes de crear el pedido."))


# =============================================================================
# Commit 25.15 -- "Fabrigray Cotización Comercial" Print Format (customer-
# facing commercial PDF).
# =============================================================================
# The Print Format itself (fixtures/print_format.json) is pure Jinja/HTML/CSS
# and carries NO business logic of its own -- every non-trivial resolution
# (contact phone/email off Contact's own child tables, Spanish long-form
# dates, per-line/aggregate tax breakdown off `item_wise_tax_details`, the
# "Aprobada"+docstatus+Company security gate) happens HERE, in
# `prepare_and_guard_quotation_pdf()`, a Quotation `before_print` doc_event
# (hooks.py) -- confirmed live by reading `frappe/www/printview.py`'s
# `get_rendered_template()` directly: `doc.run_method("before_print",
# print_settings)` runs for EVERY print path (Desk's own print preview,
# `frappe.utils.print_format.download_pdf()`, `frappe.get_print()`) since
# they all funnel through that one function -- there is no separate,
# un-hooked path a user could hit instead. The template only ever reads
# `doc.fg_pdf_*`/`row.fg_pdf_*` attributes this function computes -- it never
# re-derives a phone/address/tax value itself, so there is exactly one place
# in this codebase that can get any of this wrong.
#
# `frappe.form_dict.get("format")` is what scopes this hook to ONLY this one
# Print Format -- confirmed live by reading `frappe/utils/print_utils.py`'s
# `get_print()` directly: it sets `local.form_dict.format = print_format`
# before rendering, for the ENTIRE duration of the request, regardless of
# entry point. Any OTHER print format ever applied to Quotation (native
# "Standard", or a future internal-only one) returns immediately, computes
# nothing, and is completely unaffected by any of this.
PDF_PRINT_FORMAT_NAME = "Fabrigray Cotización Comercial"

_SPANISH_MONTHS = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}


def _format_date_es_long(date_value):
    """"10 de septiembre de 2026" -- built from a fixed Spanish month-name
    table, deliberately never `frappe.utils.formatdate()`/babel locale
    formatting (which follows the SITE's configured language, not
    necessarily Spanish, and is one more moving part to depend on for a
    single, fixed, always-Spanish commercial document). Returns `None` for
    a null date -- the template shows "—", never invents a date."""
    if not date_value:
        return None
    date_value = frappe.utils.getdate(date_value)
    return f"{date_value.day} de {_SPANISH_MONTHS[date_value.month]} de {date_value.year}"


def _resolve_pdf_contact(qtn):
    """CONTACTO/TELÉFONO/CORREO (section 6) -- resolved from the REAL
    Contact record (`qtn.contact_person`), never trusted purely from
    `qtn.contact_mobile`/`contact_email` (both native Quotation fields,
    snapshotted at insert time -- confirmed live on a real approved
    Quotation in this site that BOTH were empty strings while the linked
    Contact's own `phone_nos` child table carried a real phone). Falls back
    to the Quotation's own snapshot fields only if the Contact itself can't
    be read; returns (name, phone, email), each `None` if genuinely absent
    -- never invented.

    Phone: prefers `is_primary_mobile_no`, then `is_primary_phone`, then
    the first `phone_nos` row -- same "prefer explicit primary, else first"
    convention as `_resolve_pdf_address()`'s own Customer-address fallback
    below. Email: prefers a row flagged `is_primary` in `email_ids`, else
    the first one.
    """
    name = qtn.contact_display or None
    phone = qtn.contact_mobile or None
    email = qtn.contact_email or None

    if not qtn.contact_person or not frappe.db.exists("Contact", qtn.contact_person):
        return name, phone, email

    contact = frappe.get_doc("Contact", qtn.contact_person)
    name = contact.full_name or name

    if contact.phone_nos:
        primary = next((r for r in contact.phone_nos if r.is_primary_mobile_no), None)
        primary = primary or next((r for r in contact.phone_nos if r.is_primary_phone), None)
        primary = primary or contact.phone_nos[0]
        phone = primary.phone or phone

    if contact.email_ids:
        primary = next((r for r in contact.email_ids if r.is_primary), None)
        primary = primary or contact.email_ids[0]
        email = primary.email_id or email

    return name, phone, email


def _resolve_pdf_address(qtn):
    """DIRECCIÓN (section 6) -- `qtn.address_display` is the REAL address
    this Quotation was actually addressed to at the moment it was created
    (native field, point-in-time, never re-derived live) -- used first,
    always. Only if the Quotation itself never carried one (confirmed live:
    true for every Quotation in this site today -- no Customer here has any
    Address at all yet) does this fall back to the Customer's own current
    primary address, read fresh via ERPNext's own native
    `get_address_display()` (erpnext/../address.py) -- the same formatter
    ERPNext itself uses everywhere else, never a hand-built string. `None`
    if neither resolves -- the template shows "—", never invents one."""
    if qtn.address_display:
        return qtn.address_display

    customer_address = frappe.db.get_value(
        "Customer", qtn.party_name, ["customer_primary_address", "primary_address"], as_dict=True
    )
    address_name = (customer_address and (customer_address.customer_primary_address or customer_address.primary_address)) or None
    if not address_name or not frappe.db.exists("Address", address_name):
        return None

    from frappe.contacts.doctype.address.address import get_address_display

    return get_address_display(frappe.get_doc("Address", address_name).as_dict()) or None


def _resolve_pdf_advisor_name(qtn):
    """ASESORA/ASESOR COMERCIAL (section 15) -- `qtn.owner`, the user who
    actually created/submitted this Quotation, is this app's own real
    identity of "who quoted this" (no separate Sales Person/Employee
    concept is used anywhere else in this app for the Vendedora role --
    confirmed by grepping this whole module and api/ventas.py). Resolved to
    `User.full_name`.

    Explicit exclusion, section 15's own instruction: "NO mostrar
    'Administrator' si existe una identidad comercial real asociada" --
    read literally, the safe direction when NO real identity exists (true
    for every Quotation in this site today -- confirmed live, the
    "Vendedora" role is currently held only by "Administrator", every named
    test Vendedora being an ephemeral test fixture) is to show NOTHING
    rather than the literal word "Administrator" on a customer-facing PDF.
    Returns `None` in that case -- the template omits the signature block
    entirely rather than printing a placeholder name."""
    if not qtn.owner or qtn.owner == "Administrator":
        return None
    full_name = frappe.db.get_value("User", qtn.owner, "full_name")
    if not full_name or full_name == "Administrator":
        return None
    return full_name


def _resolve_pdf_company_info(company):
    """Company masthead/footer fields (sections 4/16) -- every one of these
    read fresh off the real `Company` record (`company_logo`/`tax_id`/
    `phone_no`/`email`), NEVER the reference image's own hardcoded example
    values ("Calle 46 Numero 22-28"/"6533068"/"3118814375"/
    "mercadeo@fabrigraysas.com") -- confirmed live: the real `fabrigraysas`
    Company record has ALL FOUR null today, and has no Address linked to it
    either (`Dynamic Link` query against it returns zero rows) -- a real,
    reportable configuration gap, not a resolver bug (see this commit's own
    STOP AND REPORT). Returns a plain dict; every value `None` when
    genuinely unset -- the template shows "—" for each, never a guess."""
    comp = frappe.get_cached_doc("Company", company)

    address_display = None
    address_name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
        "parent",
    )
    if address_name:
        from frappe.contacts.doctype.address.address import get_address_display

        address_display = get_address_display(frappe.get_doc("Address", address_name).as_dict()) or None

    logo_url = comp.company_logo or None
    if logo_url and logo_url.startswith("/"):
        logo_url = frappe.utils.get_url(logo_url)

    return {
        "logo_url": logo_url,
        "tax_id": comp.tax_id or None,
        "phone": comp.phone_no or None,
        "email": comp.email or None,
        "address_display": address_display,
    }


def _build_pdf_tax_context(qtn):
    """IVA/VALOR IVA per line (section 8) + the totals-section breakdown
    (section 13) -- BOTH read from `qtn.item_wise_tax_details` (native
    Table field on Quotation, child doctype "Item Wise Tax Detail":
    `item_row`/`tax_row`/`rate`/`amount`/`taxable_amount`, confirmed by
    reading `erpnext/controllers/taxes_and_totals.py` and the child
    doctype's own JSON directly) -- the one native structure that captures
    the REAL, possibly-per-item-different effective rate (Item Tax Template
    overrides can make two lines under the same Quotation carry different
    rates even under one Sales Taxes and Charges Template), which a plain
    read of `qtn.taxes` (one row per tax ACCOUNT, not per rate) cannot
    reliably reproduce. NEVER hardcodes "Exe"/"5%"/"19%" -- a line with NO
    entry here prints "Exe" (genuinely exempt/no tax template resolved,
    matching every line on every real Quotation in this site today, since
    no Company Sales Taxes and Charges Template is actually applied to any
    of them yet); a line WITH an entry prints its own real `rate` (`0%`
    included, distinct from "Exe").

    Sets `row.fg_pdf_tax_label`/`fg_pdf_tax_amount`/`fg_pdf_line_total`
    directly on each `Quotation Item` row (ephemeral Python attributes,
    never saved -- `before_print` always runs on a throwaway in-memory
    `doc`) so the template itself does zero lookup/grouping of its own.
    Returns the totals-section list, `[{"label": "IVA {rate}%", "amount":
    ...}, ...]` sorted by rate, or a single `{"label": "IVA", "amount": 0}`
    when the whole document has no tax entries at all -- section 13's own
    "pueden mostrarse por separado SI los datos reales lo soportan",
    Subtotal/TOTAL GENERAL always come straight from `qtn.total`/
    `qtn.grand_total` (never recomputed here)."""
    by_item_row = {}
    by_rate = {}
    for detail in qtn.item_wise_tax_details or []:
        by_item_row.setdefault(detail.item_row, []).append(detail)
        by_rate[detail.rate] = by_rate.get(detail.rate, 0) + flt(detail.amount)

    for row in qtn.items:
        entries = by_item_row.get(row.name)
        if not entries:
            row.fg_pdf_tax_label = "Exe"
            row.fg_pdf_tax_amount = 0
        else:
            rate = entries[0].rate
            row.fg_pdf_tax_label = f"{rate:g}%"
            row.fg_pdf_tax_amount = sum(flt(e.amount) for e in entries)
        row.fg_pdf_line_total = flt(row.amount) + flt(row.fg_pdf_tax_amount)

    if not by_rate:
        return [{"label": "IVA", "amount": 0}]
    return [{"label": f"IVA {rate:g}%", "amount": amount} for rate, amount in sorted(by_rate.items())]


def _assert_quotation_pdf_eligible(qtn):
    """Commit 25.15 review fix -- the ONE, shared "is this Quotation
    eligible for the Fabrigray commercial PDF" check, called from THREE
    independent places: `get_fabrigray_quotation_pdf_view_url()` (VER PDF),
    `download_fabrigray_quotation_pdf()` (DESCARGAR PDF), and
    `prepare_and_guard_quotation_pdf()` (the `before_print` hook, kept as
    defense-in-depth -- see that function's own docstring for why it is
    deliberately NOT the only place this runs any more). Company isolation:
    `frappe.has_permission()` (called by `validate_print_permission()` just
    before Desk's own print pipeline reaches `before_print`, and NOT called
    at all by the two dedicated endpoints below before they do their own
    `check_permission()`) only checks role-level Custom DocPerm grants in
    this app -- there is no `has_permission` doctype hook registered for
    Quotation (only `permission_query_conditions`, which filters LIST
    queries, never a single-document check) -- so `assert_same_company()`
    is what actually stops a cross-Company read here, exactly like every
    write path in this module."""
    qtn.check_permission("read")
    assert_same_company(qtn)

    if qtn.docstatus == 2:
        frappe.throw(
            _(
                "No se puede generar el PDF comercial de una versión cancelada de la cotización. "
                "Genera el PDF desde la versión vigente y aprobada."
            )
        )
    if qtn.get("fg_billing_review_status") != BILLING_REVIEW_APPROVED:
        frappe.throw(_("La cotización debe estar aprobada por Facturación antes de generar el PDF comercial."))


def _build_quotation_pdf_context(qtn):
    """Computes every `fg_pdf_*` display field the "Fabrigray Cotización
    Comercial" Print Format's own Jinja template reads -- called ONLY by
    `prepare_and_guard_quotation_pdf()` (the `before_print` hook), never
    directly by either dedicated endpoint (they never render/return HTML
    themselves, only validate then delegate to Frappe's own native
    print/PDF pipeline, which is what triggers `before_print` in turn)."""
    qtn.fg_pdf_number = qtn.name
    qtn.fg_pdf_date_display = _format_date_es_long(qtn.transaction_date)
    qtn.fg_pdf_valid_till_display = _format_date_es_long(qtn.valid_till)
    qtn.fg_pdf_nit = frappe.db.get_value("Customer", qtn.party_name, "tax_id") or None

    contact_name, contact_phone, contact_email = _resolve_pdf_contact(qtn)
    qtn.fg_pdf_contact_name = contact_name
    qtn.fg_pdf_contact_phone = contact_phone
    qtn.fg_pdf_contact_email = contact_email

    qtn.fg_pdf_address_display = _resolve_pdf_address(qtn)

    qtn.fg_pdf_payment_terms = None
    if qtn.payment_terms_template:
        qtn.fg_pdf_payment_terms = (
            frappe.db.get_value("Payment Terms Template", qtn.payment_terms_template, "template_name")
            or qtn.payment_terms_template
        )

    qtn.fg_pdf_advisor_name = _resolve_pdf_advisor_name(qtn)

    company_info = _resolve_pdf_company_info(qtn.company)
    qtn.fg_pdf_company_logo_url = company_info["logo_url"]
    qtn.fg_pdf_company_tax_id = company_info["tax_id"]
    qtn.fg_pdf_company_phone = company_info["phone"]
    qtn.fg_pdf_company_email = company_info["email"]
    qtn.fg_pdf_company_address = company_info["address_display"]

    qtn.fg_pdf_tax_groups = _build_pdf_tax_context(qtn)


def prepare_and_guard_quotation_pdf(qtn, method=None, print_settings=None):
    """Quotation `before_print` doc_event (hooks.py) -- Commit 25.15
    review fix: this hook is DELIBERATELY NOT the only security boundary
    any more (it was, in this commit's own first draft) -- see
    `get_fabrigray_quotation_pdf_view_url()`/`download_fabrigray_
    quotation_pdf()` below, the two dedicated, explicitly-controlled
    endpoints VER PDF/DESCARGAR PDF actually call, each running
    `_assert_quotation_pdf_eligible()` itself BEFORE ever touching
    Frappe's own print pipeline. This hook remains as defense-in-depth
    (never `ignore_permissions`/a weaker check anywhere else could ever
    reach the real PDF content without going through it too, e.g. Desk's
    own native print preview, reached by neither dedicated endpoint) --
    it is NOT redundant busywork, it is the backstop for every path this
    app's own two controlled endpoints do not cover.

    Scoped to ONLY the "Fabrigray Cotización Comercial" format -- NEVER a
    blanket block on printing a Quotation with any other format (section
    1's own explicit correction: the first draft's `before_print`
    registration looked global -- e.g. a future internal/administrative
    Print Format for a Borrador/Pendiente Quotation must keep working
    exactly as before, completely untouched by this). Confirmed
    robust, live, by reading `frappe/utils/print_utils.py::get_print()`
    directly: it sets `local.form_dict.format = print_format` before
    rendering, for the ENTIRE duration of the request, for every call
    into Frappe's print pipeline (Desk preview, `download_pdf()`, a
    direct `frappe.get_print()` call, including both of THIS module's own
    two calls below) -- there is no code path into `get_rendered_
    template()`/`before_print` in this whole codebase that does not set
    it first. The one theoretical gap (a bare `/printview` request with NO
    explicit `format` param, which falls back to `meta.default_print_
    format`) does not apply here: nothing in this app's fixtures ever sets
    Quotation's own DocType-level default print format, confirmed by
    grepping `fixtures/property_setter.json` -- if that ever changes, this
    scoping check would need to change with it. Pinned by
    `test_cotizaciones_pdf.py::test_f_other_print_format_never_blocked_
    globally`.
    """
    if frappe.form_dict.get("format") != PDF_PRINT_FORMAT_NAME:
        return

    _assert_quotation_pdf_eligible(qtn)
    _build_quotation_pdf_context(qtn)


@frappe.whitelist()
def get_fabrigray_quotation_pdf_view_url(quotation_name):
    """Commit 25.15 review fix, section 2/4 -- VER PDF's own dedicated,
    explicitly-controlled endpoint. Runs the full eligibility check
    (`_assert_quotation_pdf_eligible()`: permission, Company, docstatus,
    "Aprobada") BEFORE returning anything -- the client never freely
    builds a `/printview` URL from just a name (which WOULD skip every one
    of those checks up front, relying only on `before_print` firing
    correctly downstream); it calls this endpoint first, and only opens
    the URL this function hands back. The Print Format name itself is
    HARDCODED here (`PDF_PRINT_FORMAT_NAME`), never accepted as a
    parameter -- a caller cannot smuggle in a different, arbitrary print
    format through this endpoint."""
    _require_login()
    qtn = frappe.get_doc("Quotation", quotation_name)
    _assert_quotation_pdf_eligible(qtn)

    from urllib.parse import quote

    return frappe.utils.get_url(
        "/printview?doctype=Quotation&name="
        + quote(qtn.name)
        + "&format="
        + quote(PDF_PRINT_FORMAT_NAME)
        + "&no_letterhead=1&trigger_print=0"
    )


@frappe.whitelist()
def download_fabrigray_quotation_pdf(quotation_name):
    """Commit 25.15, section 23/24 (review fix, section 2/4) -- DESCARGAR
    PDF's own dedicated, explicitly-controlled endpoint. Runs the full
    eligibility check (`_assert_quotation_pdf_eligible()`) itself, FIRST,
    explicitly -- never relies solely on `prepare_and_guard_quotation_
    pdf()` (the `before_print` hook, kept as a second, independent layer,
    see its own docstring) to catch an ineligible document. Section 23's
    own explicit "preferir mecanismo nativo de Frappe... NO inventar un
    motor PDF propio" is honored literally: the actual PDF generation
    below is 100% native -- `frappe.utils.print_format.download_pdf()`
    itself, the exact same `frappe.get_print()` -> wkhtmltopdf pipeline.
    `format` is HARDCODED to `PDF_PRINT_FORMAT_NAME` here too -- never a
    client-supplied parameter, so a caller cannot request a different
    print format through this endpoint either.

    This function's own SECOND job -- purely cosmetic, not security --
    is the download filename: `download_pdf()` hardcodes it to
    `f"{name}.pdf"` with no parameter to override it (confirmed by reading
    `frappe/utils/print_format.py` directly) -- section 24's own
    "Cotizacion-Fabrigray-<quotation_name>.pdf" naming needs one more line
    after it. No PDF bytes are touched, read, or re-generated here --
    `frappe.local.response.filecontent` is exactly what the native call
    already produced; only `.filename` is overwritten afterward, sanitized
    the identical way the native function already sanitizes its own
    default (`" " -> "-"`, `"/" -> "-"`)."""
    _require_login()
    qtn = frappe.get_doc("Quotation", quotation_name)
    _assert_quotation_pdf_eligible(qtn)
    from frappe.utils.print_format import download_pdf

    download_pdf(doctype="Quotation", name=quotation_name, format=PDF_PRINT_FORMAT_NAME, no_letterhead=1)

    safe_name = quotation_name.replace(" ", "-").replace("/", "-")
    frappe.local.response.filename = f"Cotizacion-Fabrigray-{safe_name}.pdf"


# =============================================================================
# Commit 25.17 -- "ENVIAR A PEDIDOS": converts an Aprobada Quotation into a
# real Sales Order, reusing ERPNext's own native Quotation -> Sales Order
# mapper (`erpnext.selling.doctype.quotation.quotation.make_sales_order()`)
# end to end -- never a hand-built field-by-field mapping. Confirmed live,
# by reading that mapper directly, that it already does everything this
# commit's brief asks for, natively:
#   - `get_mapped_doc()`'s own default same-fieldname copy carries `rate`/
#     `price_list_rate`/`discount_percentage`/`amount`/`item_code`/
#     `item_name`/`description`/`qty`/`uom`/`conversion_factor`/`warehouse`/
#     `company`/`customer`/`transaction_date`/`currency`/`conversion_rate`/
#     `selling_price_list` from Quotation (Item) to Sales Order (Item)
#     automatically -- nothing here re-specifies any of it.
#   - `Quotation Item -> Sales Order Item` sets `prevdoc_docname` (parent
#     Quotation name) and `quotation_item` (source row name) on every Sales
#     Order Item -- ERPNext's own NATIVE Quotation<->Sales Order link, the
#     same one `Quotation.get_ordered_status()` itself already reads. No
#     Custom Field is added for this relationship.
#   - `Sales Taxes and Charges` rows are copied too (`"reset_value": True`).
#   - the linked Customer is reused as-is (`_make_customer()`, internal to
#     the mapper: `quotation_to == "Customer"` -- true for every Quotation
#     this app ever creates, see `create_and_submit_quotation()` -- returns
#     the EXISTING `frappe.get_doc("Customer", party_name)` directly, never
#     creates one).
#   - the already-copied `rate`/`price_list_rate`/`discount_percentage` are
#     NEVER overwritten by the mapper's own trailing `calculate_taxes_and_
#     totals()` call -- confirmed by reading `AccountsController.
#     set_missing_item_details()` directly: a field already set (non-None)
#     on the target row is left untouched unless its name is in that
#     function's own `force_item_fields` tuple, which contains
#     `item_group`/`brand`/`stock_uom`/`is_fixed_asset`/`pricing_rules`/
#     `weight_per_unit`/`weight_uom`/`total_weight`/`valuation_rate` --
#     `rate`/`price_list_rate`/`discount_percentage` are NOT in that tuple.
#     So the already-Aprobada rate is the one and only number that ends up
#     on the Sales Order -- no fresh Item Price lookup, no re-applied
#     10/15/20/25% discount, no compounding.
# =============================================================================


def _existing_sales_order_for_quotation(quotation_name):
    """The ONE idempotency check `create_sales_order_from_quotation()` (and
    every card read, `get_my_quotations()`/`get_quotation_detail()` above)
    relies on -- a single indexed lookup on `Sales Order Item.
    prevdoc_docname` (the native link `make_sales_order()` itself sets),
    filtered to `docstatus != 2` so a genuinely CANCELLED Sales Order never
    blocks a fresh one from being created again (Frappe mirrors a parent's
    own `docstatus` onto every one of its child rows automatically -- no
    join back to the Sales Order itself needed). Same raw-`frappe.db`
    "does a related document already exist" convention `get_my_orders()`
    already uses for its own amend-chain-tip check
    (`frappe.db.exists("Sales Order", {"amended_from": name})`) -- existence
    only, never any economic content. Returns `{"name", "status"}` or
    `None`."""
    so_name = frappe.db.get_value(
        "Sales Order Item", {"prevdoc_docname": quotation_name, "docstatus": ["!=", 2]}, "parent"
    )
    if not so_name:
        return None
    return {"name": so_name, "status": frappe.db.get_value("Sales Order", so_name, "status")}


@frappe.whitelist()
def create_sales_order_from_quotation(quotation_name):
    """Commit 25.17 -- "ENVIAR A PEDIDOS". Server-side only: the client
    sends nothing but `quotation_name` -- no `customer`/`items`/`rate`/
    `discount`/`tax` of any kind is ever accepted here (there is no such
    parameter to accept), matching this whole module's own standing
    "Vendedora never sends an economic field" convention. Every value that
    ends up on the Sales Order comes from re-reading the Quotation itself,
    fresh, server-side, via the native mapper -- see this section's own
    module-level comment above for the full audit of what that mapper
    already does and why nothing here duplicates it.

    Validations, all re-derived from the document itself, never trusted
    from a hidden button/stale client state (same "the button being
    hidden is not the security boundary" convention `send_quotation_to_
    billing()`/`approve_quotation_billing()` already establish):
    Quotation exists (`frappe.get_doc()` raises `DoesNotExistError`
    otherwise) -> `check_permission("read")` -> `assert_same_company()` ->
    `docstatus == 1` (never a Draft, never an old, cancelled amendment --
    section 7's own explicit concern: an old `docstatus == 2` version of a
    Quotation can never reach this far, its own `fg_billing_review_status`
    might still misleadingly read "Aprobada", frozen from before it was
    superseded, but `docstatus` alone already excludes it here) ->
    `fg_billing_review_status == "Aprobada"` -> has a customer -> has at
    least one item -> every qty > 0.

    Idempotency (section 6): `_existing_sales_order_for_quotation()` runs
    BEFORE the mapper is ever touched -- if a non-cancelled Sales Order
    already traces back to this exact Quotation, it is returned as-is
    (`already_exists: true`), the mapper is never called a second time, no
    second Sales Order is ever created. A residual, narrow race (two
    genuinely concurrent requests both passing this check before either
    finishes inserting) is not closed by an explicit lock here -- same
    honest "not solved by a custom lock" note `confirm_order()`
    (api/ventas.py) already carries for its own, analogous race, for the
    identical reason: Frappe's own `Document.insert()`/`.submit()` still
    run under real permission/validation checks either way, and a genuine
    double-click in practice reaches this function sequentially, not
    concurrently.

    No stock/availability validation of any kind (section 15 -- Bodega
    determines physical shortages during picking, not this endpoint, same
    policy `create_and_submit_quotation()`/`create_and_submit_sales_order()`
    already establish); no Material Request; no `Bin` read or write.

    `.insert()` then `.submit()` -- never `ignore_permissions`, never a
    `db_set`/manual `docstatus` write. `.submit()` alone is what triggers
    `Sales Order.on_submit` (hooks.py) -> the exact same Fulfillment Engine
    entrypoint (`process_sales_order_for_confirmation()`) every other
    Sales Order in this app already goes through -- Bodega sees this order
    exactly like any other, no second pipeline.

    `fg_billing_review_status` is never touched here (section 13) -- the
    Quotation stays "Aprobada", permanently, as its own historical/auditable
    commercial record, completely independent of whether a Sales Order was
    ever created from it.

    Returns `{"quotation", "sales_order", "status", "already_exists"}` --
    no economic field, matching section 18's own proposed shape.
    """
    _require_login()
    frappe.has_permission("Sales Order", "create", throw=True)

    qtn = frappe.get_doc("Quotation", quotation_name)
    qtn.check_permission("read")
    assert_same_company(qtn)

    if qtn.docstatus != 1:
        frappe.throw(_("Solo se puede enviar a pedidos una cotización sometida y vigente."))
    if qtn.get("fg_billing_review_status") != BILLING_REVIEW_APPROVED:
        frappe.throw(_("La cotización debe estar aprobada por Facturación antes de enviarla a pedidos."))
    if not qtn.party_name:
        frappe.throw(_("La cotización debe tener un cliente."))
    if not qtn.items:
        frappe.throw(_("La cotización debe tener al menos un producto."))
    for row in qtn.items:
        if flt(row.qty) <= 0:
            frappe.throw(_("La cantidad debe ser mayor a cero para {0}.").format(row.item_code))

    existing = _existing_sales_order_for_quotation(qtn.name)
    if existing:
        return {
            "quotation": qtn.name,
            "sales_order": existing["name"],
            "status": existing["status"],
            "already_exists": True,
        }

    from erpnext.selling.doctype.quotation.quotation import make_sales_order

    so = make_sales_order(qtn.name)
    # Quotation Item's own `warehouse` is never set (Vendedora's item
    # allowlist is `{"item_code", "qty"}` only, see `create_and_submit_
    # quotation()`) -- but `get_mapped_doc()`'s default same-fieldname
    # copy still carries that EMPTY STRING over onto the Sales Order Item
    # row verbatim. That matters: native `set_missing_item_details()`
    # only fills a field whose current value `is None` -- an empty
    # string reads as "already set" and is left alone, so the warehouse
    # native precedence chain `create_and_submit_sales_order()` already
    # relies on (Item Default -> Item Group -> Brand -> Stock Settings)
    # would otherwise never run at all, and `.insert()` would reject the
    # row outright ("Source warehouse required for stock item ...").
    # Normalizing "" -> None here, before `.insert()` (which re-runs
    # `set_missing_item_details()` natively on its own, same as any other
    # Sales Order in this app), is what lets that same native resolution
    # actually happen -- never a hand-picked warehouse of this function's
    # own choosing.
    for row in so.items:
        if not row.warehouse:
            row.warehouse = None
    # Quotation has no `delivery_date` field of its own -- the mapper's own
    # default same-fieldname copy leaves the mapped Sales Order's
    # `delivery_date` unset, which native `validate_delivery_date()`
    # otherwise rejects outright ("Please enter Delivery Date"). Same
    # `DEFAULT_DELIVERY_LEAD_DAYS` (7) lead time api/ventas.py's own
    # `create_and_submit_sales_order()` already uses, computed off the
    # mapped Sales Order's own `transaction_date` (copied from the
    # Quotation, possibly long in the past) rather than `nowdate()` -- so
    # this can never fail native's own "delivery date must be after the
    # order date" check regardless of how old the approved Quotation is.
    so.delivery_date = add_days(so.transaction_date, DEFAULT_DELIVERY_LEAD_DAYS)
    so.insert()  # no ignore_permissions
    so.submit()  # triggers on_submit -> process_sales_order_for_confirmation() (Fulfillment Engine)

    return {"quotation": qtn.name, "sales_order": so.name, "status": so.status, "already_exists": False}
