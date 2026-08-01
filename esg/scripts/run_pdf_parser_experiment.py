"""Run a small, isolated PDF parser experiment.

Only a copy of src/ is executed. Source PDFs are copied into the run folder,
and all parser outputs are written below outputs/pdf_parser_experiments/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402


TARGETS = {
    "PVH": ["PVH-PVH CORP-2016.pdf"],
    "GES": ["GES-GUESS INC-2018&2019.pdf"],
    "WMT": ["WMT-WALMART INC-2015.pdf"],
    "SGI": ["SGI-SOMNIGROUP INTERNATIONAL INC(TEMPUR SEALY)-2020.pdf"],
    "VZ": ["VZ-VERIZON COMMUNICATIONS INC-2016.pdf"],
    "TGT": ["TGT-TARGET CORP-2015.pdf"],
    "ULTA": ["ULTA-ULTA BEAUTY INC-2020.pdf"],
    "SONO": ["SONO-SONOS INC-2019.pdf"],
    "HD": ["HD-HOME DEPOT INC-2018.pdf"],
    "PTRN": [
        "PTRN-PATTERN GROUP INC-2015.pdf",
        "PTRN-PATTERN GROUP INC-2016.pdf",
    ],
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def selected_targets(tickers: list[str] | None) -> list[tuple[str, str]]:
    names = [name.upper() for name in tickers] if tickers else list(TARGETS)
    unknown = sorted(set(names) - TARGETS.keys())
    if unknown:
        raise SystemExit(f"Unknown ticker(s): {', '.join(unknown)}")
    return [(ticker, filename) for ticker in names for filename in TARGETS[ticker]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--input-root", type=Path, default=None)
    parser.add_argument("--run-name", default=None, help="Optional stable name for the run folder.")
    parser.add_argument("--parser", default="pdf_parser.py", help="Parser filename inside src/.")
    parser.add_argument("--ticker", action="append", dest="tickers", help="Limit to one or more tickers.")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--prefer-pdfium", action="store_true")
    parser.add_argument("--prefer-pymupdf", action="store_true")
    parser.add_argument("--auto-layout-pdfium", action="store_true")
    parser.add_argument("--auto-repair-layout", action="store_true")
    parser.add_argument("--no-auto-layout-pdfium", action="store_true")
    parser.add_argument("--auto-layout-dpi", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-pages", action="store_true")
    parser.add_argument("--keep-run-on-error", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    # Joined against --repo, which may point at another checkout, so the
    # config path is reduced to its repo-relative form first.
    input_root = (
        args.input_root
        or repo / config.as_repo_relative(config.RAW_DIR) / "esg_archive_pilot"
    ).resolve()
    source_src = repo / "src"
    parser_file = source_src / args.parser
    if not parser_file.is_file():
        raise SystemExit(f"Missing parser: {parser_file}")
    parser_source = parser_file.read_text(encoding="utf-8")
    if args.auto_repair_layout and "--auto-repair-layout" not in parser_source:
        raise SystemExit(
            f"{args.parser} does not support --auto-repair-layout. "
            "Use the parser's supported layout options or restore the previous parser."
        )
    if args.auto_layout_dpi is not None and "--auto-layout-dpi" not in parser_source:
        raise SystemExit(
            f"{args.parser} does not support --auto-layout-dpi. "
            "Remove that option or use a parser version that defines it."
        )
    if not input_root.is_dir():
        raise SystemExit(f"Missing input root: {input_root}")

    targets = selected_targets(args.tickers)
    missing = [f"{ticker}/{filename}" for ticker, filename in targets if not (input_root / ticker / filename).is_file()]
    if missing:
        raise SystemExit("Missing target PDF(s):\n" + "\n".join(missing))

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_name = args.run_name or f"parser_{stamp}"
    run_root = repo / "outputs" / "pdf_parser_experiments" / run_name
    if run_root.exists():
        raise SystemExit(f"Run folder already exists: {run_root}. Choose another --run-name.")
    run_root.mkdir(parents=True)

    try:
        run_src = run_root / "src_snapshot"
        run_input = run_root / "input_pdfs"
        run_output = run_root / "parser_output"
        copy_tree(source_src, run_src)
        run_input.mkdir()

        source_manifest = []
        for path in sorted(source_src.rglob("*.py")):
            source_manifest.append({"path": path.relative_to(source_src).as_posix(), "sha256": sha256(path)})
        write_json(run_root / "src_manifest.json", source_manifest)

        pdf_manifest = []
        for ticker, filename in targets:
            source = input_root / ticker / filename
            target_dir = run_input / ticker
            target_dir.mkdir(exist_ok=True)
            target = target_dir / filename
            shutil.copy2(source, target)
            target.chmod(0o444)
            pdf_manifest.append(
                {
                    "ticker": ticker,
                    "filename": filename,
                    "source": str(source),
                    "run_copy": str(target),
                    "sha256": sha256(source),
                    "size_bytes": source.stat().st_size,
                }
            )
        write_json(run_root / "pdf_manifest.json", pdf_manifest)

        overrides = repo / config.as_repo_relative(config.ESG_PARSER_OVERRIDES_CSV)
        run_overrides = run_root / "esg_parser_overrides.csv"
        if overrides.is_file():
            shutil.copy2(overrides, run_overrides)
        else:
            run_overrides.write_text("ticker,pdf_file,parser_mode,reason,active\n", encoding="utf-8")

        command = [
            sys.executable,
            str(run_src / args.parser),
            "--root",
            str(run_input),
            "--out",
            str(run_output / "esg_text"),
            "--index",
            str(run_output / "esg_parse_index.csv"),
            "--parser-overrides",
            str(run_overrides),
            "--workers",
            str(args.workers),
            "--force",
        ]
        if args.prefer_pdfium:
            command.append("--prefer-pdfium")
        if args.prefer_pymupdf:
            command.append("--prefer-pymupdf")
        if args.auto_layout_pdfium:
            command.append("--auto-layout-pdfium")
        if args.auto_repair_layout:
            command.append("--auto-repair-layout")
        if args.no_auto_layout_pdfium:
            command.append("--no-auto-layout-pdfium")
        if args.auto_layout_dpi is not None:
            command.extend(["--auto-layout-dpi", str(args.auto_layout_dpi)])
        if args.log_pages:
            command.append("--log-pages")

        metadata = {
            "created_at_utc": datetime.now(UTC).isoformat(),
            "repo": str(repo),
            "input_root": str(input_root),
            "targets": targets,
            "command": command,
            "rule": "Only src_snapshot is executed; PDFs and parser outputs are copies inside this run folder.",
        }
        write_json(run_root / "run_metadata.json", metadata)
        completed = subprocess.run(command, cwd=run_root, text=True)
        if completed.returncode:
            print(f"Parser failed. Run artifacts are in: {run_root}", file=sys.stderr)
            return completed.returncode

        changed_inputs = [
            item for item in pdf_manifest if sha256(Path(item["run_copy"])) != item["sha256"]
        ]
        if changed_inputs:
            raise RuntimeError("A copied input PDF changed during the run: " + repr(changed_inputs))

        print(f"Experiment complete: {run_root}")
        print(f"Parser index: {run_output / 'esg_parse_index.csv'}")
        return 0
    except Exception:
        if not args.keep_run_on_error and run_root.exists():
            shutil.rmtree(run_root)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
