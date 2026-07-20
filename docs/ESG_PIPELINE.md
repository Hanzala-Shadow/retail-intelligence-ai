# ESG Document Intelligence Pipeline

## Purpose

The ESG pipeline converts downloaded sustainability report PDFs into parsed text, canonical ESG sections, retrieval chunks, QA evidence, and PostgreSQL-ready records. It is separate from the existing 10-K pipeline and writes only ESG-specific outputs.

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
- `data/03_sections/esg/{ticker}/{pdf_stem}__{section_code}__{instance_ordinal}.txt`
- `data/04_chunks/esg/{ticker}/{pdf_stem}__{section_code}__{instance_ordinal}__chunk_{0000}.txt`
- `data/00_reference/esg_parse_index.csv`
- `data/00_reference/esg_sections_index.csv`
- `data/00_reference/esg_chunks_index.csv`
- `data/00_reference/esg_source_registry.csv`
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

If a re-split report no longer produces a prior section instance, the chunker
removes only that orphaned `ticker + pdf_stem + section_instance_id` group's
chunks and index rows during resume. It never clears another section or an
entire ticker.

The chunker supports parallel planning with `--workers N`. Worker processes
build chunk/citation plans, but the parent process remains the only writer for
chunk files and `esg_chunks_index.csv`. This speeds up tokenization and
citation validation without allowing concurrent CSV writes.

Use the following sequence on EC2:

```bash
python src/drive_downloader.py --resume
python src/pdf_parser.py --resume --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --resume --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --resume --workers 4 --checkpoint-every 100 --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv
python src/drive_to_db.py --dry-run
```

`--checkpoint-every N` changes the checkpoint cadence; the safe default is
`--checkpoint-every 1`. `--force` intentionally rebuilds the selected scope,
even if source fingerprints match. Use it for a known-bad output or after
changing parser/splitter/chunker behavior:

For full provenance rebuilds, use a larger checkpoint batch such as
`--checkpoint-every 500`; checkpointing every section is safer but much slower
because the full chunk index is rewritten after each completed section.

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

Use the default `--pdf-base original` for scanned PDFs that have little or no
embedded text. If the source PDF already has a corrupted embedded text layer
such as heavy `(cid:...)` artifacts, use `--pdf-base image` so the helper first
renders the pages to an image-only PDF and then adds the Tesseract text layer:

```bash
python src/ocr_pdf.py --input "data/01_raw/sustainability/ETSY/ETSY-ETSY INC-2024.pdf" --output-text "data/02_interim/ocr_text/ETSY/ETSY-ETSY INC-2024.txt" --output-pdf "data/02_interim/ocr_staging/ETSY/ETSY-ETSY INC-2024.pdf" --pdf-text-mode ordered --pdf-base image
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

### Hybrid PDF Parser Policy

The ESG parser uses a hybrid extraction policy so visually complex reports do
not silently produce bad reading order:

1. `pdfplumber` remains the default extractor.
2. For a clear two-to-four-column page, it reconstructs the text from PDF word
   coordinates in left-to-right column order. The reconstruction is accepted
   only when every word is preserved and the column starts are stable and
   vertically overlapping.
3. The parser automatically tries `pypdfium2` when `pdfplumber` output has low
   text density, low page coverage, or `(cid:...)` artifacts.
4. The parser records pages with visual grid/tile risk and pages whose
   coordinate structure is too irregular to reconstruct. Use
   `--auto-layout-pdfium` only for a scoped, reviewed calibration.
5. Document-level overrides remain available for proven exceptions in
   `data/00_reference/esg_parser_overrides.csv`, but the former Amazon 2021
   PDFium override was retired after the coordinate-order pilot showed that
   PDFium itself interleaved columns.

Override CSV schema:

```csv
ticker,pdf_file,parser_mode,reason,active
TEST,TEST-Report-2024.pdf,pypdfium,reviewed_extraction_exception,true
```

Use overrides only after evidence from manual review or audit shows that the
normal extraction order is wrong. Resume mode respects the override: a PDF with
`parser_mode=pypdfium` is not considered complete unless the parse index records
the expected pypdfium policy. Rows created by the previous automatic layout
replacement are intentionally reprocessed on the next default resume; other
completed rows remain resumable.

The parse index records parser provenance:

- `parser_used`: actual extractor path, such as `pdfplumber`,
  `pdfplumber_column_order`, `pdfplumber+pypdfium_text`,
  `pdfplumber+pypdfium_layout`, or
  `pypdfium_text_forced`.
- `parser_policy`: why that extractor path was selected, such as
  `auto_pdfplumber_column_order_v1`, `auto_text_layer_fallback`,
   `auto_layout_grid_fallback`, `override_pdfium`, or `cli_forced_pdfium`.
- `parser_reason`: short reason string.
- `layout_risk_pages`: semicolon-separated page numbers that triggered the
  visual grid/tile risk detector.
- `reading_order_repaired_pages`: semicolon-separated pages automatically
  rebuilt in coordinate column order.
- `reading_order_unresolved_pages`: pages with a material but irregular
  coordinate layout; these require the layout gate to keep them out of
  retrieval unless another verified parser path is selected.

Useful targeted commands:

```bash
python src/pdf_parser.py --ticker AMZN --pdf-file AMZN-Amazon-2021.pdf --force
python src/pdf_parser.py --ticker AMZN --pdf-file AMZN-Amazon-2021.pdf --prefer-pdfium --force
python src/pdf_parser.py --ticker AMZN --pdf-file AMZN-Amazon-2021.pdf --auto-layout-pdfium --force
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

The page map contains `page`, `char_start`, `char_end`, and `char_count` offsets
into the parsed text. A canonical `section_code` is a topic label, not a unique
physical location. Repeated topics therefore receive distinct IDs such as
`community__0001` and `community__0002`. Every section must satisfy this exact
half-open-span invariant:

```text
section_text == parsed_text[source_start_char:source_end_char]
```

The ESG section and chunk indexes propagate:

- `source_id` and `source_version_id`
- `section_instance_id` and `section_code`
- `chunk_type`
- `short_section_action`
- `short_section_reason`
- `merged_section_ids`
- `source_start_char`
- `source_end_char`
- `page_start`
- `page_end`
- `citation_validation_status`
- `citation_validation_version`
- `citation_ready`

`citation_ready=true` is derived, not inferred from non-empty fields. It is true
only when the complete chunk matches its declared parsed-text slice, the slice
is inside its section, the parsed-text fingerprint matches, and pages resolve.
Allowed validation statuses are `verified_exact` and
`verified_whitespace_normalized`, with
`citation_validation_version=semantic_v1`. A prefix-only match, invalid bound,
fingerprint mismatch, or missing page map is not citation-ready. Retrieval must
also require `include_in_esg_index=true` and the document's quality gate.

## Chunking Policy

Normal ESG chunks remain bounded to `100 <= token_count <= 600`. Some
sustainability reports contain real disclosure sections below 100 tokens, such
as compact metrics, captions, short governance disclosures, and brief
environmental statements. These are preserved as one `chunk_type=short_evidence`
row when `25 <= token_count < 100` and the chunk still passes the same
`semantic_v1` citation validation as normal chunks.

Short-evidence rows are marked with:

- `chunk_type=short_evidence`
- `short_section_action=preserved`
- `short_section_reason=meaningful_short_section`

Obvious table-of-contents or navigation fragments are not embedded. They remain
unchunked and are classified by the chunker as
`table_of_contents_or_navigation` or `navigation_term_cluster`. QA and DB dry
run checks are type-aware: a 50-token `normal` chunk is invalid, but a
50-token `short_evidence` chunk is valid.

As of the July 15, 2026 short-evidence rebuild:

- `40,355` total ESG chunks
- `38,678` normal chunks
- `1,677` short-evidence chunks
- `16` short sections intentionally left unchunked as TOC/navigation
- `0` invalid token-count chunks
- `0` provenance validation errors

`data/00_reference/esg_source_registry.csv` stores sparse source decisions that
must survive future OCR replacements. For example, ETSY 2024 is allowed as
`annual_report_with_esg`, `source_scope=excerpt`, and
`retrieval_tier=supplementary`; its source type should be displayed in a user
citation. The registry also records canonical ownership and duplicate intake
history, such as APC files whose reports belong to ARKO.

## Website-Only ESG Sources

Some tracker rows have no standalone sustainability PDF but mention ESG-like
information on an investor or corporate website. These rows should stay
`not_found` for the PDF ESG pipeline unless a PDF is later added. Website-only
ESG content is a different source class and must not be silently mixed into
PDF-derived chunks.

If the team decides to use website ESG content, ingest it through a separate
source path with:

- `source_type=web_esg_page`
- `source_scope=supplemental`
- `retrieval_tier=supplemental_web`
- URL, accessed date, page title, ticker, extraction method, and content hash
- visible citations that identify the source as a website, not a PDF report

The current not-found classification is stored in:

- `data/00_reference/esg_not_found_reason_codes.csv`
- `data/00_reference/esg_supplemental_source_candidates.csv`
- `reports/esg_not_found_coverage_audit_2026-07-15.md`

As of the July 15, 2026 coverage audit, the explicit website/partial-source
candidates are DDS, RVLV, RH, and EVGO. These should not count as missing PDF
pipeline failures; they are candidates for later supplemental-source handling.

## Automatic Page-Layout QA

Run the read-only page-level layout gate after chunking and before QA or vector
manifest creation:

```bash
python src/esg_layout_qa.py --resume --workers 4
```

It writes `data/00_reference/esg_page_layout_qa.csv`, with one row per physical
PDF page. Each row is fingerprinted to the current source PDF and parsed text.
The audit compares native PDF geometry with the selected parsed text and, for
clear multi-column candidates, requires an exact match to the deterministic
coordinate reconstruction. For low-text structural candidates it also compares
a PDFium text-layer candidate.

The gate is automatic and fail-closed:

- ordinary pages receive `auto_pass`;
- successfully reconstructed pages receive
  `auto_pass_column_order_reconstructed` only when the live parsed page text
  matches the reconstructed order exactly;
- low-native-text pages can receive `auto_pass_pdfium_coverage` only when
  PDFium materially improves readable coverage;
- ambiguous multi-column, mismatched coordinate-order, or failed-audit pages
  receive `auto_hold` or `audit_error`;
- table-of-contents, maps, card grids, charts, and dense table-like pages are
  intentionally held when their grid-specific geometry means a single text
  order cannot be defended. Their text remains citation-traceable but is
  excluded from retrieval until a separately verified table/image extraction
  path is available;
- source or parsed-text changes invalidate the old page audit on resume.

The audit never overwrites parsed text, sections, or chunks. Instead,
`scripts/build_esg_vector_manifest.py` excludes a chunk whose citation page
range overlaps an `auto_hold`/`audit_error` page. The chunk stays stored and
citation-ready for auditability, but cannot enter eligible retrieval. The
vector builder requires the layout audit by default; its
`--allow-missing-layout-audit` switch is only for isolated legacy diagnostics.

The fast runner includes this as `-Stage layout` and runs it automatically in
`-Stage all` after chunking. A scoped example is:

```powershell
.\scripts\run_esg_pipeline_fast.cmd -Stage layout -Ticker AMZN -PdfFile AMZN-Amazon-2022.pdf
```

## QA Report

Run:

```bash
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv --layout-audit data/00_reference/esg_page_layout_qa.csv
```

The QA report reconciles tracker rows, local PDFs, parsed text, sections, chunks, companies, type-aware token bounds, document quality, RAG action, and citation metadata. Rows are report-level where a tracker row has a single report year, not just ticker-level. It prints priority fixes in this order:

1. Possible wrong document type.
2. Downloaded tracker rows with no local PDF.
3. Parsed PDFs with zero sections.
4. Sectioned PDFs with zero chunks.
5. Invalid chunk token counts.
6. Chunks missing citation metadata.
7. Tracker cleanup issues.

Strict provenance validation can be run independently:

```bash
python scripts/validate_esg_provenance.py --parse-index data/00_reference/esg_parse_index.csv --sections-index data/00_reference/esg_sections_index.csv --chunks-index data/00_reference/esg_chunks_index.csv --json-out reports/esg_provenance_validation_short_evidence_full.json
```

## Known Limitations

- Drive download requires `DRIVE_ROOT_FOLDER_ID` and OAuth credentials in the local environment.
- This pipeline marks scanned PDFs as `ocr_required`; OCR must be run separately.
- ESG report headings are not standardized, so section splitting uses conservative regex heuristics and falls back to `full_document` when reliable headings are not found.
- Tracker rows should be one row per report/year. Multi-year tracker rows are still supported, but report-level QA and DB metadata are strongest after tracker cleanup.
- SEC-like PDFs remain available for auditability. Annual-report ESG excerpts
  may be indexed when the source registry explicitly marks them as allowed;
  unrelated annual filings remain excluded from the ESG index.

## How to Re-run One Ticker

```bash
python src/drive_downloader.py --ticker GAP
python src/pdf_parser.py --ticker GAP --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --ticker GAP --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --ticker GAP --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_layout_qa.py --ticker GAP --resume
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv --layout-audit data/00_reference/esg_page_layout_qa.csv
python scripts/build_esg_vector_manifest.py
python src/drive_to_db.py --dry-run
```

## How to Re-run Full ESG Pipeline

```bash
python src/drive_downloader.py
python src/pdf_parser.py --root data/01_raw/sustainability --out data/02_interim/esg_text --index data/00_reference/esg_parse_index.csv --workers 1
python src/section_splitter_esg.py --input data/02_interim/esg_text --out data/03_sections/esg --index data/00_reference/esg_sections_index.csv
python src/esg_chunker.py --input data/03_sections/esg --out data/04_chunks/esg --index data/00_reference/esg_chunks_index.csv
python src/esg_layout_qa.py --resume --workers 4
python src/esg_pipeline_qa.py --out data/00_reference/esg_pipeline_qa.csv --layout-audit data/00_reference/esg_page_layout_qa.csv
python scripts/build_esg_vector_manifest.py
python src/drive_to_db.py --dry-run
python src/drive_to_db.py --commit
```
