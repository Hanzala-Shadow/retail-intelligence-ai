"""Tier 2 mathematical QA: sectioning quality of the ESG corpus.

Distributional, not invariant. Nothing here has a provably correct answer the
way Tier 1 does -- a check can only flag a distribution as implausible, not
prove it wrong. Run qa_tier1_invariants.py first: these numbers are only
trustworthy once sections are known to tile their documents.

The pipeline lever behind every check here is section_splitter_esg.py.

Everything is READ-ONLY. The database is opened with mode=ro.

Checks
    8   full_document fallback rate, by ticker / year / parser_used
    9   section_code concentration (Shannon entropy, HHI) vs the canonical
        vocabulary, per document and corpus-wide
    10  is section.char_count multi-modal (KDE + Hartigan's dip test)
    11  sections per document vs page count (regression + residuals)
    12  are repeated section instances plausible (max instance ordinal)
    13  do section codes appear in a consistent order across documents
        (pairwise order-agreement rate)

Usage
    python esg/scripts/esg_database_tiers/qa_tier2_sectioning.py
    python esg/scripts/esg_database_tiers/qa_tier2_sectioning.py --checks 8,9
    python esg/scripts/esg_database_tiers/qa_tier2_sectioning.py --json-out reports/qa_tier2.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
from qa_tier1_invariants import (  # noqa: E402
    CheckResult,
    connect,
    describe,
    load_documents,
    page_map_path,
    percentile,
    read_page_count,
)

try:  # single source of truth for the vocabulary sectioning is allowed to emit
    from section_splitter_esg import CANONICAL_SECTION_CODES  # noqa: E402
except Exception:  # pragma: no cover - keeps QA runnable if the module moves
    CANONICAL_SECTION_CODES = {
        "ceo_letter", "about_this_report", "environmental", "climate", "energy",
        "emissions", "waste", "water", "social", "human_capital",
        "diversity_equity_inclusion", "supply_chain_ethics", "community",
        "governance", "ethics_compliance", "data_summary", "appendix", "other",
        "full_document",
    }

try:
    import diptest
except ImportError:  # pragma: no cover
    diptest = None

ALL_CHECKS = ["8", "9", "10", "11", "12", "13"]

# Below this a section is thin enough that the splitter likely found a heading
# immediately followed by another; above it, the splitter likely missed the
# next heading and swallowed unrelated content. Thresholds are the ones named
# in the QA document, not derived from this corpus.
SHORT_SECTION_CHARS = 500
LONG_SECTION_CHARS = 50_000

# section_instance_id is "{code}__{ordinal:04d}" (section_splitter_esg.py); an
# ordinal at or beyond this suggests one heading is being detected repeatedly
# and fragmenting a single real section into many instances.
HIGH_ORDINAL_FLOOR = 10

# A code pair needs to co-occur at least this many times before its order
# agreement rate is treated as informative rather than small-sample noise.
MIN_COOCCURRENCE = 5


# ---------------------------------------------------------------------------
# small helpers specific to this tier
# ---------------------------------------------------------------------------


def shannon_entropy_bits(counts: list[int]) -> float:
    total = sum(counts)
    if total == 0:
        return 0.0
    return -sum(
        (c / total) * math.log2(c / total) for c in counts if c > 0
    )


def herfindahl_index(counts: list[int]) -> float:
    total = sum(counts)
    if total == 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)


def gaussian_kde_grid(values: np.ndarray, n_grid: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """Silverman-bandwidth Gaussian KDE, evaluated on a grid.

    No scipy in this environment; the estimator itself is ~10 lines of numpy
    and needs no dependency beyond it.
    """
    n = len(values)
    std = float(np.std(values, ddof=1))
    iqr = float(np.subtract(*np.percentile(values, [75, 25])))
    scale = min(std, iqr / 1.349) if iqr > 0 else std
    bandwidth = 0.9 * scale * n ** (-1 / 5) if scale > 0 else 1.0
    bandwidth = bandwidth or 1.0

    grid = np.linspace(values.min(), values.max(), n_grid)
    diffs = (grid[:, None] - values[None, :]) / bandwidth
    density = np.exp(-0.5 * diffs ** 2).sum(axis=1) / (n * bandwidth * math.sqrt(2 * math.pi))
    return grid, density


def count_kde_modes(grid: np.ndarray, density: np.ndarray) -> int:
    """Count local maxima in the KDE, ignoring shoulders from flat regions."""
    modes = 0
    for i in range(1, len(density) - 1):
        if density[i] > density[i - 1] and density[i] > density[i + 1]:
            modes += 1
    return modes


def skew_kurtosis(values: np.ndarray) -> tuple[float, float]:
    n = len(values)
    mean = values.mean()
    sd = values.std(ddof=0)
    if sd == 0 or n < 3:
        return 0.0, 0.0
    m3 = np.mean((values - mean) ** 3)
    m4 = np.mean((values - mean) ** 4)
    skew = m3 / sd ** 3
    kurtosis_excess = m4 / sd ** 4 - 3.0
    return float(skew), float(kurtosis_excess)


def ols_slope_intercept(xs: list[float], ys: list[float]) -> tuple[float, float]:
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    x_mean, y_mean = x.mean(), y.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return 0.0, float(y_mean)
    slope = ((x - x_mean) * (y - y_mean)).sum() / denom
    intercept = y_mean - slope * x_mean
    return float(slope), float(intercept)


def r_squared(ys: list[float], preds: list[float]) -> float:
    y = np.asarray(ys, dtype=float)
    p = np.asarray(preds, dtype=float)
    ss_res = ((y - p) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    return float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0


# ---------------------------------------------------------------------------
# provenance lookups: report_year and parser_used hang off documents via the
# provenance ids, not off documents/sections directly.
# ---------------------------------------------------------------------------


def load_document_provenance(con: sqlite3.Connection) -> dict[int, dict]:
    rows = con.execute(
        """
        SELECT d.doc_id, l.report_year, e.parser_or_model
        FROM documents d
        LEFT JOIN logical_sources l ON l.logical_source_id = d.logical_source_id
        LEFT JOIN extraction_artifacts e ON e.extraction_artifact_id = d.extraction_artifact_id
        """
    ).fetchall()
    return {r["doc_id"]: {"year": r["report_year"], "parser": r["parser_or_model"]} for r in rows}


def load_sections_by_doc(con: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    by_doc: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in con.execute(
        """
        SELECT doc_id, section_id, section_instance_id, section_code,
               char_count, source_start_char
        FROM sections
        """
    ):
        by_doc[row["doc_id"]].append(row)
    return by_doc


# ---------------------------------------------------------------------------
# check 8 -- full_document fallback rate
# ---------------------------------------------------------------------------


def check_8(con, docs, provenance, examples_wanted):
    result = CheckResult("8", "full_document fallback rate")
    by_doc = load_sections_by_doc(con)

    def breakdown(key_fn) -> dict:
        counts: dict = defaultdict(lambda: [0, 0])  # key -> [fallback, total]
        for doc_id, sections in by_doc.items():
            key = key_fn(doc_id)
            counts[key][1] += 1
            if len(sections) == 1:
                counts[key][0] += 1
        return {
            str(key): {
                "fallback": fb, "total": total,
                "rate": round(fb / total, 4) if total else None,
            }
            for key, (fb, total) in sorted(counts.items(), key=lambda kv: -kv[1][1])
        }

    total_docs = len(by_doc)
    fallback_docs = [doc_id for doc_id, sections in by_doc.items() if len(sections) == 1]
    n_fallback = len(fallback_docs)

    # Sanity-check the stated hypothesis: a single-section doc should carry the
    # section_code "full_document", not merely have a count of one.
    fallback_codes = Counter(
        by_doc[doc_id][0]["section_code"] for doc_id in fallback_docs
    )
    mislabeled = [
        doc_id for doc_id in fallback_docs
        if by_doc[doc_id][0]["section_code"] != "full_document"
    ]

    result.stats = {
        "documents_with_sections": total_docs,
        "documents_with_exactly_one_section": n_fallback,
        "fallback_rate": round(n_fallback / total_docs, 4) if total_docs else None,
        "single_section_code_counts": dict(fallback_codes),
        "single_section_not_coded_full_document": len(mislabeled),
        "by_ticker": breakdown(lambda d: docs.get(d, {}).get("ticker")),
        "by_year": breakdown(lambda d: provenance.get(d, {}).get("year")),
        "by_parser_used": breakdown(lambda d: provenance.get(d, {}).get("parser")),
    }
    result.examples = [
        {
            "ticker": docs.get(d, {}).get("ticker"),
            "stem": docs.get(d, {}).get("stem"),
            "section_code": by_doc[d][0]["section_code"],
        }
        for d in mislabeled[:examples_wanted]
    ]
    result.ok(
        f"{n_fallback}/{total_docs} documents ({n_fallback / total_docs:.1%}) "
        f"fell back to a single section"
        if total_docs else "no documents with sections"
    )
    if mislabeled:
        result.warn(
            f"{len(mislabeled)} single-section documents are not coded full_document "
            "-- the fallback hypothesis does not fully explain the single-section rate"
        )
    return result


# ---------------------------------------------------------------------------
# check 9 -- section_code concentration
# ---------------------------------------------------------------------------


def check_9(con, docs, examples_wanted):
    result = CheckResult("9", "section_code concentration (entropy / HHI)")
    by_doc = load_sections_by_doc(con)
    vocab_size = len(CANONICAL_SECTION_CODES)
    max_entropy = math.log2(vocab_size) if vocab_size > 1 else 1.0

    per_doc_entropy, per_doc_hhi = [], []
    low_entropy_docs = []
    corpus_counts: Counter = Counter()

    for doc_id, sections in by_doc.items():
        counts = Counter(s["section_code"] for s in sections)
        corpus_counts.update(counts)
        if len(sections) < 2:
            continue  # a single-section doc has zero entropy by construction
        values = list(counts.values())
        entropy = shannon_entropy_bits(values)
        hhi = herfindahl_index(values)
        per_doc_entropy.append(entropy)
        per_doc_hhi.append(hhi)
        normalized = entropy / max_entropy if max_entropy else 0.0
        if normalized < 0.25:
            doc = docs.get(doc_id, {})
            low_entropy_docs.append(
                {
                    "ticker": doc.get("ticker"),
                    "stem": doc.get("stem"),
                    "n_sections": len(sections),
                    "distinct_codes": len(counts),
                    "dominant_code": counts.most_common(1)[0][0],
                    "dominant_share": round(counts.most_common(1)[0][1] / sum(values), 4),
                    "entropy_bits": round(entropy, 4),
                }
            )

    corpus_values = list(corpus_counts.values())
    corpus_entropy = shannon_entropy_bits(corpus_values)
    corpus_hhi = herfindahl_index(corpus_values)
    unused_codes = sorted(CANONICAL_SECTION_CODES - set(corpus_counts))

    result.stats = {
        "canonical_vocabulary_size": vocab_size,
        "codes_never_observed": unused_codes,
        "codes_observed": len(corpus_counts),
        "corpus_wide": {
            "entropy_bits": round(corpus_entropy, 4),
            "max_possible_entropy_bits": round(max_entropy, 4),
            "normalized_entropy": round(corpus_entropy / max_entropy, 4) if max_entropy else None,
            "herfindahl_index": round(corpus_hhi, 6),
            "code_shares": {
                code: round(n / sum(corpus_values), 4)
                for code, n in corpus_counts.most_common()
            },
        },
        "per_document_multi_section": {
            "n_documents": len(per_doc_entropy),
            "entropy_bits": describe(per_doc_entropy),
            "herfindahl_index": describe(per_doc_hhi),
            "documents_below_25pct_normalized_entropy": len(low_entropy_docs),
        },
    }
    result.examples = low_entropy_docs[:examples_wanted]

    if not corpus_counts:
        result.status = "SKIP"
        result.headline = "no sections found"
    elif len(low_entropy_docs) > 0.25 * max(len(per_doc_entropy), 1):
        result.warn(
            f"{len(low_entropy_docs)}/{len(per_doc_entropy)} multi-section documents "
            "are dominated by one code (normalized entropy < 0.25)"
        )
    else:
        result.ok(
            f"corpus normalized entropy {corpus_entropy / max_entropy:.3f}; "
            f"{len(unused_codes)} canonical codes never observed"
        )
    return result


# ---------------------------------------------------------------------------
# check 10 -- is char_count multi-modal
# ---------------------------------------------------------------------------


def check_10(con, examples_wanted):
    result = CheckResult("10", "section.char_count multi-modality")

    rows = con.execute(
        "SELECT char_count FROM sections WHERE section_code != 'full_document' "
        "AND char_count IS NOT NULL AND char_count > 0"
    ).fetchall()
    values = np.array([r[0] for r in rows], dtype=float)

    if len(values) < 10:
        result.status = "SKIP"
        result.headline = "too few non-fallback sections to test"
        return result

    grid, density = gaussian_kde_grid(values)
    n_modes = count_kde_modes(grid, density)
    skew, kurtosis_excess = skew_kurtosis(values)

    dip_stat = dip_p = None
    if diptest is not None:
        sample = values if len(values) <= 20_000 else np.random.default_rng(0).choice(
            values, 20_000, replace=False
        )
        dip_stat, dip_p = diptest.diptest(np.sort(sample))

    short_mask = values < SHORT_SECTION_CHARS
    long_mask = values > LONG_SECTION_CHARS

    result.stats = {
        "sections_measured": len(values),
        "excludes": "section_code = 'full_document' (whole-document fallback, not a real section)",
        "char_count": describe(list(values)),
        "skewness": round(skew, 4),
        "excess_kurtosis": round(kurtosis_excess, 4),
        "kde_local_maxima": n_modes,
        "kde_bandwidth_rule": "Silverman",
        "hartigan_dip_statistic": round(dip_stat, 6) if dip_stat is not None else None,
        "hartigan_dip_p_value": round(dip_p, 6) if dip_p is not None else None,
        "dip_test_note": (
            "diptest package unavailable; only the KDE mode count is reported"
            if diptest is None else "p < 0.05 rejects unimodality"
        ),
        "short_tail": {
            "threshold_chars": SHORT_SECTION_CHARS,
            "count": int(short_mask.sum()),
            "rate": round(float(short_mask.mean()), 4),
        },
        "long_tail": {
            "threshold_chars": LONG_SECTION_CHARS,
            "count": int(long_mask.sum()),
            "rate": round(float(long_mask.mean()), 4),
        },
    }
    result.examples = {
        "shortest": sorted(values.tolist())[:examples_wanted],
        "longest": sorted(values.tolist())[-examples_wanted:],
    }

    # The dip test is the gate; KDE mode count is descriptive only. With extreme
    # right skew (this corpus: skew ~40) a fixed-bandwidth KDE grows spurious
    # bumps in the sparse tail even when the data is genuinely unimodal, so
    # `kde_local_maxima > 1` alone is not trustworthy evidence of multimodality.
    if dip_p is not None:
        is_multimodal = dip_p < 0.05
        headline = f"dip p={dip_p:.4f} ({n_modes} KDE maxima, tail-sensitive, for context)"
    else:
        is_multimodal = n_modes > 1
        headline = f"{n_modes} KDE maxima (diptest unavailable; less reliable than the dip test)"

    if is_multimodal:
        result.warn(f"distribution looks multi-modal: {headline}")
    else:
        result.ok(f"looks unimodal: {headline}")
    return result


# ---------------------------------------------------------------------------
# check 11 -- sections per document vs page count
# ---------------------------------------------------------------------------


def check_11(con, docs, text_root, examples_wanted):
    result = CheckResult("11", "Sections per document vs page count")

    by_doc = load_sections_by_doc(con)
    pairs, doc_ids = [], []
    missing_pages = 0
    for doc_id, sections in by_doc.items():
        doc = docs.get(doc_id)
        if doc is None:
            continue
        pages = read_page_count(page_map_path(text_root, doc))
        if pages is None or pages <= 0:
            missing_pages += 1
            continue
        pairs.append((float(pages), float(len(sections))))
        doc_ids.append(doc_id)

    if len(pairs) < 5:
        result.status = "SKIP"
        result.headline = "too few documents with a page map to regress"
        return result

    pages_list = [p for p, _ in pairs]
    sections_list = [s for _, s in pairs]
    slope, intercept = ols_slope_intercept(pages_list, sections_list)
    preds = [slope * p + intercept for p in pages_list]
    r2 = r_squared(sections_list, preds)

    residuals = [s - p for s, p in zip(sections_list, preds)]
    resid_arr = np.asarray(residuals)
    resid_sd = float(resid_arr.std(ddof=1)) if len(residuals) > 1 else 0.0

    sections_per_page = [s / p for p, s in pairs if p > 0]

    outliers = []
    if resid_sd > 0:
        for doc_id, pages, sections, resid in zip(doc_ids, pages_list, sections_list, residuals):
            z = resid / resid_sd
            if abs(z) > 3:
                doc = docs.get(doc_id, {})
                outliers.append(
                    {
                        "ticker": doc.get("ticker"),
                        "stem": doc.get("stem"),
                        "pages": int(pages),
                        "sections": int(sections),
                        "predicted_sections": round(slope * pages + intercept, 2),
                        "residual_z": round(z, 2),
                    }
                )
    outliers.sort(key=lambda e: -abs(e["residual_z"]))

    result.stats = {
        "documents_regressed": len(pairs),
        "documents_missing_page_map": missing_pages,
        "sections_per_page": describe(sections_per_page),
        "ols_sections_on_pages": {
            "slope": round(slope, 6),
            "intercept": round(intercept, 4),
            "r_squared": round(r2, 4),
        },
        "residual_sd": round(resid_sd, 4),
        "documents_beyond_3sd_residual": len(outliers),
    }
    result.examples = outliers[:examples_wanted]
    if len(outliers) > 0.05 * len(pairs):
        result.warn(
            f"{len(outliers)}/{len(pairs)} documents ({len(outliers) / len(pairs):.1%}) "
            "have a sections-vs-pages residual beyond 3 SD"
        )
    else:
        result.ok(
            f"R^2={r2:.3f}; median {percentile(sections_per_page, 0.5):.3f} sections/page; "
            f"{len(outliers)} outliers beyond 3 SD"
        )
    return result


# ---------------------------------------------------------------------------
# check 12 -- repeated section instances
# ---------------------------------------------------------------------------


def check_12(con, docs, examples_wanted):
    result = CheckResult("12", "Repeated section instances (max ordinal per doc/code)")

    by_doc_code: dict[tuple[int, str], int] = defaultdict(int)
    for row in con.execute("SELECT doc_id, section_code, section_instance_id FROM sections"):
        instance_id = row["section_instance_id"] or ""
        ordinal = instance_id.rsplit("__", 1)[-1]
        if not ordinal.isdigit():
            continue
        key = (row["doc_id"], row["section_code"])
        by_doc_code[key] = max(by_doc_code[key], int(ordinal))

    max_ordinals = list(by_doc_code.values())
    high = [
        (doc_id, code, ordinal)
        for (doc_id, code), ordinal in by_doc_code.items()
        if ordinal >= HIGH_ORDINAL_FLOOR
    ]
    high.sort(key=lambda t: -t[2])

    result.stats = {
        "doc_code_pairs": len(by_doc_code),
        "max_instance_ordinal": describe([float(v) for v in max_ordinals]),
        "high_ordinal_floor": HIGH_ORDINAL_FLOOR,
        "doc_code_pairs_at_or_above_floor": len(high),
        "ordinal_frequency": dict(sorted(Counter(max_ordinals).items())),
    }
    result.examples = [
        {
            "ticker": docs.get(doc_id, {}).get("ticker"),
            "stem": docs.get(doc_id, {}).get("stem"),
            "section_code": code,
            "max_instance_ordinal": ordinal,
        }
        for doc_id, code, ordinal in high[:examples_wanted]
    ]
    if high:
        result.warn(
            f"{len(high)} (doc, section_code) pairs reach instance ordinal "
            f">= {HIGH_ORDINAL_FLOOR} -- likely fragmentation of one real section"
        )
    else:
        result.ok(f"no (doc, section_code) pair reaches ordinal {HIGH_ORDINAL_FLOOR}")
    return result


# ---------------------------------------------------------------------------
# check 13 -- section code order consistency across documents
# ---------------------------------------------------------------------------


def check_13(con, examples_wanted):
    result = CheckResult("13", "Section code order consistency across documents")

    by_doc = load_sections_by_doc(con)

    # First-occurrence position of each code within a document, ordered by
    # source_start_char. Repeated instances of the same code only count once,
    # at their first appearance -- order consistency is about macro layout.
    doc_sequences: list[list[str]] = []
    for sections in by_doc.values():
        ordered = sorted(
            (s for s in sections if s["source_start_char"] is not None),
            key=lambda s: s["source_start_char"],
        )
        seen, sequence = set(), []
        for s in ordered:
            if s["section_code"] not in seen and s["section_code"] != "full_document":
                seen.add(s["section_code"])
                sequence.append(s["section_code"])
        if len(sequence) >= 2:
            doc_sequences.append(sequence)

    # For every pair of codes that co-occur in a document, tally which of the
    # two came first. A pair with a stable order across the corpus is
    # templated; a pair with no majority direction is unreliable labeling.
    pair_before: Counter = Counter()
    pair_total: Counter = Counter()
    for sequence in doc_sequences:
        position = {code: i for i, code in enumerate(sequence)}
        for a, b in combinations(sorted(position), 2):
            pair_total[(a, b)] += 1
            if position[a] < position[b]:
                pair_before[(a, b)] += 1

    agreement_rates = []
    low_agreement_pairs = []
    for pair, total in pair_total.items():
        if total < MIN_COOCCURRENCE:
            continue
        before = pair_before[pair]
        rate = max(before, total - before) / total
        agreement_rates.append(rate)
        if rate < 0.75:
            low_agreement_pairs.append(
                {
                    "codes": list(pair),
                    "co_occurrences": total,
                    "a_before_b": before,
                    "agreement_rate": round(rate, 4),
                }
            )
    low_agreement_pairs.sort(key=lambda e: e["agreement_rate"])

    result.stats = {
        "documents_with_orderable_sequence": len(doc_sequences),
        "code_pairs_considered": len(pair_total),
        "code_pairs_above_min_cooccurrence": len(agreement_rates),
        "min_cooccurrence": MIN_COOCCURRENCE,
        "agreement_rate": describe(agreement_rates),
        "code_pairs_below_75pct_agreement": len(low_agreement_pairs),
    }
    result.examples = low_agreement_pairs[:examples_wanted]

    if not agreement_rates:
        result.status = "SKIP"
        result.headline = "no code pair reached the co-occurrence floor"
    elif len(low_agreement_pairs) > 0.25 * len(agreement_rates):
        result.warn(
            f"{len(low_agreement_pairs)}/{len(agreement_rates)} code pairs disagree on "
            "order in more than a quarter of the documents they share"
        )
    else:
        result.ok(
            f"median order-agreement {percentile(agreement_rates, 0.5):.3f} across "
            f"{len(agreement_rates)} code pairs"
        )
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 2 -- sectioning quality")
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
    # Tier 2 is distributional: WARN is the strongest signal a check can raise
    # on its own, so there is no pass/fail gate here the way Tier 1 has one.
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--esg-text-root", type=Path, default=config.ESG_TEXT_DIR)
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
        provenance = load_document_provenance(con)
        results: list[CheckResult] = []

        if "8" in selected:
            results.append(check_8(con, docs, provenance, args.examples))
        if "9" in selected:
            results.append(check_9(con, docs, args.examples))
        if "10" in selected:
            results.append(check_10(con, args.examples))
        if "11" in selected:
            results.append(check_11(con, docs, args.esg_text_root, args.examples))
        if "12" in selected:
            results.append(check_12(con, docs, args.examples))
        if "13" in selected:
            results.append(check_13(con, args.examples))
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
