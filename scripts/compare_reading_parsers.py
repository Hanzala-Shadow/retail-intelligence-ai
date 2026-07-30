"""Compare reading-order parser variants page by page on whole PDFs.

Read-only. Nothing here is wired into production and no corpus data is written.

Five variants run over the same pages so their differences are attributable to
the reader alone:

``raw_text``
    ``page.extract_text()``. The floor: no navigation stripping, no ordering
    work. Included so the other three can be judged against doing nothing.
``column_order``
    ``esg_reading_order.reconstruct_column_order`` -- the validated production
    reader, which picks ONE column count for the whole page and returns an
    empty text with ``ambiguous``/``not_applicable`` rather than guessing.
``regions``
    ``esg_reading_regions.reconstruct_by_regions`` -- the default-off candidate:
    per-region column detection, panel peeling, row-structure classification.
``regions_tables``
    ``regions``, except that a page whose ruled table can be extracted AND
    verified is handed to that extraction instead, through the module's existing
    ``verified_table_text`` hook.
``regions_table_cells``
    ``regions``, except verified ruled-table markdown replaces one uniquely
    matched region. Recall and extra tokens are measured against that region's
    navigation-stripped words, not against the whole page.

The table variant deliberately reuses the audit's own acceptance bars
(``esg_layout_qa``: markdown shape, ``TABLE_MIN_TOKEN_RECALL``,
``TABLE_MAX_EXTRA_TOKEN_RATIO``) rather than inventing a looser test, so
"verified" here means what it means in production. The whole-page table variant
still refuses partial-page tables. The region-scoped variant uses the same bars
at the smaller scope and records ambiguous or unmatched geometry explicitly.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from esg_layout_qa import (  # noqa: E402
    TABLE_MAX_EXTRA_TOKEN_RATIO,
    TABLE_MIN_TOKEN_RECALL,
    _markdown_table_shape,
    _semantic_token_counter,
    _upright_source_tokens,
)
from esg_navigation import build_navigation_profile, clean_navigation  # noqa: E402
from esg_reading_order import reconstruct_column_order  # noqa: E402
from esg_reading_regions import (  # noqa: E402
    VERIFIED_REGION_MIN_CONTAINMENT,
    _region_containment_share,
    reconstruct_by_regions,
)

RAW_ROOT = REPO_ROOT / "data" / "01_raw" / "sustainability"
OUT_DIR = REPO_ROOT / "reports" / "parser_comparison_2026-07-30"

VARIANTS = [
    "raw_text",
    "column_order",
    "regions",
    "regions_tables",
    "regions_table_cells",
]

RULED_TABLE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}

# Visual review of 60 dpi renders for all 42 pages in the fixed comparison
# scope. ACI p2 has an unruled table of contents, which is navigation rather
# than a data table and is excluded from this count.
UNRULED_DATA_TABLE_PAGE_COUNT = 0
UNRULED_REVIEW_NOTE = (
    "All 42 pages were reviewed as renders. The four pages with no ruled "
    "detection were AEO p2 and ACI p2, p3, and p28. None contains an "
    "unruled data table; ACI p2 is an unruled table of contents and was "
    "excluded as navigation."
)


def extract_words(page) -> list[dict]:
    try:
        return page.extract_words(
            use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
        ) or []
    except TypeError:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False) or []


def table_markdown(page) -> tuple[str | None, str]:
    """Largest ruled table on the page as markdown, plus why not when None.

    The distinct refusals are reported separately on purpose. Ruled-line
    detection fires on page furniture -- dividers, key lines, boxed callouts --
    which surface as 1- and 2-row "tables". Collapsing those into one
    "no table found" bucket makes it look as though these reports have no
    tables, when in fact most detections are decoration and the few real tables
    are refused later, for an unrelated reason.
    """

    try:
        tables = page.find_tables(table_settings=RULED_TABLE_SETTINGS)
    except Exception as error:
        return None, f"table_finder_error={type(error).__name__}"
    if not tables:
        return None, "no_ruled_lines_on_page"
    table = max(tables, key=lambda t: len(t.rows) * max(len(t.columns), 1))
    try:
        grid = table.extract()
    except Exception as error:
        return None, f"table_extract_error={type(error).__name__}"
    rows = [
        [(cell or "").replace("\n", " ").replace("|", r"\|").strip() for cell in row]
        for row in (grid or [])
    ]
    rows = [row for row in rows if any(row)]
    found = f"{len(tables)} ruled region(s), largest {len(table.rows)}x{len(table.columns)}"
    if not rows:
        return None, f"ruled_region_holds_no_text ({found})"
    if len(rows) < 3:
        return None, f"only_{len(rows)}_non_empty_row(s), reads as page furniture ({found})"
    width = max(len(row) for row in rows)
    if width < 2:
        return None, f"single_column_region ({found})"
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines), found


def ruled_table_candidates(page) -> tuple[list[dict], list[str]]:
    """All ruled regions that look like real tables, plus visible refusals.

    The 3-non-empty-row and 2-column floors are the same furniture filters as
    the whole-page path. Unlike ``table_markdown``, this keeps every eligible
    table so multi-table/one-region matches can be measured instead of hidden
    by selecting only the largest region.
    """

    try:
        tables = page.find_tables(table_settings=RULED_TABLE_SETTINGS)
    except Exception as error:
        return [], [f"table_finder_error={type(error).__name__}"]
    if not tables:
        return [], ["no_ruled_lines_on_page"]

    candidates: list[dict] = []
    refusals: list[str] = []
    for table_index, table in enumerate(tables):
        shape = f"ruled region {table_index + 1}/{len(tables)}: {len(table.rows)}x{len(table.columns)}"
        try:
            grid = table.extract()
        except Exception as error:
            refusals.append(f"{shape}, table_extract_error={type(error).__name__}")
            continue
        rows = [
            [(cell or "").replace("\n", " ").replace("|", r"\|").strip() for cell in row]
            for row in (grid or [])
        ]
        rows = [row for row in rows if any(row)]
        if not rows:
            refusals.append(f"{shape}, ruled_region_holds_no_text")
            continue
        if len(rows) < 3:
            refusals.append(f"{shape}, only_{len(rows)}_non_empty_row(s), page furniture")
            continue
        width = max(len(row) for row in rows)
        if width < 2:
            refusals.append(f"{shape}, single_column_region")
            continue
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(["---"] * width) + " |",
            *["| " + " | ".join(row) + " |" for row in rows[1:]],
        ]
        markdown = "\n".join(lines)
        row_count, column_count = _markdown_table_shape(markdown)
        candidates.append(
            {
                "table_index": table_index,
                "bbox": tuple(float(value) for value in table.bbox),
                "markdown": markdown,
                "row_count": row_count,
                "column_count": column_count,
                "detail": shape,
            }
        )
    return candidates, refusals


def token_metrics(markdown: str, words: list[dict]) -> dict:
    source = _upright_source_tokens(words)
    output = _semantic_token_counter(markdown)
    source_count = sum(source.values())
    matched_count = sum((source & output).values())
    return {
        "source_token_count": source_count,
        "output_token_count": sum(output.values()),
        "recall": matched_count / source_count if source_count else 0.0,
        "extra_token_ratio": (
            sum((output - source).values()) / source_count if source_count else 0.0
        ),
    }


def verify_table(found: tuple[str | None, str], words: list[dict]) -> tuple[str | None, str]:
    """Apply the audit's own acceptance bars. Returns (accepted_text, reason)."""

    markdown, detail = found
    if markdown is None:
        return None, detail
    row_count, column_count = _markdown_table_shape(markdown)
    if row_count < 2 or column_count < 2:
        return None, "invalid_markdown_shape"
    metrics = token_metrics(markdown, words)
    source_count = metrics["source_token_count"]
    if source_count == 0:
        return None, "missing_source_tokens"
    recall = metrics["recall"]
    extra = metrics["extra_token_ratio"]
    # Recall is measured against every upright word on the PAGE, because the
    # hook this feeds replaces the whole page. A table that is one element among
    # several therefore scores low and is refused -- correctly, since accepting
    # it would discard the rest of the page. The number is reported either way:
    # a near miss means the table IS the page bar its heading, which is a very
    # different situation from a table that covers a quarter of it.
    if recall < TABLE_MIN_TOKEN_RECALL:
        return None, (
            f"real table ({detail}) refused: covers {recall:.1%} of page words, "
            f"needs {TABLE_MIN_TOKEN_RECALL:.1%}; extra={extra:.3f}"
        )
    if extra > TABLE_MAX_EXTRA_TOKEN_RATIO:
        return None, f"real table ({detail}) refused: extra_token_ratio={extra:.4f} > {TABLE_MAX_EXTRA_TOKEN_RATIO}"
    return markdown, f"verified rows={row_count} columns={column_count} recall={recall:.4f}"


def words_for_region(words: list[dict], region) -> list[dict]:
    """Navigation-stripped words whose bbox centers fall inside a region."""

    selected = []
    for word in words:
        x0 = float(word.get("x0", 0.0))
        x1 = float(word.get("x1", x0))
        top = float(word.get("top", 0.0))
        bottom = float(word.get("bottom", top))
        center_x = (x0 + x1) / 2
        center_y = (top + bottom) / 2
        if (
            region.left <= center_x <= region.right
            and region.top <= center_y <= region.bottom
        ):
            selected.append(word)
    return selected


def match_and_verify_region_tables(
    candidates: list[dict],
    region_result,
    body_words: list[dict],
) -> tuple[list[tuple[tuple[float, float, float, float], str]], list[dict], dict]:
    """Match ruled tables to regions, audit them, and expose every case.

    The match rule is shared with ``esg_reading_regions``: intersection area
    must cover at least 80% of the region area. A substitution is allowed only
    for a unique table-to-region and region-to-table match. Ambiguous geometry
    is reported and left unchanged.
    """

    table_to_regions: list[list[int]] = []
    region_to_tables: list[list[int]] = [[] for _ in region_result.regions]
    overlap_shares: dict[tuple[int, int], float] = {}
    for candidate_index, candidate in enumerate(candidates):
        matched = []
        for region_index, region in enumerate(region_result.regions):
            share = _region_containment_share(region, candidate["bbox"])
            if share >= VERIFIED_REGION_MIN_CONTAINMENT:
                matched.append(region_index)
                region_to_tables[region_index].append(candidate_index)
                overlap_shares[(candidate_index, region_index)] = share
        table_to_regions.append(matched)

    case_counts = {
        "eligible_ruled_tables": len(candidates),
        "table_without_region_match": sum(not matches for matches in table_to_regions),
        "one_table_multiple_regions": sum(len(matches) > 1 for matches in table_to_regions),
        "several_tables_one_region": sum(len(matches) > 1 for matches in region_to_tables),
        "non_row_structured_overlaps": sum(
            region_result.regions[region_index].region_type != "row_structured"
            for matches in table_to_regions
            for region_index in matches
        ),
        "unique_match_verification_failed": 0,
        "substitutions": 0,
    }

    verified: list[tuple[tuple[float, float, float, float], str]] = []
    reviews: list[dict] = []
    page_text = region_result.text
    for candidate_index, candidate in enumerate(candidates):
        page_metrics = token_metrics(candidate["markdown"], body_words)
        matched_regions = table_to_regions[candidate_index]
        review = {
            "table_index": candidate["table_index"],
            "bbox": list(candidate["bbox"]),
            "rows": candidate["row_count"],
            "columns": candidate["column_count"],
            "page_recall": page_metrics["recall"],
            "page_extra_token_ratio": page_metrics["extra_token_ratio"],
            "matched_region_indices": matched_regions,
            "matched_region_types": [
                region_result.regions[index].region_type for index in matched_regions
            ],
            "overlap_shares": [
                overlap_shares[(candidate_index, index)] for index in matched_regions
            ],
            "substitution_fired": False,
            "region_recall": None,
            "region_extra_token_ratio": None,
            "decision": "",
            "before_text": (
                "\n\n--- region boundary ---\n\n".join(
                    region_result.region_texts[index] for index in matched_regions
                )
                if matched_regions
                else page_text
            ),
            "after_text": "",
            "table_markdown": candidate["markdown"],
        }

        if not matched_regions:
            review["decision"] = "no region reached the 80% containment bar"
        elif len(matched_regions) > 1:
            review["decision"] = "one table overlaps several regions; left unchanged"
        else:
            region_index = matched_regions[0]
            if len(region_to_tables[region_index]) > 1:
                review["decision"] = "several tables overlap one region; left unchanged"
            else:
                scoped_words = words_for_region(
                    body_words, region_result.regions[region_index]
                )
                metrics = token_metrics(candidate["markdown"], scoped_words)
                review["region_recall"] = metrics["recall"]
                review["region_extra_token_ratio"] = metrics["extra_token_ratio"]
                if not metrics["source_token_count"]:
                    review["decision"] = "matched region has no upright source tokens"
                    case_counts["unique_match_verification_failed"] += 1
                elif metrics["recall"] < TABLE_MIN_TOKEN_RECALL:
                    review["decision"] = (
                        f"region recall {metrics['recall']:.4f} is below "
                        f"{TABLE_MIN_TOKEN_RECALL:.4f}"
                    )
                    case_counts["unique_match_verification_failed"] += 1
                elif metrics["extra_token_ratio"] > TABLE_MAX_EXTRA_TOKEN_RATIO:
                    review["decision"] = (
                        f"region extra ratio {metrics['extra_token_ratio']:.4f} exceeds "
                        f"{TABLE_MAX_EXTRA_TOKEN_RATIO:.4f}"
                    )
                    case_counts["unique_match_verification_failed"] += 1
                else:
                    verified.append((candidate["bbox"], candidate["markdown"]))
                    review["substitution_fired"] = True
                    review["decision"] = "unique match passed the strict region token audit"
                    case_counts["substitutions"] += 1
        reviews.append(review)

    return verified, reviews, case_counts


def run_page(page, profile) -> dict:
    width, height = float(page.width), float(page.height)
    words = extract_words(page)
    cleaned = clean_navigation(words, page.chars, width, height, profile)
    body = cleaned.body_words

    results: dict[str, dict] = {}

    results["raw_text"] = {
        "text": page.extract_text() or "",
        "status": "n/a",
        "reason": "pdfplumber extract_text, no navigation stripping",
    }

    column = reconstruct_column_order(body, width, height)
    results["column_order"] = {
        "text": column.text,
        "status": column.status,
        "reason": f"{column.reason} (columns={column.column_count})",
    }

    region = reconstruct_by_regions(body, width, height)
    results["regions"] = {
        "text": region.text,
        "status": region.status,
        "reason": f"{region.reason} (regions={len(region.regions)})",
        "region_types": [r.region_type for r in region.regions],
    }

    accepted, table_reason = verify_table(table_markdown(page), body)
    if accepted is not None:
        table_region = reconstruct_by_regions(body, width, height, verified_table_text=accepted)
        results["regions_tables"] = {
            "text": table_region.text,
            "status": table_region.status,
            "reason": f"table path used: {table_reason}",
            "table_used": True,
        }
    else:
        results["regions_tables"] = {
            "text": region.text,
            "status": region.status,
            "reason": f"fell back to regions ({table_reason})",
            "table_used": False,
        }

    candidates, ruled_refusals = ruled_table_candidates(page)
    verified_regions, table_reviews, case_counts = match_and_verify_region_tables(
        candidates, region, body
    )
    region_cells = reconstruct_by_regions(
        body,
        width,
        height,
        verified_region_tables=verified_regions or None,
    )
    for review in table_reviews:
        matched_regions = review["matched_region_indices"]
        review["after_text"] = (
            "\n\n--- region boundary ---\n\n".join(
                region_cells.region_texts[index] for index in matched_regions
            )
            if matched_regions
            else region_cells.text
        )
    actual_substitutions = sum(
        region_info.region_type == "table_verified"
        for region_info in region_cells.regions
    )
    results["regions_table_cells"] = {
        "text": region_cells.text,
        "status": region_cells.status,
        "reason": (
            f"region table substitutions={actual_substitutions}; "
            f"eligible ruled tables={len(candidates)}; "
            f"refusals={len(ruled_refusals)}"
        ),
        "table_used": actual_substitutions > 0,
        "substitution_count": actual_substitutions,
        "region_types": [r.region_type for r in region_cells.regions],
        "table_reviews": table_reviews,
        "match_case_counts": case_counts,
        "ruled_detection_refusals": ruled_refusals,
        "preservation_ratio": region_cells.preservation_ratio,
    }

    return {
        "page": page.page_number,
        "body_word_count": len(body),
        "navigation_item_count": len(cleaned.navigation_items),
        "variants": results,
    }


def run_document(ticker: str, pdf_file: str, pages: list[int] | None) -> dict:
    path = RAW_ROOT / ticker / pdf_file
    rows = []
    with pdfplumber.open(path) as pdf:
        profile = build_navigation_profile(
            [(p.chars, float(p.width), float(p.height)) for p in pdf.pages]
        )
        selected = pages or range(1, len(pdf.pages) + 1)
        for number in selected:
            row = run_page(pdf.pages[number - 1], profile)
            if row["body_word_count"] == 0:
                continue
            rows.append(row)
            print(
                f"  p{row['page']:<4d} "
                + "  ".join(
                    f"{name}={row['variants'][name]['status']}" for name in ("column_order", "regions")
                )
                + ("  [TABLE PATH]" if row["variants"]["regions_tables"]["table_used"] else "")
                + (
                    f"  [REGION TABLES={row['variants']['regions_table_cells']['substitution_count']}]"
                    if row["variants"]["regions_table_cells"]["table_used"]
                    else ""
                )
            )
    return {"ticker": ticker, "pdf_file": pdf_file, "pages": rows}


STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 0 20px 60px; background: #f6f7f9; color: #202124; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin: 28px 0 8px; }
table.summary { border-collapse: collapse; font-size: 13px; margin-bottom: 24px; }
table.summary th, table.summary td { border: 1px solid #d7dbe0; padding: 4px 8px; text-align: left; }
table.summary td.differs { background: #fef3c7; }
table.summary td.tablepath { background: #dcfce7; font-weight: 600; }
article { background: #fff; border: 1px solid #d7dbe0; border-radius: 8px; padding: 14px; margin: 0 0 20px; }
.grid { display: grid; grid-template-columns: repeat(5, minmax(220px, 1fr)); gap: 12px; }
h3 { margin: 0 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #5f6368; }
.reason { font-size: 11px; color: #5f6368; margin: 0 0 6px; min-height: 28px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 10px; background: #111827;
      color: #e5e7eb; border-radius: 4px; max-height: 420px; overflow-y: auto; font-size: 11px; line-height: 1.45; }
pre.empty { background: #7f1d1d; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 999px; font-size: 11px; border: 1px solid #d7dbe0; }
@media (max-width: 1500px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e8eaed; }
  article { background: #1f2126; border-color: #33363b; }
  table.summary th, table.summary td { border-color: #44474d; }
  .reason, h3 { color: #9aa0a6; }
}
"""


def write_report(documents: list[dict], *, write_fixed_summary: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "parser_comparison.json").write_text(
        json.dumps(documents, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Reading-order parser comparison</title>",
        f"<style>{STYLE}</style></head><body>",
        "<h1>Reading-order parser comparison</h1>",
        "<p>Five readers over the same pages. <code>column_order</code> is the validated "
        "production reader and returns <strong>empty text</strong> when it will not guess "
        "(shown in red). <code>regions</code> is the default-off candidate. "
        "<code>regions_tables</code> adds the whole-page verified ruled-table path. "
        "<code>regions_table_cells</code> uses the same strict bars against one uniquely "
        "matched region at a time.</p>",
    ]

    for document in documents:
        parts.append(f"<h2>{html.escape(document['ticker'])} &mdash; {html.escape(document['pdf_file'])}</h2>")
        parts.append('<table class="summary"><tr><th>Page</th><th>Body words</th>'
                     "<th>column_order</th><th>regions</th><th>whole-page table</th>"
                     "<th>region tables</th></tr>")
        for row in document["pages"]:
            variants = row["variants"]
            column_status = variants["column_order"]["status"]
            differs = "differs" if column_status != "reconstructed" else ""
            table_cell = "yes" if variants["regions_tables"]["table_used"] else ""
            region_table_count = variants["regions_table_cells"]["substitution_count"]
            parts.append(
                f"<tr><td>{row['page']}</td><td>{row['body_word_count']}</td>"
                f'<td class="{differs}">{html.escape(column_status)}</td>'
                f"<td>{html.escape(variants['regions']['status'])} "
                f"({len(variants['regions'].get('region_types', []))} regions)</td>"
                f'<td class="{"tablepath" if table_cell else ""}">{table_cell}</td>'
                f'<td class="{"tablepath" if region_table_count else ""}">'
                f'{region_table_count or ""}</td></tr>'
            )
        parts.append("</table>")

        for row in document["pages"]:
            parts.append(
                f"<article><h3 style=\"font-size:14px;text-transform:none;color:inherit\">"
                f"Page {row['page']} &middot; {row['body_word_count']} body words</h3>"
                '<div class="grid">'
            )
            for name in VARIANTS:
                variant = row["variants"][name]
                text = variant["text"]
                empty = " empty" if not text.strip() else ""
                parts += [
                    f"<section><h3>{name} <span class='badge'>{html.escape(str(variant['status']))}</span></h3>",
                    f"<p class='reason'>{html.escape(str(variant['reason']))}</p>",
                    f"<pre class='{empty.strip()}'>{html.escape(text) or '(no text returned)'}</pre></section>",
                ]
            parts.append("</div></article>")

    parts.append("</body></html>")
    (OUT_DIR / "parser_comparison.html").write_text("\n".join(parts), encoding="utf-8")
    if write_fixed_summary:
        write_region_table_summary(documents)
    print(f"\nwrote {OUT_DIR / 'parser_comparison.html'}")


def _markdown_block(text: str, language: str = "text") -> list[str]:
    return [f"~~~{language}", text, "~~~"]


def _append_table_case(lines: list[str], item: dict) -> None:
    review = item["review"]
    fired = review["substitution_fired"]
    region_recall = review["region_recall"]
    region_extra = review["region_extra_token_ratio"]
    lines.extend(
        [
            f"### {item['ticker']} page {item['page']}",
            "",
            f"- Substitution fired: **{'yes' if fired else 'no'}**.",
            f"- Decision: {review['decision']}.",
            f"- Whole-page recall: {review['page_recall']:.4f} "
            f"({review['page_recall']:.1%}); extra-token ratio: "
            f"{review['page_extra_token_ratio']:.4f}.",
            (
                f"- Region recall: {region_recall:.4f}; region extra-token ratio: "
                f"{region_extra:.4f}."
                if region_recall is not None
                else "- Region recall: not computed because the geometry match was not one-to-one."
            ),
            f"- Matched region types: {', '.join(review['matched_region_types']) or 'none'}.",
            "- Before/after result: unchanged because no safe substitution passed."
            if not fired
            else "- Before/after result: the matched region changed to verified table markdown.",
            "",
            "<details><summary>Before text</summary>",
            "",
            *_markdown_block(review["before_text"]),
            "",
            "</details>",
            "",
            "<details><summary>After text</summary>",
            "",
            *_markdown_block(review["after_text"]),
            "",
            "</details>",
            "",
            "<details><summary>Ruled-table markdown considered</summary>",
            "",
            *_markdown_block(review["table_markdown"], "markdown"),
            "",
            "</details>",
            "",
        ]
    )


def write_region_table_summary(documents: list[dict]) -> None:
    """Write the technical summary for the fixed 42-page comparison."""

    all_rows: list[dict] = []
    focus: list[dict] = []
    totals: dict[str, int] = {}
    eligible_pages = 0
    unchanged_pages = 0
    preservation_failures = 0

    for document in documents:
        for row in document["pages"]:
            all_rows.append(row)
            variant = row["variants"]["regions_table_cells"]
            if variant["table_reviews"]:
                eligible_pages += 1
            if variant["text"] == row["variants"]["regions"]["text"]:
                unchanged_pages += 1
            if variant["preservation_ratio"] < 1.0:
                preservation_failures += 1
            for key, value in variant["match_case_counts"].items():
                totals[key] = totals.get(key, 0) + int(value)

            # The existing whole-page path identifies the nine measured real
            # table pages. Use its largest-table selection for the requested
            # before/after page set while still counting every eligible ruled
            # candidate in the match audit above.
            if "real table" not in row["variants"]["regions_tables"]["reason"]:
                continue
            reviews = variant["table_reviews"]
            if not reviews:
                continue
            primary = max(
                reviews,
                key=lambda review: (review["rows"] * review["columns"], -review["table_index"]),
            )
            focus.append(
                {
                    "ticker": document["ticker"],
                    "pdf_file": document["pdf_file"],
                    "page": row["page"],
                    "review": primary,
                }
            )

    partial = sorted(
        [item for item in focus if item["review"]["page_recall"] < 0.50],
        key=lambda item: item["review"]["page_recall"],
    )
    remaining = sorted(
        [item for item in focus if item["review"]["page_recall"] >= 0.50],
        key=lambda item: (
            item["review"]["page_recall"] < 0.94,
            -item["review"]["page_recall"],
        ),
    )

    lines = [
        "# Region-level verified table substitution",
        "",
        "Date: 2026-07-30",
        "",
        "## Technical summary",
        "",
        "**This did not improve any of the 42 pages.** The fifth parser variant used the",
        "same strict token bars at region scope, but no ruled table had both a safe one-to-one",
        "geometry match and passing region tokens. `regions_table_cells` therefore equals",
        f"`regions` on {unchanged_pages} of {len(all_rows)} pages.",
        "",
        f"There were {totals.get('eligible_ruled_tables', 0)} eligible ruled table grids on",
        f"{eligible_pages} pages after the 3-row and 2-column furniture filters. "
        f"Substitutions fired: {totals.get('substitutions', 0)}. Preservation failures: "
        f"{preservation_failures}.",
        "",
        "The scope correction was sound, but region detection was the blocker. Partial-page",
        "tables were usually only part of a larger prose/chart region, while dense full-page",
        "tables were often split into several small regions. The next useful test is a table-box",
        "region splitter, not a looser token threshold.",
        "",
        "## The four partial-page tables did not reach substitution",
        "",
        "These are listed first as requested. Recall is against navigation-stripped body words.",
        "The before/after blocks show the exact compared text; they are unchanged when the",
        "substitution did not fire.",
        "",
    ]
    for item in partial:
        _append_table_case(lines, item)

    lines.extend(
        [
            "## Near-misses and ACI page 30 also stayed unchanged",
            "",
        ]
    )
    for item in remaining:
        _append_table_case(lines, item)

    lines.extend(
        [
            "## Match audit",
            "",
            "The bbox rule counts a match when the table intersection covers at least 80% of",
            "the region area. Exact bbox equality is not used because ruling-line boxes are",
            "usually larger than navigation-stripped word boxes.",
            "",
            f"- Eligible ruled tables: {totals.get('eligible_ruled_tables', 0)}.",
            f"- Tables with no matching region: {totals.get('table_without_region_match', 0)}.",
            f"- One table overlapping several regions: {totals.get('one_table_multiple_regions', 0)}.",
            f"- Regions overlapped by several tables: {totals.get('several_tables_one_region', 0)}.",
            f"- Table-region overlap pairs where the region was not `row_structured`: "
            f"{totals.get('non_row_structured_overlaps', 0)}.",
            f"- Unique geometry matches that failed strict token verification: "
            f"{totals.get('unique_match_verification_failed', 0)}.",
            f"- Successful substitutions: {totals.get('substitutions', 0)}.",
            "",
            "The 24 non-row overlap pairs came from four tables whose ruling lines saw a grid",
            "that the region classifier split into `single_column_prose` or `heading` pieces.",
            "That is a classifier gap, not evidence that the token bars are too strict.",
            "",
            "## Unruled-table review",
            "",
            f"Unruled data-table pages: **{UNRULED_DATA_TABLE_PAGE_COUNT}**.",
            "",
            UNRULED_REVIEW_NOTE,
            "",
            "## Scope and method",
            "",
            "- Scope: all 10 AEO-2024 pages and all 32 ACI-2022 pages.",
            "- Detection: `pdfplumber.find_tables` with vertical and horizontal `lines` strategies.",
            "- Furniture filters: at least 3 non-empty rows and at least 2 columns.",
            "- Verification: existing markdown shape, 0.995 token recall, and 0.005 maximum",
            "  extra-token ratio, measured against one matched region's words.",
            "- Substitution: unique matches only. All other regions stay in their existing order.",
            "- Visual check: 60 dpi renders of all 42 pages for unruled data tables.",
            "",
            "## Limits and robustness checks",
            "",
            "- Region words and raw table cells are not the same source. Raw cell extraction",
            "  produced extra garbled tokens on ACI p30 (0.0778) and ACI p32 (0.0066). These",
            "  exceed the existing bar and were not subtracted or hidden.",
            "- AEO p4 was the only unique geometry match. Its region recall was 0.9797, below",
            "  0.995, because the region still includes text outside the ruled table.",
            "- Multi-region tables were left unchanged. Replacing one piece or merging pieces",
            "  without an explicit table-box split could lose or duplicate words.",
            "- The fresh before/after Bundle 2 pilot JSON files are byte-identical.",
            "",
            "## Recommended next step",
            "",
            "Add a geometry stage that uses a verified ruled table bbox as a region boundary,",
            "then rerun this same strict audit. Do not lower the token bars. The current result",
            "shows that table-to-region alignment, not page-versus-region token scope, is the",
            "main blocker on this sample.",
            "",
            "## Further question",
            "",
            "Can a table-box splitter preserve nearby chart and prose order on AEO p5, p6, p10,",
            "and ACI p14 without creating page-specific rules? That is the smallest follow-up",
            "that could turn the four partial-page negative results into a useful path.",
            "",
            "Source artifacts: `parser_comparison.json`, `parser_comparison.html`, and the two",
            "source PDFs. The visual sweep method and exclusions are recorded above.",
            "",
        ]
    )
    (OUT_DIR / "regions_table_cells_summary.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doc",
        action="append",
        metavar="TICKER:FILE[:PAGES]",
        help="repeatable; PAGES like 1,4,8-12. Defaults to two complex reports.",
    )
    arguments = parser.parse_args()

    specs = arguments.doc or [
        "AEO:AEO-AMERICAN EAGLE OUTFITTERS INC-2024.pdf",
        "ACI:ACI-ALBERTSONS COS INC-2022.pdf",
    ]

    documents = []
    for spec in specs:
        pieces = spec.split(":")
        ticker, pdf_file = pieces[0], pieces[1]
        pages: list[int] | None = None
        if len(pieces) > 2 and pieces[2]:
            pages = []
            for chunk in pieces[2].split(","):
                if "-" in chunk:
                    start, end = chunk.split("-")
                    pages.extend(range(int(start), int(end) + 1))
                else:
                    pages.append(int(chunk))
        print(f"{ticker} {pdf_file}")
        documents.append(run_document(ticker, pdf_file, pages))
    write_report(documents, write_fixed_summary=arguments.doc is None)


if __name__ == "__main__":
    main()
