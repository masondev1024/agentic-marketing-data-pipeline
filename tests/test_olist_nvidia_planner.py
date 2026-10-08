import asyncio
import json
import time

import httpx
import pytest

from llm_ops_lab.olist.intent_plan import plan_messages
from llm_ops_lab.olist.nvidia_planner import (
    DEFAULT_MAX_TOTAL_CALLS,
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    NvidiaPlanner,
    NvidiaPlannerError,
)

MODEL_ID = "vendor/model-test"
API_KEY = "test-secret-key"
PLANNER_MESSAGES = plan_messages("최근 실행 상태를 확인해 주세요.")


def completion(
    output='{"requests":["pipeline_history"]}',
    *,
    prompt_tokens=12,
    completion_tokens=7,
    total_tokens=None,
    finish_reason="stop",
):
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "id": "completion-id",
        "model": "server-alias-may-change",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def adapter(handler, **kwargs):
    return NvidiaPlanner(
        model_id=MODEL_ID,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_generate_uses_fixed_bounded_chat_completion_contract():
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        assert body == {
            "model": MODEL_ID,
            "messages": PLANNER_MESSAGES,
            "max_tokens": 256,
            "temperature": 0,
            "seed": 42,
            "stream": False,
        }
        return httpx.Response(200, json=completion())

    with adapter(handler) as planner:
        result = planner.generate(PLANNER_MESSAGES, 5.0)
        assert planner.identity == {
            "model_id": MODEL_ID,
            "model_revision": None,
            "provider": "nvidia_hosted",
            "revision_pinned": False,
        }
    assert len(seen) == 1
    assert result["output"] == '{"requests":["pipeline_history"]}'
    assert result["prompt_tokens"] == 12
    assert result["output_tokens"] == result["generation_tokens"] == 7
    assert result["model_id"] == MODEL_ID
    assert result["model_revision"] is None
    assert result["generation_seconds"] >= 0


def test_model_id_is_required_and_api_key_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", API_KEY)
    with pytest.raises(TypeError):
        NvidiaPlanner(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with NvidiaPlanner(
        model_id=MODEL_ID,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    ) as planner:
        assert planner.identity["model_id"] == MODEL_ID


@pytest.mark.parametrize(
    ("model_id", "max_total_calls"),
    [("", 2), ("  ", 2), ("m" * 257, 2), (MODEL_ID, 0), (MODEL_ID, 129), (MODEL_ID, True)],
)
def test_constructor_rejects_invalid_model_or_budget(model_id, max_total_calls):
    with pytest.raises(NvidiaPlannerError):
        NvidiaPlanner(
            model_id=model_id,
            api_key=API_KEY,
            max_total_calls=max_total_calls,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )


def test_missing_credentials_fail_without_network(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(NvidiaPlannerError, match="credentials_missing"):
        NvidiaPlanner(
            model_id=MODEL_ID,
            transport=httpx.MockTransport(lambda _: pytest.fail("network called")),
        )


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (301, "unexpected_http_status"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (500, "upstream_unavailable"),
        (503, "upstream_unavailable"),
    ],
)
def test_http_errors_are_sanitized_and_never_retried(status, expected_code):
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(status, content=f"{API_KEY} private provider body".encode())

    with adapter(handler) as planner:
        with pytest.raises(NvidiaPlannerError, match=expected_code) as error:
            planner.generate(PLANNER_MESSAGES, 5.0)
    assert len(calls) == 1
    assert API_KEY not in str(error.value)
    assert "private provider body" not in str(error.value)


def test_redirect_is_not_followed():
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/collect"})

    with adapter(handler) as planner:
        with pytest.raises(NvidiaPlannerError, match="unexpected_http_status"):
            planner.generate(PLANNER_MESSAGES, 5.0)
    assert calls == [1]


def test_transport_timeout_is_sanitized():
    def handler(_request):
        raise httpx.ReadTimeout("secret detail")

    with adapter(handler) as planner:
        with pytest.raises(NvidiaPlannerError, match="upstream_timeout") as error:
            planner.generate(PLANNER_MESSAGES, 1.0)
    assert "secret detail" not in str(error.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (b"not json", "invalid_upstream_response"),
        (json.dumps({"choices": []}).encode(), "invalid_upstream_response"),
        (json.dumps(completion(finish_reason="length")).encode(), "truncated_response"),
        (json.dumps(completion(prompt_tokens=True)).encode(), "invalid_usage"),
        (json.dumps(completion(completion_tokens=257)).encode(), "invalid_usage"),
        (json.dumps(completion(total_tokens=1)).encode(), "invalid_usage"),
        (json.dumps(completion())[:-1].encode(), "invalid_upstream_response"),
    ],
)
def test_malformed_truncated_or_invalid_usage_responses_fail_closed(response, code):
    with adapter(lambda _: httpx.Response(200, content=response)) as planner:
        with pytest.raises(NvidiaPlannerError, match=code):
            planner.generate(PLANNER_MESSAGES, 5.0)


def test_response_byte_bound_checks_header_and_streamed_content():
    oversized_length = adapter(
        lambda _: httpx.Response(
            200, content=b"{}", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}
        )
    )
    with oversized_length as planner:
        with pytest.raises(NvidiaPlannerError, match="response_too_large"):
            planner.generate(PLANNER_MESSAGES, 5.0)

    stream = httpx.ByteStream(b"x" * (MAX_RESPONSE_BYTES + 1))
    oversized_stream = adapter(lambda _: httpx.Response(200, stream=stream))
    with oversized_stream as planner:
        with pytest.raises(NvidiaPlannerError, match="response_too_large"):
            planner.generate(PLANNER_MESSAGES, 5.0)


def test_slow_drip_response_is_cancelled_at_whole_call_deadline():
    body = json.dumps(completion()).encode()

    class SlowDrip(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            self.completed = False

        async def __aiter__(self):
            yield body[:1]
            await asyncio.sleep(0.25)
            self.completed = True
            yield body[1:]

        async def aclose(self):
            self.closed = True

    stream = SlowDrip()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            stream=stream,
            headers={"Content-Length": str(len(body))},
        )
    )
    started = time.monotonic()
    with NvidiaPlanner(model_id=MODEL_ID, api_key=API_KEY, transport=transport) as planner:
        with pytest.raises(NvidiaPlannerError, match="upstream_timeout"):
            planner.generate(PLANNER_MESSAGES, 0.03)
    assert time.monotonic() - started < 0.2
    assert stream.closed is True
    assert stream.completed is False


def test_sync_adapter_rejects_use_inside_an_existing_event_loop():
    async def invoke():
        with adapter(lambda _: pytest.fail("network called")) as planner:
            with pytest.raises(NvidiaPlannerError, match="event_loop_running"):
                planner.generate(PLANNER_MESSAGES, 1.0)

    asyncio.run(invoke())


def test_call_budget_counts_failed_attempts_and_context_close_is_terminal():
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(429)

    with adapter(handler) as planner:
        for _ in range(DEFAULT_MAX_TOTAL_CALLS):
            with pytest.raises(NvidiaPlannerError, match="rate_limited"):
                planner.generate(PLANNER_MESSAGES, 5.0)
        with pytest.raises(NvidiaPlannerError, match="call_budget_exhausted"):
            planner.generate(PLANNER_MESSAGES, 5.0)
    assert len(calls) == DEFAULT_MAX_TOTAL_CALLS
    with pytest.raises(NvidiaPlannerError, match="client_closed"):
        planner.generate(PLANNER_MESSAGES, 5.0)


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "system", "content": "other"}, PLANNER_MESSAGES[1]],
        [*PLANNER_MESSAGES, {"role": "user", "content": "extra"}],
        [
            PLANNER_MESSAGES[0],
            {"role": "user", "content": '{"question_untrusted":"x","observations":[]}'},
        ],
    ],
)
def test_only_exact_bounded_planner_message_shape_is_sent(messages):
    with adapter(lambda _: pytest.fail("network called")) as planner:
        with pytest.raises(NvidiaPlannerError, match="invalid_planner_messages"):
            planner.generate(messages, 5.0)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), 301])
def test_timeout_must_be_positive_finite_and_bounded(timeout):
    with adapter(lambda _: pytest.fail("network called")) as planner:
        with pytest.raises(NvidiaPlannerError, match="invalid_timeout"):
            planner.generate(PLANNER_MESSAGES, timeout)


def test_super_profile_disables_reasoning_without_expanding_token_budget():
    def handler(request):
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["max_tokens"] == 256
        assert body["temperature"] == 0
        return httpx.Response(200, json=completion())

    with NvidiaPlanner(model_id="nvidia/nemotron-3-super-120b-a12b", api_key=API_KEY,
                       transport=httpx.MockTransport(handler)) as planner:
        assert planner.identity["enable_thinking"] is False
        planner.generate(PLANNER_MESSAGES, 5)
