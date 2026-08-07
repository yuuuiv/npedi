"""Read frame durations straight out of the WebP container.

Neither Pillow nor this ffmpeg build reports them, so parse the RIFF chunks:
each ANMF payload carries x, y, w-1, h-1 (3 bytes each) then a 24-bit little
endian duration in milliseconds.
"""
from __future__ import annotations

import sys
from pathlib import Path


def u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def scan(path: Path) -> None:
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        print(f"{path.name}: not a RIFF/WEBP file")
        return
    pos, durations, anim = 12, [], None
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if tag == b"ANIM":
            anim = int.from_bytes(body[4:6], "little")  # loop count
        elif tag == b"ANMF":
            durations.append(u24(body[12:15]))
        pos += 8 + size + (size & 1)
    total = sum(durations) / 1000
    uniq = sorted(set(durations))
    print(f"{path.name}")
    print(f"  frames={len(durations)}  loop_count={anim}  total={total:.2f}s")
    print(f"  distinct durations(ms)={uniq[:8]}{' …' if len(uniq) > 8 else ''}")
    print(f"  first 10={durations[:10]}")


for name in sys.argv[1:]:
    scan(Path(name))
