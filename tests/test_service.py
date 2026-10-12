import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_ops_lab.api import MAX_BODY_BYTES, RequestBoundary, create_app
from llm_ops_lab.service import ServiceConfig, ServiceError, Supervisor

FIXTURE = Path(__file__).with_name("fixtures") / "service_worker.py"


def make_supervisor(tmp_path, **kwargs):
    config = ServiceConfig(output_dir=tmp_path, startup_timeout=3, **kwargs)
    return config, Supervisor(config, worker_command=[sys.executable, str(FIXTURE)])


async def wait_ready(supervisor, *, old_pid=None):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if supervisor.ready and supervisor.process.pid != old_pid:
            return supervisor.process.pid
        await asyncio.sleep(0.01)
    raise AssertionError(supervisor.health())


async def request(supervisor, question, key="id"):
    return await supervisor.submit(key, {"question": question})


@pytest.mark.parametrize(
    "field,value",
    [
        ("queue_size", -1),
        ("queue_size", 9),
        ("request_timeout", float("nan")),
        ("request_timeout", 0),
        ("startup_timeout", 301),
        ("max_restarts", -1),
    ],
)
def test_service_configuration_is_bounded(field, value):
    with pytest.raises(ValueError):
        ServiceConfig(**{field: value})


def test_process_reuse_bounded_admission_and_cleanup(tmp_path):
    async def run():
        _, supervisor = make_supervisor(tmp_path, queue_size=1)
        supervisor.start()
        try:
            pid = await wait_ready(supervisor)
            first = asyncio.create_task(request(supervisor, "slow", "one"))
            second = asyncio.create_task(request(supervisor, "slow", "two"))
            await asyncio.sleep(0.05)
            with pytest.raises(ServiceError) as error:
                await request(supervisor, "third", "three")
            assert error.value.status == 429
            a, b = await asyncio.gather(first, second)
            assert a["worker_pid"] == b["worker_pid"] == pid
            assert b["queue_seconds"] >= 0.2
            assert supervisor.health()["active"] == supervisor.health()["queued"] == 0
        finally:
            await supervisor.close()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(run())


@pytest.mark.parametrize("question,status", [("hang", 504), ("crash", 503), ("invalid-ipc", 503)])
def test_timeout_or_crash_reaps_before_new_worker_and_fails_queue(tmp_path, question, status):
    async def run():
        _, supervisor = make_supervisor(tmp_path, queue_size=1, request_timeout=0.3)
        supervisor.start()
        try:
            pid = await wait_ready(supervisor)
            first = asyncio.create_task(request(supervisor, question, "one"))
            second = asyncio.create_task(request(supervisor, "never-run", "two"))
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert results[0].status == status
            assert results[1].status == 503
            await wait_ready(supervisor, old_pid=pid)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert (await request(supervisor, "normal", "three"))["status"] == "generated"
            started = [
                json.loads(x) for x in (tmp_path / "fixture-started.jsonl").read_text().splitlines()
            ]
            assert all(r["settings"]["question"] != "never-run" for r in started)
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_expired_queued_job_never_sent_and_does_not_restart_worker(tmp_path):
    async def run():
        _, supervisor = make_supervisor(tmp_path, queue_size=1)
        supervisor.start()
        try:
            pid = await wait_ready(supervisor)
            first = asyncio.create_task(request(supervisor, "slow", "one"))
            second = asyncio.create_task(request(supervisor, "expired", "two"))
            await asyncio.sleep(0.05)
            supervisor.jobs["two"].deadline = time.monotonic() - 1
            await first
            with pytest.raises(ServiceError) as error:
                await second
            assert error.value.code == "queue_deadline_exceeded"
            assert supervisor.process.pid == pid
            assert "expired" not in (tmp_path / "fixture-started.jsonl").read_text()
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_request_cancellation_does_not_free_active_capacity(tmp_path):
    async def run():
        _, supervisor = make_supervisor(tmp_path, queue_size=0)
        supervisor.start()
        try:
            await wait_ready(supervisor)
            task = asyncio.create_task(request(supervisor, "slow"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ServiceError) as error:
                await request(supervisor, "second", "second")
            assert error.value.status == 429
            await asyncio.sleep(0.4)
            assert (await request(supervisor, "third", "third"))["status"] == "generated"
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_shutdown_fails_active_and_pending_requests(tmp_path):
    async def run():
        _, supervisor = make_supervisor(tmp_path, queue_size=1)
        supervisor.start()
        pid = await wait_ready(supervisor)
        first = asyncio.create_task(request(supervisor, "hang", "one"))
        second = asyncio.create_task(request(supervisor, "never-run", "two"))
        await asyncio.sleep(0.05)
        await supervisor.close()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(r.status == 503 for r in results)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(run())


def test_startup_timeout_and_restart_budget_are_finite(tmp_path):
    async def run():
        config = ServiceConfig(output_dir=tmp_path, startup_timeout=0.15, max_restarts=1)
        supervisor = Supervisor(
            config, worker_command=[sys.executable, str(FIXTURE), "startup-hang"]
        )
        supervisor.start()
        try:
            deadline = time.monotonic() + 5
            while supervisor.state != "failed" and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert supervisor.state == "failed"
            assert supervisor.restarts == 1
            assert supervisor.process is None
            with pytest.raises(ServiceError) as error:
                await request(supervisor, "normal")
            assert error.value.status == 503
        finally:
            await supervisor.close()

    asyncio.run(run())


@pytest.fixture
def client(tmp_path):
    config, supervisor = make_supervisor(tmp_path)
    with TestClient(
        create_app(config, supervisor=supervisor),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as client:
        deadline = time.monotonic() + 5
        while client.get("/health/ready").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.get("/health/ready").status_code == 200
        yield client


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": " "},
        {"question": "x" * 4097},
        {"question": "q", "model": "../../private"},
        {"question": "q", "max_tokens": 513},
        {"question": "q", "seed": -1},
        {"question": "q", "temperature": "0"},
        {"question": "q", "max_tokens": True},
        {"question": "q", "answer_mode": "other"},
    ],
)
def test_http_contract_rejects_invalid_input_without_echo(client, body):
    response = client.post("/v1/answers", json=body)
    assert response.status_code == 422
    assert set(response.json()) == {"request_id", "error"}
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert client.get("/metrics").json()["counters"]["accepted"] == 0


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{bad",
        b'{"question":"q","question":"r"}',
        b'{"question":"q","temperature":NaN}',
        b'{"question":"\\ud800"}',
    ],
)
def test_http_rejects_malformed_json_and_surrogates(client, body):
    response = client.post(
        "/v1/answers", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_http_size_host_origin_and_media_type_boundaries(client):
    assert (
        client.post(
            "/v1/answers",
            content=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/v1/answers", json={"question": "q"}, headers={"Origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
    assert client.post("/v1/answers", content="q").status_code == 415
    assert client.get("/health/live", headers={"Host": "untrusted.example"}).status_code == 400


def test_http_response_and_audit_link_without_logging_prompt(client, tmp_path):
    response = client.post("/v1/answers", json={"question": "private-test-marker"})
    assert response.status_code == 200
    body = response.json()
    assert body["validation"]["semantic_quality"] == "unverified"
    assert len(response.headers.get_list("x-request-id")) == 1
    logs = (tmp_path / "access.jsonl").read_text()
    assert body["request_id"] in logs and "private-test-marker" not in logs


def test_audit_failure_does_not_return_success(client, tmp_path):
    # Force a real filesystem write error without removing existing logs.
    (tmp_path / "access.jsonl").rename(tmp_path / "access.before.jsonl")
    (tmp_path / "access.jsonl").mkdir()
    response = client.post("/v1/answers", json={"question": "normal"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "audit_write_failed"


def test_chunked_body_limit_applies_without_content_length(tmp_path):
    async def run():
        config, supervisor = make_supervisor(tmp_path)
        app = create_app(config, supervisor=supervisor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:

                async def chunks():
                    yield b"x" * MAX_BODY_BYTES
                    yield b"y"

                response = await client.post(
                    "/v1/answers", content=chunks(), headers={"Content-Type": "application/json"}
                )
                assert response.status_code == 413

    asyncio.run(run())


def test_slow_body_times_out_before_app_or_worker(tmp_path, monkeypatch):
    import llm_ops_lab.api as api

    monkeypatch.setattr(api, "BODY_TIMEOUT_SECONDS", 0.05)

    async def run():
        called = []

        async def app(*args):
            called.append(True)

        boundary = RequestBoundary(app, tmp_path)

        async def receive():
            await asyncio.sleep(1)

        messages = []

        async def send(message):
            messages.append(message)

        await boundary(
            {
                "type": "http",
                "client": ("127.0.0.1", 12345),
                "method": "POST",
                "path": "/v1/answers",
                "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 408 and called == []
        assert boundary.inflight == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["direct", "evidence-v1"])
def test_token_overflow_is_input_error_without_restart_in_both_modes(tmp_path, mode):
    from llm_ops_lab.cli import MODEL_SPECS
    from llm_ops_lab.inference import InputLimitError
    from llm_ops_lab.service_worker import process_request

    class Runtime:
        model_spec = MODEL_SPECS["qwen3-4b"]
        identity = {"model_id": model_spec["id"], "model_revision": model_spec["revision"]}

        class mx:
            @staticmethod
            def get_peak_memory():
                return 0

        def generate(self, messages, limit, trace, **kwargs):
            trace.update(prompt_tokens=2100, model_executed=False)
            raise InputLimitError("Input exceeds budget")

    response = process_request(
        {"model": "qwen3-4b", "corpus_dir": "data/runbooks", "output_dir": str(tmp_path)},
        {
            "request_id": "overflow",
            "settings": {
                "question": "멱등성은 무엇인가?",
                "temperature": 0,
                "max_tokens": 128,
                "seed": 42,
                "answer_mode": mode,
            },
        },
        Runtime(),
    )
    assert response["http_status"] == 422 and response["restart"] is False
    record = json.loads((tmp_path / "overflow.json").read_text())
    assert record["model_executed"] is False
    assert record["prompt_tokens"] == 2100


def test_partial_direct_failure_preserves_execution_provenance(tmp_path):
    from llm_ops_lab.cli import MODEL_SPECS
    from llm_ops_lab.service_worker import process_request

    class Runtime:
        model_spec = MODEL_SPECS["qwen3-4b"]
        identity = {"model_id": model_spec["id"], "model_revision": model_spec["revision"]}

        def generate(self, messages, limit, trace, **kwargs):
            trace.update(
                model_executed=True,
                prompt_tokens=100,
                generation_tokens=2,
                generation_seconds=0.1,
                output="partial",
            )
            raise RuntimeError("injected backend failure")

    response = process_request(
        {"model": "qwen3-4b", "corpus_dir": "data/runbooks", "output_dir": str(tmp_path)},
        {
            "request_id": "partial",
            "settings": {
                "question": "멱등성은 무엇인가?",
                "temperature": 0,
                "max_tokens": 128,
                "seed": 42,
                "answer_mode": "direct",
            },
        },
        Runtime(),
    )
    assert response["http_status"] == 500 and "result" not in response
    record = json.loads((tmp_path / "partial.json").read_text())
    assert record["model_executed"] is True
    assert record["model_id"] == Runtime.model_spec["id"]
    assert record["generation_tokens"] == 2 and record["generation_seconds"] == 0.1
    assert record["output"] == ""


def test_slow_audit_does_not_block_event_loop_and_remains_admission_bounded(tmp_path, monkeypatch):
    import llm_ops_lab.api as api

    original = api.append_access_event

    def slow_write(path, event):
        time.sleep(0.2)
        original(path, event)

    monkeypatch.setattr(api, "append_access_event", slow_write)

    async def run():
        config, supervisor = make_supervisor(tmp_path)
        app = create_app(config, supervisor=supervisor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                task = asyncio.create_task(client.get("/health/live"))
                started = time.monotonic()
                await asyncio.sleep(0.05)
                assert time.monotonic() - started < 0.15
                assert (await task).status_code == 200

    asyncio.run(run())


def test_audit_admission_remains_bounded_after_client_cancellation(tmp_path, monkeypatch):
    import threading

    import llm_ops_lab.api as api

    entered, release = threading.Event(), threading.Event()

    def blocked_write(*args):
        entered.set()
        release.wait(3)

    monkeypatch.setattr(api, "MAX_HTTP_REQUESTS", 1)
    monkeypatch.setattr(api, "append_access_event", blocked_write)

    async def run():
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        boundary = RequestBoundary(app, tmp_path)
        scope = {
            "type": "http",
            "client": ("127.0.0.1", 12345),
            "method": "GET",
            "path": "/health/live",
            "headers": [(b"host", b"127.0.0.1")],
        }

        async def receive():
            return {"type": "http.disconnect"}

        first_messages = []

        async def first_send(message):
            first_messages.append(message)

        task = asyncio.create_task(boundary(dict(scope), receive, first_send))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.005)
            rejected = []

            async def reject_send(message):
                rejected.append(message)

            await boundary(dict(scope), receive, reject_send)
            assert rejected[0]["status"] == 429
            assert len(boundary.audit_tasks) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert boundary.inflight == 0 and len(boundary.audit_tasks) == 1
            rejected.clear()
            await boundary(dict(scope), receive, reject_send)
            assert rejected[0]["status"] == 503
            assert len(boundary.audit_tasks) == 1
        finally:
            release.set()
            await asyncio.gather(*list(boundary.audit_tasks))

    asyncio.run(run())


def test_remote_peer_cannot_spoof_loopback_host(tmp_path):
    async def run():
        config, supervisor = make_supervisor(tmp_path)
        app = create_app(config, supervisor=supervisor)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 12345))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/health/live")
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "loopback_client_required"

    asyncio.run(run())


def test_record_write_failure_does_not_restart_healthy_worker(tmp_path, monkeypatch):
    from llm_ops_lab import service_worker

    def fail_write(*args):
        raise OSError("injected storage failure")

    monkeypatch.setattr(service_worker, "save_result", fail_write)
    response = service_worker.process_request(
        {"model": "qwen3-4b", "corpus_dir": "data/runbooks", "output_dir": str(tmp_path)},
        {
            "request_id": "storage",
            "settings": {
                "question": "오늘 서울 날씨는?",
                "temperature": 0,
                "max_tokens": 128,
                "seed": 42,
                "answer_mode": "direct",
            },
        },
        None,
    )
    assert response == {
        "request_id": "storage",
        "error": "storage_write_failed",
        "http_status": 500,
        "restart": False,
    }


def test_remote_reservation_survives_worker_restart_and_never_refunds(tmp_path):
    async def run():
        _, supervisor = make_supervisor(
            tmp_path, olist_provider="nvidia-super", olist_engine="intent-v1",
            olist_root=tmp_path, remote_call_budget=4, request_timeout=0.2,
        )
        supervisor.start()
        try:
            pid = await wait_ready(supervisor)
            with pytest.raises(ServiceError, match="inference_deadline_exceeded"):
                await supervisor.submit("timeout", {"operation": "olist_investigate",
                                                    "question": "hang"})
            await wait_ready(supervisor, old_pid=pid)
            assert supervisor.health()["remote_budget"]["reserved"] == 2
            await supervisor.submit("ok", {"operation": "olist_investigate", "question": "ok"})
            with pytest.raises(ServiceError, match="remote_call_budget_exhausted"):
                await supervisor.submit("over", {"operation": "olist_investigate",
                                                 "question": "never-run"})
            assert supervisor.health()["remote_budget"] == {
                "limit": 4, "reserved": 4, "remaining": 0, "scope": "api_process_lifetime",
            }
            assert supervisor.counters["remote_budget_rejected"] == 1
            started = [json.loads(line) for line in
                       (tmp_path / "fixture-started.jsonl").read_text().splitlines()]
            assert [r["request_id"] for r in started] == ["timeout", "ok"]
            assert 0 < started[0]["timeout_seconds"] <= 0.2
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_remote_cancelled_queue_does_not_spend_budget_and_active_slot_is_held(tmp_path):
    async def run():
        _, supervisor = make_supervisor(
            tmp_path, olist_provider="nvidia-super", olist_engine="intent-v1",
            olist_root=tmp_path, remote_call_budget=4, queue_size=1,
        )
        supervisor.start()
        try:
            await wait_ready(supervisor)
            active = asyncio.create_task(supervisor.submit(
                "active", {"operation": "olist_investigate", "question": "slow"}))
            queued = asyncio.create_task(supervisor.submit(
                "queued", {"operation": "olist_investigate", "question": "never-run"}))
            await asyncio.sleep(0.05)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert supervisor.health()["active"] == 1
            assert supervisor.health()["remote_budget"]["reserved"] == 2
            with pytest.raises(ServiceError, match="capacity_exceeded"):
                await supervisor.submit("extra", {"question": "never-run"})
            for _ in range(100):
                if not supervisor.jobs:
                    break
                await asyncio.sleep(0.01)
            assert not supervisor.jobs
            assert supervisor.health()["remote_budget"]["reserved"] == 2
            started = (tmp_path / "fixture-started.jsonl").read_text()
            assert "never-run" not in started
        finally:
            await supervisor.close()

    asyncio.run(run())
