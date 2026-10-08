"""Local deterministic diagnostic graph with durable fail-closed recovery.

The SQLite journal bridges node effects and LangGraph checkpoints. It is not an
external side-effect executor and does not promise exactly-once tool execution.
"""

import fcntl
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from llm_ops_lab.marketing.contracts import ID, ContractError, canonical, utc
from llm_ops_lab.marketing.diagnostics import (
    DiagnosticSession,
    Limits,
    Scope,
    Snapshot,
    observed_comparison,
    strict_json,
)
from llm_ops_lab.marketing.pipeline import digest

VERSION = "marketing-graph.v1"
PLAN = ("metrics", "quality", "freshness")


class State(TypedDict):
    binding: dict
    index: int
    session: dict
    result: dict
    decision: str


def failure(code, detail, usage=None):
    return {
        "status": "error",
        "error": {"code": code, "detail": detail},
        "conclusion": "unknown",
        "causality": "unknown",
        "proposed_actions": [],
        "usage": usage or {"actions": 0, "tool_calls": 0, "token_units": 0},
    }


@contextmanager
def exclusive(database):
    """One local writer per database, across processes; OS releases lock on death."""
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with database.with_suffix(database.suffix + ".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError("resume_busy", "checkpoint database is in use") from error
        try:
            yield database
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def dump_session(session):
    # Versioned adapter for the existing v1 boundary; no model can supply this state.
    return {
        "usage": session.usage,
        "seen": sorted(session._seen),
        "evidence": {k: v.decode() for k, v in session._evidence.items()},
        "terminal": session._terminal,
    }


def restore_session(snapshot, binding, saved, now):
    session = DiagnosticSession(snapshot, limits=Limits(**binding["limits"]), now=now)
    session._started = time.monotonic() - max(0, time.time() - binding["started"])
    if saved:
        session._actions = saved["usage"]["actions"]
        session._calls = saved["usage"]["tool_calls"]
        session._units = saved["usage"]["token_units"]
        session._seen = set(saved["seen"])
        session._evidence = {k: v.encode() for k, v in saved["evidence"].items()}
        session._terminal = saved["terminal"]
        for key, raw in session._evidence.items():
            item = strict_json(raw)
            if (
                digest(raw) != key
                or item["scope"] != asdict(snapshot.scope)
                or item["manifest_sha256"] != snapshot.manifest_sha256
            ):
                raise ContractError("checkpoint_integrity", "saved evidence binding mismatch")
            session._validate_result(item["tool"], item["data"], strict_json(snapshot.payload))
    return session


class GraphRun:
    def __init__(self, connection, root, snapshot, thread, now, fault):
        self.db, self.root, self.snapshot = connection, root, snapshot
        self.thread, self.now, self.fault = thread, now, fault
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS graph_bindings (
            thread TEXT PRIMARY KEY, binding TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS graph_actions (
            thread TEXT NOT NULL, step INTEGER NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(thread, step));
          CREATE TABLE IF NOT EXISTS graph_reviews (
            thread TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def check_source(self):
        current = Snapshot.open(self.root, self.snapshot.scope)
        if current != self.snapshot:
            raise ContractError("snapshot_integrity", "captured source changed")

    def validate_scope(self, state):
        self.check_source()
        return {}

    def plan(self, state):
        return {}

    def execute(self, state):
        step, binding = state["index"], state["binding"]
        cached = self.db.execute(
            "SELECT value FROM graph_actions WHERE thread=? AND step=?", (self.thread, step)
        ).fetchone()
        if cached:
            entry = strict_json(cached[0])
            if entry["status"] == "pending":
                return {
                    "result": failure(
                        "interrupted_action", "outcome unknown; start new thread", entry["usage"]
                    ),
                    "session": state["session"] | {"usage": entry["usage"]},
                }
            return entry["update"]
        self.check_source()
        session = restore_session(self.snapshot, binding, state["session"], self.now)
        if step < len(PLAN):
            action = {"action": "tool", "tool": PLAN[step], "parameters": {}}
        else:
            rows = strict_json(self.snapshot.payload)["rows"]
            rows = sorted(rows, key=lambda r: r["business_date"])
            conclusion = observed_comparison(rows)
            action = {
                "action": "finish",
                "conclusion": conclusion,
                "evidence_ids": list(session._evidence),
                "claims": [],
            }
        # Reserve before invoking. Ambiguous process death consumes the remaining
        # budget and is terminal; it can never buy more attempts through restart.
        reserved = {
            "actions": binding["limits"]["max_actions"],
            "tool_calls": binding["limits"]["max_tool_calls"],
            "token_units": binding["limits"]["max_token_units"],
        }
        with self.db:
            self.db.execute(
                "INSERT INTO graph_actions VALUES (?,?,?)",
                (self.thread, step, json.dumps({"status": "pending", "usage": reserved})),
            )
        self.fault("reserved")
        result = session.dispatch(json.dumps(action))
        self.fault("tool_returned")
        update = {"index": step + 1, "session": dump_session(session), "result": result}
        with self.db:
            self.db.execute(
                "UPDATE graph_actions SET value=? WHERE thread=? AND step=?",
                (json.dumps({"status": "complete", "update": update}), self.thread, step),
            )
        self.fault("action_committed")
        return update

    def evidence_validation(self, state):
        if state["result"]["status"] == "error":
            return {}
        restore_session(self.snapshot, state["binding"], state["session"], self.now)
        self.check_source()
        return {}

    def route(self, state):
        if state["result"]["status"] in ("error", "abstained"):
            return "abstain"
        return "plan" if state["index"] <= len(PLAN) else "review"

    def review(self, state):
        proposal = {
            "kind": "investigation_only",
            "scope": state["binding"]["scope"],
            "evidence_ids": state["result"]["evidence_ids"],
            "text": "Inspect source completeness and refund-adjusted ROAS before decisions.",
        }
        decision = interrupt(proposal)
        if decision not in ("accept", "reject"):
            raise ContractError("review_schema", "accept or reject required")
        self.check_source()
        record = {
            "decision": decision,
            "proposal": proposal,
            "proposal_id": digest(canonical({"thread": self.thread, "proposal": proposal})),
            "external_actions": [],
        }
        old = self.db.execute(
            "SELECT value FROM graph_reviews WHERE thread=?", (self.thread,)
        ).fetchone()
        if old:
            receipt = strict_json(old[0])
            if any(receipt.get(key) != value for key, value in record.items()):
                raise ContractError("decision_conflict", "review already recorded differently")
            update = receipt.get("completion_update")
            if update != {"decision": decision, "result": state["result"]}:
                raise ContractError("checkpoint_integrity", "review receipt update mismatch")
            # Replay an already committed historical decision, not a new approval.
            # The original freshness gate ran before the durable receipt was written.
            return update
        # Approval time cannot refresh evidence: re-run freshness gate with CURRENT clock.
        session = restore_session(self.snapshot, state["binding"], state["session"], self.now)
        result = session._finish(
            {
                "action": "finish",
                "conclusion": state["result"]["conclusion"],
                "evidence_ids": proposal["evidence_ids"],
                "claims": [],
            }
        )
        if result["status"] != "ok":
            return {"result": result | {"usage": state["session"]["usage"]}, "decision": "expired"}
        update = {"decision": decision, "result": state["result"]}
        record |= {"reviewed_at": self.now, "completion_update": update}
        with self.db:
            self.db.execute(
                "INSERT INTO graph_reviews VALUES (?,?)",
                (self.thread, json.dumps(record)),
            )
        self.fault("review_committed")
        return update

    def compile(self):
        graph = StateGraph(State)
        for name, node in (
            ("scope", self.validate_scope),
            ("plan", self.plan),
            ("execute", self.execute),
            ("validate", self.evidence_validation),
            ("review", self.review),
            ("finish", lambda state: {}),
            ("abstain", lambda state: {}),
        ):
            graph.add_node(name, node)
        graph.add_edge(START, "scope")
        graph.add_edge("scope", "plan")
        graph.add_edge("plan", "execute")
        graph.add_edge("execute", "validate")
        graph.add_conditional_edges("validate", self.route, ["plan", "review", "abstain"])
        for name in ("review", "abstain"):
            graph.add_edge(name, "finish")
        graph.add_edge("finish", END)
        return graph.compile(checkpointer=SqliteSaver(self.db))


def run_graph(
    root: Path,
    database: Path,
    thread: str,
    scope: Scope,
    *,
    operation="start",
    decision=None,
    limits=Limits(),
    now=None,
    fault=lambda point: None,
):
    """Host-only interface. Reuse EXACT scope/limits on resume; no model input updates.

    `now` is a trusted offline evaluation clock, not model/user protocol input.
    `fault` is a test hook for real process termination at durability boundaries.
    """
    if type(thread) is not str or not ID.fullmatch(thread):
        raise ContractError("thread", "bounded thread ID required")
    if operation not in ("start", "resume") or decision not in (None, "accept", "reject"):
        raise ContractError("request_schema", "invalid graph operation/decision")
    if operation == "start" and decision is not None:
        raise ContractError("request_schema", "cannot approve before proposal")
    now = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    utc(now)
    snapshot = Snapshot.open(Path(root), scope)
    requested = {
        "version": VERSION,
        "scope": asdict(scope),
        "limits": asdict(limits),
        "root": str(Path(root).resolve()),
        "manifest": snapshot.manifest_sha256,
        "payload_sha256": digest(snapshot.payload),
    }
    with (
        exclusive(database) as filename,
        closing(sqlite3.connect(filename, check_same_thread=False)) as db,
        tracing_context(enabled=False),
    ):
        db.execute("PRAGMA synchronous=FULL")
        run = GraphRun(db, Path(root), snapshot, thread, now, fault)
        existing = db.execute(
            "SELECT binding FROM graph_bindings WHERE thread=?", (thread,)
        ).fetchone()
        if existing:
            binding = strict_json(existing[0])
            if {k: binding[k] for k in requested} != requested:
                raise ContractError(
                    "changed_input", "scope, source, limits and version are immutable"
                )
            if operation == "start":
                raise ContractError("thread_exists", "use resume; start cannot reset budgets")
        else:
            if operation != "start":
                raise ContractError("thread_missing", "cannot resume unknown thread")
            binding = requested | {"started": time.time()}
            with db:
                db.execute("INSERT INTO graph_bindings VALUES (?,?)", (thread, json.dumps(binding)))
        recorded = db.execute(
            "SELECT value FROM graph_reviews WHERE thread=?", (thread,)
        ).fetchone()
        if recorded and decision is not None and strict_json(recorded[0])["decision"] != decision:
            raise ContractError("decision_conflict", "review already recorded differently")
        graph = run.compile()
        config = {"configurable": {"thread_id": thread}, "recursion_limit": 40}
        saved = graph.get_state(config)
        if saved.values:
            if saved.values["binding"] != binding:
                raise ContractError("checkpoint_integrity", "graph and journal binding differ")
            if not saved.next:
                if decision is not None and decision != saved.values.get("decision"):
                    raise ContractError("decision_conflict", "terminal review cannot change")
                return saved.values
            interrupted = any(task.interrupts for task in saved.tasks)
            if decision is not None and not interrupted:
                raise ContractError("review_not_ready", "no proposal awaiting review")
            if interrupted and decision is None:
                return saved.values | {
                    "pending_review": True,
                    "proposal": next(i.value for t in saved.tasks for i in t.interrupts),
                }
            value = Command(resume=decision) if interrupted else None
        else:
            if decision is not None:
                raise ContractError("review_not_ready", "no proposal awaiting review")
            value = {"binding": binding, "index": 0, "session": {}, "result": {}, "decision": ""}
        result = graph.invoke(value, config, durability="sync")
        result.pop("__interrupt__", None)
        pending = [i.value for t in graph.get_state(config).tasks for i in t.interrupts]
        if pending:
            result["pending_review"] = True
            result["proposal"] = pending[0]
        return result
