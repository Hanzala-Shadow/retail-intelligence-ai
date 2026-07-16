from pathlib import Path
import math
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from src.rag_eval_harness import (
    CHALLENGER_MODEL_ID,
    DEFAULT_MODEL_ID,
    FAIL,
    NOT_EVALUATED,
    PASS,
    apply_decision_rule,
    aggregate,
    evaluate,
    gate_evidence_present,
    gate_gold_integrity,
    gate_wrong_doc_type,
    hit_at_k,
    load_retrieval,
    main,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
    score_question,
    split_multi,
)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def test_recall_at_5_counts_fraction_of_relevant_found():
    # Cross-company questions have two relevant chunks; finding one is half.
    assert recall_at_k(["a", "x", "y", "z", "w"], {"a", "b"}, 5) == 0.5
    assert recall_at_k(["a", "b", "y", "z", "w"], {"a", "b"}, 5) == 1.0
    assert recall_at_k(["x", "y", "z", "w", "v"], {"a", "b"}, 5) == 0.0


def test_recall_at_5_ignores_results_below_rank_5():
    # Relevant chunk sits at rank 6 - outside the @5 window.
    assert recall_at_k(["x", "y", "z", "w", "v", "a"], {"a"}, 5) == 0.0


def test_hit_at_5_is_binary():
    assert hit_at_k(["x", "a"], {"a", "b"}, 5) == 1.0
    assert hit_at_k(["x", "y"], {"a", "b"}, 5) == 0.0


def test_reciprocal_rank_uses_first_relevant_position():
    assert reciprocal_rank_at_k(["a"], {"a"}, 10) == 1.0
    assert reciprocal_rank_at_k(["x", "a"], {"a"}, 10) == 0.5
    assert reciprocal_rank_at_k(["x", "y", "a"], {"a"}, 10) == pytest.approx(1 / 3)
    assert reciprocal_rank_at_k(["x", "y", "z"], {"a"}, 10) == 0.0


def test_reciprocal_rank_respects_k_cutoff():
    ranked = ["x"] * 9 + ["a"]
    assert reciprocal_rank_at_k(ranked, {"a"}, 10) == pytest.approx(0.1)
    assert reciprocal_rank_at_k(ranked, {"a"}, 5) == 0.0


def test_ndcg_is_one_when_relevant_chunks_lead_the_ranking():
    assert ndcg_at_k(["a", "b", "x"], {"a", "b"}, 10) == pytest.approx(1.0)


def test_ndcg_penalises_lower_placement():
    perfect = ndcg_at_k(["a", "x", "y"], {"a"}, 10)
    demoted = ndcg_at_k(["x", "a", "y"], {"a"}, 10)
    assert perfect == pytest.approx(1.0)
    assert demoted == pytest.approx(1 / math.log2(3))
    assert demoted < perfect


def test_ndcg_ideal_is_capped_at_k():
    # Three relevant chunks but k=2: the ideal ranking can only hold two.
    value = ndcg_at_k(["a", "b"], {"a", "b", "c"}, 2)
    assert value == pytest.approx(1.0)


def test_ndcg_is_zero_when_nothing_relevant_retrieved():
    assert ndcg_at_k(["x", "y"], {"a"}, 10) == 0.0


# --------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------


def test_split_multi_handles_pipe_separated_contract_fields():
    assert split_multi("RCKY|BOBS") == ["RCKY", "BOBS"]
    assert split_multi(" 313088 | 245459 ") == ["313088", "245459"]
    assert split_multi("") == []
    assert split_multi(None) == []


def test_score_question_reports_both_relevant_chunks_for_cross_company():
    question = {
        "question_id": "10K-V2-XC-003",
        "question_group": "cross_company",
        "supporting_chunk_ids": "313088|245459",
    }
    row = score_question(question, ["313088", "x", "y", "z", "245459"])
    assert row["relevant_chunk_count"] == 2
    assert row["recall_at_5"] == 1.0
    assert row["hit_at_5"] == 1.0
    assert row["mrr_at_10"] == 1.0


def test_aggregate_means_over_questions_and_groups():
    rows = [
        {"question_id": "q1", "question_group": "Item_1", "recall_at_5": 1.0, "hit_at_5": 1.0,
         "mrr_at_10": 1.0, "ndcg_at_10": 1.0, "mrr_at_5": 1.0, "ndcg_at_5": 1.0},
        {"question_id": "q2", "question_group": "Item_1", "recall_at_5": 0.0, "hit_at_5": 0.0,
         "mrr_at_10": 0.0, "ndcg_at_10": 0.0, "mrr_at_5": 0.0, "ndcg_at_5": 0.0},
        {"question_id": "q3", "question_group": "Item_7", "recall_at_5": 0.5, "hit_at_5": 1.0,
         "mrr_at_10": 0.5, "ndcg_at_10": 0.5, "mrr_at_5": 0.5, "ndcg_at_5": 0.5},
    ]
    result = aggregate(rows)
    assert result["question_count"] == 3
    assert result["overall"]["recall_at_5"] == pytest.approx(0.5)
    assert result["by_group"]["Item_1"]["recall_at_5"] == pytest.approx(0.5)
    assert result["by_group"]["Item_7"]["recall_at_5"] == pytest.approx(0.5)


def test_load_retrieval_rejects_non_contiguous_ranks(tmp_path):
    path = tmp_path / "retrieval.csv"
    path.write_text(
        "model_id,question_id,rank,chunk_id\nm,q1,1,a\nm,q1,3,b\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="contiguous"):
        load_retrieval(path)


def test_load_retrieval_rejects_duplicate_chunks_in_one_question(tmp_path):
    path = tmp_path / "retrieval.csv"
    path.write_text(
        "model_id,question_id,rank,chunk_id\nm,q1,1,a\nm,q1,2,a\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_retrieval(path)


def test_load_retrieval_orders_by_rank_not_file_order(tmp_path):
    path = tmp_path / "retrieval.csv"
    path.write_text(
        "model_id,question_id,rank,chunk_id\nm,q1,2,b\nm,q1,1,a\n", encoding="utf-8"
    )
    assert load_retrieval(path)["m"]["q1"] == ["a", "b"]


def test_load_retrieval_rejects_missing_columns(tmp_path):
    path = tmp_path / "retrieval.csv"
    path.write_text("model_id,question_id,chunk_id\nm,q1,a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        load_retrieval(path)


# --------------------------------------------------------------------------
# Hard gates
# --------------------------------------------------------------------------


QUESTION = {
    "question_id": "q1",
    "question_group": "Item_1",
    "required_doc_type": "10-K",
    "supporting_chunk_ids": "c1",
    "supporting_tickers": "ASO",
    "supporting_filing_years": "2024",
    "supporting_section_codes": "Item_1",
    "supporting_passages": "we operate three distribution centers",
}


def test_wrong_doc_type_gate_fails_on_an_esg_chunk_in_a_10k_answer():
    catalogue = {
        "c1": {"chunk_id": "c1", "doc_type": "10-K"},
        "c2": {"chunk_id": "c2", "doc_type": "sustainability"},
    }
    result = gate_wrong_doc_type({"q1": QUESTION}, {"q1": ["c1", "c2"]}, catalogue)
    assert result["status"] == FAIL
    assert result["violation_count"] == 1
    assert result["wrong_doc_type_rate"] == 0.5


def test_wrong_doc_type_gate_passes_when_every_chunk_matches():
    catalogue = {"c1": {"chunk_id": "c1", "doc_type": "10-K"}}
    result = gate_wrong_doc_type({"q1": QUESTION}, {"q1": ["c1"]}, catalogue)
    assert result["status"] == PASS
    assert result["wrong_doc_type_rate"] == 0.0


def test_wrong_doc_type_gate_accepts_multi_source_required_doc_type():
    # Multi-source rows carry one doc type per source: "10-K|10-K". Treating
    # that field as a single string wrongly fails every chunk.
    question = dict(QUESTION, required_doc_type="10-K|10-K", supporting_chunk_ids="c1|c2")
    catalogue = {
        "c1": {"chunk_id": "c1", "doc_type": "10-K"},
        "c2": {"chunk_id": "c2", "doc_type": "10-K"},
    }
    result = gate_wrong_doc_type({"q1": question}, {"q1": ["c1", "c2"]}, catalogue)
    assert result["status"] == PASS
    assert result["wrong_doc_type_rate"] == 0.0


def test_wrong_doc_type_gate_still_fails_a_type_outside_the_allowed_set():
    question = dict(QUESTION, required_doc_type="10-K|10-K")
    catalogue = {
        "c1": {"chunk_id": "c1", "doc_type": "10-K"},
        "c2": {"chunk_id": "c2", "doc_type": "sustainability"},
    }
    result = gate_wrong_doc_type({"q1": question}, {"q1": ["c1", "c2"]}, catalogue)
    assert result["status"] == FAIL
    assert result["violation_count"] == 1


def test_wrong_doc_type_gate_never_silently_passes_without_metadata():
    result = gate_wrong_doc_type({"q1": QUESTION}, {"q1": ["c1"]}, None)
    assert result["status"] == NOT_EVALUATED


def test_wrong_doc_type_gate_does_not_pass_when_a_retrieved_chunk_is_unknown():
    # An unknown chunk cannot be certified as the right doc type, so the gate
    # must refuse to pass rather than ignore it.
    catalogue = {"c1": {"chunk_id": "c1", "doc_type": "10-K"}}
    result = gate_wrong_doc_type({"q1": QUESTION}, {"q1": ["c1", "ghost"]}, catalogue)
    assert result["status"] == NOT_EVALUATED
    assert "ghost" in result["unresolved_chunk_ids"]


def test_gold_integrity_gate_catches_a_chunk_from_the_wrong_company():
    catalogue = {"c1": {"chunk_id": "c1", "ticker": "LULU", "filing_year": "2024",
                        "section_code": "Item_1"}}
    result = gate_gold_integrity([QUESTION], catalogue)
    assert result["status"] == FAIL
    assert result["mismatches"][0]["field"] == "ticker"
    assert result["mismatches"][0]["claimed"] == "ASO"
    assert result["mismatches"][0]["actual"] == "LULU"


def test_gold_integrity_gate_catches_a_chunk_from_the_wrong_year():
    catalogue = {"c1": {"chunk_id": "c1", "ticker": "ASO", "filing_year": "2026",
                        "section_code": "Item_1"}}
    result = gate_gold_integrity([QUESTION], catalogue)
    assert result["status"] == FAIL
    assert any(m["field"] == "filing_year" for m in result["mismatches"])


def test_gold_integrity_gate_passes_on_a_consistent_contract():
    catalogue = {"c1": {"chunk_id": "c1", "ticker": "ASO", "filing_year": "2024",
                        "section_code": "Item_1"}}
    assert gate_gold_integrity([QUESTION], catalogue)["status"] == PASS


def test_gold_integrity_aligns_multi_source_fields_by_position():
    # XC rows carry parallel pipe-separated lists; index i must line up.
    question = {
        "question_id": "xc",
        "question_group": "cross_company",
        "supporting_chunk_ids": "a|b",
        "supporting_tickers": "RCKY|BOBS",
        "supporting_filing_years": "2025|2026",
        "supporting_section_codes": "Item_1A|Item_1A",
    }
    catalogue = {
        "a": {"chunk_id": "a", "ticker": "RCKY", "filing_year": "2025", "section_code": "Item_1A"},
        "b": {"chunk_id": "b", "ticker": "BOBS", "filing_year": "2026", "section_code": "Item_1A"},
    }
    assert gate_gold_integrity([question], catalogue)["status"] == PASS

    swapped = dict(question, supporting_tickers="BOBS|RCKY")
    assert gate_gold_integrity([swapped], catalogue)["status"] == FAIL


def test_gold_integrity_skips_refusal_rows_which_have_no_chunks():
    refusal = {
        "question_id": "ref",
        "question_group": "refusal",
        "supporting_chunk_ids": "",
        "supporting_tickers": "",
        "supporting_filing_years": "",
        "supporting_section_codes": "",
    }
    catalogue = {"c1": {"chunk_id": "c1", "ticker": "ASO", "filing_year": "2024",
                        "section_code": "Item_1"}}
    assert gate_gold_integrity([refusal], catalogue)["status"] == PASS


def test_evidence_gate_fails_when_the_passage_is_not_in_the_cited_chunk():
    catalogue = {"c1": {"chunk_id": "c1", "embedding_text": "something else entirely"}}
    result = gate_evidence_present([QUESTION], catalogue, "embedding_text")
    assert result["status"] == FAIL
    assert result["not_found_count"] == 1


def test_evidence_gate_passes_and_ignores_whitespace_and_case():
    catalogue = {
        "c1": {"chunk_id": "c1", "embedding_text": "We  operate THREE\ndistribution centers today"}
    }
    result = gate_evidence_present([QUESTION], catalogue, "embedding_text")
    assert result["status"] == PASS


def test_evidence_gate_not_evaluated_without_a_text_column():
    catalogue = {"c1": {"chunk_id": "c1", "ticker": "ASO"}}
    result = gate_evidence_present([QUESTION], catalogue, "embedding_text")
    assert result["status"] == NOT_EVALUATED


def test_evidence_gate_reports_passage_count_mismatch_instead_of_skipping():
    # Two chunks but one passage: the row cannot be verified. Silently checking
    # only the first chunk would let unverified evidence through.
    question = dict(QUESTION, supporting_chunk_ids="c1|c2", supporting_passages="only one")
    catalogue = {
        "c1": {"chunk_id": "c1", "embedding_text": "only one"},
        "c2": {"chunk_id": "c2", "embedding_text": "only one"},
    }
    result = gate_evidence_present([question], catalogue, "embedding_text")
    assert result["status"] == FAIL
    assert result["misalignment_count"] == 1
    assert result["misalignments"][0]["chunk_count"] == 2
    assert result["misalignments"][0]["passage_count"] == 1


def test_gold_integrity_reports_field_count_mismatch_instead_of_skipping():
    question = dict(
        QUESTION,
        supporting_chunk_ids="c1|c2",
        supporting_tickers="ASO",  # one ticker for two chunks
        supporting_filing_years="2024|2024",
        supporting_section_codes="Item_1|Item_1",
    )
    catalogue = {
        "c1": {"chunk_id": "c1", "ticker": "ASO", "filing_year": "2024", "section_code": "Item_1"},
        "c2": {"chunk_id": "c2", "ticker": "ASO", "filing_year": "2024", "section_code": "Item_1"},
    }
    result = gate_gold_integrity([question], catalogue)
    assert result["status"] == FAIL
    assert result["misalignment_count"] == 1
    assert result["misalignments"][0]["field"] == "supporting_tickers"


# --------------------------------------------------------------------------
# Decision rule (Ayse Cetinel, 2026-07-16)
# --------------------------------------------------------------------------


def build_model(model_id, overall_mrr, group_mrrs, gate_status=PASS):
    return {
        "model_id": model_id,
        "gates": {"overall": gate_status},
        "scores": {
            "overall": {"mrr_at_10": overall_mrr},
            "by_group": {group: {"mrr_at_10": value} for group, value in group_mrrs.items()},
        },
    }


ALL_GROUPS = ("Item_1", "Item_1A", "Item_7", "Item_8", "cross_company", "time_change")


def test_challenger_wins_only_when_both_conditions_are_met():
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    challenger = build_model(CHALLENGER_MODEL_ID, 0.65, {g: 0.65 for g in ALL_GROUPS})
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    assert decision["winner"] == CHALLENGER_MODEL_ID
    assert decision["condition_overall_improvement"]["met"] is True
    assert decision["condition_group_breadth"]["met"] is True


def test_base_wins_when_overall_gain_is_below_the_threshold():
    # +0.02 overall: fails condition 1 even though every group improves.
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    challenger = build_model(CHALLENGER_MODEL_ID, 0.62, {g: 0.62 for g in ALL_GROUPS})
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    assert decision["winner"] == DEFAULT_MODEL_ID
    assert decision["condition_overall_improvement"]["met"] is False


def test_base_wins_when_the_gain_is_concentrated_in_too_few_groups():
    # Big overall gain, but driven by 3 groups only: fails condition 2.
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    challenger_groups = {g: 0.60 for g in ALL_GROUPS}
    for group in ALL_GROUPS[:3]:
        challenger_groups[group] = 0.90
    challenger = build_model(CHALLENGER_MODEL_ID, 0.75, challenger_groups)
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    assert decision["winner"] == DEFAULT_MODEL_ID
    assert decision["condition_overall_improvement"]["met"] is True
    assert decision["condition_group_breadth"]["met"] is False
    assert decision["condition_group_breadth"]["actual"] == 3


def test_exactly_four_groups_and_exactly_threshold_is_a_win():
    # The rule says "at least 0.03" and "at least 4 of 6" - boundaries included.
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    challenger_groups = {g: 0.60 for g in ALL_GROUPS}
    for group in ALL_GROUPS[:4]:
        challenger_groups[group] = 0.63
    challenger = build_model(CHALLENGER_MODEL_ID, 0.63, challenger_groups)
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    assert decision["winner"] == CHALLENGER_MODEL_ID
    assert decision["condition_group_breadth"]["actual"] == 4


def test_failed_gates_block_the_decision_entirely():
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    challenger = build_model(
        CHALLENGER_MODEL_ID, 0.95, {g: 0.95 for g in ALL_GROUPS}, gate_status=FAIL
    )
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    # A model that returns wrong-doc-type chunks cannot win on score.
    assert decision["status"] == NOT_EVALUATED
    assert "winner" not in decision


def test_ungated_metadata_blocks_the_decision():
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS},
                       gate_status=NOT_EVALUATED)
    challenger = build_model(CHALLENGER_MODEL_ID, 0.70, {g: 0.70 for g in ALL_GROUPS},
                             gate_status=NOT_EVALUATED)
    decision = apply_decision_rule(
        {DEFAULT_MODEL_ID: base, CHALLENGER_MODEL_ID: challenger}, "mrr_at_10", 0.03
    )
    assert decision["status"] == NOT_EVALUATED


def test_decision_needs_both_models_present():
    base = build_model(DEFAULT_MODEL_ID, 0.60, {g: 0.60 for g in ALL_GROUPS})
    decision = apply_decision_rule({DEFAULT_MODEL_ID: base}, "mrr_at_10", 0.03)
    assert decision["status"] == NOT_EVALUATED


# --------------------------------------------------------------------------
# End-to-end
# --------------------------------------------------------------------------


def test_evaluate_excludes_refusal_rows_from_retrieval_scoring():
    questions = [
        dict(QUESTION),
        {
            "question_id": "ref1",
            "question_group": "refusal",
            "required_doc_type": "10-K",
            "supporting_chunk_ids": "",
            "supporting_tickers": "",
            "supporting_filing_years": "",
            "supporting_section_codes": "",
            "supporting_passages": "",
        },
    ]
    retrieval = {"m": {"q1": ["c1"], "ref1": ["c1"]}}
    report = evaluate(questions, retrieval, None, "embedding_text")
    assert report["scored_question_count"] == 1
    assert report["refusal_question_count"] == 1
    assert [r["question_id"] for r in report["models"]["m"]["per_question"]] == ["q1"]


def test_evaluate_warns_when_depth_is_too_shallow_for_at_10_metrics():
    # Ayse's procedure asks for top-5, but the metric spec names MRR@10 and
    # nDCG@10. A 5-deep run cannot produce a true @10 figure.
    questions = [dict(QUESTION)]
    retrieval = {"m": {"q1": ["c1", "x", "y", "z", "w"]}}
    report = evaluate(questions, retrieval, None, "embedding_text")
    assert report["models"]["m"]["deep_metrics_truncated"] is True
    assert any("NOT true @10" in w for w in report["warnings"])


def test_evaluate_does_not_flag_truncation_at_full_depth():
    questions = [dict(QUESTION)]
    retrieval = {"m": {"q1": ["c1"] + [f"x{i}" for i in range(9)]}}
    report = evaluate(questions, retrieval, None, "embedding_text")
    assert report["models"]["m"]["deep_metrics_truncated"] is False


def test_evaluate_warns_about_questions_a_model_never_answered():
    questions = [dict(QUESTION), dict(QUESTION, question_id="q2")]
    retrieval = {"m": {"q1": ["c1"]}}
    report = evaluate(questions, retrieval, None, "embedding_text")
    assert any("no retrieval results" in w for w in report["warnings"])


def test_evaluate_marks_gates_not_evaluated_without_metadata():
    questions = [dict(QUESTION)]
    retrieval = {"m": {"q1": ["c1"]}}
    report = evaluate(questions, retrieval, None, "embedding_text")
    assert report["models"]["m"]["gates"]["overall"] == NOT_EVALUATED


def test_main_exits_nonzero_when_gates_could_not_be_evaluated(tmp_path):
    # A run that certified nothing must not report success via exit code 0.
    retrieval = tmp_path / "retrieval.csv"
    retrieval.write_text(
        "model_id,question_id,rank,chunk_id\n"
        "bge_base_en_v1_5,10K-V2-I1-001,1,238872\n",
        encoding="utf-8",
    )
    code = main(
        [
            "--questions",
            str(PROJECT_ROOT / "data" / "00_reference" / "rag_eval_questions.csv"),
            "--retrieval",
            str(retrieval),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 1


def test_evaluate_full_pass_with_complete_metadata():
    questions = [dict(QUESTION)]
    retrieval = {"m": {"q1": ["c1"]}}
    catalogue = {
        "c1": {
            "chunk_id": "c1",
            "doc_type": "10-K",
            "ticker": "ASO",
            "filing_year": "2024",
            "section_code": "Item_1",
            "embedding_text": "We operate three distribution centers in Katy, Texas",
        }
    }
    report = evaluate(questions, retrieval, catalogue, "embedding_text")
    model = report["models"]["m"]
    assert model["gates"]["overall"] == PASS
    assert model["scores"]["overall"]["recall_at_5"] == 1.0
