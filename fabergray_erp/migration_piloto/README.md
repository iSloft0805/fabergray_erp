# Migración de datos legados Access -> Customer / Item

Importa Clientes y Productos desde los dos Excel exportados del sistema
Access anterior (`clientes base de datos.xlsx`, `productos base de datos.xlsx`)
hacia `Customer` e `Item`, más `Item Price` para los productos con precio de
venta público válido. Alcance actual: **solo Customer + Item + Item Price**.
Existencia, Bin, Stock Ledger, valuation_rate, compras, ventas/facturas
históricas, proveedores, etc. quedan explícitamente fuera.

## Archivos

- `importer.py` -- lógica pura de transformación e idempotencia: mapeo
  Excel -> doctype, `resolve_item_code()` (código de producto con fallback
  `ACCESS-{IdProducto}`), `compute_duplicate_codes()`, `upsert_item_price()`,
  `validate_prerequisites()`. No sabe leer Excel ni hacer reportes.
- `migrate.py` -- orquestación para producción: carga los `.xlsx`, valida
  prerrequisitos, corre en modo `dry_run` (solo lectura) o real (escribe),
  con progreso periódico y reporte final de `created/updated/skipped/errors`.
- `README.md` -- este archivo.

Los Custom Fields que la migración usa como clave de idempotencia **no**
viven aquí -- están versionados como fixtures nativos de la app (ver más
abajo), no dependen de correr ningún script de este directorio.

## Custom Fields (idempotencia)

| Doctype | Fieldname | Tipo | Unique | Propósito |
|---|---|---|---|---|
| Customer | `access_id_cliente` | Data | sí | IdCliente histórico de Access. Clave de búsqueda para update-vs-create. |
| Customer | `access_nombre_comercial` | Data | no | Nombre Comercial de Access (sin campo nativo equivalente en Customer). |
| Item | `access_id_producto` | Data | sí | IdProducto histórico de Access. Clave de búsqueda para update-vs-create. |

Versionados en `fabergray_erp/fixtures/custom_field.json` (mismo mecanismo
que usan los campos `fg_*` de Fulfillment Engine en esta app), declarados en
el filtro `Custom Field` de `hooks.py::fixtures`. Se instalan automáticamente
con `bench migrate` en cualquier sitio nuevo -- no hace falta ejecutar nada
de este directorio para que existan.

## Prerrequisitos (fallan explícito, nunca se crean solos)

Antes de escribir cualquier dato, `migrate_access_customers_and_items()`
verifica que ya existan:

- Item Group **Fabrigray**
- UOM **Unidad**
- Price List **Standard Selling**
- Currency **COP**

Si falta alguno, lanza `frappe.throw()` con el detalle exacto y no escribe
nada. Si el sitio destino usa otra moneda/lista de precios/grupo, hay que
crearlos a mano primero (o ajustar las constantes en `importer.py`) -- este
importador no los crea silenciosamente.

> `Unidad` es un fallback temporal para poder construir el maestro sin
> inventar unidades reales de Access (`IdUnidad` no se interpreta). No
> implica que todo se venda físicamente por unidad -- eso requiere una
> normalización de UOM aparte, todavía no implementada aquí.

## Cómo ejecutar

Vía `bench execute` (Administrator es suficiente, no usar
`frappe.set_user()` dentro del importador):

```bash
# Análisis, sin escribir nada
bench --site fabergray.local execute \
  fabergray_erp.migration_piloto.migrate.migrate_access_customers_and_items \
  --kwargs "{'customers_xlsx': '/ruta/a/clientes.xlsx', 'products_xlsx': '/ruta/a/productos.xlsx', 'dry_run': True}"

# Ejecución real
bench --site fabergray.local execute \
  fabergray_erp.migration_piloto.migrate.migrate_access_customers_and_items \
  --kwargs "{'customers_xlsx': '/ruta/a/clientes.xlsx', 'products_xlsx': '/ruta/a/productos.xlsx'}"
```

Antes de una corrida real en cualquier sitio: **`bench --site <site> backup`**
primero. Sin excepción.

## Idempotencia

- `Customer` se localiza por `access_id_cliente` (nunca por `customer_name`
  -- Frappe puede resolver nativamente nombres repetidos con un sufijo
  `- 1`, `- 2`; el importador no escribe lógica propia de desambiguación).
- `Item` se localiza por `access_id_producto`.
- `Item Price` se localiza por `(item_code, price_list, uom)`.

Correr la migración dos veces con el mismo Excel debe dar `created=0` en las
tres entidades la segunda vez; solo cambian los `updated`.

## Clientes omitidos

Un cliente se omite (no se crea, no se inventa `customer_name`) si su
columna `Nombre` viene vacía en el Excel. Se listan en el resultado bajo
`clientes_omitidos_detalle` con `IdCliente`/`Documento` para corrección
manual en el origen -- el importador nunca escribe un placeholder.

## Garantías de escritura

Exclusivamente `frappe.get_doc()` / `.insert()` / `.save()`. Nada de SQL
INSERT directo, `ignore_permissions=True`, ni manipulación de stock (Bin,
Stock Ledger Entry, Stock Reconciliation). `frappe.db.commit()` se llama
solo cada `commit_every` filas (default 250), nunca dentro del loop por
fila -- cada fila individual está aislada con un savepoint nativo
(`frappe.database.savepoint`), así que un error en una fila hace rollback
solo de esa fila y no del lote ya confirmado.
