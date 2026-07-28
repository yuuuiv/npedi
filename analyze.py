"""离线数据画像：只读库，不联网，用来验证这批数据的结构与业务规则。

背景 —— 这个站点不是物流跟踪系统，是**出口舱单核放比对系统**。
三个 Remark 列各自只有 3 个固定取值，都在拿"货代舱单"比对三个外部数据源：
    sldFlag    ↔ sldRemark     电子口岸三联单
    matouFlag  ↔ matouRemark   码头运抵报告
    customFlag ↔ customRemark  海关放行信息
齐了才发送、才放行、才装船。

用法：
    python analyze.py              # 跑除 keys 外的全部（keys 较慢）
    python analyze.py lifecycle    # 只跑生命周期横截面
    python analyze.py changes      # 只看增量抓到的变更
    python analyze.py all          # 全部，含函数依赖检验
    python analyze.py lifecycle --csv export/lifecycle.csv
    python analyze.py changes --csv export/changes_detail.csv   # 字段级变更明细

各节：
    columns     每列的非空率 / 基数 / 常见值，标出全空的死列
    keys        唯一键与函数依赖（全表扫多次，慢）
    lifecycle   按 compareTime 分桶的生命周期横截面 ★核心
    rules       三条不变量的校验与反例计数
    terminals   同箱同票同航次跨两个码头的成对分析
    changes     增量抓到的变更：哪些字段在动、状态位怎么流转 ★核心

前五节看的是"现在长什么样"，changes 看的是"这段时间发生了什么"——
后者才是每天跑三轮增量换来的东西，单看快照是看不到的。
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timedelta

from config import load_config

# 业务列以外的簿记列，画像时跳过
BOOKKEEPING = ("extra_json", "row_hash", "first_seen_at", "updated_at")

# 唯一键：实测这五列组合在全表唯一，少任何一列都不唯一
BUSINESS_KEY = ("containerno", "billno", "unvessel", "voyage", "matou")

# 三组「校验位 ↔ 原因说明」。flag=Y 时 remark 必为空
CHECKS = (
    ("sldFlag", "sldRemark", "电子口岸三联单"),
    ("matouFlag", "matouRemark", "码头运抵报告"),
    ("customFlag", "customRemark", "海关放行信息"),
)

# 生命周期分桶：相对「库里最新的 compareTime」往前推的天数上界
BUCKETS = ((1, "最近 1 天"), (4, "1~4 天前"), (8, "4~8 天前"),
           (15, "8~15 天前"), (30, "15~30 天前"), (180, "1~6 个月前"),
           (None, "半年以上"))


def ne(col: str) -> str:
    """该列「有值」的 SQL 条件（空串和 NULL 都算没值）。"""
    return f"TRIM(COALESCE(\"{col}\", '')) <> ''"


def pct(part: int, whole: int) -> str:
    return "  —  " if not whole else f"{100 * part / whole:5.1f}%"


class Report:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.total = self.n("SELECT COUNT(*) FROM containers")
        self.cols = [r[1] for r in conn.execute("PRAGMA table_info(containers)")]
        self.biz = [c for c in self.cols if c not in BOOKKEEPING]

    def n(self, sql: str, *args) -> int:
        return self.conn.execute(sql, args).fetchone()[0]

    def count(self, where: str = "") -> int:
        return self.n("SELECT COUNT(*) FROM containers" + (f" WHERE {where}" if where else ""))

    # ---------------------------------------------------------------- columns

    def columns(self) -> None:
        head(f"列画像（共 {len(self.biz)} 个业务列，{self.total} 行）")
        dead, thin = [], []
        print(f"  {'列名':<16}{'非空率':>9}{'不同值':>10}   最常见的值")
        print("  " + "-" * 86)
        for col in self.biz:
            nn = self.count(ne(col))
            if nn == 0:
                dead.append(col)
                continue
            nd = self.n(f'SELECT COUNT(DISTINCT "{col}") FROM containers WHERE {ne(col)}')
            top = [str(r[0])[:26] for r in self.conn.execute(
                f'SELECT "{col}" FROM containers WHERE {ne(col)} '
                f"GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 2")]
            if nn < self.total * 0.01:
                thin.append(col)
            print(f"  {col:<16}{pct(nn, self.total):>9}{nd:>10}   {' / '.join(top)}")
        print(f"\n  全空的死列（{len(dead)} 个，可以完全忽略）: {', '.join(dead) or '无'}")
        print(f"  近乎恒定的列（非空率 <1%）: {', '.join(thin) or '无'}")

    # ------------------------------------------------------------------- keys

    def keys(self) -> None:
        head("唯一键与函数依赖")
        cat = lambda cs: " || '|' || ".join(f'"{c}"' for c in cs)
        print("  候选键的去重后基数：")
        for i in range(1, len(BUSINESS_KEY) + 1):
            part = BUSINESS_KEY[:i]
            nd = self.n(f"SELECT COUNT(DISTINCT {cat(part)}) FROM containers")
            mark = "← 唯一" if nd == self.total else f"重复 {self.total - nd}"
            print(f"    {'+'.join(part):<46}{nd:>9}   {mark}")

        print("\n  函数依赖（给定左键，右列是否被唯一确定）：")
        for label, cols in (("containerno", ("containerno",)),
                            ("billno", ("billno",)),
                            ("unvessel+voyage", ("unvessel", "voyage")),
                            ("billno+unvessel+voyage", ("billno", "unvessel", "voyage"))):
            ngrp = self.n(f"SELECT COUNT(*) FROM (SELECT {cat(cols)} g FROM containers GROUP BY g)")
            determined = []
            for col in self.biz:
                if col in cols or col == "id":
                    continue
                if self.count_groups_violating(cat(cols), col) == 0:
                    determined.append(col)

            print(f"    {label}（{ngrp} 组）→ {', '.join(determined) or '（只决定自己）'}")

    def count_groups_violating(self, key_expr: str, col: str) -> int:
        return self.n(
            f"SELECT COUNT(*) FROM (SELECT {key_expr} g FROM containers "
            f"GROUP BY g HAVING COUNT(DISTINCT COALESCE(\"{col}\", '')) > 1)")

    # -------------------------------------------------------------- lifecycle

    def lifecycle(self, csv_path: str | None = None) -> None:
        """按 compareTime 分桶看各阶段的完成度。

        关键在方向性：箱子走完流程就不再被比对，compareTime 停在最后一次。
        所以 compareTime 越新，阶段反而越早 —— 它是「这行还在动」的指示器。
        """
        newest = self.conn.execute(
            f"SELECT MAX(compareTime) FROM containers WHERE {ne('compareTime')}").fetchone()[0]
        if not newest:
            print("  库里没有 compareTime，跳过")
            return
        anchor = datetime.strptime(newest, "%Y%m%d%H%M%S")
        head(f"生命周期横截面（锚点 = 库内最新 compareTime {anchor:%Y-%m-%d %H:%M:%S}）")

        rows, prev = [], None
        for days, label in BUCKETS:
            lo = "00000000000000" if days is None else (anchor - timedelta(days=days)).strftime("%Y%m%d%H%M%S")
            hi = "99999999999999" if prev is None else prev
            prev = lo
            w = f"compareTime >= '{lo}' AND compareTime < '{hi}'"
            n = self.count(w)
            if not n:
                continue
            rows.append({
                "bucket": label, "rows": n,
                "sld_Y": self.count(f"{w} AND sldFlag='Y'"),
                "matou_Y": self.count(f"{w} AND matouFlag='Y'"),
                "custom_Y": self.count(f"{w} AND customFlag='Y'"),
                "pass_Y": self.count(f"{w} AND passFlag='Y'"),
                "loaded": self.count(f"{w} AND {ne('loadtime')}"),
                "not_received": self.count(f"{w} AND NOT {ne('receivetime')}"),
            })

        cols = [("sld_Y", "三联单"), ("matou_Y", "已运抵"), ("custom_Y", "海关放"),
                ("pass_Y", "已放行"), ("loaded", "已装船"), ("not_received", "未运抵")]
        print(f"  {'compareTime 桶':<14}{'行数':>9} | " + " ".join(f"{t:>7}" for _, t in cols))
        print("  " + "-" * 78)
        for r in rows:
            print(f"  {r['bucket']:<14}{r['rows']:>9} | "
                  + " ".join(pct(r[k], r["rows"]) + " " for k, _ in cols))
        print("\n  读法：越靠上 = compareTime 越新 = 阶段越早。'未运抵' 高的那一桶是当前活跃工作面。")

        if csv_path:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            print(f"  已写出 {csv_path}")

    # ------------------------------------------------------------------ rules

    def rules(self) -> None:
        head("不变量校验（括号内是反例数，0 表示规则成立）")

        a = self.count(f"{ne('passFlag')} AND NOT {ne('receivetime')}")
        b = self.count(f"NOT {ne('passFlag')} AND {ne('receivetime')}")
        print(f"\n  1. passFlag 有值 ⟺ receivetime 有值   （{a} / {b}）")
        print("     没运抵就不会有放行判定，这两列同生共死。")

        print("\n  2. flag=Y 时对应 Remark 必为空")
        for flag, remark, what in CHECKS:
            bad = self.count(f"\"{flag}\"='Y' AND {ne(remark)}")
            missing = self.count(f"\"{flag}\"='N' AND NOT {ne(remark)}")
            print(f"     {flag:<11}({what})  Y 却有说明 {bad}｜N 却无说明 {missing}")
        print("     Remark 是 flag=N 的原因，不是独立信息；各只有 3 种固定措辞。")

        allY = " AND ".join(f"\"{f}\"='Y'" for f, _, _ in CHECKS)
        sent = self.count(ne("sendTime"))
        sent_ok = self.count(f"{ne('sendTime')} AND {allY}")
        print(f"\n  3. 有 sendTime ⇒ 三个校验位全 Y   （{sent - sent_ok}）")
        print(f"     {sent} 行已发送，其中 {sent_ok} 行三证齐全。")

        head("反直觉的地方 —— 放行不等于三证齐全")
        pass_no = self.count(f"NOT ({allY}) AND passFlag='Y'")
        no_pass = self.count(f"{allY} AND passFlag='N'")
        print(f"  三证不全却已放行: {pass_no}")
        for v, n in self.conn.execute(
                f"SELECT COALESCE(NULLIF(TRIM(remark),''),'(空)'), COUNT(*) FROM containers "
                f"WHERE NOT ({allY}) AND passFlag='Y' GROUP BY 1 ORDER BY 2 DESC LIMIT 3"):
            print(f"      remark={v[:24]:<26}{n:>8}")
        print(f"  三证齐全却未放行: {no_pass}")
        print("  → 放行是独立判定，不是三个校验位的与。发送才是。")

    # -------------------------------------------------------------- terminals

    def terminals(self) -> None:
        head("跨码头成对分析（同箱 同票 同航次，落在两个码头）")
        grp = " || '|' || ".join(f'"{c}"' for c in BUSINESS_KEY[:4])
        pairs = self.conn.execute(f"""
            SELECT {grp} g, COUNT(*) n FROM containers GROUP BY g HAVING n = 2""").fetchall()
        if not pairs:
            print("  没有跨码头的成对记录")
            return
        print(f"  共 {len(pairs)} 组\n")
        print("  各列在两行之间不一致的比例（越高＝越属于码头级状态）：")
        for col in ("matou", "matouFlag", "passFlag", "receivetime", "compareFlag",
                    "loadtime", "sendFlag", "customFlag", "rktime", "sldFlag", "status"):
            diff = self.n(f"""
                SELECT COUNT(*) FROM (SELECT {grp} g FROM containers
                GROUP BY g HAVING COUNT(*) = 2
                   AND COUNT(DISTINCT COALESCE("{col}", '')) > 1)""")
            print(f"    {col:<14}{pct(diff, len(pairs))}")
        one_side = self.n(f"""
            SELECT COUNT(*) FROM (SELECT {grp} g FROM containers GROUP BY g
            HAVING COUNT(*) = 2 AND SUM(CASE WHEN {ne('receivetime')} THEN 1 ELSE 0 END) = 1)""")
        print(f"\n  恰好一边有 receivetime 的组: {one_side} / {len(pairs)}  {pct(one_side, len(pairs))}")
        print("  → 不是两套并行状态，而是同一个箱子登记在两个码头、只有一个真正收到了它。")
        print("  → 按箱聚合前必须先滤掉空壳行，否则同一个箱子会被算两次。")


    # ---------------------------------------------------------------- changes

    def changes(self, csv_path: str | None = None) -> None:
        """增量抓到的变更画像。数据来自 container_history 的字段级差异。

        快照回答"现在什么样"，这里回答"这段时间动了什么"——
        是每天三轮增量真正换来的东西。
        """
        nhist = self.n("SELECT COUNT(*) FROM container_history")
        if nhist == 0:
            head("变更画像")
            print("  还没有变更历史。跑过 backfill 之后，等增量轮攒下变更再看这一节。")
            return

        head(f"变更画像（{nhist} 条字段级变更记录）")

        print("  每轮的收成：")
        print(f"    {'轮次':<6}{'类型':<13}{'开始时间':<21}"
              + "".join(f"{t:>9}" for t in ("新增", "变更", "空转", "未变", "请求")))
        print("    " + "-" * 88)
        # rows_touched 是后加的列，老库里可能还没有（analyze 只读，不做迁移）
        run_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sync_runs)")}
        touched = "COALESCE(rows_touched,0)" if "rows_touched" in run_cols else "0"
        for r in self.conn.execute(
                f"SELECT run_id, kind, started_at, rows_new, rows_updated, {touched}, "
                "rows_unchanged, requests_made "
                "FROM sync_runs WHERE status='ok' ORDER BY run_id DESC LIMIT 15"):
            print(f"    #{r[0]:<5}{r[1]:<13}{r[2]:<21}"
                  + "".join(f"{v:>9}" for v in (r[3], r[4], r[5], r[6], r[7])))

        # 字段变更频率。json_each 把 {"字段": [旧,新]} 摊平成一行一个字段。
        print("\n  哪些字段在动：")
        rows = self.conn.execute(
            "SELECT je.key, COUNT(*) n FROM container_history h, json_each(h.changed_fields) je "
            "GROUP BY je.key ORDER BY n DESC").fetchall()
        width = max(len(k) for k, _ in rows)
        for key, n in rows:
            print(f"    {key:<{width + 2}}{n:>8}  {pct(n, nhist)}")

        # 只有 TOUCH_ONLY 字段变的那些：平台重新比对了一遍，业务内容没动
        noise = self.n("""
            SELECT COUNT(*) FROM (SELECT h.hist_id FROM container_history h,
            json_each(h.changed_fields) je GROUP BY h.hist_id
            HAVING COUNT(*) = 1 AND MAX(je.key) = 'compareTime')""")
        if noise:
            print(f"\n  其中 {noise} 条（{pct(noise, nhist).strip()}）只有 compareTime 变了 —— "
                  "平台重新比对了一遍，业务内容没动。")
            print("  这些是历史遗留：现在的 upsert 已经把它们归为「空转」，不再记进变更历史。")

        print("\n  关键状态位的流转（旧值 → 新值）：")
        flags = ("passFlag", "sendFlag", "sldFlag", "matouFlag", "customFlag",
                 "compareFlag", "stayFlag")
        for flag in flags:
            moves = self.conn.execute(f"""
                SELECT COALESCE(NULLIF(json_extract(changed_fields,'$."{flag}"[0]'),''),'(空)'),
                       COALESCE(NULLIF(json_extract(changed_fields,'$."{flag}"[1]'),''),'(空)'),
                       COUNT(*) n
                FROM container_history
                WHERE json_extract(changed_fields,'$."{flag}"') IS NOT NULL
                GROUP BY 1,2 ORDER BY n DESC""").fetchall()
            if not moves:
                continue
            total = sum(n for _, _, n in moves)
            detail = "，".join(f"{a}→{b} {n}" for a, b, n in moves)
            print(f"    {flag:<13}{total:>7}   {detail}")
        print("    正向 N→Y 是流程在推进；反向 Y→N 少但要紧 —— 那是已经确认的状态又被推翻。")

        print("\n  变更最集中的航次：")
        for un, voy, n, boxes in self.conn.execute("""
                SELECT c.unvessel, c.voyage, COUNT(*) n, COUNT(DISTINCT c.id) boxes
                FROM container_history h JOIN containers c ON c.id = h.id
                GROUP BY c.unvessel, c.voyage ORDER BY n DESC LIMIT 10"""):
            print(f"    {un}/{voy:<8}{n:>8} 次变更，涉及 {boxes} 个箱子")

        churn = self.conn.execute("""
            SELECT cnt, COUNT(*) FROM (SELECT id, COUNT(*) cnt FROM container_history GROUP BY id)
            GROUP BY cnt ORDER BY cnt""").fetchall()
        print("\n  每个箱子被改过几次：" + "，".join(f"{c} 次 {n} 个" for c, n in churn[:8]))

        if csv_path:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["changed_at", "run_id", "id", "containerno", "unvessel",
                            "voyage", "field", "old", "new"])
                w.writerows(self.conn.execute("""
                    SELECT h.changed_at, h.run_id, h.id, c.containerno, c.unvessel, c.voyage,
                           je.key, json_extract(je.value,'$[0]'), json_extract(je.value,'$[1]')
                    FROM container_history h
                    JOIN containers c ON c.id = h.id, json_each(h.changed_fields) je
                    ORDER BY h.hist_id"""))
            print(f"\n  已写出字段级变更明细 {csv_path}")


def head(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    csv_path = None
    if "--csv" in sys.argv:
        csv_path = sys.argv[sys.argv.index("--csv") + 1]

    cfg = load_config()
    if not cfg.db_path.exists():
        print(f"找不到库文件 {cfg.db_path}，请先跑 python sync.py backfill")
        return 1

    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    rep = Report(conn)
    if rep.total == 0:
        print("库是空的，先跑 python sync.py backfill")
        return 1

    want = args[0] if args else "default"
    sections = {"columns": rep.columns, "rules": rep.rules,
                "terminals": rep.terminals, "keys": rep.keys}
    if want in sections:
        sections[want]()
    elif want == "lifecycle":
        rep.lifecycle(csv_path)
    elif want == "changes":
        rep.changes(csv_path)
    else:
        rep.columns()
        rep.lifecycle(csv_path if want != "all" else None)
        rep.rules()
        rep.terminals()
        rep.changes()
        if want == "all":
            rep.keys()
        else:
            print("\n（函数依赖检验较慢，需要时跑 python analyze.py keys）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
