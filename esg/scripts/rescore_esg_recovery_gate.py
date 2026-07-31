"""Re-run only the reading-order recovery gate over the pages it already judged.

The candidate audit in ``reports/esg_recovery_candidate/`` was produced by the
gate as it stood before the first Terra review found six of its passes unsafe.
Sampling a second review from that file would draw pages the current gate no
longer certifies, which tells us nothing about the current gate.

This re-scores those pages -- and only those pages -- with the gate as it
stands now. It is not an audit run: it does not parse, does not touch the page
maps, does not build a manifest, and writes one new CSV. Everything except the
four recovery columns is copied through from the input row, so the result is a
drop-in ``--audit`` input for ``prepare_esg_recovery_terra_sample``.

Read-only apart from its own output file.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from esg_layout_qa import (
    AUTO_HOLD,
    AUTO_PASS_RECOVERED_ORDER,
    page_texts_from_map,
    parse_int,
    read_csv,
    resolve_path,
)
from esg_order_recovery import recover_reading_order
from esg_order_safety import has_full_page_image
from esg_page_role import apply_navigation_override

DEFAULT_AUDIT = (
    config.REPORTS_DIR / "esg_recovery_candidate" / "esg_page_layout_qa_candidate.csv"
)
DEFAULT_OUT = (
    config.REPORTS_DIR / "esg_recovery_candidate" / "esg_recovery_rescore_v2.csv"
)


def parse_index_lookup() -> dict[tuple[str, str], dict[str, str]]:
    return {
        (row["ticker"].strip().upper(), Path(row["pdf_file"]).stem): row
        for row in read_csv(Path(config.ESG_PARSE_INDEX_CSV))
        if row.get("status") == "parsed"
    }


def rescore(rows: list[dict[str, str]], lookup: dict, quiet: bool) -> list[dict[str, str]]:
    import pdfplumber

    by_document: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_document[(row["ticker"].upper(), row["pdf_stem"])].append(row)

    done = 0
    for key, pages in sorted(by_document.items()):
        parse_row = lookup.get(key)
        if not parse_row:
            for row in pages:
                row["decision_reason"] = "rescore_skipped: no parse-index row"
            continue
        text_path = resolve_path(parse_row.get("parsed_text_file"))
        map_path = resolve_path(parse_row.get("page_map_file"))
        source = resolve_path(parse_row.get("source_pdf"))
        if not (text_path and map_path and source and source.exists()):
            for row in pages:
                row["decision_reason"] = "rescore_skipped: source or page map missing"
            continue
        page_texts = page_texts_from_map(text_path, map_path)

        with pdfplumber.open(source) as pdf:
            for row in sorted(pages, key=lambda r: int(r["page"])):
                number = int(row["page"])
                current_text = page_texts.get(number, "")
                page = pdf.pages[number - 1]
                words = (
                    page.extract_words(
                        use_text_flow=False,
                        keep_blank_chars=False,
                        extra_attrs=["size", "upright"],
                    )
                    or []
                )
                table_like = bool(
                    parse_int(row.get("page_map_table_candidate_count")) or 0
                ) or any(
                    line.strip().startswith("|") for line in current_text.splitlines()
                )
                recovery = recover_reading_order(
                    words,
                    float(page.width),
                    float(page.height),
                    current_text,
                    table_like=table_like,
                    visual_object_count=int(row.get("visual_object_count") or 0),
                    mixed_column_lines=int(row.get("mixed_column_lines") or 0),
                    full_page_image=has_full_page_image(
                        list(getattr(page, "images", []) or []),
                        float(page.width),
                        float(page.height),
                    ),
                )
                metrics = ";".join(
                    f"{k}={v}" for k, v in sorted(recovery.metrics.items())
                )
                if recovery.recovered_now:
                    decision = AUTO_PASS_RECOVERED_ORDER
                    reason = f"{recovery.reason}: {metrics}"
                else:
                    decision = AUTO_HOLD
                    reason = f"auto_hold_{recovery.outcome}: {recovery.reason}"
                decision, reason, page_role = apply_navigation_override(
                    decision, reason, current_text
                )
                row["decision"] = decision
                row["decision_reason"] = reason
                # The audit stores the role's reason string, empty for an
                # ordinary page. Storing the PageRoleResult itself would make
                # every row look like it carries a role, and the leakage check
                # in report_esg_recovery_candidate reads exactly this column.
                row["page_role"] = page_role.reason
                row["recovery_parser"] = recovery.parser
                row["recovery_metrics"] = metrics
                done += 1
        if not quiet:
            print(f"  {done:5}/{len(rows)}  {key[0]} {key[1][:52]}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    audit = read_csv(args.audit.resolve())
    eligible = [row for row in audit if (row.get("recovery_metrics") or "").strip()]
    print(f"re-scoring {len(eligible)} pages the recovery gate already judged")

    before = Counter(row["decision"] for row in eligible)
    rescore(eligible, parse_index_lookup(), args.quiet)
    after = Counter(row["decision"] for row in eligible)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(audit[0].keys()), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(eligible)

    print("\ndecision            before   after   delta")
    for key in sorted(set(before) | set(after)):
        print(f"{key:34} {before.get(key,0):6} {after.get(key,0):7} {after.get(key,0)-before.get(key,0):+7}")
    print(f"\nwrote {args.out}")
    print("The candidate audit, the live v8 audit and every manifest are unchanged.")


if __name__ == "__main__":
    main()
