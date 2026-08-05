"""Verify the Telegram Bot B reader without requesting an NPEDI SMS."""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import AutoLoginError, TelegramOtpReader
from config import load_config


def main() -> int:
    config = load_config()
    reader = TelegramOtpReader(
        reader_bot_token=config.telegram_reader_bot_token,
        chat_id=config.telegram_chat_id,
        sender_bot_id=config.telegram_sms_sender_bot_id,
        code_pattern=config.sms_code_pattern,
        timeout_seconds=config.sms_code_timeout_seconds,
    )
    print("Waiting for a new test code sent by SmsForwarder Bot A...")
    try:
        code = reader.wait_for_code(not_before=int(time.time()) - 2)
    except AutoLoginError as exc:
        print(f"Telegram OTP test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        reader.close()
    print(f"Telegram OTP test succeeded; received a {len(code)}-digit code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
