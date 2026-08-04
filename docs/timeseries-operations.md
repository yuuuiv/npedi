# Timeseries Phase 1

Phase 1 只访问 API 参考中已验证的非码头接口：

- `/vessel/plan/selectContainerDynamicPlan`
- `/vessel/dzyjh/getlist`

不接入价格数据，也不访问任何 `/matou/*` 路径。Token 继续由现有 `.env` 或环境变量 `WEB_TOKEN` 读取。

## Fixture 验证

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## 真实接口验证

```powershell
python -c "from config import load_config; from timeseries import run_phase1; print(run_phase1(load_config()))"
```

这条命令需要现有有效 Token；无 Token 时仅能完成 Fixture 验证。每页原始响应写入 `raw_api_response`，下一页写入 `crawl_checkpoint`，Bronze 记录按业务键和内容哈希幂等更新，Silver 保留计划快照。`totalPages` 不参与终止判断。

## 迁移

`TimeseriesStore` 启动时按文件名应用 `migrations/*.sql`，由 `schema_migration` 记录已应用迁移；现有 `voyages`、`containers` 和 `gate_events` 表不重建。

## VGM 采集

VGM 实际按箱号调用 `/ctnvgm/getlist`。运行时优先读取 `container_enrichment_queue`，为空时回退到旧库 `containers.containerno`：

```powershell
python npedi.py crawl vgm --limit 500 --offset 0 --resume
python npedi.py crawl vgm --limit 500 --offset 500 --resume
python npedi.py crawl container-history --limit 500 --offset 0 --resume
python npedi.py crawl container-history --limit 500 --offset 500 --resume
```

`--limit/--offset` 是箱号批次；每个箱号使用独立 checkpoint，单个 HTTP 400 会记录到 `ingest_error` 并使本轮标记为 `partial`，后续箱号继续处理。船舶参数探测返回 400，因此不再使用计划表 `vessel_code` 作为 VGM 查询参数。

container history 使用相同的箱号队列和固定排序。队列超过一个批次时，继续增加 offset；中断后使用相同 offset 和 `--resume` 重试。

## As-of 回测聚类

先按观测时间重建历史 Gold，再生成对应曲线和聚类：

```powershell
python npedi.py aggregate --as-of 2026-08-01T23:59:59+00:00
python npedi.py build-curves --as-of 2026-08-01T23:59:59+00:00
python npedi.py cluster --entity terminal --curve-type vgm --algorithm hierarchical --as-of 2026-08-01T23:59:59+00:00
```

`--as-of` 使用采集观测时间，避免把后来才采集到的事实提前泄露到历史窗口。迁移 003 后，VGM、cargo release、transshipment 和 container history 的每次变化都会追加到 `fact_record_version`。
