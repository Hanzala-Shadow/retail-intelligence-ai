"""Offset-preserving, rejection-first 10-K sectioning for the v2 corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from section_splitter_10k import (
    _collect_item_candidates,
    _find_signature_candidate,
    _select_ordered_candidates,
)

SPLITTER_VERSION = "fy2325-section-v2.7"
MANDATORY_MAJOR = {"Item_1", "Item_1A", "Item_7", "Item_8"}
TOC_RE = re.compile(r"table\s+of\s+contents", re.I)
AUDITOR_RE = re.compile(
    r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm",
    re.I,
)
SIGNATURE_RE = re.compile(r"(?im)^\s*signatures?\s*$")
RESERVED_RE = re.compile(r"(?i)^\s*item\s+6\b.{0,120}\breserved\b")


ITEM_HEADING_RE = re.compile(
    r"(?im)^\s*item\s+(?:no\.?\s*)?"
    r"(?:1a?|1b|1c|2|3|4|5|6|7a?|8|9a?|9b|9c|10|11|12|13|14|15|16)\b"
)
ITEM_NO_PREFIX_RE = re.compile(
    r"(?im)^(\s*item\s+)no\.?\s*(?=\d{1,2}[a-c]?\b)"
)
TOC_PAGE_TRAILER_RE = re.compile(r"(?:\.{2,}|\s)\s*\d{1,4}\s*$")
REVERSED_ITEM_7A_RE = re.compile(
    r"(?i)^item\s+(?:no\.?\s*)?7a\b.*"
    r"qualitative\s+and\s+quantitative\s+disclosures.*"
    r"market\s+risk"
)

STANDALONE_TOC_RE = re.compile(
    r"(?im)^\s*table\s+of\s+contents\s*$"
)

SINGULAR_ITEM_8_RE = re.compile(
    r"(?i)^\s*item\s+(?:no\.?\s*)?8"
    r"\s*[\.\:\-\–\—|]?\s*"
    r"financial\s+statement\s+and\s+supplementary\s+data"
    r"\s*[\.]?\s*$"
)

CANONICAL_BOUNDARY_PATTERNS = {
    "Item_1": re.compile(
        r"(?i)^\s*item\s+(?:no\.?\s*)?1\b.{0,30}"
        r"(?:business|description\s+of\s+business)\b"
    ),
    "Item_1A": re.compile(
        r"(?i)^\s*item\s+(?:no\.?\s*)?1a\b.{0,30}"
        r"risk\s+factors?\b"
    ),
    "Item_7": re.compile(
        r"(?i)^\s*item\s+(?:no\.?\s*)?7\b.{0,40}"
        r"management.{0,30}discussion"
    ),
    "Item_7A": re.compile(
        r"(?i)^\s*item\s+(?:no\.?\s*)?7a\b.{0,50}"
        r"(?:quantitative\s+and\s+qualitative|"
        r"qualitative\s+and\s+quantitative).*market\s+risk"
    ),
    "Item_8": re.compile(
        r"(?i)^\s*item\s+(?:no\.?\s*)?8\b.{0,40}"
        r"financial\s+statements?\s+and\s+supplementary\s+"
        r"(?:financial\s+)?data"
    ),
}

VARIANT_MANDATORY_PATTERNS = {
    "Item_1": re.compile(
        r"(?i)^\s*item\s+(?:1|i)"
        r"(?:\s*[\.\:\-\–\—|]\s*)*"
        r"(?:business|description\s+of\s+business)\b"
    ),
    "Item_1A": re.compile(
        r"(?i)^\s*(?:item|ite\s*m|ltem)\s*1\s*a"
        r"(?:\s*[\.\:\-\–\—|]\s*)*risk\s+factors?\b"
    ),
    "Item_7": re.compile(
        r"(?i)^\s*(?:item|ite\s*m|ltem)\s*7"
        r"(?:\s*[\.\:\-\–\—|]\s*)*"
        r"management.{0,35}discussion"
    ),
    "Item_8": re.compile(
        r"(?i)^\s*(?:item|ite\s*m|ltem)\s*8"
        r"(?:\s*[\.\:\-\–\—|]\s*)*financial\s+statements?"
        r"\s+and\s+supplementary\s+(?:financial\s+)?data"
    ),
}

BARE_ITEM_HEADING_RE = re.compile(
    r"(?i)^\s*item\s+(?:no\.?\s*)?"
    r"\d{1,2}[a-c]?\s*[\.\:\-\–\—]?\s*$"
)



def collect_item_candidates_v2(
    text: str,
) -> dict[str, list[dict[str, object]]]:
    """Extend the legacy collector for same-line `Item No. N` headings."""
    candidates = _collect_item_candidates(text)
    normalized = ITEM_NO_PREFIX_RE.sub(
        lambda match: (
            match.group(1)
            + " " * (len(match.group(0)) - len(match.group(1)))
        ),
        text,
    )

    if normalized == text:
        return candidates

    supplemental = _collect_item_candidates(normalized)
    existing = {
        (code, int(candidate["position"]))
        for code, values in candidates.items()
        for candidate in values
    }

    for code, values in supplemental.items():
        for candidate in values:
            key = (code, int(candidate["position"]))
            if key in existing:
                continue

            candidate = dict(candidate)
            position = int(candidate["position"])
            line_end = text.find("\n", position)
            if line_end < 0:
                line_end = len(text)

            candidate["line"] = text[position:line_end].strip()
            candidate["method"] = "same_line_item_no_candidate"
            candidates.setdefault(code, []).append(candidate)
            existing.add(key)

    return candidates

def filter_toc_candidates(
    text: str,
    candidates: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Reject dense or page-numbered Item references inside an explicit TOC."""
    toc_positions = [match.start() for match in TOC_RE.finditer(text)]
    if not toc_positions:
        return candidates

    filtered: dict[str, list[dict[str, object]]] = {}
    for code, values in candidates.items():
        retained = []
        for candidate in values:
            position = int(candidate["position"])
            near_preceding_toc = any(
                0 <= position - toc_position <= 30_000
                for toc_position in toc_positions
            )
            line = str(candidate.get("line", "")).strip()
            forward_window = text[position : position + 2_500]
            dense_item_sequence = (
                len(ITEM_HEADING_RE.findall(forward_window)) >= 3
            )
            page_numbered_entry = bool(
                TOC_PAGE_TRAILER_RE.search(line)
                and not BARE_ITEM_HEADING_RE.fullmatch(line)
            )
            pattern = CANONICAL_BOUNDARY_PATTERNS.get(code)
            canonical_body_heading = bool(
                pattern
                and pattern.search(line)
                and not page_numbered_entry
                and not candidate.get(
                    "toc_page_number_after_heading",
                    False,
                )
            )

            if near_preceding_toc and (
                dense_item_sequence or page_numbered_entry
            ) and not canonical_body_heading:
                continue
            retained.append(candidate)
        filtered[code] = retained

    return filtered


@dataclass(frozen=True)
class SectionRecord:
    section_id: str
    company_id: str
    ticker: str
    coverage_year: int
    accession_number: str
    canonical_section_code: str
    section_heading: str
    subsection_heading: str
    source_start_char: int
    source_end_char: int
    section_text_sha256: str
    source_text_sha256: str
    splitter_version: str
    splitter_config_sha256: str
    boundary_method: str
    boundary_confidence: str
    quality_status: str
    quality_flags: tuple[str, ...]
    rag_action: str
    output_file: str


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def splitter_config_sha256() -> str:
    payload = {
        "version": SPLITTER_VERSION,
        "mandatory_major": sorted(MANDATORY_MAJOR),
        "candidate_source": (
            "v2_item_no_plus_renderer_variants_plus_multiline_canonical"
        ),
        "selection": "global_monotonic_dynamic_programming",
        "fallback_policy": "reject_no_synthesis",
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def trimmed_span(text: str, start: int, end: int) -> tuple[int, int, str]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end, text[start:end]


def boundary_confidence(candidate: dict[str, object]) -> str:
    score = int(candidate.get("score", 0))
    code = str(candidate.get("code", ""))
    line = str(candidate.get("line", ""))
    canonical = bool(
        CANONICAL_BOUNDARY_PATTERNS.get(
            code,
            re.compile(r"(?!x)x"),
        ).search(line)
    )

    if (
        canonical
        and not candidate.get("title_only")
        and not candidate.get("toc_page_number_after_heading", False)
    ):
        return "high"
    if score >= 85 and not candidate.get("title_only"):
        return "high"
    if score >= 85:
        return "medium"
    return "low"


def section_quality(code: str, text: str) -> tuple[str, list[str], str]:
    flags = []
    rag_action = "include"
    if code == "HEADER":
        flags.append("header_archival_only")
        rag_action = "exclude"
    if code == "Signatures" or SIGNATURE_RE.search(text):
        flags.append("signature_content")
        rag_action = "exclude"
    if code == "Item_6":
        flags.append("item_6_non_rag")
        rag_action = "exclude"
        if RESERVED_RE.search(text[:400]):
            flags.append("reserved_item_6")
    toc_count = len(STANDALONE_TOC_RE.findall(text))
    if toc_count:
        flags.append("contains_toc_running_header")
    if AUDITOR_RE.search(text):
        flags.append("contains_auditor_report")
    if "\ufffd" in text:
        flags.append("replacement_character")
    status = "failed" if "replacement_character" in flags else "passed"
    return status, flags, rag_action




def add_reversed_item_7a_candidates(
    text: str,
    candidates: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Recognize `Qualitative and Quantitative` Item 7A headings."""
    lines = text.splitlines(keepends=True)
    positions = []
    position = 0

    for line in lines:
        positions.append(position)
        position += len(line)

    existing_positions = {
        int(candidate["position"])
        for candidate in candidates.get("Item_7A", [])
    }

    for index in range(len(lines)):
        values = []

        for next_index in range(index, min(index + 7, len(lines))):
            value = lines[next_index].strip()
            if value:
                values.append(value)
            if len(values) >= 4:
                break

        combined = " ".join(values)

        if (
            positions[index] not in existing_positions
            and REVERSED_ITEM_7A_RE.search(combined)
        ):
            candidates.setdefault("Item_7A", []).append(
                {
                    "code": "Item_7A",
                    "position": positions[index],
                    "line_number": index + 1,
                    "score": 100,
                    "line": combined,
                    "method": "reversed_item_7a_title_alias",
                    "toc_cluster": False,
                    "toc_page_number_after_heading": False,
                }
            )
            existing_positions.add(positions[index])

    return candidates

def add_singular_item_8_candidates(
    text: str,
    candidates: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Recognize the observed LIVE singular Item 8 heading."""
    position = 0
    existing_positions = {
        int(candidate["position"])
        for candidate in candidates.get("Item_8", [])
    }

    for line_number, line in enumerate(
        text.splitlines(keepends=True),
        1,
    ):
        cleaned = line.strip()

        if (
            position not in existing_positions
            and SINGULAR_ITEM_8_RE.fullmatch(cleaned)
        ):
            candidates.setdefault("Item_8", []).append(
                {
                    "code": "Item_8",
                    "position": position,
                    "line_number": line_number,
                    "score": 100,
                    "line": cleaned,
                    "method": "canonical_singular_item_8_alias",
                    "toc_cluster": False,
                    "toc_page_number_after_heading": False,
                }
            )
            existing_positions.add(position)

        position += len(line)

    return candidates


def add_variant_mandatory_candidates(
    text: str,
    candidates: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Recognize recurring SEC renderer/OCR variants without synthesis."""
    lines = text.splitlines(keepends=True)
    positions = []
    position = 0
    for line in lines:
        positions.append(position)
        position += len(line)

    existing = {
        (code, int(candidate["position"]))
        for code, values in candidates.items()
        for candidate in values
    }

    for index, line in enumerate(lines):
        first_line = line.strip()
        if not first_line or TOC_PAGE_TRAILER_RE.search(first_line):
            continue

        for code, pattern in VARIANT_MANDATORY_PATTERNS.items():
            key = (code, positions[index])
            if key in existing or not pattern.search(first_line):
                continue
            candidates.setdefault(code, []).append(
                {
                    "code": code,
                    "position": positions[index],
                    "line_number": index + 1,
                    "score": 100,
                    "line": first_line,
                    "method": "observed_item_heading_variant",
                    "toc_cluster": False,
                    "toc_page_number_after_heading": False,
                }
            )
            existing.add(key)

    return candidates


def promote_multiline_canonical_candidates(
    text: str,
    candidates: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Use continuation lines when a canonical Item title wraps."""
    for code, values in candidates.items():
        pattern = CANONICAL_BOUNDARY_PATTERNS.get(code)
        if pattern is None:
            continue

        for candidate in values:
            position = int(candidate["position"])
            line = str(candidate.get("line", "")).strip()
            if (
                TOC_PAGE_TRAILER_RE.search(line)
                or candidate.get(
                    "toc_page_number_after_heading",
                    False,
                )
                or pattern.search(line)
            ):
                continue

            continuation = []
            for next_line in text[position:].splitlines()[:6]:
                value = next_line.strip()
                if value:
                    continuation.append(value)
                combined = " ".join(continuation)
                if pattern.search(combined) or len(continuation) >= 3:
                    break

            if not pattern.search(combined):
                continue
            candidate["line"] = combined
            candidate["score"] = max(
                100,
                int(candidate.get("score", 0)),
            )
            candidate["toc_cluster"] = False
            candidate["method"] = "multiline_canonical_item_heading"

    return candidates

def selected_boundaries(text: str) -> tuple[list[dict[str, object]], dict[str, int]]:
    candidates = collect_item_candidates_v2(text)
    candidates = add_reversed_item_7a_candidates(text, candidates)
    candidates = add_singular_item_8_candidates(text, candidates)
    candidates = add_variant_mandatory_candidates(text, candidates)
    candidates = promote_multiline_canonical_candidates(text, candidates)
    candidates = filter_toc_candidates(text, candidates)

    for values in candidates.values():
        for candidate in values:
            code = str(candidate.get("code", ""))
            line = str(candidate.get("line", ""))
            pattern = CANONICAL_BOUNDARY_PATTERNS.get(code)

            if (
                pattern
                and pattern.search(line)
                and not candidate.get("title_only")
                and not candidate.get(
                    "toc_page_number_after_heading",
                    False,
                )
            ):
                candidate["score"] = max(
                    100,
                    int(candidate.get("score", 0)),
                )
                candidate["toc_cluster"] = False

    selected = _select_ordered_candidates(candidates)
    if selected:
        signature = _find_signature_candidate(text, int(selected[-1]["position"]))
        if signature:
            selected.append(signature)
    selected.sort(key=lambda row: int(row["position"]))
    duplicates = {
        code: len(values) for code, values in candidates.items() if len(values) > 1
    }
    return selected, duplicates


def run(
    parsed_root: Path, output_root: Path, expected_documents: int = 561
) -> dict[str, object]:
    parsed_manifest = parsed_root / "parsed_documents.jsonl"
    documents = read_jsonl(parsed_manifest)
    if len(documents) != expected_documents:
        raise ValueError(
            f"expected {expected_documents} parsed documents, found {len(documents)}"
        )
    staging = output_root.with_name(output_root.name + ".staging")
    if output_root.exists() or staging.exists():
        raise FileExistsError(f"refusing existing output/staging root: {output_root}")
    staging_files = staging / "10k"
    staging_files.mkdir(parents=True)
    records: list[SectionRecord] = []
    document_summary = []
    status_counts = Counter()
    config_hash = splitter_config_sha256()
    for document in documents:
        accession = str(document["accession_number"])
        ticker = str(document["ticker"])
        path = parsed_root / "html_text" / f"{ticker}__10-K__{accession}.txt"
        text = path.read_text(encoding="utf-8")
        source_hash = sha256_text(text)
        if source_hash != document["text_sha256"]:
            raise RuntimeError(f"parsed-text hash mismatch: {path}")
        selected, duplicate_candidates = selected_boundaries(text)
        selected_codes = {str(row["code"]) for row in selected}
        missing = sorted(MANDATORY_MAJOR - selected_codes)
        low_confidence = sorted(
            str(row["code"])
            for row in selected
            if str(row["code"]) in MANDATORY_MAJOR
            and boundary_confidence(row) == "low"
        )
        document_status = (
            "passed"
            if not missing and not low_confidence and document["parse_status"] == "passed"
            else "review_required"
        )
        status_counts[document_status] += 1
        boundaries = []
        if selected and int(selected[0]["position"]) > 0:
            boundaries.append(
                {
                    "code": "HEADER",
                    "position": 0,
                    "score": 100,
                    "line": "",
                    "method": "document_prefix",
                }
            )
        boundaries.extend(selected)
        for index, candidate in enumerate(boundaries):
            start = int(candidate["position"])
            end = (
                int(boundaries[index + 1]["position"])
                if index + 1 < len(boundaries)
                else len(text)
            )
            start, end, section_text = trimmed_span(text, start, end)
            if not section_text:
                continue
            code = str(candidate["code"])
            section_status, flags, rag_action = section_quality(code, section_text)
            if document_status != "passed":
                flags.append("document_boundary_review_required")
                if rag_action == "include":
                    rag_action = "review_required"
                section_status = "review_required"
            confidence = (
                "high" if code == "HEADER" else boundary_confidence(candidate)
            )
            method = str(
                candidate.get(
                    "method",
                    "title_only_candidate"
                    if candidate.get("title_only")
                    else "canonical_item_candidate",
                )
            )
            section_id = f"{ticker}-{document['coverage_year']}-{accession}-{code}"
            relative_file = Path("10k") / f"{ticker}__10-K__{accession}__{code}.txt"
            output_path = staging / relative_file
            temporary = output_path.with_suffix(".txt.tmp")
            temporary.write_text(section_text, encoding="utf-8")
            temporary.replace(output_path)
            if text[start:end] != section_text:
                raise RuntimeError(f"source-offset reconstruction failed: {section_id}")
            records.append(
                SectionRecord(
                    section_id=section_id,
                    company_id=str(document["company_id"]),
                    ticker=ticker,
                    coverage_year=int(document["coverage_year"]),
                    accession_number=accession,
                    canonical_section_code=code,
                    section_heading=str(candidate.get("line", "")),
                    subsection_heading="",
                    source_start_char=start,
                    source_end_char=end,
                    section_text_sha256=sha256_text(section_text),
                    source_text_sha256=source_hash,
                    splitter_version=SPLITTER_VERSION,
                    splitter_config_sha256=config_hash,
                    boundary_method=method,
                    boundary_confidence=confidence,
                    quality_status=section_status,
                    quality_flags=tuple(sorted(set(flags))),
                    rag_action=rag_action,
                    output_file=str(output_root / relative_file),
                )
            )
        document_summary.append(
            {
                "ticker": ticker,
                "coverage_year": int(document["coverage_year"]),
                "accession_number": accession,
                "status": document_status,
                "selected_codes": sorted(selected_codes),
                "missing_mandatory": missing,
                "low_confidence_mandatory": low_confidence,
                "duplicate_candidate_counts": duplicate_candidates,
            }
        )
    with (staging / "sections.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    with (staging / "document_section_status.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in document_summary:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    staging.replace(output_root)
    return {
        "documents": len(documents),
        "sections": len(records),
        "document_status_counts": dict(sorted(status_counts.items())),
        "review_required_accessions": [
            row["accession_number"]
            for row in document_summary
            if row["status"] != "passed"
        ],
        "output_root": str(output_root),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parsed-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-documents", type=int, default=561)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.parsed_root, args.output_root, args.expected_documents),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
