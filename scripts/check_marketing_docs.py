"""Read-only AC6 documentation/evidence consistency check; never runs inference."""

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = "evidence/marketing-ac6-docs.json"


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def read_json(root, name):
    return json.loads((root / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_path(root, name):
    path = root / name
    require(not Path(name).is_absolute() and ".." not in Path(name).parts,
            f"non-workspace reference: {name}")
    require(path.is_file(), f"missing reference: {name}")
    return path


def validate(root=ROOT):
    receipt = read_json(root, RECEIPT)
    require(receipt["operator"] == "assistant", "actor mismatch")
    require(receipt["learner_marketing_execution_verified"] is False, "learner overclaim")
    require(receipt["aws"] == "optional_design_only_not_provisioned", "AWS overclaim")
    require(receipt["agent_designation"] == "experimental", "promotion overclaim")
    require([d["number"] for d in receipt["deliverables"]] == list(range(1, 8)),
            "seven deliverables required")
    for item in receipt["deliverables"]:
        require(0 < len(item["text"]) <= 2000, "oversized requirement evidence")
        for name in item["references"]:
            checked_path(root, name)
    for name, expected in receipt["reviewed_sha256"].items():
        require(digest(checked_path(root, name)) == expected, f"review changed: {name}")
    for item in receipt["authority"]:
        # The observer intentionally excludes private work/. Its digest is retained,
        # while original-workspace checks additionally verify the available brief.
        path = root / item["path"]
        if path.exists():
            require(digest(path) == item["sha256"], f"authority changed: {item['path']}")
    frozen = read_json(root, "data/marketing/v1/acceptance-freeze.json")
    for name, expected in frozen["sha256"].items():
        require(digest(root / "data/marketing/v1" / name) == expected, "freeze changed")
    experiment = read_json(root, "evidence/marketing-ac4-experiment.json")
    registration = read_json(root, "data/marketing/experiments/preregistered-v1.json")
    require(experiment["registration"] == registration, "registration mismatch")
    comparison = {
        name: {"experiment_sha256": expected, "current_sha256": digest(checked_path(root, name))}
        for name, expected in experiment["source_sha256"].items()
    }
    require(receipt["model_source_comparison"] == comparison, "unreviewed inference source change")
    drift = [name for name, hashes in comparison.items()
             if hashes["experiment_sha256"] != hashes["current_sha256"]]
    if drift:
        require(receipt["project_verdict"] == "withheld_current_inference_revalidation_required",
                "historical inference cannot certify changed code")
        require("실추론 재검증" in (root / "docs/marketing/results.md").read_text(),
                "missing historical evidence limitation")
    cases = experiment["cases"]
    require(len(cases) == 3, "real subset changed; review needed")
    require({c["case"] for c in cases} == set(registration["cases"]), "case mismatch")
    totals = {"real_cases": len(cases), "correct": 0, "complete_evidence": 0,
              "errors": 0, "abstentions": 0, "failed_closed_unknown": 0}
    for case in cases:
        baseline, actual = case["baseline"], case["actual"]
        require(baseline["scope"] == actual["scope"], "snapshot/scope comparison changed")
        require(actual["scope"]["snapshot_id"] == case["snapshot_id"], "snapshot mismatch")
        require(actual["kind"] == "actual_offline_mlx" and not actual["test_double"],
                "test double presented as real inference")
        require(case["model"]["model_revision"] == registration["model_revision"],
                "model revision mismatch")
        require(actual["generations"] and all(g["model_executed"]
                                             for g in actual["generations"]),
                "missing real generations")
        require(actual["result"]["conclusion"] == "unknown", "unreviewed model conclusion")
        require(actual["result"]["error"]["code"] == receipt["model_errors"][case["case"]],
                "unreviewed model error")
        require(case["actual_score"]["tool_calls"] == actual["usage"]["tool_calls"] == 1,
                "model calls mismatch")
        score = case["actual_score"]
        for key in ("correct", "complete_evidence", "failed_closed_unknown"):
            totals[key] += int(score[key])
        totals["errors"] += int(score["error"] is not None)
        totals["abstentions"] += int(score["abstained"])
        require(case["baseline_score"]["correct"] and
                case["baseline_score"]["complete_evidence"], "baseline result changed")
        for run in (baseline, actual):
            for observation in run["observations"]:
                require(observation["evidence"]["manifest_sha256"] == case["manifest_sha256"],
                        "manifest comparison mismatch")
    require(all(experiment["summary"][k] == v for k, v in totals.items()),
            "experiment summary mismatch")
    require(receipt["model_summary"] == totals, "documentation score mismatch")
    require(experiment["summary"]["promotion_passed"] is False, "promotion changed")
    require(not all(experiment["promotion_gates"].values()), "promotion gates changed")
    recovery = read_json(root, "evidence/marketing-ac5-recovery.json")
    require(recovery["test_count"] == len(recovery["cases"]) == receipt["graph_recovery_cases"] and
            all(c["outcome"] == "passed" for c in recovery["cases"]),
            "recovery evidence mismatch")
    docs = sorted((root / "docs/marketing").glob("*.md"))
    for doc in docs:
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", doc.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            require((doc.parent / target.split("#")[0]).is_file(),
                    f"broken documentation link: {doc.name}: {target}")
    return {"status": "documentation_verified", "deliverables": 7,
            "documents_checked": len(docs), "model_summary": totals,
            "agent_designation": "experimental", "operator": "assistant",
            "aws": receipt["aws"], "inference_rerun": False,
            "project_verdict": receipt["project_verdict"], "inference_source_drift": drift}


def main():
    try:
        print(json.dumps(validate(), ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
