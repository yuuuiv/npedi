<#
.SYNOPSIS
    Task Scheduler 调用的包装脚本：执行一轮增量同步并按退出码给出提示。
.PARAMETER Command
    sync.py 的子命令，默认 incremental。周末对账可传 reconcile。
#>
param(
    [string]$Command = "incremental",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

& $Python (Join-Path $root "sync.py") $Command
$code = $LASTEXITCODE

switch ($code) {
    0 { Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Command 完成" }
    2 {
        Write-Warning "token 已失效，本轮未采集。请按 ALERT_TOKEN_EXPIRED 文件里的步骤更新 .env"
        # 弹一个 Windows 通知，避免定时任务静默失败没人发现
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $icon = New-Object System.Windows.Forms.NotifyIcon
            $icon.Icon = [System.Drawing.SystemIcons]::Warning
            $icon.Visible = $true
            $icon.ShowBalloonTip(10000, "npedi 采集中断", "Web-Token 已失效，需要手动更新 .env", "Warning")
            Start-Sleep -Seconds 10
            $icon.Dispose()
        } catch { }
    }
    3 { Write-Warning "上一轮仍在运行，本轮跳过" }
    default { Write-Error "$Command 失败（退出码 $code），详见 logs\sync.log" }
}

exit $code
