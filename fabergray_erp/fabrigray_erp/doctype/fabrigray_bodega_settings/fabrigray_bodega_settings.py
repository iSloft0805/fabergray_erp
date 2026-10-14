# Copyright (c) 2026, Fabrigray SAS and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FabrigrayBodegaSettings(Document):
	"""Global Bodega policy. Read and changed only through
	fabergray_erp.fulfillment.bodega_settings (fresh reads, audited
	changes); any other write of the policy is refused here."""

	def validate(self):
		from fabergray_erp.fulfillment.bodega_settings import guard_settings_write

		guard_settings_write(self)
