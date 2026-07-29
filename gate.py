"""进出门（CODECO）报文采集流程（对应 ARCHITECTURE-GATE.md §4）。

与 npp 管线共用 client/store/config/锁/告警，只在库表和调度上并存。
两条管线的数据性质不同，流程也因此简单得多：报文只增不改，
入库是纯 INSERT OR IGNORE，没有 hash 比对、没有变更历史。

三个已实测的接口事实决定了本模块的写法（详见架构文档 §1、§2）：
  1. voyage 必须用目录原值精确匹配，猜一个字符就恒返回 0 条；
  2. 留空 voyage/vesselCode 会被服务端拒绝（400），只能逐航次查；
  3. 列表按 msgReceiveTime 降序 —— 增量据此提前停，无新报文时每方向只花 1 次请求。
"""

from __future__ import annotations

import logging
from datetime import datetime

from client import AuthExpired, NpediClient
from config import Config
from exporter import export_gate, export_gate_gap
from store import GATE_DIRECTIONS, Store
from sync import EXIT_OK, preflight

log = logging.getLogger("npedi.gate")

DIRECTION_LABEL = {"GATE_IN": "进门", "GATE_OUT": "出门"}


def _merge(target: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    for key, value in delta.items():
        target[key] = target.get(key, 0) + value
    return target


def sync_gate_catalog(
    client: NpediClient, store: Store, now: datetime
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    stats, new_keys = store.upsert_gate_voyages(client.vessel_list(), now)
    log.info(
        "进出门航次目录：接口返回 %d 条（去重后 %d 个船×航次），新航次 %d 个",
        stats["seen"], stats["unique"], stats["new"],
    )
    return stats, new_keys


def fetch_gate_unit(
    client: NpediClient,
    store: Store,
    run_id: int,
    *,
    vesselcode: str,
    voyage: str,
    direction: str,
    label: str,
    early_stop: bool = False,
    verify_dup: bool = False,
) -> tuple[dict[str, int], int, str]:
    """抓一个 (航次 × 方向) 单元，返回 (计数, 接口 total, 见过的最大 msgReceiveTime)。

    early_stop 用于增量：列表按 msgReceiveTime 降序，一旦某页整页都已入库，
    后面的只会更旧，没必要再翻。回填时必须关掉它 —— 断点续爬的航次前面本来
    就有已入库的整页，早停会把后面还没抓的部分留下空洞。
    """
    stats: dict[str, int] = {}
    total = 0
    newest = ""

    for page, page_total, rows in client.iter_scodeco(
        direction=direction, vessel_code=vesselcode, voyage=voyage
    ):
        total = page_total
        if page == 1 and page_total == 0:
            log.info("%s：无报文", label)
            break

        delta = store.insert_gate_events(
            rows, run_id, direction=direction, vesselcode=vesselcode,
            voyage=voyage, verify_dup=verify_dup,
        )
        _merge(stats, delta)
        for row in rows:
            value = str(row.get("msgReceiveTime") or "")
            if value > newest:
                newest = value

        pages = max(1, -(-page_total // client.cfg.gate_page_size))
        log.info(
            "%s 第 %d/%d 页：本页 %d 行｜累计 新增 %d / 已有 %d",
            label, page, pages, len(rows), stats.get("new", 0), stats.get("dup", 0),
        )
        if early_stop and delta.get("new", 0) == 0:
            log.info("%s 第 %d 页无新报文，按时间倒序提前停止", label, page)
            break

    return stats, total, newest


# --------------------------------------------------------------------- 回填

def run_gate_backfill(cfg: Config, args) -> int:
    """全量回填（§4.1）。

    13954 个航次 × 2 个方向再加翻页，总量在 6-12 万次请求，串行温和延时下
    要跑十几到三十几小时 —— 所以这里不是"一次跑完"，而是每次跑一段请求预算，
    靠 (航次 × 方向) 粒度的断点标记连续几晚铺完。中途断掉不丢进度。
    """
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        run_id = store.start_run("gate_backfill", None, now.strftime("%Y%m%d%H%M%S"), cfg.run_log_name)
        stats: dict[str, int] = {}
        try:
            preflight(cfg, client, store, now)
            sync_gate_catalog(client, store, now)

            budget = args.max_requests if args.max_requests is not None else cfg.gate_backfill_max_requests
            spent_before = client.request_count
            units = store.gate_units_pending(limit=args.limit)
            done_units = 0
            if not units:
                log.info("没有待回填的航次×方向，全部已完成")
            else:
                log.info(
                    "待回填 %d 个航次×方向（在册航次优先）｜本次请求预算 %s",
                    len(units), budget or "不限",
                )

            for vesselcode, voyage, name, direction in units:
                spent = client.request_count - spent_before
                if budget and spent >= budget:
                    log.info("已用满本次请求预算 %d 次，停止；下次运行自动从这里续跑", budget)
                    break
                label = (
                    f"[{done_units + 1}/{len(units)}] {name or vesselcode}/{voyage} "
                    f"{DIRECTION_LABEL[direction]}"
                )
                unit_stats, total, newest = fetch_gate_unit(
                    client, store, run_id,
                    vesselcode=vesselcode, voyage=voyage, direction=direction, label=label,
                )
                _merge(stats, unit_stats)
                # total=0 也算完成：目录里挂着的航次本来就有大量没有报文的，
                # 不标完成的话每次运行都会把它们重查一遍，预算全耗在空查询上。
                store.mark_gate_done(vesselcode, voyage, direction, total)
                if newest:
                    store.note_gate_events_seen(
                        vesselcode, voyage, newest=newest, had_new=True,
                        inactive_rounds=cfg.gate_inactive_rounds, retire_ok=False,
                    )
                done_units += 1

            remaining = len(store.gate_units_pending())
            log.info("本次完成 %d 个单元，剩余 %d 个待回填", done_units, remaining)
            store.finish_run(run_id, "ok", requests_made=client.request_count,
                             stats=_run_stats(stats))
            _report_gate(cfg, store, run_id, client, stats, "进出门回填",
                         full_snapshot=bool(args.export_all))
            return EXIT_OK
        except AuthExpired as exc:
            store.finish_run(run_id, "auth_expired", requests_made=client.request_count,
                             stats=_run_stats(stats), error=str(exc))
            raise
        except Exception as exc:
            store.finish_run(run_id, "failed", requests_made=client.request_count,
                             stats=_run_stats(stats), error=str(exc))
            raise


# --------------------------------------------------------------------- 增量

def run_gate_incremental(cfg: Config, args) -> int:
    """每日一次的增量（§4.2）。

    历史航次回填过就是死数据，会动的只有活跃航次；活跃航次里绝大多数
    每轮也没有新报文，靠降序早停各花 1 次请求就能确认。
    """
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        run_id = store.start_run("gate_incremental", None, now.strftime("%Y%m%d%H%M%S"),
                                 cfg.run_log_name)
        stats: dict[str, int] = {}
        try:
            preflight(cfg, client, store, now)
            _, new_keys = sync_gate_catalog(client, store, now)

            # 每个航次本轮的观察结果（新报文数、见过的最大 msgReceiveTime）。
            # 两个阶段都往这里记，最后统一结算 idle_rounds —— 分头记的话，
            # 一个刚在阶段 1 抓到几百条新报文的航次会在阶段 2 被当成"本轮没动静"。
            observed: dict[tuple[str, str], list] = {}

            def observe(key: tuple[str, str], unit_stats: dict[str, int], newest: str) -> None:
                slot = observed.setdefault(key, [0, ""])
                slot[0] += unit_stats.get("new", 0)
                if newest > slot[1]:
                    slot[1] = newest

            # 1) 新航次 / 从没回填过的活跃航次：整段抓，不能早停
            active = store.gate_active_voyages(now, active_days=cfg.gate_active_days)
            active_rows = {(r["vesselcode"], r["voyage"]): r for r in active}
            full_keys = list(dict.fromkeys(list(new_keys) + list(active_rows)))
            pending = store.gate_units_pending(keys=full_keys, limit=cfg.gate_new_voyage_limit)
            pending_units = {(vc, voy, d) for vc, voy, _, d in pending}
            if pending:
                log.info("需要整段回填的单元 %d 个（新航次 %d 个）", len(pending), len(new_keys))
            for idx, (vesselcode, voyage, name, direction) in enumerate(pending, 1):
                label = (f"回填[{idx}/{len(pending)}] {name or vesselcode}/{voyage} "
                         f"{DIRECTION_LABEL[direction]}")
                unit_stats, total, newest = fetch_gate_unit(
                    client, store, run_id,
                    vesselcode=vesselcode, voyage=voyage, direction=direction, label=label,
                )
                _merge(stats, unit_stats)
                observe((vesselcode, voyage), unit_stats, newest)
                store.mark_gate_done(vesselcode, voyage, direction, total)

            # 2) 活跃航次：只看队头，撞见整页已入库就停
            if args.limit:
                active = active[:args.limit]
            log.info("活跃航次 %d 个（目录共 %d 个）", len(active), store.gate_counts()["voyages"])
            budget = args.max_requests if args.max_requests is not None else 0
            spent_before = client.request_count
            for idx, voy in enumerate(active, 1):
                if budget and client.request_count - spent_before >= budget:
                    log.warning("已用满请求预算 %d 次，本轮剩余 %d 个活跃航次留到下一轮",
                                budget, len(active) - idx + 1)
                    break
                vesselcode, voyage = voy["vesselcode"], voy["voyage"]
                for direction in GATE_DIRECTIONS:
                    if (vesselcode, voyage, direction) in pending_units:
                        continue        # 刚在上一步整段抓过了
                    label = (f"[{idx}/{len(active)}] {voy['vesselename'] or vesselcode}/{voyage} "
                             f"{DIRECTION_LABEL[direction]}")
                    unit_stats, _, unit_newest = fetch_gate_unit(
                        client, store, run_id,
                        vesselcode=vesselcode, voyage=voyage, direction=direction,
                        label=label, early_stop=True, verify_dup=True,
                    )
                    _merge(stats, unit_stats)
                    observe((vesselcode, voyage), unit_stats, unit_newest)

            # 3) 结算：没新报文的累加 idle_rounds，攒够轮数且已离开 npp 在册目录才退休。
            #    npp 目录由 npp 增量维护，所以本命令排在当天 npp 增量之后跑（架构 §4.3）。
            npp_mark = store.npp_catalog_watermark()
            retired = 0
            for key, (new_count, newest) in observed.items():
                row = active_rows.get(key)
                # 只在本轮真正巡查过的活跃航次上退休；阶段 1 里新发现的航次不参与
                retire_ok = bool(npp_mark) and row is not None and (
                    row["npp_last_seen"] is None or row["npp_last_seen"] < npp_mark
                )
                if store.note_gate_events_seen(
                    key[0], key[1], newest=newest, had_new=new_count > 0,
                    inactive_rounds=cfg.gate_inactive_rounds, retire_ok=retire_ok,
                ):
                    retired += 1

            if retired:
                log.info("%d 个航次连续 %d 轮无新报文且已离开 npp 目录，转为 inactive",
                         retired, cfg.gate_inactive_rounds)
            if stats.get("conflict"):
                log.warning("有 %d 条报文的内容发生了变化，与'报文不可变'的前提不符，值得排查",
                            stats["conflict"])

            store.finish_run(run_id, "ok", requests_made=client.request_count,
                             stats=_run_stats(stats))
            _report_gate(cfg, store, run_id, client, stats, "进出门增量")
            return EXIT_OK
        except AuthExpired as exc:
            store.finish_run(run_id, "auth_expired", requests_made=client.request_count,
                             stats=_run_stats(stats), error=str(exc))
            raise
        except Exception as exc:
            store.finish_run(run_id, "failed", requests_made=client.request_count,
                             stats=_run_stats(stats), error=str(exc))
            raise


# --------------------------------------------------------------------- 汇报与只读命令

def _run_stats(stats: dict[str, int]) -> dict[str, int]:
    """gate 的计数映射到 sync_runs 的通用列：dup（已入库）记在 unchanged 上。"""
    return {
        "seen": stats.get("seen", 0),
        "new": stats.get("new", 0),
        "unchanged": stats.get("dup", 0),
    }


def _report_gate(cfg: Config, store: Store, run_id: int, client: NpediClient,
                 stats: dict[str, int], kind: str, *, full_snapshot: bool = True) -> None:
    log.info(
        "%s 完成：请求 %d 次｜读取 %d 行｜新增 %d｜已有 %d",
        kind, client.request_count, stats.get("seen", 0),
        stats.get("new", 0), stats.get("dup", 0),
    )
    written = export_gate(cfg, store, run_id, full_snapshot=full_snapshot)
    for name, desc in written.items():
        log.info("导出 %s → %s", name, desc)
    log.info("CSV 目录：%s", cfg.export_dir)


def cmd_gate_export(cfg: Config, args) -> int:
    with Store(cfg.db_path) as store:
        written = export_gate(cfg, store, None)
    for name, desc in written.items():
        print(f"  {name}: {desc}")
    print(f"输出目录：{cfg.export_dir}")
    return EXIT_OK


def cmd_gate_gap(cfg: Config, args) -> int:
    """导出"闸口有报文、npp 核放库查不到"的缺口清单（§5）。"""
    with Store(cfg.db_path) as store:
        summary = store.gate_gap_summary()
        written = export_gate_gap(cfg, store)
    print("=== 进出门 ↔ npp 关联 ===")
    print(f"进出门报文      : {summary['gate_events']}")
    print(f"能对上 npp 的   : {summary['matched']}")
    print(f"npp 缺失的      : {summary['gap']}（涉及 {summary['gap_voyages']} 个航次）")
    for name, desc in written.items():
        print(f"  {name}: {desc}")
    return EXIT_OK


def cmd_gate_status(cfg: Config, args) -> int:
    with Store(cfg.db_path) as store:
        counts = store.gate_counts()
        lo, hi = store.gate_event_time_range()
        pending = len(store.gate_units_pending())
        print("=== 进出门库状态 ===")
        print(f"航次目录        : {counts['voyages']}"
              f"（两个方向都已回填 {counts['voyages_done']}，inactive {counts['voyages_inactive']}）")
        print(f"待回填单元      : {pending} 个航次×方向")
        print(f"报文总数        : {counts['events']}"
              f"（进门 {counts['events_in']} / 出门 {counts['events_out']}）")
        print(f"报文时间范围    : {_fmt(lo)} ~ {_fmt(hi)}")
        print("\n=== 最近的进出门运行 ===")
        for r in store.recent_runs(30):
            if not str(r["kind"]).startswith("gate_"):
                continue
            print(f"  #{r['run_id']:<4} {r['kind']:<18} {r['started_at']} → {r['finished_at'] or '进行中'} "
                  f"{r['status']:<12} 请求 {r['requests_made']:<6} "
                  f"新增 {r['rows_new']:<8} 已有 {r['rows_unchanged']:<8}"
                  + (f" err={r['error'][:60]}" if r["error"] else ""))
    return EXIT_OK


def _fmt(ts: str | None) -> str:
    if not ts or len(ts) != 14 or not ts.isdigit():
        return ts or "-"
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
