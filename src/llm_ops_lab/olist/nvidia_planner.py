"""Optional bounded NVIDIA-hosted adapter for the Olist intent planner.

This adapter is opt-in and is not a production failover. Callers must provide a
model ID explicitly; the remote service does not promise a pinned model revision.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from typing import Any

import httpx

from . import intent_plan

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_PROMPT_TOKENS = 2_048
MAX_OUTPUT_TOKENS = 256
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOTAL_CALLS = 2
MAX_TOTAL_CALLS = 128


class NvidiaPlannerError(RuntimeError):
    """Sanitized adapter failure; the code never contains provider response text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NvidiaPlanner:
    """Generate typed intent plans through NVIDIA's fixed hosted chat endpoint."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: str | None = None,
        max_total_calls: int = DEFAULT_MAX_TOTAL_CALLS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if type(model_id) is not str or not model_id.strip() or len(model_id) > 256:
            raise NvidiaPlannerError("invalid_model_id")
        if (
            type(max_total_calls) is not int
            or not 1 <= max_total_calls <= MAX_TOTAL_CALLS
        ):
            raise NvidiaPlannerError("invalid_call_budget")
        resolved_key = api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY")
        if type(resolved_key) is not str or not resolved_key.strip():
            raise NvidiaPlannerError("credentials_missing")

        self.model_id = model_id.strip()
        self._api_key = resolved_key.strip()
        self.identity = {
            "model_id": self.model_id,
            "model_revision": None,
            "provider": "nvidia_hosted",
            "revision_pinned": False,
        }
        if self.model_id == "nvidia/nemotron-3-super-120b-a12b":
            self.identity["enable_thinking"] = False
        self._max_total_calls = max_total_calls
        self._calls = 0
        self._calls_lock = threading.Lock()
        self._closed = False
        # `transport` is an injection seam for deterministic tests; the request URL
        # remains fixed and no environment proxy configuration is honored.
        if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
            raise NvidiaPlannerError("invalid_transport")
        self._transport = transport

    def __enter__(self) -> NvidiaPlanner:
        if self._closed:
            raise NvidiaPlannerError("client_closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    def generate(self, messages: list[dict[str, str]], timeout: float) -> dict[str, Any]:
        """Return the existing model callback shape with strictly checked metadata."""
        if self._closed:
            raise NvidiaPlannerError("client_closed")
        _validate_planner_messages(messages)
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise NvidiaPlannerError("invalid_timeout")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise NvidiaPlannerError("event_loop_running")
        with self._calls_lock:
            if self._calls >= self._max_total_calls:
                raise NvidiaPlannerError("call_budget_exhausted")
            self._calls += 1

        body = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "seed": 42,
            "stream": False,
        }
        if self.identity.get("enable_thinking") is False:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        started = time.monotonic()
        try:
            raw = asyncio.run(
                asyncio.wait_for(self._request(body, float(timeout)), timeout=float(timeout))
            )
        except NvidiaPlannerError:
            raise
        except TimeoutError:
            raise NvidiaPlannerError("upstream_timeout") from None
        except httpx.TimeoutException:
            raise NvidiaPlannerError("upstream_timeout") from None
        except httpx.RequestError:
            raise NvidiaPlannerError("upstream_transport_error") from None

        payload = _decode_json(raw)
        output, prompt_tokens, output_tokens = _parse_completion(payload)
        return {
            "output": output,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "generation_tokens": output_tokens,
            "generation_seconds": time.monotonic() - started,
            "model_id": self.model_id,
            # Hosted aliases can move; do not invent a pinned server revision.
            "model_revision": None,
        }

    async def _request(self, body: dict[str, Any], timeout: float) -> bytes:
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=None,
            transport=self._transport,
        ) as client:
            async with client.stream(
                "POST",
                ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=httpx.Timeout(timeout),
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise _status_error(response.status_code)
                return await _aread_bounded_response(response)


def _validate_planner_messages(messages: Any) -> None:
    if type(messages) is not list or len(messages) != 2:
        raise NvidiaPlannerError("invalid_planner_messages")
    system, user = messages
    expected_system = intent_plan.prompt_bytes().decode("utf-8")
    if (
        type(system) is not dict
        or set(system) != {"role", "content"}
        or system.get("role") != "system"
        or system.get("content") != expected_system
        or type(user) is not dict
        or set(user) != {"role", "content"}
        or user.get("role") != "user"
        or type(user.get("content")) is not str
    ):
        raise NvidiaPlannerError("invalid_planner_messages")
    try:
        payload = json.loads(
            user["content"],
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        raise NvidiaPlannerError("invalid_planner_messages") from None
    if (
        type(payload) is not dict
        or not {"question_untrusted"} <= set(payload)
        or set(payload) - {"question_untrusted", "correction"}
        or type(payload.get("question_untrusted")) is not str
        or not payload["question_untrusted"].strip()
        or len(payload["question_untrusted"]) > intent_plan.MAX_QUESTION_CHARS
        or (
            "correction" in payload
            and (
                type(payload["correction"]) is not str
                or len(payload["correction"]) > intent_plan.MAX_CORRECTION_CHARS
            )
        )
    ):
        raise NvidiaPlannerError("invalid_planner_messages")


async def _aread_bounded_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal() or int(content_length) > MAX_RESPONSE_BYTES:
            raise NvidiaPlannerError("response_too_large")
    chunks = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                raise NvidiaPlannerError("response_too_large")
            chunks.extend(chunk)
    except httpx.DecodingError:
        raise NvidiaPlannerError("invalid_response_encoding") from None
    return bytes(chunks)


def _decode_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise NvidiaPlannerError("invalid_upstream_response") from None
    if type(value) is not dict:
        raise NvidiaPlannerError("invalid_upstream_response")
    return value


def _parse_completion(payload: dict[str, Any]) -> tuple[str, int, int]:
    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise NvidiaPlannerError("invalid_upstream_response")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise NvidiaPlannerError("truncated_response")
    if finish_reason != "stop":
        raise NvidiaPlannerError("invalid_upstream_response")
    message = choice.get("message")
    if (
        type(message) is not dict
        or message.get("role") != "assistant"
        or type(message.get("content")) is not str
    ):
        raise NvidiaPlannerError("invalid_upstream_response")
    output = message["content"]
    try:
        output_bytes = output.encode("utf-8")
    except UnicodeEncodeError:
        raise NvidiaPlannerError("invalid_upstream_response") from None
    if not output_bytes or len(output_bytes) > intent_plan.MAX_PLAN_BYTES:
        raise NvidiaPlannerError("invalid_upstream_response")

    usage = payload.get("usage")
    if type(usage) is not dict:
        raise NvidiaPlannerError("invalid_usage")
    prompt_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        type(prompt_tokens) is not int
        or type(output_tokens) is not int
        or type(total_tokens) is not int
        or not 0 <= prompt_tokens <= MAX_PROMPT_TOKENS
        or not 0 <= output_tokens <= MAX_OUTPUT_TOKENS
        or total_tokens != prompt_tokens + output_tokens
    ):
        raise NvidiaPlannerError("invalid_usage")
    return output, prompt_tokens, output_tokens


def _status_error(status_code: int) -> NvidiaPlannerError:
    if status_code in {401, 403}:
        return NvidiaPlannerError("authentication_failed")
    if status_code == 429:
        return NvidiaPlannerError("rate_limited")
    if 500 <= status_code <= 599:
        return NvidiaPlannerError("upstream_unavailable")
    return NvidiaPlannerError("unexpected_http_status")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
