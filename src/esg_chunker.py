from __future__ import annotations

import argparse
import csv
from pathlib import Path

import tiktoken


ENCODING = "cl100k_base"
CHUNK_SIZE = 500
OVERLAP = 50
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
DOC_TYPE = "sustainability"

CHUNKS_INDEX_FIELDS = [
    "chunk_id",
    "ticker",
    "doc_type",
    "doc_quality_status",
    "rag_action",
    "quality_flags",
    "pdf_stem",
    "section_code",
    "chunk_index",
    "token_count",
    "char_count",
    "chunk_file",
    "source_section_file",
    "source_start_char",
    "source_end_char",
    "page_start",
    "page_end",
    "citation_ready",
]


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def row_quality_flags(row: dict) -> set[str]:
    raw_flags = (row.get("quality_flags") or "").strip()
    return {flag for flag in raw_flags.split("|") if flag}


def doc_quality_status(parse_row: dict) -> str:
    flags = row_quality_flags(parse_row)
    if parse_bool(parse_row.get("possible_wrong_doc_type")):
        return "exclude_from_esg_rag"
    if parse_row.get("status") != "parsed" or flags & {
        "garbled_text",
        "low_readable_word_ratio",
        "low_text_per_page",
    }:
        return "needs_review"
    return "ok"


def rag_action_for_status(status: str) -> str:
    if status == "exclude_from_esg_rag":
        return "exclude_from_esg_index"
    if status == "needs_review":
        return "manual_review_before_indexing"
    return "index_as_esg"


def doc_type_for_parse_row(parse_row: dict) -> str:
    if parse_bool(parse_row.get("possible_wrong_doc_type")):
        return "annual_report_with_esg"
    return DOC_TYPE


def load_doc_metadata(parse_index_path: Path) -> dict[tuple[str, str], dict]:
    metadata: dict[tuple[str, str], dict] = {}
    if not parse_index_path.exists():
        return metadata
    with parse_index_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip().upper()
            source_pdf = row.get("source_pdf") or row.get("pdf_file") or ""
            pdf_stem = Path(source_pdf).stem if source_pdf else Path(row.get("pdf_file") or "").stem
            if not ticker or not pdf_stem:
                continue
            quality_status = doc_quality_status(row)
            metadata[(ticker, pdf_stem)] = {
                "doc_type": doc_type_for_parse_row(row),
                "doc_quality_status": quality_status,
                "rag_action": rag_action_for_status(quality_status),
                "quality_flags": row.get("quality_flags") or "",
                "page_map_file": row.get("page_map_file") or "",
            }
    return metadata


def load_section_metadata(sections_index_path: Path) -> dict[tuple[str, str, str], dict]:
    metadata: dict[tuple[str, str, str], dict] = {}
    if not sections_index_path.exists():
        return metadata
    with sections_index_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip().upper()
            pdf_stem = (row.get("pdf_stem") or "").strip()
            section_code = (row.get("section_code") or "").strip()
            if ticker and pdf_stem and section_code:
                metadata[(ticker, pdf_stem, section_code)] = row
    return metadata


def read_page_map(page_map_file: str | None) -> list[dict]:
    path = resolve_path(page_map_file)
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pages_for_span(page_spans: list[dict], start: int | None, end: int | None) -> tuple[str, str]:
    if start is None or end is None or not page_spans:
        return "", ""

    pages: list[int] = []
    for row in page_spans:
        page_start = parse_int(row.get("char_start"))
        page_end = parse_int(row.get("char_end"))
        page_number = parse_int(row.get("page"))
        if page_start is None or page_end is None or page_number is None:
            continue
        if page_end <= start or page_start >= end:
            continue
        pages.append(page_number)

    if not pages:
        return "", ""
    return str(min(pages)), str(max(pages))


def locate_text_span(source_text: str, needle: str, start_hint: int = 0) -> tuple[int | None, int | None]:
    text = needle.strip()
    if not text:
        return None, None
    exact_start = source_text.find(text, start_hint)
    if exact_start < 0:
        exact_start = source_text.find(text)
    if exact_start >= 0:
        return exact_start, exact_start + len(text)

    snippet = text[:240].strip()
    if len(snippet) < 20:
        return None, None
    start = source_text.find(snippet, start_hint)
    if start < 0:
        start = source_text.find(snippet)
    if start < 0:
        return locate_text_span_normalized(source_text, text)
    return start, start + len(snippet)


def normalize_with_source_map(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    source_positions: list[int] = []
    previous_space = False

    for index, char in enumerate(text):
        if char.isspace():
            if not previous_space:
                normalized_chars.append(" ")
                source_positions.append(index)
                previous_space = True
            continue
        normalized_chars.append(char)
        source_positions.append(index)
        previous_space = False

    return "".join(normalized_chars).strip(), source_positions


def locate_text_span_normalized(source_text: str, needle: str) -> tuple[int | None, int | None]:
    normalized_source, source_map = normalize_with_source_map(source_text)
    normalized_needle, _ = normalize_with_source_map(needle)
    if not normalized_source or not normalized_needle:
        return None, None

    normalized_start = normalized_source.find(normalized_needle)
    if normalized_start < 0:
        return None, None

    normalized_end = normalized_start + len(normalized_needle) - 1
    if normalized_start >= len(source_map) or normalized_end >= len(source_map):
        return None, None
    return source_map[normalized_start], source_map[normalized_end] + 1


def chunk_tokens(tokens: list[int]) -> list[list[int]]:
    if len(tokens) < MIN_CHUNK_TOKENS:
        return []

    if len(tokens) <= MAX_CHUNK_TOKENS:
        return [tokens]

    chunks: list[list[int]] = []
    start = 0
    total = len(tokens)

    while start < total:
        end = min(start + CHUNK_SIZE, total)
        remaining = total - end

        if 0 < remaining < MIN_CHUNK_TOKENS and total - start <= MAX_CHUNK_TOKENS:
            end = total

        chunk = tokens[start:end]
        if len(chunk) >= MIN_CHUNK_TOKENS:
            chunks.append(chunk)

        if end >= total:
            break

        start = max(end - OVERLAP, start + 1)

    return chunks


def parse_section_filename(section_file: Path) -> tuple[str, str] | None:
    if "__" not in section_file.stem:
        return None
    pdf_stem, section_code = section_file.stem.rsplit("__", 1)
    if not pdf_stem or not section_code:
        return None
    return pdf_stem, section_code


def clear_existing_chunks(ticker_out: Path, pdf_stem: str, section_code: str) -> None:
    if not ticker_out.exists():
        return
    prefix = f"{pdf_stem}__{section_code}__chunk_"
    for stale_file in ticker_out.glob(f"{prefix}*.txt"):
        stale_file.unlink()


def clear_ticker_chunks(output_root: Path, ticker: str) -> None:
    ticker_out = output_root / ticker
    if not ticker_out.exists():
        return
    for stale_file in ticker_out.glob("*.txt"):
        stale_file.unlink()


def process_section_file(
    section_file: Path,
    output_root: Path,
    encoder,
    section_metadata: dict[tuple[str, str, str], dict],
    doc_metadata: dict[tuple[str, str], dict],
) -> tuple[list[dict], str | None]:
    ticker = section_file.parent.name.upper()
    parsed_name = parse_section_filename(section_file)
    if parsed_name is None:
        return [], f"bad filename: {section_file}"

    pdf_stem, section_code = parsed_name
    text = section_file.read_text(encoding="utf-8", errors="replace").strip()
    tokens = encoder.encode(text)
    section_meta = section_metadata.get((ticker, pdf_stem, section_code), {})
    doc_meta = doc_metadata.get((ticker, pdf_stem), {})
    page_spans = read_page_map(doc_meta.get("page_map_file"))
    section_source_start = parse_int(section_meta.get("source_start_char"))

    if len(tokens) < MIN_CHUNK_TOKENS:
        return [], f"skipped short section ({len(tokens)} tokens): {section_file}"

    ticker_out = output_root / ticker
    ticker_out.mkdir(parents=True, exist_ok=True)
    clear_existing_chunks(ticker_out, pdf_stem, section_code)

    rows: list[dict] = []
    search_pos = 0
    for index, chunk_token_ids in enumerate(chunk_tokens(tokens)):
        token_count = len(chunk_token_ids)
        if token_count < MIN_CHUNK_TOKENS or token_count > MAX_CHUNK_TOKENS:
            return rows, f"invalid chunk size ({token_count} tokens): {section_file}"

        chunk_text = encoder.decode(chunk_token_ids).strip()
        local_start, local_end = locate_text_span(text, chunk_text, search_pos)
        if local_start is not None and local_end is not None:
            search_pos = max(search_pos, local_start + 1)

        source_start = (
            section_source_start + local_start
            if section_source_start is not None and local_start is not None
            else None
        )
        source_end = (
            section_source_start + local_end
            if section_source_start is not None and local_end is not None
            else None
        )
        page_start, page_end = pages_for_span(page_spans, source_start, source_end)
        citation_ready = bool(page_start and page_end and source_start is not None and source_end is not None)

        doc_type = doc_meta.get("doc_type") or DOC_TYPE
        quality_status = doc_meta.get("doc_quality_status") or "needs_review"
        rag_action = doc_meta.get("rag_action") or rag_action_for_status(quality_status)
        chunk_id = f"{ticker}__{doc_type}__{pdf_stem}__{section_code}__chunk_{index:04d}"
        chunk_file = ticker_out / f"{pdf_stem}__{section_code}__chunk_{index:04d}.txt"
        chunk_file.write_text(chunk_text, encoding="utf-8")

        rows.append(
            {
                "chunk_id": chunk_id,
                "ticker": ticker,
                "doc_type": doc_type,
                "doc_quality_status": quality_status,
                "rag_action": rag_action,
                "quality_flags": doc_meta.get("quality_flags") or "",
                "pdf_stem": pdf_stem,
                "section_code": section_code,
                "chunk_index": index,
                "token_count": token_count,
                "char_count": len(chunk_text),
                "chunk_file": display_path(chunk_file),
                "source_section_file": display_path(section_file),
                "source_start_char": source_start if source_start is not None else "",
                "source_end_char": source_end if source_end is not None else "",
                "page_start": page_start,
                "page_end": page_end,
                "citation_ready": "true" if citation_ready else "false",
            }
        )

    return rows, None


def discover_section_files(input_root: Path, ticker: str | None = None) -> list[Path]:
    if not input_root.exists():
        return []
    if ticker:
        return sorted((input_root / ticker.upper()).glob("*.txt"))
    return sorted(input_root.glob("*/*.txt"))


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [{field: row.get(field, "") for field in CHUNKS_INDEX_FIELDS} for row in reader]


def write_index(index_path: Path, rows: list[dict]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        rows,
        key=lambda r: (
            r.get("ticker", ""),
            r.get("pdf_stem", ""),
            r.get("section_code", ""),
            int(r.get("chunk_index") or 0),
        ),
    )
    with index_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CHUNKS_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upsert_index(index_path: Path, new_rows: list[dict], processed_tickers: set[str], replace_all: bool) -> None:
    if replace_all:
        rows = new_rows
    else:
        existing = read_existing_index(index_path)
        rows = [
            row
            for row in existing
            if row.get("ticker", "") not in processed_tickers
        ]
        rows.extend(new_rows)
    write_index(index_path, rows)


def run(
    input_root: str | Path,
    out: str | Path,
    index: str | Path,
    sections_index: str | Path = "data/00_reference/esg_sections_index.csv",
    parse_index: str | Path = "data/00_reference/esg_parse_index.csv",
    ticker: str | None = None,
) -> list[dict]:
    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)
    encoder = tiktoken.get_encoding(ENCODING)
    section_metadata = load_section_metadata(Path(sections_index))
    doc_metadata = load_doc_metadata(Path(parse_index))

    section_files = discover_section_files(input_root, ticker=ticker)
    print(f"Found {len(section_files)} ESG section file(s) under {input_root}")

    rows: list[dict] = []
    processed_tickers = {section_file.parent.name.upper() for section_file in section_files}
    for processed_ticker in processed_tickers:
        clear_ticker_chunks(output_root, processed_ticker)

    skipped_messages: list[str] = []

    for section_file in section_files:
        section_rows, warning = process_section_file(
            section_file,
            output_root,
            encoder,
            section_metadata,
            doc_metadata,
        )
        rows.extend(section_rows)
        if warning:
            skipped_messages.append(warning)

        print(f"  {section_file.parent.name.upper()} {section_file.stem}: {len(section_rows)} chunk(s)")

    upsert_index(index_path, rows, processed_tickers, replace_all=ticker is None)
    print()
    print(f"Chunks created: {len(rows)}")
    print(f"Skipped/warnings: {len(skipped_messages)}")
    for message in skipped_messages[:20]:
        print(f"  - {message}")
    if len(skipped_messages) > 20:
        print(f"  ... {len(skipped_messages) - 20} more")
    print(f"Index saved to: {index_path}")
    print(f"Chunks saved to: {output_root.resolve()}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Chunk ESG sections into retrieval-ready text chunks.")
    parser.add_argument("--input", default="data/03_sections/esg")
    parser.add_argument("--out", default="data/04_chunks/esg")
    parser.add_argument("--index", default="data/00_reference/esg_chunks_index.csv")
    parser.add_argument("--sections-index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--ticker", default=None)
    args = parser.parse_args()

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        sections_index=args.sections_index,
        parse_index=args.parse_index,
        ticker=args.ticker.upper() if args.ticker else None,
    )


if __name__ == "__main__":
    main()
