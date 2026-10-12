"""Durable journal crash/replay tests; fixtures make no hosted-model claim."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from llm_ops_lab.olist import durable_graph
from llm_ops_lab.olist.durable_graph import DurableError, budget_status, investigate_durable

SCRIPT = r"""
import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
from llm_ops_lab.olist import durable_graph
from llm_ops_lab.olist.diagnostics import _evidence_id, METRIC_DEFINITIONS

p=json.loads(sys.argv[1])
QUALITY={"version_id":"v1", "active_published_version_id":"v1",
 "transform_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
 "quality_status":"passed", "failed_check_count":0, "warning_count":0, "check_count":4}
CHANNEL=[{"origin":None,"lead_count":3,"contract_count":1,
 "linked_contract_count":0,"unlinked_contract_count":1}]
class FakeSession:
 def __init__(self, dsn, *, version_id, deadline, started_at=None):
  self.dataset_version=version_id or "v1"
  self.started_at=started_at
 def __enter__(self): return self
 def __exit__(self, *args): return False
 def collect_bundle(self, tools):
  with open(p["db_calls"],"a") as f: f.write(self.started_at.isoformat()+"\n")
  quality={"evidence_id":_evidence_id(self.dataset_version,"version_evidence",QUALITY),
   "tool":"version_evidence","dataset_version":self.dataset_version,"data":QUALITY}
  result=[quality]
  for tool in tools:
   data=CHANNEL if tool=="channel_funnel" else {"metric_definitions":METRIC_DEFINITIONS,
    "metric_policy":{},"policy_sha256":"b"*64}
   result.append({"evidence_id":_evidence_id(self.dataset_version,tool,data),
    "tool":tool,"dataset_version":self.dataset_version,"data":data})
  return result
durable_graph.OlistToolSession=FakeSession
def generate(*_):
 with open(p["model_calls"],"a") as f: f.write("m\n")
 return {"output":'{"requests":["coverage"]}', "prompt_tokens":80,
  "output_tokens":24,"model_id":"fixture","model_revision":"r1"}
generate.durable_identity={"provider":"test","model":"fixture","revision":"r1"}
def fault(point):
 if p.get("crash")==point: os._exit(73)
try:
 result=durable_graph.investigate_durable(Path(p["db"]),"fixture-dsn", "coverage?",
  generate, principal="local-test", idempotency_key="stable-key", fault=fault)
 print(json.dumps(result, sort_keys=True))
except durable_graph.DurableError as error:
 print(json.dumps({"error":error.code,"status":error.status}, sort_keys=True))
 sys.exit(2)
"""


def execute(db: Path, model_calls: Path, db_calls: Path, crash: str | None = None):
    payload = json.dumps(
        {
            "db": str(db),
            "model_calls": str(model_calls),
            "db_calls": str(db_calls),
            "crash": crash,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", SCRIPT, payload],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=Path(__file__).resolve().parents[1],
    )


def count(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.mark.parametrize("point", ["attempt_reserved", "provider_returned"])
def test_pending_model_attempt_is_unknown_and_never_replayed(tmp_path, point):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, point)
    assert crashed.returncode == 73
    before = count(model_calls)
    resumed = execute(db, model_calls, db_calls)
    assert resumed.returncode == 2
    assert json.loads(resumed.stdout) == {"error": "attempted_unknown", "status": 503}
    assert count(model_calls) == before
    assert count(db_calls) == 0
    assert budget_status(db)["reserved"] == 1


def test_committed_plan_response_recovers_without_a_second_provider_call(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, "response_committed")
    assert crashed.returncode == 73
    assert count(model_calls) == 1
    resumed = execute(db, model_calls, db_calls)
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == "completed"
    assert result["replayed"] is False
    assert result["usage"]["generations"] == 1
    assert count(model_calls) == 1
    assert count(db_calls) == 1


def test_committed_plan_resumes_after_http_deadline_without_new_model_call(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, "response_committed")
    assert crashed.returncode == 73
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE durable_runs SET created_at=created_at-120")
        started_at = connection.execute("SELECT created_at FROM durable_runs").fetchone()[0]
    resumed = execute(db, model_calls, db_calls)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "completed"
    assert count(model_calls) == count(db_calls) == 1
    from datetime import UTC, datetime

    assert db_calls.read_text().strip() == datetime.fromtimestamp(started_at, UTC).isoformat()


def test_terminal_result_commit_gap_replays_without_model_or_database_calls(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, "result_committed")
    assert crashed.returncode == 73
    first_counts = (count(model_calls), count(db_calls))
    replay = execute(db, model_calls, db_calls)
    assert replay.returncode == 0, replay.stderr
    result = json.loads(replay.stdout)
    assert result["replayed"] is True
    assert result["status"] == "completed"
    assert (count(model_calls), count(db_calls)) == first_counts == (1, 1)


def test_committed_evidence_bundle_reuses_the_whole_snapshot(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, "evidence_committed")
    assert crashed.returncode == 73
    resumed = execute(db, model_calls, db_calls)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "completed"
    assert count(model_calls) == count(db_calls) == 1
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status,payload_json,payload_sha256 FROM durable_evidence"
        ).fetchone()
    assert row[0] == "complete"
    assert len(json.loads(row[1])) == 2
    assert len(row[2]) == 64


def test_interrupted_database_bundle_is_never_requeried_or_partially_reused(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    crashed = execute(db, model_calls, db_calls, "evidence_returned")
    assert crashed.returncode == 73
    assert count(model_calls) == count(db_calls) == 1
    resumed = execute(db, model_calls, db_calls)
    assert resumed.returncode == 2
    assert json.loads(resumed.stdout) == {"error": "evidence_attempted_unknown", "status": 503}
    assert count(model_calls) == count(db_calls) == 1


def test_idempotency_conflict_budget_and_read_only_status(tmp_path):
    db, model_calls, db_calls = (
        tmp_path / "state.sqlite",
        tmp_path / "model.log",
        tmp_path / "db.log",
    )
    assert budget_status(db) == {
        "limit": 32,
        "reserved": 0,
        "remaining": 32,
        "scope": "durable_ledger",
    }
    first = execute(db, model_calls, db_calls)
    assert first.returncode == 0

    def never(*_):
        pytest.fail("completed result must replay without model invocation")

    never.durable_identity = {"provider": "test", "model": "fixture", "revision": "r1"}
    result = investigate_durable(
        db,
        "unused",
        "coverage?",
        never,
        principal="local-test",
        idempotency_key="stable-key",
        timeout_seconds=30,
    )
    assert result["replayed"] is True
    assert budget_status(db)["reserved"] == 1
    with pytest.raises(DurableError, match="idempotency_conflict"):
        investigate_durable(
            db,
            "unused",
            "different question",
            never,
            principal="local-test",
            idempotency_key="stable-key",
        )
    with pytest.raises(DurableError, match="durable_config_changed"):
        budget_status(db, total_call_budget=31)
    with pytest.raises(DurableError, match="idempotency_conflict"):
        investigate_durable(
            db,
            "unused",
            "coverage?",
            never,
            principal="local-test",
            idempotency_key="stable-key",
            version_id="v2",
        )


@pytest.mark.parametrize("provider_code", ["rate_limited", "upstream_unavailable"])
def test_known_provider_error_is_saved_and_replayed_safely(tmp_path, provider_code):
    calls = 0

    class ProviderFailure(Exception):
        code = provider_code

    def generate(*_):
        nonlocal calls
        calls += 1
        raise ProviderFailure("provider text must not be stored")

    generate.durable_identity = {"provider": "test", "model": "fixture", "revision": "r1"}
    database = tmp_path / "state.sqlite"
    first = investigate_durable(
        database,
        "unused",
        "coverage?",
        generate,
        principal="local-test",
        idempotency_key="provider-failure",
    )
    assert first["provider_error"] == provider_code
    assert first["stop_reason"] == "model_unavailable"
    assert calls == 1
    replay = investigate_durable(
        database,
        "unused",
        "coverage?",
        generate,
        principal="local-test",
        idempotency_key="provider-failure",
    )
    assert replay["provider_error"] == provider_code
    assert replay["replayed"] is True
    assert calls == 1
    with sqlite3.connect(database) as connection:
        assert (
            "provider text"
            not in connection.execute(
                "SELECT response_json FROM durable_attempts WHERE investigation_id=?",
                (first["investigation_id"],),
            ).fetchone()[0]
        )


@pytest.mark.parametrize("provider_code", ["upstream_timeout", "upstream_transport_error"])
def test_ambiguous_transport_failure_remains_unknown(tmp_path, provider_code):
    calls = 0

    class ProviderFailure(Exception):
        code = provider_code

    def generate(*_):
        nonlocal calls
        calls += 1
        raise ProviderFailure("transport detail must not be stored")

    generate.durable_identity = {"provider": "test", "model": "fixture", "revision": "r1"}
    database = tmp_path / "state.sqlite"
    for _ in range(2):
        with pytest.raises(DurableError, match="attempted_unknown"):
            investigate_durable(
                database,
                "unused",
                "coverage?",
                generate,
                principal="local-test",
                idempotency_key="unknown-transport",
            )
    assert calls == 1
    assert budget_status(database)["reserved"] == 1


def test_source_fingerprint_includes_worker_wrapper():
    worker = Path(durable_graph.__file__).parent.parent / "service_worker.py"
    source_hashes = durable_graph._source_fingerprints()
    assert source_hashes["../service_worker.py"] == hashlib.sha256(worker.read_bytes()).hexdigest()


def test_result_digest_detects_corruption_before_replay(tmp_path):
    database = tmp_path / "state.sqlite"
    model_calls, db_calls = tmp_path / "model.log", tmp_path / "db.log"
    first = execute(database, model_calls, db_calls)
    assert first.returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE durable_runs SET result_json=? WHERE investigation_id=?",
            ('{"status":"completed"}', json.loads(first.stdout)["investigation_id"]),
        )
    replay = execute(database, model_calls, db_calls)
    assert replay.returncode == 2
    assert json.loads(replay.stdout)["error"] == "durable_result_corrupt"
    assert count(model_calls) == count(db_calls) == 1


def test_source_binding_drift_conflicts_under_existing_idempotency_key(tmp_path, monkeypatch):
    database = tmp_path / "state.sqlite"
    model_calls, db_calls = tmp_path / "model.log", tmp_path / "db.log"
    first = execute(database, model_calls, db_calls)
    assert first.returncode == 0
    original = durable_graph._source_fingerprints
    monkeypatch.setattr(
        durable_graph,
        "_source_fingerprints",
        lambda: original() | {"intent_agent.py": "0" * 64},
    )
    def never(*_):
        pytest.fail("source drift must conflict before model dispatch")

    never.durable_identity = {"provider": "test", "model": "fixture", "revision": "r1"}
    with pytest.raises(DurableError, match="idempotency_conflict"):
        investigate_durable(
            database,
            "unused",
            "coverage?",
            never,
            principal="local-test",
            idempotency_key="stable-key",
        )
    assert count(model_calls) == count(db_calls) == 1


def test_durable_files_are_private_and_symlinks_are_rejected(tmp_path):
    database = tmp_path / "state.sqlite"
    model_calls, db_calls = tmp_path / "model.log", tmp_path / "db.log"
    result = execute(database, model_calls, db_calls)
    assert result.returncode == 0
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.with_suffix(".sqlite.lock").stat().st_mode & 0o777 == 0o600
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(database)
    with pytest.raises(DurableError, match="durable_store_unavailable"):
        budget_status(linked)
    with pytest.raises(DurableError, match="durable_store_unavailable"):
        investigate_durable(
            linked,
            "unused",
            "coverage?",
            lambda *_: pytest.fail("symlink must be rejected before model dispatch"),
            principal="local-test",
            idempotency_key="linked",
        )
