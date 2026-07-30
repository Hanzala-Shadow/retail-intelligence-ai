"""Blind, reproducible vision review of the two PDF reading-order readers.

This is a read-only evaluation tool.  It writes only under ``reports/`` and
does not change the PDF corpus or either reader.  Run its three phases in
order.  The paid ``judge`` phase deliberately reads only ``judging_queue.json``
and rendered images; the reader-to-A/B key is kept in a separate file.

Examples (from the repository root)::

    python scripts/run_llm_reader_review.py prepare
    $env:ANTHROPIC_API_KEY = '...'
    python scripts/run_llm_reader_review.py judge
    python scripts/run_llm_reader_review.py unblind

The default model and rates were checked against Anthropic's official model and
pricing pages on 2026-07-30.  They are command-line options so a rerun can pin a
later documented model or price without editing this script.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pdfplumber
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from esg_navigation import build_navigation_profile, clean_navigation  # noqa: E402
from esg_reading_order import reconstruct_column_order  # noqa: E402
from esg_reading_regions import reconstruct_by_regions  # noqa: E402

RAW_ROOT = REPO_ROOT / "data" / "01_raw" / "sustainability"
HUMAN_SCORES = REPO_ROOT / "reports" / "old_vs_new_review_2026-07-30" / "recovered_scores_unblinded.json"
HUMAN_SOURCE_COMPARISON = REPO_ROOT / "reports" / "parser_comparison_2026-07-30" / "parser_comparison.json"
SEED = 20260730
DEFAULT_DATE = "2026-07-30"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_INPUT_PRICE_PER_MTOK = 2.0
DEFAULT_OUTPUT_PRICE_PER_MTOK = 10.0
MODEL_DOC_URL = "https://docs.anthropic.com/en/docs/about-claude/models/overview"
PRICING_DOC_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
RENDER_DPI = 110
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MIN_IMAGE_DIMENSION = 700

RUBRIC = """- Are sentences whole, or does text from elsewhere cut into them?
- Does each label stay next to its value (a heading with its paragraph,
  a table row's label with its number, a chart title with its data)?
- Are separate visual blocks kept separate, or interleaved?
- Reading blocks in a slightly unusual ORDER is a minor fault.
  SHREDDING a sentence by interleaving is a major fault.
- If both texts are wrong in different ways, answer \"No meaningful
  difference\". That is a real answer, not a cop-out."""

JUDGE_PROMPT = f"""You are judging the reading order of one rendered PDF page.
Use the page image as the source of truth. Parser A and Parser B are blind labels;
do not infer how either parser works.

{RUBRIC}

Classify the PAGE TYPE from the image, not from either parser text:
- prose_dominant: connected prose or multi-panel prose is the main content.
- table_chart_dominant: a table, chart, data grid, or row/column mapping is the main content.

Answer with JSON only, exactly this schema:
{{"verdict":"A better"|"B better"|"No meaningful difference", "justification":"one sentence citing a concrete feature visible on the page", "confidence":"high"|"medium"|"low", "page_type":"prose_dominant"|"table_chart_dominant"}}

Parser A:
{{parser_a}}

Parser B:
{{parser_b}}"""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")


def extract_words(page) -> list[dict]:
    try:
        return page.extract_words(
            use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]
        ) or []
    except TypeError:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False) or []


def pdf_inventory() -> list[tuple[str, Path]]:
    return sorted((path.parent.name, path) for path in RAW_ROOT.glob("*/*.pdf"))


def page_readers(page, profile) -> dict[str, Any] | None:
    words = extract_words(page)
    width, height = float(page.width), float(page.height)
    body = clean_navigation(words, page.chars, width, height, profile).body_words
    if not body:
        return None
    production = reconstruct_column_order(body, width, height)
    candidate = reconstruct_by_regions(body, width, height)
    if production.status == "reconstructed" and production.text != candidate.text:
        group = "reconstructed_text_differs"
    elif production.status == "ambiguous":
        group = "production_ambiguous"
    else:
        return None
    return {
        "page": page.page_number,
        "group": group,
        "production_status": production.status,
        "production_reason": production.reason,
        "production_text": production.text,
        "candidate_status": candidate.status,
        "candidate_reason": candidate.reason,
        "candidate_text": candidate.text,
        "body_word_count": len(body),
    }


def read_document_candidates(ticker: str, path: Path) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        profile = build_navigation_profile(
            [(page.chars, float(page.width), float(page.height)) for page in pdf.pages]
        )
        rows = []
        for page in pdf.pages:
            row = page_readers(page, profile)
            if row:
                row.update({"ticker": ticker, "pdf_file": path.name, "pdf_path": str(path)})
                rows.append(row)
        return rows


def load_human_pages() -> list[dict]:
    scores = read_json(HUMAN_SCORES)["unblinded"]
    expected = {
        ("AEO", 3), ("AEO", 4), ("ACI", 4), ("ACI", 6), ("ACI", 7),
        ("ACI", 9), ("ACI", 10), ("ACI", 11), ("ACI", 13), ("ACI", 15),
        ("ACI", 16), ("ACI", 18), ("ACI", 19),
    }
    parsed = []
    for row in scores:
        match = re.fullmatch(r"([A-Z]+) p(\d+)", row["page"])
        if not match:
            raise ValueError(f"Unexpected human page identifier: {row['page']!r}")
        human_winner = {"regions": "candidate", "column_order": "production", "tie": "tie"}.get(row["winner"])
        if human_winner is None:
            raise ValueError(f"Unexpected human winner: {row['winner']!r}")
        parsed.append({
            "ticker": match.group(1), "page": int(match.group(2)),
            "human_winner": human_winner, "human_id": row["id"],
        })
    if {(row["ticker"], row["page"]) for row in parsed} != expected:
        raise ValueError("Human validation page list did not match the documented 13 pages.")
    return parsed


def human_source_files() -> dict[str, str]:
    """Read the exact source PDFs from the existing 42-page comparison record."""
    documents = read_json(HUMAN_SOURCE_COMPARISON)
    files = {row["ticker"]: row["pdf_file"] for row in documents}
    if not {"AEO", "ACI"}.issubset(files):
        raise RuntimeError("The parser comparison record does not identify both human-score PDFs.")
    return files


def select_main_sample(inventory: list[tuple[str, Path]], target: int) -> list[dict]:
    """Choose eligible pages from broad documents, with no more than 3 per document."""
    rng = random.Random(SEED)
    shuffled = inventory[:]
    rng.shuffle(shuffled)
    candidates_by_doc: list[list[dict]] = []
    for index, (ticker, path) in enumerate(shuffled, 1):
        rows = read_document_candidates(ticker, path)
        if rows:
            rng.shuffle(rows)
            candidates_by_doc.append(rows)
        # A broad first pass avoids scanning the whole corpus once 18 usable
        # documents already provide ample choices for a 36-page sample.
        if len(candidates_by_doc) >= max(18, target // 2):
            break
        if index == len(shuffled) and len(candidates_by_doc) < 15:
            raise RuntimeError("Fewer than 15 documents contained eligible comparison pages.")

    if len(candidates_by_doc) < 15:
        raise RuntimeError("Could not find eligible pages in at least 15 documents.")

    selected: list[dict] = []
    per_doc: Counter[tuple[str, str]] = Counter()
    group_counts: Counter[str] = Counter()
    # First take one page from each of 18 documents.  Prefer the smaller group
    # whenever a document offers it, while retaining the fixed random order.
    for rows in candidates_by_doc:
        options = sorted(rows, key=lambda row: (group_counts[row["group"]], row["page"]))
        chosen = options[0]
        selected.append(chosen)
        per_doc[(chosen["ticker"], chosen["pdf_file"])] += 1
        group_counts[chosen["group"]] += 1
        if len(selected) >= min(target, len(candidates_by_doc)):
            break

    # Then fill to target while preserving the 3-page cap and balancing groups
    # where the available corpus allows it.
    pool = [row for rows in candidates_by_doc for row in rows if row not in selected]
    rng.shuffle(pool)
    while len(selected) < target:
        eligible = [
            row for row in pool
            if per_doc[(row["ticker"], row["pdf_file"])] < 3
        ]
        if not eligible:
            break
        minimum = min(group_counts.values(), default=0)
        preferred = [row for row in eligible if group_counts[row["group"]] == minimum]
        chosen = (preferred or eligible)[0]
        selected.append(chosen)
        pool.remove(chosen)
        per_doc[(chosen["ticker"], chosen["pdf_file"])] += 1
        group_counts[chosen["group"]] += 1

    if not 30 <= len(selected) <= 40:
        raise RuntimeError(f"Expected a 30-40 page main sample; selected {len(selected)}.")
    if len({(row["ticker"], row["pdf_file"]) for row in selected}) < 15:
        raise RuntimeError("Main sample did not reach 15 different documents.")
    if max(per_doc.values(), default=0) > 3:
        raise RuntimeError("Main sample violates the 3-pages-per-document cap.")
    return selected


def reader_rows_for_humans(inventory: list[tuple[str, Path]]) -> list[dict]:
    by_ticker: dict[str, list[Path]] = defaultdict(list)
    for ticker, path in inventory:
        by_ticker[ticker].append(path)
    source_files = human_source_files()
    rows = []
    for human in load_human_pages():
        matches = by_ticker.get(human["ticker"], [])
        source_name = source_files[human["ticker"]]
        path = next((candidate for candidate in matches if candidate.name == source_name), None)
        if path is None:
            raise RuntimeError(f"Human validation source PDF is missing: {human['ticker']}/{source_name}")
        ticker = human["ticker"]
        with pdfplumber.open(path) as pdf:
            profile = build_navigation_profile(
                [(page.chars, float(page.width), float(page.height)) for page in pdf.pages]
            )
            page = pdf.pages[human["page"] - 1]
            words = extract_words(page)
            width, height = float(page.width), float(page.height)
            body = clean_navigation(words, page.chars, width, height, profile).body_words
            production = reconstruct_column_order(body, width, height)
            candidate = reconstruct_by_regions(body, width, height)
        group = "production_ambiguous" if production.status == "ambiguous" else "reconstructed_text_differs"
        rows.append({
            "ticker": ticker, "pdf_file": path.name, "pdf_path": str(path), "page": human["page"],
            "group": group, "set": "human_validation", "human_winner": human["human_winner"],
            "human_id": human["human_id"], "production_status": production.status,
            "production_reason": production.reason, "production_text": production.text,
            "candidate_status": candidate.status, "candidate_reason": candidate.reason,
            "candidate_text": candidate.text, "body_word_count": len(body),
        })
    return rows


def render_row(row: dict, out_dir: Path) -> tuple[dict | None, dict | None]:
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{row['set']}_{row['ticker']}_{slug(Path(row['pdf_file']).stem)}_p{row['page']}.png"
    path = image_dir / file_name
    try:
        with pdfplumber.open(Path(row["pdf_path"])) as pdf:
            pdf.pages[row["page"] - 1].to_image(resolution=RENDER_DPI).save(path, format="PNG")
        with Image.open(path) as image:
            width, height = image.size
        size = path.stat().st_size
        if min(width, height) < MIN_IMAGE_DIMENSION:
            raise ValueError(f"render too small ({width}x{height}px)")
        if size > MAX_IMAGE_BYTES:
            with Image.open(path) as image:
                image.save(path, format="PNG", optimize=True)
            size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"render is {size} bytes after optimization (limit {MAX_IMAGE_BYTES})")
        copied = dict(row)
        copied["image_path"] = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        copied["render"] = {"dpi": RENDER_DPI, "width_px": width, "height_px": height, "bytes": size}
        return copied, None
    except Exception as error:  # Render drops are evidence, not silently omitted.
        path.unlink(missing_ok=True)
        return None, {"ticker": row["ticker"], "pdf_file": row["pdf_file"], "page": row["page"],
                      "set": row["set"], "reason": f"{type(error).__name__}: {error}"}


def make_blinded_files(rows: list[dict], out_dir: Path) -> None:
    rng = random.Random(SEED + 1)
    queue, key = [], []
    for index, row in enumerate(rows, 1):
        mapping = rng.choice(["production", "candidate"])
        parser_a = row[f"{mapping}_text"]
        other = "candidate" if mapping == "production" else "production"
        item_id = f"{row['set']}_{index:03d}"
        queue.append({
            "item_id": item_id, "set": row["set"], "image_path": row["image_path"],
            "parser_a": parser_a, "parser_b": row[f"{other}_text"],
        })
        key.append({
            "item_id": item_id, "ticker": row["ticker"], "pdf_file": row["pdf_file"], "page": row["page"],
            "set": row["set"], "group": row["group"], "parser_a": mapping, "parser_b": other,
            "production_status": row["production_status"], "production_text": row["production_text"],
            "candidate_status": row["candidate_status"], "candidate_text": row["candidate_text"],
            "image_path": row["image_path"], "render": row["render"],
            "human_winner": row.get("human_winner"), "human_id": row.get("human_id"),
        })
    write_json(out_dir / "judging_queue.json", queue)
    write_json(out_dir / "blind_key.json", key)


def prepare(args: argparse.Namespace) -> None:
    inventory = pdf_inventory()
    if not inventory:
        raise RuntimeError(f"No PDFs found below {RAW_ROOT}")
    main = select_main_sample(inventory, args.target)
    for row in main:
        row["set"] = "main_sample"
    validation = reader_rows_for_humans(inventory)
    rendered, drops = [], []
    for row in main + validation:
        ready, drop = render_row(row, args.out_dir)
        if ready:
            rendered.append(ready)
        if drop:
            drops.append(drop)
    main_count = sum(row["set"] == "main_sample" for row in rendered)
    if main_count != args.target:
        raise RuntimeError(f"{args.target - main_count} main pages could not be rendered; rerun after reviewing render_drops.json.")
    if sum(row["set"] == "human_validation" for row in rendered) != 13:
        raise RuntimeError("One or more required human validation pages could not be rendered.")
    main_docs = {(row["ticker"], row["pdf_file"]) for row in rendered if row["set"] == "main_sample"}
    if len(main_docs) < 15 or max(Counter((row["ticker"], row["pdf_file"]) for row in rendered if row["set"] == "main_sample").values()) > 3:
        raise RuntimeError("Rendered main sample no longer meets document diversity rules.")
    make_blinded_files(rendered, args.out_dir)
    page_list = [{key: row[key] for key in ("set", "ticker", "pdf_file", "page", "group", "image_path", "render")}
                 for row in rendered]
    write_json(args.out_dir / "sampled_pages.json", page_list)
    write_json(args.out_dir / "render_drops.json", drops)
    (args.out_dir / "sampled_pages.txt").write_text(
        "\n".join(f"{row['set']}\t{row['ticker']}\t{row['pdf_file']}\t{row['page']}\t{row['group']}" for row in page_list) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared {main_count} main pages from {len(main_docs)} documents and 13 human-validation pages.")


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. The paid judge phase was not started.")
    return key


def call_judge(item: dict, *, swapped: bool, args: argparse.Namespace) -> dict:
    image_path = REPO_ROOT / item["image_path"]
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    parser_a, parser_b = item["parser_a"], item["parser_b"]
    if swapped:
        parser_a, parser_b = parser_b, parser_a
    prompt = JUDGE_PROMPT.replace("{parser_a}", parser_a).replace("{parser_b}", parser_b)
    body = {
        "model": args.model, "max_tokens": 350, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
            {"type": "text", "text": prompt},
        ]}],
    }
    request = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key(), "anthropic-version": API_VERSION},
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.load(response)
            text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
            match = re.search(r"\{.*\}", text, re.DOTALL)
            parsed = json.loads(match.group(0) if match else text)
            required = {"verdict", "justification", "confidence", "page_type"}
            if set(parsed) != required:
                raise ValueError(f"Unexpected judge fields: {sorted(parsed)}")
            if parsed["verdict"] not in {"A better", "B better", "No meaningful difference"}:
                raise ValueError(f"Unexpected verdict: {parsed['verdict']!r}")
            if parsed["confidence"] not in {"high", "medium", "low"}:
                raise ValueError(f"Unexpected confidence: {parsed['confidence']!r}")
            if parsed["page_type"] not in {"prose_dominant", "table_chart_dominant"}:
                raise ValueError(f"Unexpected page type: {parsed['page_type']!r}")
            return {"ok": True, "response": parsed, "usage": payload.get("usage", {}),
                    "model": payload.get("model", args.model), "stop_reason": payload.get("stop_reason")}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < 2:
                time.sleep(2 ** attempt)
    return {"ok": False, "error": last_error}


def judge(args: argparse.Namespace) -> None:
    # Fail before dispatching worker threads.  This avoids a noisy partial run
    # when the key has not been placed in the shell environment.
    api_key()
    queue_path = args.out_dir / "judging_queue.json"
    if not queue_path.exists():
        raise RuntimeError("Run prepare first; judging_queue.json is missing.")
    queue = read_json(queue_path)
    result_path = args.out_dir / "judge_runs_blinded.json"
    existing = read_json(result_path) if result_path.exists() else {}
    jobs = [(item, swapped) for item in queue for swapped in (False, True)
            if f"{item['item_id']}:{'swapped' if swapped else 'original'}" not in existing]
    lock = threading.Lock()

    def save_result(item: dict, swapped: bool) -> None:
        name = f"{item['item_id']}:{'swapped' if swapped else 'original'}"
        result = call_judge(item, swapped=swapped, args=args)
        with lock:
            existing[name] = {"item_id": item["item_id"], "run": "swapped" if swapped else "original", **result}
            write_json(result_path, existing)
            print(f"{len(existing)}/{len(queue) * 2}: {name} {'ok' if result['ok'] else result['error']}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(save_result, item, swapped) for item, swapped in jobs]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def unblind_verdict(verdict: str, mapping: dict[str, str]) -> str:
    if verdict == "No meaningful difference":
        return "tie"
    reader = mapping["parser_a"] if verdict == "A better" else mapping["parser_b"]
    return reader


def cost_from_usage(usage: dict, args: argparse.Namespace) -> float:
    input_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    return input_tokens * args.input_price_per_mtok / 1_000_000 + output_tokens * args.output_price_per_mtok / 1_000_000


def unblind(args: argparse.Namespace) -> None:
    key_path, run_path = args.out_dir / "blind_key.json", args.out_dir / "judge_runs_blinded.json"
    if not key_path.exists() or not run_path.exists():
        raise RuntimeError("Run prepare and judge before unblinding.")
    keys = {row["item_id"]: row for row in read_json(key_path)}
    runs = read_json(run_path)
    results = []
    for item_id, key in keys.items():
        original = runs.get(f"{item_id}:original")
        swapped = runs.get(f"{item_id}:swapped")
        final = {"ticker": key["ticker"], "pdf_file": key["pdf_file"], "page": key["page"], "group": key["group"],
                 "set": key["set"], "production_status": key["production_status"], "candidate_status": key["candidate_status"],
                 "production_text": key["production_text"], "candidate_text": key["candidate_text"], "image_path": key["image_path"],
                 "render": key["render"], "human_winner": key.get("human_winner"), "human_id": key.get("human_id"),
                 "judge_runs": {"original": original, "swapped": swapped}}
        if not original or not swapped or not original.get("ok") or not swapped.get("ok"):
            final.update({"final_verdict": "judge_unreliable", "confidence": None, "justification": None, "page_type": None})
        else:
            original_winner = unblind_verdict(original["response"]["verdict"], key)
            swapped_mapping = {"parser_a": key["parser_b"], "parser_b": key["parser_a"]}
            swapped_winner = unblind_verdict(swapped["response"]["verdict"], swapped_mapping)
            position_flip = (original["response"]["verdict"] in {"A better", "B better"}
                             and original["response"]["verdict"] == swapped["response"]["verdict"])
            reliable = original_winner == swapped_winner
            final.update({
                "original_unblinded_verdict": original_winner, "swapped_unblinded_verdict": swapped_winner,
                "position_bias_flip": position_flip,
                "final_verdict": original_winner if reliable else "judge_unreliable",
                "confidence": original["response"]["confidence"] if reliable else None,
                "justification": original["response"]["justification"] if reliable else None,
                "page_type": original["response"]["page_type"] if reliable and original["response"]["page_type"] == swapped["response"]["page_type"] else "judge_unreliable",
            })
        results.append(final)
    write_json(args.out_dir / "results.json", results)
    write_summary(results, args)


def tally(rows: list[dict]) -> dict[str, int]:
    return {winner: sum(row["final_verdict"] == winner for row in rows) for winner in ("candidate", "production", "tie", "judge_unreliable")}


def write_summary(results: list[dict], args: argparse.Namespace) -> None:
    main = [row for row in results if row["set"] == "main_sample"]
    validation = [row for row in results if row["set"] == "human_validation"]
    complete = [row for row in results if row["final_verdict"] != "judge_unreliable"]
    directional = [row for row in complete if row["original_unblinded_verdict"] != "tie" and row["swapped_unblinded_verdict"] != "tie"]
    flips = sum(bool(row.get("position_bias_flip")) for row in directional)
    human_matches = [row for row in validation if row["final_verdict"] == row["human_winner"]]
    valid_reliable = [row for row in validation if row["final_verdict"] != "judge_unreliable"]
    usage = Counter()
    cost = 0.0
    models = set()
    for row in results:
        for run in row["judge_runs"].values():
            if run and run.get("ok"):
                usage.update({key: int(value) for key, value in run.get("usage", {}).items() if isinstance(value, int)})
                cost += cost_from_usage(run.get("usage", {}), args)
                models.add(run.get("model", args.model))
    headline = "UNVALIDATED"
    if len(valid_reliable) == 13 and len(human_matches) / 13 >= 0.70 and (not directional or flips / len(directional) <= 0.10):
        headline = "VALIDATED"
    lines = [
        "# LLM reading-order review", "", f"Date: {DEFAULT_DATE}", f"Seed: {SEED}", "",
        "## Validation first", "",
        f"**Human agreement: {len(human_matches)} / 13 ({len(human_matches) / 13:.1%}) overall; {sum(row['final_verdict'] == row['human_winner'] for row in valid_reliable)} / {len(valid_reliable)} among judge-reliable validation pages.**",
        f"**Position-bias flip rate: {flips} / {len(directional)} ({(flips / len(directional)) if directional else 0:.1%}) among two-directional verdict pairs.**",
        f"**Status: {headline}.** " + ("The broader sample may be used as evidence." if headline == "VALIDATED" else "Do not quote the broader headline as validated evidence."),
        "", "The 13 historical human pages are a separate validation set. This is necessary because ACI supplies 11 of them, which would violate the new main sample's maximum of three pages per document.",
        "", "## Headline: main sample only, kept separate by production status", "",
        "The two eligibility groups are intentionally not pooled: a candidate that produces text cannot be credited simply for filling a production refusal. Only pages whose two label positions agree after unblinding count in the three verdict categories.",
        "", "## Breakdown by production-status group and page type", "",
        "| Production-status group | Page type from render | Candidate better | Production better | Tie | Judge unreliable |", "|---|---|---:|---:|---:|---:|",
    ]
    by_group = defaultdict(list)
    for row in main:
        by_group[row["group"]].append(row)
    for group, bucket in sorted(by_group.items()):
        for page_type in ("prose_dominant", "table_chart_dominant", "judge_unreliable"):
            page_bucket = [row for row in bucket if row.get("page_type") == page_type]
            counts = tally(page_bucket)
            lines.append(f"| {group} | {page_type} | {counts['candidate']} | {counts['production']} | {counts['tie']} | {counts['judge_unreliable']} |")
    lines += ["", "## API usage and cost", "", f"- Model(s): {', '.join(sorted(models)) or 'No successful calls'}", f"- Input tokens: {usage.get('input_tokens', 0):,}", f"- Output tokens: {usage.get('output_tokens', 0):,}", f"- Estimated API cost: ${cost:.4f} (input ${args.input_price_per_mtok}/MTok; output ${args.output_price_per_mtok}/MTok)", f"- Pricing source checked: {PRICING_DOC_URL}", f"- Model source checked: {MODEL_DOC_URL}", "", "## Files", "", "- `results.json`: unblinded page-level results, parser texts, both judge runs, and image paths.", "- `sampled_pages.json` / `sampled_pages.txt`: reproducible page list.", "- `blind_key.json`: parser A/B mapping; it is not read by the judge phase.", "- `judging_queue.json`: the blinded inputs read by the judge phase.", "- `render_drops.json`: any pages excluded because their render failed or was too small/large.", ""]
    (args.out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "judge", "unblind"))
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "reports" / f"llm_reader_review_{DEFAULT_DATE}")
    parser.add_argument("--target", type=int, default=36, help="Main sample target: 30-40 pages (default: 36).")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--input-price-per-mtok", type=float, default=DEFAULT_INPUT_PRICE_PER_MTOK)
    parser.add_argument("--output-price-per-mtok", type=float, default=DEFAULT_OUTPUT_PRICE_PER_MTOK)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 30 <= args.target <= 40:
        raise SystemExit("--target must be between 30 and 40.")
    if args.command == "prepare":
        prepare(args)
    elif args.command == "judge":
        judge(args)
    else:
        unblind(args)


if __name__ == "__main__":
    main()
