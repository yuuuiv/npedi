# NPEDI 历史数据采集、时序分析与曲线系统架构设计

**文档版本**：v1.0  
**适用范围**：NPEDI Onesite 工作台公开可访问业务接口  
**明确排除**：价格数据、运价预测、码头专属业务接口  
**目标读者**：数据工程、后端开发、数据分析、算法与可视化开发人员

---

## 1. 背景与目标

现有 `NPEDI-API-REFERENCE.md` 已完成对多个真实接口的参数和响应结构验证，可支持构建船舶计划、进箱公告、VGM、散杂货放行、内支中转和单箱历史等数据集。

本项目的目标不是简单抓取接口并保存 JSON，而是建立一套可以长期运行的历史数据系统：

1. 首次运行时完成历史数据回填；
2. 后续按固定周期增量抓取和修订已有数据；
3. 将不同接口映射到统一的船舶、航次、箱、货物、路线和时间维度；
4. 构造计划需求、申报需求、放行货流、中转货流、运行延迟和供需压力等时序曲线；
5. 对路线、货类、码头、船公司等对象进行可解释的时序聚类；
6. 在新增数据进入后更新曲线、异常、变点和聚类状态；
7. 保留原始响应、抓取过程和数据血缘，以便审计和重新计算。

---

## 2. 范围

### 2.1 本期纳入接口

#### A. 船舶计划

- `/vessel/plan/selectContainerDynamicPlan`
- 用途：历史和未来船舶计划、ETA/ETD、ATA/ATD、进箱窗口、截关和截港时间、上一港和下一港。
- 数据类型：会被反复修订的快照型数据。
- 采集模式：定时快照 + 业务主键 Upsert + 版本保留。

#### B. 每日进箱公告

- `/vessel/dzyjh/getlist`
- 用途：船舶、航次、码头、进箱开始和截止时间、挂靠港、方向、船公司。
- 数据类型：计划意图和未来需求窗口。
- 采集模式：全量分页快照 + 内容哈希去重。

#### C. VGM

- `/ctnvgm/getlist`
- 用途：箱号、航次、船公司、毛重、申报时间、码头接收时间和处理结果。
- 数据类型：接近实际出口需求的申报事件。
- 采集模式：历史回填 + 重叠窗口增量。

#### D. 散杂货海关放行

- `/ediCustptrSZ/getEdiCustptrSz`
- 用途：船名、航次、方向、提单、放行时间、码头、货物体积和毛重。
- 数据类型：大规模历史货物流量。
- 注意：接口数据量大，服务端过滤效果存疑，必须进行客户端过滤和去重。
- 采集模式：分页全量回填 + 页级检查点 + 哈希去重 + 周期性重扫。

#### E. 内支中转

- `/npp/nzx/getNzwPageResult`
- 用途：第一程和第二程船舶、航次、装卸港、中转港、箱号、提单、件数、重量、体积、货描、开航和截关日期。
- 数据类型：货物—路线—数量事实数据。
- 采集模式：全量分页 + 重叠窗口或全量重扫。

#### F. 单箱历史

- `/ediContainerlog/getEdiContainerlog/{container_no}`
- 用途：补充同一箱在多个码头、航次和年份中的动作历史。
- 数据类型：按箱号查询的事件增强数据。
- 采集模式：只对已在核心数据集中出现的新箱号或高价值箱号进行异步增强，不作为全量入口。

### 2.2 可选增强接口

以下接口可以在核心版本稳定后接入：

- `/track/getContainerTrackInfo`：补充进门、出门、装船、卸船、海关放行等生命周期状态；
- `/scoedor/getEdiScoedor`：补充堆存开始时间；
- `/transferlogSearch/getMessageLogStatistic`：构造本公司范围内的报文流量和系统负载指标；
- `/cusretrec/getEdiCusretrec`：补充舱单回执和处理结果；
- `/costcoHistory/getEdiCostco`：补充装箱单历史。

### 2.3 明确排除

1. 所有 `/matou/*` 码头专属业务接口；
2. 任何依赖码头商务授权才能访问的数据；
3. 货物成交价、海运费、箱价和价格预测；
4. 未验证参数结构且当前会导致“系统异常”的接口；
5. 通过高频请求规避服务端限制或绕过权限的实现。

---

## 3. 设计原则

### 3.1 原始数据永不覆盖

任何接口响应都先进入原始层。规范化表和聚合表可以重建，原始响应必须可追溯。

### 3.2 区分三种时间

每条记录尽可能保留：

- `event_time`：业务事件实际发生时间；
- `source_update_time`：来源系统记录更新时间；
- `ingested_at`：本系统抓取时间。

### 3.3 区分 Append、Upsert 和 Snapshot

- Append：真实事件，例如 VGM 成功申报、海关放行；
- Upsert：同一业务对象可能被修订，例如船舶计划；
- Snapshot：接口只返回某一时点状态，需要由持续抓取创造历史。

### 3.4 不依赖 `totalPages`

所有分页接口以 `total`、当前页记录数和页码计算是否结束。禁止使用响应中的 `totalPages` 作为终止条件。

### 3.5 未知字段不丢弃

规范化表仅提取已理解字段，但必须保留 `raw_json`。新增字段通过 Schema Drift 检查发现后再进入正式模型。

### 3.6 聚类必须可解释

聚类结果必须同时保存：

- 输入时间窗口；
- 特征定义；
- 标准化方式；
- 模型和参数；
- 聚类原型；
- 样本数；
- 质量指标；
- 业务解释标签；
- 模型版本。

---

## 4. 总体架构

```text
NPEDI API
   │
   ▼
Crawler / Scheduler
   │
   ├── 认证与 Token 检查
   ├── 分页、重试、限速
   ├── Checkpoint 与断点续传
   └── 原始响应落库
   │
   ▼
Bronze 原始层
   ├── crawl_run
   ├── crawl_checkpoint
   ├── raw_api_response
   └── ingest_error
   │
   ▼
Silver 规范层
   ├── dim_vessel
   ├── dim_terminal
   ├── dim_route
   ├── dim_cargo_group
   ├── fact_vessel_plan_snapshot
   ├── fact_container_vgm
   ├── fact_cargo_release
   ├── fact_transshipment
   └── fact_container_event
   │
   ▼
Gold 分析层
   ├── agg_flow_daily
   ├── agg_flow_weekly
   ├── agg_route_weekly
   ├── mart_curve_series
   ├── feature_series_window
   ├── cluster_run
   ├── cluster_assignment
   ├── change_point_event
   ├── anomaly_event
   └── trend_snapshot
   │
   ▼
API / Notebook / Dashboard
   ├── 需求曲线
   ├── 货流曲线
   ├── 延迟曲线
   ├── 聚类原型
   ├── 状态迁移
   └── 历史相似窗口
```

---

## 5. 推荐技术栈

以下是实现建议，不是来源文档中的既有约束。

- Python 3.12；
- HTTP 客户端：复用现有 `client.py`，外层增加统一重试、限速和结构化日志；
- 数据库：PostgreSQL 16；
- 迁移：SQLAlchemy 2 + Alembic；
- 数据处理：Polars 优先，Pandas 作为兼容；
- 调度：开发期使用 CLI + Windows Task Scheduler/Cron，正式环境可切换 Prefect 或 Airflow；
- 时序和聚类：
  - scikit-learn；
  - tslearn；
  - hdbscan；
  - ruptures；
  - stumpy；
- 可视化：Plotly；
- API：FastAPI；
- 测试：pytest；
- 配置：Pydantic Settings；
- 日志：structlog 或标准库 JSON 日志。

数据库也可以替换为 SQLite 进行本地原型，但正式历史回填和多表分析建议使用 PostgreSQL。

---

## 6. 项目目录

```text
project/
├─ NPEDI-API-REFERENCE.md
├─ client.py
├─ pyproject.toml
├─ alembic.ini
├─ .env.example
├─ src/
│  └─ npedi_pipeline/
│     ├─ config.py
│     ├─ logging.py
│     ├─ cli.py
│     ├─ db/
│     │  ├─ base.py
│     │  ├─ models_bronze.py
│     │  ├─ models_silver.py
│     │  ├─ models_gold.py
│     │  └─ repositories/
│     ├─ crawlers/
│     │  ├─ base.py
│     │  ├─ vessel_plan.py
│     │  ├─ container_notice.py
│     │  ├─ vgm.py
│     │  ├─ cargo_release.py
│     │  ├─ transshipment.py
│     │  └─ container_history.py
│     ├─ parsers/
│     │  ├─ common.py
│     │  ├─ datetime_parser.py
│     │  ├─ numeric_parser.py
│     │  └─ cargo_normalizer.py
│     ├─ pipelines/
│     │  ├─ backfill.py
│     │  ├─ incremental.py
│     │  ├─ normalize.py
│     │  ├─ aggregate.py
│     │  └─ recompute.py
│     ├─ analytics/
│     │  ├─ features.py
│     │  ├─ curves.py
│     │  ├─ clustering.py
│     │  ├─ changepoints.py
│     │  ├─ anomalies.py
│     │  └─ lead_lag.py
│     ├─ visualization/
│     │  ├─ demand.py
│     │  ├─ flow.py
│     │  ├─ clusters.py
│     │  └─ dashboard.py
│     └─ api/
│        ├─ main.py
│        └─ routes/
├─ migrations/
├─ tests/
│  ├─ fixtures/
│  ├─ unit/
│  ├─ integration/
│  └─ data_quality/
├─ scripts/
│  ├─ backfill.ps1
│  ├─ incremental.ps1
│  └─ rebuild_gold.ps1
├─ notebooks/
├─ data/
│  └─ samples/
└─ docs/
   ├─ architecture.md
   ├─ data_dictionary.md
   ├─ endpoint_mapping.md
   └─ operations.md
```

---

## 7. Bronze 原始层表设计

### 7.1 `crawl_run`

记录每次任务运行。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| job_name | text | 任务名 |
| endpoint_name | text | 逻辑接口名 |
| mode | text | backfill/incremental/rescan/enrichment |
| started_at | timestamptz | 开始时间 |
| finished_at | timestamptz | 结束时间 |
| status | text | running/success/partial/failed |
| request_count | bigint | 请求数 |
| row_count_raw | bigint | 原始记录数 |
| row_count_inserted | bigint | 新增数 |
| row_count_updated | bigint | 更新数 |
| error_count | bigint | 错误数 |
| config_json | jsonb | 本次参数 |
| error_summary | text | 错误摘要 |

索引：

```sql
(job_name, started_at desc)
(endpoint_name, status, started_at desc)
```

### 7.2 `crawl_checkpoint`

支持分页断点续传。

| 字段 | 类型 | 说明 |
|---|---|---|
| job_name | text | 任务名 |
| partition_key | text | 日期、月份、航次或全量分区 |
| next_page | integer | 下一页 |
| observed_total | bigint | 最近一次 total |
| last_success_at | timestamptz | 最近成功时间 |
| cursor_json | jsonb | 其他游标信息 |
| updated_at | timestamptz | 更新时间 |

唯一键：

```sql
(job_name, partition_key)
```

### 7.3 `raw_api_response`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigserial | 主键 |
| crawl_run_id | UUID | 关联任务 |
| endpoint_name | text | 接口名 |
| request_fingerprint | text | 去敏后的请求哈希 |
| page_num | integer | 页码 |
| business_partition | text | 业务分区 |
| response_code | integer | 业务 code |
| response_msg | text | 业务 msg |
| http_status | integer | HTTP 状态 |
| payload_hash | text | 响应内容哈希 |
| raw_json | jsonb | 原始响应 |
| fetched_at | timestamptz | 抓取时间 |

唯一键建议：

```sql
(endpoint_name, request_fingerprint, page_num, payload_hash)
```

### 7.4 `ingest_error`

保存失败请求、解析失败和数据质量错误，禁止只写日志后丢失。

---

## 8. Silver 规范层表设计

### 8.1 `dim_vessel`

| 字段 | 类型 |
|---|---|
| vessel_key | bigserial |
| vessel_code | text |
| vessel_en_name | text |
| vessel_cn_name | text |
| mmsi | text |
| first_seen_at | timestamptz |
| last_seen_at | timestamptz |
| raw_aliases | jsonb |

优先匹配顺序：

1. `vessel_code`；
2. MMSI；
3. 标准化英文船名；
4. 英文船名 + 航次 + 时间窗口的弱匹配。

禁止仅凭相似船名强行合并。

### 8.2 `dim_terminal`

保存码头代码和标准名称。未知码头先按原代码保留，不猜测业务含义。

### 8.3 `dim_route`

建议主键粒度：

```text
origin_port_code
destination_port_code
transshipment_port_code
direction
```

如果当前接口只能得到上一港和下一港，则保留 `route_quality`：

- `direct_confirmed`；
- `adjacent_ports_only`；
- `transshipment_confirmed`；
- `unknown`.

### 8.4 `dim_cargo_group`

| 字段 | 说明 |
|---|---|
| cargo_group_key | 主键 |
| level_1 | 一级货类 |
| level_2 | 二级货类 |
| normalized_name | 标准货名 |
| rule_version | 规则版本 |
| match_type | exact/regex/dictionary/manual/unknown |
| active | 是否启用 |

第一版不要直接使用无监督文本聚类替代货类字典。应先建设可审计的规则和人工映射表。

### 8.5 `fact_vessel_plan_snapshot`

业务键建议：

```text
vessel_code + voyage + terminal + direction + planned_arrival_date
```

字段：

```text
vessel_plan_key
vessel_key
voyage
terminal_key
direction
trade_flag
ctn_start_time
ctn_end_time
custom_close_time
port_close_time
eta
etd
ata
atd
estimated_anchor_time
actual_anchor_time
last_port_code
next_port_code
berth_reference
status
published
publish_time
source_update_time
snapshot_time
record_hash
raw_json
```

唯一键：

```sql
(business_key_hash, snapshot_time)
```

另建当前视图 `current_vessel_plan`，按业务键取最新快照。

### 8.6 `fact_container_vgm`

业务键优先：

```text
container_no + vessel_code + voyage + operator_time + sender_code
```

字段：

```text
container_no
vessel_key
voyage
terminal_code
operator_code
direction
container_type
vgm_weight_kg
vgm_method
operator_time
terminal_received_time
result_code
result_description
sender_code
receiver_code
ingested_at
record_hash
raw_json
```

### 8.7 `fact_cargo_release`

业务键优先：

```text
bill_no + vessel_code + voyage + pass_time + terminal_code
```

若提单号为空，则降级使用记录哈希，不能人为编造 ID。

字段：

```text
vessel_key
vessel_name_raw
voyage
direction
bill_no
pass_time
terminal_code
flag
cargo_volume
gross_weight
ingested_at
record_hash
raw_json
```

### 8.8 `fact_transshipment`

业务键建议：

```text
container_no + first_vessel_code + first_voyage
+ second_vessel_code + second_voyage + bill_no
```

字段：

```text
container_no
operator_code
bill_no
quantity
weight
volume
cargo_description_raw
cargo_group_key
first_vessel_key
first_voyage
first_load_port_code
first_discharge_port_code
second_vessel_key
second_voyage
second_trans_port_code
sailing_date
cutoff_date
terminal_code
check_flag
send_flag
ingested_at
record_hash
raw_json
```

### 8.9 `fact_container_event`

统一容器事件表，用于吸收单箱历史和后续 Track 数据。

```text
container_no
event_type
event_code_raw
event_time
vessel_key
voyage
terminal_code
direction
bill_no
event_source
event_confidence
source_record_hash
raw_json
```

`event_type` 只允许已验证的标准值。无法确认的动作代码写入 `UNKNOWN`，保留 `event_code_raw`，不得猜测。

---

## 9. Gold 分析层表设计

### 9.1 `agg_flow_daily`

推荐粒度：

```text
date
+ terminal
+ direction
+ route
+ cargo_group
+ vessel_operator
```

指标：

```text
vgm_container_count
vgm_weight_kg
released_bill_count
released_weight
released_volume
transshipment_container_count
transshipment_weight
transshipment_volume
planned_vessel_call_count
actual_vessel_call_count
avg_arrival_delay_hours
avg_departure_delay_hours
```

### 9.2 `agg_flow_weekly`

由日表统一聚合，周定义必须固定，例如 ISO 周。禁止不同图表各自定义周起点。

### 9.3 `mart_curve_series`

用于前端直接读取曲线。

| 字段 | 说明 |
|---|---|
| curve_id | 曲线标识 |
| curve_type | 计划需求/VGM/放行/中转/延迟/压力 |
| entity_type | terminal/route/cargo/operator/cluster |
| entity_key | 对象 |
| granularity | day/week/month |
| time_bucket | 时间 |
| value | 数值 |
| lower_bound | 可选区间下界 |
| upper_bound | 可选区间上界 |
| quality_flag | complete/partial/imputed/anomalous |
| model_version | 计算版本 |
| computed_at | 计算时间 |

唯一键：

```sql
(curve_id, time_bucket, model_version)
```

### 9.4 `feature_series_window`

每个对象、每个观察窗口的一组特征。

```text
entity_type
entity_key
window_start
window_end
frequency
feature_version
mean
median
std
coefficient_of_variation
trend_slope
recent_growth_rate
peak_to_median
zero_ratio
seasonal_strength
autocorrelation_lag_1
autocorrelation_lag_7_or_4
arrival_delay_mean
arrival_delay_p90
transshipment_share
data_completeness
feature_json
```

### 9.5 `cluster_run`

```text
cluster_run_id
algorithm
algorithm_version
feature_version
window_start
window_end
entity_type
parameters_json
normalization_method
sample_count
cluster_count
silhouette_score
stability_score
created_at
artifact_path
```

### 9.6 `cluster_assignment`

```text
cluster_run_id
entity_key
cluster_id
membership_probability
distance_to_prototype
is_outlier
business_label
assigned_at
```

### 9.7 `change_point_event`

保存变点，不直接覆盖曲线。

```text
entity_key
curve_type
change_time
change_score
before_level
after_level
before_trend
after_trend
algorithm
model_version
detected_at
```

### 9.8 `trend_snapshot`

面向业务的最新趋势结果：

```text
entity_key
curve_type
as_of_time
trend_state
growth_1w
growth_4w
growth_13w
volatility_13w
current_cluster
previous_cluster
change_point_recent
anomaly_score
data_quality
explanation_json
```

---

## 10. 抓取器设计

### 10.1 统一基类

每个爬虫实现：

```python
class BaseCrawler:
    endpoint_name: str
    mode: Literal["append", "upsert", "snapshot", "enrichment"]

    def build_requests(self, context) -> Iterable[RequestSpec]: ...
    def fetch_page(self, request: RequestSpec, page_num: int) -> ApiPage: ...
    def extract_rows(self, response: dict) -> list[dict]: ...
    def get_total(self, response: dict) -> int | None: ...
    def normalize(self, row: dict) -> list[NormalizedRecord]: ...
    def business_key(self, row: dict) -> str: ...
    def event_time(self, row: dict) -> datetime | None: ...
```

统一能力：

- Token 检查；
- 指数退避重试；
- 401/403 立即停止并提示更新 Token；
- 业务 `code=400` 分类；
- 每个接口独立限速；
- 请求参数脱敏；
- 保存原始响应；
- 页级 checkpoint；
- 可重复执行；
- 幂等写入。

### 10.2 分页终止条件

```python
while True:
    page = fetch(page_num)

    rows = extract_rows(page)
    total = get_total(page)

    persist_raw(page)
    persist_rows(rows)
    save_checkpoint(page_num + 1)

    if not rows:
        break

    if total is not None and page_num * page_size >= total:
        break

    if len(rows) < page_size:
        break

    page_num += 1
```

必须增加最大页数保护和“连续重复页面哈希”保护，防止接口忽略页码后无限循环。

### 10.3 历史回填

#### 散杂货放行

由于过滤语义不可靠：

1. 使用较大但安全的 `pageSize`；
2. 保存每页原始响应；
3. 每页计算记录集合哈希；
4. 检测相邻页重复；
5. 按 `record_hash` 去重；
6. 按 `pass_time` 在本地划分历史分区；
7. 全量完成后输出：
   - 抓取记录数；
   - 唯一记录数；
   - 时间最小值/最大值；
   - 空时间比例；
   - 重复率；
   - 页间重复率。

#### VGM

文档中的查询至少需要箱号或船舶条件。第一版可采用：

- 从船舶计划、进箱公告和其他事实表提取船舶与航次；
- 按船舶/航次组合抓取；
- 对出现过的箱号进行去重；
- 不通过枚举随机箱号扩大范围。

#### 单箱历史

建立待增强队列：

```text
container_no
priority
source
first_seen_at
last_attempt_at
attempt_count
status
```

只有核心数据中新出现的箱号进入队列。

---

## 11. 增量更新策略

### 11.1 调度建议

| 任务 | 建议频率 | 模式 |
|---|---:|---|
| 船舶计划 | 每 1 小时 | Snapshot/Upsert |
| 进箱公告 | 每 6 小时 | Snapshot |
| VGM | 每 6 小时 | Append + 重叠重扫 |
| 散杂货放行 | 每日 | Append + 周期性重扫 |
| 内支中转 | 每日 | Upsert/Append |
| 单箱历史增强 | 每小时批处理 | Enrichment |
| 日聚合 | 每日 | Rebuild affected partitions |
| 周聚合 | 每日 | Rebuild recent 8 weeks |
| 聚类 | 每周或漂移触发 | Model refresh |
| 曲线和趋势快照 | 每次增量后 | Incremental |

实际频率应根据接口稳定性和账号使用限制调整。

### 11.2 重叠窗口

- VGM：最近 7 天；
- 海关放行：最近 30 天或定期全量校验；
- 船舶计划：过去 14 天到未来 60 天；
- 内支中转：最近 30 天；
- Gold 聚合：重算受影响日期到当前日期。

### 11.3 受影响分区重算

新记录进入后，不全表重算：

```text
affected_dates
affected_routes
affected_cargo_groups
affected_terminals
```

只删除并重建相应 Gold 分区。

---

## 12. 数据标准化

### 12.1 时间

统一保存为带时区时间。原始字符串保留。解析失败时：

- 规范字段写 NULL；
- `quality_flag=invalid_datetime`；
- 原值保存在 `raw_json`；
- 写入数据质量报告。

### 12.2 数值

重量、体积和件数字段可能是字符串：

- 空字符串、空格、`null` 转 NULL；
- 非法值禁止强制变成 0；
- 保存解析失败数量；
- 单位统一后再聚合。

### 12.3 船名

标准化操作只包括：

- trim；
- 大小写统一；
- 连续空格压缩；
- 常见全角字符转换。

不要自动删除可能影响身份的字符。

### 12.4 货描

第一版流程：

1. 原始文本规范化；
2. 精确字典匹配；
3. 正则规则匹配；
4. 无法匹配写入 `UNKNOWN`；
5. 生成待人工审核列表；
6. 映射规则有版本号；
7. 映射规则变更后可重建下游聚合。

---

## 13. 曲线体系

### 13.1 计划需求曲线

指标：

```text
planned_vessel_call_count
planned_route_count
future_window_count
ctn_window_hours
```

不能把计划航次数直接解释为箱量。

### 13.2 VGM 需求曲线

```text
unique_container_count
vgm_weight_kg
avg_weight_per_container
success_rate
terminal_receive_lag_hours
```

### 13.3 放行货流曲线

```text
released_bill_count
released_weight
released_volume
avg_weight_per_bill
```

### 13.4 中转货流曲线

```text
transshipment_container_count
transshipment_weight
transshipment_volume
route_share
cargo_mix_share
```

### 13.5 船舶运行延迟曲线

```text
arrival_delay_hours = ata - eta
departure_delay_hours = atd - etd
anchor_delay_hours = actual_anchor_time - estimated_anchor_time
```

只有时间字段均存在且顺序合理时才计算。

### 13.6 计划修订曲线

同一业务键不同快照之间计算：

```text
eta_revision_hours
etd_revision_hours
ctn_end_revision_hours
terminal_change_flag
berth_change_flag
status_change_count
```

这可以反映运行不确定性。

### 13.7 供需压力指数

本期不接价格，因此只输出“供需压力指数”，不输出价格预测。

建议初始版本：

```text
SPI =
  0.30 * Z(vgm_weight_growth)
+ 0.20 * Z(released_weight_growth)
+ 0.15 * Z(transshipment_weight_growth)
+ 0.15 * Z(arrival_delay)
+ 0.10 * Z(departure_delay)
+ 0.10 * Z(plan_revision_frequency)
```

注意：

- 权重只是初始规则，必须在配置中保存；
- 在同一对象历史内部进行稳健标准化；
- 数据缺失时重新归一化剩余权重；
- 输出各分项贡献；
- 名称必须为“压力指数”，不能称为价格或运价。

---

## 14. 聚类设计

### 14.1 聚类对象

优先顺序：

1. 路线；
2. 标准货类；
3. 路线 × 货类；
4. 船公司；
5. 码头；
6. 船舶。

只有在观察窗口数据完整度达到阈值时进入聚类。

### 14.2 第一阶段：特征聚类

特征：

```text
mean
median
std
coefficient_of_variation
trend_slope
growth_4w
growth_13w
peak_to_median
zero_ratio
seasonal_strength
arrival_delay_mean
arrival_delay_p90
transshipment_share
plan_revision_frequency
```

算法：

- HDBSCAN：识别自然群体和离群对象；
- 层次聚类：生成可解释树状图；
- Gaussian Mixture：输出软分配概率。

### 14.3 第二阶段：曲线形态聚类

在数据长度满足要求后增加：

- K-Shape；
- K-Medoids + 弹性距离；
- 对每条序列先做稳健缩放，保留另一份原始量级用于解释。

### 14.4 状态和变点

使用变点检测将曲线划分为：

- 平稳；
- 增长；
- 衰退；
- 高波动；
- 突发峰值；
- 数据不足。

业务状态由特征和规则解释，不能直接把聚类编号当作业务含义。

### 14.5 共识聚类

不同算法得到共现矩阵：

```text
co_assignment(i, j)
= 同组算法数 / 有效算法数
```

再对共现矩阵聚类，输出稳定群体和稳定性分数。

### 14.6 聚类更新

- 默认每周全量重估；
- 新数据到达后只做最近模型的预测分配；
- 出现重大变点、低分配置信度或数据分布漂移时提前重估；
- 保留历史聚类运行，不覆盖旧标签。

---

## 15. 可视化设计

### 15.1 曲线总览

每张曲线包含：

- 原始值；
- 4 周移动平均；
- 同比或环比；
- 数据完整度；
- 变点；
- 异常点。

### 15.2 聚类原型

每个聚类展示：

- 标准化中位数曲线；
- 25%—75%区间；
- 10%—90%区间；
- 样本数；
- 真实量级中位数；
- 代表对象；
- 离群对象。

### 15.3 状态迁移

展示对象随时间的：

```text
稳定 → 增长 → 高波动 → 恢复
```

### 15.4 路线与货类热力图

- 横轴：时间；
- 纵轴：路线或货类；
- 值：货量增速、压力指数、延迟或异常分数。

### 15.5 当前窗口历史相似图

对最近 8 周或 13 周：

- 找历史最相似窗口；
- 展示后续 4 周实际走势；
- 显示相似度和当时业务状态；
- 不直接表述为确定性预测。

### 15.6 输出形式

- Plotly HTML；
- PNG/SVG 静态图；
- CSV；
- JSON API；
- 可选 FastAPI Dashboard。

---

## 16. 数据质量与业务逻辑检查

### 16.1 通用检查

- 主键重复率；
- 时间解析失败率；
- 数值解析失败率；
- 分页重复率；
- 页码是否生效；
- 时间范围是否突然缩短；
- 接口字段是否新增或消失；
- 同一接口记录量是否异常下降。

### 16.2 时序逻辑

仅对已确认事件使用：

```text
VGM 时间 ≤ 码头接收时间
ETA/ETD 可以修订
ATA/ATD 通常不应反复回退
计划靠泊时间通常 ≤ 计划离泊时间
实际靠泊时间通常 ≤ 实际离泊时间
```

遇到违反记录：

- 不立即删除；
- 标记异常；
- 保留原始值；
- 从默认聚合中排除或降权；
- 在质量报告中统计。

### 16.3 数据完整度

每条曲线计算：

```text
expected_buckets
observed_buckets
completeness_ratio
latest_event_time
latest_ingested_at
```

低完整度曲线不得参与聚类或趋势判断。

---

## 17. API 与命令行

### 17.1 CLI

```bash
npedi crawl vessel-plan --mode incremental
npedi crawl container-notice --mode snapshot
npedi crawl vgm --mode backfill
npedi crawl cargo-release --resume
npedi crawl transshipment --mode incremental
npedi enrich container-history --limit 500
npedi normalize --since 2026-01-01
npedi aggregate --granularity day
npedi build-curves --affected-only
npedi cluster --entity route --window 52w
npedi detect-changepoints --window 104w
npedi render --dashboard
npedi quality-report
```

### 17.2 FastAPI

建议端点：

```text
GET /curves
GET /curves/{curve_id}
GET /entities/{entity_type}/{entity_key}/trend
GET /clusters/runs
GET /clusters/runs/{run_id}
GET /quality/latest
GET /crawl-runs
```

---

## 18. 安全与合规

1. Token 仅从环境变量或本机安全配置读取；
2. 日志中不得记录完整 Token、Cookie、手机号或敏感提单信息；
3. 原始响应访问应有权限控制；
4. 不绕过接口权限；
5. 不并发轰炸服务；
6. 默认设置低并发、随机抖动和全局速率限制；
7. 请求失败时优先停止并报告，不进行攻击式重试；
8. 数据只用于授权范围内的分析。

---

## 19. 实施阶段

### Phase 0：项目勘察

- 阅读 `NPEDI-API-REFERENCE.md` 和现有 `client.py`；
- 运行最小只读探针；
- 保存可复现样例；
- 确认数据库和运行环境。

### Phase 1：Bronze 与爬虫框架

- 建立数据库迁移；
- 实现 `crawl_run`、`checkpoint`、`raw_response`；
- 实现认证、分页、重试、限速；
- 完成船舶计划和进箱公告。

### Phase 2：核心历史事实

- 实现 VGM；
- 实现散杂货放行；
- 实现内支中转；
- 完成断点续传和幂等。

### Phase 3：Silver 与数据质量

- 建立维表和事实表；
- 实现标准化；
- 实现质量报告；
- 实现货描映射表。

### Phase 4：Gold 与曲线

- 日、周聚合；
- 计划、VGM、放行、中转、延迟和压力曲线；
- Plotly 输出。

### Phase 5：聚类与趋势

- 特征工程；
- HDBSCAN 和层次聚类；
- 变点；
- 聚类原型；
- 趋势快照。

### Phase 6：增量运行与运维

- 调度；
- 受影响分区重算；
- 监控；
- 失败重跑；
- 操作文档。

---

## 20. 验收标准

### 数据采集

- 所有核心接口支持断点续传；
- 重复执行不会产生重复事实记录；
- 每次抓取都有运行记录；
- 401/403 不会无限重试；
- 分页不依赖 `totalPages`；
- 散杂货大接口可从中断页继续。

### 数据模型

- 原始响应可追溯；
- 规范表保留来源哈希；
- 船舶、航次、箱、提单和路线不被无依据合并；
- 未知动作代码不被猜测解释。

### 曲线

- 至少输出计划需求、VGM、放行货流、中转货流、船舶延迟和供需压力六类曲线；
- 日、周粒度一致；
- 图表显示完整度；
- 所有曲线均可回溯到事实表。

### 聚类

- 至少支持 HDBSCAN 和层次聚类；
- 保存运行参数和版本；
- 输出聚类原型、样本数和离群对象；
- 数据不足对象不会被强制分类；
- 历史聚类结果不会被覆盖。

### 运维

- 有 `.env.example`；
- 有数据库迁移；
- 有单元测试和集成测试；
- 有模拟响应 Fixture；
- 有运行、恢复、重建和质量检查文档。

---

## 21. 第一版完成定义

第一版完成时，系统应能够：

1. 从零数据库开始完成核心接口历史回填；
2. 中断后从 checkpoint 恢复；
3. 每日增量抓取并修订最近数据；
4. 生成标准化事实表；
5. 生成日、周时序曲线；
6. 对路线和货类进行特征聚类；
7. 输出变点、异常和最新趋势；
8. 通过 Plotly HTML 或 FastAPI 查询结果；
9. 在不接价格、不使用码头专属接口的前提下长期稳定运行。

---

## 22. 来源映射

本设计中关于接口路径、参数、响应字段、权限限制和分页异常的事实均以 `NPEDI-API-REFERENCE.md` 为依据。技术栈、分层模型、表结构、聚类算法、调度频率和实现目录属于本架构的建议方案，需要在实际项目中验证后调整。
