#!/usr/bin/env python3
"""Run configurable candidate-depth and lexical/hybrid retrieval strategies.

The runner evaluates all 24 supported frozen questions. Approved routing fields
are used before retrieval; gold answer/chunk/passage fields are never consulted.
Each question is embedded once, each route is fetched once at the maximum depths
required by the configuration, and each unique question/chunk pair is scored by
the pinned cross-encoder once before deterministic strategy-specific selection.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_production_retrieval_benchmark import build_sources  # noqa: E402
from src.query_api import (  # noqa: E402
    BI_ENCODER_REPO,
    BI_ENCODER_REVISION,
    CROSS_ENCODER_BATCH_SIZE,
    CROSS_ENCODER_REPO,
    CROSS_ENCODER_REVISION,
    EMBEDDING_TABLE,
    LIVE_VIEW,
    QUERY_PREFIX,
    ProductionRetriever,
    SourceSpec,
    _round_robin,
    _vector_literal,
)

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
DEFAULT_CONFIG = Path("config/retrieval_candidate_strategies_v1.json")
SUPPORTED_QUESTIONS = 24
REFUSAL_QUESTIONS = 5
LEXICAL_CONFIG = "english"
POLICIES = {"semantic", "union", "rrf_pool"}

ROUTING_FIELDS = (
    "question_id",
    "question_group",
    "question",
    "expected_tickers",
    "expected_years",
    "required_doc_type",
    "required_sections",
    "supporting_accession_numbers",
)
PROHIBITED_FIELDS = (
    "expected_answer",
    "supporting_chunk_ids",
    "supporting_passages",
    "supporting_chunk_indexes",
    "supporting_source_files",
    "supporting_file_sha256",
    "supporting_token_counts",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


@dataclass(frozen=True)
class Strategy:
    method_id: str
    candidate_policy: str
    semantic_depth: int
    lexical_depth: int
    pool_limit: int


@dataclass(frozen=True)
class ExperimentConfig:
    schema_version: int
    experiment_id: str
    in_sample: bool
    final_evidence_count: int
    rrf_k: int
    strategies: tuple[Strategy, ...]


def load_config(path: Path) -> ExperimentConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("configuration schema_version must be 1")
    experiment_id = str(raw.get("experiment_id", "")).strip()
    if not experiment_id:
        raise ValueError("experiment_id must be non-empty")
    if raw.get("in_sample") is not True:
        raise ValueError("frozen-24 candidate experiments must declare in_sample=true")
    final_count = int(raw.get("final_evidence_count", 0))
    rrf_k = int(raw.get("rrf_k", 0))
    if final_count <= 0 or rrf_k <= 0:
        raise ValueError("final_evidence_count and rrf_k must be positive")

    strategies = []
    seen = set()
    for item in raw.get("strategies", []):
        strategy = Strategy(
            method_id=str(item.get("method_id", "")).strip(),
            candidate_policy=str(item.get("candidate_policy", "")).strip(),
            semantic_depth=int(item.get("semantic_depth", 0)),
            lexical_depth=int(item.get("lexical_depth", 0)),
            pool_limit=int(item.get("pool_limit", 0)),
        )
        if not strategy.method_id or strategy.method_id in seen:
            raise ValueError("strategy method_id values must be non-empty and unique")
        seen.add(strategy.method_id)
        if strategy.candidate_policy not in POLICIES:
            raise ValueError(f"unsupported candidate_policy: {strategy.candidate_policy}")
        if strategy.semantic_depth <= 0 or strategy.pool_limit < final_count:
            raise ValueError("semantic_depth must be positive and pool_limit >= final count")
        if strategy.lexical_depth < 0:
            raise ValueError("lexical_depth cannot be negative")
        if strategy.candidate_policy == "semantic" and strategy.lexical_depth != 0:
            raise ValueError("semantic policy requires lexical_depth=0")
        if strategy.candidate_policy != "semantic" and strategy.lexical_depth <= 0:
            raise ValueError("lexical policies require lexical_depth > 0")
        if strategy.pool_limit > strategy.semantic_depth + strategy.lexical_depth:
            raise ValueError("pool_limit exceeds maximum candidate union size")
        strategies.append(strategy)
    if not strategies:
        raise ValueError("at least one strategy is required")
    return ExperimentConfig(
        schema_version=1,
        experiment_id=experiment_id,
        in_sample=True,
        final_evidence_count=final_count,
        rrf_k=rrf_k,
        strategies=tuple(strategies),
    )


def _approved_view(row: dict[str, str]) -> dict[str, str]:
    return {field: row[field] for field in ROUTING_FIELDS}


def load_questions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    supported = [row for row in rows if row["question_group"] != "refusal"]
    refusals = [row for row in rows if row["question_group"] == "refusal"]
    if len(supported) != SUPPORTED_QUESTIONS or len(refusals) != REFUSAL_QUESTIONS:
        raise RuntimeError(
            f"frozen shape mismatch: supported={len(supported)}, refusals={len(refusals)}"
        )
    return [_approved_view(row) for row in supported]


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    by_chunk: dict[int, dict[str, Any]] = {}
    for row in rows:
        chunk_id = int(row["chunk_id"])
        if chunk_id not in by_chunk:
            copied = dict(row)
            by_chunk[chunk_id] = copied
            output.append(copied)
        else:
            for key, value in row.items():
                if key not in by_chunk[chunk_id] or by_chunk[chunk_id][key] is None:
                    by_chunk[chunk_id][key] = value
    return output


def select_pool(
    strategy: Strategy,
    semantic: list[dict[str, Any]],
    lexical: list[dict[str, Any]],
    rrf_k: int,
) -> list[dict[str, Any]]:
    semantic_rows = semantic[: strategy.semantic_depth]
    if strategy.candidate_policy == "semantic":
        return semantic_rows[: strategy.pool_limit]
    lexical_rows = lexical[: strategy.lexical_depth]
    union = _deduplicate(semantic_rows + lexical_rows)
    if strategy.candidate_policy == "union":
        return union[: strategy.pool_limit]

    semantic_rank = {int(row["chunk_id"]): rank for rank, row in enumerate(semantic_rows, 1)}
    lexical_rank = {int(row["chunk_id"]): rank for rank, row in enumerate(lexical_rows, 1)}
    ranked = []
    for row in union:
        chunk_id = int(row["chunk_id"])
        score = 0.0
        if chunk_id in semantic_rank:
            score += 1.0 / (rrf_k + semantic_rank[chunk_id])
        if chunk_id in lexical_rank:
            score += 1.0 / (rrf_k + lexical_rank[chunk_id])
        copied = dict(row)
        copied["candidate_rrf_score"] = score
        copied["candidate_rrf_semantic_rank"] = semantic_rank.get(chunk_id)
        copied["candidate_rrf_lexical_rank"] = lexical_rank.get(chunk_id)
        ranked.append(copied)
    ranked.sort(key=lambda row: (-row["candidate_rrf_score"], int(row["chunk_id"])))
    return ranked[: strategy.pool_limit]


class CandidateEngine:
    def __init__(self, retriever: ProductionRetriever):
        self.retriever = retriever

    def encode(self, question: str) -> str:
        vector = self.retriever.bi_encoder.encode(
            [QUERY_PREFIX + question],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        return _vector_literal(vector)

    def fetch_semantic(
        self, source: SourceSpec, vector: str, limit: int
    ) -> list[dict[str, Any]]:
        sql = f"""
          SELECT r.chunk_id, r.chunk_index, r.embedding_text,
                 1-(e.embedding <=> %s::vector) AS semantic_score
          FROM public.{EMBEDDING_TABLE} e
          JOIN public.{LIVE_VIEW} r USING(chunk_id)
          WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
            AND r.accession_number=%s AND r.section_code=%s
          ORDER BY e.embedding <=> %s::vector, r.chunk_id
          LIMIT %s
        """
        params = (
            vector,
            source.ticker,
            source.filing_year,
            source.doc_type,
            source.accession_number,
            source.section_code,
            vector,
            limit,
        )
        return self._fetch(sql, params, "semantic_score", "semantic_rank")

    def fetch_lexical(
        self, question: str, source: SourceSpec, limit: int
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        sql = f"""
          WITH q AS (
            SELECT websearch_to_tsquery(%s::regconfig, %s) AS query
          )
          SELECT r.chunk_id, r.chunk_index, r.embedding_text,
                 ts_rank_cd(
                   to_tsvector(%s::regconfig, coalesce(r.embedding_text, '')),
                   q.query
                 ) AS lexical_score
          FROM public.{LIVE_VIEW} r
          CROSS JOIN q
          WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
            AND r.accession_number=%s AND r.section_code=%s
            AND q.query @@ to_tsvector(
              %s::regconfig, coalesce(r.embedding_text, '')
            )
          ORDER BY lexical_score DESC, r.chunk_id
          LIMIT %s
        """
        params = (
            LEXICAL_CONFIG,
            question,
            LEXICAL_CONFIG,
            source.ticker,
            source.filing_year,
            source.doc_type,
            source.accession_number,
            source.section_code,
            LEXICAL_CONFIG,
            limit,
        )
        return self._fetch(sql, params, "lexical_score", "lexical_rank")

    def _fetch(
        self, sql: str, params: tuple[Any, ...], score_field: str, rank_field: str
    ) -> list[dict[str, Any]]:
        with self.retriever.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [column.name for column in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        for rank, row in enumerate(rows, 1):
            row[score_field] = float(row[score_field])
            row[rank_field] = rank
        return rows

    def score_unique(
        self, question: str, pools: list[list[dict[str, Any]]]
    ) -> dict[int, float]:
        unique = _deduplicate([row for pool in pools for row in pool])
        pairs = [(question, row["embedding_text"] or "") for row in unique]
        scores = self.retriever.cross_encoder.predict(
            pairs,
            batch_size=CROSS_ENCODER_BATCH_SIZE,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return {
            int(row["chunk_id"]): float(score)
            for row, score in zip(unique, scores, strict=True)
        }


def run_benchmark(
    questions_path: Path, config: ExperimentConfig, engine: CandidateEngine
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    questions = load_questions(questions_path)
    max_semantic = max(item.semantic_depth for item in config.strategies)
    max_lexical = max(item.lexical_depth for item in config.strategies)
    csv_rows = {item.method_id: [] for item in config.strategies}
    details: dict[str, Any] = {
        "experiment_id": config.experiment_id,
        "in_sample": True,
        "gold_fields_used": False,
        "prohibited_fields": list(PROHIBITED_FIELDS),
        "strategies": [asdict(item) for item in config.strategies],
        "questions": [],
    }

    for question_number, row in enumerate(questions, 1):
        question = row["question"]
        sources = build_sources(row)
        vector = engine.encode(question)
        route_data = []
        for route_number, source in enumerate(sources, 1):
            semantic = engine.fetch_semantic(source, vector, max_semantic)
            lexical = engine.fetch_lexical(question, source, max_lexical)
            if not semantic:
                raise RuntimeError(f"{row['question_id']}: route {route_number} is empty")
            pools = {
                strategy.method_id: select_pool(
                    strategy, semantic, lexical, config.rrf_k
                )
                for strategy in config.strategies
            }
            scores = engine.score_unique(question, list(pools.values()))
            ranked_by_method = {}
            for strategy in config.strategies:
                ranked = [dict(item) for item in pools[strategy.method_id]]
                for item in ranked:
                    item["cross_encoder_score"] = scores[int(item["chunk_id"])]
                    item["source"] = asdict(source)
                ranked.sort(
                    key=lambda item: (-item["cross_encoder_score"], int(item["chunk_id"]))
                )
                for rank, item in enumerate(ranked, 1):
                    item["cross_encoder_rank_within_source"] = rank
                ranked_by_method[strategy.method_id] = ranked
            route_data.append(
                {
                    "route_number": route_number,
                    "source": asdict(source),
                    "semantic_fetched": len(semantic),
                    "lexical_fetched": len(lexical),
                    "ranked_by_method": ranked_by_method,
                }
            )

        question_detail = {
            "question_id": row["question_id"],
            "question_group": row["question_group"],
            "route_count": len(route_data),
            "methods": {},
        }
        for strategy in config.strategies:
            per_source = [route["ranked_by_method"][strategy.method_id] for route in route_data]
            evidence = _round_robin(per_source, config.final_evidence_count)
            if len(evidence) != config.final_evidence_count:
                raise RuntimeError(
                    f"{row['question_id']} {strategy.method_id}: expected "
                    f"{config.final_evidence_count} evidence rows, found {len(evidence)}"
                )
            compact = []
            for final_rank, item in enumerate(evidence, 1):
                csv_rows[strategy.method_id].append(
                    {
                        "model_id": strategy.method_id,
                        "question_id": row["question_id"],
                        "rank": final_rank,
                        "chunk_id": int(item["chunk_id"]),
                        "score": item["cross_encoder_score"],
                    }
                )
                compact.append(
                    {
                        "final_rank": final_rank,
                        "chunk_id": int(item["chunk_id"]),
                        "chunk_index": int(item["chunk_index"]),
                        "semantic_rank": item.get("semantic_rank"),
                        "lexical_rank": item.get("lexical_rank"),
                        "cross_encoder_score": item["cross_encoder_score"],
                        "cross_encoder_rank_within_source": item[
                            "cross_encoder_rank_within_source"
                        ],
                        "source": item["source"],
                    }
                )
            question_detail["methods"][strategy.method_id] = compact
        details["questions"].append(question_detail)
        print(
            f"[{question_number}/{len(questions)}] {row['question_id']} "
            f"routes={len(route_data)}",
            file=sys.stderr,
            flush=True,
        )
    expected = SUPPORTED_QUESTIONS * config.final_evidence_count
    for method_id, rows in csv_rows.items():
        if len(rows) != expected:
            raise RuntimeError(f"{method_id}: expected {expected} CSV rows, found {len(rows)}")
    return csv_rows, details


def _write_outputs(
    output_dir: Path,
    config: ExperimentConfig,
    rows_by_method: dict[str, list[dict[str, Any]]],
    details: dict[str, Any],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for method_id, rows in rows_by_method.items():
        path = output_dir / f"{method_id}.csv"
        with path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("model_id", "question_id", "rank", "chunk_id", "score"),
            )
            writer.writeheader()
            writer.writerows(rows)
        hashes[str(path)] = _sha256(path)
    details_path = output_dir / "strategy_details.json"
    with details_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(details, indent=2, sort_keys=True) + "\n")
    hashes[str(details_path)] = _sha256(details_path)
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing existing output directory: {args.output_dir}")

    config = load_config(args.config)
    started_at = _utcnow()
    started = time.monotonic()
    with ProductionRetriever() as retriever:
        retriever._load_models()
        rows_by_method, details = run_benchmark(
            args.questions, config, CandidateEngine(retriever)
        )
    output_hashes = _write_outputs(args.output_dir, config, rows_by_method, details)
    manifest = {
        "started_at": started_at,
        "finished_at": _utcnow(),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "git_head": _git_head(),
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "config_path": str(args.config),
        "config_sha256": _sha256(args.config),
        "script_sha256": _sha256(Path(__file__)),
        "experiment_id": config.experiment_id,
        "in_sample": True,
        "database_writes": False,
        "question_count": SUPPORTED_QUESTIONS,
        "refusals_excluded": REFUSAL_QUESTIONS,
        "method_count": len(config.strategies),
        "cross_encoder": CROSS_ENCODER_REPO,
        "cross_encoder_revision": CROSS_ENCODER_REVISION,
        "bi_encoder": BI_ENCODER_REPO,
        "bi_encoder_revision": BI_ENCODER_REVISION,
        "output_hashes": output_hashes,
    }
    manifest_path = args.output_dir / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
