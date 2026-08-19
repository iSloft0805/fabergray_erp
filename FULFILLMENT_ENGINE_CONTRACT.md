# Fulfillment Engine -- extension contract (Commit 7)

**This document does not implement the Fulfillment Engine.** It records the
contract a future engine must follow so it can create `Reporte de Faltante`
records safely, using code that already exists, without duplicating any
validation. Nothing in this commit creates Material Requests, Purchase
Orders or Work Orders, reserves stock, writes to Sales Order, or automates
purchasing/production. It also does not modify `apps/frappe` or
`apps/erpnext`.

## The one entry point: `_create_shortage_report()`

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

Defined in `fabergray_erp/api/bodega.py`, first written in Commit 4 for
`report_shortage()` (the physical-discrepancy path used by `/app/bodega`).
It was deliberately written generic from the start -- `detected_by` is a
parameter, not hardcoded -- specifically so a future engine could reuse it
instead of a second implementation. **Reuse it as-is. Do not write a second
`Reporte de Faltante` creator, and do not call `frappe.get_doc({"doctype":
"Reporte de Faltante", ...}).insert()` directly from engine code** -- every
guarantee below only holds because this one function is the sole entry
point.

What it guarantees, today, unconditionally:
- `item_code`, `warehouse`, `sales_order`, `material_request` are always
  *derived* from `row` / `pick_list_doc` -- never accepted as free-form
  input from the caller.
- Permission is checked for real: `frappe.has_permission("Reporte de
  Faltante", "create", throw=True)`. A future engine must run as a user
  that actually has create permission on this doctype (today: System
  Manager, Bodega -- not Jefe de Bodega, which is read/write on *existing*
  reports only). No new role/user is created by this commit; deciding what
  identity the engine runs as is future work.
- Exactly one document is inserted. Never touches Stock Ledger, Bin, Sales
  Order, Material Request, Purchase Order or Work Order. No stock
  reservation, no side effects beyond the one `insert()`.
- Returns the new document's `name` and nothing else.

### Known limitation -- not solved by this commit

`_create_shortage_report()` requires an already-loaded `Pick List` document
and one of its `locations` rows. It only covers a shortage detected
*against an existing Pick List*. A shortage detected upstream -- e.g. while
looking at a Material Request before any Pick List exists -- has no
supported entry point yet. That needs a deliberate signature extension (or
a sibling function) in a later commit; it is a documented gap, not
something to work around by bypassing this function or hand-rolling a
second insert path.

## `detected_by` contract

| Value | Meaning | Set by |
|---|---|---|
| `Bodega` | Physical discrepancy a warehouse employee found while picking | `api.bodega.report_shortage()`, human-initiated from `/app/bodega` |
| `Fulfillment Engine` | Shortage detected upstream, without a person physically finding it on the floor | Reserved for the future engine -- **no code path sets this today** |

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

## Explicit non-goals of this commit

- No Fulfillment Engine implementation.
- No Material Request, Purchase Order or Work Order creation.
- No stock reservation.
- No Sales Order changes.
- No purchasing/production automation.
- No changes to `apps/frappe` or `apps/erpnext`.
- No new validation logic -- everything above already existed; this commit
  only writes down the contract and points at it from the code.
