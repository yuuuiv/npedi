# NPEDI 项目交接说明

最后更新：2026-08-11 14:46（Asia/Shanghai）

## 一句话状态

项目正在对由完整 CODECO 闸口历史建立的 2,488,083 个有效 ISO 6346 箱号，分别执行 VGM 和 container-history 远程全量增强。两类任务都支持并发认领、断点续跑和失败回收；当前没有队列错误，预计以 container-history 为准还需约 1.7 天，即在 2026-08-13 前后完成远程采集。完成后还需要运行离线聚合、周曲线、质量报告和仪表板重建。

## 当前正在运行什么

当前后台主要是两类逐箱请求：

- VGM：查询每个箱号对应的 VGM 记录；
- container-history：查询每个箱号的历史轨迹事件。

2026-08-11 14:46 的数据库快照：

| 任务 | 已完成 | 总量 | 完成率 | 剩余 | 近 30 分钟速度 | ETA |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| VGM | 1,633,130 | 2,488,083 | 65.64% | 854,953 | 8.06 req/s | 约 1.2 天 |
| container-history | 1,475,499 | 2,488,083 | 59.30% | 1,012,584 | 6.94 req/s | 约 1.7 天 |

运行参数及资源：

- 每批认领 500 个箱号，每次必须使用 `offset=0`；完成后待处理集合会收缩，递增 offset 会跳过箱号；
- 当前请求间隔为 `REQUEST_DELAY_MS=200-400`；
- 2026-08-11 已对该间隔连续监控 30 分钟：VGM 完成 12,001 请求、history 完成 11,000 请求，新增错误、partial、failed、429 和 502 均为 0；
- 当前约有 5 个 VGM 和 5 个 history 逻辑 worker，批次切换瞬间在 `crawl_run` 中可能只显示 4 个；
- `npedi.sqlite` 约 95.42 GB，C 盘剩余约 263.93 GB，按当前增长速度足以完成本轮；
- 数据库队列错误数为 0。8 月 10 日曾遇到一次集中 HTTP 502，失败认领已释放并在后续批次补采，未造成跳箱。

实时查看进度，不会停止爬虫：

```powershell
& .\.venv\Scripts\python.exe scripts\backfill_progress.py --watch 300 --window-hours 0.5
```

查看 worker 和日志：

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'run_enrichment_workers|npedi\.py crawl (vgm|container-history)' } |
    Select-Object ProcessId, CreationDate, CommandLine

Get-Content .\logs\vgm_worker.out.log -Tail 20
Get-Content .\logs\vgm_worker2.out.log -Tail 20
Get-Content .\logs\history_workers.out.log -Tail 20
```

## 已经完成的工作

### 数据覆盖和采集可靠性

- 已从完整 `gate_events` 建立箱号目录，不再使用只有 53 个箱号的旧增强队列；目录共有约 248.84 万个箱号，其中 2,488,083 个通过 ISO 6346 校验并进入远程增强。
- container-history CLI 已支持 `--limit`、`--offset` 和 claim，不再只处理队列前 500 个箱号。
- VGM 与 container-history 分别维护 pending/claimed/complete/error 状态；并发 worker 认领互斥，中断后认领可回收，重复运行幂等。
- worker 单批失败不再立即杀死整组；只有连续失败达到阈值才停止，避免单次服务端抖动长期损失并发。
- SQLite 使用 WAL、`synchronous=NORMAL`，并把原先约每箱 26 次提交收敛到请求边界一次提交，解决多 worker 写锁瓶颈。
- HTTP 429/5xx 的退避重试已写入日志，限流和服务端抖动不再不可观测。
- 已完成 CODECO 闸口历史回填，当前 `gate_events` 约 365.97 万条；闸口事件按报文类型判断 IN/OUT，并按箱号、日期、方向去重。

### 可回测的数据模型

- vessel plan 保存不可变快照；VGM、cargo release、transshipment 和 container history 使用 append-only `fact_record_version` 保存观测版本。
- `aggregate`、`build-curves`、`cluster` 和 `render` 均支持 `--as-of`，只读取截止时间之前已经被系统观察到的版本，避免未来数据泄漏。
- 聚类支持模型版本隔离和时间截面回放，可用于 point-in-time 回测。
- 已有本地可复现性基线和验证脚本草稿，但尚未提交，见下方“未提交文件”。

### 曲线与业务口径

- 已修正“全港闸口箱流量”的含义：它表示已采集 CODECO 接口覆盖，不冒充官方全港吞吐量。
- 闸口聚合严格按报文 `type` 判断方向；`GATE_OUT` 即使携带历史入闸时间也不会合成第二次入闸。
- 20/40/45 英尺箱按 1/2/2.25 TEU 转换，并在图表中明确标注口径。
- cargo-release 的 `cargovolum` 已明确为件数；`grossweight` 按版本化规则标准化为 kg，同时保留原始值、标准化值、单位和规则版本。主业务量仍使用放行提单数，重量单独展示。
- `rebuild_meaningful_dashboard.ps1` 会重建 Gold、周曲线并输出 `export/npedi_port_dashboard.html`。

### 自动登录

- 自动登录已切换到当前 portal-api 合约，并在发送短信前先通过图片验证码，避免错误识别时反复触发短信。
- 图片验证码模型使用 354 张人工标注图片训练；固定留出集单字符准确率 86.97%，整张四位验证码准确率 57.75%。登录会刷新图片重试，不能假设一次必定识别成功。
- 短信验证码读取已接入 temp-mail Address JWT，只读轮询目标收件箱，并按收件人、发件人、NPEDI 正文标记和时间过滤。
- `scripts/check_auto_login.py` 当前所有离线检查均通过，`AUTO_LOGIN` 已开启；手机号、JWT、邮箱地址、验证码和 token 仅保存在被 Git 忽略的本地配置中。
- 尚需在合适时机执行一次真实短信端到端测试，确认“图片验证码 → 发短信 → temp-mail 收码 → 换取新 token”完整链路。该测试会真实发送短信，不应在回填正常运行时随意触发。

## 还需要做什么，以及顺序

### 1. 等远程增强完成

预计截至 2026-08-11 14:46 还需约 1.7 天。只要进度持续增长且 errors 为 0，不要重启或重复启动更多 worker。不要再缩短 `200-400 ms`；当前瓶颈主要是服务端响应，再加压收益有限且可能重新触发 502。

如果电脑重启或所有 worker 消失，可在四个 PowerShell 终端恢复到已验证过的约 5+5 并发：

```powershell
# 终端 1
& .\.venv\Scripts\python.exe scripts\run_enrichment_workers.py vgm --workers 4 --batch-size 500

# 终端 2
& .\.venv\Scripts\python.exe scripts\run_enrichment_workers.py vgm --workers 1 --batch-size 500

# 终端 3
& .\.venv\Scripts\python.exe scripts\run_enrichment_workers.py history --workers 4 --batch-size 500

# 终端 4
& .\.venv\Scripts\python.exe scripts\run_enrichment_workers.py history --workers 1 --batch-size 500
```

这些命令始终从未完成集合认领，不会从头重跑已完成箱号。

### 2. 验收全量覆盖

两个 worker 组都输出 `All available ... rows finished` 后运行：

```powershell
& .\.venv\Scripts\python.exe npedi.py coverage-status
& .\.venv\Scripts\python.exe scripts\backfill_progress.py
```

验收标准：VGM 和 container-history 均为 `2,488,083/2,488,083`，remaining=0，errors=0，claims=0。若只剩 error，不要直接把它们标记 complete；查看 `ingest_error`，修复原因后重新运行 worker。

### 3. 重建分析产物

远程采集完成后再运行，避免与 95 GB 主库争用 I/O：

```powershell
powershell -ExecutionPolicy Bypass -File .\rebuild_meaningful_dashboard.ps1
& .\.venv\Scripts\python.exe npedi.py quality-report
```

这一步会依次执行 Gold 聚合、周曲线和 HTML 渲染。首次基于完整数据运行尚无可靠耗时基准，应预留数小时；不要在没有证据时宣称已经完成。

需要回测时必须指定同一个 `--as-of`：

```powershell
& .\.venv\Scripts\python.exe npedi.py aggregate --as-of 2026-08-01T23:59:59+00:00
& .\.venv\Scripts\python.exe npedi.py build-curves --granularity week --as-of 2026-08-01T23:59:59+00:00
& .\.venv\Scripts\python.exe npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical --as-of 2026-08-01T23:59:59+00:00
& .\.venv\Scripts\python.exe npedi.py render --granularity week --as-of 2026-08-01T23:59:59+00:00
```

### 4. 验证自动登录

先做无副作用的离线检查：

```powershell
& .\.venv\Scripts\python.exe scripts\check_auto_login.py
```

只有在允许真实发送一条短信时再执行：

```powershell
& .\.venv\Scripts\python.exe scripts\test_auto_login.py --request-sms
```

若真实测试失败，保持现有 Web-Token，不要反复请求短信；按 `docs/auto-login.md` 分别测试 CAPTCHA 推理和 temp-mail 读取。

## 已知限制

- VGM 与 container-history 官方接口只支持按单箱可靠过滤；全覆盖必须各发约 248.8 万个请求，没有可用的批量参数。
- cargo-release 在线接口会忽略已测试的船名/航次过滤条件，因此不能声称已完成可信的全港 cargo-release 全量回填。现有历史记录可用于其已观测范围内的分析和回测。
- point-in-time 回测保证“当时已经采集到什么就只能看到什么”，不保证系统在早期截止时刻已经回填完现实世界中此前发生的全部事件。晚采集到的旧事件不会泄漏进较早 cutoff。
- CAPTCHA 当前整图准确率为 57.75%，可靠性依赖刷新图片重试；增加人工标注样本并重新做固定留出集评估，仍是提升自动登录稳定性的主要路径。
- `npedi.sqlite`、`.env`、验证码样本、模型权重和运行日志均属于本地状态，不应提交 Git。

## 仓库和未提交文件

- 本文创建前，`main` 与 `origin/main` 同步在提交 `ae9b12a`。
- `docs/reproducibility/baseline-20260810T045302Z.json` 与 `scripts/verify_reproducible.py` 是已有的未跟踪实验文件，不属于本 handover 提交范围；其中脚本的部分注释存在编码异常，需审查、修复和验证后再单独提交。
- 推送前应确认 `.env`、数据库、WAL、模型、样本和日志仍被 `.gitignore` 排除。

