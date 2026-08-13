#!/usr/bin/env python3
"""Run the loader's database-only acceptance gates."""
import sys

from load_fy2325_v2_staging import connect, validate


def main():
    conn = connect()
    try:
        validate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise
