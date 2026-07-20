"""VLM pipeline stages for the ESG corpus: page classification and table extraction.

Design contract (owner-approved 2026-07-20):
- These stages sit BESIDE the deterministic parser, never inside it. The classifier can
  only ADD hold-recommendations/extraction targets; it can never release a held page.
- Model and prompts are PINNED: a dated model snapshot and verbatim prompt constants whose
  sha256 is stamped into every artifact. Changing either changes the cache key, so stale
  artifacts are re-made rather than silently reused.
- Every page is paid for once: artifacts are cached by
  (source_sha256, page, model_snapshot, prompt_hash, stage).
- The digit screen is an ANNOTATOR, never a gate (owner ruling): it records, per number,
  whether the PDF text layer corroborates it. Numbers only visible in graphics are real
  data the text layer cannot see — they ship, labeled as image-only.
- Transport is selectable per run: "sync" (immediate, threaded) or "batch" (OpenAI Batch
  API, 50% price, results within 24h). Identical request bodies, identical artifacts.
- Spend is capped: a run refuses to submit past the configured budget.

The API key comes ONLY from the OPENAI_API_KEY environment variable. Never store it in
the repository.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

MODEL_SNAPSHOT = "gpt-5-mini-2025-08-07"
RENDER_SCALE = 2
PRICE_PER_MTOK = {"input": 0.25, "output": 2.00}  # USD, gpt-5-mini sync; batch is half
DEFAULT_BUDGET_USD = 30.0
BATCH_SHARD_LIMIT_BYTES = 140 * 1024 * 1024
OCR_LINEAGE_DOCS = {"ETSY-2024", "NGVC-2021", "NGVC-2022", "SHOO-2021", "WMT-2024"}

# --------------------------------------------------------------------------- prompts
# Validated 2026-07-20 against the 449-page human-reviewed gold set (blind, one-shot:
# 0/215 representative-prose false holds, 94.1% pooled recall, 3-class kappa 0.813).
# Derived verbatim from HANDOFF_TO_CODEX_2026-07-18.md section 2.2 with the CASY worked
# example redacted (that page is in the evaluation set). Do not edit without re-running
# scripts/vlm_regression_check.py and re-validating against gold.
CLASSIFY_INSTR = """You are labelling one rendered PDF page from a corporate ESG/sustainability report. Apply the following label definitions and operational test EXACTLY.

**Decision class (3 values):**
- `table_dominant` — the page's dominant text is row-structured; column-major reading
  scrambles meaning (detaches row-level label↔value pairs). **Should be held.**
- `prose` — genuine multi-column prose; column-major is correct, OR the page has no
  label-value structure that reading order could corrupt (independent bullet lists,
  self-contained stat callouts where the number and its caption are in the same
  contiguous run, self-contained name:title lines, brand-profile cards, etc.). **Should
  pass.**
- `ambiguous_or_mixed` — independent self-contained boxes with no single canonical linear
  order, OR prose with a **substantial** embedded table that does not clearly dominate
  the page. Default to `prose` only when the embedded table is genuinely small/minor; when
  a real ruled/structured table occupies a meaningful fraction of the page (roughly a
  third or more) alongside real prose, prefer `ambiguous_or_mixed` over forcing a binary
  call.

**The operational test to apply to every page:** would reading this page's text
column-major (read column 1 fully top-to-bottom, then column 2, etc.) detach a row's
label from its value, or a table cell's row-header from its column-value? If yes and it's
the dominant content → `table_dominant`. If yes but it's a minor/small portion of an
otherwise-prose page → lean `prose` unless the table is genuinely substantial
(→ `ambiguous_or_mixed`). If no (clean prose, or independent parallel lists/boxes that
don't have cross-column correspondence to lose) → `prose`. If the page is genuinely a set
of independent boxes with no natural single reading order (not a table, not simple prose
either) → `ambiguous_or_mixed`.

**Important nuance:** many pages have 2+ *columns* that are NOT a table — e.g. two
independent bullet lists under two different headers side-by-side (reading column-major
is *correct* because there's no row-correspondence between them), or self-contained stat
callouts where the number and caption are one contiguous phrase (nothing to detach).
Don't call these `table_dominant` just because they're multi-column. Conversely, watch
for genuine tables disguised as "just a grid of cards" — the test is whether a SHARED ROW
LABEL spans multiple value-columns (worked example redacted).

Answer with JSON only: {"decision_class": "prose" | "table_dominant" | "ambiguous_or_mixed", "reason": "<at most 15 words>"}"""

# Approved config "mini-low-v2" (owner-reviewed 50/50 on the adversarial sample;
# data-focused graphics per owner instruction 2026-07-20).
EXTRACT_INSTR = """Transcribe this corporate-report page into markdown, in natural reading order.

Rules — follow exactly:
- Tables become markdown tables. Put the table's title (as printed on the page) as a bold
  line above it. Keep units with their values or in the column header, exactly as printed.
- Stat cards (a large number with a caption) become lines like: **CAPTION:** value
- Prose becomes ordinary paragraphs in reading order. Headings become markdown headings.
- Transcribe every number EXACTLY as printed: commas, decimals, %, $, parentheses, signs.
  Never compute, round, convert, or infer a number that is not printed on the page.
- Charts, graphs, and data-bearing infographics get a block starting with **[Graphic]** on
  its own line. Extract the DATA the graphic communicates, never its appearance:
  * If values are readable, transcribe them as "label: value" lines or a small markdown
    table — metric names, units, and periods exactly as printed on the graphic.
  * If exact values are not readable, state in ONE line what the graphic shows about the
    data (trend, comparison, ranking).
  * Never describe visual styling, colors, icons, photos, or layout.
- Purely decorative visuals (photos, page art, icons, logos, dividers): skip entirely —
  no block, no mention.
- Only inside **[Graphic]** blocks may you transcribe numbers that appear in graphics
  rather than as text.
- Skip page furniture: page numbers, running headers/footers, navigation ribbons.
- Output markdown only. No preamble, no commentary."""


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class StageConfig:
    stage: str                      # "classifier" | "extraction"
    instr: str
    reasoning_effort: str
    max_completion_tokens: int
    json_mode: bool
    model: str = MODEL_SNAPSHOT
    detail: str = "high"

    @property
    def instr_hash(self) -> str:
        return prompt_hash(self.instr)


CLASSIFIER_CONFIG = StageConfig("classifier", CLASSIFY_INSTR, "low", 2000, True)
EXTRACTION_CONFIG = StageConfig("extraction", EXTRACT_INSTR, "low", 8000, False)


def sanitize(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "_", stem)


def page_key(ticker: str, pdf_stem: str, page: int) -> str:
    return f"{ticker}_{sanitize(pdf_stem)}_p{int(page)}"


def is_ocr_lineage(pdf_stem: str) -> bool:
    return any(pdf_stem.startswith(d.split("-")[0]) and d.split("-")[-1] in pdf_stem
               for d in OCR_LINEAGE_DOCS)


def cache_key(source_sha256: str, page: int, cfg: StageConfig) -> str:
    raw = f"{source_sha256}|{page}|{cfg.model}|{cfg.instr_hash}|{cfg.stage}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def request_body(cfg: StageConfig, png_bytes: bytes) -> dict:
    b64 = base64.b64encode(png_bytes).decode()
    body = {
        "model": cfg.model,
        "reasoning_effort": cfg.reasoning_effort,
        "max_completion_tokens": cfg.max_completion_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": cfg.instr},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}",
                               "detail": cfg.detail}},
            ],
        }],
    }
    if cfg.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def estimate_cost_usd(n_pages: int, cfg: StageConfig, transport: str) -> float:
    per_in = 3200 if cfg.stage == "classifier" else 2900
    per_out = 120 if cfg.stage == "classifier" else 1200
    cost = n_pages * (per_in * PRICE_PER_MTOK["input"]
                      + per_out * PRICE_PER_MTOK["output"]) / 1e6
    return cost * (0.5 if transport == "batch" else 1.0)


# --------------------------------------------------------------- digit screen (annotator)
NUM_RE = re.compile(r"\d[\d.,]*\d|\d")


def numeric_tokens(text: str) -> set[str]:
    return {m.rstrip(".,") for m in NUM_RE.findall(text) if m.rstrip(".,")}


def split_graphic_blocks(md: str) -> tuple[str, str]:
    """Split markdown into (non_graphic_text, graphic_text) on **[Graphic]** blocks."""
    non_g: list[str] = []
    g: list[str] = []
    in_g = False
    for line in md.splitlines():
        if line.strip().startswith("**[Graphic]**"):
            in_g = True
        elif in_g and line.strip() == "":
            in_g = False
        (g if in_g else non_g).append(line)
    return "\n".join(non_g), "\n".join(g)


def text_layer_pool(words: list[str]) -> set[str]:
    pool: set[str] = set()
    for w in words:
        pool |= numeric_tokens(w)
    for a, b in zip(words, words[1:]):  # pdfplumber split-token joins
        pool |= numeric_tokens(a + b)
    return pool


def screen_annotation(md: str, words: list[str]) -> dict:
    """Per-number corroboration record. Informational only — never gates (owner ruling:
    numbers visible only in graphics are data the text layer cannot see)."""
    non_g, g = split_graphic_blocks(md)
    pool = text_layer_pool(words)
    body_nums = numeric_tokens(non_g)
    graphic_nums = numeric_tokens(g)
    return {
        "n_numbers_total": len(body_nums | graphic_nums),
        "n_text_corroborated": len((body_nums | graphic_nums) & pool),
        "body_uncorroborated": sorted(body_nums - pool),
        "graphic_only_numbers": sorted(graphic_nums - pool),
        "screen_version": "v3_annotator",
    }


# ----------------------------------------------------------------------------- rendering
def render_page(source_pdf: Path, page: int, out_path: Path) -> None:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(source_pdf))
    try:
        pdf[page - 1].render(scale=RENDER_SCALE).to_pil().save(out_path)
    finally:
        pdf.close()


# ----------------------------------------------------------------------------- transports
def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (never store it in the repo)")
    return key


def run_sync(items: list[tuple[str, dict]], on_result, workers: int = 10,
             log=print) -> None:
    """items: (key, body). on_result(key, content, usage) called under a lock."""
    import urllib.request
    lock = threading.Lock()

    def one(key: str, body: dict) -> None:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_api_key()}"})
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    resp = json.load(r)
                content = resp["choices"][0]["message"]["content"]
                with lock:
                    on_result(key, content, resp.get("usage", {}))
                return
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced
                last = exc
                time.sleep(5 * (attempt + 1))
        with lock:
            on_result(key, None, {"error": repr(last)})

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, k, b) for k, b in items]
        for n, f in enumerate(as_completed(futures), 1):
            f.result()
            if n % 100 == 0 or n == len(futures):
                log(f"sync: {n}/{len(futures)} ({time.time()-t0:.0f}s)")


# def batch_submit(items: list[tuple[str, dict]], state_path: Path, name: str,
#                  log=print) -> None:
#     """Shard items into <=140MB JSONL files and submit as OpenAI batches. State is
#     persisted so collection can happen in any later invocation."""
#     import httpx
#     st = json.loads(state_path.read_text()) if state_path.exists() else {"batches": {}}
#     shards: list[tuple[str, list[str]]] = []
#     buf: list[str] = []
#     size = 0
#     idx = 0
#     for key, body in items:
#         line = json.dumps({"custom_id": key, "method": "POST",
#                            "url": "/v1/chat/completions", "body": body})
#         if size + len(line) > BATCH_SHARD_LIMIT_BYTES and buf:
#             shards.append((f"{name}_s{idx}", buf))
#             idx += 1
#             buf, size = [], 0
#         buf.append(line)
#         size += len(line) + 1
#     if buf:
#         shards.append((f"{name}_s{idx}", buf))
#     headers = {"Authorization": f"Bearer {_api_key()}"}
#     with httpx.Client(timeout=600) as cx:
#         for shard_name, lines in shards:
#             if shard_name in st["batches"]:
#                 continue
#             data = ("\n".join(lines)).encode()
#             up = cx.post("https://api.openai.com/v1/files", headers=headers,
#                          files={"file": (f"{shard_name}.jsonl", io.BytesIO(data),
#                                           "application/jsonl")},
#                          data={"purpose": "batch"})
#             up.raise_for_status()
#             b = cx.post("https://api.openai.com/v1/batches",
#                         headers={**headers, "Content-Type": "application/json"},
#                         json={"input_file_id": up.json()["id"],
#                               "endpoint": "/v1/chat/completions",
#                               "completion_window": "24h"})
#             b.raise_for_status()
#             st["batches"][shard_name] = {"id": b.json()["id"], "n": len(lines),
#                                          "collected": False}
#             state_path.write_text(json.dumps(st, indent=2))
#             log(f"batch: submitted {shard_name} ({len(lines)} pages, id {b.json()['id']})")


def batch_submit(
    items: list[tuple[str, dict]],
    state_path: Path,
    name: str,
    log=print,
) -> None:
    """Shard items into <=140MB JSONL files and submit as OpenAI batches.

    State is persisted so collection can happen in any later invocation.
    File uploads are retried after temporary connection failures.
    """
    import random
    import time

    import httpx

    st = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"batches": {}}
    )

    shards: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    size = 0
    idx = 0

    for key, body in items:
        line = json.dumps(
            {
                "custom_id": key,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body,
            }
        )

        if size + len(line) > BATCH_SHARD_LIMIT_BYTES and buf:
            shards.append((f"{name}_s{idx}", buf))
            idx += 1
            buf, size = [], 0

        buf.append(line)
        size += len(line) + 1

    if buf:
        shards.append((f"{name}_s{idx}", buf))

    headers = {"Authorization": f"Bearer {_api_key()}"}

    timeout = httpx.Timeout(
        connect=30.0,
        read=600.0,
        write=600.0,
        pool=30.0,
    )

    with httpx.Client(timeout=timeout) as cx:
        for shard_name, lines in shards:
            # Previously submitted shards remain untouched.
            if shard_name in st["batches"]:
                log(f"batch: skipping existing {shard_name}")
                continue

            data = ("\n".join(lines)).encode()
            size_mb = len(data) / (1024 * 1024)

            # Retry only the file upload. A fresh BytesIO is required
            # for every attempt.
            max_attempts = 6

            for attempt in range(1, max_attempts + 1):
                try:
                    log(
                        f"batch: uploading {shard_name} "
                        f"({size_mb:.1f} MB), "
                        f"attempt {attempt}/{max_attempts}"
                    )

                    up = cx.post(
                        "https://api.openai.com/v1/files",
                        headers=headers,
                        files={
                            "file": (
                                f"{shard_name}.jsonl",
                                io.BytesIO(data),
                                "application/jsonl",
                            )
                        },
                        data={"purpose": "batch"},
                    )

                    up.raise_for_status()
                    break

                except httpx.TransportError as exc:
                    if attempt == max_attempts:
                        raise

                    delay = min(
                        60.0,
                        (2 ** (attempt - 1)) + random.random(),
                    )

                    log(
                        f"batch: connection error uploading "
                        f"{shard_name}: {exc}; "
                        f"retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code

                    # Retry rate limits and temporary server errors.
                    if status != 429 and status < 500:
                        raise

                    if attempt == max_attempts:
                        raise

                    delay = min(
                        60.0,
                        (2 ** (attempt - 1)) + random.random(),
                    )

                    log(
                        f"batch: upload returned HTTP {status} for "
                        f"{shard_name}; retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)

            # Create the batch only after receiving a confirmed file ID.
            uploaded_file_id = up.json()["id"]

            b = cx.post(
                "https://api.openai.com/v1/batches",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                },
                json={
                    "input_file_id": uploaded_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": "24h",
                    "metadata": {
                        "stage": name,
                        "shard": shard_name,
                    },
                },
            )

            b.raise_for_status()
            batch_id = b.json()["id"]

            st["batches"][shard_name] = {
                "id": batch_id,
                "n": len(lines),
                "collected": False,
            }

            # Save immediately after every successfully created batch.
            state_path.write_text(json.dumps(st, indent=2))

            log(
                f"batch: submitted {shard_name} "
                f"({len(lines)} pages, id {batch_id})"
            )

def batch_collect(state_path: Path, on_result, log=print) -> bool:
    """Collect finished batches; returns True when all are collected."""
    import httpx
    if not state_path.exists():
        return False
    st = json.loads(state_path.read_text())
    headers = {"Authorization": f"Bearer {_api_key()}"}
    all_done = True
    with httpx.Client(timeout=600) as cx:
        for shard_name, info in st["batches"].items():
            if info["collected"]:
                continue
            b = cx.get(f"https://api.openai.com/v1/batches/{info['id']}",
                       headers=headers).json()
            status = b.get("status")
            if status in ("failed", "expired", "cancelled"):
                log(f"batch: {shard_name} {status} — inspect error file id "
                    f"{b.get('error_file_id')}")
                info["collected"] = True
                state_path.write_text(json.dumps(st, indent=2))
                continue
            if status != "completed":
                log(f"batch: {shard_name} still {status}")
                all_done = False
                continue
            ofid = b.get("output_file_id")
            if ofid:
                content = cx.get(f"https://api.openai.com/v1/files/{ofid}/content",
                                 headers=headers).text
                for line in content.splitlines():
                    rec = json.loads(line)
                    body = rec.get("response", {}).get("body", {})
                    try:
                        msg = body["choices"][0]["message"]["content"]
                        usage = body.get("usage", {})
                    except Exception:  # noqa: BLE001
                        msg, usage = None, {"error": "malformed batch response"}
                    on_result(rec["custom_id"], msg, usage)
            info["collected"] = True
            state_path.write_text(json.dumps(st, indent=2))
            log(f"batch: collected {shard_name}")
    return all_done and bool(st["batches"])
