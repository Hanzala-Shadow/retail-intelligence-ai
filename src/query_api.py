#!/usr/bin/env python3
"""Production 10-K retrieval entry point.

Policy: required-section routing -> top 20 semantic candidates per authorized
source -> pinned cross-encoder reranking -> deterministic source-aware top 5.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

BI_ENCODER_REPO = "BAAI/bge-base-en-v1.5"
BI_ENCODER_REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
BI_ENCODER_DIMENSION = 768
BI_ENCODER_MAX_LENGTH = 512
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CROSS_ENCODER_REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
CROSS_ENCODER_REVISION = "c5ee24cb16019beea0893ab7796b1df96625c6b8"
CROSS_ENCODER_MAX_LENGTH = 512

EMBEDDING_TABLE = "benchmark_embeddings_bge_base_en_v15"
LIVE_VIEW = "rag_eligible_10k_chunks"
CANDIDATES_PER_SOURCE = 20
FINAL_EVIDENCE_COUNT = 5
CROSS_ENCODER_BATCH_SIZE = 8
MULTIVIEW_RRF_K = 60
SECTION_ADAPTIVE_POLICY_VERSION = "1.0.0"
SECTION_PROFILES = {
    "Item_1": "business operations products services customers channels stores distribution sourcing suppliers competition",
    "Item_1A": "risk factors exposure uncertainty adverse impact mitigation regulation cybersecurity supply chain macroeconomic",
    "Item_7": "management discussion analysis results operations trends drivers changes year over year revenue margin expenses inventory liquidity cash flows",
    "Item_8": "financial statements notes accounting policy recognition measurement estimates commitments contingencies impairment taxes leases",
}
NARRATIVE_RRF_SECTIONS = {"Item_1", "Item_1A", "Item_7"}


@dataclass(frozen=True)
class SourceSpec:
    ticker: str
    filing_year: int
    accession_number: str
    section_code: str
    doc_type: str = "10-K"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SourceSpec":
        required = ("ticker", "filing_year", "accession_number", "section_code")
        missing = [name for name in required if value.get(name) in (None, "")]
        if missing:
            raise ValueError(f"source is missing required fields: {', '.join(missing)}")
        doc_type = str(value.get("doc_type", "10-K")).strip()
        if doc_type != "10-K":
            raise ValueError("production 10-K retrieval only permits doc_type='10-K'")
        return cls(
            ticker=str(value["ticker"]).strip(),
            filing_year=int(value["filing_year"]),
            accession_number=str(value["accession_number"]).strip(),
            section_code=str(value["section_code"]).strip(),
            doc_type=doc_type,
        )


def _vector_literal(vector: Iterable[float]) -> str:
    import numpy as np

    values = np.asarray(list(vector), dtype=np.float32)
    if values.shape != (BI_ENCODER_DIMENSION,) or not np.isfinite(values).all():
        raise ValueError("query embedding must be a finite 768-dimensional vector")
    return "[" + ",".join(format(float(item), ".9g") for item in values) + "]"


def _connect():
    from dotenv import load_dotenv
    import psycopg2

    load_dotenv()
    dsn = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    return psycopg2.connect(dsn) if dsn else psycopg2.connect()


def _round_robin(per_source: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    depth = 0
    while len(output) < limit and any(depth < len(rows) for rows in per_source):
        for rows in per_source:
            if depth >= len(rows):
                continue
            row = rows[depth]
            chunk_id = int(row["chunk_id"])
            if chunk_id not in seen:
                output.append(row)
                seen.add(chunk_id)
                if len(output) == limit:
                    break
        depth += 1
    return output


class ProductionRetriever:
    def __init__(self, conn=None, bi_encoder=None, cross_encoder=None):
        self.conn = conn or _connect()
        self._owns_connection = conn is None
        self.bi_encoder = bi_encoder
        self.cross_encoder = cross_encoder

    def close(self) -> None:
        if self._owns_connection and self.conn is not None:
            self.conn.close()
        self.conn = None

    def __enter__(self) -> "ProductionRetriever":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _load_models(self) -> None:
        if self.bi_encoder is None:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(2)
            self.bi_encoder = SentenceTransformer(
                BI_ENCODER_REPO,
                revision=BI_ENCODER_REVISION,
                trust_remote_code=False,
                device="cpu",
            )
            self.bi_encoder.max_seq_length = BI_ENCODER_MAX_LENGTH
            dimension = self.bi_encoder.get_embedding_dimension()
            if dimension != BI_ENCODER_DIMENSION:
                raise RuntimeError(
                    f"bi-encoder dimension mismatch: {dimension} != {BI_ENCODER_DIMENSION}"
                )
        if self.cross_encoder is None:
            from sentence_transformers import CrossEncoder

            self.cross_encoder = CrossEncoder(
                CROSS_ENCODER_REPO,
                revision=CROSS_ENCODER_REVISION,
                device="cpu",
                max_length=CROSS_ENCODER_MAX_LENGTH,
            )

    def _fetch_candidates(
        self, question: str, source: SourceSpec, query_vector: str
    ) -> list[dict[str, Any]]:
        sql = f"""
          SELECT r.chunk_id, r.ticker, r.filing_year, r.accession_number,
                 r.doc_type, r.section_code, r.chunk_index, r.chunk_text,
                 r.embedding_text, r.token_count,
                 1-(e.embedding <=> %s::vector) AS semantic_score
          FROM public.{EMBEDDING_TABLE} e
          JOIN public.{LIVE_VIEW} r USING(chunk_id)
          WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
            AND r.accession_number=%s AND r.section_code=%s
          ORDER BY e.embedding <=> %s::vector, r.chunk_id
          LIMIT %s
        """
        params = (
            query_vector,
            source.ticker,
            source.filing_year,
            source.doc_type,
            source.accession_number,
            source.section_code,
            query_vector,
            CANDIDATES_PER_SOURCE,
        )
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [column.name for column in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        for rank, row in enumerate(rows, 1):
            row["semantic_score"] = float(row["semantic_score"])
            row["semantic_rank"] = rank
            row["source"] = asdict(source)
        if not rows:
            raise LookupError(f"authorized source produced no candidates: {source}")
        return rows

    def retrieve(self, question: str, sources: Iterable[SourceSpec]) -> dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            raise ValueError("question must be non-empty")
        source_list = list(sources)
        if not source_list:
            raise ValueError("at least one authorized source is required")
        if len(set(source_list)) != len(source_list):
            raise ValueError("authorized sources must be unique")

        self._load_models()
        vector = self.bi_encoder.encode(
            [QUERY_PREFIX + question],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        query_vector = _vector_literal(vector)
        candidates = [
            self._fetch_candidates(question, source, query_vector)
            for source in source_list
        ]
        for rows in candidates:
            pairs = [(question, row["embedding_text"] or "") for row in rows]
            scores = self.cross_encoder.predict(
                pairs,
                batch_size=CROSS_ENCODER_BATCH_SIZE,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            for row, score in zip(rows, scores, strict=True):
                row["cross_encoder_score"] = float(score)
            rows.sort(
                key=lambda row: (-row["cross_encoder_score"], int(row["chunk_id"]))
            )
            for rank, row in enumerate(rows, 1):
                row["cross_encoder_rank_within_source"] = rank

        evidence = _round_robin(candidates, FINAL_EVIDENCE_COUNT)
        if len(evidence) < FINAL_EVIDENCE_COUNT:
            raise RuntimeError(
                f"retrieval returned {len(evidence)} unique chunks; expected 5"
            )
        for rank, row in enumerate(evidence, 1):
            row["final_rank"] = rank

        return {
            "question": question,
            "policy": {
                "section_routing": "required_hard_filter",
                "candidates_per_source": CANDIDATES_PER_SOURCE,
                "reranker": CROSS_ENCODER_REPO,
                "reranker_revision": CROSS_ENCODER_REVISION,
                "final_evidence_count": FINAL_EVIDENCE_COUNT,
                "multi_source_merge": "deterministic_round_robin",
                "bi_encoder": BI_ENCODER_REPO,
                "bi_encoder_revision": BI_ENCODER_REVISION,
            },
            "sources": [asdict(source) for source in source_list],
            "candidate_counts_by_source": [len(rows) for rows in candidates],
            "evidence": evidence,
        }

    def retrieve_requirement(
        self,
        subquery: Any,
        *,
        original_question: str,
    ) -> dict[str, Any]:
        """Retrieve one decomposed requirement using the frozen adaptive policy.

        Models remain pinned. Candidate diversity comes from three deterministic
        query views; final ordering is selected only by SEC section family.
        """
        original_question = str(original_question or "").strip()
        focused_question = str(subquery.question or "").strip()
        claim_key = str(subquery.claim_key or "").strip()
        if not original_question or not focused_question or not claim_key:
            raise ValueError("adaptive requirement retrieval needs original, focused, and claim text")
        source = SourceSpec(
            ticker=subquery.ticker,
            filing_year=subquery.filing_year,
            accession_number=subquery.accession_number,
            section_code=subquery.section_code,
            doc_type=subquery.doc_type,
        )
        profile = SECTION_PROFILES.get(
            source.section_code,
            "annual report disclosure description factors changes impacts",
        )
        raw_views = {
            "original": original_question,
            "focused": focused_question,
            "profile": f"{claim_key}. {profile}",
        }
        for index, query in enumerate(
            tuple(getattr(subquery, "search_queries", ()))[:2], start=1
        ):
            query = str(query or "").strip()
            if query:
                raw_views[f"planner_{index}"] = query
        views: dict[str, str] = {}
        seen_queries: set[str] = set()
        for name, query in raw_views.items():
            normalized = " ".join(query.casefold().split())
            if normalized not in seen_queries:
                views[name] = query
                seen_queries.add(normalized)

        self._load_models()
        candidates_by_view: dict[str, list[dict[str, Any]]] = {}
        for name, query in views.items():
            vector = self.bi_encoder.encode(
                [QUERY_PREFIX + query],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
            candidates_by_view[name] = self._fetch_candidates(
                query, source, _vector_literal(vector)
            )

        by_chunk: dict[int, dict[str, Any]] = {}
        for name, rows in candidates_by_view.items():
            for rank, row in enumerate(rows, start=1):
                chunk_id = int(row["chunk_id"])
                pooled = by_chunk.setdefault(chunk_id, dict(row))
                pooled.setdefault("view_ranks", {})[name] = rank
                pooled["candidate_rrf_score"] = float(
                    pooled.get("candidate_rrf_score", 0.0)
                ) + 1.0 / (MULTIVIEW_RRF_K + rank)
        pool = list(by_chunk.values())
        if not pool:
            raise LookupError(f"authorized source produced no candidates: {source}")
        for row in pool:
            row["semantic_rank"] = min(row["view_ranks"].values())

        pairs = [(focused_question, row.get("embedding_text") or "") for row in pool]
        scores = self.cross_encoder.predict(
            pairs,
            batch_size=CROSS_ENCODER_BATCH_SIZE,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        for row, score in zip(pool, scores, strict=True):
            row["cross_encoder_score"] = float(score)
        ce_order = sorted(
            pool,
            key=lambda row: (-row["cross_encoder_score"], int(row["chunk_id"])),
        )
        for rank, row in enumerate(ce_order, start=1):
            row["cross_encoder_rank_within_source"] = rank
        rrf_order = sorted(
            pool,
            key=lambda row: (-row["candidate_rrf_score"], int(row["chunk_id"])),
        )
        for rank, row in enumerate(rrf_order, start=1):
            row["multiview_rrf_rank"] = rank

        if source.section_code == "Item_8":
            ordered = ce_order
            selection_policy = "cross_encoder_financial_notes"
        elif source.section_code in NARRATIVE_RRF_SECTIONS:
            ordered = rrf_order
            selection_policy = "multiview_rrf_narrative"
        else:
            for row in pool:
                row["adaptive_hybrid_score"] = (
                    0.5 / row["cross_encoder_rank_within_source"]
                    + 0.5 / row["multiview_rrf_rank"]
                )
            ordered = sorted(
                pool,
                key=lambda row: (
                    -row["adaptive_hybrid_score"],
                    row["cross_encoder_rank_within_source"],
                    row["multiview_rrf_rank"],
                    int(row["chunk_id"]),
                ),
            )
            selection_policy = "equal_rank_blend_unknown_section"

        evidence = ordered[:FINAL_EVIDENCE_COUNT]
        if len(evidence) != FINAL_EVIDENCE_COUNT:
            raise RuntimeError(
                f"adaptive retrieval returned {len(evidence)} unique chunks; expected 5"
            )
        for rank, row in enumerate(evidence, start=1):
            row["final_rank"] = rank
            row["selection_rank"] = rank
            row["selection_policy"] = selection_policy
        return {
            "question": focused_question,
            "policy": {
                "policy_version": SECTION_ADAPTIVE_POLICY_VERSION,
                "models_fixed": True,
                "section_routing": "detected_required_hard_filter",
                "query_views": list(views),
                "candidates_per_view": CANDIDATES_PER_SOURCE,
                "candidate_fusion": f"reciprocal_rank_fusion_k_{MULTIVIEW_RRF_K}",
                "section_selection": selection_policy,
                "final_evidence_count": FINAL_EVIDENCE_COUNT,
                "bi_encoder": BI_ENCODER_REPO,
                "bi_encoder_revision": BI_ENCODER_REVISION,
                "reranker": CROSS_ENCODER_REPO,
                "reranker_revision": CROSS_ENCODER_REVISION,
            },
            "sources": [asdict(source)],
            "candidate_counts_by_view": {
                name: len(rows) for name, rows in candidates_by_view.items()
            },
            "pooled_candidate_count": len(pool),
            "evidence": evidence,
        }


def _load_request(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="request JSON path, or - for stdin")
    parser.add_argument("--output", help="optional output JSON path; refuses overwrite")
    args = parser.parse_args()

    request = _load_request(args.request)
    sources = [SourceSpec.from_mapping(item) for item in request.get("sources", [])]
    with ProductionRetriever() as retriever:
        result = retriever.retrieve(request.get("question", ""), sources)
    result["request_id"] = request.get("request_id")
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
