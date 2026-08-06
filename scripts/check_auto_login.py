"""Offline readiness checks for NPEDI automatic login."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from captcha_cnn.model import CnnConfig
from config import load_config


def main() -> int:
    cfg = load_config()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("NPEDI mobile", bool(re.fullmatch(r"\d{6,20}", cfg.npedi_mobile)), "set NPEDI_MOBILE"))
    checks.append(("OTP reader backend", cfg.otp_reader_backend in {"temp_mail", "telegram"}, "set OTP_READER_BACKEND"))
    if cfg.otp_reader_backend == "temp_mail":
        checks.append(("temp-mail base URL", bool(re.fullmatch(r"https?://\S+", cfg.temp_mail_base_url)), "set TEMP_MAIL_BASE_URL"))
        checks.append(("temp-mail Address JWT", len(cfg.temp_mail_address_jwt) >= 20, "set TEMP_MAIL_ADDRESS_JWT"))
        checks.append(("temp-mail recipient", bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cfg.temp_mail_recipient)), "set TEMP_MAIL_RECIPIENT"))
        checks.append(("temp-mail sender filter", bool(cfg.temp_mail_allowed_sender), "set TEMP_MAIL_ALLOWED_SENDER"))
        checks.append(("temp-mail NPEDI marker", bool(cfg.temp_mail_required_text), "set TEMP_MAIL_REQUIRED_TEXT"))
        checks.append(("duplicate mail confirmation", cfg.temp_mail_required_copies == 2, "set TEMP_MAIL_REQUIRED_COPIES=2"))
    elif cfg.otp_reader_backend == "telegram":
        checks.append(("Telegram Bot B token", bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", cfg.telegram_reader_bot_token)), "set TELEGRAM_READER_BOT_TOKEN"))
        checks.append(("Telegram chat ID", bool(re.fullmatch(r"-?\d+", cfg.telegram_chat_id)), "set TELEGRAM_CHAT_ID"))
        checks.append(("SmsForwarder Bot A ID", bool(re.fullmatch(r"\d+", cfg.telegram_sms_sender_bot_id)), "set TELEGRAM_SMS_SENDER_BOT_ID"))

    argv = shlex.split(cfg.captcha_solver_command, posix=False) if cfg.captcha_solver_command else []
    executable = PROJECT_ROOT / argv[0] if argv else Path()
    checks.append(("captcha solver command", bool(argv) and executable.is_file(), "run scripts/setup_npedi_captcha_cnn.ps1"))

    cnn = CnnConfig.load(PROJECT_ROOT / "captcha_cnn" / "npedi.json")
    labeled_count = len(list(cnn.labeled_image_dir.glob("*.jpg"))) if cnn.labeled_image_dir.exists() else 0
    checks.append(("labeled CAPTCHA images", labeled_count >= 20, f"currently {labeled_count}; label at least a starter batch"))
    checks.append(("trained CNN weights", cnn.model_weights.is_file(), "train captcha-model/npedi.weights.h5"))

    for name, ok, action in checks:
        print(f"[{'OK' if ok else 'MISSING'}] {name}" + ("" if ok else f" - {action}"))
    print(f"[{'ON' if cfg.auto_login else 'OFF'}] AUTO_LOGIN")
    if all(ok for _, ok, _ in checks):
        print("Offline checks passed. Test Telegram, then run the explicit SMS login test.")
        return 0
    print("Automatic login is not ready; keep AUTO_LOGIN=false.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
