$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repo
$py = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else { "python" }

function Run-Npedi {
    param([Parameter(Mandatory)][string[]]$Arguments)
    Write-Host ("`n> {0} {1}" -f $py, ($Arguments -join " ")) -ForegroundColor Cyan
    & $py @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $($Arguments -join ' ')" }
}

Run-Npedi -Arguments @("npedi.py", "aggregate")
Run-Npedi -Arguments @("npedi.py", "build-curves", "--granularity", "week")
$output = Join-Path $repo "export\npedi_port_dashboard.html"
Run-Npedi -Arguments @("npedi.py", "render", "--granularity", "week", "--output", $output)
Write-Host "`nMeaningful dashboard ready: $output" -ForegroundColor Green
