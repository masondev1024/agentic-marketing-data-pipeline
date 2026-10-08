"""Execution guarantees; scripted plans do not establish model classification quality."""

import json
import time

import pytest
from test_olist_agent import FakeSession, generation

from llm_ops_lab.olist import intent_agent


def planned(disposition="supported", intents=("coverage",)):
    return generation({"requests": list(intents) if disposition == "supported" else [disposition]})


def test_composed_plan_queries_once_and_excludes_unrequested_findings(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", FakeSession)
    result = intent_agent.investigate(
        "reader", "coverage and origin", lambda *_: planned(intents=("coverage", "origin"))
    )
    assert result["status"] == "completed"
    assert {f["code"] for f in result["findings"]} == {
        "seller_sample_coverage_unknown",
        "missing_origin_leads",
    }
    assert result["usage"]["tool_calls"] == 1
    assert result["usage"]["generations"] == 1
    ids = {e["evidence_id"] for e in result["evidence"]}
    assert all(set(f["evidence_ids"]) <= ids for f in result["findings"])


@pytest.mark.parametrize(
    "disposition", ["unsafe", "out_of_scope", "unavailable_root_cause", "ambiguous"]
)
def test_semantic_refusal_never_opens_database(monkeypatch, disposition):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))
    result = intent_agent.investigate(
        "unused", "untrusted request", lambda *_: planned(disposition, ())
    )
    assert result["status"] == "abstained"
    assert result["stop_reason"] == "model_abstained"
    assert result["findings"] == result["evidence"] == []
    assert result["dataset_version"] is None
    assert result["usage"]["tool_calls"] == 0


def test_financial_refusal_reads_only_version_and_definitions(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", FakeSession)
    result = intent_agent.investigate(
        "reader", "30day revenue", lambda *_: planned("financial_unsupported", ())
    )
    assert result["stop_reason"] == "model_abstained"
    assert result["limitations"] == ["unsupported_metric"]
    assert [e["tool"] for e in result["evidence"]] == ["version_evidence", "definitions"]


def test_malformed_plan_repairs_once_and_never_becomes_semantic_refusal(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))
    result = intent_agent.investigate(
        "reader", "question", lambda *_: {"output": "{bad", "prompt_tokens": 2}
    )
    assert result["stop_reason"] == "invalid_request_plan"
    assert result["usage"]["generations"] == 2
    assert result["trace"][0]["repair_requested"]


def test_planner_deadline_has_no_database_access_and_records_attempt(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))

    def generate(*_):
        raise TimeoutError("private worker detail")

    result = intent_agent.investigate("reader", "question", generate)
    assert result["stop_reason"] == "investigation_deadline_exceeded"
    assert result["usage"]["generation_attempts"] == 1
    assert result["usage"]["generations"] == 0
    assert "private" not in json.dumps(result)


def test_return_after_deadline_cannot_execute_plan(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))
    result = intent_agent.investigate(
        "reader", "question", lambda *_: time.sleep(0.02) or planned(), timeout_seconds=0.01
    )
    assert result["stop_reason"] == "investigation_deadline_exceeded"


def test_zero_warnings_are_a_supported_observation(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", FakeSession)
    result = intent_agent.investigate(
        "reader", "origin and quality", lambda *_: planned(intents=("origin", "quality"))
    )
    assert result["status"] == "completed"
    assert [f["code"] for f in result["findings"]] == [
        "missing_origin_leads",
        "published_quality_no_warnings",
    ]


@pytest.mark.parametrize(
    "intent,code",
    [
        ("origin", "origin_missing_count_zero"),
        ("coverage", "seller_sample_unlinked_count_zero"),
        ("pipeline_history", "recent_pipeline_no_adverse_status"),
    ],
)
def test_observed_zero_counts_do_not_imply_global_health(intent, code):
    entry = {
        "data": (
            [
                {
                    "origin": "organic",
                    "lead_count": 10,
                    "contract_count": 2,
                    "linked_contract_count": 2,
                    "unlinked_contract_count": 0,
                }
            ]
            if intent != "pipeline_history"
            else [{"status": "published"}]
        ),
        "evidence_id": "a" * 64,
    }
    tool = "run_history" if intent == "pipeline_history" else "channel_funnel"
    findings = intent_agent._findings_for(intent, {tool: entry})
    assert findings[0]["code"] == code
    assert "does not" in findings[0]["summary"]
    assert intent_agent._findings_for(intent, {tool: entry | {"data": []}}) == []


def test_missing_count_is_unknown_not_zero():
    with pytest.raises(ValueError, match="invalid_tool_evidence"):
        intent_agent._findings_for(
            "coverage",
            {"channel_funnel": {"data": [{"origin": "organic"}], "evidence_id": "a" * 64}},
        )


def test_worker_exception_is_sanitized(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))

    def fail(*_):
        raise RuntimeError("secret-worker-detail")

    result = intent_agent.investigate("reader", "question", fail)
    assert result["stop_reason"] == "model_unavailable"
    assert result["model_execution_status"] == "attempted_unknown"
    assert "secret-worker-detail" not in json.dumps(result)


def test_invalid_evidence_and_failed_quality_cannot_be_rendered(monkeypatch):
    class Corrupt(FakeSession):
        def call(self, tool):
            return super().call(tool) | {"evidence_id": "a" * 64}

    monkeypatch.setattr(intent_agent, "OlistToolSession", Corrupt)
    result = intent_agent.investigate("reader", "coverage", lambda *_: planned())
    assert result["stop_reason"] == "invalid_tool_evidence"
    assert result["findings"] == []

    class Failed(FakeSession):
        def initialize(self):
            from llm_ops_lab.olist.diagnostics import _evidence_id

            item = super().initialize()
            item["data"]["quality_status"] = "failed"
            item["evidence_id"] = _evidence_id(self.dataset_version, item["tool"], item["data"])
            return item

    monkeypatch.setattr(intent_agent, "OlistToolSession", Failed)
    result = intent_agent.investigate("reader", "coverage", lambda *_: planned())
    assert result["stop_reason"] == "published_quality_gate_failed"
    assert result["usage"]["tool_calls"] == 0


@pytest.mark.parametrize(
    "metadata", [{"prompt_tokens": 2049}, {"output_tokens": True}, {"max_tokens_reached": True}]
)
def test_generation_budget_failures_are_terminal(monkeypatch, metadata):
    monkeypatch.setattr(intent_agent, "OlistToolSession", lambda *a, **k: pytest.fail("DB opened"))
    result = intent_agent.investigate("reader", "question", lambda *_: planned() | metadata)
    assert result["stop_reason"] == "model_token_budget"


def test_observed_denominators_come_from_same_evidence(monkeypatch):
    monkeypatch.setattr(intent_agent, "OlistToolSession", FakeSession)
    result = intent_agent.investigate(
        "reader", "origin and coverage", lambda *_: planned(intents=("origin", "coverage"))
    )
    a, b = [f["observed_counts"] for f in result["findings"]]
    assert (a["numerator"], a["denominator"], a["unit"]) == (3, 13, "leads")
    assert (b["numerator"], b["denominator"], b["unit"]) == (1, 3, "contracts")
