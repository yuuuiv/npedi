$ErrorActionPreference = "Stop"

# Continue the recovery pipeline after cargo-release was blocked by the
# endpoint's unreliable vessel/voyage filters. This script never calls cargo.
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

function Test-PythonModule {
    param([Parameter(Mandatory)][string]$ModuleName)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Get-LatestStatus {
    param([Parameter(Mandatory)][string]$JobName)
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
row = c.execute(
    'select status from crawl_run where job_name=? order by started_at desc limit 1',
    ('$JobName',),
).fetchone()
print(row[0] if row else 'missing')
c.close()
"@
    $value = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read $JobName status"
    }
    return ($value | Select-Object -Last 1).ToString().Trim()
}

function Get-PendingHistoryCount {
    $code = @'
import sqlite3
c = sqlite3.connect('npedi.sqlite')
print(c.execute("select count(*) from container_enrichment_queue where status='pending'").fetchone()[0])
c.close()
'@
    $value = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read pending container-history queue"
    }
    return [int](($value | Select-Object -Last 1).ToString().Trim())
}

# Do not race a VGM batch that another terminal may still be running.
while ($true) {
    $vgmStatus = Get-LatestStatus -JobName "vgm"
    if ($vgmStatus -eq "success") { break }
    if ($vgmStatus -in @("partial", "failed")) {
        throw "VGM status is $vgmStatus. Fix that batch before continuing."
    }
    Write-Host "VGM status is $vgmStatus; waiting 30 seconds..." -ForegroundColor Yellow
    Start-Sleep -Seconds 30
}

$historyQueue = Get-PendingHistoryCount
for ($offset = 0; $offset -lt $historyQueue; $offset += 500) {
    Run-Npedi -Arguments @(
        "npedi.py", "crawl", "container-history",
        "--limit", "500", "--offset", "$offset", "--resume"
    )
    $historyStatus = Get-LatestStatus -JobName "container_history"
    if ($historyStatus -ne "success") {
        throw "container-history status is $historyStatus; fix this batch before continuing."
    }
}

Run-Npedi -Arguments @("npedi.py", "aggregate")
Run-Npedi -Arguments @("npedi.py", "build-curves")

if (Test-PythonModule -ModuleName "sklearn") {
    Run-Npedi -Arguments @("npedi.py", "cluster")
} else {
    Write-Warning "scikit-learn is not installed; cluster was skipped. Install it, then run: $py npedi.py cluster"
}

Run-Npedi -Arguments @("npedi.py", "quality-report")
Run-Npedi -Arguments @("npedi.py", "render")

Write-Host "`nContinuation completed. HTML: export\timeseries_curves.html" -ForegroundColor Green
