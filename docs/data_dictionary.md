# 数据字典（已实现字段）

| 层 | 表 | 关键字段 |
|---|---|---|
| Bronze | `crawl_run` | 运行状态、请求数、错误数、配置 |
| Bronze | `crawl_checkpoint` | `job_name`、`partition_key`、`next_page` |
| Bronze | `raw_api_response` | 去敏请求指纹、页码、payload hash、原始 JSON |
| Bronze | `ingest_error` | 阶段、错误、请求/行 JSON |
| Silver | `fact_vessel_plan_snapshot` | 业务键、ETA/ETD、进箱窗口、快照时间 |
| Silver | `fact_container_vgm` | 箱号、船舶、重量、申报/接收时间 |
| Silver | `fact_cargo_release` | 提单、放行时间、码头、重量/体积 |
| Silver | `fact_transshipment` | 两程船舶、港口、箱量、重量/体积、货描 |
| Silver | `fact_container_event` | `UNKNOWN` 事件、原始动作代码、来源哈希 |
| Gold | `agg_flow_daily/weekly` | 统一日/ISO 周指标 |
| Gold | `mart_curve_series` | 曲线值、完整度、模型版本、来源 JSON |
| Gold | `feature_series_window` | 时序统计特征和完整度 |
| Gold | `cluster_run/assignment` | 聚类参数、样本数、离群、历史运行 |
| Gold | `change_point_event/anomaly_event/trend_snapshot` | 变点、异常、趋势状态 |

非法数值和解析失败写 NULL；原始值留在 `raw_json`，不强制转成 0 或当前时间。

进出门（CODECO）管线的 `gate_events` / `gate_voyages` 不属于上述分层，
逐字段说明、取值分布与数据质量陷阱见
[gate-events-fields.md](gate-events-fields.md)。
