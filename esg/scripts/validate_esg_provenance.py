from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path


VERIFIED_STATUSES = {
    "verified_exact",
    "verified_whitespace_normalized",
}
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 600
SHORT_EVIDENCE_MIN_TOKENS = 25
CHUNK_TYPE_NORMAL = "normal"
CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"
SECTIONER_VERSION = "contiguous_v2"


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def parse_int(value: str | int | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def valid_chunk_token_count(token_count: int | None, chunk_type: str | None) -> bool:
    if token_count is None:
        return False
    if (chunk_type or CHUNK_TYPE_NORMAL).strip() == CHUNK_TYPE_SHORT_EVIDENCE:
        return SHORT_EVIDENCE_MIN_TOKENS <= token_count < MIN_CHUNK_TOKENS
    return MIN_CHUNK_TOKENS <= token_count <= MAX_CHUNK_TOKENS


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_doc_key(row: dict) -> tuple[str, str] | None:
    ticker = (row.get("ticker") or "").strip().upper()
    source = row.get("source_pdf") or row.get("pdf_file") or ""
    pdf_stem = Path(source).stem
    if not ticker or not pdf_stem:
        return None
    return ticker, pdf_stem


def row_matches_scope(
    row: dict,
    ticker: str | None = None,
    pdf_stem: str | None = None,
) -> bool:
    selected_ticker = ticker.strip().upper() if ticker else None
    selected_stem = Path(pdf_stem).stem if pdf_stem else None
    row_ticker = (row.get("ticker") or "").strip().upper()
    row_stem = (
        parse_doc_key(row)[1]
        if parse_doc_key(row) is not None
        else (row.get("pdf_stem") or "").strip()
    )
    return (
        (not selected_ticker or row_ticker == selected_ticker)
        and (not selected_stem or row_stem == selected_stem)
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def validate(parse_rows: list[dict], section_rows: list[dict], chunk_rows: list[dict]) -> dict:
    errors: list[str] = []
    parse_by_doc = {
        key: row
        for row in parse_rows
        if (key := parse_doc_key(row)) is not None
    }
    parsed_cache: dict[tuple[str, str], tuple[str, str]] = {}

    def parsed_source(key: tuple[str, str]) -> tuple[str | None, str]:
        if key in parsed_cache:
            return parsed_cache[key]
        parse_row = parse_by_doc.get(key)
        path = resolve_path(parse_row.get("parsed_text_file") if parse_row else None)
        if path is None or not path.is_file():
            return None, ""
        value = (
            path.read_text(encoding="utf-8", errors="replace"),
            sha256_bytes(path),
        )
        parsed_cache[key] = value
        return value

    sections: dict[tuple[str, str, str], dict] = {}
    validated_documents: set[tuple[str, str]] = set()
    section_instance_counts: Counter[tuple[str, str, str]] = Counter()
    exact_sections = 0

    for row_number, row in enumerate(section_rows, start=2):
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        instance = (row.get("section_instance_id") or "").strip()
        key = (ticker, pdf_stem, instance)
        if ticker and pdf_stem:
            validated_documents.add((ticker, pdf_stem))
        section_instance_counts[key] += 1
        if not ticker or not pdf_stem or not instance:
            errors.append(f"section row {row_number}: missing v2 section identity")
            continue
        if row.get("provenance_version") != SECTIONER_VERSION:
            errors.append(f"{key}: provenance_version is not {SECTIONER_VERSION}")

        parsed_text, parsed_sha256 = parsed_source((ticker, pdf_stem))
        section_path = resolve_path(row.get("section_file"))
        if parsed_text is None:
            errors.append(f"{key}: parsed text file missing")
            continue
        if section_path is None or not section_path.is_file():
            errors.append(f"{key}: section file missing")
            continue

        start = parse_int(row.get("source_start_char"))
        end = parse_int(row.get("source_end_char"))
        section_text = section_path.read_text(encoding="utf-8", errors="replace")
        if start is None or end is None or not 0 <= start < end <= len(parsed_text):
            errors.append(f"{key}: invalid section source bounds")
            continue
        if parsed_text[start:end] != section_text:
            errors.append(f"{key}: section is not the declared exact source slice")
            continue
        exact_sections += 1
        if row.get("source_sha256") and row.get("source_sha256") != parsed_sha256:
            errors.append(f"{key}: parsed-text fingerprint mismatch")
        sections[key] = {
            "start": start,
            "end": end,
            "text": section_text,
            "sha256": sha256_bytes(section_path),
        }

    for key, count in section_instance_counts.items():
        if count > 1:
            errors.append(f"{key}: duplicate section instance ({count} rows)")

    chunk_id_counts: Counter[str] = Counter()
    citation_status_counts: Counter[str] = Counter()
    verified_exact = 0
    verified_normalized = 0
    valid_token_chunks = 0
    short_evidence_chunks = 0
    normal_chunks = 0
    chunked_section_instances: set[tuple[str, str, str]] = set()

    for row_number, row in enumerate(chunk_rows, start=2):
        chunk_id = (row.get("chunk_id") or "").strip()
        chunk_id_counts[chunk_id] += 1
        ticker = (row.get("ticker") or "").strip().upper()
        pdf_stem = (row.get("pdf_stem") or "").strip()
        instance = (row.get("section_instance_id") or "").strip()
        section_key = (ticker, pdf_stem, instance)
        status = (row.get("citation_validation_status") or "").strip()
        citation_status_counts[status] += 1
        chunk_type = (row.get("chunk_type") or CHUNK_TYPE_NORMAL).strip()
        if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE:
            short_evidence_chunks += 1
        else:
            normal_chunks += 1
        section = sections.get(section_key)
        if not chunk_id or not ticker or not pdf_stem or not instance:
            errors.append(f"chunk row {row_number}: missing v2 chunk identity")
            continue
        if section is None:
            errors.append(f"{chunk_id}: no matching exact section instance")
            continue
        chunked_section_instances.add(section_key)

        source_id = (row.get("source_id") or "").strip()
        source_version_id = (row.get("source_version_id") or "").strip()
        chunk_index = parse_int(row.get("chunk_index"))
        if not source_id or not source_version_id or chunk_index is None:
            errors.append(f"{chunk_id}: missing source/version/chunk identity")
        else:
            expected_id = f"{source_id}__{instance}__chunk_{chunk_index:04d}"
            if chunk_id != expected_id:
                errors.append(f"{chunk_id}: does not match v2 ID {expected_id}")

        token_count = parse_int(row.get("token_count"))
        if not valid_chunk_token_count(token_count, chunk_type):
            if chunk_type == CHUNK_TYPE_SHORT_EVIDENCE:
                errors.append(f"{chunk_id}: short_evidence token_count outside 25..99")
            else:
                errors.append(f"{chunk_id}: token_count outside 100..600")
        else:
            valid_token_chunks += 1

        chunk_path = resolve_path(row.get("chunk_file"))
        if chunk_path is None or not chunk_path.is_file():
            errors.append(f"{chunk_id}: chunk file missing")
            continue
        chunk_text = chunk_path.read_text(encoding="utf-8", errors="replace")
        start = parse_int(row.get("source_start_char"))
        end = parse_int(row.get("source_end_char"))
        parsed_text, parsed_sha256 = parsed_source((ticker, pdf_stem))
        if (
            parsed_text is None
            or start is None
            or end is None
            or not section["start"] <= start < end <= section["end"]
        ):
            errors.append(f"{chunk_id}: invalid or out-of-section source bounds")
            continue

        source_slice = parsed_text[start:end]
        exact = source_slice == chunk_text
        normalized = normalize_whitespace(source_slice) == normalize_whitespace(chunk_text)
        if not normalized:
            errors.append(f"{chunk_id}: full chunk does not match declared source slice")
            continue
        if row.get("parsed_text_sha256") != parsed_sha256:
            errors.append(f"{chunk_id}: parsed_text_sha256 mismatch")
        if row.get("section_text_sha256") != section["sha256"]:
            errors.append(f"{chunk_id}: section_text_sha256 mismatch")

        expected_status = (
            "verified_exact" if exact else "verified_whitespace_normalized"
        )
        if status != expected_status:
            errors.append(
                f"{chunk_id}: status {status!r} should be {expected_status!r}"
            )
        elif exact:
            verified_exact += 1
        else:
            verified_normalized += 1
        if row.get("citation_validation_version") != "semantic_v1":
            errors.append(f"{chunk_id}: citation validation version is not semantic_v1")
        if status not in VERIFIED_STATUSES or not parse_bool(row.get("citation_ready")):
            errors.append(f"{chunk_id}: verified source slice is not citation-ready")
        if not row.get("page_start") or not row.get("page_end"):
            errors.append(f"{chunk_id}: missing page mapping")

    for chunk_id, count in chunk_id_counts.items():
        if not chunk_id:
            continue
        if count > 1:
            errors.append(f"{chunk_id}: duplicate chunk ID ({count} rows)")

    metrics = {
        "parse_index_documents": len(parse_by_doc),
        "validated_documents": len(validated_documents),
        "sections": len(section_rows),
        "unique_section_instances": len(section_instance_counts),
        "chunked_section_instances": len(chunked_section_instances),
        "unchunked_section_instances": len(sections) - len(chunked_section_instances),
        "exact_sections": exact_sections,
        "chunks": len(chunk_rows),
        "normal_chunks": normal_chunks,
        "short_evidence_chunks": short_evidence_chunks,
        "unique_chunk_ids": len([key for key in chunk_id_counts if key]),
        "verified_exact_chunks": verified_exact,
        "verified_whitespace_normalized_chunks": verified_normalized,
        "valid_token_chunks": valid_token_chunks,
        "citation_status_counts": dict(sorted(citation_status_counts.items())),
        "errors": len(errors),
    }
    return {"passed": not errors, "metrics": metrics, "error_samples": errors[:100]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Independently validate ESG section/chunk citation provenance."
    )
    parser.add_argument("--parse-index", required=True)
    parser.add_argument("--sections-index", required=True)
    parser.add_argument("--chunks-index", required=True)
    parser.add_argument("--json-out")
    parser.add_argument("--ticker")
    parser.add_argument("--pdf-file")
    parser.add_argument("--pdf-stem")
    args = parser.parse_args()
    if args.pdf_file and args.pdf_stem:
        parser.error("use only one of --pdf-file and --pdf-stem")
    selected_stem = args.pdf_stem or (Path(args.pdf_file).stem if args.pdf_file else None)

    parse_rows = [
        row for row in read_csv(Path(args.parse_index))
        if row_matches_scope(row, args.ticker, selected_stem)
    ]
    section_rows = [
        row for row in read_csv(Path(args.sections_index))
        if row_matches_scope(row, args.ticker, selected_stem)
    ]
    chunk_rows = [
        row for row in read_csv(Path(args.chunks_index))
        if row_matches_scope(row, args.ticker, selected_stem)
    ]

    result = validate(
        parse_rows,
        section_rows,
        chunk_rows,
    )
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))
    if result["error_samples"]:
        print("Error samples:")
        for error in result["error_samples"]:
            print(f"- {error}")
    if args.json_out:
        atomic_write_json(Path(args.json_out), result)
        print(f"JSON saved to: {Path(args.json_out)}")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
