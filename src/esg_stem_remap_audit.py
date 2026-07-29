"""Read-only audit of pdf_stem drift between the parse index and the raw PDFs.

The parse index records, per document, the source PDF filename *and* its
SHA-256. PDFs were renamed after parsing, so filename is no longer a reliable
key -- but the hash is. This script rebuilds the authoritative mapping from
content hash to current filename and reports where the index disagrees.

Three drift categories are distinguished because they need different fixes:

  year_shift    the recorded year differs from the current file's year. The
                index year is wrong; report_year inherits the error.
  ticker_change the ticker or company name changed; the year is unaffected.
  span_change   a single year became a range, or a range was reordered.
  name_only     cosmetic difference with identical ticker and years.

It also reports COLLISIONS: stale stems that are byte-identical in name to a
different document now on disk. Re-running the parser before resolving these
would produce two different documents under one stem.

Writes reports/esg_stem_remap_audit.csv. Modifies nothing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from collections import Counter

import config

PARSE_INDEX = str(config.ESG_PARSE_INDEX_CSV)
RAW_ROOT = str(config.RAW_SUSTAINABILITY_DIR)
OUT = str(config.ESG_STEM_REMAP_AUDIT_CSV)

_YEARS = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_TICKER = re.compile(r"^([A-Z][A-Z0-9.\-]*?)-")


def years_of(stem: str):
    return sorted({int(y) for y in _YEARS.findall(stem) if 1990 <= int(y) <= 2030})


def ticker_of(stem: str):
    m = _TICKER.match(stem)
    return m.group(1) if m else ""


def classify(old_stem: str, new_stem: str) -> str:
    old_y, new_y = years_of(old_stem), years_of(new_stem)
    old_t, new_t = ticker_of(old_stem), ticker_of(new_stem)
    if old_t != new_t:
        return "ticker_change"
    if old_y != new_y:
        # A shift in the latest covered year is the dangerous case: it is what
        # report_year resolves to, so the retrieval filter inherits the error.
        if old_y and new_y and max(old_y) != max(new_y):
            return "year_shift"
        return "span_change"
    return "name_only"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parse-index", default=PARSE_INDEX)
    ap.add_argument("--raw-root", default=RAW_ROOT)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    with open(args.parse_index, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    # content hash -> current filename(s) on disk
    by_hash = {}
    n_pdf = 0
    for root, _dirs, files in os.walk(args.raw_root):
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            n_pdf += 1
            path = os.path.join(root, name)
            by_hash.setdefault(sha256_file(path), []).append(path)

    disk_stems = {os.path.splitext(os.path.basename(p))[0]
                  for paths in by_hash.values() for p in paths}

    out_rows = []
    counts = Counter()
    for row in rows:
        recorded = row.get("pdf_file", "")
        old_stem = os.path.splitext(recorded)[0]
        sha = row.get("source_sha256", "")
        matches = by_hash.get(sha, [])

        if not matches:
            counts["source_pdf_missing"] += 1
            out_rows.append({
                "category": "source_pdf_missing", "ticker": row.get("ticker", ""),
                "old_stem": old_stem, "new_stem": "", "old_year": "",
                "new_year": "", "collides": "", "sha256": sha,
            })
            continue

        new_stem = os.path.splitext(os.path.basename(matches[0]))[0]
        if new_stem == old_stem:
            counts["ok"] += 1
            continue

        category = classify(old_stem, new_stem)
        counts[category] += 1
        # Collision: the stale stem names a real, different document on disk.
        collides = old_stem in disk_stems
        if collides:
            counts["COLLISION"] += 1
        old_y, new_y = years_of(old_stem), years_of(new_stem)
        out_rows.append({
            "category": category, "ticker": row.get("ticker", ""),
            "old_stem": old_stem, "new_stem": new_stem,
            "old_year": max(old_y) if old_y else "",
            "new_year": max(new_y) if new_y else "",
            "collides": "YES" if collides else "",
            "sha256": sha,
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fields = ["category", "ticker", "old_stem", "new_stem", "old_year",
              "new_year", "collides", "sha256"]
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(out_rows, key=lambda r: (r["category"], r["old_stem"])))

    print(f"parse-index rows: {len(rows)}   PDFs hashed: {n_pdf}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    shifted = [r for r in out_rows if r["category"] == "year_shift"]
    if shifted:
        tickers = sorted({r["ticker"] for r in shifted})
        print(f"\nyear_shift affects {len(shifted)} documents "
              f"across {len(tickers)} tickers: {', '.join(tickers)}")
    if counts["COLLISION"]:
        print(f"\n*** {counts['COLLISION']} stale stems name a DIFFERENT document "
              f"now on disk. Re-running the parser before fixing these would "
              f"merge two documents under one stem. ***")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
