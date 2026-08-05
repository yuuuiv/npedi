"""Automatic NPEDI SMS login primitives.

Secrets are supplied through Config/environment and are never logged.  The
captcha recognizer is an external local command so an NPEDI-specific model can
be upgraded independently from the crawler.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

import httpx

from config import DEFAULT_UA

if TYPE_CHECKING:
    from config import Config


class AutoLoginError(RuntimeError):
    """Automatic login could not safely obtain a new token."""


class RefreshLock:
    """Cross-process lock that waits for another token refresh to finish."""

    def __init__(self, path: Path, *, timeout_seconds: float):
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self) -> "RefreshLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            handle = self.path.open("a+")
            handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError:
                handle.close()
                if time.monotonic() >= deadline:
                    raise AutoLoginError("timed out waiting for another token refresh")
                time.sleep(0.5)

    def __exit__(self, *_: Any) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


class CaptchaSolver(Protocol):
    def solve(self, image: bytes) -> str: ...


class OtpReader(Protocol):
    def wait_for_code(self, *, not_before: int) -> str: ...


@dataclass(frozen=True)
class CaptchaChallenge:
    uuid: str
    image: bytes


class CommandCaptchaSolver:
    """Run a local recognizer as ``command <temporary-jpeg>``.

    The command must print only the answer on its final non-empty stdout line.
    Shell execution is deliberately disabled.
    """

    def __init__(self, command: str, *, timeout_seconds: float = 30.0):
        self.argv = shlex.split(command, posix=os.name != "nt")
        if not self.argv:
            raise AutoLoginError("CAPTCHA_SOLVER_COMMAND is empty")
        self.timeout_seconds = timeout_seconds

    def solve(self, image: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(image)
            image_path = Path(handle.name)
        try:
            result = subprocess.run(
                [*self.argv, str(image_path)],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutoLoginError(f"captcha solver failed: {type(exc).__name__}") from exc
        finally:
            image_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise AutoLoginError(f"captcha solver exited with code {result.returncode}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        answer = lines[-1] if lines else ""
        if not re.fullmatch(r"[-+A-Za-z0-9]{1,12}", answer):
            raise AutoLoginError("captcha solver returned an invalid answer")
        return answer


class TelegramOtpReader:
    """Read SmsForwarder messages through a dedicated second Telegram bot."""

    def __init__(
        self,
        *,
        reader_bot_token: str,
        chat_id: str,
        sender_bot_id: str,
        code_pattern: str = r"(?<!\d)(\d{4,8})(?!\d)",
        timeout_seconds: float = 180.0,
        client: httpx.Client | None = None,
    ):
        if not reader_bot_token or not chat_id or not sender_bot_id:
            raise AutoLoginError("Telegram reader bot token, chat id, and sender bot id are required")
        self.chat_id = str(chat_id)
        self.sender_bot_id = str(sender_bot_id)
        self.pattern = re.compile(code_pattern)
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=f"https://api.telegram.org/bot{reader_bot_token}",
            timeout=httpx.Timeout(30.0, read=35.0),
        )
        self.offset: int | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def wait_for_code(self, *, not_before: int) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            params: dict[str, Any] = {
                "timeout": min(20, max(1, int(deadline - time.monotonic()))),
                "allowed_updates": json.dumps(["message", "channel_post"]),
            }
            if self.offset is not None:
                params["offset"] = self.offset
            response = self.client.get("/getUpdates", params=params)
            if response.status_code == 409:
                raise AutoLoginError("Telegram reader bot has a webhook; getUpdates cannot run at the same time")
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise AutoLoginError("Telegram getUpdates returned ok=false")
            for update in payload.get("result") or []:
                update_id = int(update.get("update_id", 0))
                self.offset = max(self.offset or 0, update_id + 1)
                message = update.get("message") or update.get("channel_post") or {}
                sender = message.get("from") or message.get("sender_chat") or {}
                if str((message.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                if str(sender.get("id")) != self.sender_bot_id:
                    continue
                if int(message.get("date") or 0) < not_before:
                    continue
                match = self.pattern.search(str(message.get("text") or message.get("caption") or ""))
                if match:
                    return match.groupdict().get("code") or match.group(1)
        raise AutoLoginError("timed out waiting for the NPEDI SMS verification code")


class NpediAuthenticator:
    def __init__(
        self,
        *,
        base_url: str,
        mobile: str,
        captcha_solver: CaptchaSolver,
        otp_reader: OtpReader,
        timeout_seconds: float = 60.0,
        captcha_attempts: int = 3,
        client: httpx.Client | None = None,
    ):
        self.mobile = mobile.strip()
        if not re.fullmatch(r"\d{6,20}", self.mobile):
            raise AutoLoginError("NPEDI_MOBILE is not a valid numeric mobile identifier")
        self.captcha_solver = captcha_solver
        self.otp_reader = otp_reader
        self.captcha_attempts = max(1, min(captcha_attempts, 5))
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/") + "/onesite-api",
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": DEFAULT_UA,
                "Referer": base_url.rstrip("/") + "/onesite/login",
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        close = getattr(self.otp_reader, "close", None)
        if close:
            close()

    @staticmethod
    def _successful_payload(response: httpx.Response, endpoint: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise AutoLoginError(f"{endpoint} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AutoLoginError(f"{endpoint} returned non-JSON") from exc
        if payload.get("code") != 200:
            raise AutoLoginError(f"{endpoint} returned code={payload.get('code')}")
        return payload

    def fetch_captcha(self) -> CaptchaChallenge:
        payload = self._successful_payload(self.client.get("/captchaImage"), "captchaImage")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        uuid = str(data.get("uuid") or "")
        encoded = data.get("img")
        if not uuid or not isinstance(encoded, str):
            raise AutoLoginError("captchaImage response is missing data.uuid or data.img")
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise AutoLoginError("captchaImage returned invalid base64") from exc
        return CaptchaChallenge(uuid=uuid, image=image)

    def send_sms(self) -> int:
        not_before = int(time.time()) - 2
        self._successful_payload(self.client.get("/getSms", params={"mobile": self.mobile}), "getSms")
        return not_before

    def login(self) -> str:
        not_before = self.send_sms()
        otp = self.otp_reader.wait_for_code(not_before=not_before)
        for _ in range(self.captcha_attempts):
            challenge = self.fetch_captcha()
            captcha = self.captcha_solver.solve(challenge.image)
            response = self.client.post(
                "/login",
                params={
                    "mobile": self.mobile,
                    "code": captcha,
                    "password": otp,
                    "uuid": challenge.uuid,
                },
            )
            if response.status_code != 200:
                raise AutoLoginError(f"login returned HTTP {response.status_code}")
            payload = response.json()
            token = ((payload.get("data") or {}).get("token") if isinstance(payload, dict) else None)
            if payload.get("code") == 200 and isinstance(token, str) and token.strip():
                return token.strip()
            # A bad image answer gets a fresh challenge; SMS is requested only once.
        raise AutoLoginError("NPEDI login failed after the configured captcha attempts")


def update_env_token(path: Path, token: str) -> None:
    """Atomically replace the token assignment while preserving the .env file."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    token_line = re.compile(r"^(\s*(?:export\s+)?(?:WEB_TOKEN|WEB-TOKEN|Web-Token)\s*=).*$", re.IGNORECASE)
    replacement = None
    output = []
    for line in lines:
        match = token_line.match(line)
        if match and replacement is None:
            replacement = f"{match.group(1)}{token}"
            output.append(replacement)
        else:
            output.append(line)
    if replacement is None:
        output.append(f"WEB_TOKEN={token}")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_authenticator(cfg: "Config") -> NpediAuthenticator:
    solver = CommandCaptchaSolver(cfg.captcha_solver_command, timeout_seconds=cfg.captcha_solver_timeout_seconds)
    reader = TelegramOtpReader(
        reader_bot_token=cfg.telegram_reader_bot_token,
        chat_id=cfg.telegram_chat_id,
        sender_bot_id=cfg.telegram_sms_sender_bot_id,
        code_pattern=cfg.sms_code_pattern,
        timeout_seconds=cfg.sms_code_timeout_seconds,
    )
    return NpediAuthenticator(
        base_url=cfg.base_url,
        mobile=cfg.npedi_mobile,
        captcha_solver=solver,
        otp_reader=reader,
        timeout_seconds=cfg.timeout_seconds,
        captcha_attempts=cfg.captcha_attempts,
    )
