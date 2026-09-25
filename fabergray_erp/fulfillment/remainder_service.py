# -*- coding: utf-8 -*-
"""Fase 28.2 -- ensure_remaining_pick_list(): the ONE place that brings the
unpicked remainder of a Sales Order line back to Bodega after its
Reporte de Faltante is really resolved.

The gap it closes (reproduced in Fase 28.1,
tests/test_production_foundation.py): pedido 10, Bodega alista 6 y
termina (a partial > 0 may finish), faltante 4. The submitted Pick List
leaves Bodega's queue ("listos") and nothing ever re-offers the 4 -- even
after the goods arrive. Today that happens with purchases
(api.jefe_bodega.receive_shortage_purchase(), which calls this now);
production will reuse it in 28.5 when a Manufacture Stock Entry resolves a
shortage.

Rules:
- only for a report that is Resuelto and whose original Pick List is
  SUBMITTED. A still-draft Pick List (e.g. the full-demand complement row
  waiting for stock) is completed in place by Bodega -- no new document;
- only that report's own Sales Order line; other lines keep their own
  shortages;
- the quantity is ERPNext's own remainder (ordered - delivered - what open
  Pick Lists already claim): the units already picked are never offered
  again, the Sales Order is never edited, and a second call creates
  nothing (idempotent);
- concurrency: the Sales Order row is locked first and the claims are read
  with a locking read, so two resolutions of the same order serialize and
  the second one sees the first one's Pick List.

System action: the caller (a Jefe de Bodega resolving a purchase, later
Producción) already passed its own checks; the Pick List is inserted with
ignore_permissions exactly like the full-demand builder does on submit."""

import frappe

from fabergray_erp.fulfillment.pick_list_service import create_pick_list_for_remaining_demand

RESOLVED_STATUS = "Resuelto"
_BLOCKED_SALES_ORDER_STATUSES = ("Closed", "On Hold", "Completed", "Cancelled")


def ensure_remaining_pick_list(shortage_report):
	"""The new Pick List's name, or None when nothing has to be created.
	`shortage_report` is a name or a loaded Reporte de Faltante."""
	report = (
		shortage_report
		if hasattr(shortage_report, "doctype")
		else frappe.get_doc("Reporte de Faltante", shortage_report)
	)
	if report.status != RESOLVED_STATUS:
		return None
	if not (report.sales_order and report.sales_order_item and report.pick_list):
		return None
	if frappe.db.get_value("Pick List", report.pick_list, "docstatus") != 1:
		return None  # still a draft: Bodega completes that same Pick List

	# Lock order: the Sales Order row, then (inside the builder) the Pick
	# List Item rows of that line.
	frappe.db.get_value("Sales Order", report.sales_order, "name", for_update=True)
	so = frappe.get_doc("Sales Order", report.sales_order, for_update=True)
	if so.docstatus != 1 or so.status in _BLOCKED_SALES_ORDER_STATUSES:
		return None
	so.flags.ignore_permissions = True  # service read of an order the caller's flow already owns

	pick_list = create_pick_list_for_remaining_demand(so, [report.sales_order_item])
	return pick_list.name if pick_list else None
