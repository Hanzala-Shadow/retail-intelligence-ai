#!/usr/bin/env python3
"""Frozen-corpus embedding and retrieval benchmark for the 2026-07-16 run.

Fail-closed design: pinned model revisions, one table per model, snapshot-only
inputs, immutable vector rows, completeness checks before HNSW, deterministic
retrieval, paired metadata filters, raw result preservation, and two-pass
reproducibility comparison.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
GROUPS = ("Item_1", "Item_1A", "Item_7", "Item_8", "cross_company", "time_change")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_ident(value: str) -> str:
    if not IDENT.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def connect(explicit: str | None = None):
    import psycopg2
    dsn = explicit or os.environ.get("DB_URL")
    if not dsn and Path(".env").is_file():
        from dotenv import dotenv_values
        dsn = dotenv_values(".env").get("DB_URL")
    if dsn and dsn.startswith("postgresql+psycopg2://"):
        dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
    return psycopg2.connect(dsn) if dsn else psycopg2.connect()


def validate_config(config: dict[str, Any]) -> None:
    snapshot = config["snapshot"]
    if snapshot["row_count"] != 89335:
        raise ValueError("Configured snapshot row count is not 89335")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot["manifest_sha256"]):
        raise ValueError("Invalid snapshot manifest SHA-256")
    safe_ident(snapshot["table"].split(".")[-1])
    if len(config["models"]) != 4:
        raise ValueError("Exactly four models are required")
    for slug, model in config["models"].items():
        safe_ident(slug)
        safe_ident(model["table"])
        if not re.fullmatch(r"[0-9a-f]{40}", model["revision"]):
            raise ValueError(f"{slug}: invalid pinned revision")
        if model["dimension"] <= 0 or model["batch_size"] <= 0:
            raise ValueError(f"{slug}: invalid dimension or batch size")
    if not config["embedding"]["normalize_embeddings"]:
        raise ValueError("Vector normalization must be enabled")


def vector_literal(vector: np.ndarray) -> str:
    values = np.asarray(vector, dtype=np.float32)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("Vector must be one-dimensional and finite")
    return "[" + ",".join(format(float(x), ".9g") for x in values) + "]"


def load_model(model: dict[str, Any]):
    import torch
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(2)
    loaded = SentenceTransformer(
        model["repo_id"],
        revision=model["revision"],
        trust_remote_code=bool(model["trust_remote_code"]),
        device="cpu",
    )
    loaded.max_seq_length = int(model["max_seq_length"])
    dimension = loaded.get_sentence_embedding_dimension()
    if dimension != int(model["dimension"]):
        raise RuntimeError(f"Dimension mismatch: loaded={dimension}, configured={model['dimension']}")
    return loaded


def create_schema(conn, config: dict[str, Any], slug: str) -> None:
    model = config["models"][slug]
    table = safe_ident(model["table"])
    dim = int(model["dimension"])
    sql = f"""
    CREATE TABLE IF NOT EXISTS public.{table} (
      chunk_id BIGINT PRIMARY KEY,
      embedding VECTOR({dim}) NOT NULL,
      model_repo_id TEXT NOT NULL,
      resolved_revision TEXT NOT NULL,
      dimension INTEGER NOT NULL,
      document_prefix TEXT NOT NULL,
      normalized BOOLEAN NOT NULL,
      corpus_snapshot TEXT NOT NULL,
      corpus_manifest_sha256 CHAR(64) NOT NULL,
      embedding_text_sha256 CHAR(64) NOT NULL,
      embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CHECK (dimension = {dim}),
      CHECK (normalized),
      CHECK (resolved_revision = '{model['revision']}')
    )
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            f"SELECT count(*) FROM public.{table} WHERE model_repo_id<>%s OR resolved_revision<>%s "
            "OR dimension<>%s OR document_prefix<>%s OR normalized IS DISTINCT FROM TRUE "
            "OR corpus_snapshot<>%s OR corpus_manifest_sha256<>%s",
            (model["repo_id"], model["revision"], dim, model["document_prefix"],
             config["snapshot"]["table"], config["snapshot"]["manifest_sha256"]),
        )
        bad = cur.fetchone()[0]
        if bad:
            raise RuntimeError(f"{table} contains {bad} incompatible rows")
    conn.commit()
    print(f"PASS: schema ready without vector index: public.{table}")


def fetch_pending(conn, config: dict[str, Any], slug: str, limit: int | None):
    model = config["models"][slug]
    table = safe_ident(model["table"])
    snapshot = safe_ident(config["snapshot"]["table"].split(".")[-1])
    sql = f"""
      SELECT s.chunk_id, s.embedding_text,
             encode(sha256(convert_to(s.embedding_text,'UTF8')),'hex')
      FROM public.{snapshot} s
      LEFT JOIN public.{table} e USING (chunk_id)
      WHERE e.chunk_id IS NULL
      ORDER BY s.chunk_id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def insert_batch(conn, config: dict[str, Any], slug: str, rows, vectors) -> None:
    from psycopg2.extras import execute_values
    model = config["models"][slug]
    table = safe_ident(model["table"])
    payload = []
    for (chunk_id, _text, text_hash), vector in zip(rows, vectors, strict=True):
        arr = np.asarray(vector, dtype=np.float32)
        if arr.shape != (int(model["dimension"]),) or not np.isfinite(arr).all():
            raise RuntimeError(f"Invalid vector for chunk {chunk_id}")
        norm = float(np.linalg.norm(arr))
        if abs(norm - 1.0) > 1e-4:
            raise RuntimeError(f"Non-normalized vector for chunk {chunk_id}: norm={norm}")
        payload.append((chunk_id, vector_literal(arr), model["repo_id"], model["revision"],
                        model["dimension"], model["document_prefix"], True,
                        config["snapshot"]["table"], config["snapshot"]["manifest_sha256"], text_hash))
    sql = f"""
      INSERT INTO public.{table}
      (chunk_id,embedding,model_repo_id,resolved_revision,dimension,document_prefix,
       normalized,corpus_snapshot,corpus_manifest_sha256,embedding_text_sha256)
      VALUES %s ON CONFLICT (chunk_id) DO NOTHING
    """
    template = "(%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s)"
    with conn.cursor() as cur:
        execute_values(cur, sql, payload, template=template)
        if cur.rowcount != len(payload):
            raise RuntimeError("Immutable insert conflict detected; refusing silent overwrite")
    conn.commit()


def embed(conn, config: dict[str, Any], slug: str, limit: int | None) -> None:
    create_schema(conn, config, slug)
    model_cfg = config["models"][slug]
    pending = fetch_pending(conn, config, slug, limit)
    if not pending:
        print("PASS: no pending rows; resume is a no-op")
        return
    model = load_model(model_cfg)
    batch_size = int(model_cfg["batch_size"])
    started = time.monotonic()
    done = 0
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        texts = [model_cfg["document_prefix"] + row[1] for row in batch]
        vectors = model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                               convert_to_numpy=True, show_progress_bar=False)
        insert_batch(conn, config, slug, batch, vectors)
        done += len(batch)
        if done == len(batch) or done % 200 == 0 or done == len(pending):
            print(f"[{slug}] embedded {done}/{len(pending)}")
    print(json.dumps({"model": slug, "new_rows": done, "seconds": time.monotonic()-started,
                      "finished_at": utcnow()}))


def verification(conn, config: dict[str, Any], slug: str, expected: int | None) -> dict[str, Any]:
    model = config["models"][slug]
    table = safe_ident(model["table"])
    snapshot = safe_ident(config["snapshot"]["table"].split(".")[-1])
    with conn.cursor() as cur:
        cur.execute(f"""
          SELECT count(*),count(DISTINCT chunk_id),
            count(*) FILTER (WHERE vector_dims(embedding)<>%s),
            count(*) FILTER (WHERE model_repo_id<>%s OR resolved_revision<>%s OR dimension<>%s
              OR document_prefix<>%s OR normalized IS DISTINCT FROM TRUE OR corpus_snapshot<>%s
              OR corpus_manifest_sha256<>%s),
            count(*) FILTER (WHERE abs(vector_norm(embedding)-1.0)>0.0001)
          FROM public.{table}
        """, (model["dimension"], model["repo_id"], model["revision"], model["dimension"],
               model["document_prefix"], config["snapshot"]["table"], config["snapshot"]["manifest_sha256"]))
        rows, unique, wrong_dim, wrong_meta, wrong_norm = cur.fetchone()
        cur.execute(f"""
          SELECT count(*) FROM public.{table} e JOIN public.{snapshot} s USING(chunk_id)
          WHERE e.embedding_text_sha256<>encode(sha256(convert_to(s.embedding_text,'UTF8')),'hex')
        """)
        wrong_hash = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM public.{table} e LEFT JOIN public.{snapshot} s USING(chunk_id) WHERE s.chunk_id IS NULL")
        orphaned = cur.fetchone()[0]
    target = expected if expected is not None else config["snapshot"]["row_count"]
    report = {"model": slug, "rows": rows, "unique_chunk_ids": unique, "expected_rows": target,
              "wrong_dimensions": wrong_dim, "wrong_metadata": wrong_meta,
              "wrong_norms": wrong_norm, "wrong_text_hashes": wrong_hash, "orphaned": orphaned}
    report["passed"] = rows == unique == target and not any((wrong_dim, wrong_meta, wrong_norm, wrong_hash, orphaned))
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise RuntimeError("Embedding verification failed")
    return report


def build_index(conn, config: dict[str, Any], slug: str) -> None:
    verification(conn, config, slug, None)
    model = config["models"][slug]
    table = safe_ident(model["table"])
    index = safe_ident(f"idx_{table}_hnsw")
    hnsw = config["database"]["hnsw"]
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=%s", (index,))
        existing = cur.fetchone()
        if existing:
            raise RuntimeError(f"Refusing to reuse existing index without an audited definition: {existing[0]}")
        started = time.monotonic()
        cur.execute(f"CREATE INDEX {index} ON public.{table} USING hnsw "
                    f"(embedding vector_cosine_ops) WITH (m={int(hnsw['m'])},ef_construction={int(hnsw['ef_construction'])})")
        cur.execute(f"ANALYZE public.{table}")
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=%s", (index,))
        created = cur.fetchone()
        if not created or "USING hnsw" not in created[0] or "vector_cosine_ops" not in created[0]:
            raise RuntimeError("Created index definition failed verification")
    conn.commit()
    print(json.dumps({"passed": True, "index": index, "indexdef": created[0],
                      "seconds": time.monotonic() - started, "finished_at": utcnow()}, indent=2))


@dataclass(frozen=True)
class Question:
    qid: str
    group: str
    text: str
    tickers: tuple[str, ...]
    years: tuple[int, ...]
    doc_types: tuple[str, ...]
    accessions: tuple[str, ...]
    gold: tuple[str, ...]


def split_field(row: dict[str, str], name: str) -> list[str]:
    value = (row.get(name) or "").strip()
    return [part.strip() for part in value.split("|")] if value else []


def load_questions(path: str | Path) -> tuple[list[Question], list[dict[str, str]]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    supported, refusals = [], []
    for row in rows:
        refusal = (row.get("refusal_expected") or "").lower() in {"true", "1", "yes"}
        if refusal:
            refusals.append(row)
            continue
        tickers = split_field(row, "expected_tickers")
        years = [int(x) for x in split_field(row, "expected_years")]
        doc_types = split_field(row, "required_doc_type")
        accessions = split_field(row, "supporting_accession_numbers")
        n = len(tickers)
        for values, label in ((years,"years"),(doc_types,"doc types"),(accessions,"accessions")):
            if len(values) == 1 and n > 1:
                values *= n
            if len(values) != n:
                raise ValueError(f"{row['question_id']}: positional {label} mismatch")
        supported.append(Question(row["question_id"], row["question_group"], row["question"],
                                  tuple(tickers), tuple(years), tuple(doc_types), tuple(accessions),
                                  tuple(split_field(row, "supporting_chunk_ids"))))
    if len(supported) != 24 or len(refusals) != 5:
        raise ValueError(f"Expected 24 supported and 5 refusal questions; got {len(supported)} and {len(refusals)}")
    return supported, refusals


def retrieve(conn, config: dict[str, Any], slug: str, model, q: Question, k: int) -> list[dict[str, Any]]:
    cfg = config["models"][slug]
    table = safe_ident(cfg["table"])
    snapshot = safe_ident(config["snapshot"]["table"].split(".")[-1])
    query = cfg["query_prefix"] + q.text
    vector = model.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
    clauses, params = [], []
    for ticker, year, doc_type, accession in zip(q.tickers, q.years, q.doc_types, q.accessions, strict=True):
        clauses.append("(s.ticker=%s AND s.filing_year=%s AND s.doc_type=%s AND s.accession_number=%s)")
        params.extend((ticker, year, doc_type, accession))
    sql = f"""
      SELECT s.chunk_id,s.ticker,s.filing_year,s.accession_number,s.doc_type,s.section_code,
             s.chunk_index,s.token_count,s.chunk_text,1-(e.embedding <=> %s::vector) score
      FROM public.{table} e JOIN public.{snapshot} s USING(chunk_id)
      WHERE ({' OR '.join(clauses)})
      ORDER BY e.embedding <=> %s::vector,s.chunk_id ASC LIMIT %s
    """
    v = vector_literal(vector)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(int(config["database"]["hnsw"]["ef_search"])),),
        )
        cur.execute(sql, (v, *params, v, k))
        names = [desc.name for desc in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def metrics(ranked: list[str], gold: tuple[str, ...], k: int = 5) -> dict[str, float]:
    gold_set = set(gold)
    hits = [i for i, cid in enumerate(ranked[:k], 1) if cid in gold_set]
    recall = len(set(ranked[:k]) & gold_set) / len(gold_set)
    mrr = 1 / hits[0] if hits else 0.0
    dcg = sum(1 / math.log2(i + 1) for i in hits)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(gold_set)) + 1))
    return {"recall_at_5": recall, "hit_rate_at_5": float(bool(hits)), "mrr": mrr, "ndcg_at_5": dcg/ideal}


def validate_results(q: Question, results: list[dict[str, Any]], k: int) -> None:
    if len(results) != k:
        raise RuntimeError(f"{q.qid}: expected {k} results, got {len(results)}")
    allowed = set(zip(q.tickers, q.years, q.doc_types, q.accessions, strict=True))
    seen: set[str] = set()
    for rank, result in enumerate(results, 1):
        identity = (result["ticker"], int(result["filing_year"]),
                    result["doc_type"], result["accession_number"])
        if identity not in allowed:
            raise RuntimeError(f"{q.qid}: metadata leakage at rank {rank}: {identity!r}")
        chunk_id = str(result["chunk_id"])
        if chunk_id in seen:
            raise RuntimeError(f"{q.qid}: duplicate chunk_id at rank {rank}: {chunk_id}")
        seen.add(chunk_id)


def evaluate(conn, config: dict[str, Any], slug: str, questions_path: str, output: str, pass_no: int) -> None:
    verification(conn, config, slug, None)
    questions, _refusals = load_questions(questions_path)
    model = load_model(config["models"][slug])
    out = Path(output)
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite immutable retrieval pass: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["run_id","retrieval_pass","model_slug","model_repo","resolved_revision","dimension",
              "query_prefix","question_id","question_group","question","rank","chunk_id","score","ticker",
              "filing_year","accession_number","doc_type","section_code","chunk_index","token_count",
              "retrieved_text","exact_gold"]
    metric_rows: list[dict[str, Any]] = []
    with out.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for q in questions:
            results = retrieve(conn, config, slug, model, q, 5)
            validate_results(q, results, 5)
            question_metrics = metrics([str(r["chunk_id"]) for r in results], q.gold, 5)
            metric_rows.append({"question_id": q.qid, "question_group": q.group, **question_metrics})
            for rank, result in enumerate(results, 1):
                writer.writerow({"run_id":config["run_id"],"retrieval_pass":pass_no,"model_slug":slug,
                    "model_repo":config["models"][slug]["repo_id"],"resolved_revision":config["models"][slug]["revision"],
                    "dimension":config["models"][slug]["dimension"],"query_prefix":config["models"][slug]["query_prefix"],
                    "question_id":q.qid,"question_group":q.group,"question":q.text,"rank":rank,
                    "chunk_id":result["chunk_id"],"score":result["score"],"ticker":result["ticker"],
                    "filing_year":result["filing_year"],"accession_number":result["accession_number"],
                    "doc_type":result["doc_type"],"section_code":result["section_code"],
                    "chunk_index":result["chunk_index"],"token_count":result["token_count"],
                    "retrieved_text":result["chunk_text"],"exact_gold":str(result["chunk_id"]) in q.gold})
    names = ("recall_at_5", "hit_rate_at_5", "mrr", "ndcg_at_5")
    def aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
        return {name: sum(float(row[name]) for row in rows) / len(rows) for name in names}
    summary = {"run_id": config["run_id"], "retrieval_pass": pass_no, "model_slug": slug,
               "supported_questions": len(metric_rows), "raw_rows": len(metric_rows) * 5,
               "overall": aggregate(metric_rows),
               "by_group": {group: aggregate([r for r in metric_rows if r["question_group"] == group])
                            for group in GROUPS}}
    metrics_out = out.with_suffix(out.suffix + ".metrics.json")
    with metrics_out.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"PASS: wrote immutable raw results: {out}")
    print(f"PASS: wrote overall and six-group metrics: {metrics_out}")


def reproduce(first: str, second: str, tolerance: float) -> None:
    def rows(path):
        with open(path, encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    a, b = rows(first), rows(second)
    if len(a) != len(b):
        raise RuntimeError("Retrieval pass row counts differ")
    ranking_diff = score_diff = 0
    max_delta = 0.0
    for left, right in zip(a, b, strict=True):
        key_fields = ("run_id","model_slug","model_repo","resolved_revision","dimension","query_prefix",
                      "question_id","question_group","question","rank","chunk_id","ticker","filing_year",
                      "accession_number","doc_type","section_code","chunk_index","token_count","retrieved_text",
                      "exact_gold")
        if tuple(left[x] for x in key_fields) != tuple(right[x] for x in key_fields):
            ranking_diff += 1
        delta = abs(float(left["score"])-float(right["score"]))
        max_delta = max(max_delta, delta)
        if delta > tolerance:
            score_diff += 1
    report = {"rows":len(a),"ranking_differences":ranking_diff,"score_differences":score_diff,
              "tolerance":tolerance,"maximum_score_delta":max_delta,"passed":ranking_diff==score_diff==0}
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise RuntimeError("Reproducibility check failed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--conn")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config")
    for name in ("schema","embed","verify-embeddings","build-index","evaluate"):
        x = sub.add_parser(name)
        x.add_argument("--model", required=True)
        if name == "embed": x.add_argument("--limit", type=int)
        if name == "verify-embeddings": x.add_argument("--expected", type=int)
        if name == "evaluate":
            x.add_argument("--questions", required=True); x.add_argument("--output", required=True); x.add_argument("--pass-number", type=int, required=True)
    x = sub.add_parser("reproduce")
    x.add_argument("--first", required=True); x.add_argument("--second", required=True); x.add_argument("--tolerance", type=float, default=1e-7)
    return p


def main() -> None:
    args = parser().parse_args()
    config = load_json(args.config)
    validate_config(config)
    if args.command == "validate-config":
        print("PASS: configuration validated")
        return
    if args.command == "reproduce":
        reproduce(args.first, args.second, args.tolerance)
        return
    if args.model not in config["models"]:
        raise SystemExit(f"Unknown model: {args.model}")
    conn = connect(args.conn)
    try:
        if args.command == "schema": create_schema(conn, config, args.model)
        elif args.command == "embed": embed(conn, config, args.model, args.limit)
        elif args.command == "verify-embeddings": verification(conn, config, args.model, args.expected)
        elif args.command == "build-index": build_index(conn, config, args.model)
        elif args.command == "evaluate": evaluate(conn, config, args.model, args.questions, args.output, args.pass_number)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
