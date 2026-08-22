# -*- coding: utf-8 -*-
"""Shared Sales Order "commercial name" resolution.

Extracted from `api/ventas.py` (Commit 18.5's `_root_commercial_name()`)
so that `api/bodega.py` can show the exact same stable "PEDIDO-N" label
Ventas already shows, without either module importing a private helper
from the other. Both call this one function; there is no second
implementation anywhere.
"""

import frappe


def root_commercial_name(so_name):
	"""Walks the native `amended_from` chain backward to the original
	document name -- the stable "PEDIDO-N" commercial identity shown
	throughout /app/ventas, independent of how many times the order has
	since been amended (Commit 18.5: the technical name becomes
	`PEDIDO-N-1`, `PEDIDO-N-2`, ... on each amend -- confirmed directly
	against `frappe/model/naming.py`'s `_set_amended_name()`, which always
	takes priority over the `PEDIDO-.#` naming series once `amended_from`
	is set, and cannot be configured to preserve the original literal name
	-- see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 18.5 -- naming"). Only
	ever walks a chain of documents the caller has already established it
	may reference (a Sales Order name it already read off a Pick List row,
	a Reporte de Faltante, or its own document) -- this is a raw,
	single-field read used only to compute a display label, never a
	document permission check, exactly like `_default_warehouse_for_item()`
	in api/ventas.py reasons about the same class of lookup."""
	current = so_name
	seen = set()
	while current not in seen:
		seen.add(current)
		parent = frappe.db.get_value("Sales Order", current, "amended_from")
		if not parent:
			return current
		current = parent
	return current  # defensive: amended_from can never actually cycle
