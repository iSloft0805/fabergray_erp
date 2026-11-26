# -*- coding: utf-8 -*-
"""Controlled procedure to set the NEXT internal invoice number of an issuer.

Context: the business asked that the next invoices be integrandoMAS 7055 and
ecoluminar 2451. Each issuer's number comes from its own native Frappe series
row (tabSeries, invoice_issuers.INVOICE_NUMBERING), taken under a row lock by
api.facturacion._next_invoice_number() when FACTURAR runs. Setting "the next
number" therefore means setting that row's `current` to next_number - 1 --
but ONLY when it is provably safe.

Hard rules (never relaxed by any flag):
- Never automatic: nothing here runs from migrate, patches, hooks or the
  scheduler. A System Manager runs it by hand (bench execute / console).
- Dry run by default: apply_next_invoice_number(..., confirm=0) only reports.
- Forward only: the new next number must be greater than EVERY number this
  issuer has ever used -- the series row, every Pick List of the issuer
  (any docstatus, cancelled included, any key format) and every deleted Pick
  List kept in Deleted Document. A series never goes back, so a number can
  never be issued twice and the sequence never runs backwards in time.
- Never below the configured start number.
- Blocked while the issuer already has duplicated numbers (needs review).
- 3CORAZONESCAFES is never adjusted here.
- Historical invoices are never renumbered: only the series row changes.
- Idempotent: if the series already points at the requested number, it is a
  no-op ("already_applied").
- Auditable: every applied change writes an Activity Log entry (who, when,
  issuer, old -> new, reason, audit snapshot).
- Race-safe: the series row is locked (SELECT ... FOR UPDATE, the same lock
  FACTURAR takes) and re-audited before writing, and the UPDATE is a
  compare-and-set on the value read under that lock.

Usage (never run against production without an approved plan):

    bench --site <site> execute fabergray_erp.invoice_numbering_admin.audit_invoice_numbering \
        --kwargs "{'targets': {'integrandoMAS': 7055, 'ecoluminar': 2451}}"
    bench --site <site> execute fabergray_erp.invoice_numbering_admin.apply_next_invoice_number \
        --kwargs "{'issuer': 'ecoluminar', 'next_number': 2451, 'reason': '...'}"          # dry run
    bench --site <site> execute fabergray_erp.invoice_numbering_admin.apply_next_invoice_number \
        --kwargs "{'issuer': 'ecoluminar', 'next_number': 2451, 'reason': '...', 'confirm': 1}"
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

from fabergray_erp.invoice_issuers import (
	INVOICE_ISSUER_ECOLUMINAR,
	INVOICE_ISSUER_INTEGRANDOMAS,
	INVOICE_ISSUERS,
	INVOICE_NUMBERING,
)

#: Issuers whose next number may be set here. 3CORAZONESCAFES is excluded on
#: purpose (its series must not be touched).
ADJUSTABLE_ISSUERS = (INVOICE_ISSUER_INTEGRANDOMAS, INVOICE_ISSUER_ECOLUMINAR)

REASON_MIN_LENGTH = 10

STATUS_SAFE = "safe"
STATUS_ALREADY_APPLIED = "already_applied"
STATUS_BLOCKED = "blocked"


class InvoiceNumberingAdjustmentError(frappe.ValidationError):
	pass


def _require_system_manager():
	"""Every entry point -- including the read-only audit/plan, which expose
	invoice data -- is System Manager only. None of them is whitelisted, so
	/api/method/... refuses them before this check is even reached; this is
	the second barrier (console / bench execute / any server-side caller)."""
	frappe.only_for("System Manager")


def _series_current(series, for_update=False):
	row = frappe.db.sql(
		"SELECT `current` FROM `tabSeries` WHERE `name` = %s" + (" FOR UPDATE" if for_update else ""), (series,)
	)
	return cint(row[0][0]) if row else None


#: Key prefix of the isolated test series (tests/fixtures.py TestWorld): a
#: deleted Pick List with such a key never consumed a real series number.
TEST_KEY_PREFIX = "FG-FACT-TEST-"


def _deleted_numbers(issuer):
	"""(real, test) numbers of deleted Pick Lists of `issuer` (Deleted
	Document keeps the full JSON). Read-only; parsed in Python. Test-keyed
	ones are reported apart and never block an adjustment."""
	numbers, test_numbers = [], []
	for data in frappe.db.sql_list(
		"""SELECT `data` FROM `tabDeleted Document`
		WHERE `deleted_doctype` = 'Pick List' AND `data` LIKE %s""",
		(f'%"fg_invoice_issuer": "{issuer}"%',),
	):
		try:
			doc = json.loads(data)
			number = cint(doc.get("fg_invoice_number"))
		except (ValueError, TypeError, AttributeError):
			continue
		if number > 0:
			is_test = (doc.get("fg_invoice_number_key") or "").startswith(TEST_KEY_PREFIX)
			(test_numbers if is_test else numbers).append(number)
	return numbers, test_numbers


def _issuer_audit(issuer, target=None, for_update=False, deleted_numbers=None):
	config = INVOICE_NUMBERING.get(issuer) or {}
	series = config.get("series")
	start = cint(config.get("start")) or None
	current = _series_current(series, for_update=for_update) if series else None

	stats = frappe.db.sql(
		"""SELECT COUNT(*) AS assigned, MIN(fg_invoice_number) AS min_number, MAX(fg_invoice_number) AS max_number,
			SUM(docstatus = 2) AS cancelled
		FROM `tabPick List` WHERE fg_invoice_issuer = %s AND IFNULL(fg_invoice_number, 0) > 0""",
		(issuer,),
		as_dict=True,
	)[0]
	duplicates = frappe.db.sql(
		"""SELECT fg_invoice_number AS number, COUNT(*) AS count, GROUP_CONCAT(name ORDER BY name) AS pick_lists
		FROM `tabPick List` WHERE fg_invoice_issuer = %s AND IFNULL(fg_invoice_number, 0) > 0
		GROUP BY fg_invoice_number HAVING COUNT(*) > 1 ORDER BY fg_invoice_number""",
		(issuer,),
		as_dict=True,
	)
	# The Deleted Document scan is the slow part: apply_next_invoice_number()
	# runs it BEFORE taking the series lock and passes the result in, so the
	# lock FACTURAR also needs is held only for the cheap re-checks.
	deleted, deleted_test = deleted_numbers if deleted_numbers is not None else _deleted_numbers(issuer)
	max_live = cint(stats.max_number) or None
	max_deleted = max(deleted) if deleted else None

	# Highest number this issuer has ever "consumed" by any path.
	candidates = [n for n in (current, max_live, max_deleted) if n]
	if start:
		candidates.append(start - 1)
	highest_used = max(candidates) if candidates else 0

	audit = {
		"issuer": issuer,
		"series": series,
		"configured_start": start,
		"series_current": current,
		"series_row_exists": current is not None,
		"next_number_if_untouched": (current + 1) if current is not None else start,
		"assigned_count": cint(stats.assigned),
		"assigned_min": cint(stats.min_number) or None,
		"assigned_max": max_live,
		"assigned_cancelled": cint(stats.cancelled),
		"duplicated_numbers": [dict(d) for d in duplicates],
		"deleted_with_number": len(deleted),
		"deleted_max": max_deleted,
		"deleted_test_series_with_number": len(deleted_test),
		"deleted_test_series_max": max(deleted_test) if deleted_test else None,
		"highest_used": highest_used,
		"adjustable": issuer in ADJUSTABLE_ISSUERS,
	}
	if target is not None:
		target = cint(target)
		at_or_above = frappe.db.sql(
			"""SELECT name, fg_invoice_number AS number, docstatus, fg_invoicing_status AS status
			FROM `tabPick List` WHERE fg_invoice_issuer = %s AND fg_invoice_number >= %s
			ORDER BY fg_invoice_number LIMIT 50""",
			(issuer, target),
			as_dict=True,
		)
		audit.update(
			{
				"target": target,
				"target_exists": any(cint(r.number) == target for r in at_or_above)
				or target in deleted,
				"numbers_at_or_above_target": [dict(r) for r in at_or_above],
				"deleted_at_or_above_target": sorted(n for n in deleted if n >= target)[:50],
			}
		)
	return audit


def audit_invoice_numbering(targets=None):
	"""Read-only report for every issuer (optionally checking `targets`,
	{issuer: next_number}). Safe to run anywhere, writes nothing."""
	_require_system_manager()
	if isinstance(targets, str):
		targets = json.loads(targets)
	targets = targets or {}
	return {
		"site_today": frappe.utils.nowdate(),
		"issuers": [_issuer_audit(issuer, targets.get(issuer)) for issuer in INVOICE_ISSUERS],
	}


def plan_next_invoice_number(issuer, next_number, for_update=False, deleted_numbers=None):
	"""What setting `next_number` as the next invoice of `issuer` would do,
	and whether it is allowed. Read-only (unless called under the lock by
	apply_next_invoice_number())."""
	_require_system_manager()
	next_number = cint(next_number)
	audit = (
		_issuer_audit(issuer, next_number, for_update=for_update, deleted_numbers=deleted_numbers)
		if issuer in INVOICE_ISSUERS
		else None
	)
	reasons = []
	if issuer not in ADJUSTABLE_ISSUERS:
		reasons.append(_("La serie de {0} no se ajusta con este procedimiento.").format(issuer))
	if next_number <= 0:
		reasons.append(_("El número debe ser un entero positivo."))
	if audit:
		if audit["duplicated_numbers"]:
			reasons.append(_("El emisor ya tiene números duplicados; requieren revisión antes de ajustar la serie."))
		# Idempotent only when the series already points there AND nothing
		# above it was ever used (an inconsistent series is never "applied").
		already = audit["series_current"] == next_number - 1 and audit["highest_used"] == next_number - 1
		if not already and next_number <= audit["highest_used"]:
			reasons.append(
				_(
					"{0} no es seguro: el emisor ya usó números hasta {1} (serie, facturas vigentes, anuladas o eliminadas). "
					"Retroceder duplicaría números o rompería el orden cronológico."
				).format(next_number, audit["highest_used"])
			)
		if audit["configured_start"] and next_number < audit["configured_start"]:
			reasons.append(_("{0} está por debajo del número inicial configurado ({1}).").format(next_number, audit["configured_start"]))
	if reasons:
		status = STATUS_BLOCKED
	elif audit["series_current"] == next_number - 1 and audit["highest_used"] == next_number - 1:
		status = STATUS_ALREADY_APPLIED
	else:
		status = STATUS_SAFE
	return {
		"issuer": issuer,
		"next_number": next_number,
		"status": status,
		"reasons": reasons,
		"series_current_before": audit["series_current"] if audit else None,
		"series_current_after": (next_number - 1) if status == STATUS_SAFE else (audit or {}).get("series_current"),
		"skipped_numbers": (next_number - 1 - audit["highest_used"]) if audit and status == STATUS_SAFE else 0,
		"safe_alternative": (audit["highest_used"] + 1) if audit and status == STATUS_BLOCKED and issuer in ADJUSTABLE_ISSUERS else None,
		"audit": audit,
	}


def apply_next_invoice_number(issuer, next_number, reason=None, confirm=0):
	"""Dry run unless confirm=1. System Manager only. Locks the series row,
	re-audits under the lock and writes current = next_number - 1 with a
	compare-and-set, plus an Activity Log entry. Never commits by itself
	(bench execute commits on success; any error rolls everything back)."""
	_require_system_manager()
	reason = (reason or "").strip()
	if cint(confirm) and len(reason) < REASON_MIN_LENGTH:
		frappe.throw(_("Indica el motivo del ajuste (mínimo {0} caracteres).").format(REASON_MIN_LENGTH))

	if not cint(confirm):
		return {"dry_run": True, **plan_next_invoice_number(issuer, next_number)}

	if issuer not in ADJUSTABLE_ISSUERS:
		frappe.throw(_("La serie de {0} no se ajusta con este procedimiento.").format(issuer), InvoiceNumberingAdjustmentError)
	deleted = _deleted_numbers(issuer)  # slow scan, outside the lock
	series = (INVOICE_NUMBERING.get(issuer) or {}).get("series")
	if series:
		# Lock the row FACTURAR locks (create it first if the issuer never invoiced:
		# INSERT IGNORE never overwrites an existing value).
		start = cint(INVOICE_NUMBERING[issuer].get("start"))
		frappe.db.sql(
			"INSERT IGNORE INTO `tabSeries` (`name`, `current`) VALUES (%s, %s)", (series, max(start - 1, 0))
		)
	plan = plan_next_invoice_number(issuer, next_number, for_update=True, deleted_numbers=deleted)
	if plan["status"] == STATUS_BLOCKED:
		frappe.throw("\n".join(plan["reasons"]), InvoiceNumberingAdjustmentError)
	if plan["status"] == STATUS_ALREADY_APPLIED:
		return {"dry_run": False, "applied": False, **plan}

	before = plan["series_current_before"]
	frappe.db.sql(
		"UPDATE `tabSeries` SET `current` = %s WHERE `name` = %s AND `current` = %s",
		(cint(next_number) - 1, series, before),
	)
	if _series_current(series) != cint(next_number) - 1:
		frappe.throw(_("La serie cambió durante el ajuste; no se aplicó nada."), InvoiceNumberingAdjustmentError)

	frappe.get_doc(
		{
			"doctype": "Activity Log",
			"subject": _("Ajuste de consecutivo de factura {0}: próximo número {1}").format(issuer, next_number),
			"content": json.dumps(
				{
					"issuer": issuer,
					"series": series,
					"current_before": before,
					"current_after": cint(next_number) - 1,
					"next_number": cint(next_number),
					"reason": reason,
					"audit": plan["audit"],
				},
				default=str,
				ensure_ascii=False,
			),
			"status": "Success",
			"reference_doctype": "Pick List",
			"user": frappe.session.user,
			"full_name": frappe.utils.get_fullname(frappe.session.user),
		}
	).insert(ignore_permissions=True)  # audit row of a System-Manager-only action

	return {"dry_run": False, "applied": True, **plan, "series_current_after": cint(next_number) - 1}
