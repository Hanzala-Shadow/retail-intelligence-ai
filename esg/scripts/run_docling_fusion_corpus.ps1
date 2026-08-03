<#
.SYNOPSIS
  Run the docling-fusion pipeline end to end over a folder of PDFs.

.DESCRIPTION
  One command for the whole chain: docling regions + PyMuPDF words -> fused
  page text -> parse index -> sections -> chunks.

  Two virtual environments are involved on purpose. docling pulls torch and
  transformers, which the production pipeline does not want, so the convert and
  fuse stages run in venv-docling and everything downstream runs in venv.

  The convert stage caches per document. Re-running after an interruption skips
  what already finished, so stopping this script is safe and resuming is just
  running it again. Pass -Force to reconvert from scratch.

  Nothing here writes to the production corpus. All output lands under
  -WorkRoot.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1

.EXAMPLE
  # stop converting after three hours, then finish the downstream stages
  powershell -ExecutionPolicy Bypass -File esg\scripts\run_docling_fusion_corpus.ps1 -TimeBudgetMin 180
#>
[CmdletBinding()]
param(
    [string] $PdfDir       = "outputs/docling_run4h/input_pdfs",
    [string] $WorkRoot     = "outputs/docling_run4h",
    [string] $RawDir       = "data/01_raw/sustainability",
    [string] $ParseIndexIn = "data/00_reference/esg_parse_index.csv",
    # Converting is the slow stage. The budget stops it at a document boundary
    # rather than mid-document, so the cache stays coherent.
    [int]    $TimeBudgetMin = 240,
    [switch] $Force,
    # Skip straight to the downstream stages when the cache is already built.
    [switch] $SkipConvert
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo

$pyDocling = Join-Path $repo "venv-docling\Scripts\python.exe"
$pyMain    = Join-Path $repo "venv\Scripts\python.exe"
foreach ($exe in @($pyDocling, $pyMain)) {
    if (-not (Test-Path $exe)) { throw "missing interpreter: $exe" }
}

$spike       = "esg\scripts\run_docling_gold_spike.py"
$bridge      = "esg\scripts\bridge_docling_to_pipeline.py"
$splitter    = "esg\src\section_splitter_esg.py"
$chunker     = "esg\src\esg_chunker.py"

$work        = Join-Path $WorkRoot "work"
$interim     = Join-Path $WorkRoot "interim/esg_text"
$parseIndex  = Join-Path $WorkRoot "esg_parse_index_v2.csv"
$sections    = Join-Path $WorkRoot "sections/esg"
$sectionsIdx = Join-Path $WorkRoot "sections_index.csv"
$chunks      = Join-Path $WorkRoot "chunks/esg"
$chunksIdx   = Join-Path $WorkRoot "chunks_index.csv"

$pdfCount = @(Get-ChildItem -Path $PdfDir -Filter *.pdf -Recurse).Count
if ($pdfCount -eq 0) { throw "no PDFs under $PdfDir" }

$started = Get-Date
Write-Host ""
Write-Host "docling fusion pipeline" -ForegroundColor Cyan
Write-Host "  documents   : $pdfCount"
Write-Host "  input       : $PdfDir"
Write-Host "  output root : $WorkRoot"
Write-Host "  convert cap : $TimeBudgetMin min"
Write-Host ""

function Invoke-Stage {
    param([string] $Name, [string] $Exe, [string[]] $StageArgs)

    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Host "  $Name" -ForegroundColor Yellow
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    $t0 = Get-Date
    & $Exe @StageArgs
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    Write-Host "  -> $Name done in $mins min" -ForegroundColor Green
    Write-Host ""
}

if (-not $SkipConvert) {
    # Stage 1. The models run here; everything after this is cheap.
    $convertArgs = @(
        $spike, "convert",
        "--pdf-dir", $PdfDir,
        "--work-dir", $work,
        "--all-pages",
        "--time-budget-min", $TimeBudgetMin
    )
    if ($Force) { $convertArgs += "--force" }
    Invoke-Stage "1/5 convert  (docling layout + tables)" $pyDocling $convertArgs

    # Stage 2. Fusion proper: docling decides regions and reading order,
    # PyMuPDF supplies the words that fill them.
    # --pdf-dir is not optional here. Without it the stage quietly processes
    # only the documents it can find and still prints a clean summary.
    Invoke-Stage "2/5 fuse     (regions + words -> page text)" $pyDocling @(
        $spike, "fuse",
        "--pdf-dir", $PdfDir,
        "--work-dir", $work
    )
}

# Stage 3. Fused pages -> the .txt/.pages.csv/.headings.csv layout the rest of
# the pipeline reads, plus a v2 parse index. Documents production never parsed
# get a synthesised row rather than being dropped.
Invoke-Stage "3/5 bridge   (-> pipeline layout + parse index)" $pyMain @(
    $bridge,
    "--work-dir", $work,
    "--out", $interim,
    "--raw-dir", $RawDir,
    "--parse-index-in", $ParseIndexIn,
    "--parse-index-out", $parseIndex
)

# Stage 4. Sectioning against docling headings. --experimental-sectioning is
# required: without it run() delegates to the legacy splitter and none of the
# heading-restriction or vocabulary work applies.
Invoke-Stage "4/5 sections (topic split at docling headings)" $pyMain @(
    $splitter,
    "--input", $interim,
    "--out", $sections,
    "--index", $sectionsIdx,
    "--experimental-sectioning"
)

# Stage 5. Chunking, with the retrieval gates: rag_action ->
# include_in_esg_index -> citation_ready.
Invoke-Stage "5/5 chunks   (+ retrieval gating)" $pyMain @(
    $chunker,
    "--input", $sections,
    "--out", $chunks,
    "--index", $chunksIdx,
    "--sections-index", $sectionsIdx,
    "--parse-index", $parseIndex
)

$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host ("=" * 64) -ForegroundColor DarkGray
Write-Host "  summary" -ForegroundColor Cyan
Write-Host ("=" * 64) -ForegroundColor DarkGray
& $pyMain "esg\scripts\summarise_fusion_run.py" --parse-index $parseIndex --chunks-index $chunksIdx
Write-Host ""
Write-Host "total elapsed: $elapsed min" -ForegroundColor Cyan
Write-Host "chunks index : $chunksIdx"
