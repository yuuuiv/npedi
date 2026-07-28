"""HTTP 客户端：认证、重试、限速、翻页（对应 ARCHITECTURE.md §1、§7）。

只访问三个 JSON 接口，绝不请求 HTML/JS/CSS/图片，也不调用 searchcountAll。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Iterator

import httpx

from config import DEFAULT_UA, Config

log = logging.getLogger("npedi.client")


class AuthExpired(RuntimeError):
    """token 失效 —— 必须立即停止本轮，绝不重试轰炸。"""


class ApiError(RuntimeError):
    """接口返回了非鉴权类的错误。"""


# 服务端在 token 失效时不一定用 HTTP 401，也可能 200 + 业务码/文案，故做双重判断。
_AUTH_MSG_PATTERN = ("未登录", "登录状态", "登录已过期", "认证失败", "无效的会话", "token", "令牌")


class NpediClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.request_count = 0
        self._last_request_at = 0.0
        if not cfg.token:
            raise AuthExpired("配置中没有 token，请在 .env 里设置 WEB_TOKEN（见 README 取值步骤）")
        self._http = httpx.Client(
            base_url=cfg.api_base,
            timeout=cfg.timeout_seconds,
            headers={
                "ediAuthorization": f"Bearer {cfg.token}",
                "Referer": cfg.referer,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": DEFAULT_UA,
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NpediClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 内部

    def _throttle(self) -> None:
        """串行 + 随机延时：对方是口岸政务系统，务必温和。"""
        lo, hi = self.cfg.request_delay
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict | None = None) -> dict:
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            try:
                resp = self._http.get(path, params=params)
                self._last_request_at = time.monotonic()
                self.request_count += 1
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 网络错误，重试 {self.cfg.max_retries} 次后仍失败: {exc}") from exc
                backoff = 3 ** (attempt - 1)
                log.warning("网络错误(%s)，%ss 后重试 %d/%d", exc, backoff, attempt, self.cfg.max_retries)
                time.sleep(backoff)
                continue

            if resp.status_code in (401, 403):
                raise AuthExpired(f"{path} 返回 HTTP {resp.status_code}，token 已失效")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 返回 HTTP {resp.status_code}，重试后仍失败")
                backoff = 3 ** (attempt - 1)
                log.warning("HTTP %d，%ss 后重试 %d/%d", resp.status_code, backoff, attempt, self.cfg.max_retries)
                time.sleep(backoff)
                continue
            if resp.status_code != 200:
                raise ApiError(f"{path} 返回 HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                payload = resp.json()
            except ValueError as exc:
                # 登录态失效时后端有可能吐回登录页 HTML 而非 JSON
                snippet = resp.text[:200]
                if "<html" in snippet.lower() or "login" in snippet.lower():
                    raise AuthExpired(f"{path} 返回了非 JSON 内容，疑似登录态失效: {snippet!r}") from exc
                raise ApiError(f"{path} 返回了非 JSON 内容: {snippet!r}") from exc

            code = payload.get("code")
            if code == 200:
                return payload
            msg = str(payload.get("msg", ""))
            if code in (401, 403) or any(p in msg for p in _AUTH_MSG_PATTERN):
                raise AuthExpired(f"{path} 返回 code={code} msg={msg!r}，token 已失效")
            raise ApiError(f"{path} 返回 code={code} msg={msg!r}")

    # ------------------------------------------------------------------ 接口

    def get_info(self) -> dict:
        """轻量探活：能正常返回即说明 token 有效（1 次请求）。"""
        return self._get("/getInfo").get("data") or {}

    def vesselinfo(self) -> list[dict]:
        """全部在册航次目录，无分页无参数（1 次请求）。"""
        data = self._get("/npp/search/vesselinfo").get("data") or []
        return [row for row in data if isinstance(row, dict)]

    def integrated_page(
        self,
        page: int,
        *,
        unvessel: str = "",
        voyage: str = "",
        compare_time: str = "",
        compare_flag: str = "",
        page_size: int | None = None,
    ) -> dict:
        """集装箱明细的单页。参数顺序与站点前端一致，空值也照样发送。"""
        params = {
            "pageNum": page,
            "pageSize": page_size or self.cfg.page_size,
            "matou": "",
            "agent": "",
            "envessel": "",
            "unvessel": unvessel,
            "voyage": voyage,
            "containerno": "",
            "billno": "",
            "compareTime": compare_time,
            "passFlag": "",
            "sendFlag": "",
            "compareFlag": compare_flag,
        }
        return self._get("/npp/search/integrated", params).get("data") or {}

    def iter_integrated(
        self,
        *,
        unvessel: str = "",
        voyage: str = "",
        compare_time: str = "",
        compare_flag: str = "",
        start_page: int = 1,
    ) -> Iterator[tuple[int, int, list[dict]]]:
        """按页迭代明细，产出 (页码, 总条数, 本页行)。

        分页以 total 为准（样本中 totalPages 恒为 0，不可信），
        并在返回空页 / 已取满 total 时提前结束。
        start_page > 1 用于断点续爬，跳过的页不会发出请求。
        """
        page = max(1, start_page)
        fetched = (page - 1) * (self.cfg.page_size)
        total = 0
        while True:
            data = self.integrated_page(
                page,
                unvessel=unvessel,
                voyage=voyage,
                compare_time=compare_time,
                compare_flag=compare_flag,
            )
            rows = data.get("list") or []
            total = int(data.get("total") or 0)
            yield page, total, rows

            fetched += len(rows)
            if not rows or fetched >= total:
                return
            if page >= self.cfg.max_pages_per_query:
                log.warning("翻页达到上限 %d（total=%d），提前停止", self.cfg.max_pages_per_query, total)
                return
            page += 1


def fmt_compare_window(start: datetime, end: datetime) -> str:
    """→ `20260721110833,20260729110833`"""
    return f"{start:%Y%m%d%H%M%S},{end:%Y%m%d%H%M%S}"
