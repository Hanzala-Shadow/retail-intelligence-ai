from scripts.build_v213_quality_sample import category


def row(**updates):
    value = {
        "rag_action": "include",
        "quality_flags": [],
        "continuation_from_previous": False,
        "continues_to_next": False,
        "chunk_type": "narrative",
    }
    value.update(updates)
    return value


def test_sample_uses_canonical_backward_continuation_field():
    assert category(row(
        continuation_from_previous=True,
    )) == "backward_continuation"


def test_sample_strata_priority_preserves_policy_cases():
    assert category(row(
        rag_action="exclude",
        continuation_from_previous=True,
    )) == "excluded"
    assert category(row(
        quality_flags=["inherited_late_financial_region"],
        continues_to_next=True,
    )) == "late_financial"
    assert category(row(
        continues_to_next=True,
    )) == "forward_continuation"
