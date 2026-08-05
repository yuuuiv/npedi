param(
    [int]$PollSeconds = 30,
    [int]$TimeoutMinutes = 240
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

function Get-LatestVgmState {
    $code = @"
import json
import sqlite3
c = sqlite3.connect('npedi.sqlite')
c.row_factory = sqlite3.Row
row = c.execute(
    "select status,started_at,finished_at,error_summary from crawl_run where job_name='vgm' order by started_at desc limit 1"
).fetchone()
print(json.dumps(dict(row) if row else {"status":"missing"}))
c.close()
"@
    $raw = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read VGM crawl state"
    }
    return (($raw | Select-Object -Last 1).ToString().Trim() | ConvertFrom-Json)
}

$watchStarted = [DateTimeOffset]::UtcNow
$deadline = (Get-Date).ToUniversalTime().AddMinutes($TimeoutMinutes)
Write-Host "Waiting for a new successful VGM crawl before creating the preview snapshot..." -ForegroundColor Yellow

while ($true) {
    $state = Get-LatestVgmState
    $startedAt = if ($state.started_at) { [DateTimeOffset]::Parse($state.started_at) } else { $null }
    $isNewRun = $startedAt -and $startedAt -ge $watchStarted.AddMinutes(-2)

    if ($isNewRun -and $state.status -eq "success") {
        break
    }
    if ($isNewRun -and $state.status -in @("partial", "failed")) {
        throw "The new VGM crawl ended with status $($state.status). Fix it before creating the preview."
    }
    if ((Get-Date).ToUniversalTime() -ge $deadline) {
        throw "Timed out waiting for VGM success."
    }

    Write-Host ("VGM status: {0}; next check in {1}s" -f $state.status, $PollSeconds) -ForegroundColor DarkYellow
    Start-Sleep -Seconds $PollSeconds
}

$previewDir = Join-Path $repo "export\preview"
New-Item -ItemType Directory -Force -Path $previewDir | Out-Null
$previewDb = Join-Path $previewDir "npedi-preview.sqlite"
$env:NPEDI_PREVIEW_DB = $previewDb

# SQLite backup gives the analysis process a consistent snapshot while the
# live crawler continues writing to npedi.sqlite.
$backupCode = @'
import os
import sqlite3
source = sqlite3.connect('npedi.sqlite')
target = sqlite3.connect(os.environ['NPEDI_PREVIEW_DB'])
source.backup(target)
target.close()
source.close()
'@
$backupCode | & $py -
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create the preview SQLite snapshot"
}

$oldExportDir = $env:EXPORT_DIR
$env:EXPORT_DIR = $previewDir
try {
    Run-Npedi -Arguments @("npedi.py", "--db", $previewDb, "aggregate")
    Run-Npedi -Arguments @("npedi.py", "--db", $previewDb, "build-curves")
    Run-Npedi -Arguments @("npedi.py", "--db", $previewDb, "render")
} finally {
    if ($null -eq $oldExportDir) {
        Remove-Item Env:EXPORT_DIR -ErrorAction SilentlyContinue
    } else {
        $env:EXPORT_DIR = $oldExportDir
    }
    Remove-Item Env:NPEDI_PREVIEW_DB -ErrorAction SilentlyContinue
}

Write-Host "`nPreview curve HTML is ready at $previewDir\timeseries_curves.html" -ForegroundColor Green
