"""On-demand regression check: re-extract the owner-approved 50-page pack and diff
against the triple-reviewed baselines. Run after ANY change to the model snapshot or a
prompt (the pinned-prompt unit test failing is the reminder). Costs ~$0.15.

    OPENAI_API_KEY=... python esg/scripts/vlm_regression_check.py

PASS bar (pre-registered): no approved text-corroborated number may disappear from more
than 2 pages, and no new body-uncorroborated numbers on more than 2 pages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import esg_vlm_stage as vlm  # noqa: E402

PACK = ROOT / "tmp" / "esg_extraction_pack_20260720"
BASELINE_DIR = PACK / "extractions_mini_low_v2"  # owner-approved 50/50 (2026-07-20)


def main() -> None:
    manifest = pd.read_csv(PACK / "pack_manifest.csv")
    items, keys = [], {}
    for _, r in manifest.iterrows():
        png = PACK / r["image"]
        items.append((r["pack_id"], vlm.request_body(vlm.EXTRACTION_CONFIG,
                                                     png.read_bytes())))
        keys[r["pack_id"]] = r
    results: dict[str, str] = {}

    def on_result(k, content, usage):
        results[k] = content or ""

    vlm.run_sync(items, on_result)

    bad_missing, bad_new = [], []
    for pid, new_md in results.items():
        base_md = (BASELINE_DIR / f"{pid}.md").read_text(encoding="utf-8")
        wl = json.loads((PACK / "wordlists" / f"{pid}.json").read_text(encoding="utf-8"))
        words = [w["text"] for w in wl["words"]]
        pool = vlm.text_layer_pool(words)
        base_verified = vlm.numeric_tokens(base_md) & pool
        missing = base_verified - vlm.numeric_tokens(new_md)
        new_ann = vlm.screen_annotation(new_md, words)
        if missing:
            bad_missing.append((pid, sorted(missing)[:8]))
        if new_ann["body_uncorroborated"]:
            bad_new.append((pid, new_ann["body_uncorroborated"][:8]))
    verdict = "PASS" if len(bad_missing) <= 2 and len(bad_new) <= 2 else "FAIL"
    print(json.dumps({"verdict": verdict,
                      "pages_missing_approved_numbers": bad_missing,
                      "pages_with_new_uncorroborated": bad_new}, indent=2))
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
