# npedi 航次数据增量爬虫与时序分析套件

把 npedi 上所有航次的集装箱明细抓进 SQLite，并同时提供两类能力：

- 传统的 npp / 进出门（CODECO）增量与回填管线，用于维护交付状态、历史补数和 CSV 导出
- 新增的时序分析管线，用于把 vessel plan、container notice、VGM、cargo release、transshipment 和 container history 等历史接口规范化为 bronze / silver / gold 层，支撑质量报告、曲线和聚类分析

这里有三条互补的管线，共用一个库、一套认证和一套调度：

| 管线   | 数据                              | 覆盖范围                | 命令前缀                   |
| ------ | --------------------------------- | ----------------------- | -------------------------- |
| npp    | 集装箱核放比对状态（放行、发送）  | 当前数据库中的航次目录，运行 `status` 查看 | `backfill` / `incremental` |
| 进出门 | 码头进门/出门 CODECO 报文流水     | 当前数据库中的历史目录，运行 `gate-status` 查看 | `gate-*`                   |
| 时序/分析 | 历史接口快照与质量/曲线分析      | 依赖已接入的时序端点    | `crawl` / `aggregate` / `build-curves` / `cluster` |

先跑通 npp 那条；进出门是后加的，用来补 npp 已下架航次的历史数据，见[下面这节](#进出门查询是另一条管线用来补历史数据)。时序分析管线则是最近补上的能力，适合做历史快照、质量评估和曲线建模。

设计思路和接口细节见 [npp 架构文档](ARCHITECTURE.md)、[进出门架构文档](ARCHITECTURE-GATE.md) 与 [时序架构文档](NPEDI_TIMESERIES_ARCHITECTURE.md)。

## 新增的时序分析入口

最近新增的时序模块已经接入了 Alembic / SQLite 迁移、Bronze / Silver / Gold 结构以及一组离线分析脚本。单独运行某个入口时，常用命令如下：

```powershell
python npedi.py crawl vessel-plan       # 抓取 vessel plan
python npedi.py crawl container-notice  # 抓取 container notice
python npedi.py crawl transshipment     # 抓取 transshipment
python npedi.py crawl vgm                # 按箱号抓取 VGM
python npedi.py crawl cargo-release      # 抓取 cargo release
python npedi.py crawl container-history  # 按箱号补充 container history
python npedi.py aggregate                # 聚合为 Gold 层
python npedi.py build-curves             # 生成日/周曲线
python npedi.py quality-report           # 输出质量报告
python npedi.py render                   # 生成曲线 HTML
python scripts/demo_timeseries.py        # 生成演示数据与报告
```

更完整的操作说明见 [docs/timeseries-operations.md](docs/timeseries-operations.md) 与 [docs/architecture.md](docs/architecture.md)。

## 用 as-of 数据生成可回测聚类

严格回测不能直接读取当前事实表。先指定一个历史截止时刻，程序只使用在该时刻之前已经观测到的事实版本：

```powershell
python npedi.py aggregate --as-of 2026-08-01T23:59:59+00:00
python npedi.py build-curves --as-of 2026-08-01T23:59:59+00:00
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical --as-of 2026-08-01T23:59:59+00:00
python npedi.py render --as-of 2026-08-01T23:59:59+00:00
```

`--as-of` 按观测时间截断，不是简单按业务事件时间截断：一条后来才被采集到、但事件发生得更早的记录，也不会提前出现在历史回测中。`fact_vessel_plan_snapshot` 已经按快照时间保存；VGM、cargo release、transshipment 和 container history 从迁移 003 之后开始保存 append-only 版本。迁移前已经被覆盖的旧版本无法凭空恢复，但原始响应仍保存在 `raw_api_response` 中，后续可以单独重建。

## 全量箱目录、闸口历史与远程增强

全量箱号不再依赖只有 53 行的旧增强队列。`seed-container-catalog` 从已经回填完成的 `gate_events` 建立目录，并分别保存 VGM 与单箱轨迹的状态。当前主库已经建立 2,488,395 个箱号，其中 2,488,083 个通过 ISO 6346 校验；无效箱号保留用于审计，但不会发送到只能按箱号过滤的接口。

```powershell
python npedi.py seed-container-catalog  # 首次或 gate 历史明显增长后刷新
python npedi.py coverage-status         # 查看目录和两套远程增强的完成数
```

`container_event_full` 统一视图直接读取 3,659,747 条 CODECO 源记录，提供当前接口目录覆盖的 `IN_GATE` / `OUT_GATE` 历史，不再把数百万事件复制一份到事实表。这里的“完整”只表示已采集源记录全部纳入，不表示官方统计口径的全港吞吐量：接口必须逐船×航次查询，历史目录和服务端留存覆盖明显不均匀。`observed_at` 使用报文采集时间，所以严格 as-of 回测不会在“当时尚未采集”时提前看到历史事件。

闸口聚合严格按报文 `type` 判断事件。`GATE_OUT` 即使携带历史 `inGateTime` 也不会再合成第二次进门；同日、同方向、同箱号去重。图表根据 ISO 箱型首码把 20/40/45 英尺箱换算为 1/2/2.25 TEU，但标题明确使用“CODECO 接口覆盖”，不能直接和官方全港 TEU 序列拼接回测。

VGM 和 `/ediContainerlog` 仍是远程增强。两个接口都只能可靠地按单箱查询，因此 248.8 万箱各需要约 248.8 万个请求；按默认 500–1000 ms 节流，单个接口的理论下限约 22 天。脚本每批始终消费 `offset=0`：成功后待处理集合会缩小，递增 offset 反而会跳箱。

一个终端顺序跑两种增强：

```powershell
powershell -ExecutionPolicy Bypass -File .\backfill_full_container_enrichment.ps1 -Target all -BatchSize 500
```

两个终端并行时，目录只需刷新一次：

```powershell
# 终端 1
powershell -ExecutionPolicy Bypass -File .\backfill_full_container_enrichment.ps1 -Target vgm -BatchSize 500 -SkipCatalogSeed

# 终端 2
powershell -ExecutionPolicy Bypass -File .\backfill_full_container_enrichment.ps1 -Target history -BatchSize 500 -SkipCatalogSeed
```

可以随时关闭终端或重启电脑，再运行同一条命令即可续跑。脚本会检查 `crawl_run`，遇到 `partial` / `failed` 或队列不前进时立即停止，不会越过错误继续。想先验证一批可加 `-MaxBatches 1`。

### cargo-release 字段规则

官网 cargo-release 表把 `cargovolum` 标为“件数”，因此新字段使用 `piece_count`，旧 `cargo_volume` 仅为兼容保留。`grossweight` 按规则 `cargo-weight-kg-v1` 标准化为 `gross_weight_kg`：官网其他货物页面明确显示 kg，VGM 使用 kg，且本库最大值 318,014,000 只有解释为 318,014 吨的散货船提单才符合物理量级。原始值、标准化值、单位和规则版本全部保留。

仪表板主业务量仍使用放行提单数；标准化货重只在独立的“放行货重（吨）”面板展示。生成当前 CODECO 覆盖图和业务图：

```powershell
powershell -ExecutionPolicy Bypass -File .\rebuild_meaningful_dashboard.ps1
```

仪表板只需要周曲线；如需日级研究再单独运行 `python npedi.py build-curves --granularity day`。这样日常重建不会先写入上百万个日曲线点。

## 第一次部署按 probe、backfill、计划任务的顺序来

先装依赖：

```powershell
pip install -r requirements.txt
```

再探一次接口能力。这一步会确认 token 有效，并试出该用哪种采集策略，结论存进库里，后面每轮自动采用：

```powershell
python sync.py probe
```

然后做一次性的全量回填。中途断了直接重跑，已完成的航次会自动跳过：

```powershell
python sync.py backfill
```

最后注册计划任务，每天 07:30、12:30、19:30 各跑一轮增量，每周日夜里跑一次对账。

Windows 用管理员权限的 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
```

Linux 默认装 cron，服务器上建议用 systemd（有 `Persistent=true`，还能从 journal 看日志）：

```bash
./setup_schedule.sh              # cron
./setup_schedule.sh --systemd    # systemd user timer
./setup_schedule.sh --remove     # 卸载，两种都清
```

改时间用 `--times 08:00,13:00,20:00`，换解释器用 `--python /opt/py/bin/python3`。
两个平台的任务都是同一套：调 `run_sync.ps1` / `run_sync.sh`，按退出码区分"完成"、"token 失效"、"上一轮还在跑"。

cron 没有"错过就补跑"的机制，但这里不需要——增量窗口从上一次成功的水位线算起，漏掉的轮次会在下一轮自动补齐。

`probe` 会打印它选定的策略。两种策略的差别只在请求量，采到的数据一样：

| 策略         | 含义                            | 每轮增量请求数 |
| ------------ | ------------------------------- | -------------- |
| `all_in_one` | `unvessel` 留空能一把捞全部航次 | 几次           |
| `per_voyage` | 只能按航次循环（降级路径）      | 1 + 活跃航次数 |

## 日常只用得上四条命令

```powershell
python sync.py status        # 库状态、水位线、token 用龄、最近几轮运行
python sync.py incremental   # 手动补跑一轮
python sync.py reconcile     # 活跃航次全量对账，计划任务每周自动跑
python sync.py export        # 只重新导出 CSV，不联网
```

想换一个比较时间的窗口重新抓，用 `replay`：

```powershell
python sync.py replay --window 20260701000000,20260728000000
```

它只做"按窗口重抓、入库"这一件事：不推进水位线，不同步航次目录，也不顺带回填新航次。重复跑不会重复入库。

## 进出门查询是另一条管线，用来补历史数据

上面那条管线（下称 npp）的航次目录只有 848 个在册航次 —— 那是"当前还在核放流程里"的工作面，走完流程的航次会从目录里下架，历史数据也就查不到了。

站点的**进出门查询**挂的是另一份目录，实测 **13,832 个船×航次对**，数据能回溯到 **2022 年 1 月**。两份目录的重合部分只有 845 个，也就是说进出门这边多出一万三千个 npp 完全够不着的历史航次。抽查过的箱号提单号在 npp 库里确实一条都没有。

它抓的是 CODECO 报文（码头发来的进门/出门流水），和 npp 的核放状态是互补的两种数据：npp 说"这箱放行了没"，进出门说"这箱几点进的闸口"。详细设计见 [ARCHITECTURE-GATE.md](ARCHITECTURE-GATE.md)。

```powershell
python sync.py gate-status                       # 进出门库状态、回填进度
python sync.py gate-backfill --max-requests 0    # 历史回填，一口气跑完
python sync.py gate-backfill                     # 同上，但跑满 5000 次请求就停
python sync.py gate-incremental                  # 增量，计划任务每天 20:30 自动跑
python sync.py gate-gap                          # 导出 npp 缺失的箱子清单
python sync.py gate-export                       # 只重新导出 CSV，不联网
```

回填要跑十几个小时，建议放在一台常开的机器上，见[在远端机器上跑历史回填](#在远端机器上跑历史回填)。

**回填是个大活。** 13,832 个航次 × 2 个方向共 27,664 个单元，加上翻页总量六到十二万次请求，按温和的串行延时算要十几到二十几小时。实测速率约 30 单元/分钟，而且会随着航次变大而下降 —— 满载的远洋船单个航次能有近 2000 条报文（20 页）。

两种跑法：一口气跑完（`--max-requests 0`，适合常开的机器），或者每次跑一段预算就停、靠计划任务每晚 00:30 续一段。断点记在航次×方向这一级，中途断电、断网、token 失效都不丢进度，重跑同一条命令即可。

进度可以随时看：

```
python sync.py gate-status
  航次目录   : 13832（两个方向都已回填 112，inactive 0）
  待回填单元 : 27440 个航次×方向
  报文总数   : 34561（进门 34160 / 出门 401）
```

**增量很便宜。** 接口返回按报文时间倒序，所以增量只翻队头几页，撞见整页都已入库就停 —— 一个没有新报文的航次每个方向只花 1 次请求。只有活跃航次会被巡查：还在 npp 在册目录里的，加上最近 14 天有过报文的驳船航次；连续 6 轮没动静且已经离开 npp 目录的会自动退出巡查。

**报文不会改。** 每行的 `id` 里带着报文接收时间戳，天然只增不改，所以入库是纯追加（`INSERT OR IGNORE`），没有 npp 那套哈希比对和字段级变更历史 —— 那对这份数据是纯开销。增量时会顺带核对重复行的内容，真出现同 id 内容不一样会在日志里报警告，那说明这个前提破了，值得排查。

## 进出门的四个 CSV

| 文件                     | 内容                                             | 更新方式                       |
| ------------------------ | ------------------------------------------------ | ------------------------------ |
| `gate_events_all.csv`    | 全部进出门报文的当前态                           | 增量轮覆盖重写，回填轮默认跳过 |
| `gate_new_<时间戳>.csv`  | 本轮新抓到的报文                                 | 每轮一个，没新增就不生成       |
| `gate_voyages.csv`       | 进出门航次目录，含每个航次两个方向的回填进度     | 每轮覆盖重写                   |
| `gate_gap_vs_npp.csv`    | 闸口有报文、npp 核放库里查不到的箱子             | 跑 `gate-gap` 时生成           |

回填轮默认不重写全量快照：回填要连着跑好几晚、每晚若干段，每段结束都重写一份几百万行的 CSV 纯属浪费。想要就加 `--export-all`。报文总量大的话，也可以在 `.env` 里设 `GATE_EXPORT_SCOPE=active` 只导活跃航次，或 `off` 完全关掉。

`gate_gap_vs_npp.csv` 是补数效果的验收表。它按箱号、提单号、UN 号、航次四列关联两边，并且已经滤掉了 npp 的双码头空壳行（见下一节），所以缺口数不会虚高。里面绝大多数应该是 npp 已下架的历史航次 —— 那正是这条管线的意义所在。

## 在远端机器上跑历史回填

回填要连续跑十几个小时，适合放在一台一直开着的机器上。git 里只有代码，**两样东西不在仓库里，得另外传**：

| 东西            | 为什么不在 git 里         | 怎么办                            |
| --------------- | ------------------------- | --------------------------------- |
| `.env`          | 里面是有效 token          | 在远端照 `.env.example` 手写一份   |
| `npedi.sqlite`  | 300MB 以上，且是数据不是代码 | 见下面两种方案                    |

```bash
git clone https://github.com/yuuuiv/npedi.git && cd npedi
pip install -r requirements.txt
cp .env.example .env      # 然后把 WEB_TOKEN= 填上，取值方法见下面「token 失效」那节
```

**方案一：远端已经跑过 npp 的 backfill —— 什么都不用传。** 直接 `git pull` 就行。进出门那两张表会在下次打开库时自动建好，npp 的数据一行不动，**没有需要手动执行的迁移命令**。`.env` 也不用改，新增的 `GATE_*` 配置项全都有默认值。

**方案二：把本机的库整个拷过去。** 适合本机已经采了一部分、不想重跑的情况。拷之前先让 WAL 落盘，否则拷过去的库会缺最后几分钟的数据：

```bash
python -c "import sqlite3; c=sqlite3.connect('npedi.sqlite'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
scp npedi.sqlite 远端:~/npedi/          # 落盘后 .sqlite-wal/.sqlite-shm 不用拷
```

**方案三：远端从零开始。** 不用拷任何数据，但要多花约一小时把 npp 那部分重跑一遍：

```bash
python sync.py probe && python sync.py backfill
```

正式开跑之前先冒烟一下，确认 token 有效、表建好了、能采到东西。只花三四次请求：

```bash
python sync.py gate-backfill --limit 2
python sync.py gate-status        # 应该能看到航次目录 13000+、报文数不为 0
```

然后启动回填。它会一直跑到全部铺完为止，中途别关终端 —— 用 `nohup` 或 `screen` 让它脱离会话：

```bash
nohup python sync.py gate-backfill --max-requests 0 > logs/gate_full.log 2>&1 &
tail -f logs/gate_full.log        # 看进度
python sync.py gate-status        # 看还剩多少个航次×方向
```

`--max-requests 0` 是取消每轮预算，一口气跑完；不加这个参数就是每次跑 5000 次请求就停（适合挂计划任务分几晚铺）。

**中断了直接重跑同一条命令。** 断点记在航次×方向这一级，已完成的自动跳过，报文按 id 幂等入库，重复跑不会重复入库，也不会漏。断电、断网、Ctrl-C、token 失效都一样。

**最可能打断你的是 token，不是别的。** token 是服务端会话，实测寿命一天上下，而回填大概率比这久。默认配置下，失效时程序会立即停下、写 `ALERT_TOKEN_EXPIRED` 并以退出码 2 退出。配置可选的自动短信登录后，客户端会换取新 token 并只重试原请求一次；失败仍按默认方式停止，不会循环发送短信。详见 [自动短信登录](docs/auto-login.md)。

想让它自己接上，可以挂个循环——token 没换之前每 10 分钟醒来一次，看到换了就继续：

```bash
until python sync.py gate-backfill --max-requests 0; do
    [ $? -eq 2 ] || break        # 只有 token 失效（退出码 2）才等，其他错误直接停
    echo "token 失效，等你更新 .env 后自动继续…"; sleep 600
done
```

**回填期间 npp 的日常增量不受影响。** 两条管线用各自的锁文件，SQLite 开着 WAL，可以同时跑。

## 结果是 export 下的三个 CSV

都是 UTF-8 with BOM，Excel 双击打开不乱码。

| 文件                   | 内容                                                      | 更新方式                 |
| ---------------------- | --------------------------------------------------------- | ------------------------ |
| `containers_all.csv`   | 全部集装箱明细的当前态                                    | 每轮覆盖重写             |
| `changes_<时间戳>.csv` | 本轮新增和变更的行，首列 `change_type` 标 `new`/`updated` | 每轮一个，没变化就不生成 |
| `voyages.csv`          | 航次目录，截港时间已补全年份                              | 每轮覆盖重写             |

下游如果只关心变化，读 `changes_*.csv` 就够了，不用每次扫全量。

时间戳字段默认转成 `2026-07-27 10:32:01` 这种写法，因为接口原始的 14 位数字会被 Excel 显示成科学计数法。想保留原样就在 `.env` 里设 `CSV_TIMESTAMP_FORMAT=raw`。

## 一行是「箱 × 提单 × 航次 × 码头」，不是一个箱子

唯一标识要五列凑齐：`containerno`、`billno`、`unvessel`、`voyage`、`matou`。少任何一列都不唯一。

一个箱子会出现在多个航次（集装箱本来就循环用），也会挂在多个提单下（拼箱），所以按箱号聚合一定会重复计数。

更容易踩的是码头这一维。同一个箱子可能同时登记在两个码头，但通常只有一个码头真正收到了它，另一行是空壳 —— 没有 `receivetime`，也没有放行判定。**按箱统计之前，先滤掉 `receivetime` 为空的行**，否则一个箱子会被算两次，其中一次状态全是空的。

## 校验位和放行是两回事

这个接口的本质是拿货代舱单比对三份外部数据。三个校验位各对应一份，`N` 的时候配套的说明列会写明缺什么：

| 校验位       | 说明列         | 比对对象       |
| ------------ | -------------- | -------------- |
| `sldFlag`    | `sldRemark`    | 电子口岸三联单 |
| `matouFlag`  | `matouRemark`  | 码头运抵报告   |
| `customFlag` | `customRemark` | 海关放行信息   |

三个全 `Y` 才会发送，`sendTime` 有值的行无一例外。

但**放行不看这三个**。有几万行三证不全却 `passFlag=Y`、`remark` 写着"放行成功"，也有几万行三证齐全反而没放行。判断箱子的实际状态就看 `passFlag`，别自己拿三个校验位求与。

`passFlag` 有值等价于 `receivetime` 有值：没运抵就不会有放行判定，这两列同生共死。

## python analyze.py 把上面这些结论当场重算一遍

```powershell
python analyze.py              # 列画像、生命周期、不变量校验、跨码头分析、变更画像
python analyze.py lifecycle    # 只看生命周期横截面
python analyze.py changes      # 只看增量抓到的变更
python analyze.py keys         # 唯一键与函数依赖，要全表扫很多次，最慢
python analyze.py lifecycle --csv export/lifecycle.csv
python analyze.py changes --csv export/changes_detail.csv
```

它以只读方式打开库，不联网也不写库。上面每条结论它都会重新算，数据变了结论跟着变，不用信这份文档的一面之词。

列画像那节会标出全空的死列 —— 接口返回的字段里有一批从头到尾没有值，做分析时可以直接忽略。

生命周期那张表按 `compareTime` 分桶，要点是方向跟直觉相反：**箱子走完流程就不再被比对，`compareTime` 停在最后一次，所以 `compareTime` 越新，阶段反而越早**。"未运抵"比例最高的那一桶就是当前的活跃工作面。分桶锚点取库里最新的 `compareTime` 而不是写死日期，所以每轮跑出来都能横着比。

## 变更画像看的是快照里没有的东西

前面几节都在回答"现在长什么样"，`python analyze.py changes` 回答的是"这段时间动了什么"—— 那才是每天跑三轮增量换来的东西，只看全量快照根本看不到。它从 `container_history` 的字段级差异出发，给四样：

- **每轮的收成**：逐轮的新增／变更／空转／未变和请求数。连着几轮变更为 0，多半是哪里断了，不是真没变化。
- **哪些字段在动**：按变更次数排的字段榜。动得最多的那几个才是这套系统日常真正在处理的东西。
- **关键状态位的流转**：`passFlag`、`sendFlag` 等每个旧值→新值的计数。正向 `N→Y` 是流程在推进；反向 `Y→N` 数量少但要紧 —— 已经确认的状态又被推翻，正是增量窗口容易漏掉、要靠 `reconcile` 兜的那类。
- **变更最集中的航次**，以及每个箱子被改过几次。

加 `--csv` 会导出字段级明细，一行一个字段的变化（`changed_at, run_id, id, containerno, unvessel, voyage, field, old, new`）。这比 `changes_*.csv` 那种整行宽表小得多，也更适合直接喂给下游。

## token 失效时，手工换 token 或启用自动短信登录

未启用自动登录时，失效后程序会写一个 `ALERT_TOKEN_EXPIRED` 文件，不会反复重试，计划任务还会弹一个 Windows 通知。然后：

1. 在有登录态的那台机器上打开 <https://www.npedi.com/onesite/>，确认还是登录状态
2. 按 F12，在 **Application** → **Cookies** → **www.npedi.com** 里复制 `Web-Token` 的值
3. 粘到 `.env` 的 `WEB_TOKEN=` 后面
4. 跑一次 `python sync.py incremental`，成功后告警文件会自动删掉

第 2 步也可以在 **Network** 里随便点一个 `/onesite-api/` 请求，复制请求头 `ediAuthorization` 中 `Bearer ` 之后的部分，值是一样的。

中断期间漏掉的轮次不用补。增量窗口从上一次**成功**的水位线算起，停多久都会在下一轮一次补齐。

每轮开采前会先探一次活，好在第一个采集请求发出去之前就报出 token 失效，而不是抓到一半断掉。不想要这次额外请求就在 `.env` 里设 `AUTH_PROBE=false`。

## 换 token 之前，程序会先提醒你

每轮运行都会记下当前 token 的启用时间，`python sync.py status` 里能看到已经用了多少天。换过一次 token 之后，程序就知道上一个 token 活了多久；当前这个用到接近那个天数时，日志会提前一天开始提醒。这样多半能在采集断掉之前把 token 换了，而不是等告警文件出现才动手。

第一个 token 期间只记录、不提醒 —— 还没有历史寿命可以参照。

这不是 JWT refresh-token 续签：JWT 只有服务端会话 ID，没有 `exp`，站点也没有续签接口。可选自动模式会在 401/403 后完整执行一次手机号、图片验证码和短信验证码登录，再原子更新 `.env`；配置与安全边界见 [docs/auto-login.md](docs/auto-login.md)。

图片识别采用固定版本的 `anexplore/cnn_for_captcha` 定长 CNN 结构。首次启用前需要人工标注 NPEDI 自有样本并训练本地模型；不要直接打开 `AUTO_LOGIN`。完整顺序与命令也在上述文档中。

自动登录支持从 `yuuuiv/temp-mail` Worker 只读 API 或 Telegram 获取转发后的短信验证码。推荐使用 temp-mail 的邮箱专属 Address JWT；密钥不会回显，也不会进入 PowerShell 历史：

```powershell
& .\.venv\Scripts\python.exe scripts\configure_auto_login.py
& .\.venv\Scripts\python.exe scripts\check_auto_login.py
```

登录恢复后，先运行 `scripts/probe_vgm_batch.py` 验证站点的船名候选值能否用于服务端批量过滤，再决定 VGM 是按航次分页还是逐箱回填。

container-history 确认只能逐箱查询。需要缩短墙钟时间时可使用带原子队列认领的受控 worker；建议先以 2 个运行并观察是否出现 429/5xx，最多允许 4 个：

```powershell
& .\.venv\Scripts\python.exe scripts\run_enrichment_workers.py history --workers 2 --batch-size 500
```

全量回填运行期间，可另开终端持续查看有效箱覆盖率、活动 worker、错误数、吞吐和动态 ETA。停止查看不会停止爬虫：

```powershell
& .\.venv\Scripts\python.exe scripts\backfill_progress.py --watch 30
```

## 哈希没变的行完全不写库

这是"增量更新而不是重新入库"的落点。每行以接口返回的 `id` 为主键做 upsert，写之前先比全字段哈希，按结果分三条路：

| 情况                    | 处理                                                       | 记进 `sync_runs` |
| ----------------------- | ---------------------------------------------------------- | ---------------- |
| 哈希一样                | 完全不碰库                                                 | 未变             |
| 只有 `compareTime` 变了 | 写入新值保持数据新鲜，但不记历史、不进增量 CSV             | 空转             |
| 有业务字段变了          | 更新，并把哪个字段从什么变成了什么写进 `container_history` | 变更             |

所以重跑、`replay`、对账都是幂等的，不会产生重复行。

**"空转"这条路是必要的**：`compareTime` 是平台每比对一次就重刷的时间戳，实测一轮增量里 58106 条变更有 50592 条（87%）除了它什么都没动。全记成变更的话，真正有内容的 7514 条会被淹掉，`changes_*.csv` 也要大出近十倍。所以这类行照写不误（生命周期分析要靠 `compareTime` 保持最新），但不占用变更历史，`updated_at` 也不动 —— 让它保持"最后一次真变更"的含义。

要调整算空转的字段，改 `store.py` 里的 `TOUCH_ONLY_FIELDS`。

## reconcile 兜住增量抓不到的变更

按 `compareTime` 窗口取增量有两个固有缺口，各有对策：

**字段变了但 `compareTime` 没变**，比如只有 `sendFlag` 从 Y 翻成 N。窗口查询抓不到这种行，靠每周一次的 `reconcile` 兜住：对活跃航次不带时间窗口拉全量，逐行比哈希。如果这类静默变更在你的数据里很常见，把计划任务里的 `reconcile` 改成每天跑一次。

**未比对的新行（`compareTime` 为空）被窗口滤掉**。`probe` 会实测这一点，确认存在时，增量轮自动追加一次 `compareFlag=N` 的查询作兜底，只多一两次请求。

## 一轮一个日志文件

出问题时要查的是"某一轮到底发生了什么"，所以日志落两路：

| 位置                            | 内容                                           |
| ------------------------------- | ---------------------------------------------- |
| `logs/runs/<时间戳>_<命令>.log` | 单轮的完整明细，一轮一个文件，默认留最近 90 个 |
| `logs/sync.log`                 | 所有轮次连起来的流水，按 5 MB 滚动，留 5 份    |

一轮 backfill 就能写几千行，全挤在一个文件里既会被滚动切断，也没法把某一轮单独摘出来。`python sync.py status` 会在每轮后面标出它对应的日志文件名，照着去 `logs/runs/` 里找就行。

留存数量用 `.env` 里的 `LOG_KEEP_RUN_FILES` 调，设 0 表示不清理。只读命令（`status`、`export`）不会往 `logs/runs/` 里落文件。

Linux 上 cron 的输出另外收在 `logs/cron.log`；用 systemd 的话直接 `journalctl --user -u npedi-incremental`。

## 每个文件负责什么

| 文件                                      | 作用                               |
| ----------------------------------------- | ---------------------------------- |
| `config.py`                               | `.env` 解析与默认值                |
| `client.py`                               | HTTP 认证、重试、限速、翻页        |
| `store.py`                                | SQLite 建表、哈希 upsert、水位线   |
| `exporter.py`                             | CSV 导出，原子写入                 |
| `sync.py`                                 | npp 主流程与命令行入口             |
| `gate.py`                                 | 进出门回填与增量流程               |
| `analyze.py`                              | 离线数据画像，只读不联网           |
| `run_sync.ps1`、`run_sync.sh`             | 计划任务包装，含失效通知           |
| `setup_schedule.ps1`、`setup_schedule.sh` | 一键注册计划任务（Windows／Linux） |

## 别把工作目录整个传出去

`.env` 里是有效的 token，目录下还有几个含账号手机号和邮箱的本地文件，`.gitignore` 里逐条列了。git 提交不会带上它们，但复制目录、发压缩包的时候要自己留意。

## 别调大采集频率

对面是口岸政务系统。默认配置是串行请求、每次间隔随机 0.5 到 1 秒、每天三轮，请保持在这个量级，也不要加并发。
