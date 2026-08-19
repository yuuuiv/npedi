<#
.SYNOPSIS
    Wait for the normal six-worker history crawl, then capture quarantined
    operational buckets into gate_anomaly_sidecar.sqlite.

.DESCRIPTION
    While the normal wrapper is alive this script performs only a cheap local
    scan for newly discovered >=10,000-row hits and places them in the existing
    quarantine table.  It never contacts NPEDI during that phase.

    After the normal queue reaches zero, it captures the sidecar sequentially
    in 500-request slices.  Each slice shares .sync.gate.lock with the normal
    gate commands, uses its own SQLite database, and resumes by page number.
#>

[CmdletBinding()]
param(
    [int]$WaitForPid = 0,
    [string]$EtaStart = "2023-01-01",
    [string]$EtaEnd = "2026-06-30",
    [string]$SidecarDb = "gate_anomaly_sidecar.sqlite",
    [ValidateRange(1, 100000)]
    [int]$TotalThreshold = 10000,
    [ValidateRange(1, 100000)]
    [int]$PageCap = 20000,
    [ValidateRange(1, 100000)]
    [int]$ChunkRequests = 500,
    [ValidateRange(0, 60000)]
    [int]$DelayMs = 1100,
    [ValidateRange(10, 3600)]
    [int]$PollSeconds = 300,
    [ValidateRange(0, 10)]
    [int]$MaxAuthRetries = 2,
    [ValidateRange(0, 20)]
    [int]$MaxTransientRetries = 5,
    [switch]$DisableAutoLogin,
    # Give the sidecar its own pipeline lock so it runs alongside the normal
    # history crawl instead of queueing behind it.  The two write different
    # databases; the shared lock only bounds concurrent upstream request
    # pressure, so the combined rate becomes your budget to justify.
    [string]$LockFile = "",
    # The remaining==0 precondition exists so the sidecar never competes with
    # the normal queue.  Pair this with -LockFile when that competition is the
    # intent; on its own it would just reintroduce lock queueing.
    [switch]$AllowConcurrentHistory
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$program = Join-Path $root "gate_anomaly_sidecar.py"
$sidecar = if ([IO.Path]::IsPathRooted($SidecarDb)) {
    [IO.Path]::GetFullPath($SidecarDb)
} else {
    [IO.Path]::GetFullPath((Join-Path $root $SidecarDb))
}
$logDir = Join-Path $root "logs"
$wrapperLog = Join-Path $logDir "gate_anomaly_sidecar_wrapper.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable not found: $python"
}
if (-not (Test-Path -LiteralPath $program)) {
    throw "Sidecar program not found: $program"
}

# $ErrorActionPreference is Stop, so an Add-Content that loses a race for the
# log file would otherwise abort a multi-hour capture.  Losing a log line is
# never worth killing the run: retry briefly, then give up on the line only.
function Write-LogLine([string]$Text) {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Add-Content -LiteralPath $wrapperLog -Encoding UTF8 -Value $Text -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Write-WrapperLog([string]$Message) {
    Write-LogLine ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Invoke-Sidecar([string[]]$Arguments) {
    $allArgs = @($program, "--sidecar-db", $sidecar) + $Arguments
    $output = @(& $python @allArgs 2>&1)
    $code = $LASTEXITCODE
    foreach ($line in $output) {
        Write-LogLine ([string]$line)
    }
    return [pscustomobject]@{
        ExitCode = $code
        Output = (($output | ForEach-Object { [string]$_ }) -join "`n")
    }
}

function Get-LastJson([string]$Output) {
    $lines = @($Output -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith("{") })
    if ($lines.Count -eq 0) {
        throw "Command did not return JSON"
    }
    return ($lines[-1] | ConvertFrom-Json)
}

function Invoke-Discovery([bool]$OutliersOnly) {
    $args = @(
        "discover", "--eta-start", $EtaStart, "--eta-end", $EtaEnd,
        "--total-threshold", [string]$TotalThreshold,
        "--page-size", "100", "--page-cap", [string]$PageCap
    )
    if ($OutliersOnly) { $args += "--outliers-only" }
    $discover = Invoke-Sidecar $args
    if ($discover.ExitCode -ne 0) {
        Write-WrapperLog "Discovery failed with exit $($discover.ExitCode); will retry."
        return $false
    }
    $quarantine = Invoke-Sidecar @("quarantine-main")
    if ($quarantine.ExitCode -ne 0) {
        Write-WrapperLog "Main quarantine update failed with exit $($quarantine.ExitCode); will retry."
        return $false
    }
    return $true
}

Write-WrapperLog "Sidecar supervisor started; wait_pid=$WaitForPid db=$sidecar."
Invoke-Discovery $false | Out-Null

while ($WaitForPid -gt 0 -and (Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue)) {
    # No network access in this loop.  This catches a newly probed runaway hit
    # before it can pin the normal queue at the end of the history crawl.
    Invoke-Discovery $true | Out-Null
    Start-Sleep -Seconds $PollSeconds
}

# One final full semantic scan catches source-plan rows that were added while
# the normal crawl was running.
if (-not (Invoke-Discovery $false)) {
    Write-WrapperLog "Final discovery failed; refusing to start remote capture."
    exit 1
}

$mainResult = Invoke-Sidecar @(
    "main-status", "--eta-start", $EtaStart, "--eta-end", $EtaEnd
)
if ($mainResult.ExitCode -ne 0) {
    Write-WrapperLog "Could not verify normal history completion; refusing to start capture."
    exit 1
}
$mainStatus = Get-LastJson $mainResult.Output
if ([int64]$mainStatus.remaining -ne 0 -or [int]$mainStatus.running_gate_history_runs -ne 0) {
    if (-not $AllowConcurrentHistory) {
        Write-WrapperLog (
            "Normal history is not complete: remaining={0} running={1}; sidecar capture not started." -f
            $mainStatus.remaining, $mainStatus.running_gate_history_runs
        )
        exit 1
    }
    if (-not $LockFile) {
        Write-WrapperLog (
            "-AllowConcurrentHistory without -LockFile would queue on the shared gate lock; refusing."
        )
        exit 1
    }
    Write-WrapperLog (
        "Normal history still active (remaining={0} running={1}); proceeding concurrently on lock {2}." -f
        $mainStatus.remaining, $mainStatus.running_gate_history_runs, $LockFile
    )
} else {
    Write-WrapperLog "Normal history is complete; starting sequential sidecar capture."
}
$authFailures = 0
$transientFailures = 0
$noProgressRounds = 0
$lastPages = -1
# Seed from the current state instead of $false.  A resume that starts with an
# errored job would otherwise burn a whole round capturing nothing, because
# --retry-errors only gets added after the first status read reports it.
$retryErrors = $false
$seedStatus = Invoke-Sidecar @("status", "--json")
if ($seedStatus.ExitCode -eq 0) {
    $retryErrors = ([int](Get-LastJson $seedStatus.Output).jobs.error -gt 0)
    if ($retryErrors) { Write-WrapperLog "Resuming with --retry-errors: error jobs present." }
}

while ($true) {
    $captureArgs = @(
        "capture", "--max-requests", [string]$ChunkRequests,
        "--delay-ms", [string]$DelayMs, "--log-every", "25"
    )
    if (-not $DisableAutoLogin) { $captureArgs += "--auto-login" }
    if ($retryErrors) { $captureArgs += "--retry-errors" }
    if ($LockFile) { $captureArgs += @("--lock-file", $LockFile) }
    $capture = Invoke-Sidecar $captureArgs

    if ($capture.ExitCode -eq 2) {
        $authFailures++
        Write-WrapperLog "Authentication failed ($authFailures/$MaxAuthRetries)."
        if ($authFailures -gt $MaxAuthRetries) { exit 2 }
        Start-Sleep -Seconds ([Math]::Min(300, 30 * [Math]::Pow(2, $authFailures - 1)))
        continue
    }
    if ($capture.ExitCode -ne 0) {
        $transientFailures++
        Write-WrapperLog "Capture failed with exit $($capture.ExitCode) ($transientFailures/$MaxTransientRetries)."
        if ($transientFailures -gt $MaxTransientRetries) { exit 1 }
        Start-Sleep -Seconds ([Math]::Min(300, 30 * [Math]::Pow(2, $transientFailures - 1)))
        continue
    }
    $authFailures = 0
    $transientFailures = 0

    $statusResult = Invoke-Sidecar @("status", "--json")
    if ($statusResult.ExitCode -ne 0) { exit 1 }
    $status = Get-LastJson $statusResult.Output
    if ([int64]$status.unfinished -eq 0) {
        Write-WrapperLog "Sidecar capture complete: pages=$($status.pages_saved) rows=$($status.rows_saved)."
        exit 0
    }

    $pages = [int64]$status.pages_saved
    if ($pages -le $lastPages) { $noProgressRounds++ } else { $noProgressRounds = 0 }
    $lastPages = $pages
    $retryErrors = ([int]$status.jobs.error -gt 0)
    Write-WrapperLog (
        "Sidecar partial: unfinished={0} pages={1} rows={2} errors={3}." -f
        $status.unfinished, $status.pages_saved, $status.rows_saved, $status.jobs.error
    )
    if ($noProgressRounds -ge 3) {
        Write-WrapperLog "No page progress for three rounds; stopping for review."
        exit 1
    }
}
