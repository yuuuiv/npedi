$ErrorActionPreference = "Stop"

# Minimal path to a first curve HTML. This intentionally skips cargo release
# and container history; those can be resumed later with resume_after_interrupt.ps1.
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

function Get-QueueCount {
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
print(c.execute("select count(*) from container_enrichment_queue").fetchone()[0])
c.close()
"@
    $count = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read container_enrichment_queue"
    }
    return [int](($count | Select-Object -Last 1).ToString().Trim())
}

function Assert-LatestVgmSuccess {
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
row = c.execute("select status from crawl_run where job_name='vgm' order by started_at desc limit 1").fetchone()
print(row[0] if row else 'missing')
c.close()
"@
    $status = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read VGM crawl status"
    }
    $value = ($status | Select-Object -Last 1).ToString().Trim()
    if ($value -ne "success") {
        throw "VGM crawl status is $value; stop before aggregation and retry the VGM batch."
    }
}

$queue = Get-QueueCount
for ($offset = 0; $offset -lt $queue; $offset += 500) {
    Run-Npedi -Arguments @(
        "npedi.py", "crawl", "vgm", "--limit", "500", "--offset", "$offset", "--resume"
    )
    Assert-LatestVgmSuccess
}

Run-Npedi -Arguments @("npedi.py", "aggregate")
Run-Npedi -Arguments @("npedi.py", "build-curves")
Run-Npedi -Arguments @("npedi.py", "render")

Write-Host "`nFirst curve HTML is ready at export\timeseries_curves.html" -ForegroundColor Green
