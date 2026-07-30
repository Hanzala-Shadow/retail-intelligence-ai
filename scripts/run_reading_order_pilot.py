"""Run the default-off Bundle 2 region reader on the 12 scoped pages only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from esg_navigation import build_navigation_profile, clean_navigation  # noqa: E402
from esg_reading_regions import reconstruct_by_regions  # noqa: E402

RAW_ROOT = REPO_ROOT / "data" / "01_raw" / "sustainability"
OUT_DIR = REPO_ROOT / "reports" / "reading_order_bundle2_2026-07-30"

PAGES = [
    ("target", "BBY", "BBY-BEST BUY CO INC-2024.pdf", 46, ["Securing customer information", "Cybersecurity"]),
    ("target", "BBWI", "BBWI-BATH & BODY WORKS INC-2023.pdf", 22, ["WE EMBRACE DIVERSITY", "IS ALL OF US"]),
    ("target", "BBWI", "BBWI-BATH & BODY WORKS INC-2023.pdf", 36, ["Supporting Purpose-Driven Marketing", "ACS"]),
    ("target", "BBW", "BBW-BUILD-A-BEAR WORKSHOP INC-2023.pdf", 2, ["Table of Contents", "About This Report"]),
    ("target", "AMZN", "AMZN-AMAZON.COM INC-2023.pdf", 63, ["Sustainability Solutions Hub", "Looking Forward"]),
    ("target", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf", 8, ["2023 PROGRESS", "TOTAL PREFERRED FIBERS"]),
    ("target", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2024.pdf", 8, ["2024 PROGRESS", "TOTAL PREFERRED FIBERS"]),
    ("control", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf", 2, ["INTRODUCTION"]),
    ("control", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf", 6, ["2023 REAL GOOD BY THE NUMBERS"]),
    ("control", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf", 10, ["2023 SUSTAINABLE POLYESTER BREAKDOWN"]),
    ("control", "AEO", "AEO-AMERICAN EAGLE OUTFITTERS INC-2023.pdf", 3, ["BUILDING A BETTER PLANET"]),
    ("control", "AAPL", "AAPL-APPLE INC-2024.pdf", 97, ["In this section", "Report notes"]),
]


def extract_words(page) -> list[dict]:
    try:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "upright"]) or []
    except TypeError:
        return page.extract_words(use_text_flow=False, keep_blank_chars=False) or []


def _landmarks(text: str, requested: list[str]) -> list[dict]:
    folded = " ".join(text.split()).casefold()
    found = []
    for label in requested:
        position = folded.find(label.casefold())
        found.append({"text": label, "position": position, "found": position >= 0})
    return sorted(found, key=lambda item: item["position"] if item["position"] >= 0 else 10**9)


def run_bundle() -> list[dict]:
    results: list[dict] = []
    opened: dict[str, pdfplumber.PDF] = {}
    profiles: dict[str, tuple] = {}
    try:
        for role, ticker, pdf_file, page_number, requested_landmarks in PAGES:
            path = RAW_ROOT / ticker / pdf_file
            if pdf_file not in opened:
                opened[pdf_file] = pdfplumber.open(path)
            pdf = opened[pdf_file]
            if pdf_file not in profiles:
                profiles[pdf_file] = build_navigation_profile(
                    [(page.chars, float(page.width), float(page.height)) for page in pdf.pages]
                )
            page = pdf.pages[page_number - 1]
            words = extract_words(page)
            cleaned = clean_navigation(words, page.chars, float(page.width), float(page.height), profiles[pdf_file])
            candidate = reconstruct_by_regions(cleaned.body_words, float(page.width), float(page.height))
            source = sorted(str(w.get("text", "")).strip().casefold() for w in cleaned.body_words if str(w.get("text", "")).strip())
            output = sorted(part.casefold() for line in candidate.text.splitlines() if not line.startswith("[excluded") for part in line.split())
            regions = [
                {"bbox": [round(r.left, 1), round(r.top, 1), round(r.right, 1), round(r.bottom, 1)],
                 "region_type": r.region_type, "column_count": r.column_count, "reason": r.reason}
                for r in candidate.regions
            ]
            usable = candidate.status == "candidate_ready" and source == output
            visual_verdict = "improved" if role == "target" and usable else ("unchanged" if role == "control" and usable else "needs review")
            # These two pages still have fewer clear geometry regions than the
            # visual audit calls for. Reporting stays honest even though word
            # preservation and the candidate safety checks pass.
            if (ticker, page_number) in {("BBWI", 36), ("AMZN", 63)}:
                visual_verdict = "needs review"
            results.append({
                "role": role, "ticker": ticker, "pdf_file": pdf_file, "page": page_number,
                "candidate_status": candidate.status, "candidate_reason": candidate.reason,
                "regions": regions, "source_word_count": candidate.source_word_count,
                "candidate_word_count": candidate.candidate_word_count,
                "word_preservation_passed": source == output,
                "preservation_ratio": candidate.preservation_ratio,
                "important_landmarks_in_output_order": _landmarks(candidate.text, requested_landmarks),
                "navigation_item_count": len(cleaned.navigation_items),
                "rotated_content_item_count": len(cleaned.rotated_content_items),
                "visual_verdict": visual_verdict,
                "candidate_text": candidate.text,
            })
    finally:
        for pdf in opened.values():
            pdf.close()
    return results


def write_outputs(results: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "bundle2_summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Reading order Bundle 2 report",
        "",
        "Date: 2026-07-30",
        "",
        "This is a default-off candidate. `candidate_ready` is not proof that page order is correct.",
        "Region counts are not proof either: BBWI-2023 p22 produced five regions while emitting three",
        "panels a slice at a time. The regression tests assert the order of the reconstructed text.",
        "",
        "## Thresholds added",
        "",
        "- Panel gap: 3.5% of page width.",
        "- Panel minimum: 10 words and 12% of page width on each side.",
        "- Panel vertical overlap: at least 30% of the shorter side.",
        "- Shared visual lines: at most 82% for an independent panel split.",
        "- Structure window: 4 visual lines on each side of a possible boundary.",
        "- Column-start change tolerance: 5.5% of content width, with a 10 point floor.",
        "- Panel peel depth: at most 3 nested side-by-side splits per page.",
        "",
        "## Ordering rules",
        "",
        "- A region is peeled into side-by-side panels **before** it is cut into vertical runs, but",
        "  only when one side reaches across a row gap that would otherwise split the other. That is",
        "  the one layout a run-first reader cannot order: a panel spanning the row breaks beside it",
        "  keeps any run boundary from falling between those rows, so every run holds one slice of",
        "  each panel. Where nothing spans, runs-then-panels is left to do the work unchanged.",
        "- A change in column structure is only a region boundary when the side that reads as",
        "  full-width really consists of one-segment lines. A four-line window under-reports any",
        "  column that happens to be sparse within it, which otherwise invents boundaries inside",
        "  two-panel layouts whose lines interleave.",
        "",
        "## Page results",
        "",
    ]
    for row in results:
        lines += [f"### {row['ticker']} — {row['pdf_file']} page {row['page']} ({row['role']})", "", f"- Status: `{row['candidate_status']}` — {row['candidate_reason']}", f"- Word preservation: {'pass' if row['word_preservation_passed'] else 'FAIL'} ({row['source_word_count']} source words)", f"- Visual verdict: {row['visual_verdict']}", f"- Navigation removed: {row['navigation_item_count']} items", "- Regions:"]
        for index, region in enumerate(row["regions"], 1):
            lines.append(f"  {index}. bbox `{region['bbox']}`, {region['region_type']}, {region['column_count']} column(s), {region['reason']}")
        labels = [item["text"] for item in row["important_landmarks_in_output_order"] if item["found"]]
        lines += [f"- Landmarks in output order: {' → '.join(labels) if labels else 'none found'}", ""]
    (OUT_DIR / "bundle2_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    rows = run_bundle()
    write_outputs(rows)
    for row in rows:
        print(f"{row['ticker']} p{row['page']}: {row['candidate_status']}, regions={len(row['regions'])}, preservation={row['word_preservation_passed']}")
