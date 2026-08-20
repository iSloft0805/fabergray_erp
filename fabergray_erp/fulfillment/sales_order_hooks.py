# -*- coding: utf-8 -*-
"""Commit 16/17 -- thin Sales Order.on_submit/on_cancel handlers, wired
via hooks.py `doc_events` (see hooks.py). No `apps/erpnext` modification,
no Sales Order class override, no Server Script -- plain doc_events
entries, the standard Frappe extension point for this.

Both handlers intentionally contain zero fulfillment logic of their own,
on purpose -- each delegates entirely to its own dedicated service
module:
- on_submit -> fulfillment.engine.process_sales_order() (Commit 15): see
  FULFILLMENT_ENGINE_CONTRACT.md, "Commit 16" for why every rule about
  what counts as a "relevant" Sales Order already lives in
  analyze_sales_order() (Commit 12) and in ERPNext's own native Sales
  Order validation -- duplicating any of it here would be a second,
  parallel copy of rules that already exist.
- on_cancel -> fulfillment.cancellation_service.cleanup_fulfillment_for_cancelled_sales_order()
  (Commit 17): see FULFILLMENT_ENGINE_CONTRACT.md, "Commit 17" for the
  full lifecycle write-up.
"""

from fabergray_erp.fulfillment.cancellation_service import cleanup_fulfillment_for_cancelled_sales_order
from fabergray_erp.fulfillment.engine import process_sales_order


def on_submit(doc, method=None):
    return process_sales_order(doc)


def on_cancel(doc, method=None):
    return cleanup_fulfillment_for_cancelled_sales_order(doc)
