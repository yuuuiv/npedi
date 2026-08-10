"""Full container catalog, enrichment queues, and coverage reporting."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from timeseries import TimeseriesStore, now_utc


_CONTAINER_RE = re.compile(r"^[A-Z]{3}[UJZ][0-9]{7}$")


def normalize_container_no(value: Any) -> str:
    return str(value or "").strip().upper()


def iso6346_valid(value: Any) -> bool:
    """Validate the ISO 6346 owner/category/serial check digit."""
    number = normalize_container_no(value)
    if not _CONTAINER_RE.fullmatch(number):
        return False
    total = 0
    for position, char in enumerate(number[:10]):
        if char.isdigit():
            code = int(char)
        else:
            base = ord(char) - 55
            code = base + (base - 1) // 10
        total += code * (2**position)
    return total % 11 % 10 == int(number[-1])


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def seed_container_catalog(store: TimeseriesStore) -> dict[str, int]:
    """Merge every locally collected CODECO container into the catalog.

    The operation is idempotent. Existing remote enrichment states are kept;
    only local source coverage and event bounds are refreshed.
    """
    conn = store.conn
    if not _has_table(conn, "gate_events"):
        raise RuntimeError("gate_events table is missing; run the gate backfill first")
    conn.create_function("iso6346_valid", 1, lambda value: int(iso6346_valid(value)))
    before = conn.execute("SELECT COUNT(*) FROM container_enrichment_state").fetchone()[0]
    stamp = now_utc()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO container_enrichment_state(
                   container_no,source,first_event_time,last_event_time,
                   gate_event_count,iso6346_valid,first_seen_at,last_seen_at,
                   vgm_status,history_status)
               SELECT UPPER(TRIM(ctnNo)), 'gate_events',
                      MIN(COALESCE(NULLIF(TRIM(inGateTime),''),
                                   NULLIF(TRIM(outGateTime),''),
                                   NULLIF(TRIM(msgReceiveTime),''))),
                      MAX(COALESCE(NULLIF(TRIM(outGateTime),''),
                                   NULLIF(TRIM(inGateTime),''),
                                   NULLIF(TRIM(msgReceiveTime),''))),
                      COUNT(*), iso6346_valid(UPPER(TRIM(ctnNo))), ?, ?,
                      CASE WHEN iso6346_valid(UPPER(TRIM(ctnNo)))=1
                           THEN 'pending' ELSE 'invalid' END,
                      CASE WHEN iso6346_valid(UPPER(TRIM(ctnNo)))=1
                           THEN 'pending' ELSE 'invalid' END
               FROM gate_events
               WHERE ctnNo IS NOT NULL AND TRIM(ctnNo)<>''
               GROUP BY UPPER(TRIM(ctnNo))
               ON CONFLICT(container_no) DO UPDATE SET
                   source=CASE WHEN instr(container_enrichment_state.source,'gate_events')>0
                               THEN container_enrichment_state.source
                               ELSE container_enrichment_state.source||'+gate_events' END,
                   first_event_time=excluded.first_event_time,
                   last_event_time=excluded.last_event_time,
                   gate_event_count=excluded.gate_event_count,
                   iso6346_valid=excluded.iso6346_valid,
                   last_seen_at=excluded.last_seen_at,
                   vgm_status=CASE
                       WHEN excluded.iso6346_valid=0 THEN 'invalid'
                       WHEN container_enrichment_state.vgm_status='invalid' THEN 'pending'
                       ELSE container_enrichment_state.vgm_status END,
                   history_status=CASE
                       WHEN excluded.iso6346_valid=0 THEN 'invalid'
                       WHEN container_enrichment_state.history_status='invalid' THEN 'pending'
                       ELSE container_enrichment_state.history_status END""",
            (stamp, stamp),
        )
        conn.execute(
            """UPDATE container_enrichment_state AS s
               SET vgm_status='complete'
               WHERE EXISTS (
                   SELECT 1 FROM fact_container_vgm v
                   WHERE UPPER(TRIM(v.container_no))=s.container_no
               )"""
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after = conn.execute("SELECT COUNT(*) FROM container_enrichment_state").fetchone()[0]
    valid = conn.execute(
        "SELECT COUNT(*) FROM container_enrichment_state WHERE iso6346_valid=1"
    ).fetchone()[0]
    return {"before": int(before), "after": int(after), "inserted": int(after - before), "valid": int(valid)}


def ensure_container_state(
    store: TimeseriesStore,
    container_no: Any,
    *,
    source: str,
) -> None:
    number = normalize_container_no(container_no)
    if not number:
        return
    valid = int(iso6346_valid(number))
    stamp = now_utc()
    initial = "pending" if valid else "invalid"
    store.conn.execute(
        """INSERT INTO container_enrichment_state(
               container_no,source,iso6346_valid,first_seen_at,last_seen_at,
               vgm_status,history_status)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(container_no) DO UPDATE SET
               source=CASE WHEN instr(container_enrichment_state.source,excluded.source)>0
                           THEN container_enrichment_state.source
                           ELSE container_enrichment_state.source||'+'||excluded.source END,
               last_seen_at=excluded.last_seen_at""",
        (number, source, valid, stamp, stamp, initial, initial),
    )


def enrichment_batch(
    store: TimeseriesStore,
    kind: str,
    limit: int,
    offset: int = 0,
) -> list[str]:
    if kind not in {"vgm", "history"}:
        raise ValueError(f"unsupported enrichment kind: {kind}")
    status_col = "vgm_status" if kind == "vgm" else "history_status"
    limit, offset = max(1, int(limit)), max(0, int(offset))
    rows = store.conn.execute(
        f"""SELECT container_no FROM container_enrichment_state
            WHERE {status_col} IN ('pending','error') AND iso6346_valid=1
            ORDER BY CASE {status_col} WHEN 'pending' THEN 0 ELSE 1 END,
                     last_event_time DESC, container_no
            LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return [row[0] for row in rows]


def claim_enrichment_batch(
    store: TimeseriesStore,
    kind: str,
    limit: int,
    worker_id: str,
) -> list[str]:
    """Atomically lease pending rows to one bounded parallel worker."""
    if kind not in {"vgm", "history"}:
        raise ValueError(f"unsupported enrichment kind: {kind}")
    worker_id = str(worker_id).strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    status_col = "vgm_status" if kind == "vgm" else "history_status"
    limit = max(1, int(limit))
    conn = store.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A killed process cannot release its rows. Two hours is comfortably
        # longer than a normal 500-container batch at the polite throttle.
        conn.execute(
            """DELETE FROM container_enrichment_claim
               WHERE datetime(claimed_at) < datetime('now','-2 hours')"""
        )
        conn.execute(
            f"""DELETE FROM container_enrichment_claim
                WHERE kind=? AND container_no IN (
                    SELECT container_no FROM container_enrichment_state
                    WHERE {status_col} IN ('complete','invalid')
                )""",
            (kind,),
        )
        rows = conn.execute(
            f"""SELECT s.container_no FROM container_enrichment_state AS s
                WHERE s.{status_col} IN ('pending','error')
                  AND s.iso6346_valid=1
                  AND NOT EXISTS (
                      SELECT 1 FROM container_enrichment_claim AS c
                      WHERE c.kind=? AND c.container_no=s.container_no
                  )
                ORDER BY CASE s.{status_col} WHEN 'pending' THEN 0 ELSE 1 END,
                         s.last_event_time DESC, s.container_no
                LIMIT ?""",
            (kind, limit),
        ).fetchall()
        stamp = now_utc()
        conn.executemany(
            """INSERT INTO container_enrichment_claim(kind,container_no,worker_id,claimed_at)
               VALUES(?,?,?,?)""",
            [(kind, row[0], worker_id, stamp) for row in rows],
        )
        conn.commit()
        return [row[0] for row in rows]
    except Exception:
        conn.rollback()
        raise


def release_enrichment_claims(
    store: TimeseriesStore,
    kind: str,
    worker_id: str,
) -> None:
    if kind not in {"vgm", "history"}:
        raise ValueError(f"unsupported enrichment kind: {kind}")
    store.conn.execute(
        "DELETE FROM container_enrichment_claim WHERE kind=? AND worker_id=?",
        (kind, str(worker_id)),
    )
    store.conn.commit()


def mark_enrichment(
    store: TimeseriesStore,
    container_no: Any,
    kind: str,
    *,
    success: bool,
    error: str | None = None,
) -> None:
    if kind not in {"vgm", "history"}:
        raise ValueError(f"unsupported enrichment kind: {kind}")
    number = normalize_container_no(container_no)
    ensure_container_state(store, number, source=kind)
    prefix = "vgm" if kind == "vgm" else "history"
    store.conn.execute(
        f"""UPDATE container_enrichment_state
            SET {prefix}_status=?,
                {prefix}_attempt_count={prefix}_attempt_count+1,
                {prefix}_last_attempt_at=?,
                {prefix}_last_error=?
            WHERE container_no=?""",
        ("complete" if success else "error", now_utc(), None if success else (error or "request failed"), number),
    )
    # Grouped with the rest of this container's writes; claim/release still
    # commit immediately because other workers must see them at once.
    store.commit()


def coverage_status(store: TimeseriesStore) -> dict[str, Any]:
    conn = store.conn
    total = conn.execute("SELECT COUNT(*) FROM container_enrichment_state").fetchone()[0]
    valid = conn.execute(
        "SELECT COUNT(*) FROM container_enrichment_state WHERE iso6346_valid=1"
    ).fetchone()[0]

    def statuses(column: str) -> dict[str, int]:
        return {
            row[0]: int(row[1])
            for row in conn.execute(
                f"SELECT {column},COUNT(*) FROM container_enrichment_state GROUP BY {column}"
            )
        }

    gate_rows = gate_boxes = gate_in = gate_out = 0
    if _has_table(conn, "gate_events"):
        gate_rows, gate_boxes, gate_in, gate_out = conn.execute(
            """SELECT COUNT(*), COUNT(DISTINCT UPPER(TRIM(ctnNo))),
                      SUM(inGateTime IS NOT NULL AND TRIM(inGateTime)<>''),
                      SUM(outGateTime IS NOT NULL AND TRIM(outGateTime)<>'')
               FROM gate_events
               WHERE ctnNo IS NOT NULL AND TRIM(ctnNo)<>''"""
        ).fetchone()
    return {
        "catalog": {"total": int(total), "iso6346_valid": int(valid), "invalid": int(total - valid)},
        "vgm": {
            "queue": statuses("vgm_status"),
            "facts": int(conn.execute("SELECT COUNT(*) FROM fact_container_vgm").fetchone()[0]),
            "containers_with_facts": int(conn.execute(
                "SELECT COUNT(DISTINCT UPPER(TRIM(container_no))) FROM fact_container_vgm WHERE container_no IS NOT NULL"
            ).fetchone()[0]),
        },
        "container_history_api": {
            "queue": statuses("history_status"),
            "facts": int(conn.execute("SELECT COUNT(*) FROM fact_container_event").fetchone()[0]),
        },
        "gate_history_full": {
            "source_rows": int(gate_rows or 0),
            "containers": int(gate_boxes or 0),
            "in_gate_events": int(gate_in or 0),
            "out_gate_events": int(gate_out or 0),
        },
    }
