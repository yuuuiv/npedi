# 集运 MaaS（ghzh.tmaas.com.cn）接口文档

**版本**: 1.0
**编制时间**: 2026-08-05
**站点**: 集运 MaaS 一门式查询 / 港航纵横 —— 上海海勃数科技术有限公司
**数据来源**: 前端 bundle 静态解析（全量接口清单）+ 登录态浏览器内实调（真实请求/响应）。
标注「实调」的响应是真跑出来的原样数据；标注「未验证」的只从代码里扒出了路径与参数名。

> 采集工具在 [maas/](maas/) 下，重跑方式见本文末「怎么重跑」。

## 0. 公共约定

**站点地址**: `https://ghzh.tmaas.com.cn`

**接口 Base URL**: `https://ghzh.tmaas.com.cn/ghzh`

前端 axios 实例是 `axios.create({baseURL:"https://ghzh.tmaas.com.cn/ghzh"})`，
**不是站点根目录**——直接打 `https://ghzh.tmaas.com.cn/api/...` 只会拿到 SPA 的 `index.html`（HTTP 200 但是 HTML）。

另有一条备用 base：带 `?token=xxx` 从海关侧嵌入时，前端切到 `baseURL:"/ghzhcustoms"`，
并改用 `Authorization: Bearer <token>` 头而不是 cookie。相同的业务路径两边都有。

**认证**: OAuth2 授权码 + PKCE 单点登录。

```
GET /OutTruck
 → 302 /oauth2/authorization/ghzh
 → 302 passport.tmaas.com.cn/oauth2/authorize?response_type=code&client_id=ghzhmaas&scope=ght
        &code_challenge=<S256>&redirect_uri=https://ghzh.tmaas.com.cn/login/oauth2/code/ghzh
 → 登录页（顶象滑块验证码）
 → 302 /login/oauth2/code/ghzh?code=xxx  → 种 session cookie → 回站点
```

登录后靠 cookie 维持会话，`withCredentials: true`。没有可直接申请的 API token。

**反爬**: 全站挂**瑞数（Botgate）动态防护**。

- 首次请求任何页面返回 `412 Precondition Failed` + 一段混淆 JS（`$_ts=window['$_ts']`），
  JS 算出动态 cookie（本次是 `2aCTSJaVda98O` / `2aCTSJaVda98P`，名字会变）后自动重载才给 200。
- cookie 名和算法每次部署都可能变，**httpx/requests 直连一律 412**，必须真浏览器。
- **有会话级封禁**：连续快打约 20 次后，整个会话被封——所有接口返回
  `400` + `Content-Type: text/html` + 只有 `\r\n\r\n` 的空体（业务 400 一定带 `msg`），
  连 SPA 自己的 js chunk 都一起 400，页面直接白屏。
  实测**重载页面、清掉动态 cookie 重跑挑战、换代理 IP 都救不回来**，只能等冷却（分钟级）。
  采集侧因此按 4 秒/次 + 5/10/15 分钟三级退避，连续三轮还封就存盘收工，下次接着补。

**免登录入口**: 路由表里 `meta.requireAuth` 为 `false` 的有 `/`、`/DngPL`（危申报）、`/nodes`（节点追踪）。
这几个页面不走 OAuth，对采集侧意味着**不用过验证码也不用维持 session**——但仍然要过瑞数。
其余页面虽然 `requireAuth: true`，`meta` 里还带 `publicQuery: true`，含义待确认。

**统一响应信封**:

```json
{"code": 200, "msg": "操作成功", "count": null, "data": ...}
```

- `code=200` 成功，`data` 是业务数据（数组或对象，无数据时是 `[]`）。注意 HTTP 状态也是 200 时 `code` 可能是 400。
- `code=400` 业务错误，`msg` 是中文原因（如「干支标记不能为空」「身份证号、车牌号必须输入一项才能查询」）。
- HTTP `403` 空体：账号无该模块权限。本次测试账号 `ela813267e45` 在 `*R`（复核/受限）系列接口上一律 403。
- HTTP `550` + `{"Code":500,"Msg":"服务器发生未处理的异常"}`：参数类型不对导致后端炸了，不是权限问题。
- HTTP `404`：路径参数为空时路由匹配不上。
- 下拉参照类接口统一返回 `[{"value":"WGQ4","label":"沪东"}]`。

**码头代码**（`/api/edi/RefInfo/TERMINAL` 实调）:

| 代码 | 名称 | 代码 | 名称 |
|---|---|---|---|
| WGQ1 | 浦东 | YS1 | 盛东 |
| WGQ2 | 振东 | YS3 | 冠东 |
| WGQ4 | 沪东 | YS4 | 尚东 |
| WGQ5 | 明东 | YD | 宜东 |
| LDMT | 罗东 | LDMTC8 | 罗东测试 |

外集卡接口的 `ter_name` 用的是「盛东(洋1)」「沪东(外4)」这种带港区后缀的写法，与上表的 label 不完全一致。

---


## 1. 箱货查询

**页面路由**: `/ContainerQuery`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `cntr_no` | 箱号 |
| `voy_imp_name` | 进口船名航次 | `voy_exp_name` | 出口船名航次 |
| `cntr_type` | 箱型 | `cntr_cop` | 持箱人 |
| `cnt_status` | 状态 | `inyard_date_time` | 进场时间 |
| `outyard_date_time` | 出场时间 | `costco_fg` | 运抵 |
| `cp_fg` | 海放 | `tp_fg` | 码放 |
| `msapass_fg` | 海事 | `stowage_fg` | 配载 |
| `tabs` | 操作 | `mass` | MaaS节点 |


### `GET /api/Container/{n}/{a}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `n` | 箱号 |
| `a` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/Container/"+n+"/"+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/Container/BEAU2324412/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `POST /api/CollectContainerGoods/{n}/maas`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `n`（含义未验证）

前端调用：
```js
$api.post("/api/CollectContainerGoods/"+n+"/maas")
```


### `GET /api/MassNodes/GetToken`

**状态**: 实调 —— 通了，但该条件下无数据

前端调用：
```js
$api.get("/api/MassNodes/GetToken")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/GetToken
```

```json
{
  "code": 200,
  "msg": "6c837c8d-5",
  "count": null,
  "data": null
}
```


## 2. 船期查询

**页面路由**: `/ScheduleQuery`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 预靠码头 | `vessel_chn_name` | 中文船名 |
| `vessel_eng_name` | 英文船名 | `voy_exp_name` | 出口航次 |
| `rcv_begin_time` | 码头进箱开始时间 | `rcv_end_time` | 码头进箱截止时间 |
| `agent_name` | 代理 | `tabs` | 操作 |
| `berth_status` | 状态 | `voy_imp_name` | 进口航次 |
| `berth_ptime` | 计划靠泊时间 | `berth_atime` | 实际靠泊时间 |
| `departure_ptime` | 计划离泊时间 | `departure_atime` | 实际离泊时间 |
| `agent_imp_name` | 代理 | `agent_exp_name` | 船代理 |
| `schedule_status` | 预报/确报 | `eta_time` | 计划抵锚地时间 |
| `ata_time` | 实际抵锚地时间 | `scd_culoc` | 抵港位置 |
| `vsl_vtpcode` | 船舶类型 | `scd_fpot` | 上一港 |


### `GET /api/ScheduleRCV?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 查询参数 `Vname` |
| `i` | 查询参数 `Voyage` |
| `n` | 查询参数 `Terid` |
| `l` | 查询参数 `Gzfg` |

前端调用：
```js
$api.get("/api/ScheduleRCV?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleRCV?Vname=&Voyage=&Terid=&Gzfg=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/ScheduleBerth?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 参数校验 —— 实调返回：干支标记不能为空

**参数**: `a`, `i`, `n`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/ScheduleBerth?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleBerth?Vname=&Voyage=&Terid=&Gzfg=
```

```json
{
  "code": 400,
  "msg": "干支标记不能为空",
  "count": null,
  "data": null
}
```


### `GET /api/AgentSchedule?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 参数校验 —— 实调返回：参数不能为空

**参数**: `a`, `i`, `n`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/AgentSchedule?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/AgentSchedule?Vname=&Voyage=&Terid=&Gzfg=
```

```json
{
  "code": 400,
  "msg": "参数不能为空",
  "count": null,
  "data": null
}
```


### `POST /api/CollectionSchedule`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/CollectionSchedule")
```


## 3. 外集卡动态信息

**页面路由**: `/OutTruck`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `index` | 序号 | `ter_name` | 码头 |
| `truck_no` | 车号 | `op_mode` | 作业方式 |
| `inyard_time` | 进场时间 | `outyard_time` | 出场时间 |
| `cntr_no1` | 拖运箱一 | `cntr_no2` | 拖运箱二 |
| `cntr_no3` | 拖运箱三 | `cntr_no4` | 拖运箱四 |


### `GET /api/OutTruckActivity/{i}/{a}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `i` | 车牌号（外集卡车号，会被前端转大写） |
| `a` | 日期，格式 `YYYY-MM-DD` |

前端调用：
```js
$api.get("/api/OutTruckActivity/"+i+"/"+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/OutTruckActivity/沪FB1502/2026-08-04
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "ter_name": "沪东(外4)",
      "truck_no": "沪FB1502",
      "op_mode": "提箱",
      "inyard_time": "2026-08-04 18:18",
      "outyard_time": "2026-08-04 19:20",
      "cntr_pkid1": "510030542764211",
      "cntr_no1": "BEAU2324412",
      "cntr_pkid2": null,
      "cntr_no2": null,
      "cntr_pkid3": null,
      "cntr_no3": null,
      "cntr_pkid4": null,
      "cntr_no4": null
    },
    {
      "ter_name": "振东(外2)",
      "truck_no": "沪FB1502",
      "op_mode": "进箱",
      "inyard_time": "2026-08-04 16:08",
      "outyard_time": "2026-08-04 16:59",
      "cntr_pkid1": "510030477457898",
      "cntr_no1": "MSMU9074356",
      "cntr_pkid2": null,
      "cntr_no2": null,
      "cntr_pkid3": null,
      "cntr_no3": null,
      "cntr_pkid4": null,
      "cntr_no4": null
    },
    "…共 4 条"
  ]
}
```


## 4. VGM 查询

**页面路由**: `/VgmQuery`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `cntr_no` | 箱号 | `ter_name` | 码头 |
| `cntr_type` | 箱型 | `vsl_exp_ename` | 英文船名 |
| `vsl_exp_cname` | 中文船名 | `voy_exp_name` | 航次 |
| `cntr_status` | 箱状态 | `vgm_kg` | VGM重量 |
| `vgm_receive_time` | 收到报文时间 |  |  |


### `GET /api/ContainertERvgm/{a}/{i}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 箱号 |
| `i` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/ContainertERvgm/"+a+"/"+i)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainertERvgm/BEAU2324412/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/ContainertVCMvgm/{a}/{i}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 箱号 |
| `i` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/ContainertVCMvgm/"+a+"/"+i)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainertVCMvgm/BEAU2324412/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


## 5. 我的收藏

**页面路由**: `/MyWatch`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `vessel_chn_name` | 中文船名 |
| `vessel_eng_name` | 英文船名 | `voy_imp_name` | 进口航次 |
| `voy_exp_name` | 出口航次 | `rcv_begin_time` | 开港时间 |
| `rcv_end_time` | 截港时间 | `berth_ptime` | 计划靠泊 |
| `berth_atime` | 实际靠泊 | `departure_ptime` | 计划离泊 |
| `departure_atime` | 实际离泊 | `tabs` | 操作 |
| `cntr_no` | 箱号 | `inyard_date_time` | 进场时间 |
| `outyard_date_time` | 出场时间 | `sur_date` | 收藏时间 |


### `GET /api/CollectionSchedule`

**状态**: 实调 —— 通了，但该条件下无数据

前端调用：
```js
$api.get("/api/CollectionSchedule")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/CollectionSchedule
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/CollectContainerGoods`

**状态**: 实调 —— 通了，但该条件下无数据

前端调用：
```js
$api.get("/api/CollectContainerGoods")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/CollectContainerGoods
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `DELETE /api/CollectionSchedule/{t.row.key_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.key_id`（含义未验证）

前端调用：
```js
$api.delete("/api/CollectionSchedule/"+t.row.key_id)
```


### `DELETE /api/CollectContainerGoods/{t.row.key_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.key_id`（含义未验证）

前端调用：
```js
$api.delete("/api/CollectContainerGoods/"+t.row.key_id)
```


## 6. 装箱单查询

**页面路由**: `/PackingListQuery`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `termcd` | 码头代码 | `termnm` | 码头名称 |
| `vslcode` | 船名代码 | `vslenname` | 英文船名 |
| `vslcnname` | 中文船名 | `voyage` | 航次 |
| `ieflag` | 进出口标志 | `ctnno` | 预录箱号 |
| `inserttime` | 制单时间 | `status` | 状态 |
| `tabs` | 操作 |  |  |


### `POST /api/edi/TerRefInfo`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/TerRefInfo")
```


### `POST /api/edi/VoyInfo`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/VoyInfo")
```


### `POST /api/edi/VoyRefInfo`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/VoyRefInfo")
```


### `GET /api/edi/RefInfo/TERMINAL`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/edi/RefInfo/TERMINAL")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/edi/RefInfo/TERMINAL
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "WGQ1",
      "label": "浦东"
    },
    {
      "value": "WGQ2",
      "label": "振东"
    },
    "…共 10 条"
  ]
}
```


### `GET /api/edi/RefInfo/CNTR_STATUS`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/edi/RefInfo/CNTR_STATUS")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/edi/RefInfo/CNTR_STATUS
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "L",
      "label": "拼箱"
    },
    {
      "value": "F",
      "label": "整箱"
    }
  ]
}
```


### `GET /api/edi/RefInfo/CNTR_SIZE`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/edi/RefInfo/CNTR_SIZE")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/edi/RefInfo/CNTR_SIZE
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "12G1",
      "label": "10GP"
    },
    {
      "value": "12P1",
      "label": "12P1"
    },
    "…共 48 条"
  ]
}
```


### `GET /api/edi/RefInfo/CNTR_TRANSMODE`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/edi/RefInfo/CNTR_TRANSMODE")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/edi/RefInfo/CNTR_TRANSMODE
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "3",
      "label": "内贸"
    }
  ]
}
```


### `GET /api/edi/RefInfo/CNTR_TEMPERATURE`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/edi/RefInfo/CNTR_TEMPERATURE")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/edi/RefInfo/CNTR_TEMPERATURE
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "F",
      "label": "华氏"
    },
    {
      "value": "C",
      "label": "摄氏"
    }
  ]
}
```


### `POST /api/edi/GetEvidence`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/GetEvidence")
```


### `POST /api/edi/COSTCOAdd`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/COSTCOAdd")
```


### `POST /api/edi/COSTCOInfos`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/COSTCOInfos")
```


### `POST /api/edi/COSTCOInfoById`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/edi/COSTCOInfoById")
```


## 7. 放行信息

**页面路由**: `/ClearancePage`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `bill_no` | 提单号 | `ter_name` | 码头 |
| `vsl_exp_cname` | 中文船名 | `vsl_exp_ename` | 英文船名 |
| `exp_voyage` | 航次 | `receive_time` | 接收时间 |
| `goods_packages` | 件数 | `goods_weight` | 重量 |
| `goods_volumn` | 体积 |  |  |


### `GET /api/GoodsPassInfo?Billno={t}&Terid={l}&Type=CP`

**状态**: 被限流 —— 空体 400/412，是 WAF 频控不是业务错误

**参数**: `t`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/GoodsPassInfo?Billno="+t+"&Terid="+l+"&Type=CP")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/GoodsPassInfo?Billno=BEAU2324412&Terid=Y&Type=CP
```

```json
(空响应体)
```


### `GET /api/GoodsPassInfo?Billno={t}&Terid={l}&Type=QP`

**状态**: 550 —— 参数类型不对，后端未处理异常

**参数**: `t`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/GoodsPassInfo?Billno="+t+"&Terid="+l+"&Type=QP")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/GoodsPassInfo?Billno=BEAU2324412&Terid=Y&Type=QP
```

```json
{
  "Code": 500,
  "Msg": "服务器发生未处理的异常",
  "Count": null,
  "Data": null
}
```


### `GET /api/GoodsPassInfo?Billno={t}&Terid={l}&Type=DP`

**状态**: 550 —— 参数类型不对，后端未处理异常

**参数**: `t`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/GoodsPassInfo?Billno="+t+"&Terid="+l+"&Type=DP")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/GoodsPassInfo?Billno=BEAU2324412&Terid=Y&Type=DP
```

```json
{
  "Code": 500,
  "Msg": "服务器发生未处理的异常",
  "Count": null,
  "Data": null
}
```


## 8. 预录信息

**页面路由**: `/CntOrderPage`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `cntr_no` | 箱号 | `term` | 码头 |
| `vnt_type` | 箱型 | `engvslname` | 船名 |
| `cnvslname` | 中文船名 | `exp_voyage` | 出口航次 |
| `cnt_inyardtm` | 集装箱进港时间 | `tabs` | 操作 |


### `GET /api/Prerecorded/{a}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 箱号 |

前端调用：
```js
$api.get("/api/Prerecorded/"+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/Prerecorded/BEAU2324412
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "cntr_no": "BEAU2324412",
      "cntr_tid": "73279665",
      "ter_sid": "100304",
      "source_version": "5",
      "term": "振东(外2)",
      "vnt_type": "GP",
      "engvslname": "KMTC XIAMEN",
      "cnvslname": "高丽厦门",
      "exp_voyage": "2505S",
      "costco_rec_tm": null,
      "costco_handle_tm": null,
      "cnt_inyardtm": "2025-07-02 04:09",
      "cntr_costco_time": null
    },
    {
      "cntr_no": "BEAU2324412",
      "cntr_tid": "72062757",
      "ter_sid": "100304",
      "source_version": "5",
      "term": "振东(外2)",
      "vnt_type": "GP",
      "engvslname": "TORRANCE",
      "cnvslname": "以星托伦斯",
      "exp_voyage": "32W",
      "costco_rec_tm": null,
      "costco_handle_tm": null,
      "cnt_inyardtm": "2025-02-27 18:05",
      "cntr_costco_time": null
    },
    "…共 8 条"
  ]
}
```


## 9. 违规查询

**页面路由**: `/ViolationRecordToWO`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `evr_number` | 违规单号 | `evr_car_number` | 车号 |
| `evr_cst_pkidtext` | 违规车队 | `evr_evt_idtext` | 违规类型 |
| `evr_time` | 违规时间 | `evr_eva_idtext` | 违规场所 |
| `evr_limit_fgtext` | 禁止作业区域 |  |  |


### `GET /api/ViolationRecordToWO/{a}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/ViolationRecordToWO/"+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ViolationRecordToWO/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


## 10. 微信推送

**页面路由**: `/WechatPush`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `vessel_chn_name` | 中文船名 | `vessel_eng_name` | 英文船名 |
| `voy_imp_name` | 进口航次 | `voy_exp_name` | 出口航次 |
| `dwcc_name` | 类型 | `create_time` | 定制时间 |
| `push_time` | 推送时间 | `tabs` | 操作 |
| `cntr_no` | 箱号 | `term_name` | 码头 |
| `billno` | 提单号 |  |  |


### `GET /api/WechatPushSchedule`

**状态**: 未验证 —— 疑似写操作，主动跳过

前端调用：
```js
$api.get("/api/WechatPushSchedule")
```


### `GET /api/WechatPushSchedule/GetWechatContainerList`

**状态**: 未验证 —— 疑似写操作，主动跳过

前端调用：
```js
$api.get("/api/WechatPushSchedule/GetWechatContainerList")
```


### `GET /api/WechatPushSchedule/GetWechatGoodsList`

**状态**: 未验证 —— 疑似写操作，主动跳过

前端调用：
```js
$api.get("/api/WechatPushSchedule/GetWechatGoodsList")
```


### `DELETE /api/WechatPush/DeleteSchedule/{t.row.key_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.key_id`（含义未验证）

前端调用：
```js
$api.delete("/api/WechatPush/DeleteSchedule/"+t.row.key_id)
```


### `DELETE /api/WechatPush/{t.row.key_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.key_id`（含义未验证）

前端调用：
```js
$api.delete("/api/WechatPush/"+t.row.key_id)
```


### `DELETE /api/WechatPush/DeleteGoods/{t.row.key_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.key_id`（含义未验证）

前端调用：
```js
$api.delete("/api/WechatPush/DeleteGoods/"+t.row.key_id)
```


## 11. 多船 AIS

**页面路由**: `/MultiVesselAis`　**需登录**: 是


### `GET https://api.map.baidu.com/geoconv/v2/?coords={n.lng}+{n.lat}&model=2&ak=HPziyEnFKU4PPykhdieP4L4K`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `n.lng`, `n.lat`（含义未验证）

前端调用：
```js
$api.get("https://api.map.baidu.com/geoconv/v2/?coords=".concat(n.lng,",").concat(n.lat,"&model=2&ak=HPziyEnFKU4PPykhdieP4L4K"))
```


### `GET /api/VesselAis/{e}`

**状态**: 550 —— 参数类型不对，后端未处理异常

**参数**: `e`（含义未验证）

前端调用：
```js
$api.get("/api/VesselAis/".concat(e))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/VesselAis/Y
```

```json
(空响应体)
```


### `GET /api/VesselAis/getMultiVesselAis?mmsis={e}`

**状态**: 550 —— 参数类型不对，后端未处理异常

**参数**: `e`（含义未验证）

前端调用：
```js
$api.get("/api/VesselAis/getMultiVesselAis?mmsis=".concat(e))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/VesselAis/getMultiVesselAis?mmsis=Y
```

```json
(空响应体)
```


### `GET /api/aissearch/AisMultiVessel/SeaRoute`

**状态**: 550 —— 参数类型不对，后端未处理异常

前端调用：
```js
$api.get("/api/aissearch/AisMultiVessel/SeaRoute")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/aissearch/AisMultiVessel/SeaRoute
```

```json
(空响应体)
```


### `GET /api/aissearch/AisMultiVessel/ServiceLine`

**状态**: 550 —— 参数类型不对，后端未处理异常

前端调用：
```js
$api.get("/api/aissearch/AisMultiVessel/ServiceLine")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/aissearch/AisMultiVessel/ServiceLine
```

```json
(空响应体)
```


### `GET /api/aissearch/AisMultiVessel/VesselMonthPlan?servicelinecd={e}&eta={t}&srtId={a}`

**状态**: 404 —— 路径参数为空，路由匹配不上

**参数**: `e`, `t`, `a`（含义未验证）

前端调用：
```js
$api.get("/api/aissearch/AisMultiVessel/VesselMonthPlan?servicelinecd=".concat(e,"&eta=").concat(t,"&srtId=").concat(a))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/aissearch/AisMultiVessel/VesselMonthPlan?servicelinecd=&eta=&srtId=
```

```json
(空响应体)
```


### `POST /api/aissearch/AisMultiVessel/VesselMonthPlanByList`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/aissearch/AisMultiVessel/VesselMonthPlanByList")
```


### `POST /api/aissearch/AisMultiVessel/Terminal`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/aissearch/AisMultiVessel/Terminal")
```


### `POST /api/aissearch/AisMultiVessel/AISAreaPoint`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/aissearch/AisMultiVessel/AISAreaPoint")
```


## 12. 申芜快航 AIS

**页面路由**: `/SWKXVesselAis`　**需登录**: 是


### `GET /api/SWKXAIS/GetSwkxLine`

**状态**: 实调 —— 有数据

前端调用：
```js
$api.get("/api/SWKXAIS/GetSwkxLine")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/SWKXAIS/GetSwkxLine
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "value": "SWHK",
      "label": "申芜快航"
    }
  ]
}
```


### `GET /api/SWKXAIS/SwkxLineVessel?linecd={e}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `e` | 查询参数 `linecd`（实调填 `Y`） |

前端调用：
```js
$api.get("/api/SWKXAIS/SwkxLineVessel?linecd=".concat(e))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/SWKXAIS/SwkxLineVessel?linecd=Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/SWKXAIS/MassSearchSchedules?VesselChnName={e}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `e` | 查询参数 `VesselChnName`（实调填 `Y`） |

前端调用：
```js
$api.get("/api/SWKXAIS/MassSearchSchedules?VesselChnName=".concat(e))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/SWKXAIS/MassSearchSchedules?VesselChnName=Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/SWKXAIS/MassSearchContainers?CntOpVslname={e}&CntOpVOyage={n}&TerId={t}`

**状态**: 504 —— <html>
<head><title>504 Gateway Time-out</title></head>
<body>
<center><h1>504 Gateway Time-out</h1></center>
<!-- a

**参数**: `e`, `n`, `t`（含义未验证）

前端调用：
```js
$api.get("/api/SWKXAIS/MassSearchContainers?CntOpVslname=".concat(e,"&CntOpVOyage=").concat(n,"&TerId=").concat(t))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/SWKXAIS/MassSearchContainers?CntOpVslname=&CntOpVOyage=&TerId=
```

```json
<html>
<head><title>504 Gateway Time-out</title></head>
<body>
<center><h1>504 Gateway Time-out</h1></center>
<!-- a padding to disable MSIE and Chrome friendly error page -->
<!-- a padding to disable MSIE and Chrome friendly error page -->
<!-- a padding to disable MSIE and Chrome friendly error page -->
<!-- a padding to disable MSIE and Chrome friendly error page -->
<!-- a padding to 
```


## 13. 备案查询

**页面路由**: `/RecordSelect`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `statustext` | 状态 | `submit_time` | 提交时间 |
| `approved_time` | 审核时间 | `driver_id` | 驾驶员身份证 |
| `escort_id` | 押运员身份证 | `truckhead_number` | 车头车号 |
| `trucktrailer_number` | 挂车车号 | `tabs` | 操作 |


### `GET /api/RecordSelect/GetListRecord?keyNo={i}&SerialNo={s}&status={a}`

**状态**: 参数校验 —— 实调返回：身份证号、车牌号必须输入一项才能查询

**参数**: `i`, `s`, `a`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetListRecord?keyNo="+i+"&SerialNo="+s+"&status="+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetListRecord?keyNo=&SerialNo=&status=
```

```json
{
  "code": 400,
  "msg": "身份证号、车牌号必须输入一项才能查询",
  "count": null,
  "data": null
}
```


## 14. 备案审核

**页面路由**: `/RecordApproved`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `statustext` | 状态 | `submit_time` | 提交时间 |
| `approved_time` | 审核时间 | `driver_id` | 驾驶员身份证 |
| `escort_id` | 押运员身份证 | `truckhead_number` | 车头车号 |
| `trucktrailer_number` | 挂车车号 | `approved_remark` | 审核说明 |
| `tabs` | 操作 |  |  |


### `GET /api/RecordSelect/GetListRecord?keyNo={s}&SerialNo={o}&status={a}&stDate={i}&endDate={n}`

**状态**: 参数校验 —— 实调返回：身份证号、车牌号必须输入一项才能查询

**参数**: `s`, `o`, `a`, `i`, `n`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetListRecord?keyNo="+s+"&SerialNo="+o+"&status="+a+"&stDate="+i+"&endDate="+n)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetListRecord?keyNo=&SerialNo=&status=&stDate=&endDate=
```

```json
{
  "code": 400,
  "msg": "身份证号、车牌号必须输入一项才能查询",
  "count": null,
  "data": null
}
```


### `POST /api/RecordSelect/UnApprovedRecord/{t.row.id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.id`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/UnApprovedRecord/"+t.row.id)
```


## 15. 核放单核对

**页面路由**: `/OdsCheck`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `index` | 序号 | `ter_name` | 码头 |
| `trucknum` | 车牌号 | `used_time` | 核销时间 |
| `tabs` | 操作 | `api_name` | 接口名称 |
| `cntr1` | 箱号1 | `cntr2` | 箱号2 |
| `dtcl_time` | 调用时间 | `status_fg` | 调用结果 |
| `dtcl_result` | 返回报文 | `serial_no` | 车号 |
| `opmode2` | 物流车队作业 | `opmode` | 作业方式 |
| `inyard_time` | 进场时间 | `cntr_no1_display` | 箱号1 |
| `cntr_no2_display` | 箱号2 | `ods_fg` | 运单匹配状态 |
| `dpr_statustext` | 资质审核状态 | `dprno_reason` | 无运单作业原因 |


### `GET /api/RecordSelect/GetOdsInfosIn7Days?CntNo={a}&SerialNo={t}&ter={e}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 查询参数 `CntNo` |
| `t` | 查询参数 `SerialNo` |
| `e` | 查询参数 `ter` |

前端调用：
```js
$api.get("/api/RecordSelect/GetOdsInfosIn7Days?CntNo="+a+"&SerialNo="+t+"&ter="+e)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetOdsInfosIn7Days?CntNo=&SerialNo=&ter=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetRecordsByOTR/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/RecordSelect/GetRecordsByOTR/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetRecordsByOTR/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `POST /api/RecordSelect/SetRecordNoOds?keyId={i}&reason={e}&PKID={s}&remark={n}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `i`, `e`, `s`, `n`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/SetRecordNoOds?keyId="+i+"&reason="+e+"&PKID="+s+"&remark="+n)
```


### `GET /api/RecordSelect/GetWhiteList?numberplate={a}&ter={e}&usedFG={i}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `a` | 查询参数 `numberplate` |
| `e` | 查询参数 `ter` |
| `i` | 查询参数 `usedFG` |

前端调用：
```js
$api.get("/api/RecordSelect/GetWhiteList?numberplate="+a+"&ter="+e+"&usedFG="+i)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetWhiteList?numberplate=&ter=&usedFG=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `POST /api/RecordSelect/AddWhiteList?numberplate={a}&ter={e}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `a`, `e`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/AddWhiteList?numberplate="+a+"&ter="+e)
```


### `POST /api/RecordSelect/DelWhiteList?key={a}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `a`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/DelWhiteList?key="+a)
```


### `GET /api/RecordSelect/GetApiCheckLog?StartDate={s}&EndDate={n}&numberplate={a}&cntr_no={i}&ter={e}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `s` | 查询参数 `StartDate` |
| `n` | 查询参数 `EndDate` |
| `a` | 查询参数 `numberplate` |
| `i` | 查询参数 `cntr_no` |
| `e` | 查询参数 `ter` |

前端调用：
```js
$api.get("/api/RecordSelect/GetApiCheckLog?StartDate="+s+"&EndDate="+n+"&numberplate="+a+"&cntr_no="+i+"&ter="+e)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetApiCheckLog?StartDate=&EndDate=&numberplate=&cntr_no=&ter=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetTruckOperInfos?SerialNo={s}&terid={a}&OnlyArchiveFG={i}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `s`, `a`, `i`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetTruckOperInfos?SerialNo="+s+"&terid="+a+"&OnlyArchiveFG="+i)
```


### `POST /api/RecordSelect/CancleSetRecordNoOds/{t.row.dprno_id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.row.dprno_id`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/CancleSetRecordNoOds/"+t.row.dprno_id)
```


## 16. 备案归档

**页面路由**: `/RecordArchive`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `serial_no` | 车号 |
| `opmode` | 作业方式 | `inyard_time` | 进场时间 |
| `cntr_no1_display` | 箱号1 | `cntr_no2_display` | 箱号2 |
| `ods_fg` | 运单匹配状态 | `dpr_statustext` | 资质审核状态 |
| `tabs` | 操作 |  |  |


### `POST /api/RecordSelect/ArchiveRecord?id={a}&otrid={s}&ldno={e}&noodsid={i}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `a`, `s`, `e`, `i`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/ArchiveRecord?id="+a+"&otrid="+s+"&ldno="+e+"&noodsid="+i)
```


### `GET /api/DngPL/Get/{e}`

**状态**: 参数校验 —— 实调返回：未找到数据

**参数**: `e`（含义未验证）

前端调用：
```js
$api.get("/api/DngPL/Get/"+e)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/DngPL/Get/1
```

```json
{
  "code": 400,
  "msg": "未找到数据",
  "count": null,
  "data": null
}
```


### `GET /api/RecordSelect/GetCntrInfoByID/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/RecordSelect/GetCntrInfoByID/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetCntrInfoByID/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetOdsInfos/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/RecordSelect/GetOdsInfos/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetOdsInfos/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetTruckOperInfos?SerialNo={l}&terid={s}&OnlyArchiveFG=Y`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `l`, `s`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetTruckOperInfos?SerialNo="+l+"&terid="+s+"&OnlyArchiveFG=Y")
```


## 17. 归档查询

**页面路由**: `/RecordArchiveSelect`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `archive_date` | 核验时间 |
| `archive_user` | 核验人员 | `driver_id` | 驾驶员身份证 |
| `escort_id` | 押运员身份证 | `truckhead_number` | 车头车号 |
| `trucktrailer_number` | 挂车车号 | `cntr_no1` | 箱号1 |
| `cntr_no2` | 箱号2 | `ods_check_time` | 运单匹配时间 |
| `ods_send_time` | 运单核查发送时间 | `send_errormsg` | 运单核查发送异常信息 |
| `tabs` | 操作 |  |  |


### `GET /api/RecordSelect/GetArchiveCntrInfoByID/{e}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `e`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetArchiveCntrInfoByID/"+e)
```


### `GET /api/RecordSelect/GetArchiveNoOdsInfoByID/{e}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `e`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetArchiveNoOdsInfoByID/"+e)
```


### `GET /api/RecordSelect/GetArchiveOdsInfoByID/{s}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `s`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetArchiveOdsInfoByID/"+s)
```


### `GET /api/RecordSelect/GetArchiveInfos?CntNo={l}&SerialNo={n}&ter={s}&stDate={a}&endDate={i}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `l`, `n`, `s`, `a`, `i`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetArchiveInfos?CntNo="+l+"&SerialNo="+n+"&ter="+s+"&stDate="+a+"&endDate="+i)
```


## 18. 核放单查询

**页面路由**: `/OdsSelect`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `serial_no` | 车牌号 |
| `tractor_vehno` | 挂车牌号 | `driver_name` | 驾驶员 |
| `driver_card` | 驾驶员身份证 | `supercargo_name` | 押运员 |
| `supercargo_card` | 押运员身份证 | `start_tran_date` | 出车时间 |
| `container_no` | 箱号 | `goods_name` | 货物品名 |
| `ods_send_time` | 运单核查发送时间 | `send_errormsg` | 运单核查发送异常信息 |


### `GET /api/RecordSelect/GetAllOdsInfos?CntNo={s}&SerialNo={l}&ter={a}&stDate={i}&endDate={n}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `s` | 查询参数 `CntNo` |
| `l` | 查询参数 `SerialNo` |
| `a` | 查询参数 `ter` |
| `i` | 查询参数 `stDate` |
| `n` | 查询参数 `endDate` |

前端调用：
```js
$api.get("/api/RecordSelect/GetAllOdsInfos?CntNo="+s+"&SerialNo="+l+"&ter="+a+"&stDate="+i+"&endDate="+n)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetAllOdsInfos?CntNo=&SerialNo=&ter=&stDate=&endDate=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


## 19. 核放单补发

**页面路由**: `/OdsReissue`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `ter_name` | 码头 | `serial_no` | 车牌号 |
| `trucktrailer_number` | 挂车牌号 | `driver_id` | 驾驶员身份证 |
| `escort_id` | 押运员身份证 | `dprno_reason` | 无运单原因 |
| `dprno_cntr_no` | 作业箱号 | `inyard_date_time` | 进场时间 |
| `dpra_archive_date` | 场内核验时间 | `dprno_ods_fg_time` | 运单匹配时间 |
| `tabs` | 操作 |  |  |


### `POST /api/RecordSelect/ReissueOds?ArchiveID={a}&ldno={e}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `a`, `e`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/ReissueOds?ArchiveID="+a+"&ldno="+e)
```


### `GET /api/RecordSelect/GetOdsInfosByArchiveID/{t}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `t`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetOdsInfosByArchiveID/"+t)
```


### `GET /api/RecordSelect/GetAllNoOds?SerialNo={n}&ter={a}&stDate={i}&endDate={s}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `n` | 查询参数 `SerialNo` |
| `a` | 查询参数 `ter` |
| `i` | 查询参数 `stDate` |
| `s` | 查询参数 `endDate` |

前端调用：
```js
$api.get("/api/RecordSelect/GetAllNoOds?SerialNo="+n+"&ter="+a+"&stDate="+i+"&endDate="+s)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetAllNoOds?SerialNo=&ter=&stDate=&endDate=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


## 20. 证件查询

**页面路由**: `/CertificatesSelect`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `file_type` | 资质类型 | `file_keyid` | 资质证号 |
| `file_info` | 资质信息 | `expiry_date` | 有效期 |
| `approved_time` | 审核时间 | `approved_user` | 审核人 |
| `tabs` | 操作 |  |  |


### `GET /api/RecordSelect/GetRelevantRecord/{t}/{e}`

**状态**: 参数校验 —— 实调返回：未找到数据

**参数**: `t`, `e`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetRelevantRecord/"+t+"/"+e)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetRelevantRecord/BEAU2324412/Y
```

```json
{
  "code": 400,
  "msg": "未找到数据",
  "count": null,
  "data": null
}
```


### `POST /api/RecordSelect/SetRecordInvalid?FILE_TYPE={i}&FILE_ID={s}&remark={e}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `i`, `s`, `e`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/SetRecordInvalid?FILE_TYPE="+i+"&FILE_ID="+s+"&remark="+e)
```


### `POST /api/RecordSelect/ModifyRecordInfo?FILE_TYPE={e}&FILE_ID={a}&FILE_INFO={i}&EXPIRY_DATE={s}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `e`, `a`, `i`, `s`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/ModifyRecordInfo?FILE_TYPE="+e+"&FILE_ID="+a+"&FILE_INFO="+i+"&EXPIRY_DATE="+s)
```


### `GET /api/RecordSelect/GetCertificates?keyNo={l}&SerialNo={n}&status={a}`

**状态**: 403 —— 测试账号无此模块权限

**参数**: `l`, `n`, `a`（含义未验证）

前端调用：
```js
$api.get("/api/RecordSelect/GetCertificates?keyNo="+l+"&SerialNo="+n+"&status="+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetCertificates?keyNo=&SerialNo=&status=
```

```json
(空响应体)
```


## 21. 节点追踪

**页面路由**: `/MaasNodes`　**需登录**: 是


**页面字段对照**（取自前端表格列定义）:

| 字段 | 中文 | 字段 | 中文 |
|---|---|---|---|
| `tabs` | 操作 |  |  |


### `GET /api/MassNodes/AllList?status={e}&iefg={t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `e` | 查询参数 `status`（实调填 `BEAU2324412`） |
| `t` | 查询参数 `iefg`（实调填 `Y`） |

前端调用：
```js
$api.get("/api/MassNodes/AllList?status="+e+"&iefg="+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/AllList?status=BEAU2324412&iefg=Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `DELETE /api/MassNodes/{a.row.id}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `a.row.id`（含义未验证）

前端调用：
```js
$api.delete("/api/MassNodes/"+a.row.id)
```


### `POST /api/MassNodes`

**状态**: 未验证 —— 非 GET，未探测

前端调用：
```js
$api.post("/api/MassNodes")
```


## 22. 节点追踪（免登录）

**页面路由**: `/nodes`　**需登录**: 否


### `GET /api/MassNodes/ListByStatus?status={t.cnStatus}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t.cnStatus` | 查询参数 `status`（实调填 `Y`） |

前端调用：
```js
$api.get("/api/MassNodes/ListByStatus?status="+t.cnStatus)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/ListByStatus?status=Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET {a.url}&cntrPkid={t.cnPkid}`

**状态**: 未验证 —— 不是接口路径

**参数**: `a.url`, `t.cnPkid`（含义未验证）

前端调用：
```js
$api.get(a.url+"&cntrPkid="+t.cnPkid)
```


## 23. 多页面共用 / 未归类接口


### `GET /api/AgentScheduleR?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 被限流 —— 空体 400/412，是 WAF 频控不是业务错误

**参数**: `a`, `i`, `n`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/AgentScheduleR?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/AgentScheduleR?Vname=&Voyage=&Terid=&Gzfg=
```

```json
(空响应体)
```


### `GET /api/CntrHistory/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 箱号 |

前端调用：
```js
$api.get("/api/CntrHistory/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/CntrHistory/BEAU2324412
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "cnttrends": [
        {
          "op_sid": "D",
          "op_name": "卸船",
          "op_time": "08/02/2026 19:12:56",
          "cntr_tid": "42764211",
          "ter_sid": "100305",
          "sort_column": "08/02/2026 19:12:56",
          "pkid": "510030542764211",
          "cntr_global_id": null
        },
        {
          "op_sid": "CP",
          "op_name": "海放",
          "op_time": "08/03/2026 10:25:41",
          "cntr_tid": "42764211",
          "ter_sid": "100305",
          "sort_column": "08/03/2026 10:25:41",
          "pkid": "510030542764211",
          "cntr_global_id": null
        },
        {
          "op_sid": "O",
          "op_name": "出场发箱",
          "op_time": "08/04/2026 18:51:37",
          "cntr_tid": "42764211",
          "ter_sid": "100305",
          "sort_column": "08/04/2026 18:51:37",
          "pkid": "510030542764211",
          "cntr_global_id": null
        }
      ],
      "ter_sid": "100305",
      "ter_name": "沪东",
      "sort_column": "08/02/2026 19:12:56",
      "in_flag": "D",
      "out_flag": null,
      "cntr_global_id": null,
      "in_display_name": "高丽深圳 2604W/2605E",
      "in_display_id": "174747",
      "out_display_name": null,
      "out_display_id": null,
      "pkid": "510030542764211"
    }
  ]
}
```


### `GET /api/ContainerDetail/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | `cntr_pkid` 箱主键（见 `/api/ContainerHistoryList` 响应） |

前端调用：
```js
$api.get("/api/ContainerDetail/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainerDetail/510030542764211
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": {
    "ter_name": "沪东(外4)",
    "cntr_no": "BEAU2324412",
    "voy_imp_name": "KMTC SHENZHEN/高丽深圳/2604W",
    "voy_exp_name": "//",
    "cntr_type": "20 GP 8'6''",
    "cntr_cop": "KMTC/高丽海运",
    "cntr_status": "IE/进口空箱",
    "inyard_date_time": "2026-08-02 19:12",
    "inyard_mode": "卸船进场",
    "outyard_date_time": "2026-08-04 19:20",
    "outyard_mode": "门/门提空箱",
    "costco_fg": "N",
    "cp_fg": "Y",
    "tp_fg": null,
    "msapass_fg": "N",
    "stowage_fg": "N",
    "port_load": "USLGB/长滩",
    "port_discharge": "CNSHA/上海",
    "port_destination": "CNSHA/上海",
    "port_transfer": "/",
    "m_tare_ton": 2210,
    "m_gross_ton": 2210,
    "m_evgm_ton": 0,
    "m_tvgm_ton": 0,
    "y_location": "场外",
    "seal_no": "CF583046",
    "cntr_settemp": null,
    "cntr_dngclass": null,
    "cntr_unno": null,
    "cntr_overlimited": null,
    "m_ovl_front": null,
    "m_ovl_bottom": null,
    "m_ovl_left": null,
    "m_ovl_right": null,
    "m_ovl_top": null,
    "cntr_from": null,
    "cntr_to": null,
    "ict_fg": "N",
    "searail_fg": "N"
  }
}
```


### `GET /api/ContainerGoodsDetail/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | `cntr_pkid` 箱主键（见 `/api/ContainerHistoryList` 响应） |

前端调用：
```js
$api.get("/api/ContainerGoodsDetail/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainerGoodsDetail/510030542764211
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "goods_ie_fg": "I",
      "goods_pkid": "510030551457648",
      "bill_no": "KORPLGB0005157",
      "goods_packages": 1,
      "goods_weight": 0,
      "goods_volume": 0,
      "entry_no": "2225202600024175",
      "cp_fg": "N",
      "cp_remark": "系统中提单号所关联的集装箱报文没有"
    }
  ]
}
```


### `GET /api/ContainerHistoryList/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 箱号 |

前端调用：
```js
$api.get("/api/ContainerHistoryList/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainerHistoryList/BEAU2324412
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "ter_name": "沪东(外4)",
      "cntr_pkid": "510030542764211",
      "cntr_no": "BEAU2324412",
      "cntr_type": "20 GP 8'6''",
      "cntr_cop": "KMTC",
      "cntr_status": "进口空箱",
      "inyard_date_time": "08/02/2026 19:12:57",
      "inyard_mode": "卸船进场",
      "outyard_date_time": "08/04/2026 19:20:13",
      "outyard_mode": "门/门提空箱"
    },
    {
      "ter_name": "沪东(外4)",
      "cntr_pkid": "510030542109524",
      "cntr_no": "BEAU2324412",
      "cntr_type": "20 GP 8'6''",
      "cntr_cop": "KMTC",
      "cntr_status": "出口重箱",
      "inyard_date_time": "05/19/2026 04:29:04",
      "inyard_mode": "出口重箱进场",
      "outyard_date_time": "05/24/2026 18:32:56",
      "outyard_mode": "装船出场"
    },
    "…共 3 条"
  ]
}
```


### `GET /api/ContainerPlanDetail/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | `cntr_pkid` 箱主键（见 `/api/ContainerHistoryList` 响应） |

前端调用：
```js
$api.get("/api/ContainerPlanDetail/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainerPlanDetail/510030542764211
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "plan_type": "门/门提空箱",
      "plan_time": "2024-09-20 07:29",
      "op_start_time": "2019-11-10 12:00",
      "op_end_time": "9999-09-19 19:00",
      "status": "完成"
    }
  ]
}
```


### `GET /api/ContainerR/{n}/{a}`

**状态**: 被限流 —— 空体 400/412，是 WAF 频控不是业务错误

**参数**: `n`, `a`（含义未验证）

前端调用：
```js
$api.get("/api/ContainerR/"+n+"/"+a)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ContainerR/BEAU2324412/Y
```

```json
(空响应体)
```


### `GET /api/DngPL/Get/{t}`

**状态**: 参数校验 —— 实调返回：未找到数据

**参数**: `t`（含义未验证）

前端调用：
```js
$api.get("/api/DngPL/Get/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/DngPL/Get/1
```

```json
{
  "code": 400,
  "msg": "未找到数据",
  "count": null,
  "data": null
}
```


### `GET /api/GuoTie/getGtTrainStationListAsync?cntrpkid={t}&opmode={e}`

**状态**: 参数校验 —— 实调返回：OracleCommand.CommandText is invalid

**参数**: `t`, `e`（含义未验证）

前端调用：
```js
$api.get("/api/GuoTie/getGtTrainStationListAsync?cntrpkid=".concat(t,"&opmode=").concat(e))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/GuoTie/getGtTrainStationListAsync?cntrpkid=BEAU2324412&opmode=Y
```

```json
{
  "code": 400,
  "msg": "OracleCommand.CommandText is invalid",
  "count": null,
  "data": null
}
```


### `GET /api/MassNodes/CntrMmsi/{cnPkid}`

**状态**: 参数校验 —— 实调返回：未找到数据

**参数**: `cnPkid`（含义未验证）

前端调用：
```js
$api.get("/api/MassNodes/CntrMmsi/"+this.cnPkid)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/CntrMmsi/Y
```

```json
{
  "code": 400,
  "msg": "未找到数据",
  "count": null,
  "data": null
}
```


### `GET /api/MassNodes/CntrMmsi/{t}/{e}/{i}`

**状态**: 400 —— {"type":"https://tools.ietf.org/html/rfc7231#section-6.5.1","title":"One or more validation errors occurred.","status":4

**参数**: `t`, `e`, `i`（含义未验证）

前端调用：
```js
$api.get("/api/MassNodes/CntrMmsi/"+t+"/"+e+"/"+i)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/CntrMmsi///
```

```json
{
  "type": "https://tools.ietf.org/html/rfc7231#section-6.5.1",
  "title": "One or more validation errors occurred.",
  "status": 400,
  "traceId": "00-00000000000000001755dc2b8eee0577-10253ef2293a329b-01",
  "errors": {
    "id": [
      "The value 'CntrMmsi' is not valid."
    ]
  }
}
```


### `GET /api/MassNodes/GtContainers/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/MassNodes/GtContainers/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/GtContainers/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/MassNodes/ListByStatus?status={t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 查询参数 `status`（实调填 `Y`） |

前端调用：
```js
$api.get("/api/MassNodes/ListByStatus?status="+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/MassNodes/ListByStatus?status=Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/Menu`

**状态**: 实调 —— 通了，但该条件下无数据

前端调用：
```js
$api.get("/api/Menu")
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/Menu
```

```json
{
  "code": 200,
  "data": null
}
```


### `GET /api/RecordSelect/GetAllOdsInfos?CntNo=&SerialNo={c}&ter={a}&stDate={n}&endDate={l}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `c` | 查询参数 `SerialNo` |
| `a` | 查询参数 `ter` |
| `n` | 查询参数 `stDate` |
| `l` | 查询参数 `endDate` |

前端调用：
```js
$api.get("/api/RecordSelect/GetAllOdsInfos?CntNo=&SerialNo="+c+"&ter="+a+"&stDate="+n+"&endDate="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetAllOdsInfos?CntNo=&SerialNo=&ter=&stDate=&endDate=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetAllOdsInfos?CntNo={o}&SerialNo={r}&ter={l}&stDate={c}&endDate={s}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `o` | 查询参数 `CntNo` |
| `r` | 查询参数 `SerialNo` |
| `l` | 查询参数 `ter` |
| `c` | 查询参数 `stDate` |
| `s` | 查询参数 `endDate` |

前端调用：
```js
$api.get("/api/RecordSelect/GetAllOdsInfos?CntNo="+o+"&SerialNo="+r+"&ter="+l+"&stDate="+c+"&endDate="+s)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetAllOdsInfos?CntNo=&SerialNo=&ter=&stDate=&endDate=
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/RecordSelect/GetRecords?SerialNo={t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 查询参数 `SerialNo`（实调填 `沪FB1502`） |

前端调用：
```js
$api.get("/api/RecordSelect/GetRecords?SerialNo="+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/GetRecords?SerialNo=沪FB1502
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "index": null,
      "id": "96674",
      "app_id": "15150055128",
      "status": "6",
      "statustext": "审核通过",
      "approved_remark": "自动审核通过",
      "driver_id": "320305********2718",
      "escort_id": "410121********4826",
      "truckhead_number": "沪FB1502",
      "trucktrailer_number": "沪BM010挂",
      "approved_user": "706742",
      "approved_time": "2026-03-16 08:36",
      "submit_time": "2026-03-16 08:36",
      "approved_fg": "N",
      "unapproved_fg": "Y",
      "dprc_sort": 4
    },
    {
      "index": null,
      "id": "57523",
      "app_id": "18601797329",
      "status": "8",
      "statustext": "资质过期",
      "approved_remark": "自动审核通过;2024-11-21 00:01自动判断资质(车头行驶证、挂车行驶证)过期;押运员从业资格证失效;押运员从业资格证失效;押运员从业资格证失效",
      "driver_id": "320922********0111",
      "escort_id": "342422********6791",
      "truckhead_number": "沪FB1502",
      "trucktrailer_number": "沪BM010挂",
      "approved_user": "706742",
      "approved_time": "2024-07-06 17:39",
      "submit_time": "2024-07-06 17:39",
      "approved_fg": "N",
      "unapproved_fg": "N",
      "dprc_sort": 6
    }
  ]
}
```


### `GET /api/RecordSelect/GetTerminals?addif=N`

**状态**: 未验证 —— 疑似写操作，主动跳过

前端调用：
```js
$api.get("/api/RecordSelect/GetTerminals?addif=N")
```


### `GET /api/RecordSelect/GetTerminals?addif=Y`

**状态**: 未验证 —— 疑似写操作，主动跳过

前端调用：
```js
$api.get("/api/RecordSelect/GetTerminals?addif=Y")
```


### `GET /api/RecordSelect/{t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 日期，格式 `YYYY-MM-DD` |

前端调用：
```js
$api.get("/api/RecordSelect/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/RecordSelect/2026-08-04
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "dpr_id": "2014",
      "dpr_status_old": "4",
      "status_old_name": "资质失效",
      "dpr_status_new": "4",
      "status_new_name": "资质失效",
      "exec_user": "717664",
      "usr_name": "LF654321",
      "exec_time": "2024-05-14 21:10",
      "exec_type": "押运员从业资格证设为失效",
      "exec_source": "WEB"
    },
    {
      "dpr_id": "2014",
      "dpr_status_old": "4",
      "status_old_name": "资质失效",
      "dpr_status_new": "4",
      "status_new_name": "资质失效",
      "exec_user": "717664",
      "usr_name": "LF654321",
      "exec_time": "2024-05-14 21:10",
      "exec_type": "驾驶员从业资格证设为失效",
      "exec_source": "WEB"
    },
    "…共 6 条"
  ]
}
```


### `GET /api/ScheduleBerth/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/ScheduleBerth/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleBerth/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/ScheduleBerthR?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 403 —— 测试账号无此模块权限

**参数**: `a`, `i`, `n`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/ScheduleBerthR?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleBerthR?Vname=&Voyage=&Terid=&Gzfg=
```

```json
(空响应体)
```


### `GET /api/ScheduleRCVR/SW?Vname={a}&Voyage={n}`

**状态**: 403 —— 测试账号无此模块权限

**参数**: `a`, `n`（含义未验证）

前端调用：
```js
$api.get("/api/ScheduleRCVR/SW?Vname="+a+"&Voyage="+n)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleRCVR/SW?Vname=BEAU2324412&Voyage=Y
```

```json
(空响应体)
```


### `GET /api/ScheduleRCVR?Vname={a}&Voyage={i}&Terid={n}&Gzfg={l}`

**状态**: 403 —— 测试账号无此模块权限

**参数**: `a`, `i`, `n`, `l`（含义未验证）

前端调用：
```js
$api.get("/api/ScheduleRCVR?Vname="+a+"&Voyage="+i+"&Terid="+n+"&Gzfg="+l)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/ScheduleRCVR?Vname=&Voyage=&Terid=&Gzfg=
```

```json
(空响应体)
```


### `GET /api/TruckGPS/getBoxing?cntrpkid={t}`

**状态**: 实调 —— 有数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 查询参数 `cntrpkid`（实调填 `沪FB1502`） |

前端调用：
```js
$api.get("/api/TruckGPS/getBoxing?cntrpkid=".concat(t))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/TruckGPS/getBoxing?cntrpkid=沪FB1502
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": [
    {
      "boxingstationname": null,
      "account": null,
      "contactperson": null,
      "contactphone": null,
      "address": null,
      "createtime": null,
      "lblat": null,
      "lblng": null,
      "ltlat": null,
      "ltlng": null,
      "rblat": null,
      "rblng": null,
      "rtlat": null,
      "rtlng": null,
      "regno": null,
      "cntrnumber": null,
      "plantime": null,
      "boxingplanstate": null,
      "boxingplanstatename": null,
      "finishtime": null,
      "cntr_pkid": null,
      "cntrsizechncd": null,
      "cntrdnglevelengcd": null,
      "cntrtypeengcd": null,
      "cntrterminal": null,
      "insertdt": null,
      "updatedt": null
    }
  ]
}
```


### `GET /api/TruckGPS/getBoxingTruckGpsHistory?cntrpkid={t}&opmode={e}&cntstatus={i}`

**状态**: 参数校验 —— 实调返回：未找到数据

**参数**: `t`, `e`, `i`（含义未验证）

前端调用：
```js
$api.get("/api/TruckGPS/getBoxingTruckGpsHistory?cntrpkid=".concat(t,"&opmode=").concat(e,"&cntstatus=").concat(i))
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/TruckGPS/getBoxingTruckGpsHistory?cntrpkid=&opmode=&cntstatus=
```

```json
{
  "code": 400,
  "msg": "未找到数据",
  "count": null,
  "data": null
}
```


### `GET /api/VoyageCallingPort/{t}`

**状态**: 实调 —— 通了，但该条件下无数据

**参数**（路径占位符的含义由实调取值反推，仅供参考——实测部分接口塞不对的值也照样返回数据）:

| 占位符 | 实际含义 |
|---|---|
| `t` | 标志位 Y/N |

前端调用：
```js
$api.get("/api/VoyageCallingPort/"+t)
```

实调：
```
GET https://ghzh.tmaas.com.cn/ghzh/api/VoyageCallingPort/Y
```

```json
{
  "code": 200,
  "msg": "操作成功",
  "count": null,
  "data": []
}
```


### `GET /api/WechatPush/GetWechatScheduleList?scdPkid={scdPkid}`

**状态**: 未验证 —— 疑似写操作，主动跳过

**参数**: `scdPkid`（含义未验证）

前端调用：
```js
$api.get("/api/WechatPush/GetWechatScheduleList?scdPkid="+this.scdPkid)
```


### `GET /checktoken`

**状态**: 未验证 —— 不是接口路径

前端调用：
```js
$api.get("/checktoken")
```


### `GET {i.url}&cntrPkid={cnPkid}`

**状态**: 未验证 —— 不是接口路径

**参数**: `i.url`, `cnPkid`（含义未验证）

前端调用：
```js
$api.get(i.url+"&cntrPkid="+e.cnPkid)
```


### `POST /api/MassNodes/GetContaienrTraceNodeAsync?cntrPkid={t.cnPkid}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `t.cnPkid`（含义未验证）

前端调用：
```js
$api.post("/api/MassNodes/GetContaienrTraceNodeAsync?cntrPkid="+t.cnPkid)
```


### `POST /api/RecordSelect/ShApprovedRecord?keyId={n}&status={t}&remark={e}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `n`, `t`, `e`（含义未验证）

前端调用：
```js
$api.post("/api/RecordSelect/ShApprovedRecord?keyId="+n+"&status="+t+"&remark="+e)
```


### `POST /api/WechatPush/SaveWechatSchedule?scddwccs={group.join(",")}&scdpkid={scdPkid}`

**状态**: 未验证 —— 非 GET，未探测

**参数**: `group.join(",")`, `scdPkid`（含义未验证）

前端调用：
```js
$api.post("/api/WechatPush/SaveWechatSchedule?scddwccs="+this.group.join(",")+"&scdpkid="+this.scdPkid)
```


---

## 附录 A：怎么重跑

```powershell
# 1. 人工过一次验证码，cookie 存进 Browser Use profile（之后 --reuse 直接复用）
python -m maas.login
python -m maas.login --reuse

# 2. 抓前端 bundle（含懒加载 chunk），解析出全量接口清单
python -m maas.bundles
python -m maas.endpoints

# 3. 登录态下实调，拿真实响应
python -m maas.crawl --cdp <上一步打印的 cdpUrl> --auto

# 4. 重新生成本文档
python -m maas.report
```

样本值在 [maas/crawl.py](maas/crawl.py) 的 `POOL` / `PAIRS` 里，换成你手头的真实单号能提高命中率。

## 附录 B：没验证的部分

- **所有 POST/PUT/DELETE 接口一律没实调**。这是生产系统，写操作（备案审批、核放单作废、白名单增删、微信推送订阅）
  跑一次就在人家库里留数据，路径和参数名从代码里扒出来了，请求体结构没验证。
- `*R` 系列（`AgentScheduleR` / `ScheduleBerthR` / `ScheduleRCVR` / `GetCertificates`）测试账号 403，
  要更高权限的账号才能确认响应结构。
- 部分接口需要真实的船名/航次/提单号才有数据，用空参数只能拿到校验提示。

## 附录 C：反爬对采集方案的影响

瑞数动态 cookie 决定了**不能写成 npedi 那种 httpx 直连的爬虫**。可行的两条路：

1. **常驻浏览器**（本仓库采用）：Browser Use 云浏览器 + profile 保持登录态，
   在页面上下文里 `fetch()` 调接口。慢，但最稳，cookie/token/WAF 全自动。
2. **定期捞 cookie**：浏览器登录后导出 `2aCTSJaVda98O` 等动态 cookie 喂给 httpx。
   代码简单，但 cookie 有效期短、名字随部署变化，需要监控失效。

**但第 2 条不会更快**——频控是按会话/IP 算的，不是按客户端类型。实测快打 ~20 次就整个会话被封，
换 httpx 一样封。所以两条路的实际吞吐上限都是**约 4 秒一个请求**，全量 117 个接口跑一轮 ≈ 8 分钟。
真要提吞吐只能多账号 + 多出口 IP 并行，那是另一件事了。

无论哪条，登录环节的顶象滑块都得人工过一次——AI agent 拖了十几次没过（烧了 $1.4）。
profile 让这一次成本摊薄到"很久一次"。
