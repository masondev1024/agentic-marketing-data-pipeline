"""Strict, versioned SQLite journal for durable Olist investigations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 2
MAX_CALL_BUDGET = 128
MAX_RESPONSE_BYTES = 256 * 1024


class StoreError(Exception):
    def __init__(self, code: str, status: int = 503):
        self.code = code
        self.status = status
        super().__init__(code)


class PendingAttempt(Exception):
    """A previous process reserved an external effect whose outcome is unknown."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_budget(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_CALL_BUDGET:
        raise StoreError("invalid_call_budget", 400)


def initialize(connection: sqlite3.Connection, call_budget: int) -> None:
    """Create only a fresh database; refuse unknown or partial schemas."""
    validate_budget(call_budget)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = {
        "durable_meta",
        "durable_runs",
        "durable_attempts",
        "durable_evidence",
        "durable_budget",
    }
    checkpoint_tables = {"checkpoints", "writes"}
    if version == 0 and not tables:
        connection.executescript(
            """
            CREATE TABLE durable_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE durable_runs (
              investigation_id TEXT PRIMARY KEY,
              principal_hash TEXT NOT NULL,
              key_hash TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              binding_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('running','complete')),
              result_json TEXT,
              result_sha256 TEXT,
              created_at REAL NOT NULL,
              UNIQUE(principal_hash, key_hash)
            );
            CREATE TABLE durable_attempts (
              investigation_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','complete','failed')),
              response_json TEXT,
              PRIMARY KEY(investigation_id, attempt),
              FOREIGN KEY(investigation_id) REFERENCES durable_runs(investigation_id)
            );
            CREATE TABLE durable_evidence (
              investigation_id TEXT PRIMARY KEY,
              status TEXT NOT NULL CHECK(status IN ('pending','complete')),
              payload_json TEXT,
              payload_sha256 TEXT,
              FOREIGN KEY(investigation_id) REFERENCES durable_runs(investigation_id)
            );
            CREATE TABLE durable_budget (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              call_limit INTEGER NOT NULL,
              calls_reserved INTEGER NOT NULL CHECK(calls_reserved >= 0)
            );
            PRAGMA user_version=2;
            """
        )
        with connection:
            connection.execute(
                "INSERT INTO durable_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT INTO durable_budget(singleton,call_limit,calls_reserved) VALUES(1,?,0)",
                (call_budget,),
            )
        return
    if version != SCHEMA_VERSION or tables - checkpoint_tables != expected:
        raise StoreError("durable_schema_unsupported")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(durable_runs)")}
    if not {"result_json", "result_sha256"}.issubset(columns):
        raise StoreError("durable_schema_unsupported")
    if tables & checkpoint_tables not in (set(), checkpoint_tables):
        raise StoreError("durable_schema_corrupt")
    stored = connection.execute(
        "SELECT value FROM durable_meta WHERE key='schema_version'"
    ).fetchone()
    budget = connection.execute(
        "SELECT call_limit,calls_reserved FROM durable_budget WHERE singleton=1"
    ).fetchone()
    if stored is None or stored[0] != str(SCHEMA_VERSION) or budget is None:
        raise StoreError("durable_schema_corrupt")
    if budget[0] != call_budget:
        raise StoreError("durable_config_changed", 409)
    if type(budget[1]) is not int or budget[1] < 0 or budget[1] > budget[0]:
        raise StoreError("durable_budget_corrupt")


def budget_status(database: Path, total_call_budget: int = 32) -> dict[str, Any]:
    """Read the durable budget without creating files or mutating the database."""
    validate_budget(total_call_budget)
    database = Path(os.path.abspath(Path(database).expanduser()))
    result = {
        "limit": total_call_budget,
        "reserved": 0,
        "remaining": total_call_budget,
        "scope": "durable_ledger",
    }
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        return result
    except OSError as error:
        raise StoreError("durable_store_unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise StoreError("durable_store_unavailable")
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and not tables:
                return result
            if version != SCHEMA_VERSION:
                raise StoreError("durable_schema_unsupported")
            expected = {
                "durable_meta",
                "durable_runs",
                "durable_attempts",
                "durable_evidence",
                "durable_budget",
            }
            if tables - {"checkpoints", "writes"} != expected:
                raise StoreError("durable_schema_corrupt")
            if tables & {"checkpoints", "writes"} not in (set(), {"checkpoints", "writes"}):
                raise StoreError("durable_schema_corrupt")
            schema_row = connection.execute(
                "SELECT value FROM durable_meta WHERE key='schema_version'"
            ).fetchone()
            if schema_row is None or schema_row[0] != str(SCHEMA_VERSION):
                raise StoreError("durable_schema_corrupt")
            row = connection.execute(
                "SELECT call_limit,calls_reserved FROM durable_budget WHERE singleton=1"
            ).fetchone()
    except sqlite3.Error as error:
        raise StoreError("durable_store_unavailable") from error
    if row is None or row[0] != total_call_budget:
        raise StoreError("durable_config_changed", 409)
    if type(row[1]) is not int or not 0 <= row[1] <= row[0]:
        raise StoreError("durable_budget_corrupt")
    result.update(reserved=row[1], remaining=row[0] - row[1])
    return result


class GraphStore:
    def __init__(self, connection: sqlite3.Connection, call_budget: int):
        self.connection = connection
        initialize(connection, call_budget)

    def claim(
        self, principal: str, key: str, binding: dict[str, Any]
    ) -> tuple[str, dict | None, dict[str, Any]]:
        principal_hash = digest(principal.encode("utf-8"))
        key_hash = digest((principal + "\0" + key).encode("utf-8"))
        request_hash = digest(canonical(binding))
        row = self.connection.execute(
            "SELECT investigation_id,request_hash,status,result_json,result_sha256,binding_json "
            "FROM durable_runs "
            "WHERE principal_hash=? AND key_hash=?",
            (principal_hash, key_hash),
        ).fetchone()
        if row:
            if row[1] != request_hash:
                raise StoreError("idempotency_conflict", 409)
            saved_binding = json.loads(row[5])
            if digest(canonical(saved_binding)) != row[1]:
                raise StoreError("durable_binding_corrupt")
            if row[2] == "complete":
                if row[3] is None or digest(row[3].encode("utf-8")) != row[4]:
                    raise StoreError("durable_result_corrupt")
                try:
                    result = json.loads(row[3])
                except (ValueError, UnicodeError) as error:
                    raise StoreError("durable_result_corrupt") from error
                if type(result) is not dict:
                    raise StoreError("durable_result_corrupt")
            else:
                result = None
            return row[0], result, saved_binding

        import uuid

        investigation_id = str(uuid.uuid4())
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO durable_runs VALUES(?,?,?,?,?,'running',NULL,NULL,?)",
                    (
                        investigation_id,
                        principal_hash,
                        key_hash,
                        request_hash,
                        json.dumps(binding, sort_keys=True, separators=(",", ":")),
                        time.time(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError("idempotency_conflict", 409) from error
        return investigation_id, None, binding

    def reserve_attempt(self, investigation_id: str, attempt: int) -> dict | None:
        row = self.connection.execute(
            "SELECT status,response_json FROM durable_attempts "
            "WHERE investigation_id=? AND attempt=?",
            (investigation_id, attempt),
        ).fetchone()
        if row:
            if row[0] == "complete" and row[1] is not None:
                return json.loads(row[1])
            if row[0] == "failed" and row[1] is not None:
                return json.loads(row[1])
            raise PendingAttempt
        budget = self.connection.execute(
            "SELECT call_limit,calls_reserved FROM durable_budget WHERE singleton=1"
        ).fetchone()
        if budget[1] >= budget[0]:
            raise StoreError("call_budget_exhausted", 503)
        with self.connection:
            self.connection.execute(
                "INSERT INTO durable_attempts VALUES(?,?,'pending',NULL)",
                (investigation_id, attempt),
            )
            self.connection.execute(
                "UPDATE durable_budget SET calls_reserved=calls_reserved+1 WHERE singleton=1"
            )
        return None

    def commit_attempt(self, investigation_id: str, attempt: int, response: dict) -> None:
        try:
            encoded = json.dumps(
                response, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as error:
            raise StoreError("provider_response_invalid") from error
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise StoreError("provider_response_too_large")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE durable_attempts SET status='complete',response_json=? "
                "WHERE investigation_id=? AND attempt=? AND status='pending'",
                (encoded, investigation_id, attempt),
            )
            if cursor.rowcount != 1:
                raise StoreError("durable_attempt_integrity")

    def commit_attempt_failure(self, investigation_id: str, attempt: int, code: str) -> None:
        encoded = json.dumps({"__provider_error__": code}, separators=(",", ":"))
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE durable_attempts SET status='failed',response_json=? "
                "WHERE investigation_id=? AND attempt=? AND status='pending'",
                (encoded, investigation_id, attempt),
            )
            if cursor.rowcount != 1:
                raise StoreError("durable_attempt_integrity")

    def evidence(self, investigation_id: str) -> list[dict] | None:
        row = self.connection.execute(
            "SELECT status,payload_json,payload_sha256 FROM durable_evidence "
            "WHERE investigation_id=?",
            (investigation_id,),
        ).fetchone()
        if row is None:
            return None
        if row[0] == "pending":
            raise PendingAttempt
        raw = row[1].encode("utf-8") if row[1] is not None else b""
        if row[1] is None or digest(raw) != row[2]:
            raise StoreError("durable_evidence_corrupt")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise StoreError("durable_evidence_corrupt") from error
        if type(value) is not list:
            raise StoreError("durable_evidence_corrupt")
        return value

    def reserve_evidence(self, investigation_id: str) -> None:
        row = self.connection.execute(
            "SELECT status FROM durable_evidence WHERE investigation_id=?", (investigation_id,)
        ).fetchone()
        if row:
            if row[0] == "pending":
                raise PendingAttempt
            return
        with self.connection:
            self.connection.execute(
                "INSERT INTO durable_evidence VALUES(?,'pending',NULL,NULL)",
                (investigation_id,),
            )

    def commit_evidence(self, investigation_id: str, bundle: list[dict]) -> None:
        raw = canonical(bundle)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise StoreError("durable_evidence_too_large")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE durable_evidence SET status='complete',payload_json=?,payload_sha256=? "
                "WHERE investigation_id=? AND status='pending'",
                (raw.decode("utf-8"), digest(raw), investigation_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("durable_evidence_integrity")

    def commit_result(self, investigation_id: str, result: dict) -> None:
        try:
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise StoreError("durable_result_invalid") from error
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise StoreError("durable_result_too_large")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE durable_runs SET status='complete',result_json=?,result_sha256=? "
                "WHERE investigation_id=? AND status='running'",
                (encoded, digest(encoded.encode("utf-8")), investigation_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("durable_result_integrity")
