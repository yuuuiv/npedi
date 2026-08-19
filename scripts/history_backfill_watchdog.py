"""Watch a history-backfill wrapper and send one durable disconnect alert.

The watchdog is deliberately independent from the collector.  It reads the
candidate queue through a read-only SQLite connection, watches the wrapper PID
and its redirected log, and persists an event before attempting delivery.

No token, request header, or response body is ever written to the state file or
logs.  The temp-mail Address JWT is used only in memory for ``POST
/api/send_mail``.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import BASE_DIR, Config, _get, _parse_env_file, load_config


log = logging.getLogger("npedi.history_watchdog")

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
# No hardcoded personal address: same "env var, then .env file" precedence as
# the rest of config.py.  Unset NPEDI_ALERT_EMAIL fails validate_spec() with a
# clear error at startup instead of silently mailing the wrong inbox.
DEFAULT_TO = _get(_parse_env_file(BASE_DIR / ".env"), "NPEDI_ALERT_EMAIL")
DEFAULT_STALE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_RETRY_SECONDS = 60.0


class WatchdogError(RuntimeError):
    """A safe-to-log watchdog error which never contains remote response data."""


class MailDeliveryError(WatchdogError):
    """A classified delivery error without credentials or response content."""


class HttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> Any: ...


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def iso_beijing(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING).isoformat(timespec="seconds")


def parse_day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


@dataclass(frozen=True)
class WatchSpec:
    pid: int
    eta_start: str
    eta_end: str
    catalog_scope: str
    log_file: Path
    state_file: Path
    recipient: str = DEFAULT_TO
    stale_seconds: float = DEFAULT_STALE_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS

    @property
    def eta_end_exclusive(self) -> str:
        end = datetime.strptime(self.eta_end, "%Y-%m-%d")
        return (end + timedelta(days=1)).strftime("%Y-%m-%d")

    @property
    def watch_key(self) -> str:
        identity = {
            "pid": self.pid,
            "eta_start": self.eta_start,
            "eta_end": self.eta_end,
            "catalog_scope": self.catalog_scope,
            "log_file": str(self.log_file.resolve()),
        }
        raw = json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def validate_spec(spec: WatchSpec) -> None:
    try:
        start = datetime.strptime(spec.eta_start, "%Y-%m-%d")
        end = datetime.strptime(spec.eta_end, "%Y-%m-%d")
    except ValueError as exc:
        raise WatchdogError("eta-start and eta-end must use YYYY-MM-DD") from exc
    if start > end:
        raise WatchdogError("eta-start must not be later than eta-end")
    if spec.pid <= 0:
        raise WatchdogError("pid must be positive")
    if spec.catalog_scope not in {"known", "unknown", "all"}:
        raise WatchdogError("catalog-scope must be known, unknown, or all")
    if spec.stale_seconds <= 0 or spec.poll_seconds <= 0:
        raise WatchdogError("stale-seconds and poll-seconds must be positive")
    if "\n" in spec.recipient or "\r" in spec.recipient or "@" not in spec.recipient:
        raise WatchdogError("invalid alert recipient")


def load_state(path: Path, spec: WatchSpec, now: datetime) -> dict[str, Any]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WatchdogError(f"cannot read watchdog state ({type(exc).__name__})") from exc
        if not isinstance(state, dict) or state.get("version") != 1:
            raise WatchdogError("unsupported watchdog state format")
        if state.get("watch_key") != spec.watch_key:
            raise WatchdogError("state file belongs to a different watched process")
        return state
    return {
        "version": 1,
        "watch_key": spec.watch_key,
        "watch_started_at": iso_beijing(now),
        "last_check_at": None,
        "last_remaining": None,
        "last_process_alive": None,
        "last_heartbeat_at": None,
        "completed_at": None,
        "event": None,
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist non-secret state in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def process_is_alive(pid: int) -> bool:
    """Check a PID without requiring psutil or terminating the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means a process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_heartbeat(path: Path) -> datetime | None:
    try:
        modified = path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None
    return datetime.fromtimestamp(modified, BEIJING)


def count_remaining(cfg: Config, spec: WatchSpec) -> int:
    """Mirror the wrapper's queue count using a strictly read-only connection."""
    database = cfg.db_path.resolve()
    uri = database.as_uri() + "?mode=ro"
    catalog_filter = ""
    params: list[Any] = [spec.eta_start, spec.eta_end_exclusive]
    if spec.catalog_scope != "all":
        catalog_filter = " AND c.vessel_in_catalog=?"
        params.append(1 if spec.catalog_scope == "known" else 0)
    query = f"""
        SELECT COUNT(*)
        FROM gate_history_candidate AS c
        WHERE c.status IN ('pending','hit')
          AND NOT EXISTS (
              SELECT 1 FROM gate_history_rejected_pair AS rejected
              WHERE rejected.vesselcode=c.vesselcode
                AND rejected.voyage=c.voyage
          )
          AND EXISTS (
              SELECT 1 FROM gate_history_candidate_month AS m
              WHERE m.vesselcode=c.vesselcode AND m.voyage=c.voyage
                AND m.last_eta>=? AND m.first_eta<?
          )
          {catalog_filter}
    """
    with sqlite3.connect(uri, uri=True, timeout=10.0) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(query, params).fetchone()
    return int(row[0]) if row else 0


class TempMailSender:
    """Send through the configured temp-mail Worker without exposing secrets."""

    def __init__(
        self,
        cfg: Config,
        recipient: str,
        *,
        client: HttpClient | None = None,
    ) -> None:
        self.base_url = cfg.temp_mail_base_url.rstrip("/")
        self.address_jwt = cfg.temp_mail_address_jwt
        self.site_password = cfg.temp_mail_site_password
        self.recipient = recipient
        self.client = client

    @staticmethod
    def _content(event: dict[str, Any]) -> str:
        reason = {
            "process_exited": "回填 wrapper 进程已退出，但队列仍未完成",
            "heartbeat_stale": "回填日志心跳超过阈值，任务可能断连或卡住",
        }.get(str(event.get("reason")), "历史回填任务异常")
        fields = [
            "NPEDI 历史回填告警",
            "",
            f"事件时间（北京时间）：{event['detected_at']}",
            f"原因：{reason}",
            f"wrapper PID：{event['pid']}",
            f"回填范围：{event['eta_start']} 至 {event['eta_end']}",
            f"候选范围：{event['catalog_scope']}",
            f"尚余船舶×航次：{event['remaining']}",
            f"最后日志心跳：{event.get('last_heartbeat_at') or '未检测到'}",
            f"日志文件：{event['log_file']}",
            f"事件 ID：{event['id']}",
            "",
            "该事件已先持久化到本地状态文件；本邮件不会触发自动登录或短信。",
        ]
        return "\n".join(fields)

    def send(self, event: dict[str, Any]) -> None:
        if not self.base_url or not self.address_jwt:
            raise MailDeliveryError("mail_configuration_missing")
        headers = {
            "Authorization": f"Bearer {self.address_jwt}",
            "Content-Type": "application/json",
            # Best-effort protection if the Worker/proxy honors this header.
            "Idempotency-Key": str(event["id"]),
        }
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        body = {
            "from_name": "NPEDI History Watchdog",
            "to_name": "NPEDI Operator",
            "to_mail": self.recipient,
            "subject": f"[NPEDI] 历史回填断连告警 {event['detected_at']}",
            "is_html": False,
            "content": self._content(event),
        }
        owns_client = self.client is None
        client: Any = self.client or httpx.Client(timeout=httpx.Timeout(30.0))
        try:
            response = client.post(
                f"{self.base_url}/api/send_mail",
                json=body,
                headers=headers,
            )
        except Exception as exc:
            # Keep only the exception class.  Some exception messages contain
            # request objects whose headers must never reach logs or state.
            raise MailDeliveryError(f"transport_{type(exc).__name__}") from exc
        finally:
            if owns_client:
                client.close()
        status = int(getattr(response, "status_code", 0))
        if not 200 <= status < 300:
            # Deliberately do not read response.text/content/json.
            raise MailDeliveryError(f"http_status_{status}")


class HistoryBackfillWatchdog:
    def __init__(
        self,
        cfg: Config,
        spec: WatchSpec,
        *,
        sender: TempMailSender | None = None,
        remaining_reader: Callable[[], int] | None = None,
        process_checker: Callable[[int], bool] = process_is_alive,
        heartbeat_reader: Callable[[Path], datetime | None] = log_heartbeat,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        validate_spec(spec)
        self.cfg = cfg
        self.spec = spec
        self.sender = sender or TempMailSender(cfg, spec.recipient)
        self.remaining_reader = remaining_reader or (lambda: count_remaining(cfg, spec))
        self.process_checker = process_checker
        self.heartbeat_reader = heartbeat_reader
        self.retry_seconds = max(float(retry_seconds), 1.0)

    def _new_event(
        self,
        *,
        reason: str,
        remaining: int,
        heartbeat: datetime | None,
        detected_at: datetime,
    ) -> dict[str, Any]:
        event_id = hashlib.sha256(
            f"{self.spec.watch_key}:disconnect".encode("ascii")
        ).hexdigest()[:24]
        threshold_at = (
            iso_beijing(heartbeat + timedelta(seconds=self.spec.stale_seconds))
            if reason == "heartbeat_stale" and heartbeat is not None
            else None
        )
        return {
            "id": event_id,
            "reason": reason,
            "detected_at": iso_beijing(detected_at),
            "threshold_crossed_at": threshold_at,
            "pid": self.spec.pid,
            "eta_start": self.spec.eta_start,
            "eta_end": self.spec.eta_end,
            "catalog_scope": self.spec.catalog_scope,
            "remaining": remaining,
            "last_heartbeat_at": iso_beijing(heartbeat) if heartbeat else None,
            "log_file": str(self.spec.log_file.resolve()),
            "delivery_status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "next_attempt_at": iso_beijing(detected_at),
            "last_error": None,
            "sent_at": None,
        }

    def _attempt_delivery(
        self,
        state: dict[str, Any],
        now: datetime,
    ) -> str:
        event = state.get("event")
        if not isinstance(event, dict):
            return "watching"
        if event.get("delivery_status") == "sent":
            return "alert_sent"
        next_attempt_raw = event.get("next_attempt_at")
        if next_attempt_raw:
            next_attempt = datetime.fromisoformat(str(next_attempt_raw))
            if now.astimezone(BEIJING) < next_attempt.astimezone(BEIJING):
                return "alert_pending"

        event["attempts"] = int(event.get("attempts") or 0) + 1
        event["last_attempt_at"] = iso_beijing(now)
        # Persist the attempt marker before any network I/O.  The event itself
        # was already persisted when detected.
        save_state(self.spec.state_file, state)
        try:
            self.sender.send(event)
        except MailDeliveryError as exc:
            event["delivery_status"] = "pending"
            event["last_error"] = str(exc)
            event["next_attempt_at"] = iso_beijing(
                now + timedelta(seconds=self.retry_seconds)
            )
            save_state(self.spec.state_file, state)
            log.warning(
                "告警邮件发送失败（%s）；事件保持 pending，稍后重试",
                str(exc),
            )
            return "alert_pending"

        event["delivery_status"] = "sent"
        event["sent_at"] = iso_beijing(now)
        event["next_attempt_at"] = None
        event["last_error"] = None
        save_state(self.spec.state_file, state)
        log.warning("断连告警已发送；事件 ID %s", event["id"])
        return "alert_sent"

    def tick(self, now: datetime | None = None) -> str:
        now = (now or beijing_now()).astimezone(BEIJING)
        state = load_state(self.spec.state_file, self.spec, now)

        # Once an event exists it is immutable as an incident.  Only its
        # delivery fields change, which prevents duplicate detection/mail.
        if isinstance(state.get("event"), dict):
            return self._attempt_delivery(state, now)

        try:
            remaining = max(int(self.remaining_reader()), 0)
        except Exception as exc:
            # Database exception messages can contain local URIs; log only the
            # class and wait for the next poll.
            state["last_check_at"] = iso_beijing(now)
            save_state(self.spec.state_file, state)
            log.warning("无法读取剩余队列（%s），稍后重试", type(exc).__name__)
            return "watching"

        alive = bool(self.process_checker(self.spec.pid))
        heartbeat = self.heartbeat_reader(self.spec.log_file)
        state.update({
            "last_check_at": iso_beijing(now),
            "last_remaining": remaining,
            "last_process_alive": alive,
            "last_heartbeat_at": iso_beijing(heartbeat) if heartbeat else None,
        })

        if remaining == 0:
            state["completed_at"] = iso_beijing(now)
            save_state(self.spec.state_file, state)
            log.info("历史回填队列 remaining=0；正常结束，不发送告警")
            return "completed"

        reason: str | None = None
        if not alive:
            reason = "process_exited"
        else:
            started = datetime.fromisoformat(str(state["watch_started_at"]))
            effective_heartbeat = heartbeat or started
            if (now - effective_heartbeat.astimezone(BEIJING)).total_seconds() > self.spec.stale_seconds:
                reason = "heartbeat_stale"
                heartbeat = effective_heartbeat

        if reason is None:
            save_state(self.spec.state_file, state)
            return "watching"

        state["event"] = self._new_event(
            reason=reason,
            remaining=remaining,
            heartbeat=heartbeat,
            detected_at=now,
        )
        # Required ordering: durable Beijing-time event first, network second.
        save_state(self.spec.state_file, state)
        log.warning("检测到历史回填异常；事件已持久化，开始发送告警")
        return self._attempt_delivery(state, now)

    def run(self) -> int:
        while True:
            outcome = self.tick()
            if outcome in {"completed", "alert_sent"}:
                return 0
            time.sleep(self.spec.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor a history-backfill wrapper and email once on disconnect."
    )
    parser.add_argument("--pid", required=True, type=int, help="wrapper process PID")
    parser.add_argument("--eta-start", required=True, help="inclusive YYYY-MM-DD")
    parser.add_argument("--eta-end", required=True, help="inclusive YYYY-MM-DD")
    parser.add_argument(
        "--catalog-scope", choices=("known", "unknown", "all"), default="all"
    )
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=PROJECT_ROOT / "logs" / "history_backfill_watchdog.json",
    )
    parser.add_argument("--to", dest="recipient", default=DEFAULT_TO)
    parser.add_argument(
        "--stale-seconds", type=float, default=float(DEFAULT_STALE_SECONDS)
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    spec = WatchSpec(
        pid=args.pid,
        eta_start=args.eta_start,
        eta_end=args.eta_end,
        catalog_scope=args.catalog_scope,
        log_file=args.log_file,
        state_file=args.state_file,
        recipient=args.recipient,
        stale_seconds=args.stale_seconds,
        poll_seconds=args.poll_seconds,
    )
    try:
        return HistoryBackfillWatchdog(load_config(), spec).run()
    except WatchdogError as exc:
        log.error("看门狗启动失败：%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("看门狗已停止；已有 pending 事件仍保留在状态文件中")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
