"""配置加载（对应 ARCHITECTURE.md §6）。

只依赖标准库解析 .env，兼容现有 .env 中 `Web-Token = xxx` 这种带空格、带连字符的写法。
环境变量优先级高于 .env 文件，便于 Task Scheduler 临时覆盖。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 浏览器 UA（服务端未做 UA 校验，保持正常值即可）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# token 在 .env 中可能用的键名，按优先级排列
_TOKEN_KEYS = ("WEB_TOKEN", "WEB-TOKEN", "EDI_TOKEN", "EDI-TOKEN", "TOKEN")


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：支持 `K=V` / `K = V` / `export K=V`、# 注释、可选引号。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.upper()] = value
    return data


def _parse_delay(spec: str) -> tuple[float, float]:
    """"500-1000" → (0.5, 1.0)；"800" → (0.8, 0.8)。单位毫秒。"""
    spec = (spec or "").strip()
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", spec)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
    elif spec.isdigit():
        lo = hi = int(spec)
    else:
        lo, hi = 500, 1000
    if lo > hi:
        lo, hi = hi, lo
    return lo / 1000.0, hi / 1000.0


@dataclass
class Config:
    token: str = ""
    base_url: str = "https://www.npedi.com"
    page_size: int = 200
    compare_window_overlap_hours: float = 2.0
    compare_window_manual: str = ""
    request_delay: tuple[float, float] = (0.5, 1.0)
    db_path: Path = BASE_DIR / "npedi.sqlite"
    export_dir: Path = BASE_DIR / "export"
    log_dir: Path = BASE_DIR / "logs"
    alert_file: Path = BASE_DIR / "ALERT_TOKEN_EXPIRED"
    lock_file: Path = BASE_DIR / ".sync.lock"
    # 进出门管线用独立的锁：一次跑满的历史回填要占十几到二十几小时，
    # 共用一把锁的话这段时间里 npp 的每日增量会全部被挡在门外。
    # 两边写的是不同的表，SQLite 开着 WAL，并发写靠 busy_timeout 排队。
    gate_lock_file: Path = BASE_DIR / ".sync.gate.lock"

    # --- 网络行为 ---
    timeout_seconds: float = 60.0
    max_retries: int = 3               # 仅针对网络错误 / 5xx / 429
    auth_probe: bool = True            # 每轮开始用 getInfo 探活 token（1 次请求）
    env_path: Path = BASE_DIR / ".env"
    auto_login: bool = False
    npedi_mobile: str = ""
    captcha_solver_command: str = ""
    captcha_solver_timeout_seconds: float = 30.0
    captcha_attempts: int = 3
    otp_reader_backend: str = "telegram"
    telegram_reader_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_sms_sender_bot_id: str = ""
    temp_mail_base_url: str = ""
    temp_mail_address_jwt: str = ""
    temp_mail_site_password: str = ""
    temp_mail_recipient: str = ""
    temp_mail_allowed_sender: str = "support@neofantasy.online"
    temp_mail_required_text: str = "宁波舟山港"
    temp_mail_required_copies: int = 2
    temp_mail_poll_seconds: float = 3.0
    sms_code_pattern: str = r"(?<!\d)(\d{4,8})(?!\d)"
    sms_code_timeout_seconds: float = 180.0
    max_pages_per_query: int = 5000    # 防翻页失控的硬上限

    # --- 增量窗口 ---
    # UI 默认窗口的 `到` 端在当前时间之后约 1 天，这里保留同样的余量，
    # 以容忍服务端时钟偏移与"未来时间"的 compareTime。
    future_margin_hours: float = 24.0

    # --- 降级路径（假设 1 不成立时按航次循环）用到的"活跃航次"判定 ---
    active_past_days: int = 7          # 截港时间早于 now 多少天内仍算活跃
    active_future_days: int = 14       # 截港时间晚于 now 多少天内仍算活跃
    active_unknown_close_days: int = 14  # 无截港时间的航次，最近多少天内有变化才算活跃

    # --- 单轮请求预算保护 ---
    max_new_voyage_backfill_per_run: int = 50  # 增量轮里最多顺带回填多少个新航次
    reconcile_max_voyages: int = 200           # 对账轮最多覆盖多少个活跃航次

    # --- 兜底策略（§5 安全网 2），auto 表示按 probe 结果决定 ---
    empty_compare_fallback: str = "auto"       # auto | on | off

    # --- 进出门管线（ARCHITECTURE-GATE.md §7）---
    gate_page_size: int = 100
    gate_backfill_max_requests: int = 5000     # 每次回填运行的请求预算，跑完即停（0=不限）
    gate_inactive_rounds: int = 6              # 连续无新增 N 轮且已离开 npp 目录 → 退出活跃集合
    gate_active_days: int = 14                 # 不在 npp 目录里的航次，最近多少天有报文才算活跃
    gate_new_voyage_limit: int = 200           # 增量轮里最多顺带回填多少个新航次×方向
    gate_export_scope: str = "all"             # all | active | off，全量快照可能有数百万行

    # --- CSV 输出 ---
    csv_timestamp_format: str = "iso"  # iso: 20260727103201 → 2026-07-27 10:32:01；raw: 原样
    csv_keep_change_files: int = 90    # 保留最近多少个 changes_*.csv，0 表示不清理

    # --- 日志 ---
    log_keep_run_files: int = 90       # 保留最近多少个单轮日志 logs/runs/*.log，0 表示不清理
    spi_weights: dict[str, float] = field(default_factory=lambda: {"vgm": .30, "cargo_release": .20, "transshipment": .15, "arrival_delay": .15, "departure_delay": .10, "plan_revision": .10})

    # 运行期填充：本轮的单轮日志文件名，写进 sync_runs.log_file 供 status 反查
    run_log_name: str = ""

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/") + "/onesite-api"

    @property
    def referer(self) -> str:
        return self.base_url.rstrip("/") + "/onesite/npp/infor/integrate"


def _get(env: dict[str, str], key: str, default: str = "") -> str:
    """环境变量优先，其次 .env 文件。"""
    return os.environ.get(key) or env.get(key.upper()) or default


def load_config(env_path: Path | None = None) -> Config:
    env_path = env_path or (BASE_DIR / ".env")
    env = _parse_env_file(env_path)

    token = ""
    for key in _TOKEN_KEYS:
        token = os.environ.get(key.replace("-", "_")) or env.get(key) or ""
        if token:
            break
    token = token.removeprefix("Bearer ").strip()

    def _path(key: str, default: Path) -> Path:
        raw = _get(env, key)
        return (BASE_DIR / raw).resolve() if raw else default

    def _num(key: str, default: float) -> float:
        raw = _get(env, key)
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    def _int(key: str, default: int) -> int:
        return int(_num(key, default))

    def _bool(key: str, default: bool) -> bool:
        raw = _get(env, key).strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on", "y")

    cfg = Config(
        token=token,
        base_url=_get(env, "BASE_URL", "https://www.npedi.com"),
        page_size=_int("PAGE_SIZE", 200),
        compare_window_overlap_hours=_num("COMPARE_WINDOW_OVERLAP_HOURS", 2.0),
        compare_window_manual=_get(env, "COMPARE_WINDOW_MANUAL"),
        request_delay=_parse_delay(_get(env, "REQUEST_DELAY_MS", "500-1000")),
        db_path=_path("DB_PATH", BASE_DIR / "npedi.sqlite"),
        export_dir=_path("EXPORT_DIR", BASE_DIR / "export"),
        log_dir=_path("LOG_DIR", BASE_DIR / "logs"),
        alert_file=_path("ALERT_FILE", BASE_DIR / "ALERT_TOKEN_EXPIRED"),
        timeout_seconds=_num("TIMEOUT_SECONDS", 60.0),
        max_retries=_int("MAX_RETRIES", 3),
        auth_probe=_bool("AUTH_PROBE", True),
        env_path=env_path.resolve(),
        auto_login=_bool("AUTO_LOGIN", False),
        npedi_mobile=_get(env, "NPEDI_MOBILE"),
        captcha_solver_command=_get(env, "CAPTCHA_SOLVER_COMMAND"),
        captcha_solver_timeout_seconds=_num("CAPTCHA_SOLVER_TIMEOUT_SECONDS", 30.0),
        captcha_attempts=_int("CAPTCHA_ATTEMPTS", 3),
        otp_reader_backend=_get(env, "OTP_READER_BACKEND", "telegram").lower(),
        telegram_reader_bot_token=_get(env, "TELEGRAM_READER_BOT_TOKEN"),
        telegram_chat_id=_get(env, "TELEGRAM_CHAT_ID"),
        telegram_sms_sender_bot_id=_get(env, "TELEGRAM_SMS_SENDER_BOT_ID"),
        temp_mail_base_url=_get(env, "TEMP_MAIL_BASE_URL"),
        temp_mail_address_jwt=_get(env, "TEMP_MAIL_ADDRESS_JWT"),
        temp_mail_site_password=_get(env, "TEMP_MAIL_SITE_PASSWORD"),
        temp_mail_recipient=_get(env, "TEMP_MAIL_RECIPIENT"),
        temp_mail_allowed_sender=_get(env, "TEMP_MAIL_ALLOWED_SENDER", "support@neofantasy.online"),
        temp_mail_required_text=_get(env, "TEMP_MAIL_REQUIRED_TEXT", "宁波舟山港"),
        temp_mail_required_copies=_int("TEMP_MAIL_REQUIRED_COPIES", 2),
        temp_mail_poll_seconds=_num("TEMP_MAIL_POLL_SECONDS", 3.0),
        sms_code_pattern=_get(env, "SMS_CODE_PATTERN", r"(?<!\d)(\d{4,8})(?!\d)"),
        sms_code_timeout_seconds=_num("SMS_CODE_TIMEOUT_SECONDS", 180.0),
        future_margin_hours=_num("FUTURE_MARGIN_HOURS", 24.0),
        active_past_days=_int("ACTIVE_PAST_DAYS", 7),
        active_future_days=_int("ACTIVE_FUTURE_DAYS", 14),
        active_unknown_close_days=_int("ACTIVE_UNKNOWN_CLOSE_DAYS", 14),
        max_new_voyage_backfill_per_run=_int("MAX_NEW_VOYAGE_BACKFILL_PER_RUN", 50),
        reconcile_max_voyages=_int("RECONCILE_MAX_VOYAGES", 200),
        empty_compare_fallback=_get(env, "EMPTY_COMPARE_FALLBACK", "auto").lower(),
        gate_page_size=_int("GATE_PAGE_SIZE", 100),
        gate_backfill_max_requests=_int("GATE_BACKFILL_MAX_REQUESTS", 5000),
        gate_inactive_rounds=_int("GATE_INCREMENTAL_INACTIVE_ROUNDS", 6),
        gate_active_days=_int("GATE_ACTIVE_DAYS", 14),
        gate_new_voyage_limit=_int("GATE_NEW_VOYAGE_LIMIT", 200),
        gate_export_scope=_get(env, "GATE_EXPORT_SCOPE", "all").lower(),
        csv_timestamp_format=_get(env, "CSV_TIMESTAMP_FORMAT", "iso").lower(),
        csv_keep_change_files=_int("CSV_KEEP_CHANGE_FILES", 90),
        log_keep_run_files=_int("LOG_KEEP_RUN_FILES", 90),
        spi_weights={"vgm": _num("SPI_VGM_WEIGHT", .30), "cargo_release": _num("SPI_RELEASE_WEIGHT", .20), "transshipment": _num("SPI_TRANSSHIPMENT_WEIGHT", .15), "arrival_delay": _num("SPI_ARRIVAL_DELAY_WEIGHT", .15), "departure_delay": _num("SPI_DEPARTURE_DELAY_WEIGHT", .10), "plan_revision": _num("SPI_PLAN_REVISION_WEIGHT", .10)},
    )
    return cfg
