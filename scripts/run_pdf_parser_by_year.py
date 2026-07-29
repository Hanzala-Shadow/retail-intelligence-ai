"""Run src/pdf_parser.py scoped to PDFs whose report year matches a target set.

pdf_parser.py has no --year flag; it only scopes a run by --root/--ticker/--pdf-file.
This driver scans the raw PDF roots, keeps only files whose report year (2023 or
2024 by default) matches, and invokes the parser once per matching
(ticker, pdf_file) pair so the normal --out/--index pipeline outputs are updated
in place.

The year of a multi-year stem is the canonical report year from src/esg_year.py:
max(year tokens), the latest year the document covers. So GES-...-2022-2023 is a
2023 document and matches; ACI-...-2022-2023 likewise. A stem whose latest year
falls outside the target set is skipped even if it mentions a target year
earlier in the range.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import esg_year  # noqa: E402

DEFAULT_ROOTS = [
    "data/01_raw/sustainability",
]


def find_matches(repo: Path, roots: list[str], target_years: set[int]) -> list[tuple[str, str, Path]]:
    matches = []
    for root_rel in roots:
        root = repo / root_rel
        if not root.is_dir():
            continue
        for ticker_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for pdf in sorted(ticker_dir.glob("*.pdf")):
                if esg_year.report_year(pdf.stem) in target_years:
                    matches.append((root_rel, ticker_dir.name, pdf))
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--years", default="2023,2024", help="Comma-separated years to include.")
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="Raw PDF root, relative to --repo (repeatable). Defaults to the Sustainability Reports root.",
    )
    parser.add_argument("--out", default="data/02_interim/esg_text")
    parser.add_argument("--index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--workers", type=int, default=1,
                        help="Passed through to each pdf_parser.py invocation (irrelevant per-call "
                             "since each call scopes exactly one PDF; use --parallel for concurrency).")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Number of pdf_parser.py subprocesses to run concurrently, one PDF each "
                             "(pdf_parser.py's own --workers only parallelizes across jobs in a single "
                             "invocation, and each invocation here has exactly one job).")
    parser.add_argument("--force", action="store_true", help="Reparse even if current outputs are complete.")
    parser.add_argument("--prefer-pdfium", action="store_true")
    parser.add_argument("--prefer-pymupdf", action="store_true")
    parser.add_argument("--log-pages", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="List matching PDFs without parsing them.")
    args = parser.parse_args()

    target_years = {int(y) for y in args.years.split(",") if y.strip()}
    if not target_years:
        parser.error("--years produced no valid years")

    repo = args.repo.resolve()
    roots = args.roots or DEFAULT_ROOTS
    matches = find_matches(repo, roots, target_years)

    if not matches:
        print(f"No PDFs matched years {sorted(target_years)} under {roots}.")
        return 0

    print(f"{len(matches)} PDF(s) matched years {sorted(target_years)}:")
    for root_rel, ticker, pdf in matches:
        print(f"  [{ticker}] {pdf.name}  ({root_rel})")

    if args.dry_run:
        return 0

    parser_path = repo / "src" / "pdf_parser.py"

    def build_command(root_rel: str, ticker: str, pdf: Path) -> list[str]:
        command = [
            sys.executable,
            str(parser_path),
            "--root",
            str(repo / root_rel),
            "--out",
            str(repo / args.out),
            "--index",
            str(repo / args.index),
            "--ticker",
            ticker,
            "--pdf-file",
            pdf.name,
            "--workers",
            str(args.workers),
        ]
        if args.force:
            command.append("--force")
        if args.prefer_pdfium:
            command.append("--prefer-pdfium")
        if args.prefer_pymupdf:
            command.append("--prefer-pymupdf")
        if args.log_pages:
            command.append("--log-pages")
        return command

    def run_one(job: tuple[str, str, Path]) -> tuple[str, str, int]:
        root_rel, ticker, pdf = job
        command = build_command(root_rel, ticker, pdf)
        completed = subprocess.run(command, cwd=repo)
        return ticker, pdf.name, completed.returncode

    failures: list[tuple[str, str, int]] = []
    parallel = max(1, args.parallel)

    # esg_parse_index.csv is a single shared CSV rewritten by each invocation
    # (upsert-on-write in src/pdf_parser.py); concurrent writers would race on
    # it, so each job writes to its own scoped, unique --index file, and the
    # shards are merged back into the real index once every job has finished.
    if parallel == 1:
        for root_rel, ticker, pdf in matches:
            print(f"\n=== Parsing [{ticker}] {pdf.name} ===")
            _, _, code = run_one((root_rel, ticker, pdf))
            if code:
                failures.append((ticker, pdf.name, code))
    else:
        print(f"\nRunning up to {parallel} parser subprocess(es) concurrently...")
        real_index = repo / args.index
        shard_dir = real_index.parent / "_year_run_shards"
        shard_dir.mkdir(parents=True, exist_ok=True)
        jobs = []
        for i, (root_rel, ticker, pdf) in enumerate(matches):
            jobs.append((root_rel, ticker, pdf, shard_dir / f"shard_{i:04d}.csv"))

        def run_sharded(job) -> tuple[str, str, int]:
            root_rel, ticker, pdf, shard_index = job
            command = build_command(root_rel, ticker, pdf)
            # swap the shared --index for this job's private shard
            idx = command.index("--index") + 1
            command[idx] = str(shard_index)
            completed = subprocess.run(command, cwd=repo)
            return ticker, pdf.name, completed.returncode

        completed_count = 0
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(run_sharded, job): job for job in jobs}
            for future in as_completed(futures):
                root_rel, ticker, pdf, _shard = futures[future]
                ticker_r, name_r, code = future.result()
                completed_count += 1
                status = "OK" if not code else f"FAILED (exit {code})"
                print(f"[{completed_count}/{len(jobs)}] [{ticker_r}] {name_r}: {status}")
                if code:
                    failures.append((ticker_r, name_r, code))

        # merge shards into the real index in original match order, then clean up
        merge_command = [
            sys.executable,
            str(repo / "scripts" / "merge_parse_index_shards.py"),
            "--index",
            str(real_index),
            "--shard-dir",
            str(shard_dir),
        ]
        completed = subprocess.run(merge_command, cwd=repo)
        if completed.returncode:
            print("\nShard merge failed; shards preserved at "
                  f"{shard_dir} for manual recovery.", file=sys.stderr)
            return 1

    if failures:
        print("\nFailures:", file=sys.stderr)
        for ticker, name, code in failures:
            print(f"  [{ticker}] {name} (exit {code})", file=sys.stderr)
        return 1

    print(f"\nDone. Parsed {len(matches) - len(failures)}/{len(matches)} PDF(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
