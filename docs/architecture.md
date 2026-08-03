# NPEDI 时序架构

```text
已验证 API → crawl_run/raw_api_response/checkpoint → bronze_record
→ fact_* / dim_* → agg_flow_daily/weekly → mart_curve_series
→ feature_series_window → cluster_run/assignment → trend_snapshot
```

现有 npp/CODECO 管线继续使用 `store.py` 的旧表；`TimeseriesStore` 只追加时序迁移，不重建旧表。时间统一为 UTC ISO 字符串，`raw_json` 保留来源字段。未知字段只进入原始层和 schema observation；单箱历史 `operateId` 不解释，事件类型固定为 `UNKNOWN`。

范围明确排除价格、运价和所有 `/matou/*` 路径。Gold 压力指数只表示业务流量/延迟压力，不表示价格。

## 迁移

- 直接运行：`TimeseriesStore(db_path)` 自动按 `migrations/*.sql` 应用。
- Alembic：`alembic upgrade head`，默认 `sqlite:///npedi.sqlite`；现有 Windows SQLite 为当前运行基线。
- `migrations/001_timeseries_bronze.sql`：Bronze、规范计划/进箱快照。
- `migrations/002_silver_gold.sql`：维表、事实、聚合、曲线、分析结果。
