$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$original = (Resolve-Path (Join-Path $PSScriptRoot "original\train_npedi_captcha_cnn.py")).Path
$train = Join-Path $root "scripts\train_npedi_captcha_cnn.py"
$crossValidation = Join-Path $root "scripts\cross_validate_npedi_captcha_cnn.py"

Copy-Item -LiteralPath $original -Destination $train -Force
if (Test-Path -LiteralPath $crossValidation) {
    $resolved = (Resolve-Path -LiteralPath $crossValidation).Path
    if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "rollback target escaped project root: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Force
}

Write-Output "Restored $train"
Write-Output "Removed $crossValidation"
