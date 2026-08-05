$ErrorActionPreference = "Stop"

# Run this file from the NPEDI repository. It resumes the interrupted
# vessel-plan checkpoint, then continues the dependent enrichment pipeline.
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repo

$py = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else {
    "python"
}

# CLI names use hyphens; crawl_run stores endpoint names with underscores.
$DatabaseJobNames = @{
    "vessel-plan" = "vessel_plan"
    "container-notice" = "container_notice"
    "cargo-release" = "cargo_release"
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

function Get-LatestCrawlStatus {
    param([Parameter(Mandatory)][string]$JobName)

    $dbJobName = if ($DatabaseJobNames.ContainsKey($JobName)) { $DatabaseJobNames[$JobName] } else { $JobName }
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
row = c.execute(
    'select status from crawl_run where job_name=? order by started_at desc limit 1',
    ('$dbJobName',),
).fetchone()
print(row[0] if row else 'missing')
c.close()
"@
    $status = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read crawl status for $JobName"
    }
    return ($status | Select-Object -Last 1).ToString().Trim()
}

function Get-LatestCrawlError {
    param([Parameter(Mandatory)][string]$JobName)

    $dbJobName = if ($DatabaseJobNames.ContainsKey($JobName)) { $DatabaseJobNames[$JobName] } else { $JobName }
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
row = c.execute(
    """select stage || ': ' || message || ' request=' || request_json
       from ingest_error
       where crawl_run_id=(select id from crawl_run where job_name=? order by started_at desc limit 1)
       order by id desc limit 1""",
    ('$dbJobName',),
).fetchone()
print(row[0] if row else 'no persisted error')
c.close()
"@
    $detail = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        return "unable to read persisted error"
    }
    return ($detail | Select-Object -Last 1).ToString().Trim()
}

function Run-Crawl {
    param(
        [Parameter(Mandatory)][string]$JobName,
        [string[]]$ExtraArguments = @()
    )

    $arguments = @("npedi.py", "crawl", $JobName) + $ExtraArguments
    Run-Npedi -Arguments $arguments

    # The CLI can return exit code 0 for a partial crawl, so also inspect the
    # persisted crawl_run status before moving to the next dependent stage.
    $status = Get-LatestCrawlStatus -JobName $JobName
    if ($status -ne "success") {
        $detail = Get-LatestCrawlError -JobName $JobName
        throw "$JobName crawl status is $status. Last error: $detail Retry the same batch after fixing the error."
    }
}

function Get-QueueCount {
    param([ValidateSet("all", "pending")][string]$Status = "all")

    $where = if ($Status -eq "pending") { " where status='pending'" } else { "" }
    $code = @"
import sqlite3
c = sqlite3.connect('npedi.sqlite')
print(c.execute("select count(*) from container_enrichment_queue$where").fetchone()[0])
c.close()
"@
    $count = $code | & $py -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read container_enrichment_queue"
    }
    return [int](($count | Select-Object -Last 1).ToString().Trim())
}

# The previous vessel-plan process was interrupted after saving its checkpoint.
# --resume starts from that checkpoint instead of page 1.
Run-Crawl -JobName "vessel-plan" -ExtraArguments @("--resume")

# This is idempotent and resumes from the existing container-notice checkpoint.
Run-Crawl -JobName "container-notice" -ExtraArguments @("--resume")
Run-Crawl -JobName "transshipment" -ExtraArguments @("--resume")

# VGM uses the complete queue. Keep the offset fixed for each retry; rerunning
# a failed batch is safe because fact upserts are idempotent.
$queue = Get-QueueCount
for ($offset = 0; $offset -lt $queue; $offset += 500) {
    Run-Crawl -JobName "vgm" -ExtraArguments @(
        "--limit", "500", "--offset", "$offset", "--resume"
    )
}

Run-Crawl -JobName "cargo-release" -ExtraArguments @("--resume")

# container-history intentionally selects pending queue rows using the stable
# first_seen_at, container_no ordering. The offset is a batch offset, not a
# request-page offset.
$historyQueue = Get-QueueCount -Status "pending"
for ($offset = 0; $offset -lt $historyQueue; $offset += 500) {
    Run-Crawl -JobName "container-history" -ExtraArguments @(
        "--limit", "500", "--offset", "$offset", "--resume"
    )
}

# Build current analysis artifacts after all fact tables are populated.
Run-Npedi -Arguments @("npedi.py", "aggregate")
Run-Npedi -Arguments @("npedi.py", "build-curves")

# Clustering needs scikit-learn. Keep the data pipeline usable when the
# optional dependency is absent, and print the exact follow-up command.
$sklearnCheck = & $py -c "import sklearn; print(sklearn.__version__)" 2>$null
if ($LASTEXITCODE -eq 0) {
    Run-Npedi -Arguments @("npedi.py", "cluster")
} else {
    Write-Warning "scikit-learn is not installed; cluster was skipped. Run: & '$py' -m pip install scikit-learn"
}

Run-Npedi -Arguments @("npedi.py", "quality-report")
Run-Npedi -Arguments @("npedi.py", "render")

Write-Host "`nRecovery pipeline completed." -ForegroundColor Green
