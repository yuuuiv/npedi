"""Wait for a new duplicated SmsForwarder email without requesting NPEDI SMS."""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import AutoLoginError, TempMailOtpReader
from config import load_config


def main() -> int:
    cfg = load_config()
    reader = TempMailOtpReader(
        base_url=cfg.temp_mail_base_url,
        address_jwt=cfg.temp_mail_address_jwt,
        site_password=cfg.temp_mail_site_password,
        recipient=cfg.temp_mail_recipient,
        allowed_sender=cfg.temp_mail_allowed_sender,
        required_text=cfg.temp_mail_required_text,
        required_copies=cfg.temp_mail_required_copies,
        code_pattern=cfg.sms_code_pattern,
        timeout_seconds=cfg.sms_code_timeout_seconds,
        poll_seconds=cfg.temp_mail_poll_seconds,
    )
    print("Waiting for two new matching SmsForwarder email copies...")
    try:
        code = reader.wait_for_code(not_before=int(time.time()) - 2)
    except AutoLoginError as exc:
        print(f"Temp-mail OTP test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        reader.close()
    print(f"Temp-mail OTP test succeeded; confirmed a duplicated {len(code)}-digit code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
