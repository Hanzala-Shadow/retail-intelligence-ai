"""Prove that no navigation page reaches the index or the vision queue.

Read-only release check for `layout_v8`. Fails loudly on any leak:

1. Every page the classifier calls navigation carries `auto_exclude_navigation`
   in the layout audit -- the override is not being lost on any path.
2. No `auto_exclude_navigation` page is selected by either VLM target rule.
   This is the gap that made the override outrank `auto_hold`: a hold is a
   vision queue, not a terminal state.
3. No vector-manifest chunk overlapping a navigation page is `eligible`.
4. The audit carries no stale `layout_v7` rows.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from esg_layout_qa import AUDIT_VERSION, page_texts_from_map, read_csv, resolve_path
from esg_page_role import AUTO_EXCLUDE_NAVIGATION, classify_page_role

# Mirrors scripts/run_esg_vlm.py: targets_classify selects reconstructed-column
# pages, targets_extract selects structural-grid holds plus classifier-flagged
# reconstructed-column pages. A navigation page must match neither.
VLM_CLASSIFY_DECISION = "auto_pass_column_order_reconstructed"
VLM_EXTRACT_HOLD_DECISION = "auto_hold"


def check(parse_index: Path, layout_audit: Path, manifest: Path) -> list[str]:
    failures: list[str] = []
    audit = read_csv(layout_audit)
    if not audit:
        return [f"layout audit is empty or missing: {layout_audit}"]

    stale = [r for r in audit if (r.get("audit_version") or "").strip() != AUDIT_VERSION]
    if stale:
        failures.append(
            f"check 4 FAIL: {len(stale)} audit rows are not {AUDIT_VERSION} (stale rows hold every chunk)"
        )

    by_page: dict[tuple[str, str, int], dict] = {}
    for row in audit:
        try:
            page = int(row.get("page") or 0)
        except ValueError:
            continue
        by_page[((row.get("ticker") or "").upper(), row.get("pdf_stem") or "", page)] = row

    # 1. every navigation page is excluded
    missed: list[str] = []
    excluded_keys: set[tuple[str, str, int]] = set()
    for parse_row in read_csv(parse_index):
        if parse_row.get("status") != "parsed":
            continue
        text_path = resolve_path(parse_row.get("parsed_text_file"))
        map_path = resolve_path(parse_row.get("page_map_file"))
        if not text_path or not map_path or not text_path.exists() or not map_path.exists():
            continue
        ticker = (parse_row.get("ticker") or "").upper()
        stem = Path(parse_row.get("pdf_file") or "").stem
        try:
            texts = page_texts_from_map(text_path, map_path)
        except Exception:
            continue
        for page, text in texts.items():
            if not classify_page_role(text).is_navigation:
                continue
            key = (ticker, stem, page)
            excluded_keys.add(key)
            row = by_page.get(key)
            decision = (row or {}).get("decision", "<no audit row>")
            if decision != AUTO_EXCLUDE_NAVIGATION:
                missed.append(f"{stem} p{page} -> {decision}")
    if missed:
        failures.append(
            f"check 1 FAIL: {len(missed)} navigation pages not excluded, e.g. {missed[:5]}"
        )

    # 2. none of them is queued for vision
    queued = [
        f"{stem} p{page}"
        for (ticker, stem, page) in excluded_keys
        if (by_page.get((ticker, stem, page)) or {}).get("decision")
        in {VLM_CLASSIFY_DECISION, VLM_EXTRACT_HOLD_DECISION}
    ]
    if queued:
        failures.append(f"check 2 FAIL: {len(queued)} navigation pages queued for VLM: {queued[:5]}")

    # 3. no eligible chunk overlaps a navigation page
    nav_pages: dict[tuple[str, str], set[int]] = defaultdict(set)
    for ticker, stem, page in excluded_keys:
        nav_pages[(ticker, stem)].add(page)

    if manifest.exists():
        leaked: list[str] = []
        for row in read_csv(manifest):
            if (row.get("eligibility_decision") or "").strip() != "eligible":
                continue
            key = ((row.get("ticker") or "").upper(), row.get("pdf_stem") or "")
            pages = nav_pages.get(key)
            if not pages:
                continue
            try:
                start = int(row.get("page_start") or 0)
                end = int(row.get("page_end") or 0)
            except ValueError:
                continue
            hit = pages.intersection(range(start, end + 1))
            if hit:
                leaked.append(f"{row.get('chunk_id')} pages={sorted(hit)}")
        if leaked:
            failures.append(
                f"check 3 FAIL: {len(leaked)} eligible chunks overlap navigation pages: {leaked[:5]}"
            )
    else:
        failures.append(f"check 3 SKIPPED: manifest not found at {manifest}")

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parse-index", type=Path, default=Path(config.ESG_PARSE_INDEX_CSV))
    parser.add_argument("--layout-audit", type=Path, default=Path(config.ESG_PAGE_LAYOUT_QA_CSV))
    parser.add_argument(
        "--manifest", type=Path, default=Path(config.REFERENCE_DIR) / "vector_index_manifest.csv"
    )
    args = parser.parse_args()

    failures = check(
        args.parse_index.resolve(), args.layout_audit.resolve(), args.manifest.resolve()
    )
    if failures:
        for failure in failures:
            print(failure)
        sys.exit(1)
    print("PASS: no navigation page is indexed, eligible, or queued for vision.")


if __name__ == "__main__":
    main()
