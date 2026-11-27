# -*- coding: utf-8 -*-
"""fabergray_erp/commercial_lines.py -- HOTFIX cantidades divididas.

ONE place that turns the internal rows of a Pick List into COMMERCIAL
lines, reused by Bodega, Facturación (review + PDF), Cartera (through the
invoice builder) and Recorridos.

Quantity semantics (ERPNext v16, Pick List Item, audited for this hotfix):
- `qty`                -> in the row's own `uom` (commercial unit);
- `stock_qty`          -> in `stock_uom` (qty x conversion_factor);
- `picked_qty`, `delivered_qty`, `transferred_qty` -> in `stock_uom`.
A Sales Order Item's `rate` is the price of ONE unit of ITS `uom`, so the
amount of a line is (billed stock qty / Sales Order Item.conversion_factor)
x rate -- the stock quantity is divided ONCE, by the factor of the Sales
Order line whose price is used (never by the row's own factor: rows created
before this hotfix may carry factor 1 for a line sold in boxes).

Grouping: rows are ONE commercial line only when they belong to the same
Sales Order line AND share item, unit, factor and (for invoicing) the same
final price -- never by item_code alone. Two lines of the same product with
different prices/discounts stay separate (different Sales Order lines).

Indivisible units: a quantity that is not a whole number of a
"must be whole number" unit is REFUSED with an explicit error, never
rounded.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

#: Precision of commercial quantities and money.
QTY_PRECISION = 6
MONEY_PRECISION = 2

#: Invoice calculation versions, persisted on the Pick List when it is
#: marked Facturado (fg_invoice_calc_version). Historical invoices (no
#: version) keep the pre-hotfix calculation forever.
CALC_VERSION_LEGACY = 0
CALC_VERSION_COMMERCIAL = 2


class CommercialQuantityError(frappe.ValidationError):
	"""A stock quantity that is not a whole number of an indivisible unit."""


def is_whole_number_uom(uom):
	return bool(uom) and bool(cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number")))


def to_commercial_qty(stock_qty, conversion_factor, uom, label=None):
	"""Stock quantity -> quantity in `uom` (the price's unit). Refuses a
	fraction of an indivisible unit instead of rounding it."""
	factor = flt(conversion_factor) or 1
	qty = flt(flt(stock_qty) / factor, QTY_PRECISION)
	if is_whole_number_uom(uom) and abs(qty - round(qty)) > 10 ** -QTY_PRECISION * 10:
		frappe.throw(
			_(
				"{0}: {1} unidades de inventario no equivalen a un número entero de {2} (factor {3}). "
				"Revisa el alistamiento; el sistema no redondea cantidades."
			).format(label or "", flt(stock_qty), uom, factor),
			CommercialQuantityError,
		)
	return qty


def _row_value(row, field):
	return row.get(field) if hasattr(row, "get") else getattr(row, field, None)


def group_key(row, extra=None):
	"""Commercial identity of a Pick List row: its Sales Order line and item
	(a row without a Sales Order line is its own group). Every row of one
	Sales Order line shares that line's unit, factor, discount and taxes by
	construction, so the line is the commercial identity; the commercial
	unit/factor always come from the Sales Order line itself, never from the
	row (a pre-hotfix top-up row may carry factor 1). `extra` adds caller
	conditions -- Facturación adds the row's final (frozen) price, so rows of
	one line with different prices never merge."""
	soi = _row_value(row, "sales_order_item")
	return (soi or f"row:{_row_value(row, 'name')}", _row_value(row, "item_code")) + tuple(extra or ())


def group_id(key):
	"""Stable identifier of a commercial line for the Bodega/Facturación
	endpoints: Sales Order line (or row) + item."""
	return "|".join(str(part) for part in key[:2])


def group_rows(rows, extra_of_row=None):
	"""[(key, [rows])] in first-appearance (idx) order."""
	groups = {}
	order = []
	for row in rows:
		key = group_key(row, extra_of_row(row) if extra_of_row else None)
		if key not in groups:
			groups[key] = []
			order.append(key)
		groups[key].append(row)
	return [(key, groups[key]) for key in order]


def find_group(rows, gid):
	"""The rows of the group whose group_id() is `gid` (empty if none)."""
	for key, members in group_rows(rows):
		if group_id(key) == gid:
			return members
	return []
