"""Read-only checks for the ESG source-identity migration.

Run this before and after V6. It never creates, updates, or deletes rows.
Warnings identify records that need review. Errors mean the migration or an ESG
load should not be approved yet.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Any


REQUIRED_BASE_TABLES = {"companies", "documents", "sections", "chunks"}
REQUIRED_IDENTITY_TABLES = {
    "logical_sources",
    "source_versions",
    "file_aliases",
    "extraction_artifacts",
    "source_approvals",
}
LINEAGE_COLUMNS = {
    "documents": {
        "logical_source_id",
        "source_version_id",
        "extraction_artifact_id",
        "lifecycle_state",
    },
    "sections": {
        "logical_source_id",
        "source_version_id",
        "extraction_artifact_id",
        "lifecycle_state",
    },
    "chunks": {
        "logical_source_id",
        "source_version_id",
        "extraction_artifact_id",
        "lifecycle_state",
    },
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: str
    count: int
    detail: str


def _fetch_count(cursor: Any, sql: str) -> int:
    cursor.execute(sql)
    row = cursor.fetchone()
    return int(row[0] or 0)


def inspect_schema(cursor: Any) -> tuple[set[str], dict[str, set[str]]]:
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
        """
    )
    columns: dict[str, set[str]] = {}
    for table_name, column_name in cursor.fetchall():
        columns.setdefault(table_name, set()).add(column_name)
    return set(columns), columns


def preflight_checks(cursor: Any, tables: set[str], columns: dict[str, set[str]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing = sorted(REQUIRED_BASE_TABLES - tables)
    results.append(
        CheckResult(
            "base_tables_present",
            "error" if missing else "ok",
            len(missing),
            "missing: " + ", ".join(missing) if missing else "all base tables are present",
        )
    )
    if missing:
        return results

    blank_paths = _fetch_count(
        cursor,
        "SELECT COUNT(*) FROM documents WHERE filepath IS NULL OR BTRIM(filepath) = ''",
    )
    results.append(
        CheckResult(
            "blank_document_paths",
            "error" if blank_paths else "ok",
            blank_paths,
            "legacy paths are needed to seed file_aliases",
        )
    )

    orphan_sections = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM sections AS section
        LEFT JOIN documents AS document ON document.doc_id = section.doc_id
        WHERE document.doc_id IS NULL
        """,
    )
    orphan_chunks = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM chunks AS chunk
        LEFT JOIN documents AS document ON document.doc_id = chunk.doc_id
        LEFT JOIN sections AS section ON section.section_id = chunk.section_id
        WHERE document.doc_id IS NULL OR section.section_id IS NULL
        """,
    )
    results.extend(
        [
            CheckResult(
                "orphan_sections",
                "error" if orphan_sections else "ok",
                orphan_sections,
                "sections must reference an existing document",
            ),
            CheckResult(
                "orphan_chunks",
                "error" if orphan_chunks else "ok",
                orphan_chunks,
                "chunks must reference an existing document and section",
            ),
        ]
    )

    if {"source_id", "source_version_id"}.issubset(columns.get("chunks", set())):
        ambiguous_versions = _fetch_count(
            cursor,
            """
            SELECT COUNT(*)
            FROM (
                SELECT BTRIM(source_version_id)
                FROM chunks
                WHERE source_version_id IS NOT NULL
                  AND BTRIM(source_version_id) <> ''
                GROUP BY BTRIM(source_version_id)
                HAVING COUNT(DISTINCT NULLIF(BTRIM(source_id), '')) <> 1
            ) AS ambiguous
            """,
        )
        multi_owner_sources = _fetch_count(
            cursor,
            """
            SELECT COUNT(*)
            FROM (
                SELECT BTRIM(source_id)
                FROM chunks
                WHERE source_id IS NOT NULL AND BTRIM(source_id) <> ''
                GROUP BY BTRIM(source_id)
                HAVING COUNT(DISTINCT company_id) > 1
            ) AS multi_owner
            """,
        )
        multi_version_documents = _fetch_count(
            cursor,
            """
            SELECT COUNT(*)
            FROM (
                SELECT doc_id
                FROM chunks
                WHERE source_version_id IS NOT NULL
                  AND BTRIM(source_version_id) <> ''
                GROUP BY doc_id
                HAVING COUNT(DISTINCT BTRIM(source_version_id)) > 1
            ) AS multi_version
            """,
        )
        results.extend(
            [
                CheckResult(
                    "ambiguous_legacy_versions",
                    "warning" if ambiguous_versions else "ok",
                    ambiguous_versions,
                    "V6 places these versions under review-only legacy owners",
                ),
                CheckResult(
                    "logical_sources_with_multiple_companies",
                    "warning" if multi_owner_sources else "ok",
                    multi_owner_sources,
                    "these identities need ownership review",
                ),
                CheckResult(
                    "documents_with_multiple_chunk_versions",
                    "warning" if multi_version_documents else "ok",
                    multi_version_documents,
                    "V6 will not guess which version is current",
                ),
            ]
        )
    return results


def postflight_checks(cursor: Any, tables: set[str], columns: dict[str, set[str]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing_tables = sorted(REQUIRED_IDENTITY_TABLES - tables)
    results.append(
        CheckResult(
            "identity_tables_present",
            "error" if missing_tables else "ok",
            len(missing_tables),
            "missing: " + ", ".join(missing_tables) if missing_tables else "all identity tables are present",
        )
    )
    missing_columns = {
        table: sorted(required - columns.get(table, set()))
        for table, required in LINEAGE_COLUMNS.items()
        if required - columns.get(table, set())
    }
    results.append(
        CheckResult(
            "lineage_columns_present",
            "error" if missing_columns else "ok",
            sum(len(value) for value in missing_columns.values()),
            json.dumps(missing_columns, sort_keys=True) if missing_columns else "all lineage columns are present",
        )
    )
    if missing_tables or missing_columns:
        return results

    for table in ("documents", "sections", "chunks"):
        missing_lineage = _fetch_count(
            cursor,
            f"""
            SELECT COUNT(*) FROM {table}
            WHERE logical_source_id IS NULL
               OR source_version_id IS NULL
               OR extraction_artifact_id IS NULL
            """,
        )
        results.append(
            CheckResult(
                f"{table}_missing_lineage",
                "error" if missing_lineage else "ok",
                missing_lineage,
                "all migrated rows must have complete lineage",
            )
        )

    version_mismatch = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM (
            SELECT logical_source_id, source_version_id FROM documents
            UNION ALL
            SELECT logical_source_id, source_version_id FROM sections
            UNION ALL
            SELECT logical_source_id, source_version_id FROM chunks
        ) AS child
        LEFT JOIN source_versions AS version
          ON version.logical_source_id = child.logical_source_id
         AND version.source_version_id = child.source_version_id
        WHERE version.source_version_id IS NULL
        """,
    )
    artifact_mismatch = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM (
            SELECT source_version_id, extraction_artifact_id FROM documents
            UNION ALL
            SELECT source_version_id, extraction_artifact_id FROM sections
            UNION ALL
            SELECT source_version_id, extraction_artifact_id FROM chunks
        ) AS child
        LEFT JOIN extraction_artifacts AS artifact
          ON artifact.source_version_id = child.source_version_id
         AND artifact.extraction_artifact_id = child.extraction_artifact_id
        WHERE artifact.extraction_artifact_id IS NULL
        """,
    )
    approval_hash_mismatch = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM source_approvals AS approval
        JOIN source_versions AS version
          ON version.source_version_id = approval.source_version_id
        JOIN extraction_artifacts AS artifact
          ON artifact.extraction_artifact_id = approval.extraction_artifact_id
        WHERE approval.approval_status = 'approved'
          AND (
              version.original_sha256 IS NULL
              OR artifact.artifact_sha256 IS NULL
              OR approval.approved_source_sha256 <> version.original_sha256
              OR approval.approved_artifact_sha256 <> artifact.artifact_sha256
          )
        """,
    )
    bad_superseded_rows = _fetch_count(
        cursor,
        """
        SELECT
            (SELECT COUNT(*) FROM logical_sources
             WHERE lifecycle_state = 'superseded'
               AND superseded_by_logical_source_id IS NULL)
          + (SELECT COUNT(*) FROM source_versions
             WHERE lifecycle_state = 'superseded'
               AND superseded_by_source_version_id IS NULL)
          + (SELECT COUNT(*) FROM extraction_artifacts
             WHERE lifecycle_state = 'superseded'
               AND superseded_by_extraction_artifact_id IS NULL)
          + (SELECT COUNT(*) FROM documents
             WHERE lifecycle_state = 'superseded' AND superseded_by_doc_id IS NULL)
          + (SELECT COUNT(*) FROM sections
             WHERE lifecycle_state = 'superseded' AND superseded_by_section_id IS NULL)
          + (SELECT COUNT(*) FROM chunks
             WHERE lifecycle_state = 'superseded' AND superseded_by_chunk_id IS NULL)
        """,
    )
    unvalidated_constraints = _fetch_count(
        cursor,
        """
        SELECT COUNT(*)
        FROM pg_constraint
        WHERE conrelid IN ('documents'::regclass, 'sections'::regclass, 'chunks'::regclass)
          AND conname ~ '^(fk|ck)_(documents|sections|chunks)_'
          AND NOT convalidated
        """,
    )
    results.extend(
        [
            CheckResult(
                "logical_version_mismatches",
                "error" if version_mismatch else "ok",
                version_mismatch,
                "child logical_source_id and source_version_id must describe one version",
            ),
            CheckResult(
                "version_artifact_mismatches",
                "error" if artifact_mismatch else "ok",
                artifact_mismatch,
                "child extraction artifacts must belong to the selected source version",
            ),
            CheckResult(
                "approved_hash_mismatches",
                "error" if approval_hash_mismatch else "ok",
                approval_hash_mismatch,
                "approved OCR/VLM hashes must still match the version and artifact",
            ),
            CheckResult(
                "superseded_rows_without_successor",
                "error" if bad_superseded_rows else "ok",
                bad_superseded_rows,
                "superseded history must point to its replacement",
            ),
            CheckResult(
                "unvalidated_lineage_constraints",
                "error" if unvalidated_constraints else "ok",
                unvalidated_constraints,
                "validate every V6 lineage constraint before approval",
            ),
        ]
    )
    return results


def run_validation(connection: Any, phase: str = "auto") -> tuple[str, list[CheckResult]]:
    connection.set_session(readonly=True, autocommit=False)
    with connection.cursor() as cursor:
        tables, columns = inspect_schema(cursor)
        selected_phase = phase
        if selected_phase == "auto":
            selected_phase = "post" if REQUIRED_IDENTITY_TABLES.issubset(tables) else "pre"
        if selected_phase == "pre":
            results = preflight_checks(cursor, tables, columns)
        else:
            results = postflight_checks(cursor, tables, columns)
    connection.rollback()
    return selected_phase, results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only checks before or after the ESG DB identity migration."
    )
    parser.add_argument("--database-url", default=os.getenv("DB_URL") or os.getenv("DATABASE_URL"))
    parser.add_argument("--phase", choices=("auto", "pre", "post"), default="auto")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DB_URL/DATABASE_URL is required")

    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise SystemExit("psycopg2 is required; install project requirements first") from exc

    connection = psycopg2.connect(args.database_url)
    try:
        phase, results = run_validation(connection, args.phase)
    finally:
        connection.close()

    if args.json:
        print(json.dumps({"phase": phase, "checks": [asdict(result) for result in results]}, indent=2))
    else:
        print(f"ESG DB identity {phase}-migration validation (read-only):")
        for result in results:
            print(f"  [{result.severity.upper()}] {result.name}: {result.count} - {result.detail}")
        print("No database writes were performed.")
    return 1 if any(result.severity == "error" and result.count for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
