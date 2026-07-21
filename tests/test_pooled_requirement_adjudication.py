from scripts.build_pooled_requirement_adjudication import (
    candidate_code, route_matches_chunk,
)


def test_candidate_codes_are_stable_and_do_not_expose_chunk_id():
    first = candidate_code("q1", 1, "12345")
    second = candidate_code("q1", 1, "12345")
    assert first == second
    assert "12345" not in first


def test_candidate_codes_change_by_requirement():
    assert candidate_code("q1", 1, "123") != candidate_code("q1", 2, "123")


def test_route_matching_requires_full_authorized_source():
    route = {"subquery": {
        "ticker": "COST", "filing_year": 2024,
        "accession_number": "acc", "section_code": "Item_7",
    }}
    matching = {
        "ticker": "COST", "filing_year": "2024",
        "accession_number": "acc", "section_code": "Item_7",
    }
    assert route_matches_chunk(route, matching)
    assert not route_matches_chunk(route, {**matching, "section_code": "Item_8"})
