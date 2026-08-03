# 数据质量

每次运行通过 `python npedi.py quality-report` 输出 JSON 和 Markdown。

报告包括：

- 接口唯一记录数、最早/最晚事件时间；
- 空时间和非法数值计数；
- Bronze/Silver 事实行数；
- 分页总数、相邻重复页数、重复率；
- Schema Drift 的新增/缺失字段；
- UNKNOWN 动作数量；
- 质量门槛结果。

默认规则：非法数值为 NULL；时间顺序异常不删除原始记录；ETA/ETD/ATA/ATD 只有字段齐全且延迟非负时进入延迟聚合；完整度低于阈值的对象不进入聚类。货描规则使用 exact/regex 版本化映射，未匹配项为 `UNKNOWN` 并保留原文。
