#!/usr/bin/env python3
"""Write a narrow provenance manifest for an isolated ESG candidate build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esg" / "src"))

import esg_chunker  # noqa: E402


PACKAGES = (
    "tiktoken",
    "transformers",
    "tokenizers",
    "sqlalchemy",
    "numpy",
    "scipy",
    "pytest",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_named_paths(values: list[str]) -> dict[str, Path]:
    parsed = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"expected NAME=PATH, got: {value}")
        name, raw_path = value.split("=", 1)
        if not name.strip() or not raw_path.strip():
            raise ValueError(f"expected NAME=PATH, got: {value}")
        if name in parsed:
            raise ValueError(f"duplicate path label: {name}")
        parsed[name] = Path(raw_path).resolve()
    return parsed


def git_value(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def package_versions() -> dict[str, str | None]:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--tokenizer-identifier", required=True)
    parser.add_argument("--tokenizer-commit", required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--checkpoint-interval", type=int, required=True)
    parser.add_argument("--build-started-at", required=True)
    parser.add_argument("--command-file", type=Path, required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--output-path", action="append", default=[])
    args = parser.parse_args()

    inputs = parse_named_paths(args.input)
    outputs = parse_named_paths(args.output_path)
    missing_inputs = [str(path) for path in inputs.values() if not path.is_file()]
    if missing_inputs:
        raise FileNotFoundError(f"provenance input file(s) missing: {missing_inputs}")
    tokenizer_path = args.tokenizer_path.resolve()
    tokenizer_files = {}
    for name in (
        "tokenizer.json",
        "vocab.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        path = tokenizer_path / name
        if not path.is_file():
            raise FileNotFoundError(f"tokenizer file missing: {path}")
        tokenizer_files[name] = sha256_file(path)

    status_text = git_value("status", "--porcelain") or ""
    commands = [
        line
        for line in args.command_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finished_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    manifest = {
        "manifest_version": 1,
        "build_status": "complete",
        "build_started_at": args.build_started_at,
        "build_finished_at": finished_at,
        "repository": {
            "root": str(ROOT),
            "git_commit": git_value("rev-parse", "HEAD"),
            "dirty_worktree": bool(status_text),
            "git_status_entry_count": len(status_text.splitlines()),
        },
        "python": {
            "executable": str(args.python_executable.resolve()),
            "version": sys.version.splitlines()[0],
        },
        "packages": package_versions(),
        "chunker": {
            "version": esg_chunker.CHUNKER_VERSION,
            "citation_validation_version": esg_chunker.CITATION_VALIDATION_VERSION,
            "source_path": str(Path(esg_chunker.__file__).resolve()),
            "source_sha256": sha256_file(Path(esg_chunker.__file__).resolve()),
            "bge_input_limit": esg_chunker.BGE_INPUT_LIMIT,
            "cl100k_encoding": esg_chunker.ENCODING,
        },
        "tokenizer": {
            "identifier": args.tokenizer_identifier,
            "resolved_commit": args.tokenizer_commit,
            "path": str(tokenizer_path),
            "files_sha256": tokenizer_files,
        },
        "options": {
            "workers": args.workers,
            "checkpoint_interval": args.checkpoint_interval,
        },
        "inputs": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in inputs.items()
        },
        "outputs": {name: str(path) for name, path in outputs.items()},
        "candidate_root": str(args.candidate_root.resolve()),
        "commands": commands,
        "safety": {
            "live_corpus_written": False,
            "embeddings_built": False,
            "vector_index_written": False,
            "candidate_promoted": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote build provenance to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
