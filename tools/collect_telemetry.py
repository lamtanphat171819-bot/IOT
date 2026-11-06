#!/usr/bin/env python3
"""Append validated greenhouse MQTT telemetry messages to a timestamped CSV."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any
import uuid


DEFAULT_BROKER = "broker.hivemq.com"
DEFAULT_TOPIC = "qhuy-greenhouse/data"
CSV_FIELDS = (
    "timestamp_utc",
    "temp_c",
    "air_humidity_pct",
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
)


def _number(
    payload: dict[str, Any],
    key: str,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
) -> int | float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key!r} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{key!r} must be between {minimum:g} and {maximum:g}")
    if integer and int(value) != value:
        raise ValueError(f"{key!r} must be an integer")
    return int(value) if integer else value


def _boolean(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if value not in (0, 1, False, True) or isinstance(value, str):
        raise ValueError(f"{key!r} must be 0 or 1")
    return int(value)


def validate_telemetry(payload: Any) -> dict[str, int | float]:
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")

    return {
        "temp_c": _number(payload, "temp", -40, 80),
        "air_humidity_pct": _number(payload, "hum", 0, 100),
        "soil_moisture_pct": _number(payload, "soil", 0, 100, integer=True),
        "light_pct": _number(payload, "light", 0, 100, integer=True),
        "fan_on": _boolean(payload, "fan"),
        "pump_on": _boolean(payload, "pump"),
        "lamp_on": _boolean(payload, "lamp"),
        "auto_mode": _boolean(payload, "auto"),
        "servo_angle_deg": _number(payload, "servo", 0, 180, integer=True),
        "schedule_enabled": _boolean(payload, "scheduleEnabled"),
        "water_hour": _number(payload, "waterHour", 0, 23, integer=True),
        "water_minute": _number(payload, "waterMinute", 0, 59, integer=True),
        "water_duration_min": _number(payload, "waterDuration", 1, 120, integer=True),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect ESP32 greenhouse telemetry into a timestamped CSV."
    )
    parser.add_argument("--broker", default=DEFAULT_BROKER, help="MQTT broker hostname")
    parser.add_argument("--port", type=int, default=1883, help="MQTT TCP port (default: 1883)")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="Telemetry topic")
    parser.add_argument(
        "--output", type=Path, default=Path("data/telemetry.csv"),
        help="CSV output path (default: data/telemetry.csv)",
    )
    parser.add_argument(
        "--client-id", default=f"greenhouse-collector-{uuid.uuid4().hex[:12]}"
    )
    args = parser.parse_args()
    if not args.broker.strip():
        parser.error("--broker must not be empty")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.topic.strip():
        parser.error("--topic must not be empty")
    if not args.client_id.strip():
        parser.error("--client-id must not be empty")
    return args


def main() -> int:
    args = parse_args()
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print(
            "Error: MQTT dependency is missing. Install it with "
            "'python -m pip install -r requirements.txt'.",
            file=sys.stderr,
        )
        return 2

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not args.output.exists() or args.output.stat().st_size == 0
        output_file = args.output.open("a", newline="", encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot open CSV output {args.output}: {exc}", file=sys.stderr)
        return 2

    writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
    try:
        if needs_header:
            writer.writeheader()
            output_file.flush()
    except OSError as exc:
        print(f"Error: cannot write CSV header to {args.output}: {exc}", file=sys.stderr)
        output_file.close()
        return 2

    def on_connect(
        client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code.is_failure:
            print(f"MQTT connection rejected: {reason_code}", file=sys.stderr, flush=True)
            return
        result, _ = client.subscribe(args.topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"Error: failed to subscribe to {args.topic!r}", file=sys.stderr, flush=True)
        else:
            print(f"Connected; collecting {args.topic!r} to {args.output}", flush=True)

    def on_message(client: Any, userdata: Any, message: Any) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            values = validate_telemetry(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"Skipped invalid message on {message.topic!r}: {exc}", file=sys.stderr, flush=True)
            return

        received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            writer.writerow({"timestamp_utc": received_at, **values})
            output_file.flush()
        except OSError as exc:
            print(f"Error: cannot write CSV row to {args.output}: {exc}", file=sys.stderr)
            client.disconnect()
            return
        print(f"Saved sample at {received_at}", flush=True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
    )
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.broker, args.port, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nCollector stopped.", flush=True)
    except OSError as exc:
        print(f"MQTT connection error: {exc}", file=sys.stderr)
        return 1
    except mqtt.MQTTException as exc:
        print(f"MQTT client error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.disconnect()
        output_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
