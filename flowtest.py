"""用假客户端回放 HAR，把 sync.py 的 probe / backfill / incremental 三条主流程跑通。"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJ = Path(r"C:\Users\yuuuiv\Downloads\climb")
sys.path.insert(0, str(PROJ))

import client as client_mod
import sync
from config import load_config
from store import Store

har = json.loads((PROJ / "www.npedi.com.har").read_text(encoding="utf-8-sig"))
VESSELS, ROWS = [], []
for e in har["log"]["entries"]:
    url = e["request"]["url"]
    text = e["response"].get("content", {}).get("text")
    if not text or "/npp/search/" not in url:
        continue
    p = json.loads(text)
    if "vesselinfo" in url and not VESSELS:
        VESSELS = p.get("data") or []
    elif "integrated" in url:
        ROWS.extend((p.get("data") or {}).get("list") or [])

# HAR 里没有"未比对"（compareTime 为空）的行，手工造两行来验证兜底路径
for i, src in enumerate(ROWS[:2]):
    ghost = dict(src)
    ghost["id"] = 900000000 + i
    ghost["containerno"] = f"GHOST{i:07d}"
    ghost["compareTime"] = ""
    ghost["compareFlag"] = "N"
    ROWS.append(ghost)


class FakeClient:
    """按参数过滤 HAR 中的明细，模拟服务端行为。"""
    blank_query_allowed = True
    window_excludes_empty_compare = True   # 模拟"未比对行会被窗口滤掉"

    def __init__(self, cfg):
        self.cfg = cfg
        self.request_count = 0
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass

    def get_info(self):
        self.request_count += 1
        return {"user": {"loginName": "test"}}

    def vesselinfo(self):
        self.request_count += 1
        return VESSELS

    def _filter(self, unvessel, voyage, compare_time, compare_flag):
        if not unvessel and not self.blank_query_allowed:
            raise client_mod.ApiError("留空查询被拒绝")
        out = []
        for r in ROWS:
            if unvessel and str(r.get("unvessel")) != unvessel:
                continue
            if voyage and str(r.get("voyage")) != voyage:
                continue
            if compare_flag and str(r.get("compareFlag") or "") != compare_flag:
                continue
            if compare_time:
                ct = str(r.get("compareTime") or "").strip()
                if not ct:
                    if self.window_excludes_empty_compare:
                        continue
                else:
                    lo, hi = compare_time.split(",")
                    if not (lo <= ct <= hi):
                        continue
            out.append(r)
        return out

    def integrated_page(self, page, *, unvessel="", voyage="", compare_time="",
                        compare_flag="", page_size=None):
        self.request_count += 1
        self.calls.append((page, unvessel, voyage, compare_time, compare_flag))
        size = page_size or self.cfg.page_size
        rows = self._filter(unvessel, voyage, compare_time, compare_flag)
        start = (page - 1) * size
        return {"pageNum": page, "pageSize": size, "total": len(rows), "totalPages": 0,
                "list": rows[start:start + size]}

    def iter_integrated(self, *, unvessel="", voyage="", compare_time="",
                        compare_flag="", start_page=1):
        page = max(1, start_page)
        fetched = (page - 1) * self.cfg.page_size
        while True:
            data = self.integrated_page(page, unvessel=unvessel, voyage=voyage,
                                        compare_time=compare_time, compare_flag=compare_flag)
            rows = data["list"]
            total = data["total"]
            yield page, total, rows
            fetched += len(rows)
            if not rows or fetched >= total:
                return
            page += 1


ok = fail = 0
def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1; print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


class Args:
    def __init__(self, **kw):
        self.resume = False
        self.window = None
        self.__dict__.update(kw)


def make_cfg(tmp: Path):
    cfg = load_config()
    cfg.db_path = tmp / "flow.sqlite"
    cfg.export_dir = tmp / "export"
    cfg.log_dir = tmp / "logs"
    cfg.alert_file = tmp / "ALERT"
    cfg.lock_file = tmp / ".lock"
    cfg.token = "fake-token"
    cfg.request_delay = (0.0, 0.0)
    return cfg


def scenario(name, blank_allowed, excludes_empty):
    print(f"\n=== 场景：{name} ===")
    FakeClient.blank_query_allowed = blank_allowed
    FakeClient.window_excludes_empty_compare = excludes_empty
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        cfg = make_cfg(tmpdir)
        sync.setup_logging(cfg)
        logging_root = __import__("logging").getLogger("npedi")
        logging_root.setLevel(__import__("logging").WARNING)  # 静音，只看断言

        client_mod.NpediClient = FakeClient
        sync.NpediClient = FakeClient

        # probe
        rc = sync.cmd_probe(cfg, Args())
        check("probe 返回 0", rc == 0)
        with Store(cfg.db_path) as s:
            strategy = s.meta_get(sync.META_STRATEGY)
            excluded = s.meta_get(sync.META_EMPTY_COMPARE_EXCLUDED)
        check("策略判定正确", strategy == ("all_in_one" if blank_allowed else "per_voyage"), strategy)
        check("兜底判定正确", excluded == ("1" if excludes_empty else "0"), f"excluded={excluded}")

        # backfill
        rc = sync.run_backfill(cfg, Args())
        check("backfill 返回 0", rc == 0)
        with Store(cfg.db_path) as s:
            c = s.counts()
            check("明细已入库", c["containers"] > 0, f"{c['containers']} 行")
            check("航次已入库", c["voyages"] > 0, f"{c['voyages']} 个")
            check("航次已标记回填完成", c["voyages_backfilled"] == c["voyages"])
            check("水位线已建立", s.last_successful_watermark() is not None)
            before = c["containers"]

        check("生成 containers_all.csv", (cfg.export_dir / "containers_all.csv").exists())
        check("生成 voyages.csv", (cfg.export_dir / "voyages.csv").exists())

        # incremental：数据没变 → 应全部 unchanged，不产生 changes 文件
        n_before = len(list(cfg.export_dir.glob("changes_*.csv")))
        rc = sync.run_incremental(cfg, Args(), "incremental")
        check("incremental 返回 0", rc == 0)
        with Store(cfg.db_path) as s:
            run = s.recent_runs(1)[0]
            check("增量轮无新增无变更", run["rows_new"] == 0 and run["rows_updated"] == 0,
                  f"new={run['rows_new']} upd={run['rows_updated']} unchanged={run['rows_unchanged']}")
            check("行数未膨胀", s.counts()["containers"] == before,
                  f"{s.counts()['containers']} vs {before}")
            check("水位线已推进", s.last_successful_watermark() == run["watermark_to"])
        n_after = len(list(cfg.export_dir.glob("changes_*.csv")))
        check("无变化不生成增量 CSV", n_after == n_before, f"{n_before} → {n_after}")

        # replay：手动窗口，不推进水位线
        with Store(cfg.db_path) as s:
            wm_before = s.last_successful_watermark()
        rc = sync.run_incremental(cfg, Args(window="20260701000000,20260729000000"), "replay")
        check("replay 返回 0", rc == 0)
        with Store(cfg.db_path) as s:
            check("replay 不推进水位线", s.last_successful_watermark() == wm_before)
            check("replay 不重复入库", s.counts()["containers"] == before)

        # reconcile
        rc = sync.run_reconcile(cfg, Args())
        check("reconcile 返回 0", rc == 0)

        # 情形 A：compareTime 刷新到当前时刻的变更 —— 增量必须抓到
        fresh = datetime.now().strftime("%Y%m%d%H%M%S")
        target_id = ROWS[0]["id"]
        snapshot0 = dict(ROWS[0])
        ROWS[0] = dict(ROWS[0], passFlag="N", remark="流程测试改动", compareTime=fresh)
        extra = dict(ROWS[1], id=888000001, containerno="NEWBOX0001", compareTime=fresh)
        ROWS.append(extra)
        try:
            check("有变更时 incremental 返回 0", sync.run_incremental(cfg, Args(), "incremental") == 0)
            with Store(cfg.db_path) as s:
                run = s.recent_runs(1)[0]
                check("A: 识别出 1 变更 + 1 新增",
                      run["rows_updated"] == 1 and run["rows_new"] == 1,
                      f"new={run['rows_new']} upd={run['rows_updated']}")
                check("A: 库里恰好多 1 行", s.counts()["containers"] == before + 1)
                hist = s.conn.execute(
                    "SELECT changed_fields FROM container_history WHERE id=?", (target_id,)
                ).fetchone()
                check("A: 变更写入历史", hist is not None and "passFlag" in hist["changed_fields"])
            chg = sorted(cfg.export_dir.glob("changes_*.csv"))[-1]
            body = chg.read_text(encoding="utf-8-sig").splitlines()
            check("A: 增量 CSV 恰好 2 行数据", len(body) - 1 == 2, f"{len(body)-1} 行")

            # 情形 B：改一行「compareTime 仍是几天前」的数据 —— 按时间窗口的增量必然漏掉
            # （架构 §5 风险 1），应由 reconcile 兜住。这里两步都验一遍。
            old_idx = 2
            snapshot_old = dict(ROWS[old_idx])
            old_id = ROWS[old_idx]["id"]
            ROWS[old_idx] = dict(ROWS[old_idx], sendFlag="N", remark="compareTime未变的变更")
            check("B: incremental 返回 0", sync.run_incremental(cfg, Args(), "incremental") == 0)
            with Store(cfg.db_path) as s:
                run = s.recent_runs(1)[0]
                check("B: 增量按预期漏掉该变更（窗口过滤的固有局限）",
                      run["rows_updated"] == 0, f"upd={run['rows_updated']}")
                row = s.conn.execute("SELECT sendFlag FROM containers WHERE id=?", (old_id,)).fetchone()
                check("B: 此时库中仍是旧值", row["sendFlag"] == snapshot_old.get("sendFlag"),
                      f"{row['sendFlag']}")
            check("B: reconcile 返回 0", sync.run_reconcile(cfg, Args()) == 0)
            with Store(cfg.db_path) as s:
                run = s.recent_runs(1)[0]
                check("B: reconcile 抓到该变更", run["rows_updated"] == 1, f"upd={run['rows_updated']}")
                row = s.conn.execute("SELECT sendFlag FROM containers WHERE id=?", (old_id,)).fetchone()
                check("B: 库中字段已更新", row["sendFlag"] == "N", str(row["sendFlag"]))
            ROWS[old_idx] = snapshot_old
        finally:
            ROWS[0] = snapshot0
            ROWS.pop()

        # status / export
        check("status 返回 0", sync.cmd_status(cfg, Args()) == 0)
        check("export 返回 0", sync.cmd_export(cfg, Args()) == 0)


def scenario_auth_expired():
    """token 失效必须：立即停止、写告警文件、退出码 2；恢复后自动清除告警。"""
    print("\n=== 场景：token 失效与恢复 ===")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        cfg = make_cfg(tmpdir)
        sync.setup_logging(cfg)
        __import__("logging").getLogger("npedi").setLevel(__import__("logging").CRITICAL)

        class ExpiredClient(FakeClient):
            def get_info(self):
                raise client_mod.AuthExpired("HTTP 401")

        # 先用正常 token 建立水位线，再模拟 token 失效
        sync.NpediClient = FakeClient
        FakeClient.blank_query_allowed = True
        FakeClient.window_excludes_empty_compare = False
        sync.run_backfill(cfg, Args())

        sync.NpediClient = ExpiredClient
        rc = sync.dispatch(cfg, "incremental", Args())
        check("退出码为 2（token 失效）", rc == sync.EXIT_AUTH, f"rc={rc}")
        check("写出告警文件", cfg.alert_file.exists())
        if cfg.alert_file.exists():
            text = cfg.alert_file.read_text(encoding="utf-8")
            check("告警文件含取 token 步骤", "Web-Token" in text and "F12" in text)
        with Store(cfg.db_path) as s:
            run = s.recent_runs(1)[0]
            check("该轮记为 auth_expired", run["status"] == "auth_expired", run["status"])
            check("失效轮不推进水位线", s.last_successful_watermark() != run["watermark_to"])

        # 换回可用的 token → 告警自动清除
        sync.NpediClient = FakeClient
        sync.run_incremental(cfg, Args(), "incremental")
        check("恢复后自动清除告警文件", not cfg.alert_file.exists())

        # 锁：同一时刻第二个实例应拿不到锁
        with sync.FileLock(cfg.lock_file):
            try:
                with sync.FileLock(cfg.lock_file):
                    check("重复运行被锁挡住", False, "第二个实例竟然拿到了锁")
            except RuntimeError as exc:
                check("重复运行被锁挡住", "已有另一个实例" in str(exc))


scenario("假设1成立 + 窗口滤掉未比对行", blank_allowed=True, excludes_empty=True)
scenario("假设1不成立（按航次循环）+ 窗口不滤", blank_allowed=False, excludes_empty=False)
scenario_auth_expired()

print(f"\n{'='*46}\n通过 {ok} 项，失败 {fail} 项")
sys.exit(0 if fail == 0 else 1)
