"""Audit corpus CSVs against the current Google Drive manifest.

This is a local, read-only check.  It does not call Google Drive.  The
``esg_drive_manifest.csv`` file is the current Drive snapshot supplied to the
audit, and the script checks that the other local records agree with it.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


PDF_SUFFIX = ".pdf"
MISSING_TRACKER_STATUSES = {
    "drive_missing",
    "removed_from_drive",
    "archived_uncached",
}
TRUE_VALUES = {"1", "true", "yes", "y"}


@dataclass
class Issue:
    severity: str
    code: str
    message: str


@dataclass
class Audit:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    counters: Counter[str] = field(default_factory=Counter)

    def error(self, code: str, message: str) -> None:
        self.errors.append(Issue("ERROR", code, message))
        self.counters[code] += 1

    def warning(self, code: str, message: str) -> None:
        self.warnings.append(Issue("WARNING", code, message))
        self.counters[code] += 1


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def require_file(audit: Audit, path: Path) -> bool:
    if not path.exists():
        audit.error("missing_csv", f"Missing CSV: {path}")
        return False
    return True


def key(ticker: str | None, filename: str | None) -> tuple[str, str]:
    # Windows treats PDF filename case as equivalent.  Drive can preserve a
    # different case from the local download, so identity checks are folded.
    return ((ticker or "").strip().upper(), (filename or "").strip().casefold())


def filename_from_tracker(row: dict[str, str]) -> str:
    notes = (row.get("notes") or "").strip()
    match = re.search(r"([^\n]*?\.pdf)", notes, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def drive_id_from_link(link: str | None) -> str:
    match = re.search(r"/d/([A-Za-z0-9_-]{20,})", link or "")
    return match.group(1) if match else ""


def int_value(value: str | None) -> int | None:
    try:
        return int((value or "").strip())
    except ValueError:
        return None


def normalised_relative_pdf(path_value: str | None, root: Path) -> str:
    if not path_value:
        return ""
    raw = Path(path_value)
    candidates = [raw]
    if not raw.is_absolute():
        candidates.append(root / raw)
    for candidate in candidates:
        try:
            relative = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        return relative.as_posix().lower()
    return raw.name.lower()


def local_pdf_map(raw_root: Path) -> dict[tuple[str, str], Path]:
    result: dict[tuple[str, str], Path] = {}
    if not raw_root.exists():
        return result
    for path in raw_root.glob("*/**/*.pdf"):
        relative = path.relative_to(raw_root)
        if len(relative.parts) != 2:
            continue
        result[key(relative.parts[0], relative.parts[1])] = path
    return result


def check_manifest(
    audit: Audit,
    rows: list[dict[str, str]],
    raw_root: Path,
    repo_root: Path,
    hash_local: bool,
) -> tuple[dict[tuple[str, str], dict[str, str]], dict[Path, dict[str, str]]]:
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    local_files = local_pdf_map(raw_root)
    digest_cache: dict[Path, dict[str, str]] = {}

    for row in rows:
        item_key = key(row.get("ticker"), row.get("drive_file_name"))
        if item_key in by_key:
            audit.error("manifest_duplicate", f"Duplicate Drive row: {item_key}")
        by_key[item_key] = row

        if not item_key[0] or not item_key[1].lower().endswith(PDF_SUFFIX):
            audit.error("manifest_bad_key", f"Invalid Drive row key: {item_key}")
            continue

        local_path = local_files.get(item_key)
        if local_path is None:
            configured = row.get("local_file") or ""
            candidate = Path(configured)
            if not candidate.is_absolute():
                candidate = repo_root / candidate
            if candidate.exists():
                local_path = candidate
        if local_path is None or not local_path.exists():
            audit.error("raw_missing", f"Raw PDF missing: {item_key[0]}/{item_key[1]}")
            continue

        actual_size = local_path.stat().st_size
        expected_local_size = int_value(row.get("local_size_bytes"))
        expected_drive_size = int_value(row.get("drive_size_bytes"))
        if expected_local_size is not None and actual_size != expected_local_size:
            audit.error(
                "manifest_local_size_mismatch",
                f"{item_key}: local_size_bytes={expected_local_size}, actual={actual_size}",
            )
        if expected_drive_size is not None and actual_size != expected_drive_size:
            audit.error(
                "manifest_drive_size_mismatch",
                f"{item_key}: drive_size_bytes={expected_drive_size}, actual={actual_size}",
            )

        drive_md5 = (row.get("drive_md5_checksum") or "").strip().lower()
        if not drive_md5:
            audit.warning(
                "drive_md5_missing",
                f"{item_key}: Drive MD5 is blank; exact byte equality is unverified",
            )

        if hash_local:
            md5 = hashlib.md5(usedforsecurity=False)
            sha256 = hashlib.sha256()
            with local_path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    md5.update(block)
                    sha256.update(block)
            digest_cache[local_path] = {
                "md5": md5.hexdigest(),
                "sha256": sha256.hexdigest(),
            }
            if drive_md5 and digest_cache[local_path]["md5"] != drive_md5:
                audit.error(
                    "manifest_md5_mismatch",
                    f"{item_key}: local MD5 does not match Drive MD5",
                )

    active_keys = set(by_key)
    local_keys = set(local_files)
    for orphan in sorted(local_keys - active_keys):
        audit.error("raw_orphan", f"Raw PDF is not in Drive manifest: {orphan[0]}/{orphan[1]}")
    for missing in sorted(active_keys - local_keys):
        if missing[0] and missing[1]:
            audit.error("raw_missing", f"Manifest PDF is not in active raw: {missing[0]}/{missing[1]}")
    return by_key, digest_cache


def check_tracker(audit: Audit, rows: list[dict[str, str]], manifest: dict[tuple[str, str], dict[str, str]]) -> None:
    tracker_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        filename = filename_from_tracker(row)
        if filename:
            tracker_by_key[key(row.get("ticker"), filename)].append(row)

    for item_key, drive_row in manifest.items():
        matches = tracker_by_key.get(item_key, [])
        if not matches:
            audit.error("tracker_missing_current", f"Tracker has no current row: {item_key}")
            continue
        if len(matches) > 1:
            audit.error("tracker_duplicate", f"Tracker has duplicate rows: {item_key}")
        expected_id = (drive_row.get("drive_file_id") or "").strip()
        for row in matches:
            actual_id = drive_id_from_link(row.get("drive_file_link"))
            if expected_id and actual_id != expected_id:
                audit.error(
                    "tracker_drive_id_mismatch",
                    f"{item_key}: tracker={actual_id or '<blank>'}, manifest={expected_id}",
                )

    for item_key, matches in tracker_by_key.items():
        if item_key in manifest:
            continue
        for row in matches:
            status = (row.get("status") or "").strip().lower()
            if status not in MISSING_TRACKER_STATUSES:
                audit.error(
                    "tracker_stale_current",
                    f"Tracker row is not in current Drive manifest: {item_key} (status={status or '<blank>'})",
                )


def check_file_catalog(
    audit: Audit,
    rows: list[dict[str, str]],
    manifest: dict[tuple[str, str], dict[str, str]],
    repo_root: Path,
    digest_cache: dict[Path, dict[str, str]],
) -> None:
    active: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if (row.get("active") or "").strip().lower() in TRUE_VALUES:
            active[key(row.get("canonical_ticker") or row.get("observed_ticker"), row.get("pdf_file"))].append(row)

    for item_key, drive_row in manifest.items():
        matches = active.get(item_key, [])
        if not matches:
            audit.error("catalog_missing_current", f"Active catalog has no current row: {item_key}")
            continue
        if len(matches) > 1:
            audit.error("catalog_duplicate", f"Active catalog has duplicate rows: {item_key}")
        expected_id = (drive_row.get("drive_file_id") or "").strip()
        expected_size = int_value(drive_row.get("local_size_bytes"))
        for row in matches:
            actual_id = (row.get("drive_id") or "").strip()
            if expected_id and actual_id != expected_id:
                audit.error(
                    "catalog_drive_id_mismatch",
                    f"{item_key}: catalog={actual_id or '<blank>'}, manifest={expected_id}",
                )
            catalog_size = int_value(row.get("size_bytes"))
            if expected_size is not None and catalog_size != expected_size:
                audit.error(
                    "catalog_size_mismatch",
                    f"{item_key}: catalog={catalog_size}, manifest={expected_size}",
                )
            if digest_cache:
                path_value = row.get("file_path") or ""
                path = Path(path_value)
                if not path.is_absolute():
                    path = repo_root / path
                expected_sha = (row.get("sha256") or "").strip().lower()
                actual_sha = digest_cache.get(path.resolve(), {}).get("sha256")
                if expected_sha and actual_sha and expected_sha != actual_sha:
                    audit.error("catalog_sha256_mismatch", f"{item_key}: catalog SHA-256 differs from local PDF")

    for item_key, matches in active.items():
        if item_key not in manifest:
            audit.error("catalog_stale_active", f"Active catalog row is not in current Drive: {item_key}")


def check_parse_index(
    audit: Audit,
    rows: list[dict[str, str]],
    manifest: dict[tuple[str, str], dict[str, str]],
    repo_root: Path,
    digest_cache: dict[Path, dict[str, str]],
) -> None:
    by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_key[key(row.get("ticker"), row.get("pdf_file"))].append(row)

    for item_key in sorted(set(manifest) - set(by_key)):
        audit.error("parse_v2_missing", f"Parse v2 has no current row: {item_key}")
    for item_key in sorted(set(by_key) - set(manifest)):
        audit.error("parse_v2_stale", f"Parse v2 row is not in current Drive: {item_key}")
    for item_key, matches in by_key.items():
        if len(matches) > 1:
            audit.error("parse_v2_duplicate", f"Parse v2 has duplicate rows: {item_key}")
        if item_key not in manifest:
            continue
        row = matches[0]
        expected_size = int_value(manifest[item_key].get("local_size_bytes"))
        actual_size = int_value(row.get("source_size_bytes"))
        if expected_size is not None and actual_size != expected_size:
            audit.error("parse_v2_size_mismatch", f"{item_key}: parse={actual_size}, manifest={expected_size}")
        if digest_cache:
            source = Path(row.get("source_pdf") or "")
            if not source.is_absolute():
                source = repo_root / source
            expected_sha = (row.get("source_sha256") or "").strip().lower()
            actual_sha = digest_cache.get(source.resolve(), {}).get("sha256")
            if expected_sha and actual_sha and expected_sha != actual_sha:
                audit.error("parse_v2_sha256_mismatch", f"{item_key}: parse SHA-256 differs from local PDF")


def check_derived_index(
    audit: Audit,
    name: str,
    rows: Iterable[dict[str, str]],
    manifest: dict[tuple[str, str], dict[str, str]],
) -> None:
    current_stems = {
        key(ticker, Path(filename).stem)
        for ticker, filename in manifest
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item_key = key(row.get("ticker"), row.get("pdf_stem"))
        if item_key[0] and item_key[1]:
            seen.add(item_key)
            if item_key not in current_stems:
                audit.error(f"{name}_stale", f"{name} contains a document outside current Drive: {item_key}")

    if not seen:
        audit.warning(f"{name}_empty", f"{name} has no document rows")


def check_legacy_parse_index(audit: Audit, rows: list[dict[str, str]], repo_root: Path) -> None:
    for row in rows:
        source = Path(row.get("source_pdf") or "")
        if not source.is_absolute():
            source = repo_root / source
        if not source.exists():
            audit.error("legacy_parse_raw_missing", f"Legacy parse index source is missing: {source}")
            continue
        expected = int_value(row.get("source_size_bytes"))
        if expected is not None and source.stat().st_size != expected:
            audit.error(
                "legacy_parse_size_mismatch",
                f"Legacy parse index size differs from local PDF: {source}",
            )


def print_report(audit: Audit, counts: dict[str, int], as_json: bool) -> None:
    result = {
        "ok": not audit.errors,
        "errors": len(audit.errors),
        "warnings": len(audit.warnings),
        "counts": counts,
        "issue_counts": dict(sorted(audit.counters.items())),
        "error_examples": [issue.__dict__ for issue in audit.errors[:20]],
        "warning_examples": [issue.__dict__ for issue in audit.warnings[:20]],
    }
    if as_json:
        print(json.dumps(result, indent=2))
        return

    state = "PASS" if result["ok"] else "FAIL"
    print(f"Drive CSV sync audit: {state}")
    print("Counts: " + ", ".join(f"{name}={value}" for name, value in counts.items()))
    print(f"Errors: {len(audit.errors)}; warnings: {len(audit.warnings)}")
    if audit.counters:
        print("Issue counts:")
        for code, count in sorted(audit.counters.items()):
            print(f"  {code}: {count}")
    for issue in audit.errors[:20]:
        print(f"ERROR [{issue.code}] {issue.message}")
    for issue in audit.warnings[:10]:
        print(f"WARNING [{issue.code}] {issue.message}")
    if len(audit.errors) > 20 or len(audit.warnings) > 10:
        print("Additional issues are summarized above.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root_from_script())
    parser.add_argument(
        "--hash-local",
        action="store_true",
        help="Hash every active raw PDF and verify MD5/SHA-256 fields. This reads about 11 GB here.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up and rewrite the tracker, file catalog, and legacy parse index before checking them.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of text.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.apply:
        from apply_drive_truth_sync import apply_plan, prepare_sync

        plan = prepare_sync(repo_root, args.hash_local)
        reference = repo_root / "data" / "00_reference"
        raw_root = repo_root / "data" / "01_raw" / "sustainability"

        # Validate the planned rows before any file is changed.  The current
        # files are allowed to be stale; only the manifest, local PDFs, and
        # the generated replacement rows must pass this preflight.
        staged_audit = Audit()
        manifest, digest_cache = check_manifest(
            staged_audit,
            read_csv(reference / "esg_drive_manifest.csv"),
            raw_root,
            repo_root,
            args.hash_local,
        )
        if not staged_audit.errors:
            check_tracker(staged_audit, plan["tracker_rows"], manifest)
            check_file_catalog(
                staged_audit,
                plan["catalog_rows"],
                manifest,
                repo_root,
                digest_cache,
            )
            check_legacy_parse_index(staged_audit, plan["parse_index_rows"], repo_root)
            check_parse_index(
                staged_audit,
                read_csv(reference / "esg_parse_index_v2.csv"),
                manifest,
                repo_root,
                digest_cache,
            )
            check_derived_index(
                staged_audit,
                "sections",
                read_csv(reference / "esg_sections_index.csv"),
                manifest,
            )
            check_derived_index(
                staged_audit,
                "chunks",
                read_csv(reference / "esg_chunks_index.csv"),
                manifest,
            )
        if staged_audit.errors:
            print("Apply aborted: planned CSV rows failed preflight; no files were changed.")
            print_report(
                staged_audit,
                {
                    "drive_manifest": len(manifest),
                    "raw_pdfs": len(local_pdf_map(raw_root)),
                    "tracker": len(plan["tracker_rows"]),
                    "file_catalog": len(plan["catalog_rows"]),
                    "parse_v2": len(read_csv(reference / "esg_parse_index_v2.csv")),
                    "sections": len(read_csv(reference / "esg_sections_index.csv")),
                    "chunks": len(read_csv(reference / "esg_chunks_index.csv")),
                },
                args.json,
            )
            return 1
        backup_dir = apply_plan(plan)
        print(f"Applied Drive-truth source CSV update. Backups: {backup_dir}")
    reference = repo_root / "data" / "00_reference"
    raw_root = repo_root / "data" / "01_raw" / "sustainability"
    paths = {
        "drive_manifest": reference / "esg_drive_manifest.csv",
        "tracker": reference / "sustainability_report_tracker.csv",
        "file_catalog": reference / "esg_file_catalog.csv",
        "legacy_parse_index": reference / "esg_parse_index.csv",
        "parse_v2": reference / "esg_parse_index_v2.csv",
        "sections": reference / "esg_sections_index.csv",
        "chunks": reference / "esg_chunks_index.csv",
    }
    audit = Audit()
    loaded: dict[str, list[dict[str, str]]] = {}
    for name, path in paths.items():
        if require_file(audit, path):
            loaded[name] = read_csv(path)

    manifest, digest_cache = check_manifest(
        audit,
        loaded.get("drive_manifest", []),
        raw_root,
        repo_root,
        args.hash_local,
    )
    check_tracker(audit, loaded.get("tracker", []), manifest)
    check_file_catalog(audit, loaded.get("file_catalog", []), manifest, repo_root, digest_cache)
    check_legacy_parse_index(audit, loaded.get("legacy_parse_index", []), repo_root)
    check_parse_index(audit, loaded.get("parse_v2", []), manifest, repo_root, digest_cache)
    check_derived_index(audit, "sections", loaded.get("sections", []), manifest)
    check_derived_index(audit, "chunks", loaded.get("chunks", []), manifest)

    counts = {
        "drive_manifest": len(loaded.get("drive_manifest", [])),
        "raw_pdfs": len(local_pdf_map(raw_root)),
        "tracker": len(loaded.get("tracker", [])),
        "file_catalog": len(loaded.get("file_catalog", [])),
        "parse_v2": len(loaded.get("parse_v2", [])),
        "sections": len(loaded.get("sections", [])),
        "chunks": len(loaded.get("chunks", [])),
    }
    print_report(audit, counts, args.json)
    return 0 if not audit.errors else 1


if __name__ == "__main__":
    sys.exit(run())
