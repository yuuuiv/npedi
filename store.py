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

-- WebToken lifetime observations.  Only a one-way SHA-256 fingerprint is
-- stored; the token, Authorization header, and response bodies never enter
-- SQLite.  Observations are deliberately batch-level to avoid write pressure.
CREATE TABLE IF NOT EXISTS auth_token_observation (
    fingerprint          TEXT PRIMARY KEY,
    first_seen_at         TEXT NOT NULL,
    first_success_at      TEXT,
    last_success_at       TEXT,
    last_auth_failure_at  TEXT,
    successful_preflights INTEGER NOT NULL DEFAULT 0,
    successful_runs       INTEGER NOT NULL DEFAULT 0,
    successful_requests   INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_token_event (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key      TEXT NOT NULL UNIQUE,
    fingerprint    TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    event_type     TEXT NOT NULL
                   CHECK(event_type IN (
                       'probe_ok','run_ok','auth_failed','refresh_detected'
                   )),
    run_id         INTEGER,
    run_kind       TEXT,
    endpoint       TEXT,
    http_status    INTEGER,
    api_code       INTEGER,
    request_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_auth_token_event_time
    ON auth_token_event(observed_at, event_type);
"""

# --------------------------------------------------------------------------
# 进出门（CODECO 报文），对应 ARCHITECTURE-GATE.md §3
# --------------------------------------------------------------------------

# 入库字段。接口每行有 66 个键，实测仅 17 个有值，其余稀疏字段原样进 raw_json。
GATE_FIELDS: tuple[str, ...] = (
    "id",
    "type",              # GATE_IN | GATE_OUT
    "vesselcode",        # UN9475648
    "voyage",
    "vessel",
    "direct",
    "senderCode",
    "ctnNo",
    "blNo",
    "ctnOperatorCode",
    "ctnSizeType",
    "ctnStatus",
    "containerType",
    "ctnGrossWeight",
    "sealNo",
    "msgReceiveTime",
    "inGateTime",
    "outGateTime",
    "dlPortCode",
    "signTrade",
)

# 这两列取自请求参数：接口返回的行里 type 与 vesselCode 恒为 null，
# UN 号只存在于查询条件中，不落列的话入库后就再也认不出这行属于哪条船。
GATE_PARAM_FIELDS: frozenset[str] = frozenset({"type", "vesselcode"})
GATE_ROW_FIELDS: tuple[str, ...] = tuple(f for f in GATE_FIELDS if f not in GATE_PARAM_FIELDS)
# SQLite 标识符大小写不敏感（vesselcode 与接口的 vesselCode 是同一列），
# 判断"这个键是不是已知字段"时统一转小写比较。
GATE_KNOWN_KEYS: frozenset[str] = frozenset(f.lower() for f in GATE_FIELDS)
GATE_COMPARE_FIELDS: tuple[str, ...] = tuple(f for f in GATE_FIELDS if f != "id")

GATE_DIRECTIONS: tuple[str, ...] = ("GATE_IN", "GATE_OUT")
# 每个方向对应 gate_voyages 上的两列（断点标记 / 总条数）
GATE_DONE_COLUMNS: dict[str, tuple[str, str]] = {
    "GATE_IN": ("gatein_done_at", "gatein_total"),
    "GATE_OUT": ("gateout_done_at", "gateout_total"),
}

# msgReceiveTime 是 14 位，两个闸口时间是 12 位（yyyyMMddHHmm，没有秒）
GATE_TIMESTAMP_FIELDS: tuple[str, ...] = ("msgReceiveTime", "inGateTime", "outGateTime")

_GATE_COLS = ", ".join(f'"{f}"' for f in GATE_FIELDS)
_GATE_PLACEHOLDERS = ", ".join("?" for _ in GATE_FIELDS)

GATE_SCHEMA = f"""
-- 进出门航次目录（独立于 voyages：voyages 是 npp 在册目录，只有 848 个；
-- 这份目录实测 13954 个船×航次对，数据可回溯到 2022 年初）
CREATE TABLE IF NOT EXISTS gate_voyages (
    vesselcode   TEXT NOT NULL,
    voyage       TEXT NOT NULL,
    vesselename  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_event_at TEXT,                     -- 该航次已见过的最大 msgReceiveTime（14 位原文）
    gatein_done_at  TEXT, gatein_total  INTEGER,   -- 回填断点：NULL=该方向尚未完成
    gateout_done_at TEXT, gateout_total INTEGER,
    idle_rounds  INTEGER NOT NULL DEFAULT 0,-- 连续多少轮增量没有新报文
    inactive     INTEGER NOT NULL DEFAULT 0,-- 1=已退出活跃集合，增量不再查询
    PRIMARY KEY (vesselcode, voyage)
);
CREATE INDEX IF NOT EXISTS idx_gate_voyages_pending
    ON gate_voyages(gatein_done_at, gateout_done_at);

-- vesselList 只是一份目录快照，并不枚举完整历史。这个独立队列从船期事实表
-- 发现目录外的船×航次，先点查、命中后才写入 gate_voyages，避免数十万个
-- 空候选污染正式目录。命令当前严格单线程运行。
CREATE TABLE IF NOT EXISTS gate_history_candidate (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    vessel_in_catalog INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','hit','empty','complete')),
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    probed_at        TEXT,
    gatein_total     INTEGER,
    gateout_total    INTEGER,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage)
);
CREATE INDEX IF NOT EXISTS idx_gate_history_candidate_pending
    ON gate_history_candidate(status, last_eta DESC, vesselcode, voyage);

-- 保留候选在哪个月份出现过的证据。不能仅靠 first_eta/last_eta 推断月份：
-- 同一个船×航次可能只在 1 月和 3 月快照里出现，并不代表它在 2 月也出现。
CREATE TABLE IF NOT EXISTS gate_history_candidate_month (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    eta_month        TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage, eta_month)
);
CREATE INDEX IF NOT EXISTS idx_gate_history_candidate_month_scope
    ON gate_history_candidate_month(eta_month, vesselcode, voyage);

CREATE TABLE IF NOT EXISTS gate_history_seed_scope (
    eta_start          TEXT NOT NULL,
    eta_end_exclusive  TEXT NOT NULL,
    known_vessels_only INTEGER NOT NULL,
    seeded_at          TEXT NOT NULL,
    PRIMARY KEY (eta_start, eta_end_exclusive, known_vessels_only)
);

-- Clearly invalid plan keys are retained for audit but are never sent to the
-- remote endpoint.  Querying a placeholder vessel code is more dangerous than
-- merely wasting a request: a server-side ignored filter could return rows
-- which the importer would otherwise attribute to that placeholder code.
CREATE TABLE IF NOT EXISTS gate_history_rejected_pair (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    reason           TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage)
);
CREATE INDEX IF NOT EXISTS idx_gate_history_rejected_reason
    ON gate_history_rejected_pair(reason, last_eta DESC);

-- 进出门报文流水。id 里含接收时间戳，报文天然只增不改（append-only），
-- 所以这里没有 row_hash / 变更历史那一套 —— 做了是纯开销。
CREATE TABLE IF NOT EXISTS gate_events (
    {chr(10).join(f'    "{f}" TEXT{" PRIMARY KEY" if f == "id" else ""},' for f in GATE_FIELDS).strip()}
    raw_json   TEXT NOT NULL DEFAULT '',    -- 其余 40+ 个稀疏字段，将来要用不必重爬
    run_id     INTEGER,                     -- 首次入库的轮次，供增量 CSV 导出
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_events_voyage ON gate_events(vesselcode, voyage);
CREATE INDEX IF NOT EXISTS idx_gate_events_ctn    ON gate_events("ctnNo", "blNo");
CREATE INDEX IF NOT EXISTS idx_gate_events_run    ON gate_events(run_id);

-- 与 npp 数据的关联视图（补数入口，ARCHITECTURE-GATE.md §5）。
-- join 条件里带 receivetime 非空：npp 的同一个箱子会在两个码头各登记一行，
-- 其中约 87% 只有一边真收到货，不滤掉空壳行的话缺口统计会虚高（见 ANALYSIS.md 第五节）。
CREATE VIEW IF NOT EXISTS v_gate_vs_npp AS
SELECT g.vesselcode, g.voyage, g.vessel, g."type", g."senderCode",
       g."ctnNo", g."blNo", g."inGateTime", g."outGateTime", g."msgReceiveTime",
       c.id AS npp_id, c.matou AS npp_matou, c."passFlag", c."sendFlag", c.remark
FROM gate_events g
LEFT JOIN containers c
       ON c.containerno = g."ctnNo"
      AND c.billno      = g."blNo"
      AND c.unvessel    = g.vesselcode
      AND c.voyage      = g.voyage
      AND TRIM(COALESCE(c.receivetime, '')) <> '';
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


def gate_history_invalid_reason(vesselcode: Any, voyage: Any) -> str:
    """Return a stable quarantine reason for an unsafe plan-derived key.

    The filter is intentionally narrow.  Non-UN/FC/CN namespaces remain
    eligible because real CODECO data has already been observed under other
    namespaces.  We only quarantine known placeholders and voyage values that
    cannot be an exact API key (whitespace/non-ASCII/manual notes).
    """
    code = _norm(vesselcode)
    trip = _norm(voyage)
    placeholder_chars = frozenset("0?/.· -_")
    if not code:
        return "empty_vesselcode"
    if code.upper() == "FC0000000" or all(ch in placeholder_chars for ch in code):
        return "placeholder_vesselcode"
    if not trip:
        return "empty_voyage"
    if all(ch in placeholder_chars for ch in trip):
        return "placeholder_voyage"
    if any(ch.isspace() for ch in trip):
        return "voyage_contains_whitespace"
    if not trip.isascii():
        return "voyage_non_ascii"
    return ""


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
        # timeout 从默认的 5s 提到 60s：npp 增量与进出门回填现在可能同时在跑，
        # WAL 下同一时刻只允许一个写者，撞上了就等对方提交（每页一提交，都是毫秒级）。
        self.conn = sqlite3.connect(self.path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function(
            "gate_history_invalid_reason",
            2,
            gate_history_invalid_reason,
            deterministic=True,
        )
        self.conn.executescript(SCHEMA)
        self.conn.executescript(GATE_SCHEMA)
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

    # ------------------------------------------------------------- token audit

    def observe_token_seen(self, fingerprint: str, observed_at: str | None = None) -> None:
        """Register a token fingerprint without claiming it authenticated."""
        ts = observed_at or now_iso()
        self.conn.execute(
            """
            INSERT INTO auth_token_observation(
                fingerprint,first_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                first_seen_at=MIN(
                    auth_token_observation.first_seen_at,
                    excluded.first_seen_at
                ),
                updated_at=excluded.updated_at
            """,
            (fingerprint, ts, ts, ts),
        )
        self.conn.commit()

    def observe_token_success(
        self,
        fingerprint: str,
        *,
        event_type: str,
        run_id: int | None,
        run_kind: str,
        request_count: int = 0,
        endpoint: str = "",
        observed_at: str | None = None,
    ) -> None:
        if event_type not in {"probe_ok", "run_ok", "refresh_detected"}:
            raise ValueError(f"unsupported token success event: {event_type}")
        ts = observed_at or now_iso()
        self.conn.execute(
            """
            INSERT INTO auth_token_observation(
                fingerprint,first_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(fingerprint) DO NOTHING
            """,
            (fingerprint, ts, ts, ts),
        )
        suffix = f"run:{run_id}" if run_id is not None else f"time:{ts}"
        event_key = f"{fingerprint}:{event_type}:{suffix}"
        inserted = self.conn.execute(
            """
            INSERT OR IGNORE INTO auth_token_event(
                event_key,fingerprint,observed_at,event_type,run_id,run_kind,
                endpoint,request_count
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                event_key, fingerprint, ts, event_type, run_id,
                run_kind or None, endpoint or None, max(int(request_count), 0),
            ),
        ).rowcount
        if inserted:
            self.conn.execute(
                """
                UPDATE auth_token_observation
                SET first_success_at=COALESCE(first_success_at,?),
                    last_success_at=?,
                    successful_preflights=successful_preflights+?,
                    successful_runs=successful_runs+?,
                    successful_requests=successful_requests+?,
                    updated_at=?
                WHERE fingerprint=?
                """,
                (
                    ts, ts, int(event_type == "probe_ok"),
                    int(event_type == "run_ok"),
                    max(int(request_count), 0) if event_type == "run_ok" else 0,
                    ts, fingerprint,
                ),
            )
        self.conn.commit()

    def observe_token_auth_failure(
        self,
        fingerprint: str,
        *,
        run_id: int | None,
        run_kind: str,
        endpoint: str = "",
        http_status: int | None = None,
        api_code: int | None = None,
        observed_at: str | None = None,
    ) -> None:
        ts = observed_at or now_iso()
        self.conn.execute(
            """
            INSERT INTO auth_token_observation(
                fingerprint,first_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(fingerprint) DO NOTHING
            """,
            (fingerprint, ts, ts, ts),
        )
        suffix = f"run:{run_id}" if run_id is not None else f"time:{ts}"
        event_key = f"{fingerprint}:auth_failed:{suffix}"
        inserted = self.conn.execute(
            """
            INSERT OR IGNORE INTO auth_token_event(
                event_key,fingerprint,observed_at,event_type,run_id,run_kind,
                endpoint,http_status,api_code
            ) VALUES(?,?,?,'auth_failed',?,?,?,?,?)
            """,
            (
                event_key, fingerprint, ts, run_id, run_kind or None,
                endpoint or None, http_status, api_code,
            ),
        ).rowcount
        if inserted:
            self.conn.execute(
                """
                UPDATE auth_token_observation
                SET last_auth_failure_at=?,updated_at=? WHERE fingerprint=?
                """,
                (ts, ts, fingerprint),
            )
        self.conn.commit()

    # ------------------------------------------------------------- runs

    def start_run(self, kind: str, wm_from: str | None, wm_to: str | None,
                  log_file: str = "") -> int:
        # 上一轮如果是被 kill / 断电 / 崩溃结束的，finish_run 没机会跑，
        # 会永远挂在 running 上。开新一轮时顺手收尾，免得 status 里越积越多。
        stale = self.conn.execute(
            "UPDATE sync_runs SET status='interrupted', finished_at=?, "
            "error='进程未正常结束（被中断或崩溃），断点已保留' "
            "WHERE status='running' AND kind=?", (now_iso(), kind),
        ).rowcount
        if stale:
            log.warning("发现 %d 条未收尾的运行记录，已标记为 interrupted", stale)

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

    # ------------------------------------------------------- 进出门：航次目录

    def upsert_gate_voyages(
        self, items: Iterable[dict], now: datetime
    ) -> tuple[dict[str, int], list[tuple[str, str]]]:
        """写入进出门航次目录，返回 (计数, 本次新出现的航次键)。

        目录里有重复（实测 14337 条 → 13954 个唯一对），且没有任何日期字段，
        所以"哪些是新航次"只能靠与库里已有键求差集得到。
        """
        stats = {"seen": 0, "unique": 0, "new": 0, "duplicates": 0}
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        best: dict[tuple[str, str], dict] = {}
        for item in items:
            vessel_code = _norm(item.get("vesselcode"))
            voyage = _norm(item.get("voyage"))
            if not vessel_code or not voyage:
                continue
            stats["seen"] += 1
            key = (vessel_code, voyage)
            if key in best:
                stats["duplicates"] += 1
                continue
            best[key] = item
        stats["unique"] = len(best)

        existing = {
            (r["vesselcode"], r["voyage"])
            for r in self.conn.execute("SELECT vesselcode, voyage FROM gate_voyages")
        }
        new_keys = [key for key in best if key not in existing]
        stats["new"] = len(new_keys)

        self.conn.executemany(
            """
            INSERT INTO gate_voyages(vesselcode, voyage, vesselename, first_seen_at, last_seen_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(vesselcode, voyage) DO UPDATE SET
                vesselename=COALESCE(NULLIF(excluded.vesselename, ''), gate_voyages.vesselename),
                last_seen_at=excluded.last_seen_at
            """,
            [
                (vessel_code, voyage, _norm(item.get("vesselename")), ts, ts)
                for (vessel_code, voyage), item in best.items()
            ],
        )
        self.conn.commit()
        return stats, new_keys

    def seed_gate_history_candidates(
        self,
        eta_start: str,
        eta_end_exclusive: str,
        *,
        known_vessels_only: bool = True,
        refresh: bool = False,
    ) -> dict[str, int]:
        """Discover vessel-plan pairs omitted from the CODECO catalog snapshot.

        ``eta_end_exclusive`` keeps the SQL boundary unambiguous.  By default
        only vessels already accepted by CODECO's own vesselList are included;
        this avoids spending requests on vessel-code namespaces the endpoint
        may not understand.  Empty probes remain in this separate audit queue
        and are never inserted into ``gate_voyages``.
        """
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fact_vessel_plan_snapshot'"
        ).fetchone():
            raise RuntimeError(
                "fact_vessel_plan_snapshot 不存在；请先完成船期历史采集与规范化"
            )

        scope_key = (eta_start, eta_end_exclusive, int(known_vessels_only))
        if not refresh and self.conn.execute(
            "SELECT 1 FROM gate_history_seed_scope "
            "WHERE eta_start=? AND eta_end_exclusive=? AND known_vessels_only=?",
            scope_key,
        ).fetchone():
            return {
                "inserted": 0,
                "rejected": int(self.conn.execute(
                    """
                    SELECT COUNT(*) FROM gate_history_rejected_pair AS r
                    WHERE r.last_eta>=? AND r.first_eta<?
                    """,
                    (eta_start, eta_end_exclusive),
                ).fetchone()[0]),
                "pending": self.gate_history_candidate_count_in_scope(
                    eta_start, eta_end_exclusive, statuses=("pending", "hit")
                ),
                "reused": 1,
            }

        known_filter = """
          AND EXISTS (
              SELECT 1 FROM gate_voyages AS vessel_match
              WHERE vessel_match.vesselcode=TRIM(p.vessel_code)
          )
        """ if known_vessels_only else ""
        valid_filter = """
          AND gate_history_invalid_reason(
                  TRIM(p.vessel_code),TRIM(p.voyage)
              )=''
        """
        before_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM gate_history_candidate"
        ).fetchone()[0])
        ts = now_iso()
        # Freeze unsafe keys in a separate audit table.  They are deliberately
        # not represented as ``empty`` because no remote query was attempted.
        self.conn.execute(
            """
            INSERT INTO gate_history_rejected_pair(
                vesselcode,voyage,reason,vesselename,first_eta,last_eta,
                source_plan_rows,discovered_at,updated_at
            )
            SELECT TRIM(p.vessel_code),TRIM(p.voyage),
                   gate_history_invalid_reason(
                       TRIM(p.vessel_code),TRIM(p.voyage)
                   ),
                   MAX(COALESCE(p.vessel_en_name,'')),
                   MIN(substr(p.eta,1,10)),MAX(substr(p.eta,1,10)),COUNT(*),?,?
            FROM fact_vessel_plan_snapshot AS p
            WHERE p.eta>=? AND p.eta<?
              AND p.vessel_code IS NOT NULL AND TRIM(p.vessel_code)<>''
              AND p.voyage IS NOT NULL AND TRIM(p.voyage)<>''
              AND gate_history_invalid_reason(
                      TRIM(p.vessel_code),TRIM(p.voyage)
                  )<>''
            GROUP BY TRIM(p.vessel_code),TRIM(p.voyage)
            ON CONFLICT(vesselcode,voyage) DO UPDATE SET
                reason=excluded.reason,
                vesselename=COALESCE(
                    NULLIF(excluded.vesselename,''),
                    gate_history_rejected_pair.vesselename
                ),
                first_eta=MIN(gate_history_rejected_pair.first_eta,excluded.first_eta),
                last_eta=MAX(gate_history_rejected_pair.last_eta,excluded.last_eta),
                source_plan_rows=MAX(
                    gate_history_rejected_pair.source_plan_rows,
                    excluded.source_plan_rows
                ),
                updated_at=excluded.updated_at
            """,
            (ts, ts, eta_start, eta_end_exclusive),
        )
        self.conn.execute(
            f"""
            INSERT INTO gate_history_candidate(
                vesselcode,voyage,vesselename,first_eta,last_eta,
                source_plan_rows,vessel_in_catalog,status,
                discovered_at,updated_at
            )
            SELECT TRIM(p.vessel_code),TRIM(p.voyage),
                   MAX(COALESCE(p.vessel_en_name,'')),
                   MIN(substr(p.eta,1,10)),MAX(substr(p.eta,1,10)),
                   COUNT(*),
                   EXISTS(
                       SELECT 1 FROM gate_voyages AS vessel_match
                       WHERE vessel_match.vesselcode=TRIM(p.vessel_code)
                   ),
                   'pending',?,?
            FROM fact_vessel_plan_snapshot AS p
            WHERE p.eta>=? AND p.eta<?
              AND p.vessel_code IS NOT NULL AND TRIM(p.vessel_code)<>''
              AND p.voyage IS NOT NULL AND TRIM(p.voyage)<>''
              {valid_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM gate_voyages AS exact_match
                  WHERE exact_match.vesselcode=TRIM(p.vessel_code)
                    AND exact_match.voyage=TRIM(p.voyage)
              )
              {known_filter}
            GROUP BY TRIM(p.vessel_code),TRIM(p.voyage)
            ON CONFLICT(vesselcode,voyage) DO UPDATE SET
                vesselename=COALESCE(
                    NULLIF(excluded.vesselename,''),gate_history_candidate.vesselename
                ),
                first_eta=MIN(gate_history_candidate.first_eta,excluded.first_eta),
                last_eta=MAX(gate_history_candidate.last_eta,excluded.last_eta),
                source_plan_rows=MAX(
                    gate_history_candidate.source_plan_rows,excluded.source_plan_rows
                ),
                vessel_in_catalog=MAX(
                    gate_history_candidate.vessel_in_catalog,excluded.vessel_in_catalog
                ),
                updated_at=excluded.updated_at
            """,
            (ts, ts, eta_start, eta_end_exclusive),
        )
        self.conn.execute(
            f"""
            INSERT INTO gate_history_candidate_month(
                vesselcode,voyage,eta_month,vesselename,first_eta,last_eta,
                source_plan_rows,discovered_at,updated_at
            )
            SELECT TRIM(p.vessel_code),TRIM(p.voyage),substr(p.eta,1,7),
                   MAX(COALESCE(p.vessel_en_name,'')),
                   MIN(substr(p.eta,1,10)),MAX(substr(p.eta,1,10)),COUNT(*),?,?
            FROM fact_vessel_plan_snapshot AS p
            WHERE p.eta>=? AND p.eta<?
              AND p.vessel_code IS NOT NULL AND TRIM(p.vessel_code)<>''
              AND p.voyage IS NOT NULL AND TRIM(p.voyage)<>''
              {valid_filter}
              AND EXISTS (
                  SELECT 1 FROM gate_history_candidate AS c
                  WHERE c.vesselcode=TRIM(p.vessel_code)
                    AND c.voyage=TRIM(p.voyage)
              )
              {known_filter}
            GROUP BY TRIM(p.vessel_code),TRIM(p.voyage),substr(p.eta,1,7)
            ON CONFLICT(vesselcode,voyage,eta_month) DO UPDATE SET
                vesselename=COALESCE(
                    NULLIF(excluded.vesselename,''),
                    gate_history_candidate_month.vesselename
                ),
                first_eta=excluded.first_eta,last_eta=excluded.last_eta,
                source_plan_rows=excluded.source_plan_rows,
                updated_at=excluded.updated_at
            """,
            (ts, ts, eta_start, eta_end_exclusive),
        )
        after_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM gate_history_candidate"
        ).fetchone()[0])
        inserted = after_count - before_count
        self.conn.execute(
            """
            INSERT INTO gate_history_seed_scope(
                eta_start,eta_end_exclusive,known_vessels_only,seeded_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(eta_start,eta_end_exclusive,known_vessels_only)
            DO UPDATE SET seeded_at=excluded.seeded_at
            """,
            (*scope_key, ts),
        )
        self.conn.commit()
        return {
            "inserted": int(inserted),
            "rejected": int(self.conn.execute(
                """
                SELECT COUNT(*) FROM gate_history_rejected_pair AS r
                WHERE r.last_eta>=? AND r.first_eta<?
                """,
                (eta_start, eta_end_exclusive),
            ).fetchone()[0]),
            "pending": self.gate_history_candidate_count_in_scope(
                eta_start, eta_end_exclusive, statuses=("pending", "hit")
            ),
            "reused": 0,
        }

    def gate_history_candidate_count_in_scope(
        self,
        eta_start: str,
        eta_end_exclusive: str,
        *,
        statuses: Sequence[str],
        vessel_in_catalog: bool | None = None,
    ) -> int:
        placeholders = ",".join("?" for _ in statuses)
        catalog_filter = (
            "" if vessel_in_catalog is None else " AND c.vessel_in_catalog=?"
        )
        params: tuple[Any, ...] = (*statuses, eta_start, eta_end_exclusive)
        if vessel_in_catalog is not None:
            params += (int(vessel_in_catalog),)
        return int(self.conn.execute(
            f"""
            SELECT COUNT(*) FROM gate_history_candidate AS c
            WHERE c.status IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM gate_history_rejected_pair AS rejected
                  WHERE rejected.vesselcode=c.vesselcode
                    AND rejected.voyage=c.voyage
              )
              AND EXISTS (
                  SELECT 1 FROM gate_history_candidate_month AS m
                  WHERE m.vesselcode=c.vesselcode AND m.voyage=c.voyage
                    AND m.last_eta>=? AND m.first_eta<?
              )
              {catalog_filter}
            """,
            params,
        ).fetchone()[0])

    def gate_history_candidates_pending(
        self,
        *,
        eta_start: str,
        eta_end_exclusive: str,
        limit: int | None,
        statuses: Sequence[str] = ("pending", "hit"),
        vessel_in_catalog: bool | None = None,
    ) -> list[sqlite3.Row]:
        """Return scoped history candidates with optional catalog filtering."""
        # A normal vesselList refresh may have filled a queued pair since it
        # was discovered.  Reconcile that locally instead of querying it again.
        self.conn.execute(
            """
            UPDATE gate_history_candidate AS c
            SET status='complete',updated_at=?
            WHERE c.status IN ('pending','hit')
              AND NOT EXISTS (
                  SELECT 1 FROM gate_history_rejected_pair AS rejected
                  WHERE rejected.vesselcode=c.vesselcode
                    AND rejected.voyage=c.voyage
              )
              AND EXISTS (
                SELECT 1 FROM gate_voyages AS g
                WHERE g.vesselcode=c.vesselcode AND g.voyage=c.voyage
                  AND g.gatein_done_at IS NOT NULL
                  AND g.gateout_done_at IS NOT NULL
            )
            """,
            (now_iso(),),
        )
        self.conn.commit()
        placeholders = ",".join("?" for _ in statuses)
        catalog_filter = (
            "" if vessel_in_catalog is None else " AND c.vessel_in_catalog=?"
        )
        limit_clause = "" if limit is None else " LIMIT ?"
        params: tuple[Any, ...] = (*statuses, eta_start, eta_end_exclusive)
        if vessel_in_catalog is not None:
            params += (int(vessel_in_catalog),)
        if limit is not None:
            params += (limit,)
        return self.conn.execute(
            f"""
            SELECT c.*,
                   g.gatein_done_at AS existing_gatein_done_at,
                   g.gatein_total AS existing_gatein_total,
                   g.gateout_done_at AS existing_gateout_done_at,
                   g.gateout_total AS existing_gateout_total,
                   CASE WHEN g.vesselcode IS NULL THEN 0 ELSE 1 END AS exact_gate_match
            FROM gate_history_candidate AS c
            LEFT JOIN gate_voyages AS g
              ON g.vesselcode=c.vesselcode AND g.voyage=c.voyage
            WHERE c.status IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM gate_history_rejected_pair AS rejected
                  WHERE rejected.vesselcode=c.vesselcode
                    AND rejected.voyage=c.voyage
              )
              AND EXISTS (
                  SELECT 1 FROM gate_history_candidate_month AS m
                  WHERE m.vesselcode=c.vesselcode AND m.voyage=c.voyage
                    AND m.last_eta>=? AND m.first_eta<?
              )
              {catalog_filter}
            ORDER BY CASE c.status WHEN 'hit' THEN 0 ELSE 1 END,
                     c.last_eta DESC,c.vesselcode,c.voyage
            {limit_clause}
            """,
            params,
        ).fetchall()

    def note_gate_history_probe(
        self,
        vesselcode: str,
        voyage: str,
        *,
        gatein_total: int | None,
        gateout_total: int | None,
    ) -> None:
        """Persist a completed first-page probe before any potentially long fetch."""
        ts = now_iso()
        self.conn.execute(
            """
            UPDATE gate_history_candidate
            SET status=CASE WHEN COALESCE(?,0)+COALESCE(?,0)>0
                            THEN 'hit' ELSE status END,
                attempt_count=attempt_count+1,probed_at=?,
                gatein_total=COALESCE(?,gatein_total),
                gateout_total=COALESCE(?,gateout_total),updated_at=?
            WHERE vesselcode=? AND voyage=?
            """,
            (
                gatein_total, gateout_total, ts,
                gatein_total, gateout_total, ts, vesselcode, voyage,
            ),
        )
        self.conn.commit()

    def add_gate_history_voyage(
        self, vesselcode: str, voyage: str, vesselename: str
    ) -> None:
        """Promote a proven non-empty candidate into the real gate catalog."""
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO gate_voyages(
                vesselcode,voyage,vesselename,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(vesselcode,voyage) DO UPDATE SET
                vesselename=COALESCE(
                    NULLIF(excluded.vesselename,''),gate_voyages.vesselename
                )
            """,
            (vesselcode, voyage, vesselename, ts, ts),
        )
        self.conn.commit()

    def finish_gate_history_candidate(
        self,
        vesselcode: str,
        voyage: str,
        *,
        status: str,
        gatein_total: int,
        gateout_total: int,
    ) -> None:
        if status not in {"empty", "complete"}:
            raise ValueError(f"unsupported gate history status: {status}")
        ts = now_iso()
        self.conn.execute(
            """
            UPDATE gate_history_candidate
            SET status=?,probed_at=COALESCE(probed_at,?),
                gatein_total=?,gateout_total=?,updated_at=?
            WHERE vesselcode=? AND voyage=?
            """,
            (status, ts, gatein_total, gateout_total, ts, vesselcode, voyage),
        )
        self.conn.commit()

    def gate_history_candidate_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "hit": 0, "empty": 0, "complete": 0}
        for row in self.conn.execute(
            """
            SELECT c.status,COUNT(*) FROM gate_history_candidate AS c
            WHERE NOT EXISTS (
                SELECT 1 FROM gate_history_rejected_pair AS rejected
                WHERE rejected.vesselcode=c.vesselcode
                  AND rejected.voyage=c.voyage
            )
            GROUP BY c.status
            """
        ):
            counts[str(row[0])] = int(row[1])
        counts["total"] = sum(counts.values())
        counts["rejected"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM gate_history_rejected_pair"
        ).fetchone()[0])
        return counts

    def gate_units_pending(
        self, *, limit: int | None = None, keys: Sequence[tuple[str, str]] | None = None,
    ) -> list[tuple[str, str, str, str]]:
        """待回填的 (船, 航次, 船名, 方向) 单元，按优先级排序。

        优先级 0 = 该航次同时在 npp 的 voyages 表里（当前工作面，先补齐）；
        优先级 1 = 只在进出门目录里的历史航次。同一航次的两个方向相邻，
        便于按航次成组完成。keys 非空时只取指定航次（增量轮回填新航次用）。
        """
        if keys is not None and not keys:
            return []
        wanted = set(keys) if keys is not None else None

        # keys 的过滤放在 Python 侧：SQLite 不支持 `(a,b) IN ((?,?),(?,?))`
        # 这种字面行值列表，而待回填集合最多一万多行，全取回来再筛毫无压力。
        sql = """
            SELECT g.vesselcode, g.voyage, g.vesselename,
                   g.gatein_done_at, g.gateout_done_at,
                   CASE WHEN v.unvessel IS NULL THEN 1 ELSE 0 END AS prio
            FROM gate_voyages g
            LEFT JOIN voyages v ON v.unvessel = g.vesselcode AND v.voyage = g.voyage
            WHERE (g.gatein_done_at IS NULL OR g.gateout_done_at IS NULL)
            ORDER BY prio, g.vesselcode, g.voyage
        """
        units: list[tuple[str, str, str, str]] = []
        for row in self.conn.execute(sql):
            key = (row["vesselcode"], row["voyage"])
            if wanted is not None and key not in wanted:
                continue
            for direction in GATE_DIRECTIONS:
                if row[GATE_DONE_COLUMNS[direction][0]] is None:
                    units.append((key[0], key[1], row["vesselename"] or "", direction))
            if limit and len(units) >= limit:
                break
        return units[:limit] if limit else units

    def mark_gate_done(self, vesselcode: str, voyage: str, direction: str, total: int) -> None:
        done_col, total_col = GATE_DONE_COLUMNS[direction]
        self.conn.execute(
            f"UPDATE gate_voyages SET {done_col}=?, {total_col}=? WHERE vesselcode=? AND voyage=?",
            (now_iso(), total, vesselcode, voyage),
        )
        self.conn.commit()

    def gate_active_voyages(self, now: datetime, *, active_days: int) -> list[sqlite3.Row]:
        """增量要查的活跃航次（ARCHITECTURE-GATE.md §4.2 步骤 2）。

        两类：仍在 npp 在册目录里的（约 850 个，核放工作面）；
        以及不在 npp 目录、但最近 active_days 天内还有报文的
        （驳船系列 TIELU*/HAITIE* 根本不进 npp 目录，只能靠报文时间判断死活）。
        """
        cutoff = (now - timedelta(days=active_days)).strftime("%Y%m%d%H%M%S")
        return self.conn.execute(
            """
            SELECT g.*, v.last_seen_at AS npp_last_seen
            FROM gate_voyages g
            LEFT JOIN voyages v ON v.unvessel = g.vesselcode AND v.voyage = g.voyage
            WHERE g.inactive = 0
              AND (v.unvessel IS NOT NULL
                   OR (g.last_event_at IS NOT NULL AND g.last_event_at >= ?))
            ORDER BY COALESCE(g.last_event_at, '') DESC, g.vesselcode, g.voyage
            """,
            (cutoff,),
        ).fetchall()

    def npp_catalog_watermark(self) -> str | None:
        """npp 航次目录最近一次同步的时刻。

        voyages 的行不会被删除，航次从 vesselinfo 下架后只是 last_seen_at 不再前进；
        拿它和这个水位线比，就能判断某个航次是否还在最新的在册目录里。
        """
        row = self.conn.execute("SELECT MAX(last_seen_at) FROM voyages").fetchone()
        return row[0] if row else None

    def note_gate_events_seen(
        self, vesselcode: str, voyage: str, *, newest: str, had_new: bool,
        inactive_rounds: int, retire_ok: bool,
    ) -> bool:
        """记录一轮增量对某航次的观察结果，返回该航次是否因此转为 inactive。

        有新报文 → 清零 idle_rounds；没有 → 累加，攒够 inactive_rounds 轮
        且该航次已离开 npp 在册目录（retire_ok）才退休 —— 只看轮数会把
        还在装货、只是这几轮恰好没动静的航次误退。
        """
        if had_new:
            self.conn.execute(
                "UPDATE gate_voyages SET idle_rounds=0, "
                "last_event_at=MAX(COALESCE(last_event_at, ''), ?) "
                "WHERE vesselcode=? AND voyage=?",
                (newest, vesselcode, voyage),
            )
            self.conn.commit()
            return False

        row = self.conn.execute(
            "SELECT idle_rounds FROM gate_voyages WHERE vesselcode=? AND voyage=?",
            (vesselcode, voyage),
        ).fetchone()
        rounds = int(row["idle_rounds"] if row else 0) + 1
        retire = retire_ok and rounds >= inactive_rounds
        self.conn.execute(
            "UPDATE gate_voyages SET idle_rounds=?, inactive=? WHERE vesselcode=? AND voyage=?",
            (rounds, 1 if retire else 0, vesselcode, voyage),
        )
        self.conn.commit()
        return retire

    # ------------------------------------------------------- 进出门：报文入库

    def insert_gate_events(
        self, rows: Iterable[dict], run_id: int, *,
        direction: str, vesselcode: str, voyage: str, verify_dup: bool = False,
    ) -> dict[str, int]:
        """按 id 追加入库，返回 seen/new/dup/skipped/conflict 计数。

        报文不可变，所以只有 INSERT OR IGNORE 一条路径：id 已存在即跳过。
        verify_dup=True 时额外核对重复行的内容（增量轮量小才值得），
        真出现同 id 内容不同就记 warning —— 那说明"报文不可变"这个前提破了。
        """
        stats = {"seen": 0, "new": 0, "dup": 0, "skipped": 0, "conflict": 0}
        ts = now_iso()
        cur = self.conn.cursor()

        for row in rows:
            stats["seen"] += 1
            rid = _norm(row.get("id"))
            if not rid:
                stats["skipped"] += 1
                continue

            values = {f: _norm(row.get(f)) for f in GATE_ROW_FIELDS}
            values["id"] = rid
            values["type"] = direction
            values["vesselcode"] = vesselcode
            values["voyage"] = values["voyage"] or voyage
            extra = {
                k: v for k, v in row.items()
                if k.lower() not in GATE_KNOWN_KEYS and v not in (None, "")
            }
            raw_json = json.dumps(extra, sort_keys=True, ensure_ascii=False) if extra else ""

            cur.execute(
                f"INSERT OR IGNORE INTO gate_events({_GATE_COLS}, raw_json, run_id, fetched_at) "
                f"VALUES({_GATE_PLACEHOLDERS}, ?, ?, ?)",
                (*(values[f] for f in GATE_FIELDS), raw_json, run_id, ts),
            )
            if cur.rowcount:
                stats["new"] += 1
                continue

            stats["dup"] += 1
            if verify_dup:
                old = cur.execute("SELECT * FROM gate_events WHERE id=?", (rid,)).fetchone()
                diff = [f for f in GATE_COMPARE_FIELDS if _norm(old[f]) != values[f]]
                if diff:
                    stats["conflict"] += 1
                    log.warning("报文 %s 的内容发生了变化（本应不可变）：%s", rid, ", ".join(diff))

        self.conn.commit()
        return stats

    # ------------------------------------------------------- 进出门：查询与导出

    def gate_counts(self) -> dict[str, int]:
        q = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "voyages": q(
                "SELECT COUNT(*) FROM gate_voyages AS g WHERE NOT EXISTS ("
                "SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "voyages_done": q(
                "SELECT COUNT(*) FROM gate_voyages AS g "
                "WHERE gatein_done_at IS NOT NULL AND gateout_done_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "voyages_inactive": q(
                "SELECT COUNT(*) FROM gate_voyages AS g WHERE inactive=1 "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "voyages_quarantined": q(
                "SELECT COUNT(*) FROM gate_voyages AS g WHERE EXISTS ("
                "SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "events": q(
                "SELECT COUNT(*) FROM gate_events AS e WHERE NOT EXISTS ("
                "SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)"
            ),
            "events_in": q(
                "SELECT COUNT(*) FROM gate_events AS e WHERE e.\"type\"='GATE_IN' "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)"
            ),
            "events_out": q(
                "SELECT COUNT(*) FROM gate_events AS e WHERE e.\"type\"='GATE_OUT' "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)"
            ),
            "events_quarantined": q(
                "SELECT COUNT(*) FROM gate_events AS e WHERE EXISTS ("
                "SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)"
            ),
        }

    def gate_event_time_range(self) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            'SELECT MIN(e."msgReceiveTime"), MAX(e."msgReceiveTime") FROM gate_events AS e '
            'WHERE TRIM(COALESCE(e."msgReceiveTime", \'\')) <> \'\' '
            'AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r '
            'WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)'
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def gate_run_event_count(self, run_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM gate_events AS e WHERE run_id=? "
            "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
            "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)",
            (run_id,),
        ).fetchone()[0])

    def iter_gate_events(
        self, *, active_only: bool = False, include_quarantined: bool = False,
    ) -> Iterable[sqlite3.Row]:
        sql = f'SELECT {_GATE_COLS}, e.fetched_at FROM gate_events AS e'
        conditions: list[str] = []
        if not include_quarantined:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage)"
            )
        if active_only:
            conditions.append(
                "(e.vesselcode, e.voyage) IN "
                "(SELECT vesselcode, voyage FROM gate_voyages WHERE inactive=0)"
            )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += ' ORDER BY e.vesselcode, e.voyage, e."msgReceiveTime"'
        yield from self.conn.execute(sql)

    def iter_gate_run_events(self, run_id: int) -> Iterable[sqlite3.Row]:
        yield from self.conn.execute(
            f'SELECT {_GATE_COLS}, e.fetched_at FROM gate_events AS e WHERE run_id=? '
            'AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r '
            'WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage) '
            'ORDER BY e.vesselcode, e.voyage, e."msgReceiveTime"',
            (run_id,),
        )

    def iter_gate_voyages(self) -> Iterable[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT vesselcode, voyage, vesselename, first_seen_at, last_seen_at, last_event_at, "
            "gatein_done_at, gatein_total, gateout_done_at, gateout_total, idle_rounds, inactive "
            "FROM gate_voyages AS g WHERE NOT EXISTS ("
            "SELECT 1 FROM gate_history_rejected_pair AS r "
            "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage) "
            "ORDER BY COALESCE(last_event_at, '') DESC, vesselcode, voyage"
        )

    def iter_gate_gap(self) -> Iterable[sqlite3.Row]:
        """闸口有报文、npp 核放库里却查不到的箱子（ARCHITECTURE-GATE.md §5）。"""
        yield from self.conn.execute(
            "SELECT * FROM v_gate_vs_npp AS g WHERE npp_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
            "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage) "
            'ORDER BY vesselcode, voyage, "ctnNo"'
        )

    def gate_gap_summary(self) -> dict[str, int]:
        q = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "gate_events": q(
                "SELECT COUNT(*) FROM v_gate_vs_npp AS g WHERE NOT EXISTS ("
                "SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "matched": q(
                "SELECT COUNT(*) FROM v_gate_vs_npp AS g WHERE npp_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "gap": q(
                "SELECT COUNT(*) FROM v_gate_vs_npp AS g WHERE npp_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage)"
            ),
            "gap_voyages": q(
                "SELECT COUNT(*) FROM (SELECT DISTINCT vesselcode, voyage "
                "FROM v_gate_vs_npp AS g WHERE npp_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM gate_history_rejected_pair AS r "
                "WHERE r.vesselcode=g.vesselcode AND r.voyage=g.voyage))"
            ),
        }
