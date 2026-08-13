#!/usr/bin/env python3
"""Compare pinned CPU and private-GPU query embeddings without DB writes."""
from __future__ import annotations

import argparse
import json
import numpy as np

from sentence_transformers import SentenceTransformer
from src.remote_embedder import DIMENSION, MODEL_ID, REVISION, RemoteEmbedder

PREFIX = "Represent this sentence for searching relevant passages: "


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    text = PREFIX + args.question.strip()
    local = SentenceTransformer(
        MODEL_ID, revision=REVISION, trust_remote_code=False,
        device="cpu", local_files_only=True,
    ).encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    remote = RemoteEmbedder.from_env().encode(
        [text], normalize_embeddings=True, convert_to_numpy=True,
        show_progress_bar=False,
    )[0]
    cosine = float(np.dot(local, remote))
    report = {
        "model_id": MODEL_ID, "revision": REVISION,
        "dimension": DIMENSION, "cosine_similarity": cosine,
        "max_absolute_difference": float(np.max(np.abs(local - remote))),
        "database_writes": False,
        "numerically_equivalent": cosine >= 0.99999,
    }
    with open(args.output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["numerically_equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
