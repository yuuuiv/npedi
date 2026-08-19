"""Gold daily/weekly aggregation from the normalized fact tables."""
from __future__ import annotations

import sqlite3
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backtest import fact_rows_as_of, normalize_as_of
from timeseries import TimeseriesStore, now_utc


ANALYSIS_MIN_DATE = date(2000, 1, 1)
ANALYSIS_FUTURE_DAYS = 180
MAX_OPERATIONAL_DELAY_HOURS = 24 * 30
LOGGER = logging.getLogger("npedi.aggregate")


def day(value: str | None) -> str | None:
    return value[:10] if value else None


def week_start(value: str) -> str:
    d = date.fromisoformat(value)
    return (d - timedelta(days=d.weekday())).isoformat()


def _row(bucket: dict[str, Any], key: tuple[str, str, str, str, str, str]) -> dict[str, Any]:
    if key not in bucket:
        bucket[key] = {"vgm_container_count": 0, "vgm_weight_kg": 0.0, "released_bill_count": 0, "released_weight": 0.0, "released_volume": 0.0, "released_weight_kg": 0.0, "released_piece_count": 0.0, "transshipment_container_count": 0, "transshipment_weight": 0.0, "transshipment_volume": 0.0, "planned_vessel_call_count": 0, "actual_vessel_call_count": 0, "arrival_delays": [], "departure_delays": []}
    return bucket[key]


def _plan_rows(store: TimeseriesStore, as_of: str | None):
    # fact_vessel_plan_snapshot is append-only. Current Gold must use one
    # latest-known snapshot per natural voyage identity; counting every
    # observation inflated a single call into many planned calls.
    cutoff = normalize_as_of(as_of) if as_of else None
    where = "WHERE snapshot_time<=?" if cutoff else ""
    args = (cutoff,) if cutoff else ()
    yield from store.conn.execute(f"""SELECT * FROM (
        SELECT p.*,
               ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(vessel_code, ''), COALESCE(voyage, ''),
                                COALESCE(terminal_code, ''), COALESCE(direction, '')
                   ORDER BY snapshot_time DESC, vessel_plan_key DESC
               ) AS latest_rank
        FROM fact_vessel_plan_snapshot AS p
        {where}
    ) WHERE latest_rank=1""", args)


def rebuild_daily(store: TimeseriesStore, start: str | None = None, end: str | None = None, as_of: str | None = None) -> int:
    as_of = normalize_as_of(as_of)
    cutoff_day = as_of[:10] if as_of else None
    if cutoff_day and (end is None or end > cutoff_day):
        end = cutoff_day
    anchor = date.fromisoformat(cutoff_day) if cutoff_day else datetime.now(timezone.utc).date()
    policy_start = ANALYSIS_MIN_DATE.isoformat()
    policy_end = (anchor + timedelta(days=ANALYSIS_FUTURE_DAYS)).isoformat()
    effective_start = max(filter(None, (start, policy_start)))
    effective_end = min(filter(None, (end, policy_end)))
    rows: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    def allowed(value: str | None) -> bool:
        return bool(value and effective_start <= value <= effective_end)

    vgm_rows = fact_rows_as_of(store, "fact_container_vgm", as_of) if as_of else store.conn.execute("SELECT * FROM fact_container_vgm")
    for r in vgm_rows:
        d = day(r["operator_time"])
        if not allowed(d):
            continue
        b = _row(rows, (d, r["terminal_code"] or "", r["direction"] or "", "", "", r["operator_code"] or ""))
        b["vgm_container_count"] += 1
        b["vgm_weight_kg"] += r["vgm_weight_kg"] or 0.0
    LOGGER.info("VGM facts aggregated; buckets=%s", len(rows))
    release_rows = fact_rows_as_of(store, "fact_cargo_release", as_of) if as_of else store.conn.execute("SELECT * FROM fact_cargo_release")
    for r in release_rows:
        d = day(r["pass_time"])
        if not allowed(d):
            continue
        b = _row(rows, (d, r["terminal_code"] or "", r["direction"] or "", "", "", ""))
        b["released_bill_count"] += int(bool(r["bill_no"]))
        b["released_weight"] += r["gross_weight"] or 0.0
        b["released_volume"] += r["cargo_volume"] or 0.0
        b["released_weight_kg"] += r["gross_weight_kg"] or 0.0
        b["released_piece_count"] += r["piece_count"] or 0.0
    LOGGER.info("cargo-release facts aggregated; buckets=%s", len(rows))
    transshipment_rows = fact_rows_as_of(store, "fact_transshipment", as_of) if as_of else store.conn.execute("SELECT * FROM fact_transshipment")
    for r in transshipment_rows:
        d = day(r["sailing_date"])
        if not allowed(d):
            continue
        route = ":".join(x or "" for x in (r["first_load_port_code"], r["first_discharge_port_code"], r["second_trans_port_code"]))
        b = _row(rows, (d, r["terminal_code"] or "", "", route, r["cargo_group_name"] or "UNKNOWN", r["operator_code"] or ""))
        b["transshipment_container_count"] += int(bool(r["container_no"]))
        b["transshipment_weight"] += r["weight"] or 0.0
        b["transshipment_volume"] += r["volume"] or 0.0
    LOGGER.info("transshipment facts aggregated; buckets=%s", len(rows))
    LOGGER.info("selecting the latest vessel-plan snapshot per voyage identity")
    for plan_count, r in enumerate(_plan_rows(store, as_of), start=1):
        d = day(r["eta"])
        if not allowed(d):
            continue
        b = _row(rows, (d, r["terminal_code"] or "", r["direction"] or "", "", "", ""))
        b["planned_vessel_call_count"] += 1
        if r["ata"]:
            b["actual_vessel_call_count"] += 1
        if r["eta"] and r["ata"]:
            try:
                delta = (datetime.fromisoformat(r["ata"]) - datetime.fromisoformat(r["eta"])).total_seconds() / 3600
                if 0 <= delta <= MAX_OPERATIONAL_DELAY_HOURS:
                    b["arrival_delays"].append(delta)
            except ValueError:
                pass
        if plan_count % 100000 == 0:
            LOGGER.info("processed %s latest vessel-plan rows", plan_count)
        if r["etd"] and r["atd"]:
            try:
                delta = (datetime.fromisoformat(r["atd"]) - datetime.fromisoformat(r["etd"])).total_seconds() / 3600
                if 0 <= delta <= MAX_OPERATIONAL_DELAY_HOURS:
                    b["departure_delays"].append(delta)
            except ValueError:
                pass

    target = "agg_flow_daily_asof" if as_of else "agg_flow_daily"
    if as_of:
        store.conn.execute("DELETE FROM agg_flow_daily_asof WHERE as_of_time=?", (as_of,))
    else:
        store.conn.execute("DELETE FROM agg_flow_daily" + ((" WHERE flow_date BETWEEN ? AND ?") if start and end else ""), ((start, end) if start and end else ()))
    daily_fields = (
        "flow_date", "terminal_code", "direction", "route_key",
        "cargo_group_key", "vessel_operator", "vgm_container_count",
        "vgm_weight_kg", "released_bill_count", "released_weight",
        "released_volume", "transshipment_container_count",
        "transshipment_weight", "transshipment_volume",
        "planned_vessel_call_count", "actual_vessel_call_count",
        "avg_arrival_delay_hours", "avg_departure_delay_hours",
        "released_weight_kg", "released_piece_count", "created_at",
    )
    for key, b in rows.items():
        av = sum(b["arrival_delays"]) / len(b["arrival_delays"]) if b["arrival_delays"] else None
        dv = sum(b["departure_delays"]) / len(b["departure_delays"]) if b["departure_delays"] else None
        values = (
            key[0], key[1], key[2], key[3], key[4], key[5],
            b["vgm_container_count"], b["vgm_weight_kg"],
            b["released_bill_count"], b["released_weight"], b["released_volume"],
            b["transshipment_container_count"], b["transshipment_weight"],
            b["transshipment_volume"], b["planned_vessel_call_count"],
            b["actual_vessel_call_count"], av, dv,
            b["released_weight_kg"], b["released_piece_count"], now_utc(),
        )
        if as_of:
            fields = ("as_of_time",) + daily_fields
            values = (as_of,) + values
            table = "agg_flow_daily_asof"
        else:
            fields = daily_fields
            table = "agg_flow_daily"
        store.conn.execute(
            f"INSERT OR REPLACE INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
    store.conn.commit()
    LOGGER.info("daily Gold aggregation committed; rows=%s", len(rows))
    rebuild_weekly(store, as_of=as_of)
    return len(rows)


def rebuild_weekly(store: TimeseriesStore, as_of: str | None = None) -> int:
    as_of = normalize_as_of(as_of)
    source = "agg_flow_weekly_asof" if as_of else "agg_flow_weekly"
    daily_source = "agg_flow_daily_asof" if as_of else "agg_flow_daily"
    grouped: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    daily_sql = f"SELECT * FROM {daily_source}" + (" WHERE as_of_time=?" if as_of else "")
    daily_args = (as_of,) if as_of else ()
    for r in store.conn.execute(daily_sql, daily_args):
        key = (week_start(r["flow_date"]), r["terminal_code"], r["direction"], r["route_key"], r["cargo_group_key"], r["vessel_operator"])
        b = grouped.setdefault(key, {"vgm_container_count": 0, "vgm_weight_kg": 0.0, "released_bill_count": 0, "released_weight": 0.0, "released_volume": 0.0, "released_weight_kg": 0.0, "released_piece_count": 0.0, "transshipment_container_count": 0, "transshipment_weight": 0.0, "transshipment_volume": 0.0, "planned_vessel_call_count": 0, "actual_vessel_call_count": 0, "arrival": [], "departure": []})
        for field in ("vgm_container_count","vgm_weight_kg","released_bill_count","released_weight","released_volume","released_weight_kg","released_piece_count","transshipment_container_count","transshipment_weight","transshipment_volume","planned_vessel_call_count","actual_vessel_call_count"):
            b[field] += r[field] or 0
        if r["avg_arrival_delay_hours"] is not None:
            b["arrival"].append(r["avg_arrival_delay_hours"])
        if r["avg_departure_delay_hours"] is not None:
            b["departure"].append(r["avg_departure_delay_hours"])
    if as_of:
        store.conn.execute("DELETE FROM agg_flow_weekly_asof WHERE as_of_time=?", (as_of,))
    else:
        store.conn.execute("DELETE FROM agg_flow_weekly")
    weekly_fields = (
        "week_start", "terminal_code", "direction", "route_key",
        "cargo_group_key", "vessel_operator", "vgm_container_count",
        "vgm_weight_kg", "released_bill_count", "released_weight",
        "released_volume", "transshipment_container_count",
        "transshipment_weight", "transshipment_volume",
        "planned_vessel_call_count", "actual_vessel_call_count",
        "avg_arrival_delay_hours", "avg_departure_delay_hours",
        "released_weight_kg", "released_piece_count", "created_at",
    )
    for key, b in grouped.items():
        values = (
            key[0], key[1], key[2], key[3], key[4], key[5],
            b["vgm_container_count"], b["vgm_weight_kg"],
            b["released_bill_count"], b["released_weight"], b["released_volume"],
            b["transshipment_container_count"], b["transshipment_weight"],
            b["transshipment_volume"], b["planned_vessel_call_count"],
            b["actual_vessel_call_count"],
            sum(b["arrival"]) / len(b["arrival"]) if b["arrival"] else None,
            sum(b["departure"]) / len(b["departure"]) if b["departure"] else None,
            b["released_weight_kg"], b["released_piece_count"], now_utc(),
        )
        if as_of:
            fields = ("as_of_time",) + weekly_fields
            values = (as_of,) + values
            table = "agg_flow_weekly_asof"
        else:
            fields = weekly_fields
            table = "agg_flow_weekly"
        store.conn.execute(
            f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
    store.conn.commit()
    LOGGER.info("weekly Gold aggregation committed; rows=%s", len(grouped))
    return len(grouped)


def rebuild_gate_daily(store: TimeseriesStore, as_of: str | None = None) -> int:
    """Materialize full-port CODECO throughput without copying raw events."""
    as_of = normalize_as_of(as_of)
    if not store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_events'"
    ).fetchone():
        return 0
    anchor = date.fromisoformat(as_of[:10]) if as_of else datetime.now(timezone.utc).date()
    policy_end = (anchor + timedelta(days=ANALYSIS_FUTURE_DAYS)).isoformat()
    fetched_filter = "AND fetched_at<=?" if as_of else ""
    args: tuple[Any, ...] = ((as_of, as_of) if as_of else ()) + (
        ANALYSIS_MIN_DATE.isoformat(), policy_end,
    )
    rows = store.conn.execute(
        f"""WITH gate_event AS (
                SELECT UPPER(TRIM(ctnNo)) AS container_no,
                       COALESCE(direct,'') AS direction,
                       TRIM(inGateTime) AS raw_time, 'in' AS event_kind,
                       UPPER(substr(TRIM(COALESCE(ctnSizeType,'')),1,1)) AS size_code
                FROM gate_events AS e
                WHERE type='GATE_IN'
                  AND ctnNo IS NOT NULL AND TRIM(ctnNo)<>''
                  AND inGateTime IS NOT NULL AND TRIM(inGateTime)<>''
                  AND NOT EXISTS (
                      SELECT 1 FROM gate_history_rejected_pair AS r
                      WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage
                  )
                  {fetched_filter}
                UNION ALL
                SELECT UPPER(TRIM(ctnNo)), COALESCE(direct,''),
                       TRIM(outGateTime), 'out',
                       UPPER(substr(TRIM(COALESCE(ctnSizeType,'')),1,1))
                FROM gate_events AS e
                WHERE type='GATE_OUT'
                  AND ctnNo IS NOT NULL AND TRIM(ctnNo)<>''
                  AND outGateTime IS NOT NULL AND TRIM(outGateTime)<>''
                  AND NOT EXISTS (
                      SELECT 1 FROM gate_history_rejected_pair AS r
                      WHERE r.vesselcode=e.vesselcode AND r.voyage=e.voyage
                  )
                  {fetched_filter}
             ), normalized AS (
                SELECT container_no,direction,event_kind,
                       CASE WHEN substr(raw_time,5,1)='-' THEN substr(raw_time,1,10)
                            WHEN length(raw_time)>=8 THEN
                              substr(raw_time,1,4)||'-'||substr(raw_time,5,2)||'-'||substr(raw_time,7,2)
                            ELSE NULL END AS flow_date,
                       CASE size_code
                            WHEN '1' THEN 0.5
                            WHEN '2' THEN 1.0
                            WHEN '3' THEN 1.5
                            WHEN '4' THEN 2.0
                            WHEN 'L' THEN 2.25
                            ELSE NULL END AS teu
                FROM gate_event
             ), deduplicated AS (
                SELECT flow_date,direction,event_kind,container_no,MAX(teu) AS teu
                FROM normalized
                WHERE flow_date BETWEEN ? AND ?
                GROUP BY flow_date,direction,event_kind,container_no
             )
             SELECT flow_date,direction,
                    SUM(event_kind='in'),
                    SUM(event_kind='out'),
                    COUNT(DISTINCT container_no),
                    SUM(CASE WHEN event_kind='in' THEN COALESCE(teu,0) ELSE 0 END),
                    SUM(CASE WHEN event_kind='out' THEN COALESCE(teu,0) ELSE 0 END),
                    SUM(event_kind='in' AND teu IS NOT NULL),
                    SUM(event_kind='out' AND teu IS NOT NULL)
             FROM deduplicated
             GROUP BY flow_date,direction
             ORDER BY flow_date,direction""",
        args,
    ).fetchall()
    table = "agg_gate_daily_asof" if as_of else "agg_gate_daily"
    if as_of:
        store.conn.execute("DELETE FROM agg_gate_daily_asof WHERE as_of_time=?", (as_of,))
    else:
        store.conn.execute("DELETE FROM agg_gate_daily")
    fields = (
        "flow_date", "direction", "in_gate_container_count",
        "out_gate_container_count", "unique_gate_container_count", "created_at",
        "in_gate_teu", "out_gate_teu", "in_teu_known_count",
        "out_teu_known_count",
    )
    for row in rows:
        values: tuple[Any, ...] = tuple(row[:5]) + (now_utc(),) + tuple(row[5:])
        target_fields = fields
        if as_of:
            target_fields = ("as_of_time",) + fields
            values = (as_of,) + values
        store.conn.execute(
            f"INSERT OR REPLACE INTO {table}({','.join(target_fields)}) VALUES({','.join('?' for _ in target_fields)})",
            values,
        )
    store.conn.commit()
    LOGGER.info("full gate-history aggregation committed; rows=%s", len(rows))
    return len(rows)


def rebuild_gold(store: TimeseriesStore, start: str | None = None, end: str | None = None, as_of: str | None = None) -> dict[str, int]:
    as_of = normalize_as_of(as_of)
    daily = rebuild_daily(store, start, end, as_of=as_of)
    gate_daily = rebuild_gate_daily(store, as_of=as_of)
    weekly_table = "agg_flow_weekly_asof" if as_of else "agg_flow_weekly"
    weekly_sql = f"SELECT COUNT(*) FROM {weekly_table}" + (" WHERE as_of_time=?" if as_of else "")
    weekly = store.conn.execute(weekly_sql, (as_of,) if as_of else ()).fetchone()[0]
    return {"daily": daily, "weekly": int(weekly), "gate_daily": gate_daily, "as_of": as_of}
