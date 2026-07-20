# ESG Pipeline Runbook (owner copy)

## THE WHOLE PIPELINE, end to end

Inputs: PDFs under `data\01_raw\sustainability\{TICKER}\` + a row in
`data\00_reference\esg_source_registry.csv`. Everything below is restart-safe and
incremental — rerunning skips what is already done.

    # A. Deterministic core: parse -> section -> chunk -> layout QA -> QA -> validate -> tests
    scripts\run_esg_pipeline_fast.cmd

    # B. VLM stages (need $env:OPENAI_API_KEY; see steps 0-4 below for detail)
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py classify --transport batch --wait
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py extract  --transport batch --wait
    .\venv\Scripts\python.exe scripts\build_esg_vlm_chunks.py

    # C. Retrieval eligibility (after VLM-integration activation, this consumes the
    #    VLM chunk index and excludes flagged originals)
    .\venv\Scripts\python.exe scripts\build_esg_vector_manifest.py

    # D. Final checks
    .\venv\Scripts\python.exe scripts\validate_esg_provenance.py
    .\venv\Scripts\python.exe -m pytest tests\ -q

Order matters: A before B (the VLM stages read the layout-QA table A produces); B before
C; C's output (`vector_index_manifest.csv`) is what the embedding/retrieval team consumes.
Adding one new document later = drop the PDF + registry row, rerun the same commands; the
parser resume and the VLM cache make it incremental and cheap (pennies per document).

---

# VLM stage detail (steps 0-6)

Written 2026-07-20. All commands from the repo root in PowerShell:
`Set-Location "C:\Users\Aziz\Documents\ChatGPT Codex\retail-intelligence-ai"`

## 0. Once per shell: set the key
    $env:OPENAI_API_KEY='sk-...your key...'

## 1. Classify the 9,078 passing pages (finds the scrambled tables)
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py classify --transport batch --wait
- batch = half price (~$5), results usually 1–6 h; the window can stay open or you can
  close it and later run: `scripts\run_esg_vlm.py collect`
- want it now instead: `--transport sync` (~$10, ~1.5 h, window stays open)
- interrupted? Just run the same command again — cached pages are never re-paid.

## 2. Extract (held table pages + everything step 1 flagged)
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py extract --transport batch --wait
- ~$6 batch / ~$13 sync. Same resume rules.

## 3. Build the chunk index
    .\venv\Scripts\python.exe scripts\build_esg_vlm_chunks.py
- Prints chunk count + unmapped/ambiguous sections. Expect unmapped ≈ 0.

## 4. Sanity-check (5 minutes)
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py status
- Open 2–3 files from data\04_vlm\extraction\ next to the PDFs.
- Look at data\04_vlm\vlm_chunks_index.csv: `body_uncorroborated_count` column — pages
  with high counts are the ones worth a human glance (informational, nothing blocked).

## 5. Activation (requires Fable/a session): manifest integration
- The ~30-line patch to scripts/build_esg_vector_manifest.py: consume
  data/04_vlm/vlm_chunks_index.csv (VLM chunks in, lineage vlm_extraction_v1) and
  exclude the parser chunks of classifier-flagged pages. Default-off flag; backup the
  live manifest first; full test suite after. THIS is the moment retrieval changes.

## 6. Afterwards
- Rotate the OpenAI key (platform.openai.com) — it appeared in chat transcripts.
- Consider committing: src/esg_vlm_stage.py, scripts/run_esg_vlm.py,
  scripts/build_esg_vlm_chunks.py, scripts/vlm_regression_check.py,
  tests/test_esg_vlm_stage.py, .env.example (NEVER a real .env).
- Any future prompt/model change: tests/test_esg_vlm_stage.py will fail on the pinned
  hash — then run scripts/vlm_regression_check.py (~$0.15) and re-validate before
  unpinning.
