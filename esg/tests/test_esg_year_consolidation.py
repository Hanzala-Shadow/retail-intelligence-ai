"""Guards that the corpus has exactly one report-year rule.

Why this file exists
--------------------
On 2026-07-29 the same document carried two different years in two files that
both feed retrieval:

    VFC-VF CORP-2023-2024   vector_index_manifest.inferred_year  = 2023
                            esg_chunks_index_enriched.report_year = 2024

Three stages each answered "what year is this document?" and two of them held
their own regex. The scoping driver used max(years), the enrichment stage used
max(years) via a private copy, and the manifest builder used the *first* token.
The disagreement was not cosmetic: it also decided document selection, so
WMK-...-2024-2025 and VFC-...-2024-2025 were excluded from a 2023/2024 run by
one rule while the other rule would have labelled them 2024 and pulled them in.

This mirrors tests/test_esg_vector_manifest.py, which exists because
LAYOUT_AUDIT_VERSION was duplicated across two modules and a silent mismatch
would have quarantined the whole corpus. Same failure class, same guard.

A grep-style "no year regex anywhere else" test would false-fail: several
modules legitimately parse years out of tracker fields or page text, which is a
different question from "the report year of this stem". These tests therefore
check *behaviour and identity* of the stem-year consumers instead.
"""

from __future__ import annotations

import pytest

import build_esg_vector_manifest as manifest
import esg_p1_enrichment as p1
import esg_stem_remap_audit as remap
import esg_year


# (stem, canonical year, span). Every real multi-year naming shape in the
# corpus is represented: ascending, descending, and the ranges that actually
# triggered the incident.
STEMS = [
    ("VFC-VF CORP-2023-2024", 2024, "2023-2024"),          # the incident
    ("VFC-VF CORP-2024-2025", 2025, "2024-2025"),          # excluded from the run
    ("WMK-WEIS MARKETS INC-2024-2025", 2025, "2024-2025"),
    ("ACI-ALBERTSONS COS INC-2021-2022", 2022, "2021-2022"),  # ascending
    ("GES-GUESS-2023-2022", 2023, "2022-2023"),               # descending
    ("GES-GUESS-2021-2020", 2021, "2020-2021"),               # descending
    ("AAP-ADVANCE AUTO PARTS INC-2017-2018", 2018, "2017-2018"),
    ("COST-COSTCO WHOLESALE CORP-2022-2023", 2023, "2022-2023"),
    ("ABG-ASBURY AUTOMOTIVE GROUP INC-2023", 2023, "2023"),    # single year
    ("EBAY-eBay-2021-Report", 2021, "2021"),                   # decorated
]


@pytest.mark.parametrize("stem,year,span", STEMS)
def test_canonical_rule_is_max_year(stem: str, year: int, span: str) -> None:
    got_year, _status, got_span = esg_year.extract_report_year(stem)
    assert got_year == year, stem
    assert got_span == span, stem


@pytest.mark.parametrize("stem,year,_span", STEMS)
def test_manifest_agrees_with_canonical(stem: str, year: int, _span: str) -> None:
    """The manifest's inferred_year must equal the canonical year.

    This is the assertion that would have caught the VFC incident: the old
    first-token implementation returns "2023" here.
    """
    assert manifest.infer_year(stem) == str(year), stem


@pytest.mark.parametrize("stem,_year,span", STEMS)
def test_manifest_span_agrees_with_canonical(stem: str, _year: int, span: str) -> None:
    assert manifest.infer_year_span(stem) == span, stem


def test_enrichment_does_not_own_a_copy_of_the_rule() -> None:
    """Identity, not equality -- a copy that agrees today can drift tomorrow."""
    assert p1.extract_report_year is esg_year.extract_report_year


def test_stem_remap_audit_delegates() -> None:
    assert remap.years_of("VFC-VF CORP-2023-2024") == [2023, 2024]
    assert remap.years_of("EBAY-eBay-2021-Report") == [2021]


def test_unparseable_year_is_not_guessed() -> None:
    """"202E" is a real typo in this corpus. It must not become 2020."""
    year, status, span = esg_year.extract_report_year("DLTR-DOLLAR TREE INC-202E")
    assert year is None and status == "unresolved" and span == ""
    assert manifest.infer_year("DLTR-DOLLAR TREE INC-202E") == ""
    assert manifest.infer_year_span("DLTR-DOLLAR TREE INC-202E") == ""


def test_report_year_span_is_carried_in_the_manifest_schema() -> None:
    """The span must survive the MANIFEST_FIELDS projection.

    csv.DictWriter is built from MANIFEST_FIELDS, so a column missing from that
    list is dropped silently on the next manifest build -- no error, no warning.
    """
    assert "report_year_span" in manifest.MANIFEST_FIELDS
    assert "inferred_year" in manifest.MANIFEST_FIELDS


def test_ordering_conventions_do_not_change_the_answer() -> None:
    """Both orderings of the same coverage period resolve identically.

    This is the property that makes max() the right rule rather than a taste
    call: first-token calls 2021-2022 and 2022-2021 different years, max does
    not.
    """
    ascending, _, span_a = esg_year.extract_report_year("X-2021-2022")
    descending, _, span_d = esg_year.extract_report_year("X-2022-2021")
    assert ascending == descending == 2022
    assert span_a == span_d == "2021-2022"
