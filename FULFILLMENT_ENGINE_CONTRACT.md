# Fulfillment Engine -- extension contract (Commit 7, revised Commit 9, extended Commit 12/13/14)

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

## Commit 13 -- the first write: create_pick_list_for_available_stock()

`fabergray_erp/fulfillment/pick_list_service.py`,
`create_pick_list_for_available_stock(sales_order)`, closes Sales Order ->
`analyze_sales_order()` -> Pick List -> `/app/bodega`. It creates exactly
one native Pick List (ERPNext's own `create_pick_list()` mapper -- the same
function behind the "Create Pick List" button and already used by this
app's own test fixtures), capped at what is genuinely pickable, and hands
it back already inserted so the existing Bodega flow
(`get_queue`/`get_pick_list`/`start_picking`/`set_picked_qty`/
`report_shortage`/`finish_picking`) operates on it completely unchanged.
Still not wired to `on_submit`, a hook, a job, or any Page -- callable only
directly, exactly like `analyze_sales_order()` itself.

**Per-line quantity:**
```
qty_still_needed = max(qty_remaining - qty_already_claimed_by_open_pick_lists_for_this_so_item, 0)
qty_to_pick      = min(qty_still_needed, qty_available_for_pick)
```
`qty_remaining` and `qty_available_for_pick` come straight from
`analyze_sales_order()` -- no parallel availability formula. The one new
piece Commit 13 adds is `qty_already_claimed_by_open_pick_lists_for_this_so_item`
(`_qty_already_claimed_by_open_pick_lists_for_so_item()`), and it exists
for a real reason, not by accident: `analyze_sales_order()`'s
`qty_remaining` is deliberately delivery-only (Commit 12 proved this on
purpose -- it does not shrink just because *some* Pick List already exists
for the line, since a shortage still needs to be visible as a shortage
even while a Pick List is in flight for part of it). Commit 13's own
idempotency needs the opposite question answered: how much of *this exact
line* has already been handed to an open Pick List, so a second run never
hands out the same paper twice. Answered via the same native relation
(`Pick List Item.sales_order_item`) and the same open-Pick-List query
shape the analyzer already uses -- reused, not reinvented, and scoped
narrower (one Sales Order Item, not one item+warehouse across every Sales
Order) because that is a genuinely different question from the one the
analyzer answers.

Discovered the hard way, via a failing test caught before commit rather
than assumed correct: without this per-line cap, a Sales Order with an
existing *open* (unsubmitted) Pick List for part of its stock, followed by
more stock arriving, would let a second run claim up to the full remaining
*ordered* quantity from the newly-available stock -- on top of what the
first Pick List already claimed -- over-committing beyond what the
customer actually ordered. `create_pick_list()`'s own native
`qty - max(picked_qty, delivered_qty)` target formula has no visibility
into a *sibling still-open* Pick List for the same line (only into
`picked_qty`, which doesn't move until submit); this app has to close that
gap itself for its own idempotency, since ERPNext's own mapper doesn't.

**Behaviour with existing Pick Lists (all proven by tests):**
- an existing *open* (draft) Pick List for the same line: a second run
  offers only the genuine remainder -- current availability minus what
  that draft already claims, capped by what the order still needs net of
  that claim;
- an existing *submitted* Pick List: `Sales Order Item.picked_qty` has
  advanced, and the submitted row's own `picked_qty - delivered_qty` is
  excluded the same way an open draft's `stock_qty` would be;
- a partially delivered Sales Order: `qty_remaining` already reflects
  `delivered_qty`, so a re-run never re-offers what already shipped;
- two Sales Orders competing for the same item+warehouse: unaffected by
  the per-line cap (it is scoped to one `sales_order_item`, so a
  competing order's own open Pick List never counts against it) -- the
  cross-order protection is exactly `analyze_sales_order()`'s own
  `qty_available_for_pick`, unchanged from Commit 12.

**Idempotency, in one sentence:** no new technical field was added
(matching the standing instruction from Commit 9's architectural ruling to
prefer native relations/deterministic checks over a dedicated idempotency
field until proven necessary) -- `Pick List Item.sales_order_item`,
`picked_qty`, `delivered_qty`, and Pick List `status` are enough, and
every required scenario has a passing test proving it.

**Commit 13 -- known concurrency window (documented, not closed).**
`Pick List.before_save()` (`pick_list.py`) unconditionally re-runs
`set_item_locations()` against live state right before writing, every
time the document is saved -- not only at `create_pick_list()`'s build
time. `create_pick_list_for_available_stock()` uses this: it checks for an
empty `locations` table *after* `insert()`, not before, and deletes the
draft it just created if the live re-derivation at write time found
nothing left. A test
(`test_concurrency_race_self_corrects_within_one_connection_not_proof_of_true_concurrency_safety`)
proves this self-corrects two calls sharing one database
connection/transaction -- which is all `bench run-tests` can actually
drive.

What this does **not** prove, and what remains genuinely open: two
independent, truly concurrent database transactions each get their own
MVCC read snapshot under Frappe/MySQL's default isolation. Neither has to
see the other's not-yet-committed (or even already-committed-after-its-
own-snapshot) insert. Two real concurrent callers could each
independently compute "N units available" and both successfully insert,
over-committing stock across two different Pick Lists for a moment. This
is not unique to this function -- the native "Create Pick List" button has
the exact same unlocked read-then-write race today, for the exact same
reason. The one native mechanism that closes this kind of race cleanly
(`Stock Reservation Entry.get_available_qty_to_reserve()`, which does use
`for_update()` row locking) was already evaluated and rejected in Commits
10/11, specifically because reserving via either Sales Order or Pick List
blocks this app's own Pick List submission outright. Bolting a lock onto
only this function, when the native mapper it depends on does not use one
and the one mechanism that does was ruled out for other reasons, would not
actually close the window (a concurrent call to the plain "Create Pick
List" button would still race unprotected) -- so, per the explicit
instruction to avoid ad hoc fixes for a window that cannot be closed
cleanly in this commit, none was added. The practical consequence if this
window is ever hit is bounded and already has a safety net: Bodega's
physical pick would come up short of what the Pick List suggests, and the
existing, already-tested `report_shortage()`/`finish_picking()` disclosure
flow (Commit 8) is exactly the mechanism that already handles "the paper
said more than what's really on the shelf."

## Commit 14 -- automatic shortage detection: sync_shortage_reports_for_sales_order()

`fabergray_erp/fulfillment/shortage_service.py`,
`sync_shortage_reports_for_sales_order(sales_order)`, closes the other half
of Commit 13's flow: Sales Order -> `analyze_sales_order()` -> stock
disponible -> Pick List (Commit 13) **and** `qty_shortage > 0` -> Reporte
de Faltante (Commit 14). Every report it creates or updates goes through
`_insert_shortage_report()` (Commit 9's one approved insert path) or a
plain `.save()` on a report this function already found -- never a second
insert path (enforced by a new AST guardrail test mirroring the existing
one for `api/bodega.py`).

**`sales_order_item` -- decision: add it, as `Data`, not `Link`.**
`Reporte de Faltante` had no way to reference one exact Sales Order Item
row before this commit -- `item_code` + `warehouse` alone is ambiguous the
moment the same Item appears on two lines of the same Sales Order (e.g.
two different destination warehouses). Added `sales_order_item` (`Data`,
hidden, read-only -- same style as the existing `pick_list_item` reference,
for consistency with an already-established pattern in this exact
doctype, not a Link, for the same reason that field isn't one either).
Used for both idempotency (Commit 14) and traceability -- and wired
through `_create_shortage_report()` too (the Bodega/Pick List adapter),
since `Pick List Item.sales_order_item` is already a native field
populated by `create_pick_list()`'s own mapper, so Bodega-created reports
get the same exact-line reference at zero extra cost, with zero change to
when or how they're created. Existing reports (created before this
commit) simply have this field empty -- nothing required it, nothing
breaks.

**Idempotency:** `_find_open_engine_report()` looks up an existing report
by `sales_order_item` + `detected_by="Fulfillment Engine"` +
`status in (Abierto, En Proceso)` -- a single, unambiguous native relation,
no hash. Reports created by Bodega are structurally invisible to every
query in this module (all scoped to `detected_by="Fulfillment Engine"`),
so they are never read, updated, or resolved by this service.

**Update vs. resolve, for V1 (as instructed):** if the shortage for a line
is still open but its quantity changed (8 -> 3), the existing open Engine
report is updated in place (`qty_solicitada`/`qty_disponible`/
`shortage_reason`), not resolved-and-recreated -- one continuous "episode"
stays one document, with `track_changes` (already on this doctype since
Commit 2) recording the history. If a line's shortage clears entirely, its
open Engine report is marked `Resuelto` with an automatic, evidence-bearing
note (`"disponible (X) ya cubre lo solicitado (Y)"`) -- Bodega-created
reports are never touched by this transition.

**A real interaction with Commit 13, found the same way Commit 13's own
gap was found -- via a failing test, not assumed correct.** The literal
field mapping given for this commit (`qty_solicitada = qty_remaining`,
`qty_disponible = qty_available_for_pick`, straight from
`analyze_sales_order()`) breaks exactly the required integration
invariant ("Pick List qty + shortage qty = the line's real pending need")
the moment this service runs *after* `create_pick_list_for_available_stock()`
has already claimed part of a line -- which is the realistic, expected
order of operations, and the one the mandatory integration test exercises.
Reason: `qty_remaining` is deliberately delivery-only (Commit 12, on
purpose) and does not shrink just because an open Pick List already
claims part of it, so a line fully claimed by a fresh Pick List still
shows a raw `qty_shortage > 0` -- correct for the analyzer's own read-only
purpose, wrong if copied verbatim into a report that's about to tell
someone to buy or manufacture stock that is, in truth, already sitting in
a Pick List waiting for delivery.

Fixed the same way Commit 13 fixed its own analogous gap -- reusing (via
plain Python import, zero modification to `pick_list_service.py`) its
`_qty_already_claimed_by_open_pick_lists_for_so_item()`:
```
qty_pending               = max(qty_remaining - qty_already_claimed_by_open_pick_lists_for_this_so_item, 0)
qty_procurement_shortage  = max(qty_pending - qty_available_for_pick, 0)
```
`qty_solicitada`/`qty_disponible` on the report are `qty_pending`/
`qty_available_for_pick`, so the doctype's own computed `qty_faltante`
always equals `qty_procurement_shortage`. When there is no pre-existing
open Pick List for the line (true for every scenario except the Commit 13
integration case), `qty_already_claimed = 0` and this reduces exactly to
the literal instruction (`qty_pending = qty_remaining`,
`qty_procurement_shortage = qty_shortage`) -- verified by every other
test in this suite still passing unchanged. This is a deliberate,
flagged deviation from the letter of "qty_solicitada = qty_remaining",
not a silent one -- surfaced here and in the Commit 14 delivery report for
the user to confirm or override.

**`shortage_reason` for automatic reports (V1, explicit, no inference):**

| `procurement_route` | `shortage_reason` |
|---|---|
| `Purchase` | `Compra pendiente` |
| `Manufacture` | `Producción pendiente` |
| `Blocked` | `Configuración incompleta` (**new option, added this commit**) |

`Blocked` needed a genuinely new option: none of the existing ones
(`Stock físico no encontrado`, `Stock insuficiente`, `Producto dañado`,
`Error de inventario`, `Compra pendiente`, `Producción pendiente`, `Otro`)
correctly describes "this line is short because its master data is
incomplete (e.g. Manufacture policy with no default BOM), not because of
a physical or purchasing issue" -- `Otro` was considered and rejected as
too vague for someone reviewing the report to know what to fix. Purely
additive to the `Select` field's options (existing values/records
untouched); the doctype's own `shortage_reason` mandatory-for-Bodega rule
is unaffected.

**Not done in this commit (explicitly out of scope):** Material Request,
Purchase Order, Work Order, Production Plan, any Sales Order hook,
background job, `fg_fulfillment_status`, or a Ventas Page.
