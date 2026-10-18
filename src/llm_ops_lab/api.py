"""Local-only HTTP contract around the bounded inference supervisor."""

import argparse
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from llm_ops_lab.cli import MODEL_SPECS
from llm_ops_lab.evidence import strict_object
from llm_ops_lab.service import ServiceConfig, ServiceError, Supervisor, append_access_event

MAX_BODY_BYTES = 24 * 1024
BODY_TIMEOUT_SECONDS = 5
MAX_HTTP_REQUESTS = 32


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    question: str = Field(min_length=1, max_length=4096)
    max_tokens: int = Field(default=256, ge=1, le=512)
    temperature: float = Field(default=0, ge=0, le=2)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    answer_mode: Literal["direct", "evidence-v1"] = "direct"

    @field_validator("question")
    @classmethod
    def validate_question(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        value.encode("utf-8")  # Reject isolated surrogates before IPC serialization.
        return value


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    question: str = Field(min_length=1, max_length=2048)
    version_id: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        value.encode("utf-8")
        return value

    @field_validator("version_id")
    @classmethod
    def validate_version_id(cls, value):
        if value is not None and any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("Version ID must be a 64-character hexadecimal digest")
        return value


def error_response(request_id, status, code):
    return JSONResponse(
        {"request_id": request_id, "error": {"code": code}},
        status_code=status,
        headers={
            "X-Request-ID": request_id,
            **({"Retry-After": "5"} if status in {429, 503} else {}),
        },
    )


class RequestBoundary:
    """Audit requests and enforce body bounds, authenticating public calls first."""

    def __init__(self, app, output_dir, public_mode=False, public_config=None):
        self.app, self.output_dir = app, output_dir
        self.public_mode = public_mode
        self.public_config = public_config
        self.public_boundary = None
        if public_config is not None:
            from llm_ops_lab.public_security import PublicBoundary

            self.public_boundary = PublicBoundary(self._body_limited_dispatch, public_config)
        self.inflight = 0
        self.audit_tasks = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        self.inflight += 1
        sent = False

        async def observed_send(message):
            nonlocal sent
            if message["type"] == "http.response.start":
                event = {
                    "event": "http_complete",
                    "at": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": (
                        scope["path"]
                        if scope["path"]
                        in {
                            "/v1/answers", "/v1/investigations", "/health/live",
                            "/health/ready", "/metrics",
                        }
                        else "other"
                    ),
                    "http_status": message["status"],
                    "elapsed_seconds": time.monotonic() - started,
                    **scope["state"].get("outcome", {}),
                }
                try:
                    if len(self.audit_tasks) >= MAX_HTTP_REQUESTS:
                        raise ServiceError(503, "audit_capacity_exceeded")
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            append_access_event, self.output_dir / "access.jsonl", event
                        )
                    )
                    self.audit_tasks.add(task)

                    def audit_done(future):
                        self.audit_tasks.discard(future)
                        if not future.cancelled():
                            future.exception()

                    task.add_done_callback(audit_done)
                    # Cancellation of this request must not release a still-running writer slot.
                    await asyncio.shield(task)
                except OSError:
                    # Successful inference is not returned without an auditable access event.
                    raise ServiceError(500, "audit_write_failed") from None
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode())]
                if self.public_config is not None:
                    from llm_ops_lab.public_security import _SECURITY_HEADERS

                    enforced = {name for name, _ in _SECURITY_HEADERS}
                    message["headers"] = [
                        (key, value)
                        for key, value in message["headers"]
                        if key.lower() not in enforced
                    ] + list(_SECURITY_HEADERS)
                sent = True
            await send(message)

        try:
            if self.inflight > MAX_HTTP_REQUESTS:
                raise ServiceError(429, "http_capacity_exceeded")
            peer = scope.get("client")
            try:
                local_peer = peer is not None and ip_address(peer[0]).is_loopback
            except ValueError:
                local_peer = False
            if not self.public_mode and not local_peer:
                raise ServiceError(403, "loopback_client_required")
            headers = {k.lower(): v for k, v in scope["headers"]}
            host = headers.get(b"host", b"").split(b":")[0].lower()
            if not self.public_mode and host not in {b"localhost", b"127.0.0.1"}:
                raise ServiceError(400, "invalid_host")
            dispatch = (
                self.public_boundary
                if self.public_boundary is not None
                else self._body_limited_dispatch
            )
            await dispatch(scope, receive, observed_send)
        except TimeoutError:
            scope["state"]["outcome"] = {"error_code": "body_read_timeout"}
            response = error_response(request_id, 408, "body_read_timeout")
            try:
                await response(scope, receive, observed_send)
            except ServiceError:
                await error_response(request_id, 500, "audit_write_failed")(scope, receive, send)
        except ServiceError as error:
            # Attempt logging before returning; storage errors use raw send to avoid recursion.
            response = error_response(request_id, error.status, error.code)
            if error.code in {
                "audit_write_failed",
                "audit_capacity_exceeded",
                "http_capacity_exceeded",
            }:
                await response(scope, receive, send)
            else:
                try:
                    scope["state"]["outcome"] = {"error_code": error.code}
                    await response(scope, receive, observed_send)
                except ServiceError:
                    await error_response(request_id, 500, "audit_write_failed")(
                        scope, receive, send
                    )
        except Exception as error:
            logging.getLogger(__name__).error(
                "Request failed id=%s type=%s", request_id, type(error).__name__
            )
            if not sent:
                await error_response(request_id, 500, "internal_error")(scope, receive, send)
        finally:
            self.inflight -= 1

    async def _body_limited_dispatch(self, scope, receive, send):
        """Validate and replay a bounded POST body before handing it to FastAPI."""
        if scope["method"] == "POST":
            # This is a CLI/API endpoint, not a browser cross-origin inference endpoint.
            headers = {}
            for key, value in scope["headers"]:
                headers.setdefault(key.lower(), value)
            if b"origin" in headers:
                raise ServiceError(403, "browser_origin_not_allowed")
            if headers.get(b"content-type", b"").split(b";")[0].strip() != b"application/json":
                raise ServiceError(415, "json_content_type_required")
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    if int(length) < 0:
                        raise ValueError
                    if int(length) > MAX_BODY_BYTES:
                        raise ServiceError(413, "request_body_too_large")
                except ValueError:
                    raise ServiceError(400, "invalid_content_length") from None
            body = bytearray()
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    part = await receive()
                    if part["type"] == "http.disconnect":
                        return
                    body.extend(part.get("body", b""))
                    if len(body) > MAX_BODY_BYTES:
                        raise ServiceError(413, "request_body_too_large")
                    if not part.get("more_body", False):
                        break
            try:
                strict_object(body.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise ServiceError(422, "invalid_json_object") from None
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        else:
            await self.app(scope, receive, send)


def public_result(record):
    retrieval = record.get("retrieval") or {}
    evidence = record.get("evidence") or {}
    return {
        "request_id": record["request_id"],
        "status": record["status"],
        "answer": record["output"],
        "answer_mode": record["answer_mode"],
        "sources": record["sources"],
        "citations": record.get("citations", []),
        "answer_sentences": record.get("answer_sentences", []),
        "retrieval_reason": retrieval.get("reason"),
        "abstention_reason": (
            "evidence_rejected"
            if record["status"] == "evidence_rejected"
            else ("empty_evidence_selection" if evidence else retrieval.get("reason"))
            if record["status"] == "abstained"
            else None
        ),
        "validation": {
            "quote_membership": evidence.get("quote_membership", "not_checked"),
            "semantic_quality": "unverified",
        },
        "model_executed": record["model_executed"],
        "model": {"id": record["model_id"], "revision": record["model_revision"]},
        "truncated": record.get("max_tokens_reached", False),
        "usage": {
            "prompt_tokens": record.get("prompt_tokens", 0),
            "output_tokens": record.get("generation_tokens", 0),
        },
        "timing": {
            "queue_seconds": record["queue_seconds"],
            "request_seconds": record["request_seconds"],
            "generation_seconds": record.get("generation_seconds", 0),
            "retrieval_seconds": retrieval.get("search_seconds", 0),
        },
    }


MAX_PUBLIC_INVESTIGATION_BYTES = 48 * 1024


def reader_ready(root):
    """Check actual published data using the same bounded read-only boundary."""
    from llm_ops_lab.olist.config import connection_info
    from llm_ops_lab.olist.tool_session import OlistToolSession

    try:
        with OlistToolSession(
            connection_info(root, reader=True), version_id=None, deadline=time.monotonic() + 3
        ) as session:
            quality = session.initialize()
            return bool(
                quality and quality["data"].get("quality_status") == "passed"
                and quality["data"].get("failed_check_count") == 0
            )
    except Exception:
        # Connection errors may contain DSNs; expose only readiness, never their text.
        return False


def public_investigation(record):
    """Expose only the bounded diagnostic contract; never return SQL or connection data."""
    prompt = record.get("prompt") or {}
    result = {
        "request_id": record["request_id"],
        "status": record["status"],
        "model_executed": record["model_executed"],
        "dataset_version": record.get("dataset_version"),
        "evidence": record.get("evidence", []),
        "findings": record.get("findings", []),
        "trace": record.get("trace", {}),
        "usage": record.get("usage", {}),
        "limitations": record.get("limitations", []),
        "diagnostic_mode": record.get("diagnostic_mode"),
        "request_plan": record.get("request_plan"),
        "model_execution_status": record.get("model_execution_status"),
        "provider": record.get("provider", "local"),
        "revision_pinned": record.get("revision_pinned"),
        "stop_reason": record.get("stop_reason"),
        "pipeline_context": record.get("pipeline_context"),
        "metric_policy": record.get("metric_policy"),
        "model": {
            "id": record.get("model_id") or (record.get("model") or {}).get("id"),
            "revision": record.get("model_revision")
            or (record.get("model") or {}).get("revision"),
        },
        "prompt": {
            "version": record.get("prompt_version") or prompt.get("version"),
            "sha256": record.get("prompt_sha256") or prompt.get("sha256"),
        },
        "timing": {
            "queue_seconds": record.get("queue_seconds", 0),
            "request_seconds": record.get("request_seconds", 0),
            "processing_seconds": record.get("service_processing_seconds", 0),
        },
    }
    if "investigation_id" in record:
        result["investigation_id"] = record["investigation_id"]
        result["replayed"] = record.get("replayed", False)
    if "semantic_validation" in record:
        result["semantic_validation"] = record["semantic_validation"]
    # Fail closed: truncating evidence would make a diagnosis look complete when it is not.
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PUBLIC_INVESTIGATION_BYTES:
        raise ServiceError(500, "investigation_result_too_large")
    return result


def create_app(config=None, *, supervisor=None, public_config=None):
    config = config or ServiceConfig()
    if public_config is not None and config.durable_db is None:
        raise ValueError("Public service requires durable investigations")
    supervisor = supervisor or Supervisor(config)

    @asynccontextmanager
    async def lifespan(app):
        supervisor.start()
        try:
            yield
        finally:
            await supervisor.close()

    app = FastAPI(title="LLM Ops Lab — Local API", version="1.0", lifespan=lifespan)
    app.state.supervisor = supervisor
    app.add_middleware(
        RequestBoundary, output_dir=config.output_dir, public_mode=public_config is not None,
        public_config=public_config,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        request.state.outcome = {"error_code": "invalid_request"}
        return error_response(request.state.request_id, 422, "invalid_request")

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        health = supervisor.health()
        if public_config is not None:
            health["database_ready"] = await asyncio.to_thread(reader_ready, config.olist_root)
            health["ready"] = bool(
                health["ready"] and health["database_ready"]
                and (health.get("remote_budget") or {}).get("state") != "unavailable"
            )
        return JSONResponse(health, status_code=200 if health["ready"] else 503)

    @app.get("/metrics")
    async def metrics():
        return supervisor.health()

    @app.post("/v1/answers")
    async def answer(payload: AnswerRequest, request: Request):
        if config.olist_provider != "local":
            request.state.outcome = {"error_code": "answers_disabled_for_provider"}
            return error_response(request.state.request_id, 503, "answers_disabled_for_provider")
        try:
            record = await supervisor.submit(request.state.request_id, payload.model_dump())
        except ServiceError as error:
            request.state.outcome = {"error_code": error.code}
            return error_response(request.state.request_id, error.status, error.code)
        request.state.outcome = {
            "status": record["status"],
            "model_executed": record["model_executed"],
            "queue_seconds": record["queue_seconds"],
            "generation_seconds": record.get("generation_seconds", 0),
        }
        return public_result(record)

    @app.post("/v1/investigations")
    async def investigate(payload: InvestigationRequest, request: Request):
        if config.olist_root is None:
            request.state.outcome = {"error_code": "investigations_disabled"}
            return error_response(request.state.request_id, 503, "investigations_disabled")
        settings = {
            "operation": "olist_investigate",
            "question": payload.question,
            "version_id": payload.version_id,
        }
        if config.durable_db is not None:
            key = request.headers.get("idempotency-key", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) is None:
                request.state.outcome = {"error_code": "idempotency_key_required"}
                return error_response(request.state.request_id, 400, "idempotency_key_required")
            settings.update(
                idempotency_key=key,
                principal=getattr(request.state, "principal", "local"),
            )
        try:
            record = await supervisor.submit(request.state.request_id, settings)
            response = public_investigation(record)
        except ServiceError as error:
            request.state.outcome = {"error_code": error.code}
            return error_response(request.state.request_id, error.status, error.code)
        request.state.outcome = {
            "status": record["status"],
            "model_executed": record["model_executed"],
            "operation": "olist_investigate",
            "queue_seconds": record.get("queue_seconds", 0),
            "request_seconds": record.get("request_seconds", 0),
        }
        return response

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--corpus-dir", type=Path, default=Path("data/runbooks"))
    parser.add_argument("--output-dir", type=Path, default=Path("runs/service"))
    parser.add_argument("--model", choices=MODEL_SPECS, default="qwen3-4b")
    parser.add_argument("--queue-size", type=int, default=2)
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--startup-timeout", type=float, default=120)
    parser.add_argument("--olist-root", type=Path)
    parser.add_argument("--olist-engine", choices=("action-v5", "intent-v1"), default="action-v5")
    parser.add_argument("--olist-provider", choices=("local", "nvidia-super"), default="local")
    parser.add_argument("--remote-call-budget", type=int, default=32,
                        help="Hosted call limit; persistent when --durable-db is supplied")
    parser.add_argument("--durable-db", type=Path)
    parser.add_argument("--public-host", help="HTTPS hostname served by a trusted loopback proxy")
    parser.add_argument("--public-token-file", type=Path)
    parser.add_argument("--rate-limit-per-minute", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        config = ServiceConfig(
            args.corpus_dir,
            args.output_dir,
            args.model,
            args.queue_size,
            args.request_timeout,
            args.startup_timeout,
            olist_root=args.olist_root,
            olist_engine=args.olist_engine,
            olist_provider=args.olist_provider,
            remote_call_budget=args.remote_call_budget,
            durable_db=args.durable_db,
        )
        public_config = None
        if args.public_host:
            from llm_ops_lab.public_security import PublicConfig

            public_config = PublicConfig.from_sources(
                allowed_host=args.public_host, token_file=args.public_token_file,
                requests_per_minute=args.rate_limit_per_minute,
            )
            if config.durable_db is None:
                raise ValueError("--public-host requires --durable-db")
        elif args.public_token_file:
            raise ValueError("--public-token-file requires --public-host")
    except ValueError as error:
        parser.error(str(error))
    import uvicorn

    # One API process and one GPU owner. Multiple Uvicorn workers would multiply model memory.
    uvicorn.run(
        create_app(config, public_config=public_config),
        host="127.0.0.1",
        port=args.port,
        workers=1,
        limit_concurrency=40,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=5,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
