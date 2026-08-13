#!/usr/bin/env python3
"""Read-only preflight for the anchored annual-filings retrieval policy."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.anchored_reranking import AnchoredRerankingConfig  # noqa: E402
from src.query_api import (  # noqa: E402
    BI_ENCODER_DIMENSION,
    BI_ENCODER_REPO,
    BI_ENCODER_REVISION,
    EMBEDDING_TABLE,
    LIVE_VIEW,
)


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def memory_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def database_preflight() -> dict[str, Any]:
    from src.query_api import _connect

    conn = _connect()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            database, user = cursor.fetchone()
            cursor.execute("SELECT to_regclass(%s), to_regclass(%s)", (
                f"public.{LIVE_VIEW}",
                f"public.{EMBEDDING_TABLE}",
            ))
            live_view, embedding_table = cursor.fetchone()
            cursor.execute(f"SELECT COUNT(*) FROM public.{LIVE_VIEW}")
            eligible_chunks = int(cursor.fetchone()[0])
            cursor.execute(f"SELECT COUNT(*) FROM public.{EMBEDDING_TABLE}")
            embedded_chunks = int(cursor.fetchone()[0])
        conn.rollback()
        return {
            "database": database,
            "user": user,
            "transaction_read_only": True,
            "live_view": str(live_view) if live_view else None,
            "embedding_table": str(embedding_table) if embedding_table else None,
            "eligible_chunks": eligible_chunks,
            "embedded_chunks": embedded_chunks,
            "pass": bool(live_view and embedding_table and eligible_chunks == embedded_chunks),
        }
    finally:
        conn.close()


def load_model(model_id: str, revision: str, max_length: int) -> dict[str, Any]:
    import gc
    from sentence_transformers import CrossEncoder

    before = memory_kib()
    model = CrossEncoder(
        model_id,
        revision=revision,
        trust_remote_code=False,
        device="cpu",
        max_length=max_length,
    )
    score = float(model.predict(
        [("revenue trend", "Revenue increased because comparable sales improved.")],
        batch_size=1,
        show_progress_bar=False,
        convert_to_numpy=True,
    )[0])
    after = memory_kib()
    del model
    gc.collect()
    return {
        "model_id": model_id,
        "revision": revision,
        "smoke_score_finite": score == score and abs(score) != float("inf"),
        "peak_rss_before_kib": before,
        "peak_rss_after_kib": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/retrieval_anchored_k16_v1.json"),
    )
    parser.add_argument("--check-db", action="store_true")
    parser.add_argument("--load-models", action="store_true")
    args = parser.parse_args()
    config = AnchoredRerankingConfig.load(args.config)
    report: dict[str, Any] = {
        "status": "preflight",
        "python": platform.python_version(),
        "retrieval_policy_env": os.getenv("RAG_RETRIEVAL_POLICY"),
        "config": {
            "policy_id": config.policy_id,
            "anchor_model": config.anchor_model_id,
            "anchor_revision": config.anchor_model_revision,
            "expansion_model": config.expansion_model_id,
            "expansion_revision": config.expansion_model_revision,
            "anchor_count": config.anchor_count,
            "evidence_limit": config.evidence_limit,
            "model_lifecycle": config.model_lifecycle,
        },
        "bi_encoder": {
            "model_id": BI_ENCODER_REPO,
            "revision": BI_ENCODER_REVISION,
            "dimension": BI_ENCODER_DIMENSION,
        },
        "packages": {
            name: package_version(name)
            for name in ("sentence-transformers", "torch", "transformers", "psycopg2-binary")
        },
        "database_writes": False,
        "gold_fields_used": False,
    }
    if args.check_db:
        report["database"] = database_preflight()
    if args.load_models:
        report["model_smoke"] = [
            load_model(config.anchor_model_id, config.anchor_model_revision, config.max_length),
            load_model(config.expansion_model_id, config.expansion_model_revision, config.max_length),
        ]
    failures = []
    if report.get("database") and not report["database"]["pass"]:
        failures.append("database_preflight")
    if report.get("model_smoke") and not all(
        item["smoke_score_finite"] for item in report["model_smoke"]
    ):
        failures.append("model_smoke")
    report["failures"] = failures
    report["structural_pass"] = not failures
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PASS: anchored server preflight" if not failures else "FAIL: anchored server preflight")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
