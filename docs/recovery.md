# 恢复流程

1. 不删除 `crawl_checkpoint`、`raw_api_response` 或原始数据库。
2. 重新运行相同 crawler 并带 `--resume`；它从每个 `job_name + partition_key` 的 `next_page` 开始。
3. 如果 token 失效，先更新 `.env` 的 `WEB_TOKEN`，再重复命令；401/403 不会指数重试。
4. 如果出现重复页保护，查看 `ingest_error` 和 `raw_api_response` 的相邻页 hash；先保留证据再调整分区。
5. 事实表按业务键/来源哈希幂等；重复运行不会新增相同事实。
6. Gold 可从 Silver 重建：`python npedi.py aggregate`、`python npedi.py build-curves`。
7. 迁移前复制 SQLite 文件；Alembic downgrade 设计为恢复备份，不执行破坏性删除。

查看断点：

```sql
SELECT job_name, partition_key, next_page, observed_total, updated_at FROM crawl_checkpoint;
SELECT endpoint_name, stage, message, created_at FROM ingest_error ORDER BY id DESC;
```
