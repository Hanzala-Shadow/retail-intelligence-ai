#!/usr/bin/env python3
"""Read-only candidate-funnel audit for the seven frozen production misses.

Retrieval candidate lists are produced from approved routes before gold fields are
read. Gold chunk IDs and passages are used only by the post-retrieval labelling
step. This is an in-sample diagnostic and does not modify production policy.
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
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_production_retrieval_benchmark import build_sources  # noqa: E402
from src.query_api import (  # noqa: E402
    BI_ENCODER_REPO,
    BI_ENCODER_REVISION,
    CANDIDATES_PER_SOURCE,
    CROSS_ENCODER_BATCH_SIZE,
    CROSS_ENCODER_REPO,
    CROSS_ENCODER_REVISION,
    EMBEDDING_TABLE,
    LIVE_VIEW,
    QUERY_PREFIX,
    ProductionRetriever,
    SourceSpec,
    _vector_literal,
)

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
MISS_IDS = (
    "10K-V2-I7-002",
    "10K-V2-I7-004",
    "10K-V2-I8-002",
    "10K-V2-I8-003",
    "10K-V2-XC-002",
    "10K-V2-TC-003",
    "10K-V2-TC-004",
)
EXPECTED_GOLD_CHUNKS = 10
LEXICAL_CONFIG = "english"
RRF_K = 60
SNAPSHOT_LIMIT = 100

GOLD_FIELDS = (
    "supporting_chunk_ids",
    "supporting_chunk_indexes",
    "supporting_passages",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parts(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split("|")]


def _load_miss_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = {row["question_id"]: row for row in rows if row["question_id"] in MISS_IDS}
    missing = sorted(set(MISS_IDS) - set(selected))
    extras = sorted(set(selected) - set(MISS_IDS))
    if missing or extras or len(selected) != len(MISS_IDS):
        raise RuntimeError(f"frozen miss-set mismatch: missing={missing}, extras={extras}")
    return [selected[question_id] for question_id in MISS_IDS]


def _retrieval_view(row: dict[str, str]) -> dict[str, str]:
    """Return only question/routing fields; gold is deliberately excluded."""
    permitted = (
        "question_id",
        "question_group",
        "question",
        "expected_tickers",
        "expected_years",
        "required_doc_type",
        "required_sections",
        "supporting_accession_numbers",
    )
    return {field: row[field] for field in permitted}


def _route_key(source: SourceSpec) -> tuple[str, int, str, str, str]:
    return (
        source.ticker,
        source.filing_year,
        source.doc_type,
        source.accession_number,
        source.section_code,
    )


def _rank_map(rows: list[dict[str, Any]]) -> dict[int, int]:
    return {int(row["chunk_id"]): rank for rank, row in enumerate(rows, 1)}


def _rrf_rows(
    semantic: list[dict[str, Any]], lexical: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    semantic_ranks = _rank_map(semantic)
    lexical_ranks = _rank_map(lexical)
    chunk_ids = set(semantic_ranks) | set(lexical_ranks)
    output = []
    for chunk_id in chunk_ids:
        semantic_rank = semantic_ranks.get(chunk_id)
        lexical_rank = lexical_ranks.get(chunk_id)
        score = 0.0
        if semantic_rank is not None:
            score += 1.0 / (RRF_K + semantic_rank)
        if lexical_rank is not None:
            score += 1.0 / (RRF_K + lexical_rank)
        output.append(
            {
                "chunk_id": chunk_id,
                "semantic_rank": semantic_rank,
                "lexical_rank": lexical_rank,
                "rrf_score": score,
            }
        )
    output.sort(key=lambda item: (-item["rrf_score"], item["chunk_id"]))
    for rank, item in enumerate(output, 1):
        item["rrf_rank"] = rank
    return output


class FunnelAuditor:
    def __init__(self, retriever: ProductionRetriever):
        self.retriever = retriever

    def _fetch_ranked(
        self, question: str, source: SourceSpec, query_vector: str, lexical: bool
    ) -> list[dict[str, Any]]:
        filters = (
            source.ticker,
            source.filing_year,
            source.doc_type,
            source.accession_number,
            source.section_code,
        )
        if lexical:
            sql = f"""
              WITH q AS (
                SELECT websearch_to_tsquery(%s::regconfig, %s) AS query
              )
              SELECT r.chunk_id, r.chunk_index, r.chunk_text, r.embedding_text,
                     ts_rank_cd(
                       to_tsvector(%s::regconfig, coalesce(r.embedding_text, '')),
                       q.query
                     ) AS lexical_score
              FROM public.{LIVE_VIEW} r
              CROSS JOIN q
              WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
                AND r.accession_number=%s AND r.section_code=%s
                AND q.query @@
                    to_tsvector(%s::regconfig, coalesce(r.embedding_text, ''))
              ORDER BY lexical_score DESC, r.chunk_id
            """
            params = (
                LEXICAL_CONFIG,
                question,
                LEXICAL_CONFIG,
                *filters,
                LEXICAL_CONFIG,
            )
        else:
            sql = f"""
              SELECT r.chunk_id, r.chunk_index, r.chunk_text, r.embedding_text,
                     1-(e.embedding <=> %s::vector) AS semantic_score
              FROM public.{EMBEDDING_TABLE} e
              JOIN public.{LIVE_VIEW} r USING(chunk_id)
              WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
                AND r.accession_number=%s AND r.section_code=%s
              ORDER BY e.embedding <=> %s::vector, r.chunk_id
            """
            params = (query_vector, *filters, query_vector)
        with self.retriever.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [column.name for column in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        score_field = "lexical_score" if lexical else "semantic_score"
        rank_field = "lexical_rank" if lexical else "semantic_rank"
        for rank, item in enumerate(rows, 1):
            item[score_field] = float(item[score_field])
            item[rank_field] = rank
        return rows

    def _rerank_top20(
        self, question: str, semantic: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = [dict(item) for item in semantic[:CANDIDATES_PER_SOURCE]]
        pairs = [(question, row["embedding_text"] or "") for row in rows]
        scores = self.retriever.cross_encoder.predict(
            pairs,
            batch_size=CROSS_ENCODER_BATCH_SIZE,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        for row, score in zip(rows, scores, strict=True):
            row["cross_encoder_score"] = float(score)
        rows.sort(key=lambda row: (-row["cross_encoder_score"], int(row["chunk_id"])))
        for rank, row in enumerate(rows, 1):
            row["cross_encoder_rank"] = rank
        return rows

    def retrieve_route(
        self, question_id: str, question: str, route_index: int, source: SourceSpec
    ) -> dict[str, Any]:
        vector = self.retriever.bi_encoder.encode(
            [QUERY_PREFIX + question],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        query_vector = _vector_literal(vector)
        semantic = self._fetch_ranked(question, source, query_vector, lexical=False)
        lexical = self._fetch_ranked(question, source, query_vector, lexical=True)
        if not semantic:
            raise RuntimeError(
                f"{question_id} route {route_index}: authorized route has no candidates"
            )
        reranked = self._rerank_top20(question, semantic)
        hybrid = _rrf_rows(semantic, lexical)
        return {
            "question_id": question_id,
            "route_index": route_index,
            "question": question,
            "source": asdict(source),
            "candidate_count": len(semantic),
            "semantic": semantic,
            "lexical": lexical,
            "reranked_top20": reranked,
            "hybrid": hybrid,
        }

    def fetch_neighbors(
        self, source: SourceSpec, chunk_index: int
    ) -> list[dict[str, Any]]:
        sql = f"""
          SELECT chunk_id, chunk_index, chunk_text, token_count
          FROM public.{LIVE_VIEW}
          WHERE ticker=%s AND filing_year=%s AND doc_type=%s
            AND accession_number=%s AND section_code=%s
            AND chunk_index IN (%s, %s, %s)
          ORDER BY chunk_index, chunk_id
        """
        params = (*_route_key(source), chunk_index - 1, chunk_index, chunk_index + 1)
        with self.retriever.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [column.name for column in cursor.description]
            return [dict(zip(names, values)) for values in cursor.fetchall()]


def _snapshot(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{field: row.get(field) for field in fields} for row in list(rows)[:SNAPSHOT_LIMIT]]


def _locate(rows: list[dict[str, Any]], chunk_id: int, rank_field: str) -> int | None:
    for row in rows:
        if int(row["chunk_id"]) == chunk_id:
            return int(row[rank_field])
    return None


def _normalize_evidence_text(value: str) -> str:
    """Normalize Unicode and whitespace without changing word content."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def _gold_labels(
    raw_row: dict[str, str], route_results: list[dict[str, Any]], auditor: FunnelAuditor
) -> list[dict[str, Any]]:
    gold_ids = [int(value) for value in _parts(raw_row["supporting_chunk_ids"])]
    gold_indexes = [int(value) for value in _parts(raw_row["supporting_chunk_indexes"])]
    passages = _parts(raw_row["supporting_passages"])
    if not (len(gold_ids) == len(gold_indexes) == len(passages) == len(route_results)):
        raise RuntimeError(f"{raw_row['question_id']}: positional gold-field mismatch")

    output = []
    for gold_id, gold_index, passage, route in zip(
        gold_ids, gold_indexes, passages, route_results, strict=True
    ):
        semantic_rank = _locate(route["semantic"], gold_id, "semantic_rank")
        lexical_rank = _locate(route["lexical"], gold_id, "lexical_rank")
        cross_encoder_rank = _locate(
            route["reranked_top20"], gold_id, "cross_encoder_rank"
        )
        hybrid_rank = _locate(route["hybrid"], gold_id, "rrf_rank")
        source = SourceSpec.from_mapping(route["source"])
        neighbors = auditor.fetch_neighbors(source, gold_index)
        gold_rows = [row for row in neighbors if int(row["chunk_id"]) == gold_id]
        if len(gold_rows) != 1:
            raise RuntimeError(
                f"{raw_row['question_id']}: gold chunk {gold_id} not uniquely present "
                "at its declared route/index"
            )
        gold_text = gold_rows[0]["chunk_text"] or ""
        passage_byte_exact = passage in gold_text
        passage_normalized = _normalize_evidence_text(passage) in _normalize_evidence_text(
            gold_text
        )
        if semantic_rank is None:
            primary = "gold_contract_issue"
        elif semantic_rank > CANDIDATES_PER_SOURCE:
            primary = "candidate_generation_failure"
        elif cross_encoder_rank is None:
            raise AssertionError("top-20 semantic gold must have a reranker rank")
        elif cross_encoder_rank > 5:
            primary = "reranking_failure"
        else:
            primary = "aggregation_or_multi_gold_coverage_failure"
        if not passage_normalized:
            primary = "gold_contract_issue"
        output.append(
            {
                "question_id": raw_row["question_id"],
                "route_index": route["route_index"],
                "source": route["source"],
                "gold_chunk_id": gold_id,
                "declared_gold_chunk_index": gold_index,
                "candidate_count": route["candidate_count"],
                "semantic_rank": semantic_rank,
                "semantic_top20": semantic_rank is not None
                and semantic_rank <= CANDIDATES_PER_SOURCE,
                "cross_encoder_rank_if_top20": cross_encoder_rank,
                "lexical_rank": lexical_rank,
                "hybrid_rrf_rank": hybrid_rank,
                "supporting_passage_byte_exact_contained": passage_byte_exact,
                "supporting_passage_normalized_contained": passage_normalized,
                "primary_machine_classification": primary,
                "decomposition_assessment": "REQUIRES_REVIEW",
                "boundary_assessment": "REQUIRES_REVIEW",
                "top5_neighbor_answer_overlap_assessment": "REQUIRES_REVIEW",
                "neighbors": neighbors,
            }
        )
    return output


def run_audit(
    questions_path: Path, auditor: FunnelAuditor
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_rows = _load_miss_rows(questions_path)
    retrieval_inputs = [_retrieval_view(row) for row in raw_rows]

    # Phase 1: all ranked lists are produced without gold fields.
    retrieval_results: list[dict[str, Any]] = []
    by_question: dict[str, list[dict[str, Any]]] = {}
    for row in retrieval_inputs:
        sources = build_sources(row)
        routes = []
        for route_index, source in enumerate(sources, 1):
            result = auditor.retrieve_route(
                row["question_id"], row["question"], route_index, source
            )
            routes.append(result)
            retrieval_results.append(result)
        by_question[row["question_id"]] = routes

    # Phase 2: gold is now read solely to label the already-produced lists.
    labels: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        labels.extend(_gold_labels(raw_row, by_question[raw_row["question_id"]], auditor))
    if len(labels) != EXPECTED_GOLD_CHUNKS:
        raise RuntimeError(f"expected {EXPECTED_GOLD_CHUNKS} gold labels, found {len(labels)}")

    compact_routes = []
    for route in retrieval_results:
        compact_routes.append(
            {
                "question_id": route["question_id"],
                "route_index": route["route_index"],
                "question": route["question"],
                "source": route["source"],
                "candidate_count": route["candidate_count"],
                "semantic_top100": _snapshot(
                    route["semantic"],
                    ("chunk_id", "chunk_index", "semantic_rank", "semantic_score"),
                ),
                "lexical_top100": _snapshot(
                    route["lexical"],
                    ("chunk_id", "chunk_index", "lexical_rank", "lexical_score"),
                ),
                "cross_encoder_top20": _snapshot(
                    route["reranked_top20"],
                    (
                        "chunk_id",
                        "chunk_index",
                        "semantic_rank",
                        "semantic_score",
                        "cross_encoder_rank",
                        "cross_encoder_score",
                        "chunk_text",
                    ),
                ),
                "hybrid_rrf_top100": _snapshot(
                    route["hybrid"],
                    ("chunk_id", "semantic_rank", "lexical_rank", "rrf_rank", "rrf_score"),
                ),
            }
        )
    report = {
        "schema_version": 2,
        "in_sample": True,
        "diagnostic_only": True,
        "database_writes": False,
        "gold_boundary": {
            "candidate_lists_produced_before_gold_read": True,
            "gold_fields_used_only_for_post_retrieval_labelling": list(GOLD_FIELDS),
        },
        "miss_question_ids": list(MISS_IDS),
        "question_count": len(raw_rows),
        "route_count": len(retrieval_results),
        "gold_chunk_count": len(labels),
        "policy": {
            "semantic_model": BI_ENCODER_REPO,
            "semantic_revision": BI_ENCODER_REVISION,
            "production_candidate_depth": CANDIDATES_PER_SOURCE,
            "cross_encoder": CROSS_ENCODER_REPO,
            "cross_encoder_revision": CROSS_ENCODER_REVISION,
            "lexical_method": "websearch_to_tsquery_plus_ts_rank_cd",
            "lexical_config": LEXICAL_CONFIG,
            "hybrid_method": "equal_semantic_lexical_rrf",
            "rrf_k": RRF_K,
            "deterministic_tie_break": "chunk_id_ascending",
            "snapshot_depth": SNAPSHOT_LIMIT,
        },
        "routes": compact_routes,
        "gold_labels": labels,
    }
    return report, labels


def _write_csv(path: Path, labels: list[dict[str, Any]]) -> None:
    fields = (
        "question_id",
        "route_index",
        "ticker",
        "filing_year",
        "section_code",
        "gold_chunk_id",
        "declared_gold_chunk_index",
        "candidate_count",
        "semantic_rank",
        "semantic_top20",
        "cross_encoder_rank_if_top20",
        "lexical_rank",
        "hybrid_rrf_rank",
        "supporting_passage_byte_exact_contained",
        "supporting_passage_normalized_contained",
        "primary_machine_classification",
        "decomposition_assessment",
        "boundary_assessment",
        "top5_neighbor_answer_overlap_assessment",
    )
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for label in labels:
            source = label["source"]
            writer.writerow(
                {
                    **{field: label.get(field) for field in fields},
                    "ticker": source["ticker"],
                    "filing_year": source["filing_year"],
                    "section_code": source["section_code"],
                }
            )


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.output, args.csv, args.manifest):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite: {path}")

    started_at = _utcnow()
    started = time.monotonic()
    with ProductionRetriever() as retriever:
        retriever._load_models()
        report, labels = run_audit(args.questions, FunnelAuditor(retriever))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write_csv(args.csv, labels)
    manifest = {
        "started_at": started_at,
        "finished_at": _utcnow(),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "git_head": _git_head(),
        "questions_path": str(args.questions),
        "questions_sha256": _sha256(args.questions),
        "script_sha256": _sha256(Path(__file__)),
        "output_path": str(args.output),
        "output_sha256": _sha256(args.output),
        "csv_path": str(args.csv),
        "csv_sha256": _sha256(args.csv),
        "question_count": len(MISS_IDS),
        "gold_chunk_count": len(labels),
        "in_sample": True,
        "diagnostic_only": True,
        "database_writes": False,
    }
    with args.manifest.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
