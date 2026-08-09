"""Manifest-driven HTML parser for the FY2023–FY2025 v2 corpus."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup, Comment, UnicodeDammit

PARSER_VERSION = "fy2325-html-v2.2"
NON_CONTENT_TAGS = ("script", "style", "noscript", "template")
BLOCK_TAGS = (
    "address", "article", "aside", "blockquote", "caption", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "summary", "ul",
)
HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
    re.I,
)
ZERO_FONT_STYLE_RE = re.compile(
    r"(?:^|;)\s*font-size\s*:\s*0(?:\.0+)?"
    r"(?:px|pt|em|rem|%)?(?:\s*;|$)",
    re.I,
)
FONT_SIZE_DECL_RE = re.compile(
    r"(?:^|;)\s*font-size\s*:\s*([^;]+)",
    re.I,
)
ZERO_FONT_VALUE_RE = re.compile(
    r"0(?:\.0+)?(?:px|pt|em|rem|%)?",
    re.I,
)

NUMBER_RE = re.compile(
    r"(?:[$€£¥]\s*)?\(?-?\d[\d,\s]*(?:\.\d+)?%?\)?|(?:19|20)\d{2}"
)
ITEM_RE = re.compile(r"(?i)^\s*item\s+\d{1,2}[a-c]?\b")
TOC_RE = re.compile(r"(?i)table\s+of\s+contents")


@dataclass(frozen=True)
class ParsedRecord:
    company_id: str
    ticker: str
    cik: str
    coverage_year: int
    filing_year: int
    accession_number: str
    source_file: str
    source_sha256: str
    output_file: str
    text_sha256: str
    parser_version: str
    parser_config_sha256: str
    char_count: int
    semantic_table_count: int
    layout_table_count: int
    parse_status: str
    quality_flags: tuple[str, ...]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parser_config_sha256() -> str:
    payload = {
        "version": PARSER_VERSION,
        "non_content_tags": NON_CONTENT_TAGS,
        "block_tags": BLOCK_TAGS,
        "hidden_style_pattern": HIDDEN_STYLE_RE.pattern,
        "zero_font_style_pattern": ZERO_FONT_STYLE_RE.pattern,
        "zero_font_policy": "preserve-nonzero-font-descendants",
        "table_classifier": "semantic-v2",
        "text_extraction": "dom-block-aware-v2.1",
        "toc_running_header_policy": "retain-first-standalone",
        "unicode_normalization": "NFKC",
    }
    return sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def decode_html(raw: bytes) -> tuple[str, list[str]]:
    flags = []
    decoded = UnicodeDammit(raw, is_html=True).unicode_markup
    if decoded is None:
        decoded = raw.decode("utf-8", errors="replace")
        flags.append("encoding_fallback_utf8_replace")
    if "\ufffd" in decoded:
        flags.append("source_contains_replacement_character")
    return decoded, flags


def zero_font_container_is_hidden(tag, style: str) -> bool:
    """Treat zero-font leaves as hidden, but preserve visible descendants."""
    if not ZERO_FONT_STYLE_RE.search(style):
        return False

    for descendant in tag.find_all(True):
        match = FONT_SIZE_DECL_RE.search(
            str(descendant.get("style", ""))
        )
        if (
            match
            and not ZERO_FONT_VALUE_RE.fullmatch(
                match.group(1).strip()
            )
        ):
            return False

    return True


def strip_hidden_and_noncontent(soup: BeautifulSoup) -> None:
    for name in NON_CONTENT_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()
    for tag in soup.find_all(
        lambda candidate: getattr(candidate, "name", "").lower()
        in {"ix:hidden", "xbrli:hidden"}
    ):
        tag.decompose()
    for tag in reversed(soup.find_all(True)):
        if not getattr(tag, "attrs", None):
            continue
        style = str(tag.get("style", ""))
        if (
            tag.has_attr("hidden")
            or str(tag.get("aria-hidden", "")).lower() == "true"
            or HIDDEN_STYLE_RE.search(style)
            or zero_font_container_is_hidden(tag, style)
        ):
            tag.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()


def clean_cell(value: str) -> str:
    value = unicodedata.normalize("NFKC", value.replace("\xa0", " "))
    return re.sub(r"\s+", " ", value).strip()


def table_rows(table) -> list[list[str]]:
    rows = []
    for row in table.find_all("tr"):
        cells = [clean_cell(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def is_semantic_table(table, rows: list[list[str]]) -> bool:
    if len(rows) < 2 or max((len(row) for row in rows), default=0) < 2:
        return False
    flattened = [cell for row in rows for cell in row]
    joined = " ".join(flattened)
    item_cells = sum(bool(ITEM_RE.match(cell)) for cell in flattened)
    if TOC_RE.search(joined) or item_cells >= 3:
        return False
    numeric_cells = sum(bool(NUMBER_RE.fullmatch(cell) or NUMBER_RE.search(cell)) for cell in flattened)
    numeric_ratio = numeric_cells / max(len(flattened), 1)
    header_evidence = bool(table.find("th")) or any(
        re.search(
            r"(?i)\b(year ended|fiscal|amount|revenue|sales|assets|liabilities|cash flows|shares|percent)\b",
            cell,
        )
        for cell in flattened[: max(12, len(rows[0]))]
    )
    return numeric_cells >= 2 and numeric_ratio >= 0.12 and (header_evidence or len(rows) >= 3)


def table_text(rows: list[list[str]], semantic: bool, table_id: str) -> str:
    lines = [" | ".join(row) for row in rows]
    if semantic:
        return "\n".join([f"[TABLE_START:{table_id}]", *lines, f"[TABLE_END:{table_id}]"])
    return "\n".join(lines)


def normalize_visible_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value.replace("\xa0", " "))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    lines = [line.strip() for line in value.splitlines()]
    output = []
    blank = False
    for line in lines:
        if not line:
            if output and not blank:
                output.append("")
            blank = True
            continue
        output.append(line)
        blank = False
    return "\n".join(output).strip()


def extract_block_aware_text(soup: BeautifulSoup) -> str:
    """Preserve block boundaries without splitting adjacent inline nodes."""
    for br in soup.find_all("br"):
        br.replace_with(soup.new_string("\n"))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(soup.new_string("\n"))
        tag.insert_after(soup.new_string("\n"))
    return soup.get_text("")


def remove_repeated_toc_headers(value: str) -> str:
    """Keep the first standalone TOC title; remove page-running copies."""
    output = []
    seen_standalone_toc = False
    for line in value.splitlines():
        if TOC_RE.fullmatch(line.strip()):
            if seen_standalone_toc:
                continue
            seen_standalone_toc = True
        output.append(line)
    return "\n".join(output)


def parse_html(path: Path) -> tuple[str, int, int, list[str]]:
    decoded, flags = decode_html(path.read_bytes())
    soup = BeautifulSoup(decoded, "lxml")
    strip_hidden_and_noncontent(soup)
    semantic_count = 0
    layout_count = 0
    top_level_tables = [
        table for table in soup.find_all("table") if table.find_parent("table") is None
    ]
    for table_number, table in enumerate(top_level_tables, 1):
        rows = table_rows(table)
        if not rows:
            table.decompose()
            continue
        semantic = is_semantic_table(table, rows)
        if semantic:
            semantic_count += 1
            table_id = f"table_{semantic_count:04d}"
        else:
            layout_count += 1
            table_id = f"layout_{layout_count:04d}"
        table.replace_with(
            soup.new_string(f"\n{table_text(rows, semantic, table_id)}\n")
        )
    text = normalize_visible_text(extract_block_aware_text(soup))
    text = remove_repeated_toc_headers(text)
    if len(text) < 2_000:
        flags.append("suspiciously_thin_parse")
    if "\ufffd" in text:
        flags.append("normalized_text_contains_replacement_character")
    return text, semantic_count, layout_count, sorted(set(flags))


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 561 or {row["selection_status"] for row in rows} != {"selected"}:
        raise ValueError("manifest is not the frozen 561-row selected corpus")
    if len({(row["ticker"], row["coverage_year"]) for row in rows}) != 561:
        raise ValueError("duplicate company/coverage-year pair in manifest")
    return rows


def source_for(row: dict[str, str], raw_root: Path) -> Path:
    matches = list((raw_root / row["ticker"]).glob(f"{row['accession_number']}.*"))
    matches = [path for path in matches if path.suffix.lower() in {".htm", ".html"}]
    if len(matches) != 1:
        raise FileNotFoundError(
            f"{row['ticker']} {row['accession_number']}: expected one raw file, found {len(matches)}"
        )
    return matches[0]


def run(
    manifest: Path,
    raw_root: Path,
    output_root: Path,
    accessions: set[str] | None = None,
) -> dict[str, object]:
    final_text = output_root / "html_text"
    final_manifest = output_root / "parsed_documents.jsonl"
    staging = output_root.with_name(output_root.name + ".staging")
    if output_root.exists() or staging.exists():
        raise FileExistsError(f"refusing existing output/staging root: {output_root}")
    staging_text = staging / "html_text"
    staging_text.mkdir(parents=True)
    records = []
    config_hash = parser_config_sha256()
    counts = Counter()
    manifest_rows = load_manifest(manifest)
    if accessions is not None:
        manifest_rows = [
            row for row in manifest_rows if row["accession_number"] in accessions
        ]
        found = {row["accession_number"] for row in manifest_rows}
        if found != accessions:
            raise ValueError(
                f"pilot accessions missing from manifest: {sorted(accessions - found)}"
            )
    for row in manifest_rows:
        source = source_for(row, raw_root)
        actual_source_hash = sha256_file(source)
        if actual_source_hash != row["source_sha256"]:
            raise RuntimeError(f"source hash mismatch: {source}")
        text, semantic_tables, layout_tables, flags = parse_html(source)
        output_name = f"{row['ticker']}__10-K__{row['accession_number']}.txt"
        output_path = staging_text / output_name
        temporary = output_path.with_suffix(".txt.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(output_path)
        status = "passed" if not {"suspiciously_thin_parse", "normalized_text_contains_replacement_character"} & set(flags) else "review_required"
        counts[status] += 1
        records.append(
            ParsedRecord(
                company_id=row["company_id"],
                ticker=row["ticker"],
                cik=row["cik"],
                coverage_year=int(row["coverage_year"]),
                filing_year=int(row["filing_year"]),
                accession_number=row["accession_number"],
                source_file=str(source),
                source_sha256=actual_source_hash,
                output_file=str(final_text / output_name),
                text_sha256=sha256_bytes(text.encode("utf-8")),
                parser_version=PARSER_VERSION,
                parser_config_sha256=config_hash,
                char_count=len(text),
                semantic_table_count=semantic_tables,
                layout_table_count=layout_tables,
                parse_status=status,
                quality_flags=tuple(flags),
            )
        )
    with (staging / "parsed_documents.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    staging.replace(output_root)
    return {
        "documents": len(records),
        "status_counts": dict(sorted(counts.items())),
        "semantic_tables": sum(record.semantic_table_count for record in records),
        "layout_tables": sum(record.layout_table_count for record in records),
        "output_root": str(output_root),
        "manifest": str(final_manifest),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--accessions-file",
        type=Path,
        help="Optional newline-delimited pilot accession list.",
    )
    args = parser.parse_args()
    accessions = None
    if args.accessions_file:
        accessions = {
            line.strip()
            for line in args.accessions_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    print(
        json.dumps(
            run(args.manifest, args.raw_root, args.output_root, accessions),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
