"""Capture suspicious CODECO responses without contaminating the main database.

The normal gate pipeline deliberately attributes rows to the vessel code used in
the request because the endpoint normally omits that field.  That is unsafe for
virtual/aggregate plan records: the remote service may return an aggregate bucket
or may ignore an invalid vessel filter.  This module keeps those responses in a
separate SQLite database, preserving every returned row exactly as received.

Typical workflow::

    python gate_anomaly_sidecar.py discover --eta-start 2023-01-01 --eta-end 2026-06-30
    python gate_anomaly_sidecar.py quarantine-main
    python gate_anomaly_sidecar.py capture --max-requests 500 --delay-ms 1100
    python gate_anomaly_sidecar.py status

``discover`` and ``status`` never contact the remote service.  ``capture`` uses
one sequential worker and commits one page plus its checkpoint atomically, so a
later invocation resumes at the next page even for responses exceeding the main
pipeline's 5,000-page guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from client import AuthExpired, NpediClient
from config import BASE_DIR, Config, load_config


log = logging.getLogger("npedi.gate_anomaly")

DIRECTIONS = ("GATE_IN", "GATE_OUT")
DEFAULT_SIDECAR = BASE_DIR / "gate_anomaly_sidecar.sqlite"
DEFAULT_LOCK = BASE_DIR / ".gate-anomaly-sidecar.lock"
SCHEMA_VERSION = "1"

SIDECAR_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sidecar_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_candidate (
    candidate_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    query_vesselcode   TEXT NOT NULL,
    query_voyage       TEXT NOT NULL,
    vessel_name        TEXT,
    first_eta          TEXT NOT NULL,
    last_eta           TEXT NOT NULL,
    source_plan_rows   INTEGER NOT NULL DEFAULT 0,
    isolation_reason   TEXT NOT NULL,
    evidence_json      TEXT NOT NULL DEFAULT '{}',
    source_plan_json   TEXT NOT NULL DEFAULT '{}',
    discovered_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    quarantined_main_at TEXT,
    UNIQUE(query_vesselcode, query_voyage)
);

CREATE TABLE IF NOT EXISTS capture_job (
    job_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id       INTEGER NOT NULL REFERENCES capture_candidate(candidate_id),
    query_direction    TEXT NOT NULL CHECK(query_direction IN ('GATE_IN','GATE_OUT')),
    page_size          INTEGER NOT NULL,
    page_cap           INTEGER NOT NULL,
    planned_total      INTEGER,
    first_total        INTEGER,
    last_total         INTEGER,
    expected_pages     INTEGER,
    next_page          INTEGER NOT NULL DEFAULT 1,
    pages_saved        INTEGER NOT NULL DEFAULT 0,
    rows_saved         INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','running','partial','complete','error')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    completed_at       TEXT,
    last_error         TEXT,
    UNIQUE(candidate_id, query_direction)
);
CREATE INDEX IF NOT EXISTS idx_capture_job_status
    ON capture_job(status, planned_total, job_id);

CREATE TABLE IF NOT EXISTS capture_run (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL CHECK(status IN ('running','partial','complete','failed','auth_expired')),
    request_budget     INTEGER,
    requests_made      INTEGER NOT NULL DEFAULT 0,
    pages_saved        INTEGER NOT NULL DEFAULT 0,
    rows_saved         INTEGER NOT NULL DEFAULT 0,
    delay_ms           INTEGER NOT NULL,
    token_fingerprint  TEXT NOT NULL,
    error_class        TEXT,
    error_message      TEXT
);

CREATE TABLE IF NOT EXISTS capture_page (
    job_id             INTEGER NOT NULL REFERENCES capture_job(job_id),
    page_no            INTEGER NOT NULL,
    api_total          INTEGER NOT NULL,
    row_count          INTEGER NOT NULL,
    response_sha256    TEXT NOT NULL,
    fetched_at         TEXT NOT NULL,
    run_id             INTEGER REFERENCES capture_run(run_id),
    PRIMARY KEY(job_id, page_no)
);

CREATE TABLE IF NOT EXISTS capture_row (
    job_id             INTEGER NOT NULL REFERENCES capture_job(job_id),
    page_no            INTEGER NOT NULL,
    row_ordinal        INTEGER NOT NULL,
    response_id        TEXT,
    response_type      TEXT,
    response_vesselcode TEXT,
    response_voyage    TEXT,
    response_vessel    TEXT,
    response_direct    TEXT,
    response_sender_code TEXT,
    row_sha256         TEXT NOT NULL,
    raw_json           TEXT NOT NULL,
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY(job_id, page_no, row_ordinal),
    FOREIGN KEY(job_id, page_no) REFERENCES capture_page(job_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_capture_row_response_id
    ON capture_row(response_id);
CREATE INDEX IF NOT EXISTS idx_capture_row_identity
    ON capture_row(response_vesselcode, response_voyage);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def ensure_separate_database(sidecar_path: Path | str, main_path: Path | str) -> None:
    sidecar = resolved(sidecar_path)
    main = resolved(main_path)
    if sidecar == main:
        raise ValueError("旁路数据库不能与主数据库使用同一路径")


class SidecarLock:
    """A small cross-platform single-process lock dedicated to this sidecar."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def __enter__(self) -> "SidecarLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        self._file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError(f"已有旁路采集实例在运行（{self.path}）") from exc
        self._file.truncate(0)
        self._file.write(f"pid={os.getpid()} at={now_iso()}\n")
        self._file.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self._file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class SidecarStore:
    def __init__(self, path: Path | str, *, main_db_path: Path | str):
        ensure_separate_database(path, main_db_path)
        self.path = resolved(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SIDECAR_SCHEMA)
        self.conn.execute(
            "INSERT INTO sidecar_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SidecarStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def seed_candidate(
        self,
        *,
        vesselcode: str,
        voyage: str,
        vessel_name: str,
        first_eta: str,
        last_eta: str,
        source_plan_rows: int,
        reason: str,
        evidence: dict[str, Any],
        source_plan: dict[str, Any],
        planned_totals: dict[str, int | None],
        page_size: int,
        page_cap: int,
    ) -> bool:
        ts = now_iso()
        existing = self.conn.execute(
            "SELECT isolation_reason,evidence_json FROM capture_candidate "
            "WHERE query_vesselcode=? AND query_voyage=?",
            (vesselcode, voyage),
        ).fetchone()
        if existing is not None:
            merged_reasons = set(str(existing["isolation_reason"]).split("+"))
            merged_reasons.update(reason.split("+"))
            reason = "+".join(sorted(part for part in merged_reasons if part))
            try:
                old_evidence = json.loads(existing["evidence_json"] or "{}")
            except json.JSONDecodeError:
                old_evidence = {}
            merged_evidence = dict(old_evidence)
            merged_evidence.update(evidence)
            merged_evidence["reasons"] = sorted(merged_reasons)
            evidence = merged_evidence
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT INTO capture_candidate(
                query_vesselcode,query_voyage,vessel_name,first_eta,last_eta,
                source_plan_rows,isolation_reason,evidence_json,source_plan_json,
                discovered_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(query_vesselcode,query_voyage) DO UPDATE SET
                vessel_name=COALESCE(NULLIF(excluded.vessel_name,''),capture_candidate.vessel_name),
                first_eta=MIN(capture_candidate.first_eta,excluded.first_eta),
                last_eta=MAX(capture_candidate.last_eta,excluded.last_eta),
                source_plan_rows=MAX(capture_candidate.source_plan_rows,excluded.source_plan_rows),
                isolation_reason=excluded.isolation_reason,
                evidence_json=excluded.evidence_json,
                source_plan_json=CASE WHEN excluded.source_plan_json='{}'
                                      THEN capture_candidate.source_plan_json
                                      ELSE excluded.source_plan_json END,
                updated_at=excluded.updated_at
            """,
            (
                vesselcode, voyage, vessel_name, first_eta, last_eta,
                source_plan_rows, reason, canonical_json(evidence),
                canonical_json(source_plan), ts, ts,
            ),
        )
        candidate_id = int(self.conn.execute(
            "SELECT candidate_id FROM capture_candidate "
            "WHERE query_vesselcode=? AND query_voyage=?",
            (vesselcode, voyage),
        ).fetchone()[0])
        for direction in DIRECTIONS:
            self.conn.execute(
                """
                INSERT INTO capture_job(
                    candidate_id,query_direction,page_size,page_cap,planned_total,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id,query_direction) DO UPDATE SET
                    planned_total=COALESCE(capture_job.planned_total,excluded.planned_total),
                    page_cap=MAX(capture_job.page_cap,excluded.page_cap),
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id, direction, page_size, page_cap,
                    planned_totals.get(direction), ts, ts,
                ),
            )
        self.conn.commit()
        return self.conn.total_changes > before

    def recover_interrupted(self) -> None:
        """Recover logical running markers while the exclusive capture lock is held."""
        ts = now_iso()
        self.conn.execute(
            "UPDATE capture_job SET status='partial',updated_at=? "
            "WHERE status='running'",
            (ts,),
        )
        self.conn.execute(
            "UPDATE capture_run SET status='failed',finished_at=?,"
            "error_class='Interrupted',error_message='process ended before run close' "
            "WHERE status='running'",
            (ts,),
        )
        self.conn.commit()

    def begin_run(self, *, request_budget: int | None, delay_ms: int, token: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO capture_run(
                started_at,status,request_budget,delay_ms,token_fingerprint
            ) VALUES(?,'running',?,?,?)
            """,
            (now_iso(), request_budget, delay_ms, sha256_text(token)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        requests_made: int,
        pages_saved: int,
        rows_saved: int,
        error: BaseException | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE capture_run SET finished_at=?,status=?,requests_made=?,
                pages_saved=?,rows_saved=?,error_class=?,error_message=?
            WHERE run_id=?
            """,
            (
                now_iso(), status, requests_made, pages_saved, rows_saved,
                type(error).__name__ if error else None,
                str(error)[:1000] if error else None, run_id,
            ),
        )
        self.conn.commit()

    def unfinished_jobs(self, *, retry_errors: bool = False) -> list[sqlite3.Row]:
        statuses = ("pending", "partial", "error") if retry_errors else ("pending", "partial")
        placeholders = ",".join("?" for _ in statuses)
        return self.conn.execute(
            f"""
            SELECT j.*,c.query_vesselcode,c.query_voyage,c.vessel_name
            FROM capture_job AS j
            JOIN capture_candidate AS c ON c.candidate_id=j.candidate_id
            WHERE j.status IN ({placeholders})
            ORDER BY CASE j.status WHEN 'pending' THEN 0 WHEN 'partial' THEN 1 ELSE 2 END,
                     CASE WHEN j.planned_total IS NULL THEN 1 ELSE 0 END,
                     COALESCE(j.planned_total,9223372036854775807),j.job_id
            """,
            statuses,
        ).fetchall()

    def start_job(self, job_id: int, *, retry_error: bool = False) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM capture_job WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["status"] == "error" and not retry_error:
            raise RuntimeError(f"job {job_id} 处于 error，需显式 --retry-errors")
        max_page = int(self.conn.execute(
            "SELECT COALESCE(MAX(page_no),0) FROM capture_page WHERE job_id=?",
            (job_id,),
        ).fetchone()[0])
        next_page = max(int(row["next_page"]), max_page + 1)
        self.conn.execute(
            "UPDATE capture_job SET status='running',next_page=?,last_error=NULL,updated_at=? "
            "WHERE job_id=?",
            (next_page, now_iso(), job_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM capture_job WHERE job_id=?", (job_id,)
        ).fetchone()

    def mark_partial(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE capture_job SET status='partial',updated_at=? "
            "WHERE job_id=? AND status='running'",
            (now_iso(), job_id),
        )
        self.conn.commit()

    def mark_error(self, job_id: int, error: BaseException | str) -> None:
        self.conn.execute(
            "UPDATE capture_job SET status='error',last_error=?,updated_at=? WHERE job_id=?",
            (str(error)[:1000], now_iso(), job_id),
        )
        self.conn.commit()

    def save_page(
        self,
        *,
        run_id: int,
        job_id: int,
        page_no: int,
        api_total: int,
        rows: list[dict[str, Any]],
    ) -> tuple[bool, int]:
        job = self.conn.execute(
            "SELECT * FROM capture_job WHERE job_id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise KeyError(job_id)
        if int(job["next_page"]) != page_no:
            raise RuntimeError(
                f"job {job_id} 页断点不一致：next={job['next_page']} response={page_no}"
            )
        # These buckets keep accumulating while a multi-hour job walks them, so
        # a growing total is normal and only a shrinking one is evidence of a
        # changed result set.  The page plan stays frozen on the page-1 total:
        # the job captures that snapshot and stops, rather than chasing a
        # moving tail it can never reach.
        first_total = job["first_total"]
        if first_total is not None and api_total < int(first_total):
            raise RuntimeError(
                f"job {job_id} total 缩水：首次 {first_total}，第 {page_no} 页 {api_total}"
            )
        planned_basis = int(first_total) if first_total is not None else api_total
        page_size = int(job["page_size"])
        expected_pages = max(1, -(-planned_basis // page_size))
        if expected_pages > int(job["page_cap"]):
            raise RuntimeError(
                f"job {job_id} 需要 {expected_pages} 页，超过旁路上限 {job['page_cap']}"
            )
        if page_no > expected_pages:
            raise RuntimeError(
                f"job {job_id} 收到超出预计范围的第 {page_no}/{expected_pages} 页"
            )
        if api_total > 0 and page_no < expected_pages and not rows:
            raise RuntimeError(
                f"job {job_id} 第 {page_no}/{expected_pages} 页为空，拒绝越过缺口"
            )
        # Row count is checked against what the *current* total makes available,
        # so growth legitimately fills the frozen window's last page; a short
        # page inside that window is still a gap and still fails.
        expected_rows = 0 if api_total == 0 else min(
            page_size, api_total - (page_no - 1) * page_size
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"job {job_id} 第 {page_no}/{expected_pages} 页应有 {expected_rows} 行，"
                f"实际 {len(rows)} 行，拒绝留下分页缺口"
            )

        fetched_at = now_iso()
        raw_rows = [canonical_json(row) for row in rows]
        page_material = canonical_json({"total": api_total, "list": rows})
        page_hash = sha256_text(page_material)
        done = page_no >= expected_pages
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO capture_page(
                    job_id,page_no,api_total,row_count,response_sha256,fetched_at,run_id
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (job_id, page_no, api_total, len(rows), page_hash, fetched_at, run_id),
            )
            self.conn.executemany(
                """
                INSERT INTO capture_row(
                    job_id,page_no,row_ordinal,response_id,response_type,
                    response_vesselcode,response_voyage,response_vessel,response_direct,
                    response_sender_code,row_sha256,raw_json,fetched_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        job_id, page_no, ordinal,
                        clean(row.get("id")) or None,
                        clean(row.get("type")) or None,
                        clean(row.get("vesselcode") or row.get("vesselCode")) or None,
                        clean(row.get("voyage")) or None,
                        clean(row.get("vessel")) or None,
                        clean(row.get("direct")) or None,
                        clean(row.get("senderCode")) or None,
                        sha256_text(raw), raw, fetched_at,
                    )
                    for ordinal, (row, raw) in enumerate(zip(rows, raw_rows), 1)
                ],
            )
            self.conn.execute(
                """
                UPDATE capture_job SET
                    planned_total=COALESCE(planned_total,?),
                    first_total=COALESCE(first_total,?),last_total=?,expected_pages=?,
                    next_page=?,pages_saved=pages_saved+1,rows_saved=rows_saved+?,
                    status=?,completed_at=CASE WHEN ? THEN ? ELSE NULL END,
                    updated_at=?,last_error=NULL
                WHERE job_id=?
                """,
                (
                    api_total, api_total, api_total, expected_pages, page_no + 1, len(rows),
                    "complete" if done else "running", 1 if done else 0,
                    fetched_at, fetched_at, job_id,
                ),
            )
        return done, len(rows)

    def status(self) -> dict[str, Any]:
        counts = {status: 0 for status in ("pending", "running", "partial", "complete", "error")}
        for row in self.conn.execute(
            "SELECT status,COUNT(*) n FROM capture_job GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["n"])
        totals = self.conn.execute(
            "SELECT COUNT(*) candidates FROM capture_candidate"
        ).fetchone()
        payload: dict[str, Any] = {
            "database": str(self.path),
            "candidates": int(totals["candidates"]),
            "jobs": counts,
            "unfinished": counts["pending"] + counts["running"] + counts["partial"] + counts["error"],
            "pages_saved": int(self.conn.execute(
                "SELECT COUNT(*) FROM capture_page"
            ).fetchone()[0]),
            "rows_saved": int(self.conn.execute(
                "SELECT COUNT(*) FROM capture_row"
            ).fetchone()[0]),
        }
        payload["bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return payload

    def run_page_counts(self, run_id: int) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) pages,COALESCE(SUM(row_count),0) rows "
            "FROM capture_page WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return int(row["pages"]), int(row["rows"])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _read_only_connection(path: Path | str) -> sqlite3.Connection:
    db_path = resolved(path)
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=60
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _date_window(start: str, end_inclusive: str) -> tuple[str, str]:
    try:
        begin = datetime.strptime(start, "%Y-%m-%d")
        end = datetime.strptime(end_inclusive, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD") from exc
    if end < begin:
        raise ValueError("结束日期不能早于开始日期")
    return begin.strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d")


def discover(
    *,
    main_db_path: Path,
    sidecar_path: Path,
    eta_start: str,
    eta_end: str,
    total_threshold: int,
    page_size: int,
    page_cap: int,
    outliers_only: bool = False,
) -> dict[str, int]:
    """Discover source-virtual or oversized, not-yet-imported candidates."""
    start, end_exclusive = _date_window(eta_start, eta_end)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    main = _read_only_connection(main_db_path)
    try:
        if not _table_exists(main, "gate_history_candidate"):
            raise RuntimeError("主库缺少 gate_history_candidate")

        # Only uncompleted hits are auto-isolated by size.  A large completed
        # real voyage must never be silently reclassified after ingestion.
        for row in main.execute(
            """
            SELECT c.* FROM gate_history_candidate AS c
            WHERE c.status='hit'
              AND COALESCE(c.gatein_total,0)+COALESCE(c.gateout_total,0)>=?
              AND EXISTS (
                  SELECT 1 FROM gate_history_candidate_month AS m
                  WHERE m.vesselcode=c.vesselcode AND m.voyage=c.voyage
                    AND m.last_eta>=? AND m.first_eta<?
              )
            """,
            (total_threshold, start, end_exclusive),
        ):
            key = (clean(row["vesselcode"]), clean(row["voyage"]))
            found[key] = {
                "vessel_name": clean(row["vesselename"]),
                "first_eta": clean(row["first_eta"]),
                "last_eta": clean(row["last_eta"]),
                "source_plan_rows": int(row["source_plan_rows"] or 0),
                "reasons": {"oversized_unvalidated_response"},
                "planned_totals": {
                    "GATE_IN": int(row["gatein_total"] or 0),
                    "GATE_OUT": int(row["gateout_total"] or 0),
                },
                "source_plan": {},
            }

        if not outliers_only and _table_exists(main, "fact_vessel_plan_snapshot"):
            # A missing sysid or missing identifiers alone is common and is
            # deliberately insufficient.  Require an explicit non-vessel
            # semantic marker *and* no valid seven-digit IMO / nine-digit MMSI.
            for row in main.execute(
                """
                WITH suspicious AS (
                    SELECT *,
                    CASE
                      WHEN UPPER(COALESCE(json_extract(raw_json,'$.vesselCode'),'')) LIKE 'ZZZ%'
                        OR COALESCE(vessel_cn_name,'') LIKE '%中转%'
                        OR UPPER(COALESCE(vessel_en_name,'')) LIKE '%ZHONGZHUAN%'
                        THEN 'transship_aggregate'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%大港虚拟船%'
                        THEN 'dagang_virtual'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%虚拟%'
                        THEN 'other_virtual'
                      WHEN UPPER(TRIM(COALESCE(vessel_en_name,'')))='KONGXIANG'
                        OR TRIM(COALESCE(vessel_cn_name,''))='还空'
                        THEN 'empty_return_bucket'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%转码头%'
                        THEN 'terminal_transfer'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%铜精矿%'
                        OR UPPER(TRIM(COALESCE(json_extract(raw_json,'$.vesselCode'),'')))='TJK'
                        THEN 'copper_cargo_bucket'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%暂落%'
                        OR UPPER(COALESCE(json_extract(raw_json,'$.vesselCode'),'')) LIKE 'YZZL%'
                        THEN 'temporary_drop_bucket'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%工作面%'
                        OR UPPER(COALESCE(json_extract(raw_json,'$.vesselCode'),'')) LIKE 'ZDGZ%'
                        THEN 'grab_workface'
                      WHEN COALESCE(vessel_cn_name,'') LIKE '%桥吊修理%'
                        OR UPPER(TRIM(COALESCE(json_extract(raw_json,'$.vesselCode'),'')))='QDXLC'
                        THEN 'crane_repair'
                    END AS pseudo_group,
                    ROW_NUMBER() OVER (
                        PARTITION BY TRIM(vessel_code),TRIM(voyage)
                        ORDER BY snapshot_time DESC,vessel_plan_key DESC
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY TRIM(vessel_code),TRIM(voyage)
                    ) AS source_rows,
                    MIN(eta) OVER (
                        PARTITION BY TRIM(vessel_code),TRIM(voyage)
                    ) AS min_eta,
                    MAX(eta) OVER (
                        PARTITION BY TRIM(vessel_code),TRIM(voyage)
                    ) AS max_eta
                    FROM fact_vessel_plan_snapshot
                    WHERE eta>=? AND eta<?
                      AND TRIM(COALESCE(vessel_code,''))<>''
                      AND TRIM(COALESCE(voyage,''))<>''
                      AND NOT (
                          LENGTH(TRIM(COALESCE(json_extract(raw_json,'$.imo'),'')))=7
                          AND TRIM(COALESCE(json_extract(raw_json,'$.imo'),''))
                              NOT GLOB '*[^0-9]*'
                      )
                      AND NOT (
                          LENGTH(TRIM(COALESCE(json_extract(raw_json,'$.mmsi'),'')))=9
                          AND TRIM(COALESCE(json_extract(raw_json,'$.mmsi'),''))
                              NOT GLOB '*[^0-9]*'
                      )
                )
                SELECT * FROM suspicious WHERE rn=1 AND pseudo_group IS NOT NULL
                """,
                (start, end_exclusive),
            ):
                key = (clean(row["vessel_code"]), clean(row["voyage"]))
                item = found.setdefault(key, {
                    "vessel_name": clean(row["vessel_en_name"]),
                    "first_eta": clean(row["min_eta"]),
                    "last_eta": clean(row["max_eta"]),
                    "source_plan_rows": int(row["source_rows"] or 0),
                    "reasons": set(),
                    "planned_totals": {"GATE_IN": None, "GATE_OUT": None},
                    "source_plan": {},
                })
                item["reasons"].add(
                    "operational_bucket:" + clean(row["pseudo_group"])
                )
                try:
                    item["source_plan"] = json.loads(row["raw_json"] or "{}")
                except json.JSONDecodeError:
                    item["source_plan"] = {"unparsed_raw_json": row["raw_json"]}
                if item["planned_totals"]["GATE_IN"] is None:
                    candidate = main.execute(
                        "SELECT gatein_total,gateout_total FROM gate_history_candidate "
                        "WHERE vesselcode=? AND voyage=?",
                        key,
                    ).fetchone()
                    if candidate:
                        item["planned_totals"] = {
                            "GATE_IN": candidate["gatein_total"],
                            "GATE_OUT": candidate["gateout_total"],
                        }
    finally:
        main.close()

    inserted = 0
    with SidecarStore(sidecar_path, main_db_path=main_db_path) as sidecar:
        for (vesselcode, voyage), item in sorted(found.items()):
            reasons = sorted(item["reasons"])
            evidence = {
                "reasons": reasons,
                "total_threshold": total_threshold,
                "planned_totals": item["planned_totals"],
                "eta_window": [start, end_exclusive],
            }
            if sidecar.seed_candidate(
                vesselcode=vesselcode,
                voyage=voyage,
                vessel_name=item["vessel_name"],
                first_eta=item["first_eta"],
                last_eta=item["last_eta"],
                source_plan_rows=item["source_plan_rows"],
                reason="+".join(reasons),
                evidence=evidence,
                source_plan=item["source_plan"],
                planned_totals=item["planned_totals"],
                page_size=page_size,
                page_cap=page_cap,
            ):
                inserted += 1
    return {"found": len(found), "seeded_or_updated": inserted}


def quarantine_main(*, main_db_path: Path, sidecar_path: Path) -> int:
    """Exclude sidecar candidates from the normal queue using its audit table."""
    ensure_separate_database(sidecar_path, main_db_path)
    with SidecarStore(sidecar_path, main_db_path=main_db_path) as sidecar:
        candidates = sidecar.conn.execute("SELECT * FROM capture_candidate").fetchall()
        if not candidates:
            return 0
        main = sqlite3.connect(resolved(main_db_path), timeout=60)
        main.row_factory = sqlite3.Row
        main.execute("PRAGMA busy_timeout=60000")
        try:
            if not _table_exists(main, "gate_history_rejected_pair"):
                raise RuntimeError("主库缺少 gate_history_rejected_pair；拒绝隐式建表")
            needs_update: list[sqlite3.Row] = []
            for row in candidates:
                expected_reason = "sidecar:" + row["isolation_reason"]
                existing = main.execute(
                    "SELECT reason,vesselename,first_eta,last_eta,source_plan_rows "
                    "FROM gate_history_rejected_pair WHERE vesselcode=? AND voyage=?",
                    (row["query_vesselcode"], row["query_voyage"]),
                ).fetchone()
                if (
                    existing is None
                    or existing["reason"] != expected_reason
                    or existing["first_eta"] != row["first_eta"]
                    or existing["last_eta"] != row["last_eta"]
                    or int(existing["source_plan_rows"] or 0) != int(row["source_plan_rows"] or 0)
                ):
                    needs_update.append(row)
            if not needs_update:
                return 0
            ts = now_iso()
            with main:
                main.executemany(
                    """
                    INSERT INTO gate_history_rejected_pair(
                        vesselcode,voyage,reason,vesselename,first_eta,last_eta,
                        source_plan_rows,discovered_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(vesselcode,voyage) DO UPDATE SET
                        reason=excluded.reason,
                        vesselename=COALESCE(NULLIF(excluded.vesselename,''),gate_history_rejected_pair.vesselename),
                        first_eta=MIN(gate_history_rejected_pair.first_eta,excluded.first_eta),
                        last_eta=MAX(gate_history_rejected_pair.last_eta,excluded.last_eta),
                        source_plan_rows=MAX(gate_history_rejected_pair.source_plan_rows,excluded.source_plan_rows),
                        updated_at=excluded.updated_at
                    """,
                    [
                        (
                            row["query_vesselcode"], row["query_voyage"],
                            "sidecar:" + row["isolation_reason"], row["vessel_name"],
                            row["first_eta"], row["last_eta"], row["source_plan_rows"],
                            row["discovered_at"], ts,
                        )
                        for row in needs_update
                    ],
                )
            with sidecar.conn:
                sidecar.conn.executemany(
                    "UPDATE capture_candidate SET quarantined_main_at=?,updated_at=? "
                    "WHERE candidate_id=?",
                    [(ts, ts, row["candidate_id"]) for row in needs_update],
                )
            return len(needs_update)
        finally:
            main.close()


def main_history_status(
    *, main_db_path: Path, eta_start: str, eta_end: str,
) -> dict[str, int]:
    """Read the normal history queue without creating or mutating anything."""
    start, end_exclusive = _date_window(eta_start, eta_end)
    main = _read_only_connection(main_db_path)
    try:
        remaining = int(main.execute(
            """
            SELECT COUNT(*) FROM gate_history_candidate AS c
            WHERE c.status IN ('pending','hit')
              AND NOT EXISTS (
                  SELECT 1 FROM gate_history_rejected_pair AS r
                  WHERE r.vesselcode=c.vesselcode AND r.voyage=c.voyage
              )
              AND EXISTS (
                  SELECT 1 FROM gate_history_candidate_month AS m
                  WHERE m.vesselcode=c.vesselcode AND m.voyage=c.voyage
                    AND m.last_eta>=? AND m.first_eta<?
              )
            """,
            (start, end_exclusive),
        ).fetchone()[0])
        running = 0
        if _table_exists(main, "sync_runs"):
            running = int(main.execute(
                "SELECT COUNT(*) FROM sync_runs "
                "WHERE kind='gate_history_backfill' AND status='running'"
            ).fetchone()[0])
        return {"remaining": remaining, "running_gate_history_runs": running}
    finally:
        main.close()


def capture_jobs(
    *,
    store: SidecarStore,
    client: Any,
    run_id: int,
    request_budget: int | None,
    retry_errors: bool = False,
    log_every: int = 25,
) -> dict[str, int]:
    """Capture pages sequentially.  Exposed separately for deterministic tests."""
    start_requests = int(client.request_count)
    stats = {"requests": 0, "pages": 0, "rows": 0, "completed_jobs": 0, "errors": 0}
    for pending in store.unfinished_jobs(retry_errors=retry_errors):
        if request_budget is not None and client.request_count - start_requests >= request_budget:
            break
        job = store.start_job(int(pending["job_id"]), retry_error=retry_errors)
        job_id = int(job["job_id"])
        while True:
            used = int(client.request_count) - start_requests
            if request_budget is not None and used >= request_budget:
                store.mark_partial(job_id)
                break
            page_no = int(job["next_page"])
            if page_no > int(job["page_cap"]):
                error = RuntimeError(
                    f"job {job_id} 下一页 {page_no} 超过旁路上限 {job['page_cap']}"
                )
                store.mark_error(job_id, error)
                stats["errors"] += 1
                break
            try:
                data = client.scodeco_page(
                    page_no,
                    direction=job["query_direction"],
                    vessel_code=pending["query_vesselcode"],
                    voyage=pending["query_voyage"],
                    page_size=int(job["page_size"]),
                )
                rows = list(data.get("list") or [])
                api_total = int(data.get("total") or 0)
                done, row_count = store.save_page(
                    run_id=run_id,
                    job_id=job_id,
                    page_no=page_no,
                    api_total=api_total,
                    rows=rows,
                )
                stats["pages"] += 1
                stats["rows"] += row_count
                if page_no == 1 or page_no % max(1, log_every) == 0 or done:
                    log.info(
                        "%s/%s %s page=%d total=%d rows=%d%s",
                        pending["query_vesselcode"], pending["query_voyage"],
                        job["query_direction"], page_no, api_total, row_count,
                        " complete" if done else "",
                    )
                if done:
                    stats["completed_jobs"] += 1
                    break
                # Establish every candidate/direction total before spending a
                # whole slice on one large response.  Once all pending jobs
                # have a page-1 checkpoint, later slices resume smallest first.
                if page_no == 1 and pending["status"] == "pending":
                    store.mark_partial(job_id)
                    break
                job = store.conn.execute(
                    "SELECT * FROM capture_job WHERE job_id=?", (job_id,)
                ).fetchone()
            except AuthExpired:
                store.mark_partial(job_id)
                raise
            except Exception as exc:
                store.mark_error(job_id, exc)
                stats["errors"] += 1
                log.error(
                    "%s/%s %s page=%d paused: %s",
                    pending["query_vesselcode"], pending["query_voyage"],
                    job["query_direction"], page_no, exc,
                )
                break
    stats["requests"] = int(client.request_count) - start_requests
    return stats


def configure_logging(log_dir: Path, *, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "gate_anomaly_sidecar.log"
    root = logging.getLogger("npedi")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    root.addHandler(handler)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="隔离保存异常/聚合 CODECO 响应，不写主 gate_events"
    )
    parser.add_argument("--env", type=Path, default=BASE_DIR / ".env")
    parser.add_argument("--main-db", type=Path, default=None)
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    discover_parser = sub.add_parser("discover", help="只读扫描主库并建立旁路任务")
    discover_parser.add_argument("--eta-start", required=True)
    discover_parser.add_argument("--eta-end", required=True)
    discover_parser.add_argument("--total-threshold", type=int, default=50_000)
    discover_parser.add_argument("--page-size", type=int, default=100)
    discover_parser.add_argument("--page-cap", type=int, default=20_000)
    discover_parser.add_argument("--outliers-only", action="store_true")

    sub.add_parser("quarantine-main", help="把旁路候选写入主库已有隔离表")

    main_status_parser = sub.add_parser(
        "main-status", help="只读查看正常历史队列是否已经结束"
    )
    main_status_parser.add_argument("--eta-start", required=True)
    main_status_parser.add_argument("--eta-end", required=True)

    capture_parser = sub.add_parser("capture", help="逐页抓取到旁路数据库")
    capture_parser.add_argument("--max-requests", type=int, default=500)
    capture_parser.add_argument("--delay-ms", type=int, default=3000)
    capture_parser.add_argument("--auto-login", action="store_true")
    capture_parser.add_argument("--retry-errors", action="store_true")
    capture_parser.add_argument("--log-every", type=int, default=25)
    capture_parser.add_argument(
        "--lock-file",
        help=(
            "抓取期占用的管线锁，默认与正常进出门管线共用 .sync.gate.lock。"
            "指定独立锁可与正常历史回填并行——两者写的是不同数据库，"
            "这把锁只用来约束对上游的并发请求压力，改之前先确认速率预算。"
        ),
    )

    status_parser = sub.add_parser("status", help="查看旁路任务状态")
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.env)
    main_db = resolved(args.main_db or cfg.db_path)
    sidecar_db = resolved(args.sidecar_db)
    ensure_separate_database(sidecar_db, main_db)
    configure_logging(cfg.log_dir, verbose=args.verbose)

    if args.command == "discover":
        if args.total_threshold <= 0 or args.page_size <= 0 or args.page_cap <= 0:
            raise ValueError("阈值、page-size 和 page-cap 必须大于 0")
        result = discover(
            main_db_path=main_db,
            sidecar_path=sidecar_db,
            eta_start=args.eta_start,
            eta_end=args.eta_end,
            total_threshold=args.total_threshold,
            page_size=args.page_size,
            page_cap=args.page_cap,
            outliers_only=args.outliers_only,
        )
        print(canonical_json(result))
        return 0

    if args.command == "quarantine-main":
        count = quarantine_main(main_db_path=main_db, sidecar_path=sidecar_db)
        print(canonical_json({"quarantined": count}))
        return 0

    if args.command == "main-status":
        print(canonical_json(main_history_status(
            main_db_path=main_db,
            eta_start=args.eta_start,
            eta_end=args.eta_end,
        )))
        return 0

    if args.command == "status":
        with SidecarStore(sidecar_db, main_db_path=main_db) as store:
            status = store.status()
        if args.json:
            print(canonical_json(status))
        else:
            print(
                f"候选 {status['candidates']}｜任务 {status['jobs']}｜"
                f"已存 {status['pages_saved']} 页/{status['rows_saved']} 行｜"
                f"未完成 {status['unfinished']}"
            )
        return 0

    if args.max_requests < 0 or args.delay_ms < 0:
        raise ValueError("max-requests 和 delay-ms 不能为负")
    cfg.request_delay = (args.delay_ms / 1000.0, args.delay_ms / 1000.0 * 1.25)
    # Sidecar login is explicit.  While the main six-worker crawl is active it
    # should remain off; after that crawl exits, its wrapper may pass this flag.
    cfg.auto_login = bool(args.auto_login)
    lock_path = sidecar_db.with_suffix(sidecar_db.suffix + ".lock")
    # A capture slice shares the normal gate lock by default.  That lock bounds
    # concurrent API pressure only -- the two pipelines write different
    # databases -- so --lock-file can hand the sidecar its own lock and let it
    # run alongside the six-worker history crawl on a deliberate rate budget.
    # The sidecar's own database lock is always taken and keeps slices serial.
    pipeline_lock = resolved(args.lock_file) if args.lock_file else cfg.gate_lock_file
    if pipeline_lock != cfg.gate_lock_file:
        log.warning(
            "使用独立管线锁 %s：本次抓取不与正常进出门管线互斥，"
            "上游速率预算需自行保证", pipeline_lock,
        )
    with SidecarLock(pipeline_lock), SidecarLock(lock_path), SidecarStore(
        sidecar_db, main_db_path=main_db
    ) as store:
        store.recover_interrupted()
        with NpediClient(cfg) as client:
            run_id = store.begin_run(
                request_budget=args.max_requests or None,
                delay_ms=args.delay_ms,
                token=cfg.token,
            )
            stats = {"requests": 0, "pages": 0, "rows": 0}
            try:
                # One authenticated probe belongs to the run budget.
                client.get_info()
                remaining_budget = None
                if args.max_requests:
                    remaining_budget = max(0, args.max_requests - client.request_count)
                stats = capture_jobs(
                    store=store,
                    client=client,
                    run_id=run_id,
                    request_budget=remaining_budget,
                    retry_errors=args.retry_errors,
                    log_every=args.log_every,
                )
                unfinished = store.status()["unfinished"]
                status = "complete" if unfinished == 0 else "partial"
                run_pages, run_rows = store.run_page_counts(run_id)
                store.finish_run(
                    run_id,
                    status=status,
                    requests_made=client.request_count,
                    pages_saved=run_pages,
                    rows_saved=run_rows,
                )
                print(canonical_json({**stats, "unfinished": unfinished, "status": status}))
                return 0
            except AuthExpired as exc:
                run_pages, run_rows = store.run_page_counts(run_id)
                store.finish_run(
                    run_id,
                    status="auth_expired",
                    requests_made=client.request_count,
                    pages_saved=run_pages,
                    rows_saved=run_rows,
                    error=exc,
                )
                log.error("旁路采集 token 失效：%s", exc)
                return 2
            except Exception as exc:
                run_pages, run_rows = store.run_page_counts(run_id)
                store.finish_run(
                    run_id,
                    status="failed",
                    requests_made=client.request_count,
                    pages_saved=run_pages,
                    rows_saved=run_rows,
                    error=exc,
                )
                log.exception("旁路采集失败")
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
