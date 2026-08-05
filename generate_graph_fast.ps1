param(
    [ValidateSet("week", "day")]
    [string]$Granularity = "week"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repo

$py = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else {
    "python"
}

function Run-Npedi {
    param([Parameter(Mandatory)][string[]]$Arguments)
    Write-Host ("`n> {0} {1}" -f $py, ($Arguments -join " ")) -ForegroundColor Cyan
    & $py @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit code $LASTEXITCODE): $($Arguments -join ' ')"
    }
}

$env:NPEDI_CURVE_GRANULARITY = $Granularity
$countCode = @'
import os
import sqlite3
c = sqlite3.connect('npedi.sqlite')
print(c.execute(
    'select count(*) from mart_curve_series where granularity=?',
    (os.environ['NPEDI_CURVE_GRANULARITY'],),
).fetchone()[0])
c.close()
'@
$curveCount = $countCode | & $py -
Remove-Item Env:NPEDI_CURVE_GRANULARITY -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { throw "Failed to inspect existing curves." }
$curveCount = [int](($curveCount | Select-Object -Last 1).ToString().Trim())

if ($curveCount -eq 0) {
    Write-Host "No $Granularity curves exist; building only that granularity from existing Gold tables..." -ForegroundColor Yellow
    if ($Granularity -eq "week") {
        # The CLI builds both granularities. This branch is only needed for a
        # database that has never materialized curves.
        Run-Npedi -Arguments @("npedi.py", "build-curves")
    } else {
        Run-Npedi -Arguments @("npedi.py", "build-curves")
    }
}

$output = Join-Path $repo "export\timeseries_curves_quick_$Granularity.html"
Run-Npedi -Arguments @(
    "npedi.py", "render",
    "--granularity", $Granularity,
    "--output", $output
)

$file = Get-Item -LiteralPath $output
Write-Host ("`nQuick chart ready: {0} ({1:N1} MB)" -f $file.FullName, ($file.Length / 1MB)) -ForegroundColor Green
