"""Bounded, deterministic diagnostics over the published Olist read model.

All SQL is owned by this module. The caller cannot supply a query, a table name,
or a row limit. Database evidence is captured in one read-only transaction and
every dataset query is pinned to one immutable ``version_id``.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

DIAGNOSTIC_MODE = "deterministic"
MAX_RESPONSE_BYTES = 16_384
MAX_VERSION_ID_CHARS = 128
MAX_ORIGIN_CHARS = 120
MAX_SQL_TIMEOUT_MS = 5_000
OVERALL_DEADLINE_SECONDS = 15.0

# This list is intentionally static and describes only counts available in the
# published mart. In particular, observed orders are not revenue or attribution.
METRIC_DEFINITIONS = {
    "lead_count": "Source-sample leads grouped by origin; a null origin is retained.",
    "contract_count": "Observed closed deals joined to leads by mql_id in this sample.",
    "linked_contract_count": "Contracts whose seller_id appears in the commerce seller sample.",
    "unlinked_contract_count": (
        "Contracts whose seller is absent from the commerce sample; outcome coverage is unknown."
    ),
    "observed_order_count": (
        "Distinct orders observed for a linked seller across the source sample; "
        "not time-attributed."
    ),
}

# Query names are internal labels for a fixed allowlist of read-model objects.
_CURRENT_VERSION_SQL = "SELECT version_id FROM olist_mart.current_version LIMIT 1"
_VERSION_EVIDENCE_SQL = """
    SELECT version_id, dataset_id, contract_version, transform_sha256, published_at,
           quality_report->>'status' AS quality_status,
           CASE WHEN jsonb_typeof(quality_report->'checks') = 'array'
                THEN jsonb_array_length(quality_report->'checks') ELSE 0 END AS check_count,
           (SELECT count(*) FROM jsonb_array_elements(
               CASE WHEN jsonb_typeof(quality_report->'checks') = 'array'
                    THEN quality_report->'checks' ELSE '[]'::jsonb END
           ) AS checks(item) WHERE item->>'status' = 'failed') AS failed_check_count,
           (SELECT count(*) FROM jsonb_array_elements(
               CASE WHEN jsonb_typeof(quality_report->'checks') = 'array'
                    THEN quality_report->'checks' ELSE '[]'::jsonb END
           ) AS checks(item) WHERE item->>'status' = 'warning') AS warning_count
    FROM olist_mart.version_evidence
    WHERE version_id = %s
    LIMIT 1
"""
_CHANNEL_FUNNEL_SQL = """
    SELECT origin, sum(lead_count)::bigint AS lead_count,
           sum(contract_count)::bigint AS contract_count,
           sum(linked_contract_count)::bigint AS linked_contract_count,
           sum(unlinked_contract_count)::bigint AS unlinked_contract_count
    FROM olist_mart.channel_funnel
    WHERE version_id = %s
    GROUP BY origin
    ORDER BY origin NULLS FIRST
    LIMIT 65
"""
_SELLER_FUNNEL_SQL = """
    SELECT coverage_state, count(*)::bigint AS contract_count,
           count(observed_order_count)::bigint AS contracts_with_observed_order_count,
           sum(observed_order_count)::bigint AS observed_order_count
    FROM olist_mart.seller_funnel
    WHERE version_id = %s
    GROUP BY coverage_state
    ORDER BY coverage_state
    LIMIT 2
"""
_RUN_HISTORY_SQL = """
    SELECT run_id::text AS run_id, version_id, status, step, error_code,
           started_at, finished_at
    FROM olist_mart.run_history
    WHERE started_at <= %s
    ORDER BY started_at DESC, run_id DESC
    LIMIT 5
"""
_READER_ROLE_SQL = "SELECT current_user AS database_role"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _evidence_id(version_id: str, tool: str, payload: Any) -> str:
    """Build a stable content identifier scoped to a dataset version and tool."""
    return hashlib.sha256(_canonical({
        "dataset_version": version_id,
        "tool": tool,
        "payload": payload,
    })).hexdigest()


def _safe_version(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_VERSION_ID_CHARS:
        return None
    return value


def _json_scalar(value: Any) -> Any:
    """Convert the small set of PostgreSQL scalar types returned by fixed queries."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _rows(cursor) -> list[dict[str, Any]]:
    return [{key: _json_scalar(value) for key, value in row.items()} for row in cursor.fetchall()]


def _findings_for(version_id: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tool = {entry["tool"]: entry for entry in evidence}
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, summary: str, refs: list[str]) -> None:
        # The reference check is deliberately centralized: no finding can point
        # at an ID that was not emitted in this response.
        known = {entry["evidence_id"] for entry in evidence}
        if refs and all(ref in known for ref in refs):
            findings.append({"code": code, "severity": severity,
                             "summary": summary, "evidence_ids": refs})

    version = by_tool.get("version_evidence", {}).get("data")
    if version:
        failed = version.get("failed_check_count", 0)
        if isinstance(failed, int) and failed > 0:
            add(
                "published_quality_checks_failed", "warning",
                f"The published quality report records {failed} failed checks; "
                "inspect the rule-level report before use.",
                [by_tool["version_evidence"]["evidence_id"]])
        warning_count = version.get("warning_count", 0)
        if isinstance(warning_count, int) and warning_count > 0:
            add(
                "published_quality_warnings", "info",
                f"The published quality report records {warning_count} "
                "warning-status checks.",
                [by_tool["version_evidence"]["evidence_id"]])

    channel = by_tool.get("channel_funnel", {}).get("data", [])
    if isinstance(channel, list):
        missing_origin = next((row for row in channel if row.get("origin") is None), None)
        if missing_origin and isinstance(missing_origin.get("lead_count"), int):
            add(
                "missing_origin_leads", "info",
                f"{missing_origin['lead_count']} leads have no observed origin; "
                "they remain in the funnel denominator.",
                [by_tool["channel_funnel"]["evidence_id"]])
        unlinked = sum(row.get("unlinked_contract_count", 0) for row in channel
                       if isinstance(row.get("unlinked_contract_count"), int))
        if unlinked > 0:
            add(
                "seller_sample_coverage_unknown", "info",
                f"{unlinked} contracts are not linked to the commerce seller sample; "
                "their order outcome is unknown, not zero.",
                [by_tool["channel_funnel"]["evidence_id"]])

    runs = by_tool.get("run_history", {}).get("data", [])
    failed_runs = (
        [row for row in runs if row.get("status") == "failed"]
        if isinstance(runs, list) else []
    )
    if failed_runs:
        add(
            "recent_pipeline_failures", "warning",
            f"{len(failed_runs)} of the latest five runs failed before the investigation "
            "start; this history is operational evidence and does not invalidate the "
            "published last-good version.",
            [by_tool["run_history"]["evidence_id"]])
    abandoned_runs = (
        [row for row in runs if row.get("status") == "abandoned"]
        if isinstance(runs, list) else []
    )
    if abandoned_runs:
        add(
            "abandoned_pipeline_runs", "info",
            f"{len(abandoned_runs)} of the latest five runs were recorded as abandoned; "
            "this is operational history and does not invalidate the published version.",
            [by_tool["run_history"]["evidence_id"]])
    running_runs = (
        [row for row in runs if row.get("status") == "running"]
        if isinstance(runs, list) else []
    )
    if running_runs:
        add(
            "pipeline_runs_in_progress", "info",
            f"{len(running_runs)} of the latest five runs were still marked running at "
            "the investigation start; no stuck-run conclusion is inferred.",
            [by_tool["run_history"]["evidence_id"]])

    # Include a neutral data-availability statement tied to the pinned version.
    if version:
        add(
            "published_sample_only", "info",
            "Counts describe this published historical sample. Missing seller links and "
            "source timestamps do not establish business outcomes or timezone-based windows.",
            [by_tool["version_evidence"]["evidence_id"]])
    return findings


def build_diagnosis(
    dataset_version: str | None,
    observations: dict[str, Any] | None = None,
    *,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON response from observations, without opening a database.

    Kept separate from I/O so deterministic diagnosis and evidence-reference
    integrity can be tested without Docker or PostgreSQL.
    """
    baseline_limitations = [
        "source_timezone_unresolved",
        "source_currency_unresolved",
        "sample_completeness_unknown",
    ]
    safe_limitations = list(dict.fromkeys(
        [*baseline_limitations, *(str(code) for code in (limitations or []))]
    ))[:8]
    if not dataset_version or observations is None:
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": None,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["no_published_data"],
        })

    tools = ("version_evidence", "channel_funnel", "seller_funnel", "run_history", "definitions")
    if any(tool not in observations for tool in tools):
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": dataset_version,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["required_observation_missing"],
        })
    version_data = observations["version_evidence"]
    if not isinstance(version_data, dict) or version_data.get("version_id") != dataset_version:
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": dataset_version,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["version_mismatch"],
        })
    channel_data = observations["channel_funnel"]
    if (not isinstance(channel_data, list) or len(channel_data) > 64
            or any(not isinstance(row, dict)
                   or (isinstance(row.get("origin"), str)
                       and len(row["origin"]) > MAX_ORIGIN_CHARS)
                   for row in channel_data)):
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": dataset_version,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["channel_evidence_bounds_exceeded"],
        })
    seller_data = observations["seller_funnel"]
    run_data = observations["run_history"]
    if (not isinstance(seller_data, list) or len(seller_data) > 2
            or not isinstance(run_data, list) or len(run_data) > 5):
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": dataset_version,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["observation_bounds_exceeded"],
        })

    evidence: list[dict[str, Any]] = []
    for tool in tools:
        if tool not in observations:
            continue
        payload = observations[tool]
        entry = {
            "evidence_id": _evidence_id(dataset_version, tool, payload),
            "tool": tool,
            "dataset_version": dataset_version,
            "data": payload,
        }
        evidence.append(entry)

    version_observation = next(
        (item for item in evidence if item["tool"] == "version_evidence"), None
    )
    if version_observation is None:
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": None,
            "evidence": [],
            "findings": [],
            "limitations": safe_limitations + ["published_version_unavailable"],
        })

    quality_data = version_observation["data"]
    if (quality_data.get("quality_status") != "passed"
            or quality_data.get("failed_check_count", 0) > 0):
        return _fit_response({
            "model_executed": False,
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "status": "abstained",
            "dataset_version": dataset_version,
            "evidence": evidence,
            "findings": [{
                "code": "published_quality_gate_failed",
                "severity": "error",
                "summary": (
                    "Published quality evidence is not fully passed; "
                    "detailed funnel diagnosis is withheld."
                ),
                "evidence_ids": [version_observation["evidence_id"]],
            }],
            "limitations": safe_limitations + ["published_quality_gate_failed"],
        })

    result = {
        "model_executed": False,
        "diagnostic_mode": DIAGNOSTIC_MODE,
        "status": "completed",
        "dataset_version": dataset_version,
        "evidence": evidence,
        "findings": _findings_for(dataset_version, evidence),
        "limitations": safe_limitations,
    }
    return _fit_response(result)


def _fit_response(response: dict[str, Any]) -> dict[str, Any]:
    """Keep output bounded without silently returning a partial evidence set."""
    if len(_canonical(response)) <= MAX_RESPONSE_BYTES:
        return response
    # Bounded SQL already limits rows. This fallback is for unexpectedly long
    # source labels or metadata and fails closed instead of truncating evidence.
    return {
        "model_executed": False,
        "diagnostic_mode": DIAGNOSTIC_MODE,
        "status": "abstained",
        "dataset_version": response.get("dataset_version"),
        "evidence": [],
        "findings": [],
        "limitations": ["response_limit"],
    }


def _remaining_ms(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return max(1, min(MAX_SQL_TIMEOUT_MS, int(remaining * 1000)))


def _execute(connection, sql: str, params: tuple, deadline: float) -> list[dict[str, Any]]:
    """Set transaction-local timeout from remaining wall budget, then run fixed SQL."""
    timeout_ms = _remaining_ms(deadline)
    connection.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
    _remaining_ms(deadline)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _rows(cursor)


def diagnose(dsn: str, *, version_id: str | None = None) -> dict[str, Any]:
    """Read a bounded Olist diagnosis from PostgreSQL using the reader role.

    ``dsn`` is supplied by trusted application configuration, never from a model
    action. Connection and SQL failures become stable limitation codes; raw
    exception text and connection material are never returned.
    """
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + OVERALL_DEADLINE_SECONDS
    selected_version = _safe_version(version_id) if version_id is not None else None
    if version_id is not None and selected_version is None:
        return build_diagnosis(None, limitations=["invalid_version_id"])

    try:
        with psycopg.connect(
            dsn,
            connect_timeout=5,
            application_name="llm-ops-olist-diagnostics",
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                role_rows = _execute(connection, _READER_ROLE_SQL, (), deadline)
                if not role_rows or role_rows[0].get("database_role") != "olist_reader":
                    return build_diagnosis(None, limitations=["reader_role_required"])

                current_rows = _execute(connection, _CURRENT_VERSION_SQL, (), deadline)
                current_version = (
                    _safe_version(current_rows[0].get("version_id")) if current_rows else None
                )
                if current_version is None:
                    return build_diagnosis(None, limitations=["no_published_data"])
                selected_version = selected_version or current_version

                version_rows = _execute(connection, _VERSION_EVIDENCE_SQL,
                                         (selected_version,), deadline)
                if not version_rows:
                    return build_diagnosis(None, limitations=["published_version_unavailable"])
                version_data = version_rows[0]
                if version_data.get("version_id") != selected_version:
                    return build_diagnosis(None, limitations=["published_version_unavailable"])

                # All data products below bind to this one selected immutable version.
                channel_rows = _execute(connection, _CHANNEL_FUNNEL_SQL,
                                         (selected_version,), deadline)
                seller_rows = _execute(connection, _SELLER_FUNNEL_SQL,
                                        (selected_version,), deadline)
                run_rows = _execute(connection, _RUN_HISTORY_SQL, (started_at,), deadline)
                if len(channel_rows) > 64 or any(
                    isinstance(row.get("origin"), str)
                    and len(row["origin"]) > MAX_ORIGIN_CHARS
                    for row in channel_rows
                ):
                    return build_diagnosis(
                        None, limitations=["channel_evidence_bounds_exceeded"]
                    )
                observations = {
                    "version_evidence": version_data,
                    "channel_funnel": channel_rows,
                    "seller_funnel": seller_rows,
                    "run_history": run_rows,
                    "definitions": METRIC_DEFINITIONS,
                }
                return build_diagnosis(selected_version, observations)
    except TimeoutError:
        return build_diagnosis(selected_version, limitations=["diagnostic_deadline_exceeded"])
    except psycopg.Error:
        return build_diagnosis(selected_version, limitations=["database_read_failed"])
    except Exception:
        # Do not expose DSNs, SQL text, local paths, or driver exception strings.
        return build_diagnosis(selected_version, limitations=["diagnostic_unavailable"])
