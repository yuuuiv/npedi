你现在是本项目的首席数据工程师、后端工程师和时序分析工程师。请在当前代码仓库中，基于 `NPEDI-API-REFERENCE.md` 和现有 `client.py`，实现一套“NPEDI 历史数据采集、增量更新、规范化入库、时序曲线和聚类分析系统”。

# 一、最高优先级约束

1. 开始编码前，必须完整阅读：
   - `NPEDI-API-REFERENCE.md`
   - `client.py`
   - 当前仓库的 README、配置、依赖和已有数据库代码。
2. 文档中已经验证的接口、参数和字段是唯一事实依据。
3. 不得猜测未知动作代码、未知字段、未验证参数或业务含义。
4. 不接入价格、运价、货物成交价，也不输出价格预测。
5. 不调用任何 `/matou/*` 码头专属业务接口。
6. 不尝试绕过权限、破解 Token、扩大账号权限或规避服务端限制。
7. 所有 API 请求必须复用现有合法认证方式。
8. 不得破坏现有可用功能；先理解现有代码，再增量实现。
9. 任务过程中不要停留在方案说明，必须实际创建代码、迁移、测试和文档。
10. 如果真实接口暂时不可访问，使用从文档结构生成的脱敏 Fixture 完成测试，但必须把真实联调状态标为“未验证”，不能假装已经成功。

# 二、项目目标

实现以下完整链路：

```text
NPEDI API
→ 原始响应 Bronze
→ 规范事实 Silver
→ 聚合与特征 Gold
→ 日/周时序曲线
→ 聚类、变点、异常和趋势
→ Plotly 输出或 FastAPI 查询
```

系统必须支持：

- 首次历史回填；
- 分页断点续传；
- 幂等执行；
- 重叠窗口增量；
- 原始响应保留；
- 数据质量检查；
- 受影响分区重算；
- 聚类结果版本化；
- Windows 环境可运行。

# 三、纳入接口

优先实现以下接口：

1. 船舶计划：
   - `/vessel/plan/selectContainerDynamicPlan`
2. 每日进箱公告：
   - `/vessel/dzyjh/getlist`
3. VGM：
   - `/ctnvgm/getlist`
4. 散杂货海关放行：
   - `/ediCustptrSZ/getEdiCustptrSz`
5. 内支中转：
   - `/npp/nzx/getNzwPageResult`
6. 单箱历史增强：
   - `/ediContainerlog/getEdiContainerlog/{container_no}`

第二阶段可选：

- `/track/getContainerTrackInfo`
- `/scoedor/getEdiScoedor`
- `/cusretrec/getEdiCusretrec`
- `/costcoHistory/getEdiCostco`
- `/transferlogSearch/getMessageLogStatistic`

不得接入：

- 任何 `/matou/*`；
- 参数结构未知且当前会“系统异常”的接口；
- 价格数据。

# 四、技术方案

如果仓库没有既定技术栈，采用：

- Python 3.12；
- 复用 `client.py`；
- PostgreSQL 16；
- SQLAlchemy 2；
- Alembic；
- Pydantic Settings；
- Polars；
- scikit-learn；
- hdbscan；
- ruptures；
- Plotly；
- FastAPI；
- pytest。

如果仓库已有等价技术栈，应优先兼容现有实现，不要为了遵守此列表而无意义重构。

# 五、目录要求

在不破坏现有结构的前提下，形成类似：

```text
src/npedi_pipeline/
├─ config.py
├─ logging.py
├─ cli.py
├─ db/
├─ crawlers/
├─ parsers/
├─ pipelines/
├─ analytics/
├─ visualization/
└─ api/
```

同时创建：

```text
migrations/
tests/
scripts/
docs/
```

# 六、数据库表

必须通过 Alembic 创建至少以下表。

## Bronze

- `crawl_run`
- `crawl_checkpoint`
- `raw_api_response`
- `ingest_error`

## Silver

- `dim_vessel`
- `dim_terminal`
- `dim_route`
- `dim_cargo_group`
- `fact_vessel_plan_snapshot`
- `fact_container_vgm`
- `fact_cargo_release`
- `fact_transshipment`
- `fact_container_event`
- `container_enrichment_queue`

## Gold

- `agg_flow_daily`
- `agg_flow_weekly`
- `mart_curve_series`
- `feature_series_window`
- `cluster_run`
- `cluster_assignment`
- `change_point_event`
- `anomaly_event`
- `trend_snapshot`

每张表都必须：

- 有明确主键；
- 有必要的唯一约束；
- 有时间和业务键索引；
- 有 `created_at` 或 `ingested_at`；
- 有来源哈希或来源记录引用；
- 对未知字段保留 `raw_json`。

# 七、抓取框架

创建统一 `BaseCrawler`，至少支持：

- 构造请求；
- 分页；
- 解析统一响应信封；
- `code=200/400/401/403` 分类；
- 指数退避；
- 可配置限速；
- 原始响应落库；
- checkpoint；
- resume；
- 幂等写入；
- 结构化日志；
- 最大页数保护；
- 连续重复页面保护。

分页逻辑不得使用 `totalPages`。应以：

- `total`；
- 当前页记录数；
- `pageSize`；
- 空页；
- 重复页面哈希；

共同判断结束。

401/403 时立即中止任务并打印明确错误，不进行无限重试。

# 八、各接口特殊要求

## 船舶计划

- 作为 Snapshot/Upsert 数据；
- 保留每次快照；
- 建立业务键；
- 可查询当前最新版本；
- 计算 ETA、ETD、进箱截止时间的修订量。

## 进箱公告

- 全量分页抓取；
- 用内容哈希识别变化；
- 保留历史快照；
- 不把航次数直接解释为箱量。

## VGM

- 从已有船舶和航次组合生成合法查询；
- 不随机枚举箱号；
- 保存箱号、船舶、航次、重量、申报时间、接收时间和结果；
- 进行自然键和哈希双重去重。

## 散杂货海关放行

这是重点接口：

- 数据量可能超过十万条；
- 服务端筛选可能不可靠；
- 必须支持页级 checkpoint；
- 必须检测接口是否忽略页码；
- 必须计算相邻页重复率；
- 必须输出全量回填审计报告；
- 必须本地按时间、船舶、航次和码头过滤；
- 不得假设传入筛选条件一定生效。

## 内支中转

- 标准化第一程、第二程、港口、箱号、提单、重量、体积、件数和货描；
- 保留货描原文；
- 建立可审计的货类规则表；
- 无法分类的货描进入 `UNKNOWN` 和待审核列表。

## 单箱历史

- 只能对核心事实中出现的箱号做增强；
- 建立队列表；
- 有优先级、重试次数和状态；
- 未知动作代码写入 `UNKNOWN`；
- 保存原始代码，不猜测动作语义。

# 九、数据处理规则

## 时间

保存：

- 业务事件时间；
- 来源更新时间；
- 抓取时间；
- 原始时间字符串。

解析失败：

- 标准字段为 NULL；
- 记录质量标志；
- 保留原值；
- 不转成当前时间。

## 数值

- 空字符串和非法值转 NULL，不转 0；
- 重量和体积统一单位；
- 保存解析失败统计；
- 聚合前检查负数和极端值。

## 船舶

优先按船舶代码匹配。没有代码时，只做保守匹配。禁止仅凭相似名称强制合并。

## 货描

实现：

1. 文本规范化；
2. 精确词典；
3. 正则规则；
4. UNKNOWN；
5. 待审核 CSV/表；
6. 规则版本化；
7. 规则变更后可重算。

# 十、曲线

生成日和周粒度：

1. 计划需求曲线；
2. VGM 箱量与重量曲线；
3. 放行票数、重量、体积曲线；
4. 中转箱量、重量和体积曲线；
5. 到港和离港延迟曲线；
6. 船舶计划修订曲线；
7. 货类结构和路线结构曲线；
8. 供需压力指数。

供需压力指数只能称为压力指数，不能称为价格。初始权重放入配置文件，输出每个分项贡献。

所有曲线写入 `mart_curve_series`，并提供：

- 原始值；
- 移动平均；
- 环比；
- 完整度；
- 异常标记；
- 变点标记；
- 计算版本。

# 十一、聚类和趋势

先实现可解释的特征聚类。

特征至少包括：

```text
均值
中位数
标准差
变异系数
趋势斜率
4 周增速
13 周增速
峰值/中位数
零值比例
季节强度
平均到港延迟
P90 到港延迟
中转占比
计划修订频率
数据完整度
```

实现：

- HDBSCAN；
- 层次聚类；
- 聚类原型；
- 离群对象；
- 聚类稳定性；
- 软分配或置信度；
- 聚类版本保存。

之后增加：

- ruptures 变点检测；
- 异常分数；
- 最近趋势状态；
- 最近聚类与前一聚类迁移；
- 数据不足状态。

不要直接把 `cluster_0` 解释成“旺季型”。业务标签必须根据原型特征生成，并允许人工修正。

# 十二、图表

使用 Plotly 输出：

1. 时序曲线总览；
2. 聚类原型及分位区间；
3. 路线 × 时间热力图；
4. 货类 × 时间热力图；
5. 状态迁移图；
6. 变点和异常图；
7. 当前窗口与历史相似窗口对比图；
8. 数据质量面板。

每张图必须显示：

- 时间粒度；
- 数据来源；
- 数据完整度；
- 计算版本；
- 更新时间。

归一化曲线和真实量级曲线必须分开。

# 十三、CLI

至少实现：

```bash
npedi crawl vessel-plan --mode incremental
npedi crawl container-notice --mode snapshot
npedi crawl vgm --mode backfill
npedi crawl cargo-release --resume
npedi crawl transshipment --mode incremental
npedi enrich container-history --limit 500
npedi normalize
npedi aggregate --affected-only
npedi build-curves --affected-only
npedi cluster --entity route --window 52w
npedi detect-changepoints
npedi render --output ./output
npedi quality-report
```

所有命令应有 `--dry-run`、`--log-level` 和清晰帮助信息。

# 十四、测试

创建：

- 统一响应信封测试；
- 分页结束测试；
- `totalPages=0` 测试；
- 重复页保护测试；
- checkpoint 恢复测试；
- 幂等写入测试；
- 401/403 停止测试；
- 时间解析测试；
- 字符串数值解析测试；
- 货描映射测试；
- 事实表唯一键测试；
- 聚合正确性测试；
- 曲线完整度测试；
- 聚类数据不足测试；
- Alembic 从空库升级测试。

测试不得依赖真实生产账号。

# 十五、数据质量报告

每次回填和增量后输出：

```text
接口
开始/结束时间
请求数
原始记录数
唯一记录数
新增数
更新数
重复率
空时间比例
解析失败率
最早/最晚事件时间
分页重复率
Schema Drift
错误摘要
```

保存为数据库记录和 Markdown/JSON 文件。

# 十六、文档

创建：

- `docs/architecture.md`
- `docs/data_dictionary.md`
- `docs/endpoint_mapping.md`
- `docs/operations.md`
- `docs/recovery.md`
- `docs/data_quality.md`

`operations.md` 必须说明：

- 如何配置 Token；
- 如何初始化数据库；
- 如何执行历史回填；
- 如何恢复中断任务；
- 如何执行增量；
- 如何重建 Gold；
- 如何更新货类规则；
- 如何查看失败记录；
- 如何导出图表。

# 十七、实施顺序

严格按以下顺序：

1. 勘察仓库和现有代码；
2. 输出实施清单；
3. 建立配置、日志和数据库基础；
4. 完成 Alembic；
5. 完成统一爬虫框架；
6. 先实现船舶计划和进箱公告；
7. 再实现 VGM；
8. 再实现散杂货放行；
9. 再实现内支中转；
10. 再实现单箱历史队列；
11. 完成标准化；
12. 完成聚合和曲线；
13. 完成聚类和变点；
14. 完成 Plotly；
15. 完成测试；
16. 完成文档；
17. 运行完整测试和最小端到端演示。

不要在基础数据链路未通过测试前提前做复杂模型。

# 十八、每阶段输出

每完成一个阶段，输出：

- 已修改文件；
- 关键设计决定；
- 测试结果；
- 尚未验证内容；
- 下一阶段。

发现文档与真实响应冲突时：

1. 保存脱敏响应样本；
2. 不静默兼容；
3. 在 `docs/endpoint_mapping.md` 记录差异；
4. 用向后兼容方式更新解析器；
5. 增加测试。

# 十九、最终验收

最终必须提供：

- 可运行代码；
- 数据库迁移；
- `.env.example`；
- CLI；
- 测试；
- 架构和运维文档；
- 至少一套脱敏 Fixture；
- 从空库到生成曲线的端到端命令；
- Plotly 示例输出；
- 数据质量报告示例；
- 已知限制列表。

最终汇报必须明确区分：

- 已通过真实接口验证；
- 仅通过 Fixture 验证；
- 尚未验证；
- 明确不在范围内。

现在开始执行。先阅读仓库和参考文档，列出不超过 15 项的实施清单，然后直接进入第一阶段编码，不要只停留在计划。
