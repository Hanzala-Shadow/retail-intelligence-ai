# ESG Staging Audit — Defect Log

Tracks every defect found during the Week 4 Tuesday-Wednesday joint ESG staging audit
(Aziz + Suleyman), per the plan's 6-dimension checklist. Every fix is logged here with
before/after evidence. This log feeds directly into Thursday's ESG Database Audit Report.

**Data source discipline:** Aziz's pipeline is actively being re-run this week, so the
underlying data changes day to day. Every entry below states exactly which dataset/export
it was found against, so findings remain traceable even as the corpus evolves.

## Audit dimensions (per plan)
1. Extraction quality on sample pages
2. Section split accuracy
3. Chunk coherence
4. Dedup
5. Token distribution vs 50-token minimum
6. Image-heavy page handling

---

## Log entries

| # | Date | Data Source | Company | Dimension | Defect Found | Fix Applied | Status |
|---|------|-------------|---------|-----------|---------------|-------------|--------|

| 1 | 2026-07-21 | Suleyman's own Monday ingestion run (`data/02_interim/esg_text/PAG`, parsed locally via `pdf_parser.py`) | PAG (2020 report) | 1. Extraction quality | `check_garbled_text.py` heuristic flagged this file as GARBLED. Root cause: table-of-contents page (first ~500 chars) uses dot-leader formatting (`.......`), which drags the letter-density ratio below the 0.5 threshold, triggering a false positive. Manually verified chars 3000-3500 and 20000-20500 are fully readable, real content — confirmed NOT actually garbled. | No fix needed to source data. Detector limitation noted: garbled-text heuristic should sample from later in the document (e.g. char 3000+) rather than the first 500 chars, to avoid TOC dot-leaders triggering false positives. Manually cleared PAG 2020 as OK. | RESOLVED (false positive) |

| 2 | 2026-07-21 → 2026-07-22 | Initially found in `esg_local_audit_2026-07-13.sqlite` (Aziz's Week 3 audit export); re-confirmed still present in `05_db.zip` (Aziz's Week 4 Day 2 chunk export, sent 2026-07-22) | APC (invalid ticker, should not exist) | 4. Dedup | MAJOR: chunks exist under ticker "APC" (pdf_stem: "APC-ARKO PETROLEUM CORP-{2021,2022,2023,2024}") — 201 chunks in the Week 3 export, 234 chunks in the Week 4 Day 2 export (grew as Aziz's pipeline re-ran and added more data, but the duplicate persisted across both). APC does not appear in companies.csv (194-company universe) — only ARKO does. Confirmed via full text-hash comparison in both exports: all APC chunks are 100% byte-for-byte identical to the corresponding ARKO chunks (same year, section_code, chunk_index) — 0 chunks unique to either side. Root cause: same source PDF ingested under two ticker labels (APC + ARKO), likely a leftover source folder in Google Drive predating a ticker correction. | AGREED FIX (2026-07-22, with Aziz): remove the APC source folder from Google Drive entirely, keep ARKO as canonical. Aziz will re-run the ESG pipeline after removal — APC chunks should not regenerate since the source folder will no longer exist. Suleyman to re-verify APC is gone from the next chunk export after Aziz's re-run completes. | FIX AGREED — pending Aziz's Drive cleanup + pipeline re-run, re-verification scheduled |

| 3 | 2026-07-22 | `05_db.zip` (Aziz's Week 4 Day 2 chunk export, 39,640 total chunks / 126 companies) | BURL (2021 report) | 2. Section split accuracy | MODERATE: BURL-Burlington Stores-2021 falls back to full_document (3 chunks) instead of proper section splitting. Root cause identified by reading the actual source text: the ingested "2021 CSR report" is not the report itself — it's a short press release (GLOBE NEWSWIRE, dated Aug 22 2022) *announcing* the report's release. A 1-2 paragraph news announcement naturally has no Environmental/Social/Governance chapter structure, so section detection correctly found nothing to split on. This is a wrong-source-file issue, not a section splitter bug. | Flagged to Aziz/Naz (Drive tracker owner) — needs the actual BURL 2021 CSR report PDF re-sourced from Drive/company site, not the press release. Current 3 chunks should be excluded from RAG indexing until the real report is ingested. | OPEN — needs correct source PDF |

| 4 | 2026-07-22 | `05_db.zip` (Aziz's Week 4 Day 2 chunk export) | TDUP (2026-RESALE), ETSY (2025-PAYGAP) | 2. Section split accuracy | MINOR/EXPECTED: Both fall back to full_document (1 and 5 chunks respectively). These are not standard annual sustainability reports — file naming indicates TDUP is a "Resale" business-specific report and ETSY is a UK-style Gender Pay Gap disclosure. Both are short, narrowly-scoped supplementary documents that would not be expected to follow the standard Environmental/Social/Governance chapter structure our splitter looks for. | No fix needed — this is expected behavior for non-standard document types, not a defect. Documenting for the audit report so the 12-point QA checklist correctly distinguishes "expected fallback" from "real defect." | RESOLVED (expected behavior, not a defect) |

| 5 | 2026-07-22 | `05_db.zip` (Aziz's Week 4 Day 2 chunk export, 39,640 total chunks / 126 companies) | Whole ESG corpus | 2. Section split accuracy (summary) | Overall fallback rate: 9 chunks out of 39,640 total (0.02%) across 3 documents out of ~470+ PDFs. Of the 3 fallback documents: 1 is a real defect (BURL, wrong source file), 2 are expected (non-standard document types). | Section splitting is performing very well corpus-wide. Only BURL requires action. | SUMMARY — 1 real defect (BURL), 2 expected non-issues |

---

## Snapshot history (for traceability)

| Snapshot | Date received | Source | Scope |
|----------|---------------|--------|-------|
| `esg_local_audit_2026-07-13.sqlite` | Week 3, Day 1 | Aziz | 121 companies, 33,042 chunks (summary/QA tables only) |
| `05_db.zip` | 2026-07-22 (Week 4, Day 2) | Aziz | 126 companies, 39,640 chunks (full chunk text files) — mid-rerun snapshot, pipeline being actively updated to fix Drive/local file mismatches |
