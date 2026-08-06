$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".captcha-cnn-venv\Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements-captcha-cnn.txt"

if (-not (Test-Path $VenvPython)) {
    & py -3.11 -m venv (Join-Path $ProjectRoot ".captcha-cnn-venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 is required. Install it, then run this script again."
    }
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install CAPTCHA CNN dependencies." }

Write-Host "CAPTCHA CNN environment is ready: $VenvPython"
