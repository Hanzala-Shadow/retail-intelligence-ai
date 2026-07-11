"""Download ESG/sustainability report PDFs from Google Drive safely.

The downloader is deliberately resumable: an existing, non-empty local PDF is
kept when its byte size and, when Drive provides it, checksum agree with the
Drive metadata.  Every download is
written to a sibling ``.tmp`` file and atomically replaced, so an interrupted
EC2 session cannot leave a partially-written PDF masquerading as a completed
one.  A small manifest is checkpointed after each file.

Usage:
    python src/drive_downloader.py --resume
    python src/drive_downloader.py --ticker GAP --force
    python src/drive_downloader.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLIENT_SECRET_PATH = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "client_secret.json")
TOKEN_PATH = "token.json"
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
SUSTAINABILITY_FOLDER_NAME = "Sustainability Reports"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "01_raw" / "sustainability"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "00_reference" / "esg_drive_manifest.csv"

MANIFEST_FIELDS = [
    "ticker",
    "drive_file_id",
    "drive_file_name",
    "drive_size_bytes",
    "drive_modified_time",
    "drive_md5_checksum",
    "local_file",
    "local_size_bytes",
    "status",
    "updated_at_utc",
    "error_message",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Atomically write a CSV checkpoint, retaining a valid prior file on stop."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def read_manifest(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Read prior manifest rows, keyed by ticker and Drive-facing filename."""
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = csv.DictReader(handle)
            return {
                ((row.get("ticker") or "").upper(), row.get("drive_file_name") or ""): row
                for row in rows
                if row.get("ticker") and row.get("drive_file_name")
            }
    except (OSError, csv.Error) as exc:
        print(f"WARNING: could not read existing Drive manifest {path}: {exc}")
        return {}


def write_manifest_checkpoint(
    manifest_path: Path,
    manifest: dict[tuple[str, str], dict[str, str]],
) -> None:
    rows = [manifest[key] for key in sorted(manifest)]
    atomic_write_csv(manifest_path, rows)


def get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(CLIENT_SECRET_PATH):
            print(f"ERROR: OAuth client secret not found at '{CLIENT_SECRET_PATH}'")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            authorization_prompt_message=(
                "\nOpen this URL in your browser if it does not open automatically:\n{url}\n"
            ),
        )
        with open(TOKEN_PATH, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_folder_children(service, parent_id: str, mime_filter: str | None = None) -> list[dict]:
    """List all files/folders directly under ``parent_id``."""
    query = f"'{parent_id}' in parents and trashed=false"
    if mime_filter:
        query += f" and mimeType='{mime_filter}'"

    results: list[dict] = []
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            fields=(
                "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum)"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def find_folder(service, name: str, parent_id: str) -> str | None:
    """Find a folder by name under ``parent_id``."""
    children = list_folder_children(
        service, parent_id, mime_filter="application/vnd.google-apps.folder"
    )
    for item in children:
        if item["name"].strip().lower() == name.strip().lower():
            return item["id"]
    return None


def remote_size_bytes(drive_file: dict) -> int | None:
    try:
        return int(drive_file["size"])
    except (KeyError, TypeError, ValueError):
        return None


def file_md5(path: Path) -> str:
    """Return the MD5 checksum used by Google Drive for binary file metadata."""
    hasher = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def local_file_state(
    dest_path: Path,
    expected_size: int | None,
    expected_md5: str | None,
) -> str:
    """Classify a local file relative to the Drive size/checksum metadata."""
    if not dest_path.exists():
        return "missing"
    try:
        local_size = dest_path.stat().st_size
    except OSError:
        return "missing"
    if local_size <= 0:
        return "zero_bytes"
    if expected_size is not None and local_size != expected_size:
        return "size_mismatch"
    if expected_md5:
        try:
            if file_md5(dest_path).lower() != expected_md5.lower():
                return "checksum_mismatch"
        except OSError:
            return "missing"
    return "valid"


def download_file(
    service,
    drive_file: dict,
    dest_path: Path,
    *,
    force: bool = False,
) -> tuple[str, str]:
    """Download one Drive file atomically.

    Returns ``(status, error_message)`` where status is one of
    ``downloaded``, ``skipped_existing``, ``redownloaded_stale``, or ``failed``.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    expected_size = remote_size_bytes(drive_file)
    expected_md5 = drive_file.get("md5Checksum") or None
    existing_state = local_file_state(dest_path, expected_size, expected_md5)

    if existing_state == "valid" and not force:
        print(f"    [skip] existing file matches Drive size: {dest_path.name}")
        return "skipped_existing", ""

    stale = existing_state in {"zero_bytes", "size_mismatch", "checksum_mismatch"}
    if force:
        print(f"    [force] downloading {dest_path.name}")
    elif stale:
        print(f"    [stale] redownloading {dest_path.name} ({existing_state})")
    else:
        print(f"    downloading {dest_path.name}")

    tmp_path = dest_path.with_name(f"{dest_path.name}.tmp")
    try:
        request = service.files().get_media(
            fileId=drive_file["id"], supportsAllDrives=True
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        payload = buffer.getvalue()
        if not payload:
            return "failed", "downloaded file was empty"
        if expected_size is not None and len(payload) != expected_size:
            return (
                "failed",
                f"downloaded size {len(payload)} does not match Drive size {expected_size}",
            )

        with tmp_path.open("wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, dest_path)
        return ("redownloaded_stale" if stale else "downloaded"), ""
    except Exception as exc:
        return "failed", str(exc)
    finally:
        # A leftover temp file is never considered valid on a resume run.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def manifest_row(
    ticker: str,
    drive_file: dict,
    destination: Path,
    status: str,
    error_message: str = "",
) -> dict[str, str]:
    try:
        local_size = str(destination.stat().st_size) if destination.exists() else ""
    except OSError:
        local_size = ""
    return {
        "ticker": ticker.upper(),
        "drive_file_id": str(drive_file.get("id") or ""),
        "drive_file_name": str(drive_file.get("name") or ""),
        "drive_size_bytes": str(remote_size_bytes(drive_file) or ""),
        "drive_modified_time": str(drive_file.get("modifiedTime") or ""),
        "drive_md5_checksum": str(drive_file.get("md5Checksum") or ""),
        "local_file": str(destination),
        "local_size_bytes": local_size,
        "status": status,
        "updated_at_utc": utc_now(),
        "error_message": error_message,
    }


def run(
    only_ticker: str | None = None,
    dry_run: bool = False,
    *,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = 1,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, int]:
    """Download ESG PDFs and return a machine-testable summary."""
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")
    if not DRIVE_ROOT_FOLDER_ID:
        print("ERROR: DRIVE_ROOT_FOLDER_ID not set in .env")
        sys.exit(1)

    service = get_service()
    print(f"Looking for '{SUSTAINABILITY_FOLDER_NAME}' under root folder...")
    esg_folder_id = find_folder(service, SUSTAINABILITY_FOLDER_NAME, DRIVE_ROOT_FOLDER_ID)

    if not esg_folder_id:
        for alt_name in ["Sustainability Report", "ESG Reports", "ESG"]:
            esg_folder_id = find_folder(service, alt_name, DRIVE_ROOT_FOLDER_ID)
            if esg_folder_id:
                print(f"Found as '{alt_name}'")
                break

    if not esg_folder_id:
        print("ERROR: Could not find sustainability folder under root.")
        print("Available folders in root:")
        folders = list_folder_children(
            service,
            DRIVE_ROOT_FOLDER_ID,
            mime_filter="application/vnd.google-apps.folder",
        )
        for folder in folders:
            print(f"  - {folder['name']} (id={folder['id']})")
        sys.exit(1)

    ticker_folders = list_folder_children(
        service,
        esg_folder_id,
        mime_filter="application/vnd.google-apps.folder",
    )
    if only_ticker:
        ticker_folders = [
            folder
            for folder in ticker_folders
            if folder["name"].upper() == only_ticker.upper()
        ]
        if not ticker_folders:
            print(f"ERROR: No subfolder found for ticker '{only_ticker}'")
            sys.exit(1)

    print(f"Found ESG folder (id={esg_folder_id})")
    print(f"Found {len(ticker_folders)} ticker subfolder(s)\n")

    summary = {
        "found_drive_files": 0,
        "downloaded": 0,
        "skipped_existing": 0,
        "redownloaded_stale": 0,
        "failed": 0,
    }
    manifest = {} if dry_run else read_manifest(manifest_path)
    completed_since_checkpoint = 0

    for folder in sorted(ticker_folders, key=lambda item: item["name"].upper()):
        ticker = folder["name"].upper()
        files = list_folder_children(service, folder["id"])
        pdfs = sorted(
            (item for item in files if item["name"].lower().endswith(".pdf")),
            key=lambda item: item["name"].lower(),
        )
        if not pdfs:
            print(f"{ticker}: no PDFs found in Drive folder")
            continue

        print(f"{ticker}: {len(pdfs)} PDF(s) found")
        summary["found_drive_files"] += len(pdfs)
        for drive_file in pdfs:
            destination = OUTPUT_DIR / ticker / drive_file["name"]
            if dry_run:
                size_mb = (remote_size_bytes(drive_file) or 0) / 1_000_000
                print(f"  [dry-run] {drive_file['name']} ({size_mb:.1f} MB) -> {destination}")
                continue

            # ``resume`` is the safe default. It keeps only local files whose
            # size/checksum still agree with current Drive metadata.
            status, error_message = download_file(
                service,
                drive_file,
                destination,
                force=force or not resume,
            )
            if status == "failed":
                print(f"    [error] failed to download {destination.name}: {error_message}")
            summary[status] += 1

            manifest[(ticker, drive_file["name"])] = manifest_row(
                ticker, drive_file, destination, status, error_message
            )
            completed_since_checkpoint += 1
            if completed_since_checkpoint >= checkpoint_every:
                write_manifest_checkpoint(manifest_path, manifest)
                completed_since_checkpoint = 0

    if not dry_run and completed_since_checkpoint:
        write_manifest_checkpoint(manifest_path, manifest)

    print("\n" + "\n".join(f"{key}: {value}" for key, value in summary.items()))
    if not dry_run:
        print(f"Files saved to: {OUTPUT_DIR.resolve()}")
        print(f"Manifest: {manifest_path.resolve()}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download ESG PDFs from Google Drive to EC2")
    parser.add_argument("--ticker", type=str, default=None, help="Download only this ticker")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Resume safely (default): keep matching non-empty local PDFs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload selected/all PDFs even when local sizes match Drive",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=1,
        help="Atomically update the Drive manifest every N files (default: 1)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Drive manifest output path (default: {DEFAULT_MANIFEST})",
    )
    args = parser.parse_args()
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every must be at least 1")

    run(
        only_ticker=args.ticker,
        dry_run=args.dry_run,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
        manifest_path=args.manifest,
    )
