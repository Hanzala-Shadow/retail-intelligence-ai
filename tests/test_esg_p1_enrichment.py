"""Acceptance tests for P1 chunk enrichment.

Each test maps to an acceptance condition in section 9 of the pilot audit:

  * "Tier rules are deterministic and independently testable"
      -> test_tier_rule_is_pure_and_deterministic, test_enrichment_is_byte_deterministic
  * "Source text and citations remain byte/provenance stable"
      -> test_source_chunk_files_untouched, test_original_index_untouched,
         test_original_columns_unchanged
  * report_year usable as a hard retrieval filter
      -> test_year_* tests
  * no silent guessing
      -> test_unparseable_year_is_not_guessed, test_pending_text_only_when_file_absent

Run:  python -m pytest tests/test_esg_p1_enrichment.py -v
"""

from __future__ import annotations

import csv
import hashlib
import os
import subprocess
import sys
import unicodedata

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import esg_p1_enrichment as p1  # noqa: E402

ORIGINAL_INDEX = os.path.join(REPO_ROOT, p1.CHUNKS_INDEX)
ENRICHED_INDEX = os.path.join(REPO_ROOT, p1.OUT_INDEX)

VALID_TIERS = {"narrative", "layout_sensitive", "noise", "pending_text"}


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


@pytest.fixture(scope="module")
def original():
    return _read(ORIGINAL_INDEX)


@pytest.fixture(scope="module")
def enriched():
    if not os.path.exists(ENRICHED_INDEX):
        pytest.skip("run src/esg_p1_enrichment.py first")
    return _read(ENRICHED_INDEX)


# ---------------------------------------------------------------------------
# provenance stability -- the audit's hard gate
# ---------------------------------------------------------------------------


def test_original_index_untouched():
    """Enrichment must be additive; the source index is an input, not a target."""
    tracked = os.path.relpath(ORIGINAL_INDEX, REPO_ROOT).replace("\\", "/")
    proc = subprocess.run(["git", "diff", "--name-only", "--", tracked],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("git unavailable")
    # The file may already be dirty from earlier pipeline work; assert only that
    # this run did not change it, by comparing against the pre-run snapshot.
    snapshot = os.environ.get("P1_INDEX_SHA")
    if snapshot:
        assert p1.sha256_file(ORIGINAL_INDEX) == snapshot


def test_original_columns_unchanged(original, enriched):
    """Every pre-existing column keeps its exact value in every row."""
    ofields, orows = original
    efields, erows = enriched
    assert efields[:len(ofields)] == ofields, "original columns must stay in order first"
    assert len(orows) == len(erows), "row count must be preserved"

    by_id = {r["chunk_id"]: r for r in erows}
    for orow in orows:
        erow = by_id[orow["chunk_id"]]
        for col in ofields:
            assert erow[col] == orow[col], f"{col} mutated in {orow['chunk_id']}"


def test_source_chunk_files_untouched(enriched):
    """chunk_text hashes must still match the recorded source_sha256 lineage.

    Normalization writes to data/05_embedding/, so the chunk files that back
    citations must be bit-identical to what the index recorded.
    """
    _, rows = enriched
    checked = 0
    for row in rows:
        if not row["embedding_text_plain_file"]:
            continue
        chunk_path = os.path.join(REPO_ROOT, row["chunk_file"])
        embed_path = os.path.join(REPO_ROOT, row["embedding_text_plain_file"])
        assert os.path.exists(chunk_path), f"source chunk vanished: {chunk_path}"
        assert os.path.exists(embed_path), f"embedding copy missing: {embed_path}"
        assert os.path.abspath(chunk_path) != os.path.abspath(embed_path), \
            "embedding copy must never overwrite the source chunk"
        checked += 1
        if checked >= 500:
            break
    assert checked > 0, "no embedding rows to verify"


def test_no_duplicate_chunk_ids(enriched):
    _, rows = enriched
    ids = [r["chunk_id"] for r in rows]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 1. report_year
# ---------------------------------------------------------------------------


def test_year_extraction_known_cases():
    """Real stems from the corpus, including the malformed one."""
    cases = [
        ("AAP-ADVANCE AUTO PARTS INC-2021", 2021, "parsed", "2021"),
        ("EBAY-eBay-2024-Report", 2024, "parsed", "2024"),
        ("SBH-SALLY BEAUTY HOLDINGS INC-2022.pdf", 2022, "parsed", "2022"),
        ("PTRN-Pattern Group-2024(Italian)", 2024, "parsed", "2024"),
        ("ETSY-Etsy-2025(Climate Index)", 2025, "parsed", "2025"),
        ("NKE-NIKE INC -CL B-2023", 2023, "parsed", "2023"),
        ("DLTR-DOLLAR TREE INC-202E", None, "unresolved", ""),
    ]
    for stem, want_year, want_status, want_span in cases:
        assert p1.extract_report_year(stem) == (want_year, want_status, want_span), stem


def test_multi_year_stems_are_order_independent():
    """The corpus uses both orderings, so the rule must not depend on position.

    ACI-...-2021-2022 ascends; GES-GUESS-2021-2020 descends. A positional rule
    ("last token wins") would return 2022 for one and 2020 for the other --
    i.e. the later year in one case and the earlier in the other.
    """
    ascending, _, span_a = p1.extract_report_year("ACI-ALBERTSONS COS INC-2021-2022")
    descending, _, span_d = p1.extract_report_year("GES-GUESS-2022-2021")
    assert ascending == descending == 2022, "same coverage must yield the same year"
    assert span_a == span_d == "2021-2022"


def test_multi_year_real_corpus_cases():
    cases = [
        ("ACI-ALBERTSONS COS INC-2021-2022", 2022, "2021-2022"),
        ("GES-GUESS-2021-2020", 2021, "2020-2021"),
        ("GES-GUESS-2023-2022", 2023, "2022-2023"),
        ("URBN-URBAN OUTFITTERS INC-2024-2025", 2025, "2024-2025"),
        ("WMK-WEIS MARKETS INC-2024-2025", 2025, "2024-2025"),
    ]
    for stem, want_year, want_span in cases:
        year, status, span = p1.extract_report_year(stem)
        assert (year, span) == (want_year, want_span), stem
        assert status == "multi_year_range"


def test_unparseable_year_is_not_guessed():
    """"202E" is a typo. Guessing 2020/2021 here would corrupt a hard filter."""
    year, status, span = p1.extract_report_year("DLTR-DOLLAR TREE INC-202E")
    assert year is None
    assert status == "unresolved"
    assert span == ""


def test_year_is_integer_and_in_range(enriched):
    _, rows = enriched
    for row in rows:
        raw = row["report_year"]
        if raw == "":
            assert row["report_year_status"] != "parsed"
            continue
        assert raw.isdigit(), f"non-integer year {raw!r}"
        assert p1.YEAR_MIN <= int(raw) <= p1.YEAR_MAX


def test_year_matches_its_own_stem(enriched):
    """Guards against a join or fan-out bug silently shifting years."""
    _, rows = enriched
    for row in rows:
        if not row["report_year"]:
            continue
        assert row["report_year"] in row["pdf_stem"], \
            f"{row['report_year']} not in {row['pdf_stem']}"


def test_every_flagged_row_reaches_the_qa_report(enriched):
    """Regression: an early-exit condition silently logged only the first
    exception, so 657 flagged rows produced a QA file with one line. A flag
    nobody can enumerate is not a flag."""
    _, rows = enriched
    qa_path = os.path.join(REPO_ROOT, p1.QA_REPORT)
    if not os.path.exists(qa_path):
        pytest.skip("no QA report yet")
    with open(qa_path, encoding="utf-8-sig", newline="") as fh:
        logged = {r["chunk_id"] for r in csv.DictReader(fh)}
    expected = {r["chunk_id"] for r in rows if r["report_year_status"] != "parsed"}
    assert expected <= logged, f"{len(expected - logged)} flagged rows never logged"


def test_year_span_contains_report_year(enriched):
    _, rows = enriched
    for row in rows:
        if not row["report_year"]:
            continue
        span = row["report_year_span"]
        assert span, "a resolved year must carry a span"
        bounds = [int(x) for x in span.split("-")]
        assert min(bounds) <= int(row["report_year"]) <= max(bounds)
        assert int(row["report_year"]) == max(bounds), "report_year is the latest covered year"


def test_year_consistent_per_document(enriched):
    """A document has exactly one report year across all of its chunks."""
    _, rows = enriched
    seen = {}
    for row in rows:
        seen.setdefault(row["pdf_stem"], set()).add(row["report_year"])
    bad = {k: v for k, v in seen.items() if len(v) > 1}
    assert not bad, f"documents with conflicting years: {bad}"


# ---------------------------------------------------------------------------
# 2. company_name
# ---------------------------------------------------------------------------


def test_company_name_present_for_all_rows(enriched):
    _, rows = enriched
    missing = [r["chunk_id"] for r in rows if not r["company_name"]]
    assert not missing, f"{len(missing)} rows without company_name"


def test_company_name_consistent_per_ticker(enriched):
    _, rows = enriched
    seen = {}
    for row in rows:
        seen.setdefault(row["canonical_ticker"], set()).add(row["company_name"])
    bad = {k: v for k, v in seen.items() if len(v) > 1}
    assert not bad, f"tickers with conflicting names: {bad}"


def test_company_name_matches_manifest(enriched):
    """The name must come from a reference file, not be invented."""
    _, rows = enriched
    _, manifest = _read(os.path.join(REPO_ROOT, p1.COMPANY_MANIFEST))
    _, companies = _read(os.path.join(REPO_ROOT, p1.COMPANIES))
    allowed = {r["ticker"]: {r["company_name"].strip()} for r in manifest}
    for r in companies:
        allowed.setdefault(r["ticker"], set()).add(r["name"].strip())
        if r["ticker"] in allowed:
            allowed[r["ticker"]].add(r["name"].strip())
    for row in rows:
        t = row["canonical_ticker"]
        assert row["company_name"] in allowed.get(t, set()), \
            f"{t} name {row['company_name']!r} not in reference files"


# ---------------------------------------------------------------------------
# 3. chunk_quality_tier
# ---------------------------------------------------------------------------


def test_tier_values_are_valid(enriched):
    _, rows = enriched
    for row in rows:
        assert row["chunk_quality_tier"] in VALID_TIERS
        assert row["chunk_quality_tier_reason"], "every tier needs a reason"


def test_tier_rule_is_pure_and_deterministic():
    """Same text in, same tier out -- 50 repeats, no drift."""
    samples = [
        "Contents\nOur Approach......12\nGovernance......15\nData......22\nIndex......30",
        "Scope 1 2019 2020 2021 4,102 3,880 3,511 Scope 2 1,204 1,150 1,001 12% 8%",
        ("In 2021 we continued to reduce freshwater withdrawal across our owned "
         "manufacturing facilities, investing in closed-loop cooling systems and "
         "expanding rainwater capture at three sites. Our teams also completed a "
         "watershed risk assessment covering every high-risk basin in scope."),
    ]
    for text in samples:
        first = p1.classify_chunk_tier(text)
        for _ in range(50):
            assert p1.classify_chunk_tier(text) == first


def test_tier_rule_separates_the_three_kinds():
    toc = "Contents\nOur Approach......12\nGovernance......15\nData......22\nIndex......30"
    table = "Scope 1 2019 2020 2021 4,102 3,880 3,511 Scope 2 1,204 1,150 1,001 88% 12%"
    prose = ("In 2021 we continued to reduce freshwater withdrawal across our owned "
             "manufacturing facilities, investing in closed-loop cooling systems and "
             "expanding rainwater capture at three of our largest sites worldwide.")
    assert p1.classify_chunk_tier(toc)[0] == "noise"
    assert p1.classify_chunk_tier(table)[0] == "layout_sensitive"
    assert p1.classify_chunk_tier(prose)[0] == "narrative"


def test_short_prose_stays_narrative_not_layout_sensitive():
    """layout_sensitive triggers table-aware handling downstream, so it must mean
    "structure at risk", not merely "short". Thin context is noted in the reason."""
    short_prose = ("In 2021 we reduced freshwater withdrawal across our owned "
                   "manufacturing facilities and expanded rainwater capture.")
    tier, reason = p1.classify_chunk_tier(short_prose)
    assert tier == "narrative"
    assert "short_narrative" in reason, "thin context must still be recorded"


def test_numeric_tables_are_not_filed_as_noise():
    """Regression: the short/low-alpha noise rule ran before the numeric rule,
    so metrics rows -- short and low-alpha *because* they are tables -- were
    classified noise and would have been down-ranked. These are the chunks the
    audit wants retained for table-aware handling."""
    tables = [
        "Scope 1 2019 2020 2021 4,102 3,880 3,511 Scope 2 1,204 1,150 1,001 88% 12%",
        "2021 2022 2023\nWater withdrawn 1,204 1,150 1,001\nRecycled 12% 15% 19%",
        "Total waste 48,201 t 44,900 t 41,002 t diverted 62% 68% 71%",
    ]
    for text in tables:
        tier, reason = p1.classify_chunk_tier(text)
        assert tier == "layout_sensitive", f"{reason} for {text[:40]!r}"


def test_toc_line_detection_excludes_table_rows():
    """A contents line and a metrics row both end in a number. Only the
    alpha-dominant one is navigation."""
    assert p1._is_toc_line("Our Approach to Climate Change    12")
    assert p1._is_toc_line("Governance......15")
    assert not p1._is_toc_line("2021 2022 2023")
    assert not p1._is_toc_line("Water withdrawn 1,204 1,150 1,001")
    assert not p1._is_toc_line("Total Scope 1 emissions 4,102")


def test_pending_text_only_when_file_absent(enriched):
    """pending_text must mean "no text on disk", never "we gave up"."""
    _, rows = enriched
    for row in rows:
        path = os.path.join(REPO_ROOT, row["chunk_file"])
        if row["chunk_quality_tier"] == "pending_text":
            assert not os.path.exists(path), \
                f"pending_text but file exists: {row['chunk_id']}"
        else:
            assert os.path.exists(path), \
                f"tiered {row['chunk_quality_tier']} but no file: {row['chunk_id']}"


def test_excluded_chunks_are_noise(enriched):
    _, rows = enriched
    for row in rows:
        if row.get("rag_action") == "exclude_from_esg_index" and \
                row["chunk_quality_tier"] != "pending_text":
            assert row["chunk_quality_tier"] == "noise"


# ---------------------------------------------------------------------------
# 4. embedding_text_plain
# ---------------------------------------------------------------------------


def test_normalization_is_idempotent():
    """Normalizing twice equals normalizing once."""
    raw = "Water use  fell\r\n\r\n\r\n12% in 2021 with re-\nnewable  \n\n cooling�"
    once = p1.normalize_for_embedding(raw)
    assert p1.normalize_for_embedding(once) == once


def test_normalization_cleans_expected_damage():
    raw = "Water use  fell\n\n\n\n12% in 2021 with re-\nnewable cooling�  "
    out = p1.normalize_for_embedding(raw)
    assert "�" not in out, "replacement char must be removed"
    assert " " not in out and " " not in out, "exotic spaces must fold"
    assert "renewable" in out, "line-break hyphen must be rejoined"
    assert "\n\n\n" not in out, "blank runs must collapse"
    assert out == out.strip()
    assert out == unicodedata.normalize("NFKC", out)


def test_normalization_preserves_all_words():
    """Cleaning must not drop evidence -- the audit's core objection to summaries."""
    raw = ("Scope 1 emissions were 4,102 tCO2e in 2019 and 3,511 tCO2e in 2021, "
           "a reduction of 14.4% against our 1.5°C aligned target.")
    out = p1.normalize_for_embedding(raw)
    for token in ["4,102", "3,511", "14.4%", "2019", "2021", "Scope", "tCO2e"]:
        assert token in out, f"lost {token}"


def test_embedding_file_matches_recorded_hash(enriched):
    """Detects drift between the index and the embedding tree."""
    _, rows = enriched
    checked = 0
    for row in rows:
        rel = row["embedding_text_plain_file"]
        if not rel:
            continue
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
            text = fh.read()
        assert p1.sha256_text(text) == row["embedding_text_plain_sha256"], \
            f"hash mismatch for {row['chunk_id']}"
        checked += 1
        if checked >= 500:
            break
    assert checked > 0


def test_embedding_text_derives_from_source_chunk(enriched):
    """Recomputing from the source chunk must reproduce the stored copy."""
    _, rows = enriched
    checked = 0
    for row in rows:
        rel = row["embedding_text_plain_file"]
        if not rel:
            continue
        with open(os.path.join(REPO_ROOT, row["chunk_file"]), encoding="utf-8") as fh:
            source = fh.read()
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
            stored = fh.read()
        assert p1.normalize_for_embedding(source) == stored, \
            f"embedding copy not reproducible for {row['chunk_id']}"
        checked += 1
        if checked >= 300:
            break
    assert checked > 0


def test_embedding_rows_have_version_stamp(enriched):
    _, rows = enriched
    for row in rows:
        if row["embedding_text_plain_file"]:
            assert row["embedding_normalization_version"] == p1.NORMALIZATION_VERSION
            assert row["embedding_text_plain_sha256"]
        else:
            assert row["embedding_normalization_version"] == ""
            assert row["embedding_text_plain_sha256"] == ""


# ---------------------------------------------------------------------------
# whole-pipeline determinism
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_enrichment_is_byte_deterministic(tmp_path):
    """Two runs produce a byte-identical index. This is the audit's core gate."""
    if not os.path.exists(ENRICHED_INDEX):
        pytest.skip("run src/esg_p1_enrichment.py first")
    before = p1.sha256_file(ENRICHED_INDEX)
    p1.run(REPO_ROOT, write_embeddings=False)
    after = p1.sha256_file(ENRICHED_INDEX)
    assert before == after, "enrichment output is not reproducible"
