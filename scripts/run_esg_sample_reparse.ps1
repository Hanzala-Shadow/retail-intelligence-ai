[CmdletBinding()]
param(
    [ValidateRange(1, 8)]
    [int]$Workers = 4,

    # Resolved from src/config.py once $Paths is loaded.
    [string]$DocList
)

# Re-parses only the layout-QA sample documents (scoped, forced), so a parser
# code change can be measured on the 50-doc sample without sweeping all 726
# corpus PDFs. Downstream stages are then run in resume mode via
# run_esg_pipeline_fast.ps1, which only redoes documents whose parse changed:
#   powershell -File scripts/run_esg_sample_reparse.ps1
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage remediate
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage section
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage chunk
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage layout
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage qa
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage manifest
#   powershell -File scripts/run_esg_pipeline_fast.ps1 -Stage tests

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python was not found at $python"
}

# The pipeline layout lives in src/config.py; this runner reads it rather
# than restating it. See scripts/PipelinePaths.ps1.
. (Join-Path $PSScriptRoot "PipelinePaths.ps1")
$Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python

if (-not $DocList) {
    $DocList = $Paths.ESG_SAMPLE_DOCS_CSV
}

if (-not (Test-Path -LiteralPath $DocList -PathType Leaf)) {
    throw "Document list not found at $DocList (columns: ticker,pdf_file)."
}

$docs = @(Import-Csv $DocList)
if ($docs.Count -lt 1) {
    throw "Document list $DocList is empty."
}

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$index = 0
foreach ($doc in $docs) {
    $index++
    Write-Host "[$index/$($docs.Count)] $($doc.ticker) :: $($doc.pdf_file)" -ForegroundColor Cyan
    & $python src/pdf_parser.py `
        --resume `
        --force `
        --root data/01_raw/sustainability `
        --ocr-root data/02_interim/ocr_staging `
        --out data/02_interim/esg_text `
        --index data/00_reference/esg_parse_index.csv `
        --workers $Workers `
        --checkpoint-every 10 `
        --ticker $doc.ticker `
        --pdf-file $doc.pdf_file
    if ($LASTEXITCODE -ne 0) {
        throw "Parse failed for $($doc.ticker) / $($doc.pdf_file) with exit code $LASTEXITCODE."
    }
}
$timer.Stop()
Write-Host "Re-parsed $($docs.Count) sample documents in $($timer.Elapsed)." -ForegroundColor Green
