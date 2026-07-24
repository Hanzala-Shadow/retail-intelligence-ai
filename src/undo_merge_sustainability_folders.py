"""
undo_merge_sustainability_folders.py

Reverses the merge performed by merge_sustainability_folders.py --execute.

Uses the same reports/sustainability_folder_comparison.csv (only_in_new rows)
and the same TICKER_CORRECTIONS mapping to know exactly where each of the
231 moved files currently sits (Old/{corrected_ticker}/filename), and moves
each one back to "Sustainability Reports New" (flat).

SAFETY: dry-run by default, same as the original merge script.
  --execute actually performs the reverse moves.
"""

import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pandas as pd

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
OLD_FOLDER_NAME = "Sustainability Reports"
NEW_FOLDER_NAME = "Sustainability Reports New"
COMPARISON_CSV = Path("reports/sustainability_folder_comparison.csv")
TOKEN_PATH = Path("token_write.json")

# Must exactly match the mapping used during the original merge, so we look
# in the CORRECT folder each file actually landed in.
TICKER_CORRECTIONS = {
    "TJS": "TJX",
    "WOOf": "WOOF",
}


def get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def find_subfolder(service, parent_id, name):
    query = f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def find_file_in_folder(service, folder_id, filename):
    query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name, parents)").execute()
    files = results.get("files", [])
    return files[0] if files else None


def move_file(service, file_id, old_parent_id, new_parent_id):
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parent_id,
        fields="id, parents",
    ).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually perform the reverse moves.")
    args = ap.parse_args()

    comparison = pd.read_csv(COMPARISON_CSV)
    to_undo = comparison[comparison["status"] == "only_in_new"]
    print(f"Files to move back to New folder: {len(to_undo)}")

    if not args.execute:
        print("\n*** DRY RUN — no changes made. Re-run with --execute to actually undo. ***\n")

    print("Authenticating with Google Drive (write access)...")
    service = get_service()

    old_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, OLD_FOLDER_NAME)
    new_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, NEW_FOLDER_NAME)
    if not old_folder_id or not new_folder_id:
        print("FATAL: could not locate one or both folders.")
        return

    moved_back, not_found, failed = 0, 0, 0
    ticker_folder_cache = {}

    for i, row in to_undo.iterrows():
        ticker, filename = row["ticker"], row["filename"]
        actual_ticker = TICKER_CORRECTIONS.get(ticker, ticker)

        if actual_ticker not in ticker_folder_cache:
            ticker_folder_cache[actual_ticker] = find_subfolder(service, old_folder_id, actual_ticker)
        ticker_folder_id = ticker_folder_cache[actual_ticker]

        if not ticker_folder_id:
            print(f"  SKIP (ticker folder not found): {actual_ticker}/{filename}")
            not_found += 1
            continue

        current_file = find_file_in_folder(service, ticker_folder_id, filename)
        if not current_file:
            print(f"  SKIP (file not found in {actual_ticker}, may have already been moved back): {filename}")
            not_found += 1
            continue

        print(f"  {'[DRY RUN] Would move back' if not args.execute else 'Moving back'}: "
              f"{actual_ticker}/{filename} -> Sustainability Reports New/")

        if args.execute:
            try:
                move_file(service, current_file["id"], ticker_folder_id, new_folder_id)
                moved_back += 1
            except Exception as e:
                print(f"    FAILED: {e}")
                failed += 1

    print(f"\n{'=' * 60}")
    if args.execute:
        print(f"Moved back: {moved_back}")
        print(f"Not found (already moved back or missing): {not_found}")
        print(f"Failed: {failed}")
    else:
        print(f"Would move back: {len(to_undo)} files")
        print("Re-run with --execute to actually perform the undo.")
    print("=" * 60)


if __name__ == "__main__":
    main()