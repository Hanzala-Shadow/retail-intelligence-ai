#!/usr/bin/env python3
"""Stream the frozen FY2325 v2.16 corpus and embeddings into isolated tables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

DATASET = "fy2325-v2.16"
MANIFEST_SHA = "fd05c470aaf63be6ae0524c016f7eee61747976721e6f12aa242033b8badc4eb"
CHUNKER_CONFIG = "f79dc25715bc364ceb21d2a84e433066f8177aa0822e1db9c18a67b6514fd936"
MODEL = "BAAI/bge-base-en-v1.5"
REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
EXPECTED = {
    "companies": 190, "filings": 561, "documents": 561, "sections": 13455,
    "chunks": 224561, "include": 158570, "exclude": 65991, "embeddings": 158570,
}


def rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                yield json.loads(line)
            except Exception as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSON") from exc


def batched(iterable, size):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def progress(stage, done, total, started):
    elapsed = max(time.time() - started, 0.001)
    rate = done / elapsed
    eta = (total - done) / rate if rate else 0
    print(
        f"PROGRESS stage={stage} rows={done}/{total} "
        f"percent={100*done/total:.1f} elapsed={elapsed:.1f}s eta={eta:.1f}s",
        flush=True,
    )


def connect():
    load_dotenv()
    url = os.getenv("DB_URL")
    if not url:
        raise RuntimeError("DB_URL is not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def initialize_dataset(cur):
    cur.execute(
        """
        INSERT INTO fy2325_v2_datasets
          (dataset_id,status,manifest_sha256,chunker_version,chunker_config_sha256,
           embedding_model,embedding_revision,embedding_dimension,normalized)
        VALUES (%s,'loading',%s,'fy2325-chunker-v2.16',%s,%s,%s,768,true)
        ON CONFLICT (dataset_id) DO UPDATE SET
          status='loading',
          manifest_sha256=excluded.manifest_sha256,
          chunker_version=excluded.chunker_version,
          chunker_config_sha256=excluded.chunker_config_sha256,
          embedding_model=excluded.embedding_model,
          embedding_revision=excluded.embedding_revision,
          embedding_dimension=excluded.embedding_dimension,
          normalized=excluded.normalized
        """,
        (DATASET, MANIFEST_SHA, CHUNKER_CONFIG, MODEL, REVISION),
    )


def load_relational(conn, root: Path, batch_size: int):
    ref = root / "data_v2/00_reference"
    parsed_root = root / "data_v2/02_interim/full_v2_2_final"
    section_root = root / "data_v2/03_sections/full_v2_7_final"
    chunk_root = root / "data_v2/04_chunks/full_profile_A_v216_conservative_final"
    cur = conn.cursor()
    initialize_dataset(cur)

    with (ref / "approved_companies.csv").open(newline="", encoding="utf-8") as handle:
        company_rows = list(csv.DictReader(handle))
    if len(company_rows) != EXPECTED["companies"]:
        raise RuntimeError(f"company count {len(company_rows)} != 190")
    execute_values(
        cur,
        """
        INSERT INTO fy2325_v2_companies
          (dataset_id,company_id,ticker,cik,name,sector,exchange,ipo_date,
           fiscal_year_end_month,fiscal_year_end_day,fiscal_year_end_source,
           fiscal_year_end_status)
        VALUES %s ON CONFLICT (dataset_id,company_id) DO NOTHING
        """,
        [(
            DATASET, int(r["company_id"]), r["ticker"], r["cik"], r["name"],
            r["sector"] or None, r["exchange"] or None, r["ipo_date"] or None,
            int(r["fiscal_year_end_month"]), int(r["fiscal_year_end_day"]),
            r["fiscal_year_end_source"], r["fiscal_year_end_status"],
        ) for r in company_rows],
    )

    with (ref / "filing_selection_manifest.csv").open(newline="", encoding="utf-8") as handle:
        filing_rows = list(csv.DictReader(handle))
    if len(filing_rows) != EXPECTED["filings"]:
        raise RuntimeError(f"filing count {len(filing_rows)} != 561")
    execute_values(
        cur,
        """
        INSERT INTO fy2325_v2_filings
          (dataset_id,company_id,ticker,cik,coverage_year,filing_year,filing_date,
           accession_number,document_period_end_date,document_fiscal_year_focus,
           form_type,is_amendment,source_file,source_sha256,selection_method,
           selection_status)
        VALUES %s ON CONFLICT (dataset_id,accession_number) DO NOTHING
        """,
        [(
            DATASET, int(r["company_id"]), r["ticker"], r["cik"],
            int(r["coverage_year"]), int(r["filing_year"]), r["filing_date"],
            r["accession_number"], r["document_period_end_date"] or None,
            int(r["document_fiscal_year_focus"]) if r["document_fiscal_year_focus"] else None,
            r["form_type"], r["is_amendment"].lower() == "true", r["source_file"],
            r["source_sha256"], r["selection_method"], r["selection_status"],
        ) for r in filing_rows],
    )
    cur.execute(
        "SELECT accession_number,filing_pk FROM fy2325_v2_filings WHERE dataset_id=%s",
        (DATASET,),
    )
    filing_map = dict(cur.fetchall())

    document_rows = list(rows(parsed_root / "parsed_documents.jsonl"))
    if len(document_rows) != EXPECTED["documents"]:
        raise RuntimeError(f"document count {len(document_rows)} != 561")
    execute_values(
        cur,
        """
        INSERT INTO fy2325_v2_documents
          (dataset_id,filing_pk,accession_number,company_id,ticker,coverage_year,
           output_file,source_file,source_sha256,text_sha256,parser_version,
           parser_config_sha256,parse_status,char_count,semantic_table_count,
           layout_table_count,quality_flags)
        VALUES %s ON CONFLICT (dataset_id,accession_number) DO NOTHING
        """,
        [(
            DATASET, filing_map[r["accession_number"]], r["accession_number"],
            int(r["company_id"]), r["ticker"], int(r["coverage_year"]),
            r["output_file"], r["source_file"], r["source_sha256"], r["text_sha256"],
            r["parser_version"], r["parser_config_sha256"], r["parse_status"],
            int(r["char_count"]), int(r["semantic_table_count"]),
            int(r["layout_table_count"]), Json(r["quality_flags"]),
        ) for r in document_rows],
    )
    cur.execute(
        "SELECT accession_number,document_pk FROM fy2325_v2_documents WHERE dataset_id=%s",
        (DATASET,),
    )
    document_map = dict(cur.fetchall())

    started = time.time()
    done = 0
    section_path = section_root / "sections.jsonl"
    for batch in batched(rows(section_path), batch_size):
        values = []
        for r in batch:
            text_path = root / r["output_file"]
            section_text = text_path.read_text(encoding="utf-8")
            if sha256(text_path) != r["section_text_sha256"]:
                raise RuntimeError(f"section hash mismatch: {r['section_id']}")
            values.append((
                DATASET, document_map[r["accession_number"]], r["section_id"],
                r["accession_number"], int(r["company_id"]), r["ticker"],
                int(r["coverage_year"]), r["canonical_section_code"],
                r["section_heading"], r["subsection_heading"], section_text,
                r["output_file"], int(r["source_start_char"]), int(r["source_end_char"]),
                r["source_text_sha256"], r["section_text_sha256"], r["splitter_version"],
                r["splitter_config_sha256"], r["boundary_method"],
                r["boundary_confidence"], r["quality_status"], Json(r["quality_flags"]),
                r["rag_action"],
            ))
        execute_values(
            cur,
            """
            INSERT INTO fy2325_v2_sections
              (dataset_id,document_pk,source_section_id,accession_number,company_id,
               ticker,coverage_year,canonical_section_code,section_heading,
               subsection_heading,section_text,output_file,source_start_char,
               source_end_char,source_text_sha256,section_text_sha256,splitter_version,
               splitter_config_sha256,boundary_method,boundary_confidence,
               quality_status,quality_flags,rag_action)
            VALUES %s ON CONFLICT (dataset_id,source_section_id) DO NOTHING
            """,
            values,
            page_size=batch_size,
        )
        done += len(batch)
        progress("sections", done, EXPECTED["sections"], started)
    cur.execute(
        "SELECT source_section_id,section_pk,document_pk FROM fy2325_v2_sections WHERE dataset_id=%s",
        (DATASET,),
    )
    section_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    started = time.time()
    done = 0
    for batch in batched(rows(chunk_root / "chunks.jsonl"), batch_size):
        values = []
        for r in batch:
            section_pk, document_pk = section_map[r["section_id"]]
            if r["chunker_config_sha256"] != CHUNKER_CONFIG:
                raise RuntimeError(f"chunker hash mismatch: {r['chunk_id']}")
            values.append((
                DATASET, section_pk, document_pk, r["chunk_id"], r["section_id"],
                r["accession_number"], int(r["company_id"]), r["ticker"],
                int(r["coverage_year"]), int(r["chunk_index"]),
                r["canonical_section_code"], r["rag_section_code"],
                r["subsection_heading"], r["chunk_type"], r["chunk_text"],
                r["embedding_text"], int(r["token_count"]),
                int(r["embedding_token_count"]), int(r["embedding_max_tokens"]),
                int(r["source_start_char"]), int(r["source_end_char"]),
                int(r["section_start_char"]), int(r["section_end_char"]),
                r["chunk_text_sha256"], r["embedding_text_sha256"],
                r["chunker_version"], r["chunker_config_sha256"],
                r.get("policy_postprocess_version"), r.get("policy_postprocess_sha256"),
                r["boundary_start_type"], r["boundary_end_type"],
                r.get("semantic_topic_count"), bool(r["continuation_from_previous"]),
                bool(r["continues_to_next"]), r["quality_status"],
                Json(r["quality_flags"]), r["rag_action"], r["embedding_model"],
                r["embedding_model_revision"],
            ))
        execute_values(
            cur,
            """
            INSERT INTO fy2325_v2_chunks
              (dataset_id,section_pk,document_pk,source_chunk_id,source_section_id,
               accession_number,company_id,ticker,coverage_year,chunk_index,
               canonical_section_code,rag_section_code,subsection_heading,chunk_type,
               chunk_text,embedding_text,token_count,embedding_token_count,
               embedding_max_tokens,source_start_char,source_end_char,section_start_char,
               section_end_char,chunk_text_sha256,embedding_text_sha256,chunker_version,
               chunker_config_sha256,policy_postprocess_version,
               policy_postprocess_sha256,boundary_start_type,boundary_end_type,
               semantic_topic_count,continuation_from_previous,continues_to_next,
               quality_status,quality_flags,rag_action,embedding_model,
               embedding_model_revision)
            VALUES %s ON CONFLICT (dataset_id,source_chunk_id) DO NOTHING
            """,
            values,
            page_size=batch_size,
        )
        done += len(batch)
        progress("chunks", done, EXPECTED["chunks"], started)
    if done != EXPECTED["chunks"]:
        raise RuntimeError(f"chunk count {done} != 224561")
    conn.commit()


def vector_literal(vector: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.9g}" for x in vector) + "]"


def load_embeddings(conn, root: Path, batch_size: int):
    stage = root / "data_v2/05_embeddings/bge_base_v216_staging"
    cur = conn.cursor()
    cur.execute(
        """SELECT source_chunk_id,chunk_pk,embedding_text_sha256,chunk_text_sha256,rag_action
           FROM fy2325_v2_chunks WHERE dataset_id=%s""",
        (DATASET,),
    )
    chunk_map = {r[0]: r[1:] for r in cur.fetchall()}
    done = 0
    started = time.time()
    shard_paths = sorted(stage.glob("embeddings_*.npz"))
    if len(shard_paths) != 32:
        raise RuntimeError(f"shard count {len(shard_paths)} != 32")
    for shard_no, path in enumerate(shard_paths, 1):
        sidecar = path.with_suffix(path.suffix + ".sha256")
        expected_hash = sidecar.read_text(encoding="utf-8").split()[0].lower()
        if sha256(path) != expected_hash:
            raise RuntimeError(f"shard checksum mismatch: {path.name}")
        with np.load(path, allow_pickle=False) as data:
            ids = data["chunk_ids"]
            vectors = data["embeddings"]
            embedding_hashes = data["embedding_text_sha256"]
            chunk_hashes = data["chunk_text_sha256"]
            if vectors.dtype != np.float32 or vectors.shape != (len(ids), 768):
                raise RuntimeError(f"invalid vector array: {path.name}")
            for start in range(0, len(ids), batch_size):
                values = []
                stop = min(start + batch_size, len(ids))
                for i in range(start, stop):
                    source_id = str(ids[i])
                    if source_id not in chunk_map:
                        raise RuntimeError(f"unexpected embedded chunk: {source_id}")
                    chunk_pk, db_ehash, db_chash, action = chunk_map[source_id]
                    if action != "include":
                        raise RuntimeError(f"excluded chunk embedded: {source_id}")
                    if str(embedding_hashes[i]) != db_ehash or str(chunk_hashes[i]) != db_chash:
                        raise RuntimeError(f"text hash mismatch: {source_id}")
                    vector = vectors[i]
                    if not np.isfinite(vector).all():
                        raise RuntimeError(f"non-finite vector: {source_id}")
                    if not np.isclose(np.linalg.norm(vector), 1.0, atol=2e-6):
                        raise RuntimeError(f"non-unit vector: {source_id}")
                    values.append((
                        DATASET, chunk_pk, source_id, vector_literal(vector),
                        str(embedding_hashes[i]), str(chunk_hashes[i]), MODEL,
                        REVISION, 768, True,
                    ))
                execute_values(
                    cur,
                    """
                    INSERT INTO fy2325_v2_embeddings
                      (dataset_id,chunk_pk,source_chunk_id,embedding,
                       embedding_text_sha256,chunk_text_sha256,model_name,
                       model_revision,dimension,normalized)
                    VALUES %s ON CONFLICT (dataset_id,source_chunk_id) DO NOTHING
                    """,
                    values,
                    template="(%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s)",
                    page_size=batch_size,
                )
                done += len(values)
                progress(f"embeddings shard={shard_no}/32", done, EXPECTED["embeddings"], started)
        conn.commit()
    if done != EXPECTED["embeddings"]:
        raise RuntimeError(f"embedding count {done} != 158570")


def validate(conn):
    cur = conn.cursor()
    checks = {}
    for name, table in (
        ("companies", "fy2325_v2_companies"), ("filings", "fy2325_v2_filings"),
        ("documents", "fy2325_v2_documents"), ("sections", "fy2325_v2_sections"),
        ("chunks", "fy2325_v2_chunks"), ("embeddings", "fy2325_v2_embeddings"),
    ):
        cur.execute(f"SELECT count(*) FROM {table} WHERE dataset_id=%s", (DATASET,))
        checks[name] = cur.fetchone()[0]
    cur.execute(
        """SELECT coverage_year,count(*) FROM fy2325_v2_filings
           WHERE dataset_id=%s GROUP BY coverage_year ORDER BY coverage_year""",
        (DATASET,),
    )
    checks["coverage"] = dict(cur.fetchall())
    cur.execute(
        """SELECT rag_action,count(*) FROM fy2325_v2_chunks
           WHERE dataset_id=%s GROUP BY rag_action ORDER BY rag_action""",
        (DATASET,),
    )
    checks["rag_action"] = dict(cur.fetchall())
    cur.execute(
        """SELECT count(*) FROM fy2325_v2_chunks c LEFT JOIN fy2325_v2_embeddings e
           ON e.dataset_id=c.dataset_id AND e.chunk_pk=c.chunk_pk
           WHERE c.dataset_id=%s AND c.rag_action='include' AND e.chunk_pk IS NULL""",
        (DATASET,),
    )
    checks["missing_embeddings"] = cur.fetchone()[0]
    cur.execute(
        """SELECT count(*) FROM fy2325_v2_embeddings e JOIN fy2325_v2_chunks c
           ON c.dataset_id=e.dataset_id AND c.chunk_pk=e.chunk_pk
           WHERE e.dataset_id=%s AND c.rag_action <> 'include'""",
        (DATASET,),
    )
    checks["excluded_embeddings"] = cur.fetchone()[0]
    expected = {
        "companies": 190, "filings": 561, "documents": 561, "sections": 13455,
        "chunks": 224561, "embeddings": 158570,
        "coverage": {2023: 186, 2024: 186, 2025: 189},
        "rag_action": {"exclude": 65991, "include": 158570},
        "missing_embeddings": 0, "excluded_embeddings": 0,
    }
    failures = {key: {"actual": checks.get(key), "expected": value}
                for key, value in expected.items() if checks.get(key) != value}
    print(json.dumps({"dataset": DATASET, "checks": checks, "failures": failures,
                      "status": "PASS" if not failures else "FAIL"}, indent=2, default=str))
    if failures:
        raise RuntimeError("staging validation failed")
    cur.execute(
        "UPDATE fy2325_v2_datasets SET status='validated',validated_at=now() WHERE dataset_id=%s",
        (DATASET,),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("relational", "embeddings", "validate", "all"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    conn = connect()
    try:
        if args.stage in ("relational", "all"):
            load_relational(conn, args.repo_root.resolve(), args.batch_size)
        if args.stage in ("embeddings", "all"):
            load_embeddings(conn, args.repo_root.resolve(), args.batch_size)
        if args.stage in ("validate", "all"):
            validate(conn)
    except Exception:
        conn.rollback()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE fy2325_v2_datasets SET status='failed' WHERE dataset_id=%s", (DATASET,))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
