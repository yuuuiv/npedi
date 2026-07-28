"""npedi 航次数据采集主流程 + CLI（对应 ARCHITECTURE.md §2、§4、§5）。

用法：
    python sync.py probe                 # 验证架构 §2 的两个假设，结论写入库
    python sync.py backfill              # 首次全量回填（支持断点续爬）
    python sync.py incremental           # 增量更新（Task Scheduler 每日三次调用）
    python sync.py reconcile             # 活跃航次小全量对账（§5 安全网 1）
    python sync.py replay --window 20260701000000,20260728000000
    python sync.py export                # 仅从库导出 CSV
    python sync.py status                # 查看库状态与最近运行

退出码：0 成功 / 1 失败 / 2 token 失效（需人工换 token）/ 3 已有实例在运行
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from client import ApiError, AuthExpired, NpediClient, fmt_compare_window
from config import Config, load_config
from exporter import export_all
from store import Store, split_unvessel

log = logging.getLogger("npedi.sync")

EXIT_OK, EXIT_FAILED, EXIT_AUTH, EXIT_BUSY = 0, 1, 2, 3

# probe 结论在 meta 表里的键
META_STRATEGY = "strategy"                      # all_in_one | per_voyage
META_EMPTY_COMPARE_EXCLUDED = "empty_compare_excluded"   # 1: 窗口会滤掉未比对的行
META_EMPTY_WINDOW_OK = "empty_window_ok"        # 1: compareTime 传空可用（不带时间过滤）
META_PROBE_AT = "probe_at"
META_BACKFILL_PAGE = "backfill_page_done"

# token 计龄（token 是服务端会话，JWT 里没有 exp，客户端解不出过期时间，
# 站点也没有续签接口——只能用"上一个 token 活了多久"做粗略的提前预警）
META_TOKEN_HASH = "token_hash"
META_TOKEN_FIRST_USED = "token_first_used_at"
META_TOKEN_PREV_DAYS = "token_prev_lifetime_days"


# --------------------------------------------------------------------- 基础设施

class FileLock:
    """跨平台单实例锁，防止两轮任务重叠执行（§4.3）。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        # Windows 的字节区间锁与文件指针位置绑定，加锁和解锁必须在同一位置，
        # 而 "a+" 打开时指针在文件末尾 —— 先归零再加锁。
        self._fh.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(f"已有另一个实例在运行（锁文件 {self.path}）") from exc
        self._fh.truncate(0)
        self._fh.write(f"pid={os.getpid()} at={datetime.now():%Y-%m-%d %H:%M:%S}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            # 关闭句柄本身就会释放锁，解锁失败不该让整轮任务失败
            log.debug("释放锁时出错（已忽略）：%s", exc)
        finally:
            self._fh.close()
            self._fh = None


# 会真正联网采集的子命令：只有这些才单独开一个轮次日志
COLLECTING_COMMANDS = ("probe", "backfill", "incremental", "reconcile", "replay")


def setup_logging(cfg: Config, verbose: bool = False, command: str = "") -> None:
    """装两路文件日志：滚动的 sync.log 看全局，logs/runs/ 下一轮一个文件看单轮。

    一轮 backfill 就能写出几千行，全都挤在 sync.log 里的话，
    既会被大小滚动切断，也没法回答"12:30 那轮到底发生了什么"。
    """
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("npedi")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "sync.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    if command not in COLLECTING_COMMANDS:
        return          # status / export 只读，不该往 runs/ 里落文件

    runs_dir = cfg.log_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    cfg.run_log_name = f"{datetime.now():%Y%m%d_%H%M%S}_{command}.log"
    per_run = logging.FileHandler(runs_dir / cfg.run_log_name, encoding="utf-8")
    per_run.setFormatter(fmt)
    root.addHandler(per_run)
    _prune_run_logs(runs_dir, cfg.log_keep_run_files)


def _prune_run_logs(runs_dir: Path, keep: int) -> None:
    """只留最近 keep 个轮次日志。文件名以时间戳打头，按名字排序即按时间排序。"""
    if keep <= 0:
        return
    stale = sorted(runs_dir.glob("*.log"))[:-keep]
    for path in stale:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("清理旧轮次日志失败 %s: %s", path.name, exc)


def raise_alert(cfg: Config, message: str) -> None:
    """token 失效时落一个显式告警文件，等人工更新 .env。"""
    cfg.alert_file.write_text(
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n\n"
        "处理步骤：\n"
        "1. 在有登录态的机器上打开 https://www.npedi.com/onesite/ 并确认已登录\n"
        "2. F12 → Application → Cookies → www.npedi.com → 复制 Web-Token 的值\n"
        "   （或在 Network 里复制任一 /onesite-api/ 请求头 ediAuthorization 中 Bearer 之后的部分）\n"
        "3. 粘贴到本机 .env 的 WEB_TOKEN=，然后手动跑一次 python sync.py incremental\n"
        "4. 成功后本文件会自动删除\n",
        encoding="utf-8",
    )
    log.error("已写入告警文件 %s", cfg.alert_file)


def clear_alert(cfg: Config) -> None:
    if cfg.alert_file.exists():
        cfg.alert_file.unlink()
        log.info("token 已恢复正常，清除告警文件 %s", cfg.alert_file.name)


def track_token(store: Store, cfg: Config, now: datetime) -> None:
    """token 换新时记下启用时间，并结算上一个 token 的寿命（天）。"""
    digest = hashlib.sha256(cfg.token.encode("utf-8")).hexdigest()[:16]
    if store.meta_get(META_TOKEN_HASH) == digest:
        return
    first = store.meta_get(META_TOKEN_FIRST_USED)
    if store.meta_get(META_TOKEN_HASH) and first:
        try:
            days = (now - datetime.strptime(first, "%Y-%m-%d %H:%M:%S")).days
            store.meta_set(META_TOKEN_PREV_DAYS, str(max(days, 0)))
        except ValueError:
            pass
    store.meta_set(META_TOKEN_HASH, digest)
    store.meta_set(META_TOKEN_FIRST_USED, now.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("检测到新 token，开始记录使用时长")


def token_age_days(store: Store, now: datetime) -> int | None:
    first = store.meta_get(META_TOKEN_FIRST_USED)
    if not first:
        return None
    try:
        return max((now - datetime.strptime(first, "%Y-%m-%d %H:%M:%S")).days, 0)
    except ValueError:
        return None


def warn_token_age(store: Store, now: datetime) -> None:
    """当前 token 用龄接近上一个 token 的寿命时，提前一天提醒更换。"""
    age = token_age_days(store, now)
    prev = store.meta_get(META_TOKEN_PREV_DAYS)
    if age is None or not prev:
        return
    prev_days = int(prev)
    if prev_days >= 2 and age >= prev_days - 1:
        log.warning(
            "当前 token 已使用 %d 天，上一个 token 寿命约 %d 天，建议尽快按 README 步骤更换，避免采集中断",
            age, prev_days,
        )


def preflight(cfg: Config, client: NpediClient, store: Store, now: datetime) -> None:
    """每轮开始的公共前置：token 计龄 + 探活。探活失败会在采集开始前就报出失效。"""
    track_token(store, cfg, now)
    warn_token_age(store, now)
    if cfg.auth_probe:
        client.get_info()
        clear_alert(cfg)


def _merge(target: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    for key, value in delta.items():
        target[key] = target.get(key, 0) + value
    return target


# --------------------------------------------------------------------- 采集单元

def fetch_into_store(
    client: NpediClient,
    store: Store,
    run_id: int,
    *,
    unvessel: str = "",
    voyage: str = "",
    compare_time: str = "",
    compare_flag: str = "",
    label: str = "",
) -> dict[str, int]:
    """按页拉取明细并逐页 upsert，返回累计计数。"""
    stats: dict[str, int] = {}
    for page, total, rows in client.iter_integrated(
        unvessel=unvessel, voyage=voyage, compare_time=compare_time, compare_flag=compare_flag
    ):
        if page == 1 and total == 0:
            log.info("%s：无数据", label)
            break
        _merge(stats, store.upsert_containers(rows, run_id))
        pages = max(1, -(-total // client.cfg.page_size))
        log.info(
            "%s 第 %d/%d 页：本页 %d 行｜累计 新增 %d / 变更 %d / 空转 %d / 未变 %d",
            label, page, pages, len(rows),
            stats.get("new", 0), stats.get("updated", 0),
            stats.get("touched", 0), stats.get("unchanged", 0),
        )
    return stats


def sync_voyage_catalog(client: NpediClient, store: Store, now: datetime) -> dict[str, int]:
    items = client.vesselinfo()
    stats = store.upsert_voyages(items, now)
    log.info("航次目录：接口返回 %d 条，其中新航次 %d 个", stats["seen"], stats["new"])
    return stats


def wide_window(now: datetime) -> str:
    """回填用的"足够宽"窗口：十年前 → now + 余量。"""
    return fmt_compare_window(now - timedelta(days=3650), now + timedelta(days=30))


def no_window_or_wide(store: Store, now: datetime) -> str:
    """compareTime 传空若可用就传空（服务端不做时间过滤），否则用十年宽窗口。"""
    return "" if store.meta_get(META_EMPTY_WINDOW_OK) == "1" else wide_window(now)


# --------------------------------------------------------------------- probe

def do_probe(cfg: Config, client: NpediClient, store: Store, now: datetime) -> dict[str, str]:
    """验证 ARCHITECTURE.md §2 的两个假设，结论写入 meta（约 4-5 次请求）。"""
    result: dict[str, str] = {}

    # --- 假设 1：unvessel/voyage 留空是否返回跨航次的全量明细 ---
    # 判据用 total 而不是"第 1 页出现几个航次"：单个航次就可能有几百个箱子
    # （实测 UN9604122/071E 一个航次 684 个），第 1 页全是同一航次很正常，
    # 只看第 1 页会把成立的假设误判为不成立，白白退回 800+ 次请求的降级路径。
    window = fmt_compare_window(now - timedelta(days=7), now + timedelta(hours=cfg.future_margin_hours))
    blank_ok = False
    sample_rows: list[dict] = []
    total_all = 0
    try:
        data = client.integrated_page(1, compare_time=window, page_size=cfg.page_size)
        sample_rows = data.get("list") or []
        total_all = int(data.get("total") or 0)
        if total_all == 0:
            # 窗口内可能真的没数据，放宽到 30 天再试一次
            window = fmt_compare_window(now - timedelta(days=30),
                                        now + timedelta(hours=cfg.future_margin_hours))
            data = client.integrated_page(1, compare_time=window, page_size=cfg.page_size)
            sample_rows = data.get("list") or []
            total_all = int(data.get("total") or 0)
        voyages = {(str(r.get("unvessel")), str(r.get("voyage"))) for r in sample_rows}
        if len(voyages) > 1:
            blank_ok = True
            log.info("假设1 探测：留空查询 total=%d，第 1 页即覆盖 %d 个航次 → 成立", total_all, len(voyages))
        elif sample_rows:
            # 第 1 页只有一个航次时，拿该航次单独查一次做对照：
            # 留空查询的 total 更大，就说明它确实跨了航次。
            one_un = split_unvessel(sample_rows[0].get("unvessel"))
            one_voy = str(sample_rows[0].get("voyage") or "")
            one = client.integrated_page(1, unvessel=one_un, voyage=one_voy, compare_time=window)
            total_one = int(one.get("total") or 0)
            blank_ok = total_all > total_one
            log.info(
                "假设1 探测：留空查询 total=%d，单航次 %s/%s total=%d → %s",
                total_all, one_un, one_voy, total_one, "成立" if blank_ok else "不成立（无法证明跨航次）",
            )
        else:
            log.info("假设1 探测：留空查询无数据 → 不成立")
    except ApiError as exc:
        log.info("假设1 探测：留空查询被拒绝（%s）→ 不成立", exc)

    strategy = "all_in_one" if blank_ok else "per_voyage"
    store.meta_set(META_STRATEGY, strategy)
    result["strategy"] = strategy

    # --- 假设 2：compareTime 窗口过滤的语义 ---
    # 取一个确定有数据的航次做对照。
    probe_unvessel = probe_voyage = ""
    if sample_rows:
        probe_unvessel = split_unvessel(sample_rows[0].get("unvessel"))
        probe_voyage = str(sample_rows[0].get("voyage") or "")
    else:
        for row in client.vesselinfo()[:1]:
            probe_unvessel = split_unvessel(row.get("unvessel"))
            probe_voyage = str(row.get("voyage") or "")

    empty_window_ok = "0"
    empty_excluded = "0"
    if probe_unvessel and probe_voyage:
        label = f"{probe_unvessel}/{probe_voyage}"
        try:
            no_win = client.integrated_page(1, unvessel=probe_unvessel, voyage=probe_voyage, compare_time="")
            total_no_win = int(no_win.get("total") or 0)
            rows_no_win = no_win.get("list") or []
            empty_window_ok = "1" if total_no_win > 0 else "0"

            wide = client.integrated_page(
                1, unvessel=probe_unvessel, voyage=probe_voyage, compare_time=wide_window(now)
            )
            total_wide = int(wide.get("total") or 0)

            # 判据同样用 total：只要"不带窗口"比"十年宽窗口"多，就说明窗口过滤会漏掉一部分行，
            # 保守起见即开启兜底。第 1 页是否肉眼可见未比对行只作为佐证（可能落在后面的页上）。
            has_empty_compare = any(not str(r.get("compareTime") or "").strip() for r in rows_no_win)
            if total_no_win > total_wide:
                empty_excluded = "1"
            log.info(
                "假设2 探测（%s）：无窗口 total=%d，十年宽窗口 total=%d，第 1 页含未比对行=%s → 窗口%s滤掉行",
                label, total_no_win, total_wide, has_empty_compare,
                "会" if empty_excluded == "1" else "不会",
            )
        except ApiError as exc:
            log.warning("假设2 探测失败：%s", exc)

    store.meta_set(META_EMPTY_WINDOW_OK, empty_window_ok)
    store.meta_set(META_EMPTY_COMPARE_EXCLUDED, empty_excluded)
    store.meta_set(META_PROBE_AT, now.strftime("%Y-%m-%d %H:%M:%S"))
    result["empty_window_ok"] = empty_window_ok
    result["empty_compare_excluded"] = empty_excluded
    return result


def ensure_strategy(cfg: Config, client: NpediClient, store: Store, now: datetime) -> str:
    strategy = store.meta_get(META_STRATEGY)
    if not strategy:
        log.info("尚无 probe 结论，先执行一次接口能力探测")
        do_probe(cfg, client, store, now)
        strategy = store.meta_get(META_STRATEGY) or "per_voyage"
    return strategy


def needs_empty_compare_fallback(cfg: Config, store: Store) -> bool:
    """§5 安全网 2：未比对（compareTime 为空）的新行是否会被窗口滤掉。"""
    if cfg.empty_compare_fallback == "on":
        return True
    if cfg.empty_compare_fallback == "off":
        return False
    return store.meta_get(META_EMPTY_COMPARE_EXCLUDED) == "1"


# --------------------------------------------------------------------- 主流程

def run_backfill(cfg: Config, args) -> int:
    """首次全量回填（§4.1）。"""
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        run_id = store.start_run("backfill", None, now.strftime("%Y%m%d%H%M%S"), cfg.run_log_name)
        stats: dict[str, int] = {}
        try:
            # 探活放在 start_run 之后：token 失效的那一轮同样要在 sync_runs 里留痕
            preflight(cfg, client, store, now)
            sync_voyage_catalog(client, store, now)
            strategy = ensure_strategy(cfg, client, store, now)
            window = no_window_or_wide(store, now)

            if strategy == "all_in_one":
                start_page = int(store.meta_get(META_BACKFILL_PAGE) or 0) + 1 if args.resume else 1
                if start_page > 1:
                    log.info("断点续爬：从第 %d 页继续", start_page)
                page_no = start_page - 1
                for page, total, rows in client.iter_integrated(
                    compare_time=window, start_page=start_page
                ):
                    _merge(stats, store.upsert_containers(rows, run_id))
                    page_no = page
                    store.meta_set(META_BACKFILL_PAGE, str(page))
                    pages = max(1, -(-total // cfg.page_size))
                    log.info(
                        "全量回填 第 %d/%d 页｜累计 新增 %d / 变更 %d / 空转 %d / 未变 %d",
                        page, pages, stats.get("new", 0), stats.get("updated", 0),
                        stats.get("touched", 0), stats.get("unchanged", 0),
                    )
                store.conn.execute("UPDATE voyages SET backfilled=1")
                store.conn.commit()
                store.meta_set(META_BACKFILL_PAGE, "0")
                log.info("全量回填完成，共 %d 页", page_no)
            else:
                pending = store.voyages_pending_backfill()
                log.info("按航次回填：待回填 %d 个航次（已完成的自动跳过）", len(pending))
                for idx, voy in enumerate(pending, 1):
                    label = f"[{idx}/{len(pending)}] {voy['unvessel']}/{voy['voyage']}"
                    _merge(stats, fetch_into_store(
                        client, store, run_id,
                        unvessel=voy["unvessel"], voyage=voy["voyage"],
                        compare_time=window, label=label,
                    ))
                    store.mark_backfilled(voy["unvessel"], voy["voyage"])

            store.finish_run(run_id, "ok", requests_made=client.request_count, stats=stats)
            _report(cfg, store, run_id, client, stats, "全量回填")
            return EXIT_OK
        except AuthExpired as exc:
            store.finish_run(run_id, "auth_expired", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise
        except Exception as exc:
            store.finish_run(run_id, "failed", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise


def resolve_window(cfg: Config, store: Store, now: datetime, manual: str | None) -> tuple[str, datetime]:
    """返回 (compareTime 查询窗口, 本轮水位线时刻)。

    水位线取"本轮开始时刻"而不是数据里的最大 compareTime，避免时钟偏移与乱序导致漏数据；
    靠 OVERLAP 的重叠回看来兜底（§4.2 步骤 6）。
    """
    manual = manual or cfg.compare_window_manual
    if manual:
        parts = [p.strip() for p in manual.split(",")]
        if len(parts) != 2 or not all(len(p) == 14 and p.isdigit() for p in parts):
            raise ValueError(f"手动窗口格式应为 `yyyyMMddHHmmss,yyyyMMddHHmmss`，收到 {manual!r}")
        return f"{parts[0]},{parts[1]}", now

    last = store.last_successful_watermark()
    if not last:
        raise RuntimeError("库里没有成功的水位线，请先执行 python sync.py backfill")
    start = datetime.strptime(last, "%Y%m%d%H%M%S") - timedelta(hours=cfg.compare_window_overlap_hours)
    end = now + timedelta(hours=cfg.future_margin_hours)
    return fmt_compare_window(start, end), now


def run_incremental(cfg: Config, args, kind: str = "incremental") -> int:
    """每日三次的增量更新（§4.2）。"""
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        # 先算窗口：算不出来（比如还没跑过 backfill）时直接退出，不必浪费一次请求
        window, watermark = resolve_window(cfg, store, now, getattr(args, "window", None))
        wm_to = None if kind == "replay" else watermark.strftime("%Y%m%d%H%M%S")
        run_id = store.start_run(kind, window.split(",")[0], wm_to, cfg.run_log_name)
        log.info("%s 开始：compareTime 窗口 = %s", kind, window)
        stats: dict[str, int] = {}
        is_replay = kind == "replay"
        try:
            # 探活放在 start_run 之后：token 失效的那一轮同样要在 sync_runs 里留痕
            preflight(cfg, client, store, now)
            # replay 只按窗口重跑：不同步航次目录、不回填新航次，除入库外不改任何状态
            if not is_replay:
                sync_voyage_catalog(client, store, now)
            strategy = ensure_strategy(cfg, client, store, now)
            fallback = needs_empty_compare_fallback(cfg, store)

            # 先确定本轮要全量回填哪些新航次（§4.2 步骤 3，单轮有上限保护）：
            # 这些航次稍后全量抓，per_voyage 的窗口查询对它们是多余请求，直接跳过
            pending = [] if is_replay else store.voyages_pending_backfill(
                limit=cfg.max_new_voyage_backfill_per_run
            )
            pending_keys = {(v["unvessel"], v["voyage"]) for v in pending}

            if strategy == "all_in_one":
                _merge(stats, fetch_into_store(client, store, run_id,
                                               compare_time=window, label="增量(全航次)"))
                if fallback:
                    _merge(stats, fetch_into_store(
                        client, store, run_id, compare_time="", compare_flag="N",
                        label="兜底(未比对行)",
                    ))
            else:
                active = store.active_voyages(
                    now,
                    past_days=cfg.active_past_days,
                    future_days=cfg.active_future_days,
                    unknown_close_days=cfg.active_unknown_close_days,
                )
                skip = sum(1 for v in active if (v["unvessel"], v["voyage"]) in pending_keys)
                log.info("按航次增量：活跃航次 %d 个（全量 %d 个）%s",
                         len(active), store.counts()["voyages"],
                         f"，其中 {skip} 个待回填航次跳过窗口查询" if skip else "")
                for idx, voy in enumerate(active, 1):
                    if (voy["unvessel"], voy["voyage"]) in pending_keys:
                        continue
                    label = f"[{idx}/{len(active)}] {voy['unvessel']}/{voy['voyage']}"
                    _merge(stats, fetch_into_store(
                        client, store, run_id,
                        unvessel=voy["unvessel"], voyage=voy["voyage"],
                        compare_time=window, label=label,
                    ))
                    if fallback:
                        _merge(stats, fetch_into_store(
                            client, store, run_id,
                            unvessel=voy["unvessel"], voyage=voy["voyage"],
                            compare_time="", compare_flag="N", label=label + " 兜底",
                        ))

            if pending:
                log.info("发现 %d 个未回填航次，本轮顺带回填", len(pending))
                full = no_window_or_wide(store, now)
                for idx, voy in enumerate(pending, 1):
                    label = f"新航次[{idx}/{len(pending)}] {voy['unvessel']}/{voy['voyage']}"
                    _merge(stats, fetch_into_store(
                        client, store, run_id,
                        unvessel=voy["unvessel"], voyage=voy["voyage"],
                        compare_time=full, label=label,
                    ))
                    store.mark_backfilled(voy["unvessel"], voy["voyage"])

            store.finish_run(run_id, "ok", requests_made=client.request_count, stats=stats)
            _report(cfg, store, run_id, client, stats, kind)
            return EXIT_OK
        except AuthExpired as exc:
            store.finish_run(run_id, "auth_expired", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise
        except Exception as exc:
            store.finish_run(run_id, "failed", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise


def run_reconcile(cfg: Config, args) -> int:
    """活跃航次小全量对账（§5 安全网 1）：不带时间窗口拉全量，与库中 hash 对账。"""
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        run_id = store.start_run("reconcile", None, now.strftime("%Y%m%d%H%M%S"), cfg.run_log_name)
        stats: dict[str, int] = {}
        try:
            # 探活放在 start_run 之后：token 失效的那一轮同样要在 sync_runs 里留痕
            preflight(cfg, client, store, now)
            sync_voyage_catalog(client, store, now)
            ensure_strategy(cfg, client, store, now)
            window = no_window_or_wide(store, now)
            active = store.active_voyages(
                now,
                past_days=cfg.active_past_days,
                future_days=cfg.active_future_days,
                unknown_close_days=cfg.active_unknown_close_days,
                limit=cfg.reconcile_max_voyages,
            )
            log.info("对账：覆盖 %d 个活跃航次（不带 compareTime 窗口）", len(active))
            for idx, voy in enumerate(active, 1):
                label = f"对账[{idx}/{len(active)}] {voy['unvessel']}/{voy['voyage']}"
                _merge(stats, fetch_into_store(
                    client, store, run_id,
                    unvessel=voy["unvessel"], voyage=voy["voyage"],
                    compare_time=window, label=label,
                ))
                store.mark_backfilled(voy["unvessel"], voy["voyage"])
            store.finish_run(run_id, "ok", requests_made=client.request_count, stats=stats)
            _report(cfg, store, run_id, client, stats, "对账")
            return EXIT_OK
        except AuthExpired as exc:
            store.finish_run(run_id, "auth_expired", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise
        except Exception as exc:
            store.finish_run(run_id, "failed", requests_made=client.request_count,
                             stats=stats, error=str(exc))
            raise


def _report(cfg: Config, store: Store, run_id: int, client: NpediClient,
            stats: dict[str, int], kind: str) -> None:
    log.info(
        "%s 完成：请求 %d 次｜读取 %d 行｜新增 %d｜变更 %d｜空转 %d｜未变 %d",
        kind, client.request_count, stats.get("seen", 0),
        stats.get("new", 0), stats.get("updated", 0),
        stats.get("touched", 0), stats.get("unchanged", 0),
    )
    written = export_all(cfg, store, run_id)
    for name, desc in written.items():
        log.info("导出 %s → %s", name, desc)
    log.info("CSV 目录：%s", cfg.export_dir)


# --------------------------------------------------------------------- CLI

def cmd_probe(cfg: Config, args) -> int:
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as client:
        preflight(cfg, client, store, now)
        result = do_probe(cfg, client, store, now)
    print("\n=== 探测结论（已写入 meta 表，后续运行自动采用）===")
    print(f"采集策略           : {result['strategy']}"
          f"{'（留空 unvessel 一把捞，最省请求）' if result['strategy'] == 'all_in_one' else '（按航次循环）'}")
    print(f"compareTime 可传空 : {result.get('empty_window_ok') == '1'}")
    print(f"窗口会滤掉未比对行 : {result.get('empty_compare_excluded') == '1'}"
          f"{'  → 增量将自动追加 compareFlag=N 兜底查询' if result.get('empty_compare_excluded') == '1' else ''}")
    return EXIT_OK


def cmd_export(cfg: Config, args) -> int:
    # 只刷新全量快照与航次目录；changes_*.csv 是每轮同步的产物，已在磁盘上，不重复生成
    with Store(cfg.db_path) as store:
        written = export_all(cfg, store, None)
    for name, desc in written.items():
        print(f"  {name}: {desc}")
    print(f"输出目录：{cfg.export_dir}")
    return EXIT_OK


def cmd_status(cfg: Config, args) -> int:
    with Store(cfg.db_path) as store:
        counts = store.counts()
        print("=== 库状态 ===")
        print(f"数据库          : {cfg.db_path}")
        print(f"航次            : {counts['voyages']}（已回填 {counts['voyages_backfilled']}）")
        print(f"集装箱明细      : {counts['containers']}")
        print(f"变更历史        : {counts['history']}")
        print(f"采集策略        : {store.meta_get(META_STRATEGY) or '未探测（首次运行会自动 probe）'}")
        print(f"探测时间        : {store.meta_get(META_PROBE_AT) or '-'}")
        print(f"最近成功水位线  : {store.last_successful_watermark() or '-'}")
        first_used = store.meta_get(META_TOKEN_FIRST_USED)
        if first_used:
            age = token_age_days(store, datetime.now())
            prev = store.meta_get(META_TOKEN_PREV_DAYS)
            print(f"token 启用时间  : {first_used}（已用 {age} 天"
                  + (f"，上一个 token 寿命约 {prev} 天" if prev else "") + "）")
        else:
            print("token 启用时间  : 未记录（跑一轮任意联网命令后开始计龄）")
        if cfg.alert_file.exists():
            print(f"\n!! token 告警未清除：{cfg.alert_file}")
        print(f"\n=== 最近运行（单轮日志在 {cfg.log_dir / 'runs'}）===")
        for r in store.recent_runs(10):
            print(f"  #{r['run_id']:<4} {r['kind']:<12} {r['started_at']} → {r['finished_at'] or '进行中'} "
                  f"{r['status']:<12} 请求 {r['requests_made']:<5} "
                  f"新增 {r['rows_new']:<6} 变更 {r['rows_updated']:<6} "
                  f"空转 {r['rows_touched'] or 0:<6} 未变 {r['rows_unchanged']:<7}"
                  + (f" log={r['log_file']}" if r["log_file"] else "")
                  + (f" err={r['error'][:60]}" if r["error"] else ""))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="npedi 航次数据增量爬虫", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--env", type=Path, default=None, help="指定 .env 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="验证接口能力，决定采集策略")

    p_back = sub.add_parser("backfill", help="首次全量回填")
    p_back.add_argument("--resume", action="store_true", help="从上次中断的页码继续（仅 all_in_one 策略）")

    p_inc = sub.add_parser("incremental", help="增量更新（每日三次）")
    p_inc.add_argument("--window", help="手动指定 compareTime 窗口 yyyyMMddHHmmss,yyyyMMddHHmmss")

    sub.add_parser("reconcile", help="活跃航次小全量对账")

    p_replay = sub.add_parser("replay", help="按指定窗口重跑一轮（不推进水位线）")
    p_replay.add_argument("--window", required=True, help="compareTime 窗口 yyyyMMddHHmmss,yyyyMMddHHmmss")

    sub.add_parser("export", help="仅从库导出 CSV")
    sub.add_parser("status", help="查看库状态与最近运行")
    return parser


def dispatch(cfg: Config, command: str, args) -> int:
    """执行一个子命令，统一处理锁、token 失效与异常 → 退出码。"""
    if command in ("export", "status"):
        return {"export": cmd_export, "status": cmd_status}[command](cfg, args)

    handlers = {
        "probe": cmd_probe,
        "backfill": run_backfill,
        "incremental": lambda c, a: run_incremental(c, a, "incremental"),
        "reconcile": run_reconcile,
        "replay": lambda c, a: run_incremental(c, a, "replay"),
    }
    try:
        with FileLock(cfg.lock_file):
            return handlers[command](cfg, args)
    # AuthExpired 继承自 RuntimeError，必须排在 RuntimeError 之前捕获，
    # 否则 token 失效会被当成普通失败，既不写告警文件也拿不到退出码 2。
    except AuthExpired as exc:
        log.error("token 失效：%s", exc)
        raise_alert(cfg, str(exc))
        return EXIT_AUTH
    except RuntimeError as exc:
        if "已有另一个实例" in str(exc):
            log.error("%s", exc)
            return EXIT_BUSY
        log.error("运行失败：%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        log.warning("已中断（下次可用 --resume 或直接重跑，upsert 幂等不会重复入库）")
        return EXIT_FAILED
    except Exception as exc:
        log.exception("运行失败：%s", exc)
        return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.env)
    setup_logging(cfg, args.verbose, args.command)
    return dispatch(cfg, args.command, args)


if __name__ == "__main__":
    sys.exit(main())
