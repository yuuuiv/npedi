"""Encode the frame sequence into an animated WebP with real frame durations.

ffmpeg's libwebp_anim in this build writes no per-frame duration (Pillow reads
them back as None), so the file plays at whatever speed the viewer picks. Doing
it here means the durations are written explicitly and can be verified.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

REC = Path(__file__).resolve().parent / "record"
FRAMES = sorted((REC / "frames").glob("*.png"))
OUT = REC / "auto-login-demo.webp"

if not FRAMES:
    sys.exit("no frames extracted")

step = int(sys.argv[1]) if len(sys.argv) > 1 else 1
quality = int(sys.argv[2]) if len(sys.argv) > 2 else 80
picked = FRAMES[::step]
duration_ms = 100 * step  # frames were extracted at 10 fps

print(f"frames={len(picked)} (every {step}) duration={duration_ms}ms quality={quality}")

first = Image.open(picked[0]).convert("RGB")
rest = [Image.open(p).convert("RGB") for p in picked[1:]]

first.save(
    OUT,
    format="WEBP",
    save_all=True,
    append_images=rest,
    duration=duration_ms,
    loop=0,
    quality=quality,
    method=6,
    minimize_size=True,
)
for im in rest:
    im.close()
first.close()

check = Image.open(OUT)
durations = []
for i in range(check.n_frames):
    check.seek(i)
    durations.append(check.info.get("duration"))
total = sum(d for d in durations if d) / 1000
mb = OUT.stat().st_size / 1024 / 1024
print(f"wrote {OUT.name}  {check.size}  frames={check.n_frames}  "
      f"total={total:.1f}s  loop={check.info.get('loop')}  {mb:.2f} MB")
print(f"first durations: {durations[:5]}  (None means broken)")
