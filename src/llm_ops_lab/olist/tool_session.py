"""One bounded, read-only PostgreSQL snapshot for Olist investigation tools."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from llm_ops_lab.olist import diagnostics

MAX_TOOL_SECONDS = 5.0
TOOLS = frozenset({"channel_funnel", "seller_funnel", "run_history", "definitions"})


class SessionError(RuntimeError):
    """Safe, stable failure raised by the database boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OlistToolSession:
    """Trusted scope and connection; model actions can name tools only."""

    tools = TOOLS

    def __init__(
        self,
        dsn: str,
        *,
        version_id: str | None,
        deadline: float,
        started_at: datetime | None = None,
    ):
        self._dsn = dsn
        self._requested_version = version_id
        self._deadline = deadline
        self._started_at = started_at or datetime.now(UTC)
        if self._started_at.tzinfo is None or self._started_at.utcoffset() is None:
            raise ValueError("started_at_must_be_timezone_aware")
        self._connection = None
        self.dataset_version: str | None = None
        self.active_published_version_id: str | None = None
        self.failure_code: str | None = None

    def __enter__(self):
        remaining = self._remaining()
        try:
            self._connection = psycopg.connect(
                self._dsn,
                connect_timeout=max(1, min(5, int(remaining))),
                application_name="llm-ops-olist-agent",
                row_factory=dict_row,
                autocommit=False,
            )
            self._connection.__enter__()
            self._connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            role_rows = self._query(diagnostics._READER_ROLE_SQL, ())
            if not role_rows or role_rows[0].get("database_role") != "olist_reader":
                raise SessionError("reader_role_required")
            current_rows = self._query(diagnostics._CURRENT_VERSION_SQL, ())
            current = (
                diagnostics._safe_version(current_rows[0].get("version_id"))
                if current_rows
                else None
            )
            if current is None:
                raise SessionError("no_published_data")
            self.active_published_version_id = current
            self.dataset_version = self._requested_version or current
            return self
        except SessionError:
            self.close()
            raise
        except psycopg.Error as error:
            self.close()
            raise SessionError("database_read_failed") from error
        except TimeoutError as error:
            self.close()
            raise SessionError("investigation_deadline_exceeded") from error

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self):
        if self._connection is not None:
            try:
                self._connection.__exit__(None, None, None)
            except Exception:
                pass
            self._connection = None

    def _remaining(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def _query(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        left = self._remaining()
        timeout_ms = max(
            1, min(diagnostics.MAX_SQL_TIMEOUT_MS, int(min(left, MAX_TOOL_SECONDS) * 1000))
        )
        self._connection.execute(
            "SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",)
        )
        self._remaining()
        with self._connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = diagnostics._rows(cursor)
        self._remaining()
        return rows

    def initialize(self) -> dict[str, Any] | None:
        """Mandatory host quality/version gate before the model can call tools."""
        try:
            rows = self._query(diagnostics._VERSION_EVIDENCE_SQL, (self.dataset_version,))
            if not rows or rows[0].get("version_id") != self.dataset_version:
                self.failure_code = "published_version_unavailable"
                return None
            # Bind the active pointer observed in this same repeatable-read snapshot.
            rows[0]["active_published_version_id"] = self.active_published_version_id
            return self._evidence("version_evidence", rows[0])
        except TimeoutError as error:
            self.failure_code = "investigation_deadline_exceeded"
            raise SessionError(self.failure_code) from error
        except psycopg.Error as error:
            self.failure_code = "database_read_failed"
            raise SessionError(self.failure_code) from error

    def call(self, tool: str) -> dict[str, Any]:
        if self._connection is None or tool not in TOOLS:
            raise SessionError("unauthorized_tool")
        try:
            if tool == "channel_funnel":
                rows = self._query(diagnostics._CHANNEL_FUNNEL_SQL, (self.dataset_version,))
                if len(rows) > 64 or any(
                    isinstance(row.get("origin"), str)
                    and len(row["origin"]) > diagnostics.MAX_ORIGIN_CHARS
                    for row in rows
                ):
                    raise SessionError("channel_evidence_bounds_exceeded")
                data: Any = rows
            elif tool == "seller_funnel":
                rows = self._query(diagnostics._SELLER_FUNNEL_SQL, (self.dataset_version,))
                if len(rows) > 2:
                    raise SessionError("seller_evidence_bounds_exceeded")
                data = rows
            elif tool == "run_history":
                rows = self._query(diagnostics._RUN_HISTORY_SQL, (self._started_at,))
                if len(rows) > 5:
                    raise SessionError("run_history_bounds_exceeded")
                data = rows
            else:
                policy_path = Path(__file__).parents[3] / "data/olist/metric-policy-v1.json"
                try:
                    policy_bytes = policy_path.read_bytes()
                    policy = json.loads(policy_bytes)
                except (OSError, ValueError) as error:
                    raise SessionError("metric_policy_unavailable") from error
                data = {
                    "metric_definitions": diagnostics.METRIC_DEFINITIONS,
                    "metric_policy": policy,
                    "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                }
            return self._evidence(tool, data)
        except TimeoutError as error:
            raise SessionError("investigation_deadline_exceeded") from error
        except psycopg.Error as error:
            raise SessionError("database_read_failed") from error

    def collect_bundle(self, tools: list[str]) -> list[dict[str, Any]]:
        """Read quality and all selected tools in this one repeatable-read transaction.

        The caller persists the returned list as one durable unit. No partial result
        escapes if a selected tool fails, and a failed quality gate runs no tools.
        """
        quality = self.initialize()
        if quality is None:
            raise SessionError(self.failure_code or "published_version_unavailable")
        bundle = [quality]
        if (
            quality["data"].get("quality_status") != "passed"
            or quality["data"].get("failed_check_count", 0) > 0
        ):
            return bundle
        for tool in tools:
            bundle.append(self.call(tool))
        return bundle

    def _evidence(self, tool: str, data: Any) -> dict[str, Any]:
        evidence_id = diagnostics._evidence_id(self.dataset_version, tool, data)
        return {
            "evidence_id": evidence_id,
            "tool": tool,
            "dataset_version": self.dataset_version,
            "data": data,
        }
