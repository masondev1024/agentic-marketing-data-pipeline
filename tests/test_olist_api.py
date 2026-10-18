import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from llm_ops_lab.api import create_app
from llm_ops_lab.service import ServiceConfig


class StubSupervisor:
    def __init__(self, record=None):
        self.record = record or {
            "status": "completed",
            "model_executed": True,
            "dataset_version": "a" * 64,
            "evidence": [{"table": "olist_mart.orders", "count": 4}],
            "findings": [{"code": "row_count", "value": 4}],
            "trace": {"query_count": 1},
            "usage": {"prompt_tokens": 20, "output_tokens": 12},
            "limitations": [],
            "diagnostic_mode": "read_only",
            "stop_reason": None,
            "pipeline_context": {"latest_run_status": "completed"},
            "metric_policy": {"version": "v1", "sha256": "c" * 64},
            "model": {"id": "pinned-model", "revision": "revision-1"},
            "prompt": {"version": "olist-investigator.v1", "sha256": "b" * 64},
        }
        self.submissions = []

    def start(self):
        pass

    async def close(self):
        pass

    async def submit(self, request_id, settings):
        self.submissions.append((request_id, settings))
        return {
            "request_id": request_id,
            "queue_seconds": 0.25,
            "request_seconds": 1.5,
            "service_processing_seconds": 1.0,
            **self.record,
        }


def api_client(tmp_path, supervisor, *, enabled=True):
    config = ServiceConfig(
        output_dir=tmp_path,
        olist_root=tmp_path if enabled else None,
    )
    return TestClient(
        create_app(config, supervisor=supervisor),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    )


def test_investigation_uses_supervisor_and_returns_bounded_diagnostic_fields(tmp_path):
    supervisor = StubSupervisor()
    with api_client(tmp_path, supervisor) as client:
        response = client.post(
            "/v1/investigations",
            json={"question": "주문 데이터의 상태를 진단해 주세요", "version_id": "c" * 64},
        )

    assert response.status_code == 200
    body = response.json()
    request_id, settings = supervisor.submissions[0]
    assert body["request_id"] == request_id == response.headers["x-request-id"]
    assert settings == {
        "operation": "olist_investigate",
        "question": "주문 데이터의 상태를 진단해 주세요",
        "version_id": "c" * 64,
    }
    assert body["status"] == "completed"
    assert body["model"]["id"] == "pinned-model"
    assert body["prompt"]["sha256"] == "b" * 64
    assert body["pipeline_context"]["latest_run_status"] == "completed"
    assert body["metric_policy"]["version"] == "v1"
    assert body["timing"] == {
        "queue_seconds": 0.25,
        "request_seconds": 1.5,
        "processing_seconds": 1.0,
    }
    assert "output" not in body and "sql" not in body and "dsn" not in body


def test_investigation_is_disabled_without_trusted_root(tmp_path):
    supervisor = StubSupervisor()
    with api_client(tmp_path, supervisor, enabled=False) as client:
        response = client.post("/v1/investigations", json={"question": "진단"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "investigations_disabled"
    assert supervisor.submissions == []


def test_semantic_validation_reaches_http_contract(tmp_path):
    supervisor = StubSupervisor()
    supervisor.record["semantic_validation"] = {
        "status": "passed", "contract_version": "olist.semantics.v1",
        "dataset_version": supervisor.record["dataset_version"],
    }
    with api_client(tmp_path, supervisor) as client:
        response = client.post("/v1/investigations", json={"question": "계약 연결 범위"})
    assert response.status_code == 200
    assert response.json()["semantic_validation"] == supervisor.record["semantic_validation"]


def test_investigation_rejects_unknown_fields_and_duplicate_keys(tmp_path):
    supervisor = StubSupervisor()
    with api_client(tmp_path, supervisor) as client:
        extra = client.post(
            "/v1/investigations",
            json={"question": "진단", "dsn": "secret", "sql": "SELECT 1"},
        )
        duplicate = client.post(
            "/v1/investigations",
            content=b'{"question":"first","question":"second"}',
            headers={"Content-Type": "application/json"},
        )
        invalid_version = client.post(
            "/v1/investigations", json={"question": "진단", "version_id": "not-a-digest"}
        )

    assert extra.status_code == duplicate.status_code == invalid_version.status_code == 422
    assert supervisor.submissions == []


def test_investigation_oversized_result_fails_closed_without_partial_evidence(tmp_path):
    supervisor = StubSupervisor({
        "request_id": "ignored",
        "status": "completed",
        "model_executed": True,
        "evidence": [{"text": "e" * (49 * 1024)}],
    })
    with api_client(tmp_path, supervisor) as client:
        response = client.post("/v1/investigations", json={"question": "진단"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "investigation_result_too_large"
    assert "evidence" not in response.json()


@pytest.mark.parametrize("engine,module_name", [
    ("action-v5", "llm_ops_lab.olist.agent"),
    ("intent-v1", "llm_ops_lab.olist.intent_agent"),
])
def test_worker_dispatches_investigation_with_fixed_runtime_and_persists(
    tmp_path, monkeypatch, engine, module_name
):
    from llm_ops_lab import service_worker
    from llm_ops_lab.olist import config as olist_config

    calls = {}
    module = types.ModuleType(module_name)

    def investigate(dsn, question, generate, *, version_id=None, timeout_seconds=60.0):
        calls.update(dsn=dsn, question=question, version_id=version_id, timeout=timeout_seconds)
        calls["generated"] = generate([{"role": "user", "content": "bounded prompt"}], 15)
        try:
            generate([{"role": "user", "content": "retry after failure"}], 10)
        except RuntimeError:
            pass
        return {
            "status": "completed",
            "model_executed": True,
            "model_id": None,
            "model_revision": None,
            "dataset_version": "d" * 64,
            "evidence": [{"table": "olist_mart.orders", "count": 1}],
            "findings": [],
            "trace": {"query_count": 1},
            "usage": {"prompt_tokens": 10, "output_tokens": 3},
        }

    module.investigate = investigate
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(
        olist_config,
        "connection_info",
        lambda root, reader=False: "trusted-reader-dsn",
    )

    class Runtime:
        identity = {"model_id": "fixed", "model_revision": "rev"}
        traces = []

        def generate(self, messages, limit, trace, *, seed, temperature):
            calls["runtime"] = (messages, limit, seed, temperature)
            self.traces.append(trace)
            if len(self.traces) > 1:
                raise RuntimeError("injected generation failure")
            trace.update(prompt_tokens=10, generation_tokens=3, output="bounded result")
            return trace

    runtime = Runtime()
    response = service_worker.process_request(
        {
            "model": "qwen3-4b",
            "output_dir": str(tmp_path),
            "olist_root": str(tmp_path), "olist_engine": engine,
            "request_timeout": 12,
        },
        {
            "request_id": "investigation-1",
            "settings": {
                "operation": "olist_investigate",
                "question": "주문 상태 진단",
                "version_id": "d" * 64,
            },
        },
        runtime,
    )

    assert response["result"]["status"] == "completed"
    assert calls["dsn"] == "trusted-reader-dsn"
    assert calls["timeout"] == 12
    assert calls["runtime"][1:] == (256, 42, 0)
    assert calls["generated"]["output"] == "bounded result"
    assert len(runtime.traces) == 2
    assert runtime.traces[0] is not runtime.traces[1]
    assert runtime.traces[1] == {}
    saved = json.loads((tmp_path / "investigation-1.json").read_text())
    assert saved["request_id"] == "investigation-1"
    assert saved["model_id"] == "fixed"


def test_worker_does_not_log_private_exception_text(tmp_path, monkeypatch, caplog):
    from llm_ops_lab import service_worker
    from llm_ops_lab.olist import config as olist_config

    module = types.ModuleType("llm_ops_lab.olist.agent")

    def investigate(*args, **kwargs):
        raise RuntimeError('password=private-driver-value')

    module.investigate = investigate
    monkeypatch.setitem(sys.modules, "llm_ops_lab.olist.agent", module)
    monkeypatch.setattr(olist_config, "connection_info", lambda *a, **kw: 'private-dsn')
    response = service_worker.process_olist_investigation(
        {'olist_root': str(tmp_path)},
        {'request_id': 'failed-test', 'settings': {'question': 'diagnose'}},
        object(),
    )
    assert response['error'] == 'investigation_failed'
    assert response['restart'] is True
    assert 'RuntimeError' in caplog.text
    assert 'private-driver-value' not in caplog.text
    assert 'private-dsn' not in caplog.text
