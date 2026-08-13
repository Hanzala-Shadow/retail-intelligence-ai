#!/usr/bin/env python3
"""Watch a resumable chunker's machine-readable progress file."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def render(payload: dict[str, object]) -> str:
    done = int(payload.get("completed_sections", 0))
    total = int(payload.get("total_sections", 0))
    ratio = done / total if total else 1.0
    width = 40
    filled = min(width, int(ratio * width))
    bar = "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
    eta = payload.get("eta_seconds")
    eta_text = (
        time.strftime("%H:%M:%S", time.gmtime(float(eta)))
        if eta is not None
        else "unknown"
    )
    elapsed = time.strftime(
        "%H:%M:%S",
        time.gmtime(float(payload.get("elapsed_seconds", 0))),
    )
    return (
        f"{bar} {done}/{total} ({100.0 * ratio:.1f}%) "
        f"elapsed={elapsed} eta={eta_text} "
        f"chunks={payload.get('chunks', 0)}\n"
        f"status={payload.get('status', 'unknown')} "
        f"current={payload.get('current_section', '')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    progress = args.staging_root / "progress.json"

    while True:
        if progress.is_file():
            payload = json.loads(progress.read_text(encoding="utf-8"))
            print("\033[2J\033[H" + render(payload), flush=True)
            if payload.get("status") == "completed":
                break
        else:
            print(f"Waiting for {progress}", flush=True)
        if args.once:
            break
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    main()
