# -*- coding: utf-8 -*-
"""Commit 19.4 -- thin Purchase Receipt.on_submit handler, wired via
hooks.py `doc_events` (see hooks.py). No `apps/erpnext` modification, no
Purchase Receipt class override, no Server Script -- plain doc_events
entry, same extension point sales_order_hooks.py already uses.

DEPRECATED as of Commit 25.4's own architecture audit -- kept as a removal
candidate, deliberately NOT redesigned, NOT deleted this session. The
approved way to resolve a Bodega-reported shortage is a native Stock
Entry (api.jefe_bodega.receive_shortage_purchase(), Commit 22.8) -- never
a Material Request -> Purchase Order -> Purchase Receipt chain (this app
has no Proveedores/Supplier module for a real Purchase Receipt to name a
Supplier; see receive_shortage_purchase()'s own module docstring for that
original reasoning). Since Commit 25.4 also stopped wiring Sales Order.
on_submit to sync_material_requests_for_sales_order(), nothing in this
app's real flow ever populates Purchase Receipt Item.sales_order any
more -- this hook is a confirmed, empirically-verified live no-op today
(see test_purchase_receipt_hooks.py's own module docstring and
test_on_submit_is_a_noop_for_a_real_purchase_receipt_unrelated_to_any_
sales_order). Left in place rather than removed because it is provably
harmless even if manually triggered on a Sales Order that went through
the real confirm flow (test_reprocessing_a_confirmed_order_via_the_
legacy_pipeline_is_a_safe_no_op, same file) -- the full-demand Pick
List's own "already claimed" accounting neutralizes process_sales_
order() by construction. Do not build new functionality on top of this
file; removing it (together with purchase_service.py and the "purchasing"
step of process_sales_order()) is a separate, later decision.

Zero fulfillment logic of its own, on purpose -- the only thing this
module does is identify which Sales Orders the just-submitted Purchase
Receipt's own rows reference (native `Purchase Receipt Item.sales_order`,
confirmed populated end to end through the MR->PO->PR chain of native
mappers -- see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 19.4" for the full
audit) and, for each DISTINCT one, calls
`fulfillment.engine.process_sales_order()` (Commits 15/19.1/19.2)
exactly once. Every rule about what happens next -- whether new stock
lets Bodega pick more, whether a Reporte de Faltante shrinks or
resolves, whether a Material Request still needs the same or a smaller
remainder -- already lives in `process_sales_order()`'s own four
composed services; nothing here duplicates or second-guesses any of it.

Confirmed live, not assumed, before wiring this in (Commit 19.4's own
audit): `PurchaseReceipt.on_submit()` (purchase_receipt.py:376) runs
`update_stock_ledger()` (which is what actually updates `Bin.actual_qty`)
INSIDE the native controller method, and Frappe's own `Document.hook()`
(`document.py:1631-1647`, the same mechanism traced for Sales Order in
Commit 16) always runs the full controller method to completion BEFORE
any `doc_events` handler -- so by the time this module's `on_submit`
runs, the received stock is already visible to `analyze_sales_order()`
exactly the way a later, independent read would see it. Traced live with
a temporary hook capturing `Bin.actual_qty` (both via `get_actual_qty()`
and raw SQL) at the exact moment a doc_events handler fires: already
reflected the just-submitted receipt, every time.
"""

from fabergray_erp.fulfillment.engine import process_sales_order


def _distinct_sales_orders_for(doc):
    """Every Sales Order this Purchase Receipt's own rows reference,
    de-duplicated, in first-seen order. Reads ONLY the native
    `Purchase Receipt Item.sales_order` field already loaded on `doc` --
    no query, no lookup by item_code/customer/warehouse, no guessing.
    A row with no `sales_order` (e.g. a Purchase Receipt built without
    going through the Material Request -> Purchase Order -> Purchase
    Receipt chain, or an unrelated purchase) is silently skipped, never
    treated as "figure out which order this might belong to."""
    seen = {}
    for row in doc.items:
        if row.sales_order and row.sales_order not in seen:
            seen[row.sales_order] = True
    return list(seen)


def on_submit(doc, method=None):
    for sales_order in _distinct_sales_orders_for(doc):
        process_sales_order(sales_order)
