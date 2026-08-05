param(
    [ValidateSet("vgm", "history", "all")]
    [string]$Target = "all",
    [ValidateRange(1, 5000)]
    [int]$BatchSize = 500,
    [ValidateRange(0, 2147483647)]
    [int]$MaxBatches = 0,
    [switch]$SkipCatalogSeed
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

function Get-EnrichmentCount {
    param(
        [ValidateSet("vgm", "history")][string]$Kind,
        [ValidateSet("remaining", "complete")][string]$State
    )
    $env:NPEDI_ENRICHMENT_COLUMN = "${Kind}_status"
    $env:NPEDI_ENRICHMENT_STATE = $State
    $code = @'
import os
import sqlite3

column = os.environ['NPEDI_ENRICHMENT_COLUMN']
state = os.environ['NPEDI_ENRICHMENT_STATE']
if column not in {'vgm_status', 'history_status'}:
    raise SystemExit('invalid status column')
where = f"{column} IN ('pending','error')" if state == 'remaining' else f"{column}='complete'"
with sqlite3.connect('npedi.sqlite') as conn:
    print(conn.execute(f'SELECT COUNT(*) FROM container_enrichment_state WHERE {where} AND iso6346_valid=1').fetchone()[0])
'@
    try {
        $value = $code | & $py -
        if ($LASTEXITCODE -ne 0) { throw "Failed to read $Kind queue state" }
        return [long](($value | Select-Object -Last 1).ToString().Trim())
    } finally {
        Remove-Item Env:NPEDI_ENRICHMENT_COLUMN -ErrorAction SilentlyContinue
        Remove-Item Env:NPEDI_ENRICHMENT_STATE -ErrorAction SilentlyContinue
    }
}

function Get-LatestCrawlStatus {
    param([ValidateSet("vgm", "history")][string]$Kind)
    $env:NPEDI_CRAWL_JOB = if ($Kind -eq "history") { "container_history" } else { "vgm" }
    $code = @'
import os
import sqlite3

with sqlite3.connect('npedi.sqlite') as conn:
    row = conn.execute(
        'SELECT status FROM crawl_run WHERE job_name=? ORDER BY started_at DESC LIMIT 1',
        (os.environ['NPEDI_CRAWL_JOB'],),
    ).fetchone()
print(row[0] if row else 'missing')
'@
    try {
        $value = $code | & $py -
        if ($LASTEXITCODE -ne 0) { throw "Failed to read latest $Kind crawl status" }
        return ($value | Select-Object -Last 1).ToString().Trim()
    } finally {
        Remove-Item Env:NPEDI_CRAWL_JOB -ErrorAction SilentlyContinue
    }
}

function Run-Enrichment {
    param([ValidateSet("vgm", "history")][string]$Kind)
    $cliTarget = if ($Kind -eq "history") { "container-history" } else { "vgm" }
    $batch = 0
    while (($before = Get-EnrichmentCount -Kind $Kind -State remaining) -gt 0) {
        if ($MaxBatches -gt 0 -and $batch -ge $MaxBatches) { break }
        Write-Host "$Kind remaining before batch: $before" -ForegroundColor Yellow
        # The pending set shrinks, so every batch must consume offset zero.
        Run-Npedi -Arguments @(
            "npedi.py", "crawl", $cliTarget,
            "--limit", "$BatchSize", "--offset", "0", "--resume"
        )
        $status = Get-LatestCrawlStatus -Kind $Kind
        $after = Get-EnrichmentCount -Kind $Kind -State remaining
        if ($status -ne "success") {
            throw "$cliTarget ended with status=$status ($before -> $after remaining). Inspect ingest_error, then rerun this script."
        }
        if ($after -ge $before) {
            throw "$cliTarget made no queue progress ($before -> $after). Stop and inspect checkpoints/status."
        }
        $batch += 1
        $complete = Get-EnrichmentCount -Kind $Kind -State complete
        Write-Host "$Kind progress: complete=$complete, remaining=$after" -ForegroundColor Green
    }
}

if (-not $SkipCatalogSeed) {
    Run-Npedi -Arguments @("npedi.py", "seed-container-catalog")
}
Write-Warning "The catalog contains about 2.49 million valid containers. At the default polite throttle, each remote enrichment can take several weeks. The script is safe to interrupt and rerun."

if ($Target -in @("vgm", "all")) { Run-Enrichment -Kind "vgm" }
if ($Target -in @("history", "all")) { Run-Enrichment -Kind "history" }

Run-Npedi -Arguments @("npedi.py", "coverage-status")
Write-Host "`nRequested enrichment run finished." -ForegroundColor Green
