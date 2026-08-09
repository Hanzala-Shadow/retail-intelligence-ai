#!/usr/bin/env python3
"""Production 10-K retrieval entry point.

Policy: required-section routing -> top 20 semantic candidates per authorized
source -> pinned cross-encoder reranking -> deterministic source-aware top 5.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
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

ANCHORED_CONFIG_DEFAULT = "config/retrieval_anchored_k16_v1.json"
ANCHORED_POLICY_ENV = "RAG_RETRIEVAL_POLICY"
ANCHORED_POLICY_ID = "balanced_anchored_round_robin_k16"

EMBEDDING_TABLE = "benchmark_embeddings_bge_base_en_v15"
LIVE_VIEW = "rag_eligible_10k_chunks"
CANDIDATES_PER_SOURCE = 20
FINAL_EVIDENCE_COUNT = 5
CROSS_ENCODER_BATCH_SIZE = 8
MULTIVIEW_RRF_K = 60
SECTION_ADAPTIVE_POLICY_VERSION = "1.0.0"
SOFT_SECTION_ROUTING_VERSION = "1.1.0"
SUPPORTED_RETRIEVAL_SECTIONS = ("Item_1", "Item_1A", "Item_7", "Item_8")
SECTION_PROFILES = {
    "Item_1": "business operations products services customers channels stores distribution sourcing suppliers competition",
    "Item_1A": "risk factors exposure uncertainty adverse impact mitigation regulation cybersecurity supply chain macroeconomic",
    "Item_7": "management discussion analysis results operations trends drivers changes year over year revenue margin expenses inventory liquidity cash flows",
    "Item_8": "financial statements notes accounting policy recognition measurement estimates commitments contingencies impairment taxes leases",
}
NARRATIVE_RRF_SECTIONS = {"Item_1", "Item_1A", "Item_7"}


def _runtime_model_device() -> str:
    """Return the explicitly authorized inference device.

    CPU remains the production default. GPU execution must be opted into for
    an isolated benchmark or a dedicated inference worker.
    """
    device = os.getenv("RAG_MODEL_DEVICE", "cpu").strip().lower()
    if device == "cpu" or device == "cuda" or device.startswith("cuda:"):
        return device
    raise ValueError("RAG_MODEL_DEVICE must be cpu, cuda, or cuda:<index>")


def _runtime_reranker_backend() -> str:
    """Return the explicitly selected anchored-reranker execution backend."""
    backend = os.getenv("RAG_RERANKER_BACKEND", "local").strip().lower()
    if backend in {"local", "remote"}:
        return backend
    raise ValueError("RAG_RERANKER_BACKEND must be local or remote")


def _runtime_embedder_backend() -> str:
    backend = os.getenv("RAG_EMBEDDER_BACKEND", "local").strip().lower()
    if backend in {"local", "remote"}:
        return backend
    raise ValueError("RAG_EMBEDDER_BACKEND must be local or remote")


def _runtime_profile_device() -> str:
    return (
        "remote_cuda"
        if _runtime_reranker_backend() == "remote"
        else _runtime_model_device()
    )


def _peak_rss_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _evidence_identity_sha256(evidence: list[dict[str, Any]]) -> str:
    """Hash only deterministic selection identity, never passage text."""
    identity = [
        {
            "final_rank": int(item["final_rank"]),
            "source_chunk_id": item.get("source_chunk_id"),
            "chunk_id": int(item["chunk_id"]),
            "selected_for_subquery_id": str(
                item.get("selected_for_subquery_id") or ""
            ),
            "selection_reason": str(item.get("selection_reason") or ""),
        }
        for item in evidence
    ]
    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    def __init__(
        self,
        conn=None,
        bi_encoder=None,
        cross_encoder=None,
        *,
        anchor_cross_encoder=None,
        expansion_cross_encoder=None,
        anchored_config=None,
        remote_reranker_client=None,
    ):
        self.conn = conn or _connect()
        self._owns_connection = conn is None
        self.bi_encoder = bi_encoder
        self.cross_encoder = cross_encoder
        self.anchor_cross_encoder = anchor_cross_encoder
        self.expansion_cross_encoder = expansion_cross_encoder
        self.anchored_config = anchored_config
        self.remote_reranker_client = remote_reranker_client

    def close(self) -> None:
        if self._owns_connection and self.conn is not None:
            self.conn.close()
        self.conn = None

    def __enter__(self) -> "ProductionRetriever":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _load_bi_encoder(
        self,
        runtime_profile: dict[str, Any] | None = None,
    ) -> None:
        if self.bi_encoder is None:
            if _runtime_embedder_backend() == "remote":
                from src.remote_embedder import RemoteEmbedder
                self.bi_encoder = RemoteEmbedder.from_env()
                if runtime_profile is not None:
                    runtime_profile["timings_ms"]["bi_encoder_load"] += 0.0
                return
            import torch
            from sentence_transformers import SentenceTransformer

            started = time.perf_counter()
            torch.set_num_threads(2)
            self.bi_encoder = SentenceTransformer(
                BI_ENCODER_REPO,
                revision=BI_ENCODER_REVISION,
                trust_remote_code=False,
                device=_runtime_model_device(),
            )
            self.bi_encoder.max_seq_length = BI_ENCODER_MAX_LENGTH
            dimension = self.bi_encoder.get_embedding_dimension()
            if dimension != BI_ENCODER_DIMENSION:
                raise RuntimeError(
                    f"bi-encoder dimension mismatch: {dimension} != {BI_ENCODER_DIMENSION}"
                )
            if runtime_profile is not None:
                runtime_profile["timings_ms"]["bi_encoder_load"] += (
                    time.perf_counter() - started
                ) * 1000

    def _load_models(self) -> None:
        self._load_bi_encoder()
        if self.cross_encoder is None:
            from sentence_transformers import CrossEncoder

            self.cross_encoder = CrossEncoder(
                CROSS_ENCODER_REPO,
                revision=CROSS_ENCODER_REVISION,
                device="cpu",
                max_length=CROSS_ENCODER_MAX_LENGTH,
            )

    def _fetch_candidates(
        self,
        question: str,
        source: SourceSpec,
        query_vector: str,
        *,
        limit: int = CANDIDATES_PER_SOURCE,
    ) -> list[dict[str, Any]]:
        sql = f"""
          SELECT r.chunk_id, r.source_chunk_id, r.chunk_text_sha256,
                 r.ticker, r.filing_year, r.coverage_year, r.accession_number,
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
            limit,
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

    def _fetch_soft_section_candidates(
        self,
        question: str,
        source: SourceSpec,
        query_vector: str,
        *,
        limit: int = CANDIDATES_PER_SOURCE,
    ) -> list[dict[str, Any]]:
        """Retrieve across supported sections while preserving hard source identity."""
        sql = f"""
          SELECT r.chunk_id, r.source_chunk_id, r.chunk_text_sha256,
                 r.ticker, r.filing_year, r.coverage_year, r.accession_number,
                 r.doc_type, r.section_code, r.chunk_index, r.chunk_text,
                 r.embedding_text, r.token_count,
                 1-(e.embedding <=> %s::vector) AS semantic_score
          FROM public.{EMBEDDING_TABLE} e
          JOIN public.{LIVE_VIEW} r USING(chunk_id)
          WHERE r.ticker=%s AND r.filing_year=%s AND r.doc_type=%s
            AND r.accession_number=%s
            AND r.section_code = ANY(%s)
          ORDER BY e.embedding <=> %s::vector, r.chunk_id
          LIMIT %s
        """
        params = (
            query_vector,
            source.ticker,
            source.filing_year,
            source.doc_type,
            source.accession_number,
            list(SUPPORTED_RETRIEVAL_SECTIONS),
            query_vector,
            limit,
        )
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [column.name for column in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        for rank, row in enumerate(rows, 1):
            row["semantic_score"] = float(row["semantic_score"])
            row["semantic_rank"] = rank
            row["source"] = asdict(source)
            row["preferred_section_code"] = source.section_code
            row["section_route_match"] = row["section_code"] == source.section_code
        if not rows:
            raise LookupError(
                f"authorized source produced no supported-section candidates: {source}"
            )
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
        views: dict[str, str] = {}
        seen_queries: set[str] = set()
        for name, query in raw_views.items():
            normalized = " ".join(query.casefold().split())
            if normalized not in seen_queries:
                views[name] = query
                seen_queries.add(normalized)

        self._load_models()
        candidates_by_view: dict[str, list[dict[str, Any]]] = {}
        soft_section_fallback_used = False
        for name, query in views.items():
            vector = self.bi_encoder.encode(
                [QUERY_PREFIX + query],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
            query_vector = _vector_literal(vector)
            try:
                preferred_candidates = self._fetch_candidates(
                    query,
                    source,
                    query_vector,
                )
            except LookupError:
                preferred_candidates = []
            if len(preferred_candidates) >= FINAL_EVIDENCE_COUNT:
                candidates_by_view[name] = preferred_candidates
            else:
                soft_section_fallback_used = True
                candidates_by_view[name] = self._fetch_soft_section_candidates(
                    query,
                    source,
                    query_vector,
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
                "soft_section_routing_version": SOFT_SECTION_ROUTING_VERSION,
                "models_fixed": True,
                "section_routing": "hard_section_with_supported_section_fallback",
                "soft_section_fallback_used": soft_section_fallback_used,
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

    def _anchored_policy(self):
        from src.anchored_reranking import AnchoredRerankingConfig

        if self.anchored_config is None:
            path = os.getenv("RAG_ANCHORED_CONFIG", ANCHORED_CONFIG_DEFAULT)
            self.anchored_config = AnchoredRerankingConfig.load(path)
        return self.anchored_config

    def _score_anchored_pairs(
        self,
        pairs: list[tuple[str, str]],
        *,
        role: str,
        runtime_profile: dict[str, Any] | None = None,
    ) -> list[float]:
        """Score one frozen reranker role with memory-safe lifecycle control."""
        import gc

        config = self._anchored_policy()
        if role == "anchor":
            injected = self.anchor_cross_encoder
            model_id = config.anchor_model_id
            revision = config.anchor_model_revision
        elif role == "expansion":
            injected = self.expansion_cross_encoder
            model_id = config.expansion_model_id
            revision = config.expansion_model_revision
        else:
            raise ValueError(f"unknown reranker role: {role}")

        if _runtime_reranker_backend() == "remote":
            if self.remote_reranker_client is None:
                from src.remote_reranker import RemoteRerankerClient

                self.remote_reranker_client = RemoteRerankerClient.from_env()
            inference_started = time.perf_counter()
            scores = self.remote_reranker_client.score(
                role=role,
                model_id=model_id,
                revision=revision,
                max_length=config.max_length,
                batch_size=config.batch_size,
                pairs=pairs,
            )
            if runtime_profile is not None:
                runtime_profile["timings_ms"][f"{role}_inference"] += (
                    time.perf_counter() - inference_started
                ) * 1000
                runtime_profile["scored_pairs"][role] = len(pairs)
            return scores

        model = injected
        loaded_here = model is None
        load_started = time.perf_counter()
        if loaded_here:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(
                model_id,
                revision=revision,
                trust_remote_code=False,
                device=_runtime_model_device(),
                max_length=config.max_length,
            )
            if config.model_lifecycle == "resident":
                if role == "anchor":
                    self.anchor_cross_encoder = model
                else:
                    self.expansion_cross_encoder = model
                loaded_here = False
        if runtime_profile is not None:
            runtime_profile["timings_ms"][f"{role}_model_load"] += (
                time.perf_counter() - load_started
            ) * 1000
            runtime_profile["scored_pairs"][role] = len(pairs)
        inference_started = time.perf_counter()
        values = model.predict(
            pairs,
            batch_size=config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        if runtime_profile is not None:
            runtime_profile["timings_ms"][f"{role}_inference"] += (
                time.perf_counter() - inference_started
            ) * 1000
        scores = [float(value) for value in values]
        if loaded_here and config.model_lifecycle == "sequential":
            del model
            gc.collect()
        return scores

    @staticmethod
    def _merge_candidate_rows(
        rows_by_view: dict[str, list[dict[str, Any]]],
        *,
        subquery: Any,
        section_policy: str,
    ) -> list[dict[str, Any]]:
        merged: dict[int, dict[str, Any]] = {}
        for view_name, rows in rows_by_view.items():
            for rank, row in enumerate(rows, 1):
                chunk_id = int(row["chunk_id"])
                item = merged.setdefault(chunk_id, dict(row))
                item.setdefault("view_ranks", {})[view_name] = rank
        output = list(merged.values())
        for item in output:
            item["selected_for_subquery_id"] = subquery.subquery_id
            item["subquery_id"] = subquery.subquery_id
            item["claim_key"] = subquery.claim_key
            item["comparison_side_id"] = subquery.comparison_side_id
            item["section_policy"] = section_policy
            item["semantic_rank"] = min(item["view_ranks"].values())
        return output

    def _build_anchored_candidate_group(
        self,
        original_question: str,
        subquery: Any,
        runtime_profile: dict[str, Any],
    ) -> dict[str, Any]:
        config = self._anchored_policy()
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
            "focused": subquery.question,
            "profile": f"{subquery.claim_key}. {profile}",
        }
        views: dict[str, str] = {}
        normalized_seen: set[str] = set()
        for name, value in raw_views.items():
            normalized = " ".join(str(value).casefold().split())
            if normalized and normalized not in normalized_seen:
                views[name] = str(value)
                normalized_seen.add(normalized)

        self._load_bi_encoder(runtime_profile)
        hard_by_view: dict[str, list[dict[str, Any]]] = {}
        soft_by_view: dict[str, list[dict[str, Any]]] = {}
        for name, query in views.items():
            embedding_started = time.perf_counter()
            vector = self.bi_encoder.encode(
                [QUERY_PREFIX + query],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
            runtime_profile["timings_ms"]["embedding"] += (
                time.perf_counter() - embedding_started
            ) * 1000
            literal = _vector_literal(vector)
            fetch_started = time.perf_counter()
            try:
                hard_by_view[name] = self._fetch_candidates(
                    query,
                    source,
                    literal,
                    limit=config.hard_candidate_limit,
                )
            except LookupError:
                hard_by_view[name] = []
            runtime_profile["timings_ms"]["database_fetch"] += (
                time.perf_counter() - fetch_started
            ) * 1000
            fetch_started = time.perf_counter()
            try:
                soft_by_view[name] = self._fetch_soft_section_candidates(
                    query,
                    source,
                    literal,
                    limit=config.soft_candidate_limit,
                )
            except LookupError:
                soft_by_view[name] = []
            runtime_profile["timings_ms"]["database_fetch"] += (
                time.perf_counter() - fetch_started
            ) * 1000
        merge_started = time.perf_counter()
        hard = self._merge_candidate_rows(
            hard_by_view,
            subquery=subquery,
            section_policy="hard",
        )
        soft = self._merge_candidate_rows(
            soft_by_view,
            subquery=subquery,
            section_policy="soft",
        )
        runtime_profile["timings_ms"]["candidate_merge"] += (
            time.perf_counter() - merge_started
        ) * 1000
        runtime_profile["candidate_counts"]["views"] += len(views)
        runtime_profile["candidate_counts"]["hard_positions"] += sum(
            len(rows) for rows in hard_by_view.values()
        )
        runtime_profile["candidate_counts"]["soft_positions"] += sum(
            len(rows) for rows in soft_by_view.values()
        )
        runtime_profile["candidate_counts"]["hard_unique"] += len(hard)
        runtime_profile["candidate_counts"]["soft_unique"] += len(soft)
        if not hard and not soft:
            raise LookupError(
                f"no candidates for anchored requirement {subquery.subquery_id}"
            )
        return {
            "requirement_id": subquery.subquery_id,
            "subquery": subquery,
            "hard": hard,
            "soft": soft,
        }

    def retrieve_anchored(
        self,
        original_question: str,
        subqueries: Iterable[Any],
    ) -> dict[str, Any]:
        """Run the frozen L12-anchor/BGE-expansion policy across requirements."""
        from src.anchored_reranking import select_anchored_evidence

        original_question = str(original_question or "").strip()
        if not original_question:
            raise ValueError("question must be non-empty")
        requirements = list(subqueries)
        if not requirements:
            raise ValueError("at least one requirement is required")
        total_started = time.perf_counter()
        config = self._anchored_policy()
        runtime_profile: dict[str, Any] = {
            "schema_version": 1,
            "device": _runtime_profile_device(),
            "reranker_backend": _runtime_reranker_backend(),
            "model_lifecycle": config.model_lifecycle,
            "requirement_count": len(requirements),
            "candidate_counts": {
                "views": 0,
                "hard_positions": 0,
                "soft_positions": 0,
                "hard_unique": 0,
                "soft_unique": 0,
                "unique_requirement_chunk_pairs": 0,
            },
            "scored_pairs": {"anchor": 0, "expansion": 0},
            "timings_ms": {
                "bi_encoder_load": 0.0,
                "embedding": 0.0,
                "database_fetch": 0.0,
                "candidate_merge": 0.0,
                "anchor_model_load": 0.0,
                "anchor_inference": 0.0,
                "expansion_model_load": 0.0,
                "expansion_inference": 0.0,
                "selection": 0.0,
                "total": 0.0,
            },
            "peak_rss_kib_before": _peak_rss_kib(),
        }
        candidate_started = time.perf_counter()
        groups = [
            self._build_anchored_candidate_group(
                original_question,
                subquery,
                runtime_profile,
            )
            for subquery in requirements
        ]
        runtime_profile["timings_ms"]["candidate_build_total"] = (
            time.perf_counter() - candidate_started
        ) * 1000

        unique_pairs: list[tuple[str, str]] = []
        pair_rows: list[list[dict[str, Any]]] = []
        by_identity: dict[tuple[str, int], int] = {}
        for group in groups:
            question = str(group["subquery"].question)
            for row in [*group["hard"], *group["soft"]]:
                identity = (group["requirement_id"], int(row["chunk_id"]))
                index = by_identity.get(identity)
                if index is None:
                    passage = str(row.get(config.passage_field) or "").strip()
                    if not passage:
                        raise ValueError(
                            f"chunk {row.get('chunk_id')} lacks {config.passage_field}"
                        )
                    index = len(unique_pairs)
                    by_identity[identity] = index
                    unique_pairs.append((question, passage))
                    pair_rows.append([])
                pair_rows[index].append(row)
        runtime_profile["candidate_counts"][
            "unique_requirement_chunk_pairs"
        ] = len(unique_pairs)

        l12_scores = self._score_anchored_pairs(
            unique_pairs,
            role="anchor",
            runtime_profile=runtime_profile,
        )
        for rows, score in zip(pair_rows, l12_scores, strict=True):
            for row in rows:
                row["l12_score"] = score
                row["cross_encoder_score"] = score

        bge_scores = self._score_anchored_pairs(
            unique_pairs,
            role="expansion",
            runtime_profile=runtime_profile,
        )
        for rows, score in zip(pair_rows, bge_scores, strict=True):
            for row in rows:
                row["bge_score"] = score

        for group in groups:
            by_chunk: dict[int, list[dict[str, Any]]] = {}
            for row in [*group["hard"], *group["soft"]]:
                by_chunk.setdefault(int(row["chunk_id"]), []).append(row)
            l12_order = sorted(
                (rows[0] for rows in by_chunk.values()),
                key=lambda row: (-row["l12_score"], int(row["chunk_id"])),
            )
            for rank, row in enumerate(l12_order, 1):
                for copy in by_chunk[int(row["chunk_id"])]:
                    copy["cross_encoder_rank_within_source"] = rank

        selection_started = time.perf_counter()
        evidence = select_anchored_evidence(groups, config)
        runtime_profile["timings_ms"]["selection"] = (
            time.perf_counter() - selection_started
        ) * 1000
        represented = {
            str(item["selected_for_subquery_id"])
            for item in evidence
        }
        coverage = [
            {
                "subquery_id": requirement.subquery_id,
                "comparison_side_id": requirement.comparison_side_id,
                "claim_key": requirement.claim_key,
                "retrieval_status": (
                    "represented" if requirement.subquery_id in represented
                    else "unsupported"
                ),
            }
            for requirement in requirements
        ]
        runtime_profile["timings_ms"]["total"] = (
            time.perf_counter() - total_started
        ) * 1000
        runtime_profile["timings_ms"] = {
            name: round(float(value), 3)
            for name, value in runtime_profile["timings_ms"].items()
        }
        runtime_profile["peak_rss_kib_after"] = _peak_rss_kib()
        runtime_profile["evidence_identity_sha256"] = (
            _evidence_identity_sha256(evidence)
        )
        return {
            "status": "success",
            "question": original_question,
            "policy": {
                "policy_id": config.policy_id,
                "anchor_model": config.anchor_model_id,
                "anchor_model_revision": config.anchor_model_revision,
                "expansion_model": config.expansion_model_id,
                "expansion_model_revision": config.expansion_model_revision,
                "passage_field": config.passage_field,
                "max_length": config.max_length,
                "anchor_count": config.anchor_count,
                "final_evidence_count": config.evidence_limit,
                "candidate_views": ["original", "focused", "profile"],
                "database_writes": False,
                "gold_fields_used": False,
                "question_id_overrides": False,
                "model_lifecycle": config.model_lifecycle,
                "bi_encoder": BI_ENCODER_REPO,
                "bi_encoder_revision": BI_ENCODER_REVISION,
            },
            "requirement_coverage": coverage,
            "runtime_profile": runtime_profile,
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
