"""Build a small, isolated ESG chunking candidate and comparison report.

The source root is read-only.  Candidate text, an index, and the report are
written only under the selected output directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

from esg_chunker_candidate import (  # noqa: E402
    BGE_INPUT_LIMIT,
    BGE_MODEL_LIMIT,
    CandidateChunk,
    chunk_section_candidate,
    final_bge_token_count,
    validate_candidate_tiling,
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metric_shape(text: str) -> tuple[int, float, float, int]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    digit_ratio = sum(char.isdigit() for char in text) / max(len(text), 1)
    short_ratio = sum(len(line.split()) <= 4 for line in lines) / max(len(lines), 1)
    dot_leaders = len(re.findall(r"\.{6,}", text))
    return len(text), digit_ratio, short_ratio, dot_leaders


def is_table_header(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    words = value.split()
    digits = sum(char.isdigit() for char in value)
    return len(words) <= 12 and ("|" in value or digits >= 4 or "202" in value)


def first_table_header(text: str) -> str:
    for line in text.splitlines():
        if is_table_header(line):
            return line.strip()
    return ""


def terminal_punctuation(text: str) -> bool:
    value = text.rstrip()
    while value and value[-1] in "\"')]}\u201d\u2019":
        value = value[:-1]
    return bool(value) and value[-1] in ".!?"


def starts_lower(text: str) -> bool:
    value = text.lstrip()
    return bool(value) and value[0].islower()


def page_span(page_rows: list[dict[str, str]], start: int, end: int) -> tuple[str, str]:
    pages: list[int] = []
    for row in page_rows:
        try:
            page_start = int(row.get("char_start") or 0)
            page_end = int(row.get("char_end") or 0)
            page = int(row.get("page") or 0)
        except ValueError:
            continue
        if page_end > start and page_start < end and page:
            pages.append(page)
    return (str(min(pages)), str(max(pages))) if pages else ("", "")


def choose_samples(section_rows: list[dict[str, str]], root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for row in section_rows:
        path = root / row["section_file"]
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chars, digit_ratio, short_ratio, dot_leaders = metric_shape(text)
        records.append({**row, "_text": text, "_chars": str(chars), "_digit": str(digit_ratio), "_short": str(short_ratio), "_dots": str(dot_leaders)})

    selected: list[dict[str, str]] = []
    used: set[tuple[str, str, str]] = set()

    def add(label: str, candidates: list[dict[str, str]]) -> None:
        for record in candidates:
            key = (record["ticker"], record["pdf_stem"], record["section_instance_id"])
            if key not in used:
                record = dict(record)
                record["sample_label"] = label
                selected.append(record)
                used.add(key)
                return

    add("normal_prose", sorted(records, key=lambda r: (r["section_code"] != "environmental", -int(r["_chars"]))))
    add("table_rows", sorted(records, key=lambda r: (int(r["_chars"]) < 1500, -(float(r["_digit"]) * float(r["_short"])), -int(r["_chars"]))))
    add("navigation_or_contents", sorted(records, key=lambda r: (-int(r["_dots"]), -int(r["_chars"]))))
    add("short_evidence", sorted(records, key=lambda r: (int(r["_chars"]), r["ticker"])))
    add("heading_and_page_span", sorted(records, key=lambda r: (int(r.get("page_start") or 0) == int(r.get("page_end") or 0), -int(r["_chars"]))))
    add("long_prefix", sorted(records, key=lambda r: (-len(r.get("section_title") or ""), -int(r["_chars"]))))
    return selected


def metadata_for(record: dict[str, str], enriched_by_key: dict[tuple[str, str, str], dict[str, str]]) -> dict[str, str]:
    key = (record["ticker"], record["pdf_stem"], record["section_instance_id"])
    row = dict(enriched_by_key.get(key, {}))
    row.setdefault("company_name", record["ticker"])
    row.setdefault("ticker", record["ticker"])
    row.setdefault("doc_type", "sustainability")
    row.setdefault("report_year", "")
    row.setdefault("report_year_status", "")
    row.setdefault("report_year_span", "")
    row.setdefault("cik", "")
    row.setdefault("section_code", record["section_code"])
    row.setdefault("section_title_original", record.get("section_title", ""))
    return row


def build_sample(
    record: dict[str, str],
    metadata: dict[str, str],
    root: Path,
    out_root: Path,
    bge,
    cl100k,
    page_rows: list[dict[str, str]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    text = record["_text"]
    try:
        chunks = chunk_section_candidate(text, metadata, bge, cl100k)
    except ValueError as exc:
        raise ValueError(
            f"{record['sample_label']} {record['ticker']} {record['section_instance_id']}: {exc}"
        ) from exc
    sample_key = f"{record['ticker']}__{record['pdf_stem']}__{record['section_instance_id']}"
    output_rows: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        output = out_root / "chunks" / record["ticker"] / f"{safe_name(sample_key)}__chunk_{index:04d}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(chunk.text, encoding="utf-8", newline="")
        source_start = int(record.get("source_start_char") or 0) + chunk.source_start
        source_end = int(record.get("source_start_char") or 0) + chunk.source_end
        page_start, page_end = page_span(page_rows, source_start, source_end)
        output_rows.append({
            "sample_label": record["sample_label"],
            "ticker": record["ticker"],
            "pdf_stem": record["pdf_stem"],
            "section_instance_id": record["section_instance_id"],
            "chunk_index": index,
            "chunk_file": output.relative_to(out_root).as_posix(),
            "chunk_text_sha256": sha256_text(chunk.text),
            "source_start_char": source_start,
            "source_end_char": source_end,
            "page_start": page_start,
            "page_end": page_end,
            "bge_tokens": chunk.bge_tokens,
            "cl100k_tokens": chunk.cl100k_tokens,
            "table_context": chunk.table_context,
            "source_exact": text[chunk.source_start : chunk.source_end] == chunk.text,
        })

    seam_starts = sum(starts_lower(chunks[i].text) for i in range(1, len(chunks)))
    seam_ends = sum(not terminal_punctuation(chunks[i - 1].text) for i in range(1, len(chunks)))
    overlap_chars = sum(max(0, chunks[i - 1].source_end - chunks[i].source_start) for i in range(1, len(chunks)))
    table_header = first_table_header(text)
    table_header_preserved = not table_header or any(table_header in chunk.text for chunk in chunks)
    failures = validate_candidate_tiling(text, chunks)
    summary = {
        "sample_label": record["sample_label"],
        "ticker": record["ticker"],
        "pdf_stem": record["pdf_stem"],
        "section_instance_id": record["section_instance_id"],
        "current_chunk_count": 0,
        "candidate_chunk_count": len(chunks),
        "candidate_max_bge_tokens": max((chunk.bge_tokens for chunk in chunks), default=0),
        "candidate_starts_lowercase": seam_starts,
        "candidate_ends_without_terminal_punctuation": seam_ends,
        "candidate_overlap_chars": overlap_chars,
        "table_header": table_header,
        "table_header_preserved": table_header_preserved,
        "source_invariant_failures": "|".join(failures),
        "safety_include_in_esg_index": metadata.get("include_in_esg_index", ""),
        "safety_rag_action": metadata.get("rag_action", ""),
        "section_page_start": record.get("page_start", ""),
        "section_page_end": record.get("page_end", ""),
    }
    return summary, output_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "esg_chunking_candidate")
    parser.add_argument("--tokenizer", type=Path, required=True)
    args = parser.parse_args(argv)

    from transformers import AutoTokenizer
    import tiktoken

    source_root = args.source_root.resolve()
    out_root = args.out.resolve()
    bge = AutoTokenizer.from_pretrained(str(args.tokenizer), local_files_only=True)
    cl100k = tiktoken.get_encoding("cl100k_base")

    sections = read_csv(source_root / "data/00_reference/esg_sections_index.csv")
    enriched = read_csv(source_root / "data/00_reference/esg_chunks_index_enriched.csv")
    count_path = source_root / "tmp/esg_task8_20260730/esg_bge_token_counts_prefixed.csv"
    counts = read_csv(count_path) if count_path.exists() else []
    bge_count_by_id = {row["chunk_id"]: row for row in counts}
    enriched_by_key = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"]): row
        for row in enriched
    }
    current_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in enriched:
        current_by_key[(row["ticker"], row["pdf_stem"], row["section_instance_id"])].append(row)

    parse_rows = read_csv(source_root / "data/00_reference/esg_parse_index.csv")
    page_map_by_doc: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in parse_rows:
        page_file = row.get("page_map_file") or ""
        if page_file and (source_root / page_file).exists():
            page_map_by_doc[(row.get("ticker", ""), Path(row.get("source_pdf") or row.get("pdf_file") or "").stem)] = read_csv(source_root / page_file)

    selected = choose_samples(sections, source_root)
    if not selected:
        raise SystemExit("no source sections selected")
    out_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, object]] = []
    index_rows: list[dict[str, object]] = []
    for record in selected:
        key = (record["ticker"], record["pdf_stem"], record["section_instance_id"])
        metadata = metadata_for(record, enriched_by_key)
        page_rows = page_map_by_doc.get((record["ticker"], record["pdf_stem"]), [])
        summary, chunk_rows = build_sample(record, metadata, source_root, out_root, bge, cl100k, page_rows)
        live_rows = current_by_key.get(key, [])
        summary["current_chunk_count"] = len(live_rows)
        summary["current_max_prefixed_bge_tokens"] = max((int(bge_count_by_id.get(row["chunk_id"], {}).get("bge_embedding_disk") or 0) for row in live_rows), default=0)
        summary["current_prefixed_over_512"] = sum(int(bge_count_by_id.get(row["chunk_id"], {}).get("bge_embedding_disk") or 0) > BGE_MODEL_LIMIT for row in live_rows)
        summaries.append(summary)
        index_rows.extend(chunk_rows)

    fields = list(index_rows[0]) if index_rows else []
    with (out_root / "candidate_index.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(index_rows)

    candidate_tokens = [int(summary["candidate_max_bge_tokens"]) for summary in summaries]
    report = [
        "# ESG chunking candidate comparison",
        "",
        "This is an isolated candidate. Source files, live chunks, audits, manifests, embeddings, and the vector index were read only.",
        "",
        f"- Samples: {len(summaries)}",
        f"- Candidate chunks: {sum(int(s['candidate_chunk_count']) for s in summaries)}",
        f"- Candidate maximum final prefixed BGE tokens: {max(candidate_tokens, default=0)}",
        f"- Candidate target: {BGE_INPUT_LIMIT} BGE tokens (model limit: {BGE_MODEL_LIMIT})",
        f"- Samples above candidate target: {sum(int(s['candidate_max_bge_tokens']) > BGE_INPUT_LIMIT for s in summaries)}",
        f"- Source invariant failures: {sum(bool(s['source_invariant_failures']) for s in summaries)}",
        "",
        "| Sample | Current chunks | Candidate chunks | Current max BGE | Candidate max BGE | Current >512 | Lowercase starts | Non-terminal ends | Overlap chars | Table header | Pages | Safety |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for summary in summaries:
        label = f"{summary['ticker']} {summary['section_instance_id']} ({summary['sample_label']})"
        pages = f"{summary['section_page_start']}-{summary['section_page_end']}"
        safety = f"{summary['safety_include_in_esg_index']}/{summary['safety_rag_action']}"
        report.append(
            f"| {label} | {summary['current_chunk_count']} | {summary['candidate_chunk_count']} | {summary['current_max_prefixed_bge_tokens']} | {summary['candidate_max_bge_tokens']} | {summary['current_prefixed_over_512']} | {summary['candidate_starts_lowercase']} | {summary['candidate_ends_without_terminal_punctuation']} | {summary['candidate_overlap_chars']} | {'yes' if summary['table_header_preserved'] else 'NO'} | {pages} | {safety} |"
        )
    report += [
        "",
        "## Acceptance checks",
        "",
        "- Candidate BGE counts use the exact production prefix and include special tokens.",
        "- Candidate chunks are exact source slices with source offsets and page spans in `candidate_index.csv`.",
        "- Sentence and table-row boundaries are preferred; whitespace is used only for an oversized unit.",
        "- Overlap is limited to one preceding safe unit and 48 BGE tokens.",
        "- Safety fields are copied from the live metadata and are not changed by the splitter.",
        "- Promotion was not run.",
        "",
    ]
    (out_root / "comparison.md").write_text("\n".join(report), encoding="utf-8", newline="")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
