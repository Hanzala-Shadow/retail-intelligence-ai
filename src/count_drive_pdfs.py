"""count_drive_pdfs.py

Recursively counts PDF files per folder in Google Drive, starting from a
given folder (defaults to DRIVE_ROOT_FOLDER_ID in .env). Read-only: it never
creates, modifies, or deletes anything in Drive.

Reuses the same OAuth desktop flow as drive_downloader.py / drive_uploader.py
(client_secret.json + token.json), so if you've already authenticated for
those scripts this will just work without a new browser prompt.

Usage:
    python src/count_drive_pdfs.py
    python src/count_drive_pdfs.py --folder-id 1AbCdEfGhIjKlMnOp
    python src/count_drive_pdfs.py --folder-name "Sustainability Reports"
    python src/count_drive_pdfs.py --max-depth 2
    python src/count_drive_pdfs.py --out reports/drive_pdf_counts.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLIENT_SECRET_PATH = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "client_secret.json")
TOKEN_PATH = "token.json"
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")

PDF_MIME_TYPE = "application/pdf"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

CSV_FIELDS = ["folder_path", "folder_id", "pdf_count", "subfolder_count", "depth"]


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


def list_children(service, parent_id: str) -> list[dict]:
    """List every non-trashed item directly under ``parent_id``."""
    results: list[dict] = []
    page_token = None
    query = f"'{parent_id}' in parents and trashed=false"
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def find_folder_by_name(service, name: str, parent_id: str) -> str | None:
    """Find a folder by exact (case-insensitive) name directly under ``parent_id``."""
    for item in list_children(service, parent_id):
        if item["mimeType"] == FOLDER_MIME_TYPE and item["name"].strip().lower() == name.strip().lower():
            return item["id"]
    return None


def get_root_name(service, folder_id: str) -> str:
    try:
        meta = service.files().get(
            fileId=folder_id, fields="name", supportsAllDrives=True
        ).execute()
        return meta.get("name", folder_id)
    except Exception:
        return folder_id


@dataclass
class FolderCount:
    folder_id: str
    path: str
    depth: int
    pdf_count: int = 0
    subfolder_count: int = 0


def walk_and_count(
    service,
    folder_id: str,
    path: str,
    depth: int,
    max_depth: int | None,
    rows: list[FolderCount],
) -> None:
    """Depth-first walk, recording one row per folder (including empty ones)."""
    children = list_children(service, folder_id)
    subfolders = [c for c in children if c["mimeType"] == FOLDER_MIME_TYPE]
    pdfs = [c for c in children if c["mimeType"] == PDF_MIME_TYPE]

    rows.append(
        FolderCount(
            folder_id=folder_id,
            path=path,
            depth=depth,
            pdf_count=len(pdfs),
            subfolder_count=len(subfolders),
        )
    )

    if max_depth is not None and depth >= max_depth:
        return

    for sub in sorted(subfolders, key=lambda item: item["name"].lower()):
        walk_and_count(
            service,
            sub["id"],
            f"{path}/{sub['name']}",
            depth + 1,
            max_depth,
            rows,
        )


def write_csv(path: Path, rows: list[FolderCount]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "folder_path": row.path,
                    "folder_id": row.folder_id,
                    "pdf_count": row.pdf_count,
                    "subfolder_count": row.subfolder_count,
                    "depth": row.depth,
                }
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--folder-id",
        default=None,
        help="Drive folder ID to start from. Defaults to DRIVE_ROOT_FOLDER_ID from .env.",
    )
    ap.add_argument(
        "--folder-name",
        default=None,
        help=(
            "Find a folder by name directly under --folder-id (or DRIVE_ROOT_FOLDER_ID) "
            "and start counting there instead, e.g. 'Sustainability Reports'."
        ),
    )
    ap.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Limit recursion depth below the start folder (default: unlimited).",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Optional CSV path to write the per-folder counts to.",
    )
    ap.add_argument(
        "--only-nonzero",
        action="store_true",
        help="Only print folders that contain at least one PDF directly.",
    )
    args = ap.parse_args()

    start_folder_id = args.folder_id or DRIVE_ROOT_FOLDER_ID
    if not start_folder_id:
        print("ERROR: no --folder-id given and DRIVE_ROOT_FOLDER_ID is not set in .env")
        sys.exit(1)

    service = get_service()

    if args.folder_name:
        found = find_folder_by_name(service, args.folder_name, start_folder_id)
        if not found:
            print(f"ERROR: no folder named '{args.folder_name}' found directly under {start_folder_id}")
            sys.exit(1)
        start_folder_id = found

    root_name = get_root_name(service, start_folder_id)
    print(f"Counting PDFs under '{root_name}' (id={start_folder_id})...\n")

    rows: list[FolderCount] = []
    walk_and_count(service, start_folder_id, root_name, 0, args.max_depth, rows)

    total_pdfs = sum(row.pdf_count for row in rows)
    total_folders = len(rows)

    display_rows = [r for r in rows if r.pdf_count > 0] if args.only_nonzero else rows
    name_width = min(max((len(r.path) for r in display_rows), default=10), 80)
    for row in display_rows:
        indent = "  " * row.depth
        label = f"{indent}{row.path.split('/')[-1]}"
        print(f"{label:<{name_width}}  {row.pdf_count:>5} pdf(s)  ({row.subfolder_count} subfolder(s))")

    print(f"\nTotal folders visited: {total_folders}")
    print(f"Total PDFs found:      {total_pdfs}")

    if args.out:
        out_path = Path(args.out)
        write_csv(out_path, rows)
        print(f"\nWrote per-folder counts to {out_path}")


if __name__ == "__main__":
    main()
