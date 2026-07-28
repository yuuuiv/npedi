#!/usr/bin/env bash
# 注册 Linux 定时任务：每天早/中/晚各跑一轮增量，每周一次对账。
# 与 Windows 上的 setup_schedule.ps1 一一对应。
#
#   ./setup_schedule.sh                      # 装 cron（默认）
#   ./setup_schedule.sh --systemd            # 装 systemd user timer
#   ./setup_schedule.sh --times 08:00,13:00,20:00
#   ./setup_schedule.sh --remove             # 卸载（cron 与 systemd 都清）
#
# 重复执行会覆盖同名任务，不会叠加。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${NPEDI_PYTHON:-$(command -v python3 || true)}"
MODE="cron"
TIMES="07:30,12:30,19:30"
RECONCILE_TIME="22:30"
RECONCILE_DAY="Sun"          # cron: 0/Sun；systemd: Sun
MARKER="# npedi-sync"        # crontab 里认领自己那几行用的标记

while [ $# -gt 0 ]; do
    case "$1" in
        --systemd) MODE="systemd" ;;
        --cron)    MODE="cron" ;;
        --remove)  MODE="remove" ;;
        --times)   TIMES="$2"; shift ;;
        --reconcile-time) RECONCILE_TIME="$2"; shift ;;
        --reconcile-day)  RECONCILE_DAY="$2"; shift ;;
        --python)  PYTHON="$2"; shift ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

[ -n "$PYTHON" ] || { echo "找不到 python3，请用 --python 指定完整路径" >&2; exit 1; }
chmod +x "$ROOT/run_sync.sh"

echo "项目目录 : $ROOT"
echo "Python   : $PYTHON"
echo "模式     : $MODE"

# ------------------------------------------------------------------ cron

remove_cron() {
    if crontab -l 2>/dev/null | grep -q "$MARKER"; then
        crontab -l 2>/dev/null | grep -v "$MARKER" | crontab -
        echo "  已从 crontab 移除 npedi 任务"
    fi
}

install_cron() {
    remove_cron
    local lines="" hhmm hour min
    # cron 没有"错过就补跑"的机制，但增量窗口从上一次成功的水位线算起，
    # 漏掉的轮次会在下一轮自动补齐，所以不需要 anacron 之类的兜底。
    IFS=',' read -ra arr <<< "$TIMES"
    for hhmm in "${arr[@]}"; do
        hour="${hhmm%%:*}"; min="${hhmm##*:}"
        lines+="${min#0} ${hour#0} * * * cd $ROOT && NPEDI_PYTHON=$PYTHON ./run_sync.sh incremental >> $ROOT/logs/cron.log 2>&1 $MARKER"$'\n'
    done
    hour="${RECONCILE_TIME%%:*}"; min="${RECONCILE_TIME##*:}"
    lines+="${min#0} ${hour#0} * * $RECONCILE_DAY cd $ROOT && NPEDI_PYTHON=$PYTHON ./run_sync.sh reconcile >> $ROOT/logs/cron.log 2>&1 $MARKER"$'\n'

    { crontab -l 2>/dev/null || true; printf '%s' "$lines"; } | crontab -
    echo "  已写入 crontab："
    crontab -l | grep "$MARKER" | sed 's/^/    /'
    echo
    echo "查看： crontab -l"
    echo "日志： tail -f $ROOT/logs/cron.log     单轮明细在 $ROOT/logs/runs/"
    echo "删除： $ROOT/setup_schedule.sh --remove"
}

# --------------------------------------------------------------- systemd

UNIT_DIR="$HOME/.config/systemd/user"

remove_systemd() {
    command -v systemctl >/dev/null 2>&1 || return 0
    local u
    for u in npedi-incremental npedi-reconcile; do
        systemctl --user disable --now "$u.timer" 2>/dev/null || true
        rm -f "$UNIT_DIR/$u.timer" "$UNIT_DIR/$u.service"
    done
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  已移除 systemd user units"
}

write_unit() {
    local name="$1" command="$2" oncal="$3" desc="$4"
    cat > "$UNIT_DIR/$name.service" <<EOF
[Unit]
Description=$desc

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=NPEDI_PYTHON=$PYTHON
ExecStart=$ROOT/run_sync.sh $command
# 退出码 2（token 失效）和 3（已有实例在运行）都不是故障，别让 systemd 记成失败
SuccessExitStatus=2 3
EOF
    cat > "$UNIT_DIR/$name.timer" <<EOF
[Unit]
Description=$desc（定时触发）

[Timer]
${oncal%$'\n'}
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF
    systemctl --user enable --now "$name.timer"
    echo "  已注册：$name.timer"
}

install_systemd() {
    command -v systemctl >/dev/null 2>&1 || { echo "这台机器没有 systemd，请用默认的 cron 模式" >&2; exit 1; }
    mkdir -p "$UNIT_DIR"
    remove_systemd

    local oncal=""
    IFS=',' read -ra arr <<< "$TIMES"
    for hhmm in "${arr[@]}"; do
        oncal+="OnCalendar=*-*-* ${hhmm}:00"$'\n'
    done
    write_unit npedi-incremental incremental "$oncal" "npedi 航次数据增量同步"
    write_unit npedi-reconcile reconcile \
        "OnCalendar=$RECONCILE_DAY *-*-* ${RECONCILE_TIME}:00" "npedi 活跃航次全量对账"

    systemctl --user daemon-reload
    echo
    echo "查看： systemctl --user list-timers 'npedi-*'"
    echo "日志： journalctl --user -u npedi-incremental -f    单轮明细在 $ROOT/logs/runs/"
    echo "试跑： systemctl --user start npedi-incremental.service"
    echo "删除： $ROOT/setup_schedule.sh --remove"
    echo
    # cron / systemd 环境里 $USER 常常是空的，别让 set -u 在这句上翻车
    echo "注意：无人登录时也要跑的话，需要开启常驻 —— sudo loginctl enable-linger ${USER:-$(id -un)}"
}

case "$MODE" in
    cron)    install_cron ;;
    systemd) install_systemd ;;
    remove)  remove_cron; remove_systemd ;;
esac
