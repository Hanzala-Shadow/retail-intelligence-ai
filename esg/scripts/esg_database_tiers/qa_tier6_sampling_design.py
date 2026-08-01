"""Tier 6 mathematical QA: sampling design for human review.

Unlike Tiers 1-5, nothing here can fail on its own -- there is no defect a
sample-size formula or a kappa calculation can detect in the corpus itself.
Tier 6 instead answers the two questions a manual-review programme needs
answered *before* anyone opens a spreadsheet: how many chunks must a human
look at to bound the corpus error rate, and how will you know whether two
reviewers actually agree. Skipping this tier is what turns "we reviewed some
chunks and they looked fine" into an unfalsifiable claim.

Everything is READ-ONLY. The database is opened with mode=ro. Check 31 can
optionally draw and write an actual stratified sample (--sample-out) for
reviewers to work from; that write goes under reports/, never under data/.

Checks
    31  required manual-review sample size for a target error-rate bound
        (Cochran's formula with a finite-population correction), plus
        proportional-allocation designs stratified by ticker, section code,
        and chunk type
    32  how annotator agreement will be measured (Cohen's kappa on a shared
        overlap subset), including the recommended overlap size and, if two
        reviewers' labels are supplied, the measured kappa itself

Usage
    python esg/scripts/esg_database_tiers/qa_tier6_sampling_design.py
    python esg/scripts/esg_database_tiers/qa_tier6_sampling_design.py --checks 31
    python esg/scripts/esg_database_tiers/qa_tier6_sampling_design.py --sample-out reports/qa_tier6_manual_review_sample.csv
    python esg/scripts/esg_database_tiers/qa_tier6_sampling_design.py --annotations reports/qa_tier6_pilot_labels.csv
    python esg/scripts/esg_database_tiers/qa_tier6_sampling_design.py --json-out reports/qa_tier6.json
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
from qa_tier1_invariants import (  # noqa: E402
    CheckResult,
    connect,
    describe,
    load_documents,
)
from qa_tier5_coverage_bias import RAG_ELIGIBLE_ACTION, load_companies  # noqa: E402

ALL_CHECKS = ["31", "32"]

# check 31 default design target. The brief's own worked example -- "roughly
# 2,400 for a proportion near 50%" -- is exactly z=1.96, p=0.5, e=0.02 with an
# infinite population, which is what these defaults reproduce; the finite
# population correction then pulls the recommended n down from there.
DEFAULT_CONFIDENCE = 0.95
DEFAULT_MARGIN = 0.02
DEFAULT_ASSUMED_P = 0.5
# Illustrates "far fewer if the true error rate is low" against the same
# confidence/margin target. Not derived from this corpus -- check 18's
# measured 3.5% short_evidence rate is one reason 0.05 and 0.02 are on the
# grid, not a claim that the true defect rate equals either.
ASSUMED_P_GRID = [0.5, 0.3, 0.2, 0.1, 0.05, 0.02]

# A stratum with fewer members than this gets bumped up to it (capped at its
# own population) so that every ticker/section/type with any presence in the
# eligible population gets at least a token chance of being reviewed, mirroring
# qa_tier4.stratified_sample_by_ticker's min-1-per-stratum convention.
DEFAULT_MIN_ALLOC_PER_STRATUM = 1

# check 32: fraction of the total review sample re-shown to a second reviewer
# to measure agreement. The brief names this figure directly ("~20%").
DEFAULT_OVERLAP_FRACTION = 0.20
# Category count assumed when illustrating kappa precision without real
# annotation data yet (equal-prevalence assumption -> pe = 1/k). 2 is the
# simplest useful review scheme: "acceptable for retrieval" vs not.
DEFAULT_LABEL_CATEGORIES = 2
# Landis & Koch (1977) benchmark scale -- not derived from this corpus.
# Ceilings are the upper edge of each band; kappa > 0.80 falls through to
# "almost perfect" (see kappa_interpretation's fallback return).
KAPPA_BANDS = [
    (0.20, "slight"),
    (0.40, "fair"),
    (0.60, "moderate"),
    (0.80, "substantial"),
]
# Below this, a low measured error rate may just mean lenient/inattentive
# reviewing rather than a genuinely clean corpus -- the exact risk check 32
# exists to catch (see the brief's own rationale for question 32).
KAPPA_WARN_FLOOR = 0.60


# ---------------------------------------------------------------------------
# check 31 -- required sample size and stratified allocation
# ---------------------------------------------------------------------------


def cochran_n(z: float, p: float, margin: float) -> float:
    """Sample size for a proportion CI of the given half-width, infinite population."""
    return (z ** 2) * p * (1 - p) / (margin ** 2)


def finite_population_correct(n0: float, population: int) -> float:
    if population <= 0:
        return n0
    return n0 / (1 + (n0 - 1) / population)


def sample_size_plan(population: int, confidence: float, margin: float, p: float) -> dict:
    z = float(sp_stats.norm.ppf(1 - (1 - confidence) / 2))
    n0 = cochran_n(z, p, margin)
    n_fpc = finite_population_correct(n0, population)
    return {
        "z": round(z, 4),
        "assumed_p": p,
        "confidence": confidence,
        "margin_of_error": margin,
        "n_infinite_population": round(n0, 1),
        "n_finite_population_corrected": round(n_fpc, 1),
        "recommended_n": math.ceil(n_fpc),
    }


def load_eligible_chunks(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT chunk_id, external_chunk_id, doc_id, company_id, section_code,
               chunk_type, chunk_index, token_count, page_start, page_end
        FROM chunks
        WHERE rag_action = ?
        """,
        (RAG_ELIGIBLE_ACTION,),
    ).fetchall()


def proportional_allocation(
    group_sizes: dict, target_n: int, min_alloc: int = DEFAULT_MIN_ALLOC_PER_STRATUM
) -> dict:
    """Proportional allocation across strata, floored at min_alloc (capped by
    the stratum's own population). Mirrors qa_tier4.stratified_sample_by_ticker's
    `min(size, max(min_alloc, round(...)))` pattern -- deliberately not forced
    to sum to exactly target_n, since flooring small strata upward means the
    achieved total can legitimately exceed it.
    """
    total = sum(group_sizes.values())
    if total == 0:
        return {}
    return {
        key: min(size, max(min_alloc, round(target_n * size / total)))
        for key, size in group_sizes.items()
    }


def _stratum_size_buckets(sizes: list[int]) -> dict:
    buckets = Counter()
    for s in sizes:
        if s == 1:
            buckets["1"] += 1
        elif s <= 2:
            buckets["2"] += 1
        elif s <= 5:
            buckets["3-5"] += 1
        elif s <= 20:
            buckets["6-20"] += 1
        elif s <= 100:
            buckets["21-100"] += 1
        else:
            buckets["100+"] += 1
    return dict(buckets)


def _allocation_summary(alloc: dict, sizes: dict) -> dict:
    values = list(alloc.values())
    return {
        "strata": len(alloc),
        "population_covered": sum(sizes.values()),
        "achieved_sample_n": sum(values),
        "allocation_per_stratum": describe([float(v) for v in values]) if values else {"n": 0},
    }


def check_31(con, companies, docs, args, plan_grid: list[dict], headline_plan: dict):
    result = CheckResult("31", "Required manual-review sample size and stratified allocation")

    rows = load_eligible_chunks(con)
    total_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_eligible = len(rows)

    if n_eligible == 0:
        result.status = "SKIP"
        result.headline = "no RAG-eligible chunks to sample from"
        return result

    def ticker_of(row) -> str:
        return companies.get(row["company_id"], {}).get("ticker") or "UNKNOWN"

    def section_of(row) -> str:
        return row["section_code"] or "unknown"

    def type_of(row) -> str:
        return row["chunk_type"] or "unknown"

    sizes_ticker = Counter(ticker_of(r) for r in rows)
    sizes_section = Counter(section_of(r) for r in rows)
    sizes_type = Counter(type_of(r) for r in rows)
    sizes_joint = Counter((ticker_of(r), section_of(r), type_of(r)) for r in rows)

    recommended_n = headline_plan["recommended_n"]

    alloc_ticker = proportional_allocation(sizes_ticker, recommended_n, args.min_alloc)
    alloc_section = proportional_allocation(sizes_section, recommended_n, args.min_alloc)
    alloc_type = proportional_allocation(sizes_type, recommended_n, args.min_alloc)

    joint_sizes_list = list(sizes_joint.values())
    joint_singleton_or_pair = sum(1 for s in joint_sizes_list if s <= 2)

    result.stats = {
        "population_all_chunks": total_chunks,
        "population_rag_eligible_chunks": n_eligible,
        "population_used_for_design": (
            f"rag_eligible_chunks (rag_action = '{RAG_ELIGIBLE_ACTION}') -- the actually "
            "retrievable population; reviewing excluded/held chunks is a separate question "
            "(see qa_tier5 checks 29/30)"
        ),
        "headline_plan": headline_plan,
        "sample_size_by_assumed_error_rate": plan_grid,
        "joint_stratum_ticker_section_type": {
            "strata": len(sizes_joint),
            "size_distribution_bucketed": _stratum_size_buckets(joint_sizes_list),
            "share_singleton_or_pair_strata": (
                round(joint_singleton_or_pair / len(sizes_joint), 4) if sizes_joint else None
            ),
            "verdict": (
                "too sparse for direct proportional allocation at this N -- with "
                f"{len(sizes_joint)} (ticker x section_code x chunk_type) cells against a "
                f"target n of {recommended_n}, most cells would receive 0 or 1 under any "
                "rounding rule. Allocate on ticker, section_code, and chunk_type SEPARATELY "
                "(below) rather than on their cross-product."
            ),
        },
        "allocation_by_ticker": _allocation_summary(alloc_ticker, sizes_ticker),
        "allocation_by_section_code": _allocation_summary(alloc_section, sizes_section),
        "allocation_by_chunk_type": _allocation_summary(alloc_type, sizes_type),
    }

    ticker_examples = sorted(
        (
            {"ticker": t, "population": sizes_ticker[t], "allocated": alloc_ticker[t]}
            for t in alloc_ticker
        ),
        key=lambda r: -r["population"],
    )
    section_examples = sorted(
        (
            {"section_code": s, "population": sizes_section[s], "allocated": alloc_section[s]}
            for s in alloc_section
        ),
        key=lambda r: -r["population"],
    )
    type_examples = sorted(
        (
            {"chunk_type": c, "population": sizes_type[c], "allocated": alloc_type[c]}
            for c in alloc_type
        ),
        key=lambda r: -r["population"],
    )
    result.examples = {
        "by_ticker_top": ticker_examples[: args.examples],
        "by_section_code": section_examples,
        "by_chunk_type": type_examples,
    }

    if args.sample_out:
        drawn = _draw_sample(rows, docs, companies, recommended_n, args.min_alloc)
        _write_sample_csv(drawn, args.sample_out)
        section_coverage = Counter(section_of(r["_row"]) for r in drawn)
        result.stats["drawn_sample"] = {
            "path": str(args.sample_out),
            "n_drawn": len(drawn),
            "stratified_on": "ticker x chunk_type (min-1 proportional allocation, seed=0)",
            "section_code_coverage_achieved": dict(section_coverage),
        }

    result.ok(
        f"{recommended_n:,} chunks recommended (n0={headline_plan['n_infinite_population']:,.0f}, "
        f"finite-population corrected from N={n_eligible:,}) at "
        f"{headline_plan['confidence']:.0%} confidence / +/-{headline_plan['margin_of_error']:.0%} "
        f"margin, p={headline_plan['assumed_p']}; cross-stratifying ticker x section_code x "
        f"chunk_type produces {len(sizes_joint)} cells, too sparse to allocate directly -- "
        "use the three single-factor allocations instead"
    )
    return result


def _draw_sample(rows, docs, companies, target_n, min_alloc):
    """Two-factor (ticker x chunk_type) stratified draw, min-1 proportional
    allocation, seed=0 for reproducibility. Each returned dict carries the
    original sqlite3.Row under "_row" so callers can still key off raw columns
    (e.g. section_code) without re-deriving ticker/doc_stem themselves.
    """
    def ticker_of(row) -> str:
        return companies.get(row["company_id"], {}).get("ticker") or "UNKNOWN"

    def type_of(row) -> str:
        return row["chunk_type"] or "unknown"

    by_stratum: dict[tuple, list] = defaultdict(list)
    for row in rows:
        by_stratum[(ticker_of(row), type_of(row))].append(row)

    sizes_joint = {key: len(v) for key, v in by_stratum.items()}
    alloc = proportional_allocation(sizes_joint, target_n, min_alloc)

    rng = np.random.default_rng(0)
    drawn = []
    for key, quota in alloc.items():
        pool = by_stratum[key]
        idx = rng.choice(len(pool), size=min(quota, len(pool)), replace=False)
        for i in idx:
            row = pool[int(i)]
            drawn.append({
                "_row": row,
                "chunk_id": row["chunk_id"],
                "external_chunk_id": row["external_chunk_id"],
                "ticker": ticker_of(row),
                "doc_stem": docs.get(row["doc_id"], {}).get("stem"),
                "section_code": row["section_code"],
                "chunk_type": row["chunk_type"],
                "chunk_index": row["chunk_index"],
                "token_count": row["token_count"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
            })
    return drawn


def _write_sample_csv(drawn: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "chunk_id", "external_chunk_id", "ticker", "doc_stem", "section_code",
        "chunk_type", "chunk_index", "token_count", "page_start", "page_end",
        "reviewer_label", "reviewer_notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in drawn:
            writer.writerow({**row, "reviewer_label": "", "reviewer_notes": ""})


# ---------------------------------------------------------------------------
# check 32 -- annotator agreement (Cohen's kappa)
# ---------------------------------------------------------------------------


def cohens_kappa(labels_a: list, labels_b: list) -> dict:
    """Cohen's kappa for two raters' categorical labels over the same items.

    Also returns the large-sample approximate standard error (Fleiss, Cohen &
    Everitt 1969): SE = sqrt(po*(1-po) / (n*(1-pe)**2)). This is the standard
    "quick" approximation, not the fully general asymptotic variance (which
    needs the full confusion matrix, not just po/pe); adequate for a go/no-go
    read on precision, not for a published confidence interval.
    """
    if len(labels_a) != len(labels_b):
        raise ValueError("labels_a and labels_b must be the same length (paired ratings)")
    n = len(labels_a)
    if n == 0:
        return {"n": 0}

    categories = sorted({*labels_a, *labels_b}, key=str)
    idx = {c: i for i, c in enumerate(categories)}
    k = len(categories)
    confusion = np.zeros((k, k), dtype=float)
    for a, b in zip(labels_a, labels_b):
        confusion[idx[a], idx[b]] += 1

    po = float(np.trace(confusion) / n)
    row_marg = confusion.sum(axis=1) / n
    col_marg = confusion.sum(axis=0) / n
    pe = float((row_marg * col_marg).sum())
    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    se = math.sqrt(po * (1 - po) / (n * (1 - pe) ** 2)) if pe < 1 else None

    return {
        "n": n,
        "categories": categories,
        "observed_agreement_po": round(po, 4),
        "expected_agreement_pe": round(pe, 4),
        "kappa": round(kappa, 4) if kappa == kappa else None,  # NaN check
        "se_approx": round(se, 4) if se is not None else None,
        "ci_95_approx": (
            [round(kappa - 1.96 * se, 4), round(kappa + 1.96 * se, 4)] if se is not None else None
        ),
    }


def kappa_interpretation(kappa: float | None) -> str:
    """Landis & Koch (1977) band for a kappa value. KAPPA_BANDS ceilings are
    the upper edge of each band, checked in ascending order.
    """
    if kappa is None:
        return "undefined (raters agreed on every item and every label, or pe = 1)"
    if kappa < 0:
        return "poor (worse than chance)"
    for ceiling, label in KAPPA_BANDS:
        if kappa <= ceiling:
            return label
    return "almost perfect"


def kappa_precision_at_n(n: int, target_kappa: float, categories: int) -> dict:
    """Illustrative SE/CI half-width for a hypothetical measured kappa, under
    an equal-prevalence assumption (pe = 1/categories) -- used only to show how
    much precision a given overlap size buys before any real labels exist.
    """
    pe = 1.0 / categories
    po = target_kappa * (1 - pe) + pe
    se = math.sqrt(po * (1 - po) / (n * (1 - pe) ** 2)) if n > 0 else None
    return {
        "target_kappa": target_kappa,
        "assumed_categories": categories,
        "assumed_pe_equal_prevalence": round(pe, 4),
        "implied_po": round(po, 4),
        "se_approx": round(se, 4) if se is not None else None,
        "ci_95_half_width_approx": round(1.96 * se, 4) if se is not None else None,
    }


def load_annotation_pairs(path: Path) -> tuple[list, list]:
    """Expects a CSV with columns chunk_id,label_a,label_b -- one row per
    chunk both reviewers labeled (the overlap subset), long-form per-reviewer
    files are not supported here.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    missing = {"label_a", "label_b"} - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"annotations file missing required column(s): {sorted(missing)}")
    return [r["label_a"] for r in rows], [r["label_b"] for r in rows]


def check_32(args, headline_plan: dict):
    result = CheckResult("32", "How annotator agreement will be measured")

    recommended_n = headline_plan["recommended_n"]
    overlap_n = math.ceil(args.overlap_fraction * recommended_n)

    precision_table = [
        kappa_precision_at_n(overlap_n, target, args.categories)
        for target in [0.9, 0.8, 0.7, 0.6, 0.4]
    ]

    result.stats = {
        "review_sample_n": recommended_n,
        "overlap_fraction": args.overlap_fraction,
        "recommended_overlap_n": overlap_n,
        "method": (
            "Cohen's kappa on the shared overlap subset: both reviewers independently label "
            "the same chunks, kappa corrects raw agreement for the rate expected by chance "
            "alone, so it (unlike raw percent-agreement) cannot be inflated just by most "
            "chunks being unambiguously fine."
        ),
        "assumed_label_categories": args.categories,
        "precision_at_recommended_overlap_n": precision_table,
        "warn_floor": KAPPA_WARN_FLOOR,
        "warn_rationale": (
            "a corpus error rate measured by a single lenient/inattentive reviewer looks "
            "identical to a genuinely clean corpus until kappa is checked -- this is exactly "
            "the risk this check exists to catch"
        ),
    }

    if args.annotations:
        labels_a, labels_b = load_annotation_pairs(args.annotations)
        if len(labels_a) < 10:
            result.status = "SKIP"
            result.headline = (
                f"only {len(labels_a)} paired labels in {args.annotations}; too few to "
                "estimate kappa"
            )
            return result
        measured = cohens_kappa(labels_a, labels_b)
        interpretation = kappa_interpretation(measured["kappa"])
        result.stats["measured"] = measured
        result.stats["measured_interpretation"] = interpretation
        result.stats["annotations_file"] = str(args.annotations)

        if measured["kappa"] is not None and measured["kappa"] < KAPPA_WARN_FLOOR:
            result.warn(
                f"measured kappa {measured['kappa']} on {measured['n']} overlap items is only "
                f"'{interpretation}' -- below the {KAPPA_WARN_FLOOR} floor. Treat this round's "
                "measured error rate as unreliable until agreement improves"
            )
        else:
            result.ok(
                f"measured kappa {measured['kappa']} on {measured['n']} overlap items "
                f"('{interpretation}') -- reviewers agree well enough to trust the measured "
                "error rate"
            )
    else:
        result.status = "SKIP"
        result.headline = (
            f"no annotations yet -- design only: review {recommended_n:,} chunks, re-show "
            f"{overlap_n:,} ({args.overlap_fraction:.0%}) to a second reviewer, then pass "
            "--annotations to measure the real kappa"
        )

    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 6 -- sampling design for human review")
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
    # No pass/fail gate: like Tiers 2-5, nothing here is a provable defect.
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--checks", default="all",
                        help=f"comma-separated subset of {','.join(ALL_CHECKS)} (default: all)")
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE,
                        help=f"target CI confidence, e.g. 0.95 (default: {DEFAULT_CONFIDENCE})")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                        help=f"target margin of error, e.g. 0.02 (default: {DEFAULT_MARGIN})")
    parser.add_argument("--assumed-p", type=float, default=DEFAULT_ASSUMED_P,
                        help=f"assumed true proportion for the headline n (default: "
                             f"{DEFAULT_ASSUMED_P}, the conservative worst case)")
    parser.add_argument("--min-alloc", type=int, default=DEFAULT_MIN_ALLOC_PER_STRATUM,
                        help="minimum chunks allocated to any non-empty stratum "
                             f"(default: {DEFAULT_MIN_ALLOC_PER_STRATUM})")
    parser.add_argument("--overlap-fraction", type=float, default=DEFAULT_OVERLAP_FRACTION,
                        help=f"fraction of the sample re-shown to a second reviewer "
                             f"(default: {DEFAULT_OVERLAP_FRACTION})")
    parser.add_argument("--categories", type=int, default=DEFAULT_LABEL_CATEGORIES,
                        help="label categories assumed for the illustrative kappa-precision "
                             f"table (default: {DEFAULT_LABEL_CATEGORIES})")
    parser.add_argument("--sample-out", type=Path, default=None,
                        help="write the actual drawn review sample to this CSV (under reports/)")
    parser.add_argument("--annotations", type=Path, default=None,
                        help="CSV with columns chunk_id,label_a,label_b to compute real kappa")
    parser.add_argument("--examples", type=int, default=10,
                        help="max example rows to show per check (default: 10)")
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
        n_eligible = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE rag_action = ?", (RAG_ELIGIBLE_ACTION,)
        ).fetchone()[0]

        headline_plan = sample_size_plan(n_eligible, args.confidence, args.margin, args.assumed_p)
        plan_grid = [
            sample_size_plan(n_eligible, args.confidence, args.margin, p)
            for p in ASSUMED_P_GRID
        ]

        results: list[CheckResult] = []
        if "31" in selected:
            results.append(check_31(con, companies, docs, args, plan_grid, headline_plan))
        if "32" in selected:
            results.append(check_32(args, headline_plan))
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
