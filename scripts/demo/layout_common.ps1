# Shared geometry + window placement for the auto-login recording.
# ASCII only: Windows PowerShell 5.1 reads .ps1 as ANSI, so non-ASCII here
# corrupts string literals and breaks parsing.

$REPO_ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Find-Exe {
  param([string]$Name, [string[]]$Candidates)
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($c in $Candidates) { if (Test-Path $c) { return $c } }
  $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter $Name `
             -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($found) { return $found.FullName }
  throw "cannot locate $Name"
}

$FFMPEG = Find-Exe "ffmpeg.exe" @()
$CHROME = Find-Exe "chrome.exe" @(
  "C:\Program Files\Google\Chrome\Application\chrome.exe",
  "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
# the demo venv carries playwright; see scripts/demo/README.md
$PY = Join-Path $REPO_ROOT ".demo-venv\Scripts\python.exe"

# Window placement uses LOGICAL coordinates: this process is DPI-unaware, so
# Windows scales what SetWindowPos is given. ffmpeg's gdigrab is DPI-aware and
# grabs PHYSICAL pixels, so the capture rect has to be scaled or it clips the
# right-hand window. Display here is 2560x1440 physical at 125%.
$SCALE = 1.25
$RX, $RY, $RW, $RH = 0, 96, 2048, 960
$TERM_W   = 1000
$GAP      = 8
$CHROME_X = $RX + $TERM_W + $GAP
$CHROME_W = $RW - $TERM_W - $GAP

# physical rect handed to ffmpeg
$PX = [int]($RX * $SCALE); $PY = [int]($RY * $SCALE)
$PW = [int]($RW * $SCALE); $PH = [int]($RH * $SCALE)

if (-not ("Win32Pos" -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Win32Pos {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(
      IntPtr hWnd, IntPtr after, int X, int Y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  static readonly IntPtr TOPMOST = new IntPtr(-1);
  public static string Rect(IntPtr h) {
    RECT r; GetWindowRect(h, out r);
    return string.Format("{0},{1} {2}x{3}", r.L, r.T, r.R - r.L, r.B - r.T);
  }
  public static void Place(IntPtr h, int x, int y, int w, int hgt) {
    ShowWindow(h, 9);                                 // SW_RESTORE, undo maximise
    // topmost rather than NOZORDER: gdigrab captures painted pixels, so
    // anything left in front of the region lands in the recording
    SetWindowPos(h, TOPMOST, x, y, w, hgt, 0x0040);   // SWP_SHOWWINDOW
    SetForegroundWindow(h);
  }
  // Topmost without touching size or position. Resizing a Playwright-created
  // window desynchronises Chrome's render area from the visible frame, which
  // pushes the right half of the page off screen.
  public static void Pin(IntPtr h) {
    SetWindowPos(h, TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040);
  }
  public static void Unpin(IntPtr h) {
    SetWindowPos(h, new IntPtr(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040);
  }
}
'@
}

function Wait-ForWindow {
  param([string]$Name, [datetime]$After, [int]$TimeoutSec = 25)
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $p = Get-Process -Name $Name -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowHandle -ne 0 -and $_.StartTime -gt $After } |
         Sort-Object StartTime -Descending | Select-Object -First 1
    if ($p) { return $p }
    Start-Sleep -Milliseconds 400
  }
  return $null
}

function Start-DemoWindows {
  param([string]$ScriptPath, [switch]$Preview)

  $t0 = (Get-Date).AddSeconds(-2)
  $rec = Join-Path (Split-Path -Parent $ScriptPath) "record"
  New-Item -ItemType Directory -Force $rec | Out-Null

  # Clear the desktop first, otherwise whatever is in front gets recorded.
  (New-Object -ComObject Shell.Application).MinimizeAll()
  Start-Sleep -Milliseconds 800

  Start-Process $CHROME -ArgumentList @(
    "--new-window",
    "--user-data-dir=$(Join-Path $rec 'chrome-profile')",
    "--no-first-run", "--no-default-browser-check",
    "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
    "--disable-features=Translate",
    # the window is narrower than the site's desktop layout; render at 0.8 so
    # the page fits instead of being clipped mid-headline
    "--force-device-scale-factor=0.8",
    "--window-position=$CHROME_X,$RY", "--window-size=$CHROME_W,$RH",
    "https://www.npedi.com/index"
  ) | Out-Null

  $chromeProc = Wait-ForWindow -Name "chrome" -After $t0
  if (-not $chromeProc) { throw "Chrome window never appeared; aborting" }
  [Win32Pos]::Place($chromeProc.MainWindowHandle, $CHROME_X, $RY, $CHROME_W, $RH)

  # No ';' in the command: Windows Terminal reads it as a pane separator.
  # No spaces in --title: Start-Process joins ArgumentList unquoted, so a
  # two-word title spills into the command line and the launch fails.
  # No --size: wt re-applies cols/rows after placement and overrides the rect.
  $inner = if ($Preview) { "& '$PY' '$ScriptPath' --preview" } else { "& '$PY' '$ScriptPath'" }
  Start-Process wt.exe -ArgumentList @(
    "--pos", "$RX,$RY", "--title", "NPEDI",
    "powershell", "-NoProfile", "-NoLogo", "-Command", $inner
  ) | Out-Null

  $termProc = Wait-ForWindow -Name "WindowsTerminal" -After $t0
  if (-not $termProc) { throw "Terminal window never appeared; aborting" }

  Start-Sleep -Seconds 3
  foreach ($pass in 1..2) {
    [Win32Pos]::Place($termProc.MainWindowHandle, $RX, $RY, $TERM_W, $RH)
    [Win32Pos]::Place($chromeProc.MainWindowHandle, $CHROME_X, $RY, $CHROME_W, $RH)
    Start-Sleep -Milliseconds 700
  }
  Write-Host ("  terminal want {0},{1} {2}x{3}  got {4}" -f $RX, $RY, $TERM_W, $RH, [Win32Pos]::Rect($termProc.MainWindowHandle))
  Write-Host ("  chrome   want {0},{1} {2}x{3}  got {4}" -f $CHROME_X, $RY, $CHROME_W, $RH, [Win32Pos]::Rect($chromeProc.MainWindowHandle))
  return @{ Chrome = $chromeProc; Term = $termProc }
}

function Stop-DemoWindows {
  param($Handles, [switch]$RestoreDesktop)
  foreach ($p in @($Handles.Term, $Handles.Chrome)) {
    if ($p -and -not $p.HasExited) {
      try { [Win32Pos]::Unpin($p.MainWindowHandle) } catch {}
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
  }
  if ($RestoreDesktop) {
    Start-Sleep -Milliseconds 400
    (New-Object -ComObject Shell.Application).UndoMinimizeALL()
  }
}
