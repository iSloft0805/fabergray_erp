# -*- coding: utf-8 -*-
"""Commit 25.8.2 -- Quick Order: pure-Python, explainable scoring of one
`parser.parse_order_line()` result against one candidate product.

Same purity contract as `normalizer.py`/`parser.py` (Commit 25.8.1): no
`frappe` import, no DB, no I/O -- every function here is deterministic given
plain Python dicts. `matcher.py` is the only consumer; this module never
picks a "winner" or ranks anything on its own -- see `score_candidate()`'s
own docstring for exactly what it returns and why that's `matcher.py`'s job
instead.

Weights and thresholds (Commit 25.8 audit, sections 15/17 -- and this
commit's own brief, sections 7/8/15/17): explicitly initial values, centralized
here as named constants, never hardcoded inline or duplicated in matcher.py.
They are a heuristic score, not a statistical probability -- recalibrating
them against the real ~2794-item catalog is explicitly Commit 25.8.3's job,
not this one's.
"""

import difflib

from fabergray_erp.quick_order.parser import PRESENTATION_ALIASES

# ---------------------------------------------------------------------------
# Weights -- priority order per the brief's own section 8, with Commit
# 25.8.4's `presentation` dimension slotted in per THAT commit's own section
# 17 (a presentation match/conflict must never outweigh, or get cancelled
# out by, a measure/talla/color contradiction):
#   1. tipo/producto      (GENERIC_MATCH_POINTS + GENERIC_COVERAGE_BONUS_MAX)
#   2. medida exacta       (MEASURE_MATCH_POINTS / MEASURE_CONFLICT_PENALTY)
#   3. talla exacta        (SIZE_MATCH_POINTS / SIZE_CONFLICT_PENALTY)
#   4. color exacto        (COLOR_MATCH_POINTS / COLOR_CONFLICT_PENALTY)
#   5. presentación (paquete/caja/galon/bulto/...) -- Commit 25.8.4, weighted
#      BELOW color on purpose: it distinguishes real variants of the SAME
#      product (PAQUETE vs BULTO of the same bolsa) rather than the product
#      itself, so it should never outrank an actual color/talla/measure
#      signal (PRESENTATION_MATCH_POINTS / PRESENTATION_CONFLICT_PENALTY,
#      plus CONTAINED_PRESENTATION_BONUS -- see score_candidate()'s own
#      "PAQUETE vs BULTO" handling)
#   6. cobertura de tokens genéricos (folded into GENERIC_COVERAGE_BONUS_MAX,
#      deliberately smaller than measure/size/color's own match bonuses)
#   7. similitud textual auxiliar    (TEXT_SIMILARITY_BONUS_MAX -- typo aid)
# Positive maxima sum to 60+10+15+10+8+6+2+3 = 114, so the final score is
# always clamped to [0, 100] (see score_candidate()) -- stacking every bonus
# at once is rare in practice, the clamp is just the safety net.
# ---------------------------------------------------------------------------
GENERIC_MATCH_POINTS = 60
GENERIC_COVERAGE_BONUS_MAX = 10
MEASURE_MATCH_POINTS = 15
MEASURE_CONFLICT_PENALTY = 40
SIZE_MATCH_POINTS = 10
SIZE_CONFLICT_PENALTY = 35
COLOR_MATCH_POINTS = 8
COLOR_CONFLICT_PENALTY = 25

# Commit 25.8.4 -- deliberately softer than color's own -25/+8: a
# presentation mismatch (ordering "paquete", candidate is a "bulto") is a
# real, useful signal but not nearly as disqualifying as a wrong color or
# measure -- both are still explicit product ATTRIBUTES the customer stated,
# while presentation is closer to "which SKU of the same product" (this
# commit's own brief, section 17: "no debe quedar anulada por una
# presentación coincidente", but also section 8: "NO descartar completamente
# BULTO -- debe quedar como alternativa con penalización"). Simulated against
# the real "PAQUETE BOLSA 70X90..." vs "BULTO BOLSA 70X90...X40 PAQUETE..."
# pair (Commit 25.8.3's own false positive #1) before picking these exact
# numbers -- see this commit's own report, section I, for the worked score.
PRESENTATION_MATCH_POINTS = 6
PRESENTATION_CONFLICT_PENALTY = 12

# Commit 25.8.4 -- Commit 25.8.3's own "no basta con `if 'paquete' in
# item_name`" finding (BULTO BOLSA 70X90...X40 PAQUETE X10 UND contains the
# word "paquete" too, just not as its PRIMARY presentation): a candidate
# whose primary presentation conflicts with the line's, but that still
# CONTAINS the line's requested presentation somewhere in its own name (the
# "PAQUETE" in that BULTO), gets partial credit back -- keeps it as a
# plausible, lower-ranked alternative instead of a full -12, matching this
# commit's own brief section 8 ("no descartar completamente").
CONTAINED_PRESENTATION_BONUS = 2

TEXT_SIMILARITY_BONUS_MAX = 3

# Confidence bands (Commit 25.8.2 brief, section 15) -- a score, not a
# statistical probability. Centralized here, never re-declared elsewhere.
CONFIDENCE_HIGH_THRESHOLD = 90
CONFIDENCE_MEDIUM_THRESHOLD = 70

# Ambiguity margin (section 17): if the #1 and #2 candidates for the same
# line score within this many points of each other, the line is flagged
# `ambiguous` regardless of how high #1's own score is -- "guantes negros"
# (no talla specified) scoring TALLA L and TALLA M almost identically is
# exactly the case this exists for (see test_quick_order_matching.py's own
# real-world case G). 8 is the brief's own worked example, adopted as-is;
# revisit once calibrated against the real catalog (Commit 25.8.3).
AMBIGUITY_MARGIN_THRESHOLD = 8

# Typo tolerance (section 18) -- auxiliary and conservative on purpose:
# ratio computed with the stdlib's own difflib.SequenceMatcher, never a new
# dependency. 0.82 keeps "guate"/"guante" (0.909) matching while keeping
# unrelated product-type words apart (measured empirically while building
# this commit: "nitrilo"/"latex" = 0.167, "guante"/"bolsa" = 0.182, every
# UOM word against every synthetic product word stayed below 0.6 except the
# literal "bolsa"/"bolsa" identity). A minimum token length of 4 keeps short
# tokens (talla/color values like "l", "9") out of fuzzy matching entirely --
# those are compared for EXACT equality only, in score_candidate() below,
# never fuzzily (a typo'd talla must never be "forgiven").
FUZZY_TOKEN_MIN_RATIO = 0.82
FUZZY_TOKEN_MIN_LENGTH = 4

# A small, explicit, conservative singular<->plural map -- exactly the
# "casos básicos necesarios" the brief's own section 6 lists (guante/bolsa/
# galon), not a general Spanish stemmer. Deliberately NOT a suffix-stripping
# rule: Spanish pluralization is irregular enough ("guante"->"guantes" adds
# only "s", "galón"->"galones" adds "es") that a blind suffix rule would
# either under- or over-strip depending on the word -- see this commit's own
# report for the concrete cases that ruled that approach out. Extend this
# dict, one explicit pair at a time, if a real order text needs another.
_PLURAL_TO_SINGULAR = {
    "guantes": "guante",
    "bolsas": "bolsa",
    "galones": "galon",
}


def _singularize(word):
    return _PLURAL_TO_SINGULAR.get(word, word)


def effective_generic_tokens(parsed_line):
    """The line's own `tokens.generic` PLUS its `detected_uom`, if any.

    Why: `parser.py` (Commit 25.8.1, unmodified) strips a recognized UOM
    word off the front of the line before tokenizing the rest -- correct for
    its own job, but it means a word like "bolsa" in "1 bolsa negra 70x90"
    never reaches `tokens.generic` even though "bolsa" is ALSO this
    catalog's actual product-type word for that item ("BOLSA NEGRA 70X90").
    Folding `detected_uom` back in here (matcher-side only, `parsed_line`
    itself is never mutated) lets a candidate whose own name literally
    contains that word ("bolsa", "galon"...) score exactly like an ordinary
    generic-token match, through the identical mechanism -- no separate
    "does detected_uom appear in item_name" bonus is needed (Commit 25.8.2
    brief, section 12: "puede ayudar únicamente si aparece explícitamente en
    item_name" -- exact generic-token matching against the candidate's own
    tokens already IS that check). Verified empirically (this commit's own
    audit) that no UOM word fuzzy-collides with any unrelated product-type
    token in the synthetic catalog, so this is safe to do unconditionally,
    not just for container-like words."""
    tokens = list(parsed_line["tokens"]["generic"])
    if parsed_line.get("detected_uom"):
        tokens.append(parsed_line["detected_uom"])
    return tokens


# Every canonical value PRESENTATION_ALIASES can produce, plus "medio_galon"
# (the one presentation token that comes from a phrase match, not a
# single-word dict lookup -- see parser.py's own `_MEDIO_GALON_RE`). Used
# ONLY to decide whether a line's `detected_uom` is a genuine presentation
# concept (caja/paquete/galon/litro/ml/kg/gramo) as opposed to a plain
# counting word that happens to also be a recognized UOM ("unidad", "par")
# or the dual-role "bolsa" (Commit 25.8.2/25.8.3's own established
# behaviour, deliberately untouched -- see effective_primary_presentation()'s
# own docstring for why treating THOSE as a presentation signal would create
# false conflicts).
_PRESENTATION_CANONICAL_VALUES = set(PRESENTATION_ALIASES.values()) | {"medio_galon"}


def effective_primary_presentation(parsed_line):
    """Commit 25.8.4 -- the line's own effective "what container/presentation
    was requested", combining two sources exactly the way
    `effective_generic_tokens()` above already combines `detected_uom` with
    `tokens.generic`: `detected_uom` wins if it is itself a genuine
    presentation concept (e.g. "2 cajas guantes..." -> "caja", stripped off
    the front by `parser.py` before `tokens.presentation` is ever computed),
    otherwise whatever `tokens.presentation.primary` found within the
    remaining product text (e.g. "botella desengrasante", no leading
    quantity at all -> "botella"). `None` if neither source has anything --
    most lines simply don't specify a presentation, and that is never
    treated as a contradiction (see `score_candidate()`'s own presentation
    block).

    `detected_uom` values like "unidad"/"par" (real UOM words, but not
    presentation/container concepts) and "bolsa" (this catalog's own
    dual-role product/container word, Commit 25.8.2/25.8.3) are deliberately
    NOT treated as a presentation signal here -- only a value that actually
    appears in PRESENTATION_ALIASES' own canonical output is."""
    detected_uom = parsed_line.get("detected_uom")
    if detected_uom in _PRESENTATION_CANONICAL_VALUES:
        return detected_uom
    return parsed_line["tokens"]["presentation"]["primary"]


def generic_token_matches(line_generic_tokens, candidate_generic_tokens):
    """Compares two generic-token lists (already `effective_generic_tokens()`
    for the line side) via singular/plural-normalized EXACT equality first;
    a line token with no exact match falls back to a conservative fuzzy
    check (see FUZZY_TOKEN_MIN_RATIO/_LENGTH above) against the candidate's
    own tokens.

    Returns `(exact: set[str], fuzzy: list[(line_token, candidate_token,
    ratio)])` -- both keyed by the ORIGINAL (non-singularized) line token,
    so callers can report back what the order actually said."""
    candidate_stems = {_singularize(tok) for tok in candidate_generic_tokens}

    exact = set()
    fuzzy = []
    for tok in line_generic_tokens:
        stem = _singularize(tok)
        if stem in candidate_stems:
            exact.add(tok)
            continue
        if len(stem) < FUZZY_TOKEN_MIN_LENGTH:
            continue
        best = None
        for candidate_tok in candidate_generic_tokens:
            ratio = difflib.SequenceMatcher(None, stem, _singularize(candidate_tok)).ratio()
            if ratio >= FUZZY_TOKEN_MIN_RATIO and (best is None or ratio > best[1]):
                best = (candidate_tok, ratio)
        if best:
            fuzzy.append((tok, best[0], best[1]))

    return exact, fuzzy


def category_signal(line_values, candidate_values):
    """"match" if the line and the candidate share at least one value in
    this category (measure/size/color); "conflict" if BOTH declare a value
    and they share none (an explicit contradiction -- Commit 25.8.2 brief,
    section 4); "neutral" if either side simply has nothing to say about
    this category -- omission is never treated as a contradiction."""
    if not line_values or not candidate_values:
        return "neutral"
    if set(line_values) & set(candidate_values):
        return "match"
    return "conflict"


def classify_confidence(score):
    """Separate, explicit classification from the raw 0-100 score (section
    15) -- centralizes the two thresholds so nothing downstream re-derives
    its own "is this high enough" logic."""
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return "high"
    if score >= CONFIDENCE_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def _apply_category(category, match_points, conflict_penalty, line_values, candidate_values, label):
    """One category's contribution to the score, plus the explanation
    pieces that feed `matched_tokens`/`conflicts`/`reasons` -- returns
    `(score_delta, matched_values, conflict_entry_or_None,
    reason_or_None)`. A pure helper, no shared mutable state, so
    `score_candidate()` below just sums the deltas -- see its own docstring
    for why that reads more plainly than an in-place accumulator."""
    signal = category_signal(line_values, candidate_values)
    if signal == "match":
        shared = sorted(set(line_values) & set(candidate_values))
        return match_points, shared, None, f"{label} coincide: {', '.join(shared)}"
    if signal == "conflict":
        conflict = {
            "category": category,
            "order_value": list(line_values),
            "candidate_value": list(candidate_values),
        }
        reason = f"{label} en conflicto: pedido={list(line_values)} vs candidato={list(candidate_values)}"
        return -conflict_penalty, [], conflict, reason
    return 0, [], None, None


def score_candidate(parsed_line, candidate):
    """Scores ONE candidate against ONE already-parsed order line. Pure
    computation -- no ranking, no Item lookup, no "pick the best one": that
    is `matcher.py`'s job (`match_order_line()`), one layer up, once every
    candidate in a pool has its own score.

    Returns:
        {
            "item_code": str,
            "item_name": str,
            "score": int,               # 0-100, clamped
            "confidence": "high"|"medium"|"low",
            "matched_tokens": [str, ...],
            "conflicts": [{"category", "order_value", "candidate_value"}, ...],
            "reasons": [str, ...],
        }
    """
    line_tokens = parsed_line["tokens"]
    candidate_tokens = candidate["tokens"]

    score = 0.0
    matched_tokens = []
    conflicts = []
    reasons = []

    effective_generic = effective_generic_tokens(parsed_line)
    if effective_generic:
        exact, fuzzy = generic_token_matches(effective_generic, candidate_tokens["generic"])
        if exact:
            score += GENERIC_MATCH_POINTS
            matched_tokens.extend(sorted(exact))
            reasons.append(f"tipo de producto coincide: {', '.join(sorted(exact))}")
        elif fuzzy:
            best_ratio = max(ratio for _, _, ratio in fuzzy)
            score += GENERIC_MATCH_POINTS * best_ratio
            matched_tokens.extend(sorted({candidate_tok for _, candidate_tok, _ in fuzzy}))
            pairs = ", ".join(f"{line_tok}~{candidate_tok}" for line_tok, candidate_tok, _ in fuzzy)
            reasons.append(f"tipo de producto probable por similitud de texto: {pairs}")

        # Coverage is measured against the line's own descriptive words
        # (tokens.generic), NOT the full effective_generic set used for
        # match DETECTION above -- detected_uom ("caja" in "2 cajas
        # guantes...") is a legitimate way to let a candidate match (see
        # effective_generic_tokens()'s own docstring), but it is not
        # descriptive text, and counting it in the coverage denominator
        # would dilute the correct candidate's own score just because an
        # unrelated UOM word never appears in that candidate's name either
        # (discovered empirically while building this commit -- "2 cajas
        # guantes talla L negro" scored 5 points lower than it should have
        # before this fix, purely because "caja" doesn't appear in "GUANTE
        # NITRILO NEGRO TALLA L" and was dragging the denominator up).
        # Falls back to effective_generic only when the line has NO
        # descriptive word of its own at all (e.g. "1 bolsa negra 70x90",
        # where "bolsa" IS the only generic signal, via detected_uom).
        fuzzy_line_tokens = {line_tok for line_tok, _, _ in fuzzy}
        coverage_basis = line_tokens["generic"] or effective_generic
        matched_in_basis = sum(1 for tok in coverage_basis if tok in exact or tok in fuzzy_line_tokens)
        score += GENERIC_COVERAGE_BONUS_MAX * (matched_in_basis / len(coverage_basis))

    for category, points, penalty, line_values, candidate_values, label in (
        ("measure", MEASURE_MATCH_POINTS, MEASURE_CONFLICT_PENALTY, line_tokens["measure"], candidate_tokens["measure"], "medida"),
        ("size", SIZE_MATCH_POINTS, SIZE_CONFLICT_PENALTY, line_tokens["size"], candidate_tokens["size"], "talla"),
        ("color", COLOR_MATCH_POINTS, COLOR_CONFLICT_PENALTY, line_tokens["color"], candidate_tokens["color"], "color"),
    ):
        delta, matched, conflict, reason = _apply_category(category, points, penalty, line_values, candidate_values, label)
        score += delta
        matched_tokens.extend(matched)
        if conflict:
            conflicts.append(conflict)
        if reason:
            reasons.append(reason)

    # Commit 25.8.4 -- presentation (paquete/caja/galon/bulto/...), weighted
    # below color on purpose (see this module's own weights docstring).
    # Deliberately NOT `_apply_category()` (which only knows exact-match-vs-
    # conflict-vs-neutral over two flat lists): a presentation conflict gets
    # PARTIAL credit back if the line's requested presentation still shows up
    # as a CONTAINED presentation on the candidate -- e.g. ordering "paquete"
    # against a candidate whose primary is "bulto" but that also contains
    # "paquete" somewhere in its own name (Commit 25.8.3's own false
    # positive #1: "BULTO BOLSA 70X90...X 40 PAQUETE X 10 UND"). Omission on
    # either side (line didn't ask for a presentation, or the candidate's
    # own name has none) is neutral, same convention as every other category
    # here -- never a contradiction.
    line_presentation = effective_primary_presentation(parsed_line)
    candidate_presentation = candidate_tokens["presentation"]
    candidate_primary_presentation = candidate_presentation["primary"]
    if line_presentation and candidate_primary_presentation:
        if line_presentation == candidate_primary_presentation:
            score += PRESENTATION_MATCH_POINTS
            matched_tokens.append(line_presentation)
            reasons.append(f"presentación coincide: {line_presentation}")
        else:
            score -= PRESENTATION_CONFLICT_PENALTY
            conflicts.append(
                {
                    "category": "presentation",
                    "order_value": [line_presentation],
                    "candidate_value": [candidate_primary_presentation],
                }
            )
            reasons.append(
                f"presentación en conflicto: pedido={line_presentation} vs candidato={candidate_primary_presentation}"
            )
            if line_presentation in candidate_presentation["contained"]:
                score += CONTAINED_PRESENTATION_BONUS
                matched_tokens.append(line_presentation)
                reasons.append(
                    f"presentación '{line_presentation}' también aparece contenida en el candidato"
                )

    similarity = difflib.SequenceMatcher(
        None, parsed_line["normalized_product_text"], candidate["normalized_name"]
    ).ratio()
    score += TEXT_SIMILARITY_BONUS_MAX * similarity

    score = max(0, min(100, round(score)))

    return {
        "item_code": candidate["item_code"],
        "item_name": candidate["item_name"],
        "score": score,
        "confidence": classify_confidence(score),
        "matched_tokens": matched_tokens,
        "conflicts": conflicts,
        "reasons": reasons,
    }
