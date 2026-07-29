# PipelinePaths.ps1 -- read the pipeline layout from src/config.py.
#
# PowerShell cannot import config.py, so a runner that spells out
# "data/00_reference/esg_parse_index.csv" forks the layout into a second
# place and silently drifts the day a directory moves. Dot-source this
# instead:
#
#     . (Join-Path $PSScriptRoot "PipelinePaths.ps1")
#     $Paths = Import-PipelinePaths -RepoRoot $repoRoot -Python $python
#     $Paths.ESG_PARSE_INDEX_CSV      # -> data/00_reference/esg_parse_index.csv
#     $Paths.Absolute.ESG_PARSE_INDEX_CSV
#
# The default (top-level) form is repo-relative with forward slashes, which
# is what these runners already passed to the Python stages -- they all
# Set-Location to the repo root first. Use .Absolute only when a path must
# survive a working-directory change.

# No Set-StrictMode here on purpose: this file is dot-sourced, so a strict
# mode set here would leak into the caller's scope and change how the runners
# behave. Typos in $Paths keys are caught statically by
# tests/test_config_single_source_of_truth.py instead.

function Import-PipelinePaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$Python
    )

    $configPath = Join-Path $RepoRoot "src\config.py"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Cannot resolve pipeline paths: $configPath is missing."
    }

    $raw = & $Python $configPath --json
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot resolve pipeline paths: '$Python $configPath --json' exited $LASTEXITCODE."
    }

    $parsed = ($raw -join "`n") | ConvertFrom-Json

    $paths = @{}
    foreach ($property in $parsed.relative.PSObject.Properties) {
        $paths[$property.Name] = $property.Value
    }

    $absolute = @{}
    foreach ($property in $parsed.absolute.PSObject.Properties) {
        $absolute[$property.Name] = $property.Value
    }
    $paths["Absolute"] = $absolute

    return $paths
}
