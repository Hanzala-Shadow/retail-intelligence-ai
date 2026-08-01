"""Prepare a small Terra visual review of recovered current-text pages.

This command is local and free. It never calls an API. It selects recovered
pages from a scored audit, renders their source PDF pages, adds the exact
current parser text, and makes three-page Terra task batches.

``--audit`` must name a file scored by the gate you intend to test. After the
first review changed the gate, that means the re-scored file from
``rescore_esg_recovery_gate.py``, not the original candidate audit: sampling
the latter would draw pages the current gate no longer certifies.

``--labels`` excludes every page that already carries a verdict, and every
document one of those pages came from. A second page from an already-reviewed
report shares its template, so it is a weaker independent test than a page from
a report nobody has looked at.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import _bootstrap  # noqa: F401
import config
import esg_vlm_stage as vlm


DEFAULT_VERSION = "esg_recovery_terra_review_v1"
DEFAULT_AUDIT = (
    config.REPORTS_DIR
    / "esg_recovery_candidate"
    / "esg_page_layout_qa_candidate.csv"
)
DEFAULT_OUT = config.REPORTS_DIR / "esg_recovery_candidate" / "terra_review_v1"
#: Pages already carrying a verdict. A second sample must not re-ask a question
#: that has been answered, and must not be scored against pages the checks were
#: designed while looking at.
DEFAULT_LABELS = config.REFERENCE_DIR / "esg_recovery_safety_labels.csv"
DEFAULT_SAMPLE_SIZE = 12
DEFAULT_BATCH_SIZE = 3
DEFAULT_SEED = 20260731

VERDICTS = {"safe_for_embedding", "unsafe_for_embedding", "needs_review"}
RESULT_FIELDS = {
    "item_id",
    "verdict",
    "text_completeness",
    "paragraph_order",
    "heading_attachment",
    "table_row_value_links",
    "number_claim_links",
    "noise_control",
    "issue_codes",
    "evidence",
    "confidence",
}
CHECK_VALUES = {"pass", "fail", "uncertain", "not_applicable"}
CONFIDENCE_VALUES = {"high", "medium", "low"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(config.REPO_ROOT.resolve()).as_posix()


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config.REPO_ROOT / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_metrics(value: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        try:
            metrics[key] = float(raw)
        except ValueError:
            continue
    return metrics


def page_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("ticker") or "").strip().upper(),
        str(row.get("pdf_file") or "").strip(),
        int(row.get("page") or 0),
    )


def old_gold_keys() -> set[tuple[str, str, int]]:
    path = config.REFERENCE_DIR / "esg_ai_gold_v1.jsonl"
    return {page_key(row) for row in read_jsonl(path)}


def reviewed_keys(labels: Path | None) -> set[tuple[str, str, int]]:
    """Pages that already carry a Terra verdict, from the durable label file."""

    if labels is None or not labels.exists():
        return set()
    return {page_key(row) for row in read_csv(labels)}


def risk_score(row: dict[str, str]) -> tuple[float, ...]:
    metrics = parse_metrics(row.get("recovery_metrics", ""))
    candidate_tokens = max(metrics.get("candidate_tokens", 0.0), 1.0)
    unmatched_ratio = metrics.get("tokens_unmatched", 0.0) / candidate_tokens
    return (
        metrics.get("column_inversions", 0.0),
        unmatched_ratio,
        metrics.get("candidates_passing", 0.0),
        float(row.get("mixed_column_lines") or 0),
    )


def complexity_score(row: dict[str, str]) -> tuple[float, ...]:
    metrics = parse_metrics(row.get("recovery_metrics", ""))
    return (
        metrics.get("segments_consumed", 0.0),
        float(row.get("mixed_column_lines") or 0),
        float(row.get("visual_object_count") or 0),
        float(row.get("native_word_count") or 0),
    )


def pick_unique_issuers(
    rows: list[dict[str, str]],
    count: int,
    *,
    used_pages: set[tuple[str, str, int]],
    used_issuers: set[str],
    score: Callable[[dict[str, str]], tuple[float, ...]] | None = None,
    rng: random.Random | None = None,
) -> list[dict[str, str]]:
    """One page per issuer, never an issuer that has been sampled before.

    The unit is the issuer, not the PDF. A company's 2023 and 2024 reports are
    different documents but are usually the same InDesign template, so two
    pages drawn from them test the geometry rules about as independently as two
    pages of one report -- which is to say, barely.
    """

    candidates = [row for row in rows if page_key(row) not in used_pages]
    if score is not None:
        candidates.sort(key=lambda row: (score(row), page_key(row)), reverse=True)
    elif rng is not None:
        rng.shuffle(candidates)
    chosen: list[dict[str, str]] = []
    for row in candidates:
        issuer = row["ticker"].upper()
        if issuer in used_issuers:
            continue
        chosen.append(row)
        used_pages.add(page_key(row))
        used_issuers.add(issuer)
        if len(chosen) == count:
            return chosen
    raise RuntimeError(f"Could only select {len(chosen)} of {count} unique issuers")


def select_sample(
    audit_rows: list[dict[str, str]],
    sample_size: int,
    seed: int,
    labels: Path | None = None,
    excluded_issuers: set[str] | None = None,
) -> list[dict[str, str]]:
    if sample_size < 6 or sample_size % 3:
        raise ValueError("sample size must be at least 6 and divisible by 3")
    excluded = old_gold_keys() | reviewed_keys(labels)
    recovered = [
        row
        for row in audit_rows
        if row.get("decision") == "auto_pass_recovered_region_order"
        and row.get("recovery_parser") == "current"
        and page_key(row) not in excluded
    ]
    if len(recovered) < sample_size:
        raise RuntimeError(f"Only {len(recovered)} eligible recovered pages found")

    per_stratum = sample_size // 3
    # Issuers already reviewed are avoided as well as the pages themselves.
    used_pages: set[tuple[str, str, int]] = set(excluded)
    used_issuers: set[str] = {key[0] for key in reviewed_keys(labels)}
    used_issuers.update(
        issuer.strip().upper() for issuer in (excluded_issuers or set()) if issuer.strip()
    )
    rng = random.Random(seed)

    selected: list[dict[str, str]] = []
    for stratum, picker in (
        ("boundary_risk", lambda: pick_unique_issuers(
            recovered, per_stratum, used_pages=used_pages,
            used_issuers=used_issuers, score=risk_score
        )),
        ("layout_complex", lambda: pick_unique_issuers(
            recovered, per_stratum, used_pages=used_pages,
            used_issuers=used_issuers, score=complexity_score
        )),
        ("random_control", lambda: pick_unique_issuers(
            recovered, per_stratum, used_pages=used_pages,
            used_issuers=used_issuers, rng=rng
        )),
    ):
        for row in picker():
            selected.append({**row, "review_stratum": stratum})
    return selected


def parse_index_lookup() -> dict[tuple[str, str], dict[str, str]]:
    return {
        (row["ticker"].strip().upper(), Path(row["pdf_file"]).stem): row
        for row in read_csv(Path(config.ESG_PARSE_INDEX_CSV))
        if row.get("status") == "parsed"
    }


def current_page_text(
    row: dict[str, str], lookup: dict[tuple[str, str], dict[str, str]]
) -> tuple[str, dict[str, Any]]:
    key = (row["ticker"].upper(), row["pdf_stem"])
    parse_row = lookup.get(key)
    if not parse_row:
        raise RuntimeError(f"Missing parse-index row for {key}")
    text_path = resolve_repo_path(parse_row["parsed_text_file"])
    map_path = resolve_repo_path(parse_row["page_map_file"])
    page_rows = {int(item["page"]): item for item in read_csv(map_path)}
    page_row = page_rows.get(int(row["page"]))
    if not page_row:
        raise RuntimeError(f"Missing page map for {key} page {row['page']}")
    text = text_path.read_text(encoding="utf-8")
    start, end = int(page_row["char_start"]), int(page_row["char_end"])
    page_text = text[start:end]
    return page_text, {
        "parsed_text_file": parse_row["parsed_text_file"],
        "page_map_file": parse_row["page_map_file"],
        "char_start": start,
        "char_end": end,
        "current_text_sha256": sha256_text(page_text),
    }


def task_prompt() -> str:
    return """# Terra recovered-page visual review

Process only the batch file named in your task. For every JSON row:

1. Open `image_path` and inspect the rendered PDF page carefully.
2. Compare the image with `current_text`.
3. Judge retrieval safety, not visual beauty or exact formatting.
4. Independent panels may appear in a different order when every panel remains
   complete and self-contained.
5. Mark unsafe if paragraphs are interleaved, a heading is attached to the
   wrong body, table labels are detached from values, numbers are attached to
   the wrong claim, important text is missing, or heavy noise breaks meaning.
6. Do not rewrite or improve the page text. Do not use any gold reference.

Write one JSON object per input row to the exact result path named in the task.
Use JSONL with exactly these fields:

```json
{
  "item_id": "recovery_review_001_TICKER_p1",
  "verdict": "safe_for_embedding | unsafe_for_embedding | needs_review",
  "text_completeness": "pass | fail | uncertain",
  "paragraph_order": "pass | fail | uncertain | not_applicable",
  "heading_attachment": "pass | fail | uncertain | not_applicable",
  "table_row_value_links": "pass | fail | uncertain | not_applicable",
  "number_claim_links": "pass | fail | uncertain | not_applicable",
  "noise_control": "pass | fail | uncertain",
  "issue_codes": ["missing_text | invented_text | interleaved_regions | detached_heading | broken_table_links | wrong_number_link | navigation_noise | other_noise"],
  "evidence": "Short, specific reason tied to visible page content",
  "confidence": "high | medium | low"
}
```

Rules:

- Preserve input order and item IDs.
- Use one line per JSON object. Do not wrap the result in Markdown.
- Do not add fields.
- Do not process pages outside the named batch.
- Do not change source, audit, manifest, parser, or queue files.
"""


def prepare(args: argparse.Namespace) -> None:
    audit_rows = read_csv(args.audit.resolve())
    selected = select_sample(
        audit_rows,
        args.sample_size,
        args.seed,
        args.labels,
        set(args.exclude_issuer),
    )
    parse_lookup = parse_index_lookup()
    image_dir = args.out_dir / "images"
    batch_dir = args.out_dir / "batches"
    result_dir = args.out_dir / "results"
    image_dir.mkdir(parents=True, exist_ok=True)
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    queue: list[dict[str, Any]] = []
    for index, row in enumerate(selected, 1):
        item_id = f"{args.item_prefix}_{index:03d}_{row['ticker']}_p{int(row['page'])}"
        image_path = image_dir / f"{item_id}.png"
        source_pdf = resolve_repo_path(row["source_pdf"])
        if args.force or not image_path.exists():
            vlm.render_page(source_pdf, int(row["page"]), image_path)
        page_text, provenance = current_page_text(row, parse_lookup)
        metrics = parse_metrics(row.get("recovery_metrics", ""))
        queue.append({
            "item_id": item_id,
            "version": args.version,
            "review_stratum": row["review_stratum"],
            "ticker": row["ticker"],
            "pdf_file": row["pdf_file"],
            "pdf_stem": row["pdf_stem"],
            "page": int(row["page"]),
            "source_pdf": row["source_pdf"],
            "source_sha256": row["source_sha256"],
            "image_path": repo_path(image_path),
            "image_sha256": sha256_file(image_path),
            "current_text": page_text,
            "current_parser_used": row["current_parser_used"],
            "recovery_parser": row["recovery_parser"],
            "recovery_metrics": row["recovery_metrics"],
            "risk_score": list(risk_score(row)),
            "complexity_score": list(complexity_score(row)),
            "candidate_tokens": int(metrics.get("candidate_tokens", 0)),
            **provenance,
        })

    write_jsonl(args.out_dir / "queue.jsonl", queue)
    batches = [queue[i : i + args.batch_size] for i in range(0, len(queue), args.batch_size)]
    commands = [
        "# Terra task commands",
        "",
        "Open one new Terra task per command. Do not combine batches.",
        "",
    ]
    manifest = []
    for number, batch in enumerate(batches, 1):
        name = f"batch_{number:03d}"
        batch_path = batch_dir / f"{name}.jsonl"
        result_path = result_dir / f"{name}.jsonl"
        write_jsonl(batch_path, batch)
        manifest.append({
            "batch": name,
            "pages": len(batch),
            "batch_path": repo_path(batch_path),
            "result_path": repo_path(result_path),
            "image_bytes": sum(
                resolve_repo_path(row["image_path"]).stat().st_size for row in batch
            ),
        })
        commands.extend([
            f"## {name}",
            "",
            f"Read `{repo_path(args.out_dir / 'TASK_PROMPT.md')}` completely. "
            f"Process only `{repo_path(batch_path)}`. Write the JSONL result to "
            f"`{repo_path(result_path)}`. Do not process any other batch.",
            "",
        ])

    (args.out_dir / "TASK_PROMPT.md").write_text(task_prompt(), encoding="utf-8")
    (args.out_dir / "TERRA_COMMANDS.md").write_text(
        "\n".join(commands), encoding="utf-8"
    )
    write_json(args.out_dir / "sample_summary.json", {
        "version": args.version,
        "seed": args.seed,
        "sample_size": len(queue),
        "documents": len({row["pdf_stem"] for row in queue}),
        "strata": {
            name: sum(row["review_stratum"] == name for row in queue)
            for name in ("boundary_risk", "layout_complex", "random_control")
        },
        "batch_size": args.batch_size,
        "batch_count": len(batches),
        "batches": manifest,
        "paid_api_calls": 0,
    })
    print(f"Prepared {len(queue)} pages across {len(batches)} Terra batches")
    print(f"Queue: {args.out_dir / 'queue.jsonl'}")
    print(f"Commands: {args.out_dir / 'TERRA_COMMANDS.md'}")
    print("No API call was made.")


def summarize(args: argparse.Namespace) -> None:
    queue = read_jsonl(args.out_dir / "queue.jsonl")
    queue_by_id = {row["item_id"]: row for row in queue}
    results: list[dict[str, Any]] = []
    for path in sorted((args.out_dir / "results").glob("batch_*.jsonl")):
        results.extend(read_jsonl(path))
    if len(results) != len(queue):
        raise RuntimeError(f"Expected {len(queue)} result rows, found {len(results)}")
    counts = Counter(row.get("item_id") for row in results)
    duplicates = sorted(item for item, count in counts.items() if count != 1)
    missing = sorted(set(queue_by_id) - set(counts))
    extra = sorted(set(counts) - set(queue_by_id))
    if duplicates or missing or extra:
        raise RuntimeError(
            f"Result ID mismatch: duplicates={duplicates}, missing={missing}, extra={extra}"
        )
    for row in results:
        if set(row) != RESULT_FIELDS:
            raise RuntimeError(f"Unexpected fields for {row.get('item_id')}: {sorted(row)}")
        if row["verdict"] not in VERDICTS:
            raise RuntimeError(f"Bad verdict for {row['item_id']}: {row['verdict']}")
        for field in (
            "text_completeness",
            "paragraph_order",
            "heading_attachment",
            "table_row_value_links",
            "number_claim_links",
            "noise_control",
        ):
            if row[field] not in CHECK_VALUES:
                raise RuntimeError(f"Bad {field} for {row['item_id']}: {row[field]}")
        if row["confidence"] not in CONFIDENCE_VALUES:
            raise RuntimeError(f"Bad confidence for {row['item_id']}: {row['confidence']}")
        if not isinstance(row["issue_codes"], list):
            raise RuntimeError(f"issue_codes is not a list for {row['item_id']}")

    ordered = sorted(results, key=lambda row: list(queue_by_id).index(row["item_id"]))
    write_jsonl(args.out_dir / "terra_review.jsonl", ordered)
    verdicts = Counter(row["verdict"] for row in ordered)
    issues = Counter(code for row in ordered for code in row["issue_codes"])
    by_stratum: dict[str, Counter[str]] = {}
    for row in ordered:
        stratum = queue_by_id[row["item_id"]]["review_stratum"]
        by_stratum.setdefault(stratum, Counter())[row["verdict"]] += 1
    summary = {
        "version": args.version,
        "pages_reviewed": len(ordered),
        "verdicts": dict(verdicts),
        "issue_codes": dict(issues),
        "by_stratum": {key: dict(value) for key, value in sorted(by_stratum.items())},
        "all_results_high_confidence": all(row["confidence"] == "high" for row in ordered),
        "promotion_recommendation": (
            "do_not_promote_candidate_gate"
            if verdicts["unsafe_for_embedding"] or verdicts["needs_review"]
            else "sample_supports_promotion"
        ),
    }
    write_json(args.out_dir / "terra_review_summary.json", summary)
    lines = [
        "# Terra review of recovered current-text pages",
        "",
        f"Reviewed **{len(ordered)}** pages: **{verdicts['safe_for_embedding']} safe**, "
        f"**{verdicts['unsafe_for_embedding']} unsafe**, and "
        f"**{verdicts['needs_review']} needing review**.",
        "",
        "The sample does **not** support promoting the recovery candidate gate "
        "because at least one page marked safe by the deterministic gate was visually unsafe.",
        "",
        "| Item | Stratum | Verdict | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for row in ordered:
        evidence = row["evidence"].replace("|", "/")
        lines.append(
            f"| {row['item_id']} | {queue_by_id[row['item_id']]['review_stratum']} | "
            f"{row['verdict']} | {evidence} |"
        )
    (args.out_dir / "terra_review_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"Validated and merged {len(ordered)} Terra results")
    print(f"Verdicts: {dict(verdicts)}")
    print(f"Summary: {args.out_dir / 'terra_review_summary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument(
        "--item-prefix",
        default="recovery_review",
        help="item_id prefix; must differ per sample so labels stay unique",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="already-reviewed pages to exclude; pass an absent path to disable",
    )
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--exclude-issuer",
        action="append",
        default=[],
        help="issuer ticker to exclude; may be passed more than once",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--summarize", action="store_true", help="validate and merge finished Terra batches"
    )
    args = parser.parse_args()
    if args.summarize:
        summarize(args)
    else:
        prepare(args)


if __name__ == "__main__":
    main()
