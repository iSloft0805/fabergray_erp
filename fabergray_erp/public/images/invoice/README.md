# Assets del PDF comercial de factura

Logos de las empresas emisoras de "Fabrigray Factura Comercial".
Frappe publica esta carpeta como `/assets/fabergray_erp/images/invoice/`.

| Emisor          | Archivo                   | URL pública                                                |
|-----------------|---------------------------|------------------------------------------------------------|
| integrandoMAS   | `integrandomas-logo.png`  | `/assets/fabergray_erp/images/invoice/integrandomas-logo.png` |
| ecoluminar      | `ecoluminar-logo.png`     | `/assets/fabergray_erp/images/invoice/ecoluminar-logo.png`    |

Las rutas están configuradas en `fabergray_erp/invoice_issuers.py`. Basta con
copiar aquí el PNG con el nombre exacto: el PDF lo usa en cuanto el archivo
existe, sin tocar el Print Format. Mientras falte, el PDF muestra el recuadro
de logo faltante y sigue marcado como BORRADOR.
