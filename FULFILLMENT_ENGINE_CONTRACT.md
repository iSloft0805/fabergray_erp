# Fulfillment Engine -- extension contract (Commit 7, revised Commit 9)

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
