#!/usr/bin/env python3
"""Convert and validate the frozen historical v3 manifest for Linux ingestion."""
from __future__ import annotations

import argparse, csv, hashlib, json, re
from collections import Counter
from pathlib import Path, PureWindowsPath

OUT = [
    "company_id","ticker","cik","coverage_year","filing_year","filing_date",
    "accession_number","report_date","dei_fiscal_year_focus","form_type",
    "source_file","source_sha256","fiscal_year_source","resolution_confidence",
    "resolution_evidence","selection_status",
]

def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()

def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--v3-manifest",type=Path,required=True)
    ap.add_argument("--companies",type=Path,required=True)
    ap.add_argument("--raw-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    with args.companies.open(encoding="utf-8-sig",newline="") as f:
        companies={r["ticker"].upper():r for r in csv.DictReader(f)}
    with args.v3_manifest.open(encoding="utf-8-sig",newline="") as f:
        source=list(csv.DictReader(f))
    if len(source)!=1752 or Counter(r["selection_status"] for r in source)!={"SELECTED":1743,"MISSING":9}:
        raise RuntimeError("v3 manifest frozen counts failed")
    output=[]; failures=[]
    for r in source:
        if r["selection_status"]!="SELECTED": continue
        ticker=r["ticker"].upper(); company=companies.get(ticker)
        if not company: failures.append(f"unapproved ticker {ticker}"); continue
        if str(int(company["cik"]))!=str(int(r["cik"])):
            failures.append(f"CIK mismatch {ticker}"); continue
        candidates=list((args.raw_root/ticker/f"FY{r['coverage_year']}").glob(f"*{r['accession_number']}*.htm*"))
        if len(candidates)!=1:
            failures.append(f"{ticker} FY{r['coverage_year']} source matches={len(candidates)}"); continue
        path=candidates[0]; actual=digest(path)
        if actual.lower()!=r["sha256"].lower():
            failures.append(f"source hash mismatch {r['accession_number']}"); continue
        evidence=r["resolution_evidence"].strip()
        try: json.loads(evidence)
        except Exception: evidence=json.dumps({"text":evidence})
        output.append({
            "company_id":company["company_id"],"ticker":ticker,"cik":company["cik"],
            "coverage_year":r["coverage_year"],"filing_year":r["filing_year"],
            "filing_date":r["filing_date"],"accession_number":r["accession_number"],
            "report_date":r["report_date"],"dei_fiscal_year_focus":r["dei_fiscal_year_focus"],
            "form_type":"10-K","source_file":str(path.relative_to(args.raw_root)),
            "source_sha256":actual,"fiscal_year_source":r["fiscal_year_source"],
            "resolution_confidence":r["resolution_confidence"],
            "resolution_evidence":evidence,"selection_status":"selected",
        })
    if failures: raise RuntimeError("\n".join(failures[:30]))
    if len(output)!=1743 or len({r['accession_number'] for r in output})!=1743 or len({(r['ticker'],r['coverage_year']) for r in output})!=1743:
        raise RuntimeError("selected identity gates failed")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=OUT); w.writeheader(); w.writerows(output)
    report={"status":"PASS","rows":len(output),"years":dict(sorted(Counter(r['coverage_year'] for r in output).items())),"manifest_sha256":digest(args.output)}
    args.output.with_suffix(".audit.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
