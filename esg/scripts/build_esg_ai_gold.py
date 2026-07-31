r"""Prepare and run the first-AI pass for a small ESG parser gold set.

The first AI sees only the rendered PDF page. It does not see parser output.
This prevents the reference transcription from copying parser mistakes.

Examples, from the repository root::

    .\venv\Scripts\python.exe esg\scripts\build_esg_ai_gold.py prepare
    $env:OPENAI_API_KEY = "..."
    .\venv\Scripts\python.exe esg\scripts\build_esg_ai_gold.py generate --budget 10

``prepare`` is free. ``generate`` makes paid OpenAI API calls and refuses to
run when its estimated cost is above ``--budget``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import config
import esg_vlm_stage as vlm


VERSION = "esg_ai_gold_first_pass_v1"
SEED = 20260731
DEFAULT_OUT_DIR = config.REPORTS_DIR / VERSION
DEFAULT_TARGET = 60
DEFAULT_BUDGET_USD = 10.0
DEFAULT_BATCH_PAGES = 3
DEFAULT_BATCH_MAX_MB = 4.0
LUNA_DIFFICULT_IDS = {
    "gold_024_WMT_p89",
    "gold_034_ETSY_p17",
    "gold_056_PTRN_p132",
}

CATEGORY_QUOTAS = {
    "known_layout_risk": 15,
    "column_reconstructed": 10,
    "table_or_grid": 10,
    "pdfium_fallback": 5,
    "navigation_contents": 5,
    "clean_control": 15,
}

FIRST_AI_PROMPT = """You are creating a reference transcription for one rendered page from a corporate ESG report. The page image is the only source of truth. Do not guess hidden or unreadable text.

Tasks:
1. Classify the page.
2. Transcribe all visible semantic text in its natural reading order.
3. Preserve every printed number, unit, sign, currency symbol, date, name, and percentage exactly.
4. Keep headings with their body text.
5. For multi-column prose, finish one logical column or region before moving to the next.
6. For tables and grids, use a markdown table when row and column links are clear. Never detach a row label from its values.
7. Include table-of-contents entries because this is a parser benchmark, but mark the page as exclude_navigation.
8. Skip repeated running headers, repeated running footers, and bare page numbers.
9. For charts, transcribe only labels and values that are clearly readable. Put unclear text in uncertain_spans instead of guessing.
10. Do not summarize, improve, explain, or add facts.

Return JSON only with exactly these fields:
{
  "page_type": "prose_single" | "prose_multicolumn" | "table_or_grid" | "mixed" | "navigation_contents" | "chart_graphic" | "text_light",
  "canonical_order": "top_to_bottom" | "column_major" | "row_major" | "region_order" | "not_applicable",
  "reference_use": "content" | "exclude_navigation" | "hold_visual_only",
  "reference_markdown": "complete transcription in natural reading order",
  "uncertain_spans": ["short exact descriptions of unreadable areas"],
  "confidence": "high" | "medium" | "low"
}
"""

FIRST_AI_CONFIG = vlm.StageConfig(
    stage="ai_gold_first_pass",
    instr=FIRST_AI_PROMPT,
    reasoning_effort="low",
    max_completion_tokens=12000,
    json_mode=True,
)

REQUIRED_RESULT_FIELDS = {
    "page_type",
    "canonical_order",
    "reference_use",
    "reference_markdown",
    "uncertain_spans",
    "confidence",
}
PAGE_TYPES = {
    "prose_single",
    "prose_multicolumn",
    "table_or_grid",
    "mixed",
    "navigation_contents",
    "chart_graphic",
    "text_light",
}
CANONICAL_ORDERS = {
    "top_to_bottom",
    "column_major",
    "row_major",
    "region_order",
    "not_applicable",
}
REFERENCE_USES = {"content", "exclude_navigation", "hold_visual_only"}
CONFIDENCE_VALUES = {"high", "medium", "low"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def as_repo_path(path: Path) -> str:
    return str(path.resolve().relative_to(config.REPO_ROOT.resolve())).replace("\\", "/")


def page_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("ticker", "")).strip().upper(),
        str(row.get("pdf_file", "")).strip(),
        int(row.get("page", 0)),
    )


def load_gold_labels() -> dict[tuple[str, str, int], dict[str, str]]:
    path = config.REFERENCE_DIR / "esg_layout_gold_labels.csv"
    return {page_key(row): row for row in read_csv(path)}


def is_pdfium(row: dict[str, str]) -> bool:
    return (
        "pdfium" in (row.get("page_map_repair_method") or "").lower()
        or "pdfium" in (row.get("current_parser_used") or "").lower()
        or "pdfium" in (row.get("candidate_preference") or "").lower()
    )


def category_candidates(
    audit_rows: list[dict[str, str]],
    gold: dict[tuple[str, str, int], dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    candidates: dict[str, list[dict[str, str]]] = {name: [] for name in CATEGORY_QUOTAS}
    for row in audit_rows:
        decision = (row.get("decision") or "").strip()
        parse_status = (row.get("page_map_parse_status") or "").strip()
        table_count = int(row.get("page_map_table_candidate_count") or 0)
        words = int(row.get("native_word_count") or 0)
        old_gold = gold.get(page_key(row))

        if old_gold and old_gold.get("decision_class") in {"table_dominant", "ambiguous_or_mixed"}:
            candidates["known_layout_risk"].append(row)
        if decision in {"auto_pass_column_order_reconstructed", "auto_pass_region_order_reconstructed"}:
            candidates["column_reconstructed"].append(row)
        if table_count > 0 or decision == "auto_pass_verified_table_extraction":
            candidates["table_or_grid"].append(row)
        if is_pdfium(row):
            candidates["pdfium_fallback"].append(row)
        if decision == "auto_pass_navigation_contents":
            candidates["navigation_contents"].append(row)
        if (
            decision == "auto_pass"
            and parse_status == "ok"
            and table_count == 0
            and words >= 100
        ):
            candidates["clean_control"].append(row)
    return candidates


def select_rows(
    audit_rows: list[dict[str, str]],
    gold: dict[tuple[str, str, int], dict[str, str]],
    *,
    seed: int = SEED,
    target: int = DEFAULT_TARGET,
) -> list[dict[str, Any]]:
    if target != sum(CATEGORY_QUOTAS.values()):
        raise ValueError(f"This v1 selector requires --target {sum(CATEGORY_QUOTAS.values())}.")

    rng = random.Random(seed)
    pools = category_candidates(audit_rows, gold)
    used: set[tuple[str, str, int]] = set()
    per_document: Counter[tuple[str, str]] = Counter()
    selected: list[dict[str, Any]] = []

    for category, quota in CATEGORY_QUOTAS.items():
        pool = pools[category][:]
        rng.shuffle(pool)
        chosen: list[dict[str, str]] = []
        for max_per_document in (3, 5, 1000):
            for row in pool:
                key = page_key(row)
                document = key[:2]
                if key in used or row in chosen or per_document[document] >= max_per_document:
                    continue
                source_pdf = config.REPO_ROOT / (row.get("source_pdf") or "")
                if not source_pdf.is_file():
                    continue
                chosen.append(row)
                used.add(key)
                per_document[document] += 1
                if len(chosen) == quota:
                    break
            if len(chosen) == quota:
                break
        if len(chosen) != quota:
            raise RuntimeError(
                f"Could select only {len(chosen)}/{quota} pages for {category}."
            )
        for index, row in enumerate(chosen):
            copied: dict[str, Any] = dict(row)
            copied["sample_category"] = category
            copied["split"] = "development" if index < round(quota * 2 / 3) else "holdout"
            old_gold = gold.get(page_key(row), {})
            copied["prior_gold_class"] = old_gold.get("decision_class", "")
            copied["prior_gold_subtype"] = old_gold.get("subtype", "")
            selected.append(copied)

    if len(selected) != target or len({page_key(row) for row in selected}) != target:
        raise RuntimeError("AI-gold selection is not unique or has the wrong size.")
    return selected


def parse_index_lookup(path: Path | None = None) -> dict[tuple[str, str], dict[str, str]]:
    lookup = {}
    for row in read_csv(path or config.ESG_PARSE_INDEX_CSV):
        lookup[(row["ticker"].strip().upper(), Path(row["pdf_file"]).stem)] = row
    return lookup


def parsed_page_text(
    row: dict[str, Any],
    parse_lookup: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, dict[str, Any]]:
    key = (row["ticker"].strip().upper(), row["pdf_stem"].strip())
    parse_row = parse_lookup.get(key)
    if not parse_row:
        raise RuntimeError(f"Missing parse-index row for {key}")
    text_path = config.REPO_ROOT / parse_row["parsed_text_file"]
    map_path = config.REPO_ROOT / parse_row["page_map_file"]
    page_rows = {int(item["page"]): item for item in read_csv(map_path)}
    page_row = page_rows.get(int(row["page"]))
    if not page_row:
        raise RuntimeError(f"Missing page map for {key} page {row['page']}")
    text = text_path.read_text(encoding="utf-8")
    start = int(page_row["char_start"])
    end = int(page_row["char_end"])
    return text[start:end], {
        "parsed_text_file": parse_row["parsed_text_file"],
        "page_map_file": parse_row["page_map_file"],
        "char_start": start,
        "char_end": end,
        "parsed_text_sha256": parse_row["content_hash"],
        "parser_used": parse_row["parser_used"],
        "parser_policy": parse_row["parser_policy"],
    }


def prepare(args: argparse.Namespace) -> None:
    audit_rows = read_csv(config.ESG_PAGE_LAYOUT_QA_CSV)
    gold = load_gold_labels()
    rows = select_rows(audit_rows, gold, seed=args.seed, target=args.target)
    parse_lookup = parse_index_lookup()
    image_dir = args.out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    queue = []
    for index, row in enumerate(rows, 1):
        source_pdf = config.REPO_ROOT / row["source_pdf"]
        item_id = f"gold_{index:03d}_{row['ticker']}_p{int(row['page'])}"
        image_path = image_dir / f"{item_id}.png"
        if args.force or not image_path.exists():
            vlm.render_page(source_pdf, int(row["page"]), image_path)
        parser_text, provenance = parsed_page_text(row, parse_lookup)
        queue.append({
            "item_id": item_id,
            "version": VERSION,
            "split": row["split"],
            "sample_category": row["sample_category"],
            "ticker": row["ticker"],
            "pdf_file": row["pdf_file"],
            "pdf_stem": row["pdf_stem"],
            "page": int(row["page"]),
            "source_pdf": row["source_pdf"],
            "source_sha256": row["source_sha256"],
            "image_path": as_repo_path(image_path),
            "image_sha256": sha256_file(image_path),
            "current_layout_decision": row["decision"],
            "current_layout_reason": row["decision_reason"],
            "prior_gold_class": row["prior_gold_class"],
            "prior_gold_subtype": row["prior_gold_subtype"],
            "parser_text": parser_text,
            **provenance,
        })

    write_jsonl(args.out_dir / "first_ai_queue.jsonl", queue)
    write_json(args.out_dir / "first_ai_prompt.json", {
        "version": VERSION,
        "model": FIRST_AI_CONFIG.model,
        "prompt_hash": FIRST_AI_CONFIG.instr_hash,
        "prompt": FIRST_AI_PROMPT,
    })
    write_json(args.out_dir / "sample_summary.json", {
        "version": VERSION,
        "seed": args.seed,
        "target": len(queue),
        "categories": dict(Counter(row["sample_category"] for row in queue)),
        "splits": dict(Counter(row["split"] for row in queue)),
        "documents": len({(row["ticker"], row["pdf_file"]) for row in queue}),
    })
    print(f"Prepared {len(queue)} pages in {args.out_dir}")
    print("No API call was made.")


def parse_first_ai_result(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if set(value) != REQUIRED_RESULT_FIELDS:
        raise ValueError(f"Unexpected result fields: {sorted(value)}")
    if value["page_type"] not in PAGE_TYPES:
        raise ValueError(f"Unexpected page_type: {value['page_type']!r}")
    if value["canonical_order"] not in CANONICAL_ORDERS:
        raise ValueError(f"Unexpected canonical_order: {value['canonical_order']!r}")
    if value["reference_use"] not in REFERENCE_USES:
        raise ValueError(f"Unexpected reference_use: {value['reference_use']!r}")
    if value["confidence"] not in CONFIDENCE_VALUES:
        raise ValueError(f"Unexpected confidence: {value['confidence']!r}")
    if not isinstance(value["reference_markdown"], str):
        raise ValueError("reference_markdown must be a string")
    if not isinstance(value["uncertain_spans"], list):
        raise ValueError("uncertain_spans must be a list")
    return value


def generate(args: argparse.Namespace) -> None:
    queue_path = args.out_dir / "first_ai_queue.jsonl"
    if not queue_path.exists():
        raise RuntimeError(f"Missing {queue_path}; run prepare first.")
    queue = read_jsonl(queue_path)
    artifact_dir = args.out_dir / "first_ai_results"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    todo = []
    by_id = {row["item_id"]: row for row in queue}
    for row in queue:
        artifact = artifact_dir / f"{row['item_id']}.json"
        cache_key = vlm.cache_key(row["source_sha256"], row["page"], FIRST_AI_CONFIG)
        if artifact.exists():
            saved = json.loads(artifact.read_text(encoding="utf-8"))
            if saved.get("cache_key") == cache_key:
                continue
        image_path = config.REPO_ROOT / row["image_path"]
        todo.append((row["item_id"], vlm.request_body(FIRST_AI_CONFIG, image_path.read_bytes())))

    if args.limit:
        todo = todo[: args.limit]
    estimate = vlm.estimate_cost_usd(len(todo), FIRST_AI_CONFIG, "sync")
    print(f"First AI: {len(todo)} uncached pages; estimated cost ${estimate:.2f}")
    if estimate > args.budget:
        raise RuntimeError(
            f"Estimated cost ${estimate:.2f} exceeds --budget ${args.budget:.2f}."
        )
    if args.dry_run:
        print("Dry run only. No API call was made.")
        return

    def on_result(item_id: str, content: str | None, usage: dict[str, Any]) -> None:
        row = by_id[item_id]
        cache_key = vlm.cache_key(row["source_sha256"], row["page"], FIRST_AI_CONFIG)
        result: dict[str, Any] = {
            **{key: value for key, value in row.items() if key != "parser_text"},
            "model": FIRST_AI_CONFIG.model,
            "prompt_hash": FIRST_AI_CONFIG.instr_hash,
            "cache_key": cache_key,
            "usage": usage,
        }
        if content is None:
            result["status"] = "api_error"
            result["error"] = usage.get("error", "no content")
        else:
            try:
                result["reference"] = parse_first_ai_result(content)
                result["status"] = "ok"
            except Exception as error:  # Keep bad responses for inspection.
                result["status"] = "invalid_response"
                result["error"] = f"{type(error).__name__}: {error}"
                result["raw_response"] = content
        write_json(artifact_dir / f"{item_id}.json", result)

    if todo:
        vlm.run_sync(todo, on_result, workers=args.workers)

    results = []
    for row in queue:
        artifact = artifact_dir / f"{row['item_id']}.json"
        if artifact.exists():
            results.append(json.loads(artifact.read_text(encoding="utf-8")))
    write_jsonl(args.out_dir / "first_ai_results.jsonl", results)
    counts = Counter(row.get("status", "missing") for row in results)
    print(f"Saved {len(results)}/{len(queue)} results: {dict(counts)}")


def snapshot(args: argparse.Namespace) -> None:
    """Save current parser text for only the 60 selected benchmark pages."""
    queue_path = args.out_dir / "first_ai_queue.jsonl"
    if not queue_path.exists():
        raise RuntimeError(f"Missing {queue_path}; run prepare first.")
    parse_index = args.parse_index.resolve()
    if not parse_index.exists():
        raise RuntimeError(f"Missing parser index: {parse_index}")
    queue = read_jsonl(queue_path)
    parse_lookup = parse_index_lookup(parse_index)
    rows = []
    for item in queue:
        text, provenance = parsed_page_text(item, parse_lookup)
        rows.append({
            "item_id": item["item_id"],
            "ticker": item["ticker"],
            "pdf_file": item["pdf_file"],
            "pdf_stem": item["pdf_stem"],
            "page": item["page"],
            "split": item["split"],
            "sample_category": item["sample_category"],
            "source_sha256": item["source_sha256"],
            "image_sha256": item["image_sha256"],
            "parser_text": text,
            "parser_page_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **provenance,
        })
    if len(rows) != len(queue) or len({row["item_id"] for row in rows}) != len(queue):
        raise RuntimeError("Parser snapshot is incomplete or has duplicate item IDs.")
    snapshot_out = args.snapshot_out.resolve()
    write_jsonl(snapshot_out, rows)
    print(f"Saved {len(rows)} selected parser pages to {snapshot_out}")


def make_batches(args: argparse.Namespace) -> None:
    """Split unfinished Terra work into small image-safe task batches."""
    queue_path = args.out_dir / "first_ai_queue.jsonl"
    if not queue_path.exists():
        raise RuntimeError(f"Missing {queue_path}; run prepare first.")
    queue = read_jsonl(queue_path)
    completed_path = args.out_dir / "terra_first_pass.jsonl"
    completed_ids: set[str] = set()
    if completed_path.exists():
        completed_ids.update(row["item_id"] for row in read_jsonl(completed_path))

    batch_dir = args.out_dir / "terra_batches"
    result_dir = batch_dir / "results"
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    for result_path in result_dir.glob("batch_*.jsonl"):
        completed_ids.update(row["item_id"] for row in read_jsonl(result_path))

    remaining = [row for row in queue if row["item_id"] not in completed_ids]
    max_bytes = int(args.batch_max_mb * 1024 * 1024)
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for row in remaining:
        image_path = config.REPO_ROOT / row["image_path"]
        image_bytes = image_path.stat().st_size
        if image_bytes > max_bytes:
            raise RuntimeError(
                f"Image exceeds one-batch limit ({image_bytes} bytes): {image_path}"
            )
        if current and (
            len(current) >= args.batch_pages or current_bytes + image_bytes > max_bytes
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        slim = {key: value for key, value in row.items() if key != "parser_text"}
        slim["image_bytes"] = image_bytes
        current.append(slim)
        current_bytes += image_bytes
    if current:
        batches.append(current)

    manifest_rows = []
    for index, rows in enumerate(batches, 1):
        name = f"batch_{index:03d}"
        path = batch_dir / f"{name}.jsonl"
        write_jsonl(path, rows)
        manifest_rows.append({
            "batch": name,
            "input": as_repo_path(path),
            "output": as_repo_path(result_dir / f"{name}.jsonl"),
            "pages": len(rows),
            "image_bytes": sum(row["image_bytes"] for row in rows),
            "item_ids": [row["item_id"] for row in rows],
        })
    write_json(batch_dir / "batch_manifest.json", {
        "version": VERSION,
        "completed_before_batching": len(completed_ids),
        "remaining_pages": len(remaining),
        "batch_pages_limit": args.batch_pages,
        "batch_image_mb_limit": args.batch_max_mb,
        "batch_count": len(batches),
        "batches": manifest_rows,
    })
    (batch_dir / "TASK_PROMPT.md").write_text(
        "# Terra batch task\n\n"
        "Process only the batch file named in the task. Open each `image_path` and "
        "inspect the page image. Do not open parser text or parsed-text files. "
        "Transcribe; do not summarize. Preserve all visible numbers, units, signs, "
        "dates, names, headings, and table row-to-value links. Skip bare page numbers "
        "and repeated running headers or footers. Never guess unreadable text.\n\n"
        "Write one JSONL row per input item to the exact output file named in the task. "
        "Copy all input metadata except `image_bytes`, then add: `status` (`ok` or "
        "`needs_review`), `page_type`, `canonical_order`, `reference_use`, "
        "`reference_markdown`, `uncertain_spans`, and `confidence`. Use the enum values "
        "defined in `reports/ESG_AI_GOLD_HANDOFF_2026-07-31.md`. Validate the row count, "
        "unique item IDs, and JSON syntax. Do not process any page outside the batch.\n",
        encoding="utf-8",
    )
    print(
        f"Prepared {len(batches)} Terra batches for {len(remaining)} unfinished pages "
        f"({len(completed_ids)} already complete)."
    )


def merge_batches(args: argparse.Namespace) -> None:
    """Merge restart-safe Terra batch outputs into the main first-pass file."""
    queue = read_jsonl(args.out_dir / "first_ai_queue.jsonl")
    expected = {row["item_id"] for row in queue}
    sources = []
    main_path = args.out_dir / "terra_first_pass.jsonl"
    if main_path.exists():
        sources.append(main_path)
    result_dir = args.out_dir / "terra_batches" / "results"
    if result_dir.exists():
        sources.extend(sorted(result_dir.glob("batch_*.jsonl")))
    merged: dict[str, dict[str, Any]] = {}
    for path in sources:
        for row in read_jsonl(path):
            item_id = row.get("item_id")
            if item_id not in expected:
                raise RuntimeError(f"Unknown item_id {item_id!r} in {path}")
            if item_id in merged and merged[item_id] != row:
                raise RuntimeError(f"Conflicting duplicate item_id {item_id!r}")
            merged[item_id] = row
    ordered = [merged[row["item_id"]] for row in queue if row["item_id"] in merged]
    write_jsonl(main_path, ordered)
    write_json(args.out_dir / "terra_first_pass_summary.json", {
        "version": VERSION,
        "expected_pages": len(queue),
        "completed_pages": len(ordered),
        "missing_item_ids": [row["item_id"] for row in queue if row["item_id"] not in merged],
        "status": dict(Counter(row.get("status", "missing") for row in ordered)),
        "page_type": dict(Counter(row.get("page_type", "missing") for row in ordered)),
        "confidence": dict(Counter(row.get("confidence", "missing") for row in ordered)),
        "sample_category": dict(Counter(row.get("sample_category", "missing") for row in ordered)),
        "split": dict(Counter(row.get("split", "missing") for row in ordered)),
    })
    print(f"Merged {len(ordered)}/{len(queue)} Terra rows into {main_path}")


def luna_prompt(item_ids: list[str], output_path: str, effort: str) -> str:
    ids = "\n".join(f"- {item_id}" for item_id in item_ids)
    difficult_rules = ""
    if effort == "xhigh":
        difficult_rules = """
These pages were uncertain in the first pass. If needed, render the original
PDF page at higher resolution or inspect a crop. Do not use the PDF text layer
as the answer. Keep `needs_review` when the image cannot prove the text.
"""
    return f"""# Luna ESG gold review

**Model:** Luna  
**Effort:** {effort}

Review only these ESG AI-gold references:

{ids}

Inputs:

- `reports/esg_ai_gold_first_pass_v1/first_ai_queue.jsonl`
- `reports/esg_ai_gold_first_pass_v1/terra_first_pass.jsonl`

For each item:

1. Find its metadata and `image_path` in `first_ai_queue.jsonl`.
2. Find Terra's reference in `terra_first_pass.jsonl`.
3. Open and inspect the page image.
4. Do not read `parser_text` or parsed-text files.
5. Check for missing or invented text, wrong reading order, detached headings,
   broken table row/value links, wrong numbers or units, navigation errors, and
   repeated page furniture.
6. Correct clear errors directly.
7. Never guess unreadable text.
8. Mark the page `needs_review` when the image is not clear enough.
{difficult_rules}
Write exactly {len(item_ids)} JSONL rows to:

- `{output_path}`

Use this schema for every row:

```json
{{
  "item_id": "...",
  "ticker": "...",
  "pdf_file": "...",
  "page": 1,
  "source_sha256": "...",
  "image_sha256": "...",
  "split": "development | holdout",
  "sample_category": "...",
  "review_status": "accepted | corrected | needs_review",
  "error_types": [
    "missing_text | invented_text | wrong_order | heading_detached | table_misaligned | numeric_error | navigation_error | noise"
  ],
  "review_notes": "short concrete explanation",
  "page_type": "...",
  "canonical_order": "...",
  "reference_use": "...",
  "reference_markdown": "accepted or corrected complete transcription",
  "uncertain_spans": [],
  "confidence": "high | medium | low"
}}
```

Validate before finishing:

- Exactly {len(item_ids)} valid JSON rows.
- Exactly the requested item IDs and no duplicates.
- Source and image hashes are unchanged.
- Every row has non-empty `reference_markdown`.
- Do not process other pages.
- Do not evaluate the parser.
"""


def make_luna_prompts(args: argparse.Namespace) -> None:
    queue = read_jsonl(args.out_dir / "first_ai_queue.jsonl")
    normal = [row for row in queue if row["item_id"] not in LUNA_DIFFICULT_IDS]
    difficult = [row for row in queue if row["item_id"] in LUNA_DIFFICULT_IDS]
    if len(normal) != 57 or len(difficult) != 3:
        raise RuntimeError(
            f"Expected 57 normal and 3 difficult pages; got {len(normal)} and {len(difficult)}."
        )

    prompt_dir = args.out_dir / "luna_review" / "prompts"
    result_dir = args.out_dir / "luna_review" / "results"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index in range(0, len(normal), 3):
        number = index // 3 + 1
        name = f"batch_{number:03d}"
        item_ids = [row["item_id"] for row in normal[index : index + 3]]
        output_path = as_repo_path(result_dir / f"{name}.jsonl")
        prompt_path = prompt_dir / f"{name}_high.md"
        prompt_path.write_text(luna_prompt(item_ids, output_path, "high"), encoding="utf-8")
        manifest.append({
            "batch": name,
            "effort": "high",
            "prompt": as_repo_path(prompt_path),
            "output": output_path,
            "item_ids": item_ids,
        })

    difficult_ids = [row["item_id"] for row in difficult]
    difficult_output = as_repo_path(result_dir / "difficult_pages.jsonl")
    difficult_prompt = prompt_dir / "difficult_pages_xhigh.md"
    difficult_prompt.write_text(
        luna_prompt(difficult_ids, difficult_output, "xhigh"),
        encoding="utf-8",
    )
    manifest.append({
        "batch": "difficult_pages",
        "effort": "xhigh",
        "prompt": as_repo_path(difficult_prompt),
        "output": difficult_output,
        "item_ids": difficult_ids,
    })
    write_json(args.out_dir / "luna_review" / "prompt_manifest.json", {
        "normal_prompt_count": 19,
        "difficult_prompt_count": 1,
        "prompts": manifest,
    })
    print(f"Created 19 High prompts and 1 Xhigh prompt in {prompt_dir}")


def merge_luna(args: argparse.Namespace) -> None:
    """Validate Luna review outputs and publish accepted/corrected gold rows."""
    queue = read_jsonl(args.out_dir / "first_ai_queue.jsonl")
    queue_by_id = {row["item_id"]: row for row in queue}
    review_root = args.out_dir / "luna_review"
    sources = sorted((review_root / "results").glob("batch_*.jsonl"))
    difficult_path = review_root / "difficult_pages.jsonl"
    if difficult_path.exists():
        sources.append(difficult_path)

    reviewed: dict[str, dict[str, Any]] = {}
    required = {
        "item_id",
        "ticker",
        "pdf_file",
        "page",
        "source_sha256",
        "image_sha256",
        "split",
        "sample_category",
        "review_status",
        "error_types",
        "review_notes",
        "page_type",
        "canonical_order",
        "reference_use",
        "reference_markdown",
        "uncertain_spans",
        "confidence",
    }
    for path in sources:
        for row in read_jsonl(path):
            item_id = row.get("item_id", "")
            if item_id not in queue_by_id:
                raise RuntimeError(f"Unknown Luna item_id {item_id!r} in {path}")
            if item_id in reviewed:
                raise RuntimeError(f"Duplicate Luna item_id {item_id!r}")
            missing = required - set(row)
            if missing:
                raise RuntimeError(f"Missing Luna fields for {item_id}: {sorted(missing)}")
            source = queue_by_id[item_id]
            for field in (
                "ticker",
                "pdf_file",
                "page",
                "source_sha256",
                "image_sha256",
                "split",
                "sample_category",
            ):
                if row[field] != source[field]:
                    raise RuntimeError(f"Luna changed {field} for {item_id}")
            if row["review_status"] not in {"accepted", "corrected", "needs_review"}:
                raise RuntimeError(f"Bad review_status for {item_id}: {row['review_status']!r}")
            if not str(row["reference_markdown"]).strip():
                raise RuntimeError(f"Empty Luna reference for {item_id}")
            reviewed[item_id] = row

    missing_ids = [row["item_id"] for row in queue if row["item_id"] not in reviewed]
    if missing_ids:
        raise RuntimeError(f"Missing {len(missing_ids)} Luna rows: {missing_ids}")

    ordered = [reviewed[row["item_id"]] for row in queue]
    second_review_path = args.out_dir / "second_ai_review.jsonl"
    write_jsonl(second_review_path, ordered)

    final_rows = []
    for row in ordered:
        if row["review_status"] not in {"accepted", "corrected"}:
            continue
        final_rows.append({
            **row,
            "gold_version": "esg_ai_gold_v1",
            "gold_status": "verified_ai_double_pass",
            "first_pass_model": "gpt-5.6-terra",
            "review_model": "luna",
        })
    final_path = config.REFERENCE_DIR / "esg_ai_gold_v1.jsonl"
    write_jsonl(final_path, final_rows)

    error_counts: Counter[str] = Counter()
    for row in ordered:
        error_counts.update(row.get("error_types") or [])
    summary = {
        "version": "esg_ai_gold_v1",
        "reviewed_pages": len(ordered),
        "final_gold_pages": len(final_rows),
        "excluded_needs_review": len(ordered) - len(final_rows),
        "review_status": dict(Counter(row["review_status"] for row in ordered)),
        "confidence": dict(Counter(row["confidence"] for row in ordered)),
        "sample_category": dict(Counter(row["sample_category"] for row in ordered)),
        "split": dict(Counter(row["split"] for row in ordered)),
        "error_types": dict(error_counts),
        "excluded_item_ids": [
            row["item_id"] for row in ordered if row["review_status"] == "needs_review"
        ],
        "second_ai_review_file": as_repo_path(second_review_path),
        "final_gold_file": as_repo_path(final_path),
    }
    write_json(review_root / "second_ai_summary.json", summary)
    print(
        f"Validated {len(ordered)} Luna reviews; published {len(final_rows)} gold pages "
        f"and excluded {len(ordered) - len(final_rows)} needs-review page(s)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "generate",
            "snapshot",
            "make-batches",
            "merge-batches",
            "make-luna-prompts",
            "merge-luna",
        ],
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--force", action="store_true", help="Re-render existing page images.")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", help="Show work and cost without an API call.")
    parser.add_argument("--parse-index", type=Path, default=config.ESG_PARSE_INDEX_CSV)
    parser.add_argument(
        "--snapshot-out",
        type=Path,
        default=config.REPORTS_DIR / VERSION / "parser_snapshot.jsonl",
    )
    parser.add_argument("--batch-pages", type=int, default=DEFAULT_BATCH_PAGES)
    parser.add_argument("--batch-max-mb", type=float, default=DEFAULT_BATCH_MAX_MB)
    args = parser.parse_args()
    args.out_dir = args.out_dir.resolve()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "generate":
        generate(args)
    elif args.command == "snapshot":
        snapshot(args)
    elif args.command == "make-batches":
        make_batches(args)
    elif args.command == "merge-batches":
        merge_batches(args)
    elif args.command == "make-luna-prompts":
        make_luna_prompts(args)
    else:
        merge_luna(args)


if __name__ == "__main__":
    main()
