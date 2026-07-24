from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


CATALOG_FIELDS = [
    "logical_source_id",
    "source_version_id",
    "file_alias_id",
    "extraction_artifact_id",
    "observed_ticker",
    "canonical_ticker",
    "pdf_file",
    "file_path",
    "drive_id",
    "artifact_role",
    "sha256",
    "size_bytes",
    "mtime_utc",
    "registry_source_id",
    "duplicate_of_source_version_id",
    "canonical_alias",
    "ownership_review_required",
    "near_duplicate_review_required",
    "review_reason",
    "processing_state",
    "active",
    "cataloged_at",
]

OCR_APPROVAL_FIELDS = [
    "logical_source_id",
    "original_source_version_id",
    "original_sha256",
    "ocr_artifact_id",
    "ocr_artifact_sha256",
    "ocr_path",
    "ocr_drive_id",
    "approval_status",
    "reviewer",
    "approval_date",
    "reason",
    "state",
]

APPROVED_STATUSES = {"approved", "approve"}
ACTIVE_STATES = {"active", "current"}


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def logical_source_id(value: str) -> str:
    return _stable_id("ls", value)


def source_version_id(source_sha256: str) -> str:
    return _stable_id("sv", source_sha256.lower())


def file_alias_id(locator: str) -> str:
    normalized = os.path.normcase(os.path.normpath(locator.strip()))
    return _stable_id("fa", normalized)


def extraction_artifact_id(role: str, artifact_sha256: str) -> str:
    return _stable_id("ea", f"{role.strip().lower()}:{artifact_sha256.lower()}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: str | Path | None) -> list[dict]:
    if path is None or not Path(path).is_file():
        return []
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def write_csv_atomic(path: str | Path, rows: Iterable[dict], fields: list[str]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(destination)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def load_registry(path: str | Path | None) -> dict[tuple[str, str], dict]:
    registry: dict[tuple[str, str], dict] = {}
    for row in read_csv(path):
        ticker = str(row.get("observed_ticker") or row.get("ticker") or "").strip().upper()
        stem = str(row.get("pdf_stem") or "").strip()
        if ticker and stem:
            registry[(ticker, stem)] = row
    return registry


def _existing_hashes(path: str | Path | None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for row in read_csv(path):
        raw_path = str(row.get("source_pdf") or "").strip()
        digest = str(row.get("source_sha256") or "").strip().lower()
        if raw_path and re.fullmatch(r"[0-9a-f]{64}", digest):
            hashes[os.path.normcase(os.path.normpath(raw_path))] = digest
    return hashes


def _path_hash(path: Path, known_hashes: dict[str, str]) -> str:
    display = path.as_posix()
    for candidate in (display, str(path), str(path.resolve())):
        known = known_hashes.get(os.path.normcase(os.path.normpath(candidate)))
        if known:
            return known
    return sha256_file(path)


@dataclass(frozen=True)
class IntakeFile:
    path: Path
    ticker: str
    artifact_role: str


def discover_files(
    raw_root: str | Path | list[str | Path],
    ocr_root: str | Path | None = None,
    *,
    ticker: str | None = None,
    pdf_file: str | None = None,
    pdf_stem: str | None = None,
) -> list[IntakeFile]:
    wanted_ticker = ticker.upper() if ticker else None
    wanted_stem = pdf_stem or (Path(pdf_file).stem if pdf_file else None)
    wanted_name = Path(pdf_file).name if pdf_file else None
    files: list[IntakeFile] = []

    def add_root(root: Path, role: str) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.glob("*/*.pdf")):
            file_ticker = path.parent.name.upper()
            if wanted_ticker and file_ticker != wanted_ticker:
                continue
            if wanted_name and path.name != wanted_name and path.stem != wanted_stem:
                continue
            if wanted_stem and path.stem != wanted_stem:
                continue
            files.append(IntakeFile(path=path, ticker=file_ticker, artifact_role=role))

    raw_roots = raw_root if isinstance(raw_root, list) else [raw_root]
    for root in raw_roots:
        add_root(Path(root), "original")
    if ocr_root is not None:
        add_root(Path(ocr_root), "ocr_derivative")
    return files


def build_catalog(
    files: Iterable[IntakeFile],
    *,
    registry: dict[tuple[str, str], dict] | None = None,
    known_hashes: dict[str, str] | None = None,
    cataloged_at: str | None = None,
) -> list[dict]:
    """Build a global alias catalog without treating OCR staging as a report.

    Exact hashes are grouped across every ticker and location. Only an original
    alias may become the canonical parse source. OCR derivatives stay physical
    evidence until a separate approval row admits them for extraction.
    """

    registry = registry or {}
    known_hashes = known_hashes or {}
    now = cataloged_at or datetime.now(UTC).isoformat()
    records: list[dict] = []
    for item in files:
        stat = item.path.stat()
        digest = _path_hash(item.path, known_hashes)
        registry_row = registry.get((item.ticker, item.path.stem), {})
        registry_id = str(registry_row.get("source_id") or "").strip()
        canonical_ticker = str(
            registry_row.get("canonical_ticker") or item.ticker
        ).strip().upper()
        identity_seed = f"registry:{registry_id}" if registry_id else f"sha256:{digest}"
        records.append(
            {
                "logical_source_id": logical_source_id(identity_seed),
                "source_version_id": source_version_id(digest),
                "file_alias_id": file_alias_id(str(item.path.resolve())),
                "extraction_artifact_id": extraction_artifact_id(item.artifact_role, digest),
                "observed_ticker": item.ticker,
                "canonical_ticker": canonical_ticker,
                "pdf_file": item.path.name,
                "file_path": item.path.as_posix(),
                "drive_id": "",
                "artifact_role": item.artifact_role,
                "sha256": digest,
                "size_bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "registry_source_id": registry_id,
                "duplicate_of_source_version_id": "",
                "canonical_alias": "false",
                "ownership_review_required": "false",
                "near_duplicate_review_required": "false",
                "review_reason": "",
                "processing_state": (
                    "held_for_approval" if item.artifact_role == "ocr_derivative" else "eligible_candidate"
                ),
                "active": "true",
                "cataloged_at": now,
                "_registry_include": _truthy(registry_row.get("include_in_esg_index")),
            }
        )

    by_hash: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        by_hash[row["sha256"]].append(row)

    for rows in by_hash.values():
        originals = [row for row in rows if row["artifact_role"] == "original"]
        if not originals:
            continue
        canonical = sorted(
            originals,
            key=lambda row: (
                not row["_registry_include"],
                not bool(row["registry_source_id"]),
                row["file_path"].casefold(),
            ),
        )[0]
        # Hash identity wins over an alias-specific registry row. This turns a
        # renamed exact duplicate into one logical source and one source version.
        shared_logical_id = canonical["logical_source_id"]
        tickers = {row["observed_ticker"] for row in originals}
        for row in rows:
            row["logical_source_id"] = shared_logical_id
            if row is canonical:
                row["canonical_alias"] = "true"
                row["processing_state"] = "eligible_candidate"
                continue
            if row["artifact_role"] == "original":
                row["duplicate_of_source_version_id"] = canonical["source_version_id"]
                row["processing_state"] = "excluded_duplicate"
                if len(tickers) > 1 and row["observed_ticker"] != canonical["observed_ticker"]:
                    row["ownership_review_required"] = "true"
                    row["review_reason"] = "exact_duplicate_under_another_ticker"

    for row in records:
        row.pop("_registry_include", None)
    return sorted(records, key=lambda row: (row["artifact_role"], row["file_path"].casefold()))


def merge_catalog(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Idempotently upsert aliases while retaining inactive history."""

    rows = {str(row.get("file_alias_id") or ""): dict(row) for row in existing}
    for row in incoming:
        rows[row["file_alias_id"]] = dict(row)
    return sorted(rows.values(), key=lambda row: str(row.get("file_path") or "").casefold())


def ensure_approval_file(path: str | Path) -> None:
    destination = Path(path)
    if destination.exists():
        return
    write_csv_atomic(destination, [], OCR_APPROVAL_FIELDS)


def approved_ocr_rows(path: str | Path | None) -> list[dict]:
    approved: list[dict] = []
    for row in read_csv(path):
        status = str(row.get("approval_status") or "").strip().lower()
        state = str(row.get("state") or "").strip().lower()
        if status in APPROVED_STATUSES and state in ACTIVE_STATES:
            approved.append(row)
    return approved


def run(args: argparse.Namespace) -> list[dict]:
    files = discover_files(
        args.raw_root,
        args.ocr_root,
        ticker=args.ticker,
        pdf_file=args.pdf_file,
        pdf_stem=args.pdf_stem,
    )
    catalog = build_catalog(
        files,
        registry=load_registry(args.source_registry),
        known_hashes=_existing_hashes(args.parse_index),
    )
    merged = merge_catalog(read_csv(args.catalog), catalog)
    write_csv_atomic(args.catalog, merged, CATALOG_FIELDS)
    ensure_approval_file(args.ocr_approval)
    print(f"Cataloged {len(files)} file alias(es); {len(merged)} total catalog row(s).")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the global ESG file and extraction-artifact catalog.")
    parser.add_argument(
        "--raw-root",
        action="append",
        default=None,
        help="Original report root; repeat for global deduplication across roots.",
    )
    parser.add_argument("--ocr-root", default="data/02_interim/ocr_staging")
    parser.add_argument("--catalog", default="data/00_reference/esg_file_catalog.csv")
    parser.add_argument("--ocr-approval", default="data/00_reference/esg_ocr_approval.csv")
    parser.add_argument("--source-registry", default="data/00_reference/esg_source_registry.csv")
    parser.add_argument("--parse-index", default="data/00_reference/esg_parse_index.csv")
    parser.add_argument("--ticker")
    parser.add_argument("--pdf-file")
    parser.add_argument("--pdf-stem")
    parser.add_argument("--force", action="store_true", help="Refresh the selected aliases; must be scoped by the runner.")
    args = parser.parse_args()
    if args.pdf_file and args.pdf_stem:
        parser.error("use only one of --pdf-file and --pdf-stem")
    args.raw_root = args.raw_root or [
        "data/01_raw/sustainability",
        "data/01_raw/sustainability_other",
    ]
    run(args)


if __name__ == "__main__":
    main()
