"""Compare a candidate recovery audit/manifest against the live v8 baseline.

Read-only. Reports exactly the items the recovery brief asks for, and re-runs
the navigation and table leakage checks against the candidate audit so a
coverage gain cannot quietly reintroduce either.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config


def read(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def doc_coverage(manifest: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in manifest:
        key = ((row.get("ticker") or "").upper(), row.get("pdf_stem") or "")
        totals[key][1] += 1
        if (row.get("eligibility_decision") or "") == "eligible":
            totals[key][0] += 1
    return {k: (v[0], v[1]) for k, v in totals.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-audit", type=Path, default=Path(config.ESG_PAGE_LAYOUT_QA_CSV))
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=Path(config.REFERENCE_DIR) / "vector_index_manifest.csv",
    )
    parser.add_argument("--candidate-audit", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    args = parser.parse_args()

    base_a, cand_a = read(args.baseline_audit), read(args.candidate_audit)
    base_m, cand_m = read(args.baseline_manifest), read(args.candidate_manifest)

    print("=" * 72)
    print("RECOVERY CANDIDATE vs LIVE v8 BASELINE")
    print("=" * 72)

    print("\n-- page decisions --")
    b, c = Counter(r["decision"] for r in base_a), Counter(r["decision"] for r in cand_a)
    print(f"{'decision':46} {'base':>7} {'cand':>7} {'delta':>7}")
    for key in sorted(set(b) | set(c)):
        print(f"{key:46} {b.get(key,0):7} {c.get(key,0):7} {c.get(key,0)-b.get(key,0):+7}")

    print("\n-- pages recovered, by chosen reader --")
    recovered = [r for r in cand_a if r["decision"] == "auto_pass_recovered_region_order"]
    print(f"recovered and indexable now: {len(recovered)}")
    print("  by parser:", dict(Counter(r.get("recovery_parser", "") for r in recovered)))
    reparse = [r for r in cand_a if "recoverable_by_reparse" in (r.get("decision_reason") or "")]
    print(f"safe only after a reparse (still held): {len(reparse)}")
    print("  by parser:", dict(Counter(r.get("recovery_parser", "") for r in reparse)))

    print("\n-- pages still held --")
    held = [r for r in cand_a if r["decision"] == "auto_hold"]
    print(f"held: {len(held)} (baseline {sum(1 for r in base_a if r['decision']=='auto_hold')})")
    for key, count in Counter(
        (r.get("decision_reason") or "").split(":")[0] for r in held
    ).most_common(8):
        print(f"  {count:6}  {key}")

    print("\n-- manifest eligibility --")
    be = sum(1 for r in base_m if r["eligibility_decision"] == "eligible")
    ce = sum(1 for r in cand_m if r["eligibility_decision"] == "eligible")
    print(f"baseline : {be:6} of {len(base_m)} ({be/max(len(base_m),1):.1%})")
    print(f"candidate: {ce:6} of {len(cand_m)} ({ce/max(len(cand_m),1):.1%})   delta {ce-be:+}")

    print("\n-- document coverage --")
    bc, cc = doc_coverage(base_m), doc_coverage(cand_m)
    zero_b = [k for k, (e, _) in bc.items() if e == 0]
    zero_c = [k for k, (e, _) in cc.items() if e == 0]
    low_c = [k for k, (e, t) in cc.items() if t and e / t < 0.25]
    low_b = [k for k, (e, t) in bc.items() if t and e / t < 0.25]
    print(f"documents with zero eligible chunks: baseline {len(zero_b)} -> candidate {len(zero_c)}")
    print(f"documents below 25% coverage:        baseline {len(low_b)} -> candidate {len(low_c)}")
    fixed = sorted(set(zero_b) - set(zero_c))
    if fixed:
        print(f"  newly non-empty ({len(fixed)}): {[s for _, s in fixed[:5]]}")
    broken = sorted(set(zero_c) - set(zero_b))
    if broken:
        print(f"  REGRESSED to empty ({len(broken)}): {[s for _, s in broken[:5]]}")

    print("\n-- leakage checks --")
    nav_base = sum(1 for r in base_a if r["decision"] == "auto_exclude_navigation")
    nav_cand = sum(1 for r in cand_a if r["decision"] == "auto_exclude_navigation")
    status = "OK" if nav_cand == nav_base else "CHANGED"
    print(f"navigation pages excluded: {nav_base} -> {nav_cand}  [{status}]")
    leaked_nav = [
        r for r in cand_a
        if r.get("page_role") and r["decision"] != "auto_exclude_navigation"
    ]
    print(f"navigation pages NOT excluded: {len(leaked_nav)}  [{'OK' if not leaked_nav else 'LEAK'}]")
    table_recovered = [
        r for r in recovered if (r.get("page_map_table_candidate_count") or "0") not in ("", "0")
    ]
    print(
        f"table-candidate pages recovered: {len(table_recovered)}  "
        f"[{'OK' if not table_recovered else 'LEAK'}]"
    )

    print("\nBaseline files were not modified.")


if __name__ == "__main__":
    main()
