# 两个 Streamlit 面板：一个给运维看，一个给报告用

| 面板 | 脚本 | 端口 | 连接方式 | 用途 |
| --- | --- | --- | --- | --- |
| 运维 | `dashboard.py` | 8081（Windows） | **可读写** | 自己排查队列、临时跑 SQL |
| 报告 | `report_dashboard.py` | 8080 | 只读 | 演示、给别人看 |

```powershell
.\run_dashboard.ps1          # 运维，8081
.\run_report_dashboard.ps1   # 报告，8080
```

两个可以同时开。给别人看、或者在报告现场投屏，**用 8080 那个**，理由见下一节。

## 运维面板会以可写方式打开 180 GB 主库

`dashboard.py:14` 是 `sqlite3.connect(r"npedi.sqlite")` —— 没有 `mode=ro`，没有
`query_only`，也没有 busy timeout。而「数据查询」页签是个自由输入的 SQL 框，
输进去什么就执行什么。所以：

- 在这个面板里敲一句 `DELETE` 或 `DROP` 会**真的作用在主库上**，没有任何拦截。
- 没有 busy timeout，爬虫一写就可能直接抛 `database is locked`。
- 路径是相对的，必须在项目根目录启动，否则会新建一个空库。

`report_dashboard.py` 用的是 `mode=ro` + `PRAGMA query_only=ON` + `timeout=30`，
不可能写到主库。**所以只有 8080 那个适合给别人操作。**

改成只读是一行的事（`dashboard.py:14`）：

```python
return sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=30)
```

改完自定义 SQL 里的写操作会直接报错——这正是想要的效果。

## 两个启动脚本的端口对不上

`run_dashboard.ps1` 用 8081，但 `run_dashboard.sh` 里写的是 8080 —— 和报告面板撞车。
在 Linux 上先起报告面板再起运维面板会起不来。要用 `.sh` 的话先把端口改成 8081。

## 运维面板的四个页签

**回填进度**：VGM、container-history 两条远程增强的完成度，以及 CODECO 候选队列
按年份的待探测数和完成度，附带按当前速率推的完成时间。注意这个 ETA 是**净运行时长**，
不含 token 失效导致的停摆，实际墙钟时间要长得多。

**聚类分析**：`cluster_run` 历史、silhouette 分数、簇分配和离群点，可导出离群点清单。
当前聚类结果是退化的（silhouette = 0.0），原因见
[docs/report_brief.md](docs/report_brief.md) §3，别直接拿来展示。

**实时监控**：最近 24 小时的运行次数、请求数、行数、平均耗时和逐轮日志。
连着几轮请求数为 0，基本就是 token 失效了，去看 `ALERT_TOKEN_EXPIRED`。

**数据查询**：预设查询加自由 SQL，结果可导 CSV。见上面的可写警告。

## 状态词的含义

VGM / container-history 队列：

| 状态 | 含义 |
| --- | --- |
| `pending` | 待查 |
| `complete` | 已拿到数据 |
| `error` | 查询失败，可重试 |
| `invalid` | 箱号过不了 ISO 6346 校验，不会发给远端，仅留档 |

CODECO 候选队列：

| 状态 | 含义 |
| --- | --- |
| `pending` | 还没点查过 |
| `hit` | 点查有数据，但整条页链路还没入完，需要续跑 |
| `empty` | 精确的船×航次×方向查询下接口返回 0 行 |
| `complete` | 全部页已入库 |

`empty` 和 `complete` 都是终态。**`empty` 不是失败**，它是"服务端确实没有这条数据"
这个事实本身，报告里不要说成失败。

## 排查

**打不开库**：确认在项目根目录启动，`npedi.sqlite` 就在那儿。目录下的 `npedi.db`
和 `store.db` 是 0 字节的误建文件，不是数据库。

**查询卡住**：主库 180 GB，`gate_events` 九千多万行，`COUNT(*)` 要跑一两分钟。
自定义 SQL 记得带 `LIMIT`，避免全表 JOIN。

**局域网访问不了**：两个脚本都监听 `0.0.0.0`，用 `http://<本机IP>:<端口>`。
访问不了先看防火墙有没有放行。

**`.ps1` 报 ParserError**：两个启动脚本必须保持**纯 ASCII**。Windows PowerShell 5.1
会按 GBK 解码没有 BOM 的 `.ps1`，中文和 emoji 会被拆成非法字节。
