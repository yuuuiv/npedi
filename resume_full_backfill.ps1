param(
    [switch]$SkipAnalytics,
    [switch]$SkipQualityReport,
    [switch]$SkipRemoteContainerEnrichment
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repo

$py = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else {
    "python"
}

$DatabaseJobNames = @{
    "vessel-plan" = "vessel_plan"
    "container-notice" = "container_notice"
    "container-history" = "container_history"
    "transshipment" = "transshipment"
    "vgm" = "vgm"
}

function Run-Npedi {
    param([Parameter(Mandatory)][string[]]$Arguments)
    Write-Host ("`n> {0} {1}" -f $py, ($Arguments -join " ")) -ForegroundColor Cyan
    & $py @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit code $LASTEXITCODE): $($Arguments -join ' ')"
    }
}

function Get-LatestStatus {
    param([Parameter(Mandatory)][string]$JobName)
    $dbJobName = $DatabaseJobNames[$JobName]
    $env:NPEDI_STATUS_JOB = $dbJobName
    $code = @'
import os
import sqlite3
c = sqlite3.connect('npedi.sqlite')
row = c.execute(
    'select status from crawl_run where job_name=? order by started_at desc limit 1',
    (os.environ['NPEDI_STATUS_JOB'],),
).fetchone()
print(row[0] if row else 'missing')
c.close()
'@
    try {
        $value = $code | & $py -
        if ($LASTEXITCODE -ne 0) { throw "Failed to read $JobName status." }
        return ($value | Select-Object -Last 1).ToString().Trim()
    } finally {
        Remove-Item Env:NPEDI_STATUS_JOB -ErrorAction SilentlyContinue
    }
}

function Get-QueueCount {
    param([ValidateSet("all", "pending")][string]$Status = "all")
    $env:NPEDI_QUEUE_STATUS = $Status
    $code = @'
import os
import sqlite3
c = sqlite3.connect('npedi.sqlite')
where = " where status='pending'" if os.environ['NPEDI_QUEUE_STATUS'] == 'pending' else ''
print(c.execute('select count(*) from container_enrichment_queue' + where).fetchone()[0])
c.close()
'@
    try {
        $value = $code | & $py -
        if ($LASTEXITCODE -ne 0) { throw "Failed to read enrichment queue." }
        return [int](($value | Select-Object -Last 1).ToString().Trim())
    } finally {
        Remove-Item Env:NPEDI_QUEUE_STATUS -ErrorAction SilentlyContinue
    }
}

function Run-Crawl {
    param(
        [Parameter(Mandatory)][string]$JobName,
        [string[]]$ExtraArguments = @()
    )
    Run-Npedi -Arguments (@("npedi.py", "crawl", $JobName) + $ExtraArguments)
    $status = Get-LatestStatus -JobName $JobName
    if ($status -ne "success") {
        throw "$JobName ended with status=$status. Re-run this script after correcting the persisted crawl error."
    }
}

function Resume-StageIfNeeded {
    param([Parameter(Mandatory)][string]$JobName)
    $status = Get-LatestStatus -JobName $JobName
    if ($status -eq "success") {
        Write-Host "Skipping completed stage: $JobName" -ForegroundColor DarkGreen
        return
    }
    Write-Host "Resuming stage $JobName from its database checkpoint (previous status: $status)..." -ForegroundColor Yellow
    Run-Crawl -JobName $JobName -ExtraArguments @("--resume")
}

# Completed stages are skipped; interrupted stages resume from SQLite.
Resume-StageIfNeeded -JobName "vessel-plan"
Resume-StageIfNeeded -JobName "container-notice"
Resume-StageIfNeeded -JobName "transshipment"

# Build the complete catalog from the already-collected CODECO history. The
# remote enrichment is intentionally delegated to the queue-head consumer;
# offset loops are unsafe because the pending set shrinks after each batch.
Run-Npedi -Arguments @("npedi.py", "seed-container-catalog")
if (-not $SkipRemoteContainerEnrichment) {
    & (Join-Path $repo "backfill_full_container_enrichment.ps1") -Target all -BatchSize 500 -SkipCatalogSeed
    if ($LASTEXITCODE -ne 0) { throw "Full container enrichment failed" }
} else {
    Write-Warning "Skipping remote VGM/container-history enrichment. Full gate history remains available locally."
}

Write-Warning "cargo-release is intentionally excluded: the live endpoint ignores vessel/voyage filters, so a trustworthy full backfill is not currently possible."

if (-not $SkipAnalytics) {
    Run-Npedi -Arguments @("npedi.py", "aggregate")
    Run-Npedi -Arguments @("npedi.py", "build-curves")
    Run-Npedi -Arguments @("npedi.py", "cluster")
    if (-not $SkipQualityReport) {
        Run-Npedi -Arguments @("npedi.py", "quality-report")
    }
    Run-Npedi -Arguments @(
        "npedi.py", "render", "--granularity", "week",
        "--output", (Join-Path $repo "export\timeseries_curves_full_week.html")
    )
}

Write-Host "`nFull backfill continuation completed." -ForegroundColor Green
