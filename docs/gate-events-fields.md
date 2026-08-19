# 进出门（CODECO）数据字段详解

本文覆盖 `gate_events` 和 `gate_voyages` 两张表——进出门管线采集并落库的全部字段。
`docs/data_dictionary.md` 描述的是时序管线的 bronze/silver/gold 分层，不含本文内容。

> **统计口径**：本文所有分布与空值率取自同一次快照，窗口固定为
> `gate_events` 的 `rowid BETWEEN 77185735 AND 77785735`，共 **600,001 行**，
> 内容是 2022 年 ETA 区间的回填数据，采集于 2026-08-17。
> 全文百分比可以互相对上；换窗口重算会有一到两个百分点的漂移。

## 1. 数据来源

上游是 npedi 的进出门查询接口 `GET /scodeco/list`，返回 CODECO
（Container Gate-in/Gate-out Report）报文。每行是**一个集装箱的一次过闸事件**。

采集以 `(vesselCode, voyage)` 为单位，每个航次分两个方向各拉一遍：

```python
GET /scodeco/list
  type            = "GATE_IN REPORT"   # 或 "GATE_OUT REPORT"
  vesselCode      = "FC0007318"
  voyage          = "2204S"
  pageNum         = 1
  pageSize        = 100
  ctnOperatorCode = ""    # 留空：不按船公司过滤
  ctnNo           = ""    # 留空：不按箱号过滤
  blNo            = ""    # 留空：不按提单过滤
```

后三个过滤参数刻意留空，每个航次**全量取回**，不做服务端筛选。
响应的 `data.total` 是该航次该方向的总行数，`data.list` 是当前页的行。

## 2. gate_events 字段

### 2.1 报文标识

| 字段 | 示例 | 说明 |
|---|---|---|
| `id` | `20221007190325255189570` | 上游报文主键，23 位。前 14 位等于 `msgReceiveTime`，后 9 位为序号。表的主键，去重依据 |
| `type` | `GATE_IN` | 事件方向。`GATE_IN` 57.27%，`GATE_OUT` 42.73% |
| `msgReceiveTime` | `20221007190325` | 报文到达港区系统的时间，`yyyyMMddHHmmss`，14 位，**100% 非空** |

`id` 是唯一能保证全表非空且唯一的字段，任何去重或增量比对都应以它为准。

### 2.2 船舶与航次

| 字段 | 示例 | 说明 |
|---|---|---|
| `vesselcode` | `FC0007318` | 船舶代码。两种编码体系并存：`FC*`（港方自编）与 `UN*`（`UN` + 7 位 IMO 号） |
| `voyage` | `2204S` | 航次号。末位字母是航向：`S` 南行、`N` 北行、`W` 西行、`E` 东行 |
| `vessel` | `JIANGHAIZHIXIN` | 船名英文拼写（此例为"江海之信"） |
| `direct` | `E` | 进出口标识。`E` 56.66%，`I` 43.34% |

`(vesselcode, voyage)` 是关联 `gate_voyages` 和 `gate_history_candidate` 的业务键。

**`direct` 与 `type` 高度冗余**，实测交叉分布（占该 `type` 的比例）：

| type | direct | 占比 |
|---|---|---|
| `GATE_IN` | `E` | ~98% |
| `GATE_IN` | `I` | ~2% |
| `GATE_OUT` | `I` | ~99% |
| `GATE_OUT` | `E` | ~1% |

符合码头作业的物理含义：卡车**进闸**送来的是待装船的**出口**箱（E），
**出闸**拉走的是已卸船的**进口**箱（I）。约 1–2% 的反例通常是空箱调运等特殊流程。

> 建模时 `direct` 基本不提供 `type` 之外的额外信息，可作冗余校验位使用，
> 不宜作为独立特征。

### 2.3 集装箱本体

| 字段 | 示例 | 空值率 | 说明 |
|---|---|---|---|
| `ctnNo` | `SEGU4123506` | 0.00% | 箱号，ISO 6346。3 位箱主代码 + `U` + 6 位序号 + 1 位校验位 |
| `ctnSizeType` | `45GP` | 0.00% | 尺寸类型码，见下 |
| `ctnStatus` | `F` | 0.00% | 空重状态 |
| `containerType` | `L` | **32.45%** | 箱种，见下 |
| `ctnGrossWeight` | `28800` | 0.22% | 箱货总重，单位**公斤** |
| `sealNo` | `5464294` | 32.29% | 铅封号 |

#### ctnSizeType — 混合了 ISO 6346 与行业简写

标准 ISO 6346 尺寸类型码为 4 位：

- **第 1 位＝长度**：`2` = 20 英尺、`4` = 40 英尺、`L` = 45 英尺
- **第 2 位＝高度/宽度**：`0` = 8'0"、`2` = 8'6"、`3` = 9'0"、`5` = 9'6"（高柜）
- **第 3-4 位＝箱型**：`GP` 普通、`G0`/`G1` 带通风普通、`R0`/`R1` 冷藏、
  `T0`/`T1`/`TN` 罐式、`UT` 开顶、`PF`/`PL` 平台/框架

实测长度位分布：`4` 72.76%、`2` 26.13%、`L` 1.11%。

实测 Top 值：

| 值 | 占比 | 含义 |
|---|---|---|
| `45GP` | 56.15% | 40 英尺高柜普箱（主力箱型） |
| `22GP` | 20.58% | 20 英尺标准普箱 |
| `43GP` | 4.26% | 40 英尺 9'0" 普箱 |
| `45G1` | 4.13% | 40 英尺高柜带通风 |
| `45G0` | 3.48% | 40 英尺高柜带通风（另一变体） |
| `45R1` | 2.69% | 40 英尺高柜冷藏箱 |
| `22G0` / `22G1` | 2.11% / 2.07% | 20 英尺带通风 |
| `L5GP` | 1.00% | **45 英尺**高柜普箱 |
| `40HQ` | 0.75% | 非 ISO 简写，"40ft High Cube" |
| `22TN` | 0.33% | 20 英尺罐式 |

> ⚠️ **`40HQ`、`40HC`、`45HQ`、`40GP`、`20GP`、`40XX` 不是合法 ISO 码**，
> 是上游混入的行业简写，合计 **1.17%**。按第 1 位解析长度时，
> `40HQ` 的 `4` 恰好解析正确；但 `45HQ` 的 `4` 会被判为 40 英尺，
> 而简写本意是 45 英尺，**必然出错**。做尺寸统计时需单独维护简写映射表。

#### ctnStatus — 空重状态

| 值 | 占比 | 含义 |
|---|---|---|
| `F` | 62.71% | Full，重箱 |
| `E` | 36.01% | Empty，空箱 |
| `L` | 1.27% | 装载中/部分装载 |
| `` (空) | 0.00% | 仅 2 行 |

#### containerType — 箱种，近三分之一缺失

| 值 | 占比 | 主要搭配的 ctnStatus |
|---|---|---|
| `L` | 36.26% | 重箱 187,172 / 空箱 23,662 / 装载中 6,698 |
| `` (空) | **32.45%** | 空箱 98,794 / 重箱 95,613 |
| `Q` | 14.47% | **空箱为主** 69,771 / 重箱 16,969 |
| `H` | 7.31% | 重箱 25,049 / 空箱 18,532 |
| `I` | 4.77% | 几乎全为重箱 28,595 |
| `N` | 4.15% | 重箱 19,401 / 空箱 5,305 |
| `T` | 0.56% | 重箱 3,290（罐箱） |
| `B` / `D` | 0.03% / 0.00% | 极少 |

**缺失不是随机的，而是按码头系统性缺失**：

| senderCode | 窗口内行数 | containerType 缺失率 |
|---|---|---|
| `YZCT` | 20,764 | **85.7%** |
| `ZIT` | 7,682 | **79.7%** |
| `BLCTZS` | 80,890 | **66.0%** |
| `BLCT3` | 143,424 | **60.5%** |
| `BLCT` | 92,188 | 25.7% |
| `B2SCT` | 5,079 | 6.0% |
| `BLCT2` | 62,467 | 2.7% |
| `BLCTMS` | 155,654 | 2.0% |
| `ZHCT` / `CNDMY` / `CNDTU` / `NCICL` | — | **0.0%** |

> 这意味着按 `containerType` 做的任何统计都会**系统性偏向填写规范的码头**。
> 需要按 `senderCode` 分层，或改用 `ctnStatus` + `ctnSizeType` 组合推断箱种。

#### ctnGrossWeight

字符串存储，单位公斤。非空行的实测范围 `min=0`、`max=79600`，
其中显式为 `0` 的有 45 行，空串 0.22%。

> 上限 79.6 吨远超 40 英尺箱的常规限重（约 30.5 吨），属上游录入异常，
> 聚合前应设合理上界过滤。空箱重量通常为箱自重（20 尺约 2.2 吨、40 尺约 3.8 吨）。

### 2.4 提单、承运与港口

| 字段 | 示例 | 空值率 | 说明 |
|---|---|---|---|
| `blNo` | `WNBWY22AX0127` | 22.60% | 提单号。`WNB` = 宁波，第 4-5 位为年份 |
| `ctnOperatorCode` | `DXF` | 0.05% | 箱管方（船公司）三字码 |
| `senderCode` | `ZHCT` | 0.00% | **发送该报文的码头** |
| `dlPortCode` | `CNHPG` | 0.84% | 卸货港 UN/LOCODE |
| `signTrade` | `N` | 7.00% | 贸易性质 |

#### senderCode — 码头分布

`BLCT` 系列（北仑）合计 **89.09%**：

| 值 | 占比 | 说明 |
|---|---|---|
| `BLCTMS` | 25.94% | 北仑梅山 |
| `BLCT3` | 23.90% | 北仑三期 |
| `BLCT` | 15.36% | 北仑 |
| `BLCTZS` | 13.48% | 北仑中宅 |
| `BLCT2` | 10.41% | 北仑二期 |
| `YZCT` | 3.46% | 大榭 |
| `ZHCT` | 2.28% | 镇海 |
| `ZIT` | 1.28% | — |
| `CNDMY` | 1.22% | — |
| `B2SCT` / `CNDTU` / `NCICL` / `YGHAL` / `NDCC` | 0.11%–0.85% | 其余 |

`DXCTE`、`TZLMG` 在窗口内各仅 1–2 行，属长尾。

#### ctnOperatorCode — 船公司

无单一主导，Top 10 合计 **62.73%**：

| 值 | 占比 | 船公司 |
|---|---|---|
| `MSC` | 11.16% | 地中海航运 |
| `EMC` | 10.27% | 长荣 |
| `COS` | 8.51% | 中远 |
| `CMA` | 7.27% | 达飞 |
| `MSK` | 7.03% | 马士基 |
| `ONE` | 6.11% | Ocean Network Express |
| `HLC` | 4.02% | 赫伯罗特 |
| `OOL` | 2.93% | 东方海外 |
| `YML` | 2.84% | 阳明 |
| `ZIM` | 2.59% | 以星 |

长尾超过 40 个代码。

#### dlPortCode — 卸货港

标准 UN/LOCODE（2 位国家码 + 3 位港口码）。Top 10：

| 值 | 占比 | 港口 |
|---|---|---|
| `NLRTM` | 3.42% | 鹿特丹 |
| `USLGB` | 2.96% | 长滩 |
| `SGSIN` | 2.79% | 新加坡 |
| `CNZPU` | 2.32% | 乍浦 |
| `USLAX` | 1.97% | 洛杉矶 |
| `CNTAO` | 1.63% | 青岛 |
| `AEJEA` | 1.47% | 杰贝阿里 |
| `DEHAM` | 1.41% | 汉堡 |
| `KRPUS` | 1.32% | 釜山 |
| `SAKAC` | 1.22% | 卡伊夫 |

国内港（`CN*`）占比可观，反映内贸与中转业务。

#### signTrade — 贸易性质

| 值 | 占比 | 含义 |
|---|---|---|
| `W` | 76.67% | 外贸 |
| `N` | 16.33% | 内贸 |
| `` (空) | 7.00% | 未标注 |

`direct` 与 `signTrade` 无强相关，进出口两个方向都以外贸为主。

### 2.5 时间戳 —— 最容易踩坑的部分

| 字段 | 格式 | 长度 | 整体空值率 |
|---|---|---|---|
| `msgReceiveTime` | `yyyyMMddHHmmss` | 14 | 0.00% |
| `inGateTime` | `yyyyMMddHHmm` | 12 | 29.75% |
| `outGateTime` | `yyyyMMddHHmm` | 12 | 57.23% |

**两个过闸时间只精确到分钟（12 位），而 `msgReceiveTime` 精确到秒（14 位）。**
统一解析时必须区分格式，不能套用同一个 pattern。

**每种报文只保证携带自己那一侧的时间**：

| type | `inGateTime` 缺失 | `outGateTime` 缺失 |
|---|---|---|
| `GATE_IN` | **0.00%** | **99.94%** |
| `GATE_OUT` | **69.61%** | **0.00%** |

也就是说：

- `GATE_IN` 记录**必有**进闸时间，几乎必然**没有**出闸时间。
- `GATE_OUT` 记录**必有**出闸时间，但只有约 **30%** 同时带进闸时间。

> ⚠️ **计算堆存周期**（进闸到出闸）只能用那约 30% 两个时间都齐全的 `GATE_OUT`
> 记录，例如 `inGateTime=202210230633` / `outGateTime=202212160444`，堆存约 54 天。
> 想覆盖全量，必须按 `ctnNo` 自行关联 `GATE_IN` 与 `GATE_OUT` 两条记录，
> 且同一箱号会跨航次复用，关联键须带时间窗口约束。

### 2.6 采集元数据（本地追加，非上游字段）

| 字段 | 示例 | 说明 |
|---|---|---|
| `run_id` | `507` | 采集批次号，关联 `crawl_run`，可追溯每行来自哪次运行 |
| `fetched_at` | `2026-08-17 14:26:00` | 本地入库时间 |
| `raw_json` | `''` | **已刻意置空，100% 为空串** |

`raw_json` 原本保存整条原始报文。由于上面 20 个字段已完整覆盖响应内容，
保留副本会让库膨胀数倍，因此清空。当前每行仅约 **160 字节**（含索引约 326 字节），
这是 7,700 万行的 `gate_events` 只占约 11 GB 表数据的原因。

## 3. gate_voyages 字段

航次级汇总，每个 `(vesselcode, voyage)` 一行。真实示例：

| 字段 | 示例 | 说明 |
|---|---|---|
| `vesselcode` | `FC0007318` | 业务键 |
| `voyage` | `2204S` | 业务键 |
| `vesselename` | `JIANGHAIZHIXIN` | 船名 |
| `gatein_total` | `169` | **上游声明的**进闸报文总数 |
| `gateout_total` | `0` | 上游声明的出闸报文总数 |
| `gatein_done_at` | `2026-08-17 14:26:01` | 进闸方向抓取完成时间 |
| `gateout_done_at` | `2026-08-17 14:26:01` | 出闸方向抓取完成时间 |
| `last_event_at` | `20221008223332` | 该航次最后一条报文的时间 |
| `first_seen_at` | `2026-08-17 14:26:00` | 本地首次见到该航次 |
| `last_seen_at` | `2026-08-17 14:26:00` | 最近一次见到 |
| `idle_rounds` | `0` | 连续多少轮无新增（增量巡检用） |
| `inactive` | `0` | 是否已判定为不再活跃 |

`gatein_total` / `gateout_total` 的核心用途是**完整性校验**：抓取结束后核对
实际入库行数是否等于上游声明的 total，不一致即判定分页存在缺口并拒绝标记完成。
`gate_anomaly_sidecar` 的 total 护栏是同源逻辑，详见
[GATE-ANOMALY-SIDECAR.md](GATE-ANOMALY-SIDECAR.md)。

`idle_rounds` 和 `inactive` 服务于日常增量巡检，历史回填路径不使用。

## 4. 使用须知

1. **所有字段都是 TEXT**，包括 `ctnGrossWeight` 和全部时间戳。聚合前需显式
   `CAST`，且时间字段有 12 位和 14 位两种格式。
2. **空值是空字符串 `''`，不是 `NULL`**。`WHERE outGateTime IS NULL` 查不到任何行，
   必须写 `WHERE outGateTime = ''`。
3. **缺失具有系统性**：`containerType` 按码头缺失（0% 到 85.7%），
   `sealNo` 缺失 32.29%，`blNo` 缺失 22.60%。分组统计前先检查该维度的缺失分布。
4. **`ctnSizeType` 混入非 ISO 简写**（1.17%），其中 `45HQ` 类会导致长度误判。
5. **`direct` 与 `type` 冗余约 98%**，不要当独立特征。
6. **`id` 是唯一可靠的去重键**；`ctnNo` 会跨航次复用，不能单独作为标识。

## 5. 常用查询示例

```sql
-- 某航次的进出闸箱量
SELECT type, COUNT(*) FROM gate_events
WHERE vesselcode = 'FC0007318' AND voyage = '2204S' GROUP BY type;

-- 可直接计算堆存天数的记录（仅约 30% 的 GATE_OUT 行满足）
SELECT ctnNo,
       julianday(
         substr(outGateTime,1,4)||'-'||substr(outGateTime,5,2)||'-'||substr(outGateTime,7,2))
     - julianday(
         substr(inGateTime,1,4)||'-'||substr(inGateTime,5,2)||'-'||substr(inGateTime,7,2))
       AS dwell_days
FROM gate_events
WHERE type = 'GATE_OUT' AND inGateTime <> '' AND outGateTime <> '';

-- 按码头统计，并暴露 containerType 的缺失率
SELECT senderCode, COUNT(*) AS rows,
       ROUND(100.0 * SUM(containerType = '') / COUNT(*), 1) AS missing_pct
FROM gate_events GROUP BY senderCode ORDER BY rows DESC;

-- 箱型长度分布（先排除非 ISO 简写再解析首位）
SELECT substr(ctnSizeType, 1, 1) AS iso_length, COUNT(*)
FROM gate_events
WHERE ctnSizeType NOT IN ('40HQ','40HC','45HQ','40GP','20GP','40XX')
GROUP BY 1 ORDER BY 2 DESC;

-- 重箱平均箱重（过滤录入异常）
SELECT ctnSizeType, COUNT(*) AS n,
       ROUND(AVG(CAST(ctnGrossWeight AS INTEGER)) / 1000.0, 2) AS avg_tonnes
FROM gate_events
WHERE ctnStatus = 'F' AND ctnGrossWeight <> ''
  AND CAST(ctnGrossWeight AS INTEGER) BETWEEN 1000 AND 40000
GROUP BY 1 HAVING n > 1000 ORDER BY n DESC;
```
