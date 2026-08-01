"""Create the local SQLite snapshot used by the ESG database QA tiers.

The normal ESG loader uses the database URL from ``.env``. This wrapper uses
the same load plan and ORM models, but points them at a new local SQLite file.
It never overwrites or updates an existing database.

Usage:
    python esg/scripts/build_esg_qa_db.py
    python esg/scripts/build_esg_qa_db.py --output data/esg.db
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
import drive_to_db  # noqa: E402
from common.models import Base  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402


def sqlite_url(path: Path) -> str:
    """Return a SQLAlchemy URL for an absolute local SQLite path."""
    return f"sqlite:///{path.resolve().as_posix()}"


def require_missing(path: Path) -> None:
    """Refuse any write when the requested database already exists."""
    if path.exists():
        raise FileExistsError(
            f"database already exists; refusing to overwrite or update it: {path}"
        )


def load_plan(
    *,
    sections_index: Path = config.ESG_SECTIONS_INDEX_CSV,
    chunks_index: Path = config.ESG_CHUNKS_INDEX_CSV,
    companies: Path = config.COMPANIES_CSV,
    tracker: Path = config.SUSTAINABILITY_TRACKER_CSV,
    parse_index: Path = config.ESG_PARSE_INDEX_CSV,
    raw_root: Path = config.RAW_SUSTAINABILITY_DIR,
    file_catalog: Path = config.ESG_FILE_CATALOG_CSV,
    ocr_approvals: Path = config.ESG_OCR_APPROVAL_CSV,
) -> drive_to_db.LoadPlan:
    """Build the same artifact load plan used by drive_to_db.py."""
    args = SimpleNamespace(
        companies=str(companies),
        tracker=str(tracker),
        parse_index=str(parse_index),
        sections_index=str(sections_index),
        chunks_index=str(chunks_index),
        raw_root=str(raw_root),
        file_catalog=str(file_catalog),
        ocr_approvals=str(ocr_approvals),
    )
    return drive_to_db.build_plan(args)


def remove_partial_database(path: Path) -> None:
    """Remove only files created for a failed new SQLite build."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.unlink()


def build_database(
    path: Path,
    *,
    sections_index: Path = config.ESG_SECTIONS_INDEX_CSV,
    chunks_index: Path = config.ESG_CHUNKS_INDEX_CSV,
    companies: Path = config.COMPANIES_CSV,
    tracker: Path = config.SUSTAINABILITY_TRACKER_CSV,
    parse_index: Path = config.ESG_PARSE_INDEX_CSV,
    raw_root: Path = config.RAW_SUSTAINABILITY_DIR,
    file_catalog: Path = config.ESG_FILE_CATALOG_CSV,
    ocr_approvals: Path = config.ESG_OCR_APPROVAL_CSV,
) -> dict[str, int]:
    """Create and populate a new QA database, leaving existing files alone."""
    path = path.resolve()
    require_missing(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    url = sqlite_url(path)
    previous_db_url = os.environ.get("DB_URL")
    engine = None
    try:
        engine = create_engine(url, future=True)
        Base.metadata.create_all(engine)
        engine.dispose()
        engine = None

        # python-dotenv does not replace a process environment variable by
        # default, so this command-only value wins without editing .env.
        os.environ["DB_URL"] = url
        plan = load_plan(
            sections_index=sections_index,
            chunks_index=chunks_index,
            companies=companies,
            tracker=tracker,
            parse_index=parse_index,
            raw_root=raw_root,
            file_catalog=file_catalog,
            ocr_approvals=ocr_approvals,
        )
        drive_to_db.print_plan(plan)
        return drive_to_db.apply_plan(plan)
    except BaseException:
        if engine is not None:
            engine.dispose()
        remove_partial_database(path)
        raise
    finally:
        if previous_db_url is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = previous_db_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=config.ESG_DB)
    parser.add_argument(
        "--sections-index",
        type=Path,
        default=config.ESG_SECTIONS_INDEX_CSV,
    )
    parser.add_argument(
        "--chunks-index",
        type=Path,
        default=config.ESG_CHUNKS_INDEX_CSV,
    )
    parser.add_argument("--companies", type=Path, default=config.COMPANIES_CSV)
    parser.add_argument(
        "--tracker", type=Path, default=config.SUSTAINABILITY_TRACKER_CSV
    )
    parser.add_argument(
        "--parse-index", type=Path, default=config.ESG_PARSE_INDEX_CSV
    )
    parser.add_argument("--raw-root", type=Path, default=config.RAW_SUSTAINABILITY_DIR)
    parser.add_argument(
        "--file-catalog", type=Path, default=config.ESG_FILE_CATALOG_CSV
    )
    parser.add_argument(
        "--ocr-approvals", type=Path, default=config.ESG_OCR_APPROVAL_CSV
    )
    args = parser.parse_args()

    try:
        counts = build_database(
            args.output,
            sections_index=args.sections_index,
            chunks_index=args.chunks_index,
            companies=args.companies,
            tracker=args.tracker,
            parse_index=args.parse_index,
            raw_root=args.raw_root,
            file_catalog=args.file_catalog,
            ocr_approvals=args.ocr_approvals,
        )
    except FileExistsError as exc:
        print(exc)
        return 2

    print(f"\nSQLite QA database created: {args.output.resolve()}")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
