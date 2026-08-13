from scripts.run_generic_multiview_requirement_benchmark import (
    adaptive_rank_pool, balanced_select, fuse_candidates,
)


def _rows(*chunk_ids):
    return [{"chunk_id": value, "embedding_text": str(value)} for value in chunk_ids]


def test_rrf_rewards_candidates_seen_by_multiple_views():
    per_view = {
        "original": _rows(1, 2, 3),
        "focused": _rows(3, 4, 5),
        "profile": _rows(3, 6, 7),
    }
    result = fuse_candidates(per_view, ("original", "focused", "profile"))
    assert result[0]["chunk_id"] == 3
    assert result[0]["view_ranks"] == {"original": 3, "focused": 1, "profile": 1}


def test_fusion_is_deterministic_and_respects_enabled_views():
    per_view = {
        "original": _rows(1, 2),
        "focused": _rows(3, 4),
        "profile": _rows(5, 6),
    }
    first = fuse_candidates(per_view, ("focused",), pool_limit=2)
    second = fuse_candidates(per_view, ("focused",), pool_limit=2)
    assert first == second
    assert [row["chunk_id"] for row in first] == [3, 4]


def test_balanced_selection_guarantees_requirement_coverage():
    first = [
        {"chunk_id": value, "cross_encoder_score": 10 - value, "rrf_score": 1}
        for value in (1, 2, 3, 4, 5)
    ]
    second = [
        {"chunk_id": value, "cross_encoder_score": 10 - value, "rrf_score": 1}
        for value in (6, 7, 8, 9, 10)
    ]
    result = balanced_select([first, second], limit=5)
    assert [row["chunk_id"] for row in result] == [1, 6, 2, 7, 3]
    assert {row["requirement_index"] for row in result} == {1, 2}


def test_adaptive_narrative_order_uses_rrf_and_item8_uses_cross_encoder():
    pool = [
        {"chunk_id": 1, "rrf_score": 0.1, "embedding_text": "a"},
        {"chunk_id": 2, "rrf_score": 0.3, "embedding_text": "b"},
    ]
    scores = {1: 0.9, 2: 0.1}
    narrative = adaptive_rank_pool(pool, scores, "Item_7")
    financial = adaptive_rank_pool(pool, scores, "Item_8")
    assert [row["chunk_id"] for row in narrative] == [2, 1]
    assert [row["chunk_id"] for row in financial] == [1, 2]
