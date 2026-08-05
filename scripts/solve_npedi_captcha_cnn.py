"""Print a four-character NPEDI CAPTCHA answer for CommandCaptchaSolver."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from captcha_cnn.model import Predictor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("NPEDI_CAPTCHA_CNN_CONFIG", PROJECT_ROOT / "captcha_cnn" / "npedi.json")),
    )
    args = parser.parse_args()
    try:
        answer = Predictor(args.config).predict_file(args.image)
    except Exception as exc:
        print(f"captcha inference failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
