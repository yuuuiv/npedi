"""把 maas_api_map.json + maas_probe.json 合成 MAAS-API-REFERENCE.md。

文档分两截：
- 手写的开头（公共约定、认证、反爬），这部分是结论，机器写不出来；
- 按页面路由分节的接口清单，从 bundle 解析 + 实调结果里生成，重跑探测就能刷新。

    python -m maas.report
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from maas.crawl import better
from maas.recon import OUT

MAP = OUT.parent / "maas_api_map.json"
PROBE = OUT.parent / "maas_probe.json"
DOC = Path("MAAS-API-REFERENCE.md")

# 路由 → 页面中文名，取自各 chunk 里的 GhzhLeftMenu title
ROUTE_NAMES = {
    "/ContainerQuery": "箱货查询", "/ScheduleQuery": "船期查询", "/OutTruck": "外集卡动态信息",
    "/VgmQuery": "VGM 查询", "/PortTraffic": "港口流量", "/KeyTransit": "海峡通道",
    "/MyWatch": "我的收藏", "/PackingListQuery": "装箱单查询", "/ClearancePage": "放行信息",
    "/CntOrderPage": "预录信息", "/ViolationRecordToWO": "违规查询", "/WechatPush": "微信推送",
    "/MultiVesselAis": "多船 AIS", "/MultiVesselAisCopy": "AIS 查询", "/Ais": "AIS 查询",
    "/SWKXVesselAis": "申芜快航 AIS", "/DngPL": "危申报", "/RecordSelect": "备案查询",
    "/RecordApproved": "备案审核", "/OdsCheck": "核放单核对", "/RecordArchive": "备案归档",
    "/RecordArchiveSelect": "归档查询", "/OdsSelect": "核放单查询", "/OdsReissue": "核放单补发",
    "/CertificatesSelect": "证件查询", "/MaasNodes": "节点追踪", "/nodes": "节点追踪（免登录）",
    "/GhzhIndex": "港航纵横首页", "/": "首页",
}

HEAD = """# 集运 MaaS（ghzh.tmaas.com.cn）接口文档

**版本**: 1.0
**编制时间**: {today}
**站点**: 集运 MaaS 一门式查询 / 港航纵横 —— 上海海勃数科技术有限公司
**数据来源**: 前端 bundle 静态解析（全量接口清单）+ 登录态浏览器内实调（真实请求/响应）。
标注「实调」的响应是真跑出来的原样数据；标注「未验证」的只从代码里扒出了路径与参数名。

> 采集工具在 [maas/](maas/) 下，重跑方式见本文末「怎么重跑」。

## 0. 公共约定

**站点地址**: `https://ghzh.tmaas.com.cn`

**接口 Base URL**: `https://ghzh.tmaas.com.cn/ghzh`

前端 axios 实例是 `axios.create({{baseURL:"https://ghzh.tmaas.com.cn/ghzh"}})`，
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
  `400` + `Content-Type: text/html` + 只有 `\\r\\n\\r\\n` 的空体（业务 400 一定带 `msg`），
  连 SPA 自己的 js chunk 都一起 400，页面直接白屏。
  实测**重载页面、清掉动态 cookie 重跑挑战、换代理 IP 都救不回来**，只能等冷却（分钟级）。
  采集侧因此按 4 秒/次 + 5/10/15 分钟三级退避，连续三轮还封就存盘收工，下次接着补。

**免登录入口**: 路由表里 `meta.requireAuth` 为 `false` 的有 `/`、`/DngPL`（危申报）、`/nodes`（节点追踪）。
这几个页面不走 OAuth，对采集侧意味着**不用过验证码也不用维持 session**——但仍然要过瑞数。
其余页面虽然 `requireAuth: true`，`meta` 里还带 `publicQuery: true`，含义待确认。

**统一响应信封**:

```json
{{"code": 200, "msg": "操作成功", "count": null, "data": ...}}
```

- `code=200` 成功，`data` 是业务数据（数组或对象，无数据时是 `[]`）。注意 HTTP 状态也是 200 时 `code` 可能是 400。
- `code=400` 业务错误，`msg` 是中文原因（如「干支标记不能为空」「身份证号、车牌号必须输入一项才能查询」）。
- HTTP `403` 空体：账号无该模块权限。本次测试账号 `ela813267e45` 在 `*R`（复核/受限）系列接口上一律 403。
- HTTP `550` + `{{"Code":500,"Msg":"服务器发生未处理的异常"}}`：参数类型不对导致后端炸了，不是权限问题。
- HTTP `404`：路径参数为空时路由匹配不上。
- 下拉参照类接口统一返回 `[{{"value":"WGQ4","label":"沪东"}}]`。

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
"""

TAIL = """
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
"""


# 压缩后的占位符叫 {i}/{a}，看不出语义。但实调时哪个样本值填进去能出数据，
# 反过来就说明那个洞要什么——所以参数含义是从命中的 URL 里反推的，不是猜的。
VALUE_MEANING = {
    "沪FB1502": "车牌号（外集卡车号，会被前端转大写）",
    "2026-08-04": "日期，格式 `YYYY-MM-DD`",
    "BEAU2324412": "箱号",
    "510030542764211": "`cntr_pkid` 箱主键（见 `/api/ContainerHistoryList` 响应）",
    "WGQ4": "码头代码（见 `/api/edi/RefInfo/TERMINAL`）",
    "Y": "标志位 Y/N",
    "1": "类型码",
}


def infer_params(path: str, probed: str) -> list[tuple[str, str]]:
    """拿模板和实际打通的 URL 对齐，推出每个占位符实际填的是什么。"""
    holes = re.findall(r"\{([^}]*)\}", path)
    if not holes or not probed:
        return []
    # 模板转成捕获组正则，直接把实际值抠出来
    pattern = re.escape(path)
    for h in holes:
        pattern = pattern.replace(re.escape("{" + h + "}"), "(.*?)", 1)
    m = re.fullmatch(pattern, probed)
    if not m:
        return []
    out = []
    for h, v in zip(holes, m.groups()):
        # query 参数自带名字（`?cntrpkid={t}`），那名字是权威的，别拿样本值去猜。
        # 实测有接口塞了明显不对的值也照样 200 出数据，所以"能出数据"不等于"类型对"。
        named = re.search(r"[?&](\w+)=\{" + re.escape(h) + r"\}", path)
        if named:
            out.append((h, f"查询参数 `{named.group(1)}`（实调填 `{v}`）" if v else f"查询参数 `{named.group(1)}`"))
        else:
            out.append((h, VALUE_MEANING.get(v, f"实调填 `{v}`" if v else "实调填空串")))
    return out


def envelope(body: str) -> str:
    """响应体太长就截断到能看清结构的程度。"""
    body = (body or "").strip()
    if not body:
        return "(空响应体)"
    try:
        obj = json.loads(body)
    except Exception:
        return body[:400]
    d = obj.get("data")
    if isinstance(d, list) and len(d) > 2:
        obj["data"] = d[:2] + [f"…共 {len(d)} 条"]
    return json.dumps(obj, ensure_ascii=False, indent=2)[:2500]


def verdict(r: dict | None) -> tuple[str, str]:
    """(状态标记, 说明) —— 给每条接口一句结论。"""
    if not r:
        return "未验证", "代码里扒出来的，没实调"
    if r.get("skip"):
        return "未验证", r["skip"]
    s, body = r.get("status"), (r.get("body") or "")
    if s == 403:
        return "403", "测试账号无此模块权限"
    if s == 404:
        return "404", "路径参数为空，路由匹配不上"
    if s == 550:
        return "550", "参数类型不对，后端未处理异常"
    if s == 200:
        try:
            o = json.loads(body)
        except Exception:
            return "200", "响应不是 JSON"
        if o.get("code") == 400:
            return "参数校验", f"实调返回：{o.get('msg')}"
        return ("实调", "有数据") if o.get("data") else ("实调", "通了，但该条件下无数据")
    if s in (400, 412) and not body.strip():
        return "被限流", "空体 400/412，是 WAF 频控不是业务错误"
    return str(s), body[:120]


def main() -> None:
    m = json.loads(MAP.read_text(encoding="utf-8"))
    # 每轮探测都可能被瑞数打断，结果分散在 maas_probe*.json 里；按"谁的结果更实"合并，
    # 免得一次被封就把上一轮问到的真响应冲掉。
    probe: dict[str, dict] = {}
    for f in sorted(PROBE.parent.glob("maas_probe*.json")):
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            if better(v, probe.get(k)):
                probe[k] = v
    routes, every = m["routes"], m["all"]

    # 接口 → 出现在哪些页面。同一个接口可能被多个页面共用（chunk-common 里的）
    where: dict[str, list[str]] = {}
    for route, info in routes.items():
        for c in info.get("api", []):
            where.setdefault(f'{c["method"]} {c["path"]}', []).append(route)

    out = [HEAD.format(today=date.today())]
    done: set[str] = set()
    n = 0

    for route, info in routes.items():
        apis = [c for c in info.get("api", []) if f'{c["method"]} {c["path"]}' not in done
                and len(where.get(f'{c["method"]} {c["path"]}', [])) == 1]
        if not apis:
            continue
        n += 1
        name = ROUTE_NAMES.get(route, route.lstrip("/"))
        out.append(f"\n## {n}. {name}\n\n**页面路由**: `{route}`　**需登录**: "
                   f"{'是' if 'requireAuth:!0' in info.get('meta', '') else '否'}\n")
        fields = info.get("fields") or {}
        if fields:
            out.append("\n**页面字段对照**（取自前端表格列定义）:\n")
            out.append("| 字段 | 中文 | 字段 | 中文 |")
            out.append("|---|---|---|---|")
            items = list(fields.items())
            for a, b in zip(items[::2], items[1::2] + [("", "")]):
                out.append(f"| `{a[0]}` | {a[1]} | {f'`{b[0]}`' if b[0] else ''} | {b[1]} |")
            out.append("")

        for c in apis:
            key = f'{c["method"]} {c["path"]}'
            done.add(key)
            out.append(section(key, c, probe.get(key)))

    # 被多个页面共用的接口单独归一节，免得重复
    shared = [(k, v) for k, v in every.items() if k not in done]
    if shared:
        out.append(f"\n## {n + 1}. 多页面共用 / 未归类接口\n")
        for key, c in sorted(shared):
            out.append(section(key, c, probe.get(key)))

    out.append(TAIL)
    DOC.write_text("\n".join(out), encoding="utf-8")
    real = sum(1 for v in probe.values() if verdict(v)[0] == "实调")
    print(f"{len(every)} 个接口，其中 {real} 个有实调响应 -> {DOC}")


def section(key: str, c: dict, r: dict | None) -> str:
    method, path = key.split(" ", 1)
    tag, note = verdict(r)
    lines = [f"\n### `{method} {path}`\n",
             f"**状态**: {tag} —— {note}\n"]
    holes = re.findall(r"\{([^}]*)\}", path)
    if holes:
        inferred = infer_params(path, (r or {}).get("probed", "")) if tag == "实调" else []
        if inferred:
            lines.append("**参数**（路径占位符的含义由实调取值反推，仅供参考——"
                         "实测部分接口塞不对的值也照样返回数据）:\n")
            lines.append("| 占位符 | 实际含义 |")
            lines.append("|---|---|")
            lines += [f"| `{h}` | {meaning} |" for h, meaning in inferred]
            lines.append("")
        else:
            lines.append(f"**参数**: {', '.join(f'`{h}`' for h in holes)}（含义未验证）\n")
    if c.get("expr"):
        lines.append(f"前端调用：\n```js\n$api.{method.lower()}({c['expr']})\n```\n")
    if r and r.get("probed"):
        lines.append(f"实调：\n```\n{method} https://ghzh.tmaas.com.cn/ghzh{r['probed']}\n```\n")
        lines.append(f"```json\n{envelope(r.get('body') or '')}\n```\n")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
