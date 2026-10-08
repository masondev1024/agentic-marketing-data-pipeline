"""Frozen acceptance inputs, real SQLite and NEW PROCESS recovery; no LLM claim."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from llm_ops_lab.marketing.contracts import ContractError
from llm_ops_lab.marketing.diagnostics import Limits, Scope
from llm_ops_lab.marketing.fixtures import scenario
from llm_ops_lab.marketing.graph import exclusive, run_graph
from llm_ops_lab.marketing.pipeline import publish_dataset

DATA = Path(__file__).resolve().parents[1] / "data/marketing/v1"


@pytest.fixture
def product(tmp_path):
    bundle = json.loads((DATA / "acceptance.json").read_text())
    root, db = tmp_path / "product", tmp_path / "checkpoint.sqlite"
    published = publish_dataset(bundle, root)
    scope = Scope(published["snapshot_id"], "alpha", bundle["date_start"], bundle["date_end"])
    return root, db, bundle, scope


def cli(product, operation="start", thread="alpha-1", decision=None, **overrides):
    root, db, bundle, scope = product
    options = {
        "root": str(root),
        "database": str(db),
        "thread": thread,
        "snapshot": scope.snapshot_id,
        "account": scope.account_id,
        "start": scope.date_start,
        "end": scope.date_end,
        "evaluation-now": bundle["as_of"],
    }
    options.update(overrides)
    if decision:
        options["decision"] = decision
    command = [sys.executable, "-m", "llm_ops_lab.marketing.graph_cli", operation]
    for key, value in options.items():
        command.extend(["--" + key, value])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.stdout, result.stderr
    return result, json.loads(result.stdout)


def counts(db):
    with sqlite3.connect(db) as connection:
        return [
            connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("graph_actions", "graph_reviews")
        ]


def crash(product, point, operation="start", decision=None, thread="alpha-1", limits=None):
    root, db, bundle, scope = product
    # Test-only Python driver: os._exit kills without finally/connection close.
    script = """
import json, os, sys
from pathlib import Path
from llm_ops_lab.marketing.graph import run_graph
from llm_ops_lab.marketing.diagnostics import Limits, Scope
p=json.loads(sys.argv[1])
def crash(point):
    if point == p['point']: os._exit(73)
result = run_graph(Path(p['root']), Path(p['db']), p['thread'], Scope(**p['scope']),
          operation=p['operation'], decision=p['decision'], now=p['now'], fault=crash,
          limits=Limits(**p['limits']))
print(json.dumps(result))
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(
                {
                    "root": str(root),
                    "db": str(db),
                    "scope": asdict(scope),
                    "now": bundle["as_of"],
                    "point": point,
                    "limits": limits or {},
                    "operation": operation,
                    "decision": decision,
                    "thread": thread,
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_new_process_review_preserves_frozen_facts_scope_budget_and_evidence(product, decision):
    first, pending = cli(product)
    assert first.returncode == 0 and first.stderr == "", first.stderr
    assert pending["pending_review"] and pending["result"]["conclusion"] == "observed_decline"
    assert pending["proposal"]["kind"] == "investigation_only"
    assert pending["proposal"]["evidence_ids"] == pending["result"]["evidence_ids"]
    inspected, waiting = cli(product, "resume")
    assert inspected.returncode == 0 and waiting == pending
    golden = json.loads((DATA / "acceptance-expected.json").read_text())["base_account"][:2]
    metrics = next(
        json.loads(v)["data"]
        for v in pending["session"]["evidence"].values()
        if json.loads(v)["tool"] == "metrics"
    )
    assert [r["spend_krw"] for r in metrics] == [r["spend_krw"] for r in golden]
    assert [r["net_attributed_sales_krw"] for r in metrics] == [
        r["net_attributed_sales_krw"] for r in golden
    ]
    second, final = cli(product, "resume", decision=decision)
    assert second.returncode == 0 and second.stderr == "", second.stderr
    assert final["decision"] == decision
    for field in ("binding", "session", "result"):
        assert final[field] == pending[field]
    assert final["session"]["usage"]["tool_calls"] == 3
    assert counts(product[1]) == [4, 1]
    again, replay = cli(product, "resume", decision=decision)
    assert again.returncode == 0 and replay == final and counts(product[1]) == [4, 1]
    conflict, _ = cli(product, "resume", decision="reject" if decision == "accept" else "accept")
    assert conflict.returncode == 1 and "decision_conflict" in conflict.stderr
    with sqlite3.connect(product[1]) as db:
        assert (
            json.loads(db.execute("SELECT value FROM graph_reviews").fetchone()[0])[
                "external_actions"
            ]
            == []
        )


def test_thread_isolation_and_changed_inputs_fail_without_execution(product):
    root, db, bundle, scope = product
    _, alpha = cli(product)
    beta_process, beta = cli(product, thread="beta-1", account="beta")
    assert beta_process.returncode == 0, beta_process.stderr
    assert not set(alpha["session"]["evidence"]) & set(beta["session"]["evidence"])
    for raw in beta["session"]["evidence"].values():
        assert json.loads(raw)["scope"]["account_id"] == "beta"
    for patch in ({"account": "beta"}, {"start": scope.date_end}):
        bad, _ = cli(product, "resume", **patch)
        assert bad.returncode == 1 and "changed_input" in bad.stderr
    with pytest.raises(ContractError, match="changed_input"):
        run_graph(root, db, "alpha-1", scope, operation="resume", limits=Limits(max_actions=7))
    bad, _ = cli(product, "resume", thread="absent")
    assert bad.returncode == 1 and "thread_missing" in bad.stderr
    bad, _ = cli(product)
    assert bad.returncode == 1 and "thread_exists" in bad.stderr
    assert counts(db) == [8, 0]


def test_concurrent_process_resume_is_rejected_without_change(product):
    cli(product)
    with exclusive(product[1]):
        bad, _ = cli(product, "resume", decision="accept")
    assert bad.returncode == 1 and "resume_busy" in bad.stderr
    assert counts(product[1]) == [4, 0]
    good, _ = cli(product, "resume", decision="reject")
    assert good.returncode == 0


@pytest.mark.parametrize("mutation", ["publish", "artifact", "manifest"])
def test_stale_or_changed_source_cannot_be_approved(product, mutation):
    root, db, bundle, scope = product
    cli(product)
    if mutation == "publish":
        publish_dataset(scenario(bundle, "corrected_spend"), root)
    else:
        path = (
            root
            / "snapshots"
            / scope.snapshot_id
            / ("gold/metrics.json" if mutation == "artifact" else "manifest.json")
        )
        path.write_bytes(path.read_bytes() + b" ")
    bad, _ = cli(product, "resume", decision="accept")
    assert bad.returncode == 1
    assert ("stale_snapshot" if mutation == "publish" else "snapshot_integrity") in bad.stderr
    assert counts(db) == [4, 0]


def test_human_wait_does_not_refresh_old_evidence(product):
    cli(product)
    process, result = cli(
        product, "resume", decision="accept", **{"evaluation-now": "2026-10-07T00:00:00Z"}
    )
    assert process.returncode == 0
    assert result["decision"] == "expired" and result["result"]["conclusion"] == "unknown"
    assert counts(product[1]) == [4, 0]


@pytest.mark.parametrize("name", ["incomplete", "zero_spend"])
def test_frozen_unsafe_cases_abstain_without_review(product, name):
    root, db, bundle, _ = product
    updated = scenario(bundle, name)
    published = publish_dataset(updated, root)
    process, result = cli(product, snapshot=published["snapshot_id"])
    assert process.returncode == 0 and process.stderr == ""
    assert result["result"]["status"] == "abstained"
    assert result["result"]["conclusion"] == "unknown" and not result.get("pending_review")
    assert counts(db) == [4, 0]


@pytest.mark.parametrize("point", ["reserved", "tool_returned"])
def test_process_death_unknown_tool_outcome_is_terminal_and_budgets_not_reset(product, point):
    killed = crash(product, point)
    assert killed.returncode == 73 and killed.stderr == "", killed.stderr
    resumed, result = cli(product, "resume")
    assert resumed.returncode == 1 and "interrupted_action" in resumed.stderr
    assert result["result"]["conclusion"] == "unknown"
    assert result["session"]["usage"] == {"actions": 8, "tool_calls": 6, "token_units": 32768}
    assert counts(product[1]) == [1, 0]
    again, final = cli(product, "resume")
    assert again.returncode == 1 and final == result and counts(product[1]) == [1, 0]


def test_process_death_after_journal_before_checkpoint_reuses_completed_action(product):
    assert crash(product, "action_committed").returncode == 73
    with sqlite3.connect(product[1]) as db:
        prior = json.loads(db.execute("SELECT value FROM graph_actions").fetchone()[0])
    process, result = cli(product, "resume")
    assert process.returncode == 0, process.stderr
    prior_evidence = prior["update"]["session"]["evidence"]
    assert prior_evidence.items() <= result["session"]["evidence"].items()
    assert result["session"]["usage"]["actions"] == 4
    assert result["session"]["usage"]["tool_calls"] == 3
    assert counts(product[1]) == [4, 0]


def test_review_crash_replay_has_single_record_and_conflicting_decision_rejected(product):
    _, before = cli(product)
    killed = crash(product, "review_committed", "resume", "accept")
    assert killed.returncode == 73 and counts(product[1]) == [4, 1]
    bad, _ = cli(product, "resume", decision="reject")
    assert bad.returncode == 1 and "decision_conflict" in bad.stderr
    good, final = cli(product, "resume", decision="accept")
    assert good.returncode == 0, good.stderr
    assert final["session"] == before["session"] and counts(product[1]) == [4, 1]


def test_review_committed_decision_survives_delayed_replay(product):
    _, before = cli(product)
    assert crash(product, "review_committed", "resume", "accept").returncode == 73
    late = {"evaluation-now": "2026-10-07T00:00:00Z"}
    process, restored = cli(product, "resume", decision="accept", **late)
    assert process.returncode == 0, process.stderr
    assert restored["decision"] == "accept"
    assert restored["result"] == before["result"]
    again, repeated = cli(product, "resume", decision="accept", **late)
    assert again.returncode == 0, again.stderr
    assert repeated == restored and counts(product[1]) == [4, 1]
    rejected, _ = cli(product, "resume", decision="reject", **late)
    assert rejected.returncode == 1 and "decision_conflict" in rejected.stderr


def test_restart_cannot_reset_elapsed_execution_budget(product):
    limits = {"wall_seconds": 3.0}
    killed = crash(product, "action_committed", limits=limits)
    assert killed.returncode == 73, killed.stderr
    time.sleep(3.1)
    resumed = crash(product, "never", operation="resume", limits=limits)
    assert resumed.returncode == 0 and resumed.stderr == "", resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["result"]["error"]["code"] == "timeout"
    assert result["session"]["usage"]["tool_calls"] == 1
    assert counts(product[1]) == [2, 0]


def test_invalid_review_and_corrupt_database_return_structured_failure(product):
    root, db, bundle, scope = product
    with pytest.raises(ContractError, match="request_schema"):
        run_graph(root, db, "invalid", scope, decision="accept")
    assert not db.exists()
    db.write_bytes(b"not sqlite")
    process, result = cli(product)
    assert process.returncode == 1 and "graph_error" in process.stderr
    assert result["conclusion"] == "unknown" and "Traceback" not in process.stderr


def test_acceptance_freeze_hashes_remain_exact():
    freeze = json.loads((DATA / "acceptance-freeze.json").read_text())
    # Reuse the independently frozen data and arithmetic; never regenerate goldens.
    for name, value in freeze["sha256"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == value
