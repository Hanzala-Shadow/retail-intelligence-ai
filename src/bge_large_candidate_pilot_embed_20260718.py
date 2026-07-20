#!/usr/bin/env python3

import csv
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

RUN_DIR = Path(
    "reports/week3_retrieval/"
    "retrieval_benchmark_20260716T164940Z"
)
EVALUATOR_PATH = Path(
    "src/retrieval_embedding_benchmark.py"
)
CONFIG_PATH = (
    RUN_DIR / "inputs/retrieval_embedding_config.json"
)
PILOT_DIR = (
    RUN_DIR / "bge_large_candidate_pilot_20260718"
)
MANIFEST_PATH = (
    PILOT_DIR / "authorized_unique_chunks.csv"
)
SUMMARY_PATH = (
    PILOT_DIR / "candidate_manifest_summary.json"
)

MODEL_SLUG = "bge_large_en_v15"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)
    return digest.hexdigest()


def load_evaluator(path):
    spec = importlib.util.spec_from_file_location(
        "targeted_embedding_evaluator",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    summary = json.loads(
        SUMMARY_PATH.read_text(encoding="utf-8")
    )

    expected_hash = summary["files"][
        MANIFEST_PATH.name
    ]
    actual_hash = sha256(MANIFEST_PATH)

    if actual_hash != expected_hash:
        raise RuntimeError(
            "Candidate manifest checksum mismatch"
        )

    manifest_hashes = {}

    with MANIFEST_PATH.open(
        encoding="utf-8",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            chunk_id = int(row["chunk_id"])

            if chunk_id in manifest_hashes:
                raise RuntimeError(
                    f"Duplicate manifest chunk: {chunk_id}"
                )

            manifest_hashes[chunk_id] = (
                row["embedding_text_sha256"]
            )

    if len(manifest_hashes) != int(
        summary["unique_candidate_chunks"]
    ):
        raise RuntimeError(
            "Manifest row count does not match summary"
        )

    module = load_evaluator(EVALUATOR_PATH)
    config = module.load_json(CONFIG_PATH)
    module.validate_config(config)

    model_config = config["models"][MODEL_SLUG]
    table = module.safe_ident(model_config["table"])
    snapshot = module.safe_ident(
        config["snapshot"]["table"].split(".")[-1]
    )

    conn = module.connect()

    try:
        module.create_schema(
            conn,
            config,
            MODEL_SLUG,
        )

        candidate_ids = sorted(manifest_hashes)

        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    s.chunk_id,
                    s.embedding_text,
                    encode(
                        sha256(
                            convert_to(
                                s.embedding_text,
                                'UTF8'
                            )
                        ),
                        'hex'
                    ) AS text_hash,
                    e.chunk_id IS NOT NULL AS exists
                FROM public.{snapshot} s
                LEFT JOIN public.{table} e
                  USING (chunk_id)
                WHERE s.chunk_id = ANY(%s)
                ORDER BY s.chunk_id
                """,
                (candidate_ids,),
            )
            database_rows = cursor.fetchall()

        if len(database_rows) != len(candidate_ids):
            found = {
                int(row[0])
                for row in database_rows
            }
            missing_snapshot = sorted(
                set(candidate_ids) - found
            )
            raise RuntimeError(
                "Authorized chunks missing from snapshot: "
                f"{missing_snapshot}"
            )

        pending = []

        for (
            chunk_id,
            embedding_text,
            text_hash,
            exists,
        ) in database_rows:
            chunk_id = int(chunk_id)

            if text_hash != manifest_hashes[chunk_id]:
                raise RuntimeError(
                    f"Text-hash mismatch: {chunk_id}"
                )

            if not exists:
                pending.append(
                    (
                        chunk_id,
                        embedding_text,
                        text_hash,
                    )
                )

        print(
            json.dumps(
                {
                    "authorized_unique_chunks":
                        len(candidate_ids),
                    "already_embedded":
                        len(candidate_ids) - len(pending),
                    "pending_embeddings":
                        len(pending),
                    "manifest_sha256":
                        actual_hash,
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )

        if not pending:
            print(
                "PASS: all authorized candidates "
                "are already embedded",
                flush=True,
            )
            return

        model = module.load_model(model_config)
        batch_size = int(model_config["batch_size"])
        started = time.monotonic()
        done = 0

        for offset in range(
            0,
            len(pending),
            batch_size,
        ):
            batch = pending[
                offset:offset + batch_size
            ]

            texts = [
                model_config["document_prefix"] + row[1]
                for row in batch
            ]

            vectors = model.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

            module.insert_batch(
                conn,
                config,
                MODEL_SLUG,
                batch,
                vectors,
            )

            done += len(batch)

            if (
                done == len(batch)
                or done % 100 == 0
                or done == len(pending)
            ):
                print(
                    f"[{MODEL_SLUG}] "
                    f"embedded {done}/{len(pending)}",
                    flush=True,
                )

        print(
            json.dumps(
                {
                    "model": MODEL_SLUG,
                    "new_rows": done,
                    "seconds":
                        time.monotonic() - started,
                    "finished_at": module.utcnow(),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
