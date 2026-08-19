"""Read-only report dashboard for NPEDI coverage, provenance, curves and clusters."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "npedi.sqlite"

TABLE_GROUPS = {
    "Bronze": [
        "crawl_run",
        "crawl_checkpoint",
        "raw_api_response",
        "ingest_error",
        "bronze_record",
        "schema_observation",
    ],
    "Silver": [
        "dim_vessel",
        "dim_terminal",
        "dim_route",
        "dim_cargo_group",
        "fact_vessel_plan_snapshot",
        "fact_container_vgm",
        "fact_cargo_release",
        "fact_transshipment",
        "fact_container_event",
        "fact_record_version",
    ],
    "Gold": [
        "agg_flow_daily",
        "agg_flow_weekly",
        "agg_gate_daily",
        "mart_curve_series",
        "feature_series_window",
        "cluster_run",
        "cluster_assignment",
        "change_point_event",
        "anomaly_event",
        "trend_snapshot",
    ],
    "CODECO": [
        "gate_voyages",
        "gate_events",
        "gate_history_candidate",
        "gate_history_candidate_month",
        "gate_history_rejected_pair",
    ],
}

TABLE_DESCRIPTIONS = {
    "crawl_run": "一次抓取任务的运行记录和结果状态",
    "crawl_checkpoint": "任务分区的分页断点和最后成功时间",
    "raw_api_response": "去敏请求指纹、页码、响应 hash 和原始 JSON",
    "ingest_error": "抓取、分页、规范化和响应校验错误",
    "bronze_record": "按接口与业务键保存的当前原始投影",
    "schema_observation": "接口字段首次/最近出现时间和样本类型",
    "fact_vessel_plan_snapshot": "船舶计划的可变快照",
    "fact_container_vgm": "VGM 重量事实",
    "fact_cargo_release": "放行提单、重量和体积事实",
    "fact_transshipment": "转运箱、路线、货类和重量事实",
    "fact_container_event": "容器事件规范化事实",
    "fact_record_version": "按观测时间保存的事实版本",
    "agg_flow_daily": "按日、码头、方向和业务维度聚合",
    "agg_flow_weekly": "按 ISO 周聚合的 Gold 指标",
    "agg_gate_daily": "CODECO 进出门日聚合",
    "mart_curve_series": "可视化曲线点、完整度和模型版本",
    "feature_series_window": "实体时间窗口特征，作为聚类输入",
    "cluster_run": "聚类算法、窗口、样本量和质量指标",
    "cluster_assignment": "实体到簇的分配及离群标记",
    "change_point_event": "曲线变点事件",
    "anomaly_event": "曲线异常事件",
    "trend_snapshot": "面向业务的趋势快照",
    "gate_voyages": "船舶/航次的进出门方向断点和总量",
    "gate_events": "以接口 id 为主键的 append-only CODECO 报文",
    "gate_history_candidate": "从计划事实发现的历史候选船舶/航次",
    "gate_history_candidate_month": "候选在哪些 ETA 月份有计划证据",
    "gate_history_rejected_pair": "不安全业务键及拒绝原因",
}

FIELD_DESCRIPTIONS = {
    "run_id": "本轮任务标识，用于关联原始响应或增量导出",
    "crawl_run_id": "原始响应对应的抓取运行标识",
    "job_name": "任务名称",
    "endpoint_name": "接口或数据端点名称",
    "mode": "backfill、incremental、snapshot 等采集模式",
    "status": "任务或候选状态",
    "started_at": "任务开始时间",
    "finished_at": "任务结束时间",
    "fetched_at": "接口响应抓取时间",
    "ingested_at": "规范化事实入库时间",
    "observed_at": "事实版本被系统观测到的时间",
    "event_time": "业务事件发生时间",
    "snapshot_time": "船舶计划快照时间",
    "as_of_time": "Gold 回溯结果的截止时间",
    "business_key_hash": "业务键的一向 hash，不保存敏感业务键原文",
    "record_hash": "规范化前/后的记录内容 hash",
    "source_record_hash": "事件源记录 hash",
    "raw_json": "接口原始行 JSON",
    "normalized_json": "用于回溯的规范化事实 JSON",
    "payload_hash": "整页响应内容 hash",
    "request_fingerprint": "接口和过滤条件的请求指纹",
    "page_num": "接口页码",
    "next_page": "下一次续跑页码",
    "observed_total": "接口报告的总行数",
    "vessel_code": "船舶编码",
    "vesselcode": "CODECO 船舶编码",
    "voyage": "航次号",
    "terminal_code": "码头编码",
    "direction": "业务方向",
    "eta": "预计到港时间",
    "etd": "预计离港时间",
    "ata": "实际到港时间",
    "atd": "实际离港时间",
    "ctnNo": "集装箱号",
    "container_no": "规范化后的集装箱号",
    "ctnGrossWeight": "CODECO 箱货总重，通常为公斤",
    "vgm_weight_kg": "VGM 重量，单位公斤",
    "msgReceiveTime": "CODECO 报文接收时间",
    "inGateTime": "进闸时间",
    "outGateTime": "出闸时间",
    "last_eta": "候选船舶/航次的最大 ETA 日期",
    "first_eta": "候选船舶/航次的最小 ETA 日期",
    "quality_flag": "曲线点的 complete、partial 等质量状态",
    "model_version": "衍生计算或模型版本",
    "data_completeness": "特征窗口的数据完整度",
    "silhouette_score": "聚类轮廓系数，越高通常表示簇间分离更好",
    "is_outlier": "是否被算法标记为离群点",
}


@st.cache_resource
def get_db() -> sqlite3.Connection:
    uri = DB_PATH.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@st.cache_data(ttl=60)
def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_db(), params=params)


def table_exists(table: str) -> bool:
    return bool(
        get_db()
        .execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        .fetchone()
    )


def table_count(table: str) -> int | None:
    if not table_exists(table):
        return None
    try:
        return int(get_db().execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def status_frame() -> pd.DataFrame:
    return query_df(
        """
        SELECT substr(c.last_eta,1,4) AS year, c.status, COUNT(*) AS count
        FROM gate_history_candidate AS c
        WHERE NOT EXISTS (
            SELECT 1 FROM gate_history_rejected_pair AS r
            WHERE r.vesselcode=c.vesselcode AND r.voyage=c.voyage
        )
        GROUP BY year, c.status
        ORDER BY year, c.status
        """
    )


def layer_rows() -> pd.DataFrame:
    rows = []
    for layer, tables in TABLE_GROUPS.items():
        for table in tables:
            rows.append(
                {
                    "layer": layer,
                    "table": table,
                    "description": TABLE_DESCRIPTIONS.get(table, ""),
                    "rows": table_count(table),
                }
            )
    return pd.DataFrame(rows)


def latest_run_frame() -> pd.DataFrame:
    return query_df(
        """
        SELECT id AS run_id, job_name, endpoint_name, mode, started_at, finished_at,
               status, request_count, row_count_raw, row_count_inserted,
               row_count_updated, error_count, error_summary
        FROM crawl_run
        ORDER BY started_at DESC
        LIMIT 40
        """
    )


def curve_types() -> list[str]:
    if not table_exists("mart_curve_series"):
        return []
    return [
        str(row[0])
        for row in get_db().execute(
            "SELECT DISTINCT curve_type FROM mart_curve_series ORDER BY curve_type"
        )
    ]


def curve_entities(curve_type: str, granularity: str) -> list[str]:
    return [
        str(row[0])
        for row in get_db().execute(
            """
            SELECT DISTINCT entity_key
            FROM mart_curve_series
            WHERE curve_type=? AND granularity=?
            ORDER BY entity_key
            LIMIT 500
            """,
            (curve_type, granularity),
        )
    ]


def dictionary_frame(selected_tables: Iterable[str], search: str) -> pd.DataFrame:
    rows = []
    needle = search.strip().lower()
    for table in selected_tables:
        if not table_exists(table):
            continue
        for column in get_db().execute(f'PRAGMA table_info("{table}")'):
            name = str(column["name"])
            description = FIELD_DESCRIPTIONS.get(name, "原始字段或内部计算字段；请结合 raw_json 和对应文档解释")
            if needle and needle not in f"{table} {name} {description}".lower():
                continue
            rows.append(
                {
                    "table": table,
                    "field": name,
                    "type": column["type"],
                    "nullable": not bool(column["notnull"]),
                    "key": "PK" if column["pk"] else "",
                    "description": description,
                }
            )
    return pd.DataFrame(rows)


st.set_page_config(
    page_title="NPEDI 报告 Dashboard",
    page_icon="N",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("NPEDI 数据回溯与衍生分析")
st.caption(
    "只读报告视图：回填进度、采集证据、as-of 回溯、Gold 曲线、聚类特征和字段字典"
)

with st.sidebar:
    st.subheader("报告控制")
    if st.button("刷新数据库快照", use_container_width=True):
        query_df.clear()
        st.rerun()
    st.caption(f"数据库：{DB_PATH.name}")
    st.caption("连接模式：SQLite read-only")

tabs = st.tabs(["总览", "回溯证据", "衍生曲线", "聚类特征", "字段字典"])

with tabs[0]:
    st.subheader("当前覆盖概览")
    status = status_frame()
    if status.empty:
        st.info("当前没有 CODECO 候选状态。")
    else:
        status["done"] = status["status"].isin(["complete", "empty"])
        total = int(status["count"].sum())
        pending = int(status.loc[~status["done"], "count"].sum())
        terminal = int(status.loc[status["done"], "count"].sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("候选船舶/航次", f"{total:,}")
        c2.metric("已终态", f"{terminal:,}")
        c3.metric("待回填/待探测", f"{pending:,}")
        c4.metric("终态比例", f"{terminal / total:.1%}" if total else "0%")

        year = status.pivot_table(
            index="year", columns="status", values="count", fill_value=0
        ).reset_index()
        for col in ("pending", "hit", "empty", "complete"):
            if col not in year:
                year[col] = 0
        year["total"] = year[["pending", "hit", "empty", "complete"]].sum(axis=1)
        year["done"] = year["complete"] + year["empty"]
        year["completion"] = year["done"] / year["total"].where(year["total"] > 0, 1)
        st.plotly_chart(
            px.bar(
                status,
                x="year",
                y="count",
                color="status",
                barmode="stack",
                title="CODECO 候选状态（empty 和 complete 都是终态）",
                labels={"year": "ETA 年份", "count": "候选数", "status": "状态"},
                color_discrete_map={
                    "complete": "#16803c",
                    "empty": "#94a3b8",
                    "hit": "#d97706",
                    "pending": "#dc2626",
                },
            ),
            use_container_width=True,
        )
        st.dataframe(
            year[
                ["year", "total", "complete", "empty", "hit", "pending", "completion"]
            ].sort_values("year", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("分层数据量")
    layers = layer_rows()
    st.dataframe(layers, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("为什么这些数据可以回溯")
    st.markdown(
        """
        **Bronze 证据链**：`crawl_run` 记录任务，`crawl_checkpoint` 记录分页断点，
        `raw_api_response` 保存请求指纹、页码、payload hash 和原始 JSON，
        `ingest_error` 保存失败上下文。

        **Silver 版本链**：变化事实写入 `fact_record_version`，按
        `business_key_hash + observed_at` 选择截止时间前最后一版。

        **Gold 隔离**：`*_asof` 表和 `model_version@as_of=...` 不覆盖当前结果。
        """
    )
    st.subheader("最近采集任务")
    runs = latest_run_frame()
    st.dataframe(runs, use_container_width=True, hide_index=True)

    st.subheader("指定时间点的回溯命令")
    as_of = st.text_input(
        "as-of 时间（ISO 8601）",
        value="2026-08-01T23:59:59+00:00",
        help="这里只展示命令，不在 Dashboard 内写库或重建结果。",
    )
    st.code(
        "\n".join(
            [
                f"python npedi.py aggregate --as-of {as_of}",
                f"python npedi.py build-curves --as-of {as_of}",
                f"python npedi.py cluster --entity terminal --curve-type vgm --as-of {as_of}",
            ]
        ),
        language="powershell",
    )

with tabs[2]:
    st.subheader("Gold 曲线与业务衍生指标")
    types = curve_types()
    if not types:
        st.info("还没有生成 mart_curve_series。先运行 aggregate 和 build-curves。")
    else:
        c1, c2, c3 = st.columns(3)
        selected_type = c1.selectbox("指标", types)
        granularity = c2.selectbox("粒度", ["week", "day"])
        entities = curve_entities(selected_type, granularity)
        selected_entity = c3.selectbox(
            "实体", entities, index=0 if entities else None
        )
        if selected_entity:
            curve = query_df(
                """
                SELECT time_bucket, value, quality_flag, source_json
                FROM mart_curve_series
                WHERE curve_type=? AND granularity=? AND entity_key=?
                ORDER BY time_bucket DESC
                LIMIT 260
                """,
                (selected_type, granularity, selected_entity),
            ).sort_values("time_bucket")
            if not curve.empty:
                fig = px.line(
                    curve,
                    x="time_bucket",
                    y="value",
                    markers=True,
                    color="quality_flag",
                    title=f"{selected_type} · {selected_entity} · {granularity}",
                    labels={"time_bucket": "时间", "value": "值"},
                )
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(curve.tail(30), use_container_width=True, hide_index=True)

    st.subheader("Gold 表预览")
    gold_table = st.selectbox(
        "选择 Gold 表",
        ["agg_flow_weekly", "agg_gate_daily", "feature_series_window"],
    )
    if table_exists(gold_table):
        preview = query_df(f'SELECT * FROM "{gold_table}" LIMIT 200')
        st.dataframe(preview, use_container_width=True, hide_index=True)
        st.download_button(
            "下载当前预览 CSV",
            preview.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{gold_table}_preview.csv",
            mime="text/csv",
        )

with tabs[3]:
    st.subheader("聚类输入和结果")
    if table_exists("cluster_run"):
        runs = query_df(
            """
            SELECT cluster_run_id, algorithm, feature_version, window_start,
                   window_end, entity_type, sample_count, cluster_count,
                   silhouette_score, created_at
            FROM cluster_run
            ORDER BY created_at DESC
            LIMIT 30
            """
        )
        st.dataframe(runs, use_container_width=True, hide_index=True)
        if not runs.empty:
            selected_run = st.selectbox(
                "选择一次聚类运行",
                runs["cluster_run_id"].tolist(),
                format_func=lambda value: str(value)[:12],
            )
            run = runs[runs["cluster_run_id"] == selected_run].iloc[0]
            assignments = query_df(
                """
                SELECT a.entity_key, a.cluster_id, a.is_outlier,
                       f.mean, f.trend_slope, f.data_completeness
                FROM cluster_assignment AS a
                LEFT JOIN feature_series_window AS f
                  ON f.entity_type=? AND f.entity_key=a.entity_key
                 AND f.feature_version=?
                WHERE a.cluster_run_id=?
                ORDER BY a.cluster_id, a.entity_key
                """,
                (
                    run["entity_type"],
                    run["feature_version"],
                    selected_run,
                ),
            )
            if not assignments.empty:
                st.plotly_chart(
                    px.scatter(
                        assignments,
                        x="mean",
                        y="trend_slope",
                        color=assignments["cluster_id"].astype(str),
                        symbol="is_outlier",
                        hover_name="entity_key",
                        title="聚类特征投影：均值 × 趋势斜率",
                        labels={"color": "cluster_id"},
                    ),
                    use_container_width=True,
                )
                st.dataframe(assignments, use_container_width=True, hide_index=True)
    else:
        st.info("还没有 cluster_run。先生成 Gold 曲线和 feature_series_window。")

with tabs[4]:
    st.subheader("动态字段字典")
    st.caption(
        "字段来自当前 SQLite schema；描述对关键业务字段做了补充，未知字段请回看对应表的 raw_json。"
    )
    selected_layer = st.multiselect(
        "数据层",
        list(TABLE_GROUPS),
        default=list(TABLE_GROUPS),
    )
    selected_tables = [
        table
        for layer in selected_layer
        for table in TABLE_GROUPS[layer]
    ]
    search = st.text_input("搜索表名、字段名或说明")
    dictionary = dictionary_frame(selected_tables, search)
    st.dataframe(dictionary, use_container_width=True, hide_index=True)
    st.download_button(
        "下载字段字典 CSV",
        dictionary.to_csv(index=False).encode("utf-8-sig"),
        file_name="npedi_field_dictionary.csv",
        mime="text/csv",
    )

