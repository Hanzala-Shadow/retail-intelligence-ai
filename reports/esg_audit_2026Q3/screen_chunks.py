"""Text-quality screening across every chunk .txt on disk.

Mirrors the pilot audit's screening signals, run over the full 57,860-chunk
corpus rather than a 3,077-chunk pilot. Signals overlap and are diagnostic,
not failure counts.
"""
import pandas as pd
import numpy as np
import re
import os
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The ESG pipeline moved under esg/; these reports live outside both
# pipelines, so they name the two entries a pipeline _bootstrap would add.
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[1] / "esg"))

import config  # noqa: E402

R = config.REFERENCE_DIR.as_posix() + "/"
OUT = HERE.as_posix() + "/"   # this script writes beside itself

ch = pd.read_csv(R + "esg_chunks_index_enriched.csv", low_memory=False,
                 usecols=["chunk_id", "canonical_ticker", "pdf_stem", "section_code",
                          "chunk_file", "token_count", "chunk_quality_tier"])
vm = pd.read_csv(R + "vector_index_manifest.csv", low_memory=False,
                 usecols=["chunk_id", "eligibility_decision"])
ch = ch.merge(vm, on="chunk_id", how="left")

RE_REPEAT = re.compile(r"([.\-_=•·*])\1{4,}")     # dot leaders / rules
RE_TOKEN = re.compile(r"\S+")
RE_NUMISH = re.compile(r"^[\d\s.,%$()\-+/]+$")
RE_TOCLINE = re.compile(r"\.{3,}\s*\d+\s*$|\s{2,}\d{1,3}\s*$")

rows = []
missing = 0
for cid, path, tok in zip(ch.chunk_id, ch.chunk_file, ch.token_count):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            t = f.read()
    except OSError:
        missing += 1
        rows.append((cid, 0, 0, 0, 0, 0, 0, 0))
        continue

    lines = [l for l in t.splitlines() if l.strip()]
    words = RE_TOKEN.findall(t)
    nw = len(words)

    short_lines = sum(1 for l in lines if len(l.strip()) < 35)
    frag = int(len(lines) >= 6 and short_lines / max(len(lines), 1) >= 0.6)

    repeated = int(bool(RE_REPEAT.search(t)))

    toc_lines = sum(1 for l in lines if RE_TOCLINE.search(l))
    tocish = int(len(lines) >= 5 and toc_lines / max(len(lines), 1) >= 0.35)

    repl = int("�" in t)

    numish = sum(1 for w in words if RE_NUMISH.match(w))
    tableish = int(nw >= 20 and numish / max(nw, 1) >= 0.35)

    tooshort = int(nw < 40)

    n_sig = frag + repeated + tocish + repl + tableish + tooshort
    rows.append((cid, frag, repeated, tocish, repl, tableish, tooshort, n_sig))

s = pd.DataFrame(rows, columns=["chunk_id", "fragmented", "repeated_chars",
                                "toc_like", "replacement_char", "table_heavy",
                                "under_40_words", "n_signals"])
d = ch.merge(s, on="chunk_id")
d.to_csv(OUT + "chunk_screening.csv", index=False)

N = len(d)
print(f"chunks screened: {N}   unreadable files: {missing}\n")
sig = ["fragmented", "repeated_chars", "toc_like", "replacement_char",
       "table_heavy", "under_40_words"]
print("=== SIGNAL COUNTS (whole corpus) ===")
for c in sig:
    print(f"{c:20s} {int(d[c].sum()):7,}  {d[c].mean()*100:6.2f}%")
print(f"{'>=2 signals':20s} {int((d.n_signals >= 2).sum()):7,}  {(d.n_signals>=2).mean()*100:6.2f}%")
print(f"{'0 signals (clean)':20s} {int((d.n_signals == 0).sum()):7,}  {(d.n_signals==0).mean()*100:6.2f}%")

el = d[d.eligibility_decision == "eligible"]
print(f"\n=== SIGNAL COUNTS (eligible only, n={len(el):,}) ===")
for c in sig:
    print(f"{c:20s} {int(el[c].sum()):7,}  {el[c].mean()*100:6.2f}%")
print(f"{'>=2 signals':20s} {int((el.n_signals >= 2).sum()):7,}  {(el.n_signals>=2).mean()*100:6.2f}%")
print(f"{'0 signals (clean)':20s} {int((el.n_signals == 0).sum()):7,}  {(el.n_signals==0).mean()*100:6.2f}%")

print("\n=== SIGNALS BY QUALITY TIER (whole corpus) ===")
print(d.groupby("chunk_quality_tier")[sig + ["n_signals"]].mean().mul(100).round(2).to_string())

print("\n=== TOP DOCS IN THE >=2 SIGNAL SET ===")
pri = d[d.n_signals >= 2]
print(pri.pdf_stem.value_counts().head(12).to_string())
print("\n=== >=2 SIGNAL SET BY SECTION ===")
print(pri.section_code.value_counts().head(8).to_string())
print("\n=== >=2 SIGNAL SET: eligible vs held ===")
print(pri.eligibility_decision.value_counts().to_string())

json.dump({"screened": int(N), "signals": {c: int(d[c].sum()) for c in sig},
           "multi_signal": int((d.n_signals >= 2).sum()),
           "clean": int((d.n_signals == 0).sum()),
           "eligible_clean": int((el.n_signals == 0).sum()),
           "eligible_n": int(len(el))},
          open(OUT + "screening_summary.json", "w"), indent=2)
print("\nwrote", OUT + "chunk_screening.csv")
