"""Validate the shared labeled CAPTCHA dataset before committing or training."""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from captcha_cnn.model import CnnConfig

EXTENSIONS = {".jpg", ".jpeg", ".png"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "captcha_cnn" / "npedi.json")
    parser.add_argument("--min-per-character", type=int, default=20, help="warn below this sample count")
    args = parser.parse_args()

    from PIL import Image

    config = CnnConfig.load(args.config)
    paths = sorted(
        path for path in config.labeled_image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in EXTENSIONS
    ) if config.labeled_image_dir.exists() else []

    problems: list[str] = []
    by_uuid: dict[str, set[str]] = collections.defaultdict(set)
    character_counts: collections.Counter[str] = collections.Counter()

    for path in paths:
        label, separator, uuid = path.stem.partition("_")
        if not separator or not uuid:
            problems.append(f"{path.name}: expected <label>_<uuid>{path.suffix}")
            continue
        if len(label) != config.fixed_length or any(char not in config.labels for char in label):
            problems.append(
                f"{path.name}: label {label!r} is not {config.fixed_length} characters from {config.labels}"
            )
            continue
        by_uuid[uuid].add(label)
        character_counts.update(label)
        with Image.open(path) as image:
            if image.size != (config.image_width, config.image_height):
                problems.append(
                    f"{path.name}: {image.width}x{image.height}, expected "
                    f"{config.image_width}x{config.image_height}"
                )

    # 多人并行标注时同一张图（uuid 相同）可能被标成不同答案，必须人工裁决后删掉错的那份。
    conflicts = {uuid: sorted(labels) for uuid, labels in by_uuid.items() if len(labels) > 1}
    for uuid, labels in sorted(conflicts.items()):
        problems.append(f"{uuid}: conflicting labels {labels}")

    missing = [character for character in config.labels if character not in character_counts]
    thin = sorted(
        (character, count)
        for character, count in character_counts.items()
        if count < args.min_per_character
    )

    print(json.dumps({
        "labeled_image_dir": str(config.labeled_image_dir),
        "images": len(paths),
        "unique_captchas": len(by_uuid),
        "conflicting_captchas": len(conflicts),
        "characters_never_seen": missing,
        "characters_below_minimum": [{"character": c, "count": n} for c, n in thin],
        "problems": problems,
    }, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
