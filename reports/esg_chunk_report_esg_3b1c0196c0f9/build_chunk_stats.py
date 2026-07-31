from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median


ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "00_reference" / "esg_chunks_index_enriched.csv"
HANDOFF = ROOT / "outputs" / "esg_chunk_handoff_2000_esg_3b1c0196c0f9" / "chunks.csv"
OUT = Path(__file__).with_name("chunk_stats.json")


def percentile(values: list[int], fraction: float) -> int:
    values = sorted(values)
    if not values:
        return 0
    index = (len(values) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return values[low]
    return round(values[low] * (high - index) + values[high] * (index - low))


def token_summary(values: list[int]) -> dict:
    return {
        "min": min(values),
        "p25": percentile(values, 0.25),
        "median": round(median(values)),
        "mean": round(mean(values), 1),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def bucket_tokens(values: list[int]) -> dict:
    buckets = Counter()
    for value in values:
        if value <= 100:
            buckets["51–100"] += 1
        elif value <= 250:
            buckets["101–250"] += 1
        elif value <= 400:
            buckets["251–400"] += 1
        else:
            buckets["401–500"] += 1
    return dict(buckets)


def page_span(row: dict) -> int:
    return int(row["page_end"]) - int(row["page_start"]) + 1


def summarize_live() -> dict:
    tokens: list[int] = []
    chars: list[int] = []
    spans: list[int] = []
    docs = Counter()
    sections = Counter()
    tiers = Counter()
    chunk_types = Counter()
    topics = Counter()
    tickers = set()
    companies = set()
    citation_status = Counter()
    per_section_spans: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)

    with LIVE.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            token = int(row["token_count"])
            tokens.append(token)
            chars.append(int(row["char_count"]))
            spans.append(page_span(row))
            docs[row["source_id"]] += 1
            section_key = (row["source_id"], row["section_instance_id"])
            sections[section_key] += 1
            tiers[row["chunk_quality_tier"] or "unknown"] += 1
            chunk_types[row["chunk_type"] or "unknown"] += 1
            topics[row["section_code"] or "unknown"] += 1
            tickers.add(row["canonical_ticker"] or row["ticker"])
            companies.add(row["company_name"])
            citation_status[row["citation_validation_status"] or "unknown"] += 1
            per_section_spans[section_key].append((int(row["source_start_char"]), int(row["source_end_char"])))

    overlapping_pairs = 0
    adjacent_pairs = 0
    overlap_chars = 0
    for section_rows in per_section_spans.values():
        ordered = sorted(section_rows)
        for left, right in zip(ordered, ordered[1:]):
            adjacent_pairs += 1
            overlap = max(0, left[1] - right[0])
            if overlap:
                overlapping_pairs += 1
                overlap_chars += overlap

    span_counts = Counter("1 page" if s == 1 else "2 pages" if s == 2 else "3+ pages" for s in spans)
    return {
        "chunks": len(tokens),
        "documents": len(docs),
        "companies": len(companies),
        "tickers": len(tickers),
        "sections": len(sections),
        "topics": len(topics),
        "tokens": token_summary(tokens),
        "token_buckets": bucket_tokens(tokens),
        "chars": {
            "min": min(chars),
            "median": round(median(chars)),
            "p90": percentile(chars, 0.90),
            "max": max(chars),
        },
        "page_spans": dict(span_counts),
        "multi_page": sum(1 for s in spans if s > 1),
        "quality_tiers": dict(tiers),
        "chunk_types": dict(chunk_types),
        "citation_status": dict(citation_status),
        "chunks_per_document": {
            "min": min(docs.values()),
            "median": round(median(docs.values())),
            "p90": percentile(list(docs.values()), 0.90),
            "max": max(docs.values()),
        },
        "chunks_per_section": {
            "one_chunk_sections": sum(1 for count in sections.values() if count == 1),
            "multi_chunk_sections": sum(1 for count in sections.values() if count > 1),
            "median": round(median(sections.values())),
            "max": max(sections.values()),
        },
        "source_span_overlap": {
            "adjacent_pairs": adjacent_pairs,
            "overlapping_pairs": overlapping_pairs,
            "overlap_chars": overlap_chars,
        },
    }


def summarize_handoff() -> dict:
    tokens: list[int] = []
    chars: list[int] = []
    spans: list[int] = []
    tiers = Counter()
    with HANDOFF.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            tokens.append(int(row["token_count"]))
            chars.append(len(row["chunk_text"]))
            spans.append(page_span(row))
            tiers[row["chunk_quality_tier"]] += 1
    return {
        "chunks": len(tokens),
        "tokens": token_summary(tokens),
        "token_buckets": bucket_tokens(tokens),
        "chars": {
            "min": min(chars),
            "median": round(median(chars)),
            "p90": percentile(chars, 0.90),
            "max": max(chars),
        },
        "multi_page": sum(1 for s in spans if s > 1),
        "page_spans": dict(Counter("1 page" if s == 1 else "2 pages" if s == 2 else "3+ pages" for s in spans)),
        "quality_tiers": dict(tiers),
    }


def main() -> None:
    result = {"live_dataset": summarize_live(), "handoff": summarize_handoff()}
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
