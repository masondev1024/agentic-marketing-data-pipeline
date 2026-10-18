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
from . import semantics as semantic_contract
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


def _result(version, status, reason, evidence, findings, limitations, trace, usage, identity, plan,
            semantic_validation=None):
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
    if semantic_validation is not None:
        result["semantic_validation"] = semantic_validation
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
        result.pop("semantic_validation", None)
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


def _empty_usage() -> dict:
    return {
        "generation_attempts": 0,
        "generations": 0,
        "tool_calls": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
    }


def plan_stage(question: str, generate: Callable, remaining: Callable) -> dict:
    """Generate and validate the bounded plan without opening the evidence DB."""
    trace, usage, identity = [], _empty_usage(), {"id": None, "revision": None}
    if type(question) is not str or not question.strip() or len(question) > 2048:
        return {"reason": "invalid_question", "trace": trace, "usage": usage,
                "identity": identity, "plan": None}
    if evidence_contract._metric_policy()[0].get("policy_version") == "unavailable":
        return {"reason": "metric_policy_unavailable", "trace": trace, "usage": usage,
                "identity": identity, "plan": None}
    correction = ""
    plan = None
    for attempt in range(MAX_PLAN_GENERATIONS):
        left = remaining()
        usage["generation_attempts"] += 1
        try:
            generation = generate(intent_plan.plan_messages(question, correction), left)
        except TimeoutError:
            return {"reason": "investigation_deadline_exceeded", "trace": trace,
                    "usage": usage, "identity": identity, "plan": None}
        except RuntimeError:
            return {"reason": "model_unavailable", "trace": trace, "usage": usage,
                    "identity": identity, "plan": None}
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
                return {"reason": "invalid_request_plan", "trace": trace,
                        "usage": usage, "identity": identity, "plan": None}
            trace[-1]["repair_requested"] = True
            correction = "형식 오류입니다. 허용된 requests 목록만 포함한 JSON 하나를 반환하세요."
    return {"reason": None, "trace": trace, "usage": usage, "identity": identity,
            "plan": plan, "semantic_refusal": plan.disposition not in {
                "supported", "financial_unsupported"}}


def evidence_stage(
    dsn: str,
    plan: intent_plan.Plan,
    version_id: str | None,
    remaining: Callable,
    *,
    deadline: float,
    evidence_bundle: Callable | None = None,
    started_at=None,
) -> dict:
    """Collect and validate one complete evidence bundle for a committed plan."""
    tools = (["definitions"] if plan.disposition == "financial_unsupported" else list(
        dict.fromkeys(tool for intent in plan.intents for tool in intent_plan.INTENT_TOOLS[intent])
    ))
    usage, trace, evidence = _empty_usage(), [], []
    if plan.disposition not in {"supported", "financial_unsupported"}:
        return {"reason": "insufficient_evidence", "semantic_refusal": True,
                "observed_version": None, "evidence": evidence, "trace": trace,
                "usage": usage}
    if evidence_bundle is None:
        session_options = {"version_id": version_id, "deadline": deadline}
        if started_at is not None:
            session_options["started_at"] = started_at
        with OlistToolSession(dsn, **session_options) as session:
            observed = session.dataset_version
            if hasattr(session, "collect_bundle"):
                bundle = session.collect_bundle(tools)
            else:
                quality = session.initialize()
                if quality is None:
                    return {"reason": session.failure_code or "published_version_unavailable",
                            "observed_version": observed, "evidence": [], "trace": trace,
                            "usage": usage}
                bundle = [quality]
                if quality["data"].get("quality_status") == "passed" and (
                    quality["data"].get("failed_check_count", 0) == 0
                ):
                    bundle.extend(session.call(tool) for tool in tools)
    else:
        bundle = evidence_bundle(tools, version_id, deadline)
        if type(bundle) is not list or not bundle:
            return {"reason": "invalid_tool_evidence", "observed_version": None,
                    "evidence": [], "trace": trace, "usage": usage}
        observed = bundle[0].get("dataset_version")
    quality = bundle[0]
    evidence_contract._validate_tool_result(quality, "version_evidence", observed)
    if quality["data"].get("version_id") != observed:
        return {"reason": "version_mismatch", "observed_version": observed,
                "evidence": [], "trace": trace, "usage": usage}
    if quality["data"].get("quality_status") != "passed" or quality["data"].get(
        "failed_check_count", 0
    ) > 0:
        return {"reason": "published_quality_gate_failed", "observed_version": observed,
                "evidence": [quality], "trace": trace, "usage": usage}
    evidence.append(quality)
    if len(bundle) != len(tools) + 1:
        return {"reason": "invalid_tool_evidence", "observed_version": observed,
                "evidence": evidence, "trace": trace, "usage": usage}
    for tool, item in zip(tools, bundle[1:], strict=True):
        remaining()
        usage["tool_calls"] += 1
        evidence_contract._validate_tool_result(item, tool, observed)
        evidence.append(item)
        trace.append({"stage": "execute", "selected_action": "tool", "selected_tool": tool})
    remaining()
    return {"reason": "unsupported_metric" if plan.disposition == "financial_unsupported" else None,
            "semantic_refusal": plan.disposition == "financial_unsupported",
            "observed_version": observed, "evidence": evidence, "trace": trace,
            "usage": usage}


def render_stage(plan, evidence_result, plan_result, *, remaining=None) -> dict:
    """Render only validated facts from the plan and committed evidence bundle."""
    evidence = evidence_result["evidence"]
    trace = plan_result["trace"] + evidence_result["trace"]
    usage = plan_result["usage"] | {
        key: plan_result["usage"][key] + evidence_result["usage"].get(key, 0)
        for key in ("tool_calls",)
    }
    identity = plan_result["identity"]
    observed = evidence_result.get("observed_version")
    reason = evidence_result.get("reason") or plan_result.get("reason")
    semantic = evidence_result.get("semantic_refusal") or plan_result.get("semantic_refusal")
    if reason:
        return _result(observed, "abstained", "model_abstained" if semantic else reason,
                       evidence, [], [reason], trace, usage, identity, plan)
    try:
        if remaining is not None:
            remaining()
        semantic_report = semantic_contract.validate_bundle(evidence)
        if remaining is not None:
            remaining()
    except semantic_contract.SemanticValidationError as error:
        return _result(observed, "abstained", "semantic_validation_failed", [], [],
                       ["semantic_validation_failed"], trace, usage, identity, plan,
                       {"status": "failed", "code": error.code})
    except TimeoutError:
        return _result(observed, "abstained", "investigation_deadline_exceeded", [], [],
                       ["investigation_deadline_exceeded"], trace, usage, identity, plan)
    findings = []
    by_tool = {item["tool"]: item for item in evidence}
    for intent in plan.intents:
        intent_findings = _findings_for(intent, by_tool)
        if not intent_findings:
            return _result(observed, "abstained", "insufficient_evidence", evidence, [],
                           ["insufficient_evidence"], trace, usage, identity, plan)
        findings.extend(intent_findings)
    trace.append({"stage": "render", "finding_codes": [f["code"] for f in findings]})
    return _result(observed, "completed", "completed", evidence, findings,
                   BACKGROUND_LIMITATIONS, trace, usage, identity, plan, semantic_report)


def investigate(
    dsn: str,
    question: str,
    generate: Callable,
    *,
    version_id=None,
    timeout_seconds=60.0,
    evidence_bundle: Callable | None = None,
    started_at=None,
) -> dict:
    """Run the shared plan, evidence, and render stages for direct or durable use."""
    empty_plan = {"trace": [], "usage": _empty_usage(), "identity": {"id": None,
                    "revision": None}, "plan": None}
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300):
        return render_stage(None, {"reason": "invalid_timeout", "evidence": [],
                            "trace": [], "usage": _empty_usage()}, empty_plan)
    if version_id is not None and evidence_contract.diagnostics._safe_version(version_id) is None:
        return render_stage(None, {"reason": "invalid_version_id", "evidence": [],
                            "trace": [], "usage": _empty_usage()}, empty_plan)
    if version_id is not None and evidence_contract.diagnostics._safe_version(version_id) is None:
        return render_stage(None, {"reason": "invalid_version_id", "evidence": [],
                            "trace": [], "usage": _empty_usage()}, empty_plan)
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("investigation_deadline_exceeded")
        return left

    try:
        planned = plan_stage(question, generate, remaining)
        if planned.get("reason") or planned.get("semantic_refusal"):
            return render_stage(planned.get("plan"), {"reason": planned.get("reason") or
                "insufficient_evidence", "semantic_refusal": planned.get("semantic_refusal"),
                "evidence": [], "trace": [], "usage": _empty_usage()}, planned)
        evidence = evidence_stage(dsn, planned["plan"], version_id, remaining,
                                  deadline=deadline, evidence_bundle=evidence_bundle,
                                  started_at=started_at)
        return render_stage(planned["plan"], evidence, planned, remaining=remaining)
    except TimeoutError:
        planned = locals().get("planned", empty_plan)
        trace = planned.get("trace", []) + [{"stage": "deadline", "generation_attempted":
                planned.get("usage", {}).get("generation_attempts", 0) > 0}]
        identity = planned.get("identity", empty_plan["identity"])
        return _result(None, "abstained", "investigation_deadline_exceeded", [], [],
                       ["investigation_deadline_exceeded"], trace,
                       planned.get("usage", _empty_usage()), identity, planned.get("plan"))
    except SessionError as error:
        return render_stage(None, {"reason": error.code, "evidence": [], "trace": [],
                            "usage": _empty_usage()}, empty_plan)
    except RuntimeError:
        planned = locals().get("planned", empty_plan)
        planned.setdefault("trace", []).append({"stage": "model_failure",
            "generation_attempted": planned.get("usage", {}).get("generation_attempts", 0) > 0})
        return _result(None, "abstained", "model_unavailable", [], [], ["model_unavailable"],
                       planned["trace"], planned.get("usage", _empty_usage()),
                       planned.get("identity", empty_plan["identity"]), planned.get("plan"))
    except (ValueError, TypeError, KeyError, OSError) as error:
        code = str(error) if isinstance(error, ValueError) else ""
        safe = {"model_response_invalid", "model_token_budget", "invalid_tool_evidence",
                "metric_policy_mismatch"}
        reason = code if code in safe else "invalid_or_unavailable_evidence"
        return render_stage(None, {"reason": reason, "evidence": [], "trace": [],
                            "usage": _empty_usage()}, empty_plan)
