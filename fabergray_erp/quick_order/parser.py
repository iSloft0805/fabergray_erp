# -*- coding: utf-8 -*-
"""Commit 25.8.1 -- Quick Order ("Pedido rápido"): pure-Python parsing of one
free-text order line ("2 cajas guantes talla L negro") into a structured
shape the future matcher (Commit 25.8.2, not built in this commit) will
consume.

Scope, per the approved Commit 25.8.1 brief -- this module deliberately does
NOT:
  - touch `frappe.db`, `frappe.get_doc`, or any other I/O (no `frappe`
    import at all -- see `normalizer.py`'s own docstring for why)
  - pick, search, or score an Item
  - compute a confidence value
  - fuzzy-match anything
  - read prices, stock, or UOM Conversion Detail
  - create or modify a Sales Order, a cart, or an Item Alias

It only interprets text. `detected_uom` is purely informational -- it is
never checked against ERPNext's own UOM doctype here, and is never assumed
to be configured as a selling UOM for any given Item (Commit 25.8 audit,
section 5: "no inventar").
"""

import re

from fabergray_erp.quick_order.normalizer import normalize_text, normalize_whitespace, strip_accents

# Raw, already accent-stripped/lowercased word -> canonical UOM label.
# Deliberately a closed, explicit list -- a word not in this dict is never
# guessed at; `detected_uom` stays None instead (see module docstring).
#
# Commit 25.8.4 -- litro/ml/kg/gramo added: confirmed real, unambiguous
# usage in the live catalog (Commit 25.8.3's own audit: LITRO 133 hits, ML
# 113, GRAMOS 18, KG 17 across 2794 real item_name values) -- not guessed,
# measured. "rollo" was ALSO confirmed real (88 hits) but is deliberately
# NOT added here: every real occurrence audited is a leading PRESENTATION
# word ("ROLLO CINTA...", "ROLLO PAPEL..."), never observed following an
# order-style quantity phrase the way "2 cajas"/"3 galones" are -- it is
# handled purely as a `presentation` token (see PRESENTATION_ALIASES below),
# not added here without that same "después de cantidad" evidence the brief
# asked for.
UOM_ALIASES = {
    "caja": "caja",
    "cajas": "caja",
    "unidad": "unidad",
    "unidades": "unidad",
    "und": "unidad",
    "galon": "galon",
    "galones": "galon",
    "paquete": "paquete",
    "paquetes": "paquete",
    "bolsa": "bolsa",
    "bolsas": "bolsa",
    "par": "par",
    "pares": "par",
    "litro": "litro",
    "litros": "litro",
    "ml": "ml",
    "kg": "kg",
    "gramo": "gramo",
    "gramos": "gramo",
}

# Written out explicitly (not derived from UOM_ALIASES' own keys) so the
# plural/abbreviation shape of each word is exactly right and easy to read --
# e.g. "unidad" pluralizes as "unidades" (not "unidad" + "s"), "par" as
# "pares" (not "par" + "s"). test_quick_order_parser.py exercises every key
# in UOM_ALIASES against this regex, which is what actually keeps the two
# lists in sync.
_UOM_WORD_RE = re.compile(
    r"^(cajas?|unidad(?:es)?|und\.?|galon(?:es)?|paquetes?|bolsas?|par(?:es)?"
    r"|litros?|ml|kg|gramos?)(?=\s|$)\s*(.*)$"
)

# Commit 25.8.4 -- commercial PRESENTATION vocabulary, a category distinct
# from UOM_ALIASES above: this dict answers "what kind of container/package
# is this", UOM_ALIASES answers "what did the qty right after the number
# mean". The two overlap on purpose (paquete/caja/galon/litro mean the same
# thing in both) but PRESENTATION_ALIASES also covers words the real
# catalog uses constantly as the LEADING word of an item_name but that were
# never observed as a customer-facing order-quantity word: bulto (100 real
# hits), cuñete (130 -- "cunete" here, post strip_accents), garrafa (17),
# botella (83). Confirmed real, all four, via Commit 25.8.4's own audit
# before adding a single one (brief section 3: "NO agregar vocabulario sin
# evidencia real").
#
# "bolsa" is deliberately ABSENT here -- Commit 25.8.2/25.8.3 already
# established (and this commit's own brief, section 6, explicitly says to
# preserve) that "bolsa" is a dual-role word: BOTH presentation-ish AND, for
# this catalog, the literal product type (a garbage bag IS "una bolsa").
# Folding it into PRESENTATION_ALIASES too would fight with
# `scoring.effective_generic_tokens()`'s own already-tested handling of it.
# "unidad"/"par" are also absent -- neither one is really a *container*
# concept the way the words above are (a "par" is a counting word, not a
# package), and this commit's own brief only asked to preserve their
# existing UOM-only behaviour, not extend it.
PRESENTATION_ALIASES = {
    "paquete": "paquete",
    "paquetes": "paquete",
    "caja": "caja",
    "cajas": "caja",
    "galon": "galon",
    "galones": "galon",
    "litro": "litro",
    "litros": "litro",
    "ml": "ml",
    "kg": "kg",
    "gramo": "gramo",
    "gramos": "gramo",
    "rollo": "rollo",
    "rollos": "rollo",
    "bulto": "bulto",
    "bultos": "bulto",
    "cunete": "cunete",
    "cunetes": "cunete",
    "garrafa": "garrafa",
    "garrafas": "garrafa",
    "botella": "botella",
    "botellas": "botella",
}

# "MEDIO GALON <producto>" -- a real, unambiguous, LEADING two-word phrase:
# 29 real item_name hits in Commit 25.8.4's own audit, every single one
# "MEDIO GALON ..." (never "GALON MEDIO", never a plural "medio galones").
# Matched and consumed as ONE canonical presentation token ("medio_galon",
# never confused with a plain "galon" match -- see this commit's own report,
# section J, for why keeping them as distinct canonical values is the safe,
# conservative choice over trying to model "medio" as a 0.5x multiplier of
# every UOM (brief section 5's own explicit warning). No plural form is
# recognized -- none was observed, so none is guessed at.
_MEDIO_GALON_RE = re.compile(r"\bmedio\s+galon\b")

# Gendered/plural surface forms -> one canonical, masculine-singular label.
# A small, explicit, easy-to-extend list -- not every Spanish color, just
# common ones a WhatsApp order is likely to use (Commit 25.8 audit, section
# C: this catalog's own `item_name` values carry color as free text, never a
# structured field).
COLOR_ALIASES = {
    "negro": "negro", "negra": "negro", "negros": "negro", "negras": "negro",
    "blanco": "blanco", "blanca": "blanco", "blancos": "blanco", "blancas": "blanco",
    "amarillo": "amarillo", "amarilla": "amarillo", "amarillos": "amarillo", "amarillas": "amarillo",
    "rojo": "rojo", "roja": "rojo", "rojos": "rojo", "rojas": "rojo",
    "azul": "azul", "azules": "azul",
    "verde": "verde", "verdes": "verde",
    "gris": "gris", "grises": "gris",
    "naranja": "naranja", "naranjas": "naranja",
    "morado": "morado", "morada": "morado", "morados": "morado", "moradas": "morado",
    "cafe": "cafe", "cafes": "cafe",
    "marron": "marron", "marrones": "marron",
    "rosado": "rosado", "rosada": "rosado", "rosados": "rosado", "rosadas": "rosado",
    "rosa": "rosa", "rosas": "rosa",
    "beige": "beige", "beiges": "beige",
    "dorado": "dorado", "dorada": "dorado", "dorados": "dorado", "doradas": "dorado",
    "plateado": "plateado", "plateada": "plateado", "plateados": "plateado", "plateadas": "plateado",
    "transparente": "transparente", "transparentes": "transparente",
}

# A leading number followed by whitespace is a CANDIDATE quantity -- the
# guard below is what stops "70 x 90 ..."/"70 por 90 ..." from ever being
# read as qty=70 (Commit 25.8 audit, section 4). A line with no leading
# digit at all ("bolsa negra 70x90") never even reaches this regex, since it
# only matches at the very start of the (already light-normalized) line.
_QTY_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s+(.*)$")
_MEASURE_GUARD_RE = re.compile(r"^(?:x|\*|por)\s*\d")

_MEASURE_TOKEN_RE = re.compile(r"\d+x\d+")

# Commit 25.8.4 -- extended to capture a fractional/range talla VERBATIM
# instead of silently truncating it to its leading integer. Real examples
# audited (Commit 25.8.4's own catalog audit): "TALLA 7 1/2" -> "7 1/2",
# "TALLA 8-8 1/2" -> "8-8 1/2" (a range ending in a fraction). The left
# alternative is tried first (digits, optional "-digits", optional a
# further " digit/digit"); a plain letter size ("L", "M", "XL") or a bare
# number falls through to the plain `[a-z0-9]+` alternative unchanged from
# Commit 25.8.1. One real, malformed legacy entry ("TALLA 7-7/2", no space
# before the fraction) only partially matches ("7-7") -- a deliberate,
# graceful degradation: this module never guesses at what a legacy typo
# meant, see this module's own docstring; that item is reported as a
# data-quality issue in Commit 25.8.4's own report, not silently "fixed"
# here.
_SIZE_RE = re.compile(r"\btalla\s+(\d+(?:-\d+)?(?:\s+\d+/\d+)?|[a-z0-9]+)\b")


def _parse_qty_number(raw):
    """"2" -> 2 (int); "2.5"/"2,5" -> 2.5 (float). Comma is always read as a
    decimal separator here, never a thousands separator -- order quantities
    in this domain (cajas, galones, pares...) are never in the thousands."""
    value = float(raw.replace(",", "."))
    return int(value) if value.is_integer() else value


def _extract_quantity_and_uom(light_normalized_text):
    """`light_normalized_text` is already accent-stripped, lowercased, and
    whitespace-collapsed (see parse_order_line()). Returns
    `(qty, uom_raw_or_None, remainder_text)` -- `uom_raw` is the raw matched
    word (e.g. "cajas", "und"), not yet canonicalized; the caller looks it
    up in UOM_ALIASES. Defaults to `(1, None, light_normalized_text)`
    whenever there is no leading quantity to extract, which is also the
    "no sobreinterpretar" fallback (Commit 25.8 audit, section 8): a bare
    "bolsa negra 70x90" is qty=1, never qty=70."""
    match = _QTY_RE.match(light_normalized_text)
    if not match:
        return 1, None, light_normalized_text

    qty_str, rest = match.group(1), match.group(2)
    if _MEASURE_GUARD_RE.match(rest):
        # The "quantity" was actually the first half of a measurement
        # ("70 x 90 ..." / "70 por 90 ..."): treat the whole line as
        # product text instead, qty defaults to 1.
        return 1, None, light_normalized_text

    qty = _parse_qty_number(qty_str)

    uom_match = _UOM_WORD_RE.match(rest)
    if uom_match:
        uom_raw = uom_match.group(1).rstrip(".")  # "und." -> "und"
        remainder = uom_match.group(2)
    else:
        uom_raw = None
        remainder = rest

    return qty, uom_raw, remainder


def extract_tokens(normalized_product_text):
    """Splits an already-`normalize_text()`-ed product string into the five
    categories the future matcher (Commits 25.8.2/25.8.4) weight separately.
    No scoring, no Item lookup -- see this module's own docstring.

    Extraction order matters, each stage replacing its own match with a
    space (never deleting outright, so unrelated neighbouring words never
    fuse together) before the next stage runs on what remains:
    measure ("70x90") -> size ("talla l" -> "l", or a fractional/range talla
    verbatim) -> presentation (the "medio galon" phrase FIRST, then single
    PRESENTATION_ALIASES words) -> whatever single words remain are checked
    against COLOR_ALIASES -- anything still left over is `generic`.

    `presentation` (Commit 25.8.4) is shaped differently from the other four
    -- `{"primary": str | None, "contained": [str, ...]}` -- because, unlike
    color/size/measure, WHERE a presentation word sits matters: "BULTO BOLSA
    70X90 BLANCA X 40 PAQUETE X 10 UND" is fundamentally a BULTO that happens
    to contain packs of "paquete" inside it, not the other way around. The
    conservative, position-based rule (Commit 25.8.4 brief, section 9): the
    presentation match that starts EARLIEST in the text is `primary`,
    everything else recognized is `contained`. This is exactly why
    PRESENTATION_ALIASES is checked as a dedicated regex-driven pass (with
    real start positions available) rather than folded into the same
    single-word loop COLOR_ALIASES uses below, where "first" would have no
    meaning once `text.split()` has already thrown position away.
    """
    text = normalized_product_text or ""

    measures = _MEASURE_TOKEN_RE.findall(text)
    text = _MEASURE_TOKEN_RE.sub(" ", text)

    sizes = []

    def _consume_size(match):
        sizes.append(match.group(1))
        return " "

    text = _SIZE_RE.sub(_consume_size, text)

    presentation_hits = []  # [(start_index, canonical), ...], in match order

    def _consume_medio_galon(match):
        presentation_hits.append((match.start(), "medio_galon"))
        return " "

    text = _MEDIO_GALON_RE.sub(_consume_medio_galon, text)

    remaining_words = []
    cursor = 0
    for word in text.split():
        # Re-locate this word's real start position in `text` (post the
        # measure/size/medio-galon removals above, which already replaced
        # their matches with spaces -- `text.split()` alone throws position
        # away, so it is recovered here via a forward-only `str.find()` from
        # the last cursor, cheap and always correct since words never
        # repeat-overlap after whitespace collapsing).
        start = text.find(word, cursor)
        cursor = start + len(word)
        canonical_presentation = PRESENTATION_ALIASES.get(word)
        if canonical_presentation:
            presentation_hits.append((start, canonical_presentation))
        else:
            remaining_words.append(word)

    primary_presentation = None
    contained_presentation = []
    if presentation_hits:
        presentation_hits.sort(key=lambda hit: hit[0])
        primary_presentation = presentation_hits[0][1]
        contained_presentation = [canonical for _, canonical in presentation_hits[1:]]

    colors = []
    generic = []
    for word in remaining_words:
        canonical_color = COLOR_ALIASES.get(word)
        if canonical_color:
            colors.append(canonical_color)
        else:
            generic.append(word)

    return {
        "generic": generic,
        "measure": measures,
        "size": sizes,
        "color": colors,
        "presentation": {"primary": primary_presentation, "contained": contained_presentation},
    }


def parse_order_line(text):
    """Parses one free-text order line into a structured dict. Pure text
    interpretation only -- see this module's own docstring for the full
    list of what it deliberately does NOT do.

    Returns:
        {
            "source_text": str,             # exactly as received
            "qty": int | float,             # 1 if none was written explicitly
            "detected_uom": str | None,     # canonical label or None -- never guessed
            "product_text": str,            # remainder after qty/uom, casually cleaned
            "normalized_product_text": str, # full normalize_text() of product_text
            "tokens": {
                "generic": [str, ...],
                "measure": [str, ...],  # "70x90"-shaped
                "size": [str, ...],     # value only, e.g. "l", "9", or "7 1/2"
                "color": [str, ...],    # canonical color labels
                "presentation": {       # Commit 25.8.4 -- see extract_tokens()'s own docstring
                    "primary": str | None,
                    "contained": [str, ...],
                },
            },
        }
    """
    source_text = text or ""

    light_normalized = normalize_whitespace(strip_accents(source_text).lower())
    qty, uom_raw, remainder = _extract_quantity_and_uom(light_normalized)

    detected_uom = UOM_ALIASES.get(uom_raw) if uom_raw else None
    product_text = remainder.strip()
    normalized_product_text = normalize_text(product_text)
    tokens = extract_tokens(normalized_product_text)

    return {
        "source_text": source_text,
        "qty": qty,
        "detected_uom": detected_uom,
        "product_text": product_text,
        "normalized_product_text": normalized_product_text,
        "tokens": tokens,
    }


def parse_order_text(text):
    """Splits a pasted, multi-line order into a list of `parse_order_line()`
    results, one per non-empty line, in original order. Blank/whitespace-
    only lines are silently skipped -- never produce a placeholder row for
    them."""
    lines = (text or "").splitlines()
    return [parse_order_line(line) for line in lines if line.strip()]
