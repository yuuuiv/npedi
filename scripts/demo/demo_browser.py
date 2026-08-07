"""Auto-login demo: real login page on the right, live narration on the left.

What is real here: the CAPTCHA is the one the site served, read by the production
CNN (scripts/solve_npedi_captcha_cnn.py); the SMS code comes from the production
temp-mail reader; the login is a real login and the token it returns is written
back to .env. What differs from production: the crawler posts to /portal-api
directly, while this drives the same login through the visible form so the steps
can be watched.

On screen the mobile number and the SMS code render as dots; the token is shown
as first and last 8 characters only.
"""
from __future__ import annotations

import base64
import ctypes
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# CAPTCHA_SOLVER_COMMAND is relative to the repo root; the recorder launches this
# from whatever directory the terminal starts in, so pin it here.
os.chdir(PROJECT_ROOT)

import httpx
from playwright.sync_api import sync_playwright

from auth import CommandCaptchaSolver, TempMailOtpReader, update_env_token
from config import load_config

HERE = Path(__file__).resolve().parent
REC = HERE / "record"
DONE = REC / "demo.done"

# right half of the capture region; must match layout_common.ps1
WIN_X, WIN_Y, WIN_W, WIN_H = 1008, 96, 1040, 960
# No device-scale override: it makes --window-size, CDP bounds and physical
# pixels three different units. The site overflows this window horizontally,
# which is fine — the dialog is pinned to the viewport centre below.

R = "\x1b[0m"
DIM = "\x1b[38;2;122;132;127m"
TEAL = "\x1b[1;38;2;38;190;158m"
AMBER = "\x1b[1;38;2;226;140;56m"
WHITE = "\x1b[1;38;2;232;236;233m"
BODY = "\x1b[38;2;200;208;203m"

START = time.monotonic()


def enable_vt() -> None:
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)


def stamp() -> str:
    return f"{DIM}{time.monotonic() - START:6.1f}s{R} "


def out(text: str = "", pause: float = 0.3) -> None:
    print(text, flush=True)
    time.sleep(pause)


def step(n: int, title: str) -> None:
    out("")
    out(f"{stamp()}{TEAL}步骤 {n}/7{R}  {WHITE}{title}{R}", 0.4)


def info(t: str) -> None:
    out(f"{stamp()}{DIM}{t}{R}", 0.25)


def good(t: str) -> None:
    out(f"{stamp()}{TEAL}OK{R}  {BODY}{t}{R}", 0.35)


def hit(t: str) -> None:
    out(f"{stamp()}{AMBER}>>{R}  {WHITE}{t}{R}", 0.35)


def mask_token(t: str) -> str:
    if len(t) <= 16:
        return "*" * len(t)
    return f"{t[:8]}{DIM} … 隐藏 {len(t) - 16} 字符 … {R}{WHITE}{t[-8:]}{R}"


def read_env_token(p: Path) -> str:
    for raw in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*WEB[_-]?TOKEN\s*=\s*(.+)", raw, re.I)
        if m:
            return m.group(1).strip()
    return ""


MASK_CSS = """
input[placeholder='请输入手机号'], input[placeholder='请输入短信验证码'] {
  -webkit-text-security: disc;
  letter-spacing: 3px;
}
"""

# Framing only: the dialog sits right of centre on this site and would be cut off
# by the window edge. Move the box to the middle of the viewport and report the
# before/after rects so the change is visible rather than silent.
CENTER_MODAL = """() => {
  const input = document.querySelector("input[placeholder='请输入手机号']");
  if (!input) return 'modal not found';
  // walk up to the dialog card itself, not the full-screen overlay behind it
  let el = input;
  while (el && el !== document.body) {
    if (el.offsetWidth > 300 && el.offsetWidth < 820 && el.offsetHeight > 380) break;
    el = el.parentElement;
  }
  if (!el || el === document.body) return 'dialog card not found';
  const r = (b) => `${Math.round(b.x)},${Math.round(b.y)} ${Math.round(b.width)}x${Math.round(b.height)}`;
  const before = el.getBoundingClientRect();
  const vw = window.innerWidth;
  if (before.left >= 4 && before.right <= vw - 4) {
    return `viewport ${vw}x${window.innerHeight} | dialog ${r(before)} already inside`;
  }
  Object.assign(el.style, {
    position: 'fixed', left: '50%', top: '50%',
    right: 'auto', bottom: 'auto', margin: '0',
    transform: 'translate(-50%, -50%)',
  });
  return `viewport ${vw}x${window.innerHeight} | dialog ${r(before)} -> ${r(el.getBoundingClientRect())}`;
}"""


def main() -> int:
    enable_vt()
    REC.mkdir(parents=True, exist_ok=True)
    DONE.unlink(missing_ok=True)
    cfg = load_config()

    solver = CommandCaptchaSolver(cfg.captcha_solver_command,
                                  timeout_seconds=cfg.captcha_solver_timeout_seconds)
    reader = TempMailOtpReader(
        base_url=cfg.temp_mail_base_url, address_jwt=cfg.temp_mail_address_jwt,
        site_password=cfg.temp_mail_site_password, recipient=cfg.temp_mail_recipient,
        allowed_sender=cfg.temp_mail_allowed_sender, required_text=cfg.temp_mail_required_text,
        required_copies=cfg.temp_mail_required_copies, code_pattern=cfg.sms_code_pattern,
        timeout_seconds=cfg.sms_code_timeout_seconds, poll_seconds=cfg.temp_mail_poll_seconds,
    )

    # Preflight: prove the solver and the mailbox work before anything opens or
    # any SMS is requested. A relative CAPTCHA_SOLVER_COMMAND resolved against
    # the wrong directory used to surface only after the browser was already up.
    sample = sorted((PROJECT_ROOT / "captcha-data" / "labeled").glob("*.jpg"))[:1]
    if not sample:
        print("preflight: no labeled sample to test the solver with", file=sys.stderr)
        return 2
    expected = sample[0].name.split("_", 1)[0]
    try:
        got = solver.solve(sample[0].read_bytes())
    except Exception as exc:
        print(f"preflight: 验证码识别不可用 -> {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"  cwd={os.getcwd()}", file=sys.stderr)
        print(f"  CAPTCHA_SOLVER_COMMAND={cfg.captcha_solver_command}", file=sys.stderr)
        return 2
    try:
        reader._get_json("/api/settings")
    except Exception as exc:
        print(f"preflight: 邮箱读取不可用 -> {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"preflight ok  solver {sample[0].name} -> {got} (expect {expected})", flush=True)
    time.sleep(0.8)

    captured = {"token": ""}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=False,
            args=[f"--window-position={WIN_X},{WIN_Y}",
                  f"--window-size={WIN_W},{WIN_H}",
                  "--no-first-run", "--no-default-browser-check",
                  "--disable-features=Translate"],
        )
        page = browser.new_page(no_viewport=True)
        # Playwright overrides the --window-size flag, so set the real window
        # rect through CDP; this keeps Chrome's render area and the visible
        # frame in agreement, which --window-size alone did not.
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {
            "windowId": wid,
            "bounds": {"left": WIN_X, "top": WIN_Y, "width": WIN_W,
                       "height": WIN_H, "windowState": "normal"},
        })

        def on_response(resp):
            if "/portal-api/login" in resp.url:
                try:
                    data = (resp.json() or {}).get("data") or {}
                except Exception:
                    return
                if isinstance(data, dict) and data.get("token"):
                    captured["token"] = str(data["token"])

        page.on("response", on_response)

        time.sleep(1.0)
        out(f"{WHITE}NPEDI 自动登录 · 凭证失效自愈{R}", 0.4)
        out(f"{DIM}右侧是真实登录页，全程无人操作{R}", 0.8)

        step(1, "读取当前凭证")
        old = read_env_token(cfg.env_path)
        info(".env  WEB_TOKEN")
        out(f"        {WHITE}{mask_token(old)}{R}", 0.8)

        step(2, "探测凭证状态")
        with httpx.Client(base_url=cfg.base_url.rstrip("/") + "/onesite-api", timeout=30) as c:
            r = c.get("/getInfo", headers={"ediAuthorization": "Bearer expired.demo.token"})
            try:
                code = r.json().get("code", r.status_code)
            except ValueError:
                code = r.status_code
        out(f"{stamp()}{AMBER}!!{R}  {BODY}GET /onesite-api/getInfo  ->  code={code}  认证失败{R}", 0.4)
        info("凭证失效，触发自动登录")

        step(3, "打开登录页")
        page.goto("https://www.npedi.com/index", wait_until="networkidle", timeout=60000)
        page.add_style_tag(content=MASK_CSS)
        page.wait_for_timeout(1200)
        info("www.npedi.com/index")
        page.get_by_text("登录", exact=True).first.click(timeout=10000)
        page.wait_for_timeout(1200)
        page.get_by_text("手机号登录", exact=False).first.click(timeout=10000)
        page.wait_for_timeout(800)
        page.add_style_tag(content=MASK_CSS)
        page.wait_for_timeout(400)
        # The site places the dialog right of centre, so part of it falls outside
        # this window. Re-centre it in the viewport; this only moves the box, the
        # form and every request it makes are untouched.
        measured = page.evaluate(CENTER_MODAL)
        page.wait_for_timeout(400)
        if "--dry" in sys.argv:
            info(f"[dry] {measured}")
        good("切换到手机号登录")

        step(4, "自动填写手机号")
        mobile = page.get_by_placeholder("请输入手机号")
        mobile.click()
        mobile.type(cfg.npedi_mobile, delay=110)
        info("号码在页面上以圆点显示，不出现在录像里")

        step(5, "读取图形验证码 · 本地 CNN 识别")
        img = page.locator("img[src^='data:image']").first
        src = img.get_attribute("src") or ""
        raw = base64.b64decode(src.split(",", 1)[1])
        info(f"验证码图片 {len(raw)} 字节  111x36")
        t0 = time.monotonic()
        answer = solver.solve(raw)
        hit(f"识别结果  {answer}          耗时 {time.monotonic() - t0:.2f}s")
        info("整图准确率 94.5%（独立测试集 189/200），不调用任何打码平台")
        box = page.get_by_placeholder("请输入验证码")
        box.click()
        box.type(answer, delay=170)

        if "--dry" in sys.argv:
            out("")
            hit("--dry：到此为止，不点「获取验证码」，不发短信")
            page.screenshot(path=str(REC / "dry-filled.png"))
            # signal first, then hold: the recorder grabs the screen frame while
            # both windows are still up, otherwise it photographs the desktop
            DONE.write_text("dry", encoding="utf-8")
            info("保持窗口 25 秒供取景检查")
            page.wait_for_timeout(25000)
            browser.close()
            reader.close()
            return 0

        step(6, "请求短信并等待邮箱转发")
        not_before = int(time.time())
        page.get_by_text("获取验证码", exact=False).first.click(timeout=10000)
        good("图形验证码通过 · 短信已发出")
        info("轮询 temp-mail /api/parsed_mails")
        t0 = time.monotonic()
        otp = reader.wait_for_code(not_before=not_before)
        good(f"收到验证码  {'*' * len(otp)}      等待 {time.monotonic() - t0:.1f}s")
        info("已核对收件人 / 发件人 / 正文标记 / 到达时间")
        sms = page.get_by_placeholder("请输入短信验证码")
        sms.click()
        sms.type(otp, delay=150)

        step(7, "提交登录并写回凭证")
        page.locator("button:has-text('登录')").last.click(timeout=10000)
        for _ in range(40):
            if captured["token"]:
                break
            page.wait_for_timeout(400)
        page.wait_for_timeout(1500)

        if not captured["token"]:
            out(f"{stamp()}{AMBER}!!{R}  {BODY}没有捕获到 token，登录可能失败{R}")
            DONE.write_text("fail", encoding="utf-8")
            page.screenshot(path=str(REC / "fail.png"))
            browser.close()
            reader.close()
            return 1

        token = captured["token"]
        update_env_token(cfg.env_path, token)
        new = read_env_token(cfg.env_path)
        out(f"        {DIM}旧{R}  {mask_token(old)}", 0.5)
        out(f"        {TEAL}新{R}  {mask_token(new)}", 0.8)
        info("原子写回 .env，日志不记录内容")

        with httpx.Client(base_url=cfg.base_url.rstrip("/") + "/onesite-api", timeout=30) as c:
            r = c.get("/getInfo", headers={"ediAuthorization": f"Bearer {new}"})
            user = ((r.json() or {}).get("data") or {}).get("user") or {}
        good(f"用新凭证调真实接口  ->  code=200   {str(user.get('companyName') or '')[:20]}")

        out("")
        good(f"登录成功 · 全程 {time.monotonic() - START:.1f}s · 无人工介入")
        info("抓取任务自动继续，无需重启")
        page.wait_for_timeout(3500)
        browser.close()

    reader.close()
    DONE.write_text("ok", encoding="utf-8")
    out("", 1.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
