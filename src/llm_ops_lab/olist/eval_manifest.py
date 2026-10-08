"""Strict loader for bounded Olist investigation evaluation manifests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import FINDING_CODES as ACTION_FINDING_CODES
from .agent import LIMITATION_CODES
from .intent_agent import OBSERVATION_CODES
from .tool_session import TOOLS

FINDING_CODES = ACTION_FINDING_CODES | OBSERVATION_CODES

MAX_MANIFEST_BYTES = 128_000
MAX_CASES = 64
MAX_QUESTION_CHARS = 2_048
MAX_LIST_ITEMS = 8
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_ROOT_REQUIRED = {"version", "purpose", "cases"}
_ROOT_OPTIONAL = {"provenance"}
_CASE_REQUIRED = {"id", "question"}
_CASE_OPTIONAL = {
    "required_codes",
    "required_tools",
    "reference_codes",
    "expected_refusal",
    "expected_abstention",
    "persona",
    "pair_id",
    "allowed_codes",
    "max_tool_calls",
}
_PROVENANCE_FIELDS = {
    "source",
    "nvidia_data_downloaded",
    "nvidia_dataset_derived",
    "evaluation_use",
    "freeze_policy",
}


class EvalManifestError(ValueError):
    """Raised when a manifest violates the evaluation data contract."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    question: str
    required_codes: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    reference_codes: tuple[str, ...] = ()
    expected_refusal: str | None = None
    expected_abstention: bool = False
    persona: str | None = None
    pair_id: str | None = None
    allowed_codes: tuple[str, ...] | None = None
    max_tool_calls: int | None = None


@dataclass(frozen=True, slots=True)
class EvalManifest:
    version: str
    purpose: str
    cases: tuple[EvalCase, ...]
    provenance: dict[str, Any] | None = None


def load_eval_manifest(path: Path | str) -> EvalManifest:
    """Read and validate a bounded JSON manifest without accepting drift."""
    source = Path(path)
    if not _SAFE_ID.fullmatch(source.stem):
        raise EvalManifestError("manifest filename must have a safe lowercase id stem")
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise EvalManifestError("manifest could not be read") from exc
    return parse_eval_manifest(raw)


def parse_eval_manifest(raw: bytes) -> EvalManifest:
    """Parse and validate the exact bounded byte sequence used for hashing."""
    if type(raw) is not bytes:
        raise EvalManifestError("manifest input must be bytes")
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise EvalManifestError("manifest must be non-empty and within the byte limit")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvalManifestError("manifest must be valid UTF-8 JSON without duplicate keys") from exc

    if type(payload) is not dict:
        raise EvalManifestError("manifest root must be an object")
    _check_fields(payload, _ROOT_REQUIRED, _ROOT_OPTIONAL, "manifest")
    version = _nonempty_string(payload["version"], "version", 64)
    if not _SAFE_VERSION.fullmatch(version):
        raise EvalManifestError("version must be a safe identifier")
    purpose = _nonempty_string(payload["purpose"], "purpose", 500)
    raw_cases = payload["cases"]
    if type(raw_cases) is not list or not 1 <= len(raw_cases) <= MAX_CASES:
        raise EvalManifestError(f"cases must contain between 1 and {MAX_CASES} entries")

    cases = tuple(_parse_case(item, index) for index, item in enumerate(raw_cases))
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise EvalManifestError("case ids must be unique")
    _validate_pairs(cases)

    provenance = None
    if "provenance" in payload:
        provenance = _validate_provenance(payload["provenance"])
    return EvalManifest(version, purpose, cases, provenance)


def _parse_case(value: Any, index: int) -> EvalCase:
    label = f"cases[{index}]"
    if type(value) is not dict:
        raise EvalManifestError(f"{label} must be an object")
    _check_fields(value, _CASE_REQUIRED, _CASE_OPTIONAL, label)
    case_id = _nonempty_string(value["id"], f"{label}.id", 64)
    if not _SAFE_ID.fullmatch(case_id):
        raise EvalManifestError(f"{label}.id must be a safe lowercase identifier")
    question = _nonempty_string(value["question"], f"{label}.question", MAX_QUESTION_CHARS)
    required_codes = _code_list(value.get("required_codes", []), FINDING_CODES,
                                f"{label}.required_codes")
    required_tools = _code_list(value.get("required_tools", []), TOOLS,
                                f"{label}.required_tools")
    reference_codes = _code_list(value.get("reference_codes", []), FINDING_CODES,
                                 f"{label}.reference_codes")
    allowed_codes = (
        _code_list(value["allowed_codes"], FINDING_CODES, f"{label}.allowed_codes")
        if "allowed_codes" in value else None
    )
    expected_codes = set(required_codes + reference_codes)
    if allowed_codes is not None and not expected_codes <= set(allowed_codes):
        raise EvalManifestError(
            f"{label}.allowed_codes must contain all required_codes and reference_codes"
        )
    max_tool_calls = value.get("max_tool_calls")
    if "max_tool_calls" in value and (
        type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 4
    ):
        raise EvalManifestError(f"{label}.max_tool_calls must be an integer from 0 through 4")

    refusal = None
    if "expected_refusal" in value:
        refusal = _nonempty_string(value["expected_refusal"], f"{label}.expected_refusal", 64)
        if refusal not in LIMITATION_CODES:
            raise EvalManifestError(f"{label}.expected_refusal is not a registered limitation")
    abstention_value = value.get("expected_abstention", False)
    if type(abstention_value) is not bool:
        raise EvalManifestError(f"{label}.expected_abstention must be a boolean")
    expected_abstention = abstention_value
    if refusal is not None and expected_abstention:
        raise EvalManifestError(f"{label} cannot require both refusal and abstention")
    if refusal is not None and (required_codes or reference_codes):
        raise EvalManifestError(f"{label} refusal cannot also require findings")
    if expected_abstention and (required_codes or reference_codes or required_tools):
        raise EvalManifestError(f"{label} abstention cannot also require findings or tools")
    if (refusal is not None or expected_abstention) and allowed_codes is not None:
        raise EvalManifestError(f"{label} refusal or abstention cannot define allowed_codes")
    positive_expectation = bool(required_codes or required_tools or reference_codes)
    if refusal is None and not expected_abstention and not positive_expectation:
        raise EvalManifestError(f"{label} has no evaluable expectation")

    persona = _optional_label(value, "persona", label)
    pair_id = _optional_label(value, "pair_id", label)
    if (persona is None) != (pair_id is None):
        raise EvalManifestError(f"{label} persona and pair_id must appear together")
    return EvalCase(
        case_id,
        question,
        required_codes,
        required_tools,
        reference_codes,
        refusal,
        expected_abstention,
        persona,
        pair_id,
        allowed_codes,
        max_tool_calls,
    )


def _validate_pairs(cases: tuple[EvalCase, ...]) -> None:
    pairs: dict[str, list[EvalCase]] = {}
    for case in cases:
        if case.pair_id is not None:
            pairs.setdefault(case.pair_id, []).append(case)
    for pair_id, members in pairs.items():
        if len(members) != 2:
            raise EvalManifestError(f"pair_id {pair_id!r} must identify exactly two cases")
        if members[0].persona == members[1].persona:
            raise EvalManifestError(f"pair_id {pair_id!r} must use distinct personas")
def _validate_provenance(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvalManifestError("provenance must be an object")
    if set(value) != _PROVENANCE_FIELDS:
        raise EvalManifestError("provenance fields must match the registered provenance schema")
    expected = {
        "source": "assistant-authored",
        "nvidia_data_downloaded": False,
        "nvidia_dataset_derived": False,
        "evaluation_use": "reserved-transfer-evaluation",
        "freeze_policy": "freeze-before-transfer-evaluation",
    }
    if value != expected or any(type(value[key]) is not bool for key in (
        "nvidia_data_downloaded", "nvidia_dataset_derived"
    )):
        raise EvalManifestError("provenance does not match the documented human-authored source")
    return dict(value)


def _code_list(value: Any, registry: frozenset[str] | set[str], label: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > MAX_LIST_ITEMS:
        raise EvalManifestError(f"{label} must be a list of at most {MAX_LIST_ITEMS} entries")
    if any(type(item) is not str or item not in registry for item in value):
        raise EvalManifestError(f"{label} contains an unregistered code or tool")
    if len(set(value)) != len(value):
        raise EvalManifestError(f"{label} entries must be unique")
    return tuple(value)


def _optional_label(value: dict[str, Any], key: str, label: str) -> str | None:
    if key not in value:
        return None
    result = _nonempty_string(value[key], f"{label}.{key}", 64)
    if not _SAFE_ID.fullmatch(result):
        raise EvalManifestError(f"{label}.{key} must be a safe lowercase identifier")
    return result


def _nonempty_string(value: Any, label: str, max_length: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > max_length:
        raise EvalManifestError(f"{label} must be a non-empty string within its length bound")
    return value


def _check_fields(
    value: dict[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise EvalManifestError(f"{label} missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise EvalManifestError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
