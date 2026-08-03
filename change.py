"""Deterministic change-point, anomaly and trend snapshots."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

from timeseries import TimeseriesStore, now_utc


def _groups(store: TimeseriesStore):
    groups = defaultdict(list)
    for row in store.conn.execute("SELECT * FROM mart_curve_series ORDER BY entity_key,curve_type,time_bucket"):
        groups[(row["entity_key"], row["curve_type"])].append(row)
    return groups


def detect_changes(store: TimeseriesStore, *, threshold: float = 2.0, model_version: str = "v1") -> dict[str, int]:
    changes = anomalies = 0
    for (entity, curve), rows in _groups(store).items():
        values = [float(row["value"]) for row in rows if row["value"] is not None]
        if len(values) >= 5:
            std = statistics.pstdev(values) or 1.0
            for i in range(2, len(values) - 2):
                before, after = statistics.fmean(values[:i]), statistics.fmean(values[i:])
                score = abs(after - before) / std
                if score >= threshold:
                    store.conn.execute("INSERT INTO change_point_event(entity_key,curve_type,change_time,change_score,before_level,after_level,before_trend,after_trend,algorithm,model_version,detected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (entity, curve, rows[i]["time_bucket"], score, before, after, 0.0, 0.0, "level_shift", model_version, now_utc()))
                    changes += 1
        if len(values) >= 3:
            mean, std = statistics.fmean(values), statistics.pstdev(values) or 1.0
            for row in rows:
                if row["value"] is not None and abs(float(row["value"]) - mean) / std >= threshold:
                    store.conn.execute("INSERT INTO anomaly_event(entity_key,curve_type,time_bucket,anomaly_score,method,model_version,detected_at,raw_json) VALUES(?,?,?,?,?,?,?,?)", (entity, curve, row["time_bucket"], abs(float(row["value"]) - mean) / std, "zscore", model_version, now_utc(), row["source_json"] or "{}"))
                    anomalies += 1
    store.conn.commit()
    return {"change_points": changes, "anomalies": anomalies}


def _growth(values: list[float], n: int) -> float | None:
    if len(values) <= n:
        return None
    base = values[-n - 1]
    return (values[-1] - base) / abs(base) if base else None


def snapshot_trends(store: TimeseriesStore, *, model_version: str = "v1") -> int:
    count = 0
    for (entity, curve), rows in _groups(store).items():
        values = [float(row["value"]) for row in rows if row["value"] is not None]
        if not values:
            continue
        g1, g4, g13 = _growth(values, 1), _growth(values, 4), _growth(values, 13)
        volatility = statistics.pstdev(values[-13:]) if len(values) > 1 else 0.0
        quality = statistics.fmean(1.0 if row["quality_flag"] == "complete" else .5 for row in rows)
        if quality < .8:
            state = "data_insufficient"
        elif g4 is not None and g4 > .1:
            state = "growth"
        elif g4 is not None and g4 < -.1:
            state = "decline"
        elif volatility > abs(statistics.fmean(values)) if statistics.fmean(values) else False:
            state = "high_volatility"
        else:
            state = "stable"
        store.conn.execute("INSERT OR REPLACE INTO trend_snapshot(entity_key,curve_type,as_of_time,trend_state,growth_1w,growth_4w,growth_13w,volatility_13w,change_point_recent,anomaly_score,data_quality,explanation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (entity, curve, rows[-1]["time_bucket"], state, g1, g4, g13, volatility, 0, None, quality, json.dumps({"model_version": model_version, "sample_count": len(values)}, ensure_ascii=False)))
        count += 1
    store.conn.commit()
    return count
