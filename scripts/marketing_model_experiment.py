"""Run within disposable copy; writes only the requested experiment output directory."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from llm_ops_lab.marketing.agent import baseline, run_agent
from llm_ops_lab.marketing.diagnostics import Scope, Snapshot
from llm_ops_lab.marketing.fixtures import scenario
from llm_ops_lab.marketing.offline_model import OfflineModel
from llm_ops_lab.marketing.pipeline import publish_dataset


def score(record, expected):
    result = record["result"]
    tools = {o["evidence"]["tool"] for o in record["observations"]}
    return {"correct": result["conclusion"] == expected and result["status"] != "error",
            "complete_evidence": {"metrics", "quality", "freshness"} <= tools,
            "error": result.get("error"), "abstained": result["status"] == "abstained",
            "failed_closed_unknown": result["status"] == "error"
            and result["conclusion"] == "unknown",
            "unsupported_claim_rejected": result.get("error", {}).get("code")
            == "unsupported_claim", "tool_calls": record["usage"]["tool_calls"],
            "latency_seconds": record["latency_seconds"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    registration_path = Path("data/marketing/experiments/preregistered-v1.json")
    registration = json.loads(registration_path.read_text())
    fixture_path = Path("data/marketing/v1/acceptance.json")
    fixture = json.loads(fixture_path.read_text())
    report = {"version": registration["version"], "operator": "assistant",
              "started_at": datetime.now(UTC).isoformat(), "agent_designation": "experimental",
              "registration": registration,
              "registration_sha256": hashlib.sha256(registration_path.read_bytes()).hexdigest(),
              "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
              "expected_figures": "data/marketing/v1/acceptance-expected.json",
              "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [Path("src/llm_ops_lab/marketing/agent.py"),
                                          Path("src/llm_ops_lab/marketing/offline_model.py"),
                                          Path("src/llm_ops_lab/marketing/model_worker.py")]},
              "cases": []}
    for name, expected in registration["cases"].items():
        bundle = scenario(fixture, name)
        root = args.output / name
        publication = publish_dataset(bundle, root)
        snapshot = Snapshot.open(root, Scope(publication["snapshot_id"], "alpha",
                                            bundle["date_start"], bundle["date_end"]))
        deterministic = baseline(snapshot, bundle["as_of"])
        # One model process at a time; loading is measured separately from diagnosis.
        with OfflineModel() as model:
            actual = run_agent(snapshot, bundle["as_of"], model)
            identity = model.identity
        report["cases"].append({"case": name, "fixture_revision": bundle["fixture_revision"],
                                "snapshot_id": snapshot.scope.snapshot_id,
                                "manifest_sha256": snapshot.manifest_sha256,
                                "model": identity, "expected_conclusion": expected,
                                "baseline": deterministic, "actual": actual,
                                "baseline_score": score(deterministic, expected),
                                "actual_score": score(actual, expected)})
        (args.output / "experiment.json").write_text(json.dumps(report, indent=2) + "\n")
    scores = [case["actual_score"] for case in report["cases"]]
    report["summary"] = {"real_cases": len(scores), "correct": sum(s["correct"] for s in scores),
                         "complete_evidence": sum(s["complete_evidence"] for s in scores),
                         "errors": sum(s["error"] is not None for s in scores),
                         "abstentions": sum(s["abstained"] for s in scores),
                         "unsupported_claims_rejected": sum(
                             s["unsupported_claim_rejected"] for s in scores),
                         "promotion_passed": False,
                         "promotion_failure": "minimum_real_cases: 3 < 20; see per-case scores"}
    thresholds = registration["promotion"]
    report["promotion_gates"] = {
        "minimum_real_cases": len(scores) >= thresholds["minimum_real_cases"],
        "correct_fraction": sum(s["correct"] for s in scores) / len(scores)
        >= thresholds["correct_fraction"],
        "complete_evidence_fraction": sum(s["complete_evidence"] for s in scores) / len(scores)
        >= thresholds["complete_evidence_fraction"],
        "maximum_errors": report["summary"]["errors"] <= thresholds["maximum_errors"],
        "maximum_unsupported_claims": all(
            c["actual"]["result"]["conclusion"] == "unknown"
            or c["actual_score"]["correct"] for c in report["cases"]),
        "maximum_latency_ratio_to_baseline": all(
            c["actual_score"]["latency_seconds"] / c["baseline_score"]["latency_seconds"]
            <= thresholds["maximum_latency_ratio_to_baseline"] for c in report["cases"]),
    }
    report["summary"]["failed_closed_unknown"] = sum(s["failed_closed_unknown"] for s in scores)
    (args.output / "experiment.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
