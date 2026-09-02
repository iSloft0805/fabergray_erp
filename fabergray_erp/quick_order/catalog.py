# -*- coding: utf-8 -*-
"""Commit 25.8.3 -- read-only ERPNext integration layer for Quick Order.

This is the ONLY module in `fabergray_erp/quick_order/` allowed to import
`frappe` -- `normalizer.py`/`parser.py`/`scoring.py`/`matcher.py` (Commits
25.8.1/25.8.2) stay exactly as pure as they already were, verified again by
this commit's own `ast`-based guardrail test (see
`test_quick_order_catalog.py`). Every function here does a `frappe.get_list`
read (never `get_all`, matching the rest of this app's own convention -- see
`api/ventas.py`'s own `search_items()`), never an insert/update/delete,
never reads price/stock/`Item Price`/`Bin`.

No endpoint is wired to this yet (Commit 25.8.4's job, not this one's) --
this module exists so the real ~2794-item catalog can be measured against
the engine before any UI or endpoint exists at all, per this commit's own
brief ("Primero quiero medir calidad real antes de exponer un endpoint").
"""

import difflib

import frappe

from fabergray_erp.quick_order import scoring
from fabergray_erp.quick_order.matcher import build_candidate

# Mirrors api/ventas.py's search_items() filter exactly (Commit 18.2) --
# "is this a real, directly sellable product", the same native ERPNext
# filters, not a new criterion invented here (Commit 25.8 audit, section B).
SELLABLE_ITEM_FILTERS = {"disabled": 0, "is_sales_item": 1, "has_variants": 0}

# item_code/item_name/description/item_group/stock_uom only -- never
# valuation_rate/standard_rate/last_purchase_rate or anything that would
# require a second query against Item Price/Bin/Stock Ledger Entry. `brand`
# is deliberately NOT loaded -- this commit's own audit confirmed it is set
# on 0 of 2794 sellable Items, so loading it would add a DB column for zero
# matching value (Commit 25.8 audit, section C; re-confirmed live in this
# commit's own report, section R).
SELLABLE_ITEM_FIELDS = ["item_code", "item_name", "description", "item_group", "stock_uom"]

# frappe.cache() key for the built catalog (candidates + inverted index).
# Versioned (the trailing "v1") so a future incompatible shape change ships
# under a new key instead of silently deserializing an old one.
CACHE_KEY = "fabergray:quick_order:item_catalog:v1"

# 15 minutes -- see this commit's own report, section F, for the full
# reasoning (measured build cost ~450ms, no hook-based invalidation in this
# commit, a short TTL is the deliberate stand-in).
CACHE_TTL_SECONDS = 900

# A token whose index bucket is smaller than this needs no fuzzy fallback --
# tier 1 (exact/singular-plural lookup) already found enough to work with.
# See search_catalog_candidates()'s own docstring for the 3-tier design.
_MIN_POOL_BEFORE_FUZZY_FALLBACK = 5

# Safety cap on how many candidates ever reach match_order_line() for one
# line -- NOT the "top 5" the UI eventually shows (matcher.py's own
# `limit`), a much looser bound so a token that happens to be common (e.g.
# "paquete", which appears in 423 real item names) can't blow up per-line
# scoring cost. Calibrated against this commit's own real-catalog
# measurements (see report, section H) -- generous on purpose: correctness
# over micro-optimization for a catalog this size, per this commit's own
# brief (section 6).
DEFAULT_LIMIT_POOL = 300


def get_sellable_item_candidates():
    """One `frappe.get_list` read (never `get_all`), built into `Candidate`
    dicts via `matcher.build_candidate()` -- the exact same construction
    Commit 25.8.2's own synthetic tests use, just fed from real rows instead
    of hand-written ones. No price, no stock, no Item Price/Bin read at
    all."""
    frappe.has_permission("Item", "read", throw=True)
    rows = frappe.get_list(
        "Item",
        filters=SELLABLE_ITEM_FILTERS,
        fields=SELLABLE_ITEM_FIELDS,
        order_by="item_name asc",
        limit_page_length=0,
    )
    return [
        build_candidate(
            row.item_code,
            row.item_name,
            description=row.description,
            item_group=row.item_group,
            stock_uom=row.stock_uom,
        )
        for row in rows
    ]


def build_inverted_index(candidates):
    """token -> {item_code, ...} across every token category (generic,
    measure, size, color) of every candidate, plus item_code -> Candidate
    for O(1) hydration. Pure -- takes whatever candidate list it is given,
    real (this module) or synthetic (test_quick_order_catalog.py's own unit
    tests, no DB needed for those)."""
    token_index = {}
    by_code = {}
    for candidate in candidates:
        by_code[candidate["item_code"]] = candidate
        tokens = candidate["tokens"]
        presentation = tokens["presentation"]
        # Commit 25.8.4 -- both primary AND contained presentation tokens
        # are indexed (a "1 paquete..." query must be able to retrieve a
        # BULTO candidate that only CONTAINS "paquete", not just the ones
        # where it is primary -- scoring.py's own contained-presentation
        # partial credit needs that candidate in the pool to begin with).
        presentation_tokens = ([presentation["primary"]] if presentation["primary"] else []) + presentation[
            "contained"
        ]
        all_tokens = (
            set(tokens["generic"])
            | set(tokens["measure"])
            | set(tokens["size"])
            | set(tokens["color"])
            | set(presentation_tokens)
        )
        for token in all_tokens:
            token_index.setdefault(token, set()).add(candidate["item_code"])
    return {"token_index": token_index, "by_code": by_code}


def get_cached_catalog():
    """Read-through cache: a hit returns the cached `{"token_index",
    "by_code"}` structure as-is; a miss rebuilds it from ERPNext (the ~450ms
    combined cost measured in this commit's own report, section J) and
    stores it for `CACHE_TTL_SECONDS`. See `invalidate_catalog_cache()` for
    why a short TTL -- not a hook -- is this commit's deliberate choice."""
    cached = frappe.cache().get_value(CACHE_KEY)
    if cached is not None:
        return cached
    index = build_inverted_index(get_sellable_item_candidates())
    frappe.cache().set_value(CACHE_KEY, index, expires_in_sec=CACHE_TTL_SECONDS)
    return index


def invalidate_catalog_cache():
    """Not wired to any Item hook in this commit -- see the report's own
    section G for why: this app's `hooks.py` has no `doc_events` entry for
    Item today, and adding one is a decision with a blast radius well
    beyond Quick Order (every Item save on the site), not something to slip
    in as a side effect of this commit. Exposed here so a future commit
    (or an ad-hoc console call after a bulk Item edit) can force a rebuild
    without waiting out the TTL."""
    frappe.cache().delete_value(CACHE_KEY)


def _fuzzy_vocabulary_matches(effective_generic_tokens, vocabulary):
    """Tier-2 fallback (Commit 25.8.3 brief, section 6): scans the index's
    OWN vocabulary (a few thousand unique tokens, cheap to scan once -- see
    report section J) for a conservative fuzzy match to a query token that
    had no exact/singular-plural hit at all. Same threshold/length floor as
    `scoring.py`'s own typo tolerance (Commit 25.8.2) -- never a second,
    looser standard invented here."""
    hits = set()
    for token in effective_generic_tokens:
        stem = scoring._singularize(token)
        if len(stem) < scoring.FUZZY_TOKEN_MIN_LENGTH:
            continue
        for vocab_token in vocabulary:
            if difflib.SequenceMatcher(None, stem, vocab_token).ratio() >= scoring.FUZZY_TOKEN_MIN_RATIO:
                hits.add(vocab_token)
    return hits


def search_catalog_candidates(parsed_line, catalog=None, limit_pool=DEFAULT_LIMIT_POOL):
    """The retrieval stage in front of `matcher.match_order_line()` --
    returns a POOL of `Candidate` dicts (NOT yet scored or trimmed to 5;
    that is `match_order_line()`'s own job, called separately by the
    caller) instead of handing it the full ~2794-item catalog every time.

    Three tiers, in order, each one only runs if the previous one came up
    short -- "el índice es una optimización, no una nueva regla de negocio"
    (Commit 25.8.3 brief, section 6):

    1. Exact / singular-plural index lookup on the line's own effective
       generic tokens (`scoring.effective_generic_tokens()` -- includes
       `detected_uom`, same reasoning as Commit 25.8.2) plus its
       measure/size/color/presentation tokens (Commit 25.8.4 adds
       `scoring.effective_primary_presentation()` to this list). Covers the
       overwhelming majority of real queries at negligible cost (a handful
       of dict lookups + set unions).
    2. If that pool is still smaller than `_MIN_POOL_BEFORE_FUZZY_FALLBACK`,
       fuzzy-scan the index's own vocabulary (`_fuzzy_vocabulary_matches()`)
       for a conservative typo match, then pull in whatever those tokens
       index to. This is what keeps "guate negro talla l" from ever
       retrieving zero candidates just because "guate" has no exact bucket
       (Commit 25.8.2's own `matcher._passes_minimum_signal()` already does
       the identical fuzzy check per-candidate for a SMALL synthetic list;
       doing it against the index's vocabulary instead of every candidate's
       raw tokens is what makes it cheap at real-catalog scale).
    3. If the pool is STILL empty after both tiers (a token nothing in the
       vocabulary even resembles) but the line has SOME token at all
       (generic/measure/size/color), fall back to the full catalog rather
       than ever returning zero for a line that plausibly has real intent
       behind it -- "correctness > micro-optimización" for a catalog this
       size (brief, section 6). A line with genuinely no tokens at all
       (`parse_order_line("")`) still returns an empty pool -- there is
       nothing to search for.

    `limit_pool` is a loose safety cap (default 300, see
    `DEFAULT_LIMIT_POOL`'s own docstring for how it was calibrated) --
    almost never hit by tier 1/2, only relevant for a token that happens to
    be extremely common in the real catalog (e.g. "paquete", 423 hits) or
    for the tier-3 full-catalog fallback itself.
    """
    catalog = catalog if catalog is not None else get_cached_catalog()
    token_index = catalog["token_index"]
    by_code = catalog["by_code"]

    effective_generic = scoring.effective_generic_tokens(parsed_line)
    other_signal_tokens = (
        parsed_line["tokens"]["measure"] + parsed_line["tokens"]["size"] + parsed_line["tokens"]["color"]
    )
    # Commit 25.8.4 -- the line's own effective primary presentation
    # (paquete/caja/galon/bulto/...) is also a valid retrieval signal, same
    # reasoning as matcher._passes_minimum_signal()'s own presentation check.
    line_presentation = scoring.effective_primary_presentation(parsed_line)
    if line_presentation:
        other_signal_tokens = other_signal_tokens + [line_presentation]

    # item_code -> number of distinct index buckets that matched it, not
    # just a flat set -- see this function's own report entry (Commit
    # 25.8.3, "H"/"Q" edge cases) for why: a plain set union of common
    # buckets ("negro" alone hits 193 real items, "caja" 184) can exceed
    # `limit_pool` on an ordinary line, and truncating an unordered
    # `set(...)[:n]` is not reproducible (Python's string hash is
    # randomized per process) -- it could silently drop the correct
    # candidate on a different run. Sorting by hit-count (most signals
    # matched first) before truncating fixes both problems: it is fully
    # deterministic, and a candidate that matches on type+measure+size+
    # color is guaranteed to outrank one that only ever matched a single,
    # generic color bucket -- so truncation only ever discards the least
    # relevant candidates, never the right one.
    hits = {}

    def _add_codes(codes):
        for code in codes:
            hits[code] = hits.get(code, 0) + 1

    for token in effective_generic:
        _add_codes(token_index.get(token, ()))
        stem = scoring._singularize(token)
        if stem != token:
            _add_codes(token_index.get(stem, ()))
    for token in other_signal_tokens:
        _add_codes(token_index.get(token, ()))

    if len(hits) < _MIN_POOL_BEFORE_FUZZY_FALLBACK and effective_generic:
        for vocab_token in _fuzzy_vocabulary_matches(effective_generic, token_index.keys()):
            _add_codes(token_index[vocab_token])

    if not hits and (effective_generic or other_signal_tokens):
        _add_codes(by_code.keys())  # tier 3 -- full catalog, last resort

    ranked_codes = sorted(hits.keys(), key=lambda code: (-hits[code], code))
    return [by_code[code] for code in ranked_codes[:limit_pool] if code in by_code]
