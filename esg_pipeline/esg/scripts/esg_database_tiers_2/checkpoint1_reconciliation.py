"""Stage 2 Checkpoint 1: reconcile the frozen corpus against its source of record.

Runs against the snapshot produced by checkpoint0_corpus_freeze.py, not against
whatever the database happens to hold now: the manifest is the corpus, and the
database is opened only for the counts that cannot be derived from it. The
snapshot's recorded SHA-256 is re-checked before anything is computed, so a
figure can never be attributed to a build it was not measured on.

Everything is READ-ONLY. The database is opened with mode=ro and nothing under
data/ is written; outputs land in <snapshot>/checkpoint1/.

Questions
    Q5  do 'Reports by publication year' counts tie to the Sustainability
        Report Tracker
        -> reports_by_year.csv, tracker_reconciliation_rows.csv
    Q6  do the provenance cardinalities reconcile (logical_sources /
        source_versions / extraction_artifacts vs file_aliases / documents)
        -> provenance_exceptions.csv
    Q7  does the company universe reconcile between manifest and database
        -> company_universe_exceptions.csv
    Q8  are the reported aggregates additive
        -> additivity.csv
    Q9  how many reports were downloaded but never made it into the index
        -> ingestion_losses.csv

Row-level matching against the tracker is by file name first -- the tracker
carries the delivered file name in `notes` -- and by (ticker, report_year)
second, because (ticker, report_year) is not unique in the tracker and cannot
be the primary key. The key that matched is recorded on every row.

Usage
    python esg/scripts/esg_database_tiers_2/checkpoint1_reconciliation.py
    python esg/scripts/esg_database_tiers_2/checkpoint1_reconciliation.py --snapshot reports/qa_stage2/corpus_20260805T093637Z
    python esg/scripts/esg_database_tiers_2/checkpoint1_reconciliation.py --allow-db-drift
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
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
PARSE_STATUS_OK = "parsed"

# Loss classes for Q9, in the order a document fails them. The first class a
# tracker row falls into is the one it is reported under.
LOSS_CLASSES = (
    "no_document_row",      # tracker says downloaded; nothing in the corpus
    "not_parsed",           # document row exists but parse_status is not 'parsed'
    "parsed_no_chunks",     # parsed, but the chunker produced nothing
    "no_eligible_chunks",   # chunked, but every chunk failed the RAG gate
    "indexed",              # reached the index
)


# ---------------------------------------------------------------------------
# result plumbing (same shape as checkpoint 0 and esg_database_tiers)
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


def norm_name(value: str | None) -> str:
    return (value or "").strip().casefold()


def norm_ticker(value: str | None) -> str:
    return (value or "").strip().upper()


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
        row["tracker_report_year"] = as_int(row["tracker_report_year"])
        for column in ("section_count", "chunk_count", "eligible_chunk_count",
                       "short_evidence_chunk_count", "page_count", "parsed_chars"):
            row[column] = as_int(row.get(column))
        manifest.append(row)
    return stamp, manifest


# ---------------------------------------------------------------------------
# Q5 -- reports by publication year vs the tracker
# ---------------------------------------------------------------------------


def check_q5(manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q5", "Reports by publication year vs the Sustainability Report Tracker")

    tracker = read_csv(config.SUSTAINABILITY_TRACKER_CSV)
    if not tracker:
        result.status = "SKIP"
        return result.ok(f"tracker not found at {config.SUSTAINABILITY_TRACKER_CSV}")

    # Index the tracker by both keys. (ticker, report_year) is not unique, so it
    # is a fallback that consumes one row at a time rather than a lookup.
    tracker_rows = []
    for index, row in enumerate(tracker):
        tracker_rows.append({
            "tracker_index": index,
            "ticker": norm_ticker(row.get("ticker")),
            "company_name": row.get("company_name"),
            "report_year": as_int(row.get("report_year")),
            "status": row.get("status"),
            "filename": (row.get("notes") or "").strip(),
            "drive_file_link": row.get("drive_file_link"),
            "matched_doc_id": None,
            "match_key": None,
        })
    by_filename: dict[str, list[dict]] = defaultdict(list)
    by_ticker_year: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
    for row in tracker_rows:
        if row["filename"]:
            by_filename[norm_name(row["filename"])].append(row)
        by_ticker_year[(row["ticker"], row["report_year"])].append(row)

    matched_pairs: list[dict] = []
    unmatched_docs: list[dict] = []
    for doc in manifest:
        candidates = by_filename.get(norm_name(doc["filename"]), [])
        hit = next((r for r in candidates if r["matched_doc_id"] is None), None)
        key = "filename"
        if hit is None:
            candidates = by_ticker_year.get((norm_ticker(doc["ticker"]), doc["report_year"]), [])
            hit = next((r for r in candidates if r["matched_doc_id"] is None), None)
            key = "ticker_year"
        if hit is None:
            unmatched_docs.append(doc)
            continue
        hit["matched_doc_id"] = doc["doc_id"]
        hit["match_key"] = key
        matched_pairs.append({"doc": doc, "tracker": hit})

    unmatched_tracker = [r for r in tracker_rows if r["matched_doc_id"] is None]

    # Per-year counts on each side. The tracker's own report_year is used for
    # the tracker column and the corpus's resolved report_year for the corpus
    # column: that is exactly the comparison the slide makes.
    tracker_by_year = Counter(r["report_year"] for r in tracker_rows)
    corpus_by_year = Counter(d["report_year"] for d in manifest)
    years = sorted(
        {y for y in tracker_by_year if y is not None} | {y for y in corpus_by_year if y is not None}
    )

    year_rows = []
    for year in years:
        tracker_n = tracker_by_year.get(year, 0)
        corpus_n = corpus_by_year.get(year, 0)
        year_rows.append({
            "report_year": year,
            "tracker_reports": tracker_n,
            "corpus_documents": corpus_n,
            "delta": corpus_n - tracker_n,
            "tracker_only_rows": sum(
                1 for r in unmatched_tracker if r["report_year"] == year
            ),
            "corpus_only_documents": sum(
                1 for d in unmatched_docs if d["report_year"] == year
            ),
        })
    year_rows.append({
        "report_year": "TOTAL",
        "tracker_reports": len(tracker_rows),
        "corpus_documents": len(manifest),
        "delta": len(manifest) - len(tracker_rows),
        "tracker_only_rows": len(unmatched_tracker),
        "corpus_only_documents": len(unmatched_docs),
    })

    # Year disagreement on rows that did match: the pair exists on both sides
    # but is filed under different years, which moves two yearly counts at once.
    year_shifted = [
        {
            "classification": "year_disagreement",
            "doc_id": pair["doc"]["doc_id"],
            "ticker": pair["doc"]["ticker"],
            "filename": pair["doc"]["filename"],
            "corpus_report_year": pair["doc"]["report_year"],
            "tracker_report_year": pair["tracker"]["report_year"],
            "match_key": pair["tracker"]["match_key"],
            "tracker_status": pair["tracker"]["status"],
        }
        for pair in matched_pairs
        if pair["doc"]["report_year"] != pair["tracker"]["report_year"]
    ]

    row_level = year_shifted + [
        {
            "classification": "tracker_only",
            "doc_id": None,
            "ticker": r["ticker"],
            "filename": r["filename"],
            "corpus_report_year": None,
            "tracker_report_year": r["report_year"],
            "match_key": None,
            "tracker_status": r["status"],
        }
        for r in unmatched_tracker
    ] + [
        {
            "classification": "corpus_only",
            "doc_id": d["doc_id"],
            "ticker": d["ticker"],
            "filename": d["filename"],
            "corpus_report_year": d["report_year"],
            "tracker_report_year": None,
            "match_key": None,
            "tracker_status": None,
        }
        for d in unmatched_docs
    ]

    paths = [
        write_csv(out_dir / "reports_by_year.csv", year_rows,
                  ["report_year", "tracker_reports", "corpus_documents", "delta",
                   "tracker_only_rows", "corpus_only_documents"]),
        write_csv(out_dir / "tracker_reconciliation_rows.csv", row_level,
                  ["classification", "doc_id", "ticker", "filename", "corpus_report_year",
                   "tracker_report_year", "match_key", "tracker_status"]),
    ]

    unexplained_years = [
        r for r in year_rows
        if r["report_year"] != "TOTAL" and r["delta"] != 0
        and (r["tracker_only_rows"] + r["corpus_only_documents"]) == 0
        and not any(
            s["corpus_report_year"] == r["report_year"]
            or s["tracker_report_year"] == r["report_year"]
            for s in year_shifted
        )
    ]

    result.outputs = [str(p) for p in paths]
    result.stats = {
        "tracker_rows": len(tracker_rows),
        "tracker_status_counts": dict(Counter(r["status"] for r in tracker_rows)),
        "corpus_documents": len(manifest),
        "matched_rows": len(matched_pairs),
        "match_keys": dict(Counter(p["tracker"]["match_key"] for p in matched_pairs)),
        "tracker_only": len(unmatched_tracker),
        "corpus_only": len(unmatched_docs),
        "matched_but_year_disagrees": len(year_shifted),
        "years_with_non_zero_delta": [
            r["report_year"] for r in year_rows
            if r["report_year"] != "TOTAL" and r["delta"] != 0
        ],
        "years_with_unexplained_delta": [r["report_year"] for r in unexplained_years],
    }
    result.examples = [
        {k: r[k] for k in ("classification", "ticker", "filename", "corpus_report_year",
                           "tracker_report_year")}
        for r in row_level[:examples_wanted]
    ]

    if unexplained_years:
        return result.fail(
            f"{len(unexplained_years)} year(s) have a delta with no row behind it: "
            f"{[r['report_year'] for r in unexplained_years]}"
        )
    if row_level:
        return result.warn(
            f"{len(row_level)} row-level difference(s) explain every yearly delta "
            f"({len(unmatched_tracker)} tracker-only, {len(unmatched_docs)} corpus-only, "
            f"{len(year_shifted)} filed under a different year) -- each needs a disposition"
        )
    return result.ok("tracker and corpus agree row for row and year for year")


# ---------------------------------------------------------------------------
# Q6 -- provenance cardinalities
# ---------------------------------------------------------------------------


def check_q6(con, manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q6", "Provenance cardinalities reconcile across the identity chain")

    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("documents", "logical_sources", "source_versions",
                      "extraction_artifacts", "file_aliases")
    }

    exceptions: list[dict] = []

    # Documents missing any identity id at all.
    for row in con.execute(
        """
        SELECT d.doc_id, k.ticker, d.filepath, d.parse_status, d.doc_quality_status
        FROM documents d JOIN companies k ON k.company_id = d.company_id
        WHERE d.logical_source_id IS NULL OR d.source_version_id IS NULL
           OR d.extraction_artifact_id IS NULL OR d.file_alias_id IS NULL
        """
    ):
        exceptions.append({
            "classification": "document_missing_provenance_id",
            "identifier": row["doc_id"], "ticker": row["ticker"],
            "detail": row["filepath"], "parse_status": row["parse_status"],
            "doc_quality_status": row["doc_quality_status"],
        })

    # Identity rows nothing points at: these are the gaps between 685/686 and
    # the 684 documents, and each one is either a superseded version or a leak.
    orphan_queries = {
        "logical_source_without_document": """
            SELECT l.logical_source_id AS id, k.ticker, l.title AS detail,
                   l.lifecycle_state, l.report_year
            FROM logical_sources l
            LEFT JOIN companies k ON k.company_id = l.company_id
            LEFT JOIN documents d ON d.logical_source_id = l.logical_source_id
            WHERE d.doc_id IS NULL""",
        "source_version_without_document": """
            SELECT v.source_version_id AS id, NULL AS ticker, v.logical_source_id AS detail,
                   v.lifecycle_state, NULL AS report_year
            FROM source_versions v
            LEFT JOIN documents d ON d.source_version_id = v.source_version_id
            WHERE d.doc_id IS NULL""",
        "extraction_artifact_without_document": """
            SELECT a.extraction_artifact_id AS id, NULL AS ticker, a.storage_path AS detail,
                   a.lifecycle_state, NULL AS report_year
            FROM extraction_artifacts a
            LEFT JOIN documents d ON d.extraction_artifact_id = a.extraction_artifact_id
            WHERE d.doc_id IS NULL""",
        "file_alias_without_document": """
            SELECT f.file_alias_id AS id, NULL AS ticker, f.file_path AS detail,
                   f.lifecycle_state, NULL AS report_year
            FROM file_aliases f
            LEFT JOIN documents d ON d.file_alias_id = f.file_alias_id
            WHERE d.doc_id IS NULL""",
    }
    orphan_counts = {}
    for classification, query in orphan_queries.items():
        rows = con.execute(query).fetchall()
        orphan_counts[classification] = len(rows)
        for row in rows:
            exceptions.append({
                "classification": classification,
                "identifier": row["id"], "ticker": row["ticker"],
                "detail": row["detail"], "parse_status": None,
                "doc_quality_status": row["lifecycle_state"],
            })

    # Two documents pointing at one identity row make the table count smaller
    # than the document count and hide an orphan behind it, so the gap alone
    # never balances. Name the sharers rather than reporting an unexplained gap.
    shared = {}
    for column, label in (
        ("logical_source_id", "logical_source"),
        ("source_version_id", "source_version"),
        ("extraction_artifact_id", "extraction_artifact"),
        ("file_alias_id", "file_alias"),
    ):
        rows = con.execute(
            f"""
            SELECT d.{column} AS id, COUNT(*) AS n,
                   GROUP_CONCAT(d.doc_id) AS doc_ids,
                   GROUP_CONCAT(d.filepath, ' | ') AS paths
            FROM documents d WHERE d.{column} IS NOT NULL
            GROUP BY d.{column} HAVING n > 1
            """
        ).fetchall()
        shared[label] = sum(row["n"] - 1 for row in rows)
        for row in rows:
            exceptions.append({
                "classification": f"documents_sharing_one_{label}",
                "identifier": row["id"], "ticker": None,
                "detail": f"doc_ids {row['doc_ids']}: {row['paths']}",
                "parse_status": None, "doc_quality_status": None,
            })

    # Dangling references in the other direction.
    dangling = {
        "source_version_without_logical_source": con.execute(
            """SELECT COUNT(*) FROM source_versions v
               LEFT JOIN logical_sources l ON l.logical_source_id = v.logical_source_id
               WHERE l.logical_source_id IS NULL"""
        ).fetchone()[0],
        "artifact_without_source_version": con.execute(
            """SELECT COUNT(*) FROM extraction_artifacts a
               LEFT JOIN source_versions v ON v.source_version_id = a.source_version_id
               WHERE v.source_version_id IS NULL"""
        ).fetchone()[0],
        "file_alias_without_source_version": con.execute(
            """SELECT COUNT(*) FROM file_aliases f
               LEFT JOIN source_versions v ON v.source_version_id = f.source_version_id
               WHERE v.source_version_id IS NULL"""
        ).fetchone()[0],
    }

    fan_out = {
        "versions_per_logical_source": dict(Counter(Counter(
            r[0] for r in con.execute("SELECT logical_source_id FROM source_versions")
        ).values())),
        "artifacts_per_source_version": dict(Counter(Counter(
            r[0] for r in con.execute("SELECT source_version_id FROM extraction_artifacts")
        ).values())),
        "aliases_per_source_version": dict(Counter(Counter(
            r[0] for r in con.execute("SELECT source_version_id FROM file_aliases")
        ).values())),
    }

    path = write_csv(out_dir / "provenance_exceptions.csv", exceptions,
                     ["classification", "identifier", "ticker", "detail",
                      "parse_status", "doc_quality_status"])

    gaps = {
        "logical_sources_minus_documents": counts["logical_sources"] - counts["documents"],
        "source_versions_minus_documents": counts["source_versions"] - counts["documents"],
        "extraction_artifacts_minus_documents":
            counts["extraction_artifacts"] - counts["documents"],
        "file_aliases_minus_documents": counts["file_aliases"] - counts["documents"],
    }
    # The identity that has to hold at every level:
    #     table rows = rows referenced by a document + rows referenced by none
    # and a document that shares its identity row with another document reduces
    # the first term by one. Expressed as a balance so the failure says which
    # level is off and by how much.
    balance = {}
    for level, (table, orphan_key, shared_key) in {
        "logical_sources": ("logical_sources", "logical_source_without_document",
                            "logical_source"),
        "source_versions": ("source_versions", "source_version_without_document",
                            "source_version"),
        "extraction_artifacts": ("extraction_artifacts", "extraction_artifact_without_document",
                                 "extraction_artifact"),
        "file_aliases": ("file_aliases", "file_alias_without_document", "file_alias"),
    }.items():
        expected = counts["documents"] - shared[shared_key] + orphan_counts[orphan_key]
        balance[level] = {
            "table_rows": counts[table],
            "referenced_by_a_document": counts["documents"] - shared[shared_key],
            "referenced_by_none": orphan_counts[orphan_key],
            "shared_by_two_documents": shared[shared_key],
            "residual": counts[table] - expected,
        }
    accounted = all(level["residual"] == 0 for level in balance.values())

    result.outputs = [str(path)]
    result.stats = {
        "row_counts": counts,
        "identity_gaps": gaps,
        "rows_not_referenced_by_any_document": orphan_counts,
        "documents_sharing_one_identity_row": shared,
        "balance": balance,
        "dangling_references": dangling,
        "fan_out": fan_out,
        "manifest_rows": len(manifest),
        "every_gap_named": accounted,
    }
    result.examples = exceptions[:examples_wanted]

    if any(dangling.values()):
        return result.fail(f"dangling identity references: {dangling}")
    if not accounted:
        off = {k: v["residual"] for k, v in balance.items() if v["residual"]}
        return result.fail(f"the identity balance does not close: {off}")
    if any(shared.values()):
        return result.fail(
            f"{max(shared.values())} document pair(s) share one identity row "
            f"({ {k: v for k, v in shared.items() if v} }) -- two documents claiming one "
            f"source is a duplicate ingestion, and every per-source figure double-counts it"
        )
    if exceptions:
        return result.warn(
            f"{len(exceptions)} identity row(s) reference no document -- the gap is named "
            f"but each row still needs a cause (superseded version, or a lost document)"
        )
    return result.ok("identity chain is 1:1:1:1 with documents; no gap to explain")


# ---------------------------------------------------------------------------
# Q7 -- the company universe
# ---------------------------------------------------------------------------


def check_q7(con, manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q7", "The company universe reconciles between manifest and database")

    universe = read_csv(config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV)
    if not universe:
        result.status = "SKIP"
        return result.ok(f"universe not found at {config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV}")

    universe_by_ticker = {norm_ticker(c.get("ticker")): c for c in universe}
    db_by_ticker = {
        norm_ticker(r["ticker"]): dict(r)
        for r in con.execute("SELECT ticker, name, sector FROM companies")
    }
    corpus_tickers = Counter(norm_ticker(d["ticker"]) for d in manifest)

    only_in_db = sorted(set(db_by_ticker) - set(universe_by_ticker))
    only_in_universe = sorted(set(universe_by_ticker) - set(db_by_ticker))

    # Before calling a company missing, check whether it is the same company
    # under a different ticker spelling (share-class suffixes, dots, hyphens).
    def base(ticker: str) -> str:
        return ticker.replace(".", "").replace("-", "").replace(" ", "")

    alias_candidates = [
        {"classification": "possible_ticker_alias", "ticker": a, "counterpart": b,
         "detail": "same ticker ignoring separators/suffix"}
        for a in only_in_universe
        for b in only_in_db
        if base(a) == base(b) or base(a).startswith(base(b)) or base(b).startswith(base(a))
    ]

    # A company that exists on both sides but contributes no document is not a
    # reconciliation failure; it is Checkpoint 4's coverage question. Counted
    # here so the two checkpoints agree on the number.
    zero_document_companies = sorted(
        t for t in set(db_by_ticker) & set(universe_by_ticker) if corpus_tickers.get(t, 0) == 0
    )

    exceptions = [
        {"classification": "in_database_not_in_universe", "ticker": t,
         "counterpart": None, "detail": db_by_ticker[t].get("name")}
        for t in only_in_db
    ] + [
        {"classification": "in_universe_not_in_database", "ticker": t,
         "counterpart": None, "detail": universe_by_ticker[t].get("company_name")}
        for t in only_in_universe
    ] + alias_candidates + [
        {"classification": "no_documents_in_corpus", "ticker": t, "counterpart": None,
         "detail": db_by_ticker[t].get("name")}
        for t in zero_document_companies
    ]

    path = write_csv(out_dir / "company_universe_exceptions.csv", exceptions,
                     ["classification", "ticker", "counterpart", "detail"])

    decisions = Counter((c.get("decision") or "").upper() for c in universe)

    result.outputs = [str(path)]
    result.stats = {
        "universe_companies": len(universe_by_ticker),
        "database_companies": len(db_by_ticker),
        "companies_with_at_least_one_document": len(corpus_tickers),
        "universe_decisions": dict(decisions),
        "in_database_not_in_universe": only_in_db,
        "in_universe_not_in_database": only_in_universe,
        "possible_ticker_aliases": len(alias_candidates),
        "companies_with_zero_documents": len(zero_document_companies),
    }
    result.examples = exceptions[:examples_wanted]

    if only_in_db or only_in_universe:
        return result.fail(
            f"{len(only_in_db)} database ticker(s) and {len(only_in_universe)} universe "
            f"ticker(s) do not reconcile"
        )
    if zero_document_companies:
        return result.warn(
            f"universe and database agree on {len(db_by_ticker)} companies, but "
            f"{len(zero_document_companies)} contribute no document (Checkpoint 4 classifies "
            f"them)"
        )
    return result.ok(f"universe and database agree on {len(db_by_ticker)} companies")


# ---------------------------------------------------------------------------
# Q8 -- additivity
# ---------------------------------------------------------------------------


def check_q8(con, manifest: list[dict], out_dir: Path) -> CheckResult:
    result = CheckResult("Q8", "The reported aggregates are additive")

    db_documents = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    db_sections = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    db_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    db_eligible = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE rag_action = ?", (RAG_ACTION_ELIGIBLE,)
    ).fetchone()[0]

    manifest_chunks = sum(d["chunk_count"] or 0 for d in manifest)
    manifest_sections = sum(d["section_count"] or 0 for d in manifest)
    manifest_eligible = sum(d["eligible_chunk_count"] or 0 for d in manifest)

    by_year = defaultdict(int)
    by_company = defaultdict(int)
    for doc in manifest:
        by_year[doc["report_year"]] += doc["chunk_count"] or 0
        by_company[doc["ticker"]] += doc["chunk_count"] or 0

    # A document with no report_year would silently vanish from the per-year
    # sum while still counting in the total, which is exactly how a corpus
    # total and the sum of its year bars come apart on a slide.
    chunks_without_year = sum(
        d["chunk_count"] or 0 for d in manifest if d["report_year"] is None
    )

    chunks_by_section = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE section_id IS NOT NULL"
    ).fetchone()[0]
    sections_with_no_document = con.execute(
        """SELECT COUNT(*) FROM sections s
           LEFT JOIN documents d ON d.doc_id = s.doc_id WHERE d.doc_id IS NULL"""
    ).fetchone()[0]
    chunks_with_no_document = con.execute(
        """SELECT COUNT(*) FROM chunks c
           LEFT JOIN documents d ON d.doc_id = c.doc_id WHERE d.doc_id IS NULL"""
    ).fetchone()[0]
    ineligible = db_chunks - db_eligible

    rows = [
        {"aggregate": "documents: manifest vs database",
         "reported": len(manifest), "expected": db_documents,
         "residual": len(manifest) - db_documents},
        {"aggregate": "sections: sum over manifest vs database",
         "reported": manifest_sections, "expected": db_sections,
         "residual": manifest_sections - db_sections},
        {"aggregate": "chunks: sum over manifest vs database",
         "reported": manifest_chunks, "expected": db_chunks,
         "residual": manifest_chunks - db_chunks},
        {"aggregate": "chunks: sum over report_year vs corpus total",
         "reported": sum(by_year.values()), "expected": manifest_chunks,
         "residual": sum(by_year.values()) - manifest_chunks},
        {"aggregate": "chunks: sum over company vs corpus total",
         "reported": sum(by_company.values()), "expected": manifest_chunks,
         "residual": sum(by_company.values()) - manifest_chunks},
        {"aggregate": "chunks: eligible + ineligible vs corpus total",
         "reported": db_eligible + ineligible, "expected": db_chunks,
         "residual": (db_eligible + ineligible) - db_chunks},
        {"aggregate": "eligible chunks: sum over manifest vs database",
         "reported": manifest_eligible, "expected": db_eligible,
         "residual": manifest_eligible - db_eligible},
        {"aggregate": "chunks attached to a section vs corpus total",
         "reported": chunks_by_section, "expected": db_chunks,
         "residual": chunks_by_section - db_chunks},
        {"aggregate": "sections orphaned from any document",
         "reported": sections_with_no_document, "expected": 0,
         "residual": sections_with_no_document},
        {"aggregate": "chunks orphaned from any document",
         "reported": chunks_with_no_document, "expected": 0,
         "residual": chunks_with_no_document},
        {"aggregate": "chunks in documents carrying no report_year",
         "reported": chunks_without_year, "expected": 0,
         "residual": chunks_without_year},
    ]

    path = write_csv(out_dir / "additivity.csv", rows,
                     ["aggregate", "reported", "expected", "residual"])
    failing = [r for r in rows if r["residual"] != 0]

    result.outputs = [str(path)]
    result.stats = {
        "checks": len(rows),
        "non_zero_residuals": len(failing),
        "corpus_chunk_total": db_chunks,
        "eligible_chunks": db_eligible,
        "ineligible_chunks": ineligible,
        "distinct_report_years": len([y for y in by_year if y is not None]),
        "distinct_companies_with_chunks": len([t for t, n in by_company.items() if n]),
    }
    result.examples = failing[:len(rows)]

    if failing:
        return result.fail(
            f"{len(failing)} aggregate(s) do not add up: "
            + "; ".join(f"{r['aggregate']} residual {r['residual']}" for r in failing)
        )
    return result.ok(f"all {len(rows)} aggregates reconcile with zero residual")


# ---------------------------------------------------------------------------
# Q9 -- downloaded but never indexed
# ---------------------------------------------------------------------------


def check_q9(manifest: list[dict], out_dir: Path, examples_wanted: int) -> CheckResult:
    result = CheckResult("Q9", "Reports downloaded but never indexed")

    tracker = read_csv(config.SUSTAINABILITY_TRACKER_CSV)
    if not tracker:
        result.status = "SKIP"
        return result.ok(f"tracker not found at {config.SUSTAINABILITY_TRACKER_CSV}")

    docs_by_filename: dict[str, list[dict]] = defaultdict(list)
    docs_by_ticker_year: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
    for doc in manifest:
        docs_by_filename[norm_name(doc["filename"])].append(doc)
        docs_by_ticker_year[(norm_ticker(doc["ticker"]), doc["report_year"])].append(doc)

    downloaded_entries = [
        entry for entry in tracker
        if (entry.get("status") or "").strip().lower() == "downloaded"
    ]
    unavailable_entries = [
        entry for entry in tracker
        if (entry.get("status") or "").strip().lower() != "downloaded"
    ]

    claimed: set[int] = set()
    rows: list[dict] = []
    for entry in downloaded_entries:
        ticker = norm_ticker(entry.get("ticker"))
        year = as_int(entry.get("report_year"))
        filename = (entry.get("notes") or "").strip()

        doc = next(
            (d for d in docs_by_filename.get(norm_name(filename), [])
             if d["doc_id"] not in claimed), None
        )
        match_key = "filename"
        if doc is None:
            doc = next(
                (d for d in docs_by_ticker_year.get((ticker, year), [])
                 if d["doc_id"] not in claimed), None
            )
            match_key = "ticker_year" if doc else None
        if doc is not None:
            claimed.add(doc["doc_id"])

        if doc is None:
            loss = "no_document_row"
        elif (doc["parse_status"] or "") != PARSE_STATUS_OK:
            loss = "not_parsed"
        elif (doc["chunk_count"] or 0) == 0:
            loss = "parsed_no_chunks"
        elif (doc["eligible_chunk_count"] or 0) == 0:
            loss = "no_eligible_chunks"
        else:
            loss = "indexed"

        rows.append({
            "loss_class": loss,
            "ticker": ticker,
            "report_year": year,
            "tracker_status": entry.get("status"),
            "filename": filename,
            "match_key": match_key,
            "doc_id": doc["doc_id"] if doc else None,
            "parse_status": doc["parse_status"] if doc else None,
            "doc_quality_status": doc["doc_quality_status"] if doc else None,
            "page_count": doc["page_count"] if doc else None,
            "byte_size": doc["byte_size"] if doc else None,
            "chunks": doc["chunk_count"] if doc else None,
            "eligible_chunks": doc["eligible_chunk_count"] if doc else None,
            "drive_file_link": entry.get("drive_file_link"),
        })

    losses = [r for r in rows if r["loss_class"] != "indexed"]
    path = write_csv(out_dir / "ingestion_losses.csv", losses,
                     ["loss_class", "ticker", "report_year", "tracker_status", "filename",
                      "match_key", "doc_id", "parse_status", "doc_quality_status",
                      "page_count", "byte_size", "chunks", "eligible_chunks",
                      "drive_file_link"])

    unavailable_rows = [{
        "tracker_status": (entry.get("status") or "").strip(),
        "ticker": norm_ticker(entry.get("ticker")),
        "report_year": as_int(entry.get("report_year")),
        "filename": (entry.get("notes") or "").strip(),
        "drive_file_link": entry.get("drive_file_link"),
    } for entry in unavailable_entries]
    unavailable_path = write_csv(
        out_dir / "unavailable_source_history.csv",
        unavailable_rows,
        ["tracker_status", "ticker", "report_year", "filename", "drive_file_link"],
    )

    counts = Counter(r["loss_class"] for r in rows)
    unavailable_statuses = Counter(
        (entry.get("status") or "").strip().lower() or "blank"
        for entry in unavailable_entries
    )

    result.outputs = [str(path), str(unavailable_path)]
    result.stats = {
        "tracker_rows": len(tracker),
        "tracker_rows_marked_downloaded": len(downloaded_entries),
        "tracker_rows_not_marked_downloaded": len(unavailable_entries),
        "non_downloaded_statuses": dict(unavailable_statuses),
        "by_loss_class": {c: counts.get(c, 0) for c in LOSS_CLASSES},
        "documents_never_claimed_by_a_tracker_row": len(
            [d for d in manifest if d["doc_id"] not in claimed]
        ),
        "downloaded_loss_rate": round(len(losses) / len(rows), 4) if rows else None,
    }
    result.examples = losses[:examples_wanted]

    hard_losses = counts.get("no_document_row", 0) + counts.get("not_parsed", 0) \
        + counts.get("parsed_no_chunks", 0)
    if hard_losses:
        return result.fail(
            f"{hard_losses} downloaded report(s) produced no indexable content "
            f"({dict((c, counts.get(c, 0)) for c in LOSS_CLASSES[:3])})"
        )
    if counts.get("no_eligible_chunks", 0):
        return result.warn(
            f"{counts['no_eligible_chunks']} report(s) were chunked but hold no eligible "
            f"chunk (Checkpoint 5 accounts for the gate that removed them)"
        )
    return result.ok(
        f"all {len(rows)} downloaded tracker reports reached the index; "
        f"{len(unavailable_entries)} non-downloaded source-history row(s) are listed separately"
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], header: dict) -> None:
    print("=" * 78)
    print("STAGE 2 CHECKPOINT 1 -- reconciliation against the source of record")
    print("=" * 78)
    print(f"manifest version : {header['manifest_version']}")
    print(f"snapshot dir     : {header['snapshot_dir']}")
    print(f"database         : {header['database']}")
    print(f"database sha256  : {header['database_sha256']} ({header['database_state']})")
    print(f"tracker          : {header['tracker']}")
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
    print(f"Checkpoint 1 gate: {gate}   ({dict(statuses)})")
    print(
        "The gate is cleared when every yearly delta in Q5 is explained by named rows and "
        "every residual in Q8 is zero. No count slide circulates before that."
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
                        help="output directory (default: <snapshot>/checkpoint1/)")
    parser.add_argument("--allow-db-drift", action="store_true",
                        help="continue with a warning when the database no longer matches the "
                             "snapshot's SHA-256 (by default this is fatal)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max examples to show per question (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON "
                             "(default: <out-dir>/checkpoint1.json)")
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

    # The snapshot is the corpus; a database that has moved underneath it makes
    # every reconciliation below meaningless, so it is checked before any work.
    actual_sha = sha256_file(db_path)
    expected_sha = stamp.get("database_sha256")
    if actual_sha == expected_sha:
        db_state = "matches the snapshot"
    elif args.allow_db_drift:
        db_state = f"DRIFTED from the snapshot ({expected_sha}) -- continuing under --allow-db-drift"
        print(f"warning: database has changed since the snapshot was frozen", file=sys.stderr)
    else:
        raise SystemExit(
            f"database has changed since the snapshot was frozen.\n"
            f"  snapshot: {expected_sha}\n  current : {actual_sha}\n"
            f"Re-run checkpoint 0, or pass --allow-db-drift to proceed anyway."
        )

    out_dir = args.out_dir or (snapshot_dir / "checkpoint1")
    out_dir.mkdir(parents=True, exist_ok=True)

    con = connect(db_path)
    try:
        results = [
            check_q5(manifest, out_dir, args.examples),
            check_q6(con, manifest, out_dir, args.examples),
            check_q7(con, manifest, out_dir, args.examples),
            check_q8(con, manifest, out_dir),
            check_q9(manifest, out_dir, args.examples),
        ]
    finally:
        con.close()

    header = {
        "manifest_version": stamp.get("manifest_version"),
        "snapshot_dir": str(snapshot_dir),
        "database": str(db_path),
        "database_sha256": actual_sha,
        "database_state": db_state,
        "tracker": str(config.SUSTAINABILITY_TRACKER_CSV),
        "out_dir": str(out_dir),
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        **header,
        "results": [
            {"question": r.key, "title": r.title, "status": r.status, "headline": r.headline,
             "stats": r.stats, "examples": r.examples, "outputs": r.outputs}
            for r in results
        ],
    }
    (out_dir / "checkpoint1.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    render(results, header)
    print(f"result set written to {out_dir / 'checkpoint1.json'}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
