"""
investigate_hash_mismatches.py

For each (ticker, filename) pair that exists in BOTH "Sustainability Reports"
and "Sustainability Reports New" with a DIFFERENT hash, download both
versions and compare: file size, page count, and first-page text — to
figure out what's actually different (draft vs. final, wrong year, a
completely different document, etc.), not just that they differ.
"""

import os
import io
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pandas as pd
import pypdfium2 as pdfium

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
OLD_FOLDER_NAME = "Sustainability Reports"
NEW_FOLDER_NAME = "Sustainability Reports New"
DOWNLOAD_DIR = Path("data/mismatch_investigation")


def get_service():
    creds = None
    token_path = Path("token.json")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def find_subfolder(service, parent_id, name):
    query = f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def find_file_id(service, folder_id, filename, ticker_subfolder=None):
    """Find a file by name, optionally inside a ticker subfolder (for the Old
    folder's nested structure) or directly in the folder (New folder is flat)."""
    search_folder_id = folder_id
    if ticker_subfolder:
        sub_id = find_subfolder(service, folder_id, ticker_subfolder)
        if sub_id:
            search_folder_id = sub_id
    query = f"'{search_folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def download_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.close()


def inspect_pdf(pdf_path):
    doc = pdfium.PdfDocument(str(pdf_path))
    n_pages = len(doc)
    first_page_text = ""
    try:
        text_page = doc[0].get_textpage()
        first_page_text = text_page.get_text_range()[:400]
    except Exception:
        pass
    doc.close()
    return n_pages, first_page_text


def main():
    comparison = pd.read_csv("reports/sustainability_folder_comparison.csv")
    mismatches = comparison[comparison["status"] == "hash_mismatch"]
    print(f"Investigating {len(mismatches)} hash-mismatched files...")

    print("Authenticating with Google Drive...")
    service = get_service()
    old_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, OLD_FOLDER_NAME)
    new_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, NEW_FOLDER_NAME)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for _, row in mismatches.iterrows():
        ticker, filename = row["ticker"], row["filename"]
        print(f"\n{ticker} / {filename}")

        old_dir = DOWNLOAD_DIR / "old"
        new_dir = DOWNLOAD_DIR / "new"
        old_dir.mkdir(parents=True, exist_ok=True)
        new_dir.mkdir(parents=True, exist_ok=True)
        old_path = old_dir / filename
        new_path = new_dir / filename

        old_file_id = find_file_id(service, old_folder_id, filename, ticker_subfolder=ticker)
        new_file_id = find_file_id(service, new_folder_id, filename)

        if not old_file_id or not new_file_id:
            print(f"  Could not locate one or both files on Drive")
            continue

        download_file(service, old_file_id, str(old_path))
        download_file(service, new_file_id, str(new_path))

        old_size = old_path.stat().st_size
        new_size = new_path.stat().st_size

        try:
            old_pages, old_text = inspect_pdf(old_path)
        except Exception as e:
            old_pages, old_text = None, f"ERROR: {e}"
        try:
            new_pages, new_text = inspect_pdf(new_path)
        except Exception as e:
            new_pages, new_text = None, f"ERROR: {e}"

        print(f"  OLD: {old_size:,} bytes, {old_pages} pages")
        print(f"  NEW: {new_size:,} bytes, {new_pages} pages")
        print(f"  OLD first-page snippet: {old_text[:150]!r}")
        print(f"  NEW first-page snippet: {new_text[:150]!r}")

        results.append({
            "ticker": ticker, "filename": filename,
            "old_size_bytes": old_size, "new_size_bytes": new_size,
            "old_pages": old_pages, "new_pages": new_pages,
            "old_first_page_snippet": old_text[:300],
            "new_first_page_snippet": new_text[:300],
        })

    out_df = pd.DataFrame(results)
    out_path = Path("reports/hash_mismatch_investigation.csv")
    out_df.to_csv(out_path, index=False)
    print(f"\n{'=' * 60}")
    print(f"Full investigation results: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()