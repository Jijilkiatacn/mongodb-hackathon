"""
Monitoring Agent — watches `sensor_readings` for anomalies.

Uses a fixed-baseline z-score approach:
  1. Compute mean/stddev from a historical window old enough to predate any
     ongoing drift (BASELINE_MAX_AGE_MINUTES ago → BASELINE_MIN_AGE_MINUTES ago).
  2. Fetch the single most recent reading within RECENT_LOOKBACK_MINUTES.
  3. Z-score = (current - baseline_mean) / baseline_stddev.

This avoids the sliding-window "baseline chasing" problem where a rolling
mean computed over recent drifting data suppresses the anomaly signal.

Env vars required: MONGO_URI, MONGO_DB
"""

import ssl_patch  # noqa: F401 — must precede pymongo import
import os
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient
from pymongo.errors import PyMongoError

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required")
DB_NAME = os.environ.get("MONGO_DB", "team_8")

Z_SCORE_THRESHOLD = float(os.environ.get("Z_SCORE_THRESHOLD", "2.5"))

# Baseline window: use data older than BASELINE_MIN_AGE_MINUTES (to exclude recent
# drifted readings) but not older than BASELINE_MAX_AGE_MINUTES (to stay within the
# seeded history). With --seed-history backfilling 30 min, this captures ~27 min of
# clean normal data per machine (well above MIN_BASELINE_COUNT).
BASELINE_MAX_AGE_MINUTES = int(os.environ.get("BASELINE_MAX_AGE_MINUTES", "35"))
BASELINE_MIN_AGE_MINUTES = int(os.environ.get("BASELINE_MIN_AGE_MINUTES", "3"))
RECENT_LOOKBACK_MINUTES = int(os.environ.get("RECENT_LOOKBACK_MINUTES", "1"))
MIN_BASELINE_COUNT = int(os.environ.get("MIN_BASELINE_COUNT", "5"))

DEBUG = os.environ.get("MONITORING_DEBUG", "0") == "1"


def get_collections(client):
    db = client[DB_NAME]
    return db["sensor_readings"], db["agent_decisions"]


def compute_zscores(sensor_readings, machine_id, field):
    """
    Fixed-baseline z-score for a single machine and field.

    Step 1: aggregate mean/stddev from the historical baseline window.
    Step 2: fetch the most recent reading.
    Step 3: compute z = (current - mean) / stddev.

    Returns a dict with keys: <field>, baseline_mean, baseline_stddev,
    baseline_count, z_score — or None if data is insufficient.
    """
    now = datetime.now(timezone.utc)
    baseline_start = now - timedelta(minutes=BASELINE_MAX_AGE_MINUTES)
    baseline_end = now - timedelta(minutes=BASELINE_MIN_AGE_MINUTES)

    # Step 1: fixed baseline stats
    baseline_pipeline = [
        {
            "$match": {
                "metadata.machine_id": machine_id,
                "timestamp": {"$gte": baseline_start, "$lt": baseline_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "mean": {"$avg": f"${field}"},
                "stddev": {"$stdDevSamp": f"${field}"},
                "count": {"$sum": 1},
            }
        },
    ]
    baseline_result = list(sensor_readings.aggregate(baseline_pipeline))

    if not baseline_result or baseline_result[0]["count"] < MIN_BASELINE_COUNT:
        count = baseline_result[0]["count"] if baseline_result else 0
        if DEBUG:
            print(f"  [DEBUG] {machine_id}/{field}: baseline count={count} < {MIN_BASELINE_COUNT}, skipping")
        return None

    b = baseline_result[0]
    baseline_mean = b["mean"]
    baseline_stddev = b["stddev"] or 1.0  # guard against zero stddev (all identical readings)

    # Step 2: most recent reading
    recent_pipeline = [
        {
            "$match": {
                "metadata.machine_id": machine_id,
                "timestamp": {"$gte": now - timedelta(minutes=RECENT_LOOKBACK_MINUTES)},
            }
        },
        {"$sort": {"timestamp": -1}},
        {"$limit": 1},
    ]
    recent_result = list(sensor_readings.aggregate(recent_pipeline))

    if not recent_result:
        if DEBUG:
            print(f"  [DEBUG] {machine_id}/{field}: no recent reading within {RECENT_LOOKBACK_MINUTES}m")
        return None

    recent_doc = recent_result[0]
    current_value = recent_doc.get(field)

    if current_value is None:
        return None

    z_score = (current_value - baseline_mean) / baseline_stddev

    if DEBUG:
        print(
            f"  [DEBUG] {machine_id}/{field}: "
            f"baseline_mean={baseline_mean:.2f}, baseline_stddev={baseline_stddev:.2f}, "
            f"baseline_count={b['count']}, current={current_value:.2f}, z={z_score:.2f}"
        )

    return {
        **recent_doc,
        "baseline_mean": baseline_mean,
        "baseline_stddev": baseline_stddev,
        "baseline_count": b["count"],
        "z_score": z_score,
    }


def check_machine(sensor_readings, agent_decisions, machine_id):
    """
    Checks temperature and vibration z-scores for a machine. Logs the check
    regardless of outcome. Returns an anomaly dict if either field crosses
    the threshold, otherwise None.
    """
    temp_doc = compute_zscores(sensor_readings, machine_id, "temperature_c")
    vib_doc = compute_zscores(sensor_readings, machine_id, "vibration_mm_s")

    if temp_doc is None or vib_doc is None:
        return None  # not enough baseline data yet for this machine

    temp_z = temp_doc.get("z_score", 0)
    vib_z = vib_doc.get("z_score", 0)
    is_anomaly = abs(temp_z) > Z_SCORE_THRESHOLD or abs(vib_z) > Z_SCORE_THRESHOLD

    log_entry = {
        "timestamp": datetime.now(timezone.utc),
        "machine_id": machine_id,
        "agent": "monitoring_agent",
        "event": "anomaly_flagged" if is_anomaly else "check_ok",
        "baseline_stats": {
            "temperature_c": temp_doc.get("temperature_c"),
            "baseline_mean_temp": round(temp_doc.get("baseline_mean", 0), 2),
            "z_score_temp": round(temp_z, 2),
            "vibration_mm_s": vib_doc.get("vibration_mm_s"),
            "baseline_mean_vibration": round(vib_doc.get("baseline_mean", 0), 2),
            "z_score_vibration": round(vib_z, 2),
        },
        "next_agent": "diagnosis_agent" if is_anomaly else None,
    }
    agent_decisions.insert_one(log_entry)

    if not is_anomaly:
        return None

    # Key names (rolling_mean_temp, rolling_mean_vibration) are kept unchanged
    # so diagnosis_agent.py's describe_anomaly() and prompt templates still work.
    return {
        "machine_id": machine_id,
        "temperature_c": temp_doc.get("temperature_c"),
        "rolling_mean_temp": temp_doc.get("baseline_mean", 0),
        "z_score_temp": temp_z,
        "vibration_mm_s": vib_doc.get("vibration_mm_s"),
        "rolling_mean_vibration": vib_doc.get("baseline_mean", 0),
        "z_score_vibration": vib_z,
    }


def run_monitoring_pass(machine_ids):
    """
    Runs one monitoring pass across all given machines.
    Returns a list of anomaly dicts (empty if nothing flagged).
    Any transient MongoDB connectivity error (SSL blip, timeout, etc.) is caught
    here so the orchestrator loop survives and retries on the next pass.
    """
    client = None
    try:
        client = MongoClient(MONGO_URI, tlsInsecure=True)
        sensor_readings, agent_decisions = get_collections(client)

        anomalies = []
        for machine_id in machine_ids:
            anomaly = check_machine(sensor_readings, agent_decisions, machine_id)
            if anomaly:
                anomalies.append(anomaly)

        return anomalies
    except PyMongoError as exc:
        print(f"  [WARN] MongoDB error (pass skipped, will retry next poll): {str(exc)[:160]}")
        return []
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == "__main__":
    # Quick standalone test: run one monitoring pass and print results.
    machine_ids = ["PUMP-01", "PUMP-02", "PUMP-03"]
    found = run_monitoring_pass(machine_ids)
    if found:
        print(f"Flagged {len(found)} anomalies:")
        for a in found:
            print(f"  {a['machine_id']}: temp_z={a['z_score_temp']:.2f}, vib_z={a['z_score_vibration']:.2f}")
    else:
        print("No anomalies detected in this pass.")
