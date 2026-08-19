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

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timedelta

from client import AuthExpired, NpediClient
from config import Config
from exporter import export_gate, export_gate_gap
from store import GATE_DIRECTIONS, Store
from sync import (
    EXIT_OK,
    observe_token_auth_failure,
    observe_token_run_ok,
    preflight,
)

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
    first_page_data: dict | None = None,
) -> tuple[dict[str, int], int, str]:
    """抓一个 (航次 × 方向) 单元，返回 (计数, 接口 total, 见过的最大 msgReceiveTime)。

    early_stop 用于增量：列表按 msgReceiveTime 降序，一旦某页整页都已入库，
    后面的只会更旧，没必要再翻。回填时必须关掉它 —— 断点续爬的航次前面本来
    就有已入库的整页，早停会把后面还没抓的部分留下空洞。
    """
    stats: dict[str, int] = {}
    total = 0
    newest = ""

    def pages():
        # History discovery may already have paid for page 1.  Reuse it and
        # drive pagination here so an empty middle page or the hard page cap
        # cannot be mistaken for a completed direction.
        page = 1
        fetched = 0
        data = first_page_data
        while True:
            if data is None:
                data = client.scodeco_page(
                    page,
                    direction=direction,
                    vessel_code=vesselcode,
                    voyage=voyage,
                )
            rows = list(data.get("list") or [])
            total = int(data.get("total") or 0)
            yield page, total, rows
            fetched += len(rows)
            if fetched >= total:
                return
            if not rows:
                raise RuntimeError(
                    f"{label} 第 {page} 页为空，但接口 total={total}、仅取得 {fetched} 行"
                )
            if page >= client.cfg.max_pages_per_query:
                raise RuntimeError(
                    f"{label} 超过最大翻页数 {client.cfg.max_pages_per_query}，方向未标完成"
                )
            page += 1
            data = None

    for page, page_total, rows in pages():
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


def _history_window(args) -> tuple[str, str]:
    """Return an inclusive CLI date range as SQL's half-open interval."""
    try:
        start = datetime.strptime(args.eta_start, "%Y-%m-%d")
        end = datetime.strptime(args.eta_end, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("--eta-start/--eta-end 必须是 YYYY-MM-DD") from exc
    if end < start:
        raise ValueError("--eta-end 不能早于 --eta-start")
    if args.limit <= 0:
        raise ValueError("--limit 必须大于 0")
    if args.max_requests is not None and args.max_requests < 0:
        raise ValueError("--max-requests 不能小于 0")
    if args.delay_ms < 0:
        raise ValueError("--delay-ms 不能小于 0")
    if not 1 <= args.workers <= 6:
        raise ValueError("--workers 必须在 1–6 之间")
    if args.max_requests and args.max_requests < 1 + 2 * args.workers:
        raise ValueError("--max-requests 至少要覆盖预检和每个 worker 的双向点查")
    if args.sample_strata and args.sample_seed is None:
        raise ValueError("--sample-strata 必须配合 --sample-seed")
    if args.unknown_vessels_only and args.include_new_vessels:
        raise ValueError("--unknown-vessels-only 与 --include-new-vessels 不能同时使用")
    if not args.allow_recent:
        latest_safe = (datetime.now() - timedelta(days=30)).date()
        if end.date() > latest_safe:
            raise ValueError(
                f"为避免把迟到报文永久判空，--eta-end 默认不能晚于 {latest_safe}; "
                "确需近期范围请显式加 --allow-recent"
            )
    return start.strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d")


def _stable_history_sample(
    candidates: list[dict], *, size: int, seed: str
) -> tuple[list[dict], str]:
    """Select a reproducible uniform sample of vessel/voyage pairs."""
    def rank(candidate: dict) -> bytes:
        material = (
            f"{seed}\x1f{candidate['vesselcode']}\x1f{candidate['voyage']}"
        ).encode("utf-8")
        return hashlib.sha256(material).digest()

    selected = sorted(candidates, key=rank)[:size]
    manifest = "\n".join(
        f"{candidate['vesselcode']}\x1f{candidate['voyage']}"
        for candidate in selected
    ).encode("utf-8")
    fingerprint = hashlib.sha256(manifest).hexdigest()[:16]
    return selected, fingerprint


def _history_prefix(vesselcode: str) -> str:
    code = vesselcode.strip().upper()
    for prefix in ("UN", "FC", "CN"):
        if code.startswith(prefix):
            return prefix
    return "OTHER"


def _parse_sample_strata(spec: str) -> dict[str, int]:
    allocations: dict[str, int] = {}
    for item in spec.split(","):
        try:
            prefix, raw_count = item.split(":", 1)
            prefix = prefix.strip().upper()
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(
                "--sample-strata 格式应为 UN:80,FC:10,CN:6"
            ) from exc
        if prefix not in {"UN", "FC", "CN", "OTHER"} or count < 0:
            raise ValueError("--sample-strata 前缀仅支持 UN/FC/CN/OTHER，数量不能为负")
        allocations[prefix] = count
    if not allocations or sum(allocations.values()) <= 0:
        raise ValueError("--sample-strata 至少需要一个正样本量")
    return allocations


def _stable_stratified_history_sample(
    candidates: list[dict], *, allocations: dict[str, int], seed: str
) -> tuple[list[dict], str, dict[str, int]]:
    selected: list[dict] = []
    universe_counts: dict[str, int] = {}
    for prefix in sorted(allocations):
        stratum = [
            candidate for candidate in candidates
            if _history_prefix(candidate["vesselcode"]) == prefix
        ]
        universe_counts[prefix] = len(stratum)
        sample, _ = _stable_history_sample(
            stratum,
            size=min(allocations[prefix], len(stratum)),
            seed=seed,
        )
        selected.extend(sample)
    manifest = "\n".join(
        f"{candidate['vesselcode']}\t{candidate['voyage']}"
        for candidate in selected
    ).encode("utf-8")
    fingerprint = hashlib.sha256(manifest).hexdigest()
    return selected, fingerprint, universe_counts


def _run_gate_history_worker(
    cfg: Config,
    *,
    worker_id: int,
    candidates: list[dict],
    run_id: int,
    request_budget: int | None,
    stop_event: threading.Event,
    probe_only: bool = False,
) -> dict:
    """Process one disjoint candidate shard with its own DB/API connection."""
    result = {
        "worker_id": worker_id,
        "requests": 0,
        "probed": 0,
        "hits": 0,
        "empty": 0,
        "completed": 0,
        "deferred": 0,
        "stats": {},
        "error": None,
    }
    worker_cfg = copy(cfg)
    # Token renewal is deliberately owned by the parent process preflight.
    # If a worker observes an expired token it stops the chunk with its
    # direction checkpoints intact; the wrapper then starts the same chunk
    # again, and that new parent preflight performs at most one SMS login.
    # This prevents six workers from independently entering the login flow.
    worker_cfg.auto_login = False
    try:
        with Store(worker_cfg.db_path) as store, NpediClient(worker_cfg) as client:
            for index, candidate in enumerate(candidates, 1):
                if stop_event.is_set():
                    break
                spent = client.request_count
                exact_gate_match = bool(candidate["exact_gate_match"])
                done_at = {
                    "GATE_IN": candidate["existing_gatein_done_at"],
                    "GATE_OUT": candidate["existing_gateout_done_at"],
                }
                pending_directions = [
                    direction for direction in GATE_DIRECTIONS
                    if not done_at[direction]
                ]
                if not pending_directions:
                    continue

                totals: dict[str, int] = {
                    "GATE_IN": int(
                        candidate["existing_gatein_total"]
                        or candidate["gatein_total"] or 0
                    ),
                    "GATE_OUT": int(
                        candidate["existing_gateout_total"]
                        or candidate["gateout_total"] or 0
                    ),
                }

                def pages_for(direction: str) -> int:
                    return max(1, -(-totals[direction] // worker_cfg.gate_page_size))

                if candidate["status"] == "hit":
                    estimated = sum(
                        pages_for(direction) for direction in pending_directions
                    )
                    if request_budget and spent + estimated > request_budget:
                        result["deferred"] += 1
                        log.info(
                            "历史[W%d %d/%d] %s：已知命中约需 %d 次请求，"
                            "超过本 worker 剩余 %d，延后",
                            worker_id, index, len(candidates), candidate["last_eta"],
                            estimated, max(0, request_budget - spent),
                        )
                        continue

                minimum_requests = len(pending_directions) if exact_gate_match else 2
                if request_budget and spent + minimum_requests > request_budget:
                    log.info(
                        "历史 W%d 已用 %d/%d 次请求，保留断点后停止",
                        worker_id, spent, request_budget,
                    )
                    break

                vesselcode = candidate["vesselcode"]
                voyage = candidate["voyage"]
                label = (
                    f"历史[W{worker_id} {index}/{len(candidates)}] "
                    f"{candidate['last_eta']}"
                )
                first_pages: dict[str, dict] = {}
                probe_directions = (
                    pending_directions if exact_gate_match else list(GATE_DIRECTIONS)
                )
                for direction in probe_directions:
                    data = client.scodeco_page(
                        1,
                        direction=direction,
                        vessel_code=vesselcode,
                        voyage=voyage,
                    )
                    first_pages[direction] = data
                    totals[direction] = int(data.get("total") or 0)
                result["probed"] += 1
                store.note_gate_history_probe(
                    vesselcode,
                    voyage,
                    gatein_total=totals["GATE_IN"],
                    gateout_total=totals["GATE_OUT"],
                )

                if not exact_gate_match and sum(totals.values()) == 0:
                    store.finish_gate_history_candidate(
                        vesselcode,
                        voyage,
                        status="empty",
                        gatein_total=0,
                        gateout_total=0,
                    )
                    result["empty"] += 1
                    log.info("%s：双向无报文", label)
                    continue

                result["hits"] += 1
                if probe_only:
                    result["deferred"] += 1
                    log.info(
                        "%s：点查命中，进门 %d / 出门 %d；probe-only 保留待回填",
                        label, totals["GATE_IN"], totals["GATE_OUT"],
                    )
                    continue
                expected_remaining = sum(
                    max(
                        0,
                        -(-totals[direction] // worker_cfg.gate_page_size) - 1,
                    )
                    for direction in probe_directions
                )
                if (
                    request_budget
                    and client.request_count + expected_remaining > request_budget
                ):
                    result["deferred"] += 1
                    log.info(
                        "%s：命中但还需 %d 页，超过本 worker 剩余 %d；"
                        "已保存命中，延后完整回填",
                        label, expected_remaining,
                        max(0, request_budget - client.request_count),
                    )
                    continue

                store.add_gate_history_voyage(
                    vesselcode,
                    voyage,
                    candidate["vesselename"] or "",
                )
                for direction in pending_directions:
                    unit_stats, total, newest = fetch_gate_unit(
                        client,
                        store,
                        run_id,
                        vesselcode=vesselcode,
                        voyage=voyage,
                        direction=direction,
                        label=f"{label} {DIRECTION_LABEL[direction]}",
                        first_page_data=first_pages[direction],
                    )
                    totals[direction] = total
                    _merge(result["stats"], unit_stats)
                    store.mark_gate_done(vesselcode, voyage, direction, total)
                    if newest:
                        store.note_gate_events_seen(
                            vesselcode,
                            voyage,
                            newest=newest,
                            had_new=unit_stats.get("new", 0) > 0,
                            inactive_rounds=worker_cfg.gate_inactive_rounds,
                            retire_ok=False,
                        )
                store.finish_gate_history_candidate(
                    vesselcode,
                    voyage,
                    status="complete",
                    gatein_total=totals["GATE_IN"],
                    gateout_total=totals["GATE_OUT"],
                )
                result["completed"] += 1
                log.info(
                    "%s：命中，进门 %d / 出门 %d，已完整入库",
                    label, totals["GATE_IN"], totals["GATE_OUT"],
                )
            result["requests"] = client.request_count
    except Exception as exc:
        stop_event.set()
        result["error"] = exc
        try:
            result["requests"] = client.request_count
        except UnboundLocalError:
            pass
    return result


def run_gate_history_backfill(cfg: Config, args) -> int:
    """Probe/backfill plan-derived history with 1–6 disjoint workers.

    Candidate assignment happens once under the gate process lock, so workers
    never query the same pair. Each worker owns an API and SQLite connection;
    page commits and direction checkpoints keep interruption recovery idempotent.
    """
    eta_start, eta_end_exclusive = _history_window(args)
    delay_seconds = args.delay_ms / 1000.0
    cfg.request_delay = (delay_seconds, delay_seconds * 1.25)
    now = datetime.now()
    with Store(cfg.db_path) as store, NpediClient(cfg) as preflight_client:
        run_id = store.start_run(
            "gate_history_backfill",
            eta_start,
            eta_end_exclusive,
            cfg.run_log_name,
        )
        stats: dict[str, int] = {}
        requests_made = 0
        try:
            preflight(
                cfg, preflight_client, store, now,
                run_id=run_id, run_kind="gate_history_backfill",
            )
            requests_made = preflight_client.request_count
            seeded = store.seed_gate_history_candidates(
                eta_start,
                eta_end_exclusive,
                known_vessels_only=not (
                    args.include_new_vessels or args.unknown_vessels_only
                ),
                refresh=args.refresh_candidates,
            )
            log.info(
                "历史候选建队：新增 %d 对｜隔离坏键 %d 对",
                seeded["inserted"], seeded.get("rejected", 0),
            )
            if args.seed_only:
                store.finish_run(
                    run_id, "ok", requests_made=requests_made,
                    stats=_run_stats(stats),
                )
                observe_token_run_ok(
                    store, cfg, run_id=run_id,
                    run_kind="gate_history_backfill",
                    request_count=requests_made,
                )
                log.info("历史候选只建队，不发 CODECO 点查请求")
                return EXIT_OK
            catalog_scope: bool | None
            if args.unknown_vessels_only:
                catalog_scope = False
                scope_label = "未知船码"
            elif args.include_new_vessels:
                catalog_scope = None
                scope_label = "全部船码"
            else:
                catalog_scope = True
                scope_label = "已知船码"

            scoped_pending = store.gate_history_candidate_count_in_scope(
                eta_start,
                eta_end_exclusive,
                statuses=("pending", "hit"),
                vessel_in_catalog=catalog_scope,
            )
            if args.sample_seed is not None:
                universe = [
                    dict(row) for row in store.gate_history_candidates_pending(
                        eta_start=eta_start,
                        eta_end_exclusive=eta_end_exclusive,
                        limit=None,
                        statuses=("pending", "hit", "empty", "complete"),
                        vessel_in_catalog=catalog_scope,
                    )
                ]
                if args.sample_strata:
                    allocations = _parse_sample_strata(args.sample_strata)
                    sample, fingerprint, universe_counts = (
                        _stable_stratified_history_sample(
                            universe,
                            allocations=allocations,
                            seed=args.sample_seed,
                        )
                    )
                    log.info(
                        "历史分层样本：总体分层=%s｜目标分层=%s",
                        universe_counts, allocations,
                    )
                else:
                    sample, fingerprint = _stable_history_sample(
                        universe,
                        size=args.limit,
                        seed=args.sample_seed,
                    )
                candidates = [
                    candidate for candidate in sample
                    if candidate["status"] in {"pending", "hit"}
                ]
                log.info(
                    "历史固定样本：范围 %s｜总体 %d 对｜seed=%s｜"
                    "样本 %d 对｜待处理 %d 对｜fingerprint=%s",
                    scope_label, len(universe), args.sample_seed,
                    len(sample), len(candidates), fingerprint,
                )
            else:
                candidates = [
                    dict(row) for row in store.gate_history_candidates_pending(
                        eta_start=eta_start,
                        eta_end_exclusive=eta_end_exclusive,
                        limit=args.limit,
                        vessel_in_catalog=catalog_scope,
                    )
                ]
            worker_count = min(args.workers, max(1, len(candidates)))
            log.info(
                "历史候选：%s %d｜%s待处理 %d｜本批 %d 对｜"
                "%d workers｜每 worker 请求间隔 %.2f–%.2f 秒",
                "复用已生成候选，新增" if seeded["reused"] else "重新生成，新增",
                seeded["inserted"], scope_label, scoped_pending, len(candidates),
                worker_count, cfg.request_delay[0], cfg.request_delay[1],
            )

            shards = [candidates[i::worker_count] for i in range(worker_count)]
            if args.max_requests:
                available = max(0, args.max_requests - requests_made)
                base, extra = divmod(available, worker_count)
                budgets: list[int | None] = [
                    base + (1 if i < extra else 0) for i in range(worker_count)
                ]
            else:
                budgets = [None] * worker_count

            stop_event = threading.Event()
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="gate-history",
            ) as pool:
                futures = [
                    pool.submit(
                        _run_gate_history_worker,
                        cfg,
                        worker_id=i + 1,
                        candidates=shards[i],
                        run_id=run_id,
                        request_budget=budgets[i],
                        stop_event=stop_event,
                        probe_only=args.probe_only,
                    )
                    for i in range(worker_count)
                ]
                results = [future.result() for future in futures]

            totals = {
                key: sum(int(result[key]) for result in results)
                for key in ("requests", "probed", "hits", "empty", "completed", "deferred")
            }
            requests_made += totals["requests"]
            for result in results:
                _merge(stats, result["stats"])

            errors = [result["error"] for result in results if result["error"]]
            if errors:
                # Authentication loss is a run-wide condition.  Prefer it
                # over a simultaneous secondary worker error so dispatch
                # returns EXIT_AUTH and the wrapper hands renewal back to the
                # next parent preflight.
                auth_errors = [error for error in errors if isinstance(error, AuthExpired)]
                raise auth_errors[0] if auth_errors else errors[0]

            candidate_counts = store.gate_history_candidate_counts()
            log.info(
                "历史补爬本批完成：%d workers｜探测 %d｜命中 %d｜空 %d｜"
                "入库完成 %d｜延后 %d｜队列 pending=%d hit=%d empty=%d complete=%d",
                worker_count, totals["probed"], totals["hits"], totals["empty"],
                totals["completed"], totals["deferred"],
                candidate_counts["pending"], candidate_counts["hit"],
                candidate_counts["empty"], candidate_counts["complete"],
            )
            store.finish_run(
                run_id,
                "ok",
                requests_made=requests_made,
                stats=_run_stats(stats),
            )
            observe_token_run_ok(
                store, cfg, run_id=run_id,
                run_kind="gate_history_backfill",
                request_count=requests_made,
            )
            return EXIT_OK
        except AuthExpired as exc:
            observe_token_auth_failure(
                store, cfg, run_id=run_id,
                run_kind="gate_history_backfill",
            )
            store.finish_run(
                run_id,
                "auth_expired",
                requests_made=requests_made,
                stats=_run_stats(stats),
                error=str(exc),
            )
            raise
        except Exception as exc:
            store.finish_run(
                run_id,
                "failed",
                requests_made=requests_made,
                stats=_run_stats(stats),
                error=str(exc),
            )
            raise


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
            preflight(
                cfg, client, store, now,
                run_id=run_id, run_kind="gate_backfill",
            )
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
            observe_token_run_ok(
                store, cfg, run_id=run_id, run_kind="gate_backfill",
                request_count=client.request_count,
            )
            _report_gate(cfg, store, run_id, client, stats, "进出门回填",
                         full_snapshot=bool(args.export_all))
            return EXIT_OK
        except AuthExpired as exc:
            observe_token_auth_failure(
                store, cfg, run_id=run_id, run_kind="gate_backfill"
            )
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
            preflight(
                cfg, client, store, now,
                run_id=run_id, run_kind="gate_incremental",
            )
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
            observe_token_run_ok(
                store, cfg, run_id=run_id, run_kind="gate_incremental",
                request_count=client.request_count,
            )
            _report_gate(cfg, store, run_id, client, stats, "进出门增量")
            return EXIT_OK
        except AuthExpired as exc:
            observe_token_auth_failure(
                store, cfg, run_id=run_id, run_kind="gate_incremental"
            )
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
        history_counts = store.gate_history_candidate_counts()
        lo, hi = store.gate_event_time_range()
        pending = len(store.gate_units_pending())
        print("=== 进出门库状态 ===")
        print(f"航次目录        : {counts['voyages']}"
              f"（两个方向都已回填 {counts['voyages_done']}，inactive {counts['voyages_inactive']}）")
        print(f"待回填单元      : {pending} 个航次×方向")
        print(
            "目录外历史候选  : "
            f"pending {history_counts['pending']} / hit待续 {history_counts['hit']} / "
            f"empty {history_counts['empty']} / complete {history_counts['complete']}"
        )
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
