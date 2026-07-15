from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import os
import platform
import resource
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import yaml


CONFIG_PATH = Path("config/embedding_benchmark_models.yaml")
FINAL_DIR = Path("reports/week3_day2/final_report")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config() -> dict[str, Any]:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a mapping")
    return config


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_input(
    config: dict[str, Any],
    rows: list[dict[str, str]],
    input_path: Path,
) -> None:
    benchmark = config["benchmark"]
    failures: list[str] = []

    expected_rows = int(benchmark["expected_rows"])
    expected_sha256 = str(benchmark["input_sha256"])
    actual_sha256 = sha256_file(input_path)

    if len(rows) != expected_rows:
        failures.append(
            f"Expected {expected_rows} rows, found {len(rows)}"
        )

    if actual_sha256 != expected_sha256:
        failures.append(
            f"Input SHA-256 mismatch: {actual_sha256}"
        )

    chunk_ids = [row["chunk_id"] for row in rows]
    if len(chunk_ids) != len(set(chunk_ids)):
        failures.append("Duplicate chunk IDs detected")

    for row_number, row in enumerate(rows, start=2):
        if not row["embedding_text"].strip():
            failures.append(f"Row {row_number}: empty embedding_text")
        if row["doc_type"] != "10-K":
            failures.append(f"Row {row_number}: doc_type is not 10-K")
        if row["doc_quality_status"] != "passed":
            failures.append(f"Row {row_number}: quality is not passed")
        if row["rag_action"] != "include":
            failures.append(f"Row {row_number}: rag_action is not include")
        if row["citation_ready"].lower() not in {"t", "true", "1"}:
            failures.append(f"Row {row_number}: not citation ready")

        token_count = int(row["token_count"])
        if not 50 <= token_count <= 500:
            failures.append(
                f"Row {row_number}: token_count={token_count}"
            )

    if failures:
        raise RuntimeError(
            "Input verification failed:\n" + "\n".join(failures[:25])
        )


def worker(model_id: str) -> int:
    result: dict[str, Any] = {
        "model_id": model_id,
        "status": "FAIL",
        "started_at": utc_now(),
    }

    config = load_config()
    benchmark = config["benchmark"]
    models = {model["id"]: model for model in config["models"]}

    if model_id not in models:
        raise KeyError(f"Unknown model ID: {model_id}")

    model_config = models[model_id]
    input_path = Path(benchmark["input_csv"])
    output_dir = Path(benchmark["output_directory"]) / model_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"

    try:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        os.environ.setdefault("MKL_NUM_THREADS", "2")

        import huggingface_hub
        import sentence_transformers
        import torch
        import transformers
        from huggingface_hub import model_info
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)

        rows = load_rows(input_path)
        validate_input(config, rows, input_path)

        texts = [row["embedding_text"] for row in rows]
        source_tokens = sum(int(row["token_count"]) for row in rows)

        requested_revision = model_config["requested_revision"]
        hub_info = model_info(
            model_config["model_name"],
            revision=requested_revision,
        )
        resolved_revision = hub_info.sha

        load_started = time.perf_counter()
        model = SentenceTransformer(
            model_config["model_name"],
            revision=resolved_revision,
            device=model_config["device"],
            trust_remote_code=model_config["trust_remote_code"],
        )
        model.max_seq_length = int(model_config["max_input_tokens"])
        model_load_seconds = time.perf_counter() - load_started

        actual_dimension = model.get_sentence_embedding_dimension()
        expected_dimension = int(model_config["dimension"])

        if actual_dimension != expected_dimension:
            raise RuntimeError(
                f"Dimension mismatch: expected {expected_dimension}, "
                f"received {actual_dimension}"
            )

        tokenizer_lengths: list[int] = []
        too_long = 0
        max_tokens = int(model_config["max_input_tokens"])

        for text in texts:
            encoded = model.tokenizer(
                text,
                add_special_tokens=True,
                truncation=False,
                return_attention_mask=False,
            )
            length = len(encoded["input_ids"])
            tokenizer_lengths.append(length)
            if length > max_tokens:
                too_long += 1

        warmup_count = min(
            int(benchmark["warmup_rows"]),
            len(texts),
        )
        model.encode(
            [
                model_config["document_prefix"] + text
                for text in texts[:warmup_count]
            ],
            batch_size=min(
                int(model_config["initial_batch_size"]),
                warmup_count,
            ),
            normalize_embeddings=bool(
                model_config["normalize_embeddings"]
            ),
            convert_to_numpy=True,
            show_progress_bar=False,
        )

        batch_size = int(model_config["initial_batch_size"])
        minimum_batch_size = int(model_config["minimum_batch_size"])
        embeddings = None
        encode_error = None

        while batch_size >= minimum_batch_size:
            try:
                started = time.perf_counter()
                embeddings = model.encode(
                    [
                        model_config["document_prefix"] + text
                        for text in texts
                    ],
                    batch_size=batch_size,
                    normalize_embeddings=bool(
                        model_config["normalize_embeddings"]
                    ),
                    convert_to_numpy=True,
                    show_progress_bar=True,
                )
                embedding_seconds = time.perf_counter() - started
                break
            except (RuntimeError, MemoryError) as exc:
                encode_error = f"{type(exc).__name__}: {exc}"
                gc.collect()
                if hasattr(torch, "cuda"):
                    torch.cuda.empty_cache()
                if batch_size == minimum_batch_size:
                    raise
                batch_size = max(minimum_batch_size, batch_size // 2)

        if embeddings is None:
            raise RuntimeError(
                encode_error or "Embedding generation returned no result"
            )

        if embeddings.shape != (len(rows), expected_dimension):
            raise RuntimeError(
                f"Shape mismatch: received {embeddings.shape}"
            )

        finite = bool(np.isfinite(embeddings).all())
        if not finite:
            raise RuntimeError("NaN or Infinity detected in embeddings")

        norms = np.linalg.norm(embeddings, axis=1)
        normalization_enabled = bool(
            model_config["normalize_embeddings"]
        )

        if normalization_enabled:
            max_norm_error = float(np.max(np.abs(norms - 1.0)))
            if max_norm_error > 0.001:
                raise RuntimeError(
                    f"Normalization error too large: {max_norm_error}"
                )
        else:
            max_norm_error = None

        check_count = min(
            int(benchmark["similarity_check_rows"]),
            len(rows),
        )
        check_vectors = embeddings[:check_count]
        similarity = check_vectors @ check_vectors.T
        diagonal = np.diag(similarity)

        elapsed = float(embedding_seconds)
        rows_per_second = len(rows) / elapsed
        source_tokens_per_second = source_tokens / elapsed
        model_tokens = sum(
            min(length, max_tokens) for length in tokenizer_lengths
        )
        model_tokens_per_second = model_tokens / elapsed

        estimated_full_seconds = 89511 / rows_per_second
        vector_bytes_per_row = expected_dimension * 4
        estimated_vector_bytes = vector_bytes_per_row * 89511

        peak_rss_kib = resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss

        result.update(
            {
                "status": "PASS",
                "completed_at": utc_now(),
                "model_name": model_config["model_name"],
                "requested_revision": requested_revision,
                "resolved_revision": resolved_revision,
                "license_reported_by_hub": (
                    hub_info.card_data.get("license")
                    if hub_info.card_data
                    else None
                ),
                "license_expected": model_config["license_expected"],
                "device": model_config["device"],
                "precision": model_config["precision"],
                "cpu_threads": torch.get_num_threads(),
                "rows": len(rows),
                "dimension": actual_dimension,
                "final_batch_size": batch_size,
                "model_load_seconds": model_load_seconds,
                "embedding_seconds": elapsed,
                "rows_per_second": rows_per_second,
                "source_tokens": source_tokens,
                "source_tokens_per_second": source_tokens_per_second,
                "model_tokens_after_truncation": model_tokens,
                "model_tokens_per_second": model_tokens_per_second,
                "inputs_exceeding_model_limit": too_long,
                "maximum_observed_model_tokens": max(tokenizer_lengths),
                "configured_max_input_tokens": max_tokens,
                "truncation_enabled": model_config["truncation"],
                "normalization_enabled": normalization_enabled,
                "minimum_vector_norm": float(norms.min()),
                "maximum_vector_norm": float(norms.max()),
                "maximum_normalization_error": max_norm_error,
                "finite_vectors": finite,
                "minimum_self_similarity": float(diagonal.min()),
                "maximum_self_similarity": float(diagonal.max()),
                "peak_worker_rss_mib": peak_rss_kib / 1024,
                "vector_bytes_per_row_float32": vector_bytes_per_row,
                "estimated_full_vector_storage_mib": (
                    estimated_vector_bytes / 1024**2
                ),
                "estimated_full_runtime_seconds": estimated_full_seconds,
                "estimated_full_runtime_hours": (
                    estimated_full_seconds / 3600
                ),
                "input_sha256": sha256_file(input_path),
                "query_prefix": model_config["query_prefix"],
                "document_prefix": model_config["document_prefix"],
                "supplied_mteb_retrieval_score": model_config[
                    "supplied_mteb_retrieval_score"
                ],
                "versions": {
                    "python": platform.python_version(),
                    "torch": torch.__version__,
                    "sentence_transformers": (
                        sentence_transformers.__version__
                    ),
                    "transformers": transformers.__version__,
                    "huggingface_hub": huggingface_hub.__version__,
                    "numpy": np.__version__,
                },
            }
        )

    except BaseException as exc:
        result.update(
            {
                "status": "FAIL",
                "completed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )

    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


def format_seconds(value: Any) -> str:
    if value is None:
        return "N/A"
    seconds = float(value)
    if seconds >= 3600:
        return f"{seconds / 3600:.2f} h"
    if seconds >= 60:
        return f"{seconds / 60:.2f} min"
    return f"{seconds:.2f} s"


def orchestrate() -> int:
    config = load_config()
    benchmark = config["benchmark"]
    input_path = Path(benchmark["input_csv"])
    rows = load_rows(input_path)
    validate_input(config, rows, input_path)

    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    output_root = Path(benchmark["output_directory"])
    output_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    for model in config["models"]:
        model_id = model["id"]
        model_dir = output_root / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        log_path = model_dir / "benchmark.log"

        print(f"\n{'=' * 70}")
        print(f"Starting model: {model_id}")
        print(f"{'=' * 70}", flush=True)

        env = os.environ.copy()
        env["TOKENIZERS_PARALLELISM"] = "false"
        env["OMP_NUM_THREADS"] = "2"
        env["MKL_NUM_THREADS"] = "2"

        started = time.perf_counter()

        with log_path.open("w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    model_id,
                ],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
            return_code = process.wait()

        wall_seconds = time.perf_counter() - started
        result_path = model_dir / "result.json"

        if result_path.exists():
            result = json.loads(
                result_path.read_text(encoding="utf-8")
            )
        else:
            result = {
                "model_id": model_id,
                "model_name": model["model_name"],
                "status": "FAIL",
                "error_type": "MissingResult",
                "error_message": (
                    "Worker ended without producing result.json; "
                    "possible OS out-of-memory termination"
                ),
            }

        result["worker_return_code"] = return_code
        result["orchestrator_wall_seconds"] = wall_seconds
        results.append(result)

        print(
            f"Completed {model_id}: {result['status']} | "
            f"wall={format_seconds(wall_seconds)}"
        )

    json_path = FINAL_DIR / "embedding_model_comparison.json"
    csv_path = FINAL_DIR / "embedding_model_comparison.csv"
    md_path = FINAL_DIR / "embedding_model_comparison.md"

    json_path.write_text(
        json.dumps(
            {
                "generated_at": utc_now(),
                "benchmark_version": benchmark["version"],
                "input_csv": str(input_path),
                "input_sha256": sha256_file(input_path),
                "results": results,
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )

    columns = [
        "model_id",
        "model_name",
        "status",
        "resolved_revision",
        "dimension",
        "final_batch_size",
        "model_load_seconds",
        "embedding_seconds",
        "rows_per_second",
        "source_tokens_per_second",
        "inputs_exceeding_model_limit",
        "maximum_observed_model_tokens",
        "peak_worker_rss_mib",
        "estimated_full_runtime_hours",
        "estimated_full_vector_storage_mib",
        "finite_vectors",
        "maximum_normalization_error",
        "error_type",
        "error_message",
    ]

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(results)

    passed = [result for result in results if result["status"] == "PASS"]
    failed = [result for result in results if result["status"] != "PASS"]

    ranking = sorted(
        passed,
        key=lambda result: result.get("rows_per_second", 0),
        reverse=True,
    )

    lines = [
        "# Embedding Model Engineering Benchmark",
        "",
        f"- Generated: `{utc_now()}`",
        f"- Input rows: `{len(rows)}`",
        f"- Input SHA-256: `{sha256_file(input_path)}`",
        f"- Models passed: `{len(passed)}/{len(results)}`",
        f"- Models failed: `{len(failed)}/{len(results)}`",
        "- Device: `CPU`",
        "- Similarity: `cosine` with normalized embeddings",
        "",
        "## Verification status",
        "",
        "| Check | Result |",
        "|---|---:|",
        f"| Input rows | {len(rows)} |",
        f"| Unique chunk IDs | {len({r['chunk_id'] for r in rows})} |",
        f"| Models configured | {len(results)} |",
        f"| Models passed | {len(passed)} |",
        f"| Models failed | {len(failed)} |",
        "",
        "## Engineering comparison",
        "",
        "| Model | Status | Dimension | Batch | Rows/s | "
        "Peak RAM MiB | Truncated inputs | Full runtime | "
        "Raw vectors MiB |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for result in results:
        lines.append(
            f"| {result.get('model_name', result['model_id'])} "
            f"| {result['status']} "
            f"| {result.get('dimension', 'N/A')} "
            f"| {result.get('final_batch_size', 'N/A')} "
            f"| {result.get('rows_per_second', 0):.2f} "
            f"| {result.get('peak_worker_rss_mib', 0):.1f} "
            f"| {result.get('inputs_exceeding_model_limit', 'N/A')} "
            f"| {format_seconds(result.get('estimated_full_runtime_seconds'))} "
            f"| {result.get('estimated_full_vector_storage_mib', 0):.1f} |"
        )

    lines.extend(
        [
            "",
            "## Throughput ranking",
            "",
        ]
    )

    if ranking:
        for position, result in enumerate(ranking, start=1):
            lines.append(
                f"{position}. `{result['model_name']}` — "
                f"{result['rows_per_second']:.2f} chunks/s, "
                f"{result['source_tokens_per_second']:.2f} "
                f"source tokens/s"
            )
    else:
        lines.append("No model completed successfully.")

    if failed:
        lines.extend(["", "## Failed models", ""])
        for result in failed:
            lines.append(
                f"- `{result['model_id']}`: "
                f"{result.get('error_type', 'Unknown')} — "
                f"{result.get('error_message', 'No error message')}"
            )

    lines.extend(
        [
            "",
            "## Interpretation constraint",
            "",
            "This is an engineering throughput and resource benchmark. "
            "It does not measure retrieval relevance. The production "
            "model must not be selected from speed or supplied MTEB "
            "scores alone. Final selection requires the approved query "
            "set, relevance judgments, and retrieval metrics including "
            "MRR@10, Recall@5, nDCG@10, and wrong-document-type rate.",
            "",
            "## Output files",
            "",
            f"- `{json_path}`",
            f"- `{csv_path}`",
            f"- `{md_path}`",
            "- Per-model logs and JSON results are under "
            f"`{output_root}`.",
        ]
    )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest_path = FINAL_DIR / "SHA256SUMS"
    manifest_paths = [json_path, csv_path, md_path]

    with manifest_path.open("w", encoding="utf-8") as handle:
        for path in manifest_paths:
            handle.write(f"{sha256_file(path)}  {path}\n")

    print(f"\nFinal report: {md_path}")
    print(f"Models passed: {len(passed)}/{len(results)}")

    return 0 if len(passed) == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", metavar="MODEL_ID")
    args = parser.parse_args()

    if args.worker:
        return worker(args.worker)
    return orchestrate()


if __name__ == "__main__":
    raise SystemExit(main())
