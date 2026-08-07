# Record the browser auto-login demo: narration left, real login page right.
# -Dry stops before requesting an SMS and grabs a still instead of video, so the
# framing can be checked without spending a message.
# ASCII only (Windows PowerShell 5.1 reads .ps1 as ANSI).
param([switch]$Dry)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "layout_common.ps1")

$rec    = Join-Path $here "record"
$demoPy = Join-Path $here "demo_browser.py"
$venvPy = $PY          # set by layout_common.ps1
$done   = Join-Path $rec "demo.done"
$mp4    = Join-Path $rec "browser.mp4"
if (-not (Test-Path $venvPy)) { throw "missing demo venv: $venvPy  (see scripts/demo/README.md)" }

New-Item -ItemType Directory -Force $rec | Out-Null
Remove-Item $done -ErrorAction SilentlyContinue
# Keep any previous take: a failed run used to delete a good recording outright.
if (Test-Path $mp4) {
  $stamp = (Get-Item $mp4).LastWriteTime.ToString("yyyyMMdd-HHmmss")
  Move-Item $mp4 (Join-Path $rec "browser-$stamp.mp4") -Force
  Write-Host "0/5  kept previous take as browser-$stamp.mp4"
}

Write-Host "1/5  clearing desktop"
(New-Object -ComObject Shell.Application).MinimizeAll()
Start-Sleep -Milliseconds 900
$t0 = (Get-Date).AddSeconds(-2)

Write-Host "2/5  launching narration terminal"
$inner = if ($Dry) { "& '$venvPy' '$demoPy' --dry" } else { "& '$venvPy' '$demoPy'" }
Start-Process wt.exe -ArgumentList @(
  "--pos", "$RX,$RY", "--title", "NPEDI",
  "powershell", "-NoProfile", "-NoLogo", "-Command", $inner
) | Out-Null

$term = Wait-ForWindow -Name "WindowsTerminal" -After $t0
if (-not $term) { throw "terminal window never appeared" }
Start-Sleep -Seconds 2
[Win32Pos]::Place($term.MainWindowHandle, $RX, $RY, $TERM_W, $RH)
Write-Host ("     terminal {0}" -f [Win32Pos]::Rect($term.MainWindowHandle))

$ff = $null
if (-not $Dry) {
  Write-Host "3/5  recording region ${RW}x${RH} @ ($RX,$RY)"
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName  = $FFMPEG
  $psi.Arguments = "-hide_banner -loglevel error -f gdigrab -framerate 12 " +
                   "-offset_x $PX -offset_y $PY -video_size ${PW}x${PH} -i desktop " +
                   "-t 500 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -y `"$mp4`""
  $psi.RedirectStandardInput = $true
  $psi.UseShellExecute = $false
  $ff = [System.Diagnostics.Process]::Start($psi)
} else {
  Write-Host "3/5  dry run - no recorder, no SMS"
}

Write-Host "4/5  waiting for the login flow (Playwright opens Chrome mid-run)"
$chrome = $null
$deadline = (Get-Date).AddSeconds(470)
while ((Get-Date) -lt $deadline) {
  if (-not $chrome) {
    $chrome = Wait-ForWindow -Name "chrome" -After $t0 -TimeoutSec 1
    if ($chrome) {
      # Playwright already positioned and sized it; only pin it on top. Resizing
      # here desynchronises Chrome's render area from the window frame.
      [Win32Pos]::Pin($chrome.MainWindowHandle)
      [Win32Pos]::Place($term.MainWindowHandle, $RX, $RY, $TERM_W, $RH)
      Write-Host ("     chrome   {0}" -f [Win32Pos]::Rect($chrome.MainWindowHandle))
    }
  }
  if (Test-Path $done) { break }
  Start-Sleep -Milliseconds 600
}
Start-Sleep -Seconds 2

Write-Host "5/5  finishing"
if ($Dry) {
  & $FFMPEG -hide_banner -loglevel error -f gdigrab `
    -offset_x $PX -offset_y $PY -video_size "${PW}x${PH}" -i desktop `
    -frames:v 1 -y "$rec\dry-frame.png"
} else {
  try { $ff.StandardInput.WriteLine("q"); $ff.StandardInput.Flush() } catch {}
  if (-not $ff.WaitForExit(20000)) { $ff.Kill() }
}

foreach ($p in @($term, $chrome)) {
  if ($p -and -not $p.HasExited) {
    try { [Win32Pos]::Unpin($p.MainWindowHandle) } catch {}
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
  }
}
Start-Sleep -Milliseconds 500
(New-Object -ComObject Shell.Application).UndoMinimizeALL()

$flag = if (Test-Path $done) { (Get-Content $done -Raw).Trim() } else { "timeout" }
$mb = if (Test-Path $mp4) { [math]::Round((Get-Item $mp4).Length / 1MB, 2) } else { 0 }
Write-Host "result=$flag  browser.mp4=${mb}MB"
