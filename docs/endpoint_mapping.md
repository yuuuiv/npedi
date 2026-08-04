# Endpoint mapping

事实依据：`NPEDI-API-REFERENCE.md`。实现只使用以下路径：

| endpoint | method | 已实现参数 | 状态 |
|---|---|---|---|
| `/vessel/plan/selectContainerDynamicPlan` | GET | `vesselCnName,vesselEnName,voyage,etaBegin,etaEnd,terminal,page,pageSize` | 真实单页 `code=200`，Fixture 全分页 |
| `/vessel/dzyjh/getlist` | GET | `pageNum,pageSize,voyage,vesselename,vesselowner,vesselowner2,matou,ctnstart` | 真实单页 `code=200`，Fixture 全分页 |
| `/ctnvgm/getlist` | GET | `pageNum,pageSize,containerNumber`；真实采集按箱号分区 | 箱号真实单页 `code=200`；船舶参数真实探测 `HTTP 400`；Fixture |
| `/ediCustptrSZ/getEdiCustptrSz` | POST/query | 文档确认的 `val/passno/billno/vesselcode/voyage/vesselAndVoyage/pageNum/pageSize` | Fixture；真实未验证 |
| `/npp/nzx/getNzwPageResult` | GET | `pageNum,pageSize` | Fixture；真实未验证 |
| `/ediContainerlog/getEdiContainerlog/{container_no}` | GET | 路径箱号 | Fixture；真实未验证 |

2026-08-03 的两次真实单页验证只输出状态、行数和字段名，不保存敏感值。2026-08-04 的 VGM 探测显示箱号参数返回 `200`，船舶参数返回 `400`，因此实现不再把计划表船舶代码直接传给 VGM。计划和进箱公告响应均含文档字段，另有额外字段；额外字段只保留在原始 JSON 并出现在 schema observation。

明确不调用价格接口、未知参数接口或任何 `/matou/*`。
