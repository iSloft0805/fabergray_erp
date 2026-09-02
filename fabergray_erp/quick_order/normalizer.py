# -*- coding: utf-8 -*-
"""Commit 25.8.1 -- pure-Python text normalization for Quick Order ("Pedido
rápido").

No `frappe` import, no DB access, no I/O of any kind: every function here is
a plain string -> string transformation, deliberately kept independent of
the Frappe framework so it can be unit-tested (and reasoned about) without a
site/DB context at all -- see the Commit 25.8 audit, section D. This module
only normalizes text; it never picks/scores an Item, never touches a Sales
Order, and is never called with anything from the database.
`fabergray_erp/quick_order/parser.py` is the only consumer today.
"""

import re
import unicodedata

# Digits on both sides -- "70x90" / "70 x 90" / "70*90" / "70 por 90" are all
# the same measurement. "por" as an ordinary word elsewhere in a sentence
# (e.g. the quantity guard in parser.py) is left untouched since this only
# fires between two numbers.
_MEASURE_SEPARATOR_RE = re.compile(r"(\d+)\s*(?:x|\*|por)\s*(\d+)")

# Anything that is not a letter, a digit, or whitespace is noise for
# matching purposes (",./-()!?\"'" etc.). Digits are NEVER stripped --
# tallas/medidas/presentaciones/referencias depend on them (Commit 25.8
# audit, section 2).
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)

_WHITESPACE_RE = re.compile(r"\s+")


def strip_accents(text):
    """"GALÓN"/"botón" -> "GALON"/"boton" -- decomposes (NFKD) and drops
    combining marks, never a hardcoded á/é/í/ó/ú/ñ lookup table. As a side
    effect "ñ" also loses its tilde ("años" -> "anos") -- an accepted,
    deliberate simplification for informal WhatsApp text, not an oversight."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_whitespace(text):
    """Collapses any run of whitespace to a single space and trims the ends."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def normalize_measure_separators(text):
    """"70 por 90" / "70 x 90" / "70*90" -> "70x90" -- one canonical form.
    Applied globally (re.sub, not a single match) so a line with more than
    one measurement is still handled in one pass."""
    return _MEASURE_SEPARATOR_RE.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text or "")


def clean_punctuation(text):
    """Drops punctuation that carries no product-matching signal, replacing
    it with a space (never deleting outright) so two words separated only by
    punctuation -- "guantes,negro" -- don't get fused into one token."""
    return _PUNCTUATION_RE.sub(" ", text or "")


def normalize_text(text):
    """The full pipeline, in this exact order (each step's precondition is
    the previous one's output):
    accents -> lowercase -> measure separators -> punctuation -> whitespace.

    Measure-separator normalization runs BEFORE punctuation cleanup on
    purpose -- "*" and "x" would otherwise already be gone (clean_punctuation
    strips "*") or never recognized as a separator once punctuation is
    stripped. Idempotent -- running it twice on its own output is a no-op.
    """
    text = strip_accents(text or "")
    text = text.lower()
    text = normalize_measure_separators(text)
    text = clean_punctuation(text)
    return normalize_whitespace(text)
