[CmdletBinding()]
param(
    [ValidateRange(1, 500)]
    [int]$Count = 50,

    # Set for a reproducible sample; omit for a fresh random draw.
    [int]$Seed,

    [ValidateRange(1, 8)]
    [int]$Workers = 4,

    [string]$DocList = "data/00_reference/esg_sample_docs.csv"
)

# One-shot sample pipeline: draws N random corpus PDFs, force re-parses them,
# then runs section -> chunk -> layout -> qa -> manifest -> tests via the fast
# runner (each stage resumes, so only changed documents are redone).
#   powershell -File scripts/run_esg_random_sample.ps1
#   powershell -File scripts/run_esg_random_sample.ps1 -Count 50 -Seed 42

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$pdfs = @(Get-ChildItem -LiteralPath "data/01_raw/sustainability" -Recurse -File |
    Where-Object { $_.Extension -in ".pdf", ".PDF" })
if ($pdfs.Count -lt $Count) {
    throw "Corpus has only $($pdfs.Count) PDFs; cannot sample $Count."
}

if ($PSBoundParameters.ContainsKey("Seed")) {
    $picked = $pdfs | Sort-Object FullName | Get-Random -Count $Count -SetSeed $Seed
}
else {
    $picked = $pdfs | Get-Random -Count $Count
}

$rows = foreach ($pdf in ($picked | Sort-Object FullName)) {
    [pscustomobject]@{
        ticker   = $pdf.Directory.Name
        pdf_file = $pdf.Name
    }
}
$rows | Export-Csv -Path $DocList -NoTypeInformation -Encoding UTF8
Write-Host "Sampled $Count PDFs across $(($rows.ticker | Sort-Object -Unique).Count) tickers -> $DocList" -ForegroundColor Green

& powershell -NoProfile -File scripts/run_esg_sample_reparse.ps1 -Workers $Workers -DocList $DocList
if ($LASTEXITCODE -ne 0) { throw "Sample re-parse failed with exit code $LASTEXITCODE." }

foreach ($stage in @("section", "chunk", "layout", "qa", "manifest", "tests")) {
    & powershell -NoProfile -File scripts/run_esg_pipeline_fast.ps1 -Stage $stage
    if ($LASTEXITCODE -ne 0) { throw "Stage $stage failed with exit code $LASTEXITCODE." }
}

Write-Host "Random-sample pipeline finished for $Count documents." -ForegroundColor Green
