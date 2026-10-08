"""Model-planned, host-executed investigation with whole-request scope control.

The model classifies intent once. Host code alone selects fixed tools and renders
findings permitted by the validated plan. The v5 action loop remains a baseline.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Callable

from . import agent as evidence_contract
from . import intent_plan
from .lineage import pipeline_context
from .tool_session import OlistToolSession, SessionError

MAX_PLAN_GENERATIONS = 2
MAX_TOTAL_TOKENS = 4608
MAX_PROMPT_TOKENS = 2048
MAX_OUTPUT_TOKENS = 256
MAX_OUTPUT_BYTES = 16384
OBSERVATION_CODES = frozenset(
    {
        "origin_missing_count_zero",
        "seller_sample_unlinked_count_zero",
        "published_quality_no_warnings",
        "recent_pipeline_no_adverse_status",
    }
)
BACKGROUND_LIMITATIONS = [
    "source_timezone_unresolved",
    "source_currency_unresolved",
    "sample_completeness_unknown",
]


def _result(version, status, reason, evidence, findings, limitations, trace, usage, identity, plan):
    prompt_hash = hashlib.sha256(intent_plan.prompt_bytes()).hexdigest()
    policy, policy_hash = evidence_contract._metric_policy()
    result = {
        "diagnostic_mode": "model_planned_investigation",
        "model_executed": usage["generations"] > 0,
        "model_execution_status": (
            "returned"
            if usage["generations"]
            else "attempted_unknown"
            if usage["generation_attempts"]
            else "not_attempted"
        ),
        "status": status,
        "stop_reason": reason,
        "dataset_version": version,
        "evidence": evidence,
        "findings": findings,
        "limitations": list(dict.fromkeys(limitations)),
        "trace": trace,
        "usage": usage,
        "model": identity,
        "model_id": identity["id"],
        "model_revision": identity["revision"],
        "prompt": {"version": intent_plan.PROMPT_VERSION, "sha256": prompt_hash},
        "prompt_sha256": prompt_hash,
        "metric_policy": {"version": policy["policy_version"], "sha256": policy_hash},
        "request_plan": (
            {"disposition": plan.disposition, "intents": list(plan.intents)} if plan else None
        ),
    }
    by_tool = {item["tool"]: item for item in evidence}
    if "run_history" in by_tool and "version_evidence" in by_tool:
        result["pipeline_context"] = pipeline_context(
            by_tool["version_evidence"]["data"], by_tool["run_history"]["data"]
        )
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        result.update(
            status="abstained",
            stop_reason="response_limit",
            evidence=[],
            findings=[],
            limitations=["response_limit"],
        )
        result.pop("pipeline_context", None)
    return result


def _generation_metadata(generation, usage, trace, identity):
    if type(generation) is not dict or type(generation.get("output")) is not str:
        raise ValueError("model_response_invalid")
    prompt = generation.get("prompt_tokens", 0)
    output = generation.get("output_tokens", generation.get("generation_tokens", 0))
    if (
        type(prompt) is not int
        or type(output) is not int
        or not 0 <= prompt <= MAX_PROMPT_TOKENS
        or not 0 <= output <= MAX_OUTPUT_TOKENS
    ):
        raise ValueError("model_token_budget")
    usage["prompt_tokens"] += prompt
    usage["output_tokens"] += output
    if usage["prompt_tokens"] + usage["output_tokens"] > MAX_TOTAL_TOKENS:
        raise ValueError("model_token_budget")
    if generation.get("max_tokens_reached"):
        raise ValueError("model_token_budget")
    identity.update(id=generation.get("model_id"), revision=generation.get("model_revision"))
    trace.append(
        {
            "stage": "plan",
            "output_sha256": hashlib.sha256(generation["output"].encode()).hexdigest(),
            **{
                key: generation[key]
                for key in ("generation_seconds", "prompt_token_ids_sha256")
                if key in generation
            },
        }
    )


def _findings_for(intent, evidence):
    """Render complete observations, including zero counts, without inferring health."""
    tool = {
        "origin": "channel_funnel",
        "coverage": "channel_funnel",
        "quality": "version_evidence",
        "pipeline_history": "run_history",
    }[intent]
    entry = evidence[tool]
    data = entry["data"]
    if intent in {"origin", "coverage"}:
        fields = (
            "lead_count",
            "contract_count",
            "linked_contract_count",
            "unlinked_contract_count",
        )
        if type(data) is not list or any(
            type(row) is not dict
            or "origin" not in row
            or any(type(row.get(k)) is not int or row[k] < 0 for k in fields)
            for row in data
        ):
            raise ValueError("invalid_tool_evidence")
    elif intent == "quality":
        if type(data) is not dict or any(
            type(data.get(k)) is not int or data[k] < 0
            for k in ("check_count", "warning_count", "failed_check_count")
        ):
            raise ValueError("invalid_tool_evidence")
    elif type(data) is not list or any(
        type(row) is not dict
        or row.get("status") not in {"failed", "abandoned", "running", "published", "reused"}
        for row in data
    ):
        raise ValueError("invalid_tool_evidence")
    findings = [
        finding
        for code in intent_plan.INTENT_FINDINGS[intent]
        if (finding := evidence_contract._supported_finding(code, evidence))
    ]
    if findings:
        if intent in {"origin", "coverage"}:
            _add_denominator(findings[0], intent, data)
        return findings
    clean = {
        "origin": (
            "origin_missing_count_zero",
            "0 leads have missing origin in the returned published channel aggregate; "
            "this does not establish source completeness.",
        ),
        "coverage": (
            "seller_sample_unlinked_count_zero",
            "0 contracts are unlinked in the returned published channel aggregate; "
            "this does not establish complete commerce coverage or order outcomes.",
        ),
        "quality": (
            "published_quality_no_warnings",
            "The published quality report records 0 warnings and 0 failed checks; "
            "this applies only to the recorded checks.",
        ),
        "pipeline_history": (
            "recent_pipeline_no_adverse_status",
            "The returned recent run history records no failed, abandoned or "
            "running status; this does not prove future reliability or root cause.",
        ),
    }
    # Empty aggregates/history carry no observations, rather than proving a clean state.
    if intent != "quality" and not data:
        return []
    if intent == "quality" and data["check_count"] == 0:
        return []
    code, summary = clean[intent]
    finding = {
        "code": code,
        "severity": "info",
        "summary": summary,
        "evidence_ids": [entry["evidence_id"]],
    }
    if intent in {"origin", "coverage"}:
        _add_denominator(finding, intent, data)
    return [finding]


def _add_denominator(finding, intent, rows):
    """Use the same full channel aggregate, never source-wide completeness claims."""
    field = "lead_count" if intent == "origin" else "contract_count"
    total = sum(row[field] for row in rows)
    count = (
        sum(row["lead_count"] for row in rows if row["origin"] is None)
        if intent == "origin"
        else sum(row["unlinked_contract_count"] for row in rows)
    )
    if count > total:
        raise ValueError("invalid_tool_evidence")
    unit = "leads" if intent == "origin" else "contracts"
    finding["observed_counts"] = {
        "numerator": count,
        "denominator": total,
        "unit": unit,
        "scope": "published_channel_aggregate",
    }
    finding["summary"] += f" Observed denominator: {count}/{total} {unit} in this published sample."


def investigate(
    dsn: str, question: str, generate: Callable, *, version_id=None, timeout_seconds=60.0
) -> dict:
    """Plan before opening the DB; semantic refusals perform zero database reads."""
    trace, evidence, findings = [], [], []
    usage = {
        "generation_attempts": 0,
        "generations": 0,
        "tool_calls": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
    }
    identity = {"id": None, "revision": None}
    plan = None
    observed_version = None

    def abstain(reason, *, semantic=False):
        return _result(
            observed_version,
            "abstained",
            "model_abstained" if semantic else reason,
            evidence,
            [],
            [reason],
            trace,
            usage,
            identity,
            plan,
        )

    if type(question) is not str or not question.strip() or len(question) > 2048:
        return abstain("invalid_question")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        return abstain("invalid_timeout")
    if version_id is not None and evidence_contract.diagnostics._safe_version(version_id) is None:
        return abstain("invalid_version_id")
    if evidence_contract._metric_policy()[0].get("policy_version") == "unavailable":
        return abstain("metric_policy_unavailable")
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("investigation_deadline_exceeded")
        return left

    try:
        correction = ""
        for attempt in range(MAX_PLAN_GENERATIONS):
            left = remaining()
            usage["generation_attempts"] += 1
            generation = generate(intent_plan.plan_messages(question, correction), left)
            usage["generations"] += 1
            _generation_metadata(generation, usage, trace, identity)
            remaining()
            try:
                plan = intent_plan.parse_plan(generation["output"])
                trace[-1].update(disposition=plan.disposition, intents=list(plan.intents))
                break
            except ValueError:
                trace[-1]["validation_error"] = "invalid_request_plan"
                if attempt + 1 == MAX_PLAN_GENERATIONS:
                    return abstain("invalid_request_plan")
                trace[-1]["repair_requested"] = True
                correction = (
                    "형식 오류입니다. 허용된 requests 목록만 포함한 JSON 하나를 반환하세요."
                )
        if plan is None:
            return abstain("invalid_request_plan")
        if plan.disposition not in {"supported", "financial_unsupported"}:
            return abstain("insufficient_evidence", semantic=True)

        with OlistToolSession(dsn, version_id=version_id, deadline=deadline) as session:
            observed_version = session.dataset_version
            quality = session.initialize()
            if quality is None:
                return abstain(session.failure_code or "published_version_unavailable")
            evidence_contract._validate_tool_result(quality, "version_evidence", observed_version)
            if quality["data"].get("version_id") != observed_version:
                return abstain("version_mismatch")
            evidence.append(quality)
            if (
                quality["data"].get("quality_status") != "passed"
                or quality["data"].get("failed_check_count", 0) > 0
            ):
                return abstain("published_quality_gate_failed")

            tools = (
                ["definitions"]
                if plan.disposition == "financial_unsupported"
                else list(
                    dict.fromkeys(
                        tool for intent in plan.intents for tool in intent_plan.INTENT_TOOLS[intent]
                    )
                )
            )
            for tool in tools:
                remaining()
                usage["tool_calls"] += 1
                trace.append({"stage": "execute", "selected_action": "tool", "selected_tool": tool})
                item = session.call(tool)
                evidence_contract._validate_tool_result(item, tool, observed_version)
                evidence.append(item)
            remaining()
            if plan.disposition == "financial_unsupported":
                return abstain("unsupported_metric", semantic=True)
            by_tool = {item["tool"]: item for item in evidence}
            for intent in plan.intents:
                intent_findings = _findings_for(intent, by_tool)
                if not intent_findings:
                    # Do not label an incomplete multi-intent request completed.
                    return abstain("insufficient_evidence")
                findings.extend(intent_findings)
            trace.append({"stage": "render", "finding_codes": [f["code"] for f in findings]})
            return _result(
                observed_version,
                "completed",
                "completed",
                evidence,
                findings,
                BACKGROUND_LIMITATIONS,
                trace,
                usage,
                identity,
                plan,
            )
    except TimeoutError:
        trace.append(
            {"stage": "deadline", "generation_attempted": usage["generation_attempts"] > 0}
        )
        return abstain("investigation_deadline_exceeded")
    except SessionError as error:
        return abstain(error.code)
    except RuntimeError:
        trace.append(
            {"stage": "model_failure", "generation_attempted": usage["generation_attempts"] > 0}
        )
        return abstain("model_unavailable")
    except (ValueError, TypeError, KeyError, OSError) as error:
        code = str(error) if isinstance(error, ValueError) else ""
        safe = {
            "model_response_invalid",
            "model_token_budget",
            "invalid_tool_evidence",
            "metric_policy_mismatch",
        }
        return abstain(code if code in safe else "invalid_or_unavailable_evidence")
