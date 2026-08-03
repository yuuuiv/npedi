"""Versioned feature windows and clustering adapters."""
from __future__ import annotations

import json
import math
import statistics
import uuid
from datetime import date
from typing import Any

from timeseries import TimeseriesStore, now_utc

FEATURE_NAMES = ("mean", "median", "std", "coefficient_of_variation", "trend_slope", "growth_4w", "growth_13w", "peak_to_median", "zero_ratio", "seasonal_strength", "autocorrelation_lag_1", "autocorrelation_lag_7_or_4", "arrival_delay_mean", "arrival_delay_p90", "transshipment_share", "plan_revision_frequency", "data_completeness")


def _slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    xbar = (len(values) - 1) / 2
    ybar = statistics.fmean(values)
    den = sum((i - xbar) ** 2 for i in range(len(values)))
    return sum((i - xbar) * (v - ybar) for i, v in enumerate(values)) / den if den else 0.0


def _growth(values: list[float], periods: int) -> float | None:
    if len(values) <= periods:
        return None
    base = values[-periods - 1]
    return (values[-1] - base) / abs(base) if base else None


def _autocorrelation(values: list[float], lag: int) -> float | None:
    if len(values) <= lag + 1:
        return None
    a, b = values[:-lag], values[lag:]
    am, bm = statistics.fmean(a), statistics.fmean(b)
    den = math.sqrt(sum((x - am) ** 2 for x in a) * sum((x - bm) ** 2 for x in b))
    return sum((x - am) * (y - bm) for x, y in zip(a, b)) / den if den else 0.0


def build_feature_windows(store: TimeseriesStore, *, curve_type: str = "vgm", granularity: str = "week", entity_type: str = "terminal", feature_version: str = "v1", min_completeness: float = 0.0) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[str, float, float]]] = {}
    for row in store.conn.execute("SELECT entity_key,time_bucket,value,quality_flag,source_json FROM mart_curve_series WHERE curve_type=? AND granularity=? AND entity_type=? ORDER BY entity_key,time_bucket", (curve_type, granularity, entity_type)):
        if row["value"] is not None:
            source = json.loads(row["source_json"] or "{}")
            grouped.setdefault(row["entity_key"], []).append((row["time_bucket"], float(row["value"]), float(source.get("completeness_ratio", 0.0))))
    result = []
    for entity_key, points in grouped.items():
        if not points:
            continue
        values = [p[1] for p in points]
        completeness = statistics.fmean(p[2] for p in points)
        if completeness < min_completeness:
            continue
        median = statistics.median(values)
        std = statistics.pstdev(values) if len(values) > 1 else 0.0
        features = {"mean": statistics.fmean(values), "median": median, "std": std, "coefficient_of_variation": std / abs(statistics.fmean(values)) if statistics.fmean(values) else 0.0, "trend_slope": _slope(values), "growth_4w": _growth(values, 4), "growth_13w": _growth(values, 13), "peak_to_median": max(values) / median if median else None, "zero_ratio": sum(v == 0 for v in values) / len(values), "seasonal_strength": 0.0, "autocorrelation_lag_1": _autocorrelation(values, 1), "autocorrelation_lag_7_or_4": _autocorrelation(values, 4 if len(values) < 14 else 7), "arrival_delay_mean": statistics.fmean(values) if curve_type == "arrival_delay" else None, "arrival_delay_p90": sorted(values)[min(len(values) - 1, math.ceil(len(values) * .9) - 1)] if curve_type == "arrival_delay" else None, "transshipment_share": 1.0 if curve_type == "transshipment" else 0.0, "plan_revision_frequency": 0.0, "data_completeness": completeness}
        row = {"entity_type": entity_type, "entity_key": entity_key, "window_start": points[0][0], "window_end": points[-1][0], "frequency": granularity, "feature_version": feature_version, **features, "feature_json": json.dumps(features, ensure_ascii=False, sort_keys=True)}
        fields = tuple(row.keys())
        values_sql = tuple(row.values())
        store.conn.execute("INSERT OR REPLACE INTO feature_series_window(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")", values_sql)
        result.append(row)
    store.conn.commit()
    return result


def _vectors(rows: list[dict[str, Any]]) -> tuple[list[list[float]], list[str]]:
    names = [x for x in FEATURE_NAMES if x != "data_completeness"]
    vectors = [[float(row.get(name) if row.get(name) is not None else 0.0) for name in names] for row in rows]
    return vectors, names


def cluster_features(store: TimeseriesStore, rows: list[dict[str, Any]], *, algorithm: str = "hierarchical", algorithm_version: str = "v1", normalization_method: str = "standard_scaler", min_completeness: float = .8) -> dict[str, Any]:
    eligible = [row for row in rows if (row.get("data_completeness") or 0.0) >= min_completeness]
    run_id = str(uuid.uuid4())
    window_start = min((r["window_start"] for r in eligible), default="")
    window_end = max((r["window_end"] for r in eligible), default="")
    if len(eligible) < 2:
        store.conn.execute("INSERT INTO cluster_run(cluster_run_id,algorithm,algorithm_version,feature_version,window_start,window_end,entity_type,parameters_json,normalization_method,sample_count,cluster_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, algorithm, algorithm_version, rows[0]["feature_version"] if rows else "", window_start, window_end, rows[0]["entity_type"] if rows else "unknown", json.dumps({"min_completeness": min_completeness}), normalization_method, len(eligible), 0, now_utc()))
        store.conn.commit()
        return {"cluster_run_id": run_id, "sample_count": len(eligible), "cluster_count": 0, "assignments": []}
    try:
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import silhouette_score
        scaler = StandardScaler()
        vectors, _ = _vectors(eligible)
        matrix = scaler.fit_transform(vectors)
        if algorithm == "hierarchical":
            from sklearn.cluster import AgglomerativeClustering
            n_clusters = min(max(2, len(eligible) // 2), len(eligible))
            labels = AgglomerativeClustering(n_clusters=n_clusters).fit_predict(matrix)
        elif algorithm == "hdbscan":
            try:
                import hdbscan
                labels = hdbscan.HDBSCAN(min_cluster_size=max(2, min(5, len(eligible)))).fit_predict(matrix)
            except ImportError as exc:
                raise RuntimeError("hdbscan dependency is required for algorithm=hdbscan") from exc
        else:
            raise ValueError("algorithm must be hierarchical or hdbscan")
        unique = sorted(set(int(x) for x in labels if int(x) >= 0))
        score = float(silhouette_score(matrix, labels)) if len(unique) >= 2 else None
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required for clustering") from exc
    store.conn.execute("INSERT INTO cluster_run(cluster_run_id,algorithm,algorithm_version,feature_version,window_start,window_end,entity_type,parameters_json,normalization_method,sample_count,cluster_count,silhouette_score,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, algorithm, algorithm_version, eligible[0]["feature_version"], window_start, window_end, eligible[0]["entity_type"], json.dumps({"min_completeness": min_completeness}), normalization_method, len(eligible), len(unique), score, now_utc()))
    assignments = []
    for row, label in zip(eligible, labels):
        assignment = (run_id, row["entity_key"], int(label) if int(label) >= 0 else None, None, None, int(label) < 0, None, now_utc())
        store.conn.execute("INSERT INTO cluster_assignment(cluster_run_id,entity_key,cluster_id,membership_probability,distance_to_prototype,is_outlier,business_label,assigned_at) VALUES(?,?,?,?,?,?,?,?)", assignment)
        assignments.append({"entity_key": row["entity_key"], "cluster_id": assignment[2], "is_outlier": assignment[5]})
    store.conn.commit()
    return {"cluster_run_id": run_id, "sample_count": len(eligible), "cluster_count": len(unique), "assignments": assignments}
