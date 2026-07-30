"""Tier 5 mathematical QA: coverage, bias, and the retrieval gate.

Distributional, like Tiers 2-4 -- a check here can only flag a population as
implausible or skewed, never prove a defect on its own. Run
qa_tier1_invariants.py first: these numbers are only meaningful once sections
and chunks are known to tile their parents correctly.

Where Tiers 2-4 ask "is the pipeline's bookkeeping healthy" and "is the text
worth retrieving", Tier 5 asks who and what the finished corpus actually
represents, and whether the RAG eligibility gates that decide what gets
indexed are exclusion mechanisms that happen to correlate with content --
i.e. a source of silent retrieval bias rather than pure quality control.

Everything is READ-ONLY. The database is opened with mode=ro. Checks 29 and
30 additionally read data/00_reference/vector_index_manifest.csv (built by
build_esg_vector_manifest.py) and the chunks table's own rag_action column;
neither is written to.

Checks
    26  corpus concentration by company (Gini coefficient, HHI, top-10 share
        of chunks)
    27  why do zero-chunk companies have zero chunks (cross-tabulated against
        whether a report was ever downloaded and, if so, where the
        parse -> section -> chunk pipeline stopped producing chunks)
    28  year distribution of chunks, and per-company-year coverage within
        each company's own reporting span
    29  what did the layout auto_hold gate actually exclude, and is the
        exclusion biased toward numeric/table-heavy content (held vs passed
        populations compared on token count, section code, and digit ratio)
    30  is the RAG-ineligible remainder (chunks whose rag_action is not
        index_as_esg) random or structured -- does it cluster by document or
        company

Usage
    python esg/scripts/esg_database_tiers/qa_tier5_coverage_bias.py
    python esg/scripts/esg_database_tiers/qa_tier5_coverage_bias.py --checks 26,27
    python esg/scripts/esg_database_tiers/qa_tier5_coverage_bias.py --json-out reports/qa_tier5.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
from esg_year import report_year  # noqa: E402
from qa_tier1_invariants import (  # noqa: E402
    CheckResult,
    connect,
    describe,
    load_documents,
    percentile,
)
from qa_tier2_sectioning import herfindahl_index, shannon_entropy_bits  # noqa: E402
from qa_tier4_content_validity import ALPHA_RE  # noqa: E402

ALL_CHECKS = ["26", "27", "28", "29", "30"]

# check 26: a corpus concentration is called out once the top 10 filers alone
# hold more than this share of all chunks. Not derived from this corpus.
TOP10_SHARE_WARN_FLOOR = 0.40
GINI_WARN_FLOOR = 0.55

# check 28: below this fraction of its own [min_year, max_year] span covered
# by at least one chunk, a company has real, not cosmetic, reporting gaps.
LOW_YEAR_COVERAGE_FLOOR = 0.50
LOW_YEAR_COVERAGE_SHARE_WARN = 0.25

# check 29: a section code needs at least this many held-or-passed chunks
# before its own held-rate is treated as informative rather than small-sample
# noise, mirroring qa_tier2's MIN_COOCCURRENCE convention.
SECTION_CODE_MIN_VOLUME = 30
# A section code's held rate must clear the corpus-wide rate by this many
# percentage points before it is called out as biased.
HELD_RATE_MARGIN = 0.10
LAYOUT_HELD_STATUS = "auto_hold"
LAYOUT_PASSED_STATUSES = {"auto_pass", "auto_pass_pdfium_coverage"}

# check 30: chunks whose rag_action is anything other than this are excluded
# from the ESG index regardless of layout or citation status -- the single
# source of truth for "does this chunk pass the RAG gate" (esg_chunker.py,
# esg_pipeline_qa.py, drive_to_db.py all write/read the same field).
RAG_ELIGIBLE_ACTION = "index_as_esg"


# ---------------------------------------------------------------------------
# small helpers specific to this tier
# ---------------------------------------------------------------------------


def gini_coefficient(values: list[float]) -> float:
    """Gini coefficient of a non-negative distribution (0 = perfectly equal,
    -> 1 as it concentrates in one unit). 0.0 for an empty or all-zero input.
    """
    n = len(values)
    if n == 0:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    ordered = sorted(values)
    weighted_sum = sum((i + 1) * v for i, v in enumerate(ordered))
    return (2 * weighted_sum) / (n * total) - (n + 1) / n


def load_companies(con: sqlite3.Connection) -> dict[int, dict]:
    return {
        row["company_id"]: {"ticker": (row["ticker"] or "").upper(), "name": row["name"]}
        for row in con.execute("SELECT company_id, ticker, name FROM companies")
    }


def load_vector_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# check 26 -- corpus concentration by company
# ---------------------------------------------------------------------------


def check_26(con, companies, examples_wanted):
    result = CheckResult("26", "Corpus concentration by company (Gini / top-10 share)")

    counts = Counter(row[0] for row in con.execute("SELECT company_id FROM chunks"))
    all_counts = [counts.get(cid, 0) for cid in companies]
    nonzero_counts = [c for c in all_counts if c > 0]
    total_chunks = sum(all_counts)

    if total_chunks == 0:
        result.status = "SKIP"
        result.headline = "no chunks in the corpus"
        return result

    # Zero-included Gini answers "how concentrated is the corpus across the
    # whole company universe" (a company with no chunks is maximal inequality
    # on its own); the chunks-only Gini isolates concentration given that a
    # company has any content at all. Both are reported; check 27 is the
    # dedicated home for the zero-chunk population itself.
    gini_all = gini_coefficient([float(c) for c in all_counts])
    gini_nonzero = gini_coefficient([float(c) for c in nonzero_counts])
    hhi = herfindahl_index(nonzero_counts)
    entropy = shannon_entropy_bits(nonzero_counts)
    max_entropy = math.log2(len(nonzero_counts)) if len(nonzero_counts) > 1 else 1.0

    ranked = sorted(companies.items(), key=lambda kv: -counts.get(kv[0], 0))
    top1_share = counts.get(ranked[0][0], 0) / total_chunks if ranked else None
    top10_share = sum(counts.get(cid, 0) for cid, _ in ranked[:10]) / total_chunks

    result.stats = {
        "companies_total": len(companies),
        "companies_with_chunks": len(nonzero_counts),
        "companies_with_zero_chunks": len(companies) - len(nonzero_counts),
        "total_chunks": total_chunks,
        "chunks_per_company_with_chunks": describe([float(c) for c in nonzero_counts]),
        "gini_all_companies_zero_included": round(gini_all, 4),
        "gini_companies_with_chunks_only": round(gini_nonzero, 4),
        "herfindahl_index_companies_with_chunks": round(hhi, 6),
        "entropy_bits_companies_with_chunks": round(entropy, 4),
        "normalized_entropy_companies_with_chunks": (
            round(entropy / max_entropy, 4) if max_entropy else None
        ),
        "top_1_share": round(top1_share, 4) if top1_share is not None else None,
        "top_10_share": round(top10_share, 4),
    }
    result.examples = [
        {
            "ticker": companies[cid]["ticker"],
            "name": companies[cid]["name"],
            "chunks": counts.get(cid, 0),
            "share_of_corpus": round(counts.get(cid, 0) / total_chunks, 4),
        }
        for cid, _ in ranked[:examples_wanted]
    ]

    if top10_share > TOP10_SHARE_WARN_FLOOR or gini_nonzero > GINI_WARN_FLOOR:
        result.warn(
            f"top 10 companies hold {top10_share:.1%} of all chunks "
            f"(Gini {gini_nonzero:.3f} among companies with any chunks) -- a skewed index "
            "biases retrieval toward verbose reporters regardless of relevance"
        )
    else:
        result.ok(
            f"top 10 companies hold {top10_share:.1%} of chunks; "
            f"Gini {gini_nonzero:.3f} among companies with any chunks"
        )
    return result


# ---------------------------------------------------------------------------
# check 27 -- why zero-chunk companies have zero chunks
# ---------------------------------------------------------------------------


def check_27(con, companies, examples_wanted):
    result = CheckResult("27", "Why zero-chunk companies have zero chunks")

    with_chunks = {row[0] for row in con.execute("SELECT DISTINCT company_id FROM chunks")}
    zero = sorted(cid for cid in companies if cid not in with_chunks)

    with_reports = {
        row[0] for row in con.execute("SELECT DISTINCT company_id FROM sustainability_reports")
    }
    docs_by_company: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in con.execute(
        "SELECT company_id, parse_status, doc_quality_status, rag_action FROM documents"
    ):
        docs_by_company[row["company_id"]].append(row)

    no_report_at_all, report_but_no_document, document_exists = [], [], []
    for cid in zero:
        has_report = cid in with_reports
        has_doc = cid in docs_by_company
        if not has_report and not has_doc:
            no_report_at_all.append(cid)
        elif not has_doc:
            report_but_no_document.append(cid)
        else:
            document_exists.append(cid)

    pipeline_stage_breakdown: Counter = Counter()
    document_exists_examples = []
    for cid in document_exists:
        for row in docs_by_company[cid]:
            pipeline_stage_breakdown[
                (row["parse_status"], row["doc_quality_status"], row["rag_action"])
            ] += 1
        if len(document_exists_examples) < examples_wanted:
            document_exists_examples.append(
                {
                    "ticker": companies[cid]["ticker"],
                    "name": companies[cid]["name"],
                    "documents": [
                        {
                            "parse_status": r["parse_status"],
                            "doc_quality_status": r["doc_quality_status"],
                            "rag_action": r["rag_action"],
                        }
                        for r in docs_by_company[cid]
                    ],
                }
            )

    result.stats = {
        "companies_total": len(companies),
        "companies_with_chunks": len(companies) - len(zero),
        "companies_with_zero_chunks": len(zero),
        "cross_tab": {
            "no_report_ever_downloaded": len(no_report_at_all),
            "report_downloaded_but_no_parsed_document": len(report_but_no_document),
            "document_exists_but_zero_chunks_reached_corpus": len(document_exists),
        },
        "document_exists_pipeline_stage_breakdown": {
            f"parse_status={k[0]}, doc_quality_status={k[1]}, rag_action={k[2]}": v
            for k, v in pipeline_stage_breakdown.items()
        },
        "data_availability_note": (
            "esg/scripts/rebuild_sustainability_tracker.py (commit 17180fff2) deliberately stopped "
            "carrying 'not_found' rows forward -- 'the rebuilt tracker describes files that exist, "
            "not_found rows ... are NOT carried over' -- so no per-company not-found reason-code "
            "text survives anywhere in this repo any more. The closest available signal is used "
            "instead: whether a report was ever downloaded (sustainability_reports) and, when a "
            "parsed document does exist, where in the parse -> section -> chunk pipeline it stopped "
            "contributing chunks."
        ),
    }
    result.examples = {
        "no_report_ever_downloaded_sample": [
            {"ticker": companies[cid]["ticker"], "name": companies[cid]["name"]}
            for cid in no_report_at_all[:examples_wanted]
        ],
        "report_but_no_parsed_document_sample": [
            {"ticker": companies[cid]["ticker"], "name": companies[cid]["name"]}
            for cid in report_but_no_document[:examples_wanted]
        ],
        "document_exists_but_zero_chunks_sample": document_exists_examples,
    }

    n_pipeline_loss = len(report_but_no_document) + len(document_exists)
    if n_pipeline_loss:
        result.warn(
            f"{n_pipeline_loss} of {len(zero)} zero-chunk companies have a downloaded report or a "
            "parsed document that never produced a chunk -- consistent with the pipeline losing "
            "content rather than none existing"
        )
    else:
        result.ok(
            f"all {len(zero)} zero-chunk companies have no downloaded report at all -- consistent "
            "with 'genuinely no report published', not a pipeline defect"
        )
    return result


# ---------------------------------------------------------------------------
# check 28 -- year distribution and per-company-year coverage
# ---------------------------------------------------------------------------


def check_28(con, docs, companies, examples_wanted):
    result = CheckResult("28", "Year distribution of chunks and per-company-year coverage")

    chunk_counts_by_doc = Counter(row[0] for row in con.execute("SELECT doc_id FROM chunks"))
    company_by_doc = {
        row["doc_id"]: row["company_id"]
        for row in con.execute("SELECT DISTINCT doc_id, company_id FROM chunks")
    }

    chunks_by_year: Counter = Counter()
    companies_by_year: dict[int, set] = defaultdict(set)
    years_by_company: dict[int, set] = defaultdict(set)
    unresolved_docs = 0

    for doc_id, n_chunks in chunk_counts_by_doc.items():
        doc = docs.get(doc_id)
        if doc is None:
            continue
        year = report_year(doc["stem"])
        if year is None:
            unresolved_docs += 1
            continue
        chunks_by_year[year] += n_chunks
        company_id = company_by_doc.get(doc_id)
        if company_id is not None:
            companies_by_year[year].add(company_id)
            years_by_company[company_id].add(year)

    if not chunks_by_year:
        result.status = "SKIP"
        result.headline = "no document resolved a report year"
        return result

    years_sorted = sorted(chunks_by_year)

    # Within-company coverage: only companies with >=2 distinct years say
    # anything about a GAP (a single-year company has, by definition, a fully
    # covered span of length 1). rate = years actually present / the span
    # between that company's own first and last year -- deliberately not the
    # corpus-wide year range, so a company that simply started reporting late
    # is not penalised for years before it existed.
    coverage_rates = []
    gap_examples = []
    for company_id, years in years_by_company.items():
        span = max(years) - min(years) + 1
        if span < 2:
            continue
        rate = len(years) / span
        coverage_rates.append(rate)
        if rate < LOW_YEAR_COVERAGE_FLOOR:
            gap_examples.append(
                {
                    "ticker": companies.get(company_id, {}).get("ticker"),
                    "years_present": sorted(years),
                    "span": [min(years), max(years)],
                    "coverage_rate": round(rate, 3),
                }
            )
    gap_examples.sort(key=lambda e: e["coverage_rate"])

    below_floor = sum(1 for r in coverage_rates if r < LOW_YEAR_COVERAGE_FLOOR)

    result.stats = {
        "documents_with_unresolved_year": unresolved_docs,
        "corpus_year_range": [years_sorted[0], years_sorted[-1]],
        "distinct_years": len(years_sorted),
        "chunks_per_year": describe([float(chunks_by_year[y]) for y in years_sorted]),
        "chunks_per_year_table": {str(y): chunks_by_year[y] for y in years_sorted},
        "companies_represented_per_year": {str(y): len(companies_by_year[y]) for y in years_sorted},
        "companies_with_multi_year_data": len(coverage_rates),
        "within_company_year_coverage_rate": (
            describe(coverage_rates) if coverage_rates else {"n": 0}
        ),
        "coverage_floor": LOW_YEAR_COVERAGE_FLOOR,
        "companies_below_coverage_floor": below_floor,
    }
    result.examples = gap_examples[:examples_wanted]

    if coverage_rates and below_floor > LOW_YEAR_COVERAGE_SHARE_WARN * len(coverage_rates):
        result.warn(
            f"{below_floor}/{len(coverage_rates)} multi-year companies cover under "
            f"{LOW_YEAR_COVERAGE_FLOOR:.0%} of their own reporting span -- time-sensitive questions "
            "will have real gaps for these companies"
        )
    elif coverage_rates:
        result.ok(
            f"corpus spans {years_sorted[0]}-{years_sorted[-1]}; median within-company coverage "
            f"{percentile(coverage_rates, 0.5):.2f} across {len(coverage_rates)} multi-year companies"
        )
    else:
        result.ok(
            f"corpus spans {years_sorted[0]}-{years_sorted[-1]}; no company has multi-year data to "
            "assess internal gaps"
        )
    return result


# ---------------------------------------------------------------------------
# check 29 -- layout auto_hold gate exclusion bias
# ---------------------------------------------------------------------------


def check_29(con, examples_wanted, manifest_path):
    result = CheckResult("29", "Layout auto_hold gate exclusion bias")

    manifest = load_vector_manifest(manifest_path)
    if not manifest:
        result.status = "SKIP"
        result.headline = f"vector manifest not found or empty: {manifest_path}"
        return result

    chunk_rows = {
        row["external_chunk_id"]: row
        for row in con.execute(
            "SELECT external_chunk_id, token_count, chunk_text, section_code FROM chunks"
        )
    }

    held_tokens, passed_tokens = [], []
    held_digit, passed_digit = [], []
    sec_held: Counter = Counter()
    sec_total: Counter = Counter()
    missing_lookup = not_run = 0

    for row in manifest:
        chunk = chunk_rows.get((row.get("chunk_id") or "").strip())
        if chunk is None:
            missing_lookup += 1
            continue
        status = (row.get("layout_qa_status") or "").strip()
        text = chunk["chunk_text"] or ""
        text_len = len(text)
        digit_ratio = 1.0 - (len(ALPHA_RE.findall(text)) / text_len) if text_len else 0.0
        section = chunk["section_code"] or "unknown"
        token_count = float(chunk["token_count"] or 0)

        if status == LAYOUT_HELD_STATUS:
            held_tokens.append(token_count)
            held_digit.append(digit_ratio)
            sec_held[section] += 1
            sec_total[section] += 1
        elif status in LAYOUT_PASSED_STATUSES:
            passed_tokens.append(token_count)
            passed_digit.append(digit_ratio)
            sec_total[section] += 1
        else:
            not_run += 1

    if len(held_tokens) < 10 or len(passed_tokens) < 10:
        result.status = "SKIP"
        result.headline = "not enough held/passed chunks to compare"
        return result

    global_held_rate = len(held_tokens) / (len(held_tokens) + len(passed_tokens))

    token_test = sp_stats.mannwhitneyu(held_tokens, passed_tokens, alternative="two-sided")
    # One-sided: the QA brief's hypothesis is specifically that holds skew
    # toward MORE numeric/table content, not merely "different".
    digit_test = sp_stats.mannwhitneyu(held_digit, passed_digit, alternative="greater")

    section_rates = [
        {
            "section_code": code,
            "held_rate": round(sec_held.get(code, 0) / total, 4),
            "total_chunks": total,
        }
        for code, total in sec_total.items()
        if total >= SECTION_CODE_MIN_VOLUME
    ]
    section_rates.sort(key=lambda r: -r["held_rate"])
    biased_sections = [
        r for r in section_rates if r["held_rate"] > global_held_rate + HELD_RATE_MARGIN
    ]

    result.stats = {
        "chunks_compared": len(held_tokens) + len(passed_tokens),
        "chunks_missing_from_chunks_table": missing_lookup,
        "chunks_layout_not_run": not_run,
        "held_chunks": len(held_tokens),
        "passed_chunks": len(passed_tokens),
        "global_held_rate": round(global_held_rate, 4),
        "token_count_held": describe(held_tokens),
        "token_count_passed": describe(passed_tokens),
        "token_count_mann_whitney_u_two_sided": {
            "statistic": round(float(token_test.statistic), 2),
            "p_value": round(float(token_test.pvalue), 6),
        },
        "non_alpha_ratio_held": describe(held_digit),
        "non_alpha_ratio_passed": describe(passed_digit),
        "non_alpha_ratio_mann_whitney_u_held_greater": {
            "statistic": round(float(digit_test.statistic), 2),
            "p_value": round(float(digit_test.pvalue), 6),
        },
        "section_code_held_rate_floor_chunks": SECTION_CODE_MIN_VOLUME,
        "held_rate_margin": HELD_RATE_MARGIN,
        "section_codes_above_global_rate_by_margin": len(biased_sections),
    }
    result.examples = {
        "section_code_held_rates": section_rates[:examples_wanted],
        "most_biased_sections": biased_sections[:examples_wanted],
    }

    if digit_test.pvalue < 0.01 and biased_sections:
        codes = ", ".join(r["section_code"] for r in biased_sections[:5])
        result.warn(
            f"held chunks run more non-alphabetic than passed chunks "
            f"(median {percentile(held_digit, 0.5):.3f} vs {percentile(passed_digit, 0.5):.3f}, "
            f"Mann-Whitney p={digit_test.pvalue:.4g}), and {len(biased_sections)} section codes hold "
            f"at more than global+{HELD_RATE_MARGIN:.0%} ({codes}) -- the layout gate concentrates on "
            "numeric/table-heavy sections, exactly the quantitative disclosures ESG questions ask about"
        )
    else:
        result.ok(
            f"global held rate {global_held_rate:.1%}; no strong numeric-content bias detected "
            f"(non-alpha Mann-Whitney p={digit_test.pvalue:.4g})"
        )
    return result


# ---------------------------------------------------------------------------
# check 30 -- is the RAG-ineligible remainder random or structured
# ---------------------------------------------------------------------------


def _concentration(total_counts: Counter, inelig_counts: Counter) -> tuple[dict, list]:
    """Chi-square-flavoured concentration score plus a ranked unit list.

    Null hypothesis: ineligible chunks are scattered uniformly at random
    across the corpus, so each unit's expected ineligible count is
    proportional to its share of ALL chunks. Many units have small expected
    counts under this null, so the statistic is reported as an indicative
    concentration score rather than a formally calibrated chi-square p-value
    -- see the caller's note in check_30's stats.
    """
    units = list(total_counts)
    observed = np.array([inelig_counts.get(u, 0) for u in units], dtype=float)
    n_ineligible = observed.sum()
    expected = np.array([total_counts[u] for u in units], dtype=float)
    expected = expected / expected.sum() * n_ineligible
    mask = expected > 0
    chi2 = float(((observed[mask] - expected[mask]) ** 2 / expected[mask]).sum())

    ranked = sorted(zip(units, observed.tolist()), key=lambda p: -p[1])
    top1 = ranked[0][1] / n_ineligible if ranked and n_ineligible else 0.0
    top5 = sum(c for _, c in ranked[:5]) / n_ineligible if n_ineligible else 0.0
    stats = {
        "units_with_any_chunk": len(units),
        "units_touched_by_ineligibility": int((observed > 0).sum()),
        "chi_square_vs_proportional_null": round(chi2, 2),
        "degrees_of_freedom": int(mask.sum()) - 1,
        "top_1_share_of_ineligible": round(top1, 4),
        "top_5_share_of_ineligible": round(top5, 4),
    }
    return stats, ranked


def check_30(con, docs, companies, examples_wanted):
    result = CheckResult("30", "Is the RAG-ineligible remainder random or structured")

    rows = con.execute("SELECT doc_id, company_id, rag_action FROM chunks").fetchall()
    total_chunks = len(rows)
    ineligible = [row for row in rows if (row["rag_action"] or "").strip() != RAG_ELIGIBLE_ACTION]
    n_ineligible = len(ineligible)

    if n_ineligible == 0:
        result.status = "SKIP"
        result.headline = "no chunks fail the RAG gate"
        return result

    rag_action_breakdown = Counter((row["rag_action"] or "blank") for row in ineligible)
    total_by_doc = Counter(row["doc_id"] for row in rows)
    inelig_by_doc = Counter(row["doc_id"] for row in ineligible)
    total_by_company = Counter(row["company_id"] for row in rows)
    inelig_by_company = Counter(row["company_id"] for row in ineligible)

    doc_stats, doc_ranked = _concentration(total_by_doc, inelig_by_doc)
    company_stats, company_ranked = _concentration(total_by_company, inelig_by_company)

    doc_examples = [
        {
            "ticker": docs.get(doc_id, {}).get("ticker"),
            "stem": docs.get(doc_id, {}).get("stem"),
            "ineligible_chunks": int(count),
            "total_chunks_in_doc": total_by_doc[doc_id],
            "share_of_all_ineligible": round(count / n_ineligible, 4),
        }
        for doc_id, count in doc_ranked[:examples_wanted]
        if count > 0
    ]
    company_examples = [
        {
            "ticker": companies.get(cid, {}).get("ticker"),
            "ineligible_chunks": int(count),
            "share_of_all_ineligible": round(count / n_ineligible, 4),
        }
        for cid, count in company_ranked[:examples_wanted]
        if count > 0
    ]

    result.stats = {
        "chunks_total": total_chunks,
        "chunks_failing_rag_gate": n_ineligible,
        "ineligible_rate": round(n_ineligible / total_chunks, 6),
        "rag_action_breakdown": dict(rag_action_breakdown),
        "note_on_chi_square": (
            "many documents/companies have small expected counts under the proportional-scatter "
            "null, so the chi-square figures are an indicative concentration score, not a formally "
            "calibrated p-value -- the top-1/top-5 share figures are the more directly interpretable "
            "evidence for clustering"
        ),
        "by_document": doc_stats,
        "by_company": company_stats,
    }
    result.examples = {"top_documents": doc_examples, "top_companies": company_examples}

    touched_doc_share = (
        doc_stats["units_touched_by_ineligibility"] / doc_stats["units_with_any_chunk"]
        if doc_stats["units_with_any_chunk"] else 1.0
    )
    if doc_stats["top_1_share_of_ineligible"] > 0.20 or touched_doc_share < 0.10:
        top_doc = doc_examples[0] if doc_examples else {}
        result.warn(
            f"{n_ineligible} RAG-ineligible chunks cluster in just "
            f"{doc_stats['units_touched_by_ineligibility']}/{doc_stats['units_with_any_chunk']} "
            f"documents ({touched_doc_share:.1%}); the single largest, "
            f"{top_doc.get('ticker')} {top_doc.get('stem')}, accounts for "
            f"{doc_stats['top_1_share_of_ineligible']:.1%} of all ineligible chunks -- "
            "structured, not scattered noise"
        )
    else:
        result.ok(
            f"{n_ineligible} ineligible chunks spread across "
            f"{doc_stats['units_touched_by_ineligibility']}/{doc_stats['units_with_any_chunk']} "
            f"documents ({touched_doc_share:.1%}) without strong concentration"
        )
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 5 -- coverage, bias, and the retrieval gate")
    print("=" * 78)
    for result in results:
        print(f"\n[{SYMBOL[result.status]}] Check {result.key}: {result.title}")
        if result.headline:
            print(f"       {result.headline}")
        for key, value in result.stats.items():
            if isinstance(value, dict):
                print(f"       {key}:")
                for sub_key, sub_value in value.items():
                    print(f"         {sub_key}: {sub_value}")
            else:
                print(f"       {key}: {value}")
        if result.examples:
            print("       examples:")
            payload = json.dumps(result.examples, indent=2, default=str)
            for line in payload.splitlines():
                print(f"         {line}")

    print("\n" + "=" * 78)
    tally = Counter(r.status for r in results)
    print("  ".join(f"{status}={tally.get(status, 0)}"
                    for status in ("PASS", "WARN", "FAIL", "SKIP")))
    # Distributional tier: WARN is the strongest signal a check raises on its
    # own, so there is no pass/fail gate here the way Tier 1 has one.
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--manifest", type=Path, default=config.VECTOR_INDEX_MANIFEST_CSV)
    parser.add_argument("--checks", default="all",
                        help=f"comma-separated subset of {','.join(ALL_CHECKS)} (default: all)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max example rows to show per check (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON")
    args = parser.parse_args()

    selected = ALL_CHECKS if args.checks == "all" else [
        c.strip() for c in args.checks.split(",") if c.strip()
    ]
    unknown = [c for c in selected if c not in ALL_CHECKS]
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)}")

    con = connect(args.db)
    try:
        docs = load_documents(con)
        companies = load_companies(con)
        results: list[CheckResult] = []

        if "26" in selected:
            results.append(check_26(con, companies, args.examples))
        if "27" in selected:
            results.append(check_27(con, companies, args.examples))
        if "28" in selected:
            results.append(check_28(con, docs, companies, args.examples))
        if "29" in selected:
            results.append(check_29(con, args.examples, args.manifest))
        if "30" in selected:
            results.append(check_30(con, docs, companies, args.examples))
    finally:
        con.close()

    render(results)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                [
                    {
                        "check": r.key, "title": r.title, "status": r.status,
                        "headline": r.headline, "stats": r.stats, "examples": r.examples,
                    }
                    for r in results
                ],
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
