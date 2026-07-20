"""Build the VLM chunk index from extraction artifacts, with inherited sections.

Sections are INHERITED, never recomputed (owner-approved design): each artifact page maps
to the section instance whose existing chunks cover that page (majority rule on ties,
ambiguity recorded). Output: data/04_vlm/vlm_chunks_index.csv — additive lineage
`vlm_extraction_v1`; consumed by the vector-manifest builder when VLM integration is
switched on. Read-only against everything except data/04_vlm/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "04_vlm"
MANIFEST = ROOT / "data" / "00_reference" / "vector_index_manifest.csv"


def page_section_map(manifest: pd.DataFrame) -> dict[tuple[str, str, int], tuple[str, str, bool]]:
    """(ticker, pdf_stem, page) -> (section_label, section_instance_id, ambiguous)."""
    out: dict[tuple[str, str, int], dict[tuple[str, str], int]] = {}
    m = manifest.dropna(subset=["page_start", "page_end"])
    for _, r in m.iterrows():
        try:
            lo, hi = int(r["page_start"]), int(r["page_end"])
        except (TypeError, ValueError):
            continue
        for p in range(lo, hi + 1):
            key = (r["ticker"], r["pdf_stem"], p)
            votes = out.setdefault(key, {})
            sec = (str(r["section_label"]), str(r["section_instance_id"]))
            votes[sec] = votes.get(sec, 0) + 1
    resolved = {}
    for key, votes in out.items():
        best = max(votes.items(), key=lambda kv: kv[1])
        resolved[key] = (best[0][0], best[0][1], len(votes) > 1)
    return resolved


def main() -> None:
    manifest = pd.read_csv(MANIFEST)
    sec_map = page_section_map(manifest)
    ext_dir = DATA / "extraction"
    rows = []
    for meta_path in sorted(ext_dir.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if "error" in meta:
            continue
        md_path = ext_dir / f"{meta['key']}.md"
        if not md_path.exists():
            continue
        sec = sec_map.get((meta["ticker"], meta["pdf_stem"], int(meta["page"])),
                          ("UNMAPPED", "UNMAPPED", False))
        screen = meta.get("screen", {})
        rows.append({
            # Page-key id: unique per (ticker, stem, page) even when two registry
            # entries share one source PDF (content-hash ids collided there).
            "chunk_id": f"vlm_{meta['key']}",
            "ticker": meta["ticker"], "pdf_stem": meta["pdf_stem"],
            "page": int(meta["page"]),
            "section_label": sec[0], "section_instance_id": sec[1],
            "section_ambiguous": sec[2],
            "chunk_file": str(md_path.relative_to(ROOT)).replace("\\", "/"),
            "lineage": meta["lineage"], "model": meta["model"],
            "prompt_hash": meta["prompt_hash"], "cache_key": meta["cache_key"],
            "n_numbers_total": screen.get("n_numbers_total", ""),
            "n_text_corroborated": screen.get("n_text_corroborated", ""),
            "graphic_only_count": len(screen.get("graphic_only_numbers", [])),
            "body_uncorroborated_count": len(screen.get("body_uncorroborated", [])),
        })
    df = pd.DataFrame(rows)
    out = DATA / "vlm_chunks_index.csv"
    df.to_csv(out, index=False)
    unmapped = int((df["section_label"] == "UNMAPPED").sum()) if len(df) else 0
    print(json.dumps({"chunks": len(df), "unmapped_sections": unmapped,
                      "ambiguous_sections": int(df["section_ambiguous"].sum()) if len(df) else 0,
                      "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
