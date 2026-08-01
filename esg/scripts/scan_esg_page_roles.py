"""Scan every parsed page for navigation role, without running the full audit.

Read-only. Answers two questions before the expensive layout audit is rerun:

* how many pages of the live corpus does ``esg_page_role`` exclude, and
* which pages are they, so a false-positive sample can be reviewed.

It reads the saved page text, which is the same input the gate feeds the
classifier, so its verdicts match what the audit will produce. It does not
re-extract anything and it writes nothing back into the corpus.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from esg_layout_qa import page_texts_from_map, read_csv, resolve_path
from esg_page_role import classify_page_role

FIELDS = [
    "ticker",
    "pdf_stem",
    "pdf_file",
    "page",
    "page_role",
    "page_role_detail",
    "current_decision",
    "page_text_chars",
    "page_text_head",
]

HEAD_CHARS = 160


def scan(parse_index: Path, layout_audit: Path | None) -> list[dict]:
    prior: dict[tuple[str, str, int], str] = {}
    if layout_audit and layout_audit.exists():
        for row in read_csv(layout_audit):
            try:
                page = int(row.get("page") or 0)
            except ValueError:
                continue
            key = ((row.get("ticker") or "").upper(), row.get("pdf_stem") or "", page)
            prior[key] = (row.get("decision") or "").strip()

    rows: list[dict] = []
    parse_rows = [r for r in read_csv(parse_index) if r.get("status") == "parsed"]
    for position, parse_row in enumerate(parse_rows, start=1):
        text_path = resolve_path(parse_row.get("parsed_text_file"))
        page_map_path = resolve_path(parse_row.get("page_map_file"))
        if text_path is None or page_map_path is None:
            continue
        if not text_path.exists() or not page_map_path.exists():
            continue
        ticker = (parse_row.get("ticker") or "").upper()
        pdf_file = parse_row.get("pdf_file") or ""
        pdf_stem = Path(pdf_file).stem
        try:
            page_texts = page_texts_from_map(text_path, page_map_path)
        except Exception as error:  # pragma: no cover - reported, not raised
            print(f"  skip {pdf_stem}: {type(error).__name__}: {error}", file=sys.stderr)
            continue

        for page, text in sorted(page_texts.items()):
            role = classify_page_role(text)
            if not role.is_navigation:
                continue
            head = " ".join((text or "").split())[:HEAD_CHARS]
            rows.append(
                {
                    "ticker": ticker,
                    "pdf_stem": pdf_stem,
                    "pdf_file": pdf_file,
                    "page": page,
                    "page_role": role.reason,
                    "page_role_detail": role.detail,
                    "current_decision": prior.get((ticker, pdf_stem, page), ""),
                    "page_text_chars": len(text or ""),
                    "page_text_head": head,
                }
            )
        if position % 25 == 0:
            print(f"  {position}/{len(parse_rows)} documents", file=sys.stderr)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parse-index", type=Path, default=Path(config.ESG_PARSE_INDEX_CSV))
    parser.add_argument("--layout-audit", type=Path, default=Path(config.ESG_PAGE_LAYOUT_QA_CSV))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(config.REPORTS_DIR) / "esg_page_role_scan" / "navigation_pages.csv",
    )
    args = parser.parse_args()

    rows = scan(args.parse_index.resolve(), args.layout_audit.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total_pages = 0
    for parse_row in read_csv(args.parse_index.resolve()):
        try:
            total_pages += int(parse_row.get("page_count") or 0)
        except ValueError:
            pass

    print(f"navigation pages: {len(rows)} of {total_pages} parsed pages")
    if total_pages:
        print(f"share: {len(rows) / total_pages:.2%}")
    print("by rule:", dict(Counter(r["page_role"] for r in rows)))
    print("by decision they would override:", dict(Counter(r["current_decision"] for r in rows)))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
