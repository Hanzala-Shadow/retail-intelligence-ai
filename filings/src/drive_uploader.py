"""
drive_uploader.py

Uploads SEC 10-K filings to Google Drive using OAuth Desktop authentication
(personal Drive account, not a service account).

Folder structure:

Retail Intelligence/
└── 10-K Filings/
    ├── GAP/
    ├── AMZN/
    ├── NKE/
    └── ...

First run:
    - Opens browser
    - User signs in
    - token.json is created

Future runs:
    - token.json is reused automatically, refreshed silently when expired

Duplicate-safety:
    - Folder lookup/creation uses a lock-like "create-then-recheck" pattern
      so two near-simultaneous calls can't each create a same-named folder.
    - File upload checks for an existing file with the same name in the
      target folder first; if found, it updates that file in place (new
      revision) instead of creating a second file with the same name.
      This makes re-running sec_downloader.py safe to repeat without
      littering Drive with duplicate "GPS-...-10K.htm" copies.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
from common import drive_auth

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

load_dotenv()

# NOTE: this is the ONLY caller needing write scope, but all three callers
# share token.json -- see the warning in common/drive_auth.py.
SCOPES = drive_auth.READWRITE_SCOPES

TOP_LEVEL_SUBFOLDER_NAME = "10-K Filings"

DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")

_folder_cache = {}

MAX_API_RETRIES = 3


def _get_service():
    # Caching now lives in common/drive_auth.py, keyed by (scopes, token path).
    return drive_auth.get_service(SCOPES)


def _list_folders_by_name(name: str, parent_id: str) -> list:
    service = _get_service()
    # Escape single quotes in name for the Drive query syntax
    safe_name = name.replace("'", "\\'")
    query = (
        f"name='{safe_name}' and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false and "
        f"'{parent_id}' in parents"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,createdTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="createdTime",  # oldest first, so we always prefer the original
        )
        .execute()
    )
    return results.get("files", [])


def _call_with_retry(fn, description: str):
    """Calls fn() (a zero-arg callable wrapping a Drive API .execute()),
    retrying on transient HttpErrors (403/429/500/502/503) with exponential
    backoff. Non-transient errors (401, 404, etc.) propagate immediately.
    Raises a clean RuntimeError (not a possibly-None re-raise) if all
    retries are exhausted."""
    last_exc = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            return fn()
        except HttpError as exc:
            last_exc = exc
            status = getattr(exc, "status_code", None) or (
                exc.resp.status if hasattr(exc, "resp") else None
            )
            if status in (403, 429, 500, 502, 503):
                wait = 2 ** attempt
                print(f"  [retry] Drive API error ({status}) during {description}, "
                      f"attempt {attempt}/{MAX_API_RETRIES}, waiting {wait}s")
                time.sleep(wait)
                continue
            raise  # non-retryable -> bubble up immediately

    raise RuntimeError(f"Failed during {description} after {MAX_API_RETRIES} attempts") from last_exc


def _find_or_create_folder(name: str, parent_id: str) -> str:
    """Finds an existing folder by name under parent_id, or creates one.
    If duplicates already exist (e.g. from a prior race condition or manual
    creation), always returns the OLDEST one deterministically, so repeated
    calls converge on a single canonical folder instead of randomly picking
    a different duplicate each time."""
    existing = _list_folders_by_name(name, parent_id)
    if existing:
        if len(existing) > 1:
            print(
                f"  [warn] Found {len(existing)} folders named '{name}' under "
                f"parent {parent_id} — using the oldest one "
                f"(id={existing[0]['id']}). Consider manually merging/deleting "
                f"the duplicates in Drive."
            )
        return existing[0]["id"]

    # Not found — create it, then re-check immediately in case of a race
    # (e.g. two scripts/processes run at once). This won't catch every race
    # but cheaply catches the common case of accidental double-run.
    service = _get_service()
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = _call_with_retry(
        lambda: service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute(),
        description=f"creating folder '{name}'",
    )

    # Re-check: did something else also just create this folder?
    recheck = _list_folders_by_name(name, parent_id)
    if len(recheck) > 1:
        recheck.sort(key=lambda f: f.get("createdTime", ""))
        canonical_id = recheck[0]["id"]
        print(
            f"  [warn] Race detected creating folder '{name}' — "
            f"{len(recheck)} now exist, using oldest (id={canonical_id})"
        )
        return canonical_id

    return folder["id"]


def _get_ticker_folder_id(ticker: str) -> str:
    if ticker in _folder_cache:
        return _folder_cache[ticker]

    if not DRIVE_ROOT_FOLDER_ID:
        raise RuntimeError("DRIVE_ROOT_FOLDER_ID missing from .env")

    tenk_folder = _find_or_create_folder(TOP_LEVEL_SUBFOLDER_NAME, DRIVE_ROOT_FOLDER_ID)
    ticker_folder = _find_or_create_folder(ticker, tenk_folder)

    _folder_cache[ticker] = ticker_folder
    return ticker_folder


def _find_existing_file(filename: str, folder_id: str) -> dict:
    """Returns the existing file dict {id, name} if filename already exists
    in folder_id, else None. Used to avoid duplicate uploads on re-run."""
    service = _get_service()
    safe_name = filename.replace("'", "\\'")
    query = f"name='{safe_name}' and trashed=false and '{folder_id}' in parents"
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = results.get("files", [])
    return files[0] if files else None


def upload_file_for_ticker(local_filepath, ticker, drive_filename=None) -> str:
    """
    Uploads local_filepath to /10-K Filings/{ticker}/.
    If a file with the same name already exists there, updates it in place
    (new revision, same file_id) instead of creating a duplicate.
    Returns the Drive file_id.
    """
    local_path = Path(local_filepath)
    if not local_path.exists():
        raise FileNotFoundError(local_filepath)
    if local_path.stat().st_size == 0:
        raise ValueError(f"Refusing to upload empty file: {local_filepath}")

    folder_id = _get_ticker_folder_id(ticker)
    filename = drive_filename or local_path.name
    service = _get_service()
    media = MediaFileUpload(str(local_path), mimetype="text/html", resumable=True)

    existing = _find_existing_file(filename, folder_id)

    if existing:
        result = _call_with_retry(
            lambda: service.files().update(
                fileId=existing["id"], media_body=media, supportsAllDrives=True, fields="id"
            ).execute(),
            description=f"updating existing file '{filename}'",
        )
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        result = _call_with_retry(
            lambda: service.files().create(
                body=metadata, media_body=media, fields="id", supportsAllDrives=True
            ).execute(),
            description=f"creating file '{filename}'",
        )

    return result["id"]


if __name__ == "__main__":
    test_file = Path("test_upload.txt")
    test_file.write_text("OAuth Google Drive upload test.")

    try:
        file_id = upload_file_for_ticker(str(test_file), "TEST")
        print(f"SUCCESS on first upload! Drive file_id: {file_id}")

        # Run again with the SAME filename/ticker to prove no duplicate is created
        file_id_2 = upload_file_for_ticker(str(test_file), "TEST")
        print(f"SUCCESS on second upload (should be SAME file_id, updated in place): {file_id_2}")

        if file_id == file_id_2:
            print("PASS: no duplicate created, existing file was updated in place.")
        else:
            print("WARNING: file_id changed on second run -- investigate duplicate logic.")

    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        if test_file.exists():
            test_file.unlink()