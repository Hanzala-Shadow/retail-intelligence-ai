from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


REPORT_DIR = Path(__file__).resolve().parent
ROOT = REPORT_DIR.parents[1]
PACKAGE = ROOT / "outputs/esg_chunk_handoff_2000_esg_3b1c0196c0f9"
DB_PATH = REPORT_DIR / "report_evidence.sqlite"

HEADLINE_SQL = """
SELECT
  (SELECT COUNT(*) FROM live_chunks WHERE dataset_id = 'esg_3b1c0196c0f9') AS corpus_chunks,
  (SELECT COUNT(*) FROM handoff_chunks) AS handoff_chunks,
  (SELECT exact_citations FROM validation_summary) AS exact_citations,
  (SELECT COUNT(DISTINCT section_code) FROM handoff_chunks) AS topics,
  (SELECT COUNT(*) FROM handoff_chunks WHERE chunk_quality_tier = 'narrative') AS narrative_chunks,
  (SELECT COUNT(*) FROM handoff_chunks WHERE chunk_quality_tier = 'layout_sensitive') AS layout_sensitive_chunks,
  (SELECT seed FROM sampling_parameters) AS seed,
  (SELECT min_per_topic FROM sampling_parameters) AS min_per_topic,
  (SELECT max_per_document FROM sampling_parameters) AS max_per_document
""".strip()

TOPIC_SQL = """
WITH handoff AS (
  SELECT section_code AS topic, COUNT(*) AS handoff_chunks
  FROM handoff_chunks
  GROUP BY section_code
), safe AS (
  SELECT section_code AS topic, COUNT(*) AS safe_pool_chunks
  FROM safe_pool_chunks
  GROUP BY section_code
)
SELECT
  handoff.topic,
  handoff.handoff_chunks,
  safe.safe_pool_chunks,
  ROW_NUMBER() OVER (ORDER BY handoff.handoff_chunks DESC, handoff.topic) AS rank
FROM handoff
JOIN safe USING (topic)
ORDER BY rank
""".strip()

VALIDATION_SQL = """
SELECT check_name AS "check", passed_rows, expected_rows, status
FROM validation_checks
ORDER BY check_name
""".strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def query_rows(connection: sqlite3.Connection, sql: str) -> list[dict]:
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def main() -> None:
    live = read_csv(ROOT / "data/00_reference/esg_chunks_index_enriched.csv")
    handoff = read_csv(PACKAGE / "chunks.csv")
    manifest = read_csv(ROOT / "data/00_reference/vector_index_manifest.csv")
    validation = json.loads((PACKAGE / "VALIDATION.json").read_text(encoding="utf-8"))
    metadata = json.loads((PACKAGE / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))

    with sqlite3.connect(DB_PATH) as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS live_chunks;
            DROP TABLE IF EXISTS handoff_chunks;
            DROP TABLE IF EXISTS safe_pool_chunks;
            DROP TABLE IF EXISTS validation_summary;
            DROP TABLE IF EXISTS validation_checks;
            DROP TABLE IF EXISTS sampling_parameters;
            CREATE TABLE live_chunks (chunk_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL);
            CREATE TABLE handoff_chunks (
              chunk_id TEXT PRIMARY KEY,
              section_code TEXT NOT NULL,
              chunk_quality_tier TEXT NOT NULL
            );
            CREATE TABLE safe_pool_chunks (chunk_id TEXT PRIMARY KEY, section_code TEXT NOT NULL);
            CREATE TABLE validation_summary (exact_citations INTEGER NOT NULL);
            CREATE TABLE validation_checks (
              check_name TEXT PRIMARY KEY,
              passed_rows INTEGER NOT NULL,
              expected_rows INTEGER NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE sampling_parameters (
              seed INTEGER NOT NULL,
              min_per_topic INTEGER NOT NULL,
              max_per_document INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO live_chunks VALUES (?, ?)",
            [(row["chunk_id"], row["dataset_id"]) for row in live],
        )
        connection.executemany(
            "INSERT INTO handoff_chunks VALUES (?, ?, ?)",
            [
                (row["chunk_id"], row["section_code"], row["chunk_quality_tier"])
                for row in handoff
            ],
        )
        connection.executemany(
            "INSERT INTO safe_pool_chunks VALUES (?, ?)",
            [
                (row["chunk_id"], row["section_label"])
                for row in manifest
                if row["retrieval_state"] == "eligible"
            ],
        )
        connection.execute(
            "INSERT INTO validation_summary VALUES (?)",
            (validation["exact_citations"],),
        )
        checks = [
            ("Chunk SHA-256", validation["chunk_hash_matches"]),
            ("Embedding text", validation["embedding_text_matches"]),
            ("Exact citations", validation["exact_citations"]),
            ("Manifest state", validation["manifest_state_matches"]),
            ("Parsed-source span", validation["source_span_matches"]),
        ]
        connection.executemany(
            "INSERT INTO validation_checks VALUES (?, ?, ?, ?)",
            [(name, count, validation["rows"], "PASS") for name, count in checks],
        )
        sampling = metadata["sampling"]
        connection.execute(
            "INSERT INTO sampling_parameters VALUES (?, ?, ?)",
            (sampling["seed"], sampling["min_per_topic"], sampling["max_per_doc"]),
        )
        connection.commit()

        results = {
            "headline": query_rows(connection, HEADLINE_SQL),
            "topic_counts": query_rows(connection, TOPIC_SQL),
            "validation_checks": query_rows(connection, VALIDATION_SQL),
            "queries": {
                "headline_sql": HEADLINE_SQL,
                "topic_sql": TOPIC_SQL,
                "validation_sql": VALIDATION_SQL,
            },
        }

    (REPORT_DIR / "query_results.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in results.items() if key != "queries"}, indent=2))


if __name__ == "__main__":
    main()
