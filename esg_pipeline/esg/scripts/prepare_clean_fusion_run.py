"""Clear Docling-fusion outputs in data/ without deleting the Docling cache."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    REPO_ROOT / "data/02_interim/sustainability/03_pipeline_text",
    REPO_ROOT / "data/03_sections/sustainability",
    REPO_ROOT / "data/04_chunks/sustainability",
    REPO_ROOT / "data/00_reference/esg_sections_index.csv",
    REPO_ROOT / "data/00_reference/esg_chunks_index.csv",
    REPO_ROOT / "data/00_reference/esg_parse_index_v2.csv",
)


def remove_target(path: Path) -> None:
    if not path.exists():
        print(f"absent: {path.relative_to(REPO_ROOT)}")
    elif path.is_dir():
        shutil.rmtree(path)
        print(f"removed directory: {path.relative_to(REPO_ROOT)}")
    else:
        path.unlink()
        print(f"removed file: {path.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="confirm deletion of the listed fusion outputs"
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("refusing to delete outputs without --yes")
    for target in TARGETS:
        remove_target(target)


if __name__ == "__main__":
    main()
