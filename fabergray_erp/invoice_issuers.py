# -*- coding: utf-8 -*-
"""fabergray_erp/invoice_issuers.py -- empresas emisoras permitidas para el
PDF comercial de factura ("Fabrigray Factura Comercial", api/facturacion.py).

Un solo Print Format para todos los emisores: el template nunca contiene
datos de una empresa concreta, solo lee `doc.fg_pdf_issuer`, que
api/facturacion.py construye a partir de ISSUER_CONFIG[issuer] según el
valor persistido en Pick List.fg_invoice_issuer.

REGLA: ningún valor de este archivo se inventa -- todos fueron entregados
por el negocio. Mientras falte cualquiera de REQUIRED_ISSUER_FIELDS el PDF
se marca como BORRADOR (no válido como factura); además, hoy siempre es
BORRADOR por falta de numeración (api/facturacion.py).
Los datos bancarios existen dos veces a propósito: dentro de
`payment_instruction` (texto literal) y normalizados en bank_* (para un
futuro bloque de pago). Un test verifica que ambos coinciden.

Logos: `logo` es la URL pública del PNG dentro de esta app. Frappe publica
fabergray_erp/public/ como /assets/fabergray_erp/ (sites/assets/
fabergray_erp es un symlink a esa carpeta), así que
    /assets/fabergray_erp/images/invoice/<archivo>.png
se sirve desde
    fabergray_erp/public/images/invoice/<archivo>.png
get_issuer_config() solo entrega el logo si el archivo EXISTE en disco; si
aún no se ha copiado, entrega None y el PDF muestra el recuadro de logo
faltante, sin error. Copiar el PNG basta -- no hay que tocar el Print
Format ni este archivo.

Claves de ISSUER_CONFIG == valores del Select Pick List.fg_invoice_issuer
(fixtures/custom_field.json) -- el mismo conjunto cerrado que ya usa
Customer.fg_customer_company_type para estas dos empresas.
"""

import os

INVOICE_ISSUER_INTEGRANDOMAS = "integrandoMAS"
INVOICE_ISSUER_ECOLUMINAR = "ecoluminar"

#: Whitelist estricta, en el orden en que se muestran en la UI.
INVOICE_ISSUERS = (INVOICE_ISSUER_INTEGRANDOMAS, INVOICE_ISSUER_ECOLUMINAR)

#: Carpeta pública de assets de facturación (ver docstring del módulo).
INVOICE_ASSETS_URL_PREFIX = "/assets/fabergray_erp/images/invoice/"
_INVOICE_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "images", "invoice")

#: Imprenta -- común a TODOS los emisores (no pertenece a ninguno), por eso
#: vive aquí una sola vez y no dentro de cada legal_footer. El Print Format
#: arma con estos campos: "Impreso por: <name> Nit: <nit> Tel <phone>".
INVOICE_PRINT_PROVIDER = {
	"name": "LITO CARIBE",
	"nit": "28410436-9",
	"phone": "(7) 6336124",
}

# Textos legales: copia LITERAL de lo entregado por el negocio (incluidas
# grafías como "Art, 774", "titulo", "tranfrencia" y "ECODLUMINAR") -- no se
# corrigen sin autorización. `legal_footer` es el párrafo jurídico y
# `payment_instruction` la instrucción de pago que lo seguía, separados solo
# para poder destacarla visualmente en el PDF.
_LEGAL_FOOTER_TEMPLATE = (
	"Esta factura de Venta se asimila en sus efectos a la letra de cambio, conforme al Art, 774 del "
	"Código de comercio. Declaro haber recibido real y materialmente las mercancías descritas en este "
	"titulo Valor. Adeudo a {name} el monto neto indicado; en caso de mora se causarán los intereses al "
	"máximo permitido legalmente. Autorizamos a {name} para registrar el nombre de nuestra razón social "
	"en los archivos de procredito o en cualquier otro sistema de información crediticia, en caso tal de "
	"que se incumpla con el pago de las obligaciones aquí constituidas."
)

ISSUER_CONFIG = {
	INVOICE_ISSUER_INTEGRANDOMAS: {
		"display_name": "INTEGRANDO MAS BGA",
		"nit": "1005281903-1",
		"tax_regime": "Régimen Simplificado",
		"address": "Calle 46 # 22-28 Oficina 2",
		"city": "Girón",
		"phones": ("3118814375", "321 5351749"),
		"email": "integrandomas@hotmail.com",
		"logo": INVOICE_ASSETS_URL_PREFIX + "integrandomas-logo.png",
		"legal_footer": _LEGAL_FOOTER_TEMPLATE.format(name="INTEGRANDO MAS BGA"),
		"payment_instruction": (
			"FAVOR GIRAR CHEQUE A NOMBRE DE NICOLAS FELIPE HERRERA MATEUS o realizar tranfrencia a la "
			"cuenta de ahorro Nro 79600011630 Bancolombia."
		),
		"bank_name": "Bancolombia",
		"bank_account_type": "Cuenta de ahorro",
		"bank_account_number": "79600011630",
		"bank_account_holder": "NICOLAS FELIPE HERRERA MATEUS",
	},
	INVOICE_ISSUER_ECOLUMINAR: {
		"display_name": "ECOLUMINAR",
		"nit": "1005109961-2",
		"tax_regime": "Régimen Simplificado",
		"address": "Calle 46 Número 22-28 Oficina 3",
		"city": "Girón",
		"phones": ("3118814375", "321 5351749"),
		"email": "ecodluminar@outlook.com",  # tal como fue entregado
		"logo": INVOICE_ASSETS_URL_PREFIX + "ecoluminar-logo.png",
		# "ECODLUMINAR" dentro del texto legal: tal como fue entregado.
		"legal_footer": _LEGAL_FOOTER_TEMPLATE.format(name="ECODLUMINAR"),
		"payment_instruction": (
			"FAVOR GIRAR CHEQUE A NOMBRE DE JUAN ANDRES LOZANO GARCIA o realizar tranfrencia a la "
			"cuenta de ahorro Nro 09036542699 Bancolombia."
		),
		"bank_name": "Bancolombia",
		"bank_account_type": "Cuenta de ahorro",
		"bank_account_number": "09036542699",
		"bank_account_holder": "JUAN ANDRES LOZANO GARCIA",
	},
}

#: Sin estos datos el PDF sale marcado como BORRADOR (además de la falta de
#: numeración, que api/facturacion.py evalúa por separado y hoy siempre aplica).
REQUIRED_ISSUER_FIELDS = (
	"display_name",
	"nit",
	"tax_regime",
	"address",
	"city",
	"phones",
	"email",
	"logo",
	"legal_footer",
)

REQUIRED_ISSUER_FIELD_LABELS = {
	"display_name": "nombre",
	"nit": "NIT",
	"tax_regime": "régimen",
	"address": "dirección",
	"city": "ciudad",
	"phones": "teléfono",
	"email": "email",
	"logo": "logo",
	"legal_footer": "texto legal",
}


def logo_file_path(logo_url):
	"""Ruta en disco de un logo configurado, o None si la URL no está dentro
	de INVOICE_ASSETS_URL_PREFIX (nunca se resuelve una ruta arbitraria)."""
	if not logo_url or not logo_url.startswith(INVOICE_ASSETS_URL_PREFIX):
		return None
	filename = logo_url[len(INVOICE_ASSETS_URL_PREFIX) :]
	if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
		return None
	return os.path.join(_INVOICE_ASSETS_DIR, filename)


def logo_available(issuer):
	"""True si el PNG configurado para `issuer` ya existe en disco."""
	config = ISSUER_CONFIG.get(issuer) or {}
	path = logo_file_path(config.get("logo"))
	return bool(path and os.path.isfile(path))


def get_issuer_config(issuer):
	"""Contexto normalizado de `issuer` para el Print Format, o None si no
	está en la whitelist. Es una COPIA construida solo desde
	ISSUER_CONFIG[issuer] -- nunca contiene datos de otro emisor.
	`phones_display` ya viene unido ("3118814375 - 321 5351749") para que el
	template no haga lógica. `logo` es la URL configurada solo si el archivo
	existe; si no, None (el PDF muestra el recuadro de logo faltante) y
	`logo_configured` conserva la URL esperada."""
	config = ISSUER_CONFIG.get(issuer) if issuer in INVOICE_ISSUERS else None
	if not config:
		return None
	context = dict(config)
	context["phones"] = list(config.get("phones") or ())
	context["phones_display"] = " - ".join(context["phones"]) or None
	context["logo_configured"] = config.get("logo")
	context["logo"] = config.get("logo") if logo_available(issuer) else None
	return context


def get_print_provider():
	"""Copia de INVOICE_PRINT_PROVIDER para el Print Format."""
	return dict(INVOICE_PRINT_PROVIDER)


def missing_issuer_fields(issuer):
	"""Etiquetas legibles de los REQUIRED_ISSUER_FIELDS aún vacíos. El logo
	cuenta como faltante mientras su archivo no exista en disco."""
	config = ISSUER_CONFIG.get(issuer) or {}
	missing = []
	for field in REQUIRED_ISSUER_FIELDS:
		present = logo_available(issuer) if field == "logo" else bool(config.get(field))
		if not present:
			missing.append(REQUIRED_ISSUER_FIELD_LABELS[field])
	return missing
