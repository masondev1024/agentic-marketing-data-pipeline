#!/usr/bin/env python3
"""Fail-closed semantic checks for a completed Olist investigation batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm_ops_lab.olist.eval_manifest import EvalManifestError, parse_eval_manifest
from llm_ops_lab.olist.evaluation import score_case
from llm_ops_lab.olist.intent_plan import DISPOSITIONS, SUPPORTED_INTENTS

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# These checks are intentionally pinned to the frozen Olist acceptance snapshot.
_COUNT_FINDING_INTENTS = {
    "missing_origin_leads": ("origin", "lead_count", "lead_count", "origin", None),
    "seller_sample_coverage_unknown": (
        "coverage",
        "unlinked_contract_count",
        "contract_count",
        None,
        "unknown, not zero",
    ),
}


class GateInputError(ValueError):
    """An input artifact is absent, malformed, or does not match the contract."""


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
        return json.loads(raw, object_pairs_hook=_unique_object), raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise GateInputError from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_contracts(contracts: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    if type(contracts) is not dict:
        raise GateInputError
    source = contracts.get("source_manifest")
    cases = contracts.get("cases")
    if type(source) is not dict or type(cases) is not dict or not cases:
        raise GateInputError
    source_sha = source.get("sha256")
    if type(source_sha) is not str or not _SHA256.fullmatch(source_sha):
        raise GateInputError
    normalized: dict[str, dict[str, Any]] = {}
    for case_id, contract in cases.items():
        if (
            type(case_id) is not str
            or not _CASE_ID.fullmatch(case_id)
            or type(contract) is not dict
            or type(contract.get("expected_disposition")) is not str
            or contract.get("expected_disposition") not in DISPOSITIONS
        ):
            raise GateInputError
        intents = contract.get("exact_supported_intents")
        if (
            type(intents) is not list
            or any(type(intent) is not str or intent not in SUPPORTED_INTENTS for intent in intents)
            or len(intents) != len(set(intents))
        ):
            raise GateInputError
        allowed = contract.get("allowed_dispositions")
        if "allowed_dispositions" in contract and (
            type(allowed) is not list
            or not allowed
            or any(type(item) is not str or item not in DISPOSITIONS for item in allowed)
            or len(allowed) != len(set(allowed))
            or contract["expected_disposition"] not in allowed
        ):
            raise GateInputError
        normalized[case_id] = contract
    return source_sha, normalized


def _check_count_evidence(value: dict[str, Any]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    findings = value.get("findings", [])
    evidence = value.get("evidence", [])
    if type(findings) is not list or type(evidence) is not list:
        return {"finding_evidence_structure": False}

    for finding in findings:
        if type(finding) is not dict:
            return {"finding_evidence_structure": False}
        code = finding.get("code")
        if type(code) is not str:
            return {"finding_evidence_structure": False}
        if code not in _COUNT_FINDING_INTENTS:
            if code == "recent_pipeline_failures":
                summary = finding.get("summary")
                checks["no_unproven_causality"] = (
                    type(summary) is str and "does not establish root cause" in summary
                )
            continue

        intent, numerator_key, denominator_key, origin_key, required_text = _COUNT_FINDING_INTENTS[
            code
        ]
        matching = [
            entry
            for entry in evidence
            if type(entry) is dict and entry.get("tool") == "channel_funnel"
        ]
        finding_counts = finding.get("observed_counts")
        summary = finding.get("summary")
        checks[f"{code}_evidence_shape"] = (
            len(matching) == 1
            and type(matching[0].get("data")) is list
            and type(finding_counts) is dict
            and type(summary) is str
        )
        if not checks[f"{code}_evidence_shape"]:
            continue
        data = matching[0]["data"]
        try:
            if intent == "origin":
                numerator = sum(row[numerator_key] for row in data if row[origin_key] is None)
            else:
                numerator = sum(row[numerator_key] for row in data)
            denominator = sum(row[denominator_key] for row in data)
            numeric_types = all(
                type(row[numerator_key]) is int and row[numerator_key] >= 0 for row in data
            )
            numeric_types = numeric_types and all(
                type(row[denominator_key]) is int and row[denominator_key] >= 0 for row in data
            )
        except (KeyError, TypeError):
            checks[f"{code}_evidence_shape"] = False
            continue
        checks[f"{code}_counts_are_nonnegative_integers"] = numeric_types
        checks[f"{code}_numerator_denominator"] = (
            finding_counts.get("numerator"),
            finding_counts.get("denominator"),
        ) == (numerator, denominator)
        expected_counts = (60, 8000) if intent == "origin" else (462, 842)
        checks[f"{code}_pinned_snapshot_counts"] = (numerator, denominator) == expected_counts
        checks[f"{code}_displayed_denominator"] = f"{numerator}/{denominator}" in summary
        if required_text:
            checks[f"{code}_unknown_not_zero"] = required_text in summary
    return checks


def evaluate_batch(batch_path: Path, contracts_path: Path) -> dict[str, Any]:
    summary, _ = _read_json(batch_path / "summary.json")
    _, evaluation_bytes = _read_json(batch_path / "evaluation.json")
    baseline, _ = _read_json(batch_path / "baseline.json")
    contracts_payload, contracts_bytes = _read_json(contracts_path)
    source_sha, contracts = _validate_contracts(contracts_payload)
    if type(summary) is not dict or type(baseline) is not dict:
        raise GateInputError
    if baseline.get("status") != "completed" or type(baseline.get("dataset_version")) is not str:
        raise GateInputError
    try:
        manifest = parse_eval_manifest(evaluation_bytes)
    except EvalManifestError:
        raise GateInputError from None
    evaluation_sha = hashlib.sha256(evaluation_bytes).hexdigest()
    if evaluation_sha != source_sha:
        raise GateInputError
    summary_sha = summary.get("evaluation_sha256")
    if (
        type(summary_sha) is not str
        or not _SHA256.fullmatch(summary_sha)
        or summary_sha != evaluation_sha
    ):
        raise GateInputError
    manifest_cases = {case.case_id: case for case in manifest.cases}
    if set(manifest_cases) != set(contracts):
        raise GateInputError
    summary_cases = summary.get("cases")
    if type(summary_cases) is not list or not summary_cases:
        raise GateInputError
    case_ids = [case.get("case_id") for case in summary_cases if type(case) is dict]
    if len(case_ids) != len(summary_cases) or any(type(case_id) is not str for case_id in case_ids):
        raise GateInputError
    if (
        len(case_ids) != len(set(case_ids))
        or any(not _CASE_ID.fullmatch(case_id) for case_id in case_ids)
        or set(case_ids) != set(manifest_cases)
    ):
        raise GateInputError
    if type(summary.get("total")) is not int or summary["total"] != len(summary_cases):
        raise GateInputError
    if type(summary.get("passed")) is not int or not 0 <= summary["passed"] <= summary["total"]:
        raise GateInputError
    if (
        summary["passed"] != sum(case.get("passed") is True for case in summary_cases)
        or summary.get("dataset_version") != baseline["dataset_version"]
    ):
        raise GateInputError

    rows = []
    for summary_case in summary_cases:
        case_id = summary_case["case_id"]
        result_path = batch_path / f"{case_id}.json"
        result, _ = _read_json(result_path)
        if type(result) is not dict:
            raise GateInputError
        try:
            structural = score_case(asdict(manifest_cases[case_id]), result, baseline)
        except Exception:
            raise GateInputError from None
        contract = contracts[case_id]
        plan = result.get("request_plan")
        if type(plan) is not dict:
            plan = {}
        intents = plan.get("intents")
        intent_shape = (
            type(intents) is list
            and all(type(item) is str for item in intents)
            and len(intents) == len(set(intents))
        )
        expected_intents = contract["exact_supported_intents"]
        allowed_dispositions = contract.get(
            "allowed_dispositions", [contract["expected_disposition"]]
        )
        checks: dict[str, bool] = {
            "structural_passed": structural["passed"] is True,
            "stored_summary_passed_matches": summary_case.get("passed") is structural["passed"],
            "stored_summary_checks_match": summary_case.get("checks") == structural["checks"],
            "stored_summary_required_codes_match": (
                summary_case.get("required_codes") == structural["required_codes"]
            ),
            "request_plan_present": type(result.get("request_plan")) is dict,
            "disposition": plan.get("disposition") in allowed_dispositions,
            "intent_list_well_formed": intent_shape,
            "exact_intents": intent_shape and set(intents) == set(expected_intents),
        }
        checks.update(_check_count_evidence(result))
        rows.append({"case_id": case_id, "passed": all(checks.values()), "checks": checks})

    semantic_passed = sum(row["passed"] for row in rows)
    if summary["passed"] != sum(row["checks"]["structural_passed"] for row in rows):
        for row in rows:
            row["checks"]["stored_summary_total_matches_recomputed"] = False
            row["passed"] = False
        semantic_passed = 0
    else:
        for row in rows:
            row["checks"]["stored_summary_total_matches_recomputed"] = True
    return {
        "schema_version": "olist.semantic-gate.v1",
        "batch": str(batch_path),
        "total": len(rows),
        "passed": semantic_passed,
        "source_manifest_sha256": source_sha,
        "semantic_contract_sha256": hashlib.sha256(contracts_bytes).hexdigest(),
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, type=Path, help="completed batch directory")
    parser.add_argument(
        "--contracts", required=True, type=Path, help="frozen semantic contract JSON"
    )
    parser.add_argument("--output", type=Path, help="optional JSON review report path")
    args = parser.parse_args(argv)
    try:
        report = evaluate_batch(args.batch, args.contracts)
    except GateInputError:
        report = {
            "schema_version": "olist.semantic-gate.v1",
            "passed": 0,
            "total": 0,
            "error": "invalid_or_mismatched_batch_or_contract_input",
        }
        status = 1
    else:
        status = int(report["passed"] != report["total"])
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        except OSError:
            print(json.dumps({"error": "could_not_write_output", "exit_code": 1}))
            return 1
    print(json.dumps({"passed": report["passed"], "total": report["total"], "exit_code": status}))
    if args.output is None:
        print(serialized, end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
