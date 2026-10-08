"""Agent protocol tests use fake generations and a fake immutable DB session."""

import json
import time

import psycopg
import pytest

from llm_ops_lab.olist import agent, tool_session

QUALITY = {
    "version_id": "v1",
    "active_published_version_id": "v1",
    "transform_sha256": "a" * 64,
    "quality_status": "passed",
    "failed_check_count": 0,
    "warning_count": 0,
    "check_count": 4,
}
CHANNEL = [
    {
        "origin": None,
        "lead_count": 3,
        "contract_count": 1,
        "linked_contract_count": 0,
        "unlinked_contract_count": 1,
    },
    {
        "origin": "organic",
        "lead_count": 10,
        "contract_count": 2,
        "linked_contract_count": 2,
        "unlinked_contract_count": 0,
    },
]
RUNS_FAILED = [
    {
        "run_id": "r1",
        "status": "failed",
        "step": "validate",
        "error_code": "source_validation_failed",
    }
]


class FakeSession:
    tools = frozenset({"channel_funnel", "seller_funnel", "run_history", "definitions"})

    def __init__(self, dsn, *, version_id, deadline):
        self.dataset_version = version_id or "v1"
        self.failure_code = None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def initialize(self):
        data = dict(QUALITY)
        return {
            "evidence_id": agent.diagnostics._evidence_id(
                self.dataset_version, "version_evidence", data
            ),
            "tool": "version_evidence",
            "dataset_version": self.dataset_version,
            "data": data,
        }

    def call(self, tool):
        self.calls.append(tool)
        data = {
            "channel_funnel": CHANNEL,
            "run_history": RUNS_FAILED,
            "seller_funnel": [],
            "definitions": {
                "metric_definitions": agent.diagnostics.METRIC_DEFINITIONS,
                "metric_policy": agent._metric_policy()[0],
                "policy_sha256": agent._metric_policy()[1],
            },
        }[tool]
        return {
            "evidence_id": agent.diagnostics._evidence_id(self.dataset_version, tool, data),
            "tool": tool,
            "dataset_version": self.dataset_version,
            "data": data,
        }


def install_fake_session(monkeypatch):
    monkeypatch.setattr(agent, "OlistToolSession", FakeSession)


def generation(output, **extra):
    return {
        "output": json.dumps(output),
        "prompt_tokens": 80,
        "output_tokens": 24,
        "model_id": "fixture-model",
        "model_revision": "rev1",
        **extra,
    }


def finish(*, status="completed", finding_codes=None, evidence_ids=None, limitation_codes=None):
    return {
        "action": "finish",
        "status": status,
        "finding_codes": finding_codes or [],
        "evidence_ids": evidence_ids or [],
        "limitation_codes": limitation_codes or [],
    }


def test_model_selects_tool_order_and_host_renders_supported_findings(monkeypatch):
    install_fake_session(monkeypatch)
    responses = iter(
        [
            generation({"action": "tool", "tool": "channel_funnel", "parameters": {}}),
            generation(finish(finding_codes=["missing_origin_leads"], evidence_ids=["e2"])),
        ]
    )
    result = agent.investigate("trusted-dsn", "Are origins missing?", lambda *_: next(responses))

    assert result["status"] == "completed"
    assert result["model_executed"] is True
    assert result["usage"]["tool_calls"] == 1
    assert result["evidence"][1]["tool"] == "channel_funnel"
    assert result["findings"] == [
        {
            "code": "missing_origin_leads",
            "severity": "info",
            "summary": "3 leads have no observed origin; they remain in the funnel denominator.",
            "evidence_ids": [result["evidence"][1]["evidence_id"]],
        }
    ]


def test_next_turn_contains_executed_action_and_remaining_tool_state():
    session = FakeSession('trusted', version_id=None, deadline=time.monotonic() + 10)
    evidence = [session.initialize(), session.call('channel_funnel')]
    messages = agent._messages('Are origins missing?', evidence)
    state = json.loads(messages[-1]['content'])['tool_state']
    assert state['executed'] == ['channel_funnel']
    assert 'channel_funnel' not in state['available']
    assert 'run_history' in state['available']


def test_model_cannot_reference_unknown_evidence_or_unproven_finding(monkeypatch):
    install_fake_session(monkeypatch)
    bad_reference = agent.investigate(
        "trusted-dsn",
        "question",
        lambda *_: generation(
            finish(finding_codes=["missing_origin_leads"], evidence_ids=["not-an-alias"])
        ),
    )
    assert bad_reference["status"] == "abstained"
    assert bad_reference["findings"] == []

    responses = iter(
        [
            generation({"action": "tool", "tool": "channel_funnel", "parameters": {}}),
            generation(finish(finding_codes=["recent_pipeline_failures"], evidence_ids=["e2"])),
        ]
    )
    false_finding = agent.investigate("trusted-dsn", "question", lambda *_: next(responses))
    assert false_finding["status"] == "abstained"
    assert false_finding["findings"] == []


def test_malformed_json_has_one_repair_then_abstains(monkeypatch):
    install_fake_session(monkeypatch)
    outputs = iter(["not json", "still not json"])
    prompts = []

    def generate(messages, timeout):
        prompts.append(json.loads(messages[-1]["content"]))
        return {"output": next(outputs), "prompt_tokens": 8, "output_tokens": 2}

    result = agent.investigate("trusted-dsn", "question", generate)
    assert result["status"] == "abstained"
    assert result["limitations"] == ["malformed_model_action"]
    assert result["usage"]["generations"] == 2
    assert prompts[1]["correction"]


def test_unsupported_metric_is_model_abstention_and_prompt_is_injection_bounded(monkeypatch):
    install_fake_session(monkeypatch)
    captured = {}
    responses = iter(
        [
            generation({"action": "tool", "tool": "definitions", "parameters": {}}),
            generation(
                finish(
                    status="abstained", evidence_ids=["e2"], limitation_codes=["unsupported_metric"]
                )
            ),
        ]
    )

    def generate(messages, timeout):
        captured.update(messages=messages, timeout=timeout)
        return next(responses)

    result = agent.investigate("trusted-dsn", "Calculate 30-day revenue", generate)
    assert result["status"] == "abstained"
    assert "unsupported_metric" in result["limitations"]
    assert "30-day" in captured["messages"][1]["content"]
    assert "untrusted" in captured["messages"][1]["content"]
    assert "cause" in captured["messages"][0]["content"]


def test_failed_run_is_history_and_never_rendered_as_root_cause(monkeypatch):
    install_fake_session(monkeypatch)
    responses = iter(
        [
            generation({"action": "tool", "tool": "run_history", "parameters": {}}),
            generation(
                finish(
                    status="completed",
                    finding_codes=["recent_pipeline_failures"],
                    evidence_ids=["e1", "e2"],
                )
            ),
        ]
    )
    result = agent.investigate(
        "trusted-dsn", "Why did the pipeline fail?", lambda *_: next(responses)
    )
    assert result["status"] == "completed"
    finding = result["findings"][0]
    assert finding["code"] == "recent_pipeline_failures"
    assert "does not establish root cause" in finding["summary"]
    assert result["pipeline_context"]["diagnosis"]["root_cause"] == "unknown"
    assert result["pipeline_context"]["last_good_preserved"] is None
    assert result["pipeline_context"]["active_published_version_available"] is True
    assert result["pipeline_context"]["active_published_version_id"] == "v1"


def test_deadline_and_token_budget_are_terminal(monkeypatch):
    install_fake_session(monkeypatch)
    deadline_result = agent.investigate(
        "trusted-dsn",
        "question",
        lambda *_: time.sleep(0.02) or generation(finish()),
        timeout_seconds=0.01,
    )
    assert deadline_result["status"] == "abstained"
    assert "investigation_deadline_exceeded" in deadline_result["limitations"]

    token_result = agent.investigate(
        "trusted-dsn",
        "question",
        lambda *_: generation(finish(), prompt_tokens=agent.MAX_PROMPT_TOKENS + 1),
    )
    assert token_result["status"] == "abstained"
    assert token_result["limitations"] == ["model_token_budget"]


def test_model_cannot_send_arbitrary_sql_or_extra_action_fields(monkeypatch):
    install_fake_session(monkeypatch)
    result = agent.investigate(
        "trusted-dsn",
        "question",
        lambda *_: generation(
            {"action": "tool", "tool": "channel_funnel", "parameters": {"sql": "DROP TABLE x"}}
        ),
    )
    assert result["status"] == "abstained"
    assert result["findings"] == []


def test_reader_query_failure_closes_the_database_connection(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            raise psycopg.OperationalError("private driver details")

        def fetchall(self):
            return []

    class Connection:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def execute(self, sql, params=()):
            return None

        def cursor(self):
            return Cursor()

    connection = Connection()
    monkeypatch.setattr(tool_session.psycopg, "connect", lambda *args, **kwargs: connection)
    session = tool_session.OlistToolSession(
        "trusted-dsn", version_id=None, deadline=time.monotonic() + 5
    )

    with pytest.raises(tool_session.SessionError) as error:
        session.__enter__()

    assert error.value.code == "database_read_failed"
    assert connection.closed is True


@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        (finish(status="completed"), "empty_or_limited_answer"),
        (
            finish(
                status="abstained",
                finding_codes=["published_sample_only"],
                evidence_ids=["e1"],
                limitation_codes=["insufficient_evidence"],
            ),
            "abstained_with_findings",
        ),
        (
            finish(
                status="abstained", evidence_ids=["e1"], limitation_codes=["unsupported_metric"]
            ),
            "unverified_metric_refusal",
        ),
    ],
)
def test_finish_requires_a_complete_cited_outcome(monkeypatch, action, expected_code):
    install_fake_session(monkeypatch)
    result = agent.investigate("trusted-dsn", "question", lambda *_: generation(action))

    assert result["status"] == "abstained"
    assert result["limitations"] == [expected_code]
    assert result["findings"] == []


@pytest.mark.parametrize(
    ("second_tool", "tool_budget", "expected_code"),
    [("channel_funnel", 4, "tool_repeated"), ("seller_funnel", 1, "tool_budget")],
)
def test_repeat_tool_and_tool_budget_are_terminal(
    monkeypatch, second_tool, tool_budget, expected_code
):
    install_fake_session(monkeypatch)
    monkeypatch.setattr(agent, "MAX_TOOL_CALLS", tool_budget)
    responses = iter(
        [
            generation({"action": "tool", "tool": "channel_funnel", "parameters": {}}),
            generation({"action": "tool", "tool": second_tool, "parameters": {}}),
        ]
    )

    result = agent.investigate("trusted-dsn", "question", lambda *_: next(responses))

    assert result["status"] == "abstained"
    assert result["limitations"] == [expected_code]
    assert result["usage"]["tool_calls"] == 1


def test_definitions_tool_policy_hash_mismatch_abstains(monkeypatch):
    class MismatchedPolicySession(FakeSession):
        def call(self, tool):
            item = super().call(tool)
            if tool == "definitions":
                item["data"]["policy_sha256"] = "0" * 64
                item["evidence_id"] = agent.diagnostics._evidence_id(
                    self.dataset_version, tool, item["data"]
                )
            return item

    monkeypatch.setattr(agent, "OlistToolSession", MismatchedPolicySession)
    result = agent.investigate(
        "trusted-dsn",
        "What metrics are blocked?",
        lambda *_: generation({"action": "tool", "tool": "definitions", "parameters": {}}),
    )

    assert result["status"] == "abstained"
    assert result["limitations"] == ["metric_policy_mismatch"]
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["tool"] == "version_evidence"


def test_missing_metric_policy_abstains_before_opening_database(monkeypatch):
    def fail_if_database_opened(*args, **kwargs):
        raise AssertionError("database must not be opened without the metric policy")

    monkeypatch.setattr(
        agent, "_metric_policy", lambda: ({"policy_version": "unavailable"}, "unavailable")
    )
    monkeypatch.setattr(agent, "OlistToolSession", fail_if_database_opened)

    result = agent.investigate("trusted-dsn", "question", lambda *_: generation(finish()))

    assert result["status"] == "abstained"
    assert result["limitations"] == ["metric_policy_unavailable"]


def test_tool_catalog_exposes_only_exact_available_zero_argument_actions():
    session = FakeSession('trusted', version_id=None, deadline=time.monotonic() + 10)
    messages = agent._messages('question', [session.initialize(), session.call('definitions')])
    catalog = json.loads(messages[-1]['content'])['allowed_tool_actions']
    assert {a['tool'] for a in catalog} == FakeSession.tools - {'definitions'}
    assert all(set(a) == {'action', 'tool', 'parameters'} and a['parameters'] == {}
               for a in catalog)


def test_rejected_parameters_never_execute_or_get_silently_removed(monkeypatch):
    install_fake_session(monkeypatch)
    responses = iter([
        generation({'action': 'tool', 'tool': 'channel_funnel',
                    'parameters': {'metric': 'invented_scope'}}),
        generation({'action': 'tool', 'tool': 'channel_funnel', 'parameters': {}}),
        generation(finish(finding_codes=['missing_origin_leads'], evidence_ids=['e2'])),
    ])
    prompts = []

    def generate(messages, remaining):
        prompts.append(messages)
        return next(responses)

    result = agent.investigate('trusted', 'question', generate)
    assert result['status'] == 'completed'
    assert result['usage']['generations'] == 3
    assert result['usage']['tool_calls'] == 1
    assert result['trace'][0]['validation_error'] == 'unexpected_tool_parameters'
    assert result['trace'][0]['repair_requested'] is True
    assert 'invented_scope' not in json.dumps(prompts[1])
    assert len(json.loads(prompts[1][-1]['content'])['observations']) == 1


def test_json_and_argument_repairs_share_one_budget(monkeypatch):
    install_fake_session(monkeypatch)
    outputs = iter([
        {'output': 'invalid json'},
        generation({'action': 'tool', 'tool': 'definitions', 'parameters': {'metric': 'x'}}),
    ])
    result = agent.investigate('trusted', 'question', lambda *_: next(outputs))
    assert result['status'] == 'abstained'
    assert result['usage']['tool_calls'] == 0
    assert result['usage']['generations'] == 2
    assert result['limitations'] == ['unauthorized_action']


def test_unknown_tools_are_terminal_without_repair(monkeypatch):
    install_fake_session(monkeypatch)
    result = agent.investigate('trusted', 'question', lambda *_: generation(
        {'action': 'tool', 'tool': 'execute_sql', 'parameters': {}}))
    assert result['usage']['generations'] == 1
    assert result['usage']['tool_calls'] == 0
    assert result['limitations'] == ['unauthorized_action']


def test_finish_catalog_binds_findings_and_metric_refusal_to_observed_aliases():
    session = FakeSession('trusted', version_id=None, deadline=time.monotonic() + 10)
    evidence = [session.initialize()]
    initial = agent._finish_actions(evidence)
    assert not any('unsupported_metric' in a['limitation_codes'] for a in initial)
    assert not any('seller_sample_coverage_unknown' in a['finding_codes'] for a in initial)
    evidence.extend([session.call('channel_funnel'), session.call('seller_funnel'),
                     session.call('definitions')])
    actions = agent._finish_actions(evidence)
    coverage = next(a for a in actions if 'seller_sample_coverage_unknown' in a['finding_codes'])
    assert coverage['evidence_ids'] == ['e2']
    metric = next(a for a in actions if 'unsupported_metric' in a['limitation_codes'])
    assert metric['evidence_ids'] == ['e4']
    assert not any('recent_pipeline_failures' in a['finding_codes'] for a in actions)
