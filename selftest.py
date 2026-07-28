"""离线自检：用 HAR 里的真实响应回放整条链路，不发任何网络请求。

验证四件事：
  1. vesselinfo 解析（unvessel 拆分、截港时间补年份）
  2. 明细 upsert 入库
  3. 幂等性 —— 同样的数据再跑一遍，必须全部判为"未变"，一行都不重写
  4. 变更检测 —— 改动一个字段后，只有该行被判为 updated，且历史记录下字段级差异
  5. CSV 导出格式（UTF-8 BOM、时间戳 ISO 化、列齐全）

用法：python selftest.py [HAR 路径]
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from config import load_config
from exporter import export_all
from store import Store, parse_portclose, parse_ship_name, split_unvessel

HAR_DEFAULT = Path(__file__).resolve().parent / "www.npedi.com.har"

_ok = 0
_fail = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _ok, _fail
    if condition:
        _ok += 1
        print(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def load_har(path: Path) -> tuple[list[dict], list[dict]]:
    """从 HAR 里取出 vesselinfo 与 integrated 的响应体。"""
    har = json.loads(path.read_text(encoding="utf-8-sig"))
    vessels: list[dict] = []
    rows: list[dict] = []
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        text = entry["response"].get("content", {}).get("text")
        if not text or "/npp/search/" not in url:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if "vesselinfo" in url and not vessels:
            vessels = payload.get("data") or []
        elif "integrated" in url:
            rows.extend((payload.get("data") or {}).get("list") or [])
    return vessels, rows


def main() -> int:
    har_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HAR_DEFAULT
    if not har_path.exists():
        print(f"找不到 HAR 文件：{har_path}")
        return 1

    print(f"读取 HAR：{har_path.name}")
    vessels, rows = load_har(har_path)
    print(f"  航次 {len(vessels)} 条，明细 {len(rows)} 行\n")

    now = datetime(2026, 7, 28, 11, 9, 19)

    print("1) 字段解析")
    check("unvessel 拆分", split_unvessel("UN9604122/071E") == "UN9604122", "UN9604122/071E → UN9604122")
    check("船名解析", parse_ship_name("EVERLOTUS/071E(01-01 00:00)") == "EVERLOTUS")
    check("截港时间补年份", parse_portclose("07-22 22:00", now) == "2026-07-22 22:00:00", "07-22 22:00 → 2026")
    # 就近原则：从 2026-07-28 看，12-30 距今年年底 155 天、距去年年底 210 天 → 取今年
    check("跨年就近取年", parse_portclose("12-30 22:00", now) == "2026-12-30 22:00:00", "7月看到12-30 → 本年")
    check("跨年就近取年(次年)", parse_portclose("01-05 08:00", datetime(2026, 12, 20)) == "2027-01-05 08:00:00",
          "12月看到01-05 → 次年")
    check("占位符按未知处理", parse_portclose("01-01 00:00", now) is None)
    check("空值按未知处理", parse_portclose("", now) is None)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cfg = load_config()
        cfg.db_path = tmpdir / "test.sqlite"
        cfg.export_dir = tmpdir / "export"

        with Store(cfg.db_path) as store:
            print("\n2) 航次目录入库")
            run1 = store.start_run("backfill", None, "20260728110919")
            vstats = store.upsert_voyages(vessels, now)
            check("航次按键去重后入库", store.counts()["voyages"] == vstats["unique"],
                  f"接口 {vstats['seen']} 条 → 去重后 {vstats['unique']} 个航次（重复 {vstats['duplicates']} 条）")
            check("首次入库全部算新增", vstats["new"] == vstats["unique"])
            vstats2 = store.upsert_voyages(vessels, now)
            check("重复导入不产生新航次", vstats2["new"] == 0)

            # 重复组里空的截港时间不能把真实值冲掉
            row = store.conn.execute(
                "SELECT portclose_at FROM voyages WHERE unvessel='UN9293246' AND voyage='147W'"
            ).fetchone()
            check("空截港时间不覆盖已知值", row is not None and row["portclose_at"] is not None,
                  f"UN9293246/147W → {row['portclose_at'] if row else '缺失'}")

            print("\n3) 明细首次入库")
            s1 = store.upsert_containers(rows, run1)
            uniq = len({r["id"] for r in rows if r.get("id") is not None})
            check("按 id 去重后全部入库", store.counts()["containers"] == uniq,
                  f"{s1['new']} 新增 / 读取 {s1['seen']} 行 / 去重后 {uniq}")
            check("首轮没有变更记录", s1["updated"] == 0 and store.counts()["history"] == 0)
            store.finish_run(run1, "ok", requests_made=0, stats=s1)

            print("\n4) 幂等性（增量的核心）")
            run2 = store.start_run("incremental", "20260728000000", "20260728120000")
            s2 = store.upsert_containers(rows, run2)
            check("同样的数据全部判为未变", s2["unchanged"] == uniq and s2["new"] == 0 and s2["updated"] == 0,
                  f"未变 {s2['unchanged']} / 新增 {s2['new']} / 变更 {s2['updated']}")
            check("未变的行不写历史", store.counts()["history"] == 0)
            check("未变的行不进增量 CSV", store.run_change_count(run2) == 0)
            store.finish_run(run2, "ok", requests_made=0, stats=s2)

            print("\n5) 变更检测")
            run3 = store.start_run("incremental", "20260728120000", "20260728180000")
            mutated = json.loads(json.dumps(rows))
            target = next(r for r in mutated if r.get("id") is not None)
            target_id = target["id"]
            target["passFlag"] = "N"
            target["remark"] = "自检改动"
            new_row = dict(target)
            new_row["id"] = 999999999
            new_row["containerno"] = "TEST0000001"
            mutated.append(new_row)

            s3 = store.upsert_containers(mutated, run3)
            check("只有被改的行判为变更", s3["updated"] == 1, f"updated={s3['updated']}")
            check("新出现的行判为新增", s3["new"] == 1, f"new={s3['new']}")
            check("其余行仍判为未变", s3["unchanged"] == uniq - 1, f"unchanged={s3['unchanged']}")

            hist = store.conn.execute(
                "SELECT changed_fields FROM container_history WHERE id=? ORDER BY hist_id DESC LIMIT 1",
                (target_id,),
            ).fetchone()
            diff = json.loads(hist["changed_fields"]) if hist else {}
            check("历史记录了字段级差异", set(diff) == {"passFlag", "remark"}, f"{list(diff)}")
            check("差异含新旧值", diff.get("passFlag", [None, None])[1] == "N", f"{diff.get('passFlag')}")
            check("增量 CSV 只含 2 行", store.run_change_count(run3) == 2)
            store.finish_run(run3, "ok", requests_made=0, stats=s3)

            print("\n6) CSV 导出")
            written = export_all(cfg, store, run3)
            snapshot = cfg.export_dir / "containers_all.csv"
            check("生成全量快照", snapshot.exists())
            check("生成航次目录", (cfg.export_dir / "voyages.csv").exists())
            change_files = list(cfg.export_dir.glob("changes_*.csv"))
            check("生成本轮增量文件", len(change_files) == 1, str([f.name for f in change_files]))

            raw = snapshot.read_bytes()
            check("UTF-8 BOM 开头（Excel 中文不乱码）", raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            header = text.splitlines()[0].split(",")
            check("表头含关键列", {"id", "containerno", "billno", "compareTime", "unvessel"} <= set(header),
                  f"{len(header)} 列")
            check("时间戳已 ISO 化", "2026-07-27 10:32:01" in text or "2026-07-2" in text)
            check("14 位纯数字时间戳未残留", ",20260727103201," not in text)
            data_lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
            check("快照行数与库一致", len(data_lines) == store.counts()["containers"],
                  f"CSV {len(data_lines)} 行 / 库 {store.counts()['containers']} 行")

            chg_text = change_files[0].read_text(encoding="utf-8-sig")
            check("增量文件首列是 change_type", chg_text.splitlines()[0].startswith("change_type"))
            check("增量文件含 new 与 updated", "new," in chg_text and "updated," in chg_text)

            print("\n7) 运行记录与水位线")
            check("水位线取最近一次成功运行", store.last_successful_watermark() == "20260728180000",
                  store.last_successful_watermark() or "")
            check("运行记录齐全", len(store.recent_runs(10)) == 3)

    print(f"\n{'=' * 46}\n通过 {_ok} 项，失败 {_fail} 项")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
