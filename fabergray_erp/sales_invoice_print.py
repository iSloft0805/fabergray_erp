# -*- coding: utf-8 -*-
"""fabergray_erp/sales_invoice_print.py -- contexto de solo lectura para el
Print Format "3CORAZONESCAFES Factura" (Sales Invoice).

Expuesto a Jinja como método (hooks.py `jinja`), NO como before_print: el
template llama get_sales_invoice_print_context(doc) y solo presenta. Nunca
escribe ni recalcula nada -- los totales salen tal cual de doc (taxes_and_
totals nativo). Cliente, contacto de compras y ejecutivo se resuelven con
los MISMOS helpers que "Fabrigray Factura Comercial" (api/facturacion.py y
api/cotizaciones.py), que solo leen campos que Sales Invoice también tiene
(customer, customer_address, address_display, contact_person, owner...).
Cada valor ausente es None/"" -- el template nunca imprime "None".
"""

import frappe

from fabergray_erp.api.cotizaciones import _resolve_pdf_advisor_name
from fabergray_erp.api.facturacion import _clean_order_observations, _resolve_invoice_customer
from fabergray_erp.invoice_issuers import get_tres_corazones_issuer


def _payment_terms(doc):
	"""Mismo criterio que la factura de IntegrandoMas: el nombre de la
	plantilla de términos de pago; POS sin plantilla = "Contado"."""
	if doc.payment_terms_template:
		return (
			frappe.db.get_value("Payment Terms Template", doc.payment_terms_template, "template_name")
			or doc.payment_terms_template
		)
	return "Contado" if doc.is_pos else None


def _sales_orders(doc):
	"""Pedidos de origen, en orden y sin repetir (Sales Invoice Item.sales_order)."""
	seen = []
	for item in doc.get("items") or []:
		if item.get("sales_order") and item.sales_order not in seen:
			seen.append(item.sales_order)
	return seen


def _order_observations(doc, sales_orders):
	"""OBSERVACIONES DEL PEDIDO: en el flujo actual la observación la escribe
	la Vendedora en Ventas (Sales Order.fg_observations, la misma fuente de
	IntegrandoMas). Si la factura no viene de un pedido con observación, se
	usa remarks de la propia factura (ignorando el "No Remarks" que ERPNext
	pone por defecto)."""
	texts = []
	for so in sales_orders:
		text = _clean_order_observations(frappe.db.get_value("Sales Order", so, "fg_observations"))
		if text and text not in texts:
			texts.append(text)
	if texts:
		return "\n\n".join(texts)
	remarks = (doc.remarks or "").strip()
	return "" if remarks == "No Remarks" else remarks


def _seller_name(doc):
	"""Ejecutivo Ventas: los Sales Person del equipo de ventas de la factura;
	si no hay, el usuario que la creó (mismo criterio que IntegrandoMas, que
	nunca imprime "Administrator")."""
	names = [row.sales_person for row in (doc.get("sales_team") or []) if row.sales_person]
	return ", ".join(names) or _resolve_pdf_advisor_name(doc) or ""


def get_sales_invoice_print_context(doc):
	customer = _resolve_invoice_customer(doc)
	if not customer.get("tax_id") and doc.tax_id:
		customer["tax_id"] = doc.tax_id
	sales_orders = _sales_orders(doc)
	return {
		"issuer": get_tres_corazones_issuer(),
		"customer": customer,
		"payment_terms": _payment_terms(doc),
		"internal_reference": ", ".join(sales_orders) or doc.name,
		"order_observations": _order_observations(doc, sales_orders),
		"seller_name": _seller_name(doc),
		"buyer_name": customer.get("contact_name") or "",
	}
