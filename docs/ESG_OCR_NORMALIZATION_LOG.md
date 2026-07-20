# ESG OCR Normalization Log

Date: 2026-07-14; updated 2026-07-15

Purpose: document PDFs whose raw local file was replaced by a searchable PDF with an OCR text layer. After this normalization, the ESG parser reads these files from `data/01_raw/sustainability/...` as normal PDFs. No `--ocr-root` sidecar mode is required for these files.

Important implementation note: for corrupted embedded-text PDFs, we used image-base OCR. That means the visible PDF page is preserved as a page image, and a clean hidden OCR text layer is attached. This avoids old `(cid:...)` text layers contaminating extraction.

Manifest: `data/00_reference/esg_ocr_normalization_manifest.csv`
Original raw backups: `backups/ocr_raw_originals_20260714_1115/` and `backups/ocr_raw_originals_20260715_070401/`

## Normalized PDFs

### WMT - WMT-WALMART INC-2024.pdf
- Problem before: zero extracted text / OCR-required before repair.
- Method: original-base OCR searchable PDF earlier; normalized raw copy now uses same searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/WMT/WMT-WALMART INC-2024.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/WMT/WMT-WALMART INC-2024.pdf`.
- Parser after normalization: `raw` / `parsed`; 479,075 chars; 132 pages.
- Output after normalization: 17 sections, 216 chunks, 0 missing citation chunks, QA `no_tracker_row_for_this_pdf`.
- QA note: Pipeline output exists, but sustainability_report_tracker.csv has no WMT 2024 row.

### ETSY - ETSY-ETSY INC-2024.pdf
- Problem before: CID-garbled embedded text / fallback risk before repair.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/ETSY/ETSY-ETSY INC-2024.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/ETSY/ETSY-ETSY INC-2024.pdf`.
- Parser after normalization: `raw` / `parsed`; 78,740 chars; 20 pages.
- Output after normalization: 14 sections, 41 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### AAPL - AAPL-APPLE INC-2024.pdf
- Problem before: CID-garbled text anomaly.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/AAPL/AAPL-APPLE INC-2024.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/AAPL/AAPL-APPLE INC-2024.pdf`.
- Parser after normalization: `raw` / `parsed`; 433,421 chars; 126 pages.
- Output after normalization: 15 sections, 211 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### MOV - MOV-MOVADO GROUP INC-2022.pdf
- Problem before: CID-garbled text anomaly.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/MOV/MOV-MOVADO GROUP INC-2022.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/MOV/MOV-MOVADO GROUP INC-2022.pdf`.
- Parser after normalization: `raw` / `parsed`; 126,116 chars; 68 pages.
- Output after normalization: 12 sections, 62 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### SBH - SBH-SALLY BEAUTY HOLDINGS INC-2024.pdf
- Problem before: CID-garbled text anomaly.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/SBH/SBH-SALLY BEAUTY HOLDINGS INC-2024.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/SBH/SBH-SALLY BEAUTY HOLDINGS INC-2024.pdf`.
- Parser after normalization: `raw` / `parsed`; 35,184 chars; 11 pages.
- Output after normalization: 9 sections, 19 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### PTRN - PTRN-Pattern Group-2023.pdf
- Problem before: garbled_text parser flag.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/PTRN/PTRN-Pattern Group-2023.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/PTRN/PTRN-Pattern Group-2023.pdf`.
- Parser after normalization: `raw` / `parsed`; 242,895 chars; 140 pages.
- Output after normalization: 17 sections, 130 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### VSXY - VSXY-Victorias Secret-2024.pdf
- Problem before: garbled_text parser flag.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/VSXY/VSXY-Victorias Secret-2024.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260714_1115/VSXY/VSXY-Victorias Secret-2024.pdf`.
- Parser after normalization: `raw` / `parsed`; 85,446 chars; 45 pages.
- Output after normalization: 7 sections, 42 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### NGVC - NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf
- Problem before: low text per page / image-like PDF before repair.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260715_070401/data/01_raw/sustainability/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf`.
- Parser after normalization: `raw` / `parsed`; 67,886 chars; 33 pages; readable word ratio 0.8279; 0 garbled chars.
- Output after normalization: 27 sections, 43 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### NGVC - NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf
- Problem before: low text per page / image-like PDF before repair.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260715_070401/data/01_raw/sustainability/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf`.
- Parser after normalization: `raw` / `parsed`; 67,246 chars; 37 pages; readable word ratio 0.8296; 0 garbled chars.
- Output after normalization: 28 sections, 42 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 

### SHOO - SHOO-MADDEN STEVEN LTD-2021.pdf
- Problem before: cipher/gibberish embedded text / low readable word ratio before repair.
- Method: image-base OCR searchable PDF.
- Current raw PDF: `data/01_raw/sustainability/SHOO/SHOO-MADDEN STEVEN LTD-2021.pdf`.
- Original raw backup: `backups/ocr_raw_originals_20260715_070401/data/01_raw/sustainability/SHOO/SHOO-MADDEN STEVEN LTD-2021.pdf`.
- Parser after normalization: `raw` / `parsed`; 76,675 chars; 57 pages; readable word ratio 0.8234; 0 garbled chars.
- Output after normalization: 26 sections, 45 chunks, 0 missing citation chunks, QA `complete`.
- QA note: 


## Pre-Existing TDUP Searchable PDFs Added To Tracking

These TDUP files were already present in the raw corpus as searchable PDFs before the 2026-07-14 raw-PDF normalization pass. They parse as normal raw PDFs with no `--ocr-root` sidecar. Because they predate the new normalization manifest, no pre-OCR local backup is available in `backups/ocr_raw_originals_20260714_1115/`.

### TDUP - TDUP-THREDUP INC-2023.pdf
- Current raw PDF: `data/01_raw/sustainability/TDUP/TDUP-THREDUP INC-2023.pdf`.
- Backup status: `not_available_pre_manifest`.
- Parser after tracking: `raw` / `parsed`; 36,702 chars; 48 pages.
- Output after tracking: 10 sections, 20 chunks, 0 missing citation chunks, QA `complete`.

### TDUP - TDUP-THREDUP INC-2024.pdf
- Current raw PDF: `data/01_raw/sustainability/TDUP/TDUP-THREDUP INC-2024.pdf`.
- Backup status: `not_available_pre_manifest`.
- Parser after tracking: `raw` / `parsed`; 33,506 chars; 44 pages.
- Output after tracking: 8 sections, 17 chunks, 0 missing citation chunks, QA `complete`.

## Verification Commands Run

- Parser rerun without `--ocr-root`; each normalized PDF reported `ocr_sources_selected: 0`.
- Section splitter and chunker rerun for WMT, ETSY, AAPL, MOV, SBH, PTRN, and VSXY.
- Parser, section splitter, and chunker rerun for NGVC 2021, NGVC 2022, and SHOO 2021.
- `python scripts/validate_esg_provenance.py --parse-index data/00_reference/esg_parse_index.csv --sections-index data/00_reference/esg_sections_index.csv --chunks-index data/00_reference/esg_chunks_index.csv --json-out reports/esg_provenance_validation_after_ngvc_shoo_ocr.json`
- `python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv`
- `python src/drive_to_db.py --dry-run`
- `python scripts/esg_current_quality_audit.py`
