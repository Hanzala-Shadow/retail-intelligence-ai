# ESG Document Intelligence Pipeline

## Purpose

The ESG pipeline converts downloaded sustainability report PDFs into parsed text, canonical ESG sections, token-bounded chunks, QA evidence, and PostgreSQL-ready records. It is separate from the existing 10-K pipeline and writes only ESG-specific outputs.

## Inputs

- `data/00_reference/companies.csv`
- `data/00_reference/sustainability_report_tracker.csv`
- `data/01_raw/sustainability/{ticker}/*.pdf`

Downloaded PDFs are expected to come from `src/drive_downloader.py`.
If a report needed OCR, replace the Drive PDF with the searchable version using
the same filename. After download, the parser reads that searchable PDF directly
from `data/01_raw/sustainability/{ticker}/`.

## Outputs

- `data/02_interim/esg_text/{ticker}/{pdf_stem}.txt`
- `data/02_interim/esg_text/{ticker}/{pdf_stem}.pages.csv`
- `data/03_sections/esg/{ticker}/{pdf_stem}__{section_code}.txt`
- `data/04_chunks/esg/{ticker}/{pdf_stem}__{section_code}__chunk_{0000}.txt`
- `data/00_reference/esg_parse_index.csv`
- `data/00_reference/esg_sections_index.csv`
- `data/00_reference/esg_chunks_index.csv`
- `data/00_reference/esg_pipeline_qa.csv`

The ESG scripts do not overwrite 10-K paths such as `data/03_sections/10k`, `data/04_chunks/10k`, `sections_index.csv`, or `chunks_index.csv`.

## Script Order

```bash
python src/drive_downloader.py --dry-run
python src/drive_downloader.py
python src/pdf_parser.py --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
python src/drive_to_db.py --commit
```

## Resume / Continue Protocol

The ESG stages are safe to restart after an SSH/EC2/network interruption. Resume
mode is the default, and it can also be stated explicitly with `--resume`. A
stage skips an item only when its output files exist **and** its index rows
match the current source fingerprint. Changed source files, including
replacement searchable TDUP PDFs and corrected ETSY reports downloaded from
Drive with the same filenames, automatically run again. Existing 10-K outputs
are not read, rewritten, or deleted by these commands.

Every stage writes its individual output through a `.tmp` file followed by an
atomic replace, and checkpoints its index after each completed input by default.
The source metadata recorded in the ESG indexes includes size, UTC modification
time, and SHA-256 where applicable. This makes a stale index recoverable just by
rerunning the same command.

If a re-split report no longer produces a prior section code, the chunker
removes only that orphaned `ticker + pdf_stem + section_code` group's chunks and
index rows during resume. It never clears another section or an entire ticker.

Use the following sequence on EC2:

```bash
python src/drive_downloader.py --resume
python src/pdf_parser.py --resume --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --resume --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --resume --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
```

`--checkpoint-every N` changes the checkpoint cadence; the safe default is
`--checkpoint-every 1`. `--force` intentionally rebuilds the selected scope,
even if source fingerprints match. Use it for a known-bad output or after
changing parser/splitter/chunker behavior:

```bash
python src/pdf_parser.py --ticker TDUP --force
python src/pdf_parser.py --ticker ETSY --force
python src/section_splitter_esg.py --ticker TDUP --force
python src/section_splitter_esg.py --ticker ETSY --force
python src/esg_chunker.py --ticker TDUP --force
python src/esg_chunker.py --ticker ETSY --force
```

The downloader keeps a checkpointed manifest at
`data/00_reference/esg_drive_manifest.csv`. It skips existing PDFs only when
they are non-empty and agree with the size and, when available, MD5 checksum
reported by Drive. A zero-byte, size-mismatched, or checksum-mismatched local
PDF is downloaded again. `--force` redownloads its scope.

### Searchable PDF replacements

`pdf_parser.py` does not perform OCR itself. If a scanned report needs OCR,
create a searchable PDF and replace the PDF in Drive using the same filename.
The downloader then places the searchable PDF at the normal raw path:
`data/01_raw/sustainability/{ticker}/{raw_pdf_filename}`. The parser reads that
file directly, so there is no TDUP-specific exception and no separate OCR folder
in the normal pipeline.

The parser still has an optional `--ocr-root` escape hatch for local recovery
work. When explicitly supplied, it can parse a matching sidecar PDF from
`{ocr_root}/{ticker}/{raw_pdf_filename}` or `{ocr_root}/{ticker}/{raw_pdf_stem}_ocr.pdf`.
Do not use that mode for the shared Drive run unless the team intentionally
keeps OCR sidecars outside the raw PDF folder.

The raw downloaded PDF remains the canonical pipeline identity: `pdf_file`,
`source_pdf`, text/page-map output names, and downstream section/chunk/DB stems
all continue to use the raw filename. The parse index records the actual
extractor input separately in `parse_source_*` fields, including its own size,
mtime, and SHA-256 fingerprint.

Rows written before `parse_source_*` existed are intentionally stale on their
next parser run, so the parser can establish unambiguous raw-versus-OCR
provenance. Later resumes skip only when both fingerprints still match.

Build searchable OCR PDFs with `src/ocr_pdf.py`. The default searchable layer is
`--pdf-text-mode ordered`, which writes the hidden PDF text in the same cleaned
Tesseract reading order used for the sidecar `.txt`. This is preferred for
copy/search/extraction quality, especially on two-column ESG report pages:

```bash
python src/ocr_pdf.py --input "data/01_raw/sustainability/TDUP/TDUP-THREDUP INC-2023.pdf" --output-text "data/02_interim/esg_text/TDUP/TDUP-THREDUP INC-2023.txt" --output-pdf "data/02_interim/ocr_staging/TDUP/TDUP-THREDUP INC-2023.pdf" --pdf-text-mode ordered
```

Upload the staged searchable PDF back to Drive as the replacement for the
original report, keeping exactly the same filename.

After replacing a Drive PDF, rerun the downloader and parser for that ticker:

```bash
python src/drive_downloader.py --ticker TDUP --resume
python src/pdf_parser.py --ticker TDUP --resume
```

Use `--pdf-text-mode positioned` only when closer search-highlight placement is
more important than extraction order. Resume mode is normally enough because
changed source fingerprints are reprocessed automatically. Use `--force` only
when you intentionally want to rebuild a ticker:

```bash
python src/pdf_parser.py --ticker TDUP --force
```

## Status Values

`esg_parse_index.csv` uses:

- `parsed`: usable text was extracted.
- `ocr_required`: fewer than 500 non-whitespace characters were extracted.
- `failed`: the PDF could not be parsed.

`esg_pipeline_qa.csv` uses:

- `complete`
- `needs_review`
- `ocr_required`
- `parse_failed`
- `missing_pdf`
- `not_found`
- `tracker_needs_cleanup`
- `incomplete`

## OCR Handling

The parser does not run OCR. If a pre-created searchable OCR PDF follows the
alternate-path convention above, it parses that PDF while retaining the raw
PDF's canonical identity. Otherwise, if extraction produces fewer than 500
non-whitespace characters, it writes the extracted text and marks the raw PDF
as `ocr_required`. This keeps scanned PDFs from failing the entire pipeline.

## Text Quality QA

`ocr_required` means too little text was extracted from the PDF. It is a parser status, not an OCR attempt.

`quality_flags` are warnings, not hard failures. A PDF can remain `parsed` while still carrying warnings that need human review before downstream RAG or analysis.

`doc_quality_status` is the RAG gate:

- `ok`: eligible for ESG indexing.
- `needs_review`: parsed, but requires human review before indexing.
- `exclude_from_esg_rag`: should not enter the ESG-only retrieval index.

`rag_action` is the operational decision:

- `index_as_esg`
- `manual_review_before_indexing`
- `exclude_from_esg_index`
- `not_applicable`

The parser writes these warning fields to `data/00_reference/esg_parse_index.csv`:

- `quality_flags`: pipe-delimited warning names, such as `possible_10k|low_text_per_page`.
- `possible_wrong_doc_type`: `true` when SEC/10-K markers appear in a sustainability-folder PDF.
- `readable_word_count` and `readable_word_ratio`: simple checks for whether extracted text looks readable.
- `chars_per_page`: average extracted characters per page.
- `garbled_char_count`: count of common bad-encoding/OCR artifact sequences.

Current flags:

- `possible_10k` catches SEC filings accidentally placed in sustainability folders.
- `garbled_text` catches bad encoding/OCR artifacts.
- `low_readable_word_ratio` catches text where too few tokens look like normal words.
- `low_text_per_page` catches likely scanned, image-heavy, or poorly extracted PDFs.

The pipeline does not delete or move suspicious PDFs automatically. It only flags them; human review decides whether to remove, replace, or reclassify the file.

## Citation Metadata

`src/pdf_parser.py` writes a page map beside every parsed text file:

```text
data/02_interim/esg_text/{ticker}/{pdf_stem}.pages.csv
```

The page map contains `page`, `char_start`, `char_end`, and `char_count` offsets into the parsed text. The ESG section and chunk indexes propagate:

- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_ready`

RAG should prefer chunks where `citation_ready=true`. Rows with missing page/span metadata are marked `needs_review` in `esg_pipeline_qa.csv`.

## QA Report

Run:

```bash
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
```

The QA report reconciles tracker rows, local PDFs, parsed text, sections, chunks, companies, token bounds, document quality, RAG action, and citation metadata. Rows are report-level where a tracker row has a single report year, not just ticker-level. It prints priority fixes in this order:

1. Possible wrong document type.
2. Downloaded tracker rows with no local PDF.
3. Parsed PDFs with zero sections.
4. Sectioned PDFs with zero chunks.
5. Invalid chunk token counts.
6. Chunks missing citation metadata.
7. Tracker cleanup issues.

## Known Limitations

- Drive download requires `DRIVE_ROOT_FOLDER_ID` and OAuth credentials in the local environment.
- This pipeline marks scanned PDFs as `ocr_required`; OCR must be run separately.
- ESG report headings are not standardized, so section splitting uses conservative regex heuristics and falls back to `full_document` when reliable headings are not found.
- Tracker rows should be one row per report/year. Multi-year tracker rows are still supported, but report-level QA and DB metadata are strongest after tracker cleanup.
- `possible_10k` or SEC-like PDFs are kept in the corpus for auditability but marked `annual_report_with_esg` / `exclude_from_esg_index` instead of being treated as ESG-only chunks.

## How to Re-run One Ticker

```bash
python src/drive_downloader.py --ticker GAP
python src/pdf_parser.py --ticker GAP --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --ticker GAP --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --ticker GAP --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
```

## How to Re-run Full ESG Pipeline

```bash
python src/drive_downloader.py
python src/pdf_parser.py --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
python src/drive_to_db.py --commit
```
