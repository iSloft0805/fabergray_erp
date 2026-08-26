# -*- coding: utf-8 -*-
"""Commit 22.4 -- read-only API layer for the future Page Inventario
(not built yet -- deferred to a later commit), used by Bodega/Jefe de
Bodega/System Manager. This commit's own setup: 9 Custom DocPerm read
grants on Item/Bin/Item Price/Stock Ledger Entry -- versioned in
fixtures/custom_docperm.json + fixtures/system_manager_custom_docperm.json
(a separate file, deliberately: filtering the main export by
role="System Manager" alone also matched unrelated Custom DocPerm rows
from other ERPNext localizations that have nothing to do with this app).
Every grant was demonstrated missing via a real has_permission() audit
under real restricted sessions before being created -- none assumed.
Warehouse read was NOT granted to anyone here: Bin.warehouse (a plain
field on an already-permitted doctype) is enough for everything this
module returns, no separate Warehouse permission is exercised.

Scope, exactly as approved: read-only. No Page yet (a later commit), no
Opening Stock, no writes of any kind -- this module never touches
Bin.actual_qty, Stock Ledger Entry, Stock Reconciliation, Item Group, or
stock_uom. Existencia from the Access Excel is out of scope entirely.

Same conventions as api/clientes.py/ventas.py/bodega.py: every read goes
through frappe.get_list()/frappe.get_doc()+check_permission()/
frappe.has_permission(throw=True), never frappe.get_all() (bypasses
permissions), never ignore_permissions=True, never frappe.set_user()
outside of tests, never frappe.db.sql()/frappe.db.commit().

Stock is always aggregated with ONE grouped frappe.get_list() query over
Bin (_bin_totals() below), never one query per Item -- the same "bulk
read, never N+1" rule api/bodega.py's own get_inventory() already
follows for its own Bin reads.

low_stock is deliberately null (with an explicit low_stock_status =
"not_configured") -- no reorder level exists yet for any migrated Item
(Stock Minimo/Maximo from Access were never imported, approved as a
later commit, 22.6), so there is no threshold to compute this from that
would not be invented.
"""

import frappe
from frappe.utils import cint, flt

from fabergray_erp.api.bodega import _require_login

PRICE_LIST = "Standard Selling"

LOW_STOCK_STATUS = "not_configured"

_ITEM_LIST_FIELDS = ["item_code", "item_name", "item_group", "stock_uom", "disabled"]

_STATUS_FILTERS = {
    "all": {},
    "active": {"disabled": 0},
    "disabled": {"disabled": 1},
}


def _bin_totals():
    """One grouped query -- {item_code: total_actual_qty} across every
    warehouse for every item that has at least one Bin row. Never one
    query per Item."""
    rows = frappe.get_list(
        "Bin",
        fields=["item_code", {"SUM": "actual_qty", "as": "total_qty"}],
        group_by="item_code",
        limit_page_length=0,
    )
    return {r.item_code: flt(r.total_qty) for r in rows}


def _selling_rates(item_codes):
    """One bulk query for exactly the item_codes on the current page --
    never one Item Price lookup per row."""
    if not item_codes:
        return {}
    rows = frappe.get_list(
        "Item Price",
        filters={"price_list": PRICE_LIST, "item_code": ["in", item_codes]},
        fields=["item_code", "price_list_rate"],
    )
    return {r.item_code: r.price_list_rate for r in rows}


@frappe.whitelist()
def get_inventory_summary():
    """KPI counts for the future Page Inventario dashboard header.

    low_stock is null + low_stock_status="not_configured" -- approved
    explicitly: no reliable reorder level exists for any migrated Item
    yet, and no threshold is invented here to fill that gap."""
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    references = frappe.get_list("Item", pluck="name")
    totals = _bin_totals()
    total_stock = sum(totals.values())

    stock_items = frappe.get_list("Item", filters={"is_stock_item": 1}, pluck="name")
    out_of_stock = sum(1 for code in stock_items if totals.get(code, 0) <= 0)

    return {
        "references": len(references),
        "total_stock": total_stock,
        "out_of_stock": out_of_stock,
        "low_stock": None,
        "low_stock_status": LOW_STOCK_STATUS,
    }


@frappe.whitelist()
def get_inventory_items(txt=None, status=None, start=0, page_length=20):
    """List + search, combined (same shape as api/clientes.py's own
    search_customers()): txt="" for the plain paginated list, txt set for
    search-as-you-type.

    status: "all" | "active" | "disabled" | "out_of_stock" -- the first
    three are plain Item.disabled filters, pushed to the database and
    paginated there. "out_of_stock" has no native Item field to filter
    on (it is derived from the Bin aggregate), so -- exactly like
    api/clientes.py's own "incomplete" tab -- it fetches every matching
    Item once (still ONE query, never per-item), filters in Python
    against the same single Bin aggregate _bin_totals() already
    computed, and paginates the filtered list in Python. An unrecognized
    status falls back to "all"."""
    _require_login()
    frappe.has_permission("Item", "read", throw=True)

    txt = txt or ""
    status = status or "all"
    start = cint(start)
    page_length = cint(page_length) or 20

    totals = _bin_totals()

    or_filters = None
    if txt:
        or_filters = [["item_code", "like", f"%{txt}%"], ["item_name", "like", f"%{txt}%"]]

    if status == "out_of_stock":
        all_matching = frappe.get_list(
            "Item",
            or_filters=or_filters,
            fields=_ITEM_LIST_FIELDS,
            order_by="item_code asc",
            limit_page_length=0,
        )
        rows = [i for i in all_matching if totals.get(i.item_code, 0) <= 0]
        total = len(rows)
        page_rows = rows[start : start + page_length]
    else:
        filters = dict(_STATUS_FILTERS.get(status, {}))
        page_rows = frappe.get_list(
            "Item",
            filters=filters,
            or_filters=or_filters,
            fields=_ITEM_LIST_FIELDS,
            order_by="item_code asc",
            limit_start=start,
            limit_page_length=page_length,
        )
        total = len(frappe.get_list("Item", filters=filters, or_filters=or_filters, pluck="name"))

    codes = [r.item_code for r in page_rows]
    rates = _selling_rates(codes)

    items = [
        {
            "item_code": r.item_code,
            "item_name": r.item_name,
            "item_group": r.item_group,
            "stock_uom": r.stock_uom,
            "disabled": r.disabled,
            "total_actual_qty": totals.get(r.item_code, 0),
            "selling_rate": rates.get(r.item_code),
        }
        for r in page_rows
    ]

    return {"items": items, "total": total}


@frappe.whitelist()
def get_inventory_item_detail(item_code):
    """Full read-only detail for one Item: header, Standard Selling
    price, total stock, per-warehouse breakdown (Bin rows), and the 20
    most recent Stock Ledger Entry movements -- via frappe.get_doc() +
    check_permission("read") for the header (real record-level
    permission), frappe.get_list() for everything else (bulk, permission-
    aware reads, never a raw SQL join)."""
    _require_login()

    doc = frappe.get_doc("Item", item_code)
    doc.check_permission("read")

    bin_rows = frappe.get_list(
        "Bin",
        filters={"item_code": item_code},
        fields=["warehouse", "actual_qty", "reserved_qty", "projected_qty"],
        order_by="warehouse asc",
    )
    total_stock = sum(flt(r.actual_qty) for r in bin_rows)

    price_rows = frappe.get_list(
        "Item Price",
        filters={"item_code": item_code, "price_list": PRICE_LIST},
        fields=["price_list_rate", "currency"],
        limit_page_length=1,
    )
    selling_rate = price_rows[0].price_list_rate if price_rows else None
    currency = price_rows[0].currency if price_rows else None

    recent_movements = frappe.get_list(
        "Stock Ledger Entry",
        filters={"item_code": item_code},
        fields=[
            "posting_date",
            "posting_time",
            "warehouse",
            "actual_qty",
            "qty_after_transaction",
            "voucher_type",
            "voucher_no",
        ],
        order_by="posting_date desc, posting_time desc, creation desc",
        limit_page_length=20,
    )

    return {
        "item_code": doc.item_code,
        "item_name": doc.item_name,
        "item_group": doc.item_group,
        "stock_uom": doc.stock_uom,
        "disabled": doc.disabled,
        "is_stock_item": doc.is_stock_item,
        "selling_rate": selling_rate,
        "currency": currency,
        "total_stock": total_stock,
        "stock_by_warehouse": bin_rows,
        "recent_movements": recent_movements,
    }
