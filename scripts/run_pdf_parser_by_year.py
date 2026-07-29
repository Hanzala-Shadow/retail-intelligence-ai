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
    parser.add_argument("--workers", type=int, default=1)
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
    failures: list[tuple[str, str, int]] = []
    for root_rel, ticker, pdf in matches:
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

        print(f"\n=== Parsing [{ticker}] {pdf.name} ===")
        completed = subprocess.run(command, cwd=repo)
        if completed.returncode:
            failures.append((ticker, pdf.name, completed.returncode))

    if failures:
        print("\nFailures:", file=sys.stderr)
        for ticker, name, code in failures:
            print(f"  [{ticker}] {name} (exit {code})", file=sys.stderr)
        return 1

    print(f"\nDone. Parsed {len(matches) - len(failures)}/{len(matches)} PDF(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
