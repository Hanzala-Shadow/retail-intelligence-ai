"""Stage 2 Checkpoint 4: coverage of the panel -- what the corpus can and cannot be asked.

Runs against the snapshot frozen by checkpoint0_corpus_freeze.py and the
company-year grid it wrote, so the denominator here is the one fixed in
Checkpoint 0 rather than one derived on the fly.

The database is opened READ-ONLY (mode=ro) and only for company metadata;
outputs land in <snapshot>/checkpoint4/.

Questions
    Q22 of the companies with zero chunks, which are absent reports and which
        are pipeline losses
        -> zero_coverage_companies.csv
    Q23 what does the company-by-year coverage matrix look like
        -> coverage_matrix.csv, coverage_matrix.md, coverage_cells.csv
    Q24 is missingness random, or structured by year, sector or company size
        -> missingness_tests.csv
    Q25 how concentrated is the index across companies and sectors
        -> concentration_by_company.csv, concentration_by_sector.csv

Only the pipeline classes in Q22 are defects. A company that never published a
report is a scope limitation, and the two are reported apart so the deck can
say which is which.

Statistics are computed without numpy or scipy, which are not installed in this
environment: the chi-square survival function is evaluated directly (see
chi2_sf) rather than approximated or dropped.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint4_coverage.py
    python esg/scripts/esg_database_tiers_2/checkpoint4_coverage.py --snapshot reports/qa_stage2/corpus_20260805T093753Z
    python esg/scripts/esg_database_tiers_2/checkpoint4_coverage.py --recent-years 1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

csv.field_size_limit(10**9)

# Q22 loss classes, in the order a company falls through them. Only the last
# two are pipeline defects.
COVERAGE_CLASSES = (
    "no_report_published",      # tracker/reason codes say nothing exists
    "report_not_found",         # a report exists somewhere but was not collected
    "downloaded_not_parsed",    # in the corpus, parse_status not 'parsed'
    "parsed_not_chunked",       # parsed, but the chunker produced nothing
)
PARSE_STATUS_OK = "parsed"

# Q24. Association tests below this p-value are reported as structured
# missingness rather than noise.
SIGNIFICANCE = 0.05
# The most recent report year is usually incomplete because companies have not
# published yet; it is excluded from the missingness tests by default and
# reported separately so publication lag is not mistaken for a collection gap.
DEFAULT_RECENT_YEARS_EXCLUDED = 1

# Q25. Concentration thresholds at which the index is called skewed enough to
# carry into retrieval evaluation as a weighting caveat.
GINI_ALERT = 0.50
TOP_10_SHARE_ALERT = 0.30


# ---------------------------------------------------------------------------
# result plumbing (same shape as checkpoints 0-3)
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


def as_bool(value) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def norm_ticker(value: str | None) -> str:
    return (value or "").strip().upper()


# ---------------------------------------------------------------------------
# statistics, without numpy or scipy
# ---------------------------------------------------------------------------


def _lower_gamma_series(s: float, x: float) -> float:
    """Regularised lower incomplete gamma P(s, x) by series expansion."""
    term = 1.0 / s
    total = term
    n = 0
    while n < 1000:
        n += 1
        term *= x / (s + n)
        total += term
        if term < total * 1e-14:
            break
    return total * math.exp(-x + s * math.log(x) - math.lgamma(s))


def _upper_gamma_cf(s: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(s, x) by continued fraction."""
    tiny = 1e-300
    b = x + 1.0 - s
    c = 1.0 / tiny
    d = 1.0 / b if b else 1.0 / tiny
    h = d
    for i in range(1, 1000):
        an = -i * (i - s)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h * math.exp(-x + s * math.log(x) - math.lgamma(s))


def chi2_sf(statistic: float, dof: int) -> float | None:
    """P(X > statistic) for a chi-square with `dof` degrees of freedom."""
    if dof <= 0 or statistic < 0:
        return None
    if statistic == 0:
        return 1.0
    s, x = dof / 2.0, statistic / 2.0
    if x < s + 1.0:
        return max(0.0, min(1.0, 1.0 - _lower_gamma_series(s, x)))
    return max(0.0, min(1.0, _upper_gamma_cf(s, x)))


def chi2_independence(table: dict[str, dict[str, int]]) -> dict:
    """Chi-square test of independence on a {row: {column: count}} table."""
    rows = sorted(table)
    columns = sorted({c for r in table.values() for c in r})
    observed = [[table[r].get(c, 0) for c in columns] for r in rows]
    total = sum(sum(r) for r in observed)
    if total == 0 or len(rows) < 2 or len(columns) < 2:
        return {"statistic": None, "dof": None, "p_value": None,
                "note": "not enough rows or columns to test"}
    row_totals = [sum(r) for r in observed]
    col_totals = [sum(observed[i][j] for i in range(len(rows))) for j in range(len(columns))]

    statistic = 0.0
    small_cells = 0
    for i in range(len(rows)):
        for j in range(len(columns)):
            expected = row_totals[i] * col_totals[j] / total
            if expected == 0:
                continue
            if expected < 5:
                small_cells += 1
            statistic += (observed[i][j] - expected) ** 2 / expected
    dof = (len(rows) - 1) * (len(columns) - 1)
    return {
        "statistic": round(statistic, 4),
        "dof": dof,
        "p_value": chi2_sf(statistic, dof),
        # The chi-square approximation degrades when expected counts are small;
        # saying so is cheaper than pretending the p-value is exact.
        "cells_with_expected_below_5": small_cells,
        "n": total,
    }


def gini(values: list[float]) -> float | None:
    """Gini coefficient of a non-negative distribution."""
    clean = sorted(v for v in values if v is not None and v >= 0)
    n = len(clean)
    total = sum(clean)
    if n == 0 or total == 0:
        return None
    cumulative = sum((i + 1) * v for i, v in enumerate(clean))
    return round((2 * cumulative) / (n * total) - (n + 1) / n, 4)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, ties averaged."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mean_x, mean_y = sum(rx) / n, sum(ry) / n
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry))
    return round(num / den, 4) if den else None


# ---------------------------------------------------------------------------
# snapshot loading
# ---------------------------------------------------------------------------


def latest_snapshot(root: Path) -> Path | None:
    candidates = sorted(
        (p for p in root.glob("corpus_*") if (p / "snapshot.json").exists()),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def load_snapshot(snapshot_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    stamp_path = snapshot_dir / "snapshot.json"
    manifest_path = snapshot_dir / "corpus_manifest.csv"
    grid_path = snapshot_dir / "company_year_grid.csv"
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
        for column in ("section_count", "chunk_count", "eligible_chunk_count", "page_count",
                       "parsed_chars", "byte_size"):
            row[column] = as_int(row.get(column))
        manifest.append(row)

    grid = []
    for row in read_csv(grid_path):
        row["year"] = as_int(row["year"])
        row["documents"] = as_int(row["documents"]) or 0
        row["chunks"] = as_int(row["chunks"]) or 0
        row["in_scope"] = as_bool(row["in_scope"])
        grid.append(row)
    if not grid:
        raise SystemExit(
            f"company_year_grid.csv missing or empty in {snapshot_dir}; re-run checkpoint 0"
        )
    return stamp, manifest, grid


# ---------------------------------------------------------------------------
# Q22 -- zero-coverage companies: absent report or pipeline loss
# ---------------------------------------------------------------------------


def check_q22(con, manifest: list[dict], grid: list[dict], out_dir: Path,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q22", "Zero-coverage companies: absent reports or pipeline losses")

    docs_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for doc in manifest:
        docs_by_ticker[norm_ticker(doc["ticker"])].append(doc)

    universe = {}
    for cell in grid:
        universe.setdefault(norm_ticker(cell["ticker"]), {
            "ticker": norm_ticker(cell["ticker"]),
            "company_name": cell.get("company_name"),
            "sector": cell.get("sector"),
            "decision": cell.get("decision"),
        })

    reasons = {
        norm_ticker(r.get("ticker")): r
        for r in read_csv(config.REFERENCE_DIR / "esg_not_found_reason_codes.csv")
    }
    tracker_by_ticker = Counter(
        norm_ticker(r.get("ticker"))
        for r in read_csv(config.SUSTAINABILITY_TRACKER_CSV)
    )

    rows = []
    for ticker, company in sorted(universe.items()):
        docs = docs_by_ticker.get(ticker, [])
        chunks = sum(d["chunk_count"] or 0 for d in docs)
        if chunks:
            continue
        reason = reasons.get(ticker, {})
        tracker_rows = tracker_by_ticker.get(ticker, 0)

        if docs and any((d["parse_status"] or "") != PARSE_STATUS_OK for d in docs):
            klass = "downloaded_not_parsed"
        elif docs:
            klass = "parsed_not_chunked"
        elif tracker_rows:
            klass = "report_not_found"
        elif (reason.get("coverage_reason_code") or "") in {
            "no_published_standalone_report", "website_only_candidate",
            "supplemental_public_pdf_candidate_not_standalone",
        }:
            klass = "no_report_published"
        elif reason:
            klass = "report_not_found"
        else:
            klass = "report_not_found"

        rows.append({
            "coverage_class": klass,
            "is_pipeline_defect": klass in {"downloaded_not_parsed", "parsed_not_chunked"},
            "ticker": ticker,
            "company_name": company["company_name"],
            "sector": company["sector"],
            "decision": company["decision"],
            "tracker_rows": tracker_rows,
            "documents": len(docs),
            "pages": sum(d["page_count"] or 0 for d in docs) or None,
            "coverage_reason_code": reason.get("coverage_reason_code"),
            "recommended_action": reason.get("recommended_action"),
        })

    rows.sort(key=lambda r: (not r["is_pipeline_defect"], r["coverage_class"], r["ticker"]))
    path = write_csv(out_dir / "zero_coverage_companies.csv", rows,
                     ["coverage_class", "is_pipeline_defect", "ticker", "company_name",
                      "sector", "decision", "tracker_rows", "documents", "pages",
                      "coverage_reason_code", "recommended_action"])

    counts = Counter(r["coverage_class"] for r in rows)
    defects = [r for r in rows if r["is_pipeline_defect"]]

    result.outputs = [str(path)]
    result.stats = {
        "companies_in_universe": len(universe),
        "companies_with_zero_chunks": len(rows),
        "by_class": {c: counts.get(c, 0) for c in COVERAGE_CLASSES},
        "pipeline_defect_companies": [r["ticker"] for r in defects],
        "documents_lost_to_pipeline": sum(r["documents"] for r in defects),
        "pages_lost_to_pipeline": sum(r["pages"] or 0 for r in defects),
        "reason_codes": dict(Counter(
            r["coverage_reason_code"] for r in rows if r["coverage_reason_code"]
        )),
    }
    result.examples = [
        {k: r[k] for k in ("coverage_class", "ticker", "documents", "pages",
                           "coverage_reason_code")}
        for r in (defects or rows)[:examples_wanted]
    ]

    if defects:
        return result.fail(
            f"{len(defects)} company(ies) have documents in the corpus but no chunk at all "
            f"({sum(r['documents'] for r in defects)} documents, "
            f"{sum(r['pages'] or 0 for r in defects)} pages): "
            f"{', '.join(r['ticker'] for r in defects[:10])}"
        )
    return result.ok(
        f"{len(rows)} zero-chunk companies, all of them absent reports rather than pipeline "
        f"losses"
    )


# ---------------------------------------------------------------------------
# Q23 -- the coverage matrix
# ---------------------------------------------------------------------------


def heatmap_row(values: list[int], breaks: list[int]) -> str:
    """One row of a text heat map, two characters per cell so it lines up with
    the two-digit year header: `.` empty, then rising density characters."""
    glyphs = " .-+*#"
    out = []
    for value in values:
        if value == 0:
            out.append(". ")
            continue
        level = 1
        for threshold in breaks:
            if value >= threshold:
                level += 1
        out.append(glyphs[min(level, len(glyphs) - 1)] + " ")
    return "".join(out)


def check_q23(grid: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q23", "The company-by-year coverage matrix")

    years = sorted({c["year"] for c in grid})
    tickers = sorted({norm_ticker(c["ticker"]) for c in grid})
    by_cell = {(norm_ticker(c["ticker"]), c["year"]): c for c in grid}

    long_rows = []
    for ticker in tickers:
        for year in years:
            cell = by_cell.get((ticker, year))
            if cell is None:
                continue
            if not cell["in_scope"]:
                state = "out_of_scope"
            elif cell["chunks"]:
                state = "covered"
            elif cell["documents"]:
                state = "document_without_chunks"
            else:
                state = "empty"
            long_rows.append({
                "ticker": ticker, "year": year,
                "sector": cell.get("sector"), "decision": cell.get("decision"),
                "in_scope": cell["in_scope"], "state": state,
                "documents": cell["documents"], "chunks": cell["chunks"],
            })

    wide_rows = []
    for ticker in tickers:
        row = {"ticker": ticker,
               "sector": by_cell.get((ticker, years[0]), {}).get("sector")}
        for year in years:
            cell = by_cell.get((ticker, year))
            row[str(year)] = cell["chunks"] if cell and cell["in_scope"] else ""
        row["total_chunks"] = sum(
            by_cell[(ticker, y)]["chunks"] for y in years if (ticker, y) in by_cell
        )
        wide_rows.append(row)
    wide_rows.sort(key=lambda r: -r["total_chunks"])

    in_scope = [r for r in long_rows if r["in_scope"]]
    covered = [r for r in in_scope if r["state"] == "covered"]

    def fill_rate(rows_: list[dict]) -> float | None:
        return round(
            len([r for r in rows_ if r["state"] == "covered"]) / len(rows_), 4
        ) if rows_ else None

    by_year = {
        str(year): {
            "in_scope": len([r for r in in_scope if r["year"] == year]),
            "covered": len([r for r in in_scope if r["year"] == year
                            and r["state"] == "covered"]),
            "fill_rate": fill_rate([r for r in in_scope if r["year"] == year]),
        }
        for year in years
    }
    by_sector = {
        (sector or "(unknown)"): {
            "in_scope": len([r for r in in_scope if (r["sector"] or "") == (sector or "")]),
            "covered": len([r for r in in_scope if (r["sector"] or "") == (sector or "")
                            and r["state"] == "covered"]),
            "fill_rate": fill_rate(
                [r for r in in_scope if (r["sector"] or "") == (sector or "")]
            ),
        }
        for sector in sorted({r["sector"] for r in in_scope}, key=lambda s: s or "")
    }

    # A text heat map so the shape of the panel is legible without a plotting
    # dependency: the pattern of the holes is the point, not their exact size.
    chunk_values = [r["chunks"] for r in covered]
    breaks = [
        int(sorted(chunk_values)[int(len(chunk_values) * q)])
        for q in (0.25, 0.5, 0.75, 0.9)
    ] if chunk_values else [1, 2, 3, 4]
    lines = [
        f"# Coverage matrix -- {len(tickers)} companies x {len(years)} years",
        "",
        "Legend: `.` no chunks   `-` low   `+` mid   `*` high   `#` top decile",
        f"Break points (chunks): {breaks}",
        "",
        "```",
        "ticker   " + "".join(str(y)[-2:] for y in years),
    ]
    for row in wide_rows:
        values = [by_cell[(row["ticker"], y)]["chunks"] if (row["ticker"], y) in by_cell else 0
                  for y in years]
        lines.append(row["ticker"].ljust(9) + heatmap_row(values, breaks).rstrip())
    lines.append("```")
    matrix_md = out_dir / "coverage_matrix.md"
    matrix_md.parent.mkdir(parents=True, exist_ok=True)
    matrix_md.write_text("\n".join(lines), encoding="utf-8")

    paths = [
        write_csv(out_dir / "coverage_matrix.csv", wide_rows,
                  ["ticker", "sector"] + [str(y) for y in years] + ["total_chunks"]),
        write_csv(out_dir / "coverage_cells.csv", long_rows,
                  ["ticker", "year", "sector", "decision", "in_scope", "state",
                   "documents", "chunks"]),
        matrix_md,
    ]

    empty_years = [y for y in years if by_year[str(y)]["covered"] == 0]
    empty_sectors = [s for s, v in by_sector.items() if v["covered"] == 0 and v["in_scope"]]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "companies": len(tickers),
        "years": f"{years[0]}-{years[-1]}",
        "in_scope_cells": len(in_scope),
        "covered_cells": len(covered),
        "fill_rate": fill_rate(in_scope),
        "cells_with_documents_but_no_chunks": len(
            [r for r in in_scope if r["state"] == "document_without_chunks"]),
        "by_year": by_year,
        "by_sector": by_sector,
        # companies.sector carries one value for the whole universe on some
        # builds. Saying so keeps a vacuous sector breakdown from reading as
        # evidence that coverage is unbiased across sectors.
        "sector_field_usable": len(by_sector) > 1 or None,
        "sector_note": (
            "companies.sector holds a single value for every company on this build, so no "
            "sector comparison is possible here"
            if len(by_sector) <= 1 else ""
        ),
        "years_with_no_coverage": empty_years,
        "sectors_with_no_coverage": empty_sectors,
    }
    result.examples = [
        {k: r[k] for k in ("ticker", "sector", "total_chunks")} for r in wide_rows[:examples_wanted]
    ]

    if empty_years or empty_sectors:
        return result.fail(
            f"a whole year ({empty_years}) or sector ({empty_sectors}) has no coverage at all; "
            f"that is a collection defect, not a scattered gap"
        )
    return result.warn(
        f"fill rate {fill_rate(in_scope)} over {len(in_scope)} in-scope company-years; the "
        f"pattern of the holes is in coverage_matrix.md and must be described in words"
    )


# ---------------------------------------------------------------------------
# Q24 -- is missingness structured
# ---------------------------------------------------------------------------


def check_q24(grid: list[dict], manifest: list[dict], out_dir: Path, recent_years: int,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q24", "Is missingness random or structured")

    in_scope = [c for c in grid if c["in_scope"]]
    if not in_scope:
        result.status = "SKIP"
        return result.ok("no in-scope cells to test")

    years = sorted({c["year"] for c in in_scope})
    excluded_years = years[-recent_years:] if recent_years else []
    tested = [c for c in in_scope if c["year"] not in excluded_years]

    def bucket(cell: dict) -> str:
        return "covered" if cell["chunks"] else "empty"

    year_table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sector_table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for cell in tested:
        year_table[str(cell["year"])][bucket(cell)] += 1
        sector_table[(cell.get("sector") or "(unknown)")][bucket(cell)] += 1

    year_test = chi2_independence({k: dict(v) for k, v in year_table.items()})
    sector_test = chi2_independence({k: dict(v) for k, v in sector_table.items()})

    # Company size, proxied by how many reports the company has in the corpus,
    # against how many of its in-scope years are covered. A positive rank
    # correlation means the panel is better populated for prolific reporters.
    docs_by_ticker = Counter(norm_ticker(d["ticker"]) for d in manifest)
    per_company: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for cell in tested:
        ticker = norm_ticker(cell["ticker"])
        per_company[ticker][1] += 1
        if cell["chunks"]:
            per_company[ticker][0] += 1
    sizes, rates = [], []
    for ticker, (covered, total) in per_company.items():
        if total:
            sizes.append(float(docs_by_ticker.get(ticker, 0)))
            rates.append(covered / total)
    size_rho = spearman(sizes, rates)

    trend_rows = [
        {
            "year": year,
            "in_scope_cells": len([c for c in in_scope if c["year"] == year]),
            "covered_cells": len([c for c in in_scope if c["year"] == year and c["chunks"]]),
            "fill_rate": round(
                len([c for c in in_scope if c["year"] == year and c["chunks"]])
                / max(len([c for c in in_scope if c["year"] == year]), 1), 4),
            "excluded_from_tests": year in excluded_years,
            "note": "most recent year: publication lag, not necessarily a gap"
                    if year in excluded_years else "",
        }
        for year in years
    ]

    test_rows = [
        {"test": "coverage vs report year", "statistic": year_test["statistic"],
         "dof": year_test["dof"],
         "p_value": None if year_test["p_value"] is None else round(year_test["p_value"], 6),
         "n": year_test.get("n"),
         "small_expected_cells": year_test.get("cells_with_expected_below_5"),
         "structured": bool(year_test["p_value"] is not None
                            and year_test["p_value"] < SIGNIFICANCE)},
        {"test": "coverage vs sector", "statistic": sector_test["statistic"],
         "dof": sector_test["dof"],
         "p_value": None if sector_test["p_value"] is None else round(sector_test["p_value"], 6),
         "n": sector_test.get("n"),
         "small_expected_cells": sector_test.get("cells_with_expected_below_5"),
         "structured": bool(sector_test["p_value"] is not None
                            and sector_test["p_value"] < SIGNIFICANCE)},
        {"test": "coverage rate vs company size (Spearman rho)", "statistic": size_rho,
         "dof": None, "p_value": None, "n": len(sizes), "small_expected_cells": None,
         "structured": bool(size_rho is not None and abs(size_rho) >= 0.3)},
    ]

    paths = [
        write_csv(out_dir / "missingness_tests.csv", test_rows,
                  ["test", "statistic", "dof", "p_value", "n", "small_expected_cells",
                   "structured"]),
        write_csv(out_dir / "coverage_trend_by_year.csv", trend_rows,
                  ["year", "in_scope_cells", "covered_cells", "fill_rate",
                   "excluded_from_tests", "note"]),
    ]

    structured = [r["test"] for r in test_rows if r["structured"]]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "cells_tested": len(tested),
        "years_excluded_as_recent": excluded_years,
        "significance": SIGNIFICANCE,
        "year_test": year_test,
        "sector_test": sector_test,
        "company_size_rho": size_rho,
        "size_proxy": "documents per company in the corpus",
        "sector_test_usable": len(sector_table) > 1,
        "structured_dimensions": structured,
        "untested_dimensions": (
            ["sector: companies.sector holds one value for every company on this build"]
            if len(sector_table) <= 1 else []
        ),
    }
    result.examples = [
        {k: r[k] for k in ("year", "in_scope_cells", "covered_cells", "fill_rate", "note")}
        for r in trend_rows[-examples_wanted:]
    ]

    if structured:
        return result.warn(
            f"missingness is structured by {', '.join(structured)}; this belongs in the "
            f"limitations section, since it biases every panel-level conclusion"
        )
    return result.ok(
        f"no association between coverage and year, sector or company size at p < {SIGNIFICANCE}"
    )


# ---------------------------------------------------------------------------
# Q25 -- concentration
# ---------------------------------------------------------------------------


def check_q25(manifest: list[dict], grid: list[dict], out_dir: Path,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q25", "Concentration of the index across companies and sectors")

    sectors = {
        norm_ticker(c["ticker"]): (c.get("sector") or "(unknown)") for c in grid
    }
    chunks_by_ticker: dict[str, int] = defaultdict(int)
    eligible_by_ticker: dict[str, int] = defaultdict(int)
    docs_by_ticker: dict[str, int] = defaultdict(int)
    for doc in manifest:
        ticker = norm_ticker(doc["ticker"])
        chunks_by_ticker[ticker] += doc["chunk_count"] or 0
        eligible_by_ticker[ticker] += doc["eligible_chunk_count"] or 0
        docs_by_ticker[ticker] += 1

    contributing = {t: n for t, n in chunks_by_ticker.items() if n}
    total_chunks = sum(contributing.values())

    company_rows = []
    running = 0
    for ticker, chunks in sorted(contributing.items(), key=lambda kv: -kv[1]):
        running += chunks
        company_rows.append({
            "rank": len(company_rows) + 1,
            "ticker": ticker,
            "sector": sectors.get(ticker, "(unknown)"),
            "documents": docs_by_ticker[ticker],
            "chunks": chunks,
            "eligible_chunks": eligible_by_ticker[ticker],
            "share_of_index": round(chunks / total_chunks, 5) if total_chunks else None,
            "cumulative_share": round(running / total_chunks, 5) if total_chunks else None,
        })

    # Companies with zero chunks are part of the universe and belong in the
    # Gini: leaving them out would understate concentration by exactly the
    # companies the index cannot answer for.
    universe_tickers = {norm_ticker(c["ticker"]) for c in grid}
    gini_all = gini([float(chunks_by_ticker.get(t, 0)) for t in universe_tickers])
    gini_contributing = gini([float(v) for v in contributing.values()])
    top_10_share = round(
        sum(r["chunks"] for r in company_rows[:10]) / total_chunks, 4
    ) if total_chunks else None

    sector_chunks: dict[str, int] = defaultdict(int)
    sector_companies: dict[str, set] = defaultdict(set)
    for ticker in universe_tickers:
        sector_companies[sectors.get(ticker, "(unknown)")].add(ticker)
        sector_chunks[sectors.get(ticker, "(unknown)")] += chunks_by_ticker.get(ticker, 0)
    total_companies = len(universe_tickers)

    sector_rows = []
    for sector in sorted(sector_chunks, key=lambda s: -sector_chunks[s]):
        chunk_share = sector_chunks[sector] / total_chunks if total_chunks else 0
        company_share = len(sector_companies[sector]) / total_companies if total_companies else 0
        sector_rows.append({
            "sector": sector,
            "companies": len(sector_companies[sector]),
            "company_share": round(company_share, 4),
            "chunks": sector_chunks[sector],
            "chunk_share": round(chunk_share, 4),
            "over_representation": round(chunk_share / company_share, 3) if company_share else None,
        })

    paths = [
        write_csv(out_dir / "concentration_by_company.csv", company_rows,
                  ["rank", "ticker", "sector", "documents", "chunks", "eligible_chunks",
                   "share_of_index", "cumulative_share"]),
        write_csv(out_dir / "concentration_by_sector.csv", sector_rows,
                  ["sector", "companies", "company_share", "chunks", "chunk_share",
                   "over_representation"]),
    ]

    skewed_sectors = [
        r["sector"] for r in sector_rows
        if r["over_representation"] is not None and (
            r["over_representation"] >= 2 or r["over_representation"] <= 0.5
        )
    ]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "companies_in_universe": total_companies,
        "companies_contributing_chunks": len(contributing),
        "total_chunks": total_chunks,
        "gini_over_universe": gini_all,
        "gini_over_contributing_companies": gini_contributing,
        "top_10_share": top_10_share,
        "top_10": [
            {k: r[k] for k in ("ticker", "chunks", "share_of_index", "cumulative_share")}
            for r in company_rows[:10]
        ],
        "sectors_over_or_under_represented_2x": skewed_sectors,
        "alerts": {"gini": GINI_ALERT, "top_10_share": TOP_10_SHARE_ALERT},
    }
    result.examples = [
        {k: r[k] for k in ("sector", "companies", "company_share", "chunk_share",
                           "over_representation")}
        for r in sector_rows[:examples_wanted]
    ]

    triggered = []
    if gini_all is not None and gini_all >= GINI_ALERT:
        triggered.append(f"Gini {gini_all}")
    if top_10_share is not None and top_10_share >= TOP_10_SHARE_ALERT:
        triggered.append(f"top-10 share {top_10_share}")
    if triggered:
        return result.warn(
            f"the index is concentrated ({', '.join(triggered)}); retrieval evaluation has to "
            f"carry this as a weighting caveat rather than leave it implicit"
        )
    return result.ok(
        f"index concentration is moderate (Gini {gini_all}, top-10 share {top_10_share})"
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], header: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 4 -- coverage of the panel")
    print("=" * 78)
    print(f"manifest version : {header['manifest_version']}")
    print(f"snapshot dir     : {header['snapshot_dir']}")
    print(f"database         : {header['database']}")
    print(f"database sha256  : {header['database_sha256']} ({header['database_state']})")
    print(f"denominator      : {header['denominator']}")
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
    print(f"Checkpoint 4 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when the coverage matrix is published together with an explicit "
        "statement of which company-years cannot be answered from this corpus."
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
                        help="output directory (default: <snapshot>/checkpoint4/)")
    parser.add_argument("--recent-years", type=int, default=DEFAULT_RECENT_YEARS_EXCLUDED,
                        help="how many trailing years to exclude from the missingness tests as "
                             "publication lag (default: 1, 0 to test every year)")
    parser.add_argument("--allow-db-drift", action="store_true",
                        help="continue with a warning when the database no longer matches the "
                             "snapshot's SHA-256 (by default this is fatal)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint4.json)")
    args = parser.parse_args()

    snapshot_root = config.REPORTS_DIR / "qa_stage2"
    snapshot_dir = args.snapshot or latest_snapshot(snapshot_root)
    if snapshot_dir is None:
        raise SystemExit(
            f"no checkpoint 0 snapshot found under {snapshot_root}. Run "
            f"checkpoint0_corpus_freeze.py first."
        )
    stamp, manifest, grid = load_snapshot(snapshot_dir)

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

    out_dir = args.out_dir or (snapshot_dir / "checkpoint4")
    out_dir.mkdir(parents=True, exist_ok=True)

    in_scope_cells = len([c for c in grid if c["in_scope"]])
    denominator = (
        f"{len({norm_ticker(c['ticker']) for c in grid})} companies x "
        f"{len({c['year'] for c in grid})} years = {in_scope_cells} in-scope cells "
        f"(from checkpoint 0 Q3)"
    )

    con = connect(db_path)
    try:
        results = [
            check_q22(con, manifest, grid, out_dir, args.examples),
            check_q23(grid, out_dir, args.examples),
            check_q24(grid, manifest, out_dir, args.recent_years, args.examples),
            check_q25(manifest, grid, out_dir, args.examples),
        ]
    finally:
        con.close()

    header = {
        "manifest_version": stamp.get("manifest_version"),
        "snapshot_dir": str(snapshot_dir),
        "database": str(db_path),
        "database_sha256": actual_sha,
        "database_state": db_state,
        "denominator": denominator,
        "out_dir": str(out_dir),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        **header,
        "thresholds": {
            "significance": SIGNIFICANCE,
            "recent_years_excluded": args.recent_years,
            "gini_alert": GINI_ALERT,
            "top_10_share_alert": TOP_10_SHARE_ALERT,
        },
        "results": [
            {"question": r.key, "title": r.title, "status": r.status, "headline": r.headline,
             "stats": r.stats, "examples": r.examples, "outputs": r.outputs}
            for r in results
        ],
    }
    (out_dir / "checkpoint4.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    render(results, header)
    print(f"result set written to {out_dir / 'checkpoint4.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
