"""Perform one explicit NPEDI SMS login and optionally enable auto-login."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import AutoLoginError, build_authenticator, update_env_token, update_env_values
from config import load_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--request-sms",
        action="store_true",
        help="required acknowledgement: this run asks NPEDI to send one SMS",
    )
    parser.add_argument("--enable", action="store_true", help="set AUTO_LOGIN=true only after login succeeds")
    args = parser.parse_args()
    if not args.request_sms:
        parser.error("refusing to send an SMS without --request-sms")

    cfg = load_config()
    authenticator = None
    try:
        authenticator = build_authenticator(cfg)
        token = authenticator.login()
        update_env_token(cfg.env_path, token)
        if args.enable:
            update_env_values(cfg.env_path, {"AUTO_LOGIN": "true"})
    except (AutoLoginError, OSError, ValueError) as exc:
        print(f"Automatic login test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if authenticator is not None:
            authenticator.close()
    print("Automatic login succeeded; a fresh token was saved without being displayed.")
    print("AUTO_LOGIN is enabled." if args.enable else "AUTO_LOGIN remains disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
