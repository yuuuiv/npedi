"""登录态浏览器里逐个实调接口，拿真实响应。

关键在于：一旦 profile 里有登录 cookie，就不用再让 AI 去点菜单了——
在 ghzh 自己的页面上下文里 `fetch("/api/...")` 就是同源请求，
瑞数 cookie、OAuth session、token 全自动带上，
endpoints.py 从 bundle 里扒出来的 117 个接口可以直接一个个打过去。

**只打 GET，且跳过任何看起来会改数据的路径**（新增/删除/审批/作废……），
这是查询系统，别在人家生产库上留脚印。

    python -m maas.crawl --cdp <url>            # 全量探测
    python -m maas.crawl --cdp <url> --only OutTruck   # 只打匹配的

产物：export/maas_probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

from maas.recon import OUT

MAP = OUT.parent / "maas_api_map.json"
PROBE = OUT.parent / "maas_probe.json"
SITE = "https://ghzh.tmaas.com.cn"
# 前端 axios 的 baseURL，不是站点根目录——直接打 /api/... 只会拿到 SPA 的 index.html。
#   axios.create({baseURL:"https://ghzh.tmaas.com.cn/ghzh"})
# 另有一条 /ghzhcustoms，是带 ?token= 从海关侧嵌进来时用的（走 Authorization: Bearer 而非 cookie）。
BASE = f"{SITE}/ghzh"

# 一律不碰的写操作。宁可漏测也不能在生产系统上落数据。
MUTATING = re.compile(r"(Add|Del|Delete|Set|Modify|Cancle|Cancel|Approv|Archive|Reissue|Save|Push|Update|Insert|Bind)", re.I)

# 占位符名字是压缩后的 t/e/i/a，看不出语义，所以不按名字对，改成"挨个试"。
# 值都是从 GetRecords / OutTruckActivity 的真实响应里摘的，不是编的。
POOL = [
    "沪FB1502",          # 车牌（外集卡）
    "2026-08-04",        # 日期，前端 formatDate(…,"YYYY-MM-DD")
    "BEAU2324412",       # 箱号
    "510030542764211",   # cntr_pkid，节点/详情类接口用这个
    "WGQ4",              # 码头代码，见 /api/edi/RefInfo/TERMINAL
    "Y",                 # 各种标志位
]
# 双参接口只试这几组有意义的搭配，别做笛卡尔积去锤人家生产库
PAIRS = [
    ("沪FB1502", "2026-08-04"),
    ("BEAU2324412", "1"),
    ("BEAU2324412", "WGQ4"),
    ("510030542764211", "WGQ4"),
    ("BEAU2324412", "Y"),
]


def has_data(body: str) -> bool:
    """响应里 data 非空才算问到了东西。"""
    try:
        d = json.loads(body).get("data")
    except Exception:
        return False
    return bool(d)


def fill(path: str, args: dict[str, str], blank: bool = False) -> str | None:
    """把 {x} 占位符换成样本值；换不动就返回 None（这条跳过）。

    blank=True 时缺省填空串——空参数要么让接口吐全量，要么让它回一句
    「车号不能为空」这类中文校验信息，两种结果对写文档都有用。
    """
    holes = re.findall(r"\{([^}]*)\}", path)
    if not holes:
        return path
    out = path
    for h in holes:
        v = args.get(h, "" if blank else None)
        if v is None:
            return None
        out = out.replace("{" + h + "}", v, 1)
    return out


async def raw_probe(pg, url: str) -> dict:
    return await pg.evaluate(
        """async u => {
            try {
              const r = await fetch(u, {credentials: 'include'});
              const t = await r.text();
              return {status: r.status, ct: r.headers.get('content-type') || '', body: t.slice(0, 20000)};
            } catch (e) { return {status: -1, ct: '', body: String(e)}; }
        }""", url)


def throttled(r: dict) -> bool:
    """瑞数封锁的样子：400/412 + text/html + body 只有几个换行（业务 400 一定带 msg）。"""
    return r["status"] in (400, 412) and len((r.get("body") or "").strip()) == 0


class Blocked(Exception):
    """连续被封，再打下去只是白费——直接收工，下次冷却完接着跑。"""


async def probe(pg, url: str, state: dict) -> dict:
    """在页面里 fetch。被封就长退避重试；连续三轮还封就抛 Blocked。

    实测：连续快打约 20 次后，瑞数会把整个会话/IP 封掉，连 SPA 的 js chunk 都返 400
    （页面直接白屏）。换 cookie、重载、换代理 IP 都救不回来，只能等冷却，
    所以这里退避是分钟级的，不是秒级。
    # ponytail: 退避写死 5/10/15 分钟，真要长期跑再按响应做自适应
    """
    r = await raw_probe(pg, url)
    if not throttled(r):
        state["blocks"] = 0
        return r
    for wait_min in (5, 10, 15):
        print(f"  …被瑞数封了，等 {wait_min} 分钟再试", flush=True)
        await asyncio.sleep(wait_min * 60)
        await pg.goto(SITE + "/OutTruck", wait_until="domcontentloaded", timeout=90000)
        await pg.wait_for_timeout(15000)
        r = await raw_probe(pg, url)
        if not throttled(r):
            state["blocks"] = 0
            return r
    raise Blocked(url)


def better(new: dict, old: dict | None) -> bool:
    """新结果比旧的强才覆盖：有数据 > 200 > 其它。别让一次限流把之前问到的真响应冲掉。"""
    if old is None:
        return True
    rank = lambda x: (2 if has_data(x.get("body") or "") else 0) + (1 if x.get("status") == 200 else 0)
    return rank(new) >= rank(old)


async def main_async(cdp: str, only: str | None, args: dict[str, str], blank: bool, auto: bool) -> None:
    data = json.loads(MAP.read_text(encoding="utf-8"))
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp)
        ctx = br.contexts[0]
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if "ghzh.tmaas.com.cn" not in pg.url:
            await pg.goto(f"{SITE}/OutTruck", wait_until="domcontentloaded", timeout=90000)
            await pg.wait_for_timeout(5000)
        if "/login" in pg.url or "passport" in pg.url:
            raise SystemExit("浏览器不是登录态，先跑 python -m maas.login")

        # 接着上次的结果跑，被限流冲掉的那些会被更好的结果补回来
        state = {"blocks": 0}
        results = json.loads(PROBE.read_text(encoding="utf-8")) if PROBE.exists() else {}
        # 菜单先打，它决定这个账号到底能看哪些模块
        if not results.get("GET /api/Menu", {}).get("body"):
            results["GET /api/Menu"] = await probe(pg, f"{BASE}/api/Menu", state)

        try:
            await sweep(pg, data, results, only, args, blank, auto, state)
        except Blocked as e:
            print(f"\n被瑞数封住了（{e}），先存盘。冷却后重跑会自动接着补。", flush=True)

        PROBE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        hit = sum(1 for v in results.values() if v.get("status") == 200)
        print(f"\n{len(results)} 条，{hit} 条 200 -> {PROBE}")


async def sweep(pg, data, results, only, args, blank, auto, state) -> None:
    for key, info in sorted(data["all"].items()):
        method, path = key.split(" ", 1)
        if has_data(results.get(key, {}).get("body") or ""):
            continue  # 上次已经问到真数据了，别再打一遍
        if only and only.lower() not in path.lower():
            continue
        if method != "GET":
            results[key] = {"status": None, "skip": "非 GET，未探测"}
            continue
        if MUTATING.search(path):
            results[key] = {"status": None, "skip": "疑似写操作，主动跳过"}
            continue
        if not path.startswith("/api/"):
            results[key] = {"status": None, "skip": "不是接口路径"}
            continue
        holes = re.findall(r"\{([^}]*)\}", path)
        # 候选取值：显式 --arg 优先，其次按洞的个数挨个试样本，最后兜底填空串
        tries: list[dict[str, str]] = []
        if args:
            tries.append(args)
        if auto and len(holes) == 1:
            tries += [{holes[0]: v} for v in POOL]
        elif auto and len(holes) == 2:
            tries += [dict(zip(holes, pair)) for pair in PAIRS]
        if blank or not tries:
            tries.append({})

        r = None
        for cand in tries:
            url = fill(path, cand, True)
            if url is None:
                continue
            r = await probe(pg, BASE + url, state)
            r["probed"] = url
            await pg.wait_for_timeout(4000)  # 口岸系统 + 瑞数频控，实测快过 ~20 次就整个会话被封
            if has_data(r.get("body") or ""):
                break
        if r is None:
            results[key] = {"status": None, "skip": f"填不出参数 {holes}"}
            continue
        if better(r, results.get(key)):
            results[key] = r
        body = (r.get("body") or "").replace("\n", " ")[:110]
        print(f"  [{r['status']}] {r['probed'][:80]} -> {body}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", required=True)
    ap.add_argument("--only", help="只探测路径里含这个词的接口")
    ap.add_argument("--auto", action="store_true", help="带参接口挨个试真实样本值，直到 data 非空")
    ap.add_argument("--blank", action="store_true", help="没给样本的占位符一律填空串（看接口的必填校验提示）")
    ap.add_argument("--arg", action="append", default=[], metavar="名=值",
                    help="给路径占位符喂样本值，可重复：--arg i=浙B12345 --arg a=20260804")
    a = ap.parse_args()
    args = dict(kv.split("=", 1) for kv in a.arg)
    asyncio.run(main_async(a.cdp, a.only, args, a.blank, a.auto))


if __name__ == "__main__":
    main()
