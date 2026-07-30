"""Build the per-page lines-strategy table-area signal table.

For every page in the reconstructed-pages frame (layout QA decision
``auto_pass_column_order_reconstructed``), measures pdfplumber
``find_tables`` with vertical/horizontal strategy "lines" and records:

- ``lines_table_area_share``: sum of detected table bbox areas / page area;
- ``share_excl_frames_080/090/095``: the same share excluding decorative
  border frames — a detected "table" whose bbox covers at least the cutoff
  share of the page with <= 4 cells is a page border, not a data table
  (measured on the gold set: 28/36 high-share prose pages are exactly one
  near-page-sized box);
- ``largest_table_share`` / ``largest_table_cells`` and the excluded-frame
  count, plus an explicit per-page ``error`` column (no silent skips).

The canonical output is ``data/00_reference/esg_lines_area_signals.csv``
(one row per frame page). Reruns are resumable: already-written
(ticker, pdf_file, page) keys are skipped. For parallel runs use
``--shard I --nshards N``; each shard writes ``<out-stem>_shardI.csv``
beside the target and the shards are concatenated afterwards.

Read-only against the corpus; writes only the output CSV.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import time
from pathlib import Path

import pandas as pd
import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

QA_PATH = config.ESG_PAGE_LAYOUT_QA_CSV
DEFAULT_OUT = config.ESG_LINES_AREA_SIGNALS_CSV

LINES_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

HEADER = [
    "ticker", "pdf_file", "page",
    "lines_table_area_share", "lines_table_count",
    "share_excl_frames_090", "share_excl_frames_080",
    "share_excl_frames_095", "largest_table_share",
    "largest_table_cells", "n_frames_excluded_090", "error",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--limit-docs", type=int, default=0, help="smoke-test cap")
    ap.add_argument(
        "--expected-frame", type=int, default=9078,
        help="assert the frame has exactly this many pages (0 disables)",
    )
    args = ap.parse_args()

    qa = pd.read_csv(QA_PATH)
    frame = qa.loc[
        qa["decision"].eq("auto_pass_column_order_reconstructed"),
        ["ticker", "pdf_file", "page", "source_pdf"],
    ].copy()
    frame["page"] = frame["page"].astype(int)
    if args.expected_frame:
        assert len(frame) == args.expected_frame, (
            f"frame is {len(frame)}, expected {args.expected_frame}"
        )

    if args.nshards > 1:
        def shard_of(source_pdf: str) -> int:
            return int(hashlib.md5(source_pdf.encode()).hexdigest(), 16) % args.nshards

        frame = frame[frame["source_pdf"].map(shard_of) == args.shard]
        out_path = args.out.with_name(f"{args.out.stem}_shard{args.shard}.csv")
    else:
        out_path = args.out

    done: set[tuple[str, str, int]] = set()
    if out_path.exists():
        prev = pd.read_csv(out_path)
        done = set(zip(prev["ticker"], prev["pdf_file"], prev["page"].astype(int)))

    todo = frame[~frame.apply(
        lambda r: (r["ticker"], r["pdf_file"], int(r["page"])) in done, axis=1
    )]
    docs = todo.groupby("source_pdf", sort=True)
    n_docs = len(docs)
    print(f"shard {args.shard}/{args.nshards}: {len(frame)} pages total, "
          f"{len(done)} already done, {len(todo)} to do across {n_docs} docs",
          flush=True)

    mode = "a" if out_path.exists() else "w"
    t0 = time.time()
    with open(out_path, mode, newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if mode == "w":
            writer.writerow(HEADER)
        for d, (source_pdf, group) in enumerate(docs, 1):
            if args.limit_docs and d > args.limit_docs:
                break
            pdf_path = ROOT / source_pdf
            try:
                with pdfplumber.open(str(pdf_path)) as pdf:
                    for _, row in group.iterrows():
                        page_number = int(row["page"])
                        try:
                            page = pdf.pages[page_number - 1]
                            page_area = float(page.width * page.height)
                            tables = page.find_tables(table_settings=LINES_SETTINGS)
                            per_table = []
                            for t in tables:
                                a = (max(0.0, float(t.bbox[2]) - float(t.bbox[0]))
                                     * max(0.0, float(t.bbox[3]) - float(t.bbox[1])))
                                per_table.append((a / page_area if page_area else 0.0,
                                                  len(t.cells)))
                            raw = sum(s for s, _ in per_table)

                            def excl(cut: float) -> float:
                                return sum(s for s, c in per_table
                                           if not (s >= cut and c <= 4))

                            largest = max(per_table, key=lambda x: x[0],
                                          default=(0.0, 0))
                            nf = sum(1 for s, c in per_table if s >= 0.90 and c <= 4)
                            writer.writerow([row["ticker"], row["pdf_file"],
                                             page_number, raw, len(tables),
                                             excl(0.90), excl(0.80), excl(0.95),
                                             largest[0], largest[1], nf, ""])
                        except Exception as exc:  # per-page error, no silent skip
                            writer.writerow([row["ticker"], row["pdf_file"],
                                             page_number,
                                             "", "", "", "", "", "", "", "",
                                             repr(exc)])
            except Exception as exc:
                for _, row in group.iterrows():
                    writer.writerow([row["ticker"], row["pdf_file"],
                                     int(row["page"]),
                                     "", "", "", "", "", "", "", "",
                                     f"pdf_open:{exc!r}"])
            fh.flush()
            if d % 5 == 0 or d == n_docs:
                el = time.time() - t0
                print(f"shard {args.shard}: doc {d}/{n_docs} ({el:.0f}s elapsed)",
                      flush=True)
    print(f"shard {args.shard}: DONE in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
