"""Stage 2 Checkpoint 2: distribution and outliers by company.

Runs against the snapshot frozen by checkpoint0_corpus_freeze.py. Every
question here returns names -- tickers, doc_ids, file paths -- rather than a
summary statistic, because the point of the checkpoint is to hand each outlier
to someone with enough context to disposition it.

The database is opened READ-ONLY (mode=ro) and only for Q14, which needs chunk
text; everything else is computed from the manifest. Outputs land in
<snapshot>/checkpoint2/.

Questions
    Q10 which companies sit in the bottom quartile of chunk counts, per year
        -> bottom_quartile_by_year.csv
    Q11 is a large chunk count a property of the documents or of the parsing
        -> company_normalisation.csv
    Q12 is chunk count explained by document size
        -> size_regression_residuals.csv
    Q13 is any company's year-over-year chunk count volatile beyond length
        -> yoy_volatility.csv
    Q14 has any source document been ingested more than once
        -> duplicate_ingestion.csv
    Q15 which documents are degenerate
        -> degenerate_documents.csv

Documents that Checkpoint 1 already classified as ingestion losses (parsed but
chunked to zero) would otherwise fill every bottom-quartile and residual list
in this checkpoint and crowd out the outliers it exists to find. They are
therefore excluded from the distributions and reported separately, under
`zero_chunk_documents`, with a pointer back to Checkpoint 1. Pass
--include-zero-chunk to fold them back in.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint2_outliers.py
    python esg/scripts/esg_database_tiers_2/checkpoint2_outliers.py --snapshot reports/qa_stage2/corpus_20260805T093753Z
    python esg/scripts/esg_database_tiers_2/checkpoint2_outliers.py --include-zero-chunk
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

csv.field_size_limit(10**9)

# A robust z-score beyond this is the line between "long report" and "look at
# the parser". Median/MAD rather than mean/sd because the distribution this
# runs on is exactly the one a handful of extreme values would distort.
ROBUST_Z_THRESHOLD = 3.0

# Standardised residual from the size regression beyond which a document's
# chunk count is not explained by how long the document is.
RESIDUAL_Z_THRESHOLD = 3.0

# Year-over-year chunk ratios outside this band are flagged and shown next to
# the page-count ratio for the same pair; a report really can triple in length,
# and the pairing is what separates that from a chunking change.
YOY_RATIO_HIGH = 3.0
YOY_RATIO_LOW = 1.0 / 3.0

# Low chunk counts and low parsed-text counts are review signals. A low chunk
# count alone is not proof of a failed parse: image-heavy reports can be many
# pages long while carrying only a small amount of extractable text.
DEGENERATE_MAX_CHUNKS = 10
DEGENERATE_MIN_PARSED_CHARS = 500
# Hard failures need both a long source and direct evidence that usable output
# is missing: zero chunks, zero sections, or almost no parsed text.
DEGENERATE_SUSPICIOUS_PAGES = 10

WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# result plumbing (same shape as checkpoints 0 and 1)
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    key: str
    title: str
    status: str = "PASS"          # PASS | FAIL | WARN | SKIP
    headline: str = ""
    stats: dict = field(default_factory=dict)
    examples: list = field(default_factory=list)
    outputs: list = field(default_factory=list)

    def fail(self, headline: str) -> "CheckResult":
        self.status, self.headline = "FAIL", headline
        return self

    def warn(self, headline: str) -> "CheckResult":
        if self.status != "FAIL":
            self.status, self.headline = "WARN", headline
        return self

    def ok(self, headline: str) -> "CheckResult":
        self.headline = headline
        return self


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def as_int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


# ---------------------------------------------------------------------------
# small numeric helpers (no numpy/scipy dependency, as in esg_database_tiers)
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return float(ordered[int(k)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (k - low))


def median(values: list[float]) -> float | None:
    return percentile(values, 0.5)


def robust_z(value: float | None, med: float | None, mad: float | None) -> float | None:
    """Median-absolute-deviation z-score, scaled to be comparable to a normal z.

    Returns None where the spread is zero, rather than an infinity that would
    make every row look like an outlier.
    """
    if value is None or med is None or not mad:
        return None
    return round(0.6745 * (value - med) / mad, 3)


def mad_of(values: list[float], med: float | None) -> float | None:
    if not values or med is None:
        return None
    return median([abs(v - med) for v in values])


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """Least-squares fit y = a + b*x; returns (a, b, r_squared)."""
    n = len(xs)
    if n < 3:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return intercept, slope, r2


# ---------------------------------------------------------------------------
# snapshot loading
# ---------------------------------------------------------------------------


def latest_snapshot(root: Path) -> Path | None:
    candidates = sorted(
        (p for p in root.glob("corpus_*") if (p / "snapshot.json").exists()),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def load_snapshot(snapshot_dir: Path) -> tuple[dict, list[dict]]:
    stamp_path = snapshot_dir / "snapshot.json"
    manifest_path = snapshot_dir / "corpus_manifest.csv"
    if not stamp_path.exists() or not manifest_path.exists():
        raise SystemExit(
            f"not a checkpoint 0 snapshot (need snapshot.json and corpus_manifest.csv): "
            f"{snapshot_dir}"
        )
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    manifest = []
    for row in read_csv(manifest_path):
        row["doc_id"] = as_int(row["doc_id"])
        row["report_year"] = as_int(row["report_year"])
        for column in ("section_count", "chunk_count", "eligible_chunk_count",
                       "short_evidence_chunk_count", "page_count", "parsed_chars",
                       "byte_size", "aliases_on_source_version"):
            row[column] = as_int(row.get(column))
        manifest.append(row)
    return stamp, manifest


# ---------------------------------------------------------------------------
# Q10 -- bottom quartile by year
# ---------------------------------------------------------------------------


def check_q10(docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q10", "Bottom-quartile chunk counts, per year, by name")

    # Aggregate to company-year: a company can file more than one document in
    # a year, and the slide counts the company, not the file.
    cells: dict[tuple[str, int], dict] = {}
    for doc in docs:
        if doc["report_year"] is None:
            continue
        key = (doc["ticker"], doc["report_year"])
        cell = cells.setdefault(key, {
            "ticker": doc["ticker"], "report_year": doc["report_year"],
            "documents": 0, "chunks": 0, "pages": 0, "parsed_chars": 0,
            "parse_statuses": set(), "filenames": [],
        })
        cell["documents"] += 1
        cell["chunks"] += doc["chunk_count"] or 0
        cell["pages"] += doc["page_count"] or 0
        cell["parsed_chars"] += doc["parsed_chars"] or 0
        cell["parse_statuses"].add(doc["parse_status"])
        cell["filenames"].append(doc["filename"])

    # The floor a bottom-quartile company is measured against: a short report
    # producing few chunks is fine, a long one producing few is not.
    density = [
        c["chunks"] / c["pages"] for c in cells.values() if c["pages"] and c["chunks"]
    ]
    density_floor = percentile(density, 0.05)

    rows: list[dict] = []
    per_year_cutoffs = {}
    for year in sorted({c["report_year"] for c in cells.values()}):
        year_cells = [c for c in cells.values() if c["report_year"] == year]
        counts = [c["chunks"] for c in year_cells]
        cutoff = percentile(counts, 0.25)
        per_year_cutoffs[year] = cutoff
        for cell in sorted(year_cells, key=lambda c: c["chunks"]):
            if cutoff is not None and cell["chunks"] > cutoff:
                continue
            chunks_per_page = (
                round(cell["chunks"] / cell["pages"], 3) if cell["pages"] else None
            )
            suspect = (
                chunks_per_page is not None and density_floor is not None
                and chunks_per_page < density_floor
            )
            rows.append({
                "report_year": year,
                "ticker": cell["ticker"],
                "chunks": cell["chunks"],
                "year_p25_cutoff": round(cutoff, 1) if cutoff is not None else None,
                "documents": cell["documents"],
                "pages": cell["pages"] or None,
                "chunks_per_page": chunks_per_page,
                "parsed_chars": cell["parsed_chars"] or None,
                "parse_status": "|".join(sorted(s or "" for s in cell["parse_statuses"])),
                "verdict": "suspected parse failure" if suspect else "short document, plausible",
                "filenames": " | ".join(cell["filenames"]),
            })

    path = write_csv(out_dir / "bottom_quartile_by_year.csv", rows,
                     ["report_year", "ticker", "chunks", "year_p25_cutoff", "documents",
                      "pages", "chunks_per_page", "parsed_chars", "parse_status", "verdict",
                      "filenames"])
    suspects = [r for r in rows if r["verdict"].startswith("suspected")]

    result.outputs = [str(path)]
    result.stats = {
        "company_year_cells": len(cells),
        "bottom_quartile_rows": len(rows),
        "per_year_p25_cutoff": {k: round(v, 1) for k, v in per_year_cutoffs.items() if v},
        "corpus_chunks_per_page_p05": round(density_floor, 3) if density_floor else None,
        "suspected_parse_failures": len(suspects),
        "suspect_tickers": sorted({r["ticker"] for r in suspects}),
    }
    result.examples = [
        {k: r[k] for k in ("report_year", "ticker", "chunks", "pages", "chunks_per_page",
                           "verdict")}
        for r in suspects[:examples_wanted] or rows[:examples_wanted]
    ]

    if suspects:
        return result.warn(
            f"{len(rows)} bottom-quartile company-years listed; {len(suspects)} produce fewer "
            f"chunks per page than the corpus 5th percentile and are suspected parse failures"
        )
    return result.ok(
        f"{len(rows)} bottom-quartile company-years listed; each has a correspondingly short "
        f"source document"
    )


# ---------------------------------------------------------------------------
# Q11 -- normalise before concluding
# ---------------------------------------------------------------------------


NORMALISED_MEASURES = ("chunks_per_page", "chunks_per_1k_chars", "chunks_per_mb")

# Only the text-grounded measures decide the verdict. Chunks per megabyte is
# computed and reported, but a PDF's byte size tracks how many photographs it
# contains, not how much text it holds, so an image-heavy report is extreme on
# that measure while being perfectly ordinary per page and per character.
# Judging on it flags healthy companies (NKE, LEG, UEIC on this build) and
# buries the real defects.
DECIDING_MEASURES = ("chunks_per_page", "chunks_per_1k_chars")


def check_q11(docs: list[dict], out_dir: Path, top_n: int, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q11", "Is a large chunk count the documents or the parsing")

    by_ticker: dict[str, dict] = {}
    for doc in docs:
        cell = by_ticker.setdefault(doc["ticker"], {
            "ticker": doc["ticker"], "documents": 0, "chunks": 0, "pages": 0,
            "parsed_chars": 0, "bytes": 0, "years": set(),
        })
        cell["documents"] += 1
        cell["chunks"] += doc["chunk_count"] or 0
        cell["pages"] += doc["page_count"] or 0
        cell["parsed_chars"] += doc["parsed_chars"] or 0
        cell["bytes"] += doc["byte_size"] or 0
        if doc["report_year"]:
            cell["years"].add(doc["report_year"])

    rows = []
    for cell in by_ticker.values():
        rows.append({
            "ticker": cell["ticker"],
            "documents": cell["documents"],
            "report_years": len(cell["years"]),
            "chunks": cell["chunks"],
            "pages": cell["pages"] or None,
            "parsed_chars": cell["parsed_chars"] or None,
            "megabytes": round(cell["bytes"] / 1e6, 2) if cell["bytes"] else None,
            "chunks_per_page": (
                round(cell["chunks"] / cell["pages"], 4) if cell["pages"] else None),
            "chunks_per_1k_chars": (
                round(1000 * cell["chunks"] / cell["parsed_chars"], 4)
                if cell["parsed_chars"] else None),
            "chunks_per_mb": (
                round(cell["chunks"] / (cell["bytes"] / 1e6), 4) if cell["bytes"] else None),
        })

    # Robust z on the raw count and on each normalised measure. The comparison
    # between the two is the answer: extreme raw, ordinary normalised means the
    # company simply files more and longer reports.
    raw_values = [float(r["chunks"]) for r in rows]
    raw_median = median(raw_values)
    raw_mad = mad_of(raw_values, raw_median)
    for row in rows:
        row["chunks_robust_z"] = robust_z(float(row["chunks"]), raw_median, raw_mad)

    stats_by_measure = {}
    for measure in NORMALISED_MEASURES:
        values = [r[measure] for r in rows if r[measure] is not None]
        med = median(values)
        mad = mad_of(values, med)
        stats_by_measure[measure] = {
            "median": round(med, 4) if med is not None else None,
            "mad": round(mad, 4) if mad is not None else None,
        }
        for row in rows:
            row[f"{measure}_robust_z"] = robust_z(row[measure], med, mad)

    for row in rows:
        deciding_z = [
            abs(row[f"{m}_robust_z"]) for m in DECIDING_MEASURES
            if row.get(f"{m}_robust_z") is not None
        ]
        extreme_raw = (
            row["chunks_robust_z"] is not None
            and abs(row["chunks_robust_z"]) > ROBUST_Z_THRESHOLD
        )
        extreme_normalised = bool(deciding_z) and max(deciding_z) > ROBUST_Z_THRESHOLD
        size_only = (
            not extreme_normalised
            and row.get("chunks_per_mb_robust_z") is not None
            and abs(row["chunks_per_mb_robust_z"]) > ROBUST_Z_THRESHOLD
        )
        if extreme_normalised:
            row["verdict"] = "parsing defect candidate: extreme per page or per character"
        elif extreme_raw:
            row["verdict"] = "document volume: extreme raw, ordinary once normalised"
        elif size_only:
            row["verdict"] = "extreme per megabyte only: image density, not chunking"
        else:
            row["verdict"] = "unremarkable"

    rows.sort(key=lambda r: -r["chunks"])
    path = write_csv(out_dir / "company_normalisation.csv", rows,
                     ["ticker", "documents", "report_years", "chunks", "chunks_robust_z",
                      "pages", "parsed_chars", "megabytes",
                      "chunks_per_page", "chunks_per_page_robust_z",
                      "chunks_per_1k_chars", "chunks_per_1k_chars_robust_z",
                      "chunks_per_mb", "chunks_per_mb_robust_z", "verdict"])

    defects = [r for r in rows if r["verdict"].startswith("parsing defect")]
    volume = [r for r in rows if r["verdict"].startswith("document volume")]
    size_only = [r for r in rows if r["verdict"].startswith("extreme per megabyte")]
    corpus_median = median([float(r["chunks"]) for r in rows])

    result.outputs = [str(path)]
    result.stats = {
        "companies": len(rows),
        "median_chunks_per_company": corpus_median,
        "deciding_measures": list(DECIDING_MEASURES),
        "normalised_measure_stats": stats_by_measure,
        "raw_outliers": [r["ticker"] for r in rows if r["chunks_robust_z"] is not None
                         and abs(r["chunks_robust_z"]) > ROBUST_Z_THRESHOLD],
        "explained_by_document_volume": [r["ticker"] for r in volume],
        "parsing_defect_candidates": [r["ticker"] for r in defects],
        "extreme_per_megabyte_only": [r["ticker"] for r in size_only],
        f"top_{top_n}": [
            {k: r[k] for k in ("ticker", "chunks", "documents", "pages", "chunks_per_page",
                               "chunks_robust_z", "verdict")}
            for r in rows[:top_n]
        ],
    }
    result.examples = [
        {k: r[k] for k in ("ticker", "chunks", "pages", "chunks_per_page",
                           "chunks_per_page_robust_z", "verdict")}
        for r in defects[:examples_wanted]
    ]

    if defects:
        return result.warn(
            f"{len(defects)} company(ies) stay beyond |z| = {ROBUST_Z_THRESHOLD} after "
            f"normalising and are parsing-defect candidates: "
            f"{', '.join(r['ticker'] for r in defects[:10])}"
        )
    return result.ok(
        f"{len(rows)} companies normalised; every raw outlier "
        f"({', '.join(r['ticker'] for r in volume) or 'none'}) is explained by document volume"
    )


# ---------------------------------------------------------------------------
# Q12 -- is chunk count explained by document size
# ---------------------------------------------------------------------------


def check_q12(docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q12", "Chunk count against document size")

    fits = {}
    residual_rows: dict[int, dict] = {}
    for predictor in ("page_count", "parsed_chars"):
        usable = [d for d in docs if d.get(predictor) and d["chunk_count"] is not None]
        if len(usable) < 3:
            fits[predictor] = None
            continue
        xs = [float(d[predictor]) for d in usable]
        ys = [float(d["chunk_count"]) for d in usable]
        fit = ols(xs, ys)
        if fit is None:
            fits[predictor] = None
            continue
        intercept, slope, r2 = fit
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        n = len(residuals)
        sigma = math.sqrt(sum(r * r for r in residuals) / max(n - 2, 1))
        fits[predictor] = {
            "n": n, "intercept": round(intercept, 4), "slope": round(slope, 6),
            "r_squared": round(r2, 4), "residual_sd": round(sigma, 3),
        }
        for doc, residual in zip(usable, residuals):
            z = round(residual / sigma, 3) if sigma else None
            row = residual_rows.setdefault(doc["doc_id"], {
                "doc_id": doc["doc_id"], "ticker": doc["ticker"],
                "report_year": doc["report_year"], "chunks": doc["chunk_count"],
                "pages": doc["page_count"], "parsed_chars": doc["parsed_chars"],
                "filepath": doc["filepath"],
            })
            row[f"residual_z_vs_{predictor}"] = z

    flagged = [
        row for row in residual_rows.values()
        if any(
            row.get(f"residual_z_vs_{p}") is not None
            and abs(row[f"residual_z_vs_{p}"]) > RESIDUAL_Z_THRESHOLD
            for p in ("page_count", "parsed_chars")
        )
    ]
    for row in flagged:
        z_page = row.get("residual_z_vs_page_count")
        row["direction"] = (
            "more chunks than length explains" if (z_page or 0) > 0
            else "fewer chunks than length explains"
        )
    flagged.sort(
        key=lambda r: -abs(r.get("residual_z_vs_page_count") or
                           r.get("residual_z_vs_parsed_chars") or 0)
    )

    path = write_csv(out_dir / "size_regression_residuals.csv", flagged,
                     ["doc_id", "ticker", "report_year", "chunks", "pages", "parsed_chars",
                      "residual_z_vs_page_count", "residual_z_vs_parsed_chars", "direction",
                      "filepath"])

    result.outputs = [str(path)]
    result.stats = {
        "fits": fits,
        "documents_scored": len(residual_rows),
        "residual_threshold": RESIDUAL_Z_THRESHOLD,
        "documents_beyond_threshold": len(flagged),
        "flagged_tickers": dict(Counter(r["ticker"] for r in flagged).most_common(10)),
        "direction_counts": dict(Counter(r["direction"] for r in flagged)),
    }
    result.examples = [
        {k: r.get(k) for k in ("doc_id", "ticker", "report_year", "chunks", "pages",
                               "residual_z_vs_page_count", "direction")}
        for r in flagged[:examples_wanted]
    ]

    if not any(fits.values()):
        result.status = "SKIP"
        return result.ok("no usable size predictor (page counts and parsed chars are absent)")
    if flagged:
        return result.warn(
            f"{len(flagged)} document(s) beyond |residual z| = {RESIDUAL_Z_THRESHOLD}; "
            f"page-count fit R^2 = "
            f"{fits.get('page_count', {}).get('r_squared') if fits.get('page_count') else 'n/a'}"
        )
    return result.ok(
        f"chunk count tracks document size for all {len(residual_rows)} documents "
        f"(R^2 = {fits.get('page_count', {}).get('r_squared')})"
    )


# ---------------------------------------------------------------------------
# Q13 -- year-over-year volatility
# ---------------------------------------------------------------------------


def check_q13(docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q13", "Year-over-year volatility against document length")

    cells: dict[tuple[str, int], dict] = defaultdict(lambda: {"chunks": 0, "pages": 0})
    for doc in docs:
        if doc["report_year"] is None:
            continue
        cell = cells[(doc["ticker"], doc["report_year"])]
        cell["chunks"] += doc["chunk_count"] or 0
        cell["pages"] += doc["page_count"] or 0

    by_ticker: dict[str, list[int]] = defaultdict(list)
    for ticker, year in cells:
        by_ticker[ticker].append(year)

    rows = []
    for ticker, years in by_ticker.items():
        for earlier, later in zip(sorted(years), sorted(years)[1:]):
            before, after = cells[(ticker, earlier)], cells[(ticker, later)]
            if not before["chunks"]:
                continue
            chunk_ratio = after["chunks"] / before["chunks"]
            page_ratio = (
                after["pages"] / before["pages"] if before["pages"] and after["pages"] else None
            )
            if YOY_RATIO_LOW <= chunk_ratio <= YOY_RATIO_HIGH:
                continue
            # A chunk swing matched by a page swing is a longer report, not a
            # chunking change; the ratio of ratios is what isolates the latter.
            explained = (
                page_ratio is not None
                and YOY_RATIO_LOW <= (chunk_ratio / page_ratio) <= YOY_RATIO_HIGH
            )
            rows.append({
                "ticker": ticker,
                "year_from": earlier, "year_to": later,
                "chunks_from": before["chunks"], "chunks_to": after["chunks"],
                "chunk_ratio": round(chunk_ratio, 3),
                "pages_from": before["pages"] or None, "pages_to": after["pages"] or None,
                "page_ratio": round(page_ratio, 3) if page_ratio else None,
                "ratio_of_ratios": (
                    round(chunk_ratio / page_ratio, 3) if page_ratio else None),
                "verdict": (
                    "explained by document length" if explained
                    else "unexplained by document length"),
            })

    rows.sort(key=lambda r: -abs(math.log(r["chunk_ratio"])) if r["chunk_ratio"] else 0)
    path = write_csv(out_dir / "yoy_volatility.csv", rows,
                     ["ticker", "year_from", "year_to", "chunks_from", "chunks_to",
                      "chunk_ratio", "pages_from", "pages_to", "page_ratio",
                      "ratio_of_ratios", "verdict"])
    unexplained = [r for r in rows if r["verdict"].startswith("unexplained")]

    result.outputs = [str(path)]
    result.stats = {
        "company_year_cells": len(cells),
        "consecutive_pairs_tested": sum(max(len(y) - 1, 0) for y in by_ticker.values()),
        "band": f"{round(YOY_RATIO_LOW, 3)}x - {YOY_RATIO_HIGH}x",
        "pairs_outside_band": len(rows),
        "explained_by_length": len(rows) - len(unexplained),
        "unexplained": len(unexplained),
        "unexplained_tickers": sorted({r["ticker"] for r in unexplained}),
    }
    result.examples = [
        {k: r[k] for k in ("ticker", "year_from", "year_to", "chunk_ratio", "page_ratio",
                           "verdict")}
        for r in unexplained[:examples_wanted]
    ]

    if unexplained:
        return result.warn(
            f"{len(unexplained)} of {len(rows)} out-of-band year pairs are not explained by a "
            f"matching change in page count"
        )
    return result.ok(
        f"{len(rows)} out-of-band year pair(s), each matched by a change in document length"
    )


# ---------------------------------------------------------------------------
# Q14 -- duplicate ingestion
# ---------------------------------------------------------------------------


def document_text_digest(con, doc_id: int) -> tuple[str, int] | None:
    """SHA-256 of a document's chunk text, whitespace-normalised and ordered."""
    digest = hashlib.sha256()
    total = 0
    for (text,) in con.execute(
        "SELECT chunk_text FROM chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,)
    ):
        normalised = WHITESPACE_RE.sub(" ", (text or "")).strip().casefold()
        total += len(normalised)
        digest.update(normalised.encode("utf-8"))
        digest.update(b"\x00")
    return (digest.hexdigest(), total) if total else None


def check_q14(con, docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q14", "Has any source document been ingested more than once")

    rows: list[dict] = []

    def add_group(classification: str, key: str, members: list[dict], detail: str = "") -> None:
        rows.append({
            "classification": classification,
            "group_key": key,
            "members": len(members),
            "doc_ids": "|".join(str(d["doc_id"]) for d in members),
            "tickers": "|".join(sorted({d["ticker"] for d in members})),
            "report_years": "|".join(sorted({str(d["report_year"]) for d in members})),
            "filenames": " | ".join(d["filename"] for d in members),
            "detail": detail,
            "cross_company": len({d["ticker"] for d in members}) > 1,
        })

    # 1. The same bytes, by the checksum the source of record carries.
    for column, classification in (
        ("drive_md5_checksum", "identical_drive_md5"),
        ("original_sha256", "identical_original_sha256"),
    ):
        groups: dict[str, list[dict]] = defaultdict(list)
        for doc in docs:
            value = (doc.get(column) or "").strip()
            if value:
                groups[value].append(doc)
        for key, members in groups.items():
            if len(members) > 1:
                add_group(classification, key, members)

    # 2. The same extracted text, which catches a re-download that produced a
    # byte-different file from the same report.
    digests: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        if not doc["chunk_count"]:
            continue
        digest = document_text_digest(con, doc["doc_id"])
        if digest:
            digests[digest[0]].append(doc)
    for key, members in digests.items():
        if len(members) > 1:
            add_group("identical_chunk_text", key, members)

    # 3. Two documents sharing one identity row, which Checkpoint 1 Q6 also
    # sees from the provenance side. Repeated here so this file is complete.
    identity_groups: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        if doc.get("source_version_id"):
            identity_groups[doc["source_version_id"]].append(doc)
    for key, members in identity_groups.items():
        if len(members) > 1:
            add_group("documents_sharing_one_source_version", key, members,
                      "see checkpoint 1 Q6")

    # 4. Benign-looking fan-out worth eyeballing: one source version with more
    # than one observed file name.
    fanned = [d for d in docs if (d.get("aliases_on_source_version") or 0) > 1]
    for doc in fanned:
        add_group("source_version_with_multiple_aliases", doc["source_version_id"], [doc],
                  f"{doc['aliases_on_source_version']} aliases on one source_version")

    path = write_csv(out_dir / "duplicate_ingestion.csv", rows,
                     ["classification", "group_key", "members", "doc_ids", "tickers",
                      "report_years", "filenames", "cross_company", "detail"])

    hard = [
        r for r in rows
        if r["classification"] in {"identical_drive_md5", "identical_original_sha256",
                                   "identical_chunk_text",
                                   "documents_sharing_one_source_version"}
    ]
    cross_company = [r for r in hard if r["cross_company"]]
    duplicated_chunks = sum(
        sum(d["chunk_count"] or 0 for d in docs if str(d["doc_id"]) in set(r["doc_ids"].split("|")))
        - max((d["chunk_count"] or 0) for d in docs
              if str(d["doc_id"]) in set(r["doc_ids"].split("|")))
        for r in rows if r["classification"] == "identical_chunk_text"
    )

    result.outputs = [str(path)]
    result.stats = {
        "documents_tested": len(docs),
        "groups_by_classification": dict(Counter(r["classification"] for r in rows)),
        "duplicate_groups": len(hard),
        "cross_company_duplicate_groups": len(cross_company),
        "chunks_double_counted_by_identical_text": duplicated_chunks,
    }
    result.examples = [
        {k: r[k] for k in ("classification", "tickers", "report_years", "members", "filenames")}
        for r in hard[:examples_wanted]
    ]

    if hard:
        return result.fail(
            f"{len(hard)} duplicate group(s) found ({duplicated_chunks} chunks double-counted "
            f"by identical text); every per-company and per-year figure over-counts them"
        )
    return result.ok(f"no duplicate ingestion across {len(docs)} documents")


# ---------------------------------------------------------------------------
# Q15 -- degenerate documents
# ---------------------------------------------------------------------------


def check_q15(docs: list[dict], zero_chunk: list[dict], out_dir: Path,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q15", "Degenerate documents")

    rows = []
    for doc in list(docs) + list(zero_chunk):
        reasons = []
        if (doc["chunk_count"] or 0) == 0:
            reasons.append("zero chunks")
        elif (doc["chunk_count"] or 0) < DEGENERATE_MAX_CHUNKS:
            reasons.append(f"fewer than {DEGENERATE_MAX_CHUNKS} chunks")
        if (doc["section_count"] or 0) == 0:
            reasons.append("zero sections")
        if doc["parsed_chars"] is not None and doc["parsed_chars"] < DEGENERATE_MIN_PARSED_CHARS:
            reasons.append(f"fewer than {DEGENERATE_MIN_PARSED_CHARS} parsed characters")
        if not reasons:
            continue
        pages = doc["page_count"]
        hard_output_gap = (
            (doc["chunk_count"] or 0) == 0
            or (doc["section_count"] or 0) == 0
            or (
                doc["parsed_chars"] is not None
                and doc["parsed_chars"] < DEGENERATE_MIN_PARSED_CHARS
            )
        )
        if pages and pages >= DEGENERATE_SUSPICIOUS_PAGES and hard_output_gap:
            verdict = "defect: long source with missing or near-empty output"
        elif pages and pages >= DEGENERATE_SUSPICIOUS_PAGES:
            verdict = "review: long source with low chunk volume"
        else:
            verdict = "plausible: genuinely short source"
        rows.append({
            "doc_id": doc["doc_id"], "ticker": doc["ticker"],
            "report_year": doc["report_year"],
            "chunks": doc["chunk_count"], "sections": doc["section_count"],
            "pages": pages, "parsed_chars": doc["parsed_chars"],
            "byte_size": doc["byte_size"], "parse_status": doc["parse_status"],
            "doc_quality_status": doc["doc_quality_status"],
            "reasons": "; ".join(reasons),
            "verdict": verdict,
            "filepath": doc["filepath"],
        })

    rows.sort(key=lambda r: (r["verdict"], -(r["pages"] or 0)))
    path = write_csv(out_dir / "degenerate_documents.csv", rows,
                     ["doc_id", "ticker", "report_year", "chunks", "sections", "pages",
                      "parsed_chars", "byte_size", "parse_status", "doc_quality_status",
                      "reasons", "verdict", "filepath"])
    defects = [r for r in rows if r["verdict"].startswith("defect")]
    reviews = [r for r in rows if r["verdict"].startswith("review")]

    result.outputs = [str(path)]
    result.stats = {
        "thresholds": {
            "max_chunks": DEGENERATE_MAX_CHUNKS,
            "min_parsed_chars": DEGENERATE_MIN_PARSED_CHARS,
            "suspicious_from_pages": DEGENERATE_SUSPICIOUS_PAGES,
        },
        "degenerate_documents": len(rows),
        "of_which_zero_chunk": len([r for r in rows if not r["chunks"]]),
        "defects": len(defects),
        "review_needed": len(reviews),
        "defect_tickers": dict(Counter(r["ticker"] for r in defects).most_common(10)),
        "pages_of_defects": {
            "median": median([float(r["pages"]) for r in defects if r["pages"]]),
            "max": max((r["pages"] for r in defects if r["pages"]), default=None),
        },
    }
    result.examples = [
        {k: r[k] for k in ("doc_id", "ticker", "report_year", "chunks", "pages", "reasons")}
        for r in defects[:examples_wanted]
    ]

    if defects:
        return result.fail(
            f"{len(defects)} document(s) of {DEGENERATE_SUSPICIOUS_PAGES}+ pages have "
            "zero or near-empty output"
        )
    if rows:
        return result.warn(
            f"{len(rows)} low-output document(s) need review; {len(reviews)} are long but "
            "still contain substantial parsed text, so low chunk count alone is not a "
            "parse failure"
        )
    return result.ok("no degenerate documents")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], header: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 2 -- distribution and outliers by company")
    print("=" * 78)
    print(f"manifest version : {header['manifest_version']}")
    print(f"snapshot dir     : {header['snapshot_dir']}")
    print(f"database         : {header['database']}")
    print(f"database sha256  : {header['database_sha256']} ({header['database_state']})")
    print(f"population       : {header['population']}")
    print(f"output dir       : {header['out_dir']}")
    print()

    for result in results:
        print(f"[{result.status:4}] {result.key}  {result.title}")
        if result.headline:
            print(f"       {result.headline}")
        for key, value in result.stats.items():
            print(f"       - {key}: {value}")
        for example in result.examples:
            print(f"       * {example}")
        for output in result.outputs:
            print(f"       -> {output}")
        print()

    statuses = Counter(r.status for r in results)
    gate = "CLEARED" if not statuses["FAIL"] else "NOT CLEARED"
    print("-" * 78)
    print(f"Checkpoint 2 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when every outlier beyond the thresholds above and every "
        "degenerate document carries a written disposition -- real, parsing defect, or "
        "duplicate -- and each defect has a ticket."
    )
    print("-" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot", type=Path, default=None,
                        help="checkpoint 0 snapshot directory "
                             "(default: the newest under reports/qa_stage2/)")
    parser.add_argument("--db", type=Path, default=None,
                        help="database to read (default: the one recorded in the snapshot)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output directory (default: <snapshot>/checkpoint2/)")
    parser.add_argument("--include-zero-chunk", action="store_true",
                        help="keep zero-chunk documents in the distributions "
                             "(by default they are held out; Checkpoint 1 Q9 owns them)")
    parser.add_argument("--top", type=int, default=10,
                        help="how many companies to show in the Q11 table (default: 10)")
    parser.add_argument("--allow-db-drift", action="store_true",
                        help="continue with a warning when the database no longer matches the "
                             "snapshot's SHA-256 (by default this is fatal)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint2.json)")
    args = parser.parse_args()

    snapshot_root = config.REPORTS_DIR / "qa_stage2"
    snapshot_dir = args.snapshot or latest_snapshot(snapshot_root)
    if snapshot_dir is None:
        raise SystemExit(
            f"no checkpoint 0 snapshot found under {snapshot_root}. Run "
            f"checkpoint0_corpus_freeze.py first."
        )
    stamp, manifest = load_snapshot(snapshot_dir)

    db_path = args.db or Path(stamp["database"])
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    actual_sha = sha256_file(db_path)
    expected_sha = stamp.get("database_sha256")
    if actual_sha == expected_sha:
        db_state = "matches the snapshot"
    elif args.allow_db_drift:
        db_state = f"DRIFTED from the snapshot ({expected_sha}) -- continuing under --allow-db-drift"
        print("warning: database has changed since the snapshot was frozen", file=sys.stderr)
    else:
        raise SystemExit(
            f"database has changed since the snapshot was frozen.\n"
            f"  snapshot: {expected_sha}\n  current : {actual_sha}\n"
            f"Re-run checkpoint 0, or pass --allow-db-drift to proceed anyway."
        )

    out_dir = args.out_dir or (snapshot_dir / "checkpoint2")
    out_dir.mkdir(parents=True, exist_ok=True)

    zero_chunk = [d for d in manifest if not (d["chunk_count"] or 0)]
    docs = manifest if args.include_zero_chunk else [
        d for d in manifest if (d["chunk_count"] or 0) > 0
    ]
    held_out = [] if args.include_zero_chunk else zero_chunk
    population = (
        f"{len(docs)} documents"
        + (f" ({len(held_out)} zero-chunk documents held out; see checkpoint 1 Q9)"
           if held_out else "")
    )

    con = connect(db_path)
    try:
        results = [
            check_q10(docs, out_dir, args.examples),
            check_q11(docs, out_dir, args.top, args.examples),
            check_q12(docs, out_dir, args.examples),
            check_q13(docs, out_dir, args.examples),
            check_q14(con, docs, out_dir, args.examples),
            check_q15(docs, held_out, out_dir, args.examples),
        ]
    finally:
        con.close()

    header = {
        "manifest_version": stamp.get("manifest_version"),
        "snapshot_dir": str(snapshot_dir),
        "database": str(db_path),
        "database_sha256": actual_sha,
        "database_state": db_state,
        "population": population,
        "zero_chunk_documents": len(zero_chunk),
        "out_dir": str(out_dir),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        **header,
        "thresholds": {
            "robust_z": ROBUST_Z_THRESHOLD,
            "residual_z": RESIDUAL_Z_THRESHOLD,
            "yoy_band": [YOY_RATIO_LOW, YOY_RATIO_HIGH],
            "degenerate_max_chunks": DEGENERATE_MAX_CHUNKS,
            "degenerate_min_parsed_chars": DEGENERATE_MIN_PARSED_CHARS,
        },
        "results": [
            {"question": r.key, "title": r.title, "status": r.status, "headline": r.headline,
             "stats": r.stats, "examples": r.examples, "outputs": r.outputs}
            for r in results
        ],
    }
    (out_dir / "checkpoint2.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    render(results, header)
    print(f"result set written to {out_dir / 'checkpoint2.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
