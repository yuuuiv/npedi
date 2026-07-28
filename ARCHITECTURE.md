# npedi.com 航次数据增量爬虫 — 架构设计

> 本文档面向实现者（人或 AI model）。所有接口事实均从 `www.npedi.com.har` 中还原，
> 无需再解析 HTML 页面（`EDI统一服务平台.html` 仅作参考，数据全部走 JSON API）。

## 0. 目标

1. 把全部航次（约 855 个）的集装箱明细全部入库（一次性全量回填）。
2. 每天早、中、晚各定时运行一次**增量更新**：只拉取变化的数据、只更新变化的行，不重复入库。
3. 网络请求次数尽可能少。
4. "比对时间"（compareTime）查询窗口可配置。

## 1. 接口清单（唯一需要访问的端点）

Base URL: `https://www.npedi.com`

### 认证（所有请求必带）

```
ediAuthorization: Bearer <TOKEN>          # TOKEN 来自 .env 的 Web-Token
Referer: https://www.npedi.com/onesite/npp/infor/integrate
Accept: application/json, text/plain, */*
User-Agent: <正常浏览器 UA>
```

- Token 是 HS512 JWT，payload 仅含 `login_user_key`（无 exp），有效期由服务端 session 控制。
- 登录态在另一台机器上，**本程序无法自动续签**。失效时（HTTP 401 或响应 `code != 200`）
  必须：立即停止本轮任务 + 记录日志 + 显式告警（如写入告警文件/发通知），等人工更新 .env。
  失效后严禁重试轰炸。

#### Token 的获取与更新流程（人工操作，从前端 JS 逆向确认）

前端逻辑（app.js）：登录成功后站点写入 cookie `edi-token`，SPA 启动时把它复制为
cookie `Web-Token`；此后每个 API 请求由 axios 拦截器读取 `Web-Token` cookie，
拼成请求头 `ediAuthorization: Bearer <token>` 发出。**服务端只认这个请求头，
不需要携带任何其他 cookie**（HAR 中的成功请求已验证）。

因此人工取 token 的步骤：

1. 在有登录态的那台机器上，用浏览器打开 https://www.npedi.com/onesite/ 并确认已登录
   （本账号为手机号+短信验证码登录，loginType=DX，**这也是无法自动化续签的原因**）。
2. F12 → Application → Cookies → `www.npedi.com`，复制 `Web-Token`（或 `edi-token`）的值。
   （替代方式：F12 → Network 任选一个 `/onesite-api/` 请求，复制请求头
   `ediAuthorization` 中 `Bearer ` 后面的部分，两者相同。）
3. 粘贴到本机 `.env` 的 `WEB_TOKEN=`，下一轮任务自动生效。

有效期未知（JWT 无 exp，取决于服务端 session/Redis 过期策略），实现上按
"用到失效为止"处理：程序检测到 401 → 写 `ALERT_FILE` 并退出，人工重复上述步骤。
可用 `GET /onesite-api/getInfo` 作为轻量的 token 有效性探活（每轮开始时调 1 次，
返回 `code:200` 且含用户信息即有效）。

### 1.1 航次目录（每轮 1 次请求）

```
GET /onesite-api/common/... 不需要。
GET /onesite-api/npp/search/vesselinfo
→ {"code":200, "data":[ {unvessel:"UN9604122/071E", voyage:"071E",
     envessel:"EVERLOTUS/071E(01-01 00:00)", portclosetime:"01-01 00:00", ...}, ... ]}
```

- 一次返回全部在册航次（HAR 中为 855 条），无分页、无参数。
- `unvessel` 字段格式为 `UN号/航次`，查询明细时只取斜杠前半部分。
- `envessel` 中括号内、以及 `portclosetime` 是截港时间（只有月-日 时:分，需自行补年份，
  注意跨年：如当前 7 月出现 01-01，按临近原则判断年份）。

### 1.2 集装箱明细（核心数据源）

```
GET /onesite-api/npp/search/integrated
    ?pageNum=1&pageSize=200
    &matou=&agent=&envessel=
    &unvessel=UN9604122          # UN 号（不含 /航次 后缀）
    &voyage=071E
    &containerno=&billno=
    &compareTime=20260721110833,20260729110833   # yyyyMMddHHmmss,yyyyMMddHHmmss
    &passFlag=&sendFlag=&compareFlag=
→ {"code":200, "data":{ pageNum, pageSize, total, totalPages, list:[ ... ] }}
```

每行关键字段：

| 字段 | 含义 | 用途 |
|---|---|---|
| `id` | 数值主键（如 105409364） | **入库主键 / upsert 依据** |
| `containerno` / `billno` | 箱号 / 提单号 | 业务自然键（备用唯一约束） |
| `unvessel` / `voyage` / `envessel` | 航次标识 | 外键关联航次表 |
| `compareTime` | 海关比对时间 | **增量水位线（watermark）** |
| `sendTime` / `receivetime` / `rktime` / `loadtime` | 各环节时间戳 | 状态跟踪 |
| `passFlag` / `sendFlag` / `compareFlag` / `customFlag` / `sldFlag` / `matouFlag` / `ifcsumFlag` | 各环节放行标志 Y/N | 状态跟踪 |
| `status` / `remark` | 状态码 / 文案（如 "放行成功"） | 状态跟踪 |

分页：`pageSize=200`（与页面一致，不要调大试探），按 `total` 计算页数循环 `pageNum`。

### 1.3 汇总接口 `searchcountAll` — **不要调用**

只返回一句统计文案（"合计:687票提单,共684个集装箱..."），数据从明细即可自行聚合，
调用它纯属浪费请求数。

## 2. 需实现者首先验证的两个假设（各花 1 次请求）

1. **`unvessel`/`voyage` 留空是否返回全部航次的明细**（UI 表单允许留空）。
   若成立，增量更新每轮只需 `1 + ceil(变化行数/200)` 次请求，是最省流量的路径，
   整个架构优先采用；若不成立，退回按航次循环（见 §4 降级策略）。
2. **`compareTime` 窗口过滤的语义**：确认它按行的 `compareTime` 字段过滤，
   且窗口可放宽（如 30 天）。同时确认 `compareTime` 为空的行（未比对的新箱）
   在窗口过滤下是否返回——若被过滤掉，增量轮必须补一个"空 compareTime"的
   兜底策略（见 §5 安全网）。

## 3. 存储设计（建议 SQLite，单机零依赖）

```sql
-- 航次目录
CREATE TABLE voyages (
  unvessel     TEXT NOT NULL,     -- UN9604122
  voyage       TEXT NOT NULL,
  envessel     TEXT,              -- 船名
  portclose_at TEXT,              -- 补全年份后的 ISO 时间
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,    -- 最近一次出现在 vesselinfo 中
  backfilled    INTEGER DEFAULT 0,-- 是否已完成全量回填
  PRIMARY KEY (unvessel, voyage)
);

-- 集装箱明细（当前态）
CREATE TABLE containers (
  id           INTEGER PRIMARY KEY,   -- 接口返回的 id
  unvessel     TEXT, voyage TEXT,
  containerno  TEXT, billno TEXT,
  -- ...其余字段照单全收...
  row_hash     TEXT NOT NULL,         -- 全字段规范化后的哈希，用于变更检测
  first_seen_at TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX idx_containers_voyage ON containers(unvessel, voyage);

-- 变更历史（可选但推荐：满足"调整比较时间"的历史回溯需求）
CREATE TABLE container_history (
  id INTEGER, changed_at TEXT, changed_fields TEXT /* JSON: {字段:[旧,新]} */
);

-- 同步运行记录 + 水位线
CREATE TABLE sync_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,               -- 'backfill' | 'incremental'
  started_at TEXT, finished_at TEXT,
  watermark_from TEXT, watermark_to TEXT,   -- 本轮 compareTime 窗口
  requests_made INTEGER, rows_upserted INTEGER, rows_changed INTEGER,
  status TEXT, error TEXT  -- 'ok' | 'auth_expired' | 'failed'
);
```

**Upsert 规则（增量的核心，保证"不重新入库"）：**
1. 以 `id` 为主键 `INSERT ... ON CONFLICT(id) DO UPDATE`。
2. 更新前先比 `row_hash`：hash 相同 → 跳过（不写、不记历史）；
   hash 不同 → 更新行 + 把字段差异写入 `container_history`。

## 4. 采集流程

### 4.1 首次全量回填（一次性）

1. 调 `vesselinfo` → upsert 全部航次到 `voyages`。
2. 若 §2 假设 1 成立：留空 `unvessel`，`compareTime` 给一个足够宽的窗口
   （或验证留空是否可行），全量翻页入库。
3. 否则：逐航次循环（855 个），每航次 `pageSize=200` 翻页；
   每完成一个航次将 `backfilled=1`（**支持断点续爬**：中断后跳过已回填航次）。
4. 请求间隔加 0.5–1s 随机延时，串行执行，不并发（对方是口岸政务系统，务必温和）。

### 4.2 每日三次增量（早/中/晚，核心流程）

```
1. 读取上次成功 run 的 watermark_to（首次 = 回填完成时刻）
2. 本轮窗口 = [watermark_to - OVERLAP, now]     # OVERLAP 默认 2h，可配置
3. 调 vesselinfo（1 次）→ 同步航次目录；发现新航次 → 标记待回填并在本轮直接回填
4. 调 integrated，compareTime=本轮窗口：
   - 优先：留空 unvessel 一把捞（假设 1 成立时）
   - 降级：只对"活跃航次"循环 —— 活跃 = 仍出现在 vesselinfo 中，
     或截港时间在 [now - 7d, now + 14d] 内；已离港且连续 N 轮无变化的航次跳过
5. 逐行 upsert（hash 比对，只写变化）
6. 写 sync_runs，watermark_to = now（不是 max(compareTime)，避免时钟/乱序问题，
   靠 OVERLAP 重叠窗口保证不漏）
```

**请求量估算**：假设 1 成立时，每轮 ≈ 2 + 变化数据页数（通常个位数）；
降级方案下每轮 ≈ 1 + 活跃航次数 × 平均页数，仍远小于 855 全量。

### 4.3 调度

- Windows 单机：Task Scheduler 三个触发器（如 07:30 / 12:30 / 19:30），
  运行 `python sync.py incremental`；比常驻进程 + APScheduler 更抗断电/重启。
- 幂等：脚本入口加文件锁，防止两轮重叠执行。

## 5. 安全网（防增量漏数据）

增量按 `compareTime` 过滤有两个已知风险，各配一个对策：

1. **行更新但 compareTime 未变**（如只有 sendFlag 翻转）→ 每周一次（或每晚一次，
   视数据量）对"活跃航次"做小全量校对：不带 compareTime 窗口拉取活跃航次全量，
   与库中 hash 对账。
2. **compareTime 为空的新行被窗口过滤掉**（待 §2 假设 2 验证）→ 若确认存在，
   增量轮对活跃航次追加一次"空窗口/宽窗口"查询作兜底。

## 5.5 CSV 导出层（最终交付格式）

SQLite 仍是唯一的真实数据源（增量 upsert 必须依赖它）；**CSV 是每轮同步结束后
从库里导出的产物**，不要让爬虫直接写 CSV（否则无法做增量去重）。

每轮成功同步后导出到 `EXPORT_DIR`：

1. `containers_all.csv` — 全量当前态快照，每轮覆盖重写。
   列 = `containers` 表全部业务字段 + `first_seen_at` / `updated_at`。
2. `changes_<YYYYMMDD_HHMMSS>.csv` — 本轮新增/变更的行（含一列 `change_type`:
   new/updated），每轮一个文件，便于下游只消费增量；本轮无变化则不生成。
3. `voyages.csv` — 航次目录快照，每轮覆盖重写。

格式要求：

- 编码 **UTF-8 with BOM**（`utf-8-sig`），否则中文 Windows 上 Excel 打开乱码。
- 箱号/提单号/手机号等全部按文本写出，不做数值转换（防 Excel 吃前导零/科学计数法）。
- 覆盖重写用"写临时文件 + 原子改名"方式，避免下游读到半个文件。

## 6. 配置项（.env / config）

```
WEB_TOKEN=...                      # 已有
BASE_URL=https://www.npedi.com
PAGE_SIZE=200
COMPARE_WINDOW_OVERLAP_HOURS=2     # 增量窗口回看重叠
COMPARE_WINDOW_MANUAL=             # 手动指定 compareTime 窗口（"起,止"，用于补数/重放）
SCHEDULE=07:30,12:30,19:30
REQUEST_DELAY_MS=500-1000
DB_PATH=./npedi.sqlite
EXPORT_DIR=./export                # CSV 输出目录（见 §5.5）
ALERT_FILE=./ALERT_TOKEN_EXPIRED   # token 失效时创建此文件并写明时间
```

`COMPARE_WINDOW_MANUAL` 即满足"可调整比较时间的选择"：命令行/配置指定任意窗口
重跑一轮，upsert 幂等保证不会重复入库。

## 7. 技术选型建议

- Python 3.11+，`httpx`（或 requests）+ 内置 `sqlite3`，无框架、单文件到三文件即可：
  `client.py`（HTTP+认证+重试）、`store.py`（SQLite+upsert）、`sync.py`（流程+CLI）。
- 重试策略：网络错误/5xx 指数退避重试 3 次；**401/鉴权失败不重试**，直接告警退出。
- 日志：每轮打印请求数、新增/变更/跳过行数，与 `sync_runs` 表一致。

## 8. 明确不做的事

- 不请求任何 HTML/JS/CSS/图片静态资源。
- 不调 `searchcountAll`、`getInfo`、`getRouters`、`allMenu` 等页面辅助接口。
- 不并发、不调大 pageSize、不高频轮询 —— 三次/天 + 温和串行已满足需求。
