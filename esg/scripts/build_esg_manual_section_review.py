#!/usr/bin/env python3
"""Build the fixed manual-review evidence file for the isolated ESG candidate."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


REVIEW_SPECS = [
    ("known_regression", "SVV", "SVV-Savers Value Village-2023", "community__0002", "REVIEW", "false_subsection_heading", "The target heading is present, but three sentence or list fragments were also promoted to subsection labels."),
    ("known_regression", "ORLY", "ORLY-O'REILLY AUTOMOTIVE INC-2024", "community__0001", "PASS", "", "The target Feeding Our Communities Partners label reaches the right chunk; the surrounding community text is readable."),
    ("known_regression", "TPR", "TPR-TAPESTRY INC-2024", "community__0005", "PASS", "", "Blue Star Families is a clear heading and the section remains on the same community topic."),
    ("known_regression", "DELL", "DELL-DELL TECHNOLOGIES INC-2023", "supply_chain_ethics__0007", "PASS", "", "The target supply-chain heading and its short narrative are clean and complete."),
    ("known_regression", "LOW", "LOW-LOWE'S COS INC-2023", "emissions__0002", "FAIL", "section_boundary_contamination", "The section starts mid-thought and mixes emissions, waste, governance, ethics, and data security under one emissions label."),
    ("known_regression", "SHOO", "SHOO-MADDEN STEVEN LTD-2023", "governance__0003", "PASS", "", "The target governance heading is clean and the short text stays on CSR program management."),
    ("excess_overlap", "DXLG", "DXLG-DESTINATION XL GROUP INC-2024", "other__0001", "FAIL", "navigation_and_excess_overlap", "A table-of-contents preamble is split with repeated text above the overlap budget. All chunks are correctly excluded from the ESG index."),
    ("excess_overlap", "EBAY", "EBAY-EBAY INC-2024", "ceo_letter__0001", "FAIL", "compound_front_matter_and_excess_overlap", "The section joins several front-matter messages and repeats a terminal span. All chunks are correctly excluded from the ESG index."),
    ("excess_overlap", "FND", "FND-FLOOR & DECOR HLDGS-2023", "governance__0001", "FAIL", "navigation_and_excess_overlap", "The section begins with a long table of contents and has two over-budget overlaps. All chunks are correctly excluded from the ESG index."),
    ("excess_overlap", "GES", "GES-GUESS-2024", "other__0001", "FAIL", "navigation_and_excess_overlap", "A long preamble and table of contents creates two over-budget overlaps. All chunks are correctly excluded from the ESG index."),
    ("excess_overlap", "NWL", "NWL-NEWELL BRANDS INC-2023", "other__0001", "FAIL", "navigation_and_excess_overlap", "The preamble is mostly navigation text and has two over-budget overlaps. All chunks are correctly excluded from the ESG index."),
    ("internal_transitions", "ABG", "ABG-ASBURY AUTOMOTIVE GROUP INC-2023", "community__0005", "REVIEW", "list_items_as_subsections", "The community text is useful, but named partner bullets were treated as subsection headings."),
    ("internal_transitions", "BBY", "BBY-BEST BUY CO INC-2023", "climate__0001", "REVIEW", "sentence_fragment_as_subsection", "The climate section is readable, but one Climate Pledge sentence fragment was promoted to a subsection label."),
    ("internal_transitions", "BIRD", "BIRD-ALLBIRDS INC-2023", "human_capital__0003", "REVIEW", "table_rows_as_subsections", "The data is useful, but table prompts and sentence fragments become subsection labels."),
    ("internal_transitions", "BJ", "BJ-BJS WHSL CLUB HLDGS INC-2023", "climate__0005", "REVIEW", "citation_labels_as_subsections", "Repeated CDP response references were treated as internal headings, making context labels noisy."),
    ("unknown_subsection", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023", "data_summary__0001", "REVIEW", "unknown_context_and_table_order", "The metrics are mostly recoverable, but chart order is dense and the candidate already routes the chunks to manual review."),
    ("unknown_subsection", "AN", "AN-AUTONATION INC-2023", "diversity_equity_inclusion__0003", "REVIEW", "section_boundary_contamination", "A workforce representation table runs into a separate SASB activity-metrics section."),
    ("unknown_subsection", "BRLT", "BRLT-BRILLIANT EARTH GP INC-2023", "appendix__0001", "FAIL", "severe_reading_order_corruption", "Text from columns and page furniture is interleaved, so the section is hard to read and unsafe for retrieval as-is."),
    ("short_section", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023", "water__0002", "REVIEW", "orphan_table", "The values are readable, but the short table lacks enough nearby narrative and is already routed to manual review."),
    ("short_section", "AMZN", "AMZN-AMAZON.COM INC-2023", "energy__0004", "FAIL", "orphan_table_rows", "The section contains only partial emissions table rows without the column headers or units needed to interpret the numbers."),
    ("short_section", "ACI", "ACI-ALBERTSONS COS INC-2023", "diversity_equity_inclusion__0001", "PASS", "", "A short but complete inclusion narrative with a clear heading and stable topic."),
    ("high_ordinal", "ABG", "ABG-ASBURY AUTOMOTIVE GROUP INC-2024", "human_capital__0010", "REVIEW", "multi_table_reading_order", "Training, hiring, wage, and board-diversity values are mixed by page order, so exact numeric use needs review."),
    ("high_ordinal", "CASY", "CASY-CASEYS GENERAL STORES INC-2023", "appendix__0010", "REVIEW", "multi_column_reading_order", "The SDG mappings are understandable, but some sentences are interleaved by the source page layout."),
    ("high_ordinal", "DELL", "DELL-DELL TECHNOLOGIES INC-2023", "data_summary__0010", "FAIL", "severe_table_extraction_corruption", "Repeated characters make the 30 eligible chunks unsafe for retrieval without a table-specific repair or hold."),
    ("table_appendix_risk", "BBWI", "BBWI-BATH & BODY WORKS INC-2023", "appendix__0001", "FAIL", "wrong_physical_section_boundary", "The section labeled Appendix starts on page 2 and includes the CEO message and other front matter through page 5."),
    ("table_appendix_risk", "BIRD", "BIRD-ALLBIRDS INC-2023", "about_this_report__0002", "REVIEW", "table_and_footnote_order", "The SASB values are useful, but disclosure prompts and footnotes are interleaved and need careful numeric use."),
    ("table_appendix_risk", "BJ", "BJ-BJS WHSL CLUB HLDGS INC-2023", "about_this_report__0006", "PASS", "", "The SASB table keeps its header, codes, topics, and values in a readable structure."),
]

# A section instance ID is only stable while all earlier same-code boundaries
# remain stable. Guard reviews whose motivating boundary is expected to
# disappear after a repair, so the old judgment cannot attach to a different
# section that later inherits the same ordinal ID.
REVIEW_SECTION_TITLE_GUARDS = {
    (
        "AMZN",
        "AMZN-AMAZON.COM INC-2023",
        "energy__0004",
    ): "| Fuel- and Energy-Related Activities | | 4.76 | 4.97 |",
    (
        "BBWI",
        "BBWI-BATH & BODY WORKS INC-2023",
        "appendix__0001",
    ): "Appendix 59",
}


def review_matches_current_section(key: tuple[str, str, str], section: dict[str, str]) -> bool:
    expected_title = REVIEW_SECTION_TITLE_GUARDS.get(key)
    return expected_title is None or section.get("section_title") == expected_title


def compact(text: str, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def load_subsection_spans(section: dict[str, str]) -> list[dict]:
    """Read optional subsection metadata from old or new section indexes."""
    raw = section.get("subsection_spans_json") or "[]"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--sections-index",
        type=Path,
        default=None,
        help="Defaults to esg_sections_index.csv inside --candidate-dir.",
    )
    parser.add_argument(
        "--chunks-index",
        type=Path,
        default=None,
        help="Defaults to esg_chunks_index_final_v2.csv inside --candidate-dir.",
    )
    args = parser.parse_args()

    sections_index = args.sections_index or (
        args.candidate_dir / "esg_sections_index.csv"
    )
    chunks_index = args.chunks_index or (
        args.candidate_dir / "esg_chunks_index_final_v2.csv"
    )
    with sections_index.open(encoding="utf-8-sig", newline="") as handle:
        section_rows = list(csv.DictReader(handle))
    with chunks_index.open(encoding="utf-8-sig", newline="") as handle:
        chunk_rows = list(csv.DictReader(handle))

    sections = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"]): row
        for row in section_rows
    }
    chunks: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in chunk_rows:
        key = (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        chunks.setdefault(key, []).append(row)

    output_rows = []
    for reason, ticker, stem, section_id, judgment, issue_type, notes in REVIEW_SPECS:
        key = (ticker, stem, section_id)
        section = sections.get(key)
        if section is None:
            print(
                "skipped missing manual review target: "
                f"{ticker}/{stem}/{section_id}"
            )
            continue
        if not review_matches_current_section(key, section):
            print(
                "skipped stale manual review: "
                f"{ticker}/{stem}/{section_id} now titles {section['section_title']!r}"
            )
            continue
        section_chunks = chunks.get(key, [])
        if not section_chunks:
            print(
                "skipped manual review target without chunks: "
                f"{ticker}/{stem}/{section_id}"
            )
            continue
        spans = load_subsection_spans(section)
        section_path = Path(section["section_file"])
        text = section_path.read_text(encoding="utf-8", errors="replace")
        include_counts = Counter(row["include_in_esg_index"] for row in section_chunks)
        rag_counts = Counter(row["rag_action"] for row in section_chunks)
        output_rows.append(
            {
                "ticker": ticker,
                "pdf_stem": stem,
                "section_instance_id": section_id,
                "sample_reason": reason,
                "section_code": section["section_code"],
                "physical_title": section["section_title"],
                "char_count": section["char_count"],
                "page_start": section["page_start"],
                "page_end": section["page_end"],
                "subsection_span_count": len(spans),
                "subsection_titles": " | ".join(
                    str(span.get("title", "")) for span in spans
                ),
                "chunk_count": len(section_chunks),
                "include_in_esg_index_counts": json.dumps(include_counts, sort_keys=True),
                "rag_action_counts": json.dumps(rag_counts, sort_keys=True),
                "text_excerpt": compact(text),
                "judgment": judgment,
                "issue_type": issue_type,
                "notes": notes,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"wrote {len(output_rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
