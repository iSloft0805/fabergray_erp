# -*- coding: utf-8 -*-
"""api/cartera.py -- Fase 27.1: Cartera foundation, interactive API.

Only the manual trigger of the reconciler lives here in 27.1; listing, KPIs,
REGISTRAR COBRO and the Cartera Page arrive in 27.2. The derivation/creation
logic itself is in fabergray_erp/cartera_service.py (a system service); this
module never bypasses permissions on its own.
"""

import frappe
from frappe import _

from erpnext import get_default_company

from fabergray_erp import cartera_service
from fabergray_erp.api.bodega import _require_login

CARTERA_ROLES = ("Cartera", "System Manager")


@frappe.whitelist(methods=["POST"])
def sync_missing_obligations():
	"""SINCRONIZAR: creates the obligation of every Entregado stop of this
	site's company that still has none (the same idempotent reconciler the
	scheduler runs every 15 minutes). Company is always resolved
	server-side."""
	_require_login()
	frappe.only_for(CARTERA_ROLES)
	frappe.has_permission("Cartera Obligacion", "read", throw=True)
	return cartera_service.sync_missing_obligations(company=get_default_company())
