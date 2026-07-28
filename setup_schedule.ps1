<#
.SYNOPSIS
    注册 Windows 计划任务：每天早/中/晚各跑一轮增量，每周一次对账。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
    powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1 -Times 08:00,13:00,20:00
.NOTES
    需要以管理员身份运行；重复执行会覆盖同名任务。
#>
param(
    [string[]]$Times = @("07:30", "12:30", "19:30"),
    [string]$ReconcileTime = "22:30",
    [string]$ReconcileDay = "Sunday",
    [string]$TaskPrefix = "npedi",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Python) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Python) { throw "找不到 python，请用 -Python 指定完整路径" }
}
# 用 pythonw 可避免每次弹出黑窗口
$pythonw = Join-Path (Split-Path $Python) "pythonw.exe"
if (Test-Path $pythonw) { $Python = $pythonw }

Write-Host "项目目录 : $root"
Write-Host "Python   : $Python"

function Register-NpediTask {
    param([string]$Name, [string]$Command, $Triggers, [string]$Description)

    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument "`"$(Join-Path $root 'sync.py')`" $Command" -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers `
        -Settings $settings -Description $Description -Force | Out-Null
    Write-Host "  已注册：$Name"
}

# 每日三次增量。-StartWhenAvailable 保证关机/休眠错过的那一轮开机后补跑一次。
$dailyTriggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-NpediTask -Name "$TaskPrefix-incremental" -Command "incremental" `
    -Triggers $dailyTriggers -Description "npedi 航次数据增量同步（每日 $($Times -join ', ')）"

# 每周一次活跃航次对账，兜住"字段变了但 compareTime 没变"的行
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $ReconcileDay -At $ReconcileTime
Register-NpediTask -Name "$TaskPrefix-reconcile" -Command "reconcile" `
    -Triggers $weekly -Description "npedi 活跃航次全量对账（每周$ReconcileDay $ReconcileTime）"

Write-Host ""
Write-Host "完成。查看： Get-ScheduledTask -TaskName '$TaskPrefix-*'"
Write-Host "手动试跑： Start-ScheduledTask -TaskName '$TaskPrefix-incremental'"
Write-Host "删除任务： Unregister-ScheduledTask -TaskName '$TaskPrefix-incremental','$TaskPrefix-reconcile' -Confirm:`$false"
