"""Gold daily/weekly aggregation from the normalized fact tables."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backtest import fact_rows_as_of, normalize_as_of
from timeseries import TimeseriesStore, now_utc


def day(value: str | None) -> str | None:
    return value[:10] if value else None


def week_start(value: str) -> str:
    d = date.fromisoformat(value)
    return (d - timedelta(days=d.weekday())).isoformat()


def _row(bucket: dict[str, Any], key: tuple[str, str, str, str, str, str]) -> dict[str, Any]:
    if key not in bucket:
        bucket[key] = {"vgm_container_count": 0, "vgm_weight_kg": 0.0, "released_bill_count": 0, "released_weight": 0.0, "released_volume": 0.0, "transshipment_container_count": 0, "transshipment_weight": 0.0, "transshipment_volume": 0.0, "planned_vessel_call_count": 0, "actual_vessel_call_count": 0, "arrival_delays": [], "departure_delays": []}
    return bucket[key]


def _plan_rows(store: TimeseriesStore, as_of: str | None):
    if not as_of:
        yield from store.conn.execute("SELECT * FROM fact_vessel_plan_snapshot")
        return
    cutoff = normalize_as_of(as_of)
    yield from store.conn.execute("""SELECT p.* FROM fact_vessel_plan_snapshot AS p
        JOIN (
            SELECT COALESCE(vessel_code, '') AS vessel_code,
                   COALESCE(voyage, '') AS voyage,
                   COALESCE(terminal_code, '') AS terminal_code,
                   COALESCE(direction, '') AS direction,
                   MAX(snapshot_time) AS snapshot_time
            FROM fact_vessel_plan_snapshot
            WHERE snapshot_time<=?
            GROUP BY COALESCE(vessel_code, ''), COALESCE(voyage, ''), COALESCE(terminal_code, ''), COALESCE(direction, '')
        ) AS latest
        ON latest.vessel_code=COALESCE(p.vessel_code, '')
        AND latest.voyage=COALESCE(p.voyage, '')
        AND latest.terminal_code=COALESCE(p.terminal_code, '')
        AND latest.direction=COALESCE(p.direction, '')
        AND latest.snapshot_time=p.snapshot_time""", (cutoff,))


def rebuild_daily(store: TimeseriesStore, start: str | None = None, end: str | None = None, as_of: str | None = None) -> int:
    as_of = normalize_as_of(as_of)
    cutoff_day = as_of[:10] if as_of else None
    if cutoff_day and (end is None or end > cutoff_day):
        end = cutoff_day
    rows: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    def allowed(value: str | None) -> bool:
        return bool(value and (start is None or value >= start) and (end is None or value <= end))

    vgm_rows = fact_rows_as_of(store, "fact_container_vgm", as_of) if as_of else store.conn.execute("SELECT * FROM fact_container_vgm")
    for r in vgm_rows:
        d = day(r["operator_time"])
        if not allowed(d):
            continue
        b = _row(rows, (d, r["terminal_code"] or "", r["direction"] or "", "", "", r["operator_code"] or ""))
        b["vgm_container_count"] += 1
        b["vgm_weight_kg"] += r["vgm_weight_kg"] or 0.0
    release_rows = fact_rows_as_of(store, "fact_cargo_release", as_of) if as_of else store.conn.execute("SELECT * FROM fact_cargo_release")
    for r in release_rows:
        d = day(r["pass_time"])
        if not allowed(d):
            continue
        b = _row(rows, (d, r["terminal_code"] or "", r["direction"] or "", "", "", ""))
        b["released_bill_count"] += int(bool(r["bill_no"]))
        b["released_weight"] += r["gross_weight"] or 0.0
        b["released_volume"] += r["cargo_volume"] or 0.0
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
    for r in _plan_rows(store, as_of):
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
                if delta >= 0:
                    b["arrival_delays"].append(delta)
            except ValueError:
                pass
        if r["etd"] and r["atd"]:
            try:
                delta = (datetime.fromisoformat(r["atd"]) - datetime.fromisoformat(r["etd"])).total_seconds() / 3600
                if delta >= 0:
                    b["departure_delays"].append(delta)
            except ValueError:
                pass

    target = "agg_flow_daily_asof" if as_of else "agg_flow_daily"
    if as_of:
        store.conn.execute("DELETE FROM agg_flow_daily_asof WHERE as_of_time=?", (as_of,))
    else:
        store.conn.execute("DELETE FROM agg_flow_daily" + ((" WHERE flow_date BETWEEN ? AND ?") if start and end else ""), ((start, end) if start and end else ()))
    for key, b in rows.items():
        av = sum(b["arrival_delays"]) / len(b["arrival_delays"]) if b["arrival_delays"] else None
        dv = sum(b["departure_delays"]) / len(b["departure_delays"]) if b["departure_delays"] else None
        if as_of:
            store.conn.execute("""INSERT OR REPLACE INTO agg_flow_daily_asof(as_of_time,flow_date,terminal_code,direction,route_key,cargo_group_key,vessel_operator,vgm_container_count,vgm_weight_kg,released_bill_count,released_weight,released_volume,transshipment_container_count,transshipment_weight,transshipment_volume,planned_vessel_call_count,actual_vessel_call_count,avg_arrival_delay_hours,avg_departure_delay_hours,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (as_of, key[0], key[1], key[2], key[3], key[4], key[5], b["vgm_container_count"], b["vgm_weight_kg"], b["released_bill_count"], b["released_weight"], b["released_volume"], b["transshipment_container_count"], b["transshipment_weight"], b["transshipment_volume"], b["planned_vessel_call_count"], b["actual_vessel_call_count"], av, dv, now_utc()))
        else:
            store.conn.execute("""INSERT OR REPLACE INTO agg_flow_daily(flow_date,terminal_code,direction,route_key,cargo_group_key,vessel_operator,vgm_container_count,vgm_weight_kg,released_bill_count,released_weight,released_volume,transshipment_container_count,transshipment_weight,transshipment_volume,planned_vessel_call_count,actual_vessel_call_count,avg_arrival_delay_hours,avg_departure_delay_hours,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (key[0], key[1], key[2], key[3], key[4], key[5], b["vgm_container_count"], b["vgm_weight_kg"], b["released_bill_count"], b["released_weight"], b["released_volume"], b["transshipment_container_count"], b["transshipment_weight"], b["transshipment_volume"], b["planned_vessel_call_count"], b["actual_vessel_call_count"], av, dv, now_utc()))
    store.conn.commit()
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
        b = grouped.setdefault(key, {"vgm_container_count": 0, "vgm_weight_kg": 0.0, "released_bill_count": 0, "released_weight": 0.0, "released_volume": 0.0, "transshipment_container_count": 0, "transshipment_weight": 0.0, "transshipment_volume": 0.0, "planned_vessel_call_count": 0, "actual_vessel_call_count": 0, "arrival": [], "departure": []})
        for field in ("vgm_container_count","vgm_weight_kg","released_bill_count","released_weight","released_volume","transshipment_container_count","transshipment_weight","transshipment_volume","planned_vessel_call_count","actual_vessel_call_count"):
            b[field] += r[field] or 0
        if r["avg_arrival_delay_hours"] is not None:
            b["arrival"].append(r["avg_arrival_delay_hours"])
        if r["avg_departure_delay_hours"] is not None:
            b["departure"].append(r["avg_departure_delay_hours"])
    if as_of:
        store.conn.execute("DELETE FROM agg_flow_weekly_asof WHERE as_of_time=?", (as_of,))
    else:
        store.conn.execute("DELETE FROM agg_flow_weekly")
    for key, b in grouped.items():
        if as_of:
            store.conn.execute("""INSERT INTO agg_flow_weekly_asof(as_of_time,week_start,terminal_code,direction,route_key,cargo_group_key,vessel_operator,vgm_container_count,vgm_weight_kg,released_bill_count,released_weight,released_volume,transshipment_container_count,transshipment_weight,transshipment_volume,planned_vessel_call_count,actual_vessel_call_count,avg_arrival_delay_hours,avg_departure_delay_hours,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (as_of, key[0], key[1], key[2], key[3], key[4], key[5], b["vgm_container_count"], b["vgm_weight_kg"], b["released_bill_count"], b["released_weight"], b["released_volume"], b["transshipment_container_count"], b["transshipment_weight"], b["transshipment_volume"], b["planned_vessel_call_count"], b["actual_vessel_call_count"], sum(b["arrival"]) / len(b["arrival"]) if b["arrival"] else None, sum(b["departure"]) / len(b["departure"]) if b["departure"] else None, now_utc()))
        else:
            store.conn.execute("""INSERT INTO agg_flow_weekly(week_start,terminal_code,direction,route_key,cargo_group_key,vessel_operator,vgm_container_count,vgm_weight_kg,released_bill_count,released_weight,released_volume,transshipment_container_count,transshipment_weight,transshipment_volume,planned_vessel_call_count,actual_vessel_call_count,avg_arrival_delay_hours,avg_departure_delay_hours,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (key[0], key[1], key[2], key[3], key[4], key[5], b["vgm_container_count"], b["vgm_weight_kg"], b["released_bill_count"], b["released_weight"], b["released_volume"], b["transshipment_container_count"], b["transshipment_weight"], b["transshipment_volume"], b["planned_vessel_call_count"], b["actual_vessel_call_count"], sum(b["arrival"]) / len(b["arrival"]) if b["arrival"] else None, sum(b["departure"]) / len(b["departure"]) if b["departure"] else None, now_utc()))
    store.conn.commit()
    return len(grouped)


def rebuild_gold(store: TimeseriesStore, start: str | None = None, end: str | None = None, as_of: str | None = None) -> dict[str, int]:
    as_of = normalize_as_of(as_of)
    daily = rebuild_daily(store, start, end, as_of=as_of)
    weekly_table = "agg_flow_weekly_asof" if as_of else "agg_flow_weekly"
    weekly_sql = f"SELECT COUNT(*) FROM {weekly_table}" + (" WHERE as_of_time=?" if as_of else "")
    weekly = store.conn.execute(weekly_sql, (as_of,) if as_of else ()).fetchone()[0]
    return {"daily": daily, "weekly": int(weekly), "as_of": as_of}
