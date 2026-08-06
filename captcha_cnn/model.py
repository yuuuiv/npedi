"""Fixed-length CAPTCHA CNN adapted for NPEDI.

Architecture reference:
https://github.com/anexplore/cnn_for_captcha/tree/02bfba9c2767ab4842ff8baf45d806fa4cddea3e

The upstream project is Apache-2.0 licensed.  This implementation is adapted
for current TensorFlow/Keras, resolves paths relative to its JSON config, and
reports whole-CAPTCHA validation accuracy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


UPSTREAM_COMMIT = "02bfba9c2767ab4842ff8baf45d806fa4cddea3e"


@dataclass(frozen=True)
class CnnConfig:
    image_height: int
    image_width: int
    fixed_length: int
    labels: str
    model_weights: Path
    labeled_image_dir: Path
    batch_size: int = 128
    learning_rate: float = 0.0001
    dropout_rate: float = 0.25
    epochs: int = 100
    validation_fraction: float = 0.2
    random_seed: int = 20260805

    @classmethod
    def load(cls, path: str | Path) -> "CnnConfig":
        config_path = Path(path).resolve()
        data = json.loads(config_path.read_text(encoding="utf-8"))

        def relative(name: str) -> Path:
            value = Path(str(data[name]))
            return value if value.is_absolute() else (config_path.parent / value).resolve()

        config = cls(
            image_height=int(data["image_height"]),
            image_width=int(data["image_width"]),
            fixed_length=int(data["fixed_length"]),
            labels=str(data["labels"]),
            model_weights=relative("model_weights"),
            labeled_image_dir=relative("labeled_image_dir"),
            batch_size=int(data.get("batch_size", 128)),
            learning_rate=float(data.get("learning_rate", 0.0001)),
            dropout_rate=float(data.get("dropout_rate", 0.25)),
            epochs=int(data.get("epochs", 100)),
            validation_fraction=float(data.get("validation_fraction", 0.2)),
            random_seed=int(data.get("random_seed", 20260805)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.image_height < 16 or self.image_width < 16:
            raise ValueError("captcha image dimensions are too small")
        if self.fixed_length < 1:
            raise ValueError("fixed_length must be positive")
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("labels must contain unique characters")
        if not 0 < self.validation_fraction < 1:
            raise ValueError("validation_fraction must be between 0 and 1")


def normalize_answer(answer: str, config: CnnConfig) -> str:
    answer = answer.strip().upper()
    if len(answer) != config.fixed_length or any(char not in config.labels for char in answer):
        raise ValueError(
            f"answer must contain exactly {config.fixed_length} characters from the configured label set"
        )
    return answer


def label_from_path(path: Path, config: CnnConfig) -> str:
    label = path.stem.split("_", 1)[0]
    return normalize_answer(label, config)


def build_model(config: CnnConfig) -> Any:
    """Build a character CNN shared by all four CAPTCHA positions.

    Training expands each CAPTCHA into four overlapping character crops before
    calling this model.  This gives the optimiser four times as many samples
    and avoids a large position-specific fully-connected output layer.
    """
    import tensorflow as tf

    keras = tf.keras
    crop_width = character_crop_width(config)
    model = keras.Sequential(
        [
            keras.Input(shape=(config.image_height, crop_width, 1)),
            keras.layers.RandomContrast(0.10),
            keras.layers.RandomTranslation(0.05, 0.05, fill_mode="nearest"),
            keras.layers.Conv2D(24, (3, 3), activation="relu", padding="same"),
            keras.layers.MaxPooling2D((2, 2)),
            keras.layers.Conv2D(48, (3, 3), activation="relu", padding="same"),
            keras.layers.MaxPooling2D((2, 2)),
            keras.layers.Conv2D(64, (3, 3), activation="relu", padding="same"),
            keras.layers.MaxPooling2D((2, 2)),
            keras.layers.Flatten(),
            keras.layers.Dense(128, activation="relu"),
            keras.layers.Dropout(config.dropout_rate),
            keras.layers.Dense(len(config.labels), activation="softmax"),
        ],
        name="npedi_captcha_character_cnn",
    )
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=config.learning_rate),
        loss="categorical_crossentropy",
        metrics=["categorical_accuracy"],
    )
    return model


def character_crop_width(config: CnnConfig) -> int:
    """Return a slot width with enough overlap for slanted glyphs."""
    return max(24, round(config.image_width / config.fixed_length) + 8)


def extract_character_crops(images: Any, config: CnnConfig) -> Any:
    """Expand N full images into N*fixed_length fixed-size character crops."""
    import numpy as np

    source = np.asarray(images, dtype="float32")
    if source.ndim == 3:
        source = source[np.newaxis, ...]
    expected = (config.image_height, config.image_width, 1)
    if source.ndim != 4 or tuple(source.shape[1:]) != expected:
        raise ValueError(f"captcha array must have shape (N, {expected[0]}, {expected[1]}, 1)")
    width = character_crop_width(config)
    half = width // 2
    padded = np.pad(source, ((0, 0), (0, 0), (half, half), (0, 0)), mode="edge")
    crops = []
    for image in range(len(source)):
        for position in range(config.fixed_length):
            center = round((position + 0.5) * config.image_width / config.fixed_length)
            start = center - half + half
            crops.append(padded[image, :, start:start + width, :])
    return np.stack(crops)


def _one_hot(label: str, config: CnnConfig) -> Any:
    import numpy as np

    result = np.zeros((config.fixed_length, len(config.labels)), dtype="float32")
    for position, character in enumerate(label):
        result[position, config.labels.index(character)] = 1.0
    return result


def _read_image(path: Path, config: CnnConfig) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.size != (config.image_width, config.image_height):
            raise ValueError(
                f"{path.name} is {image.width}x{image.height}; expected "
                f"{config.image_width}x{config.image_height}"
            )
        grayscale = image.convert("L")
        return (np.asarray(grayscale, dtype="float32") / 255.0).reshape(
            config.image_height, config.image_width, 1
        )


def load_labeled_dataset(config: CnnConfig) -> tuple[Any, Any, list[Path]]:
    import numpy as np

    extensions = {".jpg", ".jpeg", ".png"}
    paths = sorted(
        path for path in config.labeled_image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    ) if config.labeled_image_dir.exists() else []
    if len(paths) < 10:
        raise ValueError("at least 10 labeled CAPTCHA images are required")
    images = np.stack([_read_image(path, config) for path in paths])
    labels = np.stack([_one_hot(label_from_path(path, config), config) for path in paths])
    return images, labels, paths


def decode_predictions(predictions: Any, config: CnnConfig) -> list[str]:
    import numpy as np

    reshaped = np.asarray(predictions).reshape(-1, config.fixed_length, len(config.labels))
    return ["".join(config.labels[index] for index in row.argmax(axis=1)) for row in reshaped]


def split_indices(count: int, config: CnnConfig) -> tuple[Any, Any]:
    import numpy as np

    rng = np.random.default_rng(config.random_seed)
    indices = rng.permutation(count)
    validation_count = max(1, int(round(count * config.validation_fraction)))
    return indices[validation_count:], indices[:validation_count]


class WholeCaptchaAccuracy:
    """Small callback factory kept lazy so non-CNN tests do not import TensorFlow."""

    @staticmethod
    def create(validation_x: Any, validation_labels: list[str], config: CnnConfig) -> Any:
        import tensorflow as tf

        class Callback(tf.keras.callbacks.Callback):
            def on_epoch_end(self, epoch: int, logs: dict[str, Any] | None = None) -> None:
                predictions = self.model.predict(validation_x, verbose=0)
                decoded = decode_predictions(predictions, config)
                correct = sum(actual == predicted for actual, predicted in zip(validation_labels, decoded))
                accuracy = correct / len(validation_labels)
                if logs is not None:
                    logs["val_whole_captcha_accuracy"] = accuracy
                print(f" - val_whole_captcha_accuracy: {accuracy:.4f}")

        return Callback()


class Predictor:
    def __init__(self, config_path: str | Path):
        self.config = CnnConfig.load(config_path)
        if not self.config.model_weights.exists():
            raise FileNotFoundError(f"model weights not found: {self.config.model_weights}")
        self.model = build_model(self.config)
        self.model.load_weights(self.config.model_weights)

    def predict_bytes(self, image_content: bytes) -> str:
        import io
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_content)) as image:
            if image.size != (self.config.image_width, self.config.image_height):
                raise ValueError(
                    f"captcha is {image.width}x{image.height}; expected "
                    f"{self.config.image_width}x{self.config.image_height}"
                )
            array = (np.asarray(image.convert("L"), dtype="float32") / 255.0).reshape(
                1, self.config.image_height, self.config.image_width, 1
            )
        crops = extract_character_crops(array, self.config)
        return decode_predictions(self.model.predict(crops, verbose=0), self.config)[0]

    def predict_file(self, path: str | Path) -> str:
        return self.predict_bytes(Path(path).read_bytes())


def iter_labels(paths: Iterable[Path], config: CnnConfig) -> list[str]:
    return [label_from_path(path, config) for path in paths]
