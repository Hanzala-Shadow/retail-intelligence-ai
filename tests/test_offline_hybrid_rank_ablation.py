from scripts.run_offline_hybrid_rank_ablation import balanced_select, hybrid_rank


def test_rrf_only_and_cross_encoder_only_follow_their_respective_ranks():
    pool = [
        {"chunk_id": 1, "rrf_score": 0.1, "cross_encoder_rank": 1},
        {"chunk_id": 2, "rrf_score": 0.3, "cross_encoder_rank": 3},
        {"chunk_id": 3, "rrf_score": 0.2, "cross_encoder_rank": 2},
    ]
    assert [row["chunk_id"] for row in hybrid_rank(pool, 0.0)] == [2, 3, 1]
    assert [row["chunk_id"] for row in hybrid_rank(pool, 1.0)] == [1, 3, 2]


def test_hybrid_ranking_is_deterministic():
    pool = [
        {"chunk_id": 2, "rrf_score": 0.2, "cross_encoder_rank": 1},
        {"chunk_id": 1, "rrf_score": 0.1, "cross_encoder_rank": 2},
    ]
    assert hybrid_rank(pool, 0.5) == hybrid_rank(pool, 0.5)


def test_balanced_selection_allocates_three_and_two_for_two_requirements():
    first = [{"chunk_id": value, "hybrid_score": 1.0} for value in range(1, 6)]
    second = [{"chunk_id": value, "hybrid_score": 1.0} for value in range(6, 11)]
    result = balanced_select([first, second])
    assert [row["chunk_id"] for row in result] == [1, 6, 2, 7, 3]
    assert [row["requirement_index"] for row in result] == [1, 2, 1, 2, 1]
