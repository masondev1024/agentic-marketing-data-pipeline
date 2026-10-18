"""Durable, single-host execution of the bounded Olist intent investigator."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from . import agent as evidence_contract
from . import intent_agent, intent_plan
from .graph_store import (
    GraphStore,
    PendingAttempt,
    StoreError,
    canonical,
    validate_budget,
)
from .graph_store import (
    budget_status as _budget_status,
)
from .tool_session import OlistToolSession, SessionError

GRAPH_VERSION = "olist-durable.v2"
SOURCE_BINDING_FILES = (
    "agent.py",
    "agent_prompt.txt",
    "diagnostics.py",
    "durable_graph.py",
    "graph_store.py",
    "intent_agent.py",
    "intent_plan.py",
    "intent_prompt.txt",
    "lineage.py",
    "nvidia_planner.py",
    "nvidia_service.py",
    "schema.sql",
    "semantics.py",
    "tool_session.py",
    "transform.sql",
    "validation.py",
    "../service_worker.py",
    "../../../data/olist/metric-policy-v1.json",
    "../../../data/olist/semantics/ontology-v1.ttl",
    "../../../data/olist/semantics/shapes-v1.ttl",
    "../../../data/olist/semantics/manifest-v1.json",
    "../../../uv.lock",
    "../../../pyproject.toml",
)
MAX_QUESTION_CHARS = 2_048
MAX_RESUME_AGE_SECONDS = 86_400
KNOWN_PROVIDER_ERRORS = frozenset(
    {
        "authentication_failed",
        "rate_limited",
        "upstream_unavailable",
        "unexpected_http_status",
        "invalid_planner_messages",
        "invalid_response_encoding",
        "invalid_upstream_response",
        "invalid_usage",
        "truncated_response",
        "response_too_large",
    }
)


class DurableError(Exception):
    """Safe public failure from the durable execution boundary."""

    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


def budget_status(database: Path, total_call_budget: int = 32) -> dict[str, Any]:
    try:
        return _budget_status(database, total_call_budget)
    except StoreError as error:
        raise DurableError(error.code, error.status) from error


class State(TypedDict):
    binding: dict[str, Any]
    question: str
    version_id: str | None
    started_at: str
    plan_result: dict[str, Any]
    evidence_result: dict[str, Any]
    result: dict[str, Any]


@contextmanager
def exclusive(database: Path):
    """One cooperative process owns the SQLite journal through execution/replay."""
    filename = Path(os.path.abspath(Path(database).expanduser()))
    filename.parent.mkdir(parents=True, exist_ok=True)
    lock_path = filename.with_suffix(filename.suffix + ".lock")
    try:
        lock_fd = _open_private_file(lock_path)
        with os.fdopen(lock_fd, "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DurableError("investigation_busy", 503) from error
            try:
                database_fd = _open_private_file(filename)
                os.close(database_fd)
                yield filename
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError as error:
        raise DurableError("durable_store_unavailable", 503) from error


def _open_private_file(path: Path) -> int:
    """Open/create an owner-only regular file without following its final symlink."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise OSError("durable file ownership or type is unsafe")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _source_fingerprints() -> dict[str, str]:
    package = Path(__file__).parent
    try:
        return {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest()
            for name in SOURCE_BINDING_FILES
        }
    except OSError as error:
        raise DurableError("durable_source_unavailable", 503) from error


def _model_binding(generate: Callable) -> dict[str, Any]:
    identity = getattr(generate, "durable_identity", None)
    if identity is None:
        identity = getattr(generate, "identity", None)
    if identity is None:
        identity = {
            "callable": f"{getattr(generate, '__module__', '')}."
            f"{getattr(generate, '__qualname__', type(generate).__qualname__)}"
        }
    if type(identity) is not dict:
        raise DurableError("durable_config_invalid", 400)
    try:
        encoded = canonical(identity)
    except (TypeError, ValueError) as error:
        raise DurableError("durable_config_invalid", 400) from error
    if len(encoded) > 4096:
        raise DurableError("durable_config_invalid", 400)
    if _contains_secret_field(identity):
        raise DurableError("durable_config_invalid", 400)
    return identity


def _contains_secret_field(value: Any) -> bool:
    if type(value) is dict:
        return any(
            any(
                part in str(key).lower()
                for part in ("api_key", "secret", "authorization", "credential")
            )
            or _contains_secret_field(item)
            for key, item in value.items()
        )
    if type(value) is list:
        return any(_contains_secret_field(item) for item in value)
    return False


def _binding(
    question: str,
    version_id: str | None,
    total_call_budget: int,
    model_identity: dict[str, Any],
) -> dict[str, Any]:
    policy, policy_hash = evidence_contract._metric_policy()
    if policy.get("policy_version") == "unavailable":
        raise DurableError("metric_policy_unavailable", 503)
    return {
        "graph_version": GRAPH_VERSION,
        "max_resume_age_seconds": MAX_RESUME_AGE_SECONDS,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "requested_version_id": version_id,
        "total_call_budget": total_call_budget,
        "prompt_version": intent_plan.PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(intent_plan.prompt_bytes()).hexdigest(),
        "metric_policy_version": policy["policy_version"],
        "metric_policy_sha256": policy_hash,
        "model": model_identity,
        "source_sha256": _source_fingerprints(),
    }


def _validate_bundle(bundle: list[dict], tools: list[str], version_id: str | None) -> None:
    if type(bundle) is not list or not bundle or type(bundle[0]) is not dict:
        raise DurableError("durable_evidence_invalid")
    observed = bundle[0].get("dataset_version")
    if type(observed) is not str or (version_id is not None and observed != version_id):
        raise DurableError("durable_source_mismatch", 409)
    quality = bundle[0]
    evidence_contract._validate_tool_result(quality, "version_evidence", observed)
    if quality["data"].get("version_id") != observed:
        raise DurableError("durable_source_mismatch", 409)
    expected_tools = (
        tools
        if quality["data"].get("quality_status") == "passed"
        and (quality["data"].get("failed_check_count", 0) == 0)
        else []
    )
    if len(bundle) != len(expected_tools) + 1:
        raise DurableError("durable_evidence_invalid")
    for expected, item in zip(expected_tools, bundle[1:], strict=True):
        evidence_contract._validate_tool_result(item, expected, observed)


class _Execution:
    def __init__(
        self,
        connection: sqlite3.Connection,
        store: GraphStore,
        investigation_id: str,
        dsn: str,
        generate: Callable,
        version_id: str | None,
        timeout_seconds: float,
        started_at: datetime,
        fault: Callable[[str], None],
    ):
        self.connection = connection
        self.store = store
        self.investigation_id = investigation_id
        self.dsn = dsn
        self.generate = generate
        self.version_id = version_id
        self.timeout_seconds = timeout_seconds
        self.started_at = started_at
        self.fault = fault
        self.provider_error: str | None = None
        self.deadline = time.monotonic() + timeout_seconds

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("investigation_deadline_exceeded")
        return left

    def plan(self, state: State) -> dict[str, Any]:
        attempt_number = 0

        def generate_durably(messages, timeout):
            nonlocal attempt_number
            attempt = attempt_number
            attempt_number += 1
            try:
                cached = self.store.reserve_attempt(self.investigation_id, attempt)
            except PendingAttempt as error:
                raise DurableError("attempted_unknown", 503) from error
            if cached is not None:
                if "__provider_error__" in cached:
                    code = cached["__provider_error__"]
                    if code not in KNOWN_PROVIDER_ERRORS:
                        raise DurableError("durable_attempt_corrupt")
                    self.provider_error = code
                    raise RuntimeError(code)
                return cached
            self.fault("attempt_reserved")
            try:
                response = self.generate(messages, timeout)
            except Exception as error:
                code = getattr(error, "code", None)
                if code in KNOWN_PROVIDER_ERRORS:
                    self.store.commit_attempt_failure(self.investigation_id, attempt, code)
                    self.provider_error = code
                    self.fault("provider_failure_committed")
                    raise RuntimeError(code) from None
                # Any exception after reservation leaves the external outcome unknown.
                raise DurableError("attempted_unknown", 503) from error
            self.fault("provider_returned")
            self.store.commit_attempt(self.investigation_id, attempt, response)
            self.fault("response_committed")
            return response

        try:
            planned = intent_agent.plan_stage(state["question"], generate_durably,
                                               self.remaining)
        except DurableError:
            raise
        except TimeoutError:
            planned = {"reason": "investigation_deadline_exceeded", "trace": [],
                       "usage": intent_agent._empty_usage(),
                       "identity": {"id": None, "revision": None}, "plan": None}
        except RuntimeError:
            planned = {"reason": "model_unavailable", "trace": [],
                       "usage": intent_agent._empty_usage(),
                       "identity": {"id": None, "revision": None}, "plan": None}
        except (ValueError, TypeError, KeyError, OSError):
            planned = {"reason": "invalid_or_unavailable_evidence", "trace": [],
                       "usage": intent_agent._empty_usage(),
                       "identity": {"id": None, "revision": None}, "plan": None}
        plan = planned.pop("plan", None)
        planned["plan"] = ({"disposition": plan.disposition, "intents": list(plan.intents)}
                           if plan else None)
        return {"plan_result": planned, "evidence_result": {}, "result": {}}

    def evidence(self, state: State) -> dict[str, Any]:
        plan_state = state["plan_result"]
        encoded_plan = plan_state.get("plan")
        if encoded_plan is None:
            return {"evidence_result": {"reason": plan_state.get("reason"),
                    "semantic_refusal": plan_state.get("semantic_refusal"), "evidence": [],
                    "trace": [], "usage": intent_agent._empty_usage()}}
        plan = intent_plan.Plan(encoded_plan["disposition"], tuple(encoded_plan["intents"]))
        try:
            evidence = intent_agent.evidence_stage(
                self.dsn, plan, state["version_id"], self.remaining,
                deadline=self.deadline, evidence_bundle=self.load_bundle,
                started_at=self.started_at,
            )
        except DurableError:
            raise
        except TimeoutError:
            evidence = {"reason": "investigation_deadline_exceeded", "evidence": [],
                        "trace": [{"stage": "deadline"}],
                        "usage": intent_agent._empty_usage()}
        except SessionError as error:
            evidence = {"reason": error.code, "evidence": [], "trace": [],
                        "usage": intent_agent._empty_usage()}
        except (ValueError, TypeError, KeyError, OSError):
            evidence = {"reason": "invalid_or_unavailable_evidence", "evidence": [],
                        "trace": [], "usage": intent_agent._empty_usage()}
        return {"evidence_result": evidence}

    def load_bundle(self, tools, requested_version, deadline):
        try:
            cached = self.store.evidence(self.investigation_id)
            if cached is not None:
                _validate_bundle(cached, tools, requested_version)
                return cached
            self.store.reserve_evidence(self.investigation_id)
        except PendingAttempt as error:
            raise DurableError("evidence_attempted_unknown", 503) from error
        self.fault("evidence_reserved")
        with OlistToolSession(
            self.dsn,
            version_id=requested_version,
            deadline=deadline,
            started_at=self.started_at,
        ) as session:
            bundle = session.collect_bundle(tools)
        _validate_bundle(bundle, tools, requested_version)
        self.fault("evidence_returned")
        self.store.commit_evidence(self.investigation_id, bundle)
        self.fault("evidence_committed")
        return bundle

    def render(self, state: State) -> dict[str, Any]:
        plan_state = state["plan_result"]
        encoded_plan = plan_state.get("plan")
        plan = (intent_plan.Plan(encoded_plan["disposition"], tuple(encoded_plan["intents"]))
                if encoded_plan else None)
        result = intent_agent.render_stage(
            plan, state["evidence_result"], plan_state, remaining=self.remaining
        )
        if self.provider_error:
            result["provider_error"] = self.provider_error
        self.store.commit_result(self.investigation_id, result)
        self.fault("result_committed")
        return {"result": result}


def _compile(connection: sqlite3.Connection, execution: _Execution):
    graph = StateGraph(State)
    graph.add_node("plan", execution.plan)
    graph.add_node("evidence", execution.evidence)
    graph.add_node("render", execution.render)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "evidence")
    graph.add_edge("evidence", "render")
    graph.add_edge("render", END)
    serde = JsonPlusSerializer(
        pickle_fallback=False, allowed_json_modules=(), allowed_msgpack_modules=None
    )
    saver = SqliteSaver(connection, serde=serde)
    saver.setup()
    connection.execute("PRAGMA synchronous=FULL")
    return graph.compile(checkpointer=saver)


def investigate_durable(
    database: Path,
    dsn: str,
    question: str,
    generate: Callable,
    *,
    principal: str,
    idempotency_key: str,
    version_id: str | None = None,
    timeout_seconds: float = 60,
    total_call_budget: int = 32,
    fault: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run/resume one immutable Olist investigation under a principal-scoped key.

    Model reservations and complete external responses are journaled before graph
    checkpoint progress. Ambiguous pending attempts fail closed and are never replayed.
    """
    validate_budget(total_call_budget)
    if (
        type(question) is not str
        or not question.strip()
        or len(question) > MAX_QUESTION_CHARS
        or not callable(generate)
        or type(principal) is not str
        or not principal.strip()
        or len(principal) > 256
        or type(idempotency_key) is not str
        or not idempotency_key.strip()
        or len(idempotency_key) > 256
    ):
        raise DurableError("invalid_request", 400)
    try:
        question.encode("utf-8")
        principal.encode("utf-8")
        idempotency_key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DurableError("invalid_request", 400) from error
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        raise DurableError("invalid_timeout", 400)
    if version_id is not None and evidence_contract.diagnostics._safe_version(version_id) is None:
        raise DurableError("invalid_version_id", 400)
    model_identity = _model_binding(generate)
    binding = _binding(question, version_id, total_call_budget, model_identity)
    fault = fault or (lambda _point: None)

    try:
        with (
            exclusive(Path(database)) as filename,
            closing(sqlite3.connect(filename, timeout=5, check_same_thread=False)) as connection,
            tracing_context(enabled=False),
        ):
            # SQLite's path opener follows symlinks; verify it attached to the
            # private inode we just opened with O_NOFOLLOW.
            path_stat = os.stat(filename, follow_symlinks=False)
            if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.geteuid():
                raise DurableError("durable_store_unavailable", 503)
            os.chmod(filename, 0o600, follow_symlinks=False)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            store = GraphStore(connection, total_call_budget)
            investigation_id, prior_result, saved_binding = store.claim(
                principal, idempotency_key, binding
            )
            if prior_result is not None:
                return prior_result | {"investigation_id": investigation_id, "replayed": True}
            row = connection.execute(
                "SELECT created_at FROM durable_runs WHERE investigation_id=?",
                (investigation_id,),
            ).fetchone()
            if row is None:
                raise StoreError("durable_run_missing")
            if time.time() - row[0] > MAX_RESUME_AGE_SECONDS:
                raise DurableError("investigation_expired", 409)
            started_at = datetime.fromtimestamp(row[0], UTC)
            execution = _Execution(
                connection,
                store,
                investigation_id,
                dsn,
                generate,
                version_id,
                float(timeout_seconds),
                started_at,
                fault,
            )
            graph = _compile(connection, execution)
            config = {"configurable": {"thread_id": investigation_id}}
            saved = graph.get_state(config)
            if saved.values and saved.values.get("binding") != saved_binding:
                raise StoreError("durable_binding_mismatch")
            if saved.values and not saved.next:
                # The ledger must have committed before any terminal checkpoint.
                raise StoreError("durable_checkpoint_without_result")
            if saved.values:
                graph.invoke(None, config, durability="sync")
            else:
                graph.invoke(
                    {
                        "binding": saved_binding,
                        "question": question,
                        "version_id": version_id,
                        "started_at": started_at.isoformat(),
                        "plan_result": {},
                        "evidence_result": {},
                        "result": {},
                    },
                    config,
                    durability="sync",
                )
            completed = connection.execute(
                "SELECT result_json,result_sha256 FROM durable_runs "
                "WHERE investigation_id=? AND status='complete'",
                (investigation_id,),
            ).fetchone()
            if completed is None or completed[0] is None:
                raise StoreError("durable_result_missing")
            if hashlib.sha256(completed[0].encode("utf-8")).hexdigest() != completed[1]:
                raise StoreError("durable_result_corrupt")
            result = json.loads(completed[0])
            return result | {"investigation_id": investigation_id, "replayed": False}
    except DurableError:
        raise
    except StoreError as error:
        raise DurableError(error.code, error.status) from error
    except sqlite3.Error as error:
        raise DurableError("durable_store_unavailable", 503) from error


__all__ = ["DurableError", "budget_status", "investigate_durable"]
