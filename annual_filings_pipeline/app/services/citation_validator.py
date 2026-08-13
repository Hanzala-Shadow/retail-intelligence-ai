"""Deterministic citation and unsupported platform-label validation."""
from __future__ import annotations

import re
from typing import Any

CANONICAL = re.compile(r"\[C(?:[1-9]|1[0-6])\]")
ANY_CITATION = re.compile(r"\[[^\]]*C[^\]]*\]")
SENTINEL = "[INSUFFICIENT_EVIDENCE]"
SIDEDNESS = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|multi|\d+)[ -]sided\b",
    re.IGNORECASE,
)


def _normalized_sidedness(text: str) -> set[str]:
    return {match.group(0).lower().replace(" ", "-") for match in SIDEDNESS.finditer(text)}


def validate_answer(
    answer: str,
    evidence: list[dict[str, Any]],
    word_budget: int,
    stop_reason: str | None,
) -> dict[str, Any]:
    answer = str(answer or "").strip()
    citation_text = answer.replace(SENTINEL, "")
    canonical = set(CANONICAL.findall(citation_text))
    any_forms = set(ANY_CITATION.findall(citation_text))
    allowed = {f"[{item['label']}]" for item in evidence}
    failures: list[str] = []
    if not answer:
        failures.append("EMPTY_ANSWER")
    if stop_reason == "max_tokens":
        failures.append("MAX_TOKEN_STOP")
    if any_forms - canonical:
        failures.append("MALFORMED_CITATION")
    if canonical - allowed:
        failures.append("UNAVAILABLE_CITATION")
    evidence_text = "\n".join(str(item.get("text") or "") for item in evidence)
    if _normalized_sidedness(answer) - _normalized_sidedness(evidence_text):
        failures.append("UNSUPPORTED_PLATFORM_SIDEDNESS")
    word_count = len(answer.split())
    if word_count > word_budget:
        failures.append("WORD_BUDGET_EXCEEDED")
    return {
        "valid": not failures,
        "failures": failures,
        "citations": sorted(canonical, key=lambda value: int(value[2:-1])),
        "insufficient_evidence": SENTINEL in answer,
        "word_count": word_count,
        "word_budget": word_budget,
    }
