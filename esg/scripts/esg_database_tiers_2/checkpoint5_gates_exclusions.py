"""Stage 2 Checkpoint 5: gates, exclusions and their owners.

Runs against the snapshot frozen by checkpoint0_corpus_freeze.py. Everything the
pipeline decided not to index needs a reason code, a count and an owner; this
checkpoint produces all three, and says plainly where an input is missing rather
than substituting a weaker signal for it.

The database is opened READ-ONLY (mode=ro); outputs land in
<snapshot>/checkpoint5/.

Questions
    Q26 where do the ineligible chunks sit, and are they concentrated
        -> ineligible_chunks.csv, ineligible_by_document.csv
    Q27 what did the layout auto_hold gate exclude, and is it biased
        -> gate_comparison.csv
    Q28 what is the disposition plan for the manual_review queue
        -> manual_review_queue.csv

Q27 needs page-level layout QA verdicts, which live in the vector index
manifest (VECTOR_INDEX_MANIFEST_CSV), not in the database. Where that file is
absent the question is reported as SKIPPED with the command that regenerates
it; the RAG-action comparison that can be computed is still written out, but
labelled as a different gate rather than passed off as the layout one.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint5_gates_exclusions.py
    python esg/scripts/esg_database_tiers_2/checkpoint5_gates_exclusions.py --layout-manifest reports/db_qa/vector_index_manifest.csv
    python esg/scripts/esg_database_tiers_2/checkpoint5_gates_exclusions.py --snapshot reports/qa_stage2/corpus_20260805T093753Z
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

RAG_ACTION_ELIGIBLE = "index_as_esg"
RAG_ACTION_MANUAL_REVIEW = "manual_review_before_indexing"
RAG_ACTION_EXCLUDED = "exclude_from_esg_index"

# Q26. Exclusions scattered in proportion to document size are the cost of
# doing business; exclusions piled into a few documents mean those documents
# are failing systematically. The chi-square below tests the first against the
# second, and these two thresholds decide when concentration is called out.
CONCENTRATION_TOP_N = 5
TOP_N_SHARE_ALERT = 0.50
SIGNIFICANCE = 0.05

# Q27. Layout QA verdicts, as written by build_esg_vector_manifest.py.
LAYOUT_HELD_STATUS = "auto_hold"
LAYOUT_PASSED_STATUSES = {"pass", "auto_pass", "manual_pass"}
LAYOUT_MANIFEST_COMMAND = "python esg/scripts/build_esg_vector_manifest.py"

ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)


# ---------------------------------------------------------------------------
# result plumbing (same shape as checkpoints 0-4)
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


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return float(ordered[int(k)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (k - low))


def describe(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p05": round(percentile(values, 0.05), 4),
        "median": round(percentile(values, 0.50), 4),
        "mean": round(sum(values) / len(values), 4),
        "p95": round(percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }


# ---------------------------------------------------------------------------
# statistics, without numpy or scipy (see checkpoint 4 for the same helpers)
# ---------------------------------------------------------------------------


def _lower_gamma_series(s: float, x: float) -> float:
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
    if dof <= 0 or statistic < 0:
        return None
    if statistic == 0:
        return 1.0
    s, x = dof / 2.0, statistic / 2.0
    if x < s + 1.0:
        return max(0.0, min(1.0, 1.0 - _lower_gamma_series(s, x)))
    return max(0.0, min(1.0, _upper_gamma_cf(s, x)))


def goodness_of_fit(observed: list[float], expected: list[float]) -> dict:
    """Chi-square against an expected distribution (here: proportional to size)."""
    pairs = [(o, e) for o, e in zip(observed, expected) if e > 0]
    if len(pairs) < 2:
        return {"statistic": None, "dof": None, "p_value": None,
                "note": "not enough non-empty cells to test"}
    statistic = sum((o - e) ** 2 / e for o, e in pairs)
    dof = len(pairs) - 1
    return {
        "statistic": round(statistic, 4),
        "dof": dof,
        "p_value": chi2_sf(statistic, dof),
        "cells_with_expected_below_5": sum(1 for _, e in pairs if e < 5),
    }


def mann_whitney_u(a: list[float], b: list[float]) -> dict:
    """Two-sided rank-sum test with a normal approximation and tie correction."""
    n1, n2 = len(a), len(b)
    if n1 < 10 or n2 < 10:
        return {"u": None, "z": None, "p_value": None, "note": "sample too small"}
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(combined)
    tie_correction = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = average
        t = j - i + 1
        if t > 1:
            tie_correction += t ** 3 - t
        i = j + 1
    rank_sum_a = sum(r for r, (_, group) in zip(ranks, combined) if group == 0)
    u_a = rank_sum_a - n1 * (n1 + 1) / 2
    n = n1 + n2
    mean_u = n1 * n2 / 2
    variance = (n1 * n2 / 12) * ((n + 1) - tie_correction / (n * (n - 1)))
    if variance <= 0:
        return {"u": u_a, "z": None, "p_value": None, "note": "zero variance"}
    z = (u_a - mean_u) / math.sqrt(variance)
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"u": round(u_a, 1), "z": round(z, 4), "p_value": round(p, 8)}


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
        for column in ("section_count", "chunk_count", "eligible_chunk_count", "page_count",
                       "parsed_chars", "byte_size"):
            row[column] = as_int(row.get(column))
        manifest.append(row)
    return stamp, manifest


# ---------------------------------------------------------------------------
# Q26 -- where the ineligible chunks sit
# ---------------------------------------------------------------------------


def check_q26(con, manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q26", "Where the ineligible chunks sit, and how concentrated")

    docs = {d["doc_id"]: d for d in manifest}
    rows = con.execute(
        "SELECT chunk_id, doc_id, rag_action, chunk_type, section_code, quality_flags, "
        "short_section_reason, token_count FROM chunks"
    ).fetchall()

    total = len(rows)
    ineligible = [r for r in rows if (r["rag_action"] or "") != RAG_ACTION_ELIGIBLE]

    by_action = Counter(r["rag_action"] or "(none)" for r in ineligible)
    by_reason: Counter = Counter()
    for row in ineligible:
        flags = (row["quality_flags"] or "").strip()
        if flags:
            for flag in flags.split("|"):
                by_reason[flag] += 1
        elif row["short_section_reason"]:
            by_reason[row["short_section_reason"]] += 1
        else:
            by_reason["(no reason code recorded)"] += 1

    unexplained = [
        r for r in ineligible
        if not (r["quality_flags"] or "").strip() and not r["short_section_reason"]
    ]

    per_doc: dict[int, dict] = {}
    for row in ineligible:
        doc = docs.get(row["doc_id"], {})
        cell = per_doc.setdefault(row["doc_id"], {
            "doc_id": row["doc_id"],
            "ticker": doc.get("ticker"), "report_year": doc.get("report_year"),
            "chunks": doc.get("chunk_count") or 0,
            "ineligible_chunks": 0,
            RAG_ACTION_EXCLUDED: 0, RAG_ACTION_MANUAL_REVIEW: 0,
            "filepath": doc.get("filepath"),
        })
        cell["ineligible_chunks"] += 1
        if (row["rag_action"] or "") in cell:
            cell[row["rag_action"]] += 1
    for cell in per_doc.values():
        cell["ineligible_rate"] = (
            round(cell["ineligible_chunks"] / cell["chunks"], 4) if cell["chunks"] else None
        )
    doc_rows = sorted(per_doc.values(), key=lambda r: -r["ineligible_chunks"])

    # Is the exclusion spread in proportion to document size, or piled into a
    # few documents? Expected counts are the corpus rate applied to each
    # document's own chunk count.
    corpus_rate = len(ineligible) / total if total else 0
    scored = [d for d in manifest if (d["chunk_count"] or 0) > 0]
    observed = [float(per_doc.get(d["doc_id"], {}).get("ineligible_chunks", 0)) for d in scored]
    expected = [corpus_rate * (d["chunk_count"] or 0) for d in scored]
    fit = goodness_of_fit(observed, expected)

    top_docs_share = (
        sum(r["ineligible_chunks"] for r in doc_rows[:CONCENTRATION_TOP_N]) / len(ineligible)
        if ineligible else 0
    )
    by_company = Counter()
    for row in ineligible:
        by_company[docs.get(row["doc_id"], {}).get("ticker")] += 1
    top_companies_share = (
        sum(n for _, n in by_company.most_common(CONCENTRATION_TOP_N)) / len(ineligible)
        if ineligible else 0
    )

    chunk_rows = [
        {
            "chunk_id": r["chunk_id"], "doc_id": r["doc_id"],
            "ticker": docs.get(r["doc_id"], {}).get("ticker"),
            "report_year": docs.get(r["doc_id"], {}).get("report_year"),
            "rag_action": r["rag_action"], "chunk_type": r["chunk_type"],
            "section_code": r["section_code"], "token_count": r["token_count"],
            "quality_flags": r["quality_flags"],
            "short_section_reason": r["short_section_reason"],
            "has_reason_code": bool((r["quality_flags"] or "").strip()
                                    or r["short_section_reason"]),
        }
        for r in ineligible
    ]

    paths = [
        write_csv(out_dir / "ineligible_chunks.csv", chunk_rows,
                  ["chunk_id", "doc_id", "ticker", "report_year", "rag_action", "chunk_type",
                   "section_code", "token_count", "quality_flags", "short_section_reason",
                   "has_reason_code"]),
        write_csv(out_dir / "ineligible_by_document.csv", doc_rows,
                  ["doc_id", "ticker", "report_year", "chunks", "ineligible_chunks",
                   "ineligible_rate", RAG_ACTION_EXCLUDED, RAG_ACTION_MANUAL_REVIEW,
                   "filepath"]),
    ]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "chunks": total,
        "ineligible_chunks": len(ineligible),
        "ineligible_share": round(len(ineligible) / total, 4) if total else None,
        "by_rag_action": dict(by_action),
        "by_reason_code": dict(by_reason.most_common(12)),
        "chunks_without_a_reason_code": len(unexplained),
        "documents_holding_ineligible_chunks": len(doc_rows),
        f"top_{CONCENTRATION_TOP_N}_documents_share": round(top_docs_share, 4),
        f"top_{CONCENTRATION_TOP_N}_companies_share": round(top_companies_share, 4),
        f"top_{CONCENTRATION_TOP_N}_companies": dict(
            by_company.most_common(CONCENTRATION_TOP_N)),
        "proportional_to_size_test": fit,
        "spread_is_proportional": bool(
            fit["p_value"] is not None and fit["p_value"] >= SIGNIFICANCE),
    }
    result.examples = [
        {k: r[k] for k in ("doc_id", "ticker", "report_year", "chunks", "ineligible_chunks",
                           "ineligible_rate")}
        for r in doc_rows[:examples_wanted]
    ]

    if unexplained:
        return result.fail(
            f"{len(unexplained)} ineligible chunk(s) carry no reason code at all; an exclusion "
            f"without a reason cannot be reviewed or reversed"
        )
    if top_docs_share > TOP_N_SHARE_ALERT:
        return result.warn(
            f"{round(top_docs_share * 100)}% of the {len(ineligible)} ineligible chunks sit in "
            f"{CONCENTRATION_TOP_N} documents; those documents are failing systematically and "
            f"need tickets of their own"
        )
    return result.warn(
        f"{len(ineligible)} ineligible chunks "
        f"({round(len(ineligible) / total * 100, 2)}% of the index) across "
        f"{len(doc_rows)} documents, each carrying a reason code"
    )


# ---------------------------------------------------------------------------
# Q27 -- what the layout auto_hold gate excluded
# ---------------------------------------------------------------------------


def check_q27(con, manifest: list[dict], layout_manifest: Path, out_dir: Path,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q27", "What the layout auto_hold gate excluded, and whether it is biased")

    docs = {d["doc_id"]: d for d in manifest}
    chunks = con.execute(
        "SELECT chunk_id, external_chunk_id, doc_id, rag_action, section_code, token_count, "
        "chunk_text FROM chunks"
    ).fetchall()

    scored = []
    for row in chunks:
        text = row["chunk_text"] or ""
        if not text:
            continue
        scored.append({
            "chunk_id": row["chunk_id"],
            "external_chunk_id": (row["external_chunk_id"] or "").strip(),
            "doc_id": row["doc_id"],
            "ticker": docs.get(row["doc_id"], {}).get("ticker"),
            "rag_action": row["rag_action"] or "",
            "section_code": row["section_code"] or "(none)",
            "token_count": float(row["token_count"] or 0),
            "non_alpha_ratio": 1.0 - (len(ALPHA_RE.findall(text)) / len(text)),
        })

    layout_rows = read_csv(layout_manifest)
    status_by_chunk = {}
    for row in layout_rows:
        chunk_id = (row.get("chunk_id") or row.get("external_chunk_id") or "").strip()
        status = (row.get("layout_qa_status") or "").strip()
        if chunk_id and status:
            status_by_chunk[chunk_id] = status

    held = [c for c in scored if status_by_chunk.get(c["external_chunk_id"]) ==
            LAYOUT_HELD_STATUS]
    passed = [c for c in scored if status_by_chunk.get(c["external_chunk_id"]) in
              LAYOUT_PASSED_STATUSES]

    def population_summary(label: str, population: list[dict]) -> dict:
        return {
            "population": label,
            "chunks": len(population),
            "median_token_count": percentile([c["token_count"] for c in population], 0.5),
            "median_non_alpha_ratio": round(
                percentile([c["non_alpha_ratio"] for c in population], 0.5) or 0, 4),
            "p95_non_alpha_ratio": round(
                percentile([c["non_alpha_ratio"] for c in population], 0.95) or 0, 4),
            "top_section_codes": "|".join(
                f"{code}:{n}" for code, n in
                Counter(c["section_code"] for c in population).most_common(5)
            ),
        }

    if not status_by_chunk:
        # The RAG gate is a different gate. It is reported because it is the
        # only exclusion signal in the database, and labelled so that nobody
        # reads it as the layout answer.
        excluded = [c for c in scored if c["rag_action"] != RAG_ACTION_ELIGIBLE]
        indexed = [c for c in scored if c["rag_action"] == RAG_ACTION_ELIGIBLE]
        comparison = [
            population_summary("rag_excluded (STAND-IN, not the layout gate)", excluded),
            population_summary("rag_indexed (STAND-IN, not the layout gate)", indexed),
        ]
        digit_test = mann_whitney_u(
            [c["non_alpha_ratio"] for c in excluded],
            [c["non_alpha_ratio"] for c in indexed],
        )
        path = write_csv(out_dir / "gate_comparison.csv", comparison,
                         ["population", "chunks", "median_token_count",
                          "median_non_alpha_ratio", "p95_non_alpha_ratio",
                          "top_section_codes"])
        result.status = "SKIP"
        result.outputs = [str(path)]
        result.stats = {
            "layout_manifest": str(layout_manifest),
            "layout_manifest_present": layout_manifest.exists(),
            "layout_rows_with_a_status": 0,
            "regenerate_with": LAYOUT_MANIFEST_COMMAND,
            "stand_in_comparison": comparison,
            "stand_in_non_alpha_test": digit_test,
        }
        result.examples = comparison
        return result.ok(
            f"the layout QA verdicts are not available ({layout_manifest}), so this question "
            f"cannot be answered on this build; regenerate with `{LAYOUT_MANIFEST_COMMAND}` "
            f"and re-run. The RAG-action stand-in below is a different gate."
        )

    if len(held) < 10 or len(passed) < 10:
        result.status = "SKIP"
        result.stats = {
            "layout_manifest": str(layout_manifest),
            "held_chunks": len(held), "passed_chunks": len(passed),
        }
        return result.ok("too few held or passed chunks to compare")

    token_test = mann_whitney_u([c["token_count"] for c in held],
                                [c["token_count"] for c in passed])
    digit_test = mann_whitney_u([c["non_alpha_ratio"] for c in held],
                                [c["non_alpha_ratio"] for c in passed])

    held_median = percentile([c["non_alpha_ratio"] for c in held], 0.5) or 0
    passed_median = percentile([c["non_alpha_ratio"] for c in passed], 0.5) or 0
    difference = round(held_median - passed_median, 4)

    section_rates = []
    held_by_section = Counter(c["section_code"] for c in held)
    total_by_section = Counter(c["section_code"] for c in held + passed)
    for code, total in total_by_section.most_common():
        section_rates.append({
            "population": f"section:{code}",
            "chunks": total,
            "held": held_by_section.get(code, 0),
            "hold_rate": round(held_by_section.get(code, 0) / total, 4) if total else None,
        })

    comparison = [population_summary("auto_hold", held), population_summary("passed", passed)]
    path = write_csv(out_dir / "gate_comparison.csv", comparison + section_rates,
                     ["population", "chunks", "held", "hold_rate", "median_token_count",
                      "median_non_alpha_ratio", "p95_non_alpha_ratio", "top_section_codes"])

    held_by_company = Counter(c["ticker"] for c in held)

    result.outputs = [str(path)]
    result.stats = {
        "layout_manifest": str(layout_manifest),
        "held_chunks": len(held),
        "passed_chunks": len(passed),
        "hold_rate": round(len(held) / (len(held) + len(passed)), 4),
        "held_minus_passed_median_non_alpha": difference,
        "non_alpha_test": digit_test,
        "token_count_test": token_test,
        "held_by_company_top_10": dict(held_by_company.most_common(10)),
        "section_hold_rates_top_5": section_rates[:5],
    }
    result.examples = comparison

    if difference > 0 and (digit_test.get("p_value") or 1) < SIGNIFICANCE:
        return result.fail(
            f"held chunks are significantly more numeric than passed chunks "
            f"(median non-alphabetic ratio +{difference}, p = {digit_test['p_value']}); the "
            f"gate has removed quantitative disclosures, which is the content ESG questions ask "
            f"about"
        )
    return result.ok(
        f"{len(held)} held against {len(passed)} passed; no significant skew toward numeric "
        f"content in the held population"
    )


# ---------------------------------------------------------------------------
# Q28 -- the manual_review queue and its owner
# ---------------------------------------------------------------------------


def check_q28(con, manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q28", "Disposition plan for the manual_review queue")

    docs = {d["doc_id"]: d for d in manifest}
    queue = con.execute(
        "SELECT chunk_id, doc_id, section_code, chunk_type, token_count, quality_flags, "
        "short_section_reason FROM chunks WHERE rag_action = ?",
        (RAG_ACTION_MANUAL_REVIEW,),
    ).fetchall()

    approvals = con.execute(
        "SELECT source_approval_id, logical_source_id, source_version_id, approval_type, "
        "approval_status, reviewer, approval_date, reason FROM source_approvals"
    ).fetchall()

    approved_versions = {
        r["source_version_id"] for r in approvals
        if (r["approval_status"] or "").lower() in {"approved", "accepted"}
    }
    reviewed_versions = {r["source_version_id"] for r in approvals}

    rows = []
    for row in queue:
        doc = docs.get(row["doc_id"], {})
        version = doc.get("source_version_id")
        rows.append({
            "chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
            "ticker": doc.get("ticker"), "report_year": doc.get("report_year"),
            "section_code": row["section_code"], "chunk_type": row["chunk_type"],
            "token_count": row["token_count"],
            "quality_flags": row["quality_flags"],
            "short_section_reason": row["short_section_reason"],
            "source_version_id": version,
            "has_review_record": version in reviewed_versions,
            "review_status": next(
                (r["approval_status"] for r in approvals if r["source_version_id"] == version),
                None),
            "reviewer": next(
                (r["reviewer"] for r in approvals if r["source_version_id"] == version), None),
            "approval_date": next(
                (r["approval_date"] for r in approvals if r["source_version_id"] == version),
                None),
        })

    unowned = [r for r in rows if not r["has_review_record"]]
    path = write_csv(out_dir / "manual_review_queue.csv", rows,
                     ["chunk_id", "doc_id", "ticker", "report_year", "section_code",
                      "chunk_type", "token_count", "quality_flags", "short_section_reason",
                      "source_version_id", "has_review_record", "review_status", "reviewer",
                      "approval_date"])

    by_company = Counter(r["ticker"] for r in rows)
    by_reason = Counter()
    for row in rows:
        flags = (row["quality_flags"] or "").strip()
        for flag in (flags.split("|") if flags else [row["short_section_reason"] or "(none)"]):
            by_reason[flag] += 1

    result.outputs = [str(path)]
    result.stats = {
        "queue_size": len(rows),
        "documents_in_queue": len({r["doc_id"] for r in rows}),
        "companies_in_queue": len(by_company),
        "top_companies": dict(by_company.most_common(10)),
        "by_reason_code": dict(by_reason.most_common(10)),
        "source_approvals_rows": len(approvals),
        "approval_statuses": dict(Counter(
            (r["approval_status"] or "(none)") for r in approvals)),
        "reviewers_named": sorted({r["reviewer"] for r in approvals if r["reviewer"]}),
        "queue_chunks_with_a_review_record": len(rows) - len(unowned),
        "queue_chunks_without_an_owner": len(unowned),
        "expected_size_after_review": "to be stated by the reviewer before the Checkpoint 6 "
                                      "re-run, so the change can be checked against it",
        "approved_source_versions": len(approved_versions),
    }
    result.examples = [
        {k: r[k] for k in ("chunk_id", "ticker", "report_year", "section_code",
                           "quality_flags", "has_review_record")}
        for r in rows[:examples_wanted]
    ]

    if rows and not approvals:
        return result.fail(
            f"{len(rows)} chunks are queued for manual review and source_approvals is empty: "
            f"the queue has no reviewer, no date and no recorded decision, so nothing will "
            f"ever leave it"
        )
    if unowned:
        return result.fail(
            f"{len(unowned)} of {len(rows)} queued chunks have no review record; every "
            f"excluded chunk needs a named owner and a target date"
        )
    if rows:
        return result.warn(
            f"{len(rows)} chunks queued, all with a review record; the expected post-review "
            f"count still has to be written down before Checkpoint 6 re-runs"
        )
    return result.ok("the manual review queue is empty")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], header: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 5 -- gates, exclusions and their owners")
    print("=" * 78)
    print(f"manifest version : {header['manifest_version']}")
    print(f"snapshot dir     : {header['snapshot_dir']}")
    print(f"database         : {header['database']}")
    print(f"database sha256  : {header['database_sha256']} ({header['database_state']})")
    print(f"layout manifest  : {header['layout_manifest']} ({header['layout_manifest_state']})")
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
    print(f"Checkpoint 5 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when every excluded chunk carries a reason code and a named "
        "owner, and the auto_hold bias test in Q27 has been run and read before any retrieval "
        "evaluation is quoted."
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
    parser.add_argument("--layout-manifest", type=Path,
                        default=config.VECTOR_INDEX_MANIFEST_CSV,
                        help="vector index manifest carrying layout_qa_status for Q27 "
                             f"(default: {config.VECTOR_INDEX_MANIFEST_CSV})")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output directory (default: <snapshot>/checkpoint5/)")
    parser.add_argument("--allow-db-drift", action="store_true",
                        help="continue with a warning when the database no longer matches the "
                             "snapshot's SHA-256 (by default this is fatal)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint5.json)")
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

    out_dir = args.out_dir or (snapshot_dir / "checkpoint5")
    out_dir.mkdir(parents=True, exist_ok=True)

    con = connect(db_path)
    try:
        results = [
            check_q26(con, manifest, out_dir, args.examples),
            check_q27(con, manifest, args.layout_manifest, out_dir, args.examples),
            check_q28(con, manifest, out_dir, args.examples),
        ]
    finally:
        con.close()

    header = {
        "manifest_version": stamp.get("manifest_version"),
        "snapshot_dir": str(snapshot_dir),
        "database": str(db_path),
        "database_sha256": actual_sha,
        "database_state": db_state,
        "layout_manifest": str(args.layout_manifest),
        "layout_manifest_state": (
            "present" if args.layout_manifest.exists() else "ABSENT -- Q27 cannot be answered"),
        "out_dir": str(out_dir),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        **header,
        "thresholds": {
            "concentration_top_n": CONCENTRATION_TOP_N,
            "top_n_share_alert": TOP_N_SHARE_ALERT,
            "significance": SIGNIFICANCE,
        },
        "results": [
            {"question": r.key, "title": r.title, "status": r.status, "headline": r.headline,
             "stats": r.stats, "examples": r.examples, "outputs": r.outputs}
            for r in results
        ],
    }
    (out_dir / "checkpoint5.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    render(results, header)
    print(f"result set written to {out_dir / 'checkpoint5.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
