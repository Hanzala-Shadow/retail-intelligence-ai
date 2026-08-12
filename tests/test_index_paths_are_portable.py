r"""The reference indexes must not persist machine-specific absolute paths.

These CSVs are packaged and extracted on a server, so a path naming a local
user profile is a defect in the artifact rather than a cosmetic issue. The
corpus reached 4,590 such paths across two indexes before anyone noticed --
2,284 inherited from a former developer's checkout and 2,306 written by our
own bridge runs -- because nothing failed when they were wrong. Everything
downstream reads the files it just produced, on the machine that produced
them.

Guarding it here means the next occurrence surfaces in seconds instead of at
package-acceptance time on someone else's server.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = REPO_ROOT / "data" / "00_reference"

# Drive letter, UNC share, or a POSIX home directory.
ABSOLUTE_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/home/|/Users/)")

INDEXES = [
    "esg_parse_index.csv",
    "esg_parse_index_v2.csv",
    "esg_sections_index.csv",
    "esg_chunks_index.csv",
]

# embedding_text holds whole chunks and overflows the csv module's default.
csv.field_size_limit(2**31 - 1)


@pytest.mark.parametrize("index_name", INDEXES)
def test_index_has_no_absolute_paths(index_name: str) -> None:
    path = REFERENCE_DIR / index_name
    if not path.exists():
        pytest.skip(f"{index_name} not present")

    offenders: list[str] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            for column, value in row.items():
                if ABSOLUTE_RE.match(str(value or "").strip()):
                    offenders.append(f"{index_name}:{line_no} {column}={str(value)[:80]}")

    assert not offenders, (
        f"{len(offenders)} absolute path(s) persisted in {index_name}. "
        "Run esg/scripts/normalize_index_paths.py --apply, and fix whatever "
        "wrote them.\n" + "\n".join(offenders[:10])
    )
