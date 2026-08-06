"""Interactively store automatic-login settings without echoing secrets."""
from __future__ import annotations

import getpass
import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import update_env_values
from config import _parse_env_file


def _prompt(label: str, current: str, *, secret: bool = False) -> str:
    suffix = " [already configured; Enter keeps it]: " if current else ": "
    reader = getpass.getpass if secret else input
    value = reader(label + suffix).strip()
    return value or current


def _require(pattern: str, value: str, label: str) -> str:
    if not re.fullmatch(pattern, value):
        raise SystemExit(f"Invalid {label}; configuration was not changed.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mail-only", action="store_true", help="configure the OTP mailbox without asking for NPEDI mobile")
    parser.add_argument("--backend", choices=("temp_mail", "telegram"), help="skip the backend selection prompt")
    args = parser.parse_args()
    env_path = PROJECT_ROOT / ".env"
    existing = _parse_env_file(env_path)
    print("Configure NPEDI automatic login locally. Secrets will not be echoed.")
    mobile = ""
    if not args.mail_only:
        mobile = _require(
            r"\d{6,20}", _prompt("NPEDI mobile number", existing.get("NPEDI_MOBILE", "")), "mobile number"
        )
    backend = args.backend or _prompt(
            "OTP reader backend (temp_mail/telegram)",
            existing.get("OTP_READER_BACKEND", "temp_mail"),
        ).lower()
    if backend not in {"temp_mail", "telegram"}:
        raise SystemExit("Invalid OTP reader backend; configuration was not changed.")
    values = {
        "AUTO_LOGIN": "false",
        "OTP_READER_BACKEND": backend,
        "CAPTCHA_SOLVER_COMMAND": r".captcha-cnn-venv\Scripts\python.exe scripts\solve_npedi_captcha_cnn.py",
    }
    if mobile:
        values["NPEDI_MOBILE"] = mobile
    if backend == "temp_mail":
        base_url = _require(
            r"https?://[^\s/]+(?:/[^\s]*)?",
            _prompt("Temp-mail Worker base URL", existing.get("TEMP_MAIL_BASE_URL", "")),
            "temp-mail base URL",
        ).rstrip("/")
        address_jwt = _prompt(
            "Temp-mail Address JWT", existing.get("TEMP_MAIL_ADDRESS_JWT", ""), secret=True
        )
        if len(address_jwt) < 20:
            raise SystemExit("Invalid Address JWT; configuration was not changed.")
        recipient = _require(
            r"[^\s@]+@[^\s@]+\.[^\s@]+",
            _prompt("Forwarding recipient address", existing.get("TEMP_MAIL_RECIPIENT", "")),
            "recipient email",
        ).lower()
        site_password = _prompt(
            "Optional temp-mail site password",
            existing.get("TEMP_MAIL_SITE_PASSWORD", ""),
            secret=True,
        )
        values.update({
            "TEMP_MAIL_BASE_URL": base_url,
            "TEMP_MAIL_ADDRESS_JWT": address_jwt,
            "TEMP_MAIL_SITE_PASSWORD": site_password,
            "TEMP_MAIL_RECIPIENT": recipient,
            "TEMP_MAIL_ALLOWED_SENDER": existing.get("TEMP_MAIL_ALLOWED_SENDER", "support@neofantasy.online"),
            "TEMP_MAIL_REQUIRED_TEXT": existing.get("TEMP_MAIL_REQUIRED_TEXT", "宁波舟山港"),
            "TEMP_MAIL_REQUIRED_COPIES": existing.get("TEMP_MAIL_REQUIRED_COPIES", "2"),
            "TEMP_MAIL_POLL_SECONDS": existing.get("TEMP_MAIL_POLL_SECONDS", "3"),
        })
    else:
        reader_token = _require(
            r"\d+:[A-Za-z0-9_-]{20,}",
            _prompt("Telegram reader Bot B token", existing.get("TELEGRAM_READER_BOT_TOKEN", ""), secret=True),
            "Telegram Bot B token",
        )
        chat_id = _require(
            r"-?\d+", _prompt("Private group/channel numeric chat ID", existing.get("TELEGRAM_CHAT_ID", "")), "chat ID"
        )
        sender_id = _require(
            r"\d+", _prompt("SmsForwarder Bot A numeric ID", existing.get("TELEGRAM_SMS_SENDER_BOT_ID", "")), "Bot A ID"
        )
        values.update({
            "TELEGRAM_READER_BOT_TOKEN": reader_token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_SMS_SENDER_BOT_ID": sender_id,
        })
    update_env_values(env_path, values)
    print("Saved to .env with AUTO_LOGIN=false. No secret was printed.")
    print(r"Next: & .\.venv\Scripts\python.exe scripts\check_auto_login.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nConfiguration cancelled; .env was not changed.", file=sys.stderr)
        raise SystemExit(130)
