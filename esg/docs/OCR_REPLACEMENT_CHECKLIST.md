# OCR Replacement Checklist

Prepared 2026-07-24. Six searchable PDFs are verified and ready to replace their
scanned/corrupt originals on Drive. One staged file must **not** be used.

Policy reference: `docs/ESG_PIPELINE.md` lines 14 and 193-237 — replace the PDF
in Drive under the **same filename**; the raw filename stays the canonical
pipeline identity and `parse_source_*` records the real extractor input.

---

## Upload target

**Drive folder: `Sustainability Reports` / `<TICKER>` / **

Confirmed: this folder holds 123 ticker subfolders and the existing reports.
Do **not** use `Sustainability Reports New` — it is empty and is not what
`drive_downloader.py` reads (`SUSTAINABILITY_FOLDER_NAME = "Sustainability Reports"`).

Do **not** leave the replacements in `ESG_OCR_STAGING`. The downloader skips
that folder on purpose (`OTHER_SKIP_SUBFOLDERS`), so files there never reach the
pipeline.

---

## The 6 files to replace

The destination name must match the existing report **exactly**. Note the
`-OCR` suffix has to be **dropped** for CVS and SFM.

| # | Ticker | Upload this file | Rename to (exact) |
|---|---|---|---|
| 1 | CVS | `data/02_interim/esg_text_ocr/CVS/CVS-CVS HEALTH CORP-2019-OCR.pdf` | `CVS-CVS HEALTH CORP-2019.pdf` |
| 2 | CVS | `data/02_interim/esg_text_ocr/CVS/CVS-CVS HEALTH CORP-2020-OCR.pdf` | `CVS-CVS HEALTH CORP-2020.pdf` |
| 3 | SFM | `data/02_interim/esg_text_ocr/SFM/SFM-SPROUTS FARMERS MARKET-2017-OCR.pdf` | `SFM-SPROUTS FARMERS MARKET-2017.pdf` |
| 4 | NGVC | `ESG_OCR_STAGING/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf` | *(same name — no change)* |
| 5 | NGVC | `ESG_OCR_STAGING/NGVC/NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf` | *(same name — no change)* |
| 6 | SHOO | `ESG_OCR_STAGING/SHOO/SHOO-MADDEN STEVEN LTD-2021.pdf` | *(same name — no change)* |

Files 1-3 exist locally. Files 4-6 exist only on Drive (in staging) and can be
moved/copied within Drive — no download needed.

### Fingerprints, to confirm the right bytes landed

| Destination filename | Size (bytes) | SHA-256 |
|---|---|---|
| `CVS-CVS HEALTH CORP-2019.pdf` | 48,745,349 | `099a4cfae2235dbaa978b0a9e0d48418713147bc089690971502e9a0d9147ee2` |
| `CVS-CVS HEALTH CORP-2020.pdf` | 57,371,862 | `e9ed59c667e62a3966898fb5be0047ab96b71be9c8cc6c3fb322dc06cfe4f5b0` |
| `SFM-SPROUTS FARMERS MARKET-2017.pdf` | 2,261,233 | `f95528464639ab9acb6bd4282979c7983162344cf29cc3b8c8c980961ee01ce7` |
| `NGVC-NATURAL GROCERS VITAMIN CTGE-2021.pdf` | 17,109,447 | `779228818f79bfd984f80f95bd20ce4e5f64694dd45b44121b2b02ce09cb2771` |
| `NGVC-NATURAL GROCERS VITAMIN CTGE-2022.pdf` | 19,051,322 | `f4f12524dc7f384be57e23bc8c1fda1c1b84814f5575603c6762c95a9caf3fdd` |
| `SHOO-MADDEN STEVEN LTD-2021.pdf` | 41,303,998 | `279bd938d432944ea8a681d350e5087e0ae7095143007a9831f37c617231d16c` |

### Why each one is being replaced

| Destination filename | Original problem | After replacement |
|---|---|---|
| CVS 2019 | 112 pages, **0** text chars (pure scan) | 183,819 chars, `content_ratio` 0.997 |
| CVS 2020 | 127 pages, **0** text chars (pure scan) | 264,336 chars, `content_ratio` 0.998 |
| SFM 2017 | 2 pages, **0** text chars (pure scan) | 4,185 chars, `content_ratio` 0.992 |
| NGVC 2021 | 94% of pages near-empty, only 5,086 chars | 57,895 chars, flags cleared |
| NGVC 2022 | 92% of pages near-empty, only 4,951 chars | 57,284 chars, flags cleared |
| SHOO 2021 | broken font encoding, `readable_word_ratio` **0.0595** | ratio **0.8242**, flags cleared |

All six: page count matches the original exactly, and `quality_flags` come back
empty after replacement.

---

## DO NOT UPLOAD

**`ESG_OCR_STAGING/ETSY/ETSY-Etsy-2024.pdf`** — this is a regression, not a fix.

- Raw `ETSY-Etsy-2024.pdf` is already clean: 0 quality flags, 0/20 pages
  failing, `content_ratio` 0.996.
- The staged copy fails **18 of 20** pages and its char count doubled
  (66,774 -> 140,207), consistent with `ocr_pdf.py --pdf-base original` stacking
  a hidden OCR layer on top of an already-good text layer, so extraction returns
  both interleaved.
- Danger: it scores `readable_word_ratio` 0.7233 and produces **no**
  `quality_flags`, so `pdf_parser` would accept it silently. Only
  `detect_page_quality` catches it.

Keep the raw ETSY file. Delete the staged copy, or move it out of staging so it
is not mistaken for a pending replacement.

---

## After uploading

1. Re-run `src/drive_downloader.py` to pull the replacements into
   `data/01_raw/sustainability/<TICKER>/`.
2. Re-parse those six documents.
3. Confirm each parse-index row shows `status=parsed` with empty `quality_flags`,
   and that `parse_source_sha256` matches the table above.

---

## Still open (not part of this batch)

- **COLM 2019 and COLM 2020** carry the `garbled_text` flag
  (`readable_word_ratio` ~0.78) and have no OCR'd version anywhere. If OCR'd,
  use `--pdf-base image` — they have existing text layers that need *replacing*,
  not supplementing. That is the mistake that ruined the staged ETSY file.
- **Stale parse-index row** for `BJ-BJS WHSL CLUB HLDGS INC-2022.PDF`
  (uppercase extension). Its text/sections/chunks are absent from disk; once
  BJ 2022 is re-downloaded as lowercase `.pdf` a second row will appear
  alongside it, because the index keys on filename including extension.
- **Ratio gate** for near-total scans: `OCR_MIN_NONSPACE_CHARS = 500` is a
  whole-document threshold, so a 94%-blank doc like NGVC 2021 cleared it 10x
  over and was never flagged. `visual_only_pages` and `text_light_pages` are
  already written to the parse index but `esg_pipeline_qa.py` never reads them.
