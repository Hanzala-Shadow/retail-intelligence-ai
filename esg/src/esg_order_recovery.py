"""Deterministic recovery for pages held as ``auto_hold_structural_multi_column``.

Generates the three available readings of a page and lets the geometry-based
safety gate in ``esg_order_safety`` decide whether any of them is retrieval
safe. Nothing here uses gold, vision, OCR, or a model call.

Selection, in the order the rules are applied:

1. If the current parsed text passes, prefer it. Nothing needs re-parsing and
   the page can be certified as it stands.
2. Otherwise, if exactly one reconstruction passes, that reconstruction is the
   answer.
3. If both reconstructions pass and materially agree on order, take the
   production reader.
4. If both pass and disagree, the page is ambiguous. Keep it held.
5. If none passes, keep it held.

An important asymmetry the caller must respect: only outcome 1 can be indexed
today. Outcomes 2 and 3 identify a reader that *would* produce safe text, but
the corpus still holds the old text, so realising them needs a reparse. They
are reported as a distinct hold reason rather than as a pass.

One consequence of how ``esg_order_safety`` is built is worth naming, because
it is what makes "table relationships not proven -> hold" true rather than
merely intended. The two table checks measure the *page*, not the candidate:
they read the unruled-table blocks found in the word coordinates, which are the
same whichever reader produced the text. So a page whose table cannot be shown
to hold whole records fails every candidate at once and lands on
``held_no_safe_order``. No reader can talk its way past it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from esg_order_safety import (
    ORDER_AGREEMENT_MIN,
    OrderSafetyResult,
    evaluate_order_safety,
    order_agreement,
)

PARSER_CURRENT = "current"
PARSER_REGION_ORDER = "reconstruct_region_order"
PARSER_BY_REGIONS = "reconstruct_by_regions"

#: The current text is safe as it stands; the page can be certified now.
OUTCOME_CURRENT = "recovered_current_text"
#: A reconstruction is safe, but the corpus text is stale until a reparse.
OUTCOME_REPARSE = "recoverable_by_reparse"
#: Several safe readings that disagree, or none at all.
OUTCOME_AMBIGUOUS = "held_ambiguous_order"
OUTCOME_NONE = "held_no_safe_order"


@dataclass
class RecoveryResult:
    outcome: str
    parser: str
    reason: str
    metrics: dict[str, float | int] = field(default_factory=dict)
    evaluations: dict[str, OrderSafetyResult] = field(default_factory=dict)

    @property
    def recovered_now(self) -> bool:
        return self.outcome == OUTCOME_CURRENT


def _reconstructions(words, page_width, page_height) -> dict[str, str]:
    """Best-effort candidate texts. A reader that raises simply does not compete."""

    out: dict[str, str] = {}
    try:
        from pdf_parser import reconstruct_region_order

        result = reconstruct_region_order(words, page_width, page_height)
        if getattr(result, "text", ""):
            out[PARSER_REGION_ORDER] = result.text
    except Exception:  # pragma: no cover - a failed reader is just absent
        pass
    try:
        from esg_reading_regions import reconstruct_by_regions

        result = reconstruct_by_regions(words, page_width, page_height)
        if getattr(result, "text", ""):
            out[PARSER_BY_REGIONS] = result.text
    except Exception:  # pragma: no cover
        pass
    return out


def recover_reading_order(
    words: list[dict],
    page_width: float,
    page_height: float,
    current_text: str,
    *,
    table_like: bool = False,
    visual_object_count: int = 0,
    mixed_column_lines: int = 0,
    full_page_image: bool = False,
) -> RecoveryResult:
    """Pick a retrieval-safe reading of a held multi-column page, or keep it held."""

    candidates: dict[str, str] = {PARSER_CURRENT: current_text or ""}
    candidates.update(_reconstructions(words, page_width, page_height))

    evaluations = {
        name: evaluate_order_safety(
            words,
            page_width,
            page_height,
            text,
            table_like=table_like,
            visual_object_count=visual_object_count,
            mixed_column_lines=mixed_column_lines,
            full_page_image=full_page_image,
        )
        for name, text in candidates.items()
    }
    passing = [name for name, result in evaluations.items() if result.passed]

    def summarise(name: str) -> dict[str, float | int]:
        metrics = dict(evaluations[name].metrics)
        metrics["candidates_passing"] = len(passing)
        return metrics

    # 1. current text wins whenever it is safe
    if PARSER_CURRENT in passing:
        return RecoveryResult(
            OUTCOME_CURRENT,
            PARSER_CURRENT,
            "current_text_order_safe",
            summarise(PARSER_CURRENT),
            evaluations,
        )

    reconstructions = [name for name in passing if name != PARSER_CURRENT]

    # 2. exactly one safe reconstruction
    if len(reconstructions) == 1:
        chosen = reconstructions[0]
        return RecoveryResult(
            OUTCOME_REPARSE, chosen, f"{chosen}_order_safe", summarise(chosen), evaluations
        )

    # 3/4. several safe reconstructions: take them only if they agree
    if len(reconstructions) > 1:
        agreement = order_agreement(
            evaluations[reconstructions[0]], evaluations[reconstructions[1]]
        )
        if agreement >= ORDER_AGREEMENT_MIN:
            chosen = (
                PARSER_REGION_ORDER
                if PARSER_REGION_ORDER in reconstructions
                else reconstructions[0]
            )
            metrics = summarise(chosen)
            metrics["order_agreement"] = round(agreement, 4)
            return RecoveryResult(
                OUTCOME_REPARSE, chosen, f"{chosen}_order_safe_agreed", metrics, evaluations
            )
        metrics = summarise(reconstructions[0])
        metrics["order_agreement"] = round(agreement, 4)
        return RecoveryResult(
            OUTCOME_AMBIGUOUS,
            "",
            f"safe_orders_disagree(agreement={agreement:.3f})",
            metrics,
            evaluations,
        )

    # 5. nothing is safe
    metrics = summarise(PARSER_CURRENT)
    return RecoveryResult(
        OUTCOME_NONE,
        "",
        "no_safe_order: " + evaluations[PARSER_CURRENT].reason,
        metrics,
        evaluations,
    )
