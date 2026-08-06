"""Validate temp-mail read-only credentials without exposing mail contents."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import AutoLoginError, TempMailOtpReader
from config import load_config


def main() -> int:
    cfg = load_config()
    reader = None
    try:
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
        settings = reader._get_json("/api/settings")
        actual = str(settings.get("address") or "").lower() if isinstance(settings, dict) else ""
        if actual and actual != cfg.temp_mail_recipient.lower():
            raise AutoLoginError("Address JWT belongs to a different recipient")
        payload = reader._get_json("/api/parsed_mails", params={"limit": 1, "offset": 0})
        count = int(payload.get("count") or 0) if isinstance(payload, dict) else len(reader._messages(payload))
    except (AutoLoginError, OSError, ValueError) as exc:
        print(f"Temp-mail connection test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if reader is not None:
            reader.close()
    print(f"Temp-mail read-only connection succeeded; mailbox contains {count} message(s).")
    print("No sender, subject, body, JWT, or verification code was displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
