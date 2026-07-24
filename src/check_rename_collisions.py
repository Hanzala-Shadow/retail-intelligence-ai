import pandas as pd
import re

investigation = pd.read_csv('reports/hash_mismatch_investigation.csv')
comparison = pd.read_csv('reports/sustainability_folder_comparison.csv')
all_existing_filenames = set(comparison['filename'])


def detect_year(text):
    fy_match = re.search(r'FY(\d{2})', text)
    if fy_match:
        return 2000 + int(fy_match.group(1))
    year_match = re.search(r'(20\d{2})', text)
    if year_match:
        return int(year_match.group(1))
    return None


rows = []
print(f"{'Ticker':<8}{'Old_year':<10}{'New_year':<10}{'Proposed_old_name':<45}{'Proposed_new_name':<45}Collision?")
for _, row in investigation.iterrows():
    old_year = detect_year(str(row['old_first_page_snippet']))
    new_year = detect_year(str(row['new_first_page_snippet']))
    ticker = row['ticker']
    parts = row['filename'].rsplit('-', 1)
    company_part = parts[0].split('-', 1)[1] if '-' in parts[0] else ticker

    old_new_name = f"{ticker}-{company_part}-{old_year}.pdf" if old_year else "UNKNOWN"
    new_new_name = f"{ticker}-{company_part}-{new_year}.pdf" if new_year else "UNKNOWN"

    old_collision = old_new_name in all_existing_filenames and old_new_name != row['filename']
    new_collision = new_new_name in all_existing_filenames and new_new_name != row['filename']
    flags = []
    if old_collision:
        flags.append("OLD collides")
    if new_collision:
        flags.append("NEW collides")
    if old_new_name == new_new_name:
        flags.append("OLD/NEW SAME NAME")

    flag_str = ", ".join(flags) if flags else "none"
    print(f"{ticker:<8}{str(old_year):<10}{str(new_year):<10}{old_new_name:<45}{new_new_name:<45}{flag_str}")

    rows.append({
        "ticker": ticker, "original_filename": row['filename'],
        "old_detected_year": old_year, "new_detected_year": new_year,
        "proposed_old_name": old_new_name, "proposed_new_name": new_new_name,
        "collision_flags": flag_str,
    })

pd.DataFrame(rows).to_csv('reports/rename_proposal.csv', index=False)
print("\nSaved to reports/rename_proposal.csv")