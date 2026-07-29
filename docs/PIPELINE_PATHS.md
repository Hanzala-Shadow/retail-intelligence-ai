# Pipeline paths — `src/config.py` is the single source of truth

Every `data/`, `reports/`, and `logs/` path used by the pipeline is defined
once, in [`src/config.py`](../src/config.py). Nothing else spells a pipeline
path out.

## Why

Before this change only one module (`sec_discovery.py`) imported `config`.
The other ~44 modules each wrote their own string literals — roughly 200
occurrences of things like `"data/00_reference/esg_parse_index.csv"`. Two
consequences:

1. **Every path was CWD-relative.** Scripts worked from the repo root and
   silently resolved to the wrong place from anywhere else.
2. **Moving a directory meant editing 40+ files.** That directly blocks the
   planned split of the ESG and 10-K pipelines into separate top-level
   directories.

A latent bug fell out of the audit: `html_parser.py` defaulted to
`data/01_raw/10-K filings` and `data/raw_text/html_text`, neither of which
exists. `run_pipeline.sh` had been silently compensating by passing correct
paths on the command line.

## Using it

### Python

```python
import config

rows = read_csv(config.ESG_PARSE_INDEX_CSV)
parser.add_argument("--index", default=str(config.ESG_PARSE_INDEX_CSV))
```

Constants are absolute `Path` objects anchored on `config.REPO_ROOT`, so they
resolve identically no matter where a script is invoked from. Wrap with
`str()` where a string is required (argparse defaults, string concatenation).

Modules under `scripts/`, `tests/`, and `reports/` need `src/` on the path
first — the existing repo idiom:

```python
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
```

### PowerShell

PowerShell cannot import a Python module, so the runners read the layout
through [`scripts/PipelinePaths.ps1`](../scripts/PipelinePaths.ps1), which
shells out to `python src/config.py --json`:

```powershell
. (Join-Path $PSScriptRoot "PipelinePaths.ps1")
$Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python

$arguments.Add($Paths.ESG_PARSE_INDEX_CSV)      # data/00_reference/esg_parse_index.csv
$Paths.Absolute.ESG_PARSE_INDEX_CSV             # C:\...\data\00_reference\...
```

The default form is **repo-relative with forward slashes** — what these
runners already passed to the Python stages, since they all `Set-Location` to
the repo root first. Use `.Absolute` only where a path must survive a
working-directory change.

Note that a PowerShell parameter default cannot reference `$Paths` (parameter
defaults are evaluated before the script body). Declare the parameter without
a default and resolve it after loading:

```powershell
param([string]$VlmDir)
...
if (-not $VlmDir) { $VlmDir = $Paths.VLM_DIR }
```

### bash

Same bridge, via `eval`:

```bash
eval "$(python3 - <<'PY'
import sys
sys.path.insert(0, "src")
import config
for name, value in config.path_constants()["relative"].items():
    print(f'CFG_{name}="{value}"')
PY
)"

find "$CFG_RAW_10K_DIR" -type f | wc -l
```

## Paths that stay relative on purpose

Three call sites must **not** become absolute, because their value is
persisted or re-anchored elsewhere. They still derive from config, via
`config.as_repo_relative()`:

| Location | Why |
|---|---|
| `db_loader.py` → `documents.filepath` | Written to Postgres; an absolute path breaks the row on another machine |
| `esg_p1_enrichment.py` | Joins everything against `--repo-root`, and writes `rel_embed` into the output index |
| `build_esg_embedding_context.py` | Writes `embedding_text_ctx_file` into the output index |
| `run_pdf_parser_by_year.py`, `run_pdf_parser_experiment.py` | Join against `--repo`, which may point at a different checkout |

## Adding a new path

1. Add the constant to the right section of `src/config.py`
   (SHARED / 10-K / ESG).
2. Import it. Do not write the literal anywhere else.

`tests/test_config_single_source_of_truth.py` enforces this. It scans `src/`,
`scripts/`, `tests/`, `reports/esg_audit_2026Q3/`, and the shell runners, and
fails on:

- a path literal (`"data/00_reference/x.csv"`, `"reports/y.md"`, `"logs/z.log"`)
- segment-wise composition (`REPO_ROOT / "data" / "00_reference"`)
- a `$Paths.FOO` or `$CFG_FOO` lookup naming a constant that does not exist —
  otherwise a typo silently yields an empty argument

It deliberately does **not** flag docstrings, vocabulary words that happen to
be `"data"`, or synthetic test fixtures like `"data/AAP/report-2024.pdf"`,
which name no stage directory and point at nothing on disk.

## Known gaps

- **Environment variables are not centralized.** `config.py` says they are,
  but `db_utils.py`, `chunks_bulk_loader.py`, and `drive_downloader.py` still
  call `os.getenv` directly — and two of them fall back to `DATABASE_URL`,
  which `config` does not read.
- **`data/00_reference/esg_sample_docs.csv` does not exist.** Both sampling
  runners depend on it, so neither currently runs.
- **`run_esg_pymupdf_full_corpus.ps1` scans `sustainability_other`**, which
  the Python intake deliberately excludes. It now reads the path from
  `config.RAW_SUSTAINABILITY_OTHER_DIR` rather than hardcoding it, but whether
  it should scan that folder at all is an open corpus-scope question.
