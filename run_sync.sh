#!/usr/bin/env bash
# cron / systemd 调用的包装脚本：执行一轮同步，按退出码给出提示。
# 与 Windows 上的 run_sync.ps1 一一对应。
#
#   ./run_sync.sh                # 一轮增量
#   ./run_sync.sh reconcile      # 一轮对账
#   NPEDI_PYTHON=/opt/py/bin/python3 ./run_sync.sh
#
# 故意不用 set -e：整个脚本就是靠退出码分支来决定说什么话的。
set -uo pipefail

COMMAND="${1:-incremental}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${NPEDI_PYTHON:-python3}"

cd "$ROOT" || exit 1
mkdir -p logs

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# 桌面通知只在有会话时才可能成功，失败一概忽略——服务器上本来就没有。
notify() {
    command -v notify-send >/dev/null 2>&1 || return 0
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 0
    notify-send -u critical "npedi 采集中断" "$1" 2>/dev/null || true
}

"$PYTHON" "$ROOT/sync.py" "$COMMAND"
code=$?

case $code in
    0) echo "[$(stamp)] $COMMAND 完成" ;;
    2)
        echo "[$(stamp)] token 已失效，本轮未采集。请按 ALERT_TOKEN_EXPIRED 里的步骤更新 .env" >&2
        notify "Web-Token 已失效，需要手动更新 .env"
        ;;
    3) echo "[$(stamp)] 上一轮仍在运行，本轮跳过" ;;
    *) echo "[$(stamp)] $COMMAND 失败（退出码 $code），详见 logs/runs/ 下本轮日志" >&2 ;;
esac

exit $code
