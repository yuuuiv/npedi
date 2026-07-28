"""SQLite 存储层（对应 ARCHITECTURE.md §3）。

增量的核心在 upsert_containers()：先比 row_hash，相同则完全跳过（不写库、不记历史），
不同才更新并记录字段级差异 —— 这就是"增量更新而不是重新入库"的落点。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("npedi.store")

# 明细字段，取自接口返回的原始键名。顺序即建表 / CSV 列顺序。
CONTAINER_FIELDS: tuple[str, ...] = (
    "id",
    "containerno",
    "billno",
    "unvessel",
    "voyage",
    "envessel",
    "linecode",
    "sizetype",
    "matou",
    "agent",
    "agentcode",
    "status",
    "remark",
    "reason",
    "disport",
    "yddisport",
    "contact",
    "compareTime",
    "compareFlag",
    "sendTime",
    "sendFlag",
    "sendEnable",
    "receivetime",
    "rktime",
    "loadtime",
    "passFlag",
    "customFlag",
    "sldFlag",
    "matouFlag",
    "ifcsumFlag",
    "sldRemark",
    "matouRemark",
    "customRemark",
    "portclosetime",
    "type",
    "companyName",
    "stayFlag",
    "flag",
)

# 参与变更检测的字段：除主键外的全部业务字段 + 未知新增字段（extra_json）
HASH_FIELDS: tuple[str, ...] = tuple(f for f in CONTAINER_FIELDS if f != "id")

# 只有这些字段变化时，算"系统又比对了一遍"，不算业务变更。
# compareTime 是平台每次比对都会重刷的时间戳：实测一轮增量的 58106 条变更里，
# 50592 条（87%）除了它什么都没动。把这些记成 updated，会让真正的 7514 条变更被淹掉，
# 也会让 changes_*.csv 膨胀近十倍。
TOUCH_ONLY_FIELDS: frozenset[str] = frozenset({"compareTime"})

# 形如 20260727103201 的时间戳字段，CSV 导出时可转成 ISO
TIMESTAMP_FIELDS: tuple[str, ...] = (
    "compareTime", "sendTime", "receivetime", "rktime", "loadtime",
)

_COLS = ", ".join(f'"{f}"' for f in CONTAINER_FIELDS)
_PLACEHOLDERS = ", ".join("?" for _ in CONTAINER_FIELDS)

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 航次目录
CREATE TABLE IF NOT EXISTS voyages (
    unvessel      TEXT NOT NULL,          -- UN9604122（不含 /航次 后缀）
    voyage        TEXT NOT NULL,
    envessel      TEXT,                   -- 船名
    portclose_at  TEXT,                   -- 补全年份后的截港时间；未知为 NULL
    portclose_raw TEXT,                   -- 接口原文，如 "07-22 22:00"
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,          -- 最近一次出现在 vesselinfo 中
    last_change_at TEXT,                  -- 该航次下最近一次有明细发生变化的时间
    backfilled    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (unvessel, voyage)
);

-- 集装箱明细（当前态）
CREATE TABLE IF NOT EXISTS containers (
    {chr(10).join(f'    "{f}" {"INTEGER PRIMARY KEY" if f == "id" else "TEXT"},' for f in CONTAINER_FIELDS).strip()}
    extra_json    TEXT NOT NULL DEFAULT '',  -- 接口日后新增的未知字段，原样留存
    row_hash      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_containers_voyage ON containers(unvessel, voyage);
CREATE INDEX IF NOT EXISTS idx_containers_updated ON containers(updated_at);
CREATE INDEX IF NOT EXISTS idx_containers_compare ON containers("compareTime");

-- 字段级变更历史
CREATE TABLE IF NOT EXISTS container_history (
    hist_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    id         INTEGER NOT NULL,
    run_id     INTEGER,
    changed_at TEXT NOT NULL,
    changed_fields TEXT NOT NULL          -- JSON: {{"字段": [旧, 新]}}
);
CREATE INDEX IF NOT EXISTS idx_history_id ON container_history(id);

-- 本轮新增/变更的行，供 CSV 增量导出
CREATE TABLE IF NOT EXISTS run_changes (
    run_id      INTEGER NOT NULL,
    id          INTEGER NOT NULL,
    change_type TEXT NOT NULL,            -- new | updated
    PRIMARY KEY (run_id, id)
);

-- 每轮运行记录 + 水位线
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- backfill | incremental | reconcile | replay | probe
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    watermark_from TEXT,
    watermark_to   TEXT,
    requests_made  INTEGER DEFAULT 0,
    rows_seen      INTEGER DEFAULT 0,
    rows_new       INTEGER DEFAULT 0,
    rows_updated   INTEGER DEFAULT 0,
    rows_touched   INTEGER DEFAULT 0,     -- 只有 compareTime 变的空转行
    rows_unchanged INTEGER DEFAULT 0,
    status      TEXT NOT NULL,            -- running | ok | auth_expired | failed
    error       TEXT,
    log_file    TEXT                      -- 本轮的单轮日志文件名（logs/runs/ 下）
);

-- 键值元数据（probe 结论、策略选择等）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm(value: Any) -> str:
    """把接口值规范化成字符串，None/空白统一成 ''，保证 hash 稳定。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def parse_portclose(raw: str | None, now: datetime) -> str | None:
    """"07-22 22:00" → "2026-07-22 22:00:00"。

    接口只给月-日 时:分，按"离当前时间最近"的年份补全（跨年安全）。
    "01-01 00:00" 是平台的空值占位符（855 条样本中仅 4 条，且对应航次均无真实截港时间），
    统一按未知处理，原文仍保留在 portclose_raw 里。
    """
    text = _norm(raw)
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    month, day, hour, minute = (int(g) for g in m.groups())
    if (month, day, hour, minute) == (1, 1, 0, 0):
        return None
    best: datetime | None = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            cand = datetime(year, month, day, hour, minute)
        except ValueError:      # 2-29 之类
            continue
        if best is None or abs(cand - now) < abs(best - now):
            best = cand
    return best.strftime("%Y-%m-%d %H:%M:%S") if best else None


def split_unvessel(raw: str | None) -> str:
    """"UN9604122/071E" → "UN9604122"（查询明细时只要斜杠前半段）。"""
    return _norm(raw).split("/", 1)[0]


def parse_ship_name(envessel: str | None) -> str:
    """"EVERLOTUS/071E(01-01 00:00)" → "EVERLOTUS"。"""
    text = _norm(envessel)
    return text.split("/", 1)[0].split("(", 1)[0].strip()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给已存在的库补上后加的列。

        CREATE TABLE IF NOT EXISTS 不会给旧表加列，而重建 containers 表在几十万行的库上
        代价太大，所以后加的列一律走 ALTER TABLE ADD COLUMN（SQLite 里是常数时间）。
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(sync_runs)")}
        for name, ddl in (("log_file", "TEXT"), ("rows_touched", "INTEGER DEFAULT 0")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE sync_runs ADD COLUMN {name} {ddl}")
                log.info("已为 sync_runs 补上 %s 列", name)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- meta

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )
        self.conn.commit()

    # ------------------------------------------------------------- runs

    def start_run(self, kind: str, wm_from: str | None, wm_to: str | None,
                  log_file: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs(kind, started_at, watermark_from, watermark_to, status, log_file) "
            "VALUES(?,?,?,?, 'running', ?)",
            (kind, now_iso(), wm_from, wm_to, log_file or None),
        )
        self.conn.commit()
        run_id = cur.lastrowid
        if run_id is None:  # INSERT 之后必然有 rowid，兜住只是为了不把 None 传下去
            raise RuntimeError("写入 sync_runs 后没拿到 run_id，本轮无法记账")
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        requests_made: int = 0,
        stats: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        stats = stats or {}
        self.conn.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, requests_made=?, "
            "rows_seen=?, rows_new=?, rows_updated=?, rows_touched=?, rows_unchanged=?, "
            "error=? WHERE run_id=?",
            (
                now_iso(), status, requests_made,
                stats.get("seen", 0), stats.get("new", 0),
                stats.get("updated", 0), stats.get("touched", 0), stats.get("unchanged", 0),
                error, run_id,
            ),
        )
        self.conn.commit()

    def last_successful_watermark(self, kinds: Sequence[str] = ("backfill", "incremental", "reconcile")) -> str | None:
        """上一次成功运行的 watermark_to；增量窗口的起点由它决定。"""
        marks = ",".join("?" for _ in kinds)
        row = self.conn.execute(
            f"SELECT watermark_to FROM sync_runs WHERE status='ok' AND kind IN ({marks}) "
            "AND watermark_to IS NOT NULL ORDER BY run_id DESC LIMIT 1",
            tuple(kinds),
        ).fetchone()
        return row["watermark_to"] if row else None

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ------------------------------------------------------------- voyages

    def upsert_voyages(self, items: Iterable[dict], now: datetime) -> dict[str, int]:
        """把 vesselinfo 的返回写入航次目录。

        接口会对同一 (unvessel, voyage) 返回多条（实测 855 条里有 23 组重复），
        差别通常是其中一条的截港时间为空、或是改期后的新旧两个时间。
        因此先在批内按键去重：优先取有截港时间的那条，多个都有则取最晚的，
        避免空值把真实截港时间冲掉。
        """
        stats = {"seen": 0, "unique": 0, "new": 0, "duplicates": 0}
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        best: dict[tuple[str, str], tuple[dict, str | None]] = {}
        for item in items:
            unvessel = split_unvessel(item.get("unvessel"))
            voyage = _norm(item.get("voyage"))
            if not unvessel or not voyage:
                continue
            stats["seen"] += 1
            key = (unvessel, voyage)
            parsed = parse_portclose(item.get("portclosetime"), now)
            if key not in best:
                best[key] = (item, parsed)
                continue
            stats["duplicates"] += 1
            _, kept = best[key]
            if kept is None or (parsed is not None and parsed > kept):
                best[key] = (item, parsed)

        stats["unique"] = len(best)
        for (unvessel, voyage), (item, parsed_close) in best.items():
            raw_close = _norm(item.get("portclosetime"))
            exists = self.conn.execute(
                "SELECT 1 FROM voyages WHERE unvessel=? AND voyage=?", (unvessel, voyage)
            ).fetchone()
            self.conn.execute(
                """
                INSERT INTO voyages(unvessel, voyage, envessel, portclose_at, portclose_raw,
                                    first_seen_at, last_seen_at, backfilled)
                VALUES(?,?,?,?,?,?,?,0)
                ON CONFLICT(unvessel, voyage) DO UPDATE SET
                    envessel=excluded.envessel,
                    -- 已知的截港时间不允许被空值覆盖
                    portclose_at=COALESCE(excluded.portclose_at, voyages.portclose_at),
                    portclose_raw=CASE WHEN excluded.portclose_at IS NULL AND voyages.portclose_at IS NOT NULL
                                       THEN voyages.portclose_raw ELSE excluded.portclose_raw END,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    unvessel, voyage, parse_ship_name(item.get("envessel")),
                    parsed_close, raw_close, ts, ts,
                ),
            )
            if not exists:
                stats["new"] += 1
        self.conn.commit()
        return stats

    def voyages_pending_backfill(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM voyages WHERE backfilled=0 ORDER BY last_seen_at DESC, unvessel"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def mark_backfilled(self, unvessel: str, voyage: str) -> None:
        self.conn.execute(
            "UPDATE voyages SET backfilled=1 WHERE unvessel=? AND voyage=?", (unvessel, voyage)
        )
        self.conn.commit()

    def active_voyages(
        self, now: datetime, *, past_days: int, future_days: int,
        unknown_close_days: int, limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """降级路径用的"活跃航次"（ARCHITECTURE.md §4.2 步骤 4）。

        活跃 = 截港时间落在 [now-past, now+future] 内；
        或者无截港时间、但最近仍有数据变动（或尚未回填过）。
        """
        lo = (now - timedelta(days=past_days)).strftime("%Y-%m-%d %H:%M:%S")
        hi = (now + timedelta(days=future_days)).strftime("%Y-%m-%d %H:%M:%S")
        stale = (now - timedelta(days=unknown_close_days)).strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            SELECT * FROM voyages
            WHERE (portclose_at IS NOT NULL AND portclose_at BETWEEN ? AND ?)
               OR (portclose_at IS NULL AND (last_change_at IS NULL OR last_change_at >= ?))
               OR backfilled = 0
            ORDER BY COALESCE(portclose_at, last_seen_at) DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, (lo, hi, stale)).fetchall()

    def touch_voyage_change(self, unvessel: str, voyage: str, when: str) -> None:
        self.conn.execute(
            "UPDATE voyages SET last_change_at=? WHERE unvessel=? AND voyage=?",
            (when, unvessel, voyage),
        )

    # ------------------------------------------------------------- containers

    def upsert_containers(self, rows: Iterable[dict], run_id: int) -> dict[str, int]:
        """按 id upsert，返回 seen/new/updated/touched/unchanged/skipped 计数。

        三条路径，区别在于"这次变化值不值得记一笔"：
          unchanged  hash 一样 —— 完全不碰库
          touched    只有 TOUCH_ONLY_FIELDS 变了 —— 写新值保持数据新鲜，但不记历史、不进变更 CSV
          updated    有业务字段变了 —— 更新 + 记字段级历史 + 进变更 CSV
        """
        stats = {"seen": 0, "new": 0, "updated": 0, "touched": 0, "unchanged": 0, "skipped": 0}
        ts = now_iso()
        touched_voyages: set[tuple[str, str]] = set()
        cur = self.conn.cursor()

        for row in rows:
            stats["seen"] += 1
            rid = row.get("id")
            if rid is None:
                stats["skipped"] += 1
                continue

            values = {f: _norm(row.get(f)) for f in HASH_FIELDS}
            extra = {k: v for k, v in row.items() if k not in CONTAINER_FIELDS}
            extra_json = json.dumps(extra, sort_keys=True, ensure_ascii=False) if extra else ""
            row_hash = hashlib.sha256(
                json.dumps({**values, "__extra__": extra_json}, sort_keys=True, ensure_ascii=False)
                .encode("utf-8")
            ).hexdigest()

            old = cur.execute("SELECT * FROM containers WHERE id=?", (int(rid),)).fetchone()

            if old is None:
                cur.execute(
                    f"INSERT INTO containers({_COLS}, extra_json, row_hash, first_seen_at, updated_at) "
                    f"VALUES({_PLACEHOLDERS}, ?, ?, ?, ?)",
                    (
                        *(int(rid) if f == "id" else values[f] for f in CONTAINER_FIELDS),
                        extra_json, row_hash, ts, ts,
                    ),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO run_changes(run_id, id, change_type) VALUES(?,?, 'new')",
                    (run_id, int(rid)),
                )
                stats["new"] += 1
            elif old["row_hash"] == row_hash:
                stats["unchanged"] += 1
                continue        # 不写库、不记历史 —— 增量的关键
            else:
                diff = {
                    f: [old[f], values[f]]
                    for f in HASH_FIELDS
                    if _norm(old[f]) != values[f]
                }
                if extra_json != (old["extra_json"] or ""):
                    diff["__extra__"] = [old["extra_json"], extra_json]
                assignments = ", ".join(f'"{f}"=?' for f in HASH_FIELDS)
                if set(diff) - TOUCH_ONLY_FIELDS:
                    cur.execute(
                        f"UPDATE containers SET {assignments}, extra_json=?, row_hash=?, updated_at=? "
                        "WHERE id=?",
                        (*(values[f] for f in HASH_FIELDS), extra_json, row_hash, ts, int(rid)),
                    )
                    cur.execute(
                        "INSERT INTO container_history(id, run_id, changed_at, changed_fields) "
                        "VALUES(?,?,?,?)",
                        (int(rid), run_id, ts, json.dumps(diff, ensure_ascii=False)),
                    )
                    cur.execute(
                        "INSERT INTO run_changes(run_id, id, change_type) VALUES(?,?, 'updated') "
                        "ON CONFLICT(run_id, id) DO NOTHING",
                        (run_id, int(rid)),
                    )
                    stats["updated"] += 1
                else:
                    # 系统重新比对了一遍，业务内容没动。写入新值让 compareTime 保持最新，
                    # 但不动 updated_at —— 它表示"最后一次真变更"，不该被空转推着走。
                    cur.execute(
                        f"UPDATE containers SET {assignments}, extra_json=?, row_hash=? WHERE id=?",
                        (*(values[f] for f in HASH_FIELDS), extra_json, row_hash, int(rid)),
                    )
                    stats["touched"] += 1

            voyage_key = (values["unvessel"], values["voyage"])
            if all(voyage_key):
                touched_voyages.add(voyage_key)

        for unvessel, voyage in touched_voyages:
            self.touch_voyage_change(unvessel, voyage, ts)
        self.conn.commit()
        return stats

    # ------------------------------------------------------------- 查询

    def counts(self) -> dict[str, int]:
        q = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "voyages": q("SELECT COUNT(*) FROM voyages"),
            "voyages_backfilled": q("SELECT COUNT(*) FROM voyages WHERE backfilled=1"),
            "containers": q("SELECT COUNT(*) FROM containers"),
            "history": q("SELECT COUNT(*) FROM container_history"),
        }

    def iter_containers(self) -> Iterable[sqlite3.Row]:
        yield from self.conn.execute(
            f"SELECT {_COLS}, first_seen_at, updated_at FROM containers ORDER BY unvessel, voyage, id"
        )

    def iter_run_changes(self, run_id: int) -> Iterable[sqlite3.Row]:
        cols = ", ".join('b."' + f + '"' for f in CONTAINER_FIELDS)
        yield from self.conn.execute(
            "SELECT c.change_type, " + cols + ", b.first_seen_at, b.updated_at "
            "FROM run_changes c JOIN containers b ON b.id = c.id "
            "WHERE c.run_id=? ORDER BY c.change_type, b.unvessel, b.voyage, b.id",
            (run_id,),
        )

    def iter_voyages(self) -> Iterable[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT unvessel, voyage, envessel, portclose_at, portclose_raw, "
            "first_seen_at, last_seen_at, last_change_at, backfilled "
            "FROM voyages ORDER BY COALESCE(portclose_at, '9999') DESC, unvessel"
        )

    def run_change_count(self, run_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM run_changes WHERE run_id=?", (run_id,)
        ).fetchone()[0])
