[CmdletBinding()]
param(
    [string]$Years = "2023,2024",

    # 24 logical CPUs / 31.6 GB on this box. The parser spawns one process per
    # PDF with max_tasks_per_child=1, so peak RSS scales with worker count and
    # the largest document in scope (PSMT-...-2023.pdf is 131 MB). 12 leaves
    # headroom; raise toward 20 if you watch memory and the big files are done.
    [ValidateRange(1, 24)]
    [int]$ParserWorkers = 12,

    [ValidateRange(1, 24)]
    [int]$ChunkWorkers = 16,

    [ValidateRange(1, 1000)]
    [int]$ParserCheckpointEvery = 10,

    [ValidateRange(1, 1000)]
    [int]$SectionCheckpointEvery = 25,

    [ValidateRange(1, 5000)]
    [int]$ChunkCheckpointEvery = 500,

    [switch]$SkipTests,
    [switch]$SkipPrereqCheck,
    [switch]$WhatIf
)

# Year-scoped full-chain ESG pipeline runner.
#
# Only the parse stage is year-aware (esg/src/pdf_parser.py --years). Every later
# stage is driven by what the previous stage produced, so on a clean tree they
# process exactly the parsed subset without needing a year flag of their own.
# That also means this script assumes the derived directories start empty; if
# data/02_interim/esg_text already holds other years, the downstream stages will
# pick those up too.
#
#   scripts\run_esg_pipeline_year.ps1
#   scripts\run_esg_pipeline_year.ps1 -Years 2024 -ParserWorkers 20
#   scripts\run_esg_pipeline_year.ps1 -WhatIf

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python was not found at $python"
}

# The pipeline layout lives in common/config.py; this runner reads it rather
# than restating it. See scripts/PipelinePaths.ps1.
. (Join-Path $PSScriptRoot "PipelinePaths.ps1")
$Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python

$targetYears = @($Years.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($targetYears.Count -eq 0) {
    throw "-Years produced no valid years."
}

function Format-Command {
    param([string]$Executable, [string[]]$Arguments)
    $escaped = foreach ($argument in $Arguments) {
        if ($argument -match '[\s\"]') { '"' + ($argument -replace '"', '\"') + '"' } else { $argument }
    }
    return ((@($Executable) + $escaped) -join " ")
}

function Invoke-PythonStage {
    param([string]$Name, [string[]]$Arguments)

    $commandText = Format-Command -Executable $python -Arguments $Arguments
    Write-Host ""
    Write-Host "[$Name] $commandText" -ForegroundColor Cyan
    if ($WhatIf) { return }

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    & $python @Arguments
    $exitCode = $LASTEXITCODE
    $timer.Stop()
    if ($exitCode -ne 0) {
        throw "$Name failed with exit code $exitCode after $($timer.Elapsed)."
    }
    Write-Host "[$Name] completed in $($timer.Elapsed)." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Prerequisite gate.
#
# These files are curated inputs, not pipeline outputs: nothing in the chain
# writes them, so a wiped data/00_reference cannot regenerate them. They are
# checked up front because the stage that needs them most (enrich) runs last,
# and a silent-degradation failure there is worse than a fast stop here.
# ---------------------------------------------------------------------------
if (-not $SkipPrereqCheck) {
    $required = @(
        @{ File = $Paths.ESG_SOURCE_REGISTRY_CSV;             Impact = "enrich aborts; chunk/qa/manifest silently stop applying curated exclusions" },
        @{ File = $Paths.ESG_ACCEPTED_COMPANY_MANIFEST_CSV;   Impact = "enrich aborts with FileNotFoundError" },
        @{ File = $Paths.COMPANIES_CSV;                       Impact = "enrich aborts; qa flags every ticker as missing" }
    )
    $advisory = @(
        @{ File = $Paths.SUSTAINABILITY_TRACKER_CSV;          Impact = "qa loses tracker cross-checks (non-fatal)" }
    )

    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $repoRoot $_.File)) })
    foreach ($item in $advisory) {
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $item.File))) {
            Write-Host "[prereq] WARNING $($item.File) is missing - $($item.Impact)" -ForegroundColor Yellow
        }
    }

    if ($missing.Count -gt 0) {
        Write-Host ""
        Write-Host "[prereq] Missing curated input(s) the chain cannot regenerate:" -ForegroundColor Red
        foreach ($item in $missing) {
            Write-Host "   $($item.File)" -ForegroundColor Red
            Write-Host "      -> $($item.Impact)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "   Restore them from HEAD (they are deleted but not committed):" -ForegroundColor Yellow
        Write-Host "      git restore -- $(($missing | ForEach-Object { $_.File }) -join ' ')" -ForegroundColor Yellow
        Write-Host ""
        throw "Prerequisite check failed. Restore the files above, or pass -SkipPrereqCheck to run anyway."
    }
    Write-Host "[prereq] All required curated inputs present." -ForegroundColor Green
}

$lockStream = $null
$lockPath = Join-Path $repoRoot "tmp\esg_pipeline_fast.lock"

try {
    if (-not $WhatIf) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $lockPath) | Out-Null
        try {
            $lockStream = [System.IO.File]::Open(
                $lockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::Read
            )
        }
        catch [System.IO.IOException] {
            throw "Another ESG pipeline runner is active. Wait for it to finish before writing the shared indexes."
        }
    }

    $overall = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host ""
    Write-Host "ESG pipeline - years $($targetYears -join ', '), parser workers $ParserWorkers, chunk workers $ChunkWorkers" -ForegroundColor Magenta

    # intake: inventories the whole raw root by design. It is an availability
    # catalogue, not a work list, so leaving it corpus-wide costs one cheap scan
    # and keeps the catalogue honest about what exists on disk.
    Invoke-PythonStage -Name "intake" -Arguments @(
        "esg/src/esg_intake_catalog.py",
        "--raw-root", $Paths.RAW_SUSTAINABILITY_DIR,
        "--ocr-root", $Paths.OCR_STAGING_DIR,
        "--catalog", $Paths.ESG_FILE_CATALOG_CSV,
        "--ocr-approval", $Paths.ESG_OCR_APPROVAL_CSV
    )

    Invoke-PythonStage -Name "parse" -Arguments @(
        "esg/src/pdf_parser.py",
        "--resume",
        "--root", $Paths.RAW_SUSTAINABILITY_DIR,
        "--ocr-root", $Paths.OCR_STAGING_DIR,
        "--out", $Paths.ESG_TEXT_DIR,
        "--index", $Paths.ESG_PARSE_INDEX_CSV,
        "--years", ($targetYears -join ","),
        "--workers", [string]$ParserWorkers,
        "--checkpoint-every", [string]$ParserCheckpointEvery
    )

    Invoke-PythonStage -Name "section" -Arguments @(
        "esg/src/section_splitter_esg.py",
        "--resume",
        "--input", $Paths.ESG_TEXT_DIR,
        "--out", $Paths.ESG_SECTIONS_DIR,
        "--index", $Paths.ESG_SECTIONS_INDEX_CSV,
        "--checkpoint-every", [string]$SectionCheckpointEvery
    )

    Invoke-PythonStage -Name "chunk" -Arguments @(
        "esg/src/esg_chunker.py",
        "--resume",
        "--input", $Paths.ESG_SECTIONS_DIR,
        "--out", $Paths.ESG_CHUNKS_DIR,
        "--index", $Paths.ESG_CHUNKS_INDEX_CSV,
        "--workers", [string]$ChunkWorkers,
        "--checkpoint-every", [string]$ChunkCheckpointEvery
    )

    Invoke-PythonStage -Name "layout" -Arguments @(
        "esg/src/esg_layout_qa.py",
        "--resume",
        "--parse-index", $Paths.ESG_PARSE_INDEX_CSV,
        "--out", $Paths.ESG_PAGE_LAYOUT_QA_CSV,
        "--workers", [string]$ParserWorkers
    )

    Invoke-PythonStage -Name "qa" -Arguments @(
        "esg/src/esg_pipeline_qa.py",
        "--out", $Paths.ESG_PIPELINE_QA_CSV,
        "--layout-audit", $Paths.ESG_PAGE_LAYOUT_QA_CSV
    )

    Invoke-PythonStage -Name "manifest" -Arguments @(
        "esg/scripts/build_esg_vector_manifest.py",
        "--chunks-index", $Paths.ESG_CHUNKS_INDEX_CSV,
        "--source-registry", $Paths.ESG_SOURCE_REGISTRY_CSV,
        "--layout-audit", $Paths.ESG_PAGE_LAYOUT_QA_CSV,
        "--out", $Paths.VECTOR_INDEX_MANIFEST_CSV
    )

    Invoke-PythonStage -Name "enrich" -Arguments @("esg/src/esg_p1_enrichment.py")

    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    Invoke-PythonStage -Name "provenance" -Arguments @(
        "esg/scripts/validate_esg_provenance.py",
        "--parse-index", $Paths.ESG_PARSE_INDEX_CSV,
        "--sections-index", $Paths.ESG_SECTIONS_INDEX_CSV,
        "--chunks-index", $Paths.ESG_CHUNKS_INDEX_CSV,
        "--json-out", "$($Paths.REPORTS_DIR)/esg_provenance_validation_year_$stamp.json"
    )

    if (-not $SkipTests) {
        # Both suites: esg/tests is this pipeline's, tests/ is the cross-cutting
    # one that polices the config split itself.
    Invoke-PythonStage -Name "tests" -Arguments @("-m", "pytest", "esg/tests", "tests", "-q")
    }

    $overall.Stop()
    if ($WhatIf) {
        Write-Host ""
        Write-Host "WhatIf complete: no pipeline files or indexes were changed." -ForegroundColor Yellow
    }
    else {
        Write-Host ""
        Write-Host "Pipeline finished in $($overall.Elapsed)." -ForegroundColor Green
    }
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}
