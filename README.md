# npedi 航次数据增量爬虫

把 npedi 上所有航次的集装箱明细抓进 SQLite，每天早、中、晚各增量更新一次，每轮结束后导出 CSV。

设计思路和接口细节见 [架构文档](ARCHITECTURE.md)。

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

最后注册计划任务，每天 07:30、12:30、19:30 各跑一轮增量，每周日夜里跑一次对账。需要管理员权限的 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
```

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
python analyze.py              # 列画像、生命周期、不变量校验、跨码头分析
python analyze.py lifecycle    # 只看生命周期横截面
python analyze.py keys         # 唯一键与函数依赖，要全表扫很多次，最慢
python analyze.py lifecycle --csv export/lifecycle.csv
```

它以只读方式打开库，不联网也不写库。上面每条结论它都会重新算，数据变了结论跟着变，不用信这份文档的一面之词。

列画像那节会标出全空的死列 —— 接口返回的字段里有一批从头到尾没有值，做分析时可以直接忽略。

生命周期那张表按 `compareTime` 分桶，要点是方向跟直觉相反：**箱子走完流程就不再被比对，`compareTime` 停在最后一次，所以 `compareTime` 越新，阶段反而越早**。"未运抵"比例最高的那一桶就是当前的活跃工作面。分桶锚点取库里最新的 `compareTime` 而不是写死日期，所以每轮跑出来都能横着比。

## token 失效时，从浏览器复制一个新的贴进 .env

这是唯一需要人工介入的环节。失效时程序会立刻停下来写一个 `ALERT_TOKEN_EXPIRED` 文件，不会反复重试，计划任务还会弹一个 Windows 通知。然后：

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

自动续签这条路走不通，三个原因都确认过：token 是服务端会话，JWT 里只有一个会话 ID，没有过期时间，客户端解不出它什么时候到期；前端没有任何续签接口；登录只有手机号加短信验证码一条路。

## 哈希没变的行完全不写库

这是"增量更新而不是重新入库"的落点：

1. 每行以接口返回的 `id` 为主键做 upsert
2. 写之前先比全字段哈希，**哈希没变就直接跳过** —— 不写库、不记历史、也不进增量 CSV
3. 哈希变了才更新，同时把哪个字段从什么变成了什么写进 `container_history`

所以重跑、`replay`、对账都是幂等的，不会产生重复行。

## reconcile 兜住增量抓不到的变更

按 `compareTime` 窗口取增量有两个固有缺口，各有对策：

**字段变了但 `compareTime` 没变**，比如只有 `sendFlag` 从 Y 翻成 N。窗口查询抓不到这种行，靠每周一次的 `reconcile` 兜住：对活跃航次不带时间窗口拉全量，逐行比哈希。如果这类静默变更在你的数据里很常见，把计划任务里的 `reconcile` 改成每天跑一次。

**未比对的新行（`compareTime` 为空）被窗口滤掉**。`probe` 会实测这一点，确认存在时，增量轮自动追加一次 `compareFlag=N` 的查询作兜底，只多一两次请求。

## 每个文件负责什么

| 文件                 | 作用                             |
| -------------------- | -------------------------------- |
| `config.py`          | `.env` 解析与默认值              |
| `client.py`          | HTTP 认证、重试、限速、翻页      |
| `store.py`           | SQLite 建表、哈希 upsert、水位线 |
| `exporter.py`        | CSV 导出，原子写入               |
| `sync.py`            | 主流程与命令行入口               |
| `analyze.py`         | 离线数据画像，只读不联网         |
| `run_sync.ps1`       | 计划任务包装，含失效通知         |
| `setup_schedule.ps1` | 一键注册计划任务                 |

## 别把工作目录整个传出去

`.env` 里是有效的 token，目录下还有几个含账号手机号和邮箱的本地文件，`.gitignore` 里逐条列了。git 提交不会带上它们，但复制目录、发压缩包的时候要自己留意。

## 别调大采集频率

对面是口岸政务系统。默认配置是串行请求、每次间隔随机 0.5 到 1 秒、每天三轮，请保持在这个量级，也不要加并发。
