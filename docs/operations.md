# 运行操作

## 安装和配置

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
# 在 .env 中填 WEB_TOKEN，不要写入日志或提交
```

## 空库 Demo

```powershell
python scripts/demo_timeseries.py
```

输出 `export/timeseries_demo/demo.sqlite`、`quality.json`、`quality.md`、`curves.html`。

## 真实运行

```powershell
python npedi.py crawl vessel-plan --mode incremental
python npedi.py crawl container-notice --mode snapshot
python npedi.py crawl vgm --mode backfill --resume
python npedi.py crawl cargo-release --mode backfill --resume
python npedi.py crawl transshipment --mode backfill --resume
python npedi.py crawl container-history --limit 500 --offset 0 --resume
python npedi.py aggregate
python npedi.py build-curves
python npedi.py quality-report
python npedi.py render
```

`--dry-run` 只打印命令和数据库位置；`--log-level` 调整日志。所有请求复用 `client.py`，401/403 停止运行。CLI 不包含任何码头专属路径。

## 运行可回测的 as-of 聚类

按观测时间重建指定历史时点的数据，再生成曲线和聚类：

```powershell
python npedi.py aggregate --as-of 2026-08-01T23:59:59+00:00
python npedi.py build-curves --as-of 2026-08-01T23:59:59+00:00
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical --as-of 2026-08-01T23:59:59+00:00
```

每个 as-of cutoff 写入独立的 Gold 表和曲线模型版本，聚类运行参数也会记录截止时刻。迁移 003 之前已被覆盖的事实版本不能自动复原；迁移后的每次观测都会追加到 `fact_record_version`。

VGM 和 container history 按 `container_enrichment_queue` 分批运行。每批使用 `--limit 500 --offset N --resume`，将 `N` 依次改为 `0`、`500`、`1000`，直到该队列没有更多待处理箱号。批次按 `first_seen_at, container_no` 固定排序；中断后可以重跑同一个 offset。

## 聚类与趋势

基础抓取、幂等、checkpoint 和质量门槛通过后运行：

```powershell
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hdbscan
python npedi.py detect-changepoints
```

HDBSCAN/ruptures/Plotly Python 包缺失时，HDBSCAN 命令应先安装 `requirements.txt`；曲线 HTML 使用 Plotly CDN。
