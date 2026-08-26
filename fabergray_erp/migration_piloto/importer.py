from collections import Counter

import frappe

PRICE_LIST = "Standard Selling"
CURRENCY = "COP"
ITEM_GROUP_FALLBACK = "Fabrigray"
STOCK_UOM_FALLBACK = "Unidad"

# (doctype, name) pairs that must already exist before any write is attempted.
# Never auto-created -- migrate_access_customers_and_items() fails explicitly
# if one is missing rather than silently creating it.
REQUIRED_PREREQUISITES = [
    ("Item Group", ITEM_GROUP_FALLBACK),
    ("UOM", STOCK_UOM_FALLBACK),
    ("Price List", PRICE_LIST),
    ("Currency", CURRENCY),
]


def norm(v):
    return "" if v is None else str(v).strip()


def compute_duplicate_codes(productos):
    """CodProducto values that appear more than once across the full product
    dataset (excluding the '-' placeholder and blanks). Must be computed over
    the WHOLE dataset, never a subset, or resolve_item_code() below can
    silently accept a code that only looks unique in a partial view."""
    codes = [norm(r.get("CodProducto")) for r in productos]
    codes = [c for c in codes if c and c != "-"]
    counts = Counter(codes)
    return {c for c, n in counts.items() if n > 1}


def has_valid_price(row):
    v = row.get("Venta Publico")
    return isinstance(v, (int, float)) and v > 0


def resolve_item_code(cod_producto, id_producto, duplicate_codes):
    cod = norm(cod_producto)
    if cod and cod != "-" and cod not in duplicate_codes:
        return cod
    return f"ACCESS-{id_producto}"


def validate_prerequisites():
    """Raises with a clear message (nothing written) if a doctype record the
    importer depends on as a fallback/target does not already exist. Called
    once at the start of migrate_access_customers_and_items(); never creates
    the missing record itself."""
    missing = [f"{dt} {name!r}" for dt, name in REQUIRED_PREREQUISITES if not frappe.db.exists(dt, name)]
    if missing:
        frappe.throw(
            "Prerrequisitos faltantes, no se escribió ningún dato: " + "; ".join(missing),
            title="Migración Access abortada",
        )


def import_customer(row):
    """row: dict with IdCliente, Documento, Nombre, 'Nombre Comercial', Habilitado.
    Idempotent by Customer.access_id_cliente. Native Customer naming (naming_series) is preserved.
    """
    id_cliente = str(row["IdCliente"])

    existing_name = frappe.db.get_value("Customer", {"access_id_cliente": id_cliente})
    if existing_name:
        doc = frappe.get_doc("Customer", existing_name)
        created = False
    else:
        doc = frappe.new_doc("Customer")
        created = True

    doc.customer_name = row["Nombre"]
    doc.customer_type = "Company"
    doc.disabled = 0 if row.get("Habilitado") else 1
    doc.tax_id = norm(row.get("Documento")) or None
    doc.access_id_cliente = id_cliente
    doc.access_nombre_comercial = norm(row.get("Nombre Comercial")) or None

    if created:
        doc.insert()
    else:
        doc.save()

    return doc.name, created


def import_item(row, duplicate_codes):
    """row: dict with IdProducto, CodProducto, Descripcion, Habilitado, 'Venta Publico'.
    Idempotent by Item.access_id_producto (item_code is also deterministic from the same inputs).
    """
    id_producto = str(row["IdProducto"])
    item_code = resolve_item_code(row.get("CodProducto"), id_producto, duplicate_codes)

    existing_name = frappe.db.get_value("Item", {"access_id_producto": id_producto})
    if existing_name:
        doc = frappe.get_doc("Item", existing_name)
        created = False
    else:
        doc = frappe.new_doc("Item")
        doc.item_code = item_code
        created = True

    doc.item_name = row["Descripcion"]
    doc.description = row["Descripcion"]
    doc.item_group = ITEM_GROUP_FALLBACK
    doc.stock_uom = STOCK_UOM_FALLBACK
    doc.disabled = 0 if row.get("Habilitado") else 1
    doc.access_id_producto = id_producto

    if created:
        doc.insert()
    else:
        doc.save()

    return doc.name, created


def upsert_item_price(item_code, price_list_rate, uom=STOCK_UOM_FALLBACK):
    """Idempotent by (item_code, price_list, uom). Only called when price_list_rate is a valid > 0 number."""
    existing_name = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": PRICE_LIST, "uom": uom},
        "name",
    )
    if existing_name:
        doc = frappe.get_doc("Item Price", existing_name)
        created = False
    else:
        doc = frappe.new_doc("Item Price")
        doc.item_code = item_code
        doc.price_list = PRICE_LIST
        doc.uom = uom
        doc.selling = 1
        created = True

    doc.price_list_rate = price_list_rate
    doc.currency = CURRENCY

    if created:
        doc.insert()
    else:
        doc.save()

    return doc.name, created
