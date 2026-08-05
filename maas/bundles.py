"""从前端 JS bundle 里扒出全量接口路径。

点菜单只能覆盖点到的接口，Vue 打包后的 bundle 里却躺着**所有**接口路径字符串——
要"全量"就得两头凑：bundles.py 给出完整清单，recon.py 给出真实请求/响应。

站点有瑞数防护，curl 拿不到 js，所以照样开一个云浏览器，
在首屏 OAuth 跳转之前把 bundle 的响应体接住。

    python -m maas.bundles              # 自己开浏览器抓
    python -m maas.bundles --cdp <url>  # 复用已有浏览器

产物：export/maas_js/*.js 与 export/maas_endpoints.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from maas.recon import API, OUT, env

JS_DIR = OUT.parent / "maas_js"
ENDPOINTS = OUT.parent / "maas_endpoints.json"
SITES = ["https://ghzh.tmaas.com.cn/OutTruck", "https://passport.tmaas.com.cn/login"]

# bundle 里接口路径就是普通字符串字面量，形如 "/api/xxx/yyy" / "/ght/xxx"。
# 只认以 / 开头、含至少一层路径、不带空格且不像静态资源的，剩下的噪音在下面过滤。
PATH_RE = re.compile(r"""["'`](/[a-zA-Z][\w\-./{}$:]{3,120})["'`]""")
NOISE = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|html|json)$|^/(static|assets|fonts|icons|img|images)/")

# webpack 运行时里的 chunk 清单：u(e){return c.p+"js/"+({1:"chunk-common"}[e]||e)+"."+{3:"e8c26b18",...}[e]+".js"}
# 路由是懒加载的，接口路径全在这些 chunk 里，首屏只会下到入口那几个。
CHUNK_NAMES = re.compile(r'\+"js/"\+\(\{([^}]*)\}\[\w\]\|\|\w\)\+"\."\+\{([^}]*)\}')


def chunk_urls(app_js: Path, base: str) -> list[str]:
    m = CHUNK_NAMES.search(app_js.read_text("utf-8", "replace"))
    if not m:
        return []
    names = dict(re.findall(r'(\d+):"([^"]+)"', m.group(1)))
    return [f'{base}/js/{names.get(cid, cid)}.{h}.js' for cid, h in re.findall(r'(\d+):"([^"]+)"', m.group(2))]


async def grab(cdp: str | None, key: str) -> list[Path]:
    """把两个站点首屏加载的 js 全部落盘，返回文件列表。"""
    JS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    h = {"X-Browser-Use-API-Key": key, "Content-Type": "application/json"}
    own = None
    if not cdp:
        r = httpx.post(f"{API}/browsers", headers=h, json={"proxyCountryCode": None}, timeout=120)
        r.raise_for_status()
        own = r.json()
        cdp = own["cdpUrl"]
        print(f"browser={own['id']}")

    async def on_response(resp) -> None:
        url = resp.request.url
        if not url.endswith(".js") or "tmaas.com.cn" not in url:
            return
        name = f"{url.split('//')[1].split('/')[0]}__{url.rsplit('/', 1)[-1]}"
        p = JS_DIR / name
        if p.exists():
            return
        try:
            p.write_bytes(await resp.body())
        except Exception as e:
            print(f"  skip {name}: {e}")
            return
        saved.append(p)
        print(f"  saved {name} ({p.stat().st_size // 1024}KB)")

    try:
        async with async_playwright() as p:
            br = await p.chromium.connect_over_cdp(cdp)
            ctx = br.contexts[0] if br.contexts else await br.new_context()
            pg = await ctx.new_page()
            pg.on("response", lambda r: asyncio.create_task(on_response(r)))
            for site in SITES:
                print(f"-> {site}")
                try:
                    await pg.goto(site, wait_until="networkidle", timeout=90000)
                except Exception as e:
                    print(f"   (goto: {e})")
                await pg.wait_for_timeout(8000)  # 瑞数首次 412 后会自己重载，等它跑完

            # 懒加载 chunk 得自己拉。先落到 ghzh 自己的静态文件上（不会被 OAuth 弹走），
            # 变成同源之后用 fetch 逐个取，绕开 CORS。
            app = JS_DIR / "ghzh.tmaas.com.cn__app.b6c15ddf.js"
            if app.exists():
                base = "https://ghzh.tmaas.com.cn"
                urls = chunk_urls(app, base) + [f"{base}/js/vendor.f47dcc96.js"]
                print(f"-> {len(urls)} 个 chunk")
                await pg.goto(f"{base}/js/app.b6c15ddf.js", wait_until="domcontentloaded", timeout=60000)
                for u in urls:
                    p = JS_DIR / f"ghzh.tmaas.com.cn__{u.rsplit('/', 1)[-1]}"
                    if p.exists():
                        continue
                    text = await pg.evaluate(
                        "u => fetch(u).then(r => r.ok ? r.text() : '')", u)
                    if text:
                        p.write_text(text, encoding="utf-8")
                        saved.append(p)
                        print(f"  saved {p.name} ({len(text) // 1024}KB)")
                    else:
                        print(f"  MISS {u}")
            await pg.close()
    finally:
        if own:
            httpx.patch(f"{API}/browsers/{own['id']}", headers=h, json={"action": "stop"}, timeout=60)
    return saved


def extract(files: list[Path]) -> dict[str, list[str]]:
    """每个 bundle 里的候选接口路径，去重排序。"""
    out: dict[str, list[str]] = {}
    for f in files:
        text = f.read_text("utf-8", "replace")
        hits = {m.group(1) for m in PATH_RE.finditer(text) if not NOISE.search(m.group(1))}
        if hits:
            out[f.name] = sorted(hits)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", help="复用已有浏览器的 cdpUrl")
    ap.add_argument("--offline", action="store_true", help="不抓，只解析 export/maas_js 下已有的 js")
    args = ap.parse_args()

    files = sorted(JS_DIR.glob("*.js")) if args.offline else asyncio.run(grab(args.cdp, env("BROWSER_USE_API_KEY")))
    data = extract(files)
    ENDPOINTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for v in data.values())
    print(f"\n{len(data)} 个 bundle，共 {total} 条候选路径 -> {ENDPOINTS}")


if __name__ == "__main__":
    main()
