"""
count_drive_esg_pdfs.py

Counts every PDF file that exists in the Google Drive "Sustainability Reports"
folder tree (all company subfolders), regardless of whether it's been
downloaded locally or ingested into the staging DB.

Answers Aziz's question: how many PDFs actually exist in Drive vs. how many
we have locally / in the DB.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")


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


def list_all_pdfs_recursive(service, folder_id, path=""):
    """Recursively walk all subfolders, return list of (company_folder, filename)."""
    results = []
    query = f"'{folder_id}' in parents and trashed=false"
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
        ).execute()
        for f in response.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                results.extend(list_all_pdfs_recursive(service, f["id"], path + "/" + f["name"]))
            elif f["name"].lower().endswith(".pdf"):
                company = path.strip("/").split("/")[-1] if path else "ROOT"
                results.append((company, f["name"]))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def main():
    print("Authenticating with Google Drive...")
    service = get_service()

    print("Locating 'Sustainability Reports' folder...")
    sustainability_folder_id = find_subfolder(service, DRIVE_ROOT_FOLDER_ID, "Sustainability Reports")
    if not sustainability_folder_id:
        print("ERROR: Could not find 'Sustainability Reports' folder under DRIVE_ROOT_FOLDER_ID")
        return

    print("Counting all PDFs (this may take a minute)...")
    all_pdfs = list_all_pdfs_recursive(service, sustainability_folder_id)

    print(f"\nTotal PDFs in Drive: {len(all_pdfs)}")

    from collections import defaultdict
    per_company = defaultdict(int)
    for company, filename in all_pdfs:
        per_company[company] += 1

    print(f"Total companies with at least 1 PDF: {len(per_company)}")
    print(f"\nPer-company counts:")
    for company, count in sorted(per_company.items()):
        print(f"  {company}: {count}")

    # Save to CSV for reference
    import csv
    with open("reports/drive_esg_pdf_inventory.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "filename"])
        writer.writerows(all_pdfs)
    print(f"\nFull inventory saved to reports/drive_esg_pdf_inventory.csv")


if __name__ == "__main__":
    main()
