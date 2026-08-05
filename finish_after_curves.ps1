$ErrorActionPreference = "Stop"

# Resume only the stages after a successful build-curves. This intentionally
# does not crawl, aggregate, or rebuild curves.
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

if (Test-PythonModule -ModuleName "sklearn") {
    Run-Npedi -Arguments @("npedi.py", "cluster")
} else {
    Write-Warning "scikit-learn is not installed; cluster is skipped for now."
    Write-Warning "After installing it, run: $py npedi.py cluster"
}

Run-Npedi -Arguments @("npedi.py", "quality-report")
Run-Npedi -Arguments @("npedi.py", "render")

Write-Host "`nFinished. HTML: export\timeseries_curves.html" -ForegroundColor Green
