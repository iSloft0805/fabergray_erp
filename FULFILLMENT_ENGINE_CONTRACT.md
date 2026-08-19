# Fulfillment Engine -- extension contract (Commit 7, revised Commit 9, extended Commit 12)

**This document does not implement the Fulfillment Engine.** It records the
contract a future engine must follow so it can create `Reporte de Faltante`
records safely, using code that already exists, without duplicating any
validation. Nothing here creates Material Requests, Purchase Orders or Work
Orders, reserves stock, writes to Sales Order, or automates
purchasing/production. It also does not modify `apps/frappe` or
`apps/erpnext`.

## The one entry point: `_insert_shortage_report()`

Commit 9 split what used to be a single Pick-List-shaped function into a
generic core plus an adapter, specifically so this contract's "no Pick List
required" promise is actually true today, not just documented as a future
gap:

```python
from fabergray_erp.api.bodega import _insert_shortage_report

_insert_shortage_report(
    item_code,              # str
    warehouse,               # str
    qty_solicitada,           # float
    qty_disponible,           # float: quantity actually available/found
    detected_by,              # "Bodega" | "Fulfillment Engine"
    sales_order=None,         # str | None
    material_request=None,    # str | None
    pick_list=None,           # str | None -- NOT required
    pick_list_item=None,      # str | None -- NOT required
    shortage_reason=None,     # only meaningful/mandatory for detected_by="Bodega"
    resolution_note=None,
)
```

This is now **the only function in the app allowed to build a
`frappe.get_doc({"doctype": "Reporte de Faltante", ...})` dict and call
`.insert()` on it** -- enforced by an automated test
(`test_only_one_reporte_de_faltante_insert_path_exists`, AST-based, fails CI
if a second insert path is ever added). It takes already-resolved values; it
has no idea whether they came from a Pick List row, a Sales Order Item, or
anywhere else, and never will. **A future Fulfillment Engine calls this
function directly** with `detected_by="Fulfillment Engine"`, its own
`sales_order`/`item_code`/`warehouse`/quantities, and `pick_list=None` /
`pick_list_item=None` -- proven to work end-to-end today by
`test_insert_shortage_report_core_accepts_fulfillment_engine_without_pick_list`
and `test_insert_shortage_report_core_links_sales_order_without_pick_list`
(both in `reporte_de_faltante/test_reporte_de_faltante.py`). **Never write a
second creator, and never call `frappe.get_doc({"doctype": "Reporte de
Faltante", ...}).insert()` directly from engine code** -- every guarantee
below only holds because this one function is the sole entry point.

What it guarantees, today, unconditionally:
- Every field is taken exactly as passed -- this function does no
  derivation itself (that is each adapter's job, see below) and does not
  accept anything beyond its named parameters.
- Permission is checked for real: `frappe.has_permission("Reporte de
  Faltante", "create", throw=True)`. A future engine must run as a user
  that actually has create permission on this doctype (today: System
  Manager, Bodega -- not Jefe de Bodega, which is read/write on *existing*
  reports only). No new role/user is created yet; deciding what identity the
  engine runs as is future work.
- Exactly one document is inserted. Never touches Stock Ledger, Bin, Sales
  Order, Material Request, Purchase Order or Work Order. No stock
  reservation, no side effects beyond the one `insert()`.
- Returns the new document's `name` and nothing else.

## The Pick List adapter: `_create_shortage_report()`

```python
from fabergray_erp.api.bodega import _create_shortage_report

_create_shortage_report(
    pick_list_doc,        # a loaded frappe.get_doc("Pick List", ...)
    row,                   # one row from pick_list_doc.get("locations")
    qty_disponible,        # float: quantity actually available/found
    shortage_reason=None,  # only meaningful for detected_by="Bodega"
    detected_by="Bodega",  # "Bodega" | "Fulfillment Engine"
    resolution_note=None,
)
```

Unchanged public behaviour since Commit 4/7 (used by `report_shortage()`,
the physical-discrepancy path from `/app/bodega`), but internally it is now
*only* a derivation layer: it reads `item_code`/`warehouse`/`sales_order`/
`material_request`/`pick_list`/`pick_list_item`/`qty_solicitada` off the
validated Pick List row and its parent, then calls
`_insert_shortage_report()` with those values. It does not insert anything
itself. A future Sales-Order-based adapter (e.g.
`_create_shortage_report_from_sales_order_item()`) would follow the exact
same shape: derive fields from its own source document, call
`_insert_shortage_report()`, insert nothing on its own.

## `detected_by` contract

| Value | Meaning | Set by |
|---|---|---|
| `Bodega` | Physical discrepancy a warehouse employee found while picking | `api.bodega.report_shortage()`, human-initiated from `/app/bodega` |
| `Fulfillment Engine` | Shortage detected upstream, without a person physically finding it on the floor | Reserved for the future engine -- **no production code path sets this today**; the core function's contract for it is verified by tests only (Commit 9) |

`Reporte de Faltante.validate()` (`reporte_de_faltante.py`) enforces:
`shortage_reason` is mandatory **only** when `detected_by == "Bodega"`. This
already exists (Commit 2) and fires automatically on `insert()` -- a future
engine caller must not re-implement or duplicate this check; it can simply
omit `shortage_reason` when calling with `detected_by="Fulfillment Engine"`.

## `shortage_reason` contract

Select field on `Reporte de Faltante`, current options:
`Stock físico no encontrado`, `Stock insuficiente`, `Producto dañado`,
`Error de inventario`, `Compra pendiente`, `Producción pendiente`, `Otro`.

These are written from a human's point of view ("what did the person on the
floor observe"), so they are only meaningful -- and only mandatory -- for
`detected_by="Bodega"`. A `Fulfillment Engine` caller should leave this
field blank. If the engine eventually needs its own categorized reasons,
that is a new, separate vocabulary to design later, not a reason to reuse
or repurpose these options.

## `status` -- out of scope here

New reports default to `Abierto` (doctype default). Moving one to `En
Proceso`/`Resuelto` is a human decision today (Jefe de Bodega already has
write access to existing reports, unchanged since Commit 1/2). This commit
does not add any automated status transition.

## Explicit non-goals (Commits 7 and 9 alike)

- No Fulfillment Engine implementation -- no automatic detection, no
  trigger, no job.
- No Material Request, Purchase Order or Work Order creation.
- No stock reservation, no Stock Settings changes.
- No Sales Order changes.
- No purchasing/production automation.
- No changes to `apps/frappe` or `apps/erpnext`.
- No new validation logic -- `_insert_shortage_report()` enforces nothing
  beyond what the doctype's own `validate()` already did; Commit 9 only
  restructured *who* is allowed to call `.insert()`, not what's allowed to
  be inserted.

## Commit 12 -- the read-only Fulfillment Analyzer

`fabergray_erp/fulfillment/analyzer.py`, `analyze_sales_order(sales_order)`,
is the first piece of code that actually looks like a "Fulfillment Engine"
-- but it still creates nothing. Given one submitted Sales Order, it returns
per-line what can be picked now, what is short, and (only when short)
whether the shortfall should route to Purchase or Manufacture. It never
calls `.insert()`, `.save()`, `.submit()`, or `_insert_shortage_report()`.
Nothing yet calls this module from a hook, a job, or a UI action.

**Availability formula** -- deliberately NOT `Bin.actual_qty`, and NOT
Stock Reservation Entry (ruled out by Commits 10/11: both block Pick List
submission in this app's workflow). It reproduces the exact rule ERPNext's
own `create_pick_list()` already applies for free when suggesting locations
for a new Pick List: `Bin.actual_qty` (via the same `get_actual_qty()`
helper `api/bodega.py` already uses) minus whatever other **open** (not
Completed/Cancelled) Pick Lists already claim for that item+warehouse. The
real exclusion logic lives in `Pick List._get_pick_list_items()`
(`pick_list.py`), a private instance method with no public equivalent --
calling it would mean instantiating a throwaway Pick List just to reach it,
with no cross-version compatibility guarantee. `analyzer.py` reproduces its
query field-for-field instead (`_qty_committed_by_open_pick_lists()`),
documented inline as to why it isn't just called directly.

```
qty_available_for_pick = max(Bin.actual_qty - qty_committed_by_other_open_pick_lists, 0)
qty_shortage           = max(qty_remaining - qty_available_for_pick, 0)
```

Nothing here is a Custom Field or a cache -- both are recomputed from
`Bin`/`Pick List Item` on every call.

**Purchase/Manufacture routing** -- reuses `Item.default_material_request_type`
(native, already existed; no `fg_procurement_policy` field was added) and
ERPNext's own `get_default_bom()` (`erpnext.stock.get_item_details`) for BOM
resolution. `Manufacture` with no resolvable default BOM is never silently
downgraded to `Purchase` -- it comes back `procurement_route="Blocked"`,
`blocking_reason="Missing BOM"`, so bad master data surfaces instead of
triggering the wrong kind of order. Any `default_material_request_type`
other than `Purchase`/`Manufacture` (`Material Transfer`, `Material Issue`,
`Customer Provided`) is out of this V1's scope and also comes back
`Blocked`, for the same reason: guessing is worse than surfacing.

**Make-or-Buy -- minimal proposal for a later phase (not implemented).**
ERPNext has no native "should this item be bought or made even though its
policy allows both" decision -- `default_material_request_type` is a fixed
per-item policy, not a per-situation choice. Inventing an optimization
engine now (lead time vs. stock-out cost vs. supplier price breaks) would be
premature. The minimal, additive proposal for whenever this becomes a real
need: keep `Item.default_material_request_type` as the V1 routing decision
exactly as this commit does, and only *if* a genuine make-or-buy need shows
up later, add a **per-Sales-Order-Item override** (not a new Item-level
field) -- e.g. a way for a human or a future rule to say "for this one line,
Purchase instead of the Item's default Manufacture" -- read by the analyzer
before falling back to `Item.default_material_request_type`, never
replacing it. This keeps the Item master's policy as the single source of
truth and avoids adding decision logic before there is a concrete, observed
case that needs it.

**Behaviour with open Pick Lists** -- proven with a live test
(`test_availability_excludes_qty_committed_by_another_open_pick_list`):
a still-draft Pick List that has picked part of an item's stock reduces
what the analyzer reports as available for a *different*, competing Sales
Order, exactly like it already reduces what `create_pick_list()` offers a
new Pick List. A submitted Pick List's picked-but-undelivered quantity
keeps being excluded too (`picked_qty - delivered_qty`), proven by
`test_partially_picked_sales_order_reflects_real_picked_qty` -- stock a
Pick List has already claimed does not become "available" again just
because that Pick List is now submitted; only a Delivery Note releases it.
