"""Check the chunking candidate against every ESG section without writing chunks."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import _bootstrap  # noqa: E402,F401
import config  # noqa: E402

from esg_chunker_candidate import (  # noqa: E402
    BGE_INPUT_LIMIT,
    chunk_section_candidate,
    final_bge_token_count,
    validate_candidate_tiling,
)

_BGE = None
_CL100K = None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def metadata_for(
    section: dict[str, str],
    enriched: dict[str, str],
) -> dict[str, str]:
    metadata = dict(enriched)
    metadata.setdefault("company_name", section["ticker"])
    metadata.setdefault("ticker", section["ticker"])
    metadata.setdefault("doc_type", "sustainability")
    metadata.setdefault("report_year", "")
    metadata.setdefault("report_year_status", "")
    metadata.setdefault("report_year_span", "")
    metadata.setdefault("cik", "")
    metadata.setdefault("section_code", section["section_code"])
    metadata.setdefault("section_title_original", section.get("section_title", ""))
    return metadata


def init_worker(tokenizer_path: str) -> None:
    global _BGE, _CL100K
    from transformers import AutoTokenizer
    import tiktoken

    _BGE = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
    _BGE.model_max_length = 1_000_000
    _CL100K = tiktoken.get_encoding("cl100k_base")


def check_one(task: tuple[str, dict[str, str]]) -> dict[str, int | str]:
    path_raw, metadata = task
    path = Path(path_raw)
    if not path.exists():
        return {"missing": 1, "error": str(path)}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        safety_before = (
            metadata.get("include_in_esg_index", ""),
            metadata.get("rag_action", ""),
        )
        chunks = chunk_section_candidate(text, metadata, _BGE, _CL100K)
        failures = validate_candidate_tiling(text, chunks)
        recount_failures = sum(
            chunk.bge_tokens
            != final_bge_token_count(metadata, chunk.text, _BGE, chunk.table_context)
            for chunk in chunks
        )
        source_failures = sum(
            text[chunk.source_start : chunk.source_end] != chunk.text
            for chunk in chunks
        )
        safety_after = (
            metadata.get("include_in_esg_index", ""),
            metadata.get("rag_action", ""),
        )
        return {
            "sections": 1,
            "chunks": len(chunks),
            "table_contexts": sum(bool(chunk.table_context) for chunk in chunks),
            "max_bge_tokens": max((chunk.bge_tokens for chunk in chunks), default=0),
            "over_limit": sum(chunk.bge_tokens > BGE_INPUT_LIMIT for chunk in chunks),
            "tiling_failures": len(failures),
            "span_gaps": failures.count("gap"),
            "source_failures": source_failures,
            "safety_mutations": int(safety_before != safety_after),
            "recount_mismatches": recount_failures,
            "missing": 0,
            "error": "",
        }
    except Exception as exc:  # report all corpus failures in one run
        return {"missing": 0, "error": f"{path}: {type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    sections = read_csv(source_root / config.as_repo_relative(config.ESG_SECTIONS_INDEX_CSV))
    enriched = read_csv(source_root / config.as_repo_relative(config.ESG_CHUNKS_INDEX_ENRICHED_CSV))
    enriched_by_key = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"]): row
        for row in enriched
    }
    tasks = []
    for section in sections:
        key = (section["ticker"], section["pdf_stem"], section["section_instance_id"])
        tasks.append(
            (
                str(source_root / section["section_file"]),
                metadata_for(section, enriched_by_key.get(key, {})),
            )
        )

    totals = {
        "sections": 0,
        "chunks": 0,
        "table_contexts": 0,
        "max_bge_tokens": 0,
        "over_limit": 0,
        "tiling_failures": 0,
        "span_gaps": 0,
        "source_failures": 0,
        "safety_mutations": 0,
        "recount_mismatches": 0,
        "missing": 0,
        "worker_errors": 0,
    }
    errors: list[str] = []
    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=init_worker,
        initargs=(str(args.tokenizer.resolve()),),
    ) as pool:
        for result in pool.map(check_one, tasks, chunksize=8):
            for key in totals:
                if key == "worker_errors":
                    continue
                if key == "max_bge_tokens":
                    totals[key] = max(totals[key], int(result.get(key, 0)))
                else:
                    totals[key] += int(result.get(key, 0))
            if result.get("error"):
                totals["worker_errors"] += 1
                errors.append(str(result["error"]))

    report = {
        "target_bge_tokens": BGE_INPUT_LIMIT,
        "workers": args.workers,
        **totals,
        "errors": errors[:20],
    }
    output = json.dumps(report, indent=2)
    print(output)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8")
    bad_keys = (
        "over_limit",
        "tiling_failures",
        "span_gaps",
        "source_failures",
        "safety_mutations",
        "recount_mismatches",
        "missing",
        "worker_errors",
    )
    return int(any(totals[key] for key in bad_keys))


if __name__ == "__main__":
    raise SystemExit(main())
