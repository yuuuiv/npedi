# NPEDI 项目交接说明

最后更新：2026-08-19 10:40（Asia/Shanghai）

## 一句话状态

远程增强（VGM、container-history）已经全部跑完；CODECO 历史回填做到 82%，
**从 2026-08-18 19:35 起停摆至今**，卡在 token 上，不是代码问题。
恢复需要人手工换一次 Web-Token。

## 现在卡在哪

2026-08-18 19:35:30，回填在处理 2021-07-01/02 的候选时收到 401，包装脚本按设计
重试两次自动登录、都被拒，于是保留断点退出。此后没有任何采集进程在跑，
`ALERT_TOKEN_EXPIRED` 一直挂着没人处理。

```
token 失效：automatic login failed: getSms refused the request:
此账号已被停用，请联系您公司管理员或咨询 0574-27681890
```

### 两件事要分清楚

**一、自动登录从来没有成功过一次。** `auth_token_event` 里 `refresh_detected`
事件数为 **0**。08-18 那三次"恢复"全部是人工把新 token 贴进 `.env`
（10:28、13:27、16:07 三次，`.env` 的 mtime 和 `manual_login2.log` 里的
`LOGIN OK fingerprint: 429dd472…` 都能对上）。脚本里的 `AUTO_LOGIN` 是
`run_gate_history_backfill.ps1:63` 用进程环境变量临时打开的，`.env` 里始终是
`false`，进程一退出就失效——所以现在直接跑 `sync.py` 也不会自动重试登录。

**二、真正在恶化的是会话寿命，不是账号被永久封。** `auth_token_observation`
记录的每个 token 实测存活：

| token 指纹 | 首次成功 | 最后成功 | 存活 | 期间请求数 |
| --- | --- | --- | ---: | ---: |
| `60dd1f39` | 08-13 17:40 | 08-16 21:44 | 3 天 4 小时 | 1,141,295 |
| `b7c4dd37` | 08-17 10:49 | 08-18 00:58 | 14 小时 | 251,681 |
| `429dd472` | 08-18 10:28 | 08-18 11:42 | 1 小时 14 分 | 41,953 |
| `9b1f3129` | 08-18 13:27 | 08-18 15:08 | 1 小时 41 分 | 56,954 |
| `6d861041` | 08-18 16:07 | 08-18 19:29 | 3 小时 21 分 | 59,930 |

第一个 token 撑了 114 万次请求 / 3 天；08-18 的三个平均只撑到 4–6 万次请求就被
判失效。**服务端现在按请求量掐会话，阈值大约 4–6 万次**，6 workers 跑下来就是
1–3.5 小时一断。

同时 `/user/getSms` 一直返回"此账号已被停用"，所以脚本没法自己续。
但 08-18 10:27 的**人工登录是成功的**（`manual_login2.log` 有 `LOGIN OK`），
说明账号本身还能登，被挡住的是脚本走的那条发短信路径。

### 怎么恢复

先手工换 token，别急着打客服电话：

1. 在有登录态的机器打开 <https://www.npedi.com/onesite/>
2. F12 → Application → Cookies → `www.npedi.com`，复制 `Web-Token`
   （或在 Network 里任取一个 `/onesite-api/` 请求，复制 `ediAuthorization` 里
   `Bearer ` 之后的部分）
3. 贴到 `.env` 的 `WEB_TOKEN=`
4. `python sync.py gate-status` 验证，成功后 `ALERT_TOKEN_EXPIRED` 会自动删掉

只有当**网页端登录本身**也提示账号停用时，才需要联系管理员或 0574-27681890。

按现在 4–6 万请求就断一次的节奏，剩下的 8.5 万个候选中途还要断十几次，
每次都得人工。要么接受这个节奏分多天跑，要么先想办法把自动登录修通。

## 回填进度

### CODECO 历史候选（主线）

候选总数 474,018，已探测 388,520（**82.0%**），剩 85,498 待探测。

| 状态 | 数量 | 含义 |
| --- | ---: | --- |
| `complete` | 211,559 | 全部页已入库 |
| `empty` | 176,956 | 精确查询下接口返回 0 行（是事实，不是失败） |
| `pending` | 85,498 | 还没点查 |
| `hit` | 5 | 见下 |

5 个 `hit` 里有 2 个是 `UN7654321`（中转梅山聚合桶，gatein_total 分别是 98,000 和
1,633,402），已经在 `gate_history_rejected_pair` 里被侧车正确隔离，队列不会再碰，
只是 candidate 表的状态字段没回写。**真正要续的是另外 3 个 2021 年的航次**，
它们在 19:34–19:35 被 token 失效掐断：

| 船码 | 航次 | ETA | 已入 IN | 已入 OUT |
| --- | --- | --- | ---: | ---: |
| `CN3104194` | 21047S | 2021-07-01 | 174 | 184 |
| `UN9437567` | JK227N | 2021-07-02 | 0 | 1,102 |
| `UN9168855` | W0519 | 2021-07-02 | 1,193 | 526 |

按 `last_eta` 从新往旧探测，2022 及以后都已收尾，现在停在 2021 年 7 月初：

| ETA 年份 | 待探测 | 候选总数 | 完成度 |
| --- | ---: | ---: | ---: |
| 2026 | 26 | 42,313 | 99.9% |
| 2025 | 83 | 81,277 | 99.9% |
| 2024 | 50 | 72,133 | 99.9% |
| 2023 | 51 | 85,006 | 99.9% |
| 2022 | 45 | 78,119 | 99.9% |
| 2021 | 27,418 | 57,345 | 52.2% |
| 2020 | 57,825 | 57,825 | 0% |
| 合计 | **85,498** | 474,018 | 82.0% |

最后一段用 6 workers、每 worker 间隔 1000–1250 ms，实测 **约 4,900 候选/小时**
（08-18 17:00 和 18:00 两个完整小时分别是 4,999 和 4,832）。照这个速率剩余
85,498 个需要 **约 17.4 小时净运行时长**。这是净时长，不含换 token 的停摆；
按 08-18 的中断频率，墙钟时间要按两三天估。

### 远程增强：已完成

| 任务 | complete | pending | error |
| --- | ---: | ---: | ---: |
| VGM | 2,488,083 | 0 | 0 |
| container-history | 2,488,083 | 0 | 0 |

另有 264 个箱号过不了 ISO 6346 校验，保留审计，不发远端。
这个 100% 的分母是**已采集 gate 目录衍生出的箱号集**，不是全港箱号全集。

## 08-13 之后做完了什么

### CODECO 历史回填从小批探测变成了主线工程

交接文档上一版还在说"2026-06 单月、4 workers、12,346 个 pending"。之后可信窗口
一路扩到 2023-01 ~ 2026-06 全量，跑完又往前补 2022、2020–2022。数据量级的变化：

| | 08-13 | 现在 |
| --- | ---: | ---: |
| `gate_voyages` | 14,318 | **225,877** |
| `gate_events` | 3,660,438 | **96,258,674** |
| 候选队列 | 12,346 pending | 474,018 总量 / 85,498 pending |

这一条把"只覆盖 2026-07-29/30 `vesselList` 快照目录"的老限制基本解掉了——
但 2020–2021 还没跑完，跨年比较仍然不安全，见下面「已知限制」。

### 异常报文侧车已经完整跑完

`gate_anomaly_sidecar.py` 把虚拟船、运营聚合桶这类报文单独存进
`gate_anomaly_sidecar.sqlite`，不混进物理船的 `gate_events`。当前状态：

```
候选 358 ｜ job complete 716 / error 0 / pending 0 ｜ 页 18,226 ｜ 行 1,753,609 ｜ 3.5 GiB
```

隔离表 `gate_history_rejected_pair` 共 3,555 条，其中占位船码 3,016、
运营聚合桶 352、非法航次号 181。这些键**不会发给远端**——服务端如果忽略非法过滤条件，
返回行会被错误归到占位船码上，那比少采更糟。

### 新增的运维件

- `run_gate_history_backfill.ps1`：分 chunk 跑、断点保留、遇 401 有限次重启。
- `scripts/history_backfill_watchdog.py`：回填异常时发邮件告警，08-18 四次中断都
  正常告警了（事件 ID 可在各 `logs/history_backfill_watchdog_*.err.log` 里查）。
- `scripts/gate_anomaly_watchdog.py`：盯侧车 supervisor。
- 两个 Streamlit 面板，见 [DASHBOARD.md](DASHBOARD.md)。
- `migrations/010`：`auth_token_observation` / `auth_token_event`，只存 token 的
  SHA-256 指纹，不存 token 本身。上面那张会话寿命表就是靠它才能算出来。

## 接下来按这个顺序

### 1. 换 token，恢复回填

```powershell
# 换完 .env 里的 WEB_TOKEN 之后
powershell -ExecutionPolicy Bypass -File .\run_gate_history_backfill.ps1 `
  -EtaStart 2020-01-01 -EtaEnd 2022-12-31
```

断点已存，不会重扫已完成的部分。3 个半成品航次会被优先续完。
只起一个 CLI 进程，6 workers 由它内部调度，**不要在后台叠多个 CLI**。

### 2. 跑完 2021 和 2020

净时长约 17.4 小时，但要按多次中断规划。跑完之后查一次：

```powershell
python sync.py gate-status
```

### 3. 重建 gate 聚合

**`agg_gate_daily` 现在还是 2,744 行、只覆盖 2021-04-16 ~ 2026-07-30，大回填之后
从来没重建过。** 9,625 万条 CODECO 事件对应 2,744 行日聚合，面板上那条曲线是失真的，
演示前必须先重跑：

```powershell
powershell -ExecutionPolicy Bypass -File .\rebuild_meaningful_dashboard.ps1
```

主库 168 GiB，这一步要预留数小时，别和采集抢 I/O。

### 4. 代码已提交（2026-08-19）

08-13 之后六天的活分两个提交推上了 `main`：`e092ec4`（CODECO 全量补爬 + 异常报文
侧车 + 两个面板 + 文档）、`f375803`（回填包装脚本 + 断连 watchdog）。
提交前顺带修了三个真问题：

- `dashboard.py` 原来是**可写**连接（`sqlite3.connect(r"npedi.sqlite")`），
  没有 `mode=ro`、没有 `query_only`，而运维面板的"数据查询"页签是个自由 SQL
  输入框——对着 168 GiB 主库，谁在那儿敲一句 `DELETE` 就真的执行了。
  改成和 `report_dashboard.py` 一样的 `mode=ro` + `PRAGMA query_only=ON`，
  验证过 DELETE/DROP/CREATE 全部被 SQLite 自己挡下。
- `run_dashboard.sh` 写的端口是 8080，和 `run_report_dashboard.ps1` 撞车；
  改成和 Windows 版一致的 8081。
- `history_backfill_watchdog.py` 和 `run_gate_history_backfill.ps1` 把告警邮箱
  硬编码成了个人 Gmail 地址。改成读 `NPEDI_ALERT_EMAIL`（环境变量优先，
  其次 `.env`，未配置时启动即报错，不会静默发到错误邮箱）；
  ps1 那边原来无论有没有配都会显式传 `--to ''` 把 Python 自己的兜底覆盖掉，
  一并修了。要用告警就在 `.env` 里配一行 `NPEDI_ALERT_EMAIL=`。
- `.gitignore` 补了 `*.db`（`npedi.db`/`store.db` 那类 0 字节误建文件）和
  `.sync.gate.sidecar.lock`（文件名和原来写的对不上，没被排除）。

提交前跑过 68 个测试全过，且确认 `.env`、`*.sqlite`、日志、验证码样本都没有
被带进 git（`git check-ignore` 逐个核对过）。

### 5. 有空再修自动登录

现在这条路径 100% 失败。修通之前，每次会话失效都要人守着。
离线检查（无副作用）：

```powershell
& .\.venv\Scripts\python.exe scripts\check_auto_login.py
```

真实短信测试会实际发一条短信，别在回填正常跑的时候触发。

## 已知限制

- **2020–2021 还没跑完，CODECO 跨年趋势仍然不可比。** 2022 及以后完成度 99.9%，
  2021 只有 52.2%，2020 是 0。现在画跨年曲线，2020–2021 的低值是没采到，
  不是业务量低。
- **`empty` 不等于失败。** 17.7 万个 `empty` 是"接口在精确的船×航次×方向查询下
  确实返回 0 行"，是关于世界的事实。报告里不要说成采集失败。
- **VGM / container-history 的 100% 只针对已采集 gate 目录衍生的箱号集**，
  不能外推成全港或全历史覆盖率。两个接口都只能按单箱查，248.8 万箱各要 248.8 万次
  请求，没有批量参数。
- **cargo-release 的在线接口会忽略船名/航次过滤**，所以不能声称完成了可信的全量回填。
  现有记录只在其已观测范围内可用。
- **可回溯的时间下界是 2026-08-04**，`fact_record_version.observed_at` 的实测最早值。
  在此之前只有当前投影，没有"当时看到的样子"，as-of 重建不了 7 月的视图。
- **聚类当前是退化的**（silhouette = 0.0，18 个实体里 12 个特征全零），根因是周曲线
  被 71% 的零填充桶稀释。细节和四步修法见
  [docs/report_brief.md](docs/report_brief.md) §3，别把现在的聚类结果当成果展示。
- **CAPTCHA 整图准确率 57.75%**（354 张标注样本训练，固定留出集），依赖刷新重试。
- `npedi.sqlite`（168 GiB）、`gate_anomaly_sidecar.sqlite`（3.5 GiB）、`.env`、
  模型权重、验证码样本、日志都是本地状态，不进 Git。

## 常用命令

```powershell
# 队列进度，不会停爬虫
& .\.venv\Scripts\python.exe scripts\backfill_progress.py --watch 300 --window-hours 0.5

# CODECO 回填状态
python sync.py gate-status

# 侧车状态
& .\.venv\Scripts\python.exe .\gate_anomaly_sidecar.py status --json

# 看有没有爬虫在跑
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python(w)?\.exe$' } |
    Select-Object ProcessId, CreationDate, CommandLine

# 日志
Get-Content .\logs\gate_history_backfill_wrapper.log -Tail 30
Get-Content .\logs\sync.log -Tail 40
```

`coverage-status` 会扫大体量 gate 事实表，要跑几分钟；只想看队列就用
`scripts/backfill_progress.py`。
