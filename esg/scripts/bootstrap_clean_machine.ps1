<#
.SYNOPSIS
  Build both environments on a fresh clone and run the fusion pipeline.

.DESCRIPTION
  One command between "git clone plus the PDFs" and a running corpus build.
  Everything else the pipeline needs -- esg_parse_index.csv, the accepted
  company manifest and the BGE tokenizer -- is committed, so this script only
  has to build the two virtualenvs and hand off to
  run_docling_fusion_corpus.ps1.

  Two environments, as documented in requirements-docling.txt: venv-docling
  carries docling and torch for stages 1-2, venv carries the light production
  dependencies for stages 3-5. They are built separately and called by path.

  On CUDA. requirements-docling.txt pins torch without an index URL, and the
  default PyPI wheels for Windows are built WITHOUT CUDA. Installing that file
  alone on a GPU machine yields a CPU-only torch and a conversion run no faster
  than the 9.91 s/page measured on CPU -- silently, since nothing errors. So
  torch is installed first from the CUDA index, and the pinned file is applied
  afterwards (pip then leaves the satisfied torch pin alone).

  The run is aborted if CUDA is unavailable, because the only reason to stand
  this up on a second machine is the GPU. Pass -AllowCpu to run anyway.

  Before converting, the script records which PDFs are actually on disk against
  the Drive manifest. The pipeline tolerates a corpus that does not match the
  manifest in either direction -- unmatched index rows are filtered out and
  documents missing from the index get synthesised rows -- so a short corpus
  produces a clean, successful run with fewer chunks and no warning. The
  inventory file is what makes the morning's chunk count interpretable.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File esg\scripts\bootstrap_clean_machine.ps1

.EXAMPLE
  # environments already built, just run
  powershell -ExecutionPolicy Bypass -File esg\scripts\bootstrap_clean_machine.ps1 -SkipSetup
#>
[CmdletBinding()]
param(
    # Wall-clock cap for the convert stage, per worker. The runner's own
    # default is 165 minutes, which would stop an overnight run before
    # midnight; 600 covers a full night.
    [int]    $TimeBudgetMin = 600,
    # PyTorch's CUDA wheel index. cu130 is the only suffix that publishes the
    # pinned torch 2.13.0 / torchvision 0.28.0 for cp313 on Windows; the older
    # suffixes stop at 2.6.0 (cu124), 2.9.1 (cu126, cu129) and 2.11.0 (cu128).
    # Change it if this build is not published for your driver's CUDA version.
    [string] $CudaIndexUrl = "https://download.pytorch.org/whl/cu130",
    # Reuse existing virtualenvs.
    [switch] $SkipSetup,
    # Proceed even when torch reports no CUDA device.
    [switch] $AllowCpu,
    # Build the environments and write the inventory, then stop.
    [switch] $SetupOnly
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo

$pyMain    = Join-Path $repo "venv\Scripts\python.exe"
$pyDocling = Join-Path $repo "venv-docling\Scripts\python.exe"

function Write-Step {
    param([string] $Text)
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Host "  $Text" -ForegroundColor Yellow
    Write-Host ("=" * 64) -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 1. Interpreter
# ---------------------------------------------------------------------------
# Both venvs in this project were built with 3.13.2. A different minor version
# resolves different wheels, which is exactly the variable a reproduction run
# is trying to hold still.
if (-not $SkipSetup) {
    Write-Step "1/6 interpreter"
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) { throw "the 'py' launcher was not found; install Python 3.13.2 from python.org" }
    $ver = (& py -3.13 -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13 is not installed (py -3.13 failed)" }
    Write-Host "  python $ver"
    if ($ver -ne "3.13.2") {
        Write-Host "  WARNING: the corpus was built with 3.13.2; wheel resolution may differ" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 2. Production environment (stages 3-5)
# ---------------------------------------------------------------------------
if (-not $SkipSetup) {
    Write-Step "2/6 venv        (production, stages 3-5)"
    if (-not (Test-Path $pyMain)) { & py -3.13 -m venv (Join-Path $repo "venv") }
    & $pyMain -m pip install --upgrade pip --quiet
    & $pyMain -m pip install -r (Join-Path $repo "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "installing requirements.txt failed" }
    Write-Host "  ok"
}

# ---------------------------------------------------------------------------
# 3. Docling environment (stages 1-2), CUDA torch first
# ---------------------------------------------------------------------------
if (-not $SkipSetup) {
    Write-Step "3/6 venv-docling (docling + CUDA torch, stages 1-2)"
    if (-not (Test-Path $pyDocling)) { & py -3.13 -m venv (Join-Path $repo "venv-docling") }
    & $pyDocling -m pip install --upgrade pip --quiet

    Write-Host "  installing torch from $CudaIndexUrl"
    & $pyDocling -m pip install torch==2.13.0 torchvision==0.28.0 --index-url $CudaIndexUrl
    if ($LASTEXITCODE -ne 0) {
        throw ("could not install the CUDA build of torch from $CudaIndexUrl. " +
               "That index may not publish these pinned versions -- list what it " +
               "carries at $CudaIndexUrl/torch/ and pass a suffix that has them, " +
               "e.g. -CudaIndexUrl https://download.pytorch.org/whl/cu131")
    }

    Write-Host "  installing the pinned docling stack"
    & $pyDocling -m pip install -r (Join-Path $repo "requirements-docling.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "installing requirements-docling.txt failed" }
    Write-Host "  ok"
}

foreach ($exe in @($pyMain, $pyDocling)) {
    if (-not (Test-Path $exe)) { throw "missing interpreter: $exe (run without -SkipSetup)" }
}

# ---------------------------------------------------------------------------
# 4. CUDA check
# ---------------------------------------------------------------------------
Write-Step "4/6 CUDA check"
$cuda = (& $pyDocling -c "import torch; print(torch.version.cuda or 'none'); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')")
$cudaLines = @($cuda)
Write-Host "  torch cuda build : $($cudaLines[0])"
Write-Host "  cuda available   : $($cudaLines[1])"
Write-Host "  device           : $($cudaLines[2])"
if ($cudaLines[1] -ne "True") {
    if (-not $AllowCpu) {
        throw ("torch reports no CUDA device, so this run would be no faster than CPU. " +
               "Reinstall torch from the CUDA index, or pass -AllowCpu to proceed anyway.")
    }
    Write-Host "  proceeding on CPU (-AllowCpu)" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 5. Environment file and corpus inventory
# ---------------------------------------------------------------------------
Write-Step "5/6 inputs"
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $repo ".env.template") $envFile
    Write-Host "  .env created from template (no values needed for this run)"
}

$paths = (& $pyMain "common\config.py" --json | ConvertFrom-Json).absolute
$pdfDir = $paths.RAW_SUSTAINABILITY_DIR
if (-not (Test-Path $pdfDir)) { throw "no PDF directory at $pdfDir -- copy the corpus in first" }
$pdfCount = @(Get-ChildItem -Path $pdfDir -Filter *.pdf -Recurse).Count
if ($pdfCount -eq 0) { throw "no PDFs under $pdfDir -- copy the corpus in first" }

# What is actually on disk, against the Drive manifest snapshot. Written before
# converting so the morning's chunk count can be attributed.
$inventory = Join-Path $repo "scratchpad\prerun_inventory.txt"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $inventory) | Out-Null
& $pyMain -c @'
import csv, pathlib, sys
manifest = pathlib.Path('data/00_reference/esg_drive_manifest.csv')
disk = {p.name.strip().lower() for p in pathlib.Path(sys.argv[1]).rglob('*.pdf')}
print('pdfs on disk :', len(disk))
if manifest.exists():
    with manifest.open(newline='', encoding='utf-8') as fh:
        man = {(r['drive_file_name'] or '').strip().lower() for r in csv.DictReader(fh)}
    print('drive manifest:', len(man))
    print('in manifest, not on disk:')
    for name in sorted(man - disk):
        print('   ', name)
    print('on disk, not in manifest:')
    for name in sorted(disk - man):
        print('   ', name)
else:
    print('drive manifest: absent (not committed; copy it in to compare)')
'@ $pdfDir | Tee-Object -FilePath $inventory
Write-Host "  inventory -> $inventory"

if ($SetupOnly) {
    Write-Host ""
    Write-Host "setup complete; stopping before the run (-SetupOnly)" -ForegroundColor Cyan
    exit 0
}

# ---------------------------------------------------------------------------
# 6. The pipeline
# ---------------------------------------------------------------------------
Write-Step "6/6 pipeline"
& (Join-Path $PSScriptRoot "run_docling_fusion_corpus.ps1") -TimeBudgetMin $TimeBudgetMin -Workers 1
if ($LASTEXITCODE -ne 0) { throw "pipeline failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "done. compare against the reference corpus:" -ForegroundColor Cyan
Write-Host "  682 documents / 50,510 chunks / 49,734 indexable / 120 tickers"
Write-Host "  inventory: $inventory"
