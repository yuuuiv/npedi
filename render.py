"""Decision-oriented Plotly dashboard renderer (no Plotly Python dependency)."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

from timeseries import TimeseriesStore, now_utc


LABELS = {
    "planned_demand": "计划靠港船次",
    "released_bill_count": "放行提单数",
    "cargo_release": "放行货重（吨，规则 v1）",
    "gate_in_teu": "进闸 TEU（CODECO 覆盖）",
    "gate_out_teu": "出闸 TEU（CODECO 覆盖）",
    "container_vgm_count": "VGM 箱数（远程增强覆盖）",
    "transshipment_container_count": "中转箱数（当前仅队列样本）",
    "arrival_delay": "平均到港延误（小时）",
    "departure_delay": "平均离港延误（小时）",
    "pressure_index": "港口压力指数",
}


def _aggregate(
    rows: list[dict[str, Any]],
    metric: str,
    reducer: Callable[[list[float]], float],
) -> list[tuple[str, float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row["curve_type"] == metric and row["value"] is not None:
            value = float(row["value"])
            if math.isfinite(value):
                grouped[row["time_bucket"]].append(value)
    return [(bucket, reducer(values)) for bucket, values in sorted(grouped.items()) if values]


def _trace(points: list[tuple[str, float]], name: str, **extra: Any) -> dict[str, Any]:
    return {
        "x": [point[0] for point in points],
        "y": [point[1] for point in points],
        "mode": "lines+markers",
        "name": name,
        "hovertemplate": "%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>",
        **extra,
    }


def _latest(points: list[tuple[str, float]], periods: int = 4) -> float | None:
    return fmean(point[1] for point in points[-periods:]) if points else None


def _format_card(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "暂无"
    return f"{value:,.{decimals}f}"


def render_curves(
    store: TimeseriesStore,
    output: Path,
    *,
    model_version: str = "v1",
    granularity: str | None = None,
    anchor_date: str | None = None,
    recent_weeks: int = 156,
) -> Path:
    granularity = granularity or "week"
    anchor = date.fromisoformat(anchor_date[:10]) if anchor_date else datetime.now(timezone.utc).date()
    start = anchor - timedelta(weeks=max(4, recent_weeks))
    end = anchor + timedelta(days=7)
    db_rows = store.conn.execute(
        """SELECT curve_id,curve_type,entity_key,granularity,time_bucket,value,
                  quality_flag,source_json,model_version
           FROM mart_curve_series
           WHERE model_version=? AND granularity=? AND time_bucket BETWEEN ? AND ?
           ORDER BY curve_type,entity_key,time_bucket""",
        (model_version, granularity, start.isoformat(), end.isoformat()),
    ).fetchall()
    rows = [dict(row) for row in db_rows]

    planned = _aggregate(rows, "planned_demand", sum)
    released = _aggregate(rows, "released_bill_count", sum)
    cargo_weight_tonnes = [
        (bucket, value / 1000.0)
        for bucket, value in _aggregate(rows, "cargo_release", sum)
    ]
    observed_gate_in = _aggregate(rows, "gate_in_teu", sum)
    observed_gate_out = _aggregate(rows, "gate_out_teu", sum)
    # Historical CODECO coverage is currently known to be directory-biased.
    # Keep the misleading cross-period line hidden until a separate coverage
    # audit explicitly marks it comparable; raw aggregates remain in the DB.
    gate_history_validated = False
    if store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone():
        flag = store.conn.execute(
            "SELECT value FROM meta WHERE key='gate_history_coverage_validated'"
        ).fetchone()
        gate_history_validated = bool(flag and str(flag[0]).strip().lower() in {
            "1", "true", "yes", "on",
        })
    gate_in = observed_gate_in if gate_history_validated else []
    gate_out = observed_gate_out if gate_history_validated else []
    vgm_count = _aggregate(rows, "container_vgm_count", sum)
    transshipment_count = _aggregate(rows, "transshipment_container_count", sum)
    arrival = _aggregate(
        [row for row in rows if row["value"] is None or 0 <= float(row["value"]) <= 720],
        "arrival_delay",
        fmean,
    )
    departure = _aggregate(
        [row for row in rows if row["value"] is None or 0 <= float(row["value"]) <= 720],
        "departure_delay",
        fmean,
    )

    entity_scores: dict[str, float] = defaultdict(float)
    for row in rows:
        if row["curve_type"] == "planned_demand" and row["value"] is not None:
            entity_scores[row["entity_key"]] += float(row["value"])
    top_entities = [key for key, _ in sorted(entity_scores.items(), key=lambda item: item[1], reverse=True)[:8]]

    by_metric_entity: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        key = (row["curve_type"], row["entity_key"])
        if row["entity_key"] in top_entities and row["value"] is not None:
            value = float(row["value"])
            if math.isfinite(value):
                by_metric_entity[key].append((row["time_bucket"], value))

    traffic_traces = [
        _trace(planned, LABELS["planned_demand"], line={"color": "#2563eb", "width": 3}),
        _trace(
            released,
            LABELS["released_bill_count"],
            yaxis="y2",
            line={"color": "#f59e0b", "width": 2},
        ),
    ]
    gate_traces = (
        [
            _trace(gate_in, LABELS["gate_in_teu"], line={"color": "#0891b2", "width": 2}),
            _trace(gate_out, LABELS["gate_out_teu"], line={"color": "#ea580c", "width": 2}),
        ]
        if gate_history_validated else []
    )
    cargo_traces = [
        _trace(cargo_weight_tonnes, LABELS["cargo_release"], line={"color": "#16a34a", "width": 2}),
    ]
    sample_traces = [
        _trace(vgm_count, LABELS["container_vgm_count"], line={"color": "#0f766e"}),
        _trace(transshipment_count, LABELS["transshipment_container_count"], line={"color": "#7c3aed"}),
    ]
    delay_traces = [
        _trace(arrival, LABELS["arrival_delay"], line={"color": "#dc2626"}),
        _trace(departure, LABELS["departure_delay"], line={"color": "#9333ea"}),
    ]
    terminal_traces = [
        _trace(by_metric_entity[("planned_demand", entity)], entity)
        for entity in top_entities
        if by_metric_entity[("planned_demand", entity)]
    ]
    pressure_traces = [
        _trace(by_metric_entity[("pressure_index", entity)], entity)
        for entity in top_entities
        if by_metric_entity[("pressure_index", entity)]
    ]

    catalog_row = store.conn.execute(
        """SELECT COUNT(*),
                  SUM(vgm_status='complete'), SUM(history_status='complete'),
                  SUM(gate_event_count)
           FROM container_enrichment_state"""
    ).fetchone()
    catalog_total = int(catalog_row[0] or 0)
    gate_quality = store.conn.execute(
        """SELECT MIN(flow_date),MAX(flow_date),
                  SUM(in_gate_container_count+out_gate_container_count),
                  SUM(in_teu_known_count+out_teu_known_count)
           FROM agg_gate_daily"""
    ).fetchone()
    metadata = {
        "generated_at": now_utc(),
        "model_version": model_version,
        "granularity": granularity,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "displayed_points": len(rows),
        "top_entities": top_entities,
        "coverage": {
            "catalog_containers": catalog_total,
            "vgm_queried": int(catalog_row[1] or 0),
            "history_api_queried": int(catalog_row[2] or 0),
            "gate_source_rows": int(catalog_row[3] or 0),
            "gate_start": gate_quality[0],
            "gate_end": gate_quality[1],
            "teu_size_coverage": (
                float(gate_quality[3] or 0) / float(gate_quality[2])
                if gate_quality[2] else 0.0
            ),
            "gate_history_validated": gate_history_validated,
            "gate_observed_points": len(observed_gate_in) + len(observed_gate_out),
        },
    }
    cards = {
        "planned": _format_card(_latest(planned)),
        "released": _format_card(_latest(released)),
        "arrival": _format_card(_latest(arrival), 1),
        "containers": _format_card(float(catalog_total)),
    }
    payload = {
        "metadata": metadata,
        "cards": cards,
        "traffic": traffic_traces,
        "gate": gate_traces,
        "cargo": cargo_traces,
        "samples": sample_traces,
        "delays": delay_traces,
        "terminals": terminal_traces,
        "pressure": pressure_traces,
    }

    document = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NPEDI 港口运行仪表板</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{margin:0;background:#f4f7fb;color:#172033;font-family:Inter,"Microsoft YaHei",sans-serif}
main{max-width:1500px;margin:auto;padding:24px}.title{display:flex;justify-content:space-between;align-items:end;gap:20px}
h1{margin:0 0 6px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.muted{color:#64748b;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}.card,.panel,.notice{background:white;border:1px solid #e5eaf1;border-radius:14px;box-shadow:0 4px 18px #1e293b0b}
.card{padding:17px}.card .value{font-size:26px;font-weight:700;margin-top:7px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.panel{padding:16px;margin-bottom:16px}.chart{height:390px}
.wide{grid-column:1/-1}.notice{padding:16px;margin:0 0 16px;border-left:5px solid #f59e0b}.notice ul{margin:8px 0 0;padding-left:20px}
@media(max-width:900px){.cards,.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style></head><body><main>
<div class="title"><div><h1>NPEDI 港口运行仪表板</h1><div class="muted" id="window"></div></div><div class="muted" id="generated"></div></div>
<div class="cards">
 <div class="card"><div class="muted">近 4 周平均计划船次</div><div class="value" id="card-planned"></div></div>
 <div class="card"><div class="muted">近 4 周平均放行提单</div><div class="value" id="card-released"></div></div>
 <div class="card"><div class="muted">近 4 周平均到港延误</div><div class="value"><span id="card-arrival"></span> 小时</div></div>
 <div class="card"><div class="muted">全量箱目录</div><div class="value" id="card-containers"></div></div>
</div>
<div class="notice"><strong>覆盖边界</strong><ul>
 <li>默认只展示最近 156 周，已排除公元 1015、3023、4012 等明显脏日期。</li>
 <li>延误超过 30 天的 ETA/ATA 或 ETD/ATD 配对不进入图表。</li>
 <li>闸口图只代表 NPEDI 的船×航次 CODECO 接口覆盖，不是官方“全港吞吐量”。当前目录 <span id="coverage-catalog"></span> 个箱号、<span id="coverage-gate"></span> 条源记录。</li>
 <li>计划事实表 2023-01 至 2026-07 有 292,871 个船×航次，原始快照目录只命中 9,476 个；历史补爬和覆盖验收完成前暂停展示闸口跨期曲线，绝不能把旧月份低值或 2026-07 突增解释为业务变化。TEU 箱型识别覆盖率为 <span id="coverage-teu"></span>。</li>
 <li>VGM 与单箱 API 轨迹是独立远程增强：已查询 <span id="coverage-vgm"></span> / <span id="coverage-history"></span> 个箱号；未完成前不外推全港重量。</li>
 <li>cargo-release 主业务量仍使用提单数。独立货重图按 <code>cargo-weight-kg-v1</code> 将原值核验为 kg 后换算成吨，原值和规则版本均保留。</li>
</ul></div>
<div class="grid">
 <section class="panel wide"><h2>业务量趋势</h2><div class="muted">计划靠港船次与放行提单分属左右坐标轴</div><div id="traffic" class="chart"></div></section>
 <section class="panel wide"><h2>CODECO 闸口历史趋势（覆盖验收后开放）</h2><div class="muted">原始聚合仍保留；当前历史候选尚在单 worker 补爬，为防止目录覆盖差异被误读成吞吐趋势，暂不绘制跨期折线。</div><div id="gate" class="chart"></div></section>
 <section class="panel"><h2>到离港延误</h2><div id="delays" class="chart"></div></section>
 <section class="panel"><h2>放行货重</h2><div class="muted">单位：吨；独立于主图提单数，规则 cargo-weight-kg-v1</div><div id="cargo" class="chart"></div></section>
 <section class="panel"><h2>远程增强样本</h2><div class="muted">VGM 未全量完成前仅展示实际查询结果，不外推全港</div><div id="samples" class="chart"></div></section>
 <section class="panel"><h2>主要码头计划船次</h2><div id="terminals" class="chart"></div></section>
 <section class="panel"><h2>主要码头压力指数</h2><div id="pressure" class="chart"></div></section>
</div>
</main><script>
const data=__PAYLOAD__, cfg={responsive:true,displaylogo:false};
document.getElementById('window').textContent=`数据窗口：${data.metadata.window_start} 至 ${data.metadata.window_end}（${data.metadata.granularity}）`;
document.getElementById('generated').textContent=`生成时间：${data.metadata.generated_at}`;
for(const k of ['planned','released','arrival','containers']) document.getElementById(`card-${k}`).textContent=data.cards[k];
document.getElementById('coverage-catalog').textContent=data.metadata.coverage.catalog_containers.toLocaleString();
document.getElementById('coverage-gate').textContent=data.metadata.coverage.gate_source_rows.toLocaleString();
document.getElementById('coverage-teu').textContent=`${(data.metadata.coverage.teu_size_coverage*100).toFixed(2)}%`;
document.getElementById('coverage-vgm').textContent=`${data.metadata.coverage.vgm_queried.toLocaleString()} / ${data.metadata.coverage.catalog_containers.toLocaleString()}`;
document.getElementById('coverage-history').textContent=`${data.metadata.coverage.history_api_queried.toLocaleString()} / ${data.metadata.coverage.catalog_containers.toLocaleString()}`;
const base={paper_bgcolor:'white',plot_bgcolor:'white',margin:{l:58,r:35,t:25,b:45},hovermode:'x unified',legend:{orientation:'h',y:1.12},xaxis:{gridcolor:'#eef2f7'},yaxis:{gridcolor:'#eef2f7',rangemode:'tozero'}};
Plotly.newPlot('traffic',data.traffic,{...base,yaxis:{...base.yaxis,title:'船次'},yaxis2:{title:'提单数',overlaying:'y',side:'right',rangemode:'tozero'}},cfg);
const gateLayout={...base,yaxis:{...base.yaxis,title:'TEU'}};
if(!data.metadata.coverage.gate_history_validated){gateLayout.annotations=[{text:'历史覆盖尚未验收，曲线暂不展示',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{size:18,color:'#b45309'}}]}
Plotly.newPlot('gate',data.gate,gateLayout,cfg);
Plotly.newPlot('delays',data.delays,{...base,yaxis:{...base.yaxis,title:'小时'}},cfg);
Plotly.newPlot('cargo',data.cargo,{...base,yaxis:{...base.yaxis,title:'吨'}},cfg);
Plotly.newPlot('samples',data.samples,{...base,yaxis:{...base.yaxis,title:'箱数'}},cfg);
Plotly.newPlot('terminals',data.terminals,{...base,yaxis:{...base.yaxis,title:'计划船次'}},cfg);
Plotly.newPlot('pressure',data.pressure,{...base,yaxis:{...base.yaxis,title:'标准化指数',rangemode:'normal'},shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:0,y1:0,line:{color:'#94a3b8',dash:'dot'}}]},cfg);
</script></body></html>'''.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return output
