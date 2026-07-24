"""
download_flagged_from_new_folder.py

Downloads a specific, targeted list of PDFs from "Sustainability Reports New"
(the 2015-2020 historical backfill), for the 10 documents Aziz flagged as
having coverage/quality concerns.
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

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
NEW_FOLDER_NAME = "Sustainability Reports New"
LOCAL_ROOT = Path("data/01_raw/sustainability")

# The 10 flagged tickers - we'll grab ALL PDFs matching these tickers from
# the New folder, since exact filenames/years weren't given precisely
# (e.g. "GES 2018-2019" could be one or two files).
FLAGGED_TICKERS = ["PVH", "GES", "WMT", "SGI", "VZ", "TGT", "ULTA", "SONO", "HD", "PTRN"]


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


def list_pdfs_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false"
    results = []
    page_token = None
    while True:
        response = service.files().list(
            q=query, fields="nextPageToken, files(id, name, size)", pageToken=page_token
        ).execute()
        results.extend([f for f in response.get("files", []) if f["name"].lower().endswith(".pdf")])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def download_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.close()


def main():
    print("Authenticating with Google Drive...")
    service = get_service()

    print(f"Locating '{NEW_FOLDER_NAME}'...")
    new_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, NEW_FOLDER_NAME)
    if not new_folder_id:
        print("ERROR: could not find the New folder")
        return

    print("Listing all PDFs in New folder...")
    all_pdfs = list_pdfs_in_folder(service, new_folder_id)
    print(f"  {len(all_pdfs)} total PDFs found")

    matching = [f for f in all_pdfs if f["name"].split("-")[0].strip() in FLAGGED_TICKERS]
    print(f"  {len(matching)} match the flagged tickers: {FLAGGED_TICKERS}")

    downloaded, skipped, failed = 0, 0, 0
    for f in matching:
        ticker = f["name"].split("-")[0].strip()
        dest_dir = LOCAL_ROOT / ticker
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f["name"]

        if dest_path.exists() and dest_path.stat().st_size == int(f.get("size", 0)):
            skipped += 1
            continue

        try:
            download_file(service, f["id"], str(dest_path))
            downloaded += 1
            print(f"  downloaded: {ticker}/{f['name']}")
        except Exception as e:
            failed += 1
            print(f"  FAILED: {f['name']} ({e})")

    print(f"\n{'=' * 60}")
    print(f"Downloaded: {downloaded}, Skipped (already present): {skipped}, Failed: {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()