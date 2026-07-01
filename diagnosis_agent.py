"""
Diagnosis Agent — takes a flagged anomaly from the Monitoring Agent,
builds a natural-language description of it, runs $vectorSearch against
`failure_signatures` (Atlas auto-embedding, voyage-4), checks the top
match's score against a confidence threshold, and asks AWS Bedrock to
phrase the final recommendation. Logs the full decision to `agent_decisions`.

Env vars required: MONGO_URI, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Optional env vars:
  MONGO_DB              (default: team_8)
  AWS_DEFAULT_REGION    (default: us-east-1)
  BEDROCK_MODEL_ID      (default: amazon.nova-lite-v1:0)
"""

import ssl_patch  # noqa: F401 — must precede pymongo import
import os
from datetime import datetime, timezone

import boto3
from pymongo import MongoClient
from pymongo.errors import OperationFailure

MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?appName=Cluster0")
DB_NAME = os.environ.get("MONGO_DB", "team_8")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")

VECTOR_INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME", "autoembed_index")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.45"))


def describe_anomaly(anomaly: dict) -> str:
    """Builds the natural-language query text used for vector search.

    Uses physical/engineering vocabulary that mirrors the failure signature descriptions
    rather than purely statistical language — this dramatically improves semantic
    similarity scores with the voyage-4 auto-embedding model.
    """
    temp_c = anomaly["temperature_c"]
    baseline_temp = anomaly["rolling_mean_temp"]
    z_temp = anomaly["z_score_temp"]
    vib = anomaly["vibration_mm_s"]
    baseline_vib = anomaly["rolling_mean_vibration"]
    z_vib = anomaly["z_score_vibration"]

    symptom_parts = []

    if abs(z_temp) > 2.5:
        direction = "rise" if z_temp > 0 else "drop"
        severity = "significant" if abs(z_temp) > 4 else "gradual"
        symptom_parts.append(
            f"{severity} temperature {direction} to {temp_c:.1f}°C "
            f"({abs(z_temp):.1f} standard deviations from the normal {baseline_temp:.1f}°C baseline)"
        )

    if abs(z_vib) > 2.5:
        direction = "increase" if z_vib > 0 else "decrease"
        severity = "significant" if abs(z_vib) > 4 else "steadily increasing"
        symptom_parts.append(
            f"{severity} vibration {direction} to {vib:.2f} mm/s "
            f"({abs(z_vib):.1f} standard deviations from the normal {baseline_vib:.1f} mm/s baseline)"
        )

    if not symptom_parts:
        symptom_parts.append("sensor readings outside normal operating range")

    symptoms = " combined with ".join(symptom_parts)
    return (
        f"Centrifugal pump anomaly detected: {symptoms}. "
        f"Pattern consistent with progressive mechanical degradation, typical of bearing wear."
    )


def vector_search_failure_match(failure_signatures, query_text: str, limit: int = 3):
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "description",
                "query": query_text,
                "numCandidates": 50,
                "limit": limit,
            }
        },
        {
            "$project": {
                "failure_type": 1,
                "description": 1,
                "recommended_action": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    return list(failure_signatures.aggregate(pipeline))


def generate_recommendation(anomaly: dict, match: dict) -> str:
    """Calls AWS Bedrock to phrase a clear, actionable recommendation for a technician,
    grounded in the matched failure signature and the actual sensor readings."""
    prompt = (
        f"You are a predictive maintenance assistant. A sensor anomaly was detected "
        f"on machine {anomaly['machine_id']}.\n\n"
        f"Current readings: temperature {anomaly['temperature_c']}°C (baseline "
        f"{anomaly['rolling_mean_temp']:.1f}°C), vibration {anomaly['vibration_mm_s']} mm/s "
        f"(baseline {anomaly['rolling_mean_vibration']:.1f} mm/s).\n\n"
        f"This pattern most closely matches a known failure signature: '{match['failure_type']}' "
        f"(similarity score {match['score']:.2f}).\n"
        f"Reference description: {match['description']}\n"
        f"Standard recommended action: {match['recommended_action']}\n\n"
        f"Write a brief, 2-3 sentence recommendation for the maintenance technician, "
        f"explaining what's happening and what to do next. Be direct and practical."
    )

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    response = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 300},
    )
    return response["output"]["message"]["content"][0]["text"]


# Per-machine timestamp of the last successful vector-search call, used to
# enforce a minimum gap between embedding requests and stay under the Voyage AI
# free-tier rate limit (3 RPM = 1 request per 20s).
_last_vector_search: dict[str, datetime] = {}
VECTOR_SEARCH_COOLDOWN_SECONDS = int(os.environ.get("VECTOR_SEARCH_COOLDOWN_SECONDS", "25"))


def diagnose(anomaly: dict) -> dict:
    """
    Full diagnosis pipeline for a single flagged anomaly. Returns a dict
    describing the outcome (whether an alert was raised, and why), and
    logs the decision to agent_decisions regardless of outcome.
    """
    client = MongoClient(MONGO_URI, tlsInsecure=True)
    db = client[DB_NAME]
    failure_signatures = db["failure_signatures"]
    agent_decisions = db["agent_decisions"]

    machine_id = anomaly["machine_id"]
    now = datetime.now(timezone.utc)

    # Enforce cooldown to avoid hitting the Voyage AI 3-RPM free-tier rate limit.
    last = _last_vector_search.get(machine_id)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < VECTOR_SEARCH_COOLDOWN_SECONDS:
            client.close()
            return {
                "machine_id": machine_id,
                "alert_sent": False,
                "reason": f"cooldown ({elapsed:.0f}s < {VECTOR_SEARCH_COOLDOWN_SECONDS}s)",
            }

    query_text = describe_anomaly(anomaly)
    try:
        matches = vector_search_failure_match(failure_signatures, query_text)
        _last_vector_search[machine_id] = datetime.now(timezone.utc)
    except OperationFailure as exc:
        # Atlas/Voyage AI rate limit (HTTP 429) or other transient vector-search failure.
        # Log it and skip this pass — the next monitoring pass will retry.
        msg = str(exc)
        is_rate_limit = "429" in msg or "RateLimit" in msg
        print(f"  [WARN] Vector search failed for {machine_id} "
              f"({'rate limit' if is_rate_limit else 'error'}): {msg[:120]}")
        log_entry = {
            "timestamp": now,
            "machine_id": machine_id,
            "agent": "diagnosis_agent",
            "event": "vector_search_error",
            "reason": "rate_limited" if is_rate_limit else "operation_failure",
            "error": msg[:300],
            "alert_sent": False,
        }
        agent_decisions.insert_one(log_entry)
        client.close()
        return {
            "machine_id": machine_id,
            "alert_sent": False,
            "reason": "rate_limited" if is_rate_limit else "vector_search_error",
        }

    if not matches:
        log_entry = {
            "timestamp": datetime.now(timezone.utc),
            "machine_id": machine_id,
            "agent": "diagnosis_agent",
            "event": "no_match_found",
            "query_text": query_text,
            "alert_sent": False,
        }
        agent_decisions.insert_one(log_entry)
        client.close()
        return {"alert_sent": False, "reason": "no_vector_match"}

    top_match = matches[0]
    confidence_met = top_match["score"] >= CONFIDENCE_THRESHOLD

    result = {
        "machine_id": anomaly["machine_id"],
        "matched_failure_type": top_match["failure_type"],
        "similarity_score": top_match["score"],
        "confidence_threshold_met": confidence_met,
        "alert_sent": False,
        "recommendation": None,
    }

    if confidence_met:
        recommendation = generate_recommendation(anomaly, top_match)
        result["recommendation"] = recommendation
        result["alert_sent"] = True

    log_entry = {
        "timestamp": datetime.now(timezone.utc),
        "machine_id": anomaly["machine_id"],
        "agent": "diagnosis_agent",
        "event": "diagnosis_complete",
        "query_text": query_text,
        "matched_failure_type": top_match["failure_type"],
        "similarity_score": top_match["score"],
        "confidence_threshold_met": confidence_met,
        "recommendation": result["recommendation"],
        "alert_sent": result["alert_sent"],
    }
    agent_decisions.insert_one(log_entry)

    client.close()
    return result


if __name__ == "__main__":
    # Quick standalone test with a fabricated anomaly resembling bearing overheating.
    test_anomaly = {
        "machine_id": "PUMP-02",
        "temperature_c": 87.0,
        "rolling_mean_temp": 65.0,
        "z_score_temp": 4.2,
        "vibration_mm_s": 6.5,
        "rolling_mean_vibration": 2.0,
        "z_score_vibration": 3.8,
    }
    outcome = diagnose(test_anomaly)
    print(outcome)
