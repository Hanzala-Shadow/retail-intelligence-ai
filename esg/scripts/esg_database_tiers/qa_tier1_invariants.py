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
    3  chunks tile their parent section (no gaps; overlap within the
       chunker's declared sliding window -- see CHUNK_OVERLAP_TOKENS)
    4  chunk spans are contained in their section's span
    5  page ranges are ordered and within the document's page count
    6  char-offset order agrees with page order (zero discordant pairs;
       cross-page only -- within-page reading order is out of scope)
    7  provenance cardinalities and identity fan-out

Checks 1, 1b, 5 and 6 need the parsed text and page maps under
data/02_interim/esg_text/, because the database stores chunk text but not the
parsed-document ground truth the offsets are defined against. Documents whose
parsed text is missing are reported as SKIPPED, never silently passed.

Usage
    python esg/scripts/esg_database_tiers/qa_tier1_invariants.py
    python esg/scripts/esg_database_tiers/qa_tier1_invariants.py --checks 1,2,3
    python esg/scripts/esg_database_tiers/qa_tier1_invariants.py --json-out reports/qa_tier1.json
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

# Chunks are cut with a deliberate sliding window: esg_chunker steps back
# OVERLAP tokens at every boundary (esg_chunker.py, `start = max(end - OVERLAP,
# start + 1)`) so that a sentence straddling a boundary survives intact in at
# least one chunk. Overlap between sibling chunks is therefore the designed
# behaviour, not a defect, and check 3 must not assert its absence -- doing so
# fails on every healthy corpus. What check 3 can still assert is that the
# overlap stays within the window the chunker asked for.
try:  # single source of truth; fall back if the pipeline src is unavailable
    from esg_chunker import OVERLAP as CHUNK_OVERLAP_TOKENS  # noqa: E402
except Exception:  # pragma: no cover - keeps QA runnable without tiktoken
    CHUNK_OVERLAP_TOKENS = 50

# Overlap is declared in tokens but stored as character offsets, so the budget
# is converted per boundary using the following chunk's own characters-per-token
# ratio. That estimate is loose where tokenisation is unusually dense (contents
# pages with dot leaders pack ~13 chars/token), so the budget carries a
# tolerance. Measured over the 24,497 overlapping boundaries in the corpus the
# ratio of actual overlap to budget has median 1.005, p99 1.50 and max 2.49; a
# 3x tolerance clears every legitimate boundary while still catching runaway
# overlap such as a wholly duplicated chunk, which lands near 10x.
OVERLAP_TOLERANCE = 3.0


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


def kendall_tau_b(xs: list[int], ys: list[int]) -> tuple[float | None, int, int, int]:
    """Kendall's tau-b, the raw discordant-pair count, and the pair census.

    Returns (tau_b, discordant, n0, n2) where n0 is the total pair count and n2
    the number tied on y. The caller needs the census because tau-b alone is not
    interpretable here: chunks share a page_start constantly, and when the
    discordant count is zero and x carries no ties -- both true of this corpus --
    tau-b algebraically reduces to sqrt(1 - n2/n0). That is a re-encoding of the
    tie density and says nothing whatever about ordering, so a low tau-b must
    never be read as disorder without checking `discordant` first.
    """
    n = len(xs)
    if n < 2:
        return None, 0, 0, 0

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
    return tau, discordant, n0, n2


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
    # Counted independently of the example lists: an example list is capped at
    # `examples_wanted`, so len() of it is a cap, never a population count.
    n_overlap_docs = n_low_coverage_docs = n_mismatches = 0
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
                n_mismatches += 1
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

        if overlap > 0:
            n_overlap_docs += 1
            if len(overlap_docs) < examples_wanted:
                overlap_docs.append(
                    {
                        "ticker": doc["ticker"],
                        "stem": doc["stem"],
                        "overlap_chars": overlap,
                        "summed_spans": total,
                        "union_spans": union,
                    }
                )
        if ratio < LOW_COVERAGE_FLOOR:
            n_low_coverage_docs += 1
            if len(low_coverage_docs) < examples_wanted:
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
        "documents_with_overlap": n_overlap_docs,
        "documents_below_coverage_floor": n_low_coverage_docs,
        "coverage_floor": LOW_COVERAGE_FLOOR,
    }
    tiling.examples = {"overlapping": overlap_docs, "low_coverage": low_coverage_docs,
                       "skipped": skipped[:examples_wanted]}
    if n_overlap_docs:
        tiling.fail(f"{n_overlap_docs} documents have overlapping section spans")
    elif n_low_coverage_docs:
        tiling.warn(f"{n_low_coverage_docs} documents below {LOW_COVERAGE_FLOOR:.0%} coverage")
    elif not coverage_ratios:
        tiling.status = "SKIP"
        tiling.headline = "no document had readable parsed text"
    else:
        tiling.ok(f"no overlaps; median coverage {percentile(coverage_ratios, 0.5):.3f}")

    fidelity.stats = {
        "sections_checked": checked_sections,
        "mismatches_found": n_mismatches,
        "examples_shown": len(mismatches),
        "note": "examples capped; rerun with --examples for more",
    }
    fidelity.examples = mismatches
    if n_mismatches:
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

    by_section: dict[int, list[tuple[int, int, int | None, int]]] = defaultdict(list)
    null_offsets = 0
    for row in con.execute(
        """
        SELECT section_id, source_start_char, source_end_char,
               token_count, LENGTH(chunk_text) AS text_len
        FROM chunks
        """
    ):
        if row["source_start_char"] is None or row["source_end_char"] is None:
            null_offsets += 1
            continue
        by_section[row["section_id"]].append(
            (int(row["source_start_char"]), int(row["source_end_char"]),
             row["token_count"], row["text_len"] or 0)
        )

    ratios: list[float] = []
    boundary_overlaps: list[float] = []
    gap_examples, excess_examples, low_coverage = [], [], []
    sections_without_chunks = 0
    sections_with_gaps = sections_with_overlap = sections_over_budget = 0
    total_gap_chars = total_overlap_chars = 0
    boundaries_total = boundaries_with_overlap = boundaries_over_budget = 0

    for section_id, section in sections.items():
        spans = by_section.get(section_id)
        if not spans:
            sections_without_chunks += 1
            continue
        char_count = section["char_count"] or 0
        if char_count <= 0:
            continue
        spans.sort()
        doc = docs.get(section["doc_id"], {})

        # -- invariant A: the chunks must leave no part of the section uncovered.
        # Nothing in the chunker intends to drop text, so any gap is a defect.
        _, union, overlap = span_metrics([(s, e) for s, e, _, _ in spans])
        ratio = union / char_count
        ratios.append(ratio)
        gap = char_count - union
        if gap > 0:
            sections_with_gaps += 1
            total_gap_chars += gap
            if len(gap_examples) < examples_wanted:
                gap_examples.append(
                    {
                        "ticker": doc.get("ticker"),
                        "stem": doc.get("stem"),
                        "section_instance_id": section["section_instance_id"],
                        "section_chars": char_count,
                        "uncovered_chars": gap,
                        "coverage_ratio": round(ratio, 6),
                    }
                )
        if ratio < LOW_COVERAGE_FLOOR and len(low_coverage) < examples_wanted:
            low_coverage.append(
                {
                    "ticker": doc.get("ticker"),
                    "stem": doc.get("stem"),
                    "section_instance_id": section["section_instance_id"],
                    "coverage_ratio": round(ratio, 4),
                    "uncovered_chars": gap,
                }
            )

        # -- invariant B: overlap is expected, but only up to the sliding window
        # the chunker declares. Excess means a boundary was not stepped back by
        # OVERLAP tokens but by something larger -- a genuine chunker fault.
        if overlap > 0:
            sections_with_overlap += 1
            total_overlap_chars += overlap
        section_over_budget = False
        for before, after in zip(spans, spans[1:]):
            boundaries_total += 1
            overlap_chars = before[1] - after[0]
            if overlap_chars <= 0:
                continue
            boundaries_with_overlap += 1
            boundary_overlaps.append(float(overlap_chars))
            tokens, text_len = after[2], after[3]
            if not tokens or not text_len:
                continue
            chars_per_token = text_len / tokens
            overlap_tokens = overlap_chars / chars_per_token
            if overlap_tokens > CHUNK_OVERLAP_TOKENS * OVERLAP_TOLERANCE:
                boundaries_over_budget += 1
                section_over_budget = True
                if len(excess_examples) < examples_wanted:
                    excess_examples.append(
                        {
                            "ticker": doc.get("ticker"),
                            "stem": doc.get("stem"),
                            "section_instance_id": section["section_instance_id"],
                            "spans": [[before[0], before[1]], [after[0], after[1]]],
                            "overlap_chars": overlap_chars,
                            "overlap_tokens_est": round(overlap_tokens, 1),
                            "budget_tokens": CHUNK_OVERLAP_TOKENS * OVERLAP_TOLERANCE,
                        }
                    )
        if section_over_budget:
            sections_over_budget += 1

    chunk_chars = con.execute("SELECT SUM(LENGTH(chunk_text)) FROM chunks").fetchone()[0] or 0

    result.stats = {
        "sections_with_chunks": len(ratios),
        "sections_without_chunks": sections_without_chunks,
        "chunks_with_null_offsets": null_offsets,
        "coverage_ratio": describe(ratios),
        "sections_with_gaps": sections_with_gaps,
        "total_uncovered_chars": total_gap_chars,
        "sections_below_coverage_floor": len(low_coverage),
        "coverage_floor": LOW_COVERAGE_FLOOR,
        # Designed overlap: reported for downstream tiers, never failed on.
        "designed_overlap_tokens": CHUNK_OVERLAP_TOKENS,
        "overlap_tolerance": OVERLAP_TOLERANCE,
        "sections_with_overlap": sections_with_overlap,
        "adjacent_boundaries": boundaries_total,
        "boundaries_with_overlap": boundaries_with_overlap,
        "total_overlap_chars": total_overlap_chars,
        "overlap_share_of_chunk_text": (
            round(total_overlap_chars / chunk_chars, 6) if chunk_chars else None
        ),
        "overlap_chars_per_boundary": describe(boundary_overlaps),
        "boundaries_exceeding_overlap_budget": boundaries_over_budget,
        "sections_exceeding_overlap_budget": sections_over_budget,
    }
    result.examples = {
        "gaps": gap_examples,
        "excess_overlap": excess_examples,
        "low_coverage": low_coverage,
    }
    if sections_with_gaps:
        result.fail(
            f"{sections_with_gaps} sections leave {total_gap_chars:,} chars uncovered "
            "by any chunk"
        )
    elif boundaries_over_budget:
        result.fail(
            f"{boundaries_over_budget} boundaries overlap by more than "
            f"{CHUNK_OVERLAP_TOKENS * OVERLAP_TOLERANCE:.0f} tokens "
            f"across {sections_over_budget} sections"
        )
    elif low_coverage:
        result.warn(f"{len(low_coverage)} sections below {LOW_COVERAGE_FLOOR:.0%} chunk coverage")
    elif ratios:
        result.ok(
            f"no gaps; median coverage {percentile(ratios, 0.5):.3f}; "
            f"{boundaries_with_overlap:,} boundaries carry the designed "
            f"{CHUNK_OVERLAP_TOKENS}-token overlap"
        )
    else:
        result.ok("no measurable sections")
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
    n_beyond_end = 0          # counted independently; beyond_end is capped
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
            n_beyond_end += 1
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
        "chunks_beyond_document_end": n_beyond_end,
        "documents_missing_page_map": missing_maps,
        "pages_spanned_per_chunk": describe([float(s) for s in spans]),
    }
    result.examples = beyond_end
    if inverted or non_positive or n_beyond_end:
        result.fail(
            f"{inverted} inverted, {non_positive} non-positive, "
            f"{n_beyond_end} beyond document end"
        )
    else:
        result.ok("all page ranges ordered, positive, and within bounds")
    return result


# ---------------------------------------------------------------------------
# check 6 -- char offset order agrees with page order
# ---------------------------------------------------------------------------


def check_6(con, docs, examples_wanted):
    """Assert that no chunk starts later in the text but earlier in the document.

    The invariant is `total_discordant_pairs == 0`. tau-b is reported alongside
    it but is NOT the gate: because chunks constantly share a page_start, tau-b
    is dragged below 1.0 by ties alone, and the effect is strongest in small
    documents where tie density is highest. Gating on a tau-b floor therefore
    fails on healthy corpora -- it flags the shortest documents, not the corrupt
    ones -- which is exactly what an earlier `min(tau) < 0.90` condition did.

    SCOPE. This compares char offsets against *page numbers*, so it only sees
    reading-order faults that cross a page boundary. The two-column parser bug
    interleaves columns *within* one page; the chunks it corrupts all carry the
    same page_start, form ties, and are invisible here. ~40% of adjacent chunk
    pairs sit on a shared page. A PASS on this check is not evidence that the
    two-column bug is absent -- that needs a linguistic test (sentence-boundary
    integrity) or the geometric data from esg_reading_order.py.
    """
    result = CheckResult("6", "Char-offset order agrees with page order")

    by_doc: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in con.execute(
        """
        SELECT doc_id, source_start_char, page_start FROM chunks
        WHERE source_start_char IS NOT NULL AND page_start IS NOT NULL
        """
    ):
        by_doc[row["doc_id"]].append((int(row["source_start_char"]), int(row["page_start"])))

    taus, inverted_docs = [], []
    total_discordant = 0
    docs_with_discordant = 0
    pairs_total = pairs_tied = 0
    for doc_id, pairs in by_doc.items():
        if len(pairs) < 2:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        tau, discordant, n0, n2 = kendall_tau_b(xs, ys)
        total_discordant += discordant
        pairs_total += n0
        pairs_tied += n2
        doc = docs.get(doc_id, {})
        if discordant:
            docs_with_discordant += 1
            if len(inverted_docs) < examples_wanted:
                inverted_docs.append(
                    {
                        "ticker": doc.get("ticker"),
                        "stem": doc.get("stem"),
                        "discordant_pairs": discordant,
                        "comparable_pairs": n0 - n2,
                        "tau_b": round(tau, 4) if tau is not None else None,
                        "chunks": len(pairs),
                    }
                )
        if tau is not None:
            taus.append(tau)

    result.stats = {
        "documents_measured": len(taus),
        # The invariant. Everything below this line is descriptive only.
        "total_discordant_pairs": total_discordant,
        "documents_with_discordant_pairs": docs_with_discordant,
        "comparable_pairs": pairs_total - pairs_tied,
        # Chunks sharing a page_start are neither concordant nor discordant, so
        # their relative order is outside this check's reach entirely -- see the
        # scope note in the check's docstring.
        "pairs_tied_on_page_untested": pairs_tied,
        "tied_pair_share": round(pairs_tied / pairs_total, 6) if pairs_total else None,
        # Descriptive: with zero discordant pairs this is sqrt(1 - n2/n0), i.e.
        # a function of tie density and document size, not of ordering.
        "tau_b_descriptive": describe(taus),
    }
    result.examples = inverted_docs
    if total_discordant:
        result.fail(
            f"{total_discordant} discordant offset/page pairs across "
            f"{docs_with_discordant} documents; reading order is inverted"
        )
    elif not taus:
        result.status = "SKIP"
        result.headline = "no document had comparable offset/page pairs"
    else:
        result.ok(
            f"no inversions in {pairs_total - pairs_tied:,} comparable pairs across "
            f"{len(taus)} documents (within-page order not tested)"
        )
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
