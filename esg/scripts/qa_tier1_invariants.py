"""Tier 1 mathematical QA: structural invariants of the ESG corpus.

These seven checks have provably correct answers. Unlike the distributional
tiers, any violation here is a defect rather than a judgement call, so this
script is the gate that runs before any distributional statistic is trusted --
if sections and chunks do not tile their parents correctly, every downstream
mean, histogram, and regression is computed over corrupted spans.

Everything is READ-ONLY. The database is opened with mode=ro and no file under
data/ is written. The only output is a report on stdout and, optionally, JSON.

Checks
    1  sections tile their parent document   (coverage / gaps / overlap)
    1b section_text == parsed_text[start:end] (the documented span invariant)
    2  section intervals are disjoint within a document
    3  chunks tile their parent section
    4  chunk spans are contained in their section's span
    5  page ranges are ordered and within the document's page count
    6  char-offset order agrees with page order (Kendall's tau-b)
    7  provenance cardinalities and identity fan-out

Checks 1, 1b, 5 and 6 need the parsed text and page maps under
data/02_interim/esg_text/, because the database stores chunk text but not the
parsed-document ground truth the offsets are defined against. Documents whose
parsed text is missing are reported as SKIPPED, never silently passed.

Usage
    python esg/scripts/qa_tier1_invariants.py
    python esg/scripts/qa_tier1_invariants.py --checks 1,2,3
    python esg/scripts/qa_tier1_invariants.py --json-out reports/qa_tier1.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

csv.field_size_limit(10**9)

ALL_CHECKS = ["1", "1b", "2", "3", "4", "5", "6", "7"]

# A document may legitimately leave front matter or a TOC unsectioned, so a
# coverage ratio below 1.0 is reported rather than failed. Below this floor the
# splitter has almost certainly lost real content and the doc is flagged.
LOW_COVERAGE_FLOOR = 0.50


# ---------------------------------------------------------------------------
# result plumbing
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    key: str
    title: str
    status: str = "PASS"          # PASS | FAIL | WARN | SKIP
    headline: str = ""
    stats: dict = field(default_factory=dict)
    examples: list = field(default_factory=list)

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


# ---------------------------------------------------------------------------
# small numeric helpers (no numpy/scipy dependency)
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile; p in [0, 1]."""
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
        "min": round(min(values), 6),
        "p05": round(percentile(values, 0.05), 6),
        "median": round(percentile(values, 0.50), 6),
        "mean": round(sum(values) / len(values), 6),
        "p95": round(percentile(values, 0.95), 6),
        "max": round(max(values), 6),
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


def span_metrics(intervals: list[tuple[int, int]]) -> tuple[int, int, int]:
    """Return (summed_length, union_length, overlap_length)."""
    total = sum(end - start for start, end in intervals)
    union = sum(end - start for start, end in merge_intervals(intervals))
    return total, union, total - union


class _Fenwick:
    """Prefix-sum tree used for O(n log n) discordant-pair counting."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.tree = [0] * (size + 1)

    def add(self, index: int) -> None:
        i = index + 1
        while i <= self.size:
            self.tree[i] += 1
            i += i & -i

    def prefix(self, index: int) -> int:
        i, total = index + 1, 0
        while i > 0:
            total += self.tree[i]
            i -= i & -i
        return total


def kendall_tau_b(xs: list[int], ys: list[int]) -> tuple[float | None, int]:
    """Kendall's tau-b with tie correction, plus the raw discordant-pair count.

    Ties matter here: many chunks share a page_start, so the uncorrected tau-a
    would understate agreement badly.
    """
    n = len(xs)
    if n < 2:
        return None, 0

    order = sorted(range(n), key=lambda i: (xs[i], ys[i]))
    ranks = {value: i for i, value in enumerate(sorted(set(ys)))}
    tree = _Fenwick(len(ranks))

    discordant = 0
    inserted = 0
    i = 0
    while i < n:
        j = i
        while j < n and xs[order[j]] == xs[order[i]]:
            j += 1
        # Compare this x-tie group only against strictly smaller x values.
        for k in range(i, j):
            rank = ranks[ys[order[k]]]
            discordant += inserted - tree.prefix(rank)
        for k in range(i, j):
            tree.add(ranks[ys[order[k]]])
            inserted += 1
        i = j

    def tie_pairs(counter: Counter) -> int:
        return sum(c * (c - 1) // 2 for c in counter.values())

    n0 = n * (n - 1) // 2
    n1 = tie_pairs(Counter(xs))
    n2 = tie_pairs(Counter(ys))
    n3 = tie_pairs(Counter(zip(xs, ys)))
    concordant = n0 - n1 - n2 + n3 - discordant

    denominator = math.sqrt((n0 - n1) * (n0 - n2))
    tau = (concordant - discordant) / denominator if denominator > 0 else None
    return tau, discordant


# ---------------------------------------------------------------------------
# corpus access
# ---------------------------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_documents(con: sqlite3.Connection) -> dict[int, dict]:
    """doc_id -> {ticker, stem, filepath, parse_status} for offset resolution."""
    rows = con.execute(
        """
        SELECT d.doc_id, d.filepath, d.parse_status, d.doc_type, k.ticker
        FROM documents d JOIN companies k ON k.company_id = d.company_id
        """
    ).fetchall()
    return {
        r["doc_id"]: {
            "ticker": (r["ticker"] or "").upper(),
            "stem": Path(r["filepath"] or "").stem,
            "filepath": r["filepath"],
            "parse_status": r["parse_status"],
            "doc_type": r["doc_type"],
        }
        for r in rows
    }


def parsed_text_path(root: Path, doc: dict) -> Path:
    return root / doc["ticker"] / f"{doc['stem']}.txt"


def page_map_path(root: Path, doc: dict) -> Path:
    return root / doc["ticker"] / f"{doc['stem']}.pages.csv"


def read_parsed_text(path: Path) -> str | None:
    """Match how drive_to_db.py read section files, so offsets line up."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def read_page_count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        pages = [row for row in csv.DictReader(handle) if row.get("page")]
    if not pages:
        return None
    return max(int(row["page"]) for row in pages)


# ---------------------------------------------------------------------------
# check 1 / 1b -- sections tile the document, and their text matches the slice
# ---------------------------------------------------------------------------


def check_1_and_1b(con, docs, text_root, examples_wanted):
    tiling = CheckResult("1", "Sections tile their parent document")
    fidelity = CheckResult("1b", "section_text == parsed_text[start:end]")

    by_doc: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in con.execute(
        """
        SELECT doc_id, section_id, section_instance_id, section_code,
               source_start_char, source_end_char, char_count, section_text
        FROM sections
        """
    ):
        by_doc[row["doc_id"]].append(row)

    coverage_ratios: list[float] = []
    overlap_docs, low_coverage_docs, skipped = [], [], []
    null_offsets = 0
    mismatches, checked_sections = [], 0

    for doc_id, sections in by_doc.items():
        doc = docs.get(doc_id)
        if doc is None:
            continue
        text = read_parsed_text(parsed_text_path(text_root, doc))
        if text is None:
            skipped.append({"doc_id": doc_id, "ticker": doc["ticker"], "stem": doc["stem"]})
            continue
        parsed_len = len(text)

        intervals = []
        for section in sections:
            start, end = section["source_start_char"], section["source_end_char"]
            if start is None or end is None:
                null_offsets += 1
                continue
            intervals.append((int(start), int(end)))

            # 1b: the documented half-open span invariant.
            checked_sections += 1
            if text[int(start):int(end)] != (section["section_text"] or ""):
                if len(mismatches) < examples_wanted:
                    mismatches.append(
                        {
                            "ticker": doc["ticker"],
                            "stem": doc["stem"],
                            "section_instance_id": section["section_instance_id"],
                            "declared_span": [int(start), int(end)],
                            "span_len": int(end) - int(start),
                            "stored_len": len(section["section_text"] or ""),
                        }
                    )

        if not intervals or parsed_len == 0:
            continue
        total, union, overlap = span_metrics(intervals)
        ratio = union / parsed_len
        coverage_ratios.append(ratio)

        if overlap > 0 and len(overlap_docs) < examples_wanted:
            overlap_docs.append(
                {
                    "ticker": doc["ticker"],
                    "stem": doc["stem"],
                    "overlap_chars": overlap,
                    "summed_spans": total,
                    "union_spans": union,
                }
            )
        if ratio < LOW_COVERAGE_FLOOR and len(low_coverage_docs) < examples_wanted:
            low_coverage_docs.append(
                {
                    "ticker": doc["ticker"],
                    "stem": doc["stem"],
                    "coverage_ratio": round(ratio, 4),
                    "uncovered_chars": parsed_len - union,
                }
            )

    tiling.stats = {
        "documents_measured": len(coverage_ratios),
        "documents_skipped_missing_parsed_text": len(skipped),
        "sections_with_null_offsets": null_offsets,
        "coverage_ratio": describe(coverage_ratios),
        "documents_with_overlap": len(overlap_docs),
        "documents_below_coverage_floor": len(low_coverage_docs),
        "coverage_floor": LOW_COVERAGE_FLOOR,
    }
    tiling.examples = {"overlapping": overlap_docs, "low_coverage": low_coverage_docs,
                       "skipped": skipped[:examples_wanted]}
    if overlap_docs:
        tiling.fail(f"{len(overlap_docs)}+ documents have overlapping section spans")
    elif low_coverage_docs:
        tiling.warn(f"{len(low_coverage_docs)}+ documents below {LOW_COVERAGE_FLOOR:.0%} coverage")
    elif not coverage_ratios:
        tiling.status = "SKIP"
        tiling.headline = "no document had readable parsed text"
    else:
        tiling.ok(f"no overlaps; median coverage {percentile(coverage_ratios, 0.5):.3f}")

    fidelity.stats = {
        "sections_checked": checked_sections,
        "mismatches_found": len(mismatches),
        "note": "examples capped; rerun with --examples for more",
    }
    fidelity.examples = mismatches
    if mismatches:
        fidelity.fail("section_text does not equal its declared parsed-text slice")
    elif checked_sections == 0:
        fidelity.status = "SKIP"
        fidelity.headline = "no sections could be checked (parsed text missing)"
    else:
        fidelity.ok(f"all {checked_sections:,} checked sections match their slice")

    return tiling, fidelity


# ---------------------------------------------------------------------------
# check 2 -- section intervals disjoint within a document
# ---------------------------------------------------------------------------


def check_2(con, docs, examples_wanted):
    result = CheckResult("2", "Section intervals are disjoint within a document")

    by_doc: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    null_offsets = 0
    for row in con.execute(
        """
        SELECT doc_id, section_instance_id, source_start_char, source_end_char
        FROM sections
        """
    ):
        if row["source_start_char"] is None or row["source_end_char"] is None:
            null_offsets += 1
            continue
        by_doc[row["doc_id"]].append(
            (int(row["source_start_char"]), int(row["source_end_char"]),
             row["section_instance_id"])
        )

    overlapping_pairs = 0
    inverted_spans = 0
    docs_affected = 0
    examples = []

    for doc_id, spans in by_doc.items():
        spans.sort()
        doc = docs.get(doc_id, {})
        doc_hit = False
        for (s1, e1, id1), (s2, e2, id2) in zip(spans, spans[1:]):
            if e1 > s2:
                overlapping_pairs += 1
                doc_hit = True
                if len(examples) < examples_wanted:
                    examples.append(
                        {
                            "ticker": doc.get("ticker"),
                            "stem": doc.get("stem"),
                            "a": {"id": id1, "span": [s1, e1]},
                            "b": {"id": id2, "span": [s2, e2]},
                            "overlap_chars": e1 - s2,
                        }
                    )
        inverted_spans += sum(1 for start, end, _ in spans if end < start)
        if doc_hit:
            docs_affected += 1

    result.stats = {
        "documents_examined": len(by_doc),
        "sections_with_null_offsets": null_offsets,
        "overlapping_adjacent_pairs": overlapping_pairs,
        "documents_affected": docs_affected,
        "inverted_spans_end_before_start": inverted_spans,
    }
    result.examples = examples
    if overlapping_pairs or inverted_spans:
        result.fail(
            f"{overlapping_pairs} overlapping pairs across {docs_affected} documents, "
            f"{inverted_spans} inverted spans"
        )
    else:
        result.ok(f"all sections disjoint across {len(by_doc)} documents")
    return result


# ---------------------------------------------------------------------------
# check 3 -- chunks tile their section
# ---------------------------------------------------------------------------


def check_3(con, docs, examples_wanted):
    result = CheckResult("3", "Chunks tile their parent section")

    sections = {
        row["section_id"]: row
        for row in con.execute(
            """
            SELECT section_id, doc_id, section_instance_id, char_count,
                   source_start_char, source_end_char
            FROM sections
            """
        )
    }

    by_section: dict[int, list[tuple[int, int]]] = defaultdict(list)
    null_offsets = 0
    for row in con.execute(
        "SELECT section_id, source_start_char, source_end_char FROM chunks"
    ):
        if row["source_start_char"] is None or row["source_end_char"] is None:
            null_offsets += 1
            continue
        by_section[row["section_id"]].append(
            (int(row["source_start_char"]), int(row["source_end_char"]))
        )

    ratios, overlaps, low_coverage = [], [], []
    sections_without_chunks = 0

    for section_id, section in sections.items():
        spans = by_section.get(section_id)
        if not spans:
            sections_without_chunks += 1
            continue
        char_count = section["char_count"] or 0
        if char_count <= 0:
            continue
        total, union, overlap = span_metrics(spans)
        ratios.append(union / char_count)
        doc = docs.get(section["doc_id"], {})
        if overlap > 0 and len(overlaps) < examples_wanted:
            overlaps.append(
                {
                    "ticker": doc.get("ticker"),
                    "stem": doc.get("stem"),
                    "section_instance_id": section["section_instance_id"],
                    "overlap_chars": overlap,
                }
            )
        if union / char_count < LOW_COVERAGE_FLOOR and len(low_coverage) < examples_wanted:
            low_coverage.append(
                {
                    "ticker": doc.get("ticker"),
                    "stem": doc.get("stem"),
                    "section_instance_id": section["section_instance_id"],
                    "coverage_ratio": round(union / char_count, 4),
                    "uncovered_chars": char_count - union,
                }
            )

    result.stats = {
        "sections_with_chunks": len(ratios),
        "sections_without_chunks": sections_without_chunks,
        "chunks_with_null_offsets": null_offsets,
        "coverage_ratio": describe(ratios),
        "sections_with_overlap": len(overlaps),
        "sections_below_coverage_floor": len(low_coverage),
    }
    result.examples = {"overlapping": overlaps, "low_coverage": low_coverage}
    if overlaps:
        result.fail(f"{len(overlaps)}+ sections have overlapping chunk spans")
    elif low_coverage:
        result.warn(f"{len(low_coverage)}+ sections below {LOW_COVERAGE_FLOOR:.0%} chunk coverage")
    else:
        result.ok(f"no overlaps; median coverage {percentile(ratios, 0.5):.3f}"
                  if ratios else "no measurable sections")
    return result


# ---------------------------------------------------------------------------
# check 4 -- chunk spans contained in their section's span
# ---------------------------------------------------------------------------


def check_4(con, examples_wanted):
    result = CheckResult("4", "Chunk spans are contained in their section's span")

    rows = con.execute(
        """
        SELECT k.ticker, d.filepath, s.section_instance_id,
               c.chunk_index, c.external_chunk_id,
               c.source_start_char AS c_start, c.source_end_char AS c_end,
               s.source_start_char AS s_start, s.source_end_char AS s_end
        FROM chunks c
        JOIN sections s ON s.section_id = c.section_id
        JOIN documents d ON d.doc_id = c.doc_id
        JOIN companies k ON k.company_id = c.company_id
        WHERE c.source_start_char IS NOT NULL AND c.source_end_char IS NOT NULL
          AND s.source_start_char IS NOT NULL AND s.source_end_char IS NOT NULL
          AND (c.source_start_char < s.source_start_char
               OR c.source_end_char > s.source_end_char
               OR c.source_end_char < c.source_start_char)
        """
    ).fetchall()

    total_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    orphan_offsets = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE source_start_char IS NULL OR source_end_char IS NULL"
    ).fetchone()[0]

    result.stats = {
        "chunks_total": total_chunks,
        "chunks_with_null_offsets": orphan_offsets,
        "containment_violations": len(rows),
        "violation_rate": round(len(rows) / total_chunks, 8) if total_chunks else None,
    }
    result.examples = [
        {
            "ticker": r["ticker"],
            "stem": Path(r["filepath"] or "").stem,
            "section_instance_id": r["section_instance_id"],
            "chunk_index": r["chunk_index"],
            "chunk_span": [r["c_start"], r["c_end"]],
            "section_span": [r["s_start"], r["s_end"]],
        }
        for r in rows[:examples_wanted]
    ]
    if rows:
        result.fail(f"{len(rows)} chunks fall outside their section's declared span")
    else:
        result.ok(f"all {total_chunks - orphan_offsets:,} offset-bearing chunks contained")
    return result


# ---------------------------------------------------------------------------
# check 5 -- page range sanity
# ---------------------------------------------------------------------------


def check_5(con, docs, text_root, examples_wanted):
    result = CheckResult("5", "Page ranges are ordered and within the document")

    inverted = con.execute(
        """
        SELECT COUNT(*) FROM chunks
        WHERE page_start IS NOT NULL AND page_end IS NOT NULL AND page_end < page_start
        """
    ).fetchone()[0]
    non_positive = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE page_start IS NOT NULL AND page_start < 1"
    ).fetchone()[0]
    null_pages = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE page_start IS NULL OR page_end IS NULL"
    ).fetchone()[0]

    # Upper bound needs the page map, which lives on disk.
    page_counts: dict[int, int] = {}
    missing_maps = 0
    for doc_id, doc in docs.items():
        count = read_page_count(page_map_path(text_root, doc))
        if count is None:
            missing_maps += 1
        else:
            page_counts[doc_id] = count

    beyond_end = []
    spans = []
    for row in con.execute(
        """
        SELECT c.doc_id, c.chunk_index, c.page_start, c.page_end, s.section_instance_id
        FROM chunks c JOIN sections s ON s.section_id = c.section_id
        WHERE c.page_start IS NOT NULL AND c.page_end IS NOT NULL
        """
    ):
        limit = page_counts.get(row["doc_id"])
        spans.append(row["page_end"] - row["page_start"] + 1)
        if limit is not None and row["page_end"] > limit:
            if len(beyond_end) < examples_wanted:
                doc = docs.get(row["doc_id"], {})
                beyond_end.append(
                    {
                        "ticker": doc.get("ticker"),
                        "stem": doc.get("stem"),
                        "section_instance_id": row["section_instance_id"],
                        "chunk_index": row["chunk_index"],
                        "page_range": [row["page_start"], row["page_end"]],
                        "document_pages": limit,
                    }
                )

    result.stats = {
        "chunks_with_null_pages": null_pages,
        "inverted_page_ranges": inverted,
        "non_positive_page_start": non_positive,
        "chunks_beyond_document_end": len(beyond_end),
        "documents_missing_page_map": missing_maps,
        "pages_spanned_per_chunk": describe([float(s) for s in spans]),
    }
    result.examples = beyond_end
    if inverted or non_positive or beyond_end:
        result.fail(
            f"{inverted} inverted, {non_positive} non-positive, "
            f"{len(beyond_end)}+ beyond document end"
        )
    else:
        result.ok("all page ranges ordered, positive, and within bounds")
    return result


# ---------------------------------------------------------------------------
# check 6 -- char offset order agrees with page order
# ---------------------------------------------------------------------------


def check_6(con, docs, examples_wanted):
    result = CheckResult("6", "Char-offset order agrees with page order (tau-b)")

    by_doc: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in con.execute(
        """
        SELECT doc_id, source_start_char, page_start FROM chunks
        WHERE source_start_char IS NOT NULL AND page_start IS NOT NULL
        """
    ):
        by_doc[row["doc_id"]].append((int(row["source_start_char"]), int(row["page_start"])))

    taus, worst = [], []
    total_discordant = 0
    for doc_id, pairs in by_doc.items():
        if len(pairs) < 2:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        tau, discordant = kendall_tau_b(xs, ys)
        total_discordant += discordant
        if tau is None:
            continue
        taus.append(tau)
        doc = docs.get(doc_id, {})
        worst.append(
            {
                "ticker": doc.get("ticker"),
                "stem": doc.get("stem"),
                "tau_b": round(tau, 4),
                "discordant_pairs": discordant,
                "chunks": len(pairs),
            }
        )

    worst.sort(key=lambda item: item["tau_b"])
    below_one = [t for t in taus if t < 0.999]

    result.stats = {
        "documents_measured": len(taus),
        "total_discordant_pairs": total_discordant,
        "documents_with_tau_below_1": len(below_one),
        "tau_b": describe(taus),
    }
    result.examples = worst[:examples_wanted]
    if taus and min(taus) < 0.90:
        result.fail(f"lowest tau-b {min(taus):.3f}; reading order likely corrupted")
    elif below_one:
        result.warn(f"{len(below_one)} documents have tau-b < 1.0")
    elif not taus:
        result.status = "SKIP"
        result.headline = "no document had comparable offset/page pairs"
    else:
        result.ok("page order perfectly agrees with char offsets everywhere")
    return result


# ---------------------------------------------------------------------------
# check 7 -- provenance cardinality and fan-out
# ---------------------------------------------------------------------------


def check_7(con, examples_wanted):
    result = CheckResult("7", "Provenance cardinalities and identity fan-out")

    def count(table: str) -> int:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    counts = {
        t: count(t)
        for t in (
            "companies", "documents", "sections", "chunks",
            "logical_sources", "source_versions", "extraction_artifacts",
            "file_aliases", "source_approvals", "sustainability_reports",
        )
    }

    docs_without_provenance = con.execute(
        """
        SELECT k.ticker, d.filepath, d.parse_status, d.doc_quality_status
        FROM documents d JOIN companies k ON k.company_id = d.company_id
        WHERE d.logical_source_id IS NULL OR d.source_version_id IS NULL
           OR d.extraction_artifact_id IS NULL
        """
    ).fetchall()

    # Fan-out: the corpus is expected to be 1:1:1 today, so anything above 1
    # is a real (if legitimate) multi-version source worth seeing.
    versions_per_source = Counter(
        r[0] for r in con.execute("SELECT logical_source_id FROM source_versions")
    )
    artifacts_per_version = Counter(
        r[0] for r in con.execute("SELECT source_version_id FROM extraction_artifacts")
    )
    aliases_per_version = Counter(
        r[0] for r in con.execute("SELECT source_version_id FROM file_aliases")
    )

    dangling = {
        "source_versions_without_logical_source": con.execute(
            """SELECT COUNT(*) FROM source_versions v LEFT JOIN logical_sources l
               ON l.logical_source_id = v.logical_source_id
               WHERE l.logical_source_id IS NULL"""
        ).fetchone()[0],
        "artifacts_without_source_version": con.execute(
            """SELECT COUNT(*) FROM extraction_artifacts a LEFT JOIN source_versions v
               ON v.source_version_id = a.source_version_id
               WHERE v.source_version_id IS NULL"""
        ).fetchone()[0],
        "file_aliases_without_source_version": con.execute(
            """SELECT COUNT(*) FROM file_aliases f LEFT JOIN source_versions v
               ON v.source_version_id = f.source_version_id
               WHERE v.source_version_id IS NULL"""
        ).fetchone()[0],
        "source_versions_without_original_sha256": con.execute(
            "SELECT COUNT(*) FROM source_versions WHERE original_sha256 IS NULL"
        ).fetchone()[0],
    }

    result.stats = {
        "row_counts": counts,
        "documents_missing_provenance": len(docs_without_provenance),
        "identity_gap_documents_minus_logical_sources":
            counts["documents"] - counts["logical_sources"],
        "identity_gap_file_aliases_minus_logical_sources":
            counts["file_aliases"] - counts["logical_sources"],
        "versions_per_logical_source": dict(Counter(versions_per_source.values())),
        "artifacts_per_source_version": dict(Counter(artifacts_per_version.values())),
        "aliases_per_source_version": dict(Counter(aliases_per_version.values())),
        "dangling_references": dangling,
    }
    result.examples = [
        {
            "ticker": r["ticker"],
            "stem": Path(r["filepath"] or "").stem,
            "parse_status": r["parse_status"],
            "doc_quality_status": r["doc_quality_status"],
        }
        for r in docs_without_provenance[:examples_wanted]
    ]

    if any(dangling.values()):
        result.fail(f"dangling identity references: {dangling}")
    elif docs_without_provenance:
        # Expected to be exactly the never-parsed local PDFs; surfaced so the
        # hypothesis is confirmed rather than assumed.
        result.warn(
            f"{len(docs_without_provenance)} documents carry no provenance "
            "(expected: the never-parsed local PDFs -- confirm parse_status)"
        )
    else:
        result.ok("identity graph fully connected")
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 1 -- structural invariants")
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
    failed = [r.key for r in results if r.status == "FAIL"]
    print(f"RESULT: {'FAIL -> checks ' + ', '.join(failed) if failed else 'PASS'}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--esg-text-root", type=Path, default=config.ESG_TEXT_DIR)
    parser.add_argument("--checks", default="all",
                        help=f"comma-separated subset of {','.join(ALL_CHECKS)} (default: all)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max example violations to show per check (default: 5)")
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
        results: list[CheckResult] = []

        if "1" in selected or "1b" in selected:
            tiling, fidelity = check_1_and_1b(con, docs, args.esg_text_root, args.examples)
            if "1" in selected:
                results.append(tiling)
            if "1b" in selected:
                results.append(fidelity)
        if "2" in selected:
            results.append(check_2(con, docs, args.examples))
        if "3" in selected:
            results.append(check_3(con, docs, args.examples))
        if "4" in selected:
            results.append(check_4(con, args.examples))
        if "5" in selected:
            results.append(check_5(con, docs, args.esg_text_root, args.examples))
        if "6" in selected:
            results.append(check_6(con, docs, args.examples))
        if "7" in selected:
            results.append(check_7(con, args.examples))
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

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
