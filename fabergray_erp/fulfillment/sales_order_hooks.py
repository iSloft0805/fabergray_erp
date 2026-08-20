# -*- coding: utf-8 -*-
"""Commit 16 -- thin Sales Order.on_submit handler, wired via hooks.py
`doc_events` (see hooks.py). No `apps/erpnext` modification, no Sales
Order class override, no Server Script -- a plain doc_events entry, the
standard Frappe extension point for this.

This module intentionally contains zero fulfillment logic of its own --
see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 16" for why: every rule about
what counts as a "relevant" Sales Order (submitted, not cancelled, has
stock lines, has valid warehouses) already lives in
analyze_sales_order() (Commit 12) and in ERPNext's own native Sales Order
validation (which runs, and must pass, before on_submit ever fires) --
duplicating any of it here would be a second, parallel copy of rules that
already exist.
"""

from fabergray_erp.fulfillment.engine import process_sales_order


def on_submit(doc, method=None):
    return process_sales_order(doc)
