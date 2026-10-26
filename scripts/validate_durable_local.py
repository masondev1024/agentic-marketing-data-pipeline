#!/usr/bin/env python3
"""Offline receipts through real ASGI, Supervisor, worker dispatch and SQLite.

No listening socket, credentials, model download or database server is needed.
Provider behavior and evidence data are fixtures, not production measurements.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from llm_ops_lab.api import create_app
from llm_ops_lab.service import ServiceConfig, Supervisor

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/durable_validation_worker.py"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def wait_ready(supervisor, old_pid=None):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if supervisor.ready and supervisor.process.pid != old_pid:
            return supervisor.process.pid
        if supervisor.state == "failed":
            raise AssertionError("fixture worker failed to start")
        await asyncio.sleep(0.01)
    raise AssertionError("fixture worker readiness deadline exceeded")


def snapshot(directory):
    """Read only aggregate metadata from this run's private synthetic ledger."""
    with closing(sqlite3.connect(
        f"file:{directory / 'state.sqlite'}?mode=ro", uri=True,
    )) as connection:
        runs = dict(connection.execute(
            "SELECT status,count(*) FROM durable_runs GROUP BY status"
        ))
        attempts = dict(connection.execute(
            "SELECT status,count(*) FROM durable_attempts GROUP BY status"
        ))
        reserved = connection.execute(
            "SELECT calls_reserved FROM durable_budget WHERE singleton=1"
        ).fetchone()[0]
    events = [json.loads(line) for line in
              (directory / "fixture-events.jsonl").read_text().splitlines()]
    counts = Counter(event["event"] for event in events)
    # These zeros come from complete local rows/events, never absent service metrics.
    return {
        "logical_jobs": sum(runs.values()),
        "complete_jobs": runs.get("complete", 0),
        "running_jobs": runs.get("running", 0),
        "pending_attempts": attempts.get("pending", 0),
        "complete_attempts": attempts.get("complete", 0),
        "provider_invocations": counts["invocation"],
        "provider_timeout_events": counts["provider_timeout"],
        "evidence_collections": counts["evidence"],
        "reserved_calls": reserved,
    }


async def validate_in_directory(directory):
    config = ServiceConfig(
        output_dir=directory, olist_root=directory, olist_engine="intent-v1",
        olist_provider="nvidia-super", durable_db=directory / "state.sqlite",
        request_timeout=2, startup_timeout=10, max_restarts=1, queue_size=0,
    )
    supervisor = Supervisor(config, worker_command=[sys.executable, str(FIXTURE)])
    app = create_app(config, supervisor=supervisor)
    samples = []

    async def post(client, case, key, expected_status, expected_error=None):
        started = time.perf_counter()
        response = await client.post(
            "/v1/investigations", headers={"Idempotency-Key": key},
            json={"question": "synthetic coverage question"},
        )
        elapsed = time.perf_counter() - started
        payload = response.json()
        require(response.status_code == expected_status,
                f"{case}: HTTP {response.status_code}, code={payload.get('error', {}).get('code')}")
        require(payload.get("error", {}).get("code") == expected_error,
                f"{case}: unexpected error code")
        if expected_status == 200:
            require(payload["status"] == "completed", f"{case}: result not completed")
            require(payload["evidence"] and payload["findings"], f"{case}: missing evidence")
        observed = snapshot(directory)
        samples.append({
            "case": case, "http_status": response.status_code,
            "error_code": expected_error, "elapsed_seconds": elapsed,
            "replayed": payload.get("replayed"), "observed": observed,
        })
        return payload, observed

    async with app.router.lifespan_context(app):
        pid = await wait_ready(supervisor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            before = (await client.get("/metrics")).json()
            require(before["remote_budget"]["reserved"] == 0, "fixture budget must start empty")
            first, initial = await post(client, "normal", "fixture-normal", 200)
            require(first["replayed"] is False, "first result incorrectly replayed")
            require(initial == {
                "logical_jobs": 1, "complete_jobs": 1, "running_jobs": 0,
                "pending_attempts": 0, "complete_attempts": 1,
                "provider_invocations": 1, "provider_timeout_events": 0,
                "evidence_collections": 1, "reserved_calls": 1,
            }, "normal request aggregate mismatch")
            replay, repeated = await post(client, "completed_replay", "fixture-normal", 200)
            require(replay["replayed"] is True and repeated == initial,
                    "completed replay added external work or reservation")
            for field in ("investigation_id", "usage", "evidence", "findings"):
                require(replay[field] == first[field], f"replay changed {field}")

            _, timeout = await post(client, "provider_timeout", "fixture-provider-timeout",
                                    503, "attempted_unknown")
            _, timeout_retry = await post(client, "provider_timeout_retry",
                                          "fixture-provider-timeout", 503, "attempted_unknown")
            require(timeout == timeout_retry and timeout["pending_attempts"] == 1
                    and timeout["provider_invocations"] == timeout["reserved_calls"] == 2,
                    "provider timeout retried, refunded or incorrectly completed")
            _, interrupted = await post(client, "worker_deadline", "fixture-watchdog",
                                        504, "inference_deadline_exceeded")
            new_pid = await wait_ready(supervisor, old_pid=pid)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("old fixture worker still exists after replacement")
            _, interrupted_retry = await post(client, "worker_deadline_retry", "fixture-watchdog",
                                              503, "attempted_unknown")
            require(interrupted == interrupted_retry and interrupted["pending_attempts"] == 2
                    and interrupted["provider_invocations"] == interrupted["reserved_calls"] == 3,
                    "interrupted provider attempt retried, refunded or incorrectly completed")
            _, final = await post(client, "fresh_after_recovery", "fixture-after-recovery", 200)
            require(final == {
                "logical_jobs": 4, "complete_jobs": 2, "running_jobs": 2,
                "pending_attempts": 2, "complete_attempts": 2,
                "provider_invocations": 4, "provider_timeout_events": 1,
                "evidence_collections": 2, "reserved_calls": 4,
            }, "final aggregate mismatch")
            after = (await client.get("/metrics")).json()
            require(after["counters"]["accepted"] == 7 and after["counters"]["completed"] == 3
                    and after["counters"]["timeouts"] == 1, "supervisor counters changed")
            require(after["remote_budget"]["reserved"] == final["reserved_calls"],
                    "service budget and ledger disagree")
    require(supervisor.process is None and supervisor.task.done() and not supervisor.jobs,
            "supervisor cleanup incomplete")
    try:
        os.kill(new_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("fixture worker survived shutdown")
    access = [json.loads(line) for line in (directory / "access.jsonl").read_text().splitlines()]
    require(sum(row["route"] == "/v1/investigations" for row in access) == len(samples),
            "HTTP audit attempt count mismatch")
    failures = sum(sample["http_status"] >= 400 for sample in samples)
    return {
        "kind": "durable_local_synthetic.v1", "status": "passed",
        "at": datetime.now(UTC).isoformat(),
        "scope": ("local ASGI HTTP; real Supervisor/worker request handler/SQLite; "
                  "mock provider/data"),
        "fixture_delay_seconds": 0.03, "worker_deadline_seconds": config.request_timeout,
        "summary": {"http_attempts": len(samples), "http_failures": failures,
                    "http_failure_denominator": len(samples), "completed_replays": 1, **final},
        "samples": samples,
        "service_metrics_before": {"counters": before["counters"],
                                   "remote_budget": before["remote_budget"]},
        "service_metrics_after": {"counters": after["counters"],
                                  "remote_budget": after["remote_budget"]},
        "cleanup": {"workers_reaped": True, "supervisor_closed": True},
        "limitations": [
            "Seven sequential HTTP attempts are not a load test or production distribution.",
            "Elapsed includes fixture delay, IPC, SQLite and audit writes; no p95 claim.",
            "Provider timeout is 503 attempted_unknown; service timeouts counts watchdog here.",
            "Cached usage/model_executed describe the original result, not new provider work.",
            "Reservations count slots; real provider usage and billed cost are unknown.",
            "No real model, PostgreSQL, AWS, HA or public network path was exercised.",
            "Production runtime bootstrap, stdout setup and runtime.close are excluded.",
        ],
    }


def run_validation():
    with TemporaryDirectory(prefix="olist-local-synthetic-") as temporary:
        return asyncio.run(validate_in_directory(Path(temporary)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New JSON receipt path (never overwritten)")
    args = parser.parse_args()
    report = run_validation()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
