# npedi.com Onesite 工作台 API 接口文档

**版本**: 1.0
**编制时间**: 2026-08-03
**数据来源**: 直接用已认证的 HTTP 客户端（`client.py` 同款认证方式）实测抓取，全部请求/响应均为真实数据，非从代码推测。测试账号角色：基础查询 / 权限最全测试号（两个账号均测过码头业务，结果一致）。

> 本文档是 [WORKBENCH-RECON.md](WORKBENCH-RECON.md) 的详细版——那份是勘察阶段的路径清单，这份补全了每个接口的真实请求参数、真实响应字段结构。已有正式设计文档的模块（无纸化核放比对、进出门 CODECO）不在此重复，见 [ARCHITECTURE.md](ARCHITECTURE.md) / [ARCHITECTURE-GATE.md](ARCHITECTURE-GATE.md)。

## 0. 公共约定

**Base URL**: `https://www.npedi.com/onesite-api`

**认证头**:
```
ediAuthorization: Bearer <Web-Token>
```
`Web-Token` 取自浏览器登录后 Cookie 或任一请求头 `ediAuthorization` 的 `Bearer ` 后半段，取值方法见 [README.md](README.md)。

**统一响应信封**:
```json
{"code": 200, "msg": "操作成功", "data": ...}
```
- `code=200` 成功，`data` 是业务数据（可能是数组、分页对象、字符串或 null）。
- `code=400` 业务错误，`msg` 给出中文原因，`data` 为 `null`。常见原因分两类：
  1. **参数校验失败**（如"装卸船类型不能为空"）——缺字段，照着本文档补上就行。
  2. **权限/业务门槛**（如"请联系码头添加权限！"）——不是参数问题，见下面"码头业务权限墙"。
- `code=401/403`：token 失效，见 README「换 token」一节。

**分页响应结构**（大多数列表接口）：
```json
{"pageNum": 1, "pageSize": 20, "total": 313, "totalPages": 0, "list": [...]}
```
`totalPages` 恒为 `0`，不可信，翻页请以 `total` 为准（跟 npp/gate 管线的既有结论一致）。

**关于码头业务权限墙**：第 7 节列出的所有 `/matou/<终端代码>/*` 接口，不论用哪个账号（含权限最全的测试账号），一律返回：
```json
{"code":400,"msg":"请联系码头添加权限！","data":null}
```
或
```json
{"code":400,"msg":"暂无权限，请联系码头添加","data":null}
```
这是**码头自己的商务授权**，与 npedi 账号角色无关——要拿到真实数据，得先让公司去跟对应码头（北一/大榭/甬舟/北三……）建立业务关系。本文档只能列出接口路径和从前端代码里扒出的参数名，响应字段结构无法验证。

---

## 1. 集装箱信息

### 1.1 VGM 信息查询

**接口描述**：按箱号/船名航次查 VGM（船货重量核验）申报记录
**接口地址**：`/ctnvgm/getlist`
**请求方式**：GET

**请求参数**：

| 参数名称 | 参数说明 | 是否必须 | 数据类型 |
|---|---|---|---|
| pageNum | 页码 | 是 | integer |
| pageSize | 每页条数 | 是 | integer |
| containerNumber | 箱号 | 否（与 vessel 二选一有值） | string |
| vessel | 船舶 UN 编码 | 否 | string |
| ctnOperatorCode | 箱属船公司代码 | 否 | string |
| senderCode | 发送方代码 | 否 | string |
| direct | 进出口标志（E=出口, I=进口） | 否 | string |
| containerType | 箱类型 | 否 | string |

**响应参数**（`data.list[]`）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| vesselcode | 船舶 UN 编码 | string |
| vessel | 船名 | string |
| voyage | 航次 | string |
| containerNumber | 箱号 | string |
| ctnOperatorCode | 箱属船公司代码 | string |
| containerType | 进出口标志（E/I） | string |
| vgmGrossWeight | VGM 毛重(kg) | string(number) |
| vgmMethod | VGM 核验方式代码 | string |
| vgmSignature | 申报签署人 | string |
| operatetime | 申报时间(yyyyMMddHHmmss) | string |
| portreceiverdate | 码头接收时间 | string |
| resultCode | 处理结果代码（00=成功） | string |
| resultDescripts | 处理结果描述 | string |
| senderCode | 发送方代码 | string |
| receiverCode | 接收方（码头）代码 | string |

**示例响应**：
```json
{"code":200,"msg":"操作成功","data":{"pageNum":1,"pageSize":6,"total":6,"list":[
  {"vesselcode":"UN9963152","vessel":"ULSANVOYAGER","voyage":"2606W","containerNumber":"SNBU8234257",
   "ctnOperatorCode":"SNL","containerType":"E","vgmGrossWeight":"16150.4","vgmMethod":"M2",
   "vgmSignature":"vivian","operatetime":"20260728121047","portreceiverdate":"20260728121200",
   "resultCode":"00","resultDescripts":"OK  ","senderCode":"NBAGENT","receiverCode":"BLCT"}
]}}
```

---

### 1.2 物流跟踪

**接口描述**：按箱号查集装箱全生命周期状态汇总（聚合报关/放行/装卸船/进出门等多个子系统状态）
**接口地址**：`/track/getContainerTrackInfo`
**请求方式**：GET

**请求参数**：

| 参数名称 | 参数说明 | 是否必须 | 数据类型 |
|---|---|---|---|
| ctnNo | 箱号 | 是 | string |
| blno | 提单号 | 是（可传空字符串，但键必须存在，否则报"系统异常"） | string |

**响应参数**（`data.result[]`，每个箱子一条汇总记录）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| ctnNo | 箱号 | string |
| blno | 提单号 | string |
| vesselCode | 船舶 UN 编码 | string |
| vesselEname | 船名 | string |
| voyage | 航次 | string |
| direct | 进出口标志 | string |
| terminal | 码头代码 | string |
| status | 箱状态（F=重箱等） | string |
| ctnSizeType | 箱型 | string |
| sealNo | 铅封号 | string |
| grossWeight | 毛重 | string(number) |
| dlPortCode | 卸货港代码 | string |
| arrivalTime / etaArrivedTime | 实际/预计到港时间 | string |
| sailingTime / etaSailingTime | 实际/预计开航时间 | string |
| bills[] | 提单明细（`blno`/`weight`/`measure`/`packageNum`） | array[object] |
| ingate | 进门子状态：`{status, operateTime, truckNo, remark}` | object |
| outgate | 出门子状态：`{status, operateTime, truckNo, remark}` | object |
| load | 装船子状态：`{status, operateTime, remark}` | object |
| discharge | 卸船子状态：`{status, operateTime, remark}` | object |
| npp | 无纸化核放比对子状态：`{status, compareFlag, compareTime, operateTime, remark}` | object |
| cusretrec | 舱单回执子状态：`{resultCode, resultDescripts, receiveTime, applyType}` | object |
| custpass | 海关放行子状态：`{status, operateTime, remark}` | object |
| cusmov | 海关查验子状态：`{status, operateTime, isCheck986, remark}` | object |
| cusbaplie | 载货清单(BAPLIE)子状态：`{status, operateTime, remark}` | object |
| costco | 装箱单回执子状态：`{status, operateTime, remark}` | object |

**示例响应**：见 [probe_output/track_getContainerTrackInfo_final.json](probe_output/track_getContainerTrackInfo_final.json)（完整字段）。

---

### 1.3 装卸船查询 (COARRI)

**接口描述**：按船名航次查装船/卸船作业记录
**接口地址**：`/scoarri/list`
**请求方式**：GET

**请求参数**：

| 参数名称 | 参数说明 | 是否必须 | 数据类型 |
|---|---|---|---|
| pageNum | 页码 | 是 | integer |
| pageSize | 每页条数 | 是 | integer |
| unvessel | 船舶 UN 编码 | 是 | string |
| voyage | 航次 | 是 | string |
| type | 作业类型：`D`=卸船, `L`=装船 | **是**（缺失报"装卸船类型不能为空"） | string |

**响应参数**：标准分页结构，测试航次当前无匹配记录（`total:0`），字段结构未见实例，导出 Excel 入口为 `/scoarri/list/ediScoarriBl?scoarriId=`（未测）。

---

### 1.4 海关信息查询（含 5 个子查询）

**接口描述**：按箱号/船名航次查海关侧 5 类状态，UI 上是一个"操作类型"下拉切换的统一表单，实际是 5 个独立接口
**请求方式**：GET（全部）
**公共请求参数**（5 个接口共用同一套）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| pageNum / pageSize | 分页 | integer |
| voyage / vesselCode / vesselUn | 航次 / 船舶代码（二选一，可留空） | string |
| ctnOperatorCode | 箱属船公司 | string |
| ctnNo / ctnno | 箱号（两个大小写变体都传） | string |
| blNo / blno / billno | 提单号（三个变体都传） | string |
| passno | 放行编号 | string |
| containerMoveType | 固定传 `C` | string |
| ciqBjno | 报检号 | string |
| companyCode | 公司代码 | string |

#### 1.4.1 海关放行
**接口地址**：`/getEdiCustptr` — 实测 17 条记录（含真实放行流水号）。

#### 1.4.2 海关闸口放行（简化版，独立小接口）
**接口地址**：`/getCustomsgate`
**请求参数**：仅 `ctnNo`（必须）。
**响应**：`data` 直接是字符串标志位，如 `"N"`（不是对象/数组）。

#### 1.4.3 海关准装指令
**接口地址**：`/getOnesiteCustLcmd` — 实测 6 条记录。UI 未在本文档最初的勘察清单里出现，是补测时发现的第 5 个"操作类型"选项。

#### 1.4.4 海关查验
**接口地址**：`/getEdiCusmov` — 测试箱号当前无匹配记录（`total:0`），标准分页结构。

#### 1.4.5 检疫检验指令
**接口地址**：`/getEdiCiqinfo` — 测试箱号当前无匹配记录（`total:0`），标准分页结构。

---

### 1.5 在场箱查询

**接口描述**：查当前仍在堆场的箱子
**接口地址**：`/onYard/getlist`
**请求方式**：GET

**请求参数**：`pageNum`、`pageSize`、`unvessel`、`voyage`、`containerno`（均为字符串，可为空，但键要存在）。

**响应**：请求形状已验证正确；测试箱号此刻不在场，返回 `{"code":400,"msg":"失败,未查到数据"}` —— 这是业务态"查无结果"，不是参数错误，要用真正当前在场的箱子才能看到真实字段结构。

---

### 1.6 铅封查验

**接口描述**：按船名航次查铅封校验记录
**接口地址**：`/sealnochk/getlist`
**请求方式**：GET

**请求参数**：`pageNum`、`pageSize`、`unvessel`、`voyage`。

**响应参数**（`data.list[]`）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| vesselcode / vesselename / voyage | 船舶代码 / 船名 / 航次 | string |
| ctnno | 箱号 | string |
| cpcode | 码头代码 | string |
| esealno1/2/3 | 出场铅封号（1-3位） | string |
| rsealno1/2/3 | 登记铅封号（1-3位） | string |
| cussealno | 海关铅封号 | string |
| revtime | 记录时间 | string |

实测 2634 条记录，功能确认可用。

---

### 1.7 单箱历史查询

**接口描述**：按箱号查该箱在所有码头/所有航次上的历史动作流水（跨码头、跨年份）
**接口地址**：`/ediContainerlog/getEdiContainerlog/{箱号}`（箱号拼在路径里，不是查询参数）
**请求方式**：GET

**响应参数**（`data.list[]`）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| vesselename | 船名 | string |
| ctnno | 箱号 | string |
| cpcode | 码头/公司代码 | string |
| direct | 进出口标志 | string |
| voyage | 航次 | string |
| vesselcode | 船舶代码 | string |
| operateId | 动作代码（`IG`进门 `DV`交箱? `PS` `KM` 等，具体含义未逐一确认） | string |
| etaarrivetime | 到港时间 | string |
| xc/jm/cm/zc/fx/cy/cq | 标志位（含义未确认，疑似"卸船/进场/查验/在场/放行/查验/查验"等阶段勾选） | string("1"/null) |
| blNo | 提单号 | string |

跨码头历史价值很高——同一箱号能看到它在 BLCT/BLCTZS/BLCT2/NCICL/NDCC 等多个码头的完整流转记录，时间跨度可到 2022 年。

---

### 1.8 堆存查询 (COEDOR)

**接口描述**：查箱子在堆场的堆存周期
**接口地址**：`/scoedor/getEdiScoedor`
**请求方式**：GET

**请求参数**（UI 无船名航次模式，必须按下列条件之一）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| pageNum / pageSize | 分页 | integer |
| senderCode | 发送方 | string |
| ctnStatus | 箱状态 | string |
| ctnOwner | 箱属 | string |
| ctnNo | 箱号（**至少填一个条件，否则报"必须选择一个条件"**） | string |
| blNo | 提单号 | string |

**响应参数**（`data.list[]`）：

| 参数名称 | 参数说明 | 数据类型 |
|---|---|---|
| senderCode | 发送方（码头）代码 | string |
| ctnOperatorCode | 箱属船公司 | string |
| ctnNo | 箱号 | string |
| ctnSizeType | 箱型 | string |
| ctnStatus | 箱状态 | string |
| vessel / voyage | 船名 / 航次 | string |
| blNo | 提单号 | string |
| interyardDate | 入场日期 | string |
| direct | 进出口标志 | string |

---

### 1.9 报文传输查询

**接口描述**：查 EDI 报文收发日志，分"统计"和"明细"两个接口
**请求方式**：GET

#### 统计
**接口地址**：`/transferlogSearch/getMessageLogStatistic`
**请求参数**：`startTime`、`endTime`（yyyy-MM-dd）、`sender`、`receiver`、`messageType`、`pageNum`、`pageSize`。
**响应**：`{result:[], success:true, sum:{sender,receiver,inmessagenumber,outmessagenumber,inmessagesize,outmessagesize}}`

#### 明细
**接口地址**：`/transferlogSearch/getTransferLog`
**参数同上**。**权限限制**：`sender`/`receiver` 必须至少一个是自己公司代码，否则返回：
```json
{"code":400,"msg":"对不起，您只能查询发送方或接收方为自己公司的报文","data":null}
```

---

### 1.10 查验退费查询

**接口描述**：按箱号 + 时间段查查验退费记录
**接口地址**：`/onesite/refund/list4User`（注意路径里带 `onesite/` 前缀，跟大部分接口不一样）
**请求方式**：GET

**请求参数**：`pageNum`、`pageSize`、`placecode`（查验地点代码）、`ctnno`、`billno`、`finishtime`（格式 `起始8位+结束8位`，如 `2026061920260803`）。

**响应**：标准分页结构，测试箱号当前无匹配记录。

---

### 1.11 镇司/大榭信业堆存查询

**接口描述**：查镇海/大榭仓储系统的堆存记录
**接口地址**：`/ZHDXStock/getZHDXStockList`
**请求方式**：POST（JSON body，不是 query string）

**请求 Body**：

| 字段 | 说明 | 数据类型 |
|---|---|---|
| terminal | 码头代码 | string |
| vessel / voyage | 船名 / 航次 | string |
| direction | 方向 | string |
| ctnowner | 箱属代码（**必填**，UI 报"请填写箱主"） | string |
| ctnno / blno | 箱号 / 提单号 | string |
| intimeB / intimeE | 入场时间起止 | string |
| portcode / ctnstatus / ctnsize / ctntype / tradeNw / reeferflag / hazardflag / odflag / damageflag | 各类过滤条件 | string |
| orderFlag | 排序标志 | string |

**响应**：测试箱主代码不对应真实数据，返回 `{"code":400,"msg":"数据请求失败"}`——请求格式已确认正确，需要真实存在的箱主代码才能拿到示例数据。

---

## 2. 船舶信息

### 2.1 新船公告

**接口地址**：`/vessel/code/noticeList`　**方式**：GET　**参数**：无

**响应参数**（`data[]`，字段很多且大多为 `null`，只列有值的）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| vesselid | 船舶内部 ID | string |
| vesselcode | 船舶 UN 编码 | string |
| chndescription / engdescription | 船名中/英文 | string |
| localcode | 本地船代码 | string |
| regtime | 登记时间 | string |

（另有 ~50 个船舶主数据字段如 `vesselLoadWeight`/`vesselSpeed`/`vesselBayCapacity` 等，样本中全为 `null`，疑似高级船舶档案字段，本账号权限下不返回值。）

### 2.2 每日进箱公告

**接口地址**：`/vessel/dzyjh/getlist`　**方式**：GET

**请求参数**：`pageNum`、`pageSize`、`voyage`、`vesselename`、`vesselowner`、`vesselowner2`、`matou`、`ctnstart`（均可为空字符串）。

**响应参数**（`data.list[]`，实测 313 条）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| matou | 码头代码 | string |
| vesselcode / vesselename | 船舶代码 / 船名 | string |
| voyage | 航次 | string |
| ctnstart / ctnend | 进箱开始/截止时间 | string |
| ediports | 挂靠港口列表（`:` 分隔） | string |
| ioflag | 进出口标志 | string |
| revtime | 记录更新时间 | string |
| cnvesselname | 船名中文 | string |
| vesselowner | 船东/船公司代码 | string |

### 2.3 船舶维护 - 代理列表

**接口地址**：`/vessel/dailyMaintain/getAllAgentList`　**方式**：GET　**参数**：无
**响应**：`data[]`，每项主要有值字段是 `shipCompany`（船公司代码，如 CMA/ONE/KMTC），其余是维护表单用的空字段模板。

### 2.4 船舶计划（4 种视图，同一批档期数据的不同聚合）

#### 2.4.1 动态计划
**接口地址**：`/vessel/plan/getDynamicPlanVessel`（首页默认）/ `/vessel/plan/getDynamicPlanNew`（切视图后实际调用）
**参数**（New 版）：`applyType`、`vesselNamec`、`vesselNamee`、`voyage`、`time`、`dependActualTime`、`operatorcompanyName`

**响应参数**（`data[]`，首页默认视图字段）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| vesselNamec / vesselNamee | 船名中/英文 | string |
| vesselMmsi | MMSI | string |
| importVoyage / exportVoyage | 进/出口航次 | string |
| importAgentCompany | 进口代理公司 | string |
| upPortCode / toPortCode | 上一港 / 目的港 | string |
| operatorcompanyName | 作业公司 | string |
| berthName | 泊位名称 | string |
| dependPlantime / leavePlantime | 计划靠/离泊时间 | string |
| dependActualtime / leaveActualtime | 实际靠/离泊时间 | string |
| importCargoName / exportCargoName | 进/出口货名 | string |
| issueActualtime | 发布时间 | string |

#### 2.4.2 周计划（实际是"动态维护"接口）
**接口地址**：`/vessel/plan/selectContainerDynamicPlan`
**参数**：`vesselCnName`、`vesselEnName`、`voyage`、`etaBegin`、`etaEnd`（日期范围）、`terminal`、`page`、`pageSize`

**响应参数**（`data.list[]`，字段远比动态计划丰富，~50 个字段，只列关键的）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| vesselUnCode / vesselEnName / vesselCnName | 船舶 UN 编码 / 英中文船名 | string |
| terminal | 码头 | string |
| voyage / vesselDirect | 航次 / 方向(E/W) | string |
| tradeFlag | 贸易标志 | string |
| ctnStartTime / ctnEndTime | 进箱开始/截止 | string |
| customCloseTime / portCloseTime | 海关截关 / 港口截港时间 | string |
| eta / etd | 预计到/离港 | string |
| ata / atd | 实际到/离港 | string |
| etanchor / atanchor | 预计/实际抛锚 | string |
| ports | 挂靠港口列表 | string |
| lastPortCode / nextPortCode / lastPortName / nextPortName | 上一港/下一港代码及中文名 | string |
| berthReference | 泊位编号 | string |
| status | 状态标志 | string |
| published / publishTime | 是否已发布 / 发布时间 | string |

#### 2.4.3 月计划
**接口地址**：`/vessel/plan/getMoonthlyPlanNew`
**参数**：`vesselNamec`、`vesselNamee`、`voyage`、`operatorcompanyShortname`、`time`（yyyyMM）、`serviceRegionCode`、`page`、`pageSize`
**响应**：`{total, list:[]}`，测试月份/船无匹配，字段结构未见实例。

#### 2.4.4 日计划
**接口地址**：`/vessel/plan/selectContainerDynamicPlanNew`
**参数**：`vesselNamec`、`vesselNamee`、`ioVoyage`、`dependPlantime`、`leavePlantime`、`dependActualtime`、`leaveActualtime`、`operatorcompanyName`、`page`、`pageSize`、`dependPlantimeBegin`、`dependPlantimeEnd`
**响应**：标准分页结构，字段应与周计划（2.4.2）同源。

港口列表 (`getPortsInfo`/`getPortsInfoNew`) 与截港时间维护 (`getCloseTimeMaintain`) 未获取到独立响应样本，但其数据已内嵌在周计划响应的 `ports`/`customCloseTime`/`portCloseTime` 字段里。

---

## 3. 海关回执信息

### 3.1 新舱单回执

**接口地址**：`/cusretrec/getEdiCustomMsgtype`（消息类型枚举）　**方式**：GET　**参数**：无

**响应**（`data[]`，固定枚举）：

| code | description |
|---|---|
| MT5102 | 水运出口舱单 |
| MT3101 | 水运出口提单 |
| MT3102 | 水运舱单提单 |
| MT6102 | 水运舱单分批 |
| MT5101 | 水运舱单收据 |

（description 为原始中文，上表按字面翻译，未逐字核实业务含义。）

**查询接口**：`/cusretrec/getEdiCusretrec`　**方式**：GET
**参数**：`pageNum`、`pageSize`、`vesselname`、`ctnno`、`blnos`、`type`、`flag`

**响应参数**（`data.list[]`，实测 32 条）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| id | 记录 ID | string |
| vesselname / voyage / vesselcode | 船名 / 航次 / 船舶代码 | string |
| ctnno | 箱号 | string |
| resultDescripts | 比对结果描述（中文，如"运抵正常"/"理货正常"） | string |
| receivertime | 接收时间 | string |
| messageid | 报文 ID | string |

### 3.2 电子装箱单回执 (COSTCO)

**接口地址**：`/costcoHistory/getEdiCostco`　**方式**：GET
**参数**：`pageNum`、`pageSize`、`ctnno`、`blnos`、`costcono`

**响应参数**（`data.list[]`，实测 4 条）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| operator | 操作方代码 | string |
| operatetime | 操作时间 | string |
| operatefrom | 来源标志(E/D) | string |
| vessel / voyage | 船名 / 航次 | string |
| ctnno | 箱号 | string |
| costcono | 装箱单编号 | string |
| historyno | 历史序号 | string |
| status | 状态（"1"=正常） | string |

### 3.3 船勤报文回执

**接口地址**：`/cqBwHz/getTerminalCode/`　**方式**：GET　**参数**：无（码头代码字典表）
**数据接口**：`/cqBwHz/getEdiCustzyjhTable/`（未测出真实参数，字段模板显示是船舶作业申请单，含 `applycompany`/`cargoname`/`preworktime`/`workcompletetime` 等 40+ 字段，样本全为 null）。

### 3.4 散杂货海关放行查询

**接口地址**：`/ediCustptrSZ/getEdiCustptrSz`　**方式**：POST（但参数拼在 query string 里，body 为空——实现上是个 GET 语义的 POST）
**参数**：`val`、`passno`、`billno`、`vesselcode`、`voyage`、`vesselAndVoyage`、`pageNum`、`pageSize`

**响应参数**（`data.list[]`，实测 total 108373 条——这是全量历史散杂货放行数据，筛选条件对这个接口影响很弱，实测传了具体 vessel/billno 依然返回了不相关的历史记录）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| envessel | 船名 | string |
| voyage | 航次 | string |
| direct | 进出口标志 | string |
| vesselcode | 船舶代码 | string |
| billno | 提单号 | string |
| passtime | 放行时间 | string |
| cpcode | 码头代码 | string |
| flag | 标志位 | string |
| cargovolum / grossweight | 货物体积 / 毛重 | string |

**注意**：此接口数据量极大（10万+条），且筛选参数实测效果存疑，批量抓取前需要先确认 `val`/`vesselAndVoyage` 等参数的真实过滤语义，否则会拉到无关数据。

---

## 4. 集卡信息

### 4.1 集卡 GIS 查询
**接口地址**：`/truck/truckGis/getOnesiteTKTruck`　**方式**：GET　**参数**：`pageNum`、`pageSize`

**响应参数**（`data[]`）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| truckNo | 集卡编号 | string |
| truckLicense | 车牌号 | string |
| truckStatus | 状态（Y=启用） | string |
| rfidType | RFID 类型 | string |
| companyNamec | 所属公司（中文） | string |
| companyAddress | 公司地址 | string |

### 4.2 集卡停牌
**接口地址**：`/truck/punish/getOnesiteTkPunish`　**方式**：GET
**参数**：`pageNum`、`pageSize`、`truckno`、`truckLicense`

**响应参数**（`data[]`）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| truckNo / truckLicense | 集卡编号 / 车牌 | string |
| startDate / endDate | 停牌起止时间 | string |
| punishLocation | 处罚地点（码头代码） | string |
| punishReason | 处罚原因（中文，如"超速""闯禁行"） | string |

### 4.3 集卡转码头明细
**接口地址**：`/truck/tranTruckcode/getTruckInfo`　**方式**：GET　**参数**：`pageNum`、`pageSize`
**响应**：标准分页结构，测试条件下暂无数据（`total:0`）。

### 4.4 集卡作业历史
**接口地址**：`/truck/history/getTruckHistory`　**方式**：GET（推测，未确认参数结构——传 `pageNum/pageSize/truckno` 报"系统异常"，真实参数未定）。

---

## 5. 无纸化（补充，核心接口见 ARCHITECTURE.md）

### 5.1 本地出口放行
**接口地址**：`/npp/list`　**方式**：GET　**参数**：`pageNum`、`pageSize`
**响应**：标准分页结构，测试条件下为空。

### 5.2 内支中转查询
**接口地址**：`/npp/nzx/getNzwPageResult`　**方式**：GET　**参数**：`pageNum`、`pageSize`

**响应参数**（`data.list[]`，实测 57 条——内贸支线中转箱明细）：

| 参数名称 | 说明 | 类型 |
|---|---|---|
| firstVesselCode/Name/Voyage | 第一程船舶代码/船名/航次 | string |
| secondVesselCode/Name/Voyage | 第二程（中转后）船舶代码/船名/航次 | string |
| ctnNo | 箱号 | string |
| ctnOperatorCode | 箱属船公司 | string |
| billNo | 提单号 | string |
| quantity / weight / volume | 件数 / 重量 / 体积 | string(number) |
| firstDischargePortCode / firstLoadPortCode | 第一程卸货港/装货港 | string |
| secondTransPortCode | 第二程中转港 | string |
| cargoDescription | 货描 | string |
| sailingDate / cutOffDate | 开航日期 / 截关日期 | string |
| terminal | 码头代码 | string |
| checkFlag / checkMessage | 审核标志 / 审核消息 | string |
| sendFlag / sendMessage | 发送标志 / 发送消息 | string |

对应的 `getWznPageResult`（另一方向）、`getCtnDetail`（明细）未测，推测结构类似。

---

## 6. 数据订阅

### 6.1 物流跟踪订阅
**接口地址**：`/onesite/databinding/getTSubsceribeInfoNew`（注意路径带 `onesite/` 前缀）　**方式**：POST
**参数**：`telNo`（手机号）、`flag`（`A`=? 未确认具体枚举含义）
**响应**：`data: []`，本账号当前无订阅记录，字段结构未见实例。

### 6.2 进箱公告订阅
**接口地址**：`/onesite/subsceribeVes/list`（同样带 `onesite/` 前缀）　**方式**：POST
**参数**：`subTel`、`voyage`、`vesselName`、`vesselCode`、`subFlag`、`userRemark`
**响应**：`data: []`，同上，无订阅记录。

---

## 7. 码头业务（⚠ 权限受限，仅路径已知）

以下全部接口，本文档测试用的两个账号（含权限最全的账号）均返回权限拒绝，**没有任何一条能拿到真实响应**。列出仅供将来拿到码头授权后参照：

| 码头 | 功能 | 接口地址 | 方式 | 已知参数 |
|---|---|---|---|---|
| 北一(BLCT) | 出口箱信息 | `/matou/blct/getExcontainerlist` | GET | `vessel`, `id` |
| 北一(BLCT) | 进口箱信息 | `/matou/blct/getImcontainerlist` | GET | `vessel` |
| 北一(BLCT) | 航次信息 | `/matou/blct/getVoyageInfo` | GET | `direct`, `voyage`, `muserid` |
| 北一(BLCT) | 货代在场出口箱 | `/matou/blct/getContainerlist4Hd` | GET | 无（连这个也权限拒绝） |
| 北一(BLCT) | 在场空箱 | `/matou/blct/getEmptycontainer` | GET | `ctty`, `lncd` |
| 大榭(BLCTZS) | 进口箱信息 | `/matou/blctzs/jinKouXiangXinXiChaXun` | POST | 需先选船名航次 |
| 大榭(BLCTZS) | 在场箱信息 | `/matou/blctzs/zaiChangXiangXinXiChaXun` | POST | `page`, `pageSize` |
| 大榭(BLCTZS) | 出口箱在场装船 | `/matou/blctzs/vesselReferenceList` / `chuKouXiangZaiChangZhuangChuanXinXi` | POST | 未测 |
| 甬舟(YZCT) | 单船进出口 | `/yzsearch/ctnListQuery` | GET | `vessel`, `voyage`（实测"系统异常"，参数名可能不对，且大概率同样卡权限） |
| 北三(BLCT3) | 出口箱动态 | `/matou/blct3/selectVesnamevoyage` | POST | 未测（同类权限墙预期一致） |

乍浦(ZIT)、独山(DSNCT) 两个码头的接口本轮完全没测，按经验大概率是同样的权限墙，不建议在拿到授权前浪费请求。

---

## 附：接口路径速查表

| 分类 | 已验证可用 | 已知路径但权限/数据受限 |
|---|---|---|
| 集装箱信息 | VGM、物流跟踪、装卸船、海关信息×5、在场箱(需真实在场箱)、铅封查验、单箱历史、堆存查询、报文传输(仅本公司)、查验退费、镇海大榭堆存(需真实箱主) | — |
| 船舶信息 | 新船公告、每日进箱公告、代理列表、船舶计划×4视图 | 港口列表/截港维护(独立接口未验证，数据已内嵌在周计划里) |
| 海关回执 | 新舱单回执、电子装箱单回执、散杂货放行 | 船勤报文回执数据接口(参数未知) |
| 集卡信息 | GIS查询、停牌 | 转码头明细(暂无数据)、作业历史(参数未知) |
| 无纸化 | 内支中转 | 本地出口放行(暂无数据) |
| 数据订阅 | 订阅查询(结构已验证，暂无订阅数据) | — |
| 码头业务 | **无一可用** | 全部（商务权限墙） |
