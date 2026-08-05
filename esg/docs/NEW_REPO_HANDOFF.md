# Handoff: docling-fusion pipeline in a repo of its own

Context for a chat that will build a **new repository** containing only the
docling-fusion pipeline, with the legacy PDF-parser path deleted and output
landing in `data/` the way the legacy pipeline's did.

Read `esg/docs/DOCLING_FUSION_PIPELINE.md` first — it describes what the six
stages do. This file describes only what to carry, what to delete, and what
breaks when you do.

Everything below was verified against the working tree on branch
`Phase_5_Aziz` (`c8bf68f`), not inferred from the pipeline doc.

---

## 1. The import closure is small

The six stages depend on nine Python modules and nothing else in the repo.
Verified by reading the import block of every stage and every helper it names:

| Module | Imports from this repo |
| --- | --- |
| `esg/scripts/run_docling_gold_spike.py` | none — stdlib + `docling` + `fitz` |
| `esg/scripts/bridge_docling_to_pipeline.py` | **none — pure stdlib** |
| `esg/scripts/summarise_fusion_run.py` | **none — pure stdlib** |
| `esg/src/section_splitter_esg.py` | `_bootstrap`, `config`, `esg_compact_toc` |
| `esg/src/esg_chunker.py` | `_bootstrap`, `config`, `esg_compact_toc`, `esg_year` |
| `esg/src/esg_compact_toc.py` | none |
| `esg/src/esg_year.py` | none |

`esg/config.py` re-exports `common/config.py`. That is the whole graph. The
other ~60 scripts in `esg/scripts/` and ~30 modules in `esg/src/` are legacy
parser path and none of them is reachable from a stage.

Two things the pipeline doc's stage tables list that are **not** actually used
by the runner: `esg/scripts/PipelinePaths.ps1` (the fusion runner never
dot-sources it) and `esg/src/esg_page_role.py` (legacy; the fusion classifier is
`classify_page_role` inside `bridge_docling_to_pipeline.py:212`).

## 2. Files to copy into the new repo

**Code**

```
esg/scripts/run_docling_gold_spike.py
esg/scripts/bridge_docling_to_pipeline.py
esg/scripts/summarise_fusion_run.py
esg/scripts/run_docling_fusion_corpus.ps1
esg/scripts/_bootstrap.py
esg/src/section_splitter_esg.py
esg/src/esg_chunker.py
esg/src/esg_compact_toc.py
esg/src/esg_year.py
esg/src/_bootstrap.py
esg/config.py
common/__init__.py
common/config.py
conftest.py
pytest.ini
```

**Environment**

```
requirements.txt              # stages 3-5
requirements-docling.txt      # stages 1-2, fully pinned, docling 2.117.0
.env.template
```

**Data that is in git and must come along**

```
models/bge-base-en-v1.5-tokenizer/    # 934 KB — chunk token counts depend on
models/.gitattributes                 #   this exact build; keep the `-text` rule
data/00_reference/companies.csv
data/00_reference/sustainability_report_tracker.csv
```

**Docs** — `esg/docs/DOCLING_FUSION_PIPELINE.md`, and this file.

**Data that is not in git and must be copied by hand** (unchanged from the
current setup):

- `data/00_reference/esg_parse_index.csv` (363 KB) — stage 3 identity columns.
  Without it stage 3 raises `TypeError` rather than degrading.
- `data/01_raw/sustainability/` — source PDFs, used for hashing synthesised rows.
- the input PDF corpus (~3.4 GB).

**Do not copy:** `client_secret.json` and `token.json` sit in the repo root of
the current project. They are Google OAuth credentials, they are not needed by
any of the six stages, and they must not be committed to a new repo.

## 3. Four things break when the legacy path is deleted

Each of these is a real edit the new repo needs, not a cleanup nicety.

**a. `section_splitter_esg.py:2375` imports the legacy splitter.** Without
`--experimental-sectioning`, `run()` delegates to `section_splitter_esg_legacy`.
Either carry `esg/src/section_splitter_esg_legacy.py` too, or — better — delete
the delegation branch and make experimental sectioning the only path. The
runner always passes the flag, so removing the fallback changes no behaviour and
removes the one way to silently measure the wrong module.

**b. `common/config.py` hard-requires `filings/config.py`.**
`PIPELINE_CONFIGS = ("esg", "filings")` and `_load_pipeline_config()` raises
`FileNotFoundError` when a named config is missing. Change the tuple to
`("esg",)`.

**c. `conftest.py` inserts `filings/` and `filings/src/` on `sys.path`.** Prune
those two entries. Keep the ordering comment's intent: `esg/` must be inserted
last so a bare `import config` resolves to `esg/config.py`.

**d. `requirements.txt` is the legacy production env.** Stages 3-5 import only
`tiktoken`, `transformers` (BGE tokenizer) and `python-dotenv` (via
`common/config.py`). `pandas`, `boto3`, `psycopg2`, `SQLAlchemy`, `pdfplumber`,
`pytesseract`, `pypdf` and the Google API packages are all legacy-path
dependencies. Trimming is safe but verify by running the tests after —
`transformers==5.14.1` may pull `torch` on install even though the fast
tokenizer path doesn't need it at run time. `requirements-docling.txt` is
already correct and fully pinned; leave it alone.

## 4. Writing output into `data/`

This is mostly a matter of deleting arguments, not adding code. Stages 4 and 5
**already default to the `data/` locations** — the current runner overrides them
to keep the fusion run out of the production corpus:

| Stage arg | Current runner value | Default in the script |
| --- | --- | --- |
| splitter `--input` | `<WorkRoot>/interim/esg_text` | `config.ESG_TEXT_DIR` = `data/02_interim/esg_text` |
| splitter `--out` | `<WorkRoot>/sections/esg` | `config.ESG_SECTIONS_DIR` = `data/03_sections/esg` |
| splitter `--index` | `<WorkRoot>/sections_index.csv` | `config.ESG_SECTIONS_INDEX_CSV` = `data/00_reference/esg_sections_index.csv` |
| chunker `--out` | `<WorkRoot>/chunks/esg` | `config.ESG_CHUNKS_DIR` = `data/04_chunks/esg` |
| chunker `--index` | `<WorkRoot>/chunks_index.csv` | `config.ESG_CHUNKS_INDEX_CSV` = `data/00_reference/esg_chunks_index.csv` |
| bridge `--out` | `<WorkRoot>/interim/esg_text` | `<work-dir>/pipeline_input` |

So the change in `run_docling_fusion_corpus.ps1` is: point `$interim` at
`data/02_interim/esg_text`, `$sections` at `data/03_sections/esg`, `$sectionsIdx`
at `data/00_reference/esg_sections_index.csv`, `$chunks` at `data/04_chunks/esg`,
`$chunksIdx` at `data/00_reference/esg_chunks_index.csv`. Keep `$work`
(`<WorkRoot>/work`) where it is — the docling JSON cache and fused pages are
intermediates, not corpus, and rebuilding the cache costs hours.

**The one genuine trap: the parse index would eat its own input.** Stage 3 reads
`data/00_reference/esg_parse_index.csv` for the identity columns
(`logical_source_id`, `source_version_id`, `file_alias_id`,
`extraction_artifact_id`, source hashes) and writes a v2 index. The chunker's
`--parse-index` default is `config.ESG_PARSE_INDEX_CSV` — the *same file stage 3
reads*. Pointing `--parse-index-out` at it makes the run non-idempotent: the
second run reads identity from a file the first run rewrote, and lineage is
destroyed silently.

Do this instead: add `ESG_PARSE_INDEX_V2_CSV = REFERENCE_DIR /
"esg_parse_index_v2.csv"` to `esg/config.py`, have the bridge write there, and
pass the chunker `--parse-index <that file>` explicitly. Never let stage 3's
output path equal its input path. (`data/00_reference/esg_parse_index_v2.csv`
already exists in the current tree from an earlier run — treat it as
overwritable output, and confirm nothing else consumes it before you do.)

**Sectioning and chunking upsert.** Re-running without clearing leaves stale
rows from removed documents and merges new rows into old — the counts look
plausible and are wrong. Now that output lands in `data/`, the clear step is
destructive to the corpus, so the new repo should ship a small script for it
rather than leaving it to memory. Clear before a rebuild:
`data/02_interim/esg_text/`, `data/03_sections/esg/`,
`data/04_chunks/esg/`, `esg_sections_index.csv`, `esg_chunks_index.csv`,
`esg_parse_index_v2.csv`. Never `<WorkRoot>/work/docling_json/`.

**`.gitignore`.** Carry the ESG-relevant lines: `data/01_raw/`,
`data/02_interim/`, `data/03_sections/esg/`, `data/04_chunks/`,
`data/00_reference/*` with `!.gitkeep`, `!companies.csv`,
`!sustainability_report_tracker.csv`, `outputs/**`, `venv/`, `venv-docling/`.
Note `models/` must **not** be ignored — the tokenizer is deliberately committed.

## 5. Tests

Eleven test files reference the kept modules:

```
esg/tests/test_bridge_docling_to_pipeline.py
esg/tests/test_esg_chunk_tiling.py
esg/tests/test_esg_chunker_candidate.py
esg/tests/test_esg_compact_toc.py
esg/tests/test_esg_heading_quality.py
esg/tests/test_esg_provenance.py
esg/tests/test_esg_rag_quality.py
esg/tests/test_esg_section_hold.py
esg/tests/test_esg_subsection_context.py
esg/tests/test_esg_year_consolidation.py
tests/test_config_single_source_of_truth.py
```

Some of them also import legacy modules (`test_esg_chunker_candidate.py` targets
`esg_chunker_candidate.py`, which is not part of the fusion chain;
`test_esg_provenance.py` covers a legacy script). Copy all eleven, run `pytest`,
and drop only the files that fail with `ImportError` on a module you
deliberately deleted — do not drop a test because it fails on a real assertion.

`tests/test_config_single_source_of_truth.py` pins the merged path-constant key
set and will need updating for the `PIPELINE_CONFIGS` change in §3b and the new
`ESG_PARSE_INDEX_V2_CSV` constant in §4. It is the test that catches a runner
looking up a config key that no longer exists, so it is worth keeping working
rather than deleting.

## 6. Suggested order of work

1. Create the new repo, copy the manifest in §2, commit — a plain copy, no edits.
2. Apply the four break-fixes in §3. Build both venvs (Python 3.13.2, Windows).
   Run `pytest`. Commit.
3. Repoint the runner paths per §4, including the parse-index v2 constant and
   the clear script. Commit.
4. Copy in `esg_parse_index.csv`, `data/01_raw/sustainability/` and a **small**
   PDF subset. Run the full chain end to end against that subset and check the
   stage-6 summary before pointing it at the 3.4 GB corpus.

Step 4 matters: the first run against `data/` writes into the production corpus
locations, and the previous safety property — "nothing here writes to `data/`" —
is exactly what is being given up. Prove the chain on a handful of documents
first.
