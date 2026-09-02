# -*- coding: utf-8 -*-
"""Commit 25.8.2 -- Quick Order: candidate retrieval + ranking on top of
`scoring.py`'s per-candidate score. Same purity contract as the rest of this
package (Commit 25.8.1's own normalizer.py/parser.py docstrings): no
`frappe` import, no DB, no I/O -- `match_order_line()`/`match_order_text()`
run entirely against plain Python `Candidate` dicts the caller already has
in hand. Feeding this from ERPNext's real Item catalog (instead of the
synthetic candidates this commit's own tests build) is Commit 25.8.3's job,
not this one's -- see this module's own `build_candidate()` for the one
seam that future commit will call, unchanged.

Scope, same as `parser.py`'s own docstring: this module does NOT touch a
Sales Order, a cart, an Item Alias, or read a price/stock field from
anywhere -- a `Candidate` (below) structurally cannot carry one.
"""

from fabergray_erp.quick_order import scoring
from fabergray_erp.quick_order.normalizer import normalize_text
from fabergray_erp.quick_order.parser import extract_tokens

# Candidates below this score are never returned at all -- distinct from
# CONFIDENCE_MEDIUM_THRESHOLD (a UI-facing label): this is the retrieval-side
# "not even a reasonable guess" floor. Set well under MEDIUM so a single,
# unambiguous but weakly-evidenced match (e.g. one generic word, nothing
# else to confirm -- see the "3 galones desengrasante" case in this
# commit's own report) still surfaces as a low-confidence *suggestion*
# rather than being silently dropped; a true non-match (no shared token at
# all) never reaches this filter in the first place -- see
# `_passes_minimum_signal()` below, which is the real gate.
_MIN_RETURNABLE_SCORE = 1


def build_candidate(item_code, item_name, description=None, item_group=None, stock_uom=None):
    """The one place a `Candidate` is built from plain values -- whether
    those values come from a synthetic test dict (this commit) or a real
    `frappe.get_list("Item", ...)` row (Commit 25.8.3) makes no difference
    to this function, which never touches `frappe` either way.

    `item_name` is tokenized through the exact same `normalize_text()` /
    `extract_tokens()` pipeline `parser.py` runs an order line through
    (Commit 25.8.1) -- the whole point of sharing it is that a candidate's
    `tokens` and a parsed line's `tokens` are directly comparable, never two
    independently-tuned tokenizers drifting apart. `description` is
    deliberately NOT merged into `normalized_name`/`tokens` in this commit
    -- kept as a separate, untokenized field for now (a documented
    simplification, not an oversight): folding in a second free-text source
    changes what "coverage" and "conflict" mean and deserves its own
    decision once real Item descriptions are actually behind this.

    Never includes `rate`/`price_list_rate`/`valuation_rate`/any stock
    field -- see this module's own docstring."""
    normalized_name = normalize_text(item_name)
    return {
        "item_code": item_code,
        "item_name": item_name,
        "normalized_name": normalized_name,
        "tokens": extract_tokens(normalized_name),
        "description": description,
        "item_group": item_group,
        "stock_uom": stock_uom,
    }


def _passes_minimum_signal(parsed_line, candidate):
    """The retrieval gate (Commit 25.8.2 brief, section 5): a candidate is
    even considered for scoring only if there is AT LEAST ONE reasonable
    semantic signal in common -- an exact or conservative-fuzzy generic-type
    token match, or an exact measure/size/color match. Nothing here is a
    string-similarity accident: "guantes talla l negro" can never retrieve
    "DESENGRASANTE 1 GALON" because none of the four checks below can ever
    be true for that pair (verified empirically for this commit's own
    synthetic catalog -- see the report's own edge-cases section)."""
    line_tokens = parsed_line["tokens"]
    candidate_tokens = candidate["tokens"]

    effective_generic = scoring.effective_generic_tokens(parsed_line)
    if effective_generic:
        exact, fuzzy = scoring.generic_token_matches(effective_generic, candidate_tokens["generic"])
        if exact or fuzzy:
            return True

    for line_values, candidate_values in (
        (line_tokens["measure"], candidate_tokens["measure"]),
        (line_tokens["size"], candidate_tokens["size"]),
        (line_tokens["color"], candidate_tokens["color"]),
    ):
        if scoring.category_signal(line_values, candidate_values) == "match":
            return True

    # Commit 25.8.4 -- a presentation-only match (line asked for "botella",
    # candidate's own primary presentation is "botella" too) is also a
    # reasonable enough signal to retrieve a candidate, symmetric with
    # measure/size/color above -- see scoring.effective_primary_presentation()
    # for what counts as a line's presentation.
    line_presentation = scoring.effective_primary_presentation(parsed_line)
    if line_presentation and line_presentation == candidate_tokens["presentation"]["primary"]:
        return True

    return False


def match_order_line(parsed_line, candidates, limit=5):
    """Scores every candidate that passes `_passes_minimum_signal()`
    against `parsed_line` (a `parser.parse_order_line()` result), sorted by
    score descending, capped at `limit` (default 5, per the brief's own
    section 14). Never returns more than `limit` candidates; returns an
    empty list -- never a guess -- when nothing clears the retrieval gate
    (Commit 25.8.2 brief, section 5's own "producto inexistente" case).

    Returns:
        {
            "candidates": [scoring.score_candidate() result, ...],  # <= limit, score DESC
            "score_margin": int | float | None,  # top1 - top2, None if < 2 candidates
            "ambiguous": bool,  # score_margin < scoring.AMBIGUITY_MARGIN_THRESHOLD
        }

    `ambiguous` is computed even when `top1`'s own score is "high" (section
    17's own point: two near-identical scores are ambiguous regardless of
    how high the top one is) -- `suggested_item()` below is what actually
    acts on this, never this function itself.
    """
    scored = [
        scoring.score_candidate(parsed_line, candidate)
        for candidate in candidates
        if _passes_minimum_signal(parsed_line, candidate)
    ]
    scored = [result for result in scored if result["score"] >= _MIN_RETURNABLE_SCORE]
    scored.sort(key=lambda result: result["score"], reverse=True)

    top = scored[: max(0, limit)]

    if len(top) >= 2:
        score_margin = top[0]["score"] - top[1]["score"]
        ambiguous = score_margin < scoring.AMBIGUITY_MARGIN_THRESHOLD
    else:
        score_margin = None
        ambiguous = False

    return {"candidates": top, "score_margin": score_margin, "ambiguous": ambiguous}


def match_order_text(parsed_lines, candidates, limit=5):
    """`match_order_line()` for every line of a `parser.parse_order_text()`
    result, in the same order, against the same candidate pool. A thin loop
    -- kept here (rather than left for the future endpoint/UI to write)
    because it is simple and every caller of `match_order_line()` in a
    multi-line order will need exactly this."""
    return [match_order_line(parsed_line, candidates, limit=limit) for parsed_line in parsed_lines]


def suggested_item(match_result):
    """Turns one `match_order_line()` result into the single, explicit
    yes/no answer the future UI (Commit 25.8.5 -- NOT built here) will need
    for "should this line come preselected": applies the HIGH/MEDIUM/LOW
    rule from the brief's own section 16, with `ambiguous` (section 17) as
    an override that always wins -- two near-tied candidates are never
    preselected, no matter how high the top score is on its own.

    Returns `None` (no suggestion at all) when there are no candidates, the
    line is ambiguous, or the top candidate's confidence is "low". Otherwise
    `{"item_code", "score", "preselected"}` -- `preselected=True` only for
    an unambiguous "high"; `preselected=False` for "medium" (a suggestion
    the asesora should still look at, never applied automatically).

    This function never touches a cart, a Sales Order, or any UI element --
    it returns data for the future UI to act on, matching this whole
    package's "no toca Sales Order/UI" scope."""
    candidates = match_result["candidates"]
    if not candidates or match_result["ambiguous"]:
        return None

    top = candidates[0]
    if top["confidence"] == "high":
        return {"item_code": top["item_code"], "score": top["score"], "preselected": True}
    if top["confidence"] == "medium":
        return {"item_code": top["item_code"], "score": top["score"], "preselected": False}
    return None
