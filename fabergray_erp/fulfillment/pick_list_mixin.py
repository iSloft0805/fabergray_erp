# -*- coding: utf-8 -*-
"""Commit 25.9 -- fixes the real operational bug reported from Bodega:

    "Insuficiente Stock
    En la fila #1: La cantidad seleccionada 1.0 para el artículo 01460
    es mayor que el stock disponible 0.0 en el almacén
    Productos terminados - FG."

Root cause (confirmed live during this commit's own audit, not assumed):
this message is 100% ERPNext core -- `PickList.validate_stock_qty()`
(erpnext/stock/doctype/pick_list/pick_list.py), called from `validate()`,
which Frappe runs on every `doc.save()` AND `doc.submit()`. It throws
whenever `row.picked_qty > Bin.actual_qty` for that item/warehouse. No
Fabrigray string ("cantidad seleccionada"/"stock disponible"/"Insuficiente")
exists anywhere in this app -- confirmed by grep before writing a single
line here.

That check encodes the WRONG business rule for Fabrigray's own Bodega flow.
`picked_qty` here means "how many units Bodega physically found and set
aside", not "how many units may be issued against the ERP's own live stock
figure" -- those are two different questions, and this app's own
Fulfillment Engine already builds the Pick List with the FULL Sales Order
demand regardless of what `Bin.actual_qty` says (Commit 13,
fulfillment/pick_list_service.py's own module docstring), specifically so
Bodega can attempt to fulfil it physically and report whatever is actually
short via Reporte de Faltante afterward. ERPNext's own native check assumes
the opposite: that `Bin.actual_qty` is trustworthy and picking beyond it is
always wrong. On this site, opening stock is frequently 0 for items that
physically exist on the shelf (never reconciled into the ERP yet), so that
assumption does not hold here.

Why this can't be fixed any of the "easy" ways:
- Cannot edit `validate_stock_qty()` directly -- it lives in erpnext core,
  never touched by this app (Commit 25.9 brief, section 6).
- Cannot use a global bypass (`frappe.flags.ignore_negative_stock` or
  similar) -- that check has no such flag guard to begin with (confirmed by
  reading its full source), and even if it did, disabling it globally would
  also silently weaken the SAME protection for every other doctype/flow on
  the site that legitimately depends on it (e.g. a real Stock Entry).
- Cannot write `Bin.actual_qty` up to make the check pass -- that would be
  falsifying inventory to satisfy a validation, exactly what this commit's
  own brief explicitly forbids (section 3).

The fix: `extend_doctype_class` (hooks.py) -- Frappe's own sanctioned,
per-app mixin mechanism for exactly this situation (see
frappe/model/base_document.py::_get_extended_class(): the extension class is
mixed in AHEAD of the base controller in MRO, so a method defined here wins
over the same-named method on erpnext's own `PickList` class). Scoped to the
Pick List doctype ONLY -- no core file is touched, no other doctype is
affected, no global flag exists to accidentally leave on for something else.

`validate_stock_qty()` below REPLACES (not supplements) ERPNext's own
version, enforcing the correct rule instead: for a physically-picked row,
`0 <= picked_qty <= stock_qty` (the row's own requested/demand quantity --
"Solicitado" in the Bodega UI), never checked against `Bin.actual_qty` at
all. `api/bodega.py::set_picked_qty()` enforces this same bound earlier, for
a friendlier error message before `.save()` is even attempted -- this mixin
is what makes the rule authoritative and unavoidable, on every save/submit
path, including `finish_picking()`'s own `pl.submit()` (Commit 25.9 audit:
submit runs the exact same `validate()` chain first).

Batch-tracked stock (ERPNext's own `batch_qty` branch, the other half of the
native `validate_stock_qty()`) is deliberately NOT replicated here:
confirmed live, this commit's own audit, that 0 of this site's Items have
`has_batch_no` set, so that branch can never fire in this app today. If that
ever changes, this mixin needs its own explicit batch-aware branch added --
never silently inherited back from ERPNext's own check, which validates
against Bin/batch stock, not against demand, the exact thing this fix
exists to avoid.
"""

import frappe
from frappe import _
from frappe.utils import flt


class PickListPhysicalCountMixin:
    """Mixed in ahead of `erpnext.stock.doctype.pick_list.pick_list.PickList`
    via hooks.py's `extend_doctype_class` -- see this module's own docstring
    for why `validate_stock_qty()` below intentionally has the same name as,
    and replaces, the native method."""

    def validate_stock_qty(self):
        for row in self.get("locations"):
            picked_qty = flt(row.picked_qty)
            if not picked_qty:
                continue

            if picked_qty < 0:
                frappe.throw(
                    _("En la fila #{0}: la cantidad alistada no puede ser negativa.").format(row.idx),
                    title=_("Cantidad alistada inválida"),
                )

            # Same field-precision utility api/bodega.py's own set_picked_qty()
            # uses for the identical comparison -- one shared tolerance, never
            # two independently-rounded copies of the same rule.
            precision = row.precision("picked_qty") or 6
            requested_qty = flt(row.stock_qty, precision)

            if requested_qty and flt(picked_qty, precision) > requested_qty:
                frappe.throw(
                    _(
                        "En la fila #{0}: la cantidad alistada {1} para el artículo {2} "
                        "supera la cantidad solicitada {3}."
                    ).format(row.idx, picked_qty, row.item_code, requested_qty),
                    title=_("Cantidad alistada inválida"),
                )
