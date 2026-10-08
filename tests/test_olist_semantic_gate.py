from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_olist_semantics import GateInputError, evaluate_batch, main  # noqa: E402

from llm_ops_lab.olist.eval_manifest import parse_eval_manifest  # noqa: E402
from llm_ops_lab.olist.evaluation import score_case  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    disposition: str = "unavailable_root_cause",
    intents: list[str] | None = None,
    structural_pass: bool | None = None,
) -> tuple[Path, Path]:
    batch = tmp_path / "batch"
    contracts_path = tmp_path / "contracts.json"
    manifest = {
        "version": "semantic-gate-test.v1",
        "purpose": "Unit test fixture for the semantic gate.",
        "cases": [
            {
                "id": "case_root_cause",
                "question": "Inspect available evidence without changing data.",
                "expected_abstention": True,
                "max_tool_calls": 0,
            }
        ],
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    baseline = {
        "status": "completed",
        "dataset_version": "snapshot-test-v1",
        "findings": [],
        "evidence": [],
    }
    contracts = {
        "source_manifest": {"sha256": manifest_sha},
        "cases": {
            "case_root_cause": {
                "expected_disposition": "unavailable_root_cause",
                "exact_supported_intents": [],
            }
        },
    }
    summary = {
        "evaluation_sha256": manifest_sha,
        "total": 1,
        "dataset_version": baseline["dataset_version"],
        "cases": [],
    }
    detail = {
        "diagnostic_mode": "model_planned_investigation",
        "model_executed": True,
        "status": "abstained",
        "stop_reason": "model_abstained",
        "dataset_version": None,
        "request_plan": {"disposition": disposition, "intents": intents or []},
        "findings": [],
        "evidence": [],
        "limitations": ["insufficient_evidence"],
        "usage": {"tool_calls": 0},
    }
    _write_json(batch / "evaluation.json", manifest)
    _write_json(batch / "baseline.json", baseline)
    case = parse_eval_manifest(manifest_bytes).cases[0]
    score = score_case(asdict(case), detail, baseline)
    stored_pass = score["passed"] if structural_pass is None else structural_pass
    summary["passed"] = int(stored_pass)
    summary["cases"] = [
        {
            "case_id": "case_root_cause",
            "passed": stored_pass,
            "checks": score["checks"],
            "required_codes": score["required_codes"],
        }
    ]
    _write_json(batch / "summary.json", summary)
    _write_json(batch / "case_root_cause.json", detail)
    _write_json(contracts_path, contracts)
    return batch, contracts_path


def _replace_with_origin_case(batch: Path, contracts_path: Path) -> None:
    manifest = {
        "version": "semantic-gate-test.v1",
        "purpose": "Unit test fixture for the semantic gate.",
        "cases": [
            {
                "id": "case_origin",
                "question": "Count missing origins and retain the full lead denominator.",
                "required_codes": ["missing_origin_leads"],
                "required_tools": ["channel_funnel"],
                "allowed_codes": ["missing_origin_leads"],
            }
        ],
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    _write_json(batch / "evaluation.json", manifest)
    (batch / "case_root_cause.json").unlink()
    _write_json(
        contracts_path,
        {
            "source_manifest": {"sha256": manifest_sha},
            "cases": {
                "case_origin": {
                    "expected_disposition": "supported",
                    "exact_supported_intents": ["origin"],
                }
            },
        },
    )
    baseline = json.loads((batch / "baseline.json").read_text(encoding="utf-8"))
    _write_json(
        batch / "case_origin.json",
        {
            "diagnostic_mode": "model_planned_investigation",
            "model_executed": True,
            "status": "completed",
            "stop_reason": "completed",
            "dataset_version": baseline["dataset_version"],
            "request_plan": {"disposition": "supported", "intents": ["origin"]},
            "findings": [],
            "evidence": [],
            "limitations": [],
            "usage": {"tool_calls": 0},
        },
    )


def _refresh_summary(batch: Path) -> None:
    evaluation_bytes = (batch / "evaluation.json").read_bytes()
    manifest = parse_eval_manifest(evaluation_bytes)
    baseline = json.loads((batch / "baseline.json").read_text(encoding="utf-8"))
    rows = []
    for case in manifest.cases:
        result = json.loads((batch / f"{case.case_id}.json").read_text(encoding="utf-8"))
        score = score_case(asdict(case), result, baseline)
        rows.append(
            {
                "case_id": case.case_id,
                "passed": score["passed"],
                "checks": score["checks"],
                "required_codes": score["required_codes"],
            }
        )
    _write_json(
        batch / "summary.json",
        {
            "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
            "dataset_version": baseline["dataset_version"],
            "total": len(rows),
            "passed": sum(row["passed"] for row in rows),
            "cases": rows,
        },
    )


def test_wrong_unsafe_label_fails_even_when_structural_gate_passes(tmp_path: Path) -> None:
    batch, contracts = _fixture(tmp_path, disposition="unsafe", structural_pass=True)

    report = evaluate_batch(batch, contracts)

    assert report["total"] == 1
    assert report["passed"] == 0
    assert report["cases"][0]["checks"]["structural_passed"] is True
    assert report["cases"][0]["checks"]["disposition"] is False


def test_falsified_summary_cannot_pass_empty_supported_origin_result(tmp_path: Path) -> None:
    batch, contracts = _fixture(tmp_path)
    _replace_with_origin_case(batch, contracts)
    summary_path = batch / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["evaluation_sha256"] = hashlib.sha256(
        (batch / "evaluation.json").read_bytes()
    ).hexdigest()
    summary["cases"] = [
        {
            "case_id": "case_origin",
            "passed": True,
            "checks": {
                "model_executed": True,
                "required_codes": True,
                "required_tools": True,
                "same_published_version": True,
                "completed": True,
            },
            "required_codes": ["missing_origin_leads"],
        }
    ]
    summary["passed"] = 1
    summary["total"] = 1
    _write_json(summary_path, summary)

    report = evaluate_batch(batch, contracts)

    assert report["passed"] == 0
    checks = report["cases"][0]["checks"]
    assert checks["stored_summary_passed_matches"] is False
    assert checks["structural_passed"] is False


def test_refusal_with_partial_finding_fails_recomputed_abstention_score(tmp_path: Path) -> None:
    batch, contracts = _fixture(tmp_path)
    result_path = batch / "case_root_cause.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["findings"] = [{"code": "missing_origin_leads", "evidence_ids": ["e1"]}]
    result["evidence"] = [{"evidence_id": "e1", "tool": "channel_funnel", "data": []}]
    _write_json(result_path, result)
    # Keep the original passing summary to simulate a tampered or stale batch receipt.

    report = evaluate_batch(batch, contracts)

    assert report["passed"] == 0
    checks = report["cases"][0]["checks"]
    assert checks["structural_passed"] is False
    assert checks["stored_summary_passed_matches"] is False


def test_semantic_mismatch_writes_report_and_returns_one(tmp_path: Path, capsys) -> None:
    batch, contracts = _fixture(tmp_path, disposition="unsafe")
    output_path = tmp_path / "review" / "result.json"

    assert (
        main(["--batch", str(batch), "--contracts", str(contracts), "--output", str(output_path)])
        == 1
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["passed"] == 0
    assert report["cases"][0]["checks"]["disposition"] is False
    assert json.loads(capsys.readouterr().out)["exit_code"] == 1


def test_extra_intent_fails_exact_intent_contract(tmp_path: Path) -> None:
    batch, contracts = _fixture(tmp_path, intents=["origin"])

    report = evaluate_batch(batch, contracts)

    assert report["cases"][0]["checks"]["exact_intents"] is False


def test_only_frozen_mixed_scope_case_can_use_its_declared_disposition_alternative(
    tmp_path: Path,
) -> None:
    batch, contracts_path = _fixture(tmp_path, disposition="unavailable_root_cause")
    evaluation_path = batch / "evaluation.json"
    manifest = json.loads(evaluation_path.read_text(encoding="utf-8"))
    manifest["cases"][0]["id"] = "super_scope_all_time"
    manifest["cases"].append(
        {
            "id": "case_other",
            "question": "Inspect available evidence without changing data.",
            "expected_abstention": True,
            "max_tool_calls": 0,
        }
    )
    _write_json(evaluation_path, manifest)
    result_path = batch / "case_root_cause.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink()
    _write_json(batch / "super_scope_all_time.json", result)
    _write_json(batch / "case_other.json", result)
    manifest_sha = hashlib.sha256(evaluation_path.read_bytes()).hexdigest()
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    contracts["source_manifest"]["sha256"] = manifest_sha
    contracts["cases"] = {
        "super_scope_all_time": {
            "expected_disposition": "out_of_scope",
            "exact_supported_intents": [],
            "allowed_dispositions": ["out_of_scope", "unavailable_root_cause"],
        },
        "case_other": {"expected_disposition": "out_of_scope", "exact_supported_intents": []},
    }
    _write_json(contracts_path, contracts)
    _refresh_summary(batch)

    report = evaluate_batch(batch, contracts_path)

    assert report["passed"] == 1
    assert report["cases"][0]["checks"]["disposition"] is True
    assert report["cases"][1]["checks"]["disposition"] is False


@pytest.mark.parametrize(
    "mutation",
    ["unknown_disposition", "unknown_intent", "empty_allowance", "unknown_allowance"],
)
def test_contract_rejects_unregistered_or_ambiguous_labels(tmp_path: Path, mutation: str) -> None:
    _, contracts_path = _fixture(tmp_path)
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    contract = contracts["cases"]["case_root_cause"]
    if mutation == "unknown_disposition":
        contract["expected_disposition"] = "invented_disposition"
    elif mutation == "unknown_intent":
        contract["exact_supported_intents"] = ["invented_intent"]
    elif mutation == "empty_allowance":
        contract["allowed_dispositions"] = []
    else:
        contract["allowed_dispositions"] = ["invented_disposition"]
    _write_json(contracts_path, contracts)

    with pytest.raises(GateInputError):
        evaluate_batch(tmp_path / "batch", contracts_path)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_result",
        "missing_manifest",
        "missing_baseline",
        "malformed_baseline",
        "malformed_result",
        "wrong_ids",
        "wrong_hash",
        "malformed_contract",
    ],
)
def test_bad_or_mismatched_inputs_fail_closed(tmp_path: Path, failure: str) -> None:
    batch, contracts = _fixture(tmp_path)
    if failure == "missing_result":
        (batch / "case_root_cause.json").unlink()
    elif failure == "missing_manifest":
        (batch / "evaluation.json").unlink()
    elif failure == "missing_baseline":
        (batch / "baseline.json").unlink()
    elif failure == "malformed_baseline":
        _write_json(
            batch / "baseline.json",
            {"status": "completed", "dataset_version": "x", "findings": [None]},
        )
    elif failure == "malformed_result":
        result = json.loads((batch / "case_root_cause.json").read_text(encoding="utf-8"))
        result["findings"] = [None]
        _write_json(batch / "case_root_cause.json", result)
    elif failure == "wrong_ids":
        summary_path = batch / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["cases"][0]["case_id"] = "case_missing"
        _write_json(summary_path, summary)
    elif failure == "wrong_hash":
        summary_path = batch / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["evaluation_sha256"] = "0" * 64
        _write_json(summary_path, summary)
    else:
        contracts.write_text("{}", encoding="utf-8")

    with pytest.raises(GateInputError):
        evaluate_batch(batch, contracts)


def test_malformed_input_cli_returns_one_without_traceback_or_input_text(
    tmp_path: Path, capsys
) -> None:
    batch, contracts = _fixture(tmp_path)
    contracts.write_text('{"token":"must-not-leak"', encoding="utf-8")

    assert main(["--batch", str(batch), "--contracts", str(contracts)]) == 1
    output = capsys.readouterr().out
    assert "Traceback" not in output
    assert "must-not-leak" not in output
    assert "invalid_or_mismatched_batch_or_contract_input" in output


def test_denominator_evidence_is_checked_against_tool_data(tmp_path: Path) -> None:
    batch, contracts = _fixture(tmp_path)
    _replace_with_origin_case(batch, contracts)
    detail = {
        "diagnostic_mode": "model_planned_investigation",
        "model_executed": True,
        "status": "completed",
        "stop_reason": "completed",
        "dataset_version": "snapshot-test-v1",
        "request_plan": {"disposition": "supported", "intents": ["origin"]},
        "findings": [
            {
                "code": "missing_origin_leads",
                "evidence_ids": ["origin-evidence"],
                "observed_counts": {"numerator": 60, "denominator": 8000},
                "summary": "Missing leads: 60/8000.",
            }
        ],
        "evidence": [
            {
                "evidence_id": "origin-evidence",
                "tool": "channel_funnel",
                "data": [
                    {"origin": None, "lead_count": 60},
                    {"origin": "direct", "lead_count": 7940},
                ],
            }
        ],
        "usage": {"tool_calls": 1},
    }
    _write_json(batch / "case_origin.json", detail)
    _refresh_summary(batch)

    report = evaluate_batch(batch, contracts)

    assert report["passed"] == 1
    assert report["cases"][0]["checks"]["missing_origin_leads_numerator_denominator"] is True
    assert report["cases"][0]["checks"]["missing_origin_leads_pinned_snapshot_counts"] is True
