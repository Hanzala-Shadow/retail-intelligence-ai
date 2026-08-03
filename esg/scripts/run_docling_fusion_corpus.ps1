<#
.SYNOPSIS
  Run the docling-fusion pipeline end to end over a folder of PDFs.

.DESCRIPTION
  One command for the whole chain: docling regions + PyMuPDF words -> fused
  page text -> parse index -> sections -> chunks.

  Two virtual environments are involved on purpose. docling pulls torch and
  transformers, which the production pipeline does not want, so the convert and
  fuse stages run in venv-docling and everything downstream runs in venv.

  The convert stage caches per document, and a document counts as cached only
  when both halves of its cache entry are present and non-empty. Cache files are
  written atomically, so a stop mid-write leaves nothing to trust by mistake.
  Re-running after an interruption skips what already finished and reconverts
  anything half-written, so stopping this script is safe and resuming is just
  running it again. Pass -Force to reconvert from scratch.

  Convert also reports documents whose text density is too low to be real --
  a picture-heavy PDF with no text layer converts quickly and looks successful.
  Anything listed as NEEDS OCR should be reconverted with -WithOcr.

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
    # rather than mid-document, so the cache stays coherent. It applies per
    # worker, and workers run concurrently, so this is wall-clock time.
    [int]    $TimeBudgetMin = 165,
    # Keep this at 1. Measured on MUSA-MURPHY-2024, 36 pages, same document:
    #   1 process  9.91 s/page  -> 0.101 pages/s
    #   2 workers 20.26 s/page  -> 0.099 pages/s
    # Docling already saturates the cores internally, so extra processes split
    # the same CPU and gain nothing. Four workers exhausted memory outright
    # (std::bad_alloc) because each loads its own copy of the models.
    [int]    $Workers = 1,
    # Chunk planning is CPU-parallel and keeps all file/index writes in the
    # parent process. Keep this separate from the Docling conversion workers.
    [ValidateRange(1, 128)]
    [int]    $ChunkWorkers = 8,
    # Docling's OCR costs ~80% of convert time and changes nothing here, because
    # fusion takes its words from PyMuPDF and discards docling's text. Measured
    # on the same document: 9.91 -> 2.04 s/page, and the fused output differed
    # by two blank lines. A scanned page with no text layer yields nothing
    # either way; that is what the quality guards are for.
    [switch] $WithOcr,
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
Write-Host "  convert cap : $TimeBudgetMin min (wall clock, $Workers workers)"
Write-Host "  chunking    : $ChunkWorkers workers"
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
    #
    # There is no CUDA on this machine, so docling runs on CPU and a single
    # converter leaves most of the box idle. Workers take disjoint document
    # lists and share one cache directory, which is safe because each writes
    # <stem>.json. Threads are divided rather than added: four processes each
    # grabbing all 16 threads would oversubscribe and thrash.
    # Single worker is the measured configuration, and it was measured with
    # torch's own thread defaults. Overriding them here would be a change no
    # timing covers, so only divide threads when actually sharding.
    $threads = [Environment]::ProcessorCount
    if ($Workers -gt 1) {
        $threads = [math]::Max(1, [int]([Environment]::ProcessorCount / $Workers))
        $env:OMP_NUM_THREADS = $threads
        $env:MKL_NUM_THREADS = $threads
    }
    $env:TOKENIZERS_PARALLELISM = "false"

    $logDir = Join-Path $WorkRoot "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null

    Write-Host ("=" * 64) -ForegroundColor DarkGray
    $ocrTag = if ($WithOcr) { "OCR on" } else { "OCR off" }
    Write-Host "  1/5 convert  ($Workers worker(s) x $threads threads, $ocrTag)" -ForegroundColor Yellow
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Host "  budget $TimeBudgetMin min per worker; logs in $logDir"
    $t0 = Get-Date

    $procs = @()
    for ($s = 0; $s -lt $Workers; $s++) {
        $shardArgs = @(
            $spike, "convert",
            "--pdf-dir", $PdfDir,
            "--work-dir", $work,
            "--all-pages",
            "--shards", $Workers,
            "--shard", $s,
            "--time-budget-min", $TimeBudgetMin
        )
        if (-not $WithOcr) { $shardArgs += "--no-ocr" }
        if ($Force) { $shardArgs += "--force" }
        $n = $s + 1
        $procs += Start-Process -FilePath $pyDocling -ArgumentList $shardArgs `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput (Join-Path $logDir "convert_shard$n.log") `
            -RedirectStandardError  (Join-Path $logDir "convert_shard$n.err")
        Write-Host "    shard $n started (pid $($procs[-1].Id))"
    }

    # A worker that dies should not take the run down: the others keep
    # converting and everything cached still flows downstream.
    $procs | Wait-Process
    foreach ($i in 0..($procs.Count - 1)) {
        $code = $procs[$i].ExitCode
        $tag  = if ($code -eq 0) { "ok" } else { "EXIT $code -- see logs" }
        Write-Host "    shard $($i + 1): $tag"
    }
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    $cached = @(Get-ChildItem -Path (Join-Path $work "docling_json") -Filter "*.pages.json" -ErrorAction SilentlyContinue).Count
    Write-Host "  -> convert done in $mins min; $cached document(s) cached" -ForegroundColor Green
    Write-Host ""

    # Stage 2. Fusion proper: docling decides regions and reading order,
    # PyMuPDF supplies the words that fill them.
    # --pdf-dir is not optional here. Without it the stage quietly processes
    # only the documents it can find and still prints a clean summary.
    # --table-mode grid rebuilds a table from docling's cell boxes instead of
    # emitting its words in reading order. Measured over the 86-document run:
    # 1,086 of 1,139 tables (95%) have a coherent grid, pipe-delimited rows go
    # from 242 to 10,603, chunks carrying a table from 14 to 1,247, and NOT ONE
    # word is lost -- the extra tokens are delimiters. The 12 tables whose cell
    # assignment is scrambled are declined by _grid_is_coherent and fall back to
    # words automatically, so this degrades per table rather than per corpus.
    # Without this flag the stage silently reverts to flattened words.
    Invoke-Stage "2/5 fuse     (regions + words -> page text)" $pyDocling @(
        $spike, "fuse",
        "--pdf-dir", $PdfDir,
        "--work-dir", $work,
        "--table-mode", "grid"
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
    "--parse-index", $parseIndex,
    "--workers", $ChunkWorkers
)

$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host ("=" * 64) -ForegroundColor DarkGray
Write-Host "  summary" -ForegroundColor Cyan
Write-Host ("=" * 64) -ForegroundColor DarkGray
& $pyMain "esg\scripts\summarise_fusion_run.py" --parse-index $parseIndex --chunks-index $chunksIdx
Write-Host ""
Write-Host "total elapsed: $elapsed min" -ForegroundColor Cyan
Write-Host "chunks index : $chunksIdx"
