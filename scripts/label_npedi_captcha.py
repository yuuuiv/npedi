"""Interactively fetch and label a small, rate-limited NPEDI CAPTCHA batch."""
from __future__ import annotations

import argparse
import base64
import re
import time
import tkinter as tk
from pathlib import Path

import httpx
from PIL import Image, ImageTk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANSWER = re.compile(r"[0-9A-Z]{4}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20, help="number to review (1-200)")
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between requests (minimum 0.5)")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "captcha-data")
    parser.add_argument("--base-url", default="https://www.npedi.com")
    args = parser.parse_args()
    if not 1 <= args.count <= 200:
        parser.error("--count must be between 1 and 200")
    delay = max(0.5, args.delay)
    review_dir = args.output / "review"
    labeled_dir = args.output / "labeled"
    review_dir.mkdir(parents=True, exist_ok=True)
    labeled_dir.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.title("CAPTCHA review")
    preview = tk.Label(root)
    preview.pack()

    with httpx.Client(base_url=args.base_url.rstrip("/") + "/onesite-api", timeout=30) as client:
        for index in range(args.count):
            response = client.get("/captchaImage")
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            if payload.get("code") != 200 or not data.get("uuid") or not data.get("img"):
                raise RuntimeError("captchaImage returned an unexpected response")
            image = base64.b64decode(data["img"], validate=True)
            uuid = re.sub(r"[^A-Za-z0-9-]", "", str(data["uuid"]))
            preview_path = review_dir / f"{uuid}.jpg"
            preview_path.write_bytes(image)
            with Image.open(preview_path) as image_file:
                photo = ImageTk.PhotoImage(image_file.resize((333, 108), Image.NEAREST))
            preview.configure(image=photo)
            preview.image = photo
            root.update()
            print(f"[{index + 1}/{args.count}] Review {preview_path}")
            while True:
                answer = input("Label (4 uppercase letters/digits), s=skip, q=quit: ").strip().upper()
                if answer == "Q":
                    root.destroy()
                    return 0
                if answer == "S":
                    break
                if ANSWER.fullmatch(answer):
                    target = labeled_dir / f"{answer}_{uuid}.jpg"
                    target.write_bytes(image)
                    print(f"Saved {target.name}")
                    break
                print("Invalid label; expected exactly four A-Z/0-9 characters.")
            if index + 1 < args.count:
                time.sleep(delay)
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
