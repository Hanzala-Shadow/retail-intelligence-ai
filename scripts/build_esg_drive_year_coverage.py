"""Year-by-year coverage report for the Drive ESG corpus.

Reads BOTH Drive manifests and treats them as one pool:
  - esg_drive_manifest.csv        -> data/01_raw/sustainability        (full reports)
  - esg_drive_manifest_other.csv  -> data/01_raw/sustainability_other  (supplements)

Deduplication runs across the union, in two passes, because the same Drive file
can appear up to three times:
  1. by `drive_file_id` -- one file logged under an old and a new name after a
     Drive-side rename. Keep the latest `drive_modified_time`.
  2. by `drive_md5_checksum` -- byte-identical copies stored under different file
     ids. Prefer the supplemental-folder row, whose name carries the curated
     classification (`...-2025-CLIMATE` beats a bare `...-2025`).

Both passes matter for classification, not just counting: LEVI 2023/2024 and
ETSY/SFM 2025 are listed under plain report names in the main manifest but are
the *same Drive files* as the `-metric&goals` / `-CLIMATE` / `-PROGRESSREPORT`
supplements. Counting the main manifest alone credits those companies with full
reports they do not have.

report_year = max(year tokens in the filename), matching src/esg_p1_enrichment.py,
because the corpus uses both `2021-2022` and `2021-2020` orderings.
"""

import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

OUT = config.ESG_DRIVE_YEAR_COVERAGE_DIR

YEAR = re.compile(r"(?:19|20)\d{2}")
YEAR_TAIL = re.compile(r"-(?:19|20)\d{2}(?:-(?:19|20)\d{2})?(.*)$")

# A suffix after the year that marks a partial/topic document rather than the
# company's standalone annual ESG report.
SUPPLEMENT_TAIL = re.compile(
    r"climate|paygap|pay.?gap|sasb|index|metric|goal|progress|resale|charter|proxy",
    re.IGNORECASE,
)


def load(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def year_tail(name):
    """Whatever follows the year token in the filename stem."""
    m = YEAR_TAIL.search(os.path.splitext(name)[0])
    return m.group(1).strip(" -_") if m else ""


def dedupe(rows):
    """Collapse renames, then byte-duplicates, across the whole pool."""
    by_id = defaultdict(list)
    for r in rows:
        by_id[r["drive_file_id"]].append(r)
    uniq = [
        max(v, key=lambda r: (r["drive_modified_time"], r["updated_at_utc"]))
        for v in by_id.values()
    ]

    by_md5 = defaultdict(list)
    for r in uniq:
        by_md5[r["drive_md5_checksum"]].append(r)

    kept, dropped = [], []
    for group in by_md5.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # A curated supplement name beats a bare report name for the same bytes.
        group.sort(key=lambda r: (not r["is_supplemental_folder"],
                                  not SUPPLEMENT_TAIL.search(year_tail(r["drive_file_name"])),
                                  len(r["drive_file_name"])))
        kept.append(group[0])
        dropped.extend(group[1:])
    return kept, dropped


def classify(row):
    if row["is_supplemental_folder"]:
        return "supplement"
    if SUPPLEMENT_TAIL.search(year_tail(row["drive_file_name"])):
        return "supplement"
    return "full_report"


def main():
    os.makedirs(OUT, exist_ok=True)

    pool = []
    for path, is_other in ((config.ESG_DRIVE_MANIFEST_CSV, False),
                           (config.REFERENCE_DIR / "esg_drive_manifest_other.csv", True)):
        for r in load(path):
            r["is_supplemental_folder"] = is_other
            pool.append(r)

    kept, dropped = dedupe(pool)

    universe = load(config.COMPANIES_CSV)
    names = {}
    for r in universe:
        names.setdefault(r["ticker"], r["name"].strip())
    banned = {r["ticker"] for r in load(config.BANNED_COMPANIES_CSV)}

    docs, unresolved = [], []
    for r in kept:
        years = sorted({int(y) for y in YEAR.findall(os.path.splitext(r["drive_file_name"])[0])})
        if not years:
            unresolved.append(r)
            continue
        tail = year_tail(r["drive_file_name"])
        docs.append({
            "report_year": max(years),
            "ticker": r["ticker"],
            "company": names.get(r["ticker"], ""),
            "doc_class": classify(r),
            "year_span": "-".join(str(y) for y in years) if len(years) > 1 else str(years[0]),
            "is_span": len(years) > 1,
            "note": tail if tail and not tail.lower().startswith(("report", "pdf")) else "",
            "file_name": r["drive_file_name"],
            "on_disk": os.path.exists(r["local_file"]),
            "drive_file_id": r["drive_file_id"],
        })

    docs.sort(key=lambda d: (d["report_year"], d["ticker"]))
    full = [d for d in docs if d["doc_class"] == "full_report"]
    supp = [d for d in docs if d["doc_class"] == "supplement"]

    # ---- per-document long table -------------------------------------------
    fields = ["report_year", "ticker", "company", "doc_class", "year_span",
              "is_span", "note", "file_name", "on_disk", "drive_file_id"]
    with open(OUT / "esg_drive_documents_by_year.csv", "w", encoding="utf-8-sig",
              newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for d in docs:
            w.writerow({k: d[k] for k in fields})

    # ---- company x year matrix (full reports) -------------------------------
    years = sorted({d["report_year"] for d in full})
    grid = defaultdict(int)
    for d in full:
        grid[(d["ticker"], d["report_year"])] += 1
    covered = sorted({d["ticker"] for d in full})

    with open(OUT / "esg_drive_company_year_matrix.csv", "w", encoding="utf-8-sig",
              newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "company", "n_reports", "first_year", "last_year",
                    "n_supplements"] + [str(y) for y in years])
        for t in covered:
            ys = [d["report_year"] for d in full if d["ticker"] == t]
            w.writerow([t, names.get(t, ""), len(ys), min(ys), max(ys),
                        sum(1 for d in supp if d["ticker"] == t)]
                       + [grid[(t, y)] or "" for y in years])
        for t in sorted({r["ticker"] for r in universe} - set(covered)):
            w.writerow([t, names.get(t, ""), 0, "", "", 0] + ["" for _ in years])

    # ---- per-year company lists (markdown) ---------------------------------
    by_year = defaultdict(list)
    for d in full:
        by_year[d["report_year"]].append(d)

    no_report = sorted({r["ticker"] for r in universe} - set(covered))
    L = ["# ESG report coverage by year - Drive `Sustainability Reports`", ""]
    L.append(f"{len(full)} standalone reports - {len(covered)} of {len(universe)} "
             f"companies - {years[0]}-{years[-1]}")
    L.append("")
    L.append("Year = the latest year in the filename (the fiscal year covered). "
             "`+` marks a filename carrying a two-year span, counted under the "
             "later year. Topic supplements (climate index, pay gap, SASB index, "
             "metrics & goals) are listed separately at the end, not here.")
    L.append("")
    for y in years:
        ds = sorted(by_year[y], key=lambda d: d["ticker"])
        L.append(f"## {y} - {len(ds)} companies")
        L.append("")
        for d in ds:
            span = f" ({d['year_span']})" if d["is_span"] else ""
            mark = " +" if d["is_span"] else ""
            note = f" - _{d['note']}_" if d["note"] else ""
            L.append(f"- **{d['ticker']}** - {d['company']}{span}{mark}{note}")
        L.append("")

    L.append(f"## Topic supplements - {len(supp)} documents")
    L.append("")
    L.append("Partial documents, not standalone annual reports.")
    L.append("")
    for d in sorted(supp, key=lambda d: (d["report_year"], d["ticker"])):
        L.append(f"- **{d['ticker']}** {d['report_year']} - {d['file_name']}")
    L.append("")

    L.append(f"## No ESG report in Drive - {len(no_report)} companies")
    L.append("")
    for t in no_report:
        L.append(f"- **{t}** - {names.get(t, '')}")
    L.append("")

    with open(OUT / "esg_drive_year_coverage.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    # ---- console summary ----------------------------------------------------
    print(f"pool rows: {len(pool)}  ->  distinct Drive files: {len(kept) + len(dropped)}"
          f"  ->  after byte-dedupe: {len(docs)}")
    print(f"full reports: {len(full)}   supplements: {len(supp)}")
    print(f"companies with >=1 report: {len(covered)} / {len(universe)}"
          f"   without: {len(no_report)}")
    print(f"unresolved year: {len(unresolved)}   "
          f"banned present: {sorted({d['ticker'] for d in docs if d['ticker'] in banned})}")
    print(f"in Drive but not on local disk: {sum(1 for d in docs if not d['on_disk'])}")
    print("\ndropped as byte-duplicates:")
    for d in dropped:
        print(f"   {d['ticker']:<6} {d['drive_file_name']}")
    print("\nyear  companies  reports")
    for y in years:
        print(f"{y}   {len({d['ticker'] for d in by_year[y]}):>6}   {len(by_year[y]):>6}")


if __name__ == "__main__":
    main()
