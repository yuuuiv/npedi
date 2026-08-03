"""Plotly CDN HTML renderer; it does not require the Plotly package at build time."""
from __future__ import annotations

import html
import json
from pathlib import Path

from timeseries import TimeseriesStore, now_utc


def render_curves(store: TimeseriesStore, output: Path, *, model_version: str = "v1") -> Path:
    rows = store.conn.execute("SELECT curve_id,curve_type,entity_key,granularity,time_bucket,value,quality_flag,source_json,model_version FROM mart_curve_series WHERE model_version=? ORDER BY curve_id,time_bucket", (model_version,)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["curve_id"], {"name": row["curve_id"], "curve_type": row["curve_type"], "entity_key": row["entity_key"], "granularity": row["granularity"], "x": [], "y": [], "quality": []})
        grouped[row["curve_id"]]["x"].append(row["time_bucket"])
        grouped[row["curve_id"]]["y"].append(row["value"])
        grouped[row["curve_id"]]["quality"].append(row["quality_flag"])
    traces = [{"x": value["x"], "y": value["y"], "mode": "lines+markers", "name": value["name"], "customdata": value["quality"]} for value in grouped.values()]
    metadata = {"generated_at": now_utc(), "model_version": model_version, "curve_count": len(traces), "source": "NPEDI normalized Gold tables"}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    document = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>NPEDI Timeseries Curves</title><script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script></head><body><h1>NPEDI Timeseries Curves</h1><pre id=\"metadata\"></pre><div id=\"chart\" style=\"width:100%;height:720px\"></div><script>const metadata=__METADATA__;const traces=__TRACES__;document.getElementById('metadata').textContent=JSON.stringify(metadata,null,2);Plotly.newPlot('chart',traces,{title:'NPEDI curves',xaxis:{title:'time'},yaxis:{title:'value'},hovermode:'x unified'});</script></body></html>""".replace("__METADATA__", json.dumps(metadata, ensure_ascii=False)).replace("__TRACES__", json.dumps(traces, ensure_ascii=False))
    output.write_text(document, encoding="utf-8")
    return output
