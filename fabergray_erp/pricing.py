# -*- coding: utf-8 -*-
"""fabergray_erp/pricing.py -- reglas de precio comercial compartidas por
Cotizaciones (api/cotizaciones.py, Commit 25.14/Hotfix 25.20.4) y
Facturación (api/facturacion.py, precios de factura por Pick List).

Una sola fuente de verdad para:
  - el conjunto cerrado de modos de precio (FULL/10/15/20/25) y sus
    etiquetas;
  - el precio público de referencia (Item Price vigente de una Selling
    Price List);
  - la fórmula del precio descontado, SIEMPRE sobre el precio público --
    nunca sobre un precio ya descontado, así un descuento jamás se acumula
    sobre otro (100.000 -10% -> 90.000; luego -20% -> 80.000, nunca 72.000).

Extraído sin cambiar comportamiento: api/cotizaciones.py conserva sus
nombres históricos (PRICE_MODE_DISCOUNTS, _PRICE_MODE_LABELS,
_reference_selling_rates, _expected_price_mode_rate) como alias de esto.
"""

import frappe
from frappe.utils import cint, flt

PRICE_MODE_FULL = "FULL"
PRICE_MODE_DISCOUNT_10 = "DISCOUNT_10"
PRICE_MODE_DISCOUNT_15 = "DISCOUNT_15"
PRICE_MODE_DISCOUNT_20 = "DISCOUNT_20"
PRICE_MODE_DISCOUNT_25 = "DISCOUNT_25"

#: Código de modo -> porcentaje de descuento. Nunca un porcentaje libre del
#: cliente: solo estas claves son aceptadas.
PRICE_MODE_DISCOUNTS = {
	PRICE_MODE_FULL: 0,
	PRICE_MODE_DISCOUNT_10: 10,
	PRICE_MODE_DISCOUNT_15: 15,
	PRICE_MODE_DISCOUNT_20: 20,
	PRICE_MODE_DISCOUNT_25: 25,
}

#: Etiqueta visible/persistida de cada modo (Quotation.fg_billing_price_mode
#: y Pick List Item.fg_invoice_price_mode usan exactamente estas).
PRICE_MODE_LABELS = {
	PRICE_MODE_FULL: "Precio completo",
	PRICE_MODE_DISCOUNT_10: "Descuento 10%",
	PRICE_MODE_DISCOUNT_15: "Descuento 15%",
	PRICE_MODE_DISCOUNT_20: "Descuento 20%",
	PRICE_MODE_DISCOUNT_25: "Descuento 25%",
}


def reference_selling_rates(item_codes, price_list, ignore_permissions=False):
	"""Precio público vigente: `Item Price.price_list_rate` de exactamente
	estos item_codes en `price_list` (la Selling Price List real del
	documento -- `selling_price_list` de la Quotation/Sales Order), en UNA
	consulta. Nunca valuation_rate, costo ni precio de compra.

	`ignore_permissions` (Commit 25.26) lo pasa en True ÚNICAMENTE
	`public_selling_rates()` de abajo, que antes verifica que la lista es la
	Selling Price List por defecto, habilitada, de venta y no de compra.
	Todos los demás llamadores conservan el valor por defecto (False) y leen
	con los permisos reales de su propia sesión, exactamente como antes.

	Limitación conocida (heredada, sin cambios): no filtra por UOM,
	vigencia, cliente ni moneda. Hoy es exacto porque "Standard Selling"
	tiene un único Item Price por producto (COP, UOM de stock, sin cliente
	ni fechas futuras); si eso cambia, este es el único punto a endurecer."""
	if not item_codes or not price_list:
		return {}
	rows = frappe.get_list(
		"Item Price",
		filters={"price_list": price_list, "item_code": ["in", list(item_codes)]},
		fields=["item_code", "price_list_rate"],
		ignore_permissions=bool(ignore_permissions),
	)
	return {r.item_code: r.price_list_rate for r in rows}


def default_public_selling_price_list():
	"""Commit 25.26 -- la Price List de referencia para el precio público del
	buscador rápido de Cotizaciones: la Selling Price List por defecto
	(Selling Settings), la misma que ERPNext asigna como
	`selling_price_list` a las Quotation que crea la app (cuando el cliente
	no tiene una propia) y por tanto la misma base que Facturación usa luego
	para PRECIO COMPLETO/-10/-15/-20/-25.

	Solo se acepta si está habilitada, es de VENTA y NO es también de
	compra. Devuelve None si no hay una lista válida (-> "SIN PRECIO")."""
	price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	if not price_list:
		return None
	pl = frappe.db.get_value("Price List", price_list, ["enabled", "selling", "buying"], as_dict=True)
	if not pl or not cint(pl.enabled) or not cint(pl.selling) or cint(pl.buying):
		return None
	return price_list


def public_selling_rates(item_codes):
	"""Commit 25.26 -- precio público de VENTA de `item_codes`, para mostrar
	a la Vendedora en el buscador rápido de Cotizaciones.

	EXCEPCIÓN DE PERMISOS DOCUMENTADA Y ÚNICA: la Vendedora no tiene (ni se
	le otorga) DocPerm sobre Item Price. Esta es la única función que llama
	reference_selling_rates() con `ignore_permissions=True`, y queda acotada
	por construcción a:
	  - la lista de `default_public_selling_price_list()` (nunca elegida por
	    el cliente, nunca una lista de compra);
	  - `price_list_rate` únicamente (reference_selling_rates() no lee otra
	    columna);
	  - solo precios > 0: ausente/0/negativo no aparece en el resultado.
	Vive aquí, fuera de api/*, a propósito: ningún módulo whitelisted
	escribe `ignore_permissions=True` (guardrail de test_regression.py).
	El llamador debe haber validado ya usuario/rol/producto."""
	price_list = default_public_selling_price_list()
	if not item_codes or not price_list:
		return {}
	rates = reference_selling_rates(item_codes, price_list, ignore_permissions=True)
	return {code: flt(rate) for code, rate in rates.items() if flt(rate) > 0}


def discounted_rate(reference_rate, discount_percentage, discount_precision, rate_precision):
	"""Precio resultante de aplicar `discount_percentage` a `reference_rate`
	con la MISMA aritmética de dos pasos y precisiones que ERPNext usa en
	calculate_item_rate(): discount_amount = flt(base * pct / 100, prec_desc)
	y rate = flt(base - discount_amount, prec_rate). La base es siempre el
	precio público, nunca un precio ya descontado."""
	reference_rate = flt(reference_rate)
	discount_amount = flt(reference_rate * flt(discount_percentage) / 100.0, discount_precision)
	return flt(reference_rate - discount_amount, rate_precision)
