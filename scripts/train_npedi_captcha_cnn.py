"""Train the NPEDI fixed-length CNN from locally labeled images."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from captcha_cnn.model import (
    CnnConfig,
    WholeCaptchaAccuracy,
    build_model,
    iter_labels,
    load_labeled_dataset,
    split_indices,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "captcha_cnn" / "npedi.json")
    args = parser.parse_args()

    import tensorflow as tf

    config = CnnConfig.load(args.config)
    images, labels, paths = load_labeled_dataset(config)
    train_indices, validation_indices = split_indices(len(paths), config)
    if len(train_indices) < 2:
        raise ValueError("not enough training images after validation split")

    model = build_model(config)
    if config.model_weights.exists():
        model.load_weights(config.model_weights)
        print(f"Continuing from {config.model_weights}")
    config.model_weights.parent.mkdir(parents=True, exist_ok=True)
    validation_labels = iter_labels([paths[index] for index in validation_indices], config)
    exact_accuracy = WholeCaptchaAccuracy.create(images[validation_indices], validation_labels, config)
    callbacks = [
        exact_accuracy,
        tf.keras.callbacks.ModelCheckpoint(
            filepath=config.model_weights,
            save_weights_only=True,
            save_best_only=True,
            monitor="val_whole_captcha_accuracy",
            mode="max",
        ),
    ]
    print(f"Training images: {len(train_indices)}; validation images: {len(validation_indices)}")
    model.fit(
        images[train_indices],
        labels[train_indices],
        batch_size=config.batch_size,
        epochs=config.epochs,
        validation_data=(images[validation_indices], labels[validation_indices]),
        callbacks=callbacks,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
