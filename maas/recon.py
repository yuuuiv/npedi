"""MaaS (ghzh.tmaas.com.cn) API 勘察：Browser Use 云浏览器开路，CDP 旁路抓全量接口。

站点是瑞数（Botgate）动态防护 + OAuth2 单点登录，httpx 直连一律 412，
所以只能真浏览器进站。Browser Use 的 agent 负责登录/点菜单（顶象滑块它自己过），
我们用 Playwright 连同一个浏览器的 CDP，把每一条 XHR/fetch 原样落盘。

    python -m maas.recon                # 起 agent + 抓包，跑到 agent 结束
    python -m maas.recon --session <id> # 复用已有 session（agent 还活着时接管抓包）

产物：export/maas_capture.jsonl（含 token，已被 .gitignore 挡住）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Response, async_playwright

API = "https://api.browser-use.com/api/v4"
OUT = Path(os.getenv("EXPORT_DIR", "./export")) / "maas_capture.jsonl"

# 静态资源不是接口，扔掉；埋点域名也扔
SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map", ".mp4")
SKIP_HOST = ("rum.tmaas.com.cn", "at.alicdn.com", "cdn.gdtspace.com", "hm.baidu.com", "browser-use.com")
MAX_BODY = 200_000  # 单条响应最多留 200KB，列表接口能翻很多页，别把盘写爆


def env(name: str, default: str = "") -> str:
    """读 .env（顺带兼容项目里 `Key = value` 的写法），环境变量优先。"""
    if os.getenv(name):
        return os.environ[name]
    p = Path(".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().replace("-", "_").upper() == name:
                return v.strip().strip("'\"")
    return default


class BU:
    """Browser Use Cloud v4 —— 官方 SDK 的 model 枚举落后于服务端，直接打 REST。"""

    def __init__(self, key: str) -> None:
        self.h = {"X-Browser-Use-API-Key": key, "Content-Type": "application/json"}
        self.c = httpx.Client(timeout=120)

    def _j(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            raise RuntimeError(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:400]}")
        return r.json()

    def start(self, task: str, model: str = "claude-sonnet-5") -> dict[str, Any]:
        body = {"task": task, "model": model, "browserSettings": {"proxyCountryCode": None}}
        return self._j(self.c.post(f"{API}/runs", headers=self.h, json=body))

    def cdp_url(self, session_id: str, tries: int = 30) -> str:
        """agent 的浏览器是异步起的，轮询到 cdpUrl 出现为止。"""
        for _ in range(tries):
            for b in self._j(self.c.get(f"{API}/browsers", headers=self.h)).get("items", []):
                if b.get("agentSessionId") == session_id and b.get("cdpUrl"):
                    return b["cdpUrl"]
            time.sleep(2)
        raise RuntimeError(f"session {session_id} 没等到 cdpUrl")

    def status(self, run_id: str) -> str:
        return self._j(self.c.get(f"{API}/runs/{run_id}/status", headers=self.h)).get("status", "?")

    def result(self, run_id: str) -> dict[str, Any]:
        return self._j(self.c.get(f"{API}/runs/{run_id}", headers=self.h))

    def say(self, session_id: str, text: str, interrupt: bool = False) -> Any:
        return self._j(self.c.post(f"{API}/sessions/{session_id}/queue", headers=self.h,
                                   json={"text": text, "interrupt": interrupt}))

    def stop(self, session_id: str) -> Any:
        return self._j(self.c.post(f"{API}/sessions/{session_id}/purge", headers=self.h, json={}))


def interesting(url: str) -> bool:
    path = url.split("?", 1)[0]
    return not (path.endswith(SKIP_EXT) or any(h in url for h in SKIP_HOST) or url.startswith(("data:", "blob:")))


class Capture:
    """把每条响应写成一行 JSON。按 (method, path) 去重计数，同一接口只留前 3 个样本。"""

    def __init__(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.fh = out.open("a", encoding="utf-8")
        self.seen: dict[str, int] = {}

    async def on_response(self, resp: Response) -> None:
        req = resp.request
        if not interesting(req.url):
            return
        key = f"{req.method} {req.url.split('?', 1)[0]}"
        n = self.seen.get(key, 0)
        self.seen[key] = n + 1
        if n >= 3:
            return
        body = None
        try:
            raw = await resp.body()
            if len(raw) <= MAX_BODY:
                body = raw.decode("utf-8", "replace")
        except Exception as e:  # 重定向/已释放的响应体拿不到，记下来就行
            body = f"<no body: {e}>"
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": req.method,
            "url": req.url,
            "resource_type": req.resource_type,
            "status": resp.status,
            "req_headers": dict(req.headers),
            "post_data": req.post_data,
            "resp_headers": dict(resp.headers),
            "body": body,
        }
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        print(f"  [{resp.status}] {req.method} {req.url[:120]}", flush=True)


async def pump(cdp: str, cap: Capture, stop: asyncio.Event) -> None:
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp)
        seen_pages = set()

        def hook(page: Any) -> None:
            if page in seen_pages:
                return
            seen_pages.add(page)
            page.on("response", lambda r: asyncio.create_task(cap.on_response(r)))

        for ctx in br.contexts:
            ctx.on("page", hook)
            for pg in ctx.pages:
                hook(pg)
        br.on("disconnected", lambda _: stop.set())
        await stop.wait()


LOGIN = """打开 https://ghzh.tmaas.com.cn/OutTruck 。会跳到 passport.tmaas.com.cn 登录页。
用「密码登录」，用户名 {user}，密码 {pwd}。如果出现顶象滑块验证码就把它拖完。
登录成功后回到 MaaS 站点首页即可，然后停下等待后续指令，不要自己乱点。
如果页面 412 或白屏，刷新一次再试。"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="复用已有 agent session，只接管抓包")
    ap.add_argument("--run", help="配合 --session：盯着这个 run 的状态，跑完就收工")
    ap.add_argument("--cdp", help="直接给 cdpUrl，跳过 Browser Use 调度")
    ap.add_argument("--model", default="claude-sonnet-5")
    args = ap.parse_args()

    bu = BU(env("BROWSER_USE_API_KEY"))
    run_id = args.run
    if args.cdp:
        cdp, session_id = args.cdp, args.session
    elif args.session:
        session_id = args.session
        cdp = bu.cdp_url(session_id)
    else:
        task = LOGIN.format(user=env("MAAS_USER"), pwd=env("MAAS_PASS"))
        run = bu.start(task, args.model)
        run_id, session_id = run["id"], run["sessionId"]
        print(f"run={run_id} session={session_id}")
        cdp = bu.cdp_url(session_id)
    print(f"cdp={cdp}\nout={OUT}")

    cap = Capture(OUT)
    stop = asyncio.Event()
    pumping = asyncio.create_task(pump(cdp, cap, stop))
    try:
        # 终态列表来自 openapi RunStatusResponse，非终态一律继续等
        while run_id and bu.status(run_id) not in ("completed", "failed", "cancelled"):
            await asyncio.sleep(10)
        if run_id:
            r = bu.result(run_id)
            print(f"\n== run {r['status']} cost=${r.get('totalCostUsd')}\n{r.get('result') or r.get('error')}")
        else:
            await stop.wait()
    finally:
        stop.set()
        await asyncio.wait_for(pumping, timeout=10)
        print(f"\n抓到 {sum(cap.seen.values())} 条，{len(cap.seen)} 个不同接口")


if __name__ == "__main__":
    asyncio.run(main())
