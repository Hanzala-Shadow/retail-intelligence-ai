[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("RUN", "PREVIEW")]
    [string]$ConfirmRun,

    [ValidateRange(1, 8)]
    [int]$ParserWorkers = 4,

    [ValidateRange(1, 12)]
    [int]$ChunkWorkers = 6
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $repoRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
$runner = Join-Path $PSScriptRoot "run_esg_pipeline_fast.cmd"
$preview = $ConfirmRun -eq "PREVIEW"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python was not found at $python"
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Fast pipeline runner was not found at $runner"
}

# The pipeline layout lives in common/config.py; this runner reads it rather
# than restating it. See scripts/PipelinePaths.ps1.
. (Join-Path $PSScriptRoot "PipelinePaths.ps1")
$Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python

& $python -c "import fitz; print('PyMuPDF', fitz.VersionBind)"
if ($LASTEXITCODE -ne 0) {
    throw "PyMuPDF is missing. Run: venv\Scripts\python.exe -m pip install -r requirements-pymupdf.txt"
}

$rawRoots = @(
    $Paths.Absolute.RAW_SUSTAINABILITY_DIR,
    # NOTE: the Python intake deliberately excludes this folder. Scanning it
    # here only widens the ticker list; see config.RAW_SUSTAINABILITY_OTHER_DIR.
    $Paths.Absolute.RAW_SUSTAINABILITY_OTHER_DIR
)
$tickerNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($rawRoot in $rawRoots) {
    if (-not (Test-Path -LiteralPath $rawRoot -PathType Container)) {
        continue
    }
    foreach ($directory in Get-ChildItem -LiteralPath $rawRoot -Directory) {
        [void]$tickerNames.Add($directory.Name.Trim().ToUpperInvariant())
    }
}
$tickers = @($tickerNames) | Sort-Object
if ($tickers.Count -eq 0) {
    throw "No ticker folders were found in the ESG raw roots."
}

Write-Host "ESG PyMuPDF full-corpus rebuild" -ForegroundColor Cyan
Write-Host "Tickers: $($tickers.Count)"
Write-Host "Parser/layout workers: $ParserWorkers"
Write-Host "Chunk workers: $ChunkWorkers"
Write-Host "Mode: $ConfirmRun"
Write-Host "Tickers are processed sequentially because indexes are shared."

if (-not $preview) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupRoot = Join-Path $repoRoot "backups\esg_before_pymupdf_$stamp"
    $resolvedBackupParent = (Resolve-Path (Join-Path $repoRoot "backups") -ErrorAction SilentlyContinue)
    if ($null -eq $resolvedBackupParent) {
        New-Item -ItemType Directory -Path (Join-Path $repoRoot "backups") | Out-Null
        $resolvedBackupParent = Resolve-Path (Join-Path $repoRoot "backups")
    }
    if (-not $resolvedBackupParent.Path.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Backup directory resolved outside the repository: $($resolvedBackupParent.Path)"
    }
    New-Item -ItemType Directory -Path $backupRoot | Out-Null

    $backupTargets = @(
        @{ Source = $Paths.ESG_TEXT_DIR;     Parent = $Paths.INTERIM_DIR },
        @{ Source = $Paths.ESG_SECTIONS_DIR; Parent = $Paths.SECTIONS_DIR },
        @{ Source = $Paths.ESG_CHUNKS_DIR;   Parent = $Paths.CHUNKS_DIR }
    )
    foreach ($target in $backupTargets) {
        $source = Join-Path $repoRoot $target.Source
        if (-not (Test-Path -LiteralPath $source)) {
            continue
        }
        $destinationParent = Join-Path $backupRoot $target.Parent
        New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
        Copy-Item -LiteralPath $source -Destination $destinationParent -Recurse
    }

    $referenceBackup = Join-Path $backupRoot $Paths.REFERENCE_DIR
    New-Item -ItemType Directory -Force -Path $referenceBackup | Out-Null
    foreach ($name in @(
        "esg_parse_index.csv",
        "esg_sections_index.csv",
        "esg_chunks_index.csv",
        "esg_page_layout_qa.csv",
        "esg_pipeline_qa.csv",
        "vector_index_manifest.csv"
    )) {
        $source = Join-Path $Paths.Absolute.REFERENCE_DIR $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination $referenceBackup
        }
    }
    Write-Host "Backup created: $backupRoot" -ForegroundColor Green
}

function Invoke-FastStage {
    param([string[]]$Arguments)

    $actual = [System.Collections.Generic.List[string]]::new()
    foreach ($argument in $Arguments) {
        $actual.Add($argument)
    }
    if ($preview) {
        $actual.Add("-WhatIf")
    }
    & $runner @($actual.ToArray())
    if ($LASTEXITCODE -ne 0) {
        throw "Pipeline command failed: $runner $($actual -join ' ')"
    }
}

$scopedPhases = @("parse", "remediate", "section", "chunk", "layout")
foreach ($phase in $scopedPhases) {
    Write-Host ""
    Write-Host "PHASE: $phase" -ForegroundColor Magenta
    foreach ($ticker in $tickers) {
        Write-Host "[$phase] $ticker" -ForegroundColor Cyan
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("-Stage")
        $arguments.Add($phase)
        $arguments.Add("-Ticker")
        $arguments.Add($ticker)
        if ($phase -eq "parse") {
            $arguments.Add("-EnablePyMuPdfParser")
            $arguments.Add("-ParserWorkers")
            $arguments.Add([string]$ParserWorkers)
            $arguments.Add("-Force")
        }
        elseif ($phase -eq "section") {
            $arguments.Add("-Force")
        }
        elseif ($phase -eq "chunk") {
            $arguments.Add("-ChunkWorkers")
            $arguments.Add([string]$ChunkWorkers)
            $arguments.Add("-Force")
        }
        elseif ($phase -eq "layout") {
            $arguments.Add("-ParserWorkers")
            $arguments.Add([string]$ParserWorkers)
            $arguments.Add("-Force")
        }
        Invoke-FastStage -Arguments $arguments.ToArray()
    }
}

foreach ($phase in @("qa", "manifest", "validate", "tests")) {
    Write-Host ""
    Write-Host "FINAL PHASE: $phase" -ForegroundColor Magenta
    Invoke-FastStage -Arguments @("-Stage", $phase)
}

if ($preview) {
    Write-Host "Preview complete. No backup or pipeline output was written." -ForegroundColor Green
}
else {
    Write-Host "Full-corpus rebuild completed. Keep the backup until visual review is complete." -ForegroundColor Green
}
