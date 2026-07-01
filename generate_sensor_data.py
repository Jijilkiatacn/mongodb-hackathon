"""
Synthetic sensor data generator for the Predictive Maintenance Agent demo.

Simulates 3 machines:
  - PUMP-01: stays normal the whole time
  - PUMP-02: drifts into a "bearing overheating" pattern halfway through
  - PUMP-03: stays normal, used as a control/comparison

Run this BEFORE the demo to seed history, then run with --live during
the demo to stream new readings in real time so judges can watch the
Monitoring Agent react.

Usage:
  python generate_sensor_data.py --seed-history     # backfill 30 min of normal data
  python generate_sensor_data.py --live             # stream 1 reading/sec, inject anomaly on PUMP-02
"""

import ssl_patch  # noqa: F401 — must precede pymongo import
import argparse
import os
import random
import time
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?appName=Cluster0")
DB_NAME = os.environ.get("MONGO_DB", "predictive_maintenance")

MACHINES = [
    {"machine_id": "PUMP-01", "machine_type": "centrifugal_pump", "location": "Plant-A-Floor2"},
    {"machine_id": "PUMP-02", "machine_type": "centrifugal_pump", "location": "Plant-A-Floor2"},
    {"machine_id": "PUMP-03", "machine_type": "centrifugal_pump", "location": "Plant-A-Floor1"},
]

BASELINE = {
    "temperature_c": 65.0,
    "vibration_mm_s": 2.0,
    "pressure_bar": 4.8,
    "rpm": 1450,
}


def normal_reading():
    """A reading with small random noise around baseline -- healthy machine."""
    return {
        "temperature_c": round(BASELINE["temperature_c"] + random.gauss(0, 1.0), 2),
        "vibration_mm_s": round(BASELINE["vibration_mm_s"] + random.gauss(0, 0.15), 2),
        "pressure_bar": round(BASELINE["pressure_bar"] + random.gauss(0, 0.1), 2),
        "rpm": round(BASELINE["rpm"] + random.gauss(0, 10)),
    }


def drifting_reading(drift_step, max_steps):
    """
    A reading that gradually drifts toward a bearing-overheating signature:
    temperature climbs, vibration climbs, pressure drops slightly, rpm dips.
    drift_step / max_steps controls how far into the failure pattern we are.
    """
    progress = min(drift_step / max_steps, 1.0)
    return {
        "temperature_c": round(BASELINE["temperature_c"] + progress * 22 + random.gauss(0, 1.0), 2),
        "vibration_mm_s": round(BASELINE["vibration_mm_s"] + progress * 4.5 + random.gauss(0, 0.15), 2),
        "pressure_bar": round(BASELINE["pressure_bar"] - progress * 0.6 + random.gauss(0, 0.1), 2),
        "rpm": round(BASELINE["rpm"] - progress * 80 + random.gauss(0, 10)),
    }


def make_document(machine, reading, ts):
    return {
        "timestamp": ts,
        "metadata": {
            "machine_id": machine["machine_id"],
            "machine_type": machine["machine_type"],
            "location": machine["location"],
        },
        **reading,
    }


def seed_history(collection, minutes=30, interval_seconds=5):
    """Backfill historical normal data for all machines so the agent has
    a baseline to compute rolling stats against before the live demo starts."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    docs = []

    steps = int((minutes * 60) / interval_seconds)
    for i in range(steps):
        ts = start + timedelta(seconds=i * interval_seconds)
        for machine in MACHINES:
            docs.append(make_document(machine, normal_reading(), ts))

    if docs:
        collection.insert_many(docs)
    print(f"Seeded {len(docs)} historical documents across {len(MACHINES)} machines.")


def run_live(collection, duration_seconds=240, anomaly_machine="PUMP-02", anomaly_start_fraction=0.4):
    """
    Streams one reading per machine per second for `duration_seconds`.
    Default 240s (4 min) fits neatly inside a live demo segment.
    The anomaly machine starts drifting at anomaly_start_fraction of the way through.
    """
    anomaly_start_step = int(duration_seconds * anomaly_start_fraction)
    drift_steps_total = duration_seconds - anomaly_start_step

    print(f"Streaming live data for {duration_seconds}s. "
          f"{anomaly_machine} will start drifting at t={anomaly_start_step}s.")

    for step in range(duration_seconds):
        ts = datetime.now(timezone.utc)
        for machine in MACHINES:
            if machine["machine_id"] == anomaly_machine and step >= anomaly_start_step:
                drift_step = step - anomaly_start_step
                reading = drifting_reading(drift_step, drift_steps_total)
            else:
                reading = normal_reading()

            doc = make_document(machine, reading, ts)
            collection.insert_one(doc)

            tag = "  <-- DRIFTING" if (machine["machine_id"] == anomaly_machine and step >= anomaly_start_step) else ""
            print(f"[t={step:>3}s] {machine['machine_id']}: temp={reading['temperature_c']:.1f}C "
                  f"vib={reading['vibration_mm_s']:.2f}mm/s{tag}")

        time.sleep(1)

    print("Live stream finished.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-history", action="store_true", help="Backfill 30 min of normal history")
    parser.add_argument("--reset", action="store_true",
                        help="Drop sensor_readings before seeding (use when re-running the demo "
                             "to prevent old --live drift data from contaminating the baseline)")
    parser.add_argument("--live", action="store_true", help="Stream live data with an injected anomaly")
    parser.add_argument("--duration", type=int, default=240, help="Live stream duration in seconds")
    parser.add_argument("--anomaly-machine", default="PUMP-02", help="Which machine should fail")
    args = parser.parse_args()

    client = MongoClient(MONGO_URI, tlsInsecure=True)
    db = client[DB_NAME]
    collection = db["sensor_readings"]

    if args.reset:
        collection.drop()
        print("Dropped sensor_readings collection.")

    if args.seed_history:
        seed_history(collection)
    if args.live:
        run_live(collection, duration_seconds=args.duration, anomaly_machine=args.anomaly_machine)
    if not args.seed_history and not args.live:
        print("Nothing to do. Pass --seed-history and/or --live. See --help.")


if __name__ == "__main__":
    main()
