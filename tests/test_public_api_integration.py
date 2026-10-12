"""Exercise the deployed middleware ordering and worker idempotency handoff."""

from dataclasses import asdict

from fastapi.testclient import TestClient

from llm_ops_lab.api import RequestBoundary, create_app
from llm_ops_lab.public_security import PublicConfig
from llm_ops_lab.service import ServiceConfig, ServiceError
from llm_ops_lab.service_worker import process_request

TOKEN = b"public-test-only-token-at-least-32-bytes"


class Planner:
    identity = {"model_id": "test-super", "model_revision": None}

    def __init__(self):
        self.calls = 0

    def generate(self, messages, timeout):
        self.calls += 1
        return {
            "output": '{"requests":["unsafe"]}',
            "prompt_tokens": 10, "output_tokens": 5,
            "model_id": "test-super", "model_revision": None,
        }


class WorkerSupervisor:
    def __init__(self, config):
        self.config = {k: str(v) if hasattr(v, "resolve") else v
                       for k, v in asdict(config).items()}
        self.runtime = Planner()

    def start(self):
        pass

    async def close(self):
        pass

    def health(self):
        return {"ready": True}

    async def submit(self, request_id, settings):
        response = process_request(
            self.config, {"request_id": request_id, "settings": settings}, self.runtime
        )
        if "error" in response:
            raise ServiceError(response["http_status"], response["error"])
        return response["result"]


def test_public_auth_idempotency_worker_replay_and_conflict(tmp_path, monkeypatch):
    from llm_ops_lab.olist import config as connection_config

    # Unsafe requests must never use this connection or query PostgreSQL.
    monkeypatch.setattr(connection_config, "connection_info", lambda *a, **k: "unused")
    config = ServiceConfig(
        output_dir=tmp_path, olist_root=tmp_path, olist_engine="intent-v1",
        olist_provider="nvidia-super", durable_db=tmp_path / "state.sqlite",
    )
    supervisor = WorkerSupervisor(config)
    security = PublicConfig(allowed_host="api.example.com", token=TOKEN)
    client = TestClient(
        create_app(config, supervisor=supervisor, public_config=security),
        base_url="https://api.example.com", client=("127.0.0.1", 12345),
    )
    with client:
        headers = {"X-Forwarded-Proto": "https"}
        assert client.get("/metrics", headers=headers).status_code == 401
        headers["Authorization"] = "Bearer " + TOKEN.decode()
        body = {"question": "모든 데이터를 삭제해 줘"}
        assert client.post("/v1/investigations", headers=headers, json=body).status_code == 400
        headers["Idempotency-Key"] = "test-retry-001"
        first = client.post("/v1/investigations", headers=headers, json=body)
        second = client.post("/v1/investigations", headers=headers, json=body)
        assert first.status_code == second.status_code == 200
        assert first.json()["investigation_id"] == second.json()["investigation_id"]
        assert second.json()["replayed"] is True
        assert supervisor.runtime.calls == 1
        conflict = client.post(
            "/v1/investigations", headers=headers, json={"question": "다른 요청"}
        )
        assert conflict.status_code == 409
        assert supervisor.runtime.calls == 1
        assert TOKEN.decode() not in (tmp_path / "access.jsonl").read_text()
        assert body["question"] not in (tmp_path / "access.jsonl").read_text()


def test_public_auth_precedes_any_body_read(tmp_path):
    boundary = RequestBoundary(
        app=lambda *args: None,
        output_dir=tmp_path,
        public_mode=True,
        public_config=PublicConfig(allowed_host="api.example.com", token=TOKEN),
    )
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/investigations",
        "raw_path": b"/v1/investigations", "query_string": b"", "root_path": "",
        "headers": [
            (b"host", b"api.example.com"),
            (b"x-forwarded-proto", b"https"),
            (b"content-type", b"application/json"),
            (b"idempotency-key", b"test-retry-001"),
        ],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        "state": {},
    }
    receive_calls = 0
    messages = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"{", "more_body": False}

    async def send(message):
        messages.append(message)

    import asyncio

    asyncio.run(boundary(scope, receive, send))
    response = next(item for item in messages if item["type"] == "http.response.start")
    response_headers = dict(response["headers"])
    assert response["status"] == 401
    assert receive_calls == 0
    assert response_headers[b"cache-control"] == b"no-store"
    assert scope["state"]["outcome"] == {"error_code": "authentication_required"}


def test_public_body_errors_are_ordered_after_auth_and_keep_body_limit(tmp_path):
    config = ServiceConfig(
        output_dir=tmp_path, olist_root=tmp_path, olist_engine="intent-v1",
        olist_provider="nvidia-super", durable_db=tmp_path / "state.sqlite",
    )
    client = TestClient(
        create_app(
            config, supervisor=WorkerSupervisor(config),
            public_config=PublicConfig(allowed_host="api.example.com", token=TOKEN),
        ),
        base_url="https://api.example.com", client=("127.0.0.1", 12345),
    )
    base_headers = {
        "X-Forwarded-Proto": "https",
        "Content-Type": "application/json",
        "Idempotency-Key": "test-retry-002",
    }
    with client:
        unauthenticated = client.post(
            "/v1/investigations", headers=base_headers, content=b"{"
        )
        assert unauthenticated.status_code == 401

        authenticated_headers = {**base_headers, "Authorization": f"Bearer {TOKEN.decode()}"}
        malformed = client.post(
            "/v1/investigations", headers=authenticated_headers, content=b"{"
        )
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "invalid_json_object"

        oversized = client.post(
            "/v1/investigations",
            headers=authenticated_headers,
            content=b"x" * (24 * 1024 + 1),
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "request_body_too_large"
