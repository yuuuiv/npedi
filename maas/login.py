"""开一个带 profile 的云浏览器，把 live 链接给人，人过一次验证码，cookie 就长在 profile 里了。

登录页挂了顶象滑块，AI agent 拖不准（试了十几次 $1.4 没过），
所以走"人工过一次 + profile 复用"这条路：profile 会持久化 cookie/localStorage，
之后 recon/crawl 都带上同一个 profileId，直接是登录态。

    python -m maas.login                    # 新建 profile，打印 live 链接，等你登进去
    python -m maas.login --reuse            # 用 .env 里已有的 MAAS_PROFILE_ID 开一个新浏览器

登进去之后脚本会把 profileId / cdpUrl 打出来，喂给 maas.crawl。
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from maas.recon import API, env

HOME = "https://ghzh.tmaas.com.cn/OutTruck"


def api(key: str) -> tuple[str, dict[str, str]]:
    return API, {"X-Browser-Use-API-Key": key, "Content-Type": "application/json"}


def save_env(name: str, value: str) -> None:
    """把 profileId 写回 .env，下次 --reuse 直接用。"""
    p = Path(".env")
    lines = p.read_text(encoding="utf-8-sig").splitlines() if p.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def wait_logged_in(cdp: str, minutes: int) -> bool:
    """盯着页面，跳回 ghzh 自己的域名就算登录成功。"""
    deadline = time.time() + minutes * 60
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp)
        ctx = br.contexts[0] if br.contexts else await br.new_context()
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if "tmaas.com.cn" not in pg.url:
            await pg.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        # 首屏必然先 412 再重载，然后才 302 去 passport。等它跳完再开始判断，
        # 否则会把"刚 goto 完还没跳走的 ghzh 地址"误判成已登录。
        await asyncio.sleep(12)
        last = ""
        while time.time() < deadline:
            if pg.url != last:
                last = pg.url
                print(f"  当前页面 {last[:100]}")
            if "ghzh.tmaas.com.cn" in pg.url and "/login" not in pg.url:
                await pg.wait_for_timeout(3000)
                if "ghzh.tmaas.com.cn" in pg.url:
                    return True
            await asyncio.sleep(3)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true", help="用 .env 里的 MAAS_PROFILE_ID")
    ap.add_argument("--cdp", help="别开新浏览器，就在这个已有的浏览器上等人工登录")
    ap.add_argument("--timeout", type=int, default=240, help="浏览器存活分钟数，上限 240")
    ap.add_argument("--wait", type=int, default=20, help="等人工登录的分钟数")
    args = ap.parse_args()

    base, h = api(env("BROWSER_USE_API_KEY"))
    if args.cdp:
        print(f"等你在 live 页面里登录……（最多 {args.wait} 分钟）")
        ok = asyncio.run(wait_logged_in(args.cdp, args.wait))
        print("\n登录成功，cookie 已存进 profile" if ok else "\n没等到登录")
        print(f"接着跑： python -m maas.crawl --cdp {args.cdp}")
        return

    pid = env("MAAS_PROFILE_ID") if args.reuse else ""
    if not pid:
        r = httpx.post(f"{base}/profiles", headers=h, json={"name": "tmaas-maas"}, timeout=60)
        r.raise_for_status()
        pid = r.json()["id"]
        save_env("MAAS_PROFILE_ID", pid)
        print(f"新建 profile {pid}（已写入 .env）")

    r = httpx.post(f"{base}/browsers", headers=h,
                   json={"profileId": pid, "proxyCountryCode": None, "timeout": args.timeout},
                   timeout=120)
    r.raise_for_status()
    b = r.json()
    print(f"\nprofile  {pid}\nbrowser  {b['id']}\ncdp      {b['cdpUrl']}\n")
    print("请在浏览器里打开下面这个链接，手动登录（拖滑块）：\n")
    print(f"    {b['liveUrl']}\n")
    print(f"账号 {env('MAAS_USER')}  密码 {env('MAAS_PASS')}")
    print(f"\n等你登录中……（最多 {args.wait} 分钟）")

    ok = asyncio.run(wait_logged_in(b["cdpUrl"], args.wait))
    print("\n登录成功，cookie 已存进 profile" if ok else "\n没等到登录")
    print(f"接着跑： python -m maas.crawl --cdp {b['cdpUrl']}")


if __name__ == "__main__":
    main()
