# MongoDB Schema — Predictive Maintenance Agent

## 1. Time Series Collection: `sensor_readings`

This stores the raw streaming data from each machine.

```javascript
db.createCollection("sensor_readings", {
  timeseries: {
    timeField: "timestamp",
    metaField: "metadata",
    granularity: "seconds"
  }
});

// Index to speed up per-machine windowed queries
db.sensor_readings.createIndex({ "metadata.machine_id": 1, "timestamp": -1 });
```

**Document shape:**

```json
{
  "timestamp": ISODate("2026-07-01T10:00:00Z"),
  "metadata": {
    "machine_id": "PUMP-01",
    "machine_type": "centrifugal_pump",
    "location": "Plant-A-Floor2"
  },
  "temperature_c": 68.4,
  "vibration_mm_s": 2.1,
  "pressure_bar": 4.8,
  "rpm": 1450
}
```

Why this shape: `metadata` groups fields MongoDB uses to bucket data internally for compression and fast filtering. Sensor values live at the top level so `$setWindowFields` can operate on them directly.

---

## 2. Vector Collection: `failure_signatures`

A small reference library of known failure patterns. We use **Atlas auto-embedding** (Voyage AI `voyage-4`) so we only store plain text — Atlas generates and manages the embeddings internally, no separate embeddings API call needed in our code.

```javascript
db.createCollection("failure_signatures");
```

Then create the vector search index in the Atlas UI (Atlas Search tab → Create Search Index → Vector Search → JSON Editor), name it `failure_vector_index`:

```json
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
```

**Document shape (note: no `embedding` field — Atlas handles that internally):**

```json
{
  "failure_type": "bearing_overheating",
  "description": "Gradual temperature rise combined with increasing vibration, typical of bearing wear in centrifugal pumps. Often precedes seizure within 24-72 hours if untouched.",
  "recommended_action": "Schedule bearing inspection and lubrication check within 48 hours.",
  "source": "synthetic_seed"
}
```

You only need 5-10 of these seeded for the demo (e.g. bearing_overheating, cavitation, misalignment, seal_leak, electrical_fault).

**Querying it — the Diagnosis Agent's core call:**

With auto-embedding, you pass plain query text and Atlas embeds it on the fly for comparison:

```javascript
db.failure_signatures.aggregate([
  {
    $vectorSearch: {
      index: "failure_vector_index",
      path: "description",
      query: "temperature climbing steadily with rising vibration over the last 10 minutes",
      numCandidates: 50,
      limit: 3
    }
  },
  {
    $project: {
      failure_type: 1,
      recommended_action: 1,
      score: { $meta: "vectorSearchScore" }
    }
  }
]);
```

The Diagnosis Agent builds the `query` text dynamically from the flagged sensor window (e.g. "temperature rose from 65 to 87, vibration rose from 2.0 to 6.5"), then uses the top result's `score` against your confidence threshold (see `agent_decisions` below).

---

## 3. Logging / Memory Collection: `agent_decisions`

This is what gives you "Memory" + "Observability" credit in the judging — every agent action gets written here, so the system has a persistent record it can also reference later.

```json
{
  "timestamp": ISODate("2026-07-01T10:05:32Z"),
  "machine_id": "PUMP-01",
  "agent": "monitoring_agent",
  "event": "anomaly_flagged",
  "window_stats": {
    "mean_temp": 71.2,
    "stddev_temp": 4.8,
    "z_score": 3.1
  },
  "next_agent": "diagnosis_agent"
}
```

```json
{
  "timestamp": ISODate("2026-07-01T10:05:34Z"),
  "machine_id": "PUMP-01",
  "agent": "diagnosis_agent",
  "event": "diagnosis_complete",
  "matched_failure_type": "bearing_overheating",
  "similarity_score": 0.87,
  "confidence_threshold_met": true,
  "recommendation": "Schedule bearing inspection and lubrication check within 48 hours.",
  "alert_sent": true
}
```

The `confidence_threshold_met` field is your guardrail: if similarity score is below a threshold (e.g. 0.7), the agent logs the event but does NOT fire an alert — this single field is an easy thing to point to when judges ask about guardrails.

---

## 4. Example Aggregation: Monitoring Agent's Core Query

This is the `$setWindowFields` query that computes a rolling z-score per machine to detect drift, the heart of the Monitoring Agent.

```javascript
db.sensor_readings.aggregate([
  {
    $match: {
      "metadata.machine_id": "PUMP-01",
      timestamp: { $gte: new Date(Date.now() - 10 * 60 * 1000) } // last 10 min
    }
  },
  {
    $setWindowFields: {
      partitionBy: "$metadata.machine_id",
      sortBy: { timestamp: 1 },
      output: {
        rolling_mean_temp: {
          $avg: "$temperature_c",
          window: { documents: [-20, 0] }
        },
        rolling_stddev_temp: {
          $stdDevSamp: "$temperature_c",
          window: { documents: [-20, 0] }
        }
      }
    }
  },
  {
    $addFields: {
      z_score: {
        $cond: [
          { $eq: ["$rolling_stddev_temp", 0] },
          0,
          {
            $divide: [
              { $subtract: ["$temperature_c", "$rolling_mean_temp"] },
              "$rolling_stddev_temp"
            ]
          }
        ]
      }
    }
  },
  { $match: { z_score: { $gt: 2.5 } } }  // flag anomalies
]);
```

---

## Why this schema scores well

- **MongoDB AI Usage:** native time series + Atlas Vector Search with auto-embedding (Voyage AI `voyage-4`), used meaningfully together (not just one or the other)
- **Memory:** `agent_decisions` collection is genuine agent memory, not just app logs
- **Guardrails:** confidence threshold is structurally encoded in the data, easy to demo and explain
- **Production readiness signal:** indexes are defined, not an afterthought
