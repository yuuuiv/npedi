#!/usr/bin/env bash
# 注册 Linux 定时任务：npp 每天早/中/晚各一轮增量 + 每周一次对账，
# 进出门每天一轮增量（20:30）+ 每晚一段历史回填（00:30）。
# 与 Windows 上的 setup_schedule.ps1 一一对应。
#
#   ./setup_schedule.sh                      # 装 cron（默认）
#   ./setup_schedule.sh --systemd            # 装 systemd user timer
#   ./setup_schedule.sh --times 08:00,13:00,20:00
#   ./setup_schedule.sh --no-gate            # 只装 npp 管线，不装进出门
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
# 进出门管线：增量排在当天最后一轮 npp 增量之后（它依赖 npp 目录判断航次死活），
# 回填放在后半夜，一晚跑一段请求预算，连着几晚把历史铺完。
GATE_TIME="20:30"
GATE_BACKFILL_TIME="00:30"
WITH_GATE=1
MARKER="# npedi-sync"        # crontab 里认领自己那几行用的标记

while [ $# -gt 0 ]; do
    case "$1" in
        --systemd) MODE="systemd" ;;
        --cron)    MODE="cron" ;;
        --remove)  MODE="remove" ;;
        --times)   TIMES="$2"; shift ;;
        --reconcile-time) RECONCILE_TIME="$2"; shift ;;
        --reconcile-day)  RECONCILE_DAY="$2"; shift ;;
        --gate-time) GATE_TIME="$2"; shift ;;
        --gate-backfill-time) GATE_BACKFILL_TIME="$2"; shift ;;
        --no-gate) WITH_GATE=0 ;;
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

    if [ "$WITH_GATE" = "1" ]; then
        hour="${GATE_TIME%%:*}"; min="${GATE_TIME##*:}"
        lines+="${min#0} ${hour#0} * * * cd $ROOT && NPEDI_PYTHON=$PYTHON ./run_sync.sh gate-incremental >> $ROOT/logs/cron.log 2>&1 $MARKER"$'\n'
        hour="${GATE_BACKFILL_TIME%%:*}"; min="${GATE_BACKFILL_TIME##*:}"
        lines+="${min#0} ${hour#0} * * * cd $ROOT && NPEDI_PYTHON=$PYTHON ./run_sync.sh gate-backfill >> $ROOT/logs/cron.log 2>&1 $MARKER"$'\n'
    fi

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
    for u in npedi-incremental npedi-reconcile npedi-gate npedi-gate-backfill; do
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
    if [ "$WITH_GATE" = "1" ]; then
        write_unit npedi-gate gate-incremental \
            "OnCalendar=*-*-* ${GATE_TIME}:00" "npedi 进出门报文增量"
        write_unit npedi-gate-backfill gate-backfill \
            "OnCalendar=*-*-* ${GATE_BACKFILL_TIME}:00" "npedi 进出门历史回填（每晚一段预算）"
    fi

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
