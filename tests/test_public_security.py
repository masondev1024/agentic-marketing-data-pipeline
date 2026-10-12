"""Public-ingress contract tests; all requests stop before provider or database code."""

import asyncio
import stat

import pytest

from llm_ops_lab.public_security import PublicBoundary, PublicConfig

TOKEN = b"public-test-token-with-at-least-32-bytes"
AUTH = (b"authorization", b"Bearer " + TOKEN)
BASE_HEADERS = [
    (b"host", b"api.example.test"),
    (b"x-forwarded-proto", b"https"),
]


def make_request(
    app,
    *,
    path="/v1/investigations",
    method="POST",
    headers=None,
    peer="127.0.0.1",
    state=None,
):
    received = False
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": list(BASE_HEADERS if headers is None else headers),
        "client": (peer, 50000),
        "server": ("127.0.0.1", 8080),
        "state": {} if state is None else state,
    }

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return scope, start, body


class RecordingApp:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append((scope["path"], scope["state"].copy()))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public, max-age=600")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})


def config(**kwargs):
    return PublicConfig("api.example.test", TOKEN, **kwargs)


def invoke(*, path="/v1/investigations", method="POST", extra_headers=None, **kwargs):
    headers = [*BASE_HEADERS, AUTH, (b"idempotency-key", b"request_001")]
    if extra_headers is not None:
        headers = [*BASE_HEADERS, *extra_headers]
    inner = RecordingApp()
    scope, response, body = make_request(
        PublicBoundary(inner, config(**kwargs)),
        path=path,
        method=method,
        headers=headers,
    )
    return inner, scope, response, body


def header_map(response):
    return {name.lower(): value for name, value in response["headers"]}


def test_public_config_validates_host_token_and_hides_secret():
    with pytest.raises(ValueError, match="hostname"):
        PublicConfig("https://api.example.test", TOKEN)
    with pytest.raises(ValueError, match="32 bytes"):
        PublicConfig("api.example.test", b"short")

    settings = config()
    assert TOKEN.decode() not in repr(settings)
    assert settings.verify_token(TOKEN)
    assert not settings.verify_token(b"wrong-token")


def test_token_can_be_loaded_from_environment_or_owner_only_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PUBLIC_TOKEN", TOKEN.decode())
    from_environment = PublicConfig.from_sources(
        allowed_host="api.example.test", token_env="TEST_PUBLIC_TOKEN", token_file=None
    )
    assert from_environment.verify_token(TOKEN)

    token_file = tmp_path / "api-token"
    token_file.write_bytes(TOKEN + b"\n")
    token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    from_file = PublicConfig.from_sources(
        allowed_host="api.example.test", token_file=token_file, token_env=None
    )
    assert from_file.verify_token(TOKEN)

    token_file.chmod(0o640)
    with pytest.raises(ValueError, match="mode 0600"):
        PublicConfig.from_sources(allowed_host="api.example.test", token_file=token_file)


def test_authenticated_investigation_sets_single_operator_and_security_headers():
    inner, scope, response, body = invoke()

    assert response["status"] == 200
    assert body == b"ok"
    assert len(inner.calls) == 1
    assert inner.calls[0][1]["principal"] == "operator"
    assert inner.calls[0][1]["principal_id"] == "operator"
    assert inner.calls[0][1]["idempotency_key"] == "request_001"
    assert scope["state"]["request_id"]
    headers = header_map(response)
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"content-security-policy"] == b"default-src 'none'; frame-ancestors 'none'"
    assert headers[b"strict-transport-security"] == b"max-age=86400"


@pytest.mark.parametrize(
    ("extra_headers", "status", "code"),
    [
        ([(b"authorization", b"Bearer wrong-token")], 401, "authentication_required"),
        ([(b"authorization", b"Bearer " + TOKEN), AUTH], 401, "authentication_required"),
        ([(b"idempotency-key", b"request_001")], 401, "authentication_required"),
        ([AUTH, (b"idempotency-key", b"request_001"), (b"idempotency-key", b"request_002")],
         400, "invalid_idempotency_key"),
        ([AUTH, (b"idempotency-key", b"short")], 400, "invalid_idempotency_key"),
        ([AUTH, (b"idempotency-key", b"request_001\xff")], 400, "invalid_idempotency_key"),
        ([AUTH, (b"idempotency-key", b"request_001"), (b"origin", b"https://evil.test")],
         403, "browser_origin_not_allowed"),
        ([AUTH, (b"idempotency-key", b"request_001"), (b"x-forwarded-proto", b"https")],
         400, "invalid_forwarded_proto"),
    ],
)
def test_rejects_bad_auth_origin_and_idempotency_without_dispatch(extra_headers, status, code):
    inner, _scope, response, body = invoke(extra_headers=extra_headers)
    import json

    assert response["status"] == status
    assert json.loads(body)["error"]["code"] == code
    assert inner.calls == []


def test_requires_exact_host_loopback_proxy_and_https_forwarding():
    inner = RecordingApp()
    boundary = PublicBoundary(inner, config())
    headers = [*BASE_HEADERS, AUTH, (b"idempotency-key", b"request_001")]
    scope, response, _ = make_request(
        boundary,
        headers=[(b"host", b"attacker.test"), *headers[1:]],
    )
    assert response["status"] == 400

    _scope, response, _ = make_request(
        boundary,
        headers=[(b"host", b"api.example.test"), *headers],
    )
    assert response["status"] == 400

    _scope, response, _ = make_request(boundary, headers=headers, peer="203.0.113.10")
    assert response["status"] == 403

    _scope, response, _ = make_request(
        boundary,
        headers=[(b"host", b"api.example.test"), (b"x-forwarded-proto", b"http"), *headers[2:]],
    )
    assert response["status"] == 400
    assert inner.calls == []


@pytest.mark.parametrize("path,method", [
    ("/v1/answers", "POST"),
    ("/health/ready", "GET"),
    ("/metrics", "GET"),
    ("/other", "GET"),
])
def test_only_investigations_and_health_metrics_are_routed(path, method):
    inner, _scope, response, _ = invoke(path=path, method=method, extra_headers=[])
    if path in {"/health/ready", "/metrics"}:
        assert response["status"] == 401
    else:
        assert response["status"] == 404
    assert inner.calls == []


def test_only_live_health_may_be_anonymous_and_can_be_disabled():
    inner, scope, response, _ = invoke(path="/health/live", method="GET", extra_headers=[])
    assert response["status"] == 200
    assert scope["state"]["principal"] == "anonymous-health"
    assert inner.calls[0][1]["principal"] == "anonymous-health"

    inner, _scope, response, _ = invoke(
        path="/health/live", method="GET", extra_headers=[], anonymous_live_health=False
    )
    assert response["status"] == 401
    assert inner.calls == []


def test_investigation_token_bucket_rejects_excess_requests():
    inner, _scope, response, _ = invoke(
        requests_per_minute=30, token_bucket_capacity=1
    )
    assert response["status"] == 200

    # Reuse one boundary so its token bucket state persists between calls.
    boundary = PublicBoundary(
        RecordingApp(), config(requests_per_minute=30, token_bucket_capacity=1)
    )
    _scope, first, _ = make_request(
        boundary,
        headers=[*BASE_HEADERS, AUTH, (b"idempotency-key", b"request_001")],
    )
    _scope, second, body = make_request(
        boundary,
        headers=[*BASE_HEADERS, AUTH, (b"idempotency-key", b"request_002")],
    )
    assert first["status"] == 200
    assert second["status"] == 429
    assert b"rate_limit_exceeded" in body
    assert header_map(second)[b"retry-after"] == b"1"


@pytest.mark.parametrize("field", ["requests_per_minute", "token_bucket_capacity"])
@pytest.mark.parametrize("value", [True, 1.5, "30"])
def test_rate_configuration_requires_bounded_integers(field, value):
    with pytest.raises(ValueError):
        PublicConfig("api.example.test", TOKEN, **{field: value})
