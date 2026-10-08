import json
from dataclasses import FrozenInstanceError

import pytest

from llm_ops_lab.olist.intent_plan import (
    DISPOSITIONS,
    INTENT_FINDINGS,
    INTENT_TOOLS,
    MAX_PLAN_BYTES,
    PROMPT_VERSION,
    REFUSAL_PRECEDENCE,
    REQUEST_LABELS,
    SUPPORTED_INTENTS,
    Plan,
    PlanError,
    parse_plan,
    plan_messages,
    prompt_bytes,
)


def encode_requests(labels, **extra):
    return json.dumps({"requests": labels, **extra}, ensure_ascii=False)


def test_parser_derives_supported_plan_from_one_label_axis():
    coverage = parse_plan(encode_requests(["coverage"]))
    assert coverage == Plan("supported", ("coverage",))

    composed = parse_plan(encode_requests(["origin", "pipeline_history", "quality"]))
    assert composed == Plan("supported", ("origin", "pipeline_history", "quality"))
    assert composed.disposition in DISPOSITIONS
    assert set(composed.intents) <= SUPPORTED_INTENTS

    with pytest.raises(FrozenInstanceError):
        composed.disposition = "unsafe"


def test_parser_rejects_the_old_dual_axis_wire_format():
    with pytest.raises(PlanError):
        parse_plan('{"disposition":"coverage","intents":["coverage"]}')
    with pytest.raises(PlanError):
        parse_plan('{"disposition":"supported","intents":["origin"]}')


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["origin", "unsafe"], "unsafe"),
        (["unsafe", "financial_unsupported"], "unsafe"),
        (["financial_unsupported", "unavailable_root_cause"], "financial_unsupported"),
        (["unavailable_root_cause", "out_of_scope"], "unavailable_root_cause"),
        (["out_of_scope", "ambiguous"], "out_of_scope"),
        (["ambiguous", "coverage"], "ambiguous"),
    ],
)
def test_host_refusal_precedence_abstains_for_the_whole_request(labels, expected):
    plan = parse_plan(encode_requests(labels))
    assert plan == Plan(expected, ())


def test_all_refusal_precedence_levels_are_explicit_and_ordered():
    assert REFUSAL_PRECEDENCE == (
        "unsafe",
        "financial_unsupported",
        "unavailable_root_cause",
        "out_of_scope",
        "ambiguous",
    )
    mixed = parse_plan(encode_requests(list(REFUSAL_PRECEDENCE)))
    assert mixed == Plan("unsafe", ())


@pytest.mark.parametrize(
    "raw",
    [
        '{"requests":["coverage"],"requests":["origin"]}',
        '{"requests":["coverage"],"sql":"SELECT 1"}',
        '{"requests":["coverage"],"disposition":"supported"}',
        '{"requests":[]}',
        '{"requests":["coverage","coverage"]}',
        '{"requests":["unknown"]}',
        '{"requests":["sample_scope"]}',
        '{"requests":[{"tool":"seller_funnel"}]}',
        '{"requests":"coverage"}',
        '{"requests":["origin","coverage","pipeline_history","quality"]}',
        '{"requests":["origin"],"x":NaN}',
        '{"requests":["origin"],"x":Infinity}',
        '["coverage"]',
        'null',
        '{',
    ],
)
def test_parser_rejects_malformed_duplicate_or_unsafe_plans(raw):
    with pytest.raises(PlanError) as error:
        parse_plan(raw)
    assert error.value.code == "invalid_plan"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b'{"requests":["coverage"]}',
        True,
    ],
)
def test_parser_requires_string_input(raw):
    with pytest.raises(PlanError) as error:
        parse_plan(raw)
    assert error.value.code == "invalid_plan"


def test_parser_enforces_utf8_byte_limit_and_rejects_surrogates():
    with pytest.raises(PlanError):
        parse_plan(" " * (MAX_PLAN_BYTES + 1))
    with pytest.raises(PlanError):
        parse_plan('{"requests":["\\ud800"]}')


def test_intent_mappings_compose_only_registered_tools_and_relevant_findings():
    plan = parse_plan(encode_requests(["origin", "coverage", "pipeline_history"]))
    tools = {tool for intent in plan.intents for tool in INTENT_TOOLS[intent]}
    findings = {code for intent in plan.intents for code in INTENT_FINDINGS[intent]}
    assert tools == {"channel_funnel", "run_history"}
    assert findings == {
        "missing_origin_leads",
        "seller_sample_coverage_unknown",
        "recent_pipeline_failures",
        "abandoned_pipeline_runs",
        "pipeline_runs_in_progress",
    }
    assert set(INTENT_TOOLS) == {"origin", "coverage", "pipeline_history", "quality"}
    assert set(INTENT_FINDINGS) == set(INTENT_TOOLS)
    assert INTENT_TOOLS["quality"] == ()
    assert INTENT_FINDINGS["quality"] == ("published_quality_warnings",)
    assert "sample_scope" not in REQUEST_LABELS
    assert "published_sample_only" not in findings
    assert REQUEST_LABELS == SUPPORTED_INTENTS | set(REFUSAL_PRECEDENCE)


def test_plan_messages_use_versioned_prompt_without_database_observations():
    messages = plan_messages("유입 경로를 확인해 주세요.")
    prompt = messages[0]["content"]
    assert PROMPT_VERSION == "olist-intent.v7"
    assert prompt_bytes()
    assert "requests" in prompt
    assert "sample_scope" not in prompt
    assert all(label in prompt for label in REQUEST_LABELS)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "supported_finish_actions" not in messages[1]["content"]
    assert "observations" not in messages[1]["content"]
    assert json.loads(messages[1]["content"]) == {
        "question_untrusted": "유입 경로를 확인해 주세요."
    }

    with pytest.raises(PlanError) as error:
        plan_messages("   ")
    assert error.value.code == "invalid_question"
    with pytest.raises(PlanError):
        plan_messages("x" * 2_049)


def test_plan_messages_include_only_bounded_correction():
    question = "유입 경로를 확인해 주세요."
    messages = plan_messages(question, "등록된 라벨만 사용하세요.")
    payload = json.loads(messages[1]["content"])
    assert payload == {
        "question_untrusted": question,
        "correction": "등록된 라벨만 사용하세요.",
    }
    with pytest.raises(PlanError):
        plan_messages(question, "x" * 513)
