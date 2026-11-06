#!/usr/bin/env python3
"""Train and time-validate a 10-minute soil-moisture forecast."""

from __future__ import annotations

import argparse
import bisect
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sys
from typing import Any


FEATURES = (
    "soil_moisture_pct",
    "temp_c",
    "air_humidity_pct",
    "light_pct",
    "fan_on",
    "pump_on",
    "lamp_on",
    "auto_mode",
    "servo_angle_deg",
    "schedule_enabled",
    "water_hour",
    "water_minute",
    "water_duration_min",
)
REQUIRED_COLUMNS = ("timestamp_utc", *FEATURES)
INTEGER_FIELDS = {
    "soil_moisture_pct",
    "light_pct",
    "fan_on",
    "pump_on",
    "lamp_on",
    "auto_mode",
    "servo_angle_deg",
    "schedule_enabled",
    "water_hour",
    "water_minute",
    "water_duration_min",
}


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    features: tuple[float, ...]
    soil_moisture: float


@dataclass(frozen=True)
class Example:
    timestamp: datetime
    target_timestamp: datetime
    features: tuple[float, ...]
    target: float


def _parse_timestamp(value: str, line_number: int) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"CSV line {line_number}: timestamp_utc is required")
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"CSV line {line_number}: invalid timestamp_utc: {value!r}") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"CSV line {line_number}: timestamp_utc must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _parse_value(row: dict[str, str], key: str, line_number: int) -> float:
    try:
        value = float(row[key])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"CSV line {line_number}: {key} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"CSV line {line_number}: {key} must be finite")
    limits = {
        "soil_moisture_pct": (0, 100),
        "temp_c": (-40, 80),
        "air_humidity_pct": (0, 100),
        "light_pct": (0, 100),
        "fan_on": (0, 1),
        "pump_on": (0, 1),
        "lamp_on": (0, 1),
        "auto_mode": (0, 1),
        "servo_angle_deg": (0, 180),
        "schedule_enabled": (0, 1),
        "water_hour": (0, 23),
        "water_minute": (0, 59),
        "water_duration_min": (1, 120),
    }
    minimum, maximum = limits[key]
    if not minimum <= value <= maximum:
        raise ValueError(
            f"CSV line {line_number}: {key} must be between {minimum} and {maximum}"
        )
    if key in INTEGER_FIELDS and not value.is_integer():
        expected = "0 or 1" if key.endswith("_on") or key in {"auto_mode", "schedule_enabled"} else "an integer"
        raise ValueError(f"CSV line {line_number}: {key} must be {expected}")
    return value


def load_samples(csv_path: Path) -> list[Sample]:
    try:
        stream = csv_path.open("r", newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Cannot open input CSV {csv_path}: {exc}") from exc

    samples: list[Sample] = []
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
            timestamp = _parse_timestamp(row.get("timestamp_utc", ""), line_number)
            values = {key: _parse_value(row, key, line_number) for key in FEATURES}
            samples.append(
                Sample(
                    timestamp=timestamp,
                    features=tuple(values[key] for key in FEATURES),
                    soil_moisture=values["soil_moisture_pct"],
                )
            )
    if not samples:
        raise ValueError("CSV contains no telemetry rows")
    samples.sort(key=lambda sample: sample.timestamp)
    for previous, current in zip(samples, samples[1:]):
        if previous.timestamp == current.timestamp:
            raise ValueError(f"Duplicate telemetry timestamp: {current.timestamp.isoformat()}")
    return samples


def make_examples(
    samples: list[Sample],
    horizon: timedelta,
    tolerance: timedelta,
    sample_interval: timedelta,
) -> list[Example]:
    timestamps = [sample.timestamp for sample in samples]
    examples: list[Example] = []
    last_selected: datetime | None = None
    for sample in samples:
        if last_selected is not None and sample.timestamp - last_selected < sample_interval:
            continue
        desired = sample.timestamp + horizon
        position = bisect.bisect_left(timestamps, desired)
        candidate_indices = [
            index for index in (position - 1, position)
            if 0 <= index < len(samples)
        ]
        candidates = [
            samples[index]
            for index in candidate_indices
            if samples[index].timestamp > sample.timestamp
        ]
        if not candidates:
            continue
        target_sample = min(candidates, key=lambda candidate: abs(candidate.timestamp - desired))
        if abs(target_sample.timestamp - desired) > tolerance:
            continue
        examples.append(
            Example(
                timestamp=sample.timestamp,
                target_timestamp=target_sample.timestamp,
                features=sample.features,
                target=target_sample.soil_moisture,
            )
        )
        last_selected = sample.timestamp
    return examples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train and time-validate a model for soil moisture 10 minutes ahead."
    )
    parser.add_argument("csv_file", type=Path, help="CSV produced by collect_telemetry.py")
    parser.add_argument("--horizon-minutes", type=float, default=10)
    parser.add_argument("--tolerance-seconds", type=float, default=30)
    parser.add_argument("--sample-interval-seconds", type=float, default=60)
    parser.add_argument("--min-train-rows", type=int, default=100)
    parser.add_argument("--min-validation-rows", type=int, default=20)
    args = parser.parse_args()
    for name in ("horizon_minutes", "tolerance_seconds", "sample_interval_seconds"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be a positive number")
    for name in ("min_train_rows", "min_validation_rows"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        samples = load_samples(args.csv_file)
        examples = make_examples(
            samples,
            timedelta(minutes=args.horizon_minutes),
            timedelta(seconds=args.tolerance_seconds),
            timedelta(seconds=args.sample_interval_seconds),
        )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Data error: {exc}", file=sys.stderr)
        return 2

    if len(examples) < args.min_train_rows + args.min_validation_rows:
        print(
            f"Insufficient labeled data: found {len(examples)} examples "
            f"({len(samples)} telemetry rows); need at least "
            f"{args.min_train_rows + args.min_validation_rows}. "
            "Collect more real telemetry or review the horizon/tolerance settings.",
            file=sys.stderr,
        )
        return 2

    split_index = int(len(examples) * 0.8)
    if split_index <= 0 or split_index >= len(examples):
        print("Insufficient data for a chronological train/validation split.", file=sys.stderr)
        return 2
    validation_start = examples[split_index].timestamp
    train = [example for example in examples if example.target_timestamp < validation_start]
    validation = [example for example in examples if example.timestamp >= validation_start]
    if len(train) < args.min_train_rows or len(validation) < args.min_validation_rows:
        print(
            "Insufficient data after time-based split and 10-minute leakage gap: "
            f"{len(train)} training, {len(validation)} validation examples; need at least "
            f"{args.min_train_rows} and {args.min_validation_rows}. "
            "Collect more real telemetry.",
            file=sys.stderr,
        )
        return 2

    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.metrics import mean_absolute_error, mean_squared_error
    except ImportError:
        print(
            "Error: scikit-learn is missing. Install dependencies with "
            "'python -m pip install -r requirements.txt'.",
            file=sys.stderr,
        )
        return 2

    model = RandomForestRegressor(
        n_estimators=200,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1,
    )
    model.fit([example.features for example in train], [example.target for example in train])
    actual = [example.target for example in validation]
    predicted = model.predict([example.features for example in validation])
    persistence = [example.features[0] for example in validation]
    model_mae = mean_absolute_error(actual, predicted)
    baseline_mae = mean_absolute_error(actual, persistence)
    model_rmse = math.sqrt(mean_squared_error(actual, predicted))
    baseline_rmse = math.sqrt(mean_squared_error(actual, persistence))

    print(f"Input telemetry rows: {len(samples)}")
    print(f"Labeled examples (one every ~{args.sample_interval_seconds:g}s): {len(examples)}")
    print(
        f"Time-based split: {len(train)} train / {len(validation)} validation; "
        f"validation starts {validation_start.isoformat()}"
    )
    print(
        f"Forecast horizon: {args.horizon_minutes:g} minutes "
        f"(nearest future sample within {args.tolerance_seconds:g}s)"
    )
    print(f"Model: RandomForestRegressor; validation MAE: {model_mae:.3f} percentage points")
    print(f"Persistence baseline: validation MAE: {baseline_mae:.3f} percentage points")
    print(f"Validation RMSE: model {model_rmse:.3f}; persistence {baseline_rmse:.3f} percentage points")
    if baseline_mae > 0:
        change = 100 * (baseline_mae - model_mae) / baseline_mae
        direction = "better" if change > 0 else "worse" if change < 0 else "equal to"
        print(f"Model MAE is {abs(change):.1f}% {direction} than persistence on this holdout.")
    else:
        print("Baseline MAE is zero; relative improvement is undefined.")
    print("Predictions are for offline evaluation only; they do not control the greenhouse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
