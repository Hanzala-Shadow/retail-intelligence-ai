# PDF parser experiments

Use this setup to test changes in `src/` against the 11 problem PDFs.

The runner makes a new folder under `outputs/pdf_parser_experiments/` for each run. It:

- copies the current `src/` into `src_snapshot/`;
- copies only the selected PDFs into `input_pdfs/`;
- runs the copied `pdf_parser.py`;
- writes text, page maps, and the parse index into `parser_output/`;
- records SHA-256 hashes for the source code and PDFs.

This means edits in `src/` are tested, while the live source tree, raw PDFs, reference data, and normal pipeline outputs are not written by the experiment.

From the repository root:

```powershell
python scripts/run_pdf_parser_experiment.py --run-name baseline
```

Run one company while tuning a parser change:

```powershell
python scripts/run_pdf_parser_experiment.py --run-name pvh-column-test --ticker PVH
```

Run layout experiments with PDFium:

```powershell
python scripts/run_pdf_parser_experiment.py --run-name layout-test --prefer-pdfium --auto-layout-pdfium
```

Run the opt-in PyMuPDF table-aware parser:

```powershell
python scripts/run_pdf_parser_experiment.py `
  --run-name pymupdf-table-pilot `
  --prefer-pymupdf `
  --workers 1
```

Run the integrated auto-layout v2 parser:

```powershell
python scripts/run_pdf_parser_experiment.py `
  --run-name auto-layout-v2-all `
  --no-auto-layout-pdfium `
  --workers 1 `
  --force `
  --log-pages
```

After changing a file in `src/`, use a new `--run-name`. Do not reuse a run folder. Compare the two `parser_output/esg_parse_index.csv` files and the page-level text/page-map files.

The default input folder is `data/01_raw/esg_archive_pilot`. Use `--input-root` if the PDFs are stored elsewhere.
