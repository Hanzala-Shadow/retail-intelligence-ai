import json
from pathlib import Path

import pytest

from src.anchored_reranking import (
    AnchoredRerankingConfig,
    select_anchored_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def config():
    return AnchoredRerankingConfig.load(
        ROOT / "config" / "retrieval_anchored_k16_v1.json"
    )


def row(requirement, chunk, l12, bge, *, soft=False, text_hash=None):
    return {
        "chunk_id": chunk,
        "source_chunk_id": f"source-{chunk}",
        "chunk_text_sha256": text_hash or f"hash-{chunk}",
        "selected_for_subquery_id": requirement,
        "l12_score": l12,
        "bge_score": bge,
        "section_policy": "soft" if soft else "hard",
    }


def test_config_pins_frozen_models_and_limits():
    item = config()
    assert item.anchor_model_id == "cross-encoder/ms-marco-MiniLM-L12-v2"
    assert item.expansion_model_id == "BAAI/bge-reranker-v2-m3"
    assert item.anchor_count == 6
    assert item.evidence_limit == 16
    assert item.max_length == 512


def test_gpu_profile_changes_runtime_only():
    cpu = config()
    gpu = AnchoredRerankingConfig.load(
        ROOT / "config" / "retrieval_anchored_k16_gpu_v1.json"
    )
    selection_fields = (
        "policy_id",
        "anchor_model_id",
        "anchor_model_revision",
        "expansion_model_id",
        "expansion_model_revision",
        "passage_field",
        "max_length",
        "anchor_count",
        "evidence_limit",
        "hard_candidate_limit",
        "soft_candidate_limit",
    )
    assert {
        name: getattr(cpu, name) for name in selection_fields
    } == {
        name: getattr(gpu, name) for name in selection_fields
    }
    assert cpu.model_lifecycle == "sequential"
    assert gpu.model_lifecycle == "resident"
    assert gpu.batch_size == 32


def test_selector_balances_anchors_and_expansion():
    groups = []
    for number, requirement in enumerate(("sq-a", "sq-b", "sq-c")):
        base = number * 100
        groups.append({
            "requirement_id": requirement,
            "hard": [
                row(requirement, base + index, 100-index, 50-index)
                for index in range(10)
            ],
            "soft": [
                row(requirement, base + 50 + index, 20-index, 80-index, soft=True)
                for index in range(4)
            ],
        })
    selected = select_anchored_evidence(groups, config())
    assert len(selected) == 16
    assert [item["selected_for_subquery_id"] for item in selected[:6]] == [
        "sq-a", "sq-b", "sq-c", "sq-a", "sq-b", "sq-c"
    ]
    assert {item["selection_reason"] for item in selected[:6]} == {"l12_anchor"}
    assert [item["selection_reason"] for item in selected[6:9]] == [
        "top_soft_per_requirement"
    ] * 3
    assert all(item["final_rank"] == index for index, item in enumerate(selected, 1))


def test_selector_preserves_equal_text_from_distinct_sources():
    duplicate = "same-disclosure"
    groups = [
        {
            "requirement_id": "sq-a",
            "hard": [row("sq-a", index, 50-index, 40-index, text_hash=(duplicate if index == 1 else None)) for index in range(1, 14)],
            "soft": [row("sq-a", 100+index, 1, 30-index, soft=True) for index in range(5)],
        },
        {
            "requirement_id": "sq-b",
            "hard": [row("sq-b", 200+index, 50-index, 40-index, text_hash=(duplicate if index == 1 else None)) for index in range(1, 14)],
            "soft": [row("sq-b", 300+index, 1, 30-index, soft=True) for index in range(5)],
        },
    ]
    selected = select_anchored_evidence(groups, config())
    assert len(selected) == 16
    assert sum(item["chunk_text_sha256"] == duplicate for item in selected) == 2


def test_selector_fails_closed_when_k16_cannot_be_filled():
    groups = [{
        "requirement_id": "sq-a",
        "hard": [row("sq-a", 1, 2, 2)],
        "soft": [],
    }]
    with pytest.raises(RuntimeError, match="expected 16"):
        select_anchored_evidence(groups, config())


def test_config_rejects_policy_drift(tmp_path):
    payload = json.loads(
        (ROOT / "config" / "retrieval_anchored_k16_v1.json").read_text()
    )
    payload["selection"]["anchor_count"] = 5
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="anchor_count=6"):
        AnchoredRerankingConfig.load(path)
