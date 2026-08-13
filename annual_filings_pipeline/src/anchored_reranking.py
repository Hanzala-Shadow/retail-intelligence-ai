"""Frozen, gold-blind anchored evidence selection for annual filings.

The selector is intentionally independent from database and model loading.  It
accepts already-scored hard/soft candidate groups, which makes the production
policy deterministic, testable, and reusable by offline validation tools.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any, Iterable


POLICY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AnchoredRerankingConfig:
    policy_id: str
    anchor_model_id: str
    anchor_model_revision: str
    expansion_model_id: str
    expansion_model_revision: str
    passage_field: str
    max_length: int
    anchor_count: int
    evidence_limit: int
    hard_candidate_limit: int
    soft_candidate_limit: int
    batch_size: int
    model_lifecycle: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AnchoredRerankingConfig":
        if int(value.get("schema_version", -1)) != POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported anchored-reranking schema_version")
        model = value.get("models") or {}
        anchor = model.get("anchor") or {}
        expansion = model.get("expansion") or {}
        selection = value.get("selection") or {}
        candidates = value.get("candidates") or {}
        runtime = value.get("runtime") or {}
        config = cls(
            policy_id=str(value.get("policy_id") or "").strip(),
            anchor_model_id=str(anchor.get("model_id") or "").strip(),
            anchor_model_revision=str(anchor.get("revision") or "").strip(),
            expansion_model_id=str(expansion.get("model_id") or "").strip(),
            expansion_model_revision=str(expansion.get("revision") or "").strip(),
            passage_field=str(value.get("passage_field") or "").strip(),
            max_length=int(value.get("max_length", 0)),
            anchor_count=int(selection.get("anchor_count", 0)),
            evidence_limit=int(selection.get("evidence_limit", 0)),
            hard_candidate_limit=int(candidates.get("hard_limit", 0)),
            soft_candidate_limit=int(candidates.get("soft_limit", 0)),
            batch_size=int(runtime.get("batch_size", 0)),
            model_lifecycle=str(runtime.get("model_lifecycle") or "").strip(),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> "AnchoredRerankingConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("anchored-reranking config must be a JSON object")
        return cls.from_mapping(payload)

    def validate(self) -> None:
        required = {
            "policy_id": self.policy_id,
            "anchor_model_id": self.anchor_model_id,
            "anchor_model_revision": self.anchor_model_revision,
            "expansion_model_id": self.expansion_model_id,
            "expansion_model_revision": self.expansion_model_revision,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing anchored-reranking fields: {', '.join(missing)}")
        if self.passage_field not in {"embedding_text", "chunk_text"}:
            raise ValueError("passage_field must be embedding_text or chunk_text")
        if self.max_length != 512:
            raise ValueError("the frozen reranker input length is exactly 512")
        if self.anchor_count != 6 or self.evidence_limit != 16:
            raise ValueError("the frozen selector requires anchor_count=6 and evidence_limit=16")
        if min(self.hard_candidate_limit, self.soft_candidate_limit, self.batch_size) < 1:
            raise ValueError("candidate limits and batch_size must be positive")
        if self.model_lifecycle not in {"resident", "sequential"}:
            raise ValueError("model_lifecycle must be resident or sequential")


def candidate_key(row: dict[str, Any]) -> tuple[str, str]:
    """Deduplicate source identity without collapsing valid year comparisons.

    Identical disclosure text can legitimately occur in two filing years and
    must remain available to a temporal comparison. Exact-text hash dedupe was
    evaluated as an ablation, but the frozen K16 selector uses source identity.
    """
    if row.get("source_chunk_id"):
        return ("source_chunk_id", str(row["source_chunk_id"]))
    return ("chunk_id", str(int(row["chunk_id"])))


def _unique(rows: Iterable[dict[str, Any]], seen: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    active = seen if seen is not None else set()
    output: list[dict[str, Any]] = []
    for row in rows:
        key = candidate_key(row)
        if key in active:
            continue
        active.add(key)
        output.append(row)
    return output


def _ordered(rows: Iterable[dict[str, Any]], score_field: str) -> list[dict[str, Any]]:
    usable = [row for row in rows if row.get(score_field) is not None]
    return sorted(
        usable,
        key=lambda row: (-float(row[score_field]), int(row["chunk_id"])),
    )


def _balanced_fill(
    groups: list[list[dict[str, Any]]],
    *,
    limit: int,
    seen: set[tuple[str, str]],
    reason: str,
) -> list[dict[str, Any]]:
    # Remove previously selected rows before assigning depths. Otherwise a
    # requirement with several anchors would start expansion deeper than a
    # requirement with fewer anchors and break balanced round-robin ordering.
    groups = [
        [row for row in rows if candidate_key(row) not in seen]
        for rows in groups
    ]
    output: list[dict[str, Any]] = []
    depth = 0
    while len(output) < limit and any(depth < len(rows) for rows in groups):
        for rows in groups:
            if depth >= len(rows):
                continue
            row = rows[depth]
            key = candidate_key(row)
            if key in seen:
                continue
            selected = dict(row)
            selected["selection_reason"] = reason
            output.append(selected)
            seen.add(key)
            if len(output) == limit:
                break
        depth += 1
    return output


def select_anchored_evidence(
    requirement_groups: list[dict[str, Any]],
    config: AnchoredRerankingConfig,
) -> list[dict[str, Any]]:
    """Select six L12 anchors, soft coverage, then BGE hard expansion.

    Each group must contain ``requirement_id``, ``hard`` and ``soft``. Candidate
    rows must have ``l12_score`` and ``bge_score``.  Selection never reads gold
    fields or benchmark identifiers.
    """
    config.validate()
    if not requirement_groups:
        raise ValueError("at least one requirement group is required")
    requirement_ids = [str(group.get("requirement_id") or "") for group in requirement_groups]
    if any(not item for item in requirement_ids) or len(set(requirement_ids)) != len(requirement_ids):
        raise ValueError("requirement_id values must be non-empty and unique")

    seen: set[tuple[str, str]] = set()
    selected: list[dict[str, Any]] = []

    anchor_groups = [_ordered(group.get("hard") or [], "l12_score") for group in requirement_groups]
    anchors = _balanced_fill(
        anchor_groups,
        limit=config.anchor_count,
        seen=seen,
        reason="l12_anchor",
    )
    selected.extend(anchors)

    # One BGE-ranked soft candidate per requirement protects coverage when an
    # exact section is narrow.  Hard-route candidates remain preferred overall.
    for group in requirement_groups:
        soft = _ordered(group.get("soft") or [], "bge_score")
        candidate = next((row for row in soft if candidate_key(row) not in seen), None)
        if candidate is None:
            continue
        copied = dict(candidate)
        copied["selection_reason"] = "top_soft_per_requirement"
        selected.append(copied)
        seen.add(candidate_key(candidate))
        if len(selected) == config.evidence_limit:
            break

    if len(selected) < config.evidence_limit:
        hard_bge_groups = [_ordered(group.get("hard") or [], "bge_score") for group in requirement_groups]
        selected.extend(_balanced_fill(
            hard_bge_groups,
            limit=config.evidence_limit - len(selected),
            seen=seen,
            reason="bge_hard_round_robin",
        ))

    if len(selected) < config.evidence_limit:
        soft_bge_groups = [_ordered(group.get("soft") or [], "bge_score") for group in requirement_groups]
        selected.extend(_balanced_fill(
            soft_bge_groups,
            limit=config.evidence_limit - len(selected),
            seen=seen,
            reason="bge_soft_fallback_round_robin",
        ))

    if len(selected) < config.evidence_limit:
        raise RuntimeError(
            f"anchored selector returned {len(selected)} unique passages; "
            f"expected {config.evidence_limit}"
        )
    valid_requirements = set(requirement_ids)
    for rank, row in enumerate(selected, 1):
        requirement_id = str(row.get("selected_for_subquery_id") or "")
        if requirement_id not in valid_requirements:
            raise ValueError("selected candidate is missing a valid selected_for_subquery_id")
        row["final_rank"] = rank
        row["aggregated_rank"] = rank
        row["selection_rank"] = rank
        row["selection_policy"] = config.policy_id
    return selected
