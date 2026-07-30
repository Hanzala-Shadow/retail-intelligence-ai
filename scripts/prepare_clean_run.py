"""Reset the ESG pipeline's derived state so a full reparse starts clean.

Why this exists
---------------
The pipeline UPSERTS. ``upsert_index_rows`` (src/pdf_parser.py:2820) merges into
whatever the index already holds unless ``replace_all=True``, and no CLI exposes
that flag; the ESG sectioner defaults it to False too. So a document that has
been renamed or removed upstream leaves a ghost row behind forever -- a reparse
rewrites the rows it recognises and silently keeps the rest.

That is how the tree reached a state with 9 live stems pointing at PDFs that no
longer exist. Re-running the pipeline would not have fixed it.

Safety model
------------
Three explicit sets, and anything unrecognised is PRESERVED, never guessed at:

  DERIVED   regenerated wholly by the pipeline -> safe to clear
  CURATED   hand-authored, not derivable -> must exist, never cleared
  unknown   anything else -> preserved and reported, so a new file added later
            cannot be silently destroyed by a future run

The curated set is checked BEFORE anything is deleted. If a curated file is
missing the run aborts, on the assumption that the tree is already damaged and
clearing more would make it worse.

Nothing is deleted without ``--execute``; the default is a dry run.
Everything cleared is copied to a timestamped folder under ``backups/`` first,
and ``RUN_STATE.json`` records the git commit, the file list and the row counts
that were discarded.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data/00_reference"

# --------------------------------------------------------------------------
# Derived: written wholly by a pipeline stage. Clearing these forces the stage
# to rebuild from source rather than merge into stale rows.
# --------------------------------------------------------------------------
DERIVED = {
    "esg_file_catalog.csv": "intake catalog (upserts and retains inactive history; rebuilt from the raw tree)",
    "esg_parse_index.csv": "parser (upserts -- ghost rows survive without this)",
    "esg_sections_index.csv": "sectioner (upserts)",
    "esg_chunks_index.csv": "chunker (upserts)",
    "esg_page_layout_qa.csv": "layout QA, keyed per page",
    "vector_index_manifest.csv": "rebuilt wholesale, cleared for consistency",
    "esg_pipeline_qa.csv": "QA report",
    "chunk_qa_company_summary.csv": "chunk validator summary",
    "esg_chunks_index_enriched.csv": "P1 enrichment output",
    "esg_chunk_embedding_context.csv": "embedding-context index",
}

# --------------------------------------------------------------------------
# Curated: hand-authored or independently produced. Not derivable from any
# pipeline stage. src/esg_p1_enrichment.py:251 refuses to run without the
# source registry and says so in as many words.
# --------------------------------------------------------------------------
CURATED = {
    "esg_source_registry.csv": "curated duplicates/exclusions/supplements",
    "esg_parser_overrides.csv": "per-document parser overrides",
    "esg_ocr_approval.csv": "OCR approval decisions (reviewer/approval_status); intake only creates the header",
    "esg_accepted_company_manifest.csv": "accepted company manifest",
    "companies.csv": "company master",
    "sustainability_report_tracker.csv": "coverage tracker",
    "esg_manual_headings_clean.csv": "manual heading curation",
    "esg_manual_headings_clean.txt": "manual heading curation",
    "esg_layout_gold_labels.csv": "449-page adjudicated gold set",
    "esg_layout_gold_annotator1.csv": "gold set, annotator 1 pass",
    "esg_layout_gold_annotator2.csv": "gold set, annotator 2 pass",
    "esg_layout_gold_disagreements.csv": "gold set adjudication record",
    "esg_lines_area_signals.csv": "9,078-page table-signal table",
    "esg_drive_manifest.csv": "Drive sync record -- owned by the sync, not the reparse",
    "esg_sample_docs.csv": "corpus scope for the sample scripts",
    "esg_ocr_normalization_manifest.csv": "manual OCR remediation record",
}

# --------------------------------------------------------------------------
# Preserved, but not this script's business: the 10-K side of the repo,
# historical audit outputs, and source workbooks. Never cleared by an ESG
# reset, and not required to exist either -- unlike CURATED, their absence is
# not evidence that the tree is damaged.
# --------------------------------------------------------------------------
PRESERVED_OTHER = {
    ".gitkeep": "directory marker",
    "apparel_footwear_v3.xlsx": "source workbook for the company list",
    "apparel_footwear_v3.zip": "untracked download artifact, referenced nowhere in code",
    "chunks_index.csv": "10-K corpus chunk index -- different pipeline",
    "sections_index.csv": "10-K corpus section index -- different pipeline",
    "document_scan.csv": "document scanner output (10-K side)",
    "download_status_report.csv": "download monitor output",
    "filing_state.csv": "10-K filing state",
    "filings.csv": "10-K filings",
    "esg_drive_folders.csv": "Drive folder map, owned by the sync",
    "esg_heading_audit.csv": "historical audit output",
    "esg_not_found_reason_codes.csv": "curated coverage explanations",
    "esg_sectioning_quality_anomalies.csv": "historical audit output",
    "esg_sectioning_quality_by_company.csv": "historical audit output",
    "esg_sectioning_quality_by_document.csv": "historical audit output",
    "esg_supplemental_source_candidates.csv": "curated supplementary candidates",
    "rag_eval_questions_seed.csv": "evaluation asset",
    "sustainability_report_tracker_cleaned.csv": "tracker derivative",
    "sustainability_report_tracker_cleanup_summary.csv": "tracker derivative",
}

# Bulk output trees, cleared only with --outputs.
OUTPUT_TREES = {
    "data/02_interim/esg_text": "parsed text",
    "data/03_sections/esg": "section text",
    "data/04_chunks/esg": "chunk text (citation evidence -- rebuilt by the reparse)",
    "data/05_embedding": "embedding text copies",
}


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def row_count(path: Path) -> int | None:
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="actually clear. Without this it is a dry run.")
    ap.add_argument("--outputs", action="store_true",
                    help="also clear parsed text, sections, chunks and embeddings")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the backup copy (not recommended)")
    args = ap.parse_args(argv)

    if not REF.is_dir():
        raise SystemExit(f"not a pipeline tree: {REF} does not exist")

    # ---- guard: curated files must all be present BEFORE we clear anything --
    missing = [n for n in CURATED if not (REF / n).exists()]
    if missing:
        print("ABORTING -- curated files are missing, so this tree is already "
              "damaged and clearing more would compound it:", file=sys.stderr)
        for n in missing:
            print(f"  {n}  ({CURATED[n]})", file=sys.stderr)
        print("\nRestore them (git checkout, or backups/) and re-run.", file=sys.stderr)
        return 2

    present = {p.name for p in REF.iterdir() if p.is_file()}
    unknown = sorted(present - set(DERIVED) - set(CURATED) - set(PRESERVED_OTHER))

    to_clear = [(n, REF / n) for n in sorted(DERIVED) if (REF / n).exists()]

    print(f"git commit : {git_commit()}")
    print(f"mode       : {'EXECUTE' if args.execute else 'DRY RUN (nothing will be deleted)'}")
    print()
    print(f"WILL CLEAR ({len(to_clear)} derived files)")
    total_rows = 0
    for name, path in to_clear:
        rc = row_count(path)
        total_rows += rc or 0
        print(f"  {name:<42} {rc if rc is not None else '?':>7} rows   {DERIVED[name]}")

    trees = []
    if args.outputs:
        print()
        print("WILL CLEAR (output trees)")
        for rel, why in OUTPUT_TREES.items():
            d = ROOT / rel
            if d.is_dir():
                n = sum(1 for _ in d.rglob("*") if _.is_file())
                trees.append((rel, d, n))
                print(f"  {rel:<42} {n:>7} files  {why}")

    print()
    print(f"PRESERVED ({len(CURATED)} curated + {len(PRESERVED_OTHER)} out-of-scope files)"
          f" -- never cleared by this script")
    if unknown:
        print()
        print(f"PRESERVED ({len(unknown)} unrecognised files) -- not in either list, "
              f"so left alone by design:")
        for n in unknown:
            print(f"  {n}")
        print("  If any of these are pipeline output, add them to DERIVED. "
              "Until then they survive a clean run.")

    if not args.execute:
        print("\nDry run. Re-run with --execute to apply.")
        return 0

    # ---- backup ---------------------------------------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "backups" / f"clean_run_{stamp}"
    if not args.no_backup:
        (backup / "data/00_reference").mkdir(parents=True, exist_ok=True)
        for name, path in to_clear:
            shutil.copy2(path, backup / "data/00_reference" / name)
        print(f"\nbacked up {len(to_clear)} files to {backup}")

    state = {
        "cleared_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "backup": str(backup.relative_to(ROOT)) if not args.no_backup else None,
        "cleared_indices": [{"file": n, "rows": row_count(p)} for n, p in to_clear],
        "cleared_rows_total": total_rows,
        "cleared_trees": [{"path": rel, "files": n} for rel, _, n in trees],
        "preserved_curated": sorted(CURATED),
        "preserved_other": sorted(PRESERVED_OTHER),
        "preserved_unrecognised": unknown,
    }

    for name, path in to_clear:
        path.unlink()
    for _rel, d, _n in trees:
        shutil.rmtree(d)

    if not args.no_backup:
        (backup / "RUN_STATE.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")
    (REF.parent / "CLEAN_RUN_STATE.json").write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"cleared {len(to_clear)} indices ({total_rows:,} rows) "
          f"and {len(trees)} output trees")
    print(f"state written to data/CLEAN_RUN_STATE.json")
    print("\nThe tree is now clean. Run the pipeline from the parse stage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
