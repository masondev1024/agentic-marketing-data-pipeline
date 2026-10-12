"""Hosted Olist service boundaries without network or credential access."""

import json

import pytest
from fastapi.testclient import TestClient

from llm_ops_lab.api import create_app
from llm_ops_lab.olist import nvidia_service
from llm_ops_lab.olist.nvidia_planner import NvidiaPlannerError
from llm_ops_lab.service import ServiceConfig, ServiceError


def test_nvidia_provider_requires_intent_engine_and_trusted_root(tmp_path):
    with pytest.raises(ValueError, match="intent-v1 and --olist-root"):
        ServiceConfig(olist_provider="nvidia-super")
    with pytest.raises(ValueError, match="intent-v1 and --olist-root"):
        ServiceConfig(olist_provider="nvidia-super", olist_engine="action-v5", olist_root=tmp_path)

    config = ServiceConfig(
        olist_provider="nvidia-super", olist_engine="intent-v1", olist_root=tmp_path
    )
    assert config.olist_provider == "nvidia-super"
    assert config.olist_engine == "intent-v1"


def test_api_key_loader_prefers_environment_and_reads_owner_only_file(tmp_path, monkeypatch):
    secret = "test-nvidia-secret"
    monkeypatch.setenv("NVIDIA_API_KEY", f"  {secret}\n")
    assert nvidia_service.load_api_key(tmp_path) == secret

    monkeypatch.delenv("NVIDIA_API_KEY")
    key_file = tmp_path / "work/olist/secrets/nvidia-api-key.txt"
    key_file.parent.mkdir(parents=True)
    key_file.write_text(f"{secret}\n", encoding="utf-8")
    key_file.chmod(0o600)
    assert nvidia_service.load_api_key(tmp_path) == secret

    key_file.chmod(0o640)
    with pytest.raises(NvidiaPlannerError) as error:
        nvidia_service.load_api_key(tmp_path)
    assert error.value.code == "credentials_file_unsafe"
    assert secret not in str(error.value)


def test_blank_environment_key_fails_without_file_fallback(tmp_path, monkeypatch):
    key_file = tmp_path / "work/olist/secrets/nvidia-api-key.txt"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("file-secret", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.setenv("NVIDIA_API_KEY", " \n")

    with pytest.raises(NvidiaPlannerError) as error:
        nvidia_service.load_api_key(tmp_path)
    assert error.value.code == "credentials_missing"
    assert "file-secret" not in str(error.value)


def test_remote_runtime_skips_local_corpus_and_mlx_startup(tmp_path, monkeypatch):
    from llm_ops_lab import service_worker

    marker = object()
    calls = []
    monkeypatch.setattr(
        nvidia_service, "create_planner", lambda root: calls.append(root) or marker
    )
    monkeypatch.setattr(
        service_worker, "load_scope_contract", lambda *a, **k: pytest.fail("local corpus loaded")
    )
    monkeypatch.setattr(
        service_worker, "load_corpus", lambda *a, **k: pytest.fail("local corpus loaded")
    )
    monkeypatch.setattr(
        service_worker, "InferenceRuntime", lambda *a, **k: pytest.fail("MLX started")
    )

    runtime = service_worker.load_runtime(
        {
            "olist_provider": "nvidia-super",
            "olist_engine": "intent-v1",
            "olist_root": str(tmp_path),
        }
    )
    assert runtime is marker
    assert calls == [tmp_path]


class _FakePlanner:
    identity = {
        "model_id": nvidia_service.MODEL_ID,
        "model_revision": None,
        "provider": "nvidia_hosted",
        "revision_pinned": False,
    }

    def __init__(self, *, output=None, error=None):
        self.output = output
        self.error = error

    def generate(self, _messages, _timeout):
        if self.error:
            raise self.error
        return {
            "output": self.output,
            "prompt_tokens": 10,
            "output_tokens": 4,
            "model_id": nvidia_service.MODEL_ID,
            "model_revision": None,
        }


class _WorkerBackedSupervisor:
    """Run the real worker boundary in-process while preserving its HTTP mapping."""

    def __init__(self, config, runtime):
        self.config = config
        self.runtime = runtime
        self.submissions = []

    def start(self):
        pass

    async def close(self):
        pass

    def health(self):
        return {"ready": True}

    async def submit(self, request_id, settings):
        from llm_ops_lab.service_worker import process_request

        self.submissions.append(settings)
        response = process_request(
            self.config,
            {"request_id": request_id, "settings": settings},
            self.runtime,
        )
        if "error" in response:
            raise ServiceError(response["http_status"], response["error"])
        return {
            **response["result"],
            "queue_seconds": 0,
            "request_seconds": 0,
        }


def _remote_api(tmp_path, runtime):
    config = ServiceConfig(
        output_dir=tmp_path,
        olist_root=tmp_path,
        olist_engine="intent-v1",
        olist_provider="nvidia-super",
    )
    worker_config = {
        "output_dir": str(tmp_path),
        "olist_root": str(tmp_path),
        "olist_engine": "intent-v1",
        "olist_provider": "nvidia-super",
        "request_timeout": 10,
    }
    supervisor = _WorkerBackedSupervisor(worker_config, runtime)
    app = create_app(config, supervisor=supervisor)
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ), supervisor


@pytest.mark.parametrize("code,status", [
    ("authentication_failed", 503), ("rate_limited", 503),
    ("upstream_timeout", 504), ("upstream_transport_error", 502),
    ("upstream_unavailable", 502), ("invalid_upstream_response", 502),
    ("truncated_response", 502),
])
def test_remote_upstream_failure_is_persisted_sanitized_http_error(
    tmp_path, monkeypatch, code, status
):
    from llm_ops_lab.olist import config as olist_config

    monkeypatch.setattr(olist_config, "connection_info", lambda *a, **k: "private-dsn")
    secret = "must-not-be-echoed"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)
    runtime = _FakePlanner(error=NvidiaPlannerError(code))
    client, supervisor = _remote_api(tmp_path, runtime)

    with client:
        response = client.post("/v1/investigations", json={"question": "상태를 확인해 주세요"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == "nvidia_" + code
    assert secret not in response.text
    assert "private-dsn" not in response.text
    assert len(supervisor.submissions) == 1
    saved = json.loads((tmp_path / f"{response.headers['x-request-id']}.json").read_text())
    assert saved["status"] == "abstained"
    assert saved["provider_error"] == code
    assert saved["remote_call_attempts"] == 1
    assert secret not in json.dumps(saved)
    assert "private-dsn" not in json.dumps(saved)


def test_semantic_planner_abstention_remains_http_200(tmp_path, monkeypatch):
    from llm_ops_lab.olist import config as olist_config

    monkeypatch.setattr(
        olist_config,
        "connection_info",
        lambda *a, **k: "trusted-reader-dsn",
    )
    runtime = _FakePlanner(output='{"requests":["out_of_scope"]}')
    client, _supervisor = _remote_api(tmp_path, runtime)

    with client:
        response = client.post("/v1/investigations", json={"question": "계정별 매출 순위는?"})

    assert response.status_code == 200
    assert response.json()["status"] == "abstained"
    assert response.json()["stop_reason"] == "model_abstained"
    assert response.json()["model"]["id"] == nvidia_service.MODEL_ID


def test_answers_endpoint_is_disabled_for_hosted_provider(tmp_path):
    class NeverSubmit:
        submissions = []

        def start(self):
            pass

        async def close(self):
            pass

        def health(self):
            return {"ready": True}

        async def submit(self, request_id, settings):
            self.submissions.append(settings)
            raise AssertionError("hosted mode must not submit /v1/answers")

    config = ServiceConfig(
        output_dir=tmp_path,
        olist_root=tmp_path,
        olist_engine="intent-v1",
        olist_provider="nvidia-super",
    )
    supervisor = NeverSubmit()
    with TestClient(
        create_app(config, supervisor=supervisor),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as client:
        response = client.post("/v1/answers", json={"question": "hello"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "answers_disabled_for_provider"
    assert supervisor.submissions == []


def test_hosted_adapter_repairs_plan_then_reads_evidence_without_sending_db_data(
    tmp_path, monkeypatch
):
    import httpx
    from test_olist_agent import FakeSession

    from llm_ops_lab import service_worker
    from llm_ops_lab.olist import config as olist_config
    from llm_ops_lab.olist import intent_agent
    from llm_ops_lab.olist.nvidia_planner import ENDPOINT, NvidiaPlanner

    monkeypatch.setattr(olist_config, "connection_info", lambda *a, **k: "private-reader-dsn")
    monkeypatch.setattr(intent_agent, "OlistToolSession", FakeSession)
    sent = []

    def respond(request):
        assert str(request.url) == ENDPOINT
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload["model"] == nvidia_service.MODEL_ID
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        output = "invalid-json" if len(sent) == 1 else '{"requests":["coverage"]}'
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": output}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        })

    with NvidiaPlanner(model_id=nvidia_service.MODEL_ID, api_key="fake-key",
                       transport=httpx.MockTransport(respond)) as runtime:
        result = service_worker.process_request(
            {"olist_root": str(tmp_path), "output_dir": str(tmp_path),
             "olist_provider": "nvidia-super", "olist_engine": "intent-v1"},
            {"request_id": "hosted-repair", "timeout_seconds": 10,
             "settings": {"operation": "olist_investigate", "question": "coverage"}},
            runtime,
        )["result"]
    assert result["status"] == "completed"
    assert result["remote_call_attempts"] == result["usage"]["generations"] == 2
    assert result["usage"]["tool_calls"] == 1
    assert result["findings"][0]["code"] == "seller_sample_coverage_unknown"
    assert result["revision_pinned"] is False
    for payload in sent:
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        user = json.loads(payload["messages"][1]["content"])
        assert set(user) <= {"question_untrusted", "correction"}
        assert "private-reader-dsn" not in json.dumps(payload)
        assert "fake-key" not in json.dumps(payload)
    assert "correction" in json.loads(sent[1]["messages"][1]["content"])
