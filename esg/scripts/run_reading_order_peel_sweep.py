"""Measurement-only sweep: blast radius of the panel-peel change in
``src/esg_reading_regions.py`` (peel-before-runs, gated by ``_bridges_row_gap``,
capped by ``MAX_PANEL_PEEL_DEPTH``).

This script does not modify ``esg_reading_regions.py`` and does not change its
behaviour. It runs the module twice per sampled page -- once as shipped, once
with ``MAX_PANEL_PEEL_DEPTH`` monkeypatched to 0 in-process -- and diffs the
two candidates. All instrumentation (peel-fire tracking, bridging-gate call
tracking) is done by wrapping module-level functions from here; the source
file is untouched and this script performs no corpus writes.

See reports/reading_order_bundle2_2026-07-30/HANDOFF_regions_2026-07-30.md
for what the peel change is and why this sweep exists.
"""

from __future__ import annotations

import json
import random
import re
import sys
from html import escape
from pathlib import Path

import pdfplumber

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

REPO_ROOT = config.REPO_ROOT

from esg_navigation import build_navigation_profile, clean_navigation  # noqa: E402
import esg_reading_regions as regions_mod  # noqa: E402
from esg_reading_regions import reconstruct_by_regions  # noqa: E402

RAW_ROOT = config.RAW_SUSTAINABILITY_DIR
OUT_DIR = config.REPORTS_DIR / "reading_order_peel_sweep_2026-07-30"
RENDER_DIR = OUT_DIR / "renders" / "review_set"

SEED = 20260730
NUM_DOCUMENTS = 32
PAGES_PER_DOCUMENT = 14
CANDIDATE_OVERSAMPLE = 3  # try up to this many extra page numbers per doc to survive empty-body skips
REVIEW_SET_SIZE = 15


def extract_words(page) -> list[dict]:
    try:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]) or []
    except TypeError:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False) or []


def _year_of(pdf_file: str) -> str:
    match = re.search(r"(20[1-2][0-9])", pdf_file)
    return match.group(1) if match else "unknown"


def discover_documents() -> list[tuple[str, str, Path]]:
    """All (ticker, pdf_file, path) triples under the raw sustainability corpus."""

    docs: list[tuple[str, str, Path]] = []
    for ticker_dir in sorted(p for p in RAW_ROOT.iterdir() if p.is_dir()):
        for pdf_path in sorted(ticker_dir.glob("*.pdf")):
            docs.append((ticker_dir.name, pdf_path.name, pdf_path))
    return docs


def sample_documents(rng: random.Random, all_docs: list[tuple[str, str, Path]]) -> list[tuple[str, str, Path]]:
    """Pick NUM_DOCUMENTS documents spread across tickers and years.

    One document per ticker (drawn from a shuffled ticker order) until the
    target count is reached, so no single ticker dominates the sample; years
    fall out of whichever file is drawn for that ticker.
    """

    by_ticker: dict[str, list[tuple[str, str, Path]]] = {}
    for ticker, pdf_file, path in all_docs:
        by_ticker.setdefault(ticker, []).append((ticker, pdf_file, path))

    tickers = list(by_ticker.keys())
    rng.shuffle(tickers)

    selected: list[tuple[str, str, Path]] = []
    for ticker in tickers:
        if len(selected) >= NUM_DOCUMENTS:
            break
        choices = by_ticker[ticker]
        selected.append(choices[rng.randrange(len(choices))])
    return selected


# ---------------------------------------------------------------------------
# Instrumentation: wraps module-level functions from OUTSIDE the source file.
# Recursive calls inside esg_reading_regions resolve `_regions_from_lines` /
# `_panel_split` as globals of that module at call time, so replacing the
# module attributes here also redirects the module's own internal recursive
# calls through these wrappers -- no source edits needed.
# ---------------------------------------------------------------------------

class Instrumentation:
    def __init__(self) -> None:
        self.current_page_key: str | None = None
        # Peel attempts are the actual in-reader calls, one at each reached
        # recursion node. Gate candidates are a complete scan of every strip
        # that clears all the _panel_split guards other than its `require`
        # predicate. This makes the gate count independent of widest-first
        # early returns in the shipped implementation.
        self.peel_attempts: list[dict] = []
        self.gate_candidates: list[dict] = []
        self._call_stack: list[tuple[int, float]] = []
        self._orig_regions_from_lines = regions_mod._regions_from_lines
        self._orig_panel_split = regions_mod._panel_split

    def start_page(self, key: str) -> None:
        self.current_page_key = key
        self._call_stack = []

    def install(self) -> None:
        regions_mod._regions_from_lines = self._wrapped_regions_from_lines
        regions_mod._panel_split = self._wrapped_panel_split

    def uninstall(self) -> None:
        regions_mod._regions_from_lines = self._orig_regions_from_lines
        regions_mod._panel_split = self._orig_panel_split

    def _wrapped_regions_from_lines(self, lines, page_width, page_height, depth=0):
        self._call_stack.append((depth, page_height))
        try:
            return self._orig_regions_from_lines(lines, page_width, page_height, depth)
        finally:
            self._call_stack.pop()

    @staticmethod
    def _strips_passing_other_panel_guards(run, page_width):
        """Reproduce `_panel_split` through its last guard before `require`.

        The shipped function stops at the first accepted strip. This read-only
        scan is deliberately separate so a later eligible strip is also counted
        when an earlier strip was accepted. Keep it in lockstep with the guards
        in the reader; it never returns a split to the reader or changes it.
        """

        words = regions_mod._run_words(run)
        intervals = sorted(
            (regions_mod._number(w, "x0"), regions_mod._number(w, "x1", regions_mod._number(w, "x0")))
            for w in words
        )
        merged: list[list[float]] = []
        for left, right in intervals:
            if not merged or left > merged[-1][1]:
                merged.append([left, right])
            else:
                merged[-1][1] = max(merged[-1][1], right)
        gaps = [
            (merged[index + 1][0] - merged[index][1], (merged[index + 1][0] + merged[index][1]) / 2)
            for index in range(len(merged) - 1)
        ]
        candidates = []
        for gap, cut in sorted(gaps, reverse=True):
            if gap < page_width * regions_mod.PANEL_GAP_WIDTH_SHARE:
                break
            left_words = [w for w in words if regions_mod._number(w, "x1", regions_mod._number(w, "x0")) <= cut]
            right_words = [w for w in words if regions_mod._number(w, "x0") >= cut]
            if len(left_words) < regions_mod.PANEL_MIN_WORDS or len(right_words) < regions_mod.PANEL_MIN_WORDS:
                continue
            left_width = max(regions_mod._number(w, "x1") for w in left_words) - min(regions_mod._number(w, "x0") for w in left_words)
            right_width = max(regions_mod._number(w, "x1") for w in right_words) - min(regions_mod._number(w, "x0") for w in right_words)
            if min(left_width, right_width) < page_width * regions_mod.PANEL_MIN_WIDTH_SHARE:
                continue
            left_top = min(regions_mod._number(w, "top") for w in left_words)
            left_bottom = max(regions_mod._number(w, "bottom") for w in left_words)
            right_top = min(regions_mod._number(w, "top") for w in right_words)
            right_bottom = max(regions_mod._number(w, "bottom") for w in right_words)
            overlap = max(0.0, min(left_bottom, right_bottom) - max(left_top, right_top))
            shorter = min(left_bottom - left_top, right_bottom - right_top)
            if shorter <= 0 or overlap / shorter < regions_mod.PANEL_MIN_VERTICAL_OVERLAP:
                continue
            groups = regions_mod._line_groups(words)
            shared = sum(
                bool([w for w in group if regions_mod._number(w, "x1") <= cut])
                and bool([w for w in group if regions_mod._number(w, "x0") >= cut])
                for group in groups
            )
            if shared / max(len(groups), 1) > regions_mod.PANEL_MAX_SHARED_LINE_SHARE:
                continue
            candidates.append((gap, cut, regions_mod._lines_with_segments(left_words, page_width), regions_mod._lines_with_segments(right_words, page_width)))
        return candidates

    def _wrapped_panel_split(self, run, page_width, require=None):
        if require is None:
            return self._orig_panel_split(run, page_width, require=require)

        current_depth, page_height = self._call_stack[-1] if self._call_stack else (0, 0.0)
        page_key = self.current_page_key

        for gap, cut, left, right in self._strips_passing_other_panel_guards(run, page_width):
            self.gate_candidates.append({
                "page": page_key,
                "depth": current_depth,
                "gap": round(gap, 3),
                "cut": round(cut, 3),
                "accepted": regions_mod._bridges_row_gap(left, right, page_height),
            })

        result = self._orig_panel_split(run, page_width, require=require)
        self.peel_attempts.append({"page": page_key, "depth": current_depth, "fired": result is not None})
        return result


def process_page(page, profile, instrumentation: Instrumentation, page_key: str) -> dict | None:
    words = extract_words(page)
    cleaned = clean_navigation(words, page.chars, float(page.width), float(page.height), profile)
    if not cleaned.body_words:
        return None

    width, height = float(page.width), float(page.height)

    assert regions_mod.MAX_PANEL_PEEL_DEPTH == 3, "expected shipped default of 3 before this run"

    instrumentation.start_page(page_key)
    enabled = reconstruct_by_regions(cleaned.body_words, width, height)

    # Retain this page's enabled-mode evidence before the disabled comparison
    # run starts. The disabled configuration never enters the peel branch.
    page_attempts = [item for item in instrumentation.peel_attempts if item["page"] == page_key]
    page_gate_candidates = [item for item in instrumentation.gate_candidates if item["page"] == page_key]

    shipped_depth = regions_mod.MAX_PANEL_PEEL_DEPTH
    regions_mod.MAX_PANEL_PEEL_DEPTH = 0
    try:
        disabled = reconstruct_by_regions(cleaned.body_words, width, height)
    finally:
        regions_mod.MAX_PANEL_PEEL_DEPTH = shipped_depth

    text_changed = enabled.text != disabled.text
    status_flip = None
    if enabled.status != disabled.status:
        status_flip = f"{disabled.status}->{enabled.status}"  # disabled (baseline) to enabled (shipped)

    return {
        "page_key": page_key,
        "enabled_status": enabled.status,
        "disabled_status": disabled.status,
        "status_flip": status_flip,
        "enabled_region_count": len(enabled.regions),
        "disabled_region_count": len(disabled.regions),
        "region_count_delta": len(enabled.regions) - len(disabled.regions),
        "source_word_count": enabled.source_word_count,
        "enabled_preservation_ratio": enabled.preservation_ratio,
        "disabled_preservation_ratio": disabled.preservation_ratio,
        "text_changed": text_changed,
        "peel_attempts": page_attempts,
        "peel_fired_depths": [item["depth"] for item in page_attempts if item["fired"]],
        "gate_candidates": page_gate_candidates,
        "enabled_text": enabled.text,
        "disabled_text": disabled.text,
        "navigation_item_count": len(cleaned.navigation_items),
    }


def run_sweep() -> tuple[list[dict], Instrumentation, list[tuple[str, str, int]]]:
    rng = random.Random(SEED)
    all_docs = discover_documents()
    sampled_docs = sample_documents(rng, all_docs)

    instrumentation = Instrumentation()
    instrumentation.install()

    page_results: list[dict] = []
    exact_page_list: list[tuple[str, str, int]] = []

    try:
        for ticker, pdf_file, path in sampled_docs:
            target_count = PAGES_PER_DOCUMENT
            with pdfplumber.open(path) as pdf:
                num_pages = len(pdf.pages)
                profile = build_navigation_profile(
                    [(page.chars, float(page.width), float(page.height)) for page in pdf.pages]
                )
                candidate_page_numbers = list(range(1, num_pages + 1))
                rng.shuffle(candidate_page_numbers)
                max_tries = min(len(candidate_page_numbers), target_count * CANDIDATE_OVERSAMPLE)
                used = 0
                for page_number in candidate_page_numbers[:max_tries]:
                    if used >= target_count:
                        break
                    page = pdf.pages[page_number - 1]
                    page_key = f"{ticker}|{pdf_file}|{page_number}"
                    result = process_page(page, profile, instrumentation, page_key)
                    if result is None:
                        continue
                    result["ticker"] = ticker
                    result["pdf_file"] = pdf_file
                    result["year"] = _year_of(pdf_file)
                    result["page_number"] = page_number
                    page_results.append(result)
                    exact_page_list.append((ticker, pdf_file, page_number))
                    used += 1
    finally:
        instrumentation.uninstall()

    if not 300 <= len(page_results) <= 500:
        raise RuntimeError(
            f"Expected a 300-500 page sweep; sampled {len(page_results)} usable body pages."
        )

    return page_results, instrumentation, exact_page_list


def summarize(page_results: list[dict], instrumentation: Instrumentation) -> dict:
    total = len(page_results)

    # 1. Peel fire rate by depth. The requested percentage is page-based, not
    # attempt-based: a page counts once at each depth where it fired.
    attempts_by_depth: dict[int, list[bool]] = {}
    pages_fired_by_depth: dict[int, set[str]] = {}
    for attempt in instrumentation.peel_attempts:
        attempts_by_depth.setdefault(attempt["depth"], []).append(attempt["fired"])
        if attempt["fired"]:
            pages_fired_by_depth.setdefault(attempt["depth"], set()).add(attempt["page"])
    fire_rate_by_depth = {
        depth: {
            "attempts": len(fired_list),
            "fired_attempts": sum(fired_list),
            "pages_fired": len(pages_fired_by_depth.get(depth, set())),
            "page_fire_rate_pct": round(100.0 * len(pages_fired_by_depth.get(depth, set())) / total, 2) if total else 0.0,
        }
        for depth, fired_list in sorted(attempts_by_depth.items())
    }
    pages_with_any_peel = len({a["page"] for a in instrumentation.peel_attempts if a["fired"]})

    # 2. Region-count delta distribution.
    delta_counts: dict[int, int] = {}
    for row in page_results:
        delta_counts[row["region_count_delta"]] = delta_counts.get(row["region_count_delta"], 0) + 1

    # 3. Text-change rate.
    text_changed_count = sum(1 for row in page_results if row["text_changed"])

    # 4. Status flips, retained in both directions for the report.
    flips = [
        {"page": row["page_key"], "flip": row["status_flip"]}
        for row in page_results
        if row["status_flip"] is not None
    ]
    flips_by_direction: dict[str, int] = {}
    for item in flips:
        flips_by_direction[item["flip"]] = flips_by_direction.get(item["flip"], 0) + 1

    # 5. Word preservation.
    preservation_failures = [
        {
            "page": row["page_key"],
            "enabled_ratio": row["enabled_preservation_ratio"],
            "disabled_ratio": row["disabled_preservation_ratio"],
        }
        for row in page_results
        if row["enabled_preservation_ratio"] != 1.0 or row["disabled_preservation_ratio"] != 1.0
    ]

    # 6. Gate effectiveness.
    bridge_true = sum(1 for c in instrumentation.gate_candidates if c["accepted"])
    bridge_false = sum(1 for c in instrumentation.gate_candidates if not c["accepted"])
    pages_with_rejected_strip = len({c["page"] for c in instrumentation.gate_candidates if not c["accepted"]})
    pages_with_accepted_strip = len({c["page"] for c in instrumentation.gate_candidates if c["accepted"]})

    return {
        "total_pages_sampled": total,
        "peel_fire_rate": {
            "by_depth": fire_rate_by_depth,
            "pages_with_any_peel_fired": pages_with_any_peel,
            "pages_with_any_peel_fired_pct": round(100.0 * pages_with_any_peel / total, 2) if total else 0.0,
        },
        "region_count_delta_distribution": dict(sorted(delta_counts.items())),
        "text_change_rate": {
            "changed_pages": text_changed_count,
            "changed_pct": round(100.0 * text_changed_count / total, 2) if total else 0.0,
        },
        "status_flips": flips,
        "status_flip_counts_by_direction": dict(sorted(flips_by_direction.items())),
        "word_preservation_failures": preservation_failures,
        "gate_effectiveness": {
            "total_qualifying_strips": len(instrumentation.gate_candidates),
            "accepted_true": bridge_true,
            "rejected_false": bridge_false,
            "pages_where_a_qualifying_strip_was_rejected_by_gate": pages_with_rejected_strip,
            "pages_where_a_qualifying_strip_was_accepted_by_gate": pages_with_accepted_strip,
        },
    }


def _review_shape(row: dict) -> str:
    """General, deterministic buckets to diversify manual review pages."""

    word_bucket = "small" if row["source_word_count"] < 100 else ("medium" if row["source_word_count"] < 300 else "large")
    baseline_bucket = "few" if row["disabled_region_count"] <= 2 else ("some" if row["disabled_region_count"] <= 5 else "many")
    fired_depths = ",".join(str(depth) for depth in row["peel_fired_depths"]) or "none"
    return f"delta={row['region_count_delta']:+d}; baseline={baseline_bucket}; words={word_bucket}; fired_depths={fired_depths}"


def select_review_pages(page_results: list[dict]) -> list[dict]:
    """Choose up to 15 changed pages across general geometry-output buckets."""

    changed = [row for row in page_results if row["text_changed"]]
    rng = random.Random(SEED + 1)
    selected: list[dict] = []

    def add_one_per_group(key):
        grouped: dict[str | int, list[dict]] = {}
        for row in changed:
            if row not in selected:
                grouped.setdefault(key(row), []).append(row)
        for group in sorted(grouped, key=str):
            candidates = sorted(grouped[group], key=lambda item: item["page_key"])
            rng.shuffle(candidates)
            selected.append(candidates.pop())
            if len(selected) == REVIEW_SET_SIZE:
                return

    # First cover every observed count delta (including contractions), then
    # fill gaps in general page-output shape, recursion depth, and word scale.
    add_one_per_group(lambda row: row["region_count_delta"])
    if len(selected) >= REVIEW_SET_SIZE:
        return selected[:REVIEW_SET_SIZE]
    add_one_per_group(_review_shape)
    if len(selected) >= REVIEW_SET_SIZE:
        return selected[:REVIEW_SET_SIZE]
    add_one_per_group(lambda row: tuple(row["peel_fired_depths"]))
    if len(selected) >= REVIEW_SET_SIZE:
        return selected[:REVIEW_SET_SIZE]
    add_one_per_group(lambda row: "small" if row["source_word_count"] < 100 else ("medium" if row["source_word_count"] < 300 else "large"))
    if len(selected) >= REVIEW_SET_SIZE:
        return selected[:REVIEW_SET_SIZE]
    add_one_per_group(lambda row: "few" if row["disabled_region_count"] <= 2 else ("some" if row["disabled_region_count"] <= 5 else "many"))
    if len(selected) >= REVIEW_SET_SIZE:
        return selected[:REVIEW_SET_SIZE]

    remaining = [row for row in changed if row not in selected]
    remaining.sort(key=lambda item: item["page_key"])
    rng.shuffle(remaining)
    selected.extend(remaining[:max(0, REVIEW_SET_SIZE - len(selected))])
    return selected


def render_review_set(page_results: list[dict]) -> list[dict]:
    """Write page PNGs and an unscored HTML comparison for changed pages."""

    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    review_rows = select_review_pages(page_results)
    manifest: list[dict] = []
    html_sections: list[str] = []

    for index, row in enumerate(review_rows, 1):
        slug = re.sub(r"[^A-Za-z0-9]+", "_", row["page_key"]).strip("_")
        image_name = f"{index:02d}_{slug}.png"
        image_path = RENDER_DIR / image_name
        render_error = None
        try:
            pdf_path = RAW_ROOT / row["ticker"] / row["pdf_file"]
            with pdfplumber.open(pdf_path) as pdf:
                pdf.pages[row["page_number"] - 1].to_image(resolution=150).save(image_path, format="PNG")
        except Exception as exc:  # Report render failures without hiding the measurement result.
            render_error = f"{type(exc).__name__}: {exc}"

        review = {
            "review_number": index,
            "page": row["page_key"],
            "review_shape": _review_shape(row),
            "image_file": image_name if render_error is None else None,
            "render_error": render_error,
            "disabled_status": row["disabled_status"],
            "enabled_status": row["enabled_status"],
            "disabled_region_count": row["disabled_region_count"],
            "enabled_region_count": row["enabled_region_count"],
            "human_score": None,
        }
        manifest.append(review)
        image_html = (
            f'<img src="{escape(image_name)}" alt="Rendered source page">'
            if render_error is None
            else f'<p class="render-error">Render failed: {escape(render_error)}</p>'
        )
        html_sections.append(
            "<article>"
            f"<h2>{index}. {escape(row['page_key'])}</h2>"
            f"<p>Automated review bucket: <code>{escape(_review_shape(row))}</code><br>"
            "Human score (leave unmarked until reviewed): ☐ Better ☐ Same ☐ Worse</p>"
            "<div class=\"grid\">"
            f"<section><h3>Source page</h3>{image_html}</section>"
            f"<section><h3>Peel disabled (depth 0)</h3><pre>{escape(row['disabled_text'])}</pre></section>"
            f"<section><h3>As shipped (depth 3)</h3><pre>{escape(row['enabled_text'])}</pre></section>"
            "</div></article>"
        )

    review_html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Panel-peel review set</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; background: #f6f7f9; color: #202124; }
article { background: white; border: 1px solid #d7dbe0; padding: 16px; margin: 0 0 24px; }
h1, h2, h3 { margin-top: 0; } .grid { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(300px, 1fr) minmax(300px, 1fr); gap: 16px; align-items: start; }
img { width: 100%; border: 1px solid #bbb; } pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 12px; background: #111827; color: #e5e7eb; min-height: 240px; }
.render-error { color: #b42318; } @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
</style></head><body>
<h1>Panel-peel changed-text review set</h1>
<p>These pages were selected automatically from text changes using general output buckets. No Better / Same / Worse judgement has been made here.</p>
""" + "\n".join(html_sections) + "\n</body></html>\n"
    (RENDER_DIR / "review_set.html").write_text(review_html, encoding="utf-8")
    (RENDER_DIR / "review_set.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def write_outputs(page_results: list[dict], summary: dict, exact_page_list: list[tuple[str, str, int]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    review_manifest = render_review_set(page_results)
    summary["review_set"] = {
        "selected_pages": len(review_manifest),
        "requested_pages": REVIEW_SET_SIZE,
        "manifest": "renders/review_set/review_set.json",
        "paired_review": "renders/review_set/review_set.html",
    }

    (OUT_DIR / "peel_sweep_results.json").write_text(
        json.dumps(page_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "peel_sweep_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    changed_pages = [row for row in page_results if row["text_changed"]]
    sampled_docs = {(row["ticker"], row["pdf_file"], row["year"]) for row in page_results}
    years: dict[str, int] = {}
    for _, _, year in sampled_docs:
        years[year] = years.get(year, 0) + 1

    lines = [
        "# Reading-order peel sweep — blast radius measurement",
        "",
        "Date: 2026-07-30",
        f"Seed: {SEED}",
        "",
        "This is a measurement-only pass over `src/esg_reading_regions.py`'s panel-peel",
        "change (peel-before-runs, gated by `_bridges_row_gap`, capped by",
        "`MAX_PANEL_PEEL_DEPTH = 3`). No source file was modified or reparsed; nothing here",
        "is wired into production. Each sampled page was run twice in-process: once as",
        "shipped, once with `MAX_PANEL_PEEL_DEPTH` monkeypatched to 0.",
        "",
        f"## Sample: {summary['total_pages_sampled']} pages",
        "",
        f"Documents sampled: {len(sampled_docs)} across {len({row['ticker'] for row in page_results})} tickers,",
        f"one document per ticker, {PAGES_PER_DOCUMENT} body pages targeted per document; pages with no body words skipped.",
        "Documents by report year: " + ", ".join(f"{year}: {count}" for year, count in sorted(years.items())),
        "Exact page list: see `sampled_pages.txt` in this directory.",
        "",
        "## 1. Peel fire rate by recursion depth",
        "",
        "| Depth | Attempts reached | Fired attempts | Pages fired | Page fire rate |",
        "|---|---|---|---|---|",
    ]
    for depth, row in summary["peel_fire_rate"]["by_depth"].items():
        lines.append(
            f"| {depth} | {row['attempts']} | {row['fired_attempts']} | {row['pages_fired']} | {row['page_fire_rate_pct']}% |"
        )
    lines += [
        "",
        f"Pages where the peel fired at least once (any depth): "
        f"{summary['peel_fire_rate']['pages_with_any_peel_fired']} / {summary['total_pages_sampled']} "
        f"({summary['peel_fire_rate']['pages_with_any_peel_fired_pct']}%)",
        "",
        "## 2. Region-count delta distribution (enabled minus disabled)",
        "",
        "| Delta | Pages |",
        "|---|---|",
    ]
    for delta, count in summary["region_count_delta_distribution"].items():
        lines.append(f"| {delta:+d} | {count} |")
    lines += [
        "",
        "## 3. Text-change rate",
        "",
        f"{summary['text_change_rate']['changed_pages']} / {summary['total_pages_sampled']} pages "
        f"({summary['text_change_rate']['changed_pct']}%) had a different `candidate.text` between the",
        "two configurations.",
        "",
        "## 4. Status flips",
        "",
        f"- `needs_review -> candidate_ready`: {summary['status_flip_counts_by_direction'].get('needs_review->candidate_ready', 0)}",
        f"- `candidate_ready -> needs_review`: {summary['status_flip_counts_by_direction'].get('candidate_ready->needs_review', 0)}",
        "",
    ]
    if summary["status_flips"]:
        lines.append("| Page | Flip (disabled -> enabled) |")
        lines.append("|---|---|")
        for flip in summary["status_flips"]:
            lines.append(f"| {flip['page']} | {flip['flip']} |")
    else:
        lines.append("None observed.")
    lines += [
        "",
        "## 5. Word preservation",
        "",
    ]
    if summary["word_preservation_failures"]:
        lines.append("**CORRECTNESS BUG** — ratio was not 1.0 on the following pages:")
        lines.append("")
        lines.append("| Page | Enabled ratio | Disabled ratio |")
        lines.append("|---|---|---|")
        for fail in summary["word_preservation_failures"]:
            lines.append(f"| {fail['page']} | {fail['enabled_ratio']} | {fail['disabled_ratio']} |")
    else:
        lines.append(f"`preservation_ratio == 1.0` in both configurations on all {summary['total_pages_sampled']} sampled pages.")
    lines += [
        "",
        "## 6. Gate effectiveness (`_bridges_row_gap`)",
        "",
        "Counts only strips that already cleared every other `_panel_split` guard (gap width,",
        "word count, side width, vertical overlap, shared-line share) — i.e. every call here",
        "is a case the gate alone decided.",
        "The instrumentation scans every such strip at each reached peel node, including strips",
        "that the shipped widest-first selection would not reach after accepting an earlier strip.",
        "",
        f"- Total qualifying strips evaluated: {summary['gate_effectiveness']['total_qualifying_strips']}",
        f"- Accepted (bridges a row gap, peel allowed): {summary['gate_effectiveness']['accepted_true']}",
        f"- Rejected (does not bridge, peel blocked): {summary['gate_effectiveness']['rejected_false']}",
        f"- Pages with at least one qualifying strip REJECTED by the gate: "
        f"{summary['gate_effectiveness']['pages_where_a_qualifying_strip_was_rejected_by_gate']}",
        f"- Pages with at least one qualifying strip ACCEPTED by the gate: "
        f"{summary['gate_effectiveness']['pages_where_a_qualifying_strip_was_accepted_by_gate']}",
        "",
    ]
    if summary["gate_effectiveness"]["rejected_false"] == 0:
        lines.append(
            "Rejected count is ~0: on this sample, every strip that cleared the other guards also"
            " bridged a row gap. The gate did not have to do any rejecting work here — decorative"
            " on this sample, though it may still matter on layouts not sampled."
        )
    else:
        lines.append(
            "Rejected count is non-trivial: the gate is doing real, load-bearing work rejecting"
            " strips that pass every other guard but do not bridge a row gap."
        )
    lines += [
        "",
        "## Changed-text pages (candidates for the paired-render review set)",
        "",
        f"{len(changed_pages)} pages had a text difference. The paired, unscored review set has",
        f"{summary['review_set']['selected_pages']} pages: `renders/review_set/review_set.html`.",
        "",
    ]
    for row in changed_pages:
        lines.append(
            f"- `{row['page_key']}` — regions {row['disabled_region_count']} -> {row['enabled_region_count']}"
            f" ({row['region_count_delta']:+d}), status {row['disabled_status']} -> {row['enabled_status']}"
        )

    (OUT_DIR / "peel_sweep_summary.md").write_text("\n".join(lines), encoding="utf-8")

    (OUT_DIR / "sampled_pages.txt").write_text(
        "\n".join(f"{ticker}\t{pdf_file}\t{page_number}" for ticker, pdf_file, page_number in exact_page_list),
        encoding="utf-8",
    )


def main() -> None:
    page_results, instrumentation, exact_page_list = run_sweep()
    summary = summarize(page_results, instrumentation)
    write_outputs(page_results, summary, exact_page_list)

    print(f"Sampled {summary['total_pages_sampled']} pages.")
    print(f"Text changed on {summary['text_change_rate']['changed_pages']} pages "
          f"({summary['text_change_rate']['changed_pct']}%).")
    print(f"Peel fired on {summary['peel_fire_rate']['pages_with_any_peel_fired']} pages "
          f"({summary['peel_fire_rate']['pages_with_any_peel_fired_pct']}%).")
    print(f"Gate rejected {summary['gate_effectiveness']['rejected_false']} / "
          f"{summary['gate_effectiveness']['total_qualifying_strips']} qualifying strips.")
    if summary["word_preservation_failures"]:
        print(f"WORD PRESERVATION FAILURES on {len(summary['word_preservation_failures'])} pages — see report.")


if __name__ == "__main__":
    main()
