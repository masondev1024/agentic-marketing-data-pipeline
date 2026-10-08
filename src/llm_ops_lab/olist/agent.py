"""Bounded model-directed investigation over the Olist read model.

The model selects which fixed, read-only evidence tool to call. It never writes
SQL or supplies a scope. Host code validates every action and renders findings.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from llm_ops_lab.olist import diagnostics
from llm_ops_lab.olist.lineage import pipeline_context
from llm_ops_lab.olist.tool_session import OlistToolSession, SessionError

PROMPT_VERSION = "olist-agent.v5"
MAX_QUESTION_CHARS = 2_048
MAX_GENERATIONS = 8
MAX_TOOL_CALLS = 4
MAX_TOTAL_TOKENS = 8_192
MAX_GENERATION_TOKENS = 256
MAX_PROMPT_TOKENS = 2_048
MAX_OUTPUT_BYTES = 16_384
FINDING_CODES = {
    "published_quality_checks_failed",
    "published_quality_warnings",
    "missing_origin_leads",
    "seller_sample_coverage_unknown",
    "recent_pipeline_failures",
    "abandoned_pipeline_runs",
    "pipeline_runs_in_progress",
    "published_sample_only",
}
LIMITATION_CODES = {
    "unsupported_metric",
    "insufficient_evidence",
    "source_timezone_unresolved",
    "source_currency_unresolved",
    "sample_completeness_unknown",
}
SAFE_FAILURE_CODES = {
    "model_response_invalid",
    "model_token_budget",
    "invalid_action",
    "unauthorized_action",
    "tool_budget",
    "tool_repeated",
    "invalid_finish",
    "invalid_evidence_reference",
    "unsupported_finding",
    "abstained_with_findings",
    "empty_or_limited_answer",
    "unverified_metric_refusal",
    "abstained_without_reason",
    "invalid_tool_evidence",
    "metric_policy_mismatch",
}


@lru_cache(maxsize=1)
def _metric_policy() -> tuple[dict[str, Any], str]:
    policy_path = Path(__file__).parents[3] / "data/olist/metric-policy-v1.json"
    try:
        raw = policy_path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return {"policy_version": "unavailable"}, "unavailable"


@lru_cache(maxsize=1)
def _prompt_asset() -> bytes:
    """Pin prompt bytes for this process; restart to adopt a reviewed revision."""
    return Path(__file__).with_name("agent_prompt.txt").read_bytes()


def _strict_json(raw: Any) -> dict[str, Any]:
    if type(raw) is not str or len(raw.encode("utf-8")) > 4_096:
        raise ValueError("invalid_action")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid_action")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_action") from error
    if type(value) is not dict:
        raise ValueError("invalid_action")
    return value


def _abstained(
    version: str | None,
    limitations: list[str],
    *,
    evidence=None,
    trace=None,
    usage=None,
    model_executed=False,
) -> dict[str, Any]:
    prompt_bytes = _prompt_asset()
    policy, policy_hash = _metric_policy()
    prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
    result = {
        "diagnostic_mode": "model_tool_agent",
        "model_executed": model_executed,
        "status": "abstained",
        "stop_reason": limitations[0] if limitations else "abstained",
        "dataset_version": version,
        "evidence": evidence or [],
        "findings": [],
        "limitations": list(dict.fromkeys(limitations))[:8],
        "trace": trace or [],
        "usage": usage
        or {"generations": 0, "tool_calls": 0, "prompt_tokens": 0, "output_tokens": 0},
        "model": {"id": None, "revision": None},
        "model_id": None,
        "model_revision": None,
        "prompt": {"version": PROMPT_VERSION, "sha256": prompt_hash},
        "prompt_sha256": prompt_hash,
        "metric_policy": {"version": policy["policy_version"], "sha256": policy_hash},
    }
    if (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_OUTPUT_BYTES
    ):
        result["evidence"] = []
        result["findings"] = []
        result["limitations"] = ["response_limit"]
        result["stop_reason"] = "response_limit"
    return result


def _supported_finding(code: str, evidence: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Return a fixed host-authored finding only when cited evidence proves it."""

    def item(tool):
        return evidence.get(tool)

    if code in {
        "published_quality_checks_failed",
        "published_quality_warnings",
        "published_sample_only",
    }:
        entry = item("version_evidence")
        if not entry:
            return None
        data = entry["data"]
        count = (
            data.get("failed_check_count", 0)
            if code == "published_quality_checks_failed"
            else data.get("warning_count", 0)
        )
        if code == "published_quality_checks_failed" and type(count) is int and count > 0:
            summary, severity = (
                f"The published quality report records {count} failed checks; "
                "inspect the rule-level report before use.",
                "warning",
            )
        elif code == "published_quality_warnings" and type(count) is int and count > 0:
            summary, severity = (
                f"The published quality report records {count} warning-status checks.",
                "info",
            )
        elif code == "published_sample_only":
            summary, severity = (
                "Counts describe this published historical sample. Missing seller links "
                "and source timestamps do not establish business outcomes or timezone-based "
                "windows.",
                "info",
            )
        else:
            return None
        return {
            "code": code,
            "severity": severity,
            "summary": summary,
            "evidence_ids": [entry["evidence_id"]],
        }

    if code in {"missing_origin_leads", "seller_sample_coverage_unknown"}:
        entry = item("channel_funnel")
        if not entry:
            return None
        rows = entry["data"]
        if code == "missing_origin_leads":
            row = next((r for r in rows if r.get("origin") is None), None)
            if not row or type(row.get("lead_count")) is not int:
                return None
            summary = (
                f"{row['lead_count']} leads have no observed origin; they remain in "
                "the funnel denominator."
            )
        else:
            count = sum(
                r.get("unlinked_contract_count", 0)
                for r in rows
                if type(r.get("unlinked_contract_count")) is int
            )
            if count <= 0:
                return None
            summary = (
                f"{count} contracts are not linked to the commerce seller sample; "
                "their order outcome is unknown, not zero."
            )
        return {
            "code": code,
            "severity": "info",
            "summary": summary,
            "evidence_ids": [entry["evidence_id"]],
        }

    if code in {"recent_pipeline_failures", "abandoned_pipeline_runs", "pipeline_runs_in_progress"}:
        entry = item("run_history")
        if not entry:
            return None
        state = {
            "recent_pipeline_failures": "failed",
            "abandoned_pipeline_runs": "abandoned",
            "pipeline_runs_in_progress": "running",
        }[code]
        count = sum(row.get("status") == state for row in entry["data"])
        if count <= 0:
            return None
        if state == "failed":
            summary = (
                f"{count} of the latest five runs failed before the investigation start; "
                "this history does not establish root cause. Active published version: "
                f"{item('version_evidence')['data'].get('active_published_version_id')}."
            )
            severity = "warning"
        elif state == "abandoned":
            summary = (
                f"{count} of the latest five runs were recorded as abandoned; this is "
                "operational history and does not invalidate the published version."
            )
            severity = "info"
        else:
            summary = (
                f"{count} of the latest five runs were still marked running at the "
                "investigation start; no stuck-run conclusion is inferred."
            )
            severity = "info"
        refs = [entry["evidence_id"]]
        version_entry = item("version_evidence")
        if version_entry:
            refs.append(version_entry["evidence_id"])
        return {
            "code": code,
            "severity": severity,
            "summary": summary,
            "evidence_ids": refs,
        }
    return None


def _finish_actions(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose grounded choices; the model still decides relevance to the question."""
    by_tool = {item["tool"]: item for item in evidence}
    aliases = {item["evidence_id"]: f"e{index}" for index, item in enumerate(evidence, 1)}
    actions = []
    for code in sorted(FINDING_CODES):
        finding = _supported_finding(code, by_tool)
        if finding:
            actions.append({
                "action": "finish", "status": "completed", "finding_codes": [code],
                "evidence_ids": [aliases[ref] for ref in finding["evidence_ids"]],
                "limitation_codes": [],
            })
    if "definitions" in by_tool:
        actions.append({
            "action": "finish", "status": "abstained", "finding_codes": [],
            "evidence_ids": [aliases[by_tool["definitions"]["evidence_id"]]],
            "limitation_codes": ["unsupported_metric"],
        })
    actions.append({
        "action": "finish", "status": "abstained", "finding_codes": [],
        "evidence_ids": [], "limitation_codes": ["insufficient_evidence"],
    })
    return actions


def _messages(question: str, evidence: list[dict[str, Any]], correction: str = ""):
    prompt = _prompt_asset().decode("utf-8")
    executed = [item['tool'] for item in evidence if item['tool'] != 'version_evidence']
    available = sorted(OlistToolSession.tools - set(executed))
    if executed:
        prompt += (
            '\nHOST LOOP STATE: The previous tool call has completed successfully. '
            f'Already executed: {", ".join(executed)}. Do not call these tools again. '
            f'Remaining tools: {", ".join(available) or "none"}. '
            'Read the returned observations. If they answer the question, FINISH now.\n'
        )
    payload = {
        "question_untrusted": question,
        "tool_state": {"executed": executed, "available": available},
        "allowed_tool_actions": [
            {"action": "tool", "tool": tool, "parameters": {}} for tool in available
        ],
        "observations": [
            {"evidence_id": f"e{index}", "tool": e["tool"], "data": _compact(e)}
            for index, e in enumerate(evidence, start=1)
        ],
        "supported_finish_actions": _finish_actions(evidence),
        "correction": correction,
    }
    messages = [{"role": "system", "content": prompt}]
    if executed:
        messages.extend([
            {"role": "user", "content": json.dumps(
                {"question_untrusted": question}, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(
                {"action": "tool", "tool": executed[-1], "parameters": {}},
                separators=(",", ":"))},
        ])
    messages.append({
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    })
    return messages


def _validate_tool_result(item: Any, tool: str, version_id: str) -> dict[str, Any]:
    if (
        type(item) is not dict
        or item.get("tool") != tool
        or item.get("dataset_version") != version_id
        or type(item.get("data")) not in (dict, list)
        or type(item.get("evidence_id")) is not str
        or len(item["evidence_id"]) != 64
        or diagnostics._evidence_id(version_id, tool, item["data"]) != item["evidence_id"]
    ):
        raise ValueError("invalid_tool_evidence")
    if tool == "definitions":
        data = item["data"]
        if (
            type(data) is not dict
            or type(data.get("metric_policy")) is not dict
            or type(data.get("metric_definitions")) is not dict
        ):
            raise ValueError("invalid_tool_evidence")
        if data.get("policy_sha256") != _metric_policy()[1]:
            raise ValueError("metric_policy_mismatch")
    return item


def _compact(entry: dict[str, Any]) -> Any:
    """Keep repeated prompts small while retaining full evidence on the host."""
    data = entry["data"]
    if entry["tool"] == "version_evidence":
        return {
            key: data.get(key)
            for key in (
                "quality_status",
                "failed_check_count",
                "warning_count",
                "check_count",
                "version_id",
                "active_published_version_id",
            )
        }
    if entry["tool"] == "channel_funnel":
        return {
            "row_count": len(data),
            "totals": {
                key: sum(row.get(key, 0) for row in data if type(row.get(key)) is int)
                for key in (
                    "lead_count",
                    "contract_count",
                    "linked_contract_count",
                    "unlinked_contract_count",
                )
            },
            "missing_origin": next(
                (
                    {
                        "lead_count": row.get("lead_count"),
                        "contract_count": row.get("contract_count"),
                        "unlinked_contract_count": row.get("unlinked_contract_count"),
                    }
                    for row in data
                    if row.get("origin") is None
                ),
                None,
            ),
        }
    if entry["tool"] == "seller_funnel":
        return data
    if entry["tool"] == "run_history":
        return [{key: row.get(key) for key in ("status", "step", "error_code")} for row in data]
    if entry["tool"] == "definitions":
        policy = data["metric_policy"]
        return {
            "allowed": policy["allowed"],
            "conditional": policy["conditional"],
            "blocked_metric_names": list(policy["blocked"]),
            "currency_status": policy["currency"]["status"],
            "timezone_status": policy["timezone"]["status"],
            "coverage_status": policy["coverage"]["status"],
            "definitions": {key: value[:150] for key, value in data["metric_definitions"].items()},
        }
    return {}


def investigate(
    dsn: str,
    question: str,
    generate: Callable[[list[dict], float], dict],
    *,
    version_id: str | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Run one model-directed investigation using a trusted DSN and fixed tools."""
    if type(question) is not str or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return _abstained(None, ["invalid_question"])
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        return _abstained(None, ["invalid_timeout"])
    if version_id is not None and diagnostics._safe_version(version_id) is None:
        return _abstained(None, ["invalid_version_id"])
    policy, _ = _metric_policy()
    if policy.get("policy_version") == "unavailable":
        return _abstained(None, ["metric_policy_unavailable"])

    started = time.monotonic()
    deadline = started + float(timeout_seconds)
    trace: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    usage = {"generations": 0, "tool_calls": 0, "prompt_tokens": 0, "output_tokens": 0}
    model_identity = {"id": None, "revision": None}
    try:
        with OlistToolSession(dsn, version_id=version_id, deadline=deadline) as session:
            version_evidence = session.initialize()
            if version_evidence is None:
                return _abstained(
                    session.dataset_version,
                    [session.failure_code or "published_version_unavailable"],
                    trace=trace,
                    usage=usage,
                )
            _validate_tool_result(version_evidence, "version_evidence", session.dataset_version)
            evidence.append(version_evidence)
            quality = version_evidence["data"]
            if quality.get("version_id") != session.dataset_version:
                return _abstained(
                    session.dataset_version,
                    ["version_mismatch"],
                    trace=trace,
                    usage=usage,
                    model_executed=False,
                )
            if (
                quality.get("quality_status") != "passed"
                or quality.get("failed_check_count", 0) > 0
            ):
                finding = _supported_finding(
                    "published_quality_checks_failed", {"version_evidence": version_evidence}
                )
                return _abstained(
                    session.dataset_version,
                    ["published_quality_gate_failed"],
                    evidence=evidence,
                    trace=trace,
                    usage=usage,
                    model_executed=False,
                ) | {"findings": [finding] if finding else []}

            called: set[str] = set()
            protocol_repairs = 0
            correction = ""
            for _ in range(MAX_GENERATIONS):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _abstained(
                        session.dataset_version,
                        ["investigation_deadline_exceeded"],
                        evidence=evidence,
                        trace=trace,
                        usage=usage,
                        model_executed=usage["generations"] > 0,
                    )
                if (
                    usage["prompt_tokens"]
                    + usage["output_tokens"]
                    + MAX_PROMPT_TOKENS
                    + MAX_GENERATION_TOKENS
                    > MAX_TOTAL_TOKENS
                ):
                    return _abstained(
                        session.dataset_version,
                        ["model_token_budget"],
                        evidence=evidence,
                        trace=trace,
                        usage=usage,
                        model_executed=usage["generations"] > 0,
                    )
                try:
                    generation = generate(_messages(question, evidence, correction), remaining)
                    usage["generations"] += 1
                    if type(generation) is not dict or type(generation.get("output")) is not str:
                        raise ValueError("model_response_invalid")
                    call_trace = {
                        "output_sha256": hashlib.sha256(
                            generation["output"].encode("utf-8")
                        ).hexdigest()
                    }
                    prompt_tokens = generation.get("prompt_tokens", 0)
                    output_tokens = generation.get(
                        "output_tokens", generation.get("generation_tokens", 0)
                    )
                    if (
                        type(prompt_tokens) is not int
                        or type(output_tokens) is not int
                        or prompt_tokens < 0
                        or output_tokens < 0
                        or prompt_tokens > MAX_PROMPT_TOKENS
                        or output_tokens > MAX_GENERATION_TOKENS
                    ):
                        raise ValueError("model_token_budget")
                    usage["prompt_tokens"] += prompt_tokens
                    usage["output_tokens"] += output_tokens
                    if usage["prompt_tokens"] + usage["output_tokens"] > MAX_TOTAL_TOKENS:
                        raise ValueError("model_token_budget")
                    trace.append(
                        call_trace
                        | {
                            key: generation[key]
                            for key in (
                                "generation_seconds",
                                "first_generation_event_seconds",
                                "prompt_token_ids_sha256",
                                "model_id",
                                "model_revision",
                            )
                            if key in generation
                        }
                    )
                    model_identity = {
                        "id": generation.get("model_id"),
                        "revision": generation.get("model_revision"),
                    }
                    if generation.get("max_tokens_reached"):
                        raise ValueError("model_token_budget")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline")
                    try:
                        action = _strict_json(generation["output"])
                    except ValueError:
                        trace[-1]["selected_action"] = "malformed_json"
                        protocol_repairs += 1
                        if protocol_repairs > 1:
                            return _abstained(
                                session.dataset_version,
                                ["malformed_model_action"],
                                evidence=evidence,
                                trace=trace,
                                usage=usage,
                                model_executed=True,
                            )
                        trace[-1]["repair_requested"] = True
                        correction = "Return one valid JSON object following the protocol exactly."
                        continue

                    if action.get("action") == "tool":
                        if (
                            set(action) != {"action", "tool", "parameters"}
                            or type(action.get("tool")) is not str
                            or action["tool"] not in session.tools
                            or type(action.get("parameters")) is not dict
                        ):
                            raise ValueError("unauthorized_action")
                        if action["parameters"]:
                            # Never strip invented scope or execute a rejected action.
                            trace[-1]["selected_action"] = "rejected_tool"
                            trace[-1]["validation_error"] = "unexpected_tool_parameters"
                            protocol_repairs += 1
                            if protocol_repairs > 1:
                                raise ValueError("unauthorized_action")
                            trace[-1]["repair_requested"] = True
                            correction = (
                                "Your tool action was rejected and NOT executed. All tools take "
                                "zero arguments. Copy one allowed_tool_actions object exactly, "
                                "or finish using evidence already observed. Do not add filters."
                            )
                            continue
                        tool = action["tool"]
                        trace[-1]["selected_action"] = "tool"
                        trace[-1]["selected_tool"] = tool
                        if tool in called:
                            raise ValueError("tool_repeated")
                        if len(called) >= MAX_TOOL_CALLS:
                            raise ValueError("tool_budget")
                        called.add(tool)
                        usage["tool_calls"] += 1
                        item = session.call(tool)
                        _validate_tool_result(item, tool, session.dataset_version)
                        evidence.append(item)
                        correction = ""
                        continue

                    if action.get("action") != "finish" or set(action) != {
                        "action",
                        "status",
                        "finding_codes",
                        "evidence_ids",
                        "limitation_codes",
                    }:
                        raise ValueError("invalid_finish")
                    if action["status"] not in {"completed", "abstained"}:
                        raise ValueError("invalid_finish")
                    codes, refs, limitations = (
                        action["finding_codes"],
                        action["evidence_ids"],
                        action["limitation_codes"],
                    )
                    aliases = {f"e{index}": entry for index, entry in enumerate(evidence, start=1)}
                    if (
                        type(codes) is not list
                        or len(codes) > 8
                        or any(type(code) is not str or code not in FINDING_CODES for code in codes)
                        or len(set(codes)) != len(codes)
                        or type(refs) is not list
                        or len(refs) > 8
                        or any(type(ref) is not str or ref not in aliases for ref in refs)
                        or type(limitations) is not list
                        or len(limitations) > 8
                        or any(
                            type(code) is not str or code not in LIMITATION_CODES
                            for code in limitations
                        )
                        or len(set(limitations)) != len(limitations)
                    ):
                        raise ValueError("invalid_evidence_reference")
                    if action["status"] == "abstained" and codes:
                        raise ValueError("abstained_with_findings")
                    if action["status"] == "abstained" and not limitations:
                        raise ValueError("abstained_without_reason")
                    if action["status"] == "completed" and (not codes or limitations):
                        raise ValueError("empty_or_limited_answer")
                    metric_limitations = {
                        "unsupported_metric",
                        "source_timezone_unresolved",
                        "source_currency_unresolved",
                        "sample_completeness_unknown",
                    }
                    if metric_limitations.intersection(limitations):
                        definitions_alias = next(
                            (
                                alias
                                for alias, entry in aliases.items()
                                if entry["tool"] == "definitions"
                            ),
                            None,
                        )
                        if definitions_alias is None or definitions_alias not in refs:
                            raise ValueError("unverified_metric_refusal")
                    trace[-1]["selected_action"] = "finish"
                    trace[-1]["finding_codes"] = codes
                    trace[-1]["limitation_codes"] = limitations
                    by_tool = {entry["tool"]: entry for entry in evidence}
                    findings = []
                    for code in codes:
                        finding = _supported_finding(code, by_tool)
                        if finding is None:
                            raise ValueError("unsupported_finding")
                        cited = []
                        for evidence_id in finding["evidence_ids"]:
                            alias = next(
                                (ref for ref in refs if aliases[ref]["evidence_id"] == evidence_id),
                                None,
                            )
                            if alias is None:
                                raise ValueError("unsupported_finding")
                            cited.append(aliases[alias]["evidence_id"])
                        finding["evidence_ids"] = cited
                        findings.append(finding)
                    status = action["status"]
                    result = {
                        "diagnostic_mode": "model_tool_agent",
                        "model_executed": True,
                        "status": status,
                        "stop_reason": "completed" if status == "completed" else "model_abstained",
                        "dataset_version": session.dataset_version,
                        "evidence": evidence,
                        "findings": findings,
                        "limitations": list(
                            dict.fromkeys(
                                [
                                    "source_timezone_unresolved",
                                    "source_currency_unresolved",
                                    "sample_completeness_unknown",
                                    *limitations,
                                ]
                            )
                        )[:8],
                        "trace": trace,
                        "usage": usage,
                        "model": model_identity,
                        "model_id": model_identity["id"],
                        "model_revision": model_identity["revision"],
                        "prompt": {
                            "version": PROMPT_VERSION,
                            "sha256": hashlib.sha256(_prompt_asset()).hexdigest(),
                        },
                    }
                    result["prompt_sha256"] = result["prompt"]["sha256"]
                    policy, policy_hash = _metric_policy()
                    result["metric_policy"] = {
                        "version": policy["policy_version"],
                        "sha256": policy_hash,
                    }
                    history = by_tool.get("run_history")
                    version_evidence = by_tool.get("version_evidence")
                    if history and version_evidence:
                        result["pipeline_context"] = pipeline_context(
                            version_evidence["data"], history["data"]
                        )
                    if (
                        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
                        > MAX_OUTPUT_BYTES
                    ):
                        return _abstained(
                            session.dataset_version,
                            ["response_limit"],
                            trace=trace,
                            usage=usage,
                            model_executed=True,
                        )
                    return result
                except TimeoutError:
                    return _abstained(
                        session.dataset_version,
                        ["investigation_deadline_exceeded"],
                        evidence=evidence,
                        trace=trace,
                        usage=usage,
                        model_executed=usage["generations"] > 0,
                    )
                except (SessionError, ValueError, TypeError, KeyError, OSError) as error:
                    safe_code = (
                        error.code
                        if isinstance(error, SessionError)
                        else str(error)
                        if isinstance(error, ValueError) and str(error) in SAFE_FAILURE_CODES
                        else "invalid_or_unavailable_evidence"
                    )
                    return _abstained(
                        session.dataset_version,
                        [safe_code],
                        evidence=evidence,
                        trace=trace,
                        usage=usage,
                        model_executed=usage["generations"] > 0,
                    )
            return _abstained(
                session.dataset_version,
                ["action_budget_exhausted"],
                evidence=evidence,
                trace=trace,
                usage=usage,
                model_executed=usage["generations"] > 0,
            )
    except SessionError as error:
        return _abstained(
            None, [error.code], trace=trace, usage=usage, model_executed=usage["generations"] > 0
        )
