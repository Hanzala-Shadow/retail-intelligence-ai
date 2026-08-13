#!/usr/bin/env python3
"""Explicit live smoke runner; it is never imported by unit tests."""
from __future__ import annotations

import argparse
import json

from src.decomposed_query_api import run_query


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--request-id", default="manual-smoke")
    args = parser.parse_args()
    print(json.dumps(run_query(args.question, request_id=args.request_id), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
