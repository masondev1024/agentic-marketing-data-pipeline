"""Strict typed intent plans for the bounded Olist investigation agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PROMPT_VERSION = "olist-intent.v7"
MAX_QUESTION_CHARS = 2_048
MAX_CORRECTION_CHARS = 512
MAX_PLAN_BYTES = 4_096
MAX_INTENTS = 3
MAX_REQUESTS = 5

DISPOSITIONS = frozenset(
    {
        "supported",
        "financial_unsupported",
        "unavailable_root_cause",
        "unsafe",
        "out_of_scope",
        "ambiguous",
    }
)
SUPPORTED_INTENTS = frozenset({"origin", "coverage", "pipeline_history", "quality"})
REFUSAL_LABELS = frozenset(
    {
        "financial_unsupported",
        "unavailable_root_cause",
        "unsafe",
        "out_of_scope",
        "ambiguous",
    }
)
REQUEST_LABELS = SUPPORTED_INTENTS | REFUSAL_LABELS
REFUSAL_PRECEDENCE = (
    "unsafe",
    "financial_unsupported",
    "unavailable_root_cause",
    "out_of_scope",
    "ambiguous",
)

INTENT_TOOLS = {
    "origin": ("channel_funnel",),
    "coverage": ("channel_funnel",),
    "pipeline_history": ("run_history",),
    "quality": (),
}
INTENT_FINDINGS = {
    "origin": ("missing_origin_leads",),
    "coverage": ("seller_sample_coverage_unknown",),
    "pipeline_history": (
        "recent_pipeline_failures",
        "abandoned_pipeline_runs",
        "pipeline_runs_in_progress",
    ),
    "quality": ("published_quality_warnings",),
}


class PlanError(ValueError):
    """A plan or planner input violates the stable typed contract."""

    def __init__(self, code: str = "invalid_plan"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Plan:
    disposition: str
    intents: tuple[str, ...]


@lru_cache(maxsize=1)
def prompt_bytes() -> bytes:
    """Return pinned prompt bytes; processes adopt revisions after restart."""
    try:
        return Path(__file__).with_name("intent_prompt.txt").read_bytes()
    except OSError as error:
        raise PlanError("prompt_unavailable") from error


def parse_plan(raw: str) -> Plan:
    """Parse one-axis request labels and derive the host disposition deterministically."""
    if type(raw) is not str:
        raise PlanError()
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PlanError() from error
    if not encoded or len(encoded) > MAX_PLAN_BYTES:
        raise PlanError()

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise PlanError() from error

    if type(payload) is not dict or set(payload) != {"requests"}:
        raise PlanError()
    requests = payload["requests"]
    if type(requests) is not list or not 1 <= len(requests) <= MAX_REQUESTS:
        raise PlanError()
    if any(type(label) is not str or label not in REQUEST_LABELS for label in requests):
        raise PlanError()
    if len(set(requests)) != len(requests):
        raise PlanError()

    blocker = next((label for label in REFUSAL_PRECEDENCE if label in requests), None)
    if blocker is not None:
        return Plan(disposition=blocker, intents=())

    intents = tuple(label for label in requests if label in SUPPORTED_INTENTS)
    if not intents or len(intents) > MAX_INTENTS:
        raise PlanError()
    return Plan(disposition="supported", intents=intents)


def plan_messages(question: str, correction: str = "") -> list[dict[str, str]]:
    """Build a planner-only prompt with no database observations or tool actions."""
    _validate_question(question)
    if type(correction) is not str or len(correction) > MAX_CORRECTION_CHARS:
        raise PlanError("invalid_question")

    payload: dict[str, Any] = {"question_untrusted": question}
    if correction:
        payload["correction"] = correction
    return [
        {"role": "system", "content": prompt_bytes().decode("utf-8")},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _validate_question(question: str) -> None:
    if (
        type(question) is not str
        or not question.strip()
        or len(question) > MAX_QUESTION_CHARS
    ):
        raise PlanError("invalid_question")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
