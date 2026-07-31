"""Score selected ESG parser pages against the double-reviewed AI gold set.

This evaluator is read-only with respect to parser and corpus data. It writes a
page-level CSV, a machine-readable summary, and a Markdown report.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import difflib
import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import _bootstrap  # noqa: F401
import config
from esg_page_role import apply_navigation_override


VERSION = "esg_parser_ai_gold_eval_v2"
DEFAULT_GOLD = config.REFERENCE_DIR / "esg_ai_gold_v1.jsonl"
DEFAULT_PARSER = (
    config.REPO_ROOT
    / "outputs"
    / "esg_ai_gold_parser_20260731"
    / "selected_page_text.jsonl"
)
DEFAULT_QUEUE = (
    config.REPORTS_DIR
    / "esg_ai_gold_first_pass_v1"
    / "first_ai_queue.jsonl"
)
DEFAULT_OUT = config.REPORTS_DIR / "esg_ai_gold_parser_benchmark_v1"

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./:&'+-][a-z0-9]+)*|[%$€£]", re.IGNORECASE)
NUMBER_RE = re.compile(
    r"(?<![a-z0-9])(?:[$€£])?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?",
    re.IGNORECASE,
)
TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")

# Release-oriented thresholds. They are declared before looking at benchmark
# results and are not fitted to the 20-page holdout split.
FAIL_TOKEN_RECALL = 0.90
FAIL_ORDERED_COVERAGE = 0.85
FAIL_SEQUENCE_F1 = 0.80
FAIL_NUMBER_RECALL = 0.95
FAIL_TABLE_ROW_ORDER = 0.75
FAIL_HEADING_ATTACHMENT = 0.70
REVIEW_TOKEN_RECALL = 0.97
REVIEW_ORDERED_COVERAGE = 0.93
REVIEW_NUMBER_RECALL = 1.00
REVIEW_TOKEN_PRECISION = 0.85
REVIEW_TABLE_ROW_ORDER = 0.90
LOW_VALUE_TOKEN_MAX = 9
# Layout decisions that keep a page out of the retrieval index. This is the
# evaluator's definition of "excluded", so it must track the gate: a navigation
# page counts as blocked only because esg_layout_qa now emits a decision that
# build_esg_vector_manifest routes down the hold path.
EXCLUDED_FROM_INDEX_DECISIONS = frozenset(
    {"auto_hold", "audit_error", "auto_exclude_navigation"}
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {error}") from error
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalized_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("−", "-").replace("–", "-").replace("—", "-")
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and all(not cell or TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells):
                continue
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)
        stripped = stripped.replace("**", "").replace("__", "").replace("`", "")
        stripped = stripped.replace("|", " ")
        lines.append(stripped)
    return re.sub(r"\s+", " ", "\n".join(lines)).strip().lower()


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(normalized_text(text))


def number_tokens(text: str) -> list[str]:
    value = normalized_text(text)
    return [match.group(0).replace(",", "") for match in NUMBER_RE.finditer(value)]


def overlap_metrics(reference: Iterable[str], candidate: Iterable[str]) -> tuple[float, float, float]:
    ref = Counter(reference)
    cand = Counter(candidate)
    overlap = sum(min(count, cand.get(token, 0)) for token, count in ref.items())
    ref_total = sum(ref.values())
    cand_total = sum(cand.values())
    recall = overlap / ref_total if ref_total else 1.0
    precision = overlap / cand_total if cand_total else (1.0 if not ref_total else 0.0)
    f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
    return recall, precision, f1


def sequence_metrics(reference: list[str], candidate: list[str]) -> tuple[float, float, float]:
    if not reference:
        return 1.0, 1.0 if not candidate else 0.0, 1.0 if not candidate else 0.0
    if not candidate:
        return 0.0, 0.0, 0.0
    matcher = difflib.SequenceMatcher(None, reference, candidate, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    recall = matched / len(reference)
    precision = matched / len(candidate)
    f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
    return recall, precision, f1


def bigram_metrics(reference: list[str], candidate: list[str]) -> tuple[float, float]:
    ref = list(zip(reference, reference[1:]))
    cand = list(zip(candidate, candidate[1:]))
    recall, precision, _ = overlap_metrics(ref, cand)
    return recall, precision


def find_subsequence(sequence: list[str], pattern: list[str]) -> int | None:
    if not pattern or len(pattern) > len(sequence):
        return None
    first = pattern[0]
    for index, token in enumerate(sequence):
        if token == first and sequence[index : index + len(pattern)] == pattern:
            return index
    return None


def find_subsequence_positions(sequence: list[str], pattern: list[str]) -> list[int]:
    if not pattern or len(pattern) > len(sequence):
        return []
    first = pattern[0]
    return [
        index
        for index, token in enumerate(sequence)
        if token == first and sequence[index : index + len(pattern)] == pattern
    ]


def anchor_occurrences(
    sequence: list[str], anchor_tokens: list[str]
) -> tuple[tuple[str, ...], list[int]]:
    """Return all matches for the strongest available anchor prefix."""
    for size in range(min(5, len(anchor_tokens)), 0, -1):
        pattern = anchor_tokens[:size]
        positions = find_subsequence_positions(sequence, pattern)
        if positions:
            return tuple(pattern), positions
    return tuple(anchor_tokens[: min(5, len(anchor_tokens))]), []


def anchor_position(sequence: list[str], anchor_tokens: list[str]) -> int | None:
    for size in range(min(5, len(anchor_tokens)), 0, -1):
        position = find_subsequence(sequence, anchor_tokens[:size])
        if position is not None:
            return position
    return None


def longest_increasing_length(values: list[int]) -> int:
    tails: list[int] = []
    for value in values:
        index = bisect.bisect_left(tails, value)
        if index == len(tails):
            tails.append(value)
        else:
            tails[index] = value
    return len(tails)


def table_row_order(reference_markdown: str, candidate_tokens: list[str]) -> tuple[int, float | None]:
    anchors: list[list[str]] = []
    for line in reference_markdown.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(not cell or TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells):
            continue
        first = next((cell for cell in cells if cell), "")
        anchor = tokens(first)
        if anchor:
            anchors.append(anchor)
    if len(anchors) < 3:
        return len(anchors), None
    # A table can contain the same row label more than once. Match the first
    # reference copy to the first candidate copy, the second to the second,
    # and so on. Reusing the first candidate position made correct repeated
    # rows look scrambled.
    seen_patterns: Counter[tuple[str, ...]] = Counter()
    positions: list[int | None] = []
    for anchor in anchors:
        pattern, matches = anchor_occurrences(candidate_tokens, anchor)
        occurrence = seen_patterns[pattern]
        seen_patterns[pattern] += 1
        positions.append(matches[occurrence] if occurrence < len(matches) else None)
    found = [position for position in positions if position is not None]
    if not found:
        return len(anchors), 0.0
    coverage = len(found) / len(anchors)
    monotonic = longest_increasing_length(found) / len(found)
    return len(anchors), coverage * monotonic


def heading_attachment(reference_markdown: str, candidate_tokens: list[str]) -> tuple[int, float | None]:
    lines = [line.strip() for line in reference_markdown.splitlines()]
    pairs: list[tuple[list[str], list[str]]] = []
    for index, line in enumerate(lines):
        if not re.match(r"^#{1,6}\s+", line):
            continue
        heading = tokens(re.sub(r"^#{1,6}\s+", "", line))
        body: list[str] = []
        for following in lines[index + 1 :]:
            if not following or following.startswith("|") or following.startswith("#"):
                continue
            body = tokens(following)
            if body:
                break
        if heading and body:
            pairs.append((heading, body))
    if not pairs:
        return 0, None
    passed = 0
    for heading, body in pairs:
        heading_pos = anchor_position(candidate_tokens, heading)
        body_pos = anchor_position(candidate_tokens, body)
        if (
            heading_pos is not None
            and body_pos is not None
            and heading_pos <= body_pos <= heading_pos + 300
        ):
            passed += 1
    return len(pairs), passed / len(pairs)


def score_page(gold: dict[str, Any], parser: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    reference = gold["reference_markdown"]
    candidate = parser.get("parser_text") or ""
    ref_tokens = tokens(reference)
    cand_tokens = tokens(candidate)
    token_recall, token_precision, token_f1 = overlap_metrics(ref_tokens, cand_tokens)
    ordered_recall, ordered_precision, sequence_f1 = sequence_metrics(ref_tokens, cand_tokens)
    bigram_recall, bigram_precision = bigram_metrics(ref_tokens, cand_tokens)
    ref_numbers = number_tokens(reference)
    cand_numbers = number_tokens(candidate)
    number_recall, number_precision, number_f1 = overlap_metrics(ref_numbers, cand_numbers)
    table_rows, table_score = table_row_order(reference, cand_tokens)
    heading_pairs, heading_score = heading_attachment(reference, cand_tokens)

    reasons: list[str] = []
    warnings: list[str] = []
    # The queue stores the layout decision recorded when the sample was built.
    # Re-apply the page-role rule to the same parser text the gate would see, so
    # the benchmark measures the current gate rather than a frozen verdict. The
    # parser itself is unchanged, so this is the decision a rerun would produce.
    layout_decision, layout_reason, _ = apply_navigation_override(
        queue.get("current_layout_decision", ""),
        queue.get("current_layout_reason", ""),
        candidate,
    )
    reference_use = gold["reference_use"]
    low_value = len(ref_tokens) <= LOW_VALUE_TOKEN_MAX

    if reference_use == "exclude_navigation":
        if layout_decision in EXCLUDED_FROM_INDEX_DECISIONS:
            outcome = "excluded_as_expected"
            severity = "none"
            embedding_safe = True
        else:
            outcome = "fail_navigation_not_excluded"
            severity = "critical"
            embedding_safe = False
            reasons.append(f"gold says exclude_navigation but layout decision is {layout_decision}")
    elif low_value and not cand_tokens:
        outcome = "pass_low_value_text_omitted"
        severity = "low"
        embedding_safe = True
        warnings.append("parser omitted a text-light page with at most 9 reference tokens")
    else:
        if not cand_tokens:
            reasons.append("parser page text is empty")
        if token_recall < FAIL_TOKEN_RECALL:
            reasons.append(f"token_recall={token_recall:.3f} < {FAIL_TOKEN_RECALL:.2f}")
        if ordered_recall < FAIL_ORDERED_COVERAGE:
            reasons.append(f"ordered_coverage={ordered_recall:.3f} < {FAIL_ORDERED_COVERAGE:.2f}")
        if sequence_f1 < FAIL_SEQUENCE_F1:
            reasons.append(f"sequence_f1={sequence_f1:.3f} < {FAIL_SEQUENCE_F1:.2f}")
        if len(ref_numbers) >= 3 and number_recall < FAIL_NUMBER_RECALL:
            reasons.append(f"number_recall={number_recall:.3f} < {FAIL_NUMBER_RECALL:.2f}")
        if table_rows >= 4 and table_score is not None and table_score < FAIL_TABLE_ROW_ORDER:
            reasons.append(f"table_row_order={table_score:.3f} < {FAIL_TABLE_ROW_ORDER:.2f}")
        if heading_pairs >= 2 and heading_score is not None and heading_score < FAIL_HEADING_ATTACHMENT:
            reasons.append(f"heading_attachment={heading_score:.3f} < {FAIL_HEADING_ATTACHMENT:.2f}")

        if reasons:
            outcome = "fail"
            severity = "critical" if not cand_tokens else "high"
            embedding_safe = False
        else:
            if token_recall < REVIEW_TOKEN_RECALL:
                warnings.append(f"token_recall={token_recall:.3f} < {REVIEW_TOKEN_RECALL:.2f}")
            if ordered_recall < REVIEW_ORDERED_COVERAGE:
                warnings.append(f"ordered_coverage={ordered_recall:.3f} < {REVIEW_ORDERED_COVERAGE:.2f}")
            if len(ref_numbers) >= 1 and number_recall < REVIEW_NUMBER_RECALL:
                warnings.append(f"number_recall={number_recall:.3f} < {REVIEW_NUMBER_RECALL:.2f}")
            if token_precision < REVIEW_TOKEN_PRECISION:
                warnings.append(f"token_precision={token_precision:.3f} < {REVIEW_TOKEN_PRECISION:.2f}")
            if table_rows >= 4 and table_score is not None and table_score < REVIEW_TABLE_ROW_ORDER:
                warnings.append(f"table_row_order={table_score:.3f} < {REVIEW_TABLE_ROW_ORDER:.2f}")
            if warnings:
                outcome = "needs_review"
                severity = "medium"
                embedding_safe = False
            else:
                outcome = "pass"
                severity = "none"
                embedding_safe = True

    structure_score = (
        table_score
        if table_score is not None and table_rows >= 4
        else heading_score
        if heading_score is not None
        else ordered_recall
    )
    quality_score = (
        0.35 * token_recall
        + 0.30 * sequence_f1
        + 0.20 * number_recall
        + 0.10 * token_precision
        + 0.05 * structure_score
    )

    return {
        "item_id": gold["item_id"],
        "ticker": gold["ticker"],
        "pdf_file": gold["pdf_file"],
        "page": gold["page"],
        "split": gold["split"],
        "sample_category": gold["sample_category"],
        "page_type": gold["page_type"],
        "canonical_order": gold["canonical_order"],
        "reference_use": reference_use,
        "gold_review_status": gold["review_status"],
        "gold_confidence": gold["confidence"],
        "current_layout_decision": layout_decision,
        "current_layout_reason": layout_reason,
        "parser_used": parser.get("parser_used", ""),
        "parser_policy": parser.get("parser_policy", ""),
        "reference_token_count": len(ref_tokens),
        "parser_token_count": len(cand_tokens),
        "token_recall": token_recall,
        "token_precision": token_precision,
        "token_f1": token_f1,
        "ordered_coverage": ordered_recall,
        "ordered_precision": ordered_precision,
        "sequence_f1": sequence_f1,
        "bigram_recall": bigram_recall,
        "bigram_precision": bigram_precision,
        "reference_number_count": len(ref_numbers),
        "parser_number_count": len(cand_numbers),
        "number_recall": number_recall,
        "number_precision": number_precision,
        "number_f1": number_f1,
        "table_row_count": table_rows,
        "table_row_order": table_score,
        "heading_pair_count": heading_pairs,
        "heading_attachment": heading_score,
        "embedding_quality_score": quality_score,
        "embedding_safe": embedding_safe,
        "outcome": outcome,
        "severity": severity,
        "failure_reasons": "; ".join(reasons),
        "review_warnings": "; ".join(warnings),
        "source_sha256": gold["source_sha256"],
        "image_sha256": gold["image_sha256"],
        "parser_page_text_sha256": parser.get("parser_page_text_sha256", ""),
    }


def median(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) not in (None, "")]
    return statistics.median(values) if values else None


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    output = []
    for value, group in sorted(groups.items()):
        outcomes = Counter(row["outcome"] for row in group)
        output.append({
            key: value,
            "pages": len(group),
            "pass": outcomes["pass"] + outcomes["pass_low_value_text_omitted"] + outcomes["excluded_as_expected"],
            "needs_review": outcomes["needs_review"],
            "fail": outcomes["fail"] + outcomes["fail_navigation_not_excluded"],
            "median_token_recall": median(group, "token_recall"),
            "median_ordered_coverage": median(group, "ordered_coverage"),
            "median_number_recall": median(group, "number_recall"),
            "median_quality_score": median(group, "embedding_quality_score"),
        })
    return output


def markdown_table(rows: list[dict[str, Any]], key: str) -> list[str]:
    lines = [
        f"| {key.replace('_', ' ').title()} | Pages | Pass | Review | Fail | Token recall | Ordered coverage | Number recall | Quality score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row[key]} | {row['pages']} | {row['pass']} | {row['needs_review']} | {row['fail']} | "
            f"{fmt(row['median_token_recall'])} | {fmt(row['median_ordered_coverage'])} | "
            f"{fmt(row['median_number_recall'])} | {fmt(row['median_quality_score'])} |"
        )
    return lines


def write_report(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    content = [row for row in rows if row["reference_use"] == "content"]
    nav = [row for row in rows if row["reference_use"] == "exclude_navigation"]
    failures = sorted(
        [row for row in rows if row["outcome"] in {"fail", "fail_navigation_not_excluded"}],
        key=lambda row: (row["embedding_quality_score"], row["item_id"]),
    )
    reviews = sorted(
        [row for row in rows if row["outcome"] == "needs_review"],
        key=lambda row: (row["embedding_quality_score"], row["item_id"]),
    )
    pass_count = sum(
        row["outcome"] in {"pass", "pass_low_value_text_omitted", "excluded_as_expected"}
        for row in rows
    )
    lines = [
        "# ESG parser benchmark against AI gold v1",
        "",
        "## Technical summary",
        "",
        f"The current parser is **not ready for unrestricted embedding** on this benchmark. "
        f"Of {len(rows)} double-reviewed gold pages, {pass_count} passed, "
        f"{len(reviews)} need review, and {len(failures)} failed the release-oriented gates.",
        "",
        f"The benchmark contains {len(content)} content pages and {len(nav)} pages that the gold set says must be "
        "excluded as navigation. Results are page-level and come from the isolated current-parser rerun.",
        "",
        "## What was measured",
        "",
        "- Token recall: visible reference words recovered, without considering order.",
        "- Ordered coverage and sequence F1: how much text remains in the same sequence.",
        "- Number recall: exact printed numbers, signs, currency, and percentages recovered.",
        "- Table row order: whether table row-label anchors remain in reference order.",
        "- Heading attachment: whether headings remain before their related body text.",
        "- Navigation safety: whether pages marked `exclude_navigation` are blocked.",
        "",
        "The composite quality score is diagnostic only. Release decisions use the individual hard gates recorded in the page CSV.",
        "",
        "## Results by development and holdout split",
        "",
        *markdown_table(summary["by_split"], "split"),
        "",
        "## Results by page category",
        "",
        *markdown_table(summary["by_sample_category"], "sample_category"),
        "",
        "## Highest-risk failures",
        "",
    ]
    if failures:
        lines.extend([
            "| Item | Type | Outcome | Score | Token recall | Ordered coverage | Number recall | Reason |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        for row in failures[:15]:
            reason = (row["failure_reasons"] or row["review_warnings"]).replace("|", "/")
            lines.append(
                f"| {row['item_id']} | {row['page_type']} | {row['outcome']} | "
                f"{row['embedding_quality_score']:.1%} | {row['token_recall']:.1%} | "
                f"{row['ordered_coverage']:.1%} | {row['number_recall']:.1%} | {reason} |"
            )
    else:
        lines.append("No pages failed the hard gates.")
    lines.extend([
        "",
        "## Pages requiring review",
        "",
    ])
    if reviews:
        lines.extend([
            "| Item | Type | Score | Warning |",
            "| --- | --- | ---: | --- |",
        ])
        for row in reviews:
            lines.append(
                f"| {row['item_id']} | {row['page_type']} | {row['embedding_quality_score']:.1%} | "
                f"{row['review_warnings'].replace('|', '/')} |"
            )
    else:
        lines.append("No pages landed in the review band.")
    lines.extend([
        "",
        "## Interpretation and next action",
        "",
        "1. Fix development-set failure classes first. Do not tune rules on the holdout pages.",
        "2. Treat every navigation page as excluded from retrieval, even if its text is readable.",
        "3. Re-run this same evaluator after parser changes and compare page-level deltas.",
        "4. Run the holdout check only after development results meet the release bar.",
        "",
        "## Caveats",
        "",
        "- The gold set is AI-generated and independently AI-reviewed, not fully human-audited.",
        "- Markdown formatting differs from parser plain text; scoring removes Markdown structure before comparison.",
        "- Table-row and heading checks use visible anchor order. They are useful warnings, not proof of semantic equivalence.",
        "- The 59-page sample is risk-stratified and should not be read as the defect rate of the full corpus.",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--parser-pages", type=Path, default=DEFAULT_PARSER)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    gold_rows = read_jsonl(args.gold.resolve())
    parser_rows = read_jsonl(args.parser_pages.resolve())
    queue_rows = read_jsonl(args.queue.resolve())
    parser_by_id = {row["item_id"]: row for row in parser_rows}
    queue_by_id = {row["item_id"]: row for row in queue_rows}
    if len(parser_by_id) != len(parser_rows) or len(queue_by_id) != len(queue_rows):
        raise RuntimeError("Duplicate item_id in parser snapshot or queue")

    scored = []
    for gold in gold_rows:
        item_id = gold["item_id"]
        if item_id not in parser_by_id or item_id not in queue_by_id:
            raise RuntimeError(f"Missing parser or queue row for {item_id}")
        parser_row = parser_by_id[item_id]
        queue_row = queue_by_id[item_id]
        for field in ("ticker", "pdf_file", "page", "source_sha256", "image_sha256"):
            if gold[field] != parser_row[field] or gold[field] != queue_row[field]:
                raise RuntimeError(f"Source mismatch for {item_id}: {field}")
        scored.append(score_page(gold, parser_row, queue_row))

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "page_scores.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scored[0]))
        writer.writeheader()
        for row in scored:
            writer.writerow(row)

    outcomes = Counter(row["outcome"] for row in scored)
    summary = {
        "version": VERSION,
        "gold_pages": len(scored),
        "content_pages": sum(row["reference_use"] == "content" for row in scored),
        "navigation_pages": sum(row["reference_use"] == "exclude_navigation" for row in scored),
        "outcomes": dict(outcomes),
        "embedding_safe_pages": sum(bool(row["embedding_safe"]) for row in scored),
        "median_token_recall": median(scored, "token_recall"),
        "median_token_precision": median(scored, "token_precision"),
        "median_ordered_coverage": median(scored, "ordered_coverage"),
        "median_sequence_f1": median(scored, "sequence_f1"),
        "median_number_recall": median(scored, "number_recall"),
        "median_quality_score": median(scored, "embedding_quality_score"),
        "by_split": group_summary(scored, "split"),
        "by_sample_category": group_summary(scored, "sample_category"),
        "thresholds": {
            "fail_token_recall": FAIL_TOKEN_RECALL,
            "fail_ordered_coverage": FAIL_ORDERED_COVERAGE,
            "fail_sequence_f1": FAIL_SEQUENCE_F1,
            "fail_number_recall": FAIL_NUMBER_RECALL,
            "fail_table_row_order": FAIL_TABLE_ROW_ORDER,
            "fail_heading_attachment": FAIL_HEADING_ATTACHMENT,
        },
        "sources": {
            "gold": str(args.gold.resolve()),
            "parser_pages": str(args.parser_pages.resolve()),
            "queue": str(args.queue.resolve()),
        },
    }
    write_json(out_dir / "summary.json", summary)
    write_report(out_dir / "report.md", scored, summary)
    print(f"Scored {len(scored)} gold pages")
    print(f"Outcomes: {dict(outcomes)}")
    print(f"Page scores: {csv_path}")
    print(f"Report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
