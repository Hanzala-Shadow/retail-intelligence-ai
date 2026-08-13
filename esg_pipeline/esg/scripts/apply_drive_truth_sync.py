"""Plan or apply a Drive-truth update for source CSVs.

The current ``esg_drive_manifest.csv`` is treated as the Drive snapshot.  The
script updates only source inventory CSVs:

* ``sustainability_report_tracker.csv``
* ``esg_file_catalog.csv``
* ``esg_parse_index.csv`` (rebuilt from current parse v2 rows plus catalog lineage)

Derived parse, section, and chunk indexes are deliberately not rewritten.
Use ``--apply`` to write.  Without it, the script only prints a plan.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from check_drive_csv_sync import (
    drive_id_from_link,
    filename_from_tracker,
    int_value,
    key,
    read_csv,
)


TRACKER_FIELDS = [
    "company_id",
    "ticker",
    "company_name",
    "report_year",
    "format",
    "drive_file_link",
    "status",
    "notes",
]
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
LINEAGE_FIELDS = (
    "logical_source_id",
    "source_version_id",
    "file_alias_id",
    "extraction_artifact_id",
)


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root_from_script())
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the updated CSVs. Without this flag, only a plan is printed.",
    )
    parser.add_argument(
        "--hash-local",
        action="store_true",
        help="Compute SHA-256 for PDFs when parse v2 has no current hash.",
    )
    return parser.parse_args(argv)


def write_csv_atomic(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    temp_path.replace(path)


def backup(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def company_map(path: Path) -> dict[str, dict[str, str]]:
    return {key(row.get("ticker"), "")[0]: row for row in read_csv(path)}


def report_year(filename: str) -> str:
    years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", filename)]
    return str(max(years)) if years else ""


def local_pdf_path(repo_root: Path, manifest_row: dict[str, str]) -> Path:
    configured = Path(manifest_row.get("local_file") or "")
    if configured.is_absolute():
        return configured
    return repo_root / configured


def local_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_manifest(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        result[key(row.get("ticker"), row.get("drive_file_name"))] = row
    return result


def current_parse_hashes(rows: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        value = (row.get("source_sha256") or "").strip().lower()
        if value:
            result[key(row.get("ticker"), row.get("pdf_file"))] = value
    return result


def sync_tracker(
    existing: list[dict[str, str]],
    manifest: dict[tuple[str, str], dict[str, str]],
    companies: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], int, int]:
    old_by_key: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in existing:
        old_by_key.setdefault(key(row.get("ticker"), filename_from_tracker(row)), []).append(row)

    output: list[dict[str, str]] = []
    updated = 0
    added = 0
    for item_key in sorted(manifest):
        drive_row = manifest[item_key]
        old = old_by_key.get(item_key, [])
        row = dict(old[0]) if old else {}
        if not old:
            added += 1
        ticker, filename = item_key
        company = companies.get(ticker, {})
        row.update(
            {
                "company_id": row.get("company_id") or company.get("company_id", ""),
                "ticker": ticker,
                "company_name": row.get("company_name") or company.get("name", ""),
                "report_year": row.get("report_year") or report_year(filename),
                "format": "PDF",
                "drive_file_link": f"https://drive.google.com/file/d/{drive_row.get('drive_file_id', '')}/view",
                "status": "downloaded",
                "notes": row.get("notes") or filename,
            }
        )
        if old and (
            drive_id_from_link(old[0].get("drive_file_link")) != drive_row.get("drive_file_id")
            or (old[0].get("status") or "").strip().lower() != "downloaded"
        ):
            updated += 1
        output.append(row)

    for item_key, old_rows in old_by_key.items():
        if item_key in manifest:
            continue
        for old in old_rows:
            row = dict(old)
            row["status"] = "drive_missing"
            output.append(row)

    output.sort(key=lambda row: (int_value(row.get("company_id")) or 10**9, row.get("ticker", ""), row.get("report_year", ""), row.get("notes", "")))
    return output, updated, sum(1 for item_key in old_by_key if item_key not in manifest)


def sync_catalog(
    existing: list[dict[str, str]],
    manifest: dict[tuple[str, str], dict[str, str]],
    parse_hashes: dict[tuple[str, str], str],
    repo_root: Path,
    hash_local: bool,
    now: str,
) -> tuple[list[dict[str, str]], int, int, int, int]:
    old_by_key: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in existing:
        old_by_key.setdefault(key(row.get("canonical_ticker") or row.get("observed_ticker"), row.get("pdf_file")), []).append(row)

    output: list[dict[str, str]] = []
    updated = 0
    added = 0
    replacements = 0
    lineage_review = 0
    for item_key in sorted(manifest):
        drive_row = manifest[item_key]
        old = old_by_key.get(item_key, [])
        active_old = next(
            (candidate for candidate in old if (candidate.get("active") or "").strip().lower() == "true"),
            old[0] if old else None,
        )
        row = dict(active_old) if active_old else {field: "" for field in CATALOG_FIELDS}
        historical_rows = [dict(candidate) for candidate in old if candidate is not active_old]
        if not old:
            added += 1
        ticker, filename = item_key
        path = local_pdf_path(repo_root, drive_row)
        current_size = int_value(drive_row.get("local_size_bytes")) or (path.stat().st_size if path.exists() else 0)
        current_sha = parse_hashes.get(item_key, "")
        if not current_sha and hash_local and path.exists():
            current_sha = local_sha256(path)

        old_size = int_value(row.get("size_bytes"))
        old_sha = (row.get("sha256") or "").strip().lower()
        changed_bytes = old_size is not None and old_size != current_size
        changed_bytes = changed_bytes or bool(old_sha and current_sha and old_sha != current_sha)
        changed_bytes = changed_bytes or not old
        if changed_bytes:
            if old:
                # Keep the old byte identity as history.  A changed PDF needs
                # a new source-version row; reusing the old IDs would make
                # lineage and downstream DB joins point at the wrong bytes.
                for historical in old:
                    if (historical.get("active") or "").strip().lower() == "true":
                        historical["active"] = "false"
                        historical["processing_state"] = "drive_replaced_history"
                        historical["review_reason"] = (
                            "Superseded by a different byte version in the current Drive manifest"
                        )
                    output.append(historical)
                replacements += 1
                historical_rows = []
            row = dict(row)
            lineage_review += 1
            for field in ("logical_source_id", "source_version_id", "file_alias_id", "extraction_artifact_id"):
                row[field] = ""
            row["processing_state"] = "drive_replaced_needs_lineage_rebuild"
            row["review_reason"] = "Drive bytes or source record changed; rebuild source lineage before DB promotion"
        else:
            row["processing_state"] = row.get("processing_state") or "eligible_candidate"
        row.update(
            {
                "observed_ticker": ticker,
                "canonical_ticker": ticker,
                "pdf_file": filename,
                "file_path": path.relative_to(repo_root).as_posix() if path.is_absolute() and path.exists() else f"data/01_raw/sustainability/{ticker}/{filename}",
                "drive_id": drive_row.get("drive_file_id", ""),
                "size_bytes": str(current_size),
                "sha256": current_sha or row.get("sha256", ""),
                "active": "true",
                "canonical_alias": row.get("canonical_alias") or "true",
                "cataloged_at": now,
            }
        )
        if old and (
            old_size != current_size
            or (active_old.get("drive_id") if active_old else "") != drive_row.get("drive_file_id")
        ):
            updated += 1
        output.extend(historical_rows)
        output.append(row)

    for item_key, old_rows in old_by_key.items():
        if item_key in manifest:
            continue
        for old in old_rows:
            row = dict(old)
            row["active"] = "false"
            row["processing_state"] = "drive_missing"
            row["review_reason"] = "No longer present in current Drive manifest"
            output.append(row)

    output.sort(key=lambda row: (row.get("canonical_ticker", ""), row.get("pdf_file", ""), row.get("active", "false") != "true"))
    return output, updated, sum(1 for item_key in old_by_key if item_key not in manifest), lineage_review, replacements


def sync_legacy_parse_index(
    parse_v2_rows: list[dict[str, str]],
    catalog_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int]:
    catalog_by_key = {
        key(row.get("canonical_ticker") or row.get("observed_ticker"), row.get("pdf_file")): row
        for row in catalog_rows
        if (row.get("active") or "").strip().lower() == "true"
    }
    output: list[dict[str, str]] = []
    missing_catalog = 0
    for parse_row in parse_v2_rows:
        row = dict(parse_row)
        item_key = key(parse_row.get("ticker"), parse_row.get("pdf_file"))
        catalog_row = catalog_by_key.get(item_key)
        if catalog_row is None:
            missing_catalog += 1
            for field in LINEAGE_FIELDS:
                row[field] = ""
        else:
            for field in LINEAGE_FIELDS:
                row[field] = catalog_row.get(field, "")
        output.append(row)
    return output, missing_catalog


def prepare_sync(repo_root: Path, hash_local: bool) -> dict[str, object]:
    reference = repo_root / "data" / "00_reference"
    tracker_path = reference / "sustainability_report_tracker.csv"
    catalog_path = reference / "esg_file_catalog.csv"
    companies_path = reference / "companies.csv"
    parse_v2_path = reference / "esg_parse_index_v2.csv"

    manifest = current_manifest(read_csv(reference / "esg_drive_manifest.csv"))
    tracker = read_csv(tracker_path)
    catalog = read_csv(catalog_path)
    companies = company_map(companies_path)
    parse_v2_rows = read_csv(parse_v2_path)
    parse_hashes = current_parse_hashes(parse_v2_rows)
    now = datetime.now(timezone.utc).isoformat()

    tracker_rows, tracker_updated, tracker_missing = sync_tracker(tracker, manifest, companies)
    catalog_rows, catalog_updated, catalog_missing, lineage_review, catalog_replacements = sync_catalog(
        catalog, manifest, parse_hashes, repo_root, hash_local, now
    )
    parse_index_rows, parse_missing_catalog = sync_legacy_parse_index(parse_v2_rows, catalog_rows)
    return {
        "reference": reference,
        "manifest": manifest,
        "tracker_path": tracker_path,
        "catalog_path": catalog_path,
        "parse_index_path": reference / "esg_parse_index.csv",
        "tracker_rows": tracker_rows,
        "catalog_rows": catalog_rows,
        "parse_index_rows": parse_index_rows,
        "tracker_updated": tracker_updated,
        "tracker_missing": tracker_missing,
        "catalog_updated": catalog_updated,
        "catalog_missing": catalog_missing,
        "catalog_replacements": catalog_replacements,
        "lineage_review": lineage_review,
        "parse_missing_catalog": parse_missing_catalog,
    }


def apply_plan(plan: dict[str, object]) -> Path:
    reference = plan["reference"]
    backup_dir = reference / "_sync_backups" / datetime.now().strftime("%Y%m%dT%H%M%SZ")
    backup(plan["tracker_path"], backup_dir)
    backup(plan["catalog_path"], backup_dir)
    backup(plan["parse_index_path"], backup_dir)
    write_csv_atomic(plan["tracker_path"], plan["tracker_rows"], TRACKER_FIELDS)
    write_csv_atomic(plan["catalog_path"], plan["catalog_rows"], CATALOG_FIELDS)
    parse_rows = plan["parse_index_rows"]
    parse_fields = list(parse_rows[0]) if parse_rows else []
    write_csv_atomic(plan["parse_index_path"], parse_rows, parse_fields)
    return backup_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    plan = prepare_sync(repo_root, args.hash_local)

    print("Drive-truth sync plan")
    print(f"  current Drive records: {len(plan['manifest'])}")
    print(f"  tracker rows: {len(read_csv(plan['tracker_path']))} -> {len(plan['tracker_rows'])}")
    print(f"  tracker current rows updated: {plan['tracker_updated']}; missing rows retained: {plan['tracker_missing']}")
    print(f"  catalog rows: {len(read_csv(plan['catalog_path']))} -> {len(plan['catalog_rows'])}")
    print(f"  catalog current rows updated: {plan['catalog_updated']}; missing rows retained inactive: {plan['catalog_missing']}")
    print(f"  catalog byte replacements: {plan['catalog_replacements']}; new source-version rows need lineage rebuild")
    print(f"  catalog rows requiring lineage rebuild: {plan['lineage_review']}")
    print(f"  legacy parse index rows: {len(plan['parse_index_rows'])}; missing catalog rows: {plan['parse_missing_catalog']}")
    print("  parse v2, sections, chunks, and QA snapshots: unchanged")

    if not args.apply:
        print("Dry run only. Add --apply to create backups and overwrite the three source CSVs.")
        return 0

    backup_dir = apply_plan(plan)
    print(f"Applied. Backups: {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
