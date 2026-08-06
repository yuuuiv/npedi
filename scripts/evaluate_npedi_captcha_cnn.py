"""Evaluate the saved CNN on the deterministic held-out validation split."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from captcha_cnn.model import (
    CnnConfig,
    build_model,
    decode_predictions,
    extract_character_crops,
    iter_labels,
    load_labeled_dataset,
    split_indices,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "captcha_cnn" / "npedi.json")
    args = parser.parse_args()
    config = CnnConfig.load(args.config)
    if not config.model_weights.is_file():
        parser.error(f"weights do not exist: {config.model_weights}")
    images, _, paths = load_labeled_dataset(config)
    _, validation_indices = split_indices(len(paths), config)
    validation_paths = [paths[index] for index in validation_indices]
    expected = iter_labels(validation_paths, config)
    model = build_model(config)
    model.load_weights(config.model_weights)
    validation_x = extract_character_crops(images[validation_indices], config)
    predicted = decode_predictions(model.predict(validation_x, verbose=0), config)
    whole_correct = sum(a == b for a, b in zip(expected, predicted))
    character_correct = sum(a == b for actual, guess in zip(expected, predicted) for a, b in zip(actual, guess))
    errors = [
        {"file": path.name, "expected": actual, "predicted": guess}
        for path, actual, guess in zip(validation_paths, expected, predicted)
        if actual != guess
    ]
    print(json.dumps({
        "validation_images": len(expected),
        "whole_correct": whole_correct,
        "whole_accuracy": round(whole_correct / len(expected), 6),
        "character_accuracy": round(character_correct / (len(expected) * config.fixed_length), 6),
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
