"""Actual PostgreSQL execution of typed plans; model semantics tested separately."""

import json

import pytest
from test_olist_postgres import ADMIN
from test_olist_postgres import database as database
from test_olist_validation import _make_fixture

from llm_ops_lab.olist import intent_agent
from llm_ops_lab.olist.pipeline import run_pipeline

pytestmark = pytest.mark.skipif(not ADMIN, reason="dedicated Olist PostgreSQL not enabled")


def test_typed_plan_executes_real_version_bound_query_once(tmp_path, database):
    writer, reader = database
    published = run_pipeline(tmp_path, writer, intake_path=_make_fixture(tmp_path))
    result = intent_agent.investigate(
        reader,
        "seller linkage",
        lambda *_: {
            "output": json.dumps({"requests": ["coverage"]}),
            "prompt_tokens": 40,
            "generation_tokens": 8,
        },
    )
    assert result["status"] == "completed"
    assert result["dataset_version"] == published["version_id"]
    assert result["usage"]["tool_calls"] == 1
    assert [e["tool"] for e in result["evidence"]] == ["version_evidence", "channel_funnel"]
    assert [f["code"] for f in result["findings"]] == ["seller_sample_coverage_unknown"]


def test_refusal_has_no_dependency_on_database_availability():
    # Port zero would fail if the engine touched the database.
    result = intent_agent.investigate(
        "host=127.0.0.1 port=0 dbname=absent",
        "unsafe",
        lambda *_: {
            "output": json.dumps({"requests": ["unsafe", "origin"]}),
            "prompt_tokens": 40,
            "generation_tokens": 8,
        },
    )
    assert result["stop_reason"] == "model_abstained"
    assert result["request_plan"]["disposition"] == "unsafe"
    assert result["evidence"] == []
    assert result["usage"]["tool_calls"] == 0
