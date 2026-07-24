"""
dedup_sustainability_reports.py

Finds and removes duplicate files created by running the copy-merge script
more than once — Drive allows multiple files with the identical name in the
same folder, unlike a normal filesystem, so a double-run creates real
duplicates rather than overwriting.

SAFETY DESIGN:
  - Groups by (parent folder, filename) AND confirms matching MD5 hash
    before treating anything as a duplicate — a same-name-different-content
    pair is never touched automatically, only flagged.
  - Removes duplicates by moving to TRASH (recoverable), never permanent
    delete.
  - Default (no flags): dry-run, lists what would be trashed, changes nothing.
  - --execute: actually trashes the extra copies (keeps the oldest one).
"""

import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from collections import defaultdict
import csv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
OLD_FOLDER_NAME = "Sustainability Reports"
TOKEN_PATH = Path("token_write.json")


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


def list_ticker_folders(service, parent_id):
    query = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = []
    page_token = None
    while True:
        response = service.files().list(
            q=query, fields="nextPageToken, files(id, name)", pageToken=page_token
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def list_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false"
    results = []
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, md5Checksum, createdTime, size)",
            pageToken=page_token,
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def trash_file(service, file_id):
    service.files().update(fileId=file_id, body={"trashed": True}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually trash the duplicates. Without this flag, only lists what would happen.")
    args = ap.parse_args()

    print("Authenticating with Google Drive (write access)...")
    service = get_service()

    old_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, OLD_FOLDER_NAME)
    if not old_folder_id:
        print("FATAL: could not locate the Sustainability Reports folder.")
        return

    print("Listing ticker folders...")
    ticker_folders = list_ticker_folders(service, old_folder_id)
    print(f"Found {len(ticker_folders)} ticker folders")

    if not args.execute:
        print("\n*** DRY RUN — no changes made. Re-run with --execute to actually trash duplicates. ***\n")

    total_dupe_groups = 0
    total_trashed = 0
    flagged_for_review = []
    log_rows = []

    for i, folder in enumerate(ticker_folders, 1):
        ticker = folder["name"]
        files = list_files_in_folder(service, folder["id"])
        if not files:
            continue

        # Group by filename
        by_name = defaultdict(list)
        for f in files:
            by_name[f["name"]].append(f)

        dupe_groups = {name: flist for name, flist in by_name.items() if len(flist) > 1}
        if not dupe_groups:
            continue

        print(f"[{i}/{len(ticker_folders)}] {ticker}: {len(dupe_groups)} duplicate filename group(s)")

        for filename, flist in dupe_groups.items():
            hashes = set(f.get("md5Checksum", "") for f in flist)

            if len(hashes) > 1:
                # Same name, DIFFERENT content - not a simple duplicate, flag for manual review
                print(f"  FLAGGED (same name, different hash — NOT auto-removing): {filename}")
                flagged_for_review.append({"ticker": ticker, "filename": filename,
                                            "file_ids": [f["id"] for f in flist]})
                continue

            # Genuine duplicates - same name, same hash. Keep the oldest, trash the rest.
            flist_sorted = sorted(flist, key=lambda f: f.get("createdTime", ""))
            keep = flist_sorted[0]
            extras = flist_sorted[1:]

            total_dupe_groups += 1
            print(f"  {filename}: {len(flist)} copies -> keeping 1, "
                  f"{'would trash' if not args.execute else 'trashing'} {len(extras)}")

            for extra in extras:
                log_rows.append({
                    "ticker": ticker, "filename": filename,
                    "kept_file_id": keep["id"], "trashed_file_id": extra["id"],
                })
                if args.execute:
                    try:
                        trash_file(service, extra["id"])
                        total_trashed += 1
                    except Exception as e:
                        print(f"    FAILED to trash {extra['id']}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Duplicate groups found: {total_dupe_groups}")
    if args.execute:
        print(f"Trashed (recoverable): {total_trashed}")
    else:
        print(f"Would trash: {sum(len(r) for r in [log_rows])}")
    print(f"Flagged for manual review (same name, different content): {len(flagged_for_review)}")
    print("=" * 60)

    if log_rows:
        out_path = Path("reports/sustainability_dedup_log.csv")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ticker", "filename", "kept_file_id", "trashed_file_id"])
            writer.writeheader()
            writer.writerows(log_rows)
        print(f"Log saved to {out_path}")


if __name__ == "__main__":
    main()