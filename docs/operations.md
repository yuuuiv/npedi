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
python npedi.py aggregate
python npedi.py build-curves
python npedi.py quality-report
python npedi.py render
```

`--dry-run` 只打印命令和数据库位置；`--log-level` 调整日志。所有请求复用 `client.py`，401/403 停止运行。CLI 不包含任何码头专属路径。

## 聚类与趋势

基础抓取、幂等、checkpoint 和质量门槛通过后运行：

```powershell
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hdbscan
python npedi.py detect-changepoints
```

HDBSCAN/ruptures/Plotly Python 包缺失时，HDBSCAN 命令应先安装 `requirements.txt`；曲线 HTML 使用 Plotly CDN。
