<#
.SYNOPSIS
    分段跑完进出门历史回填：每段跑一个请求预算，然后歇一会儿，直到全部铺完自动停。
.DESCRIPTION
    对方是口岸政务系统，这个脚本的用意是把总量摊开、留出空档，而不是一口气打满。
    每段的请求速率与 npp 的 backfill 完全一致（串行、随机 0.5-1 秒间隔），
    默认一段约 45 分钟，跟那次跑了 3163 次请求的 npp 回填一个量级。

    断点记在航次×方向一级，中途关窗口、断电、token 失效都不丢进度，
    重跑本脚本即可续上。全部回填完成后脚本自己退出，不会空转。
.PARAMETER ChunkRequests
    每段的请求预算，默认 3000（约 45 分钟）。
.PARAMETER PauseMinutes
    两段之间歇多久，默认 30 分钟。
.PARAMETER RetryMinutes
    token 失效后每隔多少分钟重试，默认 10。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_gate_backfill.ps1
.EXAMPLE
    # 更保守：每段 1500 次请求，歇一小时
    powershell -ExecutionPolicy Bypass -File .\run_gate_backfill.ps1 -ChunkRequests 1500 -PauseMinutes 60
.NOTES
    跑的过程中别关这个窗口。npp 的日常增量不受影响 —— 两条管线用各自的锁文件。
#>
param(
    [int]$ChunkRequests = 3000,
    [int]$PauseMinutes = 30,
    [int]$RetryMinutes = 10,
    [string]$Python = "python"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 日志里有中文，GBK 控制台上会抛 UnicodeEncodeError 把整段打断
$env:PYTHONIOENCODING = "utf-8"

New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
$wrapperLog = Join-Path $root "logs\gate_backfill_wrapper.log"

function Say {
    param([string]$Message, [string]$Color = "Gray")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $line -ForegroundColor $Color
    Add-Content -Path $wrapperLog -Value $line -Encoding UTF8
}

# 剩余与总共多少个航次×方向。走项目自己的 config/store，不硬编码库路径。
# 分母取"当前"总量而不是开跑时的快照：航次目录是活的，每段开头同步目录都会
# 发现新航次，拿旧快照当分母会算出负的进度。
function Get-Progress {
    $out = & $Python -c "from config import load_config; from store import Store; s=Store(load_config().db_path); print(len(s.gate_units_pending()), s.gate_counts()['voyages'] * 2); s.close()"
    if ($LASTEXITCODE -ne 0) { throw "读取回填进度失败：$out" }
    $parts = ($out | Select-Object -Last 1).Trim() -split '\s+'
    return @{ Remaining = [int]$parts[0]; Total = [int]$parts[1] }
}

$p = Get-Progress
Say "开始分段回填：待回填 $($p.Remaining) / $($p.Total) 个航次×方向，每段 $ChunkRequests 次请求，段间歇 $PauseMinutes 分钟" "Cyan"

$chunk = 0
$code = 0
while ($true) {
    $p = Get-Progress
    if ($p.Remaining -eq 0) {
        Say "全部回填完成（$($p.Total) 个单元）" "Green"
        $code = 0
        break
    }

    $chunk++
    $donePct = [math]::Round(100 * ($p.Total - $p.Remaining) / [math]::Max($p.Total, 1), 1)
    Say "第 $chunk 段开始：剩余 $($p.Remaining) / $($p.Total) 个单元（已完成 $donePct%）" "Cyan"

    & $Python (Join-Path $root "sync.py") gate-backfill --max-requests $ChunkRequests
    $code = $LASTEXITCODE

    if ($code -eq 3) { Say "已有另一个回填实例在跑，本次退出" "Yellow"; break }
    if ($code -eq 2) {
        # token 是服务端会话、短信验证码登录，没法自动续签，只能等人更新 .env
        Say "token 已失效。请按 ALERT_TOKEN_EXPIRED 里的步骤更新 .env，$RetryMinutes 分钟后自动重试" "Yellow"
        Start-Sleep -Seconds ($RetryMinutes * 60)
        continue
    }
    if ($code -ne 0) {
        Say "第 $chunk 段失败（退出码 $code），详见 logs\runs\ 下的本轮日志。断点已保留，修好后重跑本脚本" "Red"
        break
    }

    # 跑完这段先看看还剩多少，没剩就别白歇这 30 分钟了
    $p = Get-Progress
    if ($p.Remaining -eq 0) {
        Say "全部回填完成（$($p.Total) 个单元）" "Green"
        break
    }
    Say "第 $chunk 段结束，歇 $PauseMinutes 分钟" "Gray"
    Start-Sleep -Seconds ($PauseMinutes * 60)
}

exit $code
