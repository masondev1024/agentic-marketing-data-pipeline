"""Fixture-only IPC worker with synthetic evidence and no provider/network calls.

The real Supervisor and worker dispatch still run. Only the provider runtime and
database evidence session are replaced, with metadata-only fault receipts.
"""

import json
import os
import sys
import time
from pathlib import Path

from llm_ops_lab import service_worker
from llm_ops_lab.olist import config as connection_config
from llm_ops_lab.olist import durable_graph
from llm_ops_lab.olist.diagnostics import METRIC_DEFINITIONS, _evidence_id
from llm_ops_lab.olist.lineage import _source_fingerprint
from llm_ops_lab.olist.nvidia_planner import NvidiaPlannerError


class FixtureRuntime:
    identity = {"model_id": "fixture", "model_revision": "r1"}

    def __init__(self, output_dir):
        self.event_path = Path(output_dir) / "fixture-events.jsonl"
        self.mode = "normal"

    def event(self, name):
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": name, "mode": self.mode}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def generate(self, _messages, _timeout):
        self.event("invocation")
        if self.mode == "watchdog":
            time.sleep(30)
        else:
            time.sleep(0.03)
        if self.mode == "provider-timeout":
            self.event("provider_timeout")
            raise NvidiaPlannerError("upstream_timeout")
        self.event("returned")
        return {
            "output": '{"requests":["coverage"]}',
            "prompt_tokens": 80,
            "output_tokens": 24,
            "model_id": "fixture",
            "model_revision": "r1",
        }


def fixture_session(runtime):
    quality = {
        "version_id": "v1",
        "active_published_version_id": "v1",
        "transform_sha256": _source_fingerprint()[1],
        "quality_status": "passed",
        "failed_check_count": 0,
        "warning_count": 0,
        "check_count": 4,
    }
    channel = [{
        "origin": None,
        "lead_count": 3,
        "contract_count": 1,
        "linked_contract_count": 0,
        "unlinked_contract_count": 1,
    }]

    class SyntheticSession:
        def __init__(self, _dsn, *, version_id, deadline, started_at=None):
            self.dataset_version = version_id or "v1"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def collect_bundle(self, tools):
            runtime.event("evidence")
            result = [{
                "evidence_id": _evidence_id(self.dataset_version, "version_evidence", quality),
                "tool": "version_evidence",
                "dataset_version": self.dataset_version,
                "data": quality,
            }]
            for tool in tools:
                data = channel if tool == "channel_funnel" else {
                    "metric_definitions": METRIC_DEFINITIONS,
                    "metric_policy": {},
                    "policy_sha256": "b" * 64,
                }
                result.append({
                    "evidence_id": _evidence_id(self.dataset_version, tool, data),
                    "tool": tool,
                    "dataset_version": self.dataset_version,
                    "data": data,
                })
            return result

    return SyntheticSession


def main():
    config = json.loads(sys.stdin.readline())
    runtime = FixtureRuntime(config["output_dir"])
    connection_config.connection_info = lambda *_args, **_kwargs: "synthetic-unused-dsn"
    durable_graph.OlistToolSession = fixture_session(runtime)
    service_worker.reply(sys.stdout, {
        "event": "ready", "pid": os.getpid(), "model": runtime.identity,
    })
    for line in sys.stdin:
        request = json.loads(line)
        key = request["settings"].get("idempotency_key", "")
        runtime.mode = next(
            (mode for mode in ("provider-timeout", "watchdog") if mode in key), "normal"
        )
        service_worker.reply(sys.stdout, service_worker.process_request(config, request, runtime))


if __name__ == "__main__":
    main()
