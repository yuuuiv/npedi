"""NPEDI Data Dashboard - Streamlit App"""
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(page_title="NPEDI 数据全览", layout="wide", initial_sidebar_state="expanded")

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "npedi.sqlite"


# Database connection.  Read-only on purpose: this page exposes a free-form SQL
# box, and the target is the live multi-hundred-GB main database.  mode=ro plus
# query_only makes a stray DELETE/DROP fail instead of destroying data, and the
# busy timeout keeps the page usable while a crawler holds the write lock.
@st.cache_resource
def get_db():
    uri = DB_PATH.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn

def query_df(sql, params=()):
    """Execute query and return DataFrame"""
    db = get_db()
    return pd.read_sql_query(sql, db, params=params)

# ============================================================================
# 标题和刷新控件
# ============================================================================
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    st.title("🎯 NPEDI 数据全览")
with col2:
    if st.button("🔄 刷新数据", use_container_width=True):
        st.rerun()
with col3:
    st.caption(f"更新于: {datetime.now().strftime('%H:%M:%S')}")

st.divider()

# ============================================================================
# Tab 1: 回填进度总览
# ============================================================================
tab1, tab2, tab3, tab4 = st.tabs(["📊 回填进度", "🎯 聚类分析", "📈 实时监控", "🔧 数据查询"])

with tab1:
    st.subheader("容器富化队列进度")

    # VGM & History 概览
    col1, col2, col3 = st.columns(3)

    with col1:
        vgm = query_df("SELECT COUNT(*) as cnt, vgm_status FROM container_enrichment_state GROUP BY vgm_status")
        vgm_complete = vgm[vgm['vgm_status'] == 'complete']['cnt'].sum()
        vgm_pending = vgm[vgm['vgm_status'] == 'pending']['cnt'].sum()
        vgm_invalid = vgm[vgm['vgm_status'] == 'invalid']['cnt'].sum()

        st.metric("VGM 完成", f"{vgm_complete:,}")
        fig = go.Figure(data=[go.Pie(
            labels=['完成', '待处理', '无效'],
            values=[vgm_complete, vgm_pending, vgm_invalid],
            hole=0.3,
            marker=dict(colors=['#2ecc71', '#f39c12', '#e74c3c'])
        )])
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        history = query_df("SELECT COUNT(*) as cnt, history_status FROM container_enrichment_state GROUP BY history_status")
        hist_complete = history[history['history_status'] == 'complete']['cnt'].sum()
        hist_pending = history[history['history_status'] == 'pending']['cnt'].sum()
        hist_invalid = history[history['history_status'] == 'invalid']['cnt'].sum()

        st.metric("Container History 完成", f"{hist_complete:,}")
        fig = go.Figure(data=[go.Pie(
            labels=['完成', '待处理', '无效'],
            values=[hist_complete, hist_pending, hist_invalid],
            hole=0.3,
            marker=dict(colors=['#2ecc71', '#f39c12', '#e74c3c'])
        )])
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with col3:
        codeco = query_df("""
            SELECT status, COUNT(*) as cnt
            FROM gate_history_candidate
            GROUP BY status
        """)
        codeco_complete = codeco[codeco['status'] == 'complete']['cnt'].sum()
        codeco_hit = codeco[codeco['status'] == 'hit']['cnt'].sum()
        codeco_empty = codeco[codeco['status'] == 'empty']['cnt'].sum()
        codeco_pending = codeco[codeco['status'] == 'pending']['cnt'].sum()

        st.metric("CODECO 候选探测", f"{codeco_complete + codeco_hit + codeco_empty:,}")
        fig = go.Figure(data=[go.Pie(
            labels=['完成/命中/空', '待处理'],
            values=[codeco_complete + codeco_hit + codeco_empty, codeco_pending],
            hole=0.3,
            marker=dict(colors=['#2ecc71', '#f39c12'])
        )])
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # CODECO 年份分层统计
    st.subheader("CODECO 历史按年份分层")

    codeco_year = query_df("""
        SELECT
            SUBSTR(last_eta, 1, 4) as year,
            status,
            COUNT(*) as cnt
        FROM gate_history_candidate
        GROUP BY year, status
        ORDER BY year DESC, status
    """)

    # 透视表
    pivot = codeco_year.pivot_table(index='year', columns='status', values='cnt', fill_value=0)
    st.dataframe(pivot, use_container_width=True)

    # 进度条可视化
    st.write("**按年份的完成度**")
    for _, row in pivot.iterrows():
        year = str(row.name)
        total = row.sum()
        done = row.get('complete', 0) + row.get('hit', 0) + row.get('empty', 0)
        pending = row.get('pending', 0)
        pct = int(done * 100 / total) if total > 0 else 0

        col1, col2, col3 = st.columns([1, 3, 1])
        with col1:
            st.write(f"**{year}**")
        with col2:
            st.progress(pct / 100, text=f"{pct}% ({done:,}/{total:,})")
        with col3:
            st.caption(f"{pending:,} pending")

# ============================================================================
# Tab 2: 聚类分析
# ============================================================================
with tab2:
    st.subheader("聚类运行历史")

    # 最近的聚类运行
    cluster_runs = query_df("""
        SELECT
            cluster_run_id,
            algorithm,
            entity_type,
            sample_count,
            cluster_count,
            ROUND(silhouette_score, 3) as silhouette_score,
            created_at
        FROM cluster_run
        ORDER BY created_at DESC
        LIMIT 20
    """)

    if not cluster_runs.empty:
        st.dataframe(cluster_runs, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("最新聚类详情")

        latest_run_id = cluster_runs.iloc[0]['cluster_run_id']

        col1, col2 = st.columns(2)

        with col1:
            st.metric("聚类簇数", int(cluster_runs.iloc[0]['cluster_count']))
            st.metric("样本数", int(cluster_runs.iloc[0]['sample_count']))

        with col2:
            silhouette = cluster_runs.iloc[0]['silhouette_score']
            if silhouette is not None:
                st.metric("Silhouette Score", f"{silhouette:.3f}")
            st.metric("算法", cluster_runs.iloc[0]['algorithm'])

        st.divider()

        # 聚类分配详情
        st.write("**簇分配概览**")
        assignments = query_df("""
            SELECT
                cluster_id,
                COUNT(*) as entity_count,
                SUM(CASE WHEN is_outlier THEN 1 ELSE 0 END) as outlier_count
            FROM cluster_assignment
            WHERE cluster_run_id = ?
            GROUP BY cluster_id
            ORDER BY cluster_id
        """, (latest_run_id,))

        st.dataframe(assignments, use_container_width=True, hide_index=True)

        # 离群点表
        st.write("**检测到的离群点**")
        outliers = query_df("""
            SELECT
                entity_key,
                cluster_id
            FROM cluster_assignment
            WHERE cluster_run_id = ? AND is_outlier = 1
            ORDER BY entity_key
        """, (latest_run_id,))

        if not outliers.empty:
            st.dataframe(outliers, use_container_width=True, hide_index=True)

            # 下载离群点
            csv = outliers.to_csv(index=False)
            st.download_button(
                "📥 下载离群点列表",
                csv,
                "outliers.csv",
                "text/csv"
            )
        else:
            st.info("未检测到离群点")
    else:
        st.info("暂无聚类运行记录")

# ============================================================================
# Tab 3: 实时监控
# ============================================================================
with tab3:
    st.subheader("最近24小时运行统计")

    # 获取最近运行
    recent_runs = query_df("""
        SELECT
            run_id,
            kind,
            started_at,
            finished_at,
            requests_made,
            rows_seen,
            status,
            ROUND((julianday(finished_at) - julianday(started_at)) * 24 * 60, 1) as duration_min
        FROM sync_runs
        WHERE kind LIKE '%history%' OR kind = 'gate_history_backfill'
        ORDER BY run_id DESC
        LIMIT 30
    """)

    col1, col2, col3, col4 = st.columns(4)

    if not recent_runs.empty:
        with col1:
            success_count = len(recent_runs[recent_runs['status'] == 'ok'])
            st.metric("成功运行", success_count)

        with col2:
            total_requests = recent_runs[recent_runs['status'] == 'ok']['requests_made'].sum()
            st.metric("总请求数", f"{total_requests:,}")

        with col3:
            total_rows = recent_runs[recent_runs['status'] == 'ok']['rows_seen'].sum()
            st.metric("总数据行", f"{total_rows:,}")

        with col4:
            avg_duration = recent_runs[recent_runs['status'] == 'ok']['duration_min'].mean()
            st.metric("平均耗时(分钟)", f"{avg_duration:.1f}")

    st.divider()
    st.write("**运行日志**")
    st.dataframe(recent_runs, use_container_width=True, hide_index=True)

    # 下载最近运行日志
    csv = recent_runs.to_csv(index=False)
    st.download_button(
        "📥 下载运行日志",
        csv,
        "sync_runs.csv",
        "text/csv"
    )

# ============================================================================
# Tab 4: 自助数据查询
# ============================================================================
with tab4:
    st.subheader("SQL 查询工具")

    # 预设查询
    preset_queries = {
        "VGM 状态统计": "SELECT vgm_status, COUNT(*) as cnt FROM container_enrichment_state GROUP BY vgm_status ORDER BY cnt DESC",
        "History 状态统计": "SELECT history_status, COUNT(*) as cnt FROM container_enrichment_state GROUP BY history_status ORDER BY cnt DESC",
        "CODECO 候选状态": "SELECT status, COUNT(*) as cnt FROM gate_history_candidate GROUP BY status ORDER BY cnt DESC",
        "最新聚类信息": "SELECT cluster_run_id, algorithm, sample_count, cluster_count, silhouette_score, created_at FROM cluster_run ORDER BY created_at DESC LIMIT 5",
        "运行性能统计": """
            SELECT
                kind,
                COUNT(*) as run_count,
                AVG(requests_made) as avg_requests,
                AVG(CAST((julianday(finished_at) - julianday(started_at)) * 24 * 60 AS FLOAT)) as avg_duration_min
            FROM sync_runs
            WHERE finished_at IS NOT NULL AND status = 'ok'
            GROUP BY kind
        """,
        "无效容器列表": "SELECT container_no, vgm_status, history_status FROM container_enrichment_state WHERE (vgm_status = 'invalid' OR history_status = 'invalid') LIMIT 100",
    }

    selected_query = st.selectbox("选择预设查询", list(preset_queries.keys()))

    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("执行预设查询"):
            st.session_state.query = preset_queries[selected_query]

    st.divider()

    # 自定义查询
    st.write("**自定义 SQL 查询**")
    default_query = st.session_state.get("query", preset_queries[selected_query])
    sql_query = st.text_area("输入 SQL 查询", value=default_query, height=200)

    if st.button("执行查询", use_container_width=True):
        try:
            result_df = query_df(sql_query)
            st.success(f"✓ 查询成功，返回 {len(result_df)} 行")

            col1, col2 = st.columns([3, 1])
            with col2:
                csv = result_df.to_csv(index=False)
                st.download_button(
                    "📥 下载 CSV",
                    csv,
                    "query_result.csv",
                    "text/csv",
                    use_container_width=True
                )

            st.dataframe(result_df, use_container_width=True)

        except Exception as e:
            st.error(f"❌ 查询失败: {str(e)}")

# ============================================================================
# 侧边栏：数据字典和帮助
# ============================================================================
with st.sidebar:
    st.subheader("📚 数据字典")

    with st.expander("VGM 状态"):
        st.write("""
        - **pending**: 待查询
        - **complete**: 已成功获取VGM数据
        - **error**: 查询失败
        - **invalid**: 容器号不符合ISO 6346标准，无需查询
        """)

    with st.expander("CODECO 候选状态"):
        st.write("""
        - **pending**: 待探测（检查是否存在数据）
        - **hit**: 发现有数据（可进行完整回填）
        - **empty**: 服务端查不到数据
        - **complete**: 已完成回填
        """)

    with st.expander("聚类结果"):
        st.write("""
        - **cluster_id**: 所属簇号（-1表示离群点）
        - **is_outlier**: 是否为离群点
        - **silhouette_score**: 聚类紧密度评分 [-1, 1]，越高越好
        """)

    st.divider()
    st.subheader("💡 使用提示")
    st.write("""
    1. **回填进度** 标签查看三项大任务的进度
    2. **聚类分析** 查看最新的聚类结果和离群点
    3. **实时监控** 观察最近24小时的运行性能
    4. **数据查询** 用SQL自助查询，支持CSV导出
    """)

    st.divider()
    st.info("💾 数据库: npedi.sqlite (~140 GB)")
