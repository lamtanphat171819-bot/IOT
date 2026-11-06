#!/usr/bin/env python3
"""Train an offline multi-label classifier for rule-defined greenhouse states."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import math
from pathlib import Path
import sys


FEATURES = ("temp_c", "air_humidity_pct", "soil_moisture_pct", "light_pct")
REQUIRED_COLUMNS = ("timestamp_utc", *FEATURES)
LABELS = ("hot", "dry_soil", "low_light")
LIMITS = {
    "temp_c": (-40, 80),
    "air_humidity_pct": (0, 100),
    "soil_moisture_pct": (0, 100),
    "light_pct": (0, 100),
}


def load_rows(csv_path: Path) -> tuple[list[str], list[list[float]]]:
    try:
        stream = csv_path.open("r", newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Cannot open input CSV {csv_path}: {exc}") from exc

    timestamps: list[str] = []
    features: list[list[float]] = []
    previous_timestamp: datetime | None = None
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames and len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV contains duplicate column names")
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"CSV line {line_number}: unexpected extra columns")
            timestamp = row.get("timestamp_utc", "")
            if not timestamp:
                raise ValueError(f"CSV line {line_number}: timestamp_utc is required")
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"CSV line {line_number}: invalid timestamp_utc: {timestamp!r}"
                ) from exc
            if parsed_timestamp.tzinfo is None:
                raise ValueError(f"CSV line {line_number}: timestamp_utc needs a timezone")
            if previous_timestamp is not None and parsed_timestamp <= previous_timestamp:
                raise ValueError(
                    f"CSV line {line_number}: timestamps must be strictly increasing"
                )
            previous_timestamp = parsed_timestamp
            try:
                values = [float(row[column]) for column in FEATURES]
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"CSV line {line_number}: sensor values must be numeric") from exc
            for column, value in zip(FEATURES, values):
                low, high = LIMITS[column]
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(
                        f"CSV line {line_number}: {column} must be finite and in [{low}, {high}]"
                    )
            timestamps.append(timestamp)
            features.append(values)
    if not features:
        raise ValueError("CSV contains no telemetry rows")
    return timestamps, features


def make_labels(features: list[list[float]]) -> list[list[int]]:
    labels: list[list[int]] = []
    for temp, _humidity, soil, light in features:
        labels.append([int(temp > 32), int(soil < 35), int(light < 30)])
    return labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train and time-validate a multi-label greenhouse state classifier."
    )
    parser.add_argument("csv_file", type=Path, help="CSV produced by collect_telemetry.py")
    parser.add_argument("--min-train-rows", type=int, default=20)
    parser.add_argument("--min-validation-rows", type=int, default=10)
    args = parser.parse_args()
    for name in ("min_train_rows", "min_validation_rows"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        timestamps, features = load_rows(args.csv_file)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Data error: {exc}", file=sys.stderr)
        return 2

    labels = make_labels(features)
    if len(features) < args.min_train_rows + args.min_validation_rows:
        print(
            f"Insufficient telemetry rows: found {len(features)}; need at least "
            f"{args.min_train_rows + args.min_validation_rows}.",
            file=sys.stderr,
        )
        return 2

    split_index = int(len(features) * 0.8)
    if split_index < args.min_train_rows or len(features) - split_index < args.min_validation_rows:
        print("Insufficient data for the requested chronological split.", file=sys.stderr)
        return 2

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, f1_score
        from sklearn.multioutput import MultiOutputClassifier
    except ImportError:
        print(
            "Error: scikit-learn is missing. Install it with "
            "'python -m pip install -r requirements.txt'.",
            file=sys.stderr,
        )
        return 2

    train_labels = labels[:split_index]
    validation_labels = labels[split_index:]
    missing_classes = []
    for index, name in enumerate(LABELS):
        if len({row[index] for row in train_labels}) < 2:
            missing_classes.append(f"{name} in training")
        if len({row[index] for row in validation_labels}) < 2:
            missing_classes.append(f"{name} in validation")
    if missing_classes:
        print(
            "Cannot evaluate all labels because both positive and negative examples are "
            "required in train and validation: " + ", ".join(missing_classes) + ". "
            "Collect data with varied conditions or use a different time period.",
            file=sys.stderr,
        )
        return 2

    model = MultiOutputClassifier(
        RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
    )
    model.fit(features[:split_index], train_labels)
    predictions: list[list[int]] = model.predict(features[split_index:]).tolist()
    exact_match = accuracy_score(validation_labels, predictions)

    print(f"Rows: {len(features)}; train: {split_index}; validation: {len(features) - split_index}")
    print(f"Validation period: {timestamps[split_index]} .. {timestamps[-1]}")
    print("Labels are weak labels copied from current firmware thresholds:")
    print("  hot: temperature > 32 C; dry_soil: soil < 35%; low_light: light < 30%.")
    print(f"Multi-label exact-match accuracy: {exact_match:.3f}")
    for index, name in enumerate(LABELS):
        actual = [row[index] for row in validation_labels]
        predicted = [row[index] for row in predictions]
        print(
            f"{name}: accuracy={accuracy_score(actual, predicted):.3f}, "
            f"F1={f1_score(actual, predicted, zero_division=0):.3f}, "
            f"support={sum(actual)}/{len(actual)} positive"
        )
    print(
        "This evaluates imitation of threshold-generated labels only, not independent "
        "agronomic truth. The model is offline and does not control devices."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
