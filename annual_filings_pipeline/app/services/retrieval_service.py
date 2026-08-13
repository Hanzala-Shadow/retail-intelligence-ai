"""Read-only retrieval adapter with process-resident routing metadata."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

LOGGER = logging.getLogger("annual_filings_chatbot.retrieval")
_CACHE_LOCK = threading.Lock()
_BI_ENCODER: Any | None = None
_ROUTING_METADATA: tuple[Any, set[str], dict[str, str]] | None = None
_ROUTING_LOAD_MS: float | None = None


def _dsn() -> str:
    value = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DB_URL/DATABASE_URL is unavailable")
    return value


def resident_bi_encoder() -> Any:
    """Create the authenticated private GPU query encoder once."""
    global _BI_ENCODER
    if _BI_ENCODER is None:
        with _CACHE_LOCK:
            if _BI_ENCODER is None:
                from src.remote_embedder import RemoteEmbedder
                _BI_ENCODER = RemoteEmbedder.from_env()
    return _BI_ENCODER


def preload_routing_metadata() -> tuple[Any, set[str], dict[str, str]]:
    """Load the eligible filing/source catalog once in a read-only transaction."""
    global _ROUTING_METADATA, _ROUTING_LOAD_MS
    if _ROUTING_METADATA is not None:
        return _ROUTING_METADATA
    with _CACHE_LOCK:
        if _ROUTING_METADATA is not None:
            return _ROUTING_METADATA
        import psycopg2
        from src.decomposed_query_api import _aliases_from_connection
        from src.query_decomposition import SourceResolver

        started = time.perf_counter()
        conn = psycopg2.connect(_dsn())
        conn.set_session(readonly=True, autocommit=False)
        try:
            resolver = SourceResolver.from_connection(conn)
            tickers, aliases = _aliases_from_connection(conn)
            conn.rollback()
        finally:
            conn.close()
        _ROUTING_METADATA = (resolver, tickers, aliases)
        _ROUTING_LOAD_MS = round((time.perf_counter() - started) * 1000, 3)
        LOGGER.info(
            "routing_catalog_ready tickers=%d aliases=%d load_ms=%.3f",
            len(tickers), len(aliases), _ROUTING_LOAD_MS,
        )
    return _ROUTING_METADATA


def routing_catalog_stats() -> dict[str, Any]:
    metadata = _ROUTING_METADATA
    return {
        "ready": metadata is not None,
        "tickers": len(metadata[1]) if metadata else 0,
        "aliases": len(metadata[2]) if metadata else 0,
        "load_ms": _ROUTING_LOAD_MS,
    }


def retrieve(question: str, request_id: str) -> dict[str, Any]:
    import psycopg2
    from src.decomposed_query_api import run_query
    from src.query_api import ProductionRetriever

    request_started = time.perf_counter()
    connect_started = time.perf_counter()
    conn = psycopg2.connect(_dsn())
    conn.set_session(readonly=True, autocommit=False)
    connect_ms = round((time.perf_counter() - connect_started) * 1000, 3)
    try:
        with ProductionRetriever(
            conn=conn,
            bi_encoder=resident_bi_encoder(),
        ) as retriever:
            # run_query otherwise performs two corpus-wide DISTINCT scans for
            # every request. The immutable process cache is built at startup.
            retriever._routing_metadata_cache = preload_routing_metadata()
            run_started = time.perf_counter()
            result = run_query(
                question, retriever=retriever, request_id=request_id,
            )
            run_ms = round((time.perf_counter() - run_started) * 1000, 3)
        conn.rollback()
        core_ms = float(
            (result.get("runtime_profile") or {})
            .get("timings_ms", {}).get("total", 0.0)
        )
        adapter_total_ms = round(
            (time.perf_counter() - request_started) * 1000, 3
        )
        result["chatbot_retrieval_timings_ms"] = {
            "adapter_total": adapter_total_ms,
            "database_connect": connect_ms,
            "routing_and_retrieval": run_ms,
            "retrieval_core": core_ms,
            "routing_orchestration": round(max(0.0, run_ms - core_ms), 3),
            "routing_catalog_load": _ROUTING_LOAD_MS,
        }
        return result
    finally:
        conn.close()
