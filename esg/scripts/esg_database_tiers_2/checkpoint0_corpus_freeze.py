"""Stage 2 Checkpoint 0: build and freeze the corpus every later figure is computed from.

Checkpoints 1-6 of the stage 2 QA set may not be computed against a live
database: the counts move between builds, and a number without a snapshot
behind it cannot be reconciled or re-run. This script produces that snapshot --
a per-document manifest, the company-year denominator, the report-year
resolution record, and an addressability pass -- and stamps it with the
database's SHA-256 and the pipeline's git commit.

Everything is READ-ONLY with respect to the database and to data/: the database
is opened with mode=ro, and the only files written are the snapshot's own
outputs under --out-dir.

Questions
    Q1  what exactly is in the corpus, at one fixed instant
        -> corpus_manifest.csv, one row per document
    Q2  the unit of analysis: how a file becomes a company-year
        -> report_year_resolution.csv, multi_year_filenames.csv
    Q3  the denominator: which company-years are in scope
        -> company_year_grid.csv
    Q4  is the frozen corpus internally addressable
        -> addressability_exceptions.csv

Q1 and Q2 need the parsed text and page maps under interim/esg_text/
(ESG_TEXT_DIR in esg/config.py; --esg-text-root to override)
for page counts and parsed character counts, because the database stores chunk
text but not the parsed-document ground truth. Point this at the same build the
database was loaded from. Documents whose parsed text is missing are reported as
missing, never silently counted as zero. Pass --no-text to skip that pass.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint0_corpus_freeze.py
    python esg/scripts/esg_database_tiers_2/checkpoint0_corpus_freeze.py --no-text
    python esg/scripts/esg_database_tiers_2/checkpoint0_corpus_freeze.py --years 2015:2025
    python esg/scripts/esg_database_tiers_2/checkpoint0_corpus_freeze.py --out-dir reports/qa_stage2/corpus_20260805
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

csv.field_size_limit(10**9)

# A chunk is "eligible" for the ESG index on exactly one rag_action value; the
# other two (exclude_from_esg_index, manual_review_before_indexing) are the
# ineligible remainder Checkpoint 5 accounts for.
RAG_ACTION_ELIGIBLE = "index_as_esg"

# Report-year precedence, applied in this order and recorded per document so
# the mix of branches is visible rather than assumed. Branch "provenance" is
# first because it is the only one that survives a file rename.
YEAR_BRANCHES = ("provenance", "filename", "tracker", "unresolved")

# A filename may name more than one year ("...-2017-2018.pdf"): a report
# covering a fiscal range is filed under the later year, so the rule is "latest
# year in the filename wins". Stated here as one constant because every
# company-year figure downstream depends on it.
MULTI_YEAR_RULE = "latest"

YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")

# Fields whose absence would make a chunk unciteable. Q4 counts nulls in each.
ADDRESSABILITY_FIELDS = (
    "external_chunk_id",
    "page_start",
    "page_end",
    "source_start_char",
    "source_end_char",
)


# ---------------------------------------------------------------------------
# result plumbing (mirrors esg_database_tiers so the two read the same way)
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


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------
# filesystem side of the manifest: page counts and parsed character counts
# ---------------------------------------------------------------------------


def parsed_text_path(root: Path, ticker: str, stem: str) -> Path:
    return root / ticker / f"{stem}.txt"


def page_map_path(root: Path, ticker: str, stem: str) -> Path:
    return root / ticker / f"{stem}.pages.csv"


def read_parsed_char_count(path: Path) -> int | None:
    """Characters as drive_to_db.py read them, so offsets line up. Streamed."""
    if not path.exists():
        return None
    total = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            total += len(block)
    return total


def read_page_count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        pages = [row for row in csv.DictReader(handle) if row.get("page")]
    if not pages:
        return None
    return max(int(row["page"]) for row in pages)


# ---------------------------------------------------------------------------
# Q2 -- report year resolution
# ---------------------------------------------------------------------------


def years_in_filename(stem: str) -> list[int]:
    return sorted({int(match) for match in YEAR_RE.findall(stem)})


def resolve_report_year(doc: dict) -> tuple[int | None, str, list[int]]:
    """Return (report_year, branch, years_found_in_filename).

    Precedence: provenance report_year, then the filename, then the tracker's
    own year for the same Drive file. A document that clears none of the three
    is left unresolved for manual cover-page review rather than guessed at.
    """
    filename_years = years_in_filename(doc["stem"])

    if doc.get("provenance_year") is not None:
        return int(doc["provenance_year"]), "provenance", filename_years
    if filename_years:
        chosen = max(filename_years) if MULTI_YEAR_RULE == "latest" else min(filename_years)
        return chosen, "filename", filename_years
    if doc.get("tracker_year") is not None:
        return int(doc["tracker_year"]), "tracker", filename_years
    return None, "unresolved", filename_years


# ---------------------------------------------------------------------------
# Q1 -- the corpus manifest
# ---------------------------------------------------------------------------


MANIFEST_COLUMNS = [
    "doc_id", "ticker", "company_id", "company_name", "sector",
    "report_year", "report_year_source", "filename_years",
    "doc_type", "parse_status", "doc_quality_status", "rag_action",
    "lifecycle_state", "filename", "filepath",
    "logical_source_id", "source_version_id", "extraction_artifact_id", "file_alias_id",
    "original_sha256", "byte_size", "media_type",
    "artifact_role", "parser_used", "verification_state", "storage_path", "drive_file_id",
    "aliases_on_source_version",
    "drive_md5_checksum", "drive_size_bytes", "drive_status", "drive_match_key",
    "tracker_status", "tracker_report_year", "tracker_match_key",
    "page_count", "parsed_chars", "parsed_text_found",
    "section_count", "chunk_count", "eligible_chunk_count", "short_evidence_chunk_count",
]


def build_manifest(con: sqlite3.Connection, text_root: Path, read_text: bool) -> list[dict]:
    rows = con.execute(
        """
        SELECT d.doc_id, d.company_id, d.doc_type, d.filepath, d.parse_status,
               d.doc_quality_status, d.rag_action, d.lifecycle_state,
               d.logical_source_id, d.source_version_id,
               d.extraction_artifact_id, d.file_alias_id,
               k.ticker, k.name AS company_name, k.sector,
               l.report_year AS provenance_year,
               v.original_sha256, v.byte_size, v.media_type,
               e.artifact_role, e.parser_or_model AS parser_used,
               e.verification_state, e.storage_path, e.drive_file_id
        FROM documents d
        JOIN companies k ON k.company_id = d.company_id
        LEFT JOIN logical_sources l ON l.logical_source_id = d.logical_source_id
        LEFT JOIN source_versions v ON v.source_version_id = d.source_version_id
        LEFT JOIN extraction_artifacts e ON e.extraction_artifact_id = d.extraction_artifact_id
        """
    ).fetchall()

    section_counts = dict(
        con.execute("SELECT doc_id, COUNT(*) FROM sections GROUP BY doc_id").fetchall()
    )
    chunk_counts = dict(
        con.execute("SELECT doc_id, COUNT(*) FROM chunks GROUP BY doc_id").fetchall()
    )
    eligible_counts = dict(
        con.execute(
            "SELECT doc_id, COUNT(*) FROM chunks WHERE rag_action = ? GROUP BY doc_id",
            (RAG_ACTION_ELIGIBLE,),
        ).fetchall()
    )
    short_counts = dict(
        con.execute(
            "SELECT doc_id, COUNT(*) FROM chunks WHERE chunk_type = 'short_evidence' GROUP BY doc_id"
        ).fetchall()
    )
    aliases_per_version = Counter(
        r[0] for r in con.execute("SELECT source_version_id FROM file_aliases")
    )

    # Both reference files are keyed on the Drive file id -- the tracker via
    # its Drive link -- but drive_file_id is not populated on every build (it
    # is null throughout the docling runs), so each lookup falls back to the
    # file name. The key that actually matched is recorded per row so a join
    # that quietly degrades to the weaker key is visible in the manifest.
    drive_by_id: dict[str, dict] = {}
    drive_by_name: dict[str, dict] = {}
    for r in read_csv(config.ESG_DRIVE_MANIFEST_CSV):
        if r.get("drive_file_id"):
            drive_by_id[r["drive_file_id"]] = r
        for name in (r.get("drive_file_name"), Path(r.get("local_file") or "").name):
            if name:
                drive_by_name.setdefault(name.strip().casefold(), r)

    tracker_by_id: dict[str, dict] = {}
    tracker_by_name: dict[str, dict] = {}
    for r in read_csv(config.SUSTAINABILITY_TRACKER_CSV):
        match = re.search(r"/d/([A-Za-z0-9_-]{20,})", r.get("drive_file_link") or "")
        if match:
            tracker_by_id[match.group(1)] = r
        # The tracker carries the delivered file name in `notes`.
        name = (r.get("notes") or "").strip().casefold()
        if name.endswith(".pdf"):
            tracker_by_name.setdefault(name, r)

    def lookup(by_id: dict, by_name: dict, drive_id: str | None, filename: str):
        if drive_id and drive_id in by_id:
            return by_id[drive_id], "drive_file_id"
        row = by_name.get(filename.strip().casefold())
        return (row, "filename") if row else ({}, "none")

    manifest: list[dict] = []
    for row in rows:
        doc = dict(row)
        ticker = (doc.get("ticker") or "").upper()
        stem = Path(doc.get("filepath") or "").stem
        doc["stem"] = stem

        filename = Path(doc.get("filepath") or "").name
        tracker_row, tracker_key = lookup(
            tracker_by_id, tracker_by_name, doc.get("drive_file_id"), filename
        )
        tracker_year = tracker_row.get("report_year")
        doc["tracker_year"] = int(tracker_year) if (tracker_year or "").strip().isdigit() else None

        report_year, branch, filename_years = resolve_report_year(doc)
        drive_row, drive_key = lookup(
            drive_by_id, drive_by_name, doc.get("drive_file_id"), filename
        )

        page_count = parsed_chars = None
        text_found = None
        if read_text:
            page_count = read_page_count(page_map_path(text_root, ticker, stem))
            parsed_chars = read_parsed_char_count(parsed_text_path(text_root, ticker, stem))
            text_found = parsed_chars is not None

        manifest.append({
            "doc_id": doc["doc_id"],
            "ticker": ticker,
            "company_id": doc["company_id"],
            "company_name": doc["company_name"],
            "sector": doc["sector"],
            "report_year": report_year,
            "report_year_source": branch,
            "filename_years": "|".join(str(y) for y in filename_years),
            "doc_type": doc["doc_type"],
            "parse_status": doc["parse_status"],
            "doc_quality_status": doc["doc_quality_status"],
            "rag_action": doc["rag_action"],
            "lifecycle_state": doc["lifecycle_state"],
            "filename": Path(doc.get("filepath") or "").name,
            "filepath": doc["filepath"],
            "logical_source_id": doc["logical_source_id"],
            "source_version_id": doc["source_version_id"],
            "extraction_artifact_id": doc["extraction_artifact_id"],
            "file_alias_id": doc["file_alias_id"],
            "original_sha256": doc["original_sha256"],
            "byte_size": doc["byte_size"],
            "media_type": doc["media_type"],
            "artifact_role": doc["artifact_role"],
            "parser_used": doc["parser_used"],
            "verification_state": doc["verification_state"],
            "storage_path": doc["storage_path"],
            "drive_file_id": doc["drive_file_id"],
            "aliases_on_source_version": aliases_per_version.get(doc["source_version_id"], 0),
            "drive_md5_checksum": drive_row.get("drive_md5_checksum"),
            "drive_size_bytes": drive_row.get("drive_size_bytes"),
            "drive_status": drive_row.get("status"),
            "drive_match_key": drive_key,
            "tracker_status": tracker_row.get("status"),
            "tracker_report_year": doc["tracker_year"],
            "tracker_match_key": tracker_key,
            "page_count": page_count,
            "parsed_chars": parsed_chars,
            "parsed_text_found": text_found,
            "section_count": section_counts.get(doc["doc_id"], 0),
            "chunk_count": chunk_counts.get(doc["doc_id"], 0),
            "eligible_chunk_count": eligible_counts.get(doc["doc_id"], 0),
            "short_evidence_chunk_count": short_counts.get(doc["doc_id"], 0),
        })

    manifest.sort(key=lambda r: (r["ticker"], r["report_year"] or 0, r["doc_id"]))
    return manifest


def check_q1(con, manifest: list[dict], out_dir: Path, read_text: bool) -> CheckResult:
    result = CheckResult("Q1", "What exactly is in the corpus, at one fixed instant")

    documents_in_db = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    unresolved_version = [r for r in manifest if not r["source_version_id"]]
    fanned_out = [r for r in manifest if (r["aliases_on_source_version"] or 0) > 1]
    missing_text = [r for r in manifest if read_text and r["parsed_text_found"] is False]

    path = write_csv(out_dir / "corpus_manifest.csv", manifest, MANIFEST_COLUMNS)
    result.outputs = [str(path)]
    result.stats = {
        "manifest_rows": len(manifest),
        "documents_in_database": documents_in_db,
        "documents_without_source_version": len(unresolved_version),
        "source_versions_with_multiple_aliases": len(fanned_out),
        "row_counts": {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "companies", "documents", "sections", "chunks",
                "logical_sources", "source_versions", "extraction_artifacts",
                "file_aliases", "sustainability_reports",
            )
        },
        "chunks_by_rag_action": dict(
            con.execute("SELECT rag_action, COUNT(*) FROM chunks GROUP BY 1").fetchall()
        ),
        "parsed_text_missing": len(missing_text) if read_text else None,
        "text_pass": "run" if read_text else "skipped (--no-text)",
        "drive_manifest_join": dict(Counter(r["drive_match_key"] for r in manifest)),
        "tracker_join": dict(Counter(r["tracker_match_key"] for r in manifest)),
    }
    result.examples = [
        {"doc_id": r["doc_id"], "ticker": r["ticker"], "filepath": r["filepath"],
         "reason": "no source_version_id"}
        for r in unresolved_version[:5]
    ] + [
        {"doc_id": r["doc_id"], "ticker": r["ticker"], "filepath": r["filepath"],
         "reason": "parsed text not found under --esg-text-root"}
        for r in missing_text[:5]
    ]

    if len(manifest) != documents_in_db:
        return result.fail(
            f"manifest has {len(manifest)} rows against {documents_in_db} documents"
        )
    if unresolved_version:
        return result.fail(
            f"{len(unresolved_version)} document(s) do not resolve to a source_version"
        )
    if missing_text:
        return result.warn(
            f"{len(manifest)} documents frozen; parsed text missing for {len(missing_text)} "
            f"(page and character counts are null for those rows)"
        )
    return result.ok(f"{len(manifest)} documents frozen, each resolving to one source_version")


# ---------------------------------------------------------------------------
# Q2 -- unit of analysis
# ---------------------------------------------------------------------------


def check_q2(manifest: list[dict], out_dir: Path) -> CheckResult:
    result = CheckResult("Q2", "The unit of analysis: how a file becomes a company-year")

    branch_counts = Counter(r["report_year_source"] for r in manifest)
    unresolved = [r for r in manifest if r["report_year"] is None]

    multi_year = [
        {
            "doc_id": r["doc_id"], "ticker": r["ticker"], "filename": r["filename"],
            "filename_years": r["filename_years"],
            "assigned_report_year": r["report_year"],
            "assignment_rule": MULTI_YEAR_RULE,
            "report_year_source": r["report_year_source"],
            "tracker_report_year": r["tracker_report_year"],
            "agrees_with_tracker": (
                None if r["tracker_report_year"] is None
                else r["tracker_report_year"] == r["report_year"]
            ),
        }
        for r in manifest
        if len(r["filename_years"].split("|")) > 1 and r["filename_years"]
    ]

    # Where a second source of truth exists, say whether the resolved year
    # agrees with it. Disagreement here is a Checkpoint 1 reconciliation item.
    comparable = [r for r in manifest if r["tracker_report_year"] is not None]
    disagreements = [r for r in comparable if r["tracker_report_year"] != r["report_year"]]

    resolution_rows = [
        {
            "doc_id": r["doc_id"], "ticker": r["ticker"], "filename": r["filename"],
            "report_year": r["report_year"], "report_year_source": r["report_year_source"],
            "filename_years": r["filename_years"],
            "tracker_report_year": r["tracker_report_year"],
        }
        for r in manifest
    ]
    paths = [
        write_csv(out_dir / "report_year_resolution.csv", resolution_rows,
                  ["doc_id", "ticker", "filename", "report_year", "report_year_source",
                   "filename_years", "tracker_report_year"]),
        write_csv(out_dir / "multi_year_filenames.csv", multi_year,
                  ["doc_id", "ticker", "filename", "filename_years", "assigned_report_year",
                   "assignment_rule", "report_year_source", "tracker_report_year",
                   "agrees_with_tracker"]),
    ]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "precedence": list(YEAR_BRANCHES),
        "multi_year_filename_rule": MULTI_YEAR_RULE,
        "documents_by_branch": {b: branch_counts.get(b, 0) for b in YEAR_BRANCHES},
        "documents_unresolved": len(unresolved),
        "multi_year_filenames": len(multi_year),
        "comparable_against_tracker": len(comparable),
        "tracker_year_disagreements": len(disagreements),
        "documents_per_report_year": dict(
            sorted(Counter(r["report_year"] for r in manifest if r["report_year"]).items())
        ),
    }
    result.examples = [
        {"doc_id": r["doc_id"], "ticker": r["ticker"], "filename": r["filename"],
         "reason": "no year in provenance, filename or tracker"}
        for r in unresolved[:5]
    ] + [
        {"doc_id": r["doc_id"], "ticker": r["ticker"], "filename": r["filename"],
         "resolved": r["report_year"], "tracker": r["tracker_report_year"]}
        for r in disagreements[:5]
    ]

    if unresolved:
        return result.fail(
            f"{len(unresolved)} document(s) carry no report_year; each needs a cover-page read"
        )
    if disagreements:
        return result.warn(
            f"every document has one report_year, but {len(disagreements)} disagree with the "
            f"tracker (Checkpoint 1 reconciles these)"
        )
    return result.ok(
        f"every document has exactly one report_year; branches "
        f"{dict((b, branch_counts.get(b, 0)) for b in YEAR_BRANCHES)}"
    )


# ---------------------------------------------------------------------------
# Q3 -- the denominator
# ---------------------------------------------------------------------------


def check_q3(con, manifest: list[dict], out_dir: Path, year_range: tuple[int, int] | None):
    result = CheckResult("Q3", "The denominator: which company-years are in scope")

    universe = read_csv(config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV)
    if not universe:
        result.status = "SKIP"
        return result.ok(
            f"company universe not found at {config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV}"
        )

    db_companies = {
        (r["ticker"] or "").upper(): dict(r)
        for r in con.execute("SELECT ticker, name, sector FROM companies")
    }

    years_present = sorted({r["report_year"] for r in manifest if r["report_year"]})
    if year_range:
        lo, hi = year_range
    elif years_present:
        lo, hi = years_present[0], years_present[-1]
    else:
        result.status = "SKIP"
        return result.ok("no report years present; cannot build a grid")
    years = list(range(lo, hi + 1))

    chunks_by_cell: dict[tuple[str, int], int] = defaultdict(int)
    docs_by_cell: dict[tuple[str, int], int] = defaultdict(int)
    for r in manifest:
        if r["report_year"]:
            chunks_by_cell[(r["ticker"], r["report_year"])] += r["chunk_count"]
            docs_by_cell[(r["ticker"], r["report_year"])] += 1

    grid: list[dict] = []
    for company in universe:
        ticker = (company.get("ticker") or "").upper()
        decision = (company.get("decision") or "").upper()
        in_db = ticker in db_companies
        for year in years:
            if decision and decision != "ACCEPT":
                in_scope, reason = False, f"company decision = {decision}"
            elif not in_db:
                in_scope, reason = False, "company absent from the database"
            else:
                in_scope, reason = True, ""
            grid.append({
                "ticker": ticker,
                "company_name": company.get("company_name") or db_companies.get(ticker, {}).get("name"),
                "sector": db_companies.get(ticker, {}).get("sector"),
                "decision": decision,
                "year": year,
                "in_scope": in_scope,
                "reason": reason,
                "documents": docs_by_cell.get((ticker, year), 0),
                "chunks": chunks_by_cell.get((ticker, year), 0),
            })

    universe_tickers = {(c.get("ticker") or "").upper() for c in universe}
    only_in_db = sorted(set(db_companies) - universe_tickers)
    only_in_universe = sorted(universe_tickers - set(db_companies))

    path = write_csv(
        out_dir / "company_year_grid.csv", grid,
        ["ticker", "company_name", "sector", "decision", "year", "in_scope", "reason",
         "documents", "chunks"],
    )
    in_scope_cells = [c for c in grid if c["in_scope"]]
    filled = [c for c in in_scope_cells if c["chunks"] > 0]

    result.outputs = [str(path)]
    result.stats = {
        "universe_file": str(config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV),
        "universe_companies": len(universe_tickers),
        "companies_in_database": len(db_companies),
        "year_range": f"{lo}:{hi}",
        "year_range_source": "--years" if year_range else "observed in the corpus",
        "grid_cells": len(grid),
        "in_scope_cells": len(in_scope_cells),
        "in_scope_cells_with_chunks": len(filled),
        "fill_rate": round(len(filled) / len(in_scope_cells), 4) if in_scope_cells else None,
        "tickers_only_in_database": only_in_db,
        "tickers_only_in_universe": only_in_universe,
    }
    result.examples = [{"ticker": t, "reason": "in database, not in universe"} for t in only_in_db[:5]]
    result.examples += [
        {"ticker": t, "reason": "in universe, not in database"} for t in only_in_universe[:5]
    ]

    if only_in_db or only_in_universe:
        return result.warn(
            f"grid built over {len(universe_tickers)} companies x {len(years)} years, but "
            f"{len(only_in_db)} database ticker(s) and {len(only_in_universe)} universe "
            f"ticker(s) do not match (Checkpoint 1, Q7)"
        )
    return result.ok(
        f"grid built: {len(universe_tickers)} companies x {len(years)} years, "
        f"{len(in_scope_cells)} in-scope cells, fill rate "
        f"{round(len(filled) / len(in_scope_cells), 4) if in_scope_cells else 'n/a'}"
    )


# ---------------------------------------------------------------------------
# Q4 -- addressability
# ---------------------------------------------------------------------------


def check_q4(con, out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q4", "Is the frozen corpus internally addressable")

    total_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    unresolvable = con.execute(
        """
        SELECT c.chunk_id, c.doc_id, c.section_id, c.company_id,
               CASE WHEN s.section_id IS NULL THEN 1 ELSE 0 END AS no_section,
               CASE WHEN d.doc_id     IS NULL THEN 1 ELSE 0 END AS no_document,
               CASE WHEN k.company_id IS NULL THEN 1 ELSE 0 END AS no_company
        FROM chunks c
        LEFT JOIN sections  s ON s.section_id  = c.section_id
        LEFT JOIN documents d ON d.doc_id      = c.doc_id
        LEFT JOIN companies k ON k.company_id  = c.company_id
        WHERE s.section_id IS NULL OR d.doc_id IS NULL OR k.company_id IS NULL
        """
    ).fetchall()

    # A chunk whose section belongs to a different document is addressable but
    # mis-parented, which corrupts every per-document figure downstream.
    misparented = con.execute(
        """
        SELECT c.chunk_id, c.doc_id AS chunk_doc_id, s.doc_id AS section_doc_id
        FROM chunks c JOIN sections s ON s.section_id = c.section_id
        WHERE s.doc_id != c.doc_id
        """
    ).fetchall()

    null_counts = {
        column: con.execute(
            f"SELECT COUNT(*) FROM chunks WHERE {column} IS NULL"
        ).fetchone()[0]
        for column in ADDRESSABILITY_FIELDS
    }
    duplicate_external_ids = con.execute(
        """
        SELECT external_chunk_id, COUNT(*) AS n FROM chunks
        WHERE external_chunk_id IS NOT NULL
        GROUP BY external_chunk_id HAVING n > 1
        """
    ).fetchall()

    exceptions: list[dict] = []
    for row in unresolvable:
        exceptions.append({
            "chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
            "section_id": row["section_id"],
            "problem": "unresolvable parent: " + ", ".join(
                name for name, flag in (
                    ("section", row["no_section"]),
                    ("document", row["no_document"]),
                    ("company", row["no_company"]),
                ) if flag
            ),
        })
    for row in misparented:
        exceptions.append({
            "chunk_id": row["chunk_id"], "doc_id": row["chunk_doc_id"],
            "section_id": None,
            "problem": f"section belongs to doc_id {row['section_doc_id']}",
        })
    for column, count in null_counts.items():
        if not count:
            continue
        for row in con.execute(
            f"SELECT chunk_id, doc_id, section_id FROM chunks WHERE {column} IS NULL "
            f"LIMIT {max(examples_wanted, 50)}"
        ):
            exceptions.append({
                "chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
                "section_id": row["section_id"], "problem": f"{column} is null",
            })

    path = write_csv(out_dir / "addressability_exceptions.csv", exceptions,
                     ["chunk_id", "doc_id", "section_id", "problem"])

    result.outputs = [str(path)]
    result.stats = {
        "chunks": total_chunks,
        "chunks_with_unresolvable_parent": len(unresolvable),
        "chunks_parented_to_the_wrong_document": len(misparented),
        "null_counts": null_counts,
        "duplicate_external_chunk_ids": len(duplicate_external_ids),
        "exception_rows_written": len(exceptions),
    }
    result.examples = exceptions[:examples_wanted]

    broken = len(unresolvable) + len(misparented) + len(duplicate_external_ids)
    nulls = sum(null_counts.values())
    if broken:
        return result.fail(
            f"{len(unresolvable)} unresolvable, {len(misparented)} mis-parented, "
            f"{len(duplicate_external_ids)} duplicated external_chunk_id"
        )
    if nulls:
        return result.warn(
            f"all {total_chunks} chunks resolve to section, document and company, but "
            f"{nulls} citation field value(s) are null: "
            + ", ".join(f"{c}={n}" for c, n in null_counts.items() if n)
        )
    return result.ok(
        f"all {total_chunks} chunks resolve to section, document and company with complete "
        f"citation fields"
    )


# ---------------------------------------------------------------------------
# rendering and the snapshot stamp
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], snapshot: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 0 -- corpus construction and freeze")
    print("=" * 78)
    print(f"manifest version : {snapshot['manifest_version']}")
    print(f"database         : {snapshot['database']}")
    print(f"database sha256  : {snapshot['database_sha256']}")
    print(f"git commit       : {snapshot['git_commit']}")
    print(f"repo root        : {snapshot['repo_root']}")
    print(f"text root        : {snapshot['esg_text_root']}")
    print(f"snapshot dir     : {snapshot['out_dir']}")
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
    print(f"Checkpoint 0 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when the manifest is frozen, hashed and version-stamped, and "
        "its version string travels with every figure computed from it."
    )
    print("-" * 78)


def parse_year_range(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        lo, hi = (int(part) for part in value.split(":", 1))
    except ValueError:
        raise SystemExit(f"--years expects LOW:HIGH, got {value!r}")
    if lo > hi:
        raise SystemExit(f"--years low bound exceeds high bound: {value!r}")
    return lo, hi


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    # The parsed text has to come from the build the database was loaded from.
    # This tree's default build writes it to ESG_TEXT_DIR; point --esg-text-root
    # at another build's interim/esg_text/ when the database came from one.
    parser.add_argument("--esg-text-root", type=Path, default=config.ESG_TEXT_DIR)
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="snapshot directory (default: "
                             "reports/qa_stage2/corpus_<UTC timestamp>)")
    parser.add_argument("--years", default=None,
                        help="in-scope year range as LOW:HIGH (default: observed in the corpus)")
    parser.add_argument("--no-text", action="store_true",
                        help="skip the filesystem pass; page_count and parsed_chars stay null")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint0.json)")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (config.REPORTS_DIR / "qa_stage2" / f"corpus_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    read_text = not args.no_text

    if read_text and not args.esg_text_root.exists():
        print(f"warning: --esg-text-root does not exist: {args.esg_text_root}", file=sys.stderr)
        print("         page counts and parsed character counts will be null", file=sys.stderr)

    con = connect(args.db)
    try:
        manifest = build_manifest(con, args.esg_text_root, read_text)
        results = [
            check_q1(con, manifest, out_dir, read_text),
            check_q2(manifest, out_dir),
            check_q3(con, manifest, out_dir, parse_year_range(args.years)),
            check_q4(con, out_dir, args.examples),
        ]
    finally:
        con.close()

    snapshot = {
        "manifest_version": f"corpus_{stamp}",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(args.db),
        "database_sha256": sha256_file(args.db),
        "database_bytes": args.db.stat().st_size,
        "git_commit": git_commit(_bootstrap.REPO_ROOT),
        "repo_root": str(_bootstrap.REPO_ROOT),
        "esg_text_root": str(args.esg_text_root),
        "text_pass": "run" if read_text else "skipped",
        "out_dir": str(out_dir),
        "results": [
            {"question": r.key, "title": r.title, "status": r.status,
             "headline": r.headline, "stats": r.stats, "examples": r.examples,
             "outputs": r.outputs}
            for r in results
        ],
    }

    (out_dir / "snapshot.json").write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    render(results, snapshot)
    print(f"snapshot stamp written to {out_dir / 'snapshot.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
