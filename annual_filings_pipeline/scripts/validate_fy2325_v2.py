#!/usr/bin/env python3
"""Compact integrity validator for v2 parser, section, and chunk outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def show_progress(
    stage: str,
    done: int,
    total: int,
    started: float,
    every: int,
) -> None:
    if done != total and done % max(1, every):
        return
    elapsed = max(0.001, time.monotonic() - started)
    rate = done / elapsed
    eta = (total - done) / rate if rate else 0
    percent = 100.0 * done / total if total else 100.0
    print(
        f"PROGRESS stage={stage} {done}/{total} ({percent:.1f}%) "
        f"elapsed={time.strftime('%H:%M:%S', time.gmtime(elapsed))} "
        f"eta={time.strftime('%H:%M:%S', time.gmtime(eta))}",
        flush=True,
    )


def validate_parsed(root: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = read_jsonl(root / "parsed_documents.jsonl")
    failures = []
    started = time.monotonic()
    for index, row in enumerate(rows, start=1):
        path = root / "html_text" / (
            f"{row['ticker']}__10-K__{row['accession_number']}.txt"
        )
        if not path.is_file():
            failures.append(f"missing parsed text: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        if sha256_text(text) != row["text_sha256"]:
            failures.append(f"parsed hash mismatch: {path}")
        show_progress("parsed", index, len(rows), started, 25)
    if failures:
        raise RuntimeError("\n".join(failures[:20]))
    return rows, {
        "documents": len(rows),
        "status_counts": dict(Counter(row["parse_status"] for row in rows)),
        "replacement_character_documents": sum(
            "\ufffd"
            in (
                root
                / "html_text"
                / f"{row['ticker']}__10-K__{row['accession_number']}.txt"
            ).read_text(encoding="utf-8")
            for row in rows
        ),
    }


def validate_sections(
    root: Path,
    parsed_root: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = read_jsonl(root / "sections.jsonl")
    by_doc = defaultdict(set)
    failures = []
    source_cache: dict[str, str] = {}
    started = time.monotonic()
    for index, row in enumerate(rows, start=1):
        section_path = root / "10k" / Path(str(row["output_file"])).name
        source_path = parsed_root / "html_text" / (
            f"{row['ticker']}__10-K__{row['accession_number']}.txt"
        )
        section_text = section_path.read_text(encoding="utf-8")
        accession = str(row["accession_number"])
        source_text = source_cache.get(accession)
        if source_text is None:
            source_text = source_path.read_text(encoding="utf-8")
            source_cache[accession] = source_text
        start = int(row["source_start_char"])
        end = int(row["source_end_char"])
        if source_text[start:end] != section_text:
            failures.append(f"section offset mismatch: {row['section_id']}")
        if sha256_text(section_text) != row["section_text_sha256"]:
            failures.append(f"section hash mismatch: {row['section_id']}")
        by_doc[row["accession_number"]].add(row["canonical_section_code"])
        show_progress("sections", index, len(rows), started, 250)
    mandatory = {"Item_1", "Item_1A", "Item_7", "Item_8"}
    missing = {
        accession: sorted(mandatory - codes)
        for accession, codes in by_doc.items()
        if mandatory - codes
    }
    if failures:
        raise RuntimeError("\n".join(failures[:20]))
    return rows, {
        "sections": len(rows),
        "documents": len(by_doc),
        "missing_mandatory_documents": len(missing),
        "quality_status_counts": dict(
            Counter(row["quality_status"] for row in rows)
        ),
        "rag_action_counts": dict(Counter(row["rag_action"] for row in rows)),
    }


def validate_chunks(
    root: Path,
    sections_root: Path,
) -> dict[str, object]:
    import tiktoken
    from transformers import AutoTokenizer

    rows = read_jsonl(root / "chunks.jsonl")
    section_rows = {
        row["section_id"]: row
        for row in read_jsonl(sections_root / "sections.jsonl")
    }
    encoder = tiktoken.get_encoding("cl100k_base")
    embedding_tokenizer = AutoTokenizer.from_pretrained(
        "BAAI/bge-base-en-v1.5",
        revision="a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
        local_files_only=True,
    )
    embedding_tokenizer.model_max_length = 1_000_000
    failures = []
    chunk_ids = set()
    section_text_cache: dict[str, str] = {}
    started = time.monotonic()
    for index, row in enumerate(rows, start=1):
        if row["chunk_id"] in chunk_ids:
            failures.append(f"duplicate chunk id: {row['chunk_id']}")
        chunk_ids.add(row["chunk_id"])
        section = section_rows[row["section_id"]]
        section_path = (
            sections_root / "10k" / Path(str(section["output_file"])).name
        )
        section_id = str(row["section_id"])
        section_text = section_text_cache.get(section_id)
        if section_text is None:
            section_text = section_path.read_text(encoding="utf-8")
            section_text_cache[section_id] = section_text
        start = int(row["section_start_char"])
        end = int(row["section_end_char"])
        if section_text[start:end] != row["chunk_text"]:
            failures.append(f"chunk offset mismatch: {row['chunk_id']}")
        if sha256_text(row["chunk_text"]) != row["chunk_text_sha256"]:
            failures.append(f"chunk hash mismatch: {row['chunk_id']}")
        actual_tokens = len(
            encoder.encode(row["chunk_text"], disallowed_special=())
        )
        if actual_tokens != int(row["token_count"]):
            failures.append(f"chunk token mismatch: {row['chunk_id']}")
        actual_embedding_tokens = len(
            embedding_tokenizer.encode(
                row["embedding_text"],
                add_special_tokens=True,
                truncation=False,
            )
        )
        if actual_embedding_tokens != int(row["embedding_token_count"]):
            failures.append(
                f"embedding token mismatch: {row['chunk_id']}"
            )
        if (
            row["rag_action"] == "include"
            and actual_embedding_tokens > 512
        ):
            failures.append(
                f"embedding maximum exceeded: {row['chunk_id']}"
            )
        human_section = str(row["rag_section_code"]).replace("_", " ")

        required_metadata = (
            "Company: ",
            f"Ticker: {row['ticker']}\n",
            "Document: Form 10-K\n",
            f"Fiscal year: FY{row['coverage_year']}\n",
            f"SEC section: {human_section}\n",
            f"Subsection: "
            f"{row['subsection_heading'] or 'Not specified'}\n",
            f"Content type: {row['chunk_type']}\n",
        )

        if (
            row["rag_section_code"]
            != row["canonical_section_code"]
        ):
            required_metadata += (
                "Source SEC container: "
                f"{str(row['canonical_section_code']).replace('_', ' ')}\n",
            )

        if (
            bool(row["continuation_from_previous"])
            != ("\nContinuation context: " in row["embedding_text"])
        ):
            failures.append(
                f"continuation metadata mismatch: {row['chunk_id']}"
            )

        if (
            bool(row["continues_to_next"])
            != (
                "Forward continuation context: "
                in row["embedding_text"]
            )
        ):
            failures.append(
                f"forward continuation metadata mismatch: "
                f"{row['chunk_id']}"
            )

        for metadata in required_metadata:
            if metadata not in row["embedding_text"]:
                failures.append(
                    f"embedding metadata missing "
                    f"{metadata!r}: {row['chunk_id']}"
                )

        if not row["embedding_text"].endswith(row["chunk_text"]):
            failures.append(
                f"embedding content mismatch: {row['chunk_id']}"
            )

        if (
            sha256_text(row["embedding_text"])
            != row["embedding_text_sha256"]
        ):
            failures.append(
                f"embedding hash mismatch: {row['chunk_id']}"
            )

        if (
            row["rag_action"] == "include"
            and (
                "[TABLE_START:" in row["chunk_text"]
                or "[TABLE_END:" in row["chunk_text"]
                or "[TABLE_START:" in row["embedding_text"]
                or "[TABLE_END:" in row["embedding_text"]
            )
        ):
            failures.append(
                f"RAG table marker leakage: {row['chunk_id']}"
            )
        show_progress("chunks", index, len(rows), started, 1000)
    if failures:
        raise RuntimeError("\n".join(failures[:20]))
    return {
        "chunks": len(rows),
        "unique_chunk_ids": len(chunk_ids),
        "rag_action_counts": dict(Counter(row["rag_action"] for row in rows)),
        "quality_status_counts": dict(
            Counter(row["quality_status"] for row in rows)
        ),
        "minimum_tokens": min(int(row["token_count"]) for row in rows),
        "maximum_tokens": max(int(row["token_count"]) for row in rows),
        "maximum_embedding_tokens": max(
            int(row["embedding_token_count"])
            for row in rows
            if row["rag_action"] == "include"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parsed-root", type=Path, required=True)
    parser.add_argument("--sections-root", type=Path)
    parser.add_argument("--chunks-root", type=Path)
    args = parser.parse_args()
    _, parsed_summary = validate_parsed(args.parsed_root)
    report = {"parsed": parsed_summary}
    if args.sections_root:
        _, section_summary = validate_sections(
            args.sections_root, args.parsed_root
        )
        report["sections"] = section_summary
    if args.chunks_root:
        if not args.sections_root:
            raise ValueError("--chunks-root requires --sections-root")
        report["chunks"] = validate_chunks(
            args.chunks_root, args.sections_root
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
