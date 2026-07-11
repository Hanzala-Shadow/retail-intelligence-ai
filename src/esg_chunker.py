from __future__ import annotations

import argparse
import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
    "source_start_char",
    "source_end_char",
    "page_start",
    "page_end",
    "citation_ready",
]


@dataclass(frozen=True)
class SourceFingerprint:
    """Fingerprint of a section input used to validate resumable output."""

    size_bytes: int
    mtime_utc: str
    sha256: str

    def as_index_fields(self) -> dict[str, str]:
        return {
            "source_size_bytes": str(self.size_bytes),
            "source_mtime_utc": self.mtime_utc,
            "source_sha256": self.sha256,
        }


@dataclass
class ChunkOutput:
    path: Path
    text: str
    row: dict


@dataclass
class SectionPlan:
    ticker: str
    pdf_stem: str
    section_code: str
    source_file: Path
    source_fingerprint: SourceFingerprint
    source_token_count: int
    outputs: list[ChunkOutput]

    @property
    def key(self) -> tuple[str, str, str]:
        return self.ticker, self.pdf_stem, self.section_code

    @property
    def is_short(self) -> bool:
        return self.source_token_count < MIN_CHUNK_TOKENS


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


def read_section_source(section_file: Path) -> tuple[str, SourceFingerprint]:
    """Read one section and make the fingerprint refer to those exact bytes."""
    before = section_file.stat()
    payload = section_file.read_bytes()
    after = section_file.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError(f"section input changed while being read: {section_file}")

    return (
        payload.decode("utf-8", errors="replace").strip(),
        SourceFingerprint(
            size_bytes=after.st_size,
            mtime_utc=datetime.fromtimestamp(
                after.st_mtime, tz=timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )


def atomic_write_text(path: Path, text: str) -> None:
    """Write one output through a sibling temporary file before replacing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def section_chunk_files(ticker_out: Path, pdf_stem: str, section_code: str) -> list[Path]:
    if not ticker_out.exists():
        return []
    prefix = f"{pdf_stem}__{section_code}__chunk_"
    return sorted(
        path
        for path in ticker_out.glob("*.txt")
        if path.name.startswith(prefix)
    )


def section_marker_path(output_root: Path, ticker: str, pdf_stem: str, section_code: str) -> Path:
    return output_root / ticker / f".{pdf_stem}__{section_code}.inprogress"


def start_section_update(marker_path: Path, fingerprint: SourceFingerprint) -> None:
    """Persist an in-progress marker so an interrupted force run is resumed safely."""
    atomic_write_text(
        marker_path,
        "\n".join(
            [
                f"source_sha256={fingerprint.sha256}",
                f"started_at={datetime.now(timezone.utc).isoformat()}",
                "",
            ]
        ),
    )


def start_orphan_section_cleanup(marker_path: Path) -> None:
    """Mark targeted cleanup for a section removed by the splitter.

    A source PDF can be re-split into a different set of section codes.  This
    marker makes removal of the now-orphaned *one section* restart-safe without
    ever clearing an entire ticker's chunks.
    """
    atomic_write_text(
        marker_path,
        "\n".join(
            [
                "operation=orphan_section_cleanup",
                f"started_at={datetime.now(timezone.utc).isoformat()}",
                "",
            ]
        ),
    )


def clear_section_marker(marker_path: Path) -> None:
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass


def build_section_plan(
    section_file: Path,
    output_root: Path,
    encoder,
    section_metadata: dict[tuple[str, str, str], dict],
    doc_metadata: dict[tuple[str, str], dict],
) -> SectionPlan:
    ticker = section_file.parent.name.upper()
    parsed_name = parse_section_filename(section_file)
    if parsed_name is None:
        raise ValueError(f"bad filename: {section_file}")

    pdf_stem, section_code = parsed_name
    text, source_fingerprint = read_section_source(section_file)
    tokens = encoder.encode(text)
    if len(tokens) < MIN_CHUNK_TOKENS:
        return SectionPlan(
            ticker=ticker,
            pdf_stem=pdf_stem,
            section_code=section_code,
            source_file=section_file,
            source_fingerprint=source_fingerprint,
            source_token_count=len(tokens),
            outputs=[],
        )

    token_groups = chunk_tokens(tokens)
    if not token_groups:
        raise ValueError(f"no valid chunks created for non-short section: {section_file}")

    section_meta = section_metadata.get((ticker, pdf_stem, section_code), {})
    doc_meta = doc_metadata.get((ticker, pdf_stem), {})
    page_spans = read_page_map(doc_meta.get("page_map_file"))
    section_source_start = parse_int(section_meta.get("source_start_char"))
    ticker_out = output_root / ticker
    outputs: list[ChunkOutput] = []
    search_pos = 0

    for chunk_index, chunk_token_ids in enumerate(token_groups):
        token_count = len(chunk_token_ids)
        if token_count < MIN_CHUNK_TOKENS or token_count > MAX_CHUNK_TOKENS:
            raise ValueError(
                f"invalid chunk size ({token_count} tokens): {section_file}"
            )

        chunk_text = encoder.decode(chunk_token_ids).strip()
        if not chunk_text:
            raise ValueError(f"empty chunk created for: {section_file}")
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
        citation_ready = bool(
            page_start and page_end and source_start is not None and source_end is not None
        )

        doc_type = doc_meta.get("doc_type") or DOC_TYPE
        quality_status = doc_meta.get("doc_quality_status") or "needs_review"
        rag_action = doc_meta.get("rag_action") or rag_action_for_status(quality_status)
        chunk_id = (
            f"{ticker}__{doc_type}__{pdf_stem}__{section_code}__chunk_{chunk_index:04d}"
        )
        chunk_file = ticker_out / f"{pdf_stem}__{section_code}__chunk_{chunk_index:04d}.txt"
        row = {
            "chunk_id": chunk_id,
            "ticker": ticker,
            "doc_type": doc_type,
            "doc_quality_status": quality_status,
            "rag_action": rag_action,
            "quality_flags": doc_meta.get("quality_flags") or "",
            "pdf_stem": pdf_stem,
            "section_code": section_code,
            "chunk_index": chunk_index,
            "token_count": token_count,
            "char_count": len(chunk_text),
            "chunk_file": display_path(chunk_file),
            "source_section_file": display_path(section_file),
            **source_fingerprint.as_index_fields(),
            "source_start_char": source_start if source_start is not None else "",
            "source_end_char": source_end if source_end is not None else "",
            "page_start": page_start,
            "page_end": page_end,
            "citation_ready": "true" if citation_ready else "false",
        }
        outputs.append(ChunkOutput(path=chunk_file, text=chunk_text, row=row))

    return SectionPlan(
        ticker=ticker,
        pdf_stem=pdf_stem,
        section_code=section_code,
        source_file=section_file,
        source_fingerprint=source_fingerprint,
        source_token_count=len(tokens),
        outputs=outputs,
    )


def row_section_key(row: dict) -> tuple[str, str, str] | None:
    ticker = (row.get("ticker") or "").strip().upper()
    pdf_stem = (row.get("pdf_stem") or "").strip()
    section_code = (row.get("section_code") or "").strip()
    if not ticker or not pdf_stem or not section_code:
        return None
    return ticker, pdf_stem, section_code


def section_rows(rows: list[dict], key: tuple[str, str, str]) -> list[dict]:
    return [row for row in rows if row_section_key(row) == key]


def normalize_index_row(row: dict) -> dict:
    normalized = {field: row.get(field, "") for field in CHUNKS_INDEX_FIELDS}
    normalized["ticker"] = (normalized.get("ticker") or "").strip().upper()
    return normalized


def index_slot_key(row: dict) -> tuple[str, str, str, int] | None:
    section_key = row_section_key(row)
    chunk_index = parse_int(row.get("chunk_index"))
    if section_key is None or chunk_index is None:
        return None
    return *section_key, chunk_index


def canonicalize_index_rows(rows: list[dict]) -> list[dict]:
    """Make the index a keyed collection, not an append-only log."""
    by_slot: dict[tuple[str, str, str, int], dict] = {}
    without_slot: list[dict] = []
    for row in rows:
        normalized = normalize_index_row(row)
        slot_key = index_slot_key(normalized)
        if slot_key is None:
            without_slot.append(normalized)
        else:
            by_slot[slot_key] = normalized

    by_chunk_id: dict[str, dict] = {}
    without_chunk_id: list[dict] = []
    for row in [*without_slot, *by_slot.values()]:
        chunk_id = (row.get("chunk_id") or "").strip()
        if chunk_id:
            by_chunk_id[chunk_id] = row
        else:
            without_chunk_id.append(row)
    return [*without_chunk_id, *by_chunk_id.values()]


def fingerprint_matches(row: dict, fingerprint: SourceFingerprint) -> bool:
    return all(
        (row.get(field) or "") == expected
        for field, expected in fingerprint.as_index_fields().items()
    )


def valid_chunk_file(path: Path, expected_text: str, encoder) -> bool:
    try:
        if not path.is_file():
            return False
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if content != expected_text:
        return False
    token_count = len(encoder.encode(content))
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def section_is_complete(
    plan: SectionPlan,
    rows: list[dict],
    encoder,
) -> bool:
    """Require index, source fingerprint, chunk files, and valid token counts."""
    if plan.is_short:
        return False

    existing_rows = section_rows(rows, plan.key)
    if len(existing_rows) != len(plan.outputs):
        return False

    rows_by_index: dict[int, dict] = {}
    for row in existing_rows:
        chunk_index = parse_int(row.get("chunk_index"))
        if chunk_index is None or chunk_index in rows_by_index:
            return False
        rows_by_index[chunk_index] = row

    seen_chunk_ids: set[str] = set()
    for output in plan.outputs:
        expected = output.row
        chunk_index = parse_int(expected.get("chunk_index"))
        if chunk_index is None:
            return False
        row = rows_by_index.get(chunk_index)
        if row is None:
            return False
        if not fingerprint_matches(row, plan.source_fingerprint):
            return False
        if (row.get("chunk_id") or "") != expected["chunk_id"]:
            return False
        if parse_int(row.get("token_count")) != parse_int(expected.get("token_count")):
            return False
        if not (MIN_CHUNK_TOKENS <= (parse_int(row.get("token_count")) or 0) <= MAX_CHUNK_TOKENS):
            return False

        recorded_path = resolve_path(row.get("chunk_file"))
        if recorded_path is None:
            return False
        try:
            if recorded_path.resolve() != output.path.resolve():
                return False
        except OSError:
            return False
        if not valid_chunk_file(recorded_path, output.text, encoder):
            return False

        chunk_id = row.get("chunk_id") or ""
        if not chunk_id or chunk_id in seen_chunk_ids:
            return False
        seen_chunk_ids.add(chunk_id)

    return True


def commit_section_outputs(plan: SectionPlan, output_root: Path) -> None:
    """Atomically replace one section's chunks, then remove only its stale chunks."""
    ticker_out = output_root / plan.ticker
    for output in plan.outputs:
        atomic_write_text(output.path, output.text)

    expected_names = {output.path.name for output in plan.outputs}
    for stale_file in section_chunk_files(ticker_out, plan.pdf_stem, plan.section_code):
        if stale_file.name not in expected_names:
            stale_file.unlink()


def clear_section_outputs(plan: SectionPlan, output_root: Path) -> None:
    """Remove output only for a source section that is now too short to chunk."""
    ticker_out = output_root / plan.ticker
    for stale_file in section_chunk_files(ticker_out, plan.pdf_stem, plan.section_code):
        stale_file.unlink()


def clear_orphan_section_outputs(
    output_root: Path, key: tuple[str, str, str]
) -> None:
    """Remove chunks for exactly one section no longer present in the section index."""
    ticker, pdf_stem, section_code = key
    ticker_out = output_root / ticker
    for stale_file in section_chunk_files(ticker_out, pdf_stem, section_code):
        stale_file.unlink()


def replace_section_rows(
    rows: list[dict], key: tuple[str, str, str], new_rows: list[dict]
) -> list[dict]:
    retained = [row for row in rows if row_section_key(row) != key]
    return canonicalize_index_rows([*retained, *new_rows])


def discover_section_files(input_root: Path, ticker: str | None = None) -> list[Path]:
    if not input_root.exists():
        return []
    files = sorted(input_root.glob("*/*.txt"))
    if ticker:
        ticker = ticker.upper()
        files = [path for path in files if path.parent.name.upper() == ticker]
    return files


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [{field: row.get(field, "") for field in CHUNKS_INDEX_FIELDS} for row in reader]


def index_sort_key(row: dict) -> tuple[str, str, str, int, str]:
    return (
        (row.get("ticker") or "").upper(),
        row.get("pdf_stem") or "",
        row.get("section_code") or "",
        parse_int(row.get("chunk_index")) or 0,
        row.get("chunk_id") or "",
    )


def write_index(index_path: Path, rows: list[dict]) -> None:
    """Atomically checkpoint the deduplicated chunks index."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(canonicalize_index_rows(rows), key=index_sort_key)
    tmp_path = index_path.with_name(f"{index_path.name}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CHUNKS_INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, index_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run(
    input_root: str | Path,
    out: str | Path,
    index: str | Path,
    sections_index: str | Path = "data/00_reference/esg_sections_index.csv",
    parse_index: str | Path = "data/00_reference/esg_parse_index.csv",
    ticker: str | None = None,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = 1,
) -> list[dict]:
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")

    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)
    encoder = tiktoken.get_encoding(ENCODING)
    section_metadata = load_section_metadata(Path(sections_index))
    doc_metadata = load_doc_metadata(Path(parse_index))
    section_files = discover_section_files(input_root, ticker=ticker)
    print(f"Found {len(section_files)} ESG section file(s) under {input_root}")

    raw_rows = read_existing_index(index_path)
    index_rows = canonicalize_index_rows(raw_rows)
    index_dirty = raw_rows != index_rows
    pending_markers: list[Path] = []
    completed_since_checkpoint = 0
    created_rows: list[dict] = []
    summary = {
        "found_section_files": len(section_files),
        "skipped_complete": 0,
        "chunked": 0,
        "reprocessed_stale": 0,
        "short_sections_skipped": 0,
        "failed": 0,
    }

    def checkpoint() -> None:
        nonlocal index_dirty, completed_since_checkpoint
        write_index(index_path, index_rows)
        for marker in pending_markers:
            clear_section_marker(marker)
        pending_markers.clear()
        index_dirty = False
        completed_since_checkpoint = 0

    # If a re-split PDF no longer produces a previous section code, the
    # corresponding section file is absent from the authoritative sections
    # index. Remove only that stale section's chunks/index rows. This reconciles
    # changing section sets while retaining every other ticker/PDF/section.
    selected_ticker = ticker.upper() if ticker else None
    valid_section_keys = set(section_metadata)
    if valid_section_keys:
        indexed_section_keys = {
            key for row in index_rows if (key := row_section_key(row)) is not None
        }
        orphaned_section_keys = sorted(
            key
            for key in indexed_section_keys
            if (selected_ticker is None or key[0] == selected_ticker)
            and key not in valid_section_keys
        )
        for orphan_key in orphaned_section_keys:
            orphan_ticker, orphan_pdf_stem, orphan_section_code = orphan_key
            marker_path = section_marker_path(
                output_root,
                orphan_ticker,
                orphan_pdf_stem,
                orphan_section_code,
            )
            try:
                start_orphan_section_cleanup(marker_path)
                clear_orphan_section_outputs(output_root, orphan_key)
                index_rows = replace_section_rows(index_rows, orphan_key, [])
                pending_markers.append(marker_path)
                index_dirty = True
                completed_since_checkpoint += 1
                if completed_since_checkpoint >= checkpoint_every:
                    checkpoint()
                print(
                    "  "
                    f"{orphan_ticker} {orphan_pdf_stem}__{orphan_section_code}: "
                    "removed orphaned chunks"
                )
            except Exception as exc:
                summary["failed"] += 1
                print(
                    "  "
                    f"{orphan_ticker} {orphan_pdf_stem}__{orphan_section_code}: "
                    f"failed orphan cleanup ({exc})"
                )

    for section_file in section_files:
        try:
            plan = build_section_plan(
                section_file,
                output_root,
                encoder,
                section_metadata,
                doc_metadata,
            )
        except Exception as exc:
            summary["failed"] += 1
            print(f"  {section_file.parent.name.upper()} {section_file.stem}: failed ({exc})")
            continue

        marker_path = section_marker_path(
            output_root, plan.ticker, plan.pdf_stem, plan.section_code
        )
        current_rows = section_rows(index_rows, plan.key)
        current_files = section_chunk_files(
            output_root / plan.ticker, plan.pdf_stem, plan.section_code
        )
        has_existing_artifacts = bool(current_rows or current_files or marker_path.exists())
        complete = not marker_path.exists() and section_is_complete(plan, index_rows, encoder)

        if resume and not force and complete:
            summary["skipped_complete"] += 1
            print(f"  {plan.ticker} {section_file.stem}: skipped complete")
            continue

        if has_existing_artifacts and not complete:
            summary["reprocessed_stale"] += 1

        needs_cleanup = plan.is_short and has_existing_artifacts
        if plan.is_short:
            summary["short_sections_skipped"] += 1
            if needs_cleanup:
                try:
                    start_section_update(marker_path, plan.source_fingerprint)
                    clear_section_outputs(plan, output_root)
                    index_rows = replace_section_rows(index_rows, plan.key, [])
                    pending_markers.append(marker_path)
                    index_dirty = True
                    completed_since_checkpoint += 1
                    if completed_since_checkpoint >= checkpoint_every:
                        checkpoint()
                except Exception as exc:
                    summary["failed"] += 1
                    print(f"  {plan.ticker} {section_file.stem}: failed ({exc})")
                    continue
            print(
                f"  {plan.ticker} {section_file.stem}: short section skipped "
                f"({plan.source_token_count} tokens)"
            )
            continue

        try:
            # The marker remains until the matching index checkpoint succeeds.
            start_section_update(marker_path, plan.source_fingerprint)
            commit_section_outputs(plan, output_root)
            new_rows = [output.row for output in plan.outputs]
            index_rows = replace_section_rows(index_rows, plan.key, new_rows)
            pending_markers.append(marker_path)
            index_dirty = True
            completed_since_checkpoint += 1
            summary["chunked"] += 1
            created_rows.extend(new_rows)
            if completed_since_checkpoint >= checkpoint_every:
                checkpoint()
            print(f"  {plan.ticker} {section_file.stem}: {len(new_rows)} chunk(s)")
        except Exception as exc:
            summary["failed"] += 1
            print(f"  {plan.ticker} {section_file.stem}: failed ({exc})")

    if index_dirty or not index_path.exists():
        checkpoint()

    print()
    for label in (
        "found_section_files",
        "skipped_complete",
        "chunked",
        "reprocessed_stale",
        "short_sections_skipped",
        "failed",
    ):
        print(f"{label}: {summary[label]}")
    print(f"chunks_created: {len(created_rows)}")
    print(f"Index saved to: {index_path}")
    print(f"Chunks saved to: {output_root.resolve()}")
    return created_rows


def main():
    parser = argparse.ArgumentParser(description="Chunk ESG sections into retrieval-ready text chunks.")
    parser.add_argument("--input", default="data/03_sections/esg")
    parser.add_argument("--out", default="data/04_chunks/esg")
    parser.add_argument("--index", default="data/00_reference/esg_chunks_index.csv")
    parser.add_argument("--sections-index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--ticker", default=None)
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Skip valid completed sections (default).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the selected ticker (or all sections) even when complete.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=positive_int,
        default=1,
        metavar="N",
        help="Atomically checkpoint the chunks index after every N updated sections (default: 1).",
    )
    args = parser.parse_args()

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        sections_index=args.sections_index,
        parse_index=args.parse_index,
        ticker=args.ticker.upper() if args.ticker else None,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
    )


if __name__ == "__main__":
    main()
