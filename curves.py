"""Curve materialization from Gold aggregates, without price data."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from backtest import model_version_for_as_of, normalize_as_of
from timeseries import TimeseriesStore, now_utc

METRICS = {
    "planned_demand": ("planned_vessel_call_count", "planned_vessel_calls"),
    "vgm": ("vgm_weight_kg", "vgm_weight_kg"),
    "cargo_release": ("released_weight", "released_weight"),
    "transshipment": ("transshipment_weight", "transshipment_weight"),
    "arrival_delay": ("avg_arrival_delay_hours", "arrival_delay_hours"),
    "departure_delay": ("avg_departure_delay_hours", "departure_delay_hours"),
    "container_vgm_count": ("vgm_container_count", "unique_container_count"),
    "released_bill_count": ("released_bill_count", "released_bill_count"),
    "transshipment_container_count": ("transshipment_container_count", "transshipment_container_count"),
}


def _z(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    return (value - mean) / std if std else 0.0


def _revision_rows(store: TimeseriesStore, as_of: str | None = None) -> list[tuple[str, str, str, float]]:
    out = []
    previous: dict[str, str] = {}
    sql = "SELECT vessel_code, voyage, terminal_code, direction, snapshot_time, record_hash FROM fact_vessel_plan_snapshot"
    args = (as_of,) if as_of else ()
    if as_of:
        sql += " WHERE snapshot_time<=?"
    sql += " ORDER BY business_key_hash, snapshot_time"
    for r in store.conn.execute(sql, args):
        identity = (r["vessel_code"] or "", r["voyage"] or "", r["terminal_code"] or "", r["direction"] or "")
        old = previous.get(identity)
        if old is not None and old != r["record_hash"]:
            out.append((r["snapshot_time"][:10], r["terminal_code"] or "", "", 1.0))
        previous[identity] = r["record_hash"]
    return out

def build_curves(store: TimeseriesStore, *, granularity: str = "day", model_version: str = "v1", spi_weights: dict[str, float] | None = None, as_of: str | None = None) -> int:
    as_of = normalize_as_of(as_of)
    effective_model_version = model_version_for_as_of(model_version, as_of)
    table = ("agg_flow_weekly_asof" if granularity == "week" else "agg_flow_daily_asof") if as_of else ("agg_flow_weekly" if granularity == "week" else "agg_flow_daily")
    time_col = "week_start" if granularity == "week" else "flow_date"
    series: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(lambda: defaultdict(list))
    source_sql = f"SELECT * FROM {table}" + (" WHERE as_of_time=?" if as_of else "")
    for row in store.conn.execute(source_sql, (as_of,) if as_of else ()):
        key = (row[time_col], row["terminal_code"] or "UNKNOWN", row["direction"] or "")
        for curve_type, (field, _) in METRICS.items():
            if row[field] is not None:
                series[key][curve_type].append(float(row[field]))
    for bucket, terminal, direction, value in _revision_rows(store, as_of):
        if granularity == "week":
            d = date.fromisoformat(bucket)
            bucket = (d - timedelta(days=d.weekday())).isoformat()
        series[(bucket, terminal or "UNKNOWN", direction)]["plan_revision"].append(value)

    grouped: dict[tuple[str, str, str], list[tuple[str, float]]] = defaultdict(list)
    for (bucket, terminal, direction), metrics in series.items():
        for curve_type, values in metrics.items():
            if values:
                grouped[(curve_type, terminal, direction)].append((bucket, statistics.fmean(values)))

    store.conn.execute("DELETE FROM mart_curve_series WHERE model_version=?", (effective_model_version,))
    count = 0
    for (curve_type, terminal, direction), points in grouped.items():
        points.sort()
        buckets = [x[0] for x in points]
        expected = 1
        if len(buckets) > 1:
            if granularity == "week":
                expected = ((date.fromisoformat(buckets[-1]) - date.fromisoformat(buckets[0])).days // 7) + 1
            else:
                expected = (date.fromisoformat(buckets[-1]) - date.fromisoformat(buckets[0])).days + 1
        curve_id = f"{curve_type}:terminal:{terminal}:{direction}:{granularity}"
        raw_values = [x[1] for x in points]
        for i, (bucket, value) in enumerate(points):
            moving = statistics.fmean(raw_values[max(0, i - 3):i + 1])
            quality = "complete" if len(buckets) == expected else "partial"
            source = {"expected_buckets": expected, "observed_buckets": len(buckets), "completeness_ratio": len(buckets) / expected, "moving_average_4": moving, "terminal": terminal, "direction": direction}
            store.conn.execute("""INSERT OR REPLACE INTO mart_curve_series(curve_id,curve_type,entity_type,entity_key,granularity,time_bucket,value,lower_bound,upper_bound,quality_flag,model_version,computed_at,source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (curve_id, curve_type, "terminal", f"{terminal}:{direction}", granularity, bucket, value, None, None, quality, effective_model_version, now_utc(), json.dumps(source, ensure_ascii=False, sort_keys=True)))
            count += 1

    # Missing inputs are omitted and the remaining SPI weights are renormalized.
    by_entity: dict[tuple[str, str], list[tuple[str, dict[str, float | None]]]] = defaultdict(list)
    for (bucket, terminal, direction), metrics in series.items():
        by_entity[(terminal, direction)].append((bucket, {name: (statistics.fmean(values) if values else None) for name, (field, _) in METRICS.items() for values in [metrics.get(name, [])]}))
    weights = spi_weights or {"vgm": .30, "cargo_release": .20, "transshipment": .15, "arrival_delay": .15, "departure_delay": .10, "plan_revision": .10}
    for (terminal, direction), rows in by_entity.items():
        rows.sort()
        all_values = {metric: [float(d[metric]) for _, d in rows if d.get(metric) is not None] for metric in weights}
        for bucket, values in rows:
            available = {metric: value for metric, value in values.items() if metric in weights and value is not None}
            if not available:
                continue
            total_weight = sum(weights[m] for m in available)
            contributions = {m: weights[m] / total_weight * _z(float(v), all_values[m]) for m, v in available.items()}
            spi = sum(contributions.values())
            curve_id = f"pressure_index:terminal:{terminal}:{direction}:{granularity}"
            source = {"contributions": contributions, "weights": weights, "missing_components": [m for m in weights if m not in available]}
            store.conn.execute("""INSERT OR REPLACE INTO mart_curve_series(curve_id,curve_type,entity_type,entity_key,granularity,time_bucket,value,lower_bound,upper_bound,quality_flag,model_version,computed_at,source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (curve_id, "pressure_index", "terminal", f"{terminal}:{direction}", granularity, bucket, spi, None, None, "complete", effective_model_version, now_utc(), json.dumps(source, ensure_ascii=False, sort_keys=True)))
            count += 1
    store.conn.commit()
    return count
