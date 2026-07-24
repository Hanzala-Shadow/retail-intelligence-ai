"""
verify_sustainability_folders.py

Aziz's request: verify whether PDFs in "Sustainability Reports" and
"Sustainability Reports New" match by hash, name, and content.

Compares both folders recursively (ticker subfolders) and reports:
  - Files present in both, with identical MD5 hash (Drive provides this
    natively via file metadata - no need to download+hash locally)
  - Files present in both, but DIFFERENT hash (same name, different content -
    a real discrepancy worth flagging)
  - Files present ONLY in old folder (missing from the new backfill)
  - Files present ONLY in new folder (genuinely new additions, e.g. the
    2015-2020 historical backfill mentioned in Ayşe's upload)
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import csv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")

OLD_FOLDER_NAME = "Sustainability Reports"
NEW_FOLDER_NAME = "Sustainability Reports New"


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


def extract_ticker_from_filename(filename):
    """Ticker is the prefix before the first hyphen, e.g.
    'DLTR-DOLLAR TREE INC-2016.pdf' -> 'DLTR'. Used as the consistent
    matching key across both folders, since the New folder has no ticker
    subfolders (flat structure) while the Old folder does."""
    return filename.split("-")[0].strip()


def list_all_pdfs_recursive(service, folder_id, ticker=None):
    """Recursively walk subfolders, return list of dicts with filename, md5Checksum,
    size. Ticker is always derived from the FILENAME, not folder nesting, so
    flat and nested folder structures compare consistently."""
    results = []
    query = f"'{folder_id}' in parents and trashed=false"
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, size)",
            pageToken=page_token,
        ).execute()
        for f in response.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                results.extend(list_all_pdfs_recursive(service, f["id"], ticker=f["name"]))
            elif f["name"].lower().endswith(".pdf"):
                results.append({
                    "ticker": extract_ticker_from_filename(f["name"]),
                    "filename": f["name"],
                    "md5": f.get("md5Checksum", ""),
                    "size": f.get("size", ""),
                })
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def main():
    print("Authenticating with Google Drive...")
    service = get_service()

    print(f"Locating '{OLD_FOLDER_NAME}'...")
    old_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, OLD_FOLDER_NAME)
    print(f"Locating '{NEW_FOLDER_NAME}'...")
    new_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, NEW_FOLDER_NAME)

    if not old_id or not new_id:
        print(f"ERROR: could not find one or both folders. old_id={old_id}, new_id={new_id}")
        return

    print("Listing all PDFs in OLD folder (this may take a minute)...")
    old_files = list_all_pdfs_recursive(service, old_id)
    print(f"  {len(old_files)} PDFs found")

    print("Listing all PDFs in NEW folder...")
    new_files = list_all_pdfs_recursive(service, new_id)
    print(f"  {len(new_files)} PDFs found")

    # Index by (ticker, filename) for matching, and separately by md5 for content comparison
    old_by_key = {(f["ticker"], f["filename"]): f for f in old_files}
    new_by_key = {(f["ticker"], f["filename"]): f for f in new_files}

    both_keys = set(old_by_key) & set(new_by_key)
    only_old_keys = set(old_by_key) - set(new_by_key)
    only_new_keys = set(new_by_key) - set(old_by_key)

    hash_match = []
    hash_mismatch = []
    for key in both_keys:
        old_f, new_f = old_by_key[key], new_by_key[key]
        if old_f["md5"] == new_f["md5"] and old_f["md5"] != "":
            hash_match.append(key)
        else:
            hash_mismatch.append((key, old_f["md5"], new_f["md5"]))

    print(f"\n{'=' * 60}")
    print("COMPARISON RESULTS")
    print("=" * 60)
    print(f"Same (ticker, filename), identical hash:     {len(hash_match)}")
    print(f"Same (ticker, filename), DIFFERENT hash:      {len(hash_mismatch)}")
    print(f"Only in OLD folder (missing from New):        {len(only_old_keys)}")
    print(f"Only in NEW folder (new additions):           {len(only_new_keys)}")

    if hash_mismatch:
        print(f"\nHASH MISMATCHES (same name, different content — needs review):")
        for (ticker, filename), old_md5, new_md5 in hash_mismatch[:20]:
            print(f"  {ticker}/{filename}: old={old_md5[:12]}... new={new_md5[:12]}...")

    # Save full detail to CSV
    rows = []
    for key in both_keys:
        ticker, filename = key
        status = "hash_match" if key in hash_match else "hash_mismatch"
        rows.append({"ticker": ticker, "filename": filename, "status": status,
                      "old_md5": old_by_key[key]["md5"], "new_md5": new_by_key[key]["md5"]})
    for key in only_old_keys:
        ticker, filename = key
        rows.append({"ticker": ticker, "filename": filename, "status": "only_in_old",
                      "old_md5": old_by_key[key]["md5"], "new_md5": ""})
    for key in only_new_keys:
        ticker, filename = key
        rows.append({"ticker": ticker, "filename": filename, "status": "only_in_new",
                      "old_md5": "", "new_md5": new_by_key[key]["md5"]})

    out_path = Path("reports/sustainability_folder_comparison.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "filename", "status", "old_md5", "new_md5"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nFull comparison saved to {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()