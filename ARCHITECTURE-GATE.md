# npedi.com 进出门（CODECO）历史数据采集 — 架构设计

> 本文档是 [ARCHITECTURE.md](ARCHITECTURE.md)（npp 核放比对数据）的姊妹篇。
> 所有接口事实从 2026-07-28 的 `www.npedi.com.har` 中还原，认证方式与 npp 完全相同。
> 阅读前提：已理解 npp 管线的 token 机制、upsert 规则与调度方式，本文只写差异。

## 0. 背景：为什么要做这条管线

npp 管线（`/onesite-api/npp/search/integrated`）的航次目录 `vesselinfo` 只返回
**在册的 848 个航次**——那是"当前还在核放流程里"的工作面，走完流程的航次会下架。

进出门查询（页面菜单"进出门查询"，接口 `scodeco`）挂的是另一份航次目录
`voyage/vesselList`，实测返回 **14,337 条航次记录（13,954 个 船×航次 对，2,148 艘船）**，
样本数据最早可追溯到 2024 年。两份目录的关系（用库快照实测）：

| 对比 | 数量 |
|---|---|
| npp 目录（=现库 `voyages`） | 848 对 |
| gate 目录 `vesselList` | 13,954 对 |
| 现库航次也在 gate 目录中 | 845 / 848 |
| gate 目录中现库没有的 | **13,109 对** |

HAR 样本查询（ONE MANEUVER/071E）返回 1,320 条进门记录，抽查其箱号
`HLBU1910360`、提单号 `HLCWJA276914A` 在现库 542,701 行中均不存在。
**结论：gate 数据源是 npp 的超集入口，能补全 npp 已下架航次的历史数据。**

**数据保留下界（2026-07-29 实测）：至少到 2022 年 1 月。** 铁路驳船系列的航次号
是日期编码（`220110A` = 2022-01-10），定向探测结果：

| 探测航次 | total | msgReceiveTime 范围 |
|---|---|---|
| TIELUYIWU2 `220110A`（2022-01-10） | 445 | 2022-01-10 ~ 01-11 |
| TIELUYIWU2 `220215A`（2022-02-15） | 90 | 2022-02-15 当日 |
| HAITIEYUNSHU `220215A` | 176 | 2022-02-15 ~ 04-15 |
| ARGUS `220E` | 1,113 | 2024-03-16 ~ 03-28 |
| TIELUYIWU2 `2103062A`（2021-03-06） | 0 | — |
| HAITIEYUNSHU `21107A`（2021-10?） | 0 | — |

两点提醒：2021 年编号的航次全部 total=0，下界大概率就在 2022 年初；
**目录里挂着的航次不保证有数据**（探测中多个航次 total=0），空结果是常态，
回填时照常标记 done、不重试。另外航次号推年份不可靠——`2022E` 查出来是
2026 年的数据（纯序号），只有日期编码系列（yymmdd 前缀）可信。

目标：

1. 以 `vesselList` 的 14k 航次为入口，把全部进门（GATE_IN）/出门（GATE_OUT）
   记录一次性回填入库（多日断点续爬）。
2. 之后对"活跃航次"做每日增量，与 npp 管线共用调度。
3. gate 数据与 npp 数据在库内可关联（箱号+提单号+航次），互为补充：
   npp 给核放状态，gate 给闸口实操时间。

## 1. 接口清单

Base URL 与认证头与 npp 完全一致（`ediAuthorization: Bearer <WEB_TOKEN>`，
同一个 token，权限清单中已含 `container:scodeco:list`）。token 失效处理照抄 npp 管线。

### 1.1 航次目录（每轮 1 次）

```
GET /onesite-api/voyage/vesselList
→ {"code":200, "data":[ {voyage:"071E", vesselcode:"UN9475648",
     vesselename:"ONE MANEUVER", vesseletrim:"ONEMANEUVER"}, ... ]}
```

- 一次返回全部（实测 14,337 条），无分页、无参数。
- **没有任何日期字段**——无法从目录判断航次新旧，新航次只能靠"目录里出现了
  库里没有的对"来发现。
- 存在少量 (vesselcode, voyage) 重复（14,337 条 → 13,954 对），入库前去重。

### 1.2 进出门明细（核心数据源）

```
GET /onesite-api/scodeco/list
    ?type=GATE_IN REPORT          # 或 GATE_OUT REPORT（URL 编码后空格为 + 或 %20）
    &pageNum=1&pageSize=100
    &voyage=071E                  # 必须与 vesselList 中的 voyage 逐字符一致
    &vesselCode=UN9475648         # = vesselList 的 vesselcode
    &ctnOperatorCode=&ctnNo=&blNo=
→ {"code":200, "data":{ pageNum, pageSize, total, totalPages, list:[ ... ] }}
```

三个已从 HAR 证实的坑：

1. **voyage 必须精确匹配**：HAR 中用户手输 `071EJ` 查了三次全部 total=0，
   改成目录里的 `071E` 才出 1,320 条。程序永远用 `vesselList` 返回的原值查询，
   不做任何猜测/变形。查询结果为 0 不代表出错，空航次是正常业务状态。
2. **`totalPages` 不可信**：total=1320 时它返回 0。翻页页数一律自己算
   `ceil(total / pageSize)`。
3. `pageSize=100` 是页面默认值，照用，不调大试探。

每行 66 个字段，实测同页 100 行中仅 17 个字段有值，关键字段：

| 字段 | 含义 | 用途 |
|---|---|---|
| `id` | 字符串主键，形如 `20240702210342227037032`（= msgReceiveTime + 9 位序号） | **入库主键 / upsert 依据** |
| `msgReceiveTime` | 报文接收时间 `yyyyMMddHHmmss` | 事件时间轴 / 增量早停判据 |
| `senderCode` | 发送方码头（如 BLCT） | 对应 npp 的 `matou` |
| `vessel` / `voyage` / `direct` | 船名 / 航次 / 东西向 | 注意：**行内 `vesselCode` 为 null**，UN 号只在请求参数里，入库时自行补列 |
| `ctnNo` / `blNo` | 箱号 / 提单号 | 与 npp `containerno`/`billno` 关联 |
| `ctnOperatorCode` / `ctnSizeType` / `ctnStatus` / `containerType` | 箱属 / 尺寸 / 空重(F/E) / 类型 | 箱信息 |
| `ctnGrossWeight` / `sealNo` | 毛重 / 铅封 | 箱信息 |
| `inGateTime` / `outGateTime` | 进门 / 出门时间 `yyyyMMddHHmm`（12 位，比 msgReceiveTime 少秒） | **核心业务时间** |
| `dlPortCode` / `signTrade` | 卸货港 / 贸易标志 | 备用 |

GATE_IN 报文里 `outGateTime` 为 null；GATE_OUT 行结构相同（HAR 中无非空样本，
schema 按同构处理，见 §2 假设 3）。

**数据性质与 npp 根本不同**：这是 CODECO 报文流水——一行 = 码头发来的一条
进门/出门报文，`id` 含接收时间戳，**天然只增不改（append-only）**。因此没有
npp 那套 hash 变更检测 / TOUCH_ONLY / 字段历史的必要，入库用
`INSERT ... ON CONFLICT(id) DO NOTHING` 即可。同一箱可能出现多条报文
（重发/补发），全部保留，按箱聚合时取 `msgReceiveTime` 最新一条。

### 1.3 不要调用的接口

- `scodeco/getCompanyCtnagent`：箱代理下拉框数据，与采集无关。
- `getRouters` / 静态资源：同 npp 管线的禁项。

## 2. 假设验证结果（2026-07-29 已实测，实现时不必重验）

1. ~~留空 `voyage`/`vesselCode` 返回全量~~ **作废**：服务端拒绝，
   `400 船名/航次、箱代理编码、箱号、提单号不能同时为空`。
   **只能逐航次查询**，回填与增量都按 §4 的逐航次路径实现。
2. **列表按 `msgReceiveTime` 降序** — 已确认（ARGUS 220E：第 1 页
   max=03-28，第 12 页 min=03-16，页内亦降序）。推论：
   - 增量对活跃航次只拉第 1 页，撞见库内已有 id 即停，通常 1 页/航次/方向；
   - 回填断点可做到页级：但注意**活航次翻页期间来新报文会使整队后移**，
     跨天续爬时用"最后已入库 id"校验衔接，发现错位就整个航次重拉（幂等无害）。
3. **GATE_OUT 与 GATE_IN 同构** — 已确认，`outGateTime` 有值。
   另实测 GATE_OUT 数据量远小于 GATE_IN（多数航次为 0，ARGUS 220E 仅 2 条
   对 1,113 条），出门报文本身就稀疏，total=0 不是异常。
4. **`ctnNo`/`blNo` 参数是否可用作点查** — 未测。成立的话，"补全 npp 缺数"
   可以反向按箱号点查，不必等全量回填铺到那个航次；实现时顺手验一次。

## 3. 存储设计（追加到现有 npedi.sqlite）

```sql
-- gate 航次目录（独立于 voyages 表，因为口径不同：voyages 是 npp 在册目录）
CREATE TABLE gate_voyages (
  vesselcode   TEXT NOT NULL,          -- UN9475648
  voyage       TEXT NOT NULL,
  vesselename  TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  -- 回填断点：每个方向独立记录（NULL=未开始）
  gatein_done_at  TEXT,  gatein_total  INTEGER,
  gateout_done_at TEXT,  gateout_total INTEGER,
  PRIMARY KEY (vesselcode, voyage)
);

-- 进出门报文流水（append-only）
CREATE TABLE gate_events (
  id            TEXT PRIMARY KEY,      -- 接口返回的字符串 id
  type          TEXT NOT NULL,         -- 'GATE_IN' | 'GATE_OUT'（接口行内 type 为 null，从请求参数落列）
  vesselcode    TEXT NOT NULL,         -- 同上，从请求参数落列
  voyage        TEXT NOT NULL,
  vessel        TEXT, direct TEXT,
  senderCode    TEXT,
  ctnNo         TEXT, blNo TEXT,
  ctnOperatorCode TEXT, ctnSizeType TEXT, ctnStatus TEXT, containerType TEXT,
  ctnGrossWeight  TEXT, sealNo TEXT,
  msgReceiveTime  TEXT,
  inGateTime      TEXT, outGateTime TEXT,
  dlPortCode      TEXT, signTrade TEXT,
  raw_json        TEXT,                -- 其余 40+ 个稀疏字段原样存 JSON，将来要用不用重爬
  fetched_at      TEXT NOT NULL
);
CREATE INDEX idx_gate_events_voyage ON gate_events(vesselcode, voyage);
CREATE INDEX idx_gate_events_ctn    ON gate_events(ctnNo, blNo);

-- 与 npp 数据的关联视图（补数分析入口，见 §5）
CREATE VIEW v_gate_vs_npp AS
SELECT g.vesselcode, g.voyage, g.ctnNo, g.blNo, g.type, g.inGateTime, g.outGateTime,
       c.id AS npp_id, c.passFlag, c.sendFlag, c.remark
FROM gate_events g
LEFT JOIN containers c
  ON c.containerno = g.ctnNo AND c.billno = g.blNo
 AND c.unvessel = g.vesselcode AND c.voyage = g.voyage;
```

`sync_runs` 表复用，`kind` 增加 `'gate_backfill' | 'gate_incremental'`。

**Upsert 规则**：`INSERT OR IGNORE`（按 id）。不做 hash、不记历史——报文不可变。
若增量中发现同 id 内容变化（理论上不该发生），记一条 warning 日志即可。

## 4. 采集流程

### 4.1 全量回填（一次性，多日任务）

请求量级要先算清楚：13,954 对 × 2 方向 ≈ **28k 次列表请求起步**，加翻页
（样本航次 GATE_IN 1,320 条 = 14 页；平均按 2–4 页估）总计 **6–12 万次请求**。
按 0.5–1s 串行延时，即 **17–33 小时纯请求时间，必须按多日断点任务设计**，
一晚跑不完是预期内的。

```
1. 调 vesselList → 去重后 upsert 到 gate_voyages
2. 按优先级排序取未完成的 (vesselcode, voyage, direction)：
     第一优先：现库 voyages 里的 845 个在册航次（先把活跃面补齐）
     第二优先：其余 13,109 个历史航次（目录无日期，按目录顺序即可）
3. 对每个 (航次, direction)：pageSize=100 翻页全量入库；
   全部页成功后写 gatein_done_at / gateout_done_at = now 和 total
   （断点粒度 = 航次×方向；中断重跑自动跳过已 done 的）
4. total=0 也写 done（空航次是合法状态，不要重试）
5. 串行 + 0.5–1s 随机延时；401 → 告警退出（断点保证续跑无损）
```

回填运行方式建议：`python sync.py gate-backfill --max-requests 5000` ——
每晚跑一段固定预算，跑完自动停，Task Scheduler 挂夜间任务连续几晚铺完；
比"一次跑 30 小时"抗断网/断电/token 失效。

### 4.2 每日增量

gate 目录里的历史航次是死数据，回填过就不会再变；会动的只有活跃航次。

```
1. 调 vesselList（1 次）→ 发现新 (vesselcode, voyage) 对 → 当场全量回填（新航次页数很少）
2. 活跃集合 = 现库 voyages 表在册航次（npp 管线每轮都在维护它）
   ∩ gate_voyages —— 约 850 对
3. 对活跃集合逐对查 GATE_IN + GATE_OUT：列表按 msgReceiveTime 降序（§2 已证实），
   从第 1 页往后翻，**本页所有 id 均已在库即停**——无新报文的航次每方向恰好
   1 次请求，每轮约 1,700 次请求是上限而非常态开销
4. 离港航次的退出条件：连续 N 轮（默认 6）无新增报文，且已不在 npp
   vesselinfo 目录中 → 标记 inactive，不再增量查询
```

增量频率：**每日 1 次**（挂在晚间那轮 npp 增量之后），不必像 npp 一样每日三次
——闸口报文的消费时效没有核放状态高，而降级路径下每轮 1,700 次请求也不便宜。

### 4.3 与 npp 管线的关系

- 同一进程/CLI（`sync.py` 加子命令），共用 client.py 的认证、重试、延时、告警。
- 调度上错开：npp 增量 07:30/12:30/19:30，gate 增量 20:30，gate 回填 00:30
  起夜间预算跑。文件锁共用，防重叠。

## 5. 补全 npp 缺数：怎么用这批数据

用户已证实 gate 查得到的箱号/单号在 containers 表里查不到。两库关联键：

```
gate_events(ctnNo, blNo, vesselcode, voyage)
  ↔ containers(containerno, billno, unvessel, voyage)
```

- **缺口清单**：`v_gate_vs_npp WHERE npp_id IS NULL` 即"闸口有实操、核放库无记录"
  的箱子——绝大多数应是 npp 已下架的历史航次，属预期缺口，gate_events 本身
  就是它们的历史留档。
- **反向缺口**：containers 有、gate 无的行也值得看（LEFT JOIN 反过来），
  能区分"未进门"和"数据缺失"。
- 注意 npp 的双码头空壳行问题（见 ANALYSIS.md §五）：按箱关联前先滤掉
  无 `receivetime` 的空壳行，否则缺口统计会虚高。
- npp 的 `matou` 与 gate 的 `senderCode` 是同一维度（码头），可作关联质检。

## 6. CSV 导出

沿用 npp 管线的导出规范（utf-8-sig、文本列防 Excel、临时文件+原子改名）。
14 位与 12 位两种时间戳都转成可读写法（`inGateTime` 只到分钟）。

1. `gate_events_all.csv` — 当前全量快照，增量轮覆盖重写。
   数据量大（预计数百万行），`GATE_EXPORT_SCOPE` 可只导活跃集合或整个关掉。
   **回填轮默认跳过**（`--export-all` 可强制导）：回填要连着跑好几晚、每晚若干段，
   每段结束都重写一份几百万行的 CSV 是纯浪费。
2. `gate_new_<ts>.csv` — 本轮新增报文，无新增不生成。靠 `gate_events.run_id` 筛选。
3. `gate_voyages.csv` — 航次目录快照，含每个航次两个方向的回填进度。
4. `gate_gap_vs_npp.csv` — §5 缺口清单快照，`sync.py gate-gap` 生成。

## 7. 配置项（.env 增量）

```
GATE_PAGE_SIZE=100
GATE_BACKFILL_MAX_REQUESTS=5000    # 每次回填运行的请求预算，跑完即停；0=不限
GATE_INCREMENTAL_INACTIVE_ROUNDS=6 # 连续无新增 N 轮且已离开 npp 目录 → 退出活跃集合
GATE_ACTIVE_DAYS=14                # 不在 npp 目录里的航次，最近多少天有报文才算活跃
GATE_NEW_VOYAGE_LIMIT=200          # 单轮增量里最多顺带整段回填多少个航次×方向
GATE_EXPORT_SCOPE=all              # all | active | off，全量快照可能有数百万行
```

调度时间不是配置项，在 `setup_schedule.ps1 -GateTime/-GateBackfillTime`
与 `setup_schedule.sh --gate-time/--gate-backfill-time` 上，默认 20:30 / 00:30。

## 9. 实现落点

| 设计 | 落点 |
|---|---|
| 接口封装（§1） | `client.py`：`vessel_list()` / `scodeco_page()` / `iter_scodeco()` |
| 表结构与视图（§3） | `store.py`：`GATE_SCHEMA` + `GATE_FIELDS` |
| 入库（§3 upsert 规则） | `store.py: insert_gate_events()` —— `INSERT OR IGNORE`，`verify_dup` 核对不可变前提 |
| 回填与增量（§4） | `gate.py`：`run_gate_backfill()` / `run_gate_incremental()` |
| 断点（§4.1） | `gate_voyages.gatein_done_at` / `gateout_done_at`，粒度 = 航次×方向 |
| 早停（§2 结论 2） | `gate.py: fetch_gate_unit(early_stop=True)` —— 整页无新报文即停 |
| 退休判定（§4.2 步骤 4） | `store.py: note_gate_events_seen()`，两个条件都满足才置 inactive |
| CSV（§6） | `exporter.py`：`export_gate()` / `export_gate_gap()` |
| CLI | `sync.py`：`gate-backfill` / `gate-incremental` / `gate-export` / `gate-gap` / `gate-status` |

两条管线共用 `NpediClient`（同一个 token、重试、限速）、`FileLock`（防重叠）
与 `sync_runs` 表（`kind` = `gate_backfill` / `gate_incremental`）。
`gate.py` 从 `sync.py` 导入 `preflight`，`sync.py` 则在 `dispatch()` 内部
延迟导入 `gate`，以此避开循环导入。

实现期间发现并已修正的一个坑：增量分两个阶段（阶段 1 整段回填没抓过的航次，
阶段 2 巡查活跃航次），若两阶段各自记账，一个刚在阶段 1 抓到几百条新报文的航次
会在阶段 2 被当成"本轮没动静"而累加 idle_rounds，几轮之后被误退休。
现在两个阶段的观察结果先汇总到一处，最后统一结算。

## 8. 明确不做的事

- 不调 `getCompanyCtnagent` 及任何下拉框/菜单接口。
- 不并发、不调大 pageSize、不对空结果重试。
- 不对 gate_events 做变更历史/hash 比对——报文不可变，做了是纯开销。
- 回填期间不动 npp 管线的任何逻辑；两条管线只在库和调度层面共存。
