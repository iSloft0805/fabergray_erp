# Assets del PDF comercial de factura

Logos de las empresas emisoras de "Fabrigray Factura Comercial".
Frappe publica esta carpeta como `/assets/fabergray_erp/images/invoice/`.

| Emisor          | Archivo                   | URL pública                                                |
|-----------------|---------------------------|------------------------------------------------------------|
| integrandoMAS   | `integrandomas-logo.png`  | `/assets/fabergray_erp/images/invoice/integrandomas-logo.png` |
| ecoluminar      | `ecoluminar-logo.png`     | `/assets/fabergray_erp/images/invoice/ecoluminar-logo.png`    |

Además, `3corazonescafes-logo.png`
(`/assets/fabergray_erp/images/invoice/3corazonescafes-logo.png`) lo usa el
Print Format "3CORAZONESCAFES Factura" (Sales Invoice), que lo referencia
directamente en su HTML (fixtures/print_format.json).

Las rutas de los emisores están configuradas en `fabergray_erp/invoice_issuers.py`. Basta con
copiar aquí el PNG con el nombre exacto: el PDF lo usa en cuanto el archivo
existe, sin tocar el Print Format. Mientras falte, el PDF muestra el recuadro
de logo faltante y sigue marcado como BORRADOR.
