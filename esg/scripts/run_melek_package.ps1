<#
.SYNOPSIS
  End-to-end build of the Melek chunk validation package.

.DESCRIPTION
  Regenerates the embedding headers (so the committed year fix is reflected),
  builds the package, verifies every hash, and optionally produces a tar.gz.

  Read-only with respect to the corpus: nothing under data/04_chunks/ is
  written, and no parse/section/chunk stage is run.

.EXAMPLE
  powershell -File scripts/run_melek_package.ps1 -Workers 20
  powershell -File scripts/run_melek_package.ps1 -Workers 20 -SkipHeaders -Archive
#>
[CmdletBinding()]
param(
    [string]$DatasetId = "esg-melek-validation-2026-07-29",

    [string]$Out = "outputs/melek_validation_package",

    [ValidateRange(20, 2000)]
    [int]$Target = 150,

    [int]$MinPerTopic = 4,

    [int]$MaxPerDoc = 3,

    [int]$Seed = 20260729,

    # Skip header regeneration if esg_chunk_embedding_context.csv is already current.
    [switch]$SkipHeaders,

    [switch]$Archive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python not found at $python"
}

function Assert-File([string]$rel, [string]$why) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $rel))) {
        throw "Missing $rel -- $why"
    }
}

Write-Host "=== Preconditions ===" -ForegroundColor Cyan

Assert-File "data/00_reference/esg_chunks_index_enriched.csv" `
    "run scripts/../src/esg_p1_enrichment.py first (P1 enrichment)"
Assert-File "data/00_reference/vector_index_manifest.csv" "the corpus manifest is required"
Assert-File "data/00_reference/esg_sections_index.csv" "section titles are required"

# The year fix must be in history, or headers will print the wrong Reporting year
# for the multi-year documents (VFC-VF CORP-2023-2024 and friends).
$yearFix = & git log --oneline --all --grep="Consolidate report-year extraction" 2>$null
if (-not $yearFix) {
    Write-Host "  WARNING: could not find the year-consolidation commit in history." -ForegroundColor Yellow
    Write-Host "           Headers may carry the wrong Reporting year. Continue only if you are sure." -ForegroundColor Yellow
}
else {
    Write-Host "  year fix present: $($yearFix | Select-Object -First 1)"
}

$chunkCount = (Get-ChildItem -Path "data/04_chunks/esg" -Filter *.txt -Recurse -File | Measure-Object).Count
Write-Host "  chunk files on disk: $chunkCount"

if (-not $SkipHeaders) {
    Write-Host ""
    Write-Host "=== Regenerating embedding headers ===" -ForegroundColor Cyan
    & $python scripts/build_esg_embedding_context.py
    if ($LASTEXITCODE -ne 0) { throw "build_esg_embedding_context.py failed ($LASTEXITCODE)" }
}
else {
    Write-Host "  skipping header regeneration (-SkipHeaders)"
}

Write-Host ""
Write-Host "=== Building package ===" -ForegroundColor Cyan
& $python scripts/build_melek_validation_package.py `
    --dataset-id $DatasetId --out $Out --target $Target `
    --min-per-topic $MinPerTopic --max-per-doc $MaxPerDoc --seed $Seed
if ($LASTEXITCODE -ne 0) { throw "build_melek_validation_package.py failed ($LASTEXITCODE)" }

Write-Host ""
Write-Host "=== Verifying SHA256SUMS ===" -ForegroundColor Cyan
$verify = @"
import hashlib, sys
from pathlib import Path
root = Path(r'$Out')
bad = missing = ok = 0
for line in (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
    if not line.strip():
        continue
    digest, rel = line.split('  ', 1)
    p = root / rel
    if not p.exists():
        missing += 1
        print('MISSING', rel)
        continue
    if hashlib.sha256(p.read_bytes()).hexdigest() == digest:
        ok += 1
    else:
        bad += 1
        print('MISMATCH', rel)
print(f'verified {ok} ok, {bad} mismatched, {missing} missing')
sys.exit(1 if (bad or missing) else 0)
"@
$verify | & $python -
if ($LASTEXITCODE -ne 0) { throw "SHA256SUMS verification FAILED" }

if ($Archive) {
    Write-Host ""
    Write-Host "=== Archiving ===" -ForegroundColor Cyan
    $stamp = Get-Date -Format "yyyyMMdd"
    $name = "melek_validation_package_$stamp.tar.gz"
    & tar -czf $name -C $Out .
    if ($LASTEXITCODE -ne 0) { throw "tar failed ($LASTEXITCODE)" }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $name).Hash.ToLower()
    $size = (Get-Item -LiteralPath $name).Length
    Write-Host "  $name"
    Write-Host "  bytes  : $size"
    Write-Host "  sha256 : $hash"
}

Write-Host ""
Write-Host "DONE." -ForegroundColor Green
Write-Host "Package: $Out"
