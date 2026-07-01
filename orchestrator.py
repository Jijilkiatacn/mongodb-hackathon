"""
Orchestrator — wires the Monitoring Agent and Diagnosis Agent together
using LangGraph as a simple two-node state graph:

    [monitor] --(anomaly found)--> [diagnose] --> END
       |
       --(no anomaly)--> END

Runs continuously, polling every POLL_INTERVAL_SECONDS, checking all
configured machines each pass. This is the script you run live during
the demo, ideally right after starting `generate_sensor_data.py --live`
in another terminal.

Env vars required: MONGO_URI, MONGO_DB, ANTHROPIC_API_KEY
Usage:
  python orchestrator.py
"""

import os
import sys
import time
from typing import TypedDict, Optional, List

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langgraph.graph import StateGraph, END

from monitoring_agent import run_monitoring_pass
from diagnosis_agent import diagnose

MACHINE_IDS = os.environ.get("MACHINE_IDS", "PUMP-01,PUMP-02,PUMP-03").split(",")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))


class PipelineState(TypedDict):
    anomalies: List[dict]
    diagnoses: List[dict]


def monitor_node(state: PipelineState) -> PipelineState:
    anomalies = run_monitoring_pass(MACHINE_IDS)
    return {"anomalies": anomalies, "diagnoses": []}


def diagnose_node(state: PipelineState) -> PipelineState:
    diagnoses = [diagnose(anomaly) for anomaly in state["anomalies"]]
    return {"anomalies": state["anomalies"], "diagnoses": diagnoses}


def route_after_monitor(state: PipelineState) -> str:
    return "diagnose" if state["anomalies"] else "end"


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("monitor", monitor_node)
    graph.add_node("diagnose", diagnose_node)

    graph.set_entry_point("monitor")
    graph.add_conditional_edges(
        "monitor",
        route_after_monitor,
        {"diagnose": "diagnose", "end": END},
    )
    graph.add_edge("diagnose", END)

    return graph.compile()


def print_pass_result(state: PipelineState):
    if not state["anomalies"]:
        print("  No anomalies this pass.")
        return

    for diagnosis in state["diagnoses"]:
        machine_id = diagnosis.get("machine_id", "?")
        if diagnosis.get("alert_sent"):
            print(f"  ALERT [{machine_id}] matched '{diagnosis['matched_failure_type']}' "
                  f"(score {diagnosis['similarity_score']:.2f})")
            print(f"    Recommendation: {diagnosis['recommendation']}")
        else:
            reason = diagnosis.get("reason", "below confidence threshold")
            print(f"  Anomaly on {machine_id} flagged but NOT alerted ({reason}) "
                  f"-- logged to agent_decisions for review.")


def main():
    app = build_graph()
    print(f"Starting monitoring loop for machines: {MACHINE_IDS}")
    print(f"Polling every {POLL_INTERVAL_SECONDS}s. Press Ctrl+C to stop.\n")

    try:
        while True:
            print(f"--- Pass at {time.strftime('%H:%M:%S')} ---")
            result = app.invoke({"anomalies": [], "diagnoses": []})
            print_pass_result(result)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
