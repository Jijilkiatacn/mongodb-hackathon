"""
Dashboard backend — serves a single HTML page and a JSON polling endpoint
that reads live data from MongoDB: current sensor readings per machine,
and recent agent_decisions entries (monitoring checks + diagnoses).

Usage:
  python dashboard_server.py
  Then open http://localhost:5000 in a browser.

Env vars required: MONGO_URI, MONGO_DB
"""

import ssl_patch  # noqa: F401 — must precede pymongo import
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory
from pymongo import MongoClient

MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.environ.get("MONGO_DB", "team_8")
MACHINE_IDS = os.environ.get("MACHINE_IDS", "PUMP-01,PUMP-02,PUMP-03").split(",")
HISTORY_MINUTES = int(os.environ.get("DASHBOARD_HISTORY_MINUTES", "5"))

app = Flask(__name__, static_folder=None)
client = MongoClient(MONGO_URI, tlsInsecure=True)
db = client[DB_NAME]


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@app.route("/api/status")
def status():
    sensor_readings = db["sensor_readings"]
    agent_decisions = db["agent_decisions"]

    machines = []
    for machine_id in MACHINE_IDS:
        latest = sensor_readings.find_one(
            {"metadata.machine_id": machine_id}, sort=[("timestamp", -1)]
        )

        from datetime import timedelta
        history_cursor = sensor_readings.find(
            {
                "metadata.machine_id": machine_id,
                "timestamp": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=HISTORY_MINUTES)},
            },
            sort=[("timestamp", 1)],
        )
        history = [
            {
                "timestamp": doc["timestamp"].isoformat(),
                "temperature_c": doc.get("temperature_c"),
                "vibration_mm_s": doc.get("vibration_mm_s"),
            }
            for doc in history_cursor
        ]

        latest_decision = agent_decisions.find_one(
            {"machine_id": machine_id, "event": "anomaly_flagged"},
            sort=[("timestamp", -1)],
        )
        latest_diagnosis = agent_decisions.find_one(
            {"machine_id": machine_id, "event": "diagnosis_complete"},
            sort=[("timestamp", -1)],
        )

        is_alerting = False
        if latest_diagnosis and latest_decision:
            # consider "alerting" only if the diagnosis is for the most recent flagged anomaly
            is_alerting = bool(latest_diagnosis.get("alert_sent"))

        machines.append({
            "machine_id": machine_id,
            "current": {
                "temperature_c": latest.get("temperature_c") if latest else None,
                "vibration_mm_s": latest.get("vibration_mm_s") if latest else None,
                "pressure_bar": latest.get("pressure_bar") if latest else None,
                "rpm": latest.get("rpm") if latest else None,
                "timestamp": latest["timestamp"].isoformat() if latest else None,
            },
            "history": history,
            "status": "alert" if is_alerting else "healthy",
            "diagnosis": {
                "matched_failure_type": latest_diagnosis.get("matched_failure_type") if latest_diagnosis else None,
                "similarity_score": latest_diagnosis.get("similarity_score") if latest_diagnosis else None,
                "recommendation": latest_diagnosis.get("recommendation") if latest_diagnosis else None,
                "timestamp": latest_diagnosis["timestamp"].isoformat() if latest_diagnosis else None,
            } if is_alerting else None,
        })

    recent_feed_cursor = agent_decisions.find().sort("timestamp", -1).limit(15)
    feed = []
    for doc in recent_feed_cursor:
        feed.append({
            "timestamp": doc["timestamp"].isoformat(),
            "machine_id": doc.get("machine_id"),
            "agent": doc.get("agent"),
            "event": doc.get("event"),
            "alert_sent": doc.get("alert_sent", False),
            "matched_failure_type": doc.get("matched_failure_type"),
            "recommendation": doc.get("recommendation"),
        })

    return jsonify({"machines": machines, "feed": feed, "server_time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
