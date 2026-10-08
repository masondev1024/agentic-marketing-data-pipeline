"""Scripted protocol doubles below are never real inference evidence."""

import json
from pathlib import Path

import pytest

from llm_ops_lab.marketing.agent import baseline, run_agent
from llm_ops_lab.marketing.diagnostics import Scope, Snapshot
from llm_ops_lab.marketing.fixtures import scenario
from llm_ops_lab.marketing.pipeline import publish_dataset


class ScriptedModel:
    kind = "scripted_test_double"

    def __init__(self, actions):
        self.actions = iter(actions)

    def generate(self, messages, timeout):
        assert 0 < timeout <= 30
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action = action(json.loads(messages[-1]["content"]))
        return {"output": action, "prompt_tokens": 100, "generation_tokens": 20,
                "max_tokens_reached": False}


def tool(name):
    return json.dumps({"action": "tool", "tool": name, "parameters": {}})


def finish(context):
    return json.dumps({"action": "finish", "conclusion": "observed_decline", "claims": [],
                       "evidence_ids": [o["evidence_id"] for o in context["observations"]]})


def product(tmp_path, name="observed_decline"):
    fixture = json.loads(Path("data/marketing/v1/acceptance.json").read_text())
    bundle = scenario(fixture, name)
    publication = publish_dataset(bundle, tmp_path)
    return Snapshot.open(tmp_path, Scope(publication["snapshot_id"], "alpha",
                                        bundle["date_start"], bundle["date_end"])), bundle["as_of"]


@pytest.mark.parametrize("name,expected", [("observed_decline", "observed_decline"),
                                           ("incomplete", "unknown"), ("zero_spend", "unknown")])
def test_same_snapshot_baseline_and_scripted_protocol(tmp_path, name, expected):
    snapshot, now = product(tmp_path, name)
    deterministic = baseline(snapshot, now)
    actual = run_agent(snapshot, now, ScriptedModel([
        tool("freshness"), tool("quality"), tool("metrics"), finish]))
    assert actual["test_double"] is True
    assert actual["scope"] == deterministic["scope"]
    assert actual["result"]["conclusion"] == deterministic["result"]["conclusion"] == expected
    assert actual["usage"]["tool_calls"] == 3
    assert len(actual["generations"]) == 4


@pytest.mark.parametrize("actions,code,calls", [
    (["{", "{"], "invalid_json", 0),
    ([tool("shell")], "unauthorized_tool", 0),
    ([tool("metrics"), tool("metrics")], "no_progress", 1),
    ([tool("metrics"), finish], "evidence_incomplete", 1),
    ([TimeoutError()], "timeout", 0),
    ([ValueError("bad generation")], "model_error", 0),
    ([json.dumps({"action":"tool", "tool":"metrics", "parameters":{"account_id":"beta"}})],
     "scope", 0),
])
def test_fail_closed(tmp_path, actions, code, calls):
    snapshot, now = product(tmp_path)
    result = run_agent(snapshot, now, ScriptedModel(actions))
    assert result["result"]["error"]["code"] == code
    assert result["result"]["conclusion"] == "unknown"
    assert result["usage"]["tool_calls"] == calls


def test_bounded_malformed_retry_then_model_selects_tools(tmp_path):
    snapshot, now = product(tmp_path)
    result = run_agent(snapshot, now, ScriptedModel([
        "{", tool("quality"), tool("metrics"), tool("freshness"), finish]))
    assert result["result"]["conclusion"] == "observed_decline"
    assert len(result["generations"]) == 5


def test_published_real_evidence_is_not_a_double():
    path = Path("evidence/marketing-ac4-experiment.json")
    if not path.exists():
        pytest.skip("reviewed real evidence not yet published")
    report = json.loads(path.read_text())
    assert len(report["cases"]) == 3
    for case in report["cases"]:
        actual = case["actual"]
        assert actual["test_double"] is False
        assert actual["kind"] == "actual_offline_mlx"
        assert actual["scope"] == case["baseline"]["scope"]
        assert any(g["model_executed"] for g in actual["generations"])
        assert actual["usage"]["tool_calls"] > 0
        assert case["model"]["model_revision"] == report["registration"]["model_revision"]
    assert report["agent_designation"] == "experimental"


def test_owned_model_timeout_reaps_process_and_releases_lock(monkeypatch):
    import subprocess
    import sys

    from llm_ops_lab.marketing import offline_model

    real_popen = subprocess.Popen
    owned = []

    def sleeping_worker(*args, **kwargs):
        process = real_popen([sys.executable, "-u", "-c",
                              'import time; print(\'{"identity": {}}\'); time.sleep(60)'],
                             **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(offline_model.subprocess, "Popen", sleeping_worker)
    with offline_model.OfflineModel() as model:
        with pytest.raises(ValueError, match="already active"):
            with offline_model.OfflineModel():
                pass
        with pytest.raises(TimeoutError):
            model.generate([], 0.05)
    assert owned[0].poll() is not None
    with offline_model.OfflineModel():
        pass
    assert owned[1].poll() is not None


def test_next_generation_reserved_before_token_ceiling(tmp_path):
    class LargeTrace(ScriptedModel):
        def generate(self, messages, timeout):
            result = super().generate(messages, timeout)
            result.update(prompt_tokens=2000, generation_tokens=100)
            return result

    snapshot, now = product(tmp_path)
    result = run_agent(snapshot, now, LargeTrace([
        tool("metrics"), tool("quality"), tool("freshness"), finish]))
    assert len(result["generations"]) == 3
    assert result["result"]["error"]["code"] == "model_token_budget"
    assert sum(g["prompt_tokens"] + g["generation_tokens"]
               for g in result["generations"]) <= 8192
