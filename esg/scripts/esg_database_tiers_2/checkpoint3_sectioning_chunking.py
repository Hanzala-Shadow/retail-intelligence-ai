"""Stage 2 Checkpoint 3: sectioning and chunking behaviour.

Runs against the snapshot frozen by checkpoint0_corpus_freeze.py. The
first-stage tiers established the distributions; this checkpoint asks which
documents are behind them, and writes every threshold down before it is applied
so the same test can be re-run after a fix and compared.

The database is opened READ-ONLY (mode=ro); outputs land in
<snapshot>/checkpoint3/.

Questions
    Q16 full_document fallback rate, broken out rather than corpus-wide
        -> fallback_by_company.csv
    Q17 do chunks cover the parsed text, and where do they stop
        -> chunk_coverage.csv
    Q18 which documents fall outside a plausible chunks-per-page band
        -> chunks_per_page_band.csv
    Q19 is the short_evidence rate concentrated in a few documents
        -> short_evidence_concentration.csv
    Q20 how much of the index is tabular or numeric, and did the gate keep it
        -> numeric_content.csv, numeric_review_sample.csv
    Q21 how much of the index is boilerplate, headers or navigation
        -> duplicate_text.csv, repeated_strings.csv

Q17 compares chunk character spans against the parsed character count recorded
in the checkpoint 0 manifest, so it needs a snapshot built without --no-text.
Q20's held-versus-passed comparison needs the layout QA manifest
(VECTOR_INDEX_MANIFEST_CSV); where that file is absent the comparison is
reported as skipped and the RAG action is used as a labelled stand-in rather
than silently substituted.

Zero-chunk documents are held out of the distributions by default -- Checkpoint
1 Q9 and Checkpoint 2 Q15 own them -- and counted in the header.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint3_sectioning_chunking.py
    python esg/scripts/esg_database_tiers_2/checkpoint3_sectioning_chunking.py --near-dup-sample 0
    python esg/scripts/esg_database_tiers_2/checkpoint3_sectioning_chunking.py --snapshot reports/qa_stage2/corpus_20260805T093753Z
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

# ---------------------------------------------------------------------------
# thresholds -- written down before the questions are asked, so that a re-run
# after a fix is comparable rather than re-tuned
# ---------------------------------------------------------------------------

# Q17. A document may legitimately leave front matter unchunked, so coverage
# below 1.0 is reported rather than failed. Above 1.0 is different: chunk
# offsets that run past the end of the parsed text cannot be cited.
COVERAGE_FLOOR = 0.95
COVERAGE_CEILING = 1.02
# Tail loss is where the appendices live -- the quantitative tables ESG
# questions actually ask about -- so the trailing gap gets its own threshold.
TRAILING_GAP_MAX = 0.05

# Q18. The band is the corpus's own 5th-95th percentile of chunks per page.
BAND_LOW_PERCENTILE = 0.05
BAND_HIGH_PERCENTILE = 0.95

# Q19. A document needs this many chunks before its short_evidence rate is
# meaningful; without a floor, a 3-chunk document with one short chunk lands at
# 33% and dominates the ranking.
SHORT_EVIDENCE_MIN_CHUNKS = 20
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"

# Q20. "Numeric" means a chunk in the top percentile of non-alphabetic
# character fraction; a document is table-heavy if that many of its chunks are.
NUMERIC_TOP_PERCENTILE = 0.99
DOCUMENT_NUMERIC_SHARE_ALERT = 0.50

# Q21. Exact duplicates are hashed on normalised text; a repeated string is
# named once it appears in this many chunks.
BOILERPLATE_MIN_REPEATS = 100
NEAR_DUP_THRESHOLD = 0.90
NEAR_DUP_SHINGLE = 5           # words per shingle
NEAR_DUP_PERMUTATIONS = 32
NEAR_DUP_BANDS = 8             # 8 bands x 4 rows over 32 permutations
CROSS_COMPANY_DUP_ALERT = 0.01  # share of the index

ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
LAYOUT_HELD_STATUS = "auto_hold"
LAYOUT_PASSED_STATUSES = {"pass", "auto_pass", "manual_pass"}
MASK64 = (1 << 64) - 1


# ---------------------------------------------------------------------------
# result plumbing (same shape as checkpoints 0-2)
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


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of half-open [start, end) intervals, sorted and coalesced."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def normalise(text: str | None) -> str:
    return WHITESPACE_RE.sub(" ", (text or "")).strip().casefold()


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
                       "byte_size"):
            row[column] = as_int(row.get(column))
        manifest.append(row)
    return stamp, manifest


# ---------------------------------------------------------------------------
# Q16 -- full_document fallback rate
# ---------------------------------------------------------------------------


def check_q16(con, docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q16", "full_document fallback rate, broken out")

    sections_by_doc: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in con.execute(
        "SELECT doc_id, section_id, section_code, char_count FROM sections"
    ):
        sections_by_doc[row["doc_id"]].append(row)

    in_scope = {d["doc_id"]: d for d in docs}
    single_section = {
        doc_id: rows for doc_id, rows in sections_by_doc.items()
        if doc_id in in_scope and len(rows) == 1
    }
    mislabelled = [
        doc_id for doc_id, rows in single_section.items()
        if rows[0]["section_code"] != "full_document"
    ]

    def breakdown(key_fn) -> dict:
        counts: dict = defaultdict(lambda: [0, 0])
        for doc_id, doc in in_scope.items():
            counts[key_fn(doc)][1] += 1
            if doc_id in single_section:
                counts[key_fn(doc)][0] += 1
        return {
            str(key): {"fallback": fb, "total": total,
                       "rate": round(fb / total, 4) if total else None}
            for key, (fb, total) in sorted(counts.items(), key=lambda kv: -kv[1][0])
        }

    by_ticker = breakdown(lambda d: d["ticker"])
    rows = [
        {"ticker": ticker, "documents": v["total"], "fallback_documents": v["fallback"],
         "fallback_rate": v["rate"],
         "verdict": ("every document falls back" if v["rate"] == 1.0
                     else "partial fallback" if v["fallback"] else "sectioned")}
        for ticker, v in by_ticker.items()
    ]
    rows.sort(key=lambda r: (-(r["fallback_rate"] or 0), -r["documents"]))

    # Reconcile against the sectioning QA reference rather than recomputing
    # blind. The file is written by an earlier build, so a mismatch is a
    # vintage difference to be reported, not silently trusted either way.
    reference = read_csv(config.REFERENCE_DIR / "esg_sectioning_quality_by_document.csv")
    reference_by_stem = {
        (r.get("pdf_stem") or "").strip().casefold(): r for r in reference if r.get("pdf_stem")
    }
    matched = disagreements = 0
    for doc in docs:
        ref = reference_by_stem.get(Path(doc["filename"]).stem.strip().casefold())
        if not ref:
            continue
        matched += 1
        ref_sections = as_int(ref.get("sections"))
        if ref_sections is not None and ref_sections != (doc["section_count"] or 0):
            disagreements += 1

    path = write_csv(out_dir / "fallback_by_company.csv", rows,
                     ["ticker", "documents", "fallback_documents", "fallback_rate", "verdict"])

    total_docs = len(in_scope)
    n_fallback = len(single_section)
    always = [r["ticker"] for r in rows if r["fallback_rate"] == 1.0]

    result.outputs = [str(path)]
    result.stats = {
        "documents": total_docs,
        "documents_with_exactly_one_section": n_fallback,
        "fallback_rate": round(n_fallback / total_docs, 4) if total_docs else None,
        "single_section_codes": dict(Counter(
            rows_[0]["section_code"] for rows_ in single_section.values()
        )),
        "single_section_not_coded_full_document": len(mislabelled),
        "companies_always_falling_back": always,
        "by_year": breakdown(lambda d: d["report_year"]),
        "by_parser_used": breakdown(lambda d: d["parser_used"] or "(not recorded)"),
        "reference_file_matched_documents": f"{matched} of {total_docs}",
        "reference_section_count_disagreements": disagreements,
    }
    result.examples = [
        {"doc_id": doc_id, "ticker": in_scope[doc_id]["ticker"],
         "section_code": rows_[0]["section_code"], "filename": in_scope[doc_id]["filename"]}
        for doc_id, rows_ in list(single_section.items())[:examples_wanted]
    ]

    # A single-section document that is not coded 'full_document' is a labelling
    # inconsistency, not a corrupted corpus: the fallback is still detectable by
    # counting sections, which is what this check does. It is reported so the
    # code vocabulary can be relied on elsewhere, but it does not hold the gate.
    if mislabelled:
        return result.warn(
            f"{len(mislabelled)} single-section document(s) are coded "
            f"{sorted({single_section[d][0]['section_code'] for d in mislabelled})} rather than "
            f"'full_document', so the fallback cannot be found from the section code alone"
        )
    if always:
        return result.warn(
            f"fallback rate {round(n_fallback / total_docs, 4) if total_docs else 0}; "
            f"{len(always)} company(ies) never section successfully: {', '.join(always[:10])}"
        )
    if n_fallback:
        return result.warn(
            f"{n_fallback} of {total_docs} documents fall back to a single section "
            f"({round(n_fallback / total_docs, 4)})"
        )
    return result.ok(f"no document falls back to a single section ({total_docs} documents)")


# ---------------------------------------------------------------------------
# Q17 -- chunk coverage of the parsed text
# ---------------------------------------------------------------------------


def check_q17(con, docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q17", "Chunk coverage of the parsed text, and where it stops")

    if not any(d["parsed_chars"] for d in docs):
        result.status = "SKIP"
        return result.ok(
            "the snapshot carries no parsed character counts; re-run checkpoint 0 without "
            "--no-text"
        )

    spans: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in con.execute(
        "SELECT doc_id, source_start_char, source_end_char FROM chunks "
        "WHERE source_start_char IS NOT NULL AND source_end_char IS NOT NULL"
    ):
        spans[row["doc_id"]].append((row["source_start_char"], row["source_end_char"]))

    rows = []
    for doc in docs:
        intervals = spans.get(doc["doc_id"], [])
        parsed = doc["parsed_chars"]
        if not intervals or not parsed:
            continue
        union = sum(end - start for start, end in merge_intervals(intervals))
        summed = sum(end - start for start, end in intervals)
        last_end = max(end for _, end in intervals)
        first_start = min(start for start, _ in intervals)
        coverage = union / parsed
        trailing_gap = (parsed - last_end) / parsed
        problems = []
        if coverage < COVERAGE_FLOOR:
            problems.append(f"coverage below {COVERAGE_FLOOR}")
        if coverage > COVERAGE_CEILING or last_end > parsed:
            problems.append("chunk offsets run past the parsed text")
        if trailing_gap > TRAILING_GAP_MAX:
            problems.append(f"trailing gap above {TRAILING_GAP_MAX}")
        rows.append({
            "doc_id": doc["doc_id"], "ticker": doc["ticker"],
            "report_year": doc["report_year"], "chunks": doc["chunk_count"],
            "parsed_chars": parsed,
            "covered_chars": union,
            "coverage": round(coverage, 4),
            "overlap_chars": summed - union,
            "leading_gap": round(first_start / parsed, 4),
            "trailing_gap": round(trailing_gap, 4),
            "last_chunk_ends_at": last_end,
            "problems": "; ".join(problems),
            "filepath": doc["filepath"],
        })

    flagged = [r for r in rows if r["problems"]]
    overruns = [r for r in flagged if "past the parsed text" in r["problems"]]
    truncations = [r for r in flagged if "trailing gap" in r["problems"]]

    flagged.sort(key=lambda r: -r["trailing_gap"])
    path = write_csv(out_dir / "chunk_coverage.csv", flagged,
                     ["doc_id", "ticker", "report_year", "chunks", "parsed_chars",
                      "covered_chars", "coverage", "overlap_chars", "leading_gap",
                      "trailing_gap", "last_chunk_ends_at", "problems", "filepath"])

    result.outputs = [str(path)]
    result.stats = {
        "documents_scored": len(rows),
        "thresholds": {"coverage_floor": COVERAGE_FLOOR, "coverage_ceiling": COVERAGE_CEILING,
                       "trailing_gap_max": TRAILING_GAP_MAX},
        "coverage": describe([r["coverage"] for r in rows]),
        "trailing_gap": describe([r["trailing_gap"] for r in rows]),
        "documents_flagged": len(flagged),
        "offsets_past_end_of_text": len(overruns),
        "trailing_truncations": len(truncations),
        "truncation_tickers": dict(Counter(r["ticker"] for r in truncations).most_common(10)),
    }
    result.examples = [
        {k: r[k] for k in ("doc_id", "ticker", "report_year", "coverage", "trailing_gap",
                           "problems")}
        for r in flagged[:examples_wanted]
    ]

    if overruns:
        return result.fail(
            f"{len(overruns)} document(s) have chunk offsets past the end of the parsed text; "
            f"their citations point outside the document"
        )
    if flagged:
        return result.warn(
            f"{len(flagged)} of {len(rows)} documents breach a coverage threshold "
            f"({len(truncations)} with a trailing gap above {TRAILING_GAP_MAX})"
        )
    return result.ok(f"all {len(rows)} documents covered within threshold")


# ---------------------------------------------------------------------------
# Q18 -- chunks per page band
# ---------------------------------------------------------------------------


def check_q18(docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q18", "Documents outside a plausible chunks-per-page band")

    scored = [
        {
            "doc_id": d["doc_id"], "ticker": d["ticker"], "report_year": d["report_year"],
            "chunks": d["chunk_count"], "pages": d["page_count"],
            "chunks_per_page": round((d["chunk_count"] or 0) / d["page_count"], 4),
            "parser_used": d["parser_used"] or "(not recorded)",
            "filepath": d["filepath"],
        }
        for d in docs if d.get("page_count")
    ]
    if not scored:
        result.status = "SKIP"
        return result.ok("no page counts in the snapshot; re-run checkpoint 0 without --no-text")

    values = [r["chunks_per_page"] for r in scored]
    low = percentile(values, BAND_LOW_PERCENTILE)
    high = percentile(values, BAND_HIGH_PERCENTILE)

    out_of_band = []
    for row in scored:
        if row["chunks_per_page"] < low:
            row["side"] = "below band"
        elif row["chunks_per_page"] > high:
            row["side"] = "above band"
        else:
            continue
        row["band_low"] = round(low, 4)
        row["band_high"] = round(high, 4)
        out_of_band.append(row)
    out_of_band.sort(key=lambda r: r["chunks_per_page"])

    path = write_csv(out_dir / "chunks_per_page_band.csv", out_of_band,
                     ["doc_id", "ticker", "report_year", "chunks", "pages", "chunks_per_page",
                      "band_low", "band_high", "side", "parser_used", "filepath"])

    result.outputs = [str(path)]
    result.stats = {
        "documents_scored": len(scored),
        "band_percentiles": [BAND_LOW_PERCENTILE, BAND_HIGH_PERCENTILE],
        "band": [round(low, 4), round(high, 4)],
        "chunks_per_page": describe(values),
        "out_of_band": len(out_of_band),
        "below_band": len([r for r in out_of_band if r["side"] == "below band"]),
        "above_band": len([r for r in out_of_band if r["side"] == "above band"]),
        "out_of_band_tickers": dict(Counter(r["ticker"] for r in out_of_band).most_common(10)),
    }
    result.examples = [
        {k: r[k] for k in ("doc_id", "ticker", "report_year", "chunks", "pages",
                           "chunks_per_page", "side")}
        for r in out_of_band[:examples_wanted]
    ]

    return result.warn(
        f"{len(out_of_band)} of {len(scored)} documents sit outside the "
        f"{round(low, 3)}-{round(high, 3)} chunks-per-page band; each needs a stated cause"
    ) if out_of_band else result.ok(f"all {len(scored)} documents inside the band")


# ---------------------------------------------------------------------------
# Q19 -- short_evidence concentration
# ---------------------------------------------------------------------------


def check_q19(docs: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q19", "short_evidence concentration across documents")

    total_chunks = sum(d["chunk_count"] or 0 for d in docs)
    total_short = sum(d["short_evidence_chunk_count"] or 0 for d in docs)

    scored = [
        {
            "doc_id": d["doc_id"], "ticker": d["ticker"], "report_year": d["report_year"],
            "chunks": d["chunk_count"], "sections": d["section_count"],
            "short_evidence_chunks": d["short_evidence_chunk_count"] or 0,
            "short_evidence_rate": round((d["short_evidence_chunk_count"] or 0)
                                         / d["chunk_count"], 4),
            "pages": d["page_count"], "filepath": d["filepath"],
        }
        for d in docs if (d["chunk_count"] or 0) >= SHORT_EVIDENCE_MIN_CHUNKS
    ]
    if not scored:
        result.status = "SKIP"
        return result.ok(
            f"no document has the {SHORT_EVIDENCE_MIN_CHUNKS} chunks needed for a meaningful rate"
        )

    rates = [r["short_evidence_rate"] for r in scored]
    cutoff = percentile(rates, 0.90)
    top_decile = [r for r in scored if r["short_evidence_rate"] >= cutoff and r[
        "short_evidence_chunks"]]
    top_decile.sort(key=lambda r: -r["short_evidence_rate"])
    for row in top_decile:
        row["decile_cutoff"] = round(cutoff, 4)

    path = write_csv(out_dir / "short_evidence_concentration.csv", top_decile,
                     ["doc_id", "ticker", "report_year", "chunks", "sections",
                      "short_evidence_chunks", "short_evidence_rate", "decile_cutoff",
                      "pages", "filepath"])

    companies = Counter(r["ticker"] for r in top_decile)
    share_top_5 = (
        sum(n for _, n in companies.most_common(5)) / len(top_decile) if top_decile else 0
    )

    result.outputs = [str(path)]
    result.stats = {
        "corpus_short_evidence_rate": (
            round(total_short / total_chunks, 4) if total_chunks else None),
        "corpus_short_evidence_chunks": f"{total_short} / {total_chunks}",
        "minimum_chunks_for_a_rate": SHORT_EVIDENCE_MIN_CHUNKS,
        "documents_scored": len(scored),
        "rate_distribution": describe(rates),
        "top_decile_cutoff": round(cutoff, 4),
        "top_decile_documents": len(top_decile),
        "distinct_companies_in_top_decile": len(companies),
        "top_companies": dict(companies.most_common(10)),
        "share_of_top_decile_in_top_5_companies": round(share_top_5, 4),
    }
    result.examples = [
        {k: r[k] for k in ("doc_id", "ticker", "report_year", "chunks",
                           "short_evidence_chunks", "short_evidence_rate")}
        for r in top_decile[:examples_wanted]
    ]

    if top_decile and share_top_5 > 0.5:
        return result.warn(
            f"{len(top_decile)} documents in the top decile, but {round(share_top_5 * 100)}% of "
            f"them belong to {min(5, len(companies))} companies -- a sectioning problem for "
            f"those companies rather than a corpus-wide chunking policy"
        )
    if top_decile:
        return result.warn(
            f"{len(top_decile)} documents above the {round(cutoff, 4)} decile cutoff, spread "
            f"over {len(companies)} companies"
        )
    return result.ok("no document carries a material short_evidence rate")


# ---------------------------------------------------------------------------
# Q20 -- numeric / tabular content, and what the gate kept
# ---------------------------------------------------------------------------


def check_q20(con, docs: list[dict], out_dir: Path, sample_size: int,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q20", "Numeric and tabular content, and what the gate kept")

    in_scope = {d["doc_id"]: d for d in docs}

    # Layout QA status lives in the vector index manifest, not in the database.
    layout_status = {}
    manifest_rows = read_csv(config.VECTOR_INDEX_MANIFEST_CSV)
    for row in manifest_rows:
        chunk_id = (row.get("chunk_id") or "").strip()
        if chunk_id:
            layout_status[chunk_id] = (row.get("layout_qa_status") or "").strip()

    per_chunk: list[tuple[int, float, int, str, str, str]] = []
    ratios_by_doc: dict[int, list[float]] = defaultdict(list)
    held_ratios: list[float] = []
    passed_ratios: list[float] = []
    held_tokens: list[float] = []
    passed_tokens: list[float] = []
    by_rag_action: dict[str, list[float]] = defaultdict(list)

    for row in con.execute(
        "SELECT chunk_id, external_chunk_id, doc_id, section_code, token_count, rag_action, "
        "chunk_text FROM chunks"
    ):
        if row["doc_id"] not in in_scope:
            continue
        text = row["chunk_text"] or ""
        if not text:
            continue
        ratio = 1.0 - (len(ALPHA_RE.findall(text)) / len(text))
        ratios_by_doc[row["doc_id"]].append(ratio)
        by_rag_action[row["rag_action"] or "(none)"].append(ratio)
        per_chunk.append((row["chunk_id"], ratio, row["token_count"] or 0,
                          row["section_code"] or "", row["rag_action"] or "",
                          text[:400]))
        status = layout_status.get((row["external_chunk_id"] or "").strip())
        if status == LAYOUT_HELD_STATUS:
            held_ratios.append(ratio)
            held_tokens.append(float(row["token_count"] or 0))
        elif status in LAYOUT_PASSED_STATUSES:
            passed_ratios.append(ratio)
            passed_tokens.append(float(row["token_count"] or 0))

    if not per_chunk:
        result.status = "SKIP"
        return result.ok("no chunk text to score")

    all_ratios = [r for _, r, *_ in per_chunk]
    numeric_cutoff = percentile(all_ratios, NUMERIC_TOP_PERCENTILE)

    doc_rows = []
    for doc_id, ratios in ratios_by_doc.items():
        numeric = [r for r in ratios if r >= numeric_cutoff]
        doc = in_scope[doc_id]
        doc_rows.append({
            "doc_id": doc_id, "ticker": doc["ticker"], "report_year": doc["report_year"],
            "chunks": len(ratios),
            "numeric_chunks": len(numeric),
            "numeric_share": round(len(numeric) / len(ratios), 4),
            "median_non_alpha_ratio": round(percentile(ratios, 0.5), 4),
            "max_non_alpha_ratio": round(max(ratios), 4),
            "filepath": doc["filepath"],
        })
    doc_rows.sort(key=lambda r: -r["numeric_share"])
    table_heavy = [r for r in doc_rows if r["numeric_share"] >= DOCUMENT_NUMERIC_SHARE_ALERT]

    # The review sample: the most numeric chunks, as stored, so a human can say
    # whether the tables survived as readable data or arrived as loose numbers.
    per_chunk.sort(key=lambda t: -t[1])
    sample_rows = [
        {
            "chunk_id": chunk_id,
            "doc_id": None,
            "non_alpha_ratio": round(ratio, 4),
            "token_count": tokens,
            "section_code": section,
            "rag_action": action,
            "text_first_400_chars": text.replace("\n", " "),
        }
        for chunk_id, ratio, tokens, section, action, text in per_chunk[:sample_size]
    ]

    paths = [
        write_csv(out_dir / "numeric_content.csv", doc_rows,
                  ["doc_id", "ticker", "report_year", "chunks", "numeric_chunks",
                   "numeric_share", "median_non_alpha_ratio", "max_non_alpha_ratio",
                   "filepath"]),
        write_csv(out_dir / "numeric_review_sample.csv", sample_rows,
                  ["chunk_id", "non_alpha_ratio", "token_count", "section_code", "rag_action",
                   "text_first_400_chars"]),
    ]

    gate_comparison: dict = {}
    if held_ratios and passed_ratios:
        gate_comparison = {
            "source": str(config.VECTOR_INDEX_MANIFEST_CSV),
            "held_chunks": len(held_ratios),
            "passed_chunks": len(passed_ratios),
            "held_non_alpha_ratio": describe(held_ratios),
            "passed_non_alpha_ratio": describe(passed_ratios),
            "held_tokens": describe(held_tokens),
            "passed_tokens": describe(passed_tokens),
            "held_minus_passed_median_ratio": round(
                (percentile(held_ratios, 0.5) or 0) - (percentile(passed_ratios, 0.5) or 0), 4
            ),
        }
    else:
        gate_comparison = {
            "status": "not available",
            "reason": f"layout QA manifest absent or carries no status: "
                      f"{config.VECTOR_INDEX_MANIFEST_CSV}",
            "stand_in": "non-alphabetic ratio by rag_action, which is a different gate",
            "by_rag_action": {
                action: describe(values) for action, values in sorted(by_rag_action.items())
            },
        }

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "chunks_scored": len(per_chunk),
        "non_alpha_ratio": describe(all_ratios),
        f"numeric_cutoff_p{int(NUMERIC_TOP_PERCENTILE * 100)}": round(numeric_cutoff, 4),
        "documents_scored": len(doc_rows),
        "table_heavy_documents": len(table_heavy),
        "table_heavy_alert_share": DOCUMENT_NUMERIC_SHARE_ALERT,
        "most_numeric_documents": [
            {k: r[k] for k in ("ticker", "report_year", "numeric_share", "chunks")}
            for r in doc_rows[:5]
        ],
        "layout_gate_comparison": gate_comparison,
        "review_sample_rows": len(sample_rows),
    }
    result.examples = [
        {k: r[k] for k in ("chunk_id", "non_alpha_ratio", "section_code",
                           "text_first_400_chars")}
        for r in sample_rows[:examples_wanted]
    ]

    if not held_ratios or not passed_ratios:
        return result.warn(
            f"{len(per_chunk)} chunks scored, but the layout QA manifest is unavailable, so "
            f"whether auto_hold removed the tables cannot be answered here (Checkpoint 5 Q27 "
            f"owns it)"
        )
    if gate_comparison["held_minus_passed_median_ratio"] > 0:
        return result.fail(
            f"held chunks are more numeric than passed chunks (median non-alphabetic ratio "
            f"+{gate_comparison['held_minus_passed_median_ratio']}); the gate is removing "
            f"quantitative content"
        )
    return result.ok(
        f"{len(per_chunk)} chunks scored; held and passed populations do not differ in the "
        f"direction that would indicate lost tables"
    )


# ---------------------------------------------------------------------------
# Q21 -- boilerplate, headers, navigation
# ---------------------------------------------------------------------------


def minhash_signature(text: str, permutations: int) -> tuple[int, ...] | None:
    """MinHash over word shingles, with permutations drawn from a fixed seed."""
    words = text.split()
    if len(words) < NEAR_DUP_SHINGLE:
        return None
    shingles = {
        hash(" ".join(words[i:i + NEAR_DUP_SHINGLE]))
        for i in range(len(words) - NEAR_DUP_SHINGLE + 1)
    }
    signature = []
    for p in range(permutations):
        a = (2 * p + 1) * 0x9E3779B97F4A7C15
        b = (p + 1) * 0xBF58476D1CE4E5B9
        signature.append(min(((h * a + b) & MASK64) for h in shingles))
    return tuple(signature)


def check_q21(con, docs: list[dict], out_dir: Path, sample_size: int,
              examples_wanted: int) -> CheckResult:
    result = CheckResult("Q21", "Boilerplate, headers and navigation in the index")

    in_scope = {d["doc_id"]: d for d in docs}
    groups: dict[str, list[dict]] = defaultdict(list)
    total = 0
    for row in con.execute(
        "SELECT chunk_id, doc_id, company_id, chunk_text FROM chunks ORDER BY chunk_id"
    ):
        if row["doc_id"] not in in_scope:
            continue
        text = normalise(row["chunk_text"])
        if not text:
            continue
        total += 1
        doc = in_scope[row["doc_id"]]
        groups[hashlib.sha256(text.encode("utf-8")).hexdigest()].append({
            "chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
            "ticker": doc["ticker"], "report_year": doc["report_year"],
            "length": len(text), "text": text,
        })

    duplicate_rows = []
    partition_counts = Counter()
    duplicate_chunks = 0
    for digest, members in groups.items():
        if len(members) < 2:
            continue
        tickers = {m["ticker"] for m in members}
        docs_in = {m["doc_id"] for m in members}
        years = {m["report_year"] for m in members}
        if len(tickers) > 1:
            partition = "cross_company"
        elif len(docs_in) > 1 or len(years) > 1:
            partition = "within_company_across_documents"
        else:
            partition = "within_document"
        partition_counts[partition] += len(members) - 1
        duplicate_chunks += len(members) - 1
        duplicate_rows.append({
            "partition": partition,
            "text_sha256": digest[:16],
            "copies": len(members),
            "companies": len(tickers),
            "documents": len(docs_in),
            "tickers": "|".join(sorted(tickers)[:10]),
            "report_years": "|".join(sorted(str(y) for y in years)[:10]),
            "chars": members[0]["length"],
            "text_first_200_chars": members[0]["text"][:200],
        })

    duplicate_rows.sort(key=lambda r: -r["copies"])
    repeated = [r for r in duplicate_rows if r["copies"] >= BOILERPLATE_MIN_REPEATS]

    paths = [
        write_csv(out_dir / "duplicate_text.csv", duplicate_rows,
                  ["partition", "text_sha256", "copies", "companies", "documents", "tickers",
                   "report_years", "chars", "text_first_200_chars"]),
        write_csv(out_dir / "repeated_strings.csv", duplicate_rows[:25],
                  ["partition", "copies", "companies", "documents", "chars",
                   "text_first_200_chars"]),
    ]

    # Near-duplicates on a deterministic stride sample: repeated headers and
    # footers that differ by a page number never collide on an exact hash.
    near_dup: dict = {"status": "skipped (--near-dup-sample 0)"}
    if sample_size:
        population = [m for members in groups.values() for m in members]
        stride = max(1, len(population) // sample_size)
        sample = population[::stride][:sample_size]
        buckets: dict[tuple, set[int]] = defaultdict(set)
        signatures: dict[int, tuple[int, ...]] = {}
        rows_per_band = max(1, NEAR_DUP_PERMUTATIONS // NEAR_DUP_BANDS)
        for index, member in enumerate(sample):
            signature = minhash_signature(member["text"], NEAR_DUP_PERMUTATIONS)
            if signature is None:
                continue
            signatures[index] = signature
            for band in range(NEAR_DUP_BANDS):
                key = (band,) + signature[band * rows_per_band:(band + 1) * rows_per_band]
                buckets[key].add(index)
        candidate_pairs = set()
        for bucket in buckets.values():
            if 1 < len(bucket) <= 50:      # a bucket of hundreds is boilerplate, already counted
                members_ = sorted(bucket)
                for i, a in enumerate(members_):
                    for b in members_[i + 1:]:
                        candidate_pairs.add((a, b))
        near_pairs = []
        for a, b in candidate_pairs:
            sig_a, sig_b = signatures[a], signatures[b]
            estimate = sum(1 for x, y in zip(sig_a, sig_b) if x == y) / NEAR_DUP_PERMUTATIONS
            if estimate >= NEAR_DUP_THRESHOLD and sample[a]["text"] != sample[b]["text"]:
                near_pairs.append((estimate, sample[a], sample[b]))
        near_dup = {
            "sampled_chunks": len(signatures),
            "stride": stride,
            "threshold": NEAR_DUP_THRESHOLD,
            "candidate_pairs": len(candidate_pairs),
            "near_duplicate_pairs": len(near_pairs),
            "estimated_rate_in_sample": (
                round(len(near_pairs) / len(signatures), 4) if signatures else None),
            "cross_company_pairs": sum(
                1 for _, a, b in near_pairs if a["ticker"] != b["ticker"]),
        }

    cross_company_chunks = partition_counts.get("cross_company", 0)
    cross_company_share = cross_company_chunks / total if total else 0

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "chunks_scored": total,
        "distinct_texts": len(groups),
        "duplicate_groups": len(duplicate_rows),
        "duplicate_chunks_beyond_first_copy": duplicate_chunks,
        "duplicate_rate": round(duplicate_chunks / total, 4) if total else None,
        "by_partition": dict(partition_counts),
        "cross_company_share_of_index": round(cross_company_share, 4),
        "strings_repeated_at_least_n_times": {
            "n": BOILERPLATE_MIN_REPEATS, "groups": len(repeated),
        },
        "near_duplicates": near_dup,
    }
    result.examples = [
        {k: r[k] for k in ("partition", "copies", "companies", "text_first_200_chars")}
        for r in duplicate_rows[:examples_wanted]
    ]

    if cross_company_share > CROSS_COMPANY_DUP_ALERT:
        return result.fail(
            f"cross-company duplicate text is {round(cross_company_share * 100, 2)}% of the "
            f"index ({cross_company_chunks} chunks); a query can match a disclaimer shared by "
            f"unrelated companies"
        )
    if duplicate_rows:
        return result.warn(
            f"{duplicate_chunks} duplicate chunks beyond the first copy "
            f"({round(duplicate_chunks / total * 100, 2)}% of the index), "
            f"{cross_company_chunks} of them cross-company"
        )
    return result.ok(f"no exact duplicate text across {total} chunks")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], header: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 3 -- sectioning and chunking behaviour")
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
    print(f"Checkpoint 3 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when every threshold above was recorded before it was applied "
        "and every document breaching one is either explained or ticketed."
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
                        help="output directory (default: <snapshot>/checkpoint3/)")
    parser.add_argument("--include-zero-chunk", action="store_true",
                        help="keep zero-chunk documents in the distributions "
                             "(by default they are held out; Checkpoint 1 Q9 owns them)")
    parser.add_argument("--review-sample", type=int, default=20,
                        help="rows in the Q20 numeric review sample (default: 20)")
    parser.add_argument("--near-dup-sample", type=int, default=4000,
                        help="chunks to sample for the Q21 near-duplicate pass, 0 to skip "
                             "(default: 4000)")
    parser.add_argument("--allow-db-drift", action="store_true",
                        help="continue with a warning when the database no longer matches the "
                             "snapshot's SHA-256 (by default this is fatal)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint3.json)")
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

    out_dir = args.out_dir or (snapshot_dir / "checkpoint3")
    out_dir.mkdir(parents=True, exist_ok=True)

    zero_chunk = [d for d in manifest if not (d["chunk_count"] or 0)]
    docs = manifest if args.include_zero_chunk else [
        d for d in manifest if (d["chunk_count"] or 0) > 0
    ]
    population = (
        f"{len(docs)} documents"
        + (f" ({len(zero_chunk)} zero-chunk documents held out; see checkpoint 1 Q9)"
           if not args.include_zero_chunk and zero_chunk else "")
    )

    con = connect(db_path)
    try:
        results = [
            check_q16(con, docs, out_dir, args.examples),
            check_q17(con, docs, out_dir, args.examples),
            check_q18(docs, out_dir, args.examples),
            check_q19(docs, out_dir, args.examples),
            check_q20(con, docs, out_dir, args.review_sample, args.examples),
            check_q21(con, docs, out_dir, args.near_dup_sample, args.examples),
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
        "out_dir": str(out_dir),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        **header,
        "thresholds": {
            "coverage_floor": COVERAGE_FLOOR,
            "coverage_ceiling": COVERAGE_CEILING,
            "trailing_gap_max": TRAILING_GAP_MAX,
            "band_percentiles": [BAND_LOW_PERCENTILE, BAND_HIGH_PERCENTILE],
            "short_evidence_min_chunks": SHORT_EVIDENCE_MIN_CHUNKS,
            "numeric_top_percentile": NUMERIC_TOP_PERCENTILE,
            "document_numeric_share_alert": DOCUMENT_NUMERIC_SHARE_ALERT,
            "boilerplate_min_repeats": BOILERPLATE_MIN_REPEATS,
            "near_dup_threshold": NEAR_DUP_THRESHOLD,
            "cross_company_dup_alert": CROSS_COMPANY_DUP_ALERT,
        },
        "results": [
            {"question": r.key, "title": r.title, "status": r.status, "headline": r.headline,
             "stats": r.stats, "examples": r.examples, "outputs": r.outputs}
            for r in results
        ],
    }
    (out_dir / "checkpoint3.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    render(results, header)
    print(f"result set written to {out_dir / 'checkpoint3.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
