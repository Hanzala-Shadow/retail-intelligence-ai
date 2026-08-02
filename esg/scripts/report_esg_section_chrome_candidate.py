"""Compare live sections with the page-chrome sectioning candidate, read-only."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import section_splitter_esg as splitter  # noqa: E402
import config  # noqa: E402


def legacy_running_page_chrome_indexes(
    candidates: list,
    page_spans: list[dict],
    total_chars: int,
    lines: list[str] | None = None,
) -> set[int]:
    """The contiguous_v1 behavior: keep the first copy in each repeat run."""
    if not page_spans:
        return set()
    positions = splitter._candidate_page_positions(candidates, page_spans)
    by_title: dict[str, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        by_title[splitter._title_key(candidate.title)].append(index)
    rejected: set[int] = set()
    for indexes in by_title.values():
        if len(indexes) < splitter.PAGE_CHROME_MIN_PAGES:
            continue
        title = candidates[indexes[0]].title
        if (
            len(indexes) >= splitter.NAVIGATION_CHROME_MIN_OCCURRENCES
            and splitter._is_navigation_or_report_chrome(title)
        ):
            rejected.update(indexes)
            continue
        paged = [index for index in indexes if index in positions]
        top = [
            index
            for index in paged
            if positions[index][1] <= splitter.PAGE_CHROME_MAX_OFFSET
        ]
        pages = sorted({positions[index][0] for index in paged})
        if len(pages) < splitter.PAGE_CHROME_MIN_PAGES:
            continue
        runs: list[list[int]] = []
        for page in pages:
            if runs and page - runs[-1][-1] <= splitter.PAGE_CHROME_MAX_PAGE_GAP:
                runs[-1].append(page)
            else:
                runs.append([page])
        qualifying = [run for run in runs if len(run) >= splitter.PAGE_CHROME_MIN_PAGES]
        if not qualifying:
            continue
        pages_in_runs = {page for run in qualifying for page in run}
        for run in qualifying:
            run_indexes = [
                index for index in indexes if positions.get(index, (None, None))[0] in run
            ]
            canonical = min(run_indexes, key=lambda index: candidates[index].char_offset)
            rejected.update(index for index in run_indexes if index != canonical)
        first_run_offset = min(
            candidates[index].char_offset
            for index in indexes
            if positions.get(index, (None, None))[0] in pages_in_runs
        )
        rejected.update(
            index
            for index in top
            if candidates[index].char_offset < first_run_offset
            and candidates[index].char_offset < total_chars * 0.10
        )
    return rejected


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def signature(row: dict[str, str]) -> tuple[str, str, int, int]:
    return (
        row.get("section_code", ""),
        row.get("section_title", ""),
        int(row.get("source_start_char") or 0),
        int(row.get("source_end_char") or 0),
    )


def section_shape(rows: list[dict[str, str]]) -> dict[str, int]:
    """Summarize fragmentation signals using the Tier 2 QA thresholds."""
    max_ordinal_by_code: dict[str, int] = defaultdict(int)
    short_sections = 0
    full_document_fallbacks = 0
    for row in rows:
        code = row.get("section_code", "")
        if code == "full_document":
            full_document_fallbacks += 1
        char_count = int(row.get("char_count") or 0)
        if char_count < 500:
            short_sections += 1
        instance_id = row.get("section_instance_id", "")
        ordinal = instance_id.rsplit("__", 1)[-1]
        if ordinal.isdigit():
            max_ordinal_by_code[code] = max(max_ordinal_by_code[code], int(ordinal))
    return {
        "total_sections": len(rows),
        "short_sections_under_500_chars": short_sections,
        "full_document_fallbacks": full_document_fallbacks,
        "doc_code_groups_at_or_above_ordinal_10": sum(
            ordinal >= 10 for ordinal in max_ordinal_by_code.values()
        ),
    }


def check_one(task: tuple[str, str, list[dict[str, str]], str, str]) -> dict:
    text_path_raw, page_map_raw, old_rows, ticker, pdf_stem = task
    text_path = Path(text_path_raw)
    text = text_path.read_text(encoding="utf-8", errors="replace")
    page_rows = read_csv(Path(page_map_raw))
    new_sections = splitter.split_esg_sections(text, page_spans=page_rows)
    promoted_chrome_rule = splitter._running_page_chrome_indexes
    splitter._running_page_chrome_indexes = legacy_running_page_chrome_indexes
    try:
        legacy_sections = splitter.split_esg_sections(text, page_spans=page_rows)
    finally:
        splitter._running_page_chrome_indexes = promoted_chrome_rule
    new_rows = [
        {
            "section_code": section.section_code,
            "section_title": section.title,
            "source_start_char": str(section.source_start_char or 0),
            "source_end_char": str(section.source_end_char or 0),
            "char_count": str(len(section.text)),
            "section_instance_id": section.section_instance_id,
        }
        for section in splitter._assign_section_instance_ids(new_sections)
    ]

    failures: list[str] = []
    for section in new_sections:
        start = section.source_start_char
        end = section.source_end_char
        if start is None or end is None or text[start:end] != section.text:
            failures.append("source_mismatch")
            continue
    for previous, current in zip(new_sections, new_sections[1:]):
        if (
            previous.source_end_char is not None
            and current.source_start_char is not None
            and current.source_start_char < previous.source_end_char
        ):
            failures.append("overlap")
        elif (
            previous.source_end_char is not None
            and current.source_start_char is not None
            and text[previous.source_end_char : current.source_start_char].strip()
        ):
            failures.append("gap")

    old_signatures = [signature(row) for row in old_rows]
    new_signatures = [signature(row) for row in new_rows]
    legacy_rows = [
        {
            "section_code": section.section_code,
            "section_title": section.title,
            "source_start_char": str(section.source_start_char or 0),
            "source_end_char": str(section.source_end_char or 0),
        }
        for section in legacy_sections
    ]
    legacy_signatures = [signature(row) for row in legacy_rows]
    old_titles = Counter(
        (row.get("section_code", ""), row.get("section_title", ""))
        for row in old_rows
    )
    legacy_titles = Counter(
        (row.get("section_code", ""), row.get("section_title", ""))
        for row in legacy_rows
    )
    new_titles = Counter(
        (row.get("section_code", ""), row.get("section_title", ""))
        for row in new_rows
    )
    removed = list((old_titles - new_titles).elements())
    added = list((new_titles - old_titles).elements())
    chrome_removed = list((legacy_titles - new_titles).elements())

    page_number_removed = 0
    for row in legacy_rows:
        title_pair = (row.get("section_code", ""), row.get("section_title", ""))
        if title_pair not in chrome_removed:
            continue
        start = int(row.get("source_start_char") or 0)
        prior_lines = [line.strip() for line in text[:start].splitlines() if line.strip()]
        previous = prior_lines[-1] if prior_lines else ""
        if re.fullmatch(r"(?:p(?:age)?\.?\s*)?\d{1,4}", previous, flags=re.I):
            page_number_removed += 1

    return {
        "ticker": ticker,
        "pdf_stem": pdf_stem,
        "old_sections": len(old_rows),
        "legacy_sections": len(legacy_rows),
        "new_sections": len(new_rows),
        "changed": old_signatures != new_signatures,
        "chrome_rule_changed": legacy_signatures != new_signatures,
        "removed_titles": [f"{code}: {title}" for code, title in removed[:20]],
        "added_titles": [f"{code}: {title}" for code, title in added[:20]],
        "page_number_removed": page_number_removed,
        "before": section_shape(old_rows),
        "after": section_shape(new_rows),
        "failures": sorted(set(failures)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.source_root.resolve()

    parse_rows = read_csv(root / config.as_repo_relative(config.ESG_PARSE_INDEX_CSV))
    section_rows = read_csv(root / config.as_repo_relative(config.ESG_SECTIONS_INDEX_CSV))
    sections_by_doc: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in section_rows:
        sections_by_doc[(row["ticker"], row["pdf_stem"])].append(row)
    for rows in sections_by_doc.values():
        rows.sort(key=lambda row: int(row.get("source_start_char") or 0))

    tasks = []
    for row in parse_rows:
        ticker = row["ticker"]
        pdf_stem = Path(row.get("source_pdf") or row.get("pdf_file") or "").stem
        text_path = root / (row.get("parsed_text_file") or "")
        page_map = root / (row.get("page_map_file") or "")
        if text_path.exists() and page_map.exists():
            tasks.append(
                (
                    str(text_path),
                    str(page_map),
                    sections_by_doc.get((ticker, pdf_stem), []),
                    ticker,
                    pdf_stem,
                )
            )

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(check_one, tasks, chunksize=2))

    changed = [row for row in results if row["changed"]]
    chrome_changed = [row for row in results if row["chrome_rule_changed"]]
    failures = [row for row in results if row["failures"]]
    removed = Counter(
        title for row in results for title in row["removed_titles"]
    )
    report = {
        "sectioner_version": splitter.PROVENANCE_VERSION,
        "documents_checked": len(results),
        "documents_changed_vs_live": len(changed),
        "documents_changed_by_chrome_rule": len(chrome_changed),
        "old_sections": sum(row["old_sections"] for row in results),
        "same_code_legacy_sections": sum(row["legacy_sections"] for row in results),
        "new_sections": sum(row["new_sections"] for row in results),
        "section_delta_vs_live": sum(row["new_sections"] - row["old_sections"] for row in results),
        "section_delta_from_chrome_rule": sum(
            row["new_sections"] - row["legacy_sections"] for row in results
        ),
        "page_number_preceded_headings_removed": sum(row["page_number_removed"] for row in results),
        "source_or_tiling_failures": len(failures),
        "before": {
            key: sum(row["before"][key] for row in results)
            for key in section_shape([])
        },
        "after": {
            key: sum(row["after"][key] for row in results)
            for key in section_shape([])
        },
        "top_added_titles": Counter(
            title for row in results for title in row["added_titles"]
        ).most_common(30),
        "top_removed_titles": removed.most_common(30),
        "changed_examples": chrome_changed[:30],
        "failure_examples": failures[:20],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
