[CmdletBinding()]
param(
    [ValidateSet("all", "intake", "parse", "remediate", "section", "chunk", "layout", "vlm", "qa", "manifest", "enrich", "validate", "tests")]
    [string]$Stage = "all",

    [string]$Ticker,
    [string]$PdfFile,
    [string]$PdfStem,

    [ValidateRange(1, 8)]
    [int]$ParserWorkers = 4,

    [ValidateRange(1, 12)]
    [int]$ChunkWorkers = 6,

    [ValidateRange(1, 1000)]
    [int]$ParserCheckpointEvery = 10,

    [ValidateRange(1, 1000)]
    [int]$SectionCheckpointEvery = 25,

    [ValidateRange(1, 5000)]
    [int]$ChunkCheckpointEvery = 500,

    [switch]$EnableVlmIntegration,

    [switch]$EnablePyMuPdfParser,

    [string]$VlmDir = "data/04_vlm",

    [switch]$Force,
    [switch]$WhatIf
)

# Safe local runner for the Ryzen 7 5825U / 24 GB development laptop.
# It changes orchestration only: parsing/sectioning/chunking logic, source
# hashing, resume validation, atomic writes, QA, provenance checks, and tests
# remain enabled. Batched checkpoints reduce repeated full-index rewrites; a
# crash can cause at most the current batch to be checked again on resume.
#
# Full restart-safe run (VLM API calls stay off):
#   scripts\run_esg_pipeline_fast.cmd
# Targeted forced repair:
#   scripts\run_esg_pipeline_fast.cmd -Ticker LOVE -PdfFile "report.pdf" -Force
# Preview commands without writes:
#   scripts\run_esg_pipeline_fast.cmd -WhatIf
# Use already verified VLM artifacts in the manifest (still no API calls):
#   scripts\run_esg_pipeline_fast.cmd -EnableVlmIntegration
# Opt in to the experimental table-aware parser for a reviewed scope:
#   scripts\run_esg_pipeline_fast.cmd -Stage parse -Ticker WMT -PdfFile "report.pdf" -EnablePyMuPdfParser -Force

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python was not found at $python"
}

if ($Ticker) {
    $Ticker = $Ticker.Trim().ToUpperInvariant()
}
if (($PdfFile -or $PdfStem) -and -not $Ticker) {
    throw "-PdfFile and -PdfStem require -Ticker so a scoped run cannot touch another company."
}
if ($Force -and -not $Ticker) {
    throw "A full-corpus forced rebuild is intentionally blocked. Supply -Ticker (and preferably -PdfFile/-PdfStem)."
}
if ($EnablePyMuPdfParser -and -not $Ticker) {
    throw "The table-aware PyMuPDF parser is opt-in and must be scoped with -Ticker (and preferably -PdfFile)."
}
if ($PdfFile -and -not $PdfStem) {
    $PdfStem = [System.IO.Path]::GetFileNameWithoutExtension($PdfFile)
}

$lockStream = $null
$lockPath = Join-Path $repoRoot "tmp\esg_pipeline_fast.lock"

function Format-Command {
    param([string]$Executable, [string[]]$Arguments)

    $escaped = foreach ($argument in $Arguments) {
        if ($argument -match '[\s\"]') {
            '"' + ($argument -replace '"', '\"') + '"'
        }
        else {
            $argument
        }
    }
    return ((@($Executable) + $escaped) -join " ")
}

function Invoke-PythonStage {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    $commandText = Format-Command -Executable $python -Arguments $Arguments
    Write-Host ""
    Write-Host "[$Name] $commandText" -ForegroundColor Cyan
    if ($WhatIf) {
        return
    }

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    & $python @Arguments
    $exitCode = $LASTEXITCODE
    $timer.Stop()
    if ($exitCode -ne 0) {
        throw "$Name failed with exit code $exitCode after $($timer.Elapsed)."
    }
    Write-Host "[$Name] completed in $($timer.Elapsed)." -ForegroundColor Green
}

function Add-ScopedArguments {
    param([System.Collections.Generic.List[string]]$Arguments)

    if ($Ticker) {
        $Arguments.Add("--ticker")
        $Arguments.Add($Ticker)
    }
}

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
            throw "Another fast ESG pipeline runner is active. Wait for it to finish before writing the shared indexes."
        }
    }

    $runParse = $Stage -in @("all", "parse")
    $runIntake = $Stage -in @("all", "intake")
    # Page-level OCR remediation rewrites parsed text and force-rebuilds the
    # doc's sections/chunks, so it refuses to run corpus-wide. In a full run it
    # is skipped unless a scope was supplied; asked for explicitly, it fails
    # here instead of part-way through the pipeline.
    $hasScope = [bool]($Ticker -or $PdfStem -or $PdfFile)
    if ($Stage -eq "remediate" -and -not $hasScope) {
        throw "-Stage remediate is scope-only. Supply -Ticker (and preferably -PdfFile/-PdfStem)."
    }
    $runRemediate = ($Stage -eq "remediate") -or ($Stage -eq "all" -and $hasScope)
    $runSection = $Stage -in @("all", "section")
    $runChunk = $Stage -in @("all", "chunk")
    $runLayout = $Stage -in @("all", "layout")
    $runVlm = $Stage -in @("all", "vlm")
    $runQa = $Stage -in @("all", "qa")
    $runManifest = $Stage -in @("all", "manifest", "vlm")
    $runEnrich = $Stage -in @("all", "enrich")
    $runValidate = $Stage -in @("all", "validate")
    $runTests = $Stage -in @("all", "tests")

    if ($runIntake) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/esg_intake_catalog.py")
        $arguments.Add("--raw-root")
        $arguments.Add("data/01_raw/sustainability")
        $arguments.Add("--raw-root")
        $arguments.Add("data/01_raw/sustainability_other")
        $arguments.Add("--ocr-root")
        $arguments.Add("data/02_interim/ocr_staging")
        $arguments.Add("--catalog")
        $arguments.Add("data/00_reference/esg_file_catalog.csv")
        $arguments.Add("--ocr-approval")
        $arguments.Add("data/00_reference/esg_ocr_approval.csv")
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        Invoke-PythonStage -Name "intake" -Arguments $arguments.ToArray()
    }

    if ($runParse) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/pdf_parser.py")
        $arguments.Add("--resume")
        $arguments.Add("--root")
        $arguments.Add("data/01_raw/sustainability")
        $arguments.Add("--ocr-root")
        $arguments.Add("data/02_interim/ocr_staging")
        $arguments.Add("--out")
        $arguments.Add("data/02_interim/esg_text")
        $arguments.Add("--index")
        $arguments.Add("data/00_reference/esg_parse_index.csv")
        $arguments.Add("--workers")
        $arguments.Add([string]$ParserWorkers)
        $arguments.Add("--checkpoint-every")
        $arguments.Add([string]$ParserCheckpointEvery)
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfStem)
        }
        if ($EnablePyMuPdfParser) {
            $arguments.Add("--prefer-pymupdf")
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "parse" -Arguments $arguments.ToArray()

        # Second parse root: supplementary reports mirrored from the Drive
        # "Other Sustainability Related Reports" folder. Same output tree and
        # index; the parser upserts by (ticker, pdf_file), so the two roots
        # never prune each other's rows. Missing root is a clean no-op.
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/pdf_parser.py")
        $arguments.Add("--resume")
        $arguments.Add("--root")
        $arguments.Add("data/01_raw/sustainability_other")
        $arguments.Add("--ocr-root")
        $arguments.Add("data/02_interim/ocr_staging")
        $arguments.Add("--out")
        $arguments.Add("data/02_interim/esg_text")
        $arguments.Add("--index")
        $arguments.Add("data/00_reference/esg_parse_index.csv")
        $arguments.Add("--workers")
        $arguments.Add([string]$ParserWorkers)
        $arguments.Add("--checkpoint-every")
        $arguments.Add([string]$ParserCheckpointEvery)
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfStem)
        }
        if ($EnablePyMuPdfParser) {
            $arguments.Add("--prefer-pymupdf")
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "parse-other" -Arguments $arguments.ToArray()
    }

    if ($runRemediate) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/pipeline_ocr_remediation_stage.py")
        $arguments.Add("--parse-index")
        $arguments.Add("data/00_reference/esg_parse_index.csv")
        $arguments.Add("--sections-index")
        $arguments.Add("data/00_reference/esg_sections_index.csv")
        $arguments.Add("--chunks-index")
        $arguments.Add("data/00_reference/esg_chunks_index.csv")
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "remediate" -Arguments $arguments.ToArray()
    }
    elseif ($Stage -eq "all") {
        Write-Host ""
        Write-Host "[remediate] Skipped: page-level OCR remediation is scope-only. Run -Stage remediate -Ticker <T> -PdfFile <file> for a held document." -ForegroundColor Yellow
    }

    if ($runSection) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/section_splitter_esg.py")
        $arguments.Add("--resume")
        $arguments.Add("--input")
        $arguments.Add("data/02_interim/esg_text")
        $arguments.Add("--out")
        $arguments.Add("data/03_sections/esg")
        $arguments.Add("--index")
        $arguments.Add("data/00_reference/esg_sections_index.csv")
        $arguments.Add("--checkpoint-every")
        $arguments.Add([string]$SectionCheckpointEvery)
        Add-ScopedArguments -Arguments $arguments
        if ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "section" -Arguments $arguments.ToArray()
    }

    if ($runChunk) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/esg_chunker.py")
        $arguments.Add("--resume")
        $arguments.Add("--input")
        $arguments.Add("data/03_sections/esg")
        $arguments.Add("--out")
        $arguments.Add("data/04_chunks/esg")
        $arguments.Add("--index")
        $arguments.Add("data/00_reference/esg_chunks_index.csv")
        $arguments.Add("--workers")
        $arguments.Add([string]$ChunkWorkers)
        $arguments.Add("--checkpoint-every")
        $arguments.Add([string]$ChunkCheckpointEvery)
        Add-ScopedArguments -Arguments $arguments
        if ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "chunk" -Arguments $arguments.ToArray()
    }

    if ($runLayout) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/esg_layout_qa.py")
        $arguments.Add("--resume")
        $arguments.Add("--parse-index")
        $arguments.Add("data/00_reference/esg_parse_index.csv")
        $arguments.Add("--out")
        $arguments.Add("data/00_reference/esg_page_layout_qa.csv")
        $arguments.Add("--workers")
        $arguments.Add([string]$ParserWorkers)
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfStem)
        }
        if ($Force) {
            $arguments.Add("--force")
        }
        Invoke-PythonStage -Name "layout" -Arguments $arguments.ToArray()
    }

    if ($runVlm) {
        Write-Host ""
        if ($EnableVlmIntegration) {
            Write-Host "[vlm] Verified local VLM artifacts will be integrated by the manifest stage. No VLM API call will run." -ForegroundColor Cyan
        }
        else {
            Write-Host "[vlm] Integration is disabled. Use -EnableVlmIntegration only after local VLM artifacts are verified. No VLM API call will run." -ForegroundColor Yellow
        }
    }

    if ($runQa) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("src/esg_pipeline_qa.py")
        $arguments.Add("--out")
        $arguments.Add("data/00_reference/esg_pipeline_qa.csv")
        $arguments.Add("--layout-audit")
        $arguments.Add("data/00_reference/esg_page_layout_qa.csv")
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        Invoke-PythonStage -Name "qa" -Arguments $arguments.ToArray()
    }

    if ($runManifest) {
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("scripts/build_esg_vector_manifest.py")
        $arguments.Add("--chunks-index")
        $arguments.Add("data/00_reference/esg_chunks_index.csv")
        $arguments.Add("--source-registry")
        $arguments.Add("data/00_reference/esg_source_registry.csv")
        $arguments.Add("--layout-audit")
        $arguments.Add("data/00_reference/esg_page_layout_qa.csv")
        $arguments.Add("--out")
        $arguments.Add("data/00_reference/vector_index_manifest.csv")
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        if ($EnableVlmIntegration) {
            $arguments.Add("--vlm-dir")
            $arguments.Add($VlmDir)
        }
        Invoke-PythonStage -Name "manifest" -Arguments $arguments.ToArray()
    }

    if ($runEnrich) {
        # P1 metadata enrichment: additive-only, deterministic. Rebuilds the
        # enriched chunk index and embedding_text_plain copies from the current
        # chunks index; the base index and chunk files are never modified.
        Invoke-PythonStage -Name "enrich" -Arguments @(
            "src/esg_p1_enrichment.py"
        )
    }

    if ($runValidate) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add("scripts/validate_esg_provenance.py")
        $arguments.Add("--parse-index")
        $arguments.Add("data/00_reference/esg_parse_index.csv")
        $arguments.Add("--sections-index")
        $arguments.Add("data/00_reference/esg_sections_index.csv")
        $arguments.Add("--chunks-index")
        $arguments.Add("data/00_reference/esg_chunks_index.csv")
        $arguments.Add("--json-out")
        $arguments.Add("reports/esg_provenance_validation_fast_$stamp.json")
        Add-ScopedArguments -Arguments $arguments
        if ($PdfFile) {
            $arguments.Add("--pdf-file")
            $arguments.Add($PdfFile)
        }
        elseif ($PdfStem) {
            $arguments.Add("--pdf-stem")
            $arguments.Add($PdfStem)
        }
        Invoke-PythonStage -Name "provenance" -Arguments $arguments.ToArray()
    }

    if ($runTests) {
        # pytest collects both unittest-style and pytest-style tests (the VLM stage
        # tests use pytest fixtures and are invisible to unittest discover).
        Invoke-PythonStage -Name "tests" -Arguments @(
            "-m", "pytest", "tests", "-q"
        )
    }

    if ($WhatIf) {
        Write-Host ""
        Write-Host "WhatIf complete: no pipeline files or indexes were changed." -ForegroundColor Yellow
    }
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}
