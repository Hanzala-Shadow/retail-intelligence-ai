#!/usr/bin/env python3
"""Run the frozen-24 question-only detector coverage audit without models."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.decomposed_query_api import _aliases_from_connection  # noqa: E402
from src.detector_coverage_audit import run_audit  # noqa: E402
from src.query_api import _connect  # noqa: E402
from src.query_decomposition import SourceResolver  # noqa: E402

DEFAULT_QUESTIONS = Path("data/00_reference/rag_eval_questions.csv")
DETECTOR_PATH = Path("src/query_decomposition.py")
AUDIT_PATH = Path("src/detector_coverage_audit.py")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_supported(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if row["question_group"] != "refusal"]


def _corpus_fingerprint(
    resolver: SourceResolver, aliases: dict[str, str]
) -> str:
    metadata = {
        "filings": [
            {
                "ticker": record.ticker,
                "filing_year": record.filing_year,
                "doc_type": record.doc_type,
                "accession_number": record.accession_number,
            }
            for record in resolver.records
        ],
        "aliases": [
            {"alias": alias, "ticker": ticker}
            for alias, ticker in sorted(aliases.items())
        ],
    }
    return _canonical_sha256(metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite: {args.output}")

    rows = _load_supported(args.questions)
    conn = _connect()
    try:
        conn.set_session(readonly=True, autocommit=True)
        resolver = SourceResolver.from_connection(conn)
        known_tickers, aliases = _aliases_from_connection(conn)
    finally:
        conn.close()

    result = run_audit(
        rows,
        known_tickers,
        aliases,
        resolver,
        questions_sha256=_sha256(args.questions),
        detector_code_sha256=_sha256(REPO_ROOT / DETECTOR_PATH),
        audit_code_sha256=_sha256(REPO_ROOT / AUDIT_PATH),
        corpus_metadata_sha256=_corpus_fingerprint(resolver, aliases),
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            result,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    print(
        json.dumps(
            {
                "mode": result["mode"],
                "no_model": result["no_model"],
                "output": str(args.output),
                "summary": result["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
