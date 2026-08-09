"""Cheap dependency readiness checks without retrieval or generation."""
from __future__ import annotations

import os
from typing import Any, Callable


def database_ready() -> bool:
    import psycopg2

    dsn = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        return False
    conn = psycopg2.connect(dsn, connect_timeout=3)
    conn.set_session(readonly=True, autocommit=False)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            ready = cursor.fetchone() == (1,)
        conn.rollback()
        return ready
    finally:
        conn.close()


def gpu_ready() -> bool:
    import requests

    url = os.getenv("RAG_GPU_RERANKER_URL", "").rstrip("/")
    token = os.getenv("RAG_GPU_RERANKER_TOKEN", "")
    if not url or len(token) < 32:
        return False
    response = requests.get(
        f"{url}/health/ready",
        headers={"Authorization": f"Bearer {token}"},
        timeout=3,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("status") == "ready" and data.get("device") == "cuda"


def readiness(
    db_check: Callable[[], bool] = database_ready,
    gpu_check: Callable[[], bool] = gpu_ready,
) -> dict[str, Any]:
    components: dict[str, bool] = {}
    for name, check in (("database", db_check), ("gpu_reranker", gpu_check)):
        try:
            components[name] = bool(check())
        except Exception:
            components[name] = False
    ready = all(components.values())
    return {
        "status": "ready" if ready else "degraded",
        "schema_version": 1,
        "components": components,
        "database_writes": False,
    }
