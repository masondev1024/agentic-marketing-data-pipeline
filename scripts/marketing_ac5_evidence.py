"""Produce reviewed AC5 receipt from actual JUnit results in the observer copy."""

import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="graph-receipt-") as temporary:
        report = Path(temporary) / "results.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_marketing_graph.py",
            "--junitxml",
            str(report),
        ]
        run = subprocess.run(command, capture_output=True, text=True, check=False)
        if run.returncode:
            print(run.stdout)
            print(run.stderr, file=sys.stderr)
            return run.returncode
        tree = ET.parse(report)
        cases = [
            {"test": case.attrib["name"], "outcome": "passed"}
            for case in tree.findall(".//testcase")
        ]
        assert cases and not tree.findall(".//failure") and not tree.findall(".//error")
        assert not tree.findall(".//skipped")
        assert not run.stderr, run.stderr
        data = Path("data/marketing/v1")
        fixture = json.loads((data / "acceptance.json").read_text())
        result = {
            "ac": 5,
            "verdict": "passed",
            "scope": "local deterministic graph recovery only",
            "actor": "assistant",
            "learner_execution_claimed": False,
            "real_model_experiment_claimed": False,
            "authority": "work/marketing-aws-implementation-brief.md sections 5 and 6",
            "versions": {
                name: version(name) for name in ("langgraph", "langgraph-checkpoint-sqlite")
            },
            "fixture_revision": fixture["fixture_revision"],
            "frozen_sha256": {
                name: hashlib.sha256((data / name).read_bytes()).hexdigest()
                for name in ("acceptance.json", "acceptance-expected.json")
            },
            "test_count": len(cases),
            "cases": cases,
            "pytest_summary": run.stdout.strip().splitlines()[-1],
            "unexpected_stderr": False,
            "recovery_policy": {
                "pending_action": "terminal unknown; conservatively exhaust remaining budget",
                "completed_action": "reuse journal result and evidence without another debit",
                "review_replay": "single local record; conflicting decisions rejected",
                "scope": "exact thread/snapshot/account/dates/limits binding",
                "external_side_effects": "none; no exactly-once guarantee claimed",
            },
        }
        print("AC5_EVIDENCE_JSON=" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
