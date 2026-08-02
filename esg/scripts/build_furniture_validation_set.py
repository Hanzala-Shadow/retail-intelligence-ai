"""Build a held-out, occurrence-level validation set for the page-furniture rule.

Read-only. Writes a JSON review package + page renders + crops into an output
directory. Nothing in data/ is touched.

Ribbon definition under test (Aziz's, 2026-08-02):
    a ribbon is text that REPEATS across pages and is PINNED TO A PAGE EDGE
    (top, bottom, left or right - not just the top) at SMALL font.

Features are computed in PDF coordinates, never from parsed-text line index.
Line index is a derived quantity that reading-order reconstruction corrupts;
`top`/`x0` are raw and survive it.

Usage:
    python esg/scripts/build_furniture_validation_set.py --out tmp/furniture_validation
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import re
import statistics
import sys

import pdfplumber

EDGE_BAND = 0.08          # within 8% of any page edge counts as edge-pinned
MIN_PAGES_ABS = 3         # repeat threshold, absolute floor
MIN_PAGES_SHARE = 0.10    # repeat threshold, share of pages
COORD_TOLERANCE = 6.0     # points; same position across pages
RENDER_DPI = 90
CROP_PAD = 90             # points of context around the occurrence crop

# Tickers already judged by Aziz in the v1 (>2-word) and 2b (<=2-word) rounds.
# Held-out validation must not reuse them.
SEEN_TICKERS = {
    "MELI", "NKE", "RL", "DECK", "WOOF", "ULTA", "VFC", "HD", "SFM", "SONO",
    "BIRD", "ORLY", "VZ", "BBWI", "BURL", "DELL", "WMT", "VVV", "SHOO",
    "GPRO", "COLM", "HOFT", "AAPL", "COST", "TGT", "LOVE", "PTRN", "GAP",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.strip())).lower().strip()


def edge_distance(word, width, height):
    """Normalised distance to the nearest page edge, and which edge."""
    d = {
        "top": word["top"] / height,
        "bottom": (height - word["bottom"]) / height,
        "left": word["x0"] / width,
        "right": (width - word["x1"]) / width,
    }
    edge = min(d, key=d.get)
    return edge, d[edge]


def scan_document(path, max_pages=None):
    """Return per-page word lists plus page dimensions."""
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, pg in enumerate(pdf.pages):
            if max_pages and i >= max_pages:
                break
            try:
                words = pg.extract_words(extra_attrs=["size", "upright", "fontname"])
            except Exception:
                words = []
            pages.append({
                "page": i + 1,
                "width": float(pg.width),
                "height": float(pg.height),
                "words": words,
            })
    return pages


def build_line_groups(words, tol=2.5):
    """Group words into visual lines by their `top` coordinate."""
    lines = collections.defaultdict(list)
    for w in words:
        key = round(w["top"] / tol)
        lines[key].append(w)
    out = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda w: w["x0"])
        out.append({
            "text": " ".join(w["text"] for w in ws),
            "top": min(w["top"] for w in ws),
            "bottom": max(w["bottom"] for w in ws),
            "x0": min(w["x0"] for w in ws),
            "x1": max(w["x1"] for w in ws),
            "size": statistics.median([w.get("size") or 0 for w in ws]),
            "words": ws,
        })
    return out


def analyse(pages):
    """Find repeated lines and score every occurrence."""
    page_count = len(pages)
    if not page_count:
        return [], {}
    threshold = max(MIN_PAGES_ABS, int(MIN_PAGES_SHARE * page_count))

    per_page_lines = {p["page"]: build_line_groups(p["words"]) for p in pages}

    seen = collections.defaultdict(set)
    for p in pages:
        for ln in per_page_lines[p["page"]]:
            n = norm(ln["text"])
            if len(n) > 3 and not n.startswith("|"):
                seen[n].add(p["page"])
    repeated = {n: pgs for n, pgs in seen.items() if len(pgs) >= threshold}

    body_size = statistics.median(
        [w.get("size") or 0 for p in pages for w in p["words"]] or [10.0]
    ) or 10.0

    occurrences = []
    for p in pages:
        W, H = p["width"], p["height"]
        lines = per_page_lines[p["page"]]
        for idx, ln in enumerate(lines):
            n = norm(ln["text"])
            if n not in repeated:
                continue
            fake = {"top": ln["top"], "bottom": ln["bottom"], "x0": ln["x0"], "x1": ln["x1"]}
            edge, dist = edge_distance(fake, W, H)
            neighbours = []
            for j in range(max(0, idx - 3), min(len(lines), idx + 4)):
                if j == idx:
                    continue
                neighbours.append({
                    "text": lines[j]["text"][:110],
                    "is_repeated": norm(lines[j]["text"]) in repeated,
                    "top": round(lines[j]["top"], 1),
                    "size": round(lines[j]["size"], 1),
                })
            occurrences.append({
                "page": p["page"],
                "text": ln["text"][:160],
                "normalized": n,
                "page_width": round(W), "page_height": round(H),
                "bbox": [round(ln["x0"], 1), round(ln["top"], 1),
                         round(ln["x1"], 1), round(ln["bottom"], 1)],
                "font_size": round(ln["size"], 1),
                "rel_font_size": round((ln["size"] or 0) / body_size, 2),
                "nearest_edge": edge,
                "edge_distance_pct": round(100 * dist, 1),
                "edge_pinned": dist <= EDGE_BAND,
                "repeats_on_pages": len(repeated[n]),
                "total_pages": page_count,
                "neighbours": neighbours,
                "adjacent_repeated": sum(1 for x in neighbours if x["is_repeated"]),
            })

    # coordinate stability: does this text land at the same spot on every page?
    by_text = collections.defaultdict(list)
    for o in occurrences:
        by_text[o["normalized"]].append(o)
    for n, group in by_text.items():
        tops = [o["bbox"][1] for o in group]
        lefts = [o["bbox"][0] for o in group]
        stable = (statistics.pstdev(tops) <= COORD_TOLERANCE if len(tops) > 1 else True) or \
                 (statistics.pstdev(lefts) <= COORD_TOLERANCE if len(lefts) > 1 else True)
        for o in group:
            o["coord_stable"] = bool(stable)
            o["predicted_furniture"] = bool(
                o["edge_pinned"] and stable and o["rel_font_size"] <= 1.3
            )
    return occurrences, repeated


def render(path, page_no, occ, outdir, stem):
    """Render the full page and a crop around the occurrence."""
    paths = {}
    try:
        with pdfplumber.open(path) as pdf:
            pg = pdf.pages[page_no - 1]
            im = pg.to_image(resolution=RENDER_DPI)
            full = os.path.join(outdir, "pages", f"{stem}_p{page_no}.png")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            im.save(full)
            paths["page_image"] = os.path.relpath(full, outdir).replace("\\", "/")

            x0, top, x1, bottom = occ["bbox"]
            box = (max(0, x0 - CROP_PAD), max(0, top - CROP_PAD),
                   min(pg.width, x1 + CROP_PAD), min(pg.height, bottom + CROP_PAD))
            crop = pg.crop(box).to_image(resolution=RENDER_DPI * 2)
            cp = os.path.join(outdir, "crops", f"{stem}_p{page_no}_{int(top)}.png")
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            crop.save(cp)
            paths["crop_image"] = os.path.relpath(cp, outdir).replace("\\", "/")
    except Exception as exc:  # rendering is best-effort; the JSON is the deliverable
        paths["render_error"] = str(exc)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tmp/furniture_validation")
    ap.add_argument("--docs", type=int, default=8, help="held-out documents to scan")
    ap.add_argument("--cases", type=int, default=20, help="occurrences to export")
    ap.add_argument("--max-pages", type=int, default=70)
    ap.add_argument("--seed", type=int, default=20260802)
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    random.seed(args.seed)
    os.makedirs(args.out, exist_ok=True)

    all_pdfs = glob.glob("data/01_raw/sustainability/*/*.pdf")
    fresh = [p for p in all_pdfs
             if os.path.basename(os.path.dirname(p)).upper() not in SEEN_TICKERS]
    random.shuffle(fresh)
    chosen = fresh[:args.docs]
    print(f"held-out pool: {len(fresh)} pdfs from {len(all_pdfs)} total")
    for p in chosen:
        print("  ", os.path.basename(p))

    pool = []
    for path in chosen:
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            pages = scan_document(path, args.max_pages)
            occ, _ = analyse(pages)
        except Exception as exc:
            print(f"  SKIP {stem}: {exc}")
            continue
        for o in occ:
            o["pdf"] = path.replace("\\", "/")
            o["stem"] = stem
        pool.extend(occ)
        print(f"  {stem}: {len(occ)} repeated-line occurrences")

    yes = [o for o in pool if o["predicted_furniture"]]
    no = [o for o in pool if not o["predicted_furniture"]]
    print(f"\npool: {len(pool)} occurrences | predicted furniture {len(yes)} | predicted content {len(no)}")

    half = args.cases // 2
    sample = random.sample(yes, min(half, len(yes))) + random.sample(no, min(args.cases - half, len(no)))
    random.shuffle(sample)  # blind: reviewer must not see the prediction ordering

    cases = []
    for i, o in enumerate(sample, 1):
        o = dict(o)
        o["case_id"] = i
        o.update(render(o["pdf"], o["page"], o, args.out, o["stem"]))
        # prediction is kept OUT of the reviewer-facing fields; stored separately
        cases.append(o)

    payload = {
        "generated": "2026-08-02",
        "purpose": "Held-out validation of the page-furniture rule. Occurrence-level, blind.",
        "definition_under_test": (
            "A ribbon/furniture line REPEATS across pages AND is pinned to a page EDGE "
            "(top, bottom, left or right) at small font. Coordinates are PDF coordinates."
        ),
        "reviewer_question": "Is this line page furniture (ribbon / running header / footer / sidebar TOC), or real content?",
        "answer_values": ["furniture", "content", "borderline"],
        "held_out_note": "No document here was used in the earlier review rounds.",
        "cases": cases,
    }
    out_json = os.path.join(args.out, "validation_cases.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    print(f"\nwrote {len(cases)} cases -> {out_json}")
    print(f"page renders -> {args.out}/pages/   crops -> {args.out}/crops/")


if __name__ == "__main__":
    main()
