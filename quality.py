"""Data-quality report for Bronze/Silver ingestion."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from timeseries import TimeseriesStore, now_utc

EXPECTED_FIELDS = {
    "vessel_plan": {"vesselUnCode", "vesselEnName", "vesselCnName", "terminal", "voyage", "vesselDirect", "tradeFlag", "ctnStartTime", "ctnEndTime", "customCloseTime", "portCloseTime", "eta", "etd", "ata", "atd", "etanchor", "atanchor", "lastPortCode", "nextPortCode", "berthReference", "status", "published", "publishTime"},
    "container_notice": {"vesselcode", "vesselename", "voyage", "matou", "ioflag", "ctnstart", "ctnend", "ediports", "revtime", "vesselowner"},
}


def _table_count(store: TimeseriesStore, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def quality_report(store: TimeseriesStore) -> dict[str, Any]:
    report: dict[str, Any] = {"generated_at": now_utc(), "endpoints": {}, "facts": {}, "schema_drift": {}}
    for endpoint in ("vessel_plan", "container_notice", "vgm", "cargo_release", "transshipment", "container_history"):
        raw = [r for r in store.conn.execute("SELECT raw_json FROM bronze_record WHERE endpoint_name=?", (endpoint,))]
        report["endpoints"][endpoint] = {"unique_records": len(raw), "empty_time": 0, "invalid_numeric": 0, "earliest_event": None, "latest_event": None}
        times = [r[0] for r in store.conn.execute("SELECT event_time FROM bronze_record WHERE endpoint_name=? AND event_time IS NOT NULL", (endpoint,))]
        if times:
            report["endpoints"][endpoint]["earliest_event"] = min(times)
            report["endpoints"][endpoint]["latest_event"] = max(times)
    for table in ("fact_vessel_plan_snapshot", "fact_container_vgm", "fact_cargo_release", "fact_transshipment", "fact_container_event"):
        report["facts"][table] = {"rows": _table_count(store, table), "duplicate_business_keys": 0}
    vgm_bad_time = int(store.conn.execute("SELECT COUNT(*) FROM fact_container_vgm WHERE operator_time IS NULL AND raw_json LIKE '%operatetime%'").fetchone()[0])
    vgm_bad_num = 0
    for row in store.conn.execute("SELECT raw_json,vgm_weight_kg FROM fact_container_vgm"):
        raw = json.loads(row[0])
        if str(raw.get("vgmGrossWeight", "")).strip() and row[1] is None:
            vgm_bad_num += 1
    report["endpoints"]["vgm"]["empty_time"] = vgm_bad_time
    report["endpoints"]["vgm"]["invalid_numeric"] = vgm_bad_num
    report["facts"]["fact_container_event"]["unknown_action_count"] = int(store.conn.execute("SELECT COUNT(*) FROM fact_container_event WHERE event_type='UNKNOWN'").fetchone()[0])
    for endpoint, expected in EXPECTED_FIELDS.items():
        actual = {r[0] for r in store.conn.execute("SELECT field_name FROM schema_observation WHERE endpoint_name=?", (endpoint,))}
        report["schema_drift"][endpoint] = {"new_fields": sorted(actual - expected), "missing_expected": sorted(expected - actual)}
    page_rows = store.conn.execute("SELECT endpoint_name,request_fingerprint,page_num,payload_hash FROM raw_api_response ORDER BY endpoint_name,request_fingerprint,page_num").fetchall()
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    repeated = 0
    for row in page_rows:
        key = (row[0], row[1])
        if row[3] in seen[key]:
            repeated += 1
        seen[key].add(row[3])
    report["pagination"] = {"pages": len(page_rows), "repeated_pages": repeated, "repeat_rate": repeated / len(page_rows) if page_rows else 0.0}
    report["summary"] = {"total_fact_rows": sum(v["rows"] for v in report["facts"].values()), "quality_gate": repeated == 0}
    return report


def write_quality_report(store: TimeseriesStore, output: Path) -> dict[str, Any]:
    report = quality_report(store)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# NPEDI Data Quality", "", f"Generated: {report['generated_at']}", "", "| Endpoint | Unique records | Invalid numeric | Empty time |", "|---|---:|---:|---:|"]
    for endpoint, value in report["endpoints"].items():
        lines.append(f"| {endpoint} | {value['unique_records']} | {value['invalid_numeric']} | {value['empty_time']} |")
    lines.extend(["", f"Pagination repeat rate: {report['pagination']['repeat_rate']}", f"Quality gate: {report['summary']['quality_gate']}"])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
