"""HTTP 客户端：认证、重试、限速、翻页。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Iterator

import httpx

from config import DEFAULT_UA, Config, load_config

log = logging.getLogger("npedi.client")


class AuthExpired(RuntimeError):
    """token 失效，立即停止本轮。"""


class ApiError(RuntimeError):
    """接口返回了非鉴权类错误。"""


_AUTH_MSG_PATTERN = ("未登录", "登录状态", "登录已过期", "认证失败", "无效的会话", "token", "令牌")
GATE_QUERY_TYPES = {"GATE_IN": "GATE_IN REPORT", "GATE_OUT": "GATE_OUT REPORT"}


class NpediClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.request_count = 0
        self._last_request_at = 0.0
        if not cfg.token and not cfg.auto_login:
            raise AuthExpired("配置中没有 token，请在 .env 里设置 WEB_TOKEN")
        headers = {
            "Referer": cfg.referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": DEFAULT_UA,
        }
        if cfg.token:
            headers["ediAuthorization"] = f"Bearer {cfg.token}"
        self._http = httpx.Client(
            base_url=cfg.api_base,
            timeout=cfg.timeout_seconds,
            headers=headers,
            follow_redirects=False,
        )

    def _refresh_token(self) -> None:
        """Refresh once under a cross-process lock without logging secrets."""
        if not self.cfg.auto_login:
            raise AuthExpired("token expired and AUTO_LOGIN is disabled")
        from auth import AutoLoginError, RefreshLock, build_authenticator, update_env_token

        timeout = self.cfg.sms_code_timeout_seconds + (
            self.cfg.captcha_solver_timeout_seconds * self.cfg.captcha_attempts
        ) + 60
        lock_path = self.cfg.env_path.parent / ".auth-refresh.lock"
        try:
            with RefreshLock(lock_path, timeout_seconds=timeout):
                latest = load_config(self.cfg.env_path).token
                if latest and latest != self.cfg.token:
                    token = latest
                else:
                    authenticator = build_authenticator(self.cfg)
                    try:
                        token = authenticator.login()
                    finally:
                        authenticator.close()
                    update_env_token(self.cfg.env_path, token)
                self.cfg.token = token
                self._http.headers["ediAuthorization"] = f"Bearer {token}"
                log.info("token refreshed automatically")
        except AutoLoginError as exc:
            raise AuthExpired(f"automatic login failed: {exc}") from exc

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NpediClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """Use the existing guarded GET path for timeseries crawlers."""
        return self._get(path, params)

    def vessel_plan_page(self, page: int, *, page_size: int, **filters: str) -> dict:
        params = {
            "vesselCnName": filters.get("vesselCnName", ""),
            "vesselEnName": filters.get("vesselEnName", ""),
            "voyage": filters.get("voyage", ""),
            "etaBegin": filters.get("etaBegin", ""),
            "etaEnd": filters.get("etaEnd", ""),
            "terminal": filters.get("terminal", ""),
            "page": page,
            "pageSize": page_size,
        }
        return self._get("/vessel/plan/selectContainerDynamicPlan", params)

    def container_notice_page(self, page: int, *, page_size: int, **filters: str) -> dict:
        params = {
            "pageNum": page,
            "pageSize": page_size,
            "voyage": filters.get("voyage", ""),
            "vesselename": filters.get("vesselename", ""),
            "vesselowner": filters.get("vesselowner", ""),
            "vesselowner2": filters.get("vesselowner2", ""),
            "matou": filters.get("matou", ""),
            "ctnstart": filters.get("ctnstart", ""),
        }
        return self._get("/vessel/dzyjh/getlist", params)

    def _throttle(self) -> None:
        lo, hi = self.cfg.request_delay
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict | None = None) -> dict:
        attempt = 0
        auth_refreshed = False
        while True:
            attempt += 1
            self._throttle()
            try:
                resp = self._http.get(path, params=params)
                self._last_request_at = time.monotonic()
                self.request_count += 1
            except httpx.TransportError as exc:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 网络错误，重试 {self.cfg.max_retries} 次后仍失败: {exc}") from exc
                backoff = 3 ** (attempt - 1)
                log.warning("网络错误，%ss 后重试 %d/%d", backoff, attempt, self.cfg.max_retries)
                time.sleep(backoff)
                continue
            if resp.status_code in (401, 403):
                if not auth_refreshed and self.cfg.auto_login:
                    self._refresh_token()
                    auth_refreshed, attempt = True, 0
                    continue
                raise AuthExpired(f"{path} 返回 HTTP {resp.status_code}，token 已失效")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 返回 HTTP {resp.status_code}，重试后仍失败")
                time.sleep(3 ** (attempt - 1))
                continue
            if resp.status_code != 200:
                raise ApiError(f"{path} 返回 HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                payload = resp.json()
            except ValueError as exc:
                snippet = resp.text[:200]
                if "<html" in snippet.lower() or "login" in snippet.lower():
                    if not auth_refreshed and self.cfg.auto_login:
                        self._refresh_token()
                        auth_refreshed, attempt = True, 0
                        continue
                    raise AuthExpired(f"{path} 返回非 JSON，疑似登录态失效") from exc
                raise ApiError(f"{path} 返回非 JSON: {snippet!r}") from exc
            code = payload.get("code")
            if code == 200:
                return payload
            msg = str(payload.get("msg", ""))
            if code in (401, 403) or any(p in msg for p in _AUTH_MSG_PATTERN):
                if not auth_refreshed and self.cfg.auto_login:
                    self._refresh_token()
                    auth_refreshed, attempt = True, 0
                    continue
                raise AuthExpired(f"{path} 返回 code={code}，token 已失效")
            raise ApiError(f"{path} 返回 code={code} msg={msg!r}")

    def vgm_page(self, page: int, *, page_size: int, **filters: str) -> dict:
        params = {"pageNum": page, "pageSize": page_size, "containerNumber": filters.get("containerNumber", ""), "vessel": filters.get("vessel", ""), "ctnOperatorCode": filters.get("ctnOperatorCode", ""), "senderCode": filters.get("senderCode", ""), "direct": filters.get("direct", ""), "containerType": filters.get("containerType", "")}
        return self._get("/ctnvgm/getlist", params)

    def cargo_release_page(self, page: int, *, page_size: int, **filters: str) -> dict:
        params = {"val": filters.get("val", ""), "passno": filters.get("passno", ""), "billno": filters.get("billno", ""), "vesselcode": filters.get("vesselcode", ""), "voyage": filters.get("voyage", ""), "vesselAndVoyage": filters.get("vesselAndVoyage", ""), "pageNum": page, "pageSize": page_size}
        return self._post("/ediCustptrSZ/getEdiCustptrSz", params)

    def transshipment_page(self, page: int, *, page_size: int, **_: str) -> dict:
        return self._get("/npp/nzx/getNzwPageResult", {"pageNum": page, "pageSize": page_size})

    def container_history(self, container_no: str) -> dict:
        return self._get(f"/ediContainerlog/getEdiContainerlog/{container_no}")

    def _post(self, path: str, params: dict) -> dict:
        attempt = 0
        auth_refreshed = False
        while True:
            attempt += 1
            self._throttle()
            try:
                resp = self._http.post(path, params=params)
                self._last_request_at = time.monotonic()
                self.request_count += 1
            except httpx.TransportError as exc:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 网络错误，重试后仍失败: {exc}") from exc
                time.sleep(3 ** (attempt - 1))
                continue
            if resp.status_code in (401, 403):
                if not auth_refreshed and self.cfg.auto_login:
                    self._refresh_token()
                    auth_refreshed, attempt = True, 0
                    continue
                raise AuthExpired(f"{path} 返回 HTTP {resp.status_code}，token 已失效")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.cfg.max_retries:
                    raise ApiError(f"{path} 返回 HTTP {resp.status_code}，重试后仍失败")
                time.sleep(3 ** (attempt - 1))
                continue
            if resp.status_code != 200:
                raise ApiError(f"{path} 返回 HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                payload = resp.json()
            except ValueError as exc:
                if not auth_refreshed and self.cfg.auto_login and "login" in resp.text[:200].lower():
                    self._refresh_token()
                    auth_refreshed, attempt = True, 0
                    continue
                raise ApiError(f"{path} 返回非 JSON") from exc
            if payload.get("code") == 200:
                return payload
            msg = str(payload.get("msg", ""))
            if payload.get("code") in (401, 403) or any(p in msg for p in _AUTH_MSG_PATTERN):
                if not auth_refreshed and self.cfg.auto_login:
                    self._refresh_token()
                    auth_refreshed, attempt = True, 0
                    continue
                raise AuthExpired(f"{path} 返回 code={payload.get('code')}，token 已失效")
            raise ApiError(f"{path} 返回 code={payload.get('code')} msg={msg!r}")
    def get_info(self) -> dict:
        return self._get("/getInfo").get("data") or {}

    def vesselinfo(self) -> list[dict]:
        data = self._get("/npp/search/vesselinfo").get("data") or []
        return [row for row in data if isinstance(row, dict)]

    def integrated_page(self, page: int, *, unvessel: str = "", voyage: str = "", compare_time: str = "", compare_flag: str = "", page_size: int | None = None) -> dict:
        params = {"pageNum": page, "pageSize": page_size or self.cfg.page_size, "matou": "", "agent": "", "envessel": "", "unvessel": unvessel, "voyage": voyage, "containerno": "", "billno": "", "compareTime": compare_time, "passFlag": "", "sendFlag": "", "compareFlag": compare_flag}
        return self._get("/npp/search/integrated", params).get("data") or {}

    def iter_integrated(self, *, unvessel: str = "", voyage: str = "", compare_time: str = "", compare_flag: str = "", start_page: int = 1) -> Iterator[tuple[int, int, list[dict]]]:
        page = max(1, start_page)
        fetched = (page - 1) * self.cfg.page_size
        while True:
            data = self.integrated_page(page, unvessel=unvessel, voyage=voyage, compare_time=compare_time, compare_flag=compare_flag)
            rows = data.get("list") or []
            total = int(data.get("total") or 0)
            yield page, total, rows
            fetched += len(rows)
            if not rows or fetched >= total:
                return
            if page >= self.cfg.max_pages_per_query:
                return
            page += 1

    def vessel_list(self) -> list[dict]:
        data = self._get("/voyage/vesselList").get("data") or []
        return [row for row in data if isinstance(row, dict)]

    def scodeco_page(self, page: int, *, direction: str, vessel_code: str, voyage: str, page_size: int | None = None) -> dict:
        params = {"type": GATE_QUERY_TYPES[direction], "pageNum": page, "pageSize": page_size or self.cfg.gate_page_size, "voyage": voyage, "vesselCode": vessel_code, "ctnOperatorCode": "", "ctnNo": "", "blNo": ""}
        return self._get("/scodeco/list", params).get("data") or {}

    def iter_scodeco(self, *, direction: str, vessel_code: str, voyage: str) -> Iterator[tuple[int, int, list[dict]]]:
        page, fetched = 1, 0
        while True:
            data = self.scodeco_page(page, direction=direction, vessel_code=vessel_code, voyage=voyage)
            rows, total = data.get("list") or [], int(data.get("total") or 0)
            yield page, total, rows
            fetched += len(rows)
            if not rows or fetched >= total or page >= self.cfg.max_pages_per_query:
                return
            page += 1


def fmt_compare_window(start: datetime, end: datetime) -> str:
    return f"{start:%Y%m%d%H%M%S},{end:%Y%m%d%H%M%S}"
