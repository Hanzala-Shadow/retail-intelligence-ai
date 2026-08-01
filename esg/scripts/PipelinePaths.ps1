# PipelinePaths.ps1 -- read the pipeline layout from common/config.py.
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
#
# Why common/config.py and not esg/config.py: splitting the pipelines split
# config.py three ways, and path_constants() walks one module namespace. Point
# this at a pipeline config and the table arrives missing every constant the
# other pipeline owns. `common/config.py --json` prints the MERGED table -- the
# same 73 keys, with the same values, that the single pre-split config.py
# printed -- so no runner has to know which config owns a given name.
#
# No Set-StrictMode here on purpose: this file is dot-sourced, so a strict
# mode set here would leak into the caller's scope and change how the runners
# behave. Typos in $Paths keys are caught statically by
# tests/test_config_single_source_of_truth.py instead.

function Import-PipelinePaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$Python,

        # Keys this runner cannot proceed without. Checked here, at load time,
        # so a shrunken table fails on line one instead of two hours into a
        # corpus run. Callers under Set-StrictMode already throw on a missing
        # key at the point of use; this moves that failure to startup.
        [string[]]$Require
    )

    $configPath = Join-Path $RepoRoot "common\config.py"
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

    # A merged table that lost a pipeline is the failure this guard exists for:
    # config.py imports both pipeline configs, and if one raised, the JSON is
    # still well-formed -- just short. Sanity-check the shape before any stage
    # runs. The floor is deliberately well under the real count (73) so adding
    # or retiring a constant does not trip it.
    if ($paths.Count -lt 40) {
        throw ("Pipeline path table looks truncated: $($paths.Count) keys from " +
               "$configPath. Expected the merged shared + ESG + filings table. " +
               "Run '$Python $configPath --json' by hand to see what it emitted.")
    }
    if ($paths.Count -ne $absolute.Count) {
        throw ("Pipeline path table is inconsistent: $($paths.Count) relative " +
               "vs $($absolute.Count) absolute keys from $configPath.")
    }

    if ($Require) {
        $missing = @($Require | Where-Object { -not $paths.ContainsKey($_) })
        if ($missing.Count -gt 0) {
            throw ("Pipeline path table is missing required key(s): " +
                   "$($missing -join ', '). Add the constant to the right " +
                   "config (common/, esg/ or filings/) and re-run.")
        }
    }

    $paths["Absolute"] = $absolute

    return $paths
}
