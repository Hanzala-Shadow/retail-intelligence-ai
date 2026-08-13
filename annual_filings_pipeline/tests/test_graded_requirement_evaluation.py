from scripts.run_graded_requirement_evaluation import _metrics


def test_requirement_metrics_reward_complete_direct_coverage():
    qrels = {1: {10: 2, 11: 1}, 2: {20: 2}}
    result = _metrics([10, 20, 99, 98, 97], qrels)
    assert result["direct_hit_at_5"] == 1.0
    assert result["mrr_direct_at_5"] == 1.0
    assert result["requirement_coverage_at_5"] == 1.0
    assert result["complete_requirement_coverage_at_5"] == 1.0
    assert result["judged_at_5_rate"] == 0.4


def test_requirement_metrics_expose_missing_comparison_side():
    qrels = {1: {10: 2}, 2: {20: 2}}
    result = _metrics([10, 11, 12, 13, 14], qrels)
    assert result["requirement_coverage_at_5"] == 0.5
    assert result["complete_requirement_coverage_at_5"] == 0.0


def test_partial_evidence_has_gain_but_is_not_direct_hit():
    qrels = {1: {10: 2, 11: 1}}
    result = _metrics([11, 99, 98, 97, 96], qrels)
    assert result["direct_hit_at_5"] == 0.0
    assert result["graded_ndcg_at_5"] > 0.0
