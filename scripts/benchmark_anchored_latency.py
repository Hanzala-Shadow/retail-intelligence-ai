#!/usr/bin/env python3
"""Read-only latency benchmark for the frozen anchored K16 retriever."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} must be a JSON object")
            required = ("request_id", "question", "filters")
            missing = [name for name in required if not value.get(name)]
            if missing:
                raise ValueError(
                    f"line {line_number} is missing: {', '.join(missing)}"
                )
            rows.append(value)
    identifiers = [str(row["request_id"]) for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("request_id values must be unique")
    return rows


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _identity_rows(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "final_rank": int(item["final_rank"]),
            "source_chunk_id": item.get("source_chunk_id"),
            "chunk_id": int(item["chunk_id"]),
            "selected_for_subquery_id": item.get(
                "selected_for_subquery_id"
            ),
            "selection_reason": item.get("selection_reason"),
        }
        for item in evidence
    ]


def _prepare_output_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--device",
        choices=("cpu", "cuda", "remote"),
        required=True,
    )
    parser.add_argument("--expected-requests", type=int, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    if args.env_file:
        from dotenv import load_dotenv

        if not args.env_file.is_file():
            raise FileNotFoundError(args.env_file)
        load_dotenv(args.env_file)
    dsn = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DB_URL/DATABASE_URL is unavailable")

    requests = _read_jsonl(args.requests)
    if len(requests) != args.expected_requests:
        raise ValueError(
            f"found {len(requests)} requests; expected {args.expected_requests}"
        )
    _prepare_output_dir(args.output_dir)

    os.environ["RAG_RETRIEVAL_POLICY"] = (
        "balanced_anchored_round_robin_k16"
    )
    os.environ["RAG_ANCHORED_CONFIG"] = str(args.config.resolve())
    os.environ["RAG_MODEL_DEVICE"] = (
        "cpu" if args.device == "remote" else args.device
    )
    os.environ["RAG_RERANKER_BACKEND"] = (
        "remote" if args.device == "remote" else "local"
    )

    if args.device == "cuda":
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but torch cannot access it")
    if args.device == "remote":
        from src.remote_reranker import RemoteRerankerClient

        RemoteRerankerClient.from_env()

    import psycopg2

    from src.decomposed_query_api import run_query
    from src.query_api import ProductionRetriever

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=False)
    output_path = args.output_dir / "responses.jsonl"
    results: list[dict[str, Any]] = []
    try:
        with ProductionRetriever(conn=conn) as retriever:
            with output_path.open("w", encoding="utf-8") as handle:
                for index, request in enumerate(requests, 1):
                    result = run_query(
                        str(request["question"]),
                        dict(request["filters"]),
                        retriever=retriever,
                        request_id=str(request["request_id"]),
                    )
                    runtime = result.get("runtime_profile") or {}
                    row = {
                        "request_id": str(request["request_id"]),
                        "status": result.get("status"),
                        "error_code": result.get("error_code"),
                        "policy_id": (result.get("policy") or {}).get(
                            "policy_id"
                        ),
                        "evidence_count": len(result.get("evidence") or []),
                        "evidence_identity_sha256": runtime.get(
                            "evidence_identity_sha256"
                        ),
                        "evidence_identity": _identity_rows(
                            result.get("evidence") or []
                        ),
                        "runtime_profile": runtime,
                    }
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    handle.flush()
                    results.append(row)
                    total = (runtime.get("timings_ms") or {}).get("total")
                    print(
                        f"[{index}/{len(requests)}] "
                        f"{row['request_id']}: {row['status']} total_ms={total}"
                    )
        conn.rollback()
    finally:
        conn.close()

    successful = [row for row in results if row["status"] == "success"]
    totals = [
        float(row["runtime_profile"]["timings_ms"]["total"])
        for row in successful
    ]
    summary = {
        "schema_version": 1,
        "policy_id": "balanced_anchored_round_robin_k16",
        "device": args.device,
        "config": str(args.config),
        "database_writes": False,
        "question_text_exported": False,
        "requests": len(results),
        "successful": len(successful),
        "errors": len(results) - len(successful),
        "latency_ms": {
            "mean": round(statistics.mean(totals), 3) if totals else None,
            "p50": round(float(_percentile(totals, 0.50)), 3)
            if totals else None,
            "p95": round(float(_percentile(totals, 0.95)), 3)
            if totals else None,
            "max": round(max(totals), 3) if totals else None,
        },
        "structural_pass": (
            len(successful) == len(results)
            and all(row["evidence_count"] == 16 for row in successful)
            and all(row["evidence_identity_sha256"] for row in successful)
        ),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary["structural_pass"]:
        print("FAIL: anchored latency benchmark")
        return 1
    print("PASS: anchored latency benchmark")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
