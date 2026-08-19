<#
.SYNOPSIS
    Resume CODECO history candidates missing from vesselList (1-6 workers).
.DESCRIPTION
    Seeds the complete candidate queue first, then runs one Python process with
    bounded worker threads, chunks, pauses, and finite transient-failure retries.
    On token expiry, restarts the same chunk so only the next parent preflight
    performs automatic SMS login. Worker threads never perform login.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_gate_history_backfill.ps1
.EXAMPLE
    # Run only two chunks for observation.
    powershell -ExecutionPolicy Bypass -File .\run_gate_history_backfill.ps1 -MaxChunks 2
#>
param(
    [string]$EtaStart = "2023-01-01",
    [string]$EtaEnd = "2026-06-30",
    [ValidateRange(1, 1000)]
    [int]$ChunkCandidates = 1000,
    [ValidateRange(3, 10000)]
    [int]$ChunkRequests = 3000,
    [ValidateRange(500, 60000)]
    [int]$DelayMs = 1500,
    [ValidateRange(1, 6)]
    [int]$Workers = 6,
    [ValidateSet("known", "unknown", "all")]
    [string]$CatalogScope = "all",
    [ValidateRange(0, 86400)]
    [int]$PauseSeconds = 0,
    [ValidateRange(0, 1000000)]
    [int]$MaxChunks = 0,
    [ValidateRange(0, 100)]
    [int]$MaxRetries = 5,
    [ValidateRange(1, 86400)]
    [int]$RetryBaseSeconds = 30,
    [ValidateRange(1, 86400)]
    [int]$RetryMaxSeconds = 300,
    [ValidateRange(0, 5)]
    [int]$MaxAuthRestarts = 2,
    [ValidateRange(1, 300)]
    [int]$AuthRestartSeconds = 5,
    [string]$AlertEmail = $env:NPEDI_ALERT_EMAIL,
    [ValidateRange(60, 86400)]
    [int]$WatchdogStaleSeconds = 3600,
    [ValidateRange(5, 3600)]
    [int]$WatchdogPollSeconds = 30,
    [switch]$DisableEmailWatchdog,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
if (-not $Python) {
    $Python = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
        (Resolve-Path ".\.venv\Scripts\python.exe").Path
    } else {
        "python"
    }
}

$env:PYTHONIOENCODING = "utf-8"
$env:AUTO_LOGIN = "1"
$env:AUTH_PROBE = "1"
$env:NPEDI_HISTORY_START = $EtaStart
$env:NPEDI_HISTORY_END_EXCLUSIVE = (
    [datetime]::ParseExact($EtaEnd, "yyyy-MM-dd", $null).AddDays(1).ToString("yyyy-MM-dd")
)
$env:NPEDI_HISTORY_CATALOG_SCOPE = $CatalogScope

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wrapperLog = Join-Path $logDir "gate_history_backfill_wrapper.log"

function Say {
    param([string]$Message, [string]$Color = "Gray")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $line -ForegroundColor $Color
    # $ErrorActionPreference is Stop; a lost race for the log file must not be
    # allowed to abort a backfill that runs for tens of hours.  Retry briefly,
    # then drop the line rather than the run.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Add-Content -LiteralPath $wrapperLog -Value $line -Encoding UTF8 -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Get-HistoryRemaining {
    $code = @'
import os
from config import load_config
from store import Store

with Store(load_config().db_path) as store:
    scope = os.environ.get("NPEDI_HISTORY_CATALOG_SCOPE", "known")
    catalog = {"known": True, "unknown": False, "all": None}[scope]
    print(store.gate_history_candidate_count_in_scope(
        os.environ["NPEDI_HISTORY_START"],
        os.environ["NPEDI_HISTORY_END_EXCLUSIVE"],
        statuses=("pending", "hit"),
        vessel_in_catalog=catalog,
    ))
'@
    $value = $code | & $Python -
    if ($LASTEXITCODE -ne 0) { throw "Failed to read CODECO history progress" }
    return [long](($value | Select-Object -Last 1).ToString().Trim())
}

function Add-CatalogScopeArgs {
    param([object[]]$Arguments)
    if ($CatalogScope -eq "unknown") { $Arguments += "--unknown-vessels-only" }
    if ($CatalogScope -eq "all") { $Arguments += "--include-new-vessels" }
    return $Arguments
}

function Invoke-WithRetry {
    param(
        [object[]]$Arguments,
        [string]$Label
    )

    $retry = 0
    $authRestart = 0
    while ($true) {
        & $Python @Arguments | Out-Host
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            if ($retry -gt 0) {
                Say "$Label succeeded after $retry retry attempt(s); retry counter reset" "Green"
            }
            $script:LastInvokeExitCode = 0
            return
        }
        if ($code -eq 2) {
            if ($authRestart -ge $MaxAuthRestarts) {
                Say "$Label still exits with code 2 after $MaxAuthRestarts parent auth restart(s); stopping with checkpoint preserved" "Red"
                $script:LastInvokeExitCode = 2
                return
            }
            $authRestart++
            Say "$Label saw an expired token; restarting the same command in $AuthRestartSeconds seconds so the next parent preflight can renew it ($authRestart/$MaxAuthRestarts)" "Yellow"
            Start-Sleep -Seconds $AuthRestartSeconds
            continue
        }
        if ($code -ne 1) {
            Say "$Label exited with code $code; stopping with checkpoint preserved" "Yellow"
            $script:LastInvokeExitCode = $code
            return
        }
        if ($retry -ge $MaxRetries) {
            Say "$Label still exits with code 1 after $MaxRetries retries; stopping with checkpoint preserved" "Yellow"
            $script:LastInvokeExitCode = 1
            return
        }

        $retry++
        $delay = [int][Math]::Min(
            [double]$RetryMaxSeconds,
            [double]$RetryBaseSeconds * [Math]::Pow(2, $retry - 1)
        )
        Say "$Label exited with code 1; retry $retry/$MaxRetries in $delay seconds" "Yellow"
        Start-Sleep -Seconds $delay
    }
}

$seedArgs = Add-CatalogScopeArgs @(
    (Join-Path $root "sync.py"), "gate-history-backfill",
    "--eta-start", $EtaStart, "--eta-end", $EtaEnd,
    "--seed-only"
)
Say "Seeding complete history candidate queue before measuring progress: $EtaStart to $EtaEnd; scope $CatalogScope" "Cyan"
Invoke-WithRetry $seedArgs "Candidate seeding"
$exitCode = $script:LastInvokeExitCode
if ($exitCode -ne 0) {
    Say "History backfill did not start because candidate seeding failed; exit code $exitCode" "Yellow"
    exit $exitCode
}

$remaining = Get-HistoryRemaining
Say "Candidate queue ready: $remaining pairs remaining" "Green"

# The watchdog is a separate process, so it can still record and deliver an
# alert after this wrapper exits.  A PID-specific state file makes delivery
# idempotent for one incident while allowing a later deliberate restart to be
# monitored independently.  MaxChunks is an intentional bounded run, so do
# not arm the disconnect alarm in that mode.
if (-not $DisableEmailWatchdog -and $MaxChunks -eq 0 -and $remaining -gt 0) {
    $watchdogScript = Join-Path $root "scripts\history_backfill_watchdog.py"
    $watchdogState = Join-Path $logDir "history_backfill_watchdog_$PID.json"
    $watchdogOut = Join-Path $logDir "history_backfill_watchdog_$PID.out.log"
    $watchdogErr = Join-Path $logDir "history_backfill_watchdog_$PID.err.log"
    $watchdogArgs = @(
        $watchdogScript,
        "--pid", $PID,
        "--eta-start", $EtaStart,
        "--eta-end", $EtaEnd,
        "--catalog-scope", $CatalogScope,
        "--log-file", $wrapperLog,
        "--state-file", $watchdogState,
        "--stale-seconds", $WatchdogStaleSeconds,
        "--poll-seconds", $WatchdogPollSeconds
    )
    # Only override the recipient when the caller actually set one.  Passing
    # "--to ''" unconditionally would override the Python script's own
    # NPEDI_ALERT_EMAIL / .env fallback with an empty string and make
    # validate_spec() reject it even when .env has a valid address configured.
    if ($AlertEmail) {
        $watchdogArgs += @("--to", $AlertEmail)
    }
    $watchdog = Start-Process -FilePath $Python -ArgumentList $watchdogArgs `
        -WindowStyle Hidden -RedirectStandardOutput $watchdogOut `
        -RedirectStandardError $watchdogErr -PassThru
    $recipientLabel = if ($AlertEmail) { $AlertEmail } else { "(from NPEDI_ALERT_EMAIL / .env)" }
    Say "Email watchdog armed: PID $($watchdog.Id); recipient $recipientLabel; process exit checks every $WatchdogPollSeconds seconds; stalled-log threshold $WatchdogStaleSeconds seconds; no test email sent" "Green"
}

Say "Starting history backfill: $EtaStart to $EtaEnd; scope $CatalogScope; $remaining pairs remaining; $Workers workers; up to $ChunkCandidates candidates and $ChunkRequests requests/chunk; per-worker delay $DelayMs-$([int]($DelayMs * 1.25))ms; pause $PauseSeconds seconds; code-1 retries $MaxRetries ($RetryBaseSeconds-$RetryMaxSeconds seconds exponential backoff); parent-only auto-login with up to $MaxAuthRestarts auth restarts" "Cyan"

$chunk = 0
$exitCode = 0
while ($remaining -gt 0) {
    if ($MaxChunks -gt 0 -and $chunk -ge $MaxChunks) { break }
    $chunk++
    $before = $remaining
    Say "Chunk $chunk starting: $before pairs remaining" "Cyan"
    $historyArgs = @(
        (Join-Path $root "sync.py"), "gate-history-backfill",
        "--eta-start", $EtaStart, "--eta-end", $EtaEnd,
        "--limit", $ChunkCandidates, "--max-requests", $ChunkRequests,
        "--delay-ms", $DelayMs, "--workers", $Workers
    )
    $historyArgs = Add-CatalogScopeArgs $historyArgs
    Invoke-WithRetry $historyArgs "Chunk $chunk"
    $exitCode = $script:LastInvokeExitCode

    if ($exitCode -ne 0) {
        break
    }

    $remaining = Get-HistoryRemaining
    Say "Chunk $chunk completed: $before -> $remaining pairs" "Green"
    if ($remaining -ge $before) {
        Say "No state progress in this chunk; an oversized voyage may exceed the budget. Stopping to avoid an idle loop" "Yellow"
        $exitCode = 1
        break
    }
    if ($remaining -eq 0) { break }
    Say "Pausing for $PauseSeconds seconds" "Gray"
    Start-Sleep -Seconds $PauseSeconds
}

Say "History backfill ended: $remaining pairs remaining; exit code $exitCode" $(if ($exitCode -eq 0) { "Green" } else { "Yellow" })
exit $exitCode
