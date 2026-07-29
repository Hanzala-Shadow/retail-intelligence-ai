# Pipeline paths — three config modules, one merged table

Every `data/`, `reports/`, and `logs/` path used by either pipeline is defined
once, in one of three files. Nothing else spells a pipeline path out.

| File | Owns |
|---|---|
| [`common/config.py`](../common/config.py) | What both pipelines share: `REPO_ROOT`, the `data/` stage directories, `COMPANIES_CSV`, the env vars, and the JSON bridge |
| [`esg/config.py`](../esg/config.py) | `ESG_*` — sustainability PDFs, text, sections, chunks, QA, the local SQLite packages |
| [`filings/config.py`](../filings/config.py) | 10-K — SEC filings, HTML text, sections, tables, chunk index |

Both pipeline configs re-export `common.config` wholesale, so a module inside
either pipeline writes `import config` and sees one flat namespace — the shared
constants and its own.

## Why it is split this way

Before the pipelines were split, one `src/config.py` held all 73 constants and
`path_constants()` walked `globals()` to build the table the shell runners
read. Splitting that file naively would have broken the bridge silently: a
runner reading only `esg/config.py` gets a table missing every 10-K constant,
and a missing key becomes an empty argument that fails deep inside a corpus
run rather than at startup.

Three things prevent that:

1. `path_constants(namespace)` takes the namespace explicitly instead of
   walking whatever module it happens to live in.
2. `merged_path_constants()` unions all three, and it is what
   `python common/config.py --json` prints. The runners read that, so they see
   the same 73 keys, with the same values, that the single config printed.
3. `tests/test_config_single_source_of_truth.py` pins the merged key set and
   checks every runner's `$Paths.FOO` / `$CFG_FOO` lookup against it.

## Using it

### Python

```python
import _bootstrap  # noqa: F401  -- puts config + common on sys.path
import config

rows = read_csv(config.ESG_PARSE_INDEX_CSV)
parser.add_argument("--index", default=str(config.ESG_PARSE_INDEX_CSV))
```

Constants are absolute `Path` objects anchored on `config.REPO_ROOT`, so they
resolve identically no matter where a script is invoked from. Wrap with
`str()` where a string is required (argparse defaults, string concatenation).

Shared modules come from the package, not the path:

```python
from common.models import Document
from common import db_utils
```

Tests need no bootstrap — the rootdir [`conftest.py`](../conftest.py) puts both
pipelines on `sys.path` before collection. A test that wants the 10-K layout
specifically should be explicit, since bare `config` resolves to ESG:

```python
from filings import config as filings_config
```

### PowerShell

PowerShell cannot import a Python module, so the runners read the layout
through [`esg/scripts/PipelinePaths.ps1`](../esg/scripts/PipelinePaths.ps1),
which shells out to `python common/config.py --json`:

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

`Import-PipelinePaths` checks the table's shape before returning, and takes an
optional `-Require` list so a runner can fail on line one instead of mid-run:

```powershell
$Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python `
    -Require @('ESG_PARSE_INDEX_CSV', 'ESG_TEXT_DIR')
```

Note that a PowerShell parameter default cannot reference `$Paths` (parameter
defaults are evaluated before the script body). Declare the parameter without
a default and resolve it after loading:

```powershell
param([string]$VlmDir)
...
if (-not $VlmDir) { $VlmDir = $Paths.VLM_DIR }
```

### bash

Same bridge, via `eval`. Use `merged_path_constants()` — a runner that spans
both pipelines needs the union:

```bash
eval "$(python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from common import config
for name, value in config.merged_path_constants()["relative"].items():
    print(f'CFG_{name}="{value}"')
PY
)"

find "$CFG_RAW_10K_DIR" -type f | wc -l
```

## Adding a new path

1. Add the constant to the config that owns it — `common/` only if **both**
   pipelines use it, otherwise `esg/` or `filings/`.
2. Import it. Do not write the literal anywhere else.

`tests/test_config_single_source_of_truth.py` enforces this. It scans
`common/`, `esg/src`, `esg/scripts`, `esg/tests`, `filings/src`, `tests/`,
`reports/esg_audit_2026Q3/`, and the shell runners, and fails on:

- a path literal (`"data/00_reference/x.csv"`, `"reports/y.md"`, `"logs/z.log"`)
- segment-wise composition (`REPO_ROOT / "data" / "00_reference"`)
- a `$Paths.FOO` or `$CFG_FOO` lookup naming a constant absent from the merged
  table — otherwise a typo silently yields an empty argument

It deliberately does **not** flag docstrings, vocabulary words that happen to
be `"data"`, or synthetic test fixtures like `"data/AAP/report-2024.pdf"`,
which name no stage directory and point at nothing on disk.

## Paths that stay relative on purpose

Several call sites must **not** become absolute, because their value is
persisted or re-anchored elsewhere. They still derive from config, via
`config.as_repo_relative()`:

| Location | Why |
|---|---|
| `filings/src/db_loader.py` → `documents.filepath` | Written to Postgres; an absolute path breaks the row on another machine |
| `esg/src/esg_p1_enrichment.py` | Joins everything against `--repo-root`, and writes `rel_embed` into the output index |
| `esg/scripts/build_esg_embedding_context.py` | Writes `embedding_text_ctx_file` into the output index |
| `esg/scripts/run_pdf_parser_by_year.py`, `run_pdf_parser_experiment.py` | Join against `--repo`, which may point at a different checkout |

## Known gaps

- **Environment variables are not centralized.** `common/config.py` says they
  are, but `common/db_utils.py`, `filings/src/chunks_bulk_loader.py`, and
  `esg/src/drive_downloader.py` still call `os.getenv` directly — and two of
  them fall back to `DATABASE_URL`, which config does not read.
- **`data/00_reference/esg_sample_docs.csv` does not exist.** Both sampling
  runners depend on it, so neither currently runs.
- **`esg/scripts/run_esg_pymupdf_full_corpus.ps1` scans `sustainability_other`**,
  which the Python intake deliberately excludes. It reads the path from
  `config.RAW_SUSTAINABILITY_OTHER_DIR` rather than hardcoding it, but whether
  it should scan that folder at all is an open corpus-scope question.
- **`filings/` has no scripts or tests yet.** Every runner and test in the repo
  drives ESG. The directories and their `_bootstrap` exist so the first 10-K
  script lands with the same import contract as everything else.
