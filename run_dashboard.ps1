$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\streamlit.exe")) {
    Write-Host "Streamlit is not installed in .venv. Install requirements-dashboard.txt first." -ForegroundColor Yellow
    exit 1
}

Write-Host "NPEDI operations dashboard: http://localhost:8081" -ForegroundColor Cyan
& ".\.venv\Scripts\streamlit.exe" run ".\dashboard.py" --server.port 8081 --server.address 0.0.0.0
