"""
drive_downloader.py

Downloads ESG/sustainability report PDFs from Google Drive to local disk.

Looks for a folder named "Sustainability Reports" under DRIVE_ROOT_FOLDER_ID,
then downloads all PDFs from each ticker subfolder into:
    data/01_raw/sustainability/{ticker}/

Uses the same OAuth flow as drive_uploader.py — reuses token.json if present,
otherwise opens browser for sign-in on first run.

Usage:
    python src/drive_downloader.py                    # download all 51 companies
    python src/drive_downloader.py --ticker GAP       # single company test
    python src/drive_downloader.py --dry-run          # list files without downloading
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLIENT_SECRET_PATH = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "client_secret.json")
TOKEN_PATH = "token.json"
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
SUSTAINABILITY_FOLDER_NAME = "Sustainability Reports"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "01_raw" / "sustainability"


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
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
        auth_url, _ = flow.authorization_url(prompt="consent")
        print(f"\nOpen this URL in your browser:\n{auth_url}\n")
        code = input("Paste the authorization code here: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_folder_children(service, parent_id: str, mime_filter: str = None) -> list:
    """List all files/folders directly under parent_id."""
    query = f"'{parent_id}' in parents and trashed=false"
    if mime_filter:
        query += f" and mimeType='{mime_filter}'"

    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, size)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def find_folder(service, name: str, parent_id: str) -> str | None:
    """Find a folder by name under parent_id. Returns folder ID or None."""
    children = list_folder_children(
        service, parent_id, mime_filter="application/vnd.google-apps.folder"
    )
    for f in children:
        if f["name"].strip().lower() == name.strip().lower():
            return f["id"]
    return None


def download_file(service, file_id: str, dest_path: Path) -> bool:
    """Download a Drive file to dest_path. Returns True on success."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if dest_path.exists() and dest_path.stat().st_size > 0:
        print(f"    [skip] already exists: {dest_path.name}")
        return True

    try:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        dest_path.write_bytes(buffer.getvalue())

        if dest_path.stat().st_size == 0:
            dest_path.unlink()
            print(f"    [error] downloaded file was empty: {dest_path.name}")
            return False

        return True
    except Exception as e:
        print(f"    [error] failed to download {dest_path.name}: {e}")
        return False


def run(only_ticker: str = None, dry_run: bool = False):
    if not DRIVE_ROOT_FOLDER_ID:
        print("ERROR: DRIVE_ROOT_FOLDER_ID not set in .env")
        sys.exit(1)

    service = get_service()

    # Find "Sustainability Reports" folder under root
    print(f"Looking for '{SUSTAINABILITY_FOLDER_NAME}' under root folder...")
    esg_folder_id = find_folder(service, SUSTAINABILITY_FOLDER_NAME, DRIVE_ROOT_FOLDER_ID)

    if not esg_folder_id:
        # Try common alternate names
        for alt_name in ["Sustainability Report", "ESG Reports", "ESG"]:
            esg_folder_id = find_folder(service, alt_name, DRIVE_ROOT_FOLDER_ID)
            if esg_folder_id:
                print(f"Found as '{alt_name}'")
                break

    if not esg_folder_id:
        print(f"ERROR: Could not find sustainability folder under root.")
        print("Available folders in root:")
        folders = list_folder_children(
            service, DRIVE_ROOT_FOLDER_ID,
            mime_filter="application/vnd.google-apps.folder"
        )
        for f in folders:
            print(f"  - {f['name']} (id={f['id']})")
        sys.exit(1)

    print(f"Found ESG folder (id={esg_folder_id})")

    # List ticker subfolders
    ticker_folders = list_folder_children(
        service, esg_folder_id,
        mime_filter="application/vnd.google-apps.folder"
    )

    if only_ticker:
        ticker_folders = [f for f in ticker_folders if f["name"].upper() == only_ticker.upper()]
        if not ticker_folders:
            print(f"ERROR: No subfolder found for ticker '{only_ticker}'")
            sys.exit(1)

    print(f"Found {len(ticker_folders)} ticker subfolder(s)\n")

    total_files = 0
    downloaded = 0
    skipped = 0
    failed = 0

    for folder in sorted(ticker_folders, key=lambda f: f["name"]):
        ticker = folder["name"]
        ticker_folder_id = folder["id"]

        # List PDFs in this ticker folder
        files = list_folder_children(service, ticker_folder_id)
        pdfs = [f for f in files if f["name"].lower().endswith(".pdf")]

        if not pdfs:
            print(f"{ticker}: no PDFs found in Drive folder")
            continue

        print(f"{ticker}: {len(pdfs)} PDF(s) found")
        total_files += len(pdfs)

        for pdf in pdfs:
            dest = OUTPUT_DIR / ticker / pdf["name"]
            if dry_run:
                size_mb = int(pdf.get("size", 0)) / 1_000_000
                print(f"  [dry-run] {pdf['name']} ({size_mb:.1f} MB) -> {dest}")
                continue

            print(f"  downloading {pdf['name']} ...")
            ok = download_file(service, pdf["id"], dest)
            if ok:
                if "already exists" in str(dest):
                    skipped += 1
                else:
                    downloaded += 1
            else:
                failed += 1

    if not dry_run:
        print(f"\nDone. downloaded={downloaded} skipped={skipped} failed={failed} total={total_files}")
        print(f"Files saved to: {OUTPUT_DIR.resolve()}")
    else:
        print(f"\nDry run complete. {total_files} PDFs would be downloaded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download ESG PDFs from Google Drive to EC2")
    parser.add_argument("--ticker", type=str, default=None, help="Download only this ticker")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    args = parser.parse_args()

    run(only_ticker=args.ticker, dry_run=args.dry_run)