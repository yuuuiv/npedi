"""顶象（dingxiang）滑块验证码：算缺口位置 + 拖过去。

passport.tmaas.com.cn 的登录页挂了顶象 basic 拼图码，AI agent 拖不准（试了十几次、烧了一块多）。
缺口检测其实很土：把背景图那条 50px 高的带子按列取灰度，缺口边缘是一条竖直的强梯度线，
找梯度最大的那一列就是了。轨迹用先快后慢 + 轻微抖动，纯匀速会被判定成机器。

    from maas.captcha import solve
    ok = await solve(page)   # 返回 True 表示滑块过了

# ponytail: 只处理 basic 拼图这一种；顶象还有点选/推理式，遇上了再说（届时走 live_url 人工过一次 + profile 存 cookie）
"""
from __future__ import annotations

import random

import numpy as np
from PIL import Image
from playwright.async_api import Page

SLIDER = "#dx_captcha_basic_sub-slider_1"
PIC = "#dx_captcha_basic_pic_1"
SUCCESS = "#dx_captcha_basic_state-box_1"


def find_gap(png: bytes, piece_w: int = 50) -> int:
    """返回缺口左边缘相对背景图左边的像素 x。找不到就返回 0。"""
    img = np.asarray(Image.open(__import__("io").BytesIO(png)).convert("L"), dtype=np.int16)
    h, w = img.shape
    band = img[h // 4: h * 3 // 4]  # 中间那条，避开上下边框
    # 相邻列差的绝对值之和：缺口的竖直边在这里是尖峰
    grad = np.abs(np.diff(band.astype(np.int32), axis=1)).sum(axis=0)
    grad[: piece_w + 20] = 0  # 左边那块是滑块自己，别把它当缺口
    grad[w - 10:] = 0
    return int(grad.argmax()) if grad.max() > 0 else 0


def track(distance: int) -> list[int]:
    """先加速后减速的位移序列，末尾故意冲过头再退回来——人手就是这样。"""
    steps, cur, v = [], 0.0, 0.0
    mid = distance * 0.75
    while cur < distance:
        a = 2.5 if cur < mid else -3.0
        v = max(v + a, 1.0)
        cur += v
        steps.append(round(cur))
    steps += [distance + 2, distance + 1, distance]
    return steps


async def solve(page: Page, attempts: int = 5) -> bool:
    for i in range(attempts):
        try:
            await page.wait_for_selector(PIC, timeout=15000)
        except Exception:
            return False
        pic = await page.locator(PIC).bounding_box()
        sld = await page.locator(SLIDER).bounding_box()
        if not pic or not sld:
            return False

        gap = find_gap(await page.locator(PIC).screenshot(), int(sld["width"]))
        # 滑块在背景图里的初始偏移，要从目标里扣掉
        distance = gap - int(sld["x"] - pic["x"])
        if distance <= 5:
            print(f"  [captcha] 第{i + 1}次：缺口没找准（gap={gap}），换一张")
            await page.locator(f"{PIC} ~ * >> nth=0").click(timeout=3000)
            continue

        x, y = sld["x"] + sld["width"] / 2, sld["y"] + sld["height"] / 2
        await page.mouse.move(x, y)
        await page.mouse.down()
        for dx in track(distance):
            await page.mouse.move(x + dx, y + random.uniform(-1.5, 1.5))
            await page.wait_for_timeout(random.randint(8, 22))
        await page.mouse.up()
        await page.wait_for_timeout(2500)

        if not await page.locator(PIC).is_visible():
            print(f"  [captcha] 第{i + 1}次通过（滑了 {distance}px）")
            return True
        print(f"  [captcha] 第{i + 1}次失败（滑了 {distance}px），重试")
        await page.wait_for_timeout(1500)
    return False
