"""
merge_sustainability_folders.py

Merges "Sustainability Reports New" (flat structure) into "Sustainability
Reports" (organized into per-ticker subfolders), moving each file into its
correct ticker folder.

SAFETY DESIGN — this modifies shared Drive data, so it defaults to dry-run:
  - Default (no flags): lists exactly what WOULD happen, changes nothing.
  - --execute: actually performs the moves.

Uses reports/sustainability_folder_comparison.csv (built earlier this week)
to decide what's safe to auto-merge:
  - status == "only_in_new"   -> SAFE, auto-merge (moves into correct ticker folder)
  - status == "hash_match"    -> SKIP (already identical in both places)
  - status == "hash_mismatch" -> SKIP (needs a human decision — see
                                  reports/rename_proposal.csv — not something
                                  this script should decide unattended)

Requires WRITE access to Drive (not just read), so uses a broader OAuth
scope than the read-only scripts built earlier this week. First run will
prompt for re-authentication.
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

# Broader scope than the read-only scripts this week - needed to move files
# and create folders. This will require a fresh token (different scope).
SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
OLD_FOLDER_NAME = "Sustainability Reports"
NEW_FOLDER_NAME = "Sustainability Reports New"
COMPARISON_CSV = Path("reports/sustainability_folder_comparison.csv")
TOKEN_PATH = Path("token_write.json")  # separate from the read-only token.json

# Known filename typos found during dry-run review (2026-07-23) — confirmed
# against the real ticker universe before merging, not guessed. Both are
# genuine existing companies with real files already in the Old folder;
# these are mistyped tickers in the New folder's filenames, not new
# companies. Without this correction, the merge would create duplicate
# wrong-name folders (TJS, WOOf) — the same class of defect as the
# APC/ARKO duplicate-ticker issue found earlier this week.
TICKER_CORRECTIONS = {
    "TJS": "TJX",   # TJX Cos Inc - confirmed 5 real files under TJX already
    "WOOf": "WOOF",  # Petco Health and Wellness - confirmed 4 real files under WOOF already
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


def create_folder(service, parent_id, name):
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def move_file(service, file_id, old_parent_id, new_parent_id):
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parent_id,
        fields="id, parents",
    ).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually perform the moves. Without this flag, only lists what would happen.")
    args = ap.parse_args()

    if not COMPARISON_CSV.exists():
        print(f"FATAL: {COMPARISON_CSV} not found. Run verify_sustainability_folders.py first.")
        return

    comparison = pd.read_csv(COMPARISON_CSV)
    to_merge = comparison[comparison["status"] == "only_in_new"]
    skip_match = comparison[comparison["status"] == "hash_match"]
    skip_mismatch = comparison[comparison["status"] == "hash_mismatch"]

    print(f"Files to merge (only_in_new): {len(to_merge)}")
    print(f"Files skipped (already identical, hash_match): {len(skip_match)}")
    print(f"Files skipped (unresolved hash_mismatch, needs human decision): {len(skip_mismatch)}")
    if len(skip_mismatch) > 0:
        print(f"  See reports/rename_proposal.csv for the {len(skip_mismatch)} pending decisions: "
              f"{sorted(skip_mismatch['ticker'].unique())}")

    if not args.execute:
        print("\n*** DRY RUN — no changes made. Re-run with --execute to actually merge. ***\n")

    print("\nAuthenticating with Google Drive (write access)...")
    service = get_service()

    old_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, OLD_FOLDER_NAME)
    new_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, NEW_FOLDER_NAME)
    if not old_folder_id or not new_folder_id:
        print("FATAL: could not locate one or both folders.")
        return

    moved, created_folders, failed = 0, 0, 0
    ticker_folder_cache = {}

    for i, row in to_merge.iterrows():
        ticker, filename = row["ticker"], row["filename"]
        if ticker in TICKER_CORRECTIONS:
            corrected = TICKER_CORRECTIONS[ticker]
            print(f"  Correcting ticker typo: {ticker} -> {corrected} ({filename})")
            ticker = corrected

        # Locate the file in the New folder
        source_file = find_file_in_folder(service, new_folder_id, filename)
        if not source_file:
            print(f"  SKIP (not found in New folder anymore): {filename}")
            continue

        # Find or create the ticker subfolder in the Old folder
        if ticker not in ticker_folder_cache:
            existing = find_subfolder(service, old_folder_id, ticker)
            if existing:
                ticker_folder_cache[ticker] = existing
            else:
                print(f"  {'[DRY RUN] Would create' if not args.execute else 'Creating'} folder: {ticker}")
                if args.execute:
                    new_id = create_folder(service, old_folder_id, ticker)
                    ticker_folder_cache[ticker] = new_id
                    created_folders += 1
                else:
                    ticker_folder_cache[ticker] = "DRY_RUN_PLACEHOLDER"

        dest_folder_id = ticker_folder_cache[ticker]

        print(f"  {'[DRY RUN] Would move' if not args.execute else 'Moving'}: "
              f"{ticker}/{filename} -> Sustainability Reports/{ticker}/")

        if args.execute and dest_folder_id != "DRY_RUN_PLACEHOLDER":
            try:
                move_file(service, source_file["id"], new_folder_id, dest_folder_id)
                moved += 1
            except Exception as e:
                print(f"    FAILED: {e}")
                failed += 1

    print(f"\n{'=' * 60}")
    if args.execute:
        print(f"Moved: {moved}")
        print(f"New ticker folders created: {created_folders}")
        print(f"Failed: {failed}")
    else:
        print(f"Would move: {len(to_merge)} files")
        print("Re-run with --execute to actually perform this merge.")
    print("=" * 60)


if __name__ == "__main__":
    main()