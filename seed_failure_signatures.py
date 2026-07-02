"""
Seeds the `failure_signatures` collection with a small library of known
failure patterns. Embeddings are NOT generated here -- this uses MongoDB
Atlas's auto-embedding feature (Voyage AI `voyage-4` model), so we just
store the plain text `description` field and Atlas embeds it automatically
based on the vector search index definition (see SCHEMA.md).

Before running this script, create the vector search index in Atlas:

  {
    "fields": [
      {
        "type": "vector",
        "modality": "text",
        "path": "description",
        "model": "voyage-4"
      }
    ]
  }

Usage:
  python seed_failure_signatures.py
"""

import ssl_patch  # noqa: F401 — must precede pymongo import
import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required")
DB_NAME = os.environ.get("MONGO_DB", "team_8")

FAILURE_SIGNATURES = [
    {
        "failure_type": "bearing_overheating",
        "description": (
            "Gradual temperature rise combined with steadily increasing vibration, "
            "typical of bearing wear in centrifugal pumps. Pressure may drop slightly "
            "as flow becomes less efficient. Often precedes seizure within 24-72 hours."
        ),
        "recommended_action": "Schedule bearing inspection and lubrication check within 48 hours.",
    },
    {
        "failure_type": "cavitation",
        "description": (
            "Sharp, irregular vibration spikes with fluctuating pressure readings and "
            "little change in temperature. Often caused by insufficient suction pressure "
            "or air ingestion, leading to impeller damage over time."
        ),
        "recommended_action": "Check suction line for blockages or air leaks; inspect impeller for pitting.",
    },
    {
        "failure_type": "shaft_misalignment",
        "description": (
            "Elevated vibration with a distinct periodic pattern synced to rotational speed, "
            "moderate temperature increase, and stable pressure. Common after recent maintenance "
            "or reassembly."
        ),
        "recommended_action": "Perform laser alignment check on motor-pump coupling.",
    },
    {
        "failure_type": "seal_leak",
        "description": (
            "Slow pressure decline over time with minimal vibration or temperature change. "
            "Often only detectable through trend analysis rather than instantaneous readings."
        ),
        "recommended_action": "Inspect mechanical seal and replace if wear is visible.",
    },
    {
        "failure_type": "electrical_fault",
        "description": (
            "Erratic RPM fluctuations with sudden temperature spikes, often without a corresponding "
            "vibration increase. Suggests motor winding or power supply issues rather than mechanical wear."
        ),
        "recommended_action": "Inspect motor windings, power supply, and control electronics.",
    },
]


def main():
    client = MongoClient(MONGO_URI, tlsInsecure=True)
    db = client[DB_NAME]
    collection = db["failure_signatures"]

    collection.delete_many({})  # clean slate for repeatable seeding

    docs = []
    for sig in FAILURE_SIGNATURES:
        docs.append({
            "failure_type": sig["failure_type"],
            "description": sig["description"],
            "recommended_action": sig["recommended_action"],
            "source": "seed_library",
        })

    collection.insert_many(docs)
    print(f"Inserted {len(docs)} failure signatures into '{DB_NAME}.failure_signatures'.")
    print("Atlas will auto-generate embeddings for the 'description' field via the "
          "'autoembed_index' (voyage-4). Give it a minute to finish indexing "
          "before running vector search queries against this collection.")


if __name__ == "__main__":
    main()

