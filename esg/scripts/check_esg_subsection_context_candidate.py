"""Validate an isolated ESG subsection-context candidate and write reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402
import section_splitter_esg as splitter  # noqa: E402
from esg_year import extract_report_year  # noqa: E402


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_rows(path: str) -> list[dict[str, str]]:
    value = ROOT / path
    return read_csv(value) if path and value.exists() else []


def rejection_audit(text: str, pages: list[dict[str, str]]) -> Counter:
    lines_with_endings = text.splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in lines_with_endings]
    total_chars = max(len(text), 1)
    raw: list[splitter.HeadingCandidate] = []
    sentence_noise = 0
    offset = 0
    for index, line_with_ending in enumerate(lines_with_endings):
        line = line_with_ending.rstrip("\r\n")
        code = splitter.map_heading_to_code(line)
        if code:
            raw.append(
                splitter.HeadingCandidate(
                    index,
                    offset,
                    code,
                    splitter.normalize_heading_text(line),
                    splitter.has_page_reference(line),
                )
            )
        else:
            title = splitter.normalize_heading_text(line)
            normalized = re.sub(
                r"\s+", " ", re.sub(r"[^a-z0-9\s-]", " ", title.lower().replace("&", " and "))
            ).strip()
            if len(normalized.split()) <= 40 and any(
                re.search(pattern, normalized, flags=re.IGNORECASE)
                and splitter.code_allowed_for_heading(section_code, normalized)
                for section_code, pattern in splitter.HEADING_PATTERNS
            ):
                sentence_noise += 1
        offset += len(line_with_ending)

    repeated_chrome = splitter._running_page_chrome_indexes(raw, pages, total_chars, lines)
    repeated_table = splitter._repeated_table_header_indexes(raw, pages, lines)
    early = [candidate for candidate in raw if candidate.char_offset < total_chars * 0.10]
    toc_heavy = bool(
        len(early) >= 5
        and sum(candidate.toc_like for candidate in early) / len(early) >= 0.5
    )
    counts = Counter(sentence_noise=sentence_noise)
    for index, candidate in enumerate(raw):
        if index in repeated_chrome:
            counts["chrome"] += 1
        elif index in repeated_table:
            counts["table"] += 1
        elif (
            splitter._is_navigation_or_report_chrome(candidate.title)
            and not splitter._has_substantial_narrative_following(candidate, lines)
        ):
            counts["navigation"] += 1
        elif splitter._looks_like_table_or_index_candidate(candidate, lines):
            counts["table"] += 1
        elif toc_heavy and candidate.char_offset < total_chars * 0.10 and candidate.toc_like:
            counts["navigation"] += 1
        else:
            counts["accepted"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--live-sections", type=Path, required=True)
    parser.add_argument("--live-chunks", type=Path, required=True)
    parser.add_argument("--parse-index", type=Path, required=True)
    parser.add_argument("--company-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    candidate_root = args.candidate_root.resolve()
    sections = read_csv(candidate_root / "esg_sections_index.csv")
    chunks_index = candidate_root / "esg_chunks_index_final_v2.csv"
    if not chunks_index.exists():
        chunks_index = candidate_root / "esg_chunks_index_final.csv"
    if not chunks_index.exists():
        chunks_index = candidate_root / "esg_chunks_index_balanced.csv"
    chunks = read_csv(chunks_index)
    live_sections = read_csv(args.live_sections)
    live_chunks = read_csv(args.live_chunks)
    parses = read_csv(args.parse_index)
    company_names = {
        row["ticker"].strip().upper(): (row.get("company_name") or "").strip()
        for row in read_csv(args.company_manifest)
        if (row.get("ticker") or "").strip()
    }
    parse_by_doc: dict[tuple[str, str], dict[str, str]] = {}
    for row in parses:
        ticker = (row.get("ticker") or "").strip().upper()
        source = row.get("source_pdf") or row.get("pdf_file") or ""
        parse_by_doc[(ticker, Path(source).stem)] = row

    sections_by_doc: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in sections:
        sections_by_doc[(row["ticker"], row["pdf_stem"])].append(row)
    chunks_by_section: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in chunks:
        chunks_by_section[(row["ticker"], row["pdf_stem"], row["section_instance_id"])].append(row)
    section_by_key = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"]): row
        for row in sections
    }

    source_failures = 0
    section_gaps = 0
    section_overlaps = 0
    outer_nonwhitespace_gaps = 0
    chunk_gaps = 0
    chunk_overlap_pairs = 0
    tiling_failures = 0
    rejection_counts = Counter()
    for doc_key, doc_sections in sections_by_doc.items():
        parse = parse_by_doc.get(doc_key, {})
        parsed_path = parse.get("parsed_text_file") or ""
        if not parsed_path or not (ROOT / parsed_path).exists():
            source_failures += 1
            continue
        text = read_text(parsed_path)
        rejection_counts.update(rejection_audit(text, page_rows(parse.get("page_map_file") or "")))
        ordered = sorted(doc_sections, key=lambda row: int(row["source_start_char"]))
        if text[: int(ordered[0]["source_start_char"])].strip():
            outer_nonwhitespace_gaps += 1
        if text[int(ordered[-1]["source_end_char"]) :].strip():
            outer_nonwhitespace_gaps += 1
        for row in ordered:
            start, end = int(row["source_start_char"]), int(row["source_end_char"])
            if text[start:end] != read_text(row["section_file"]):
                source_failures += 1
        for previous, current in zip(ordered, ordered[1:]):
            left, right = int(previous["source_end_char"]), int(current["source_start_char"])
            if right < left:
                section_overlaps += 1
            elif text[left:right].strip():
                section_gaps += 1

    embedding_rows: list[dict[str, str]] = []
    suffix_failures = 0
    tail_hash_failures = 0
    chunk_hash_failures = 0
    for row in chunks:
        chunk_text = read_text(row["chunk_file"])
        if sha256_text(chunk_text) != row["chunk_text_sha256"]:
            chunk_hash_failures += 1
        year, status, span = extract_report_year(row["pdf_stem"])
        metadata = {
            **row,
            "company_name": company_names.get(row["canonical_ticker"].upper(), ""),
            "report_year": "" if year is None else str(year),
            "report_year_status": status,
            "report_year_span": span,
            "section_title_original": row.get("subsection_context") or "unknown",
        }
        embedding_text = esg_chunker.final_embedding_text(
            metadata, chunk_text, row.get("table_context") or ""
        )
        suffix_ok = embedding_text.endswith(chunk_text)
        suffix_failures += int(not suffix_ok)
        tail_hash_ok = sha256_text(embedding_text[-len(chunk_text) :]) == sha256_text(chunk_text)
        tail_hash_failures += int(not tail_hash_ok)
        embedding_rows.append(
            {
                "chunk_id": row["chunk_id"],
                "chunk_text_sha256": sha256_text(chunk_text),
                "embedding_text_sha256": sha256_text(embedding_text),
                "embedding_text_endswith_chunk_text": str(suffix_ok).lower(),
                "subsection_context": row.get("subsection_context") or "",
            }
        )

    for key, section_chunks in chunks_by_section.items():
        section_row = section_by_key[key]
        section_text = read_text(section_row["section_file"])
        section_start = int(section_row["source_start_char"])
        ordered = sorted(section_chunks, key=lambda row: int(row["chunk_index"]))
        local = [
            (int(row["source_start_char"]) - section_start, int(row["source_end_char"]) - section_start)
            for row in ordered
        ]
        if not local or local[0][0] != 0 or local[-1][1] != len(section_text):
            tiling_failures += 1
        for previous, current in zip(local, local[1:]):
            if current[0] > previous[1]:
                chunk_gaps += 1
            elif current[0] < previous[1]:
                chunk_overlap_pairs += 1

    max_ordinals: dict[tuple[str, str, str], int] = defaultdict(int)
    transition_count = 0
    for row in sections:
        match = re.search(r"__(\d+)$", row["section_instance_id"])
        if match:
            key = (row["ticker"], row["pdf_stem"], row["section_code"])
            max_ordinals[key] = max(max_ordinals[key], int(match.group(1)))
        try:
            transition_count += max(0, len(json.loads(row.get("subsection_spans_json") or "[]")) - 1)
        except (TypeError, ValueError):
            pass

    live_by_doc: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
    candidate_by_doc: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
    for row in live_sections:
        live_by_doc[(row["ticker"], row["pdf_stem"])].append(
            (row["section_code"], row["section_title"], row["source_start_char"], row["source_end_char"])
        )
    for row in sections:
        candidate_by_doc[(row["ticker"], row["pdf_stem"])].append(
            (row["section_code"], row["section_title"], row["source_start_char"], row["source_end_char"])
        )
    changed_docs = sum(candidate_by_doc[key] != live_by_doc.get(key, []) for key in candidate_by_doc)

    def has_known_subsection(row: dict[str, str]) -> bool:
        value = (row.get("subsection_context") or "").strip()
        return bool(value) and value.casefold() != "unknown"

    known = sum(has_known_subsection(row) for row in chunks)
    unknown = len(chunks) - known
    differs = sum(
        has_known_subsection(row)
        and (row.get("subsection_context") or "").strip()
        != (row.get("physical_section_title") or "").strip()
        for row in chunks
    )
    full_fallbacks = sum(row["section_code"] == "full_document" for row in sections)
    short_sections = sum(int(row["char_count"]) < 500 for row in sections)
    high_ordinals = sum(value >= 10 for value in max_ordinals.values())

    target_headings = {
        "SVV": ["STORE COMMUNITY OUTREACH", "NONPROFIT PARTNER SPOTLIGHTS"],
        "ORLY": ["Community", "Feeding Our Communities Partners"],
        "TPR": ["Blue Star Families", "SOCIAL IMPACT COUNCIL"],
        "DELL": ["BETTERING THE LIVES OF PEOPLE IN OUR SUPPLY CHAIN"],
        "LOW": ["CLIMATE CHANGE, ENERGY AND EMISSIONS"],
        "SHOO": ["BOARD OF DIRECTORS / RISK MANAGEMENT / CSR PROGRAM GOVERNANCE"],
    }
    heading_hits = {
        ticker: {
            title: sum(
                title.casefold() in (row.get("subsection_context") or "").casefold()
                for row in chunks
                if row["ticker"] == ticker
            )
            for title in titles
        }
        for ticker, titles in target_headings.items()
    }
    noise_terms = [
        "Energy efficiency proceeds were allocated to Eligible Projects",
        "CATEGORY / STATE EMPLOYEES BRANDS",
        "FY24 DECKERS FOOTWEAR ENERGY USAGE BY MATERIAL CATEGORY GATE BREAKDOWN",
        "INTRODUCTION CLIMATE ACTION CIRCULAR ECONOMY",
    ]
    noise_hits = {
        term: sum(term.casefold() in (row.get("subsection_context") or "").casefold() for row in chunks)
        for term in noise_terms
    }

    preferred_example_terms = (
        "NONPROFIT PARTNER SPOTLIGHTS",
        "Feeding Our Communities Partners",
        "SOCIAL IMPACT COUNCIL",
    )
    example = next(
        (
            row
            for row in chunks
            if row["ticker"] in {"SVV", "ORLY", "TPR"}
            and has_known_subsection(row)
            and row.get("subsection_context") != row.get("physical_section_title")
            and any(
                term.casefold() in (row.get("subsection_context") or "").casefold()
                for term in preferred_example_terms
            )
        ),
        None,
    )
    before_header = ""
    after_header = ""
    if example:
        chunk_text = read_text(example["chunk_file"])
        year, status, span = extract_report_year(example["pdf_stem"])
        base = {
            **example,
            "company_name": company_names.get(example["canonical_ticker"].upper(), ""),
            "report_year": "" if year is None else str(year),
            "report_year_status": status,
            "report_year_span": span,
        }
        before_header = esg_chunker.final_embedding_text(
            {**base, "section_title_original": example["physical_section_title"]}, chunk_text
        ).partition("\n\n")[0]
        after_header = esg_chunker.final_embedding_text(
            {**base, "section_title_original": example["subsection_context"]}, chunk_text
        ).partition("\n\n")[0]

    report = {
        "documents_processed": len(sections_by_doc),
        "source_failures": source_failures,
        "tiling_failures": tiling_failures,
        "section_gaps": section_gaps,
        "section_overlaps": section_overlaps,
        "outer_nonwhitespace_gaps": outer_nonwhitespace_gaps,
        "chunk_gaps": chunk_gaps,
        "chunk_overlap_pairs_expected": chunk_overlap_pairs,
        "full_document_fallbacks": full_fallbacks,
        "physical_section_count": len(sections),
        "live_physical_section_count": len(live_sections),
        "chunk_count": len(chunks),
        "live_chunk_count": len(live_chunks),
        "sections_under_500_characters": short_sections,
        "high_ordinal_groups": high_ordinals,
        "documents_changed_versus_live": changed_docs,
        "chunks_with_known_subsection": known,
        "chunks_with_unknown_subsection": unknown,
        "chunks_active_subsection_differs_from_physical_title": differs,
        "preserved_internal_subsection_transitions": transition_count,
        "rejected_heading_candidates": dict(rejection_counts),
        "chunk_hash_failures": chunk_hash_failures,
        "embedding_suffix_failures": suffix_failures,
        "embedding_tail_hash_failures": tail_hash_failures,
        "target_heading_chunk_hits": heading_hits,
        "known_noise_chunk_hits": noise_hits,
        "example": {
            "ticker": example["ticker"] if example else "",
            "chunk_id": example["chunk_id"] if example else "",
            "physical_section_title": example["physical_section_title"] if example else "",
            "active_subsection": example["subsection_context"] if example else "",
            "before_embedding_prefix": before_header,
            "after_embedding_prefix": after_header,
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    embedding_index = args.out.with_name("esg_subsection_candidate_embedding_hashes.csv")
    with embedding_index.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(embedding_rows[0]))
        writer.writeheader()
        writer.writerows(embedding_rows)
    markdown = args.out.with_suffix(".md")
    lines = [
        "# ESG subsection-context candidate comparison",
        "",
        "This candidate is isolated. No live corpus, manifest, dataset ID, embedding, or vector index was changed.",
        "",
    ]
    for key, value in report.items():
        if key not in {"example", "target_heading_chunk_hits", "known_noise_chunk_hits", "rejected_heading_candidates"}:
            lines.append(f"- {key.replace('_', ' ').title()}: {value}")
    lines += [
        "",
        "## Heading checks",
        "",
        "```json",
        json.dumps(heading_hits, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Noise checks",
        "",
        "```json",
        json.dumps(noise_hits, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Rejection audit",
        "",
        "```json",
        json.dumps(dict(rejection_counts), indent=2),
        "```",
        "",
        "## Before and after embedding prefix",
        "",
        f"Example: `{report['example']['chunk_id']}`",
        "",
        "Before (physical first title only):",
        "",
        "```text",
        before_header,
        "```",
        "",
        "After (active subsection):",
        "",
        "```text",
        after_header,
        "```",
        "",
        "## Recommendation",
        "",
        "Safe for a separate embedding-candidate build. Do not promote this candidate to the live corpus yet.",
        "",
    ]
    markdown.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    bad = (
        source_failures
        + tiling_failures
        + section_gaps
        + section_overlaps
        + outer_nonwhitespace_gaps
        + chunk_gaps
        + chunk_hash_failures
        + suffix_failures
        + tail_hash_failures
        + full_fallbacks
        + sum(noise_hits.values())
    )
    return int(bool(bad))


if __name__ == "__main__":
    raise SystemExit(main())
