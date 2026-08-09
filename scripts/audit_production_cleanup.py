#!/usr/bin/env python3
"""Read-only repository inventory for a later explicit cleanup manifest."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def command(repo: Path, *args: str) -> str:
    return subprocess.run(args, cwd=repo, text=True, capture_output=True,
                          check=True).stdout.strip()


def size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            candidate = Path(root, name)
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except FileNotFoundError:
                pass
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if command(repo, "git", "rev-parse", "--is-inside-work-tree") != "true":
        raise SystemExit("not a git worktree")
    top = []
    for item in sorted(repo.iterdir(), key=lambda value: value.name.casefold()):
        if item.name == ".git":
            continue
        top.append({"name": item.name, "kind": "directory" if item.is_dir() else "file",
                    "size_bytes": size(item)})
    report = {
        "schema_version": 1, "read_only": True, "repo": str(repo),
        "branch": command(repo, "git", "branch", "--show-current"),
        "head": command(repo, "git", "rev-parse", "HEAD"),
        "remote": command(repo, "git", "remote", "get-url", "origin"),
        "status_porcelain": command(repo, "git", "status", "--porcelain=v1"),
        "top_level": top,
        "tracked_files": int(command(repo, "git", "ls-files", "-z").count("\0")),
        "ignored_files_sample": "\n".join(
            command(repo, "git", "status", "--ignored", "--short").splitlines()[:500]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: read-only cleanup inventory written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
