"""Requirement-aware evidence allocation and conservative support contracts.

This module deliberately does not convert retrieval scores into probabilities.
Only an explicit direct-support decision may produce ``satisfied``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

SUPPORT_CONTRACT_VERSION = "1.0.0"


class SupportStatus(str, Enum):
    SATISFIED = "satisfied"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class SupportLabel(str, Enum):
    DIRECT = "direct"
    PARTIAL = "partial"
    CONTEXT = "context"
    IRRELEVANT = "irrelevant"


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    comparison_side_id: str
    claim_key: str
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str


@dataclass
class Candidate:
    chunk_id: int
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str
    relevance_rank: int
    support: dict[str, SupportLabel] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


def dynamic_evidence_budget(requirement_count: int, comparison_sides: int) -> int:
    """Return the bounded evaluation budget defined by question structure."""
    if requirement_count < 1 or comparison_sides < 1:
        raise ValueError("requirement_count and comparison_sides must be positive")
    if requirement_count == 1:
        return 5
    if comparison_sides == 2 and requirement_count <= 4:
        return 6
    if comparison_sides >= 3 or requirement_count >= 6:
        return 8
    return min(8, max(6, requirement_count + 2))


def _source_compatible(requirement: Requirement, candidate: Candidate) -> bool:
    return (
        requirement.ticker == candidate.ticker
        and requirement.filing_year == candidate.filing_year
        and requirement.doc_type == candidate.doc_type
        and requirement.accession_number == candidate.accession_number
        and requirement.section_code == candidate.section_code
    )


def validate_support_associations(
    requirements: Iterable[Requirement],
    candidates: Iterable[Candidate],
) -> None:
    """Reject support credit across a source, year, or section boundary."""
    by_id = {item.requirement_id: item for item in requirements}
    for candidate in candidates:
        for requirement_id in candidate.support:
            requirement = by_id.get(requirement_id)
            if requirement is None:
                raise ValueError(f"unknown requirement association: {requirement_id}")
            if not _source_compatible(requirement, candidate):
                raise ValueError(
                    f"chunk {candidate.chunk_id} cannot support {requirement_id}: "
                    "source or section mismatch"
                )


def allocate_evidence(
    requirements: list[Requirement],
    candidates: list[Candidate],
    *,
    limit: int | None = None,
) -> list[Candidate]:
    """Select each chunk once while maximizing direct requirement coverage.

    A candidate may receive credit for multiple compatible requirements. Direct
    support is prioritized, partial support is secondary, and rank is the stable
    tie-breaker. Remaining slots are filled by relevance rank.
    """
    if not requirements:
        raise ValueError("at least one requirement is required")
    validate_support_associations(requirements, candidates)
    side_count = len({item.comparison_side_id for item in requirements})
    budget = limit or dynamic_evidence_budget(len(requirements), side_count)
    if budget < 1:
        raise ValueError("evidence limit must be positive")

    unique: dict[int, Candidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.relevance_rank):
        existing = unique.get(candidate.chunk_id)
        if existing is None:
            unique[candidate.chunk_id] = candidate
            continue
        existing.support.update(candidate.support)

    remaining = list(unique.values())
    selected: list[Candidate] = []
    direct_covered: set[str] = set()
    partial_covered: set[str] = set()
    while remaining and len(selected) < budget:
        def utility(item: Candidate) -> tuple[int, int, int, int]:
            new_direct = {
                key for key, label in item.support.items()
                if label == SupportLabel.DIRECT and key not in direct_covered
            }
            new_partial = {
                key for key, label in item.support.items()
                if label in {SupportLabel.PARTIAL, SupportLabel.CONTEXT}
                and key not in direct_covered and key not in partial_covered
            }
            return (
                len(new_direct),
                len(new_partial),
                len(item.support),
                -item.relevance_rank,
            )

        best = max(remaining, key=utility)
        if utility(best)[:2] == (0, 0):
            break
        selected.append(best)
        remaining.remove(best)
        direct_covered.update(
            key for key, label in best.support.items()
            if label == SupportLabel.DIRECT
        )
        partial_covered.update(
            key for key, label in best.support.items()
            if label in {SupportLabel.PARTIAL, SupportLabel.CONTEXT}
        )

    for candidate in sorted(remaining, key=lambda item: item.relevance_rank):
        if len(selected) == budget:
            break
        selected.append(candidate)
    return selected


def build_support_contract(
    query_type: str,
    requirements: list[Requirement],
    selected: list[Candidate],
) -> dict[str, Any]:
    """Build generator-facing support state without uncalibrated confidence."""
    validate_support_associations(requirements, selected)
    requirement_rows: list[dict[str, Any]] = []
    statuses: list[SupportStatus] = []
    for requirement in requirements:
        direct = [
            item for item in selected
            if item.support.get(requirement.requirement_id) == SupportLabel.DIRECT
        ]
        partial = [
            item for item in selected
            if item.support.get(requirement.requirement_id)
            in {SupportLabel.PARTIAL, SupportLabel.CONTEXT}
        ]
        if direct:
            status = SupportStatus.SATISFIED
            evidence = direct
            missing = None
        elif partial:
            status = SupportStatus.PARTIAL
            evidence = partial
            missing = "Direct evidence was not established."
        else:
            status = SupportStatus.UNSUPPORTED
            evidence = []
            missing = "No approved direct-support evidence was selected."
        statuses.append(status)
        requirement_rows.append({
            "requirement_id": requirement.requirement_id,
            "comparison_side_id": requirement.comparison_side_id,
            "claim_key": requirement.claim_key,
            "source": {
                "ticker": requirement.ticker,
                "filing_year": requirement.filing_year,
                "doc_type": requirement.doc_type,
                "accession_number": requirement.accession_number,
                "section_code": requirement.section_code,
            },
            "support_status": status.value,
            "support_confidence": None,
            "evidence": [
                {
                    "chunk_id": item.chunk_id,
                    "rank": rank,
                    "support_label": item.support[requirement.requirement_id].value,
                }
                for rank, item in enumerate(selected, 1)
                if item in evidence
            ],
            "missing_support": missing,
        })
    if all(item == SupportStatus.SATISFIED for item in statuses):
        overall = SupportStatus.SATISFIED
    elif any(item != SupportStatus.UNSUPPORTED for item in statuses):
        overall = SupportStatus.PARTIAL
    else:
        overall = SupportStatus.UNSUPPORTED
    return {
        "support_contract_version": SUPPORT_CONTRACT_VERSION,
        "query_type": query_type,
        "overall_support_status": overall.value,
        "support_confidence": None,
        "requirements": requirement_rows,
    }
