# ESG Pipeline Runbook (owner copy)

## THE WHOLE PIPELINE, end to end

Inputs: PDFs under `data\01_raw\sustainability\{TICKER}\` + a row in
`data\00_reference\esg_source_registry.csv`. Everything below is restart-safe and
incremental — rerunning skips what is already done.

    # A. Safe full run: intake -> parse -> remediation -> section -> chunk ->
    #    layout -> VLM decision -> QA -> manifest -> validate -> tests.
    #    Paid VLM calls are OFF.
    scripts\run_esg_pipeline_fast.cmd

    # B. OPTIONAL PAID WORK. Run only after clear human approval.
    #    These commands are never called by the fast runner.
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py classify --transport batch --wait
    .\venv\Scripts\python.exe scripts\run_esg_vlm.py extract  --transport batch --wait
    .\venv\Scripts\python.exe scripts\build_esg_vlm_chunks.py

    # C. Preview use of verified local VLM artifacts. No model API is called.
    scripts\run_esg_pipeline_fast.cmd -Stage vlm -EnableVlmIntegration -WhatIf

    # D. After approval, integrate them and rebuild retrieval eligibility.
    scripts\run_esg_pipeline_fast.cmd -Stage vlm -EnableVlmIntegration

    # E. Optional repeat of final checks
    .\venv\Scripts\python.exe scripts\validate_esg_provenance.py
    .\venv\Scripts\python.exe -m pytest tests\ -q

Order matters: A before B because paid VLM work reads the layout-QA table. B must
finish and be checked before C or D. The manifest is what the retrieval team
uses. The default A run still rebuilds the manifest with unsafe pages held.

For a no-write full preview, run:

    scripts\run_esg_pipeline_fast.cmd -Stage all -WhatIf

For a no-write report preview, run:

    scripts\run_esg_pipeline_fast.cmd -Ticker LOVE -PdfFile "LOVE-Report-2024.pdf" -WhatIf

The runner blocks `-Force` unless `-Ticker` is set. It does not download or
change Drive files. It does not run a live DB load, migration, embedding job, or
paid VLM call.

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

## 5. Activation: manifest integration
- Confirm the local artifacts have model, prompt, source-page hash, output hash,
  and a verified state. A classifier result by itself is not a replacement.
- Preview with:

      scripts\run_esg_pipeline_fast.cmd -Stage vlm -EnableVlmIntegration -WhatIf

- After owner approval, remove `-WhatIf`. The manifest builder admits only
  verified replacements. Failed or missing replacements leave parser chunks
  held. This is the step that can change retrieval.

## 6. Afterwards
- Rotate the OpenAI key (platform.openai.com) — it appeared in chat transcripts.
- Consider committing: src/esg_vlm_stage.py, scripts/run_esg_vlm.py,
  scripts/build_esg_vlm_chunks.py, scripts/vlm_regression_check.py,
  tests/test_esg_vlm_stage.py, .env.example (NEVER a real .env).
- Any future prompt/model change: tests/test_esg_vlm_stage.py will fail on the pinned
  hash — then run scripts/vlm_regression_check.py (~$0.15) and re-validate before
  unpinning.
