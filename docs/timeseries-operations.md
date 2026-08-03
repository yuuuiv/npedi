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
