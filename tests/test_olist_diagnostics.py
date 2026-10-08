"""Pure tests for deterministic Olist diagnosis assembly and bounded output."""

import json

from llm_ops_lab.olist import diagnostics


def sample_observations():
    return {
        "version_evidence": {
            "version_id": "v1",
            "dataset_id": "dataset-a",
            "contract_version": "olist.raw.v1",
            "transform_sha256": "a" * 64,
            "published_at": "2026-10-06T10:00:00+00:00",
            "quality_status": "passed",
            "check_count": 4,
            "failed_check_count": 0,
            "warning_count": 0,
        },
        "channel_funnel": [
            {"origin": None, "lead_count": 60, "contract_count": 2,
             "linked_contract_count": 1, "unlinked_contract_count": 1},
            {"origin": "organic", "lead_count": 100, "contract_count": 10,
             "linked_contract_count": 6, "unlinked_contract_count": 4},
        ],
        "seller_funnel": [
            {"coverage_state": "linked_sample", "contract_count": 7,
             "contracts_with_observed_order_count": 4, "observed_order_count": 12},
            {"coverage_state": "unlinked_unknown", "contract_count": 5,
             "contracts_with_observed_order_count": 0, "observed_order_count": None},
        ],
        "run_history": [
            {"run_id": "run-1", "version_id": None, "status": "failed",
             "step": "validate", "error_code": "source_validation_failed",
             "started_at": "2026-10-06T11:00:00+00:00", "finished_at": None},
            {"run_id": "run-2", "version_id": "v1", "status": "published",
             "step": "complete", "error_code": None,
             "started_at": "2026-10-06T10:00:00+00:00", "finished_at": None},
        ],
        "definitions": diagnostics.METRIC_DEFINITIONS,
    }


def test_build_diagnosis_is_deterministic_and_all_findings_reference_evidence():
    first = diagnostics.build_diagnosis("v1", sample_observations())
    second = diagnostics.build_diagnosis("v1", sample_observations())

    assert first == second
    assert first["status"] == "completed"
    assert first["diagnostic_mode"] == "deterministic"
    assert first["model_executed"] is False
    known_ids = {item["evidence_id"] for item in first["evidence"]}
    assert len(known_ids) == len(first["evidence"])
    assert all(ref in known_ids for finding in first["findings"]
               for ref in finding["evidence_ids"])
    assert all(item["dataset_version"] == "v1" for item in first["evidence"])


def test_missing_origin_and_unlinked_sellers_are_explicit_unknown_coverage():
    result = diagnostics.build_diagnosis("v1", sample_observations())
    by_code = {finding["code"]: finding for finding in result["findings"]}

    assert "missing_origin_leads" in by_code
    assert "60 leads" in by_code["missing_origin_leads"]["summary"]
    assert "seller_sample_coverage_unknown" in by_code
    assert "unknown, not zero" in by_code["seller_sample_coverage_unknown"]["summary"]


def test_recent_pipeline_failure_is_separate_from_last_good_published_data():
    result = diagnostics.build_diagnosis("v1", sample_observations())
    failure = next(item for item in result["findings"]
                   if item["code"] == "recent_pipeline_failures")

    assert result["status"] == "completed"
    assert "does not invalidate the published last-good version" in failure["summary"]
    assert len(failure["evidence_ids"]) == 1


def test_missing_current_snapshot_abstains_without_claims():
    empty = diagnostics.build_diagnosis(None)
    missing_version_evidence = diagnostics.build_diagnosis("v1", {
        "channel_funnel": sample_observations()["channel_funnel"],
    })

    for result in (empty, missing_version_evidence):
        assert result["status"] == "abstained"
        assert result["model_executed"] is False
        assert result["evidence"] == []
        assert result["findings"] == []
        assert result["limitations"]


def test_missing_required_observation_or_version_mismatch_abstains():
    observations = sample_observations()
    observations.pop("seller_funnel")
    missing = diagnostics.build_diagnosis("v1", observations)
    mismatched = sample_observations()
    mismatched["version_evidence"]["version_id"] = "v2"
    wrong_version = diagnostics.build_diagnosis("v1", mismatched)

    assert missing["status"] == "abstained"
    assert missing["limitations"][-1] == "required_observation_missing"
    assert missing["findings"] == []
    assert wrong_version["status"] == "abstained"
    assert wrong_version["limitations"][-1] == "version_mismatch"
    assert wrong_version["evidence"] == []


def test_quality_gate_fails_closed_for_warning_status_or_failed_checks():
    warning_report = sample_observations()
    warning_report["version_evidence"]["quality_status"] = "warning"
    failed_check_report = sample_observations()
    failed_check_report["version_evidence"]["failed_check_count"] = 1

    for observations in (warning_report, failed_check_report):
        result = diagnostics.build_diagnosis("v1", observations)
        assert result["status"] == "abstained"
        assert result["limitations"][-1] == "published_quality_gate_failed"
        assert [item["code"] for item in result["findings"]] == [
            "published_quality_gate_failed"
        ]
        known_ids = {item["evidence_id"] for item in result["evidence"]}
        assert all(ref in known_ids for finding in result["findings"]
                   for ref in finding["evidence_ids"])


def test_failed_quality_check_is_cited_as_its_own_evidence():
    observations = sample_observations()
    observations["version_evidence"]["failed_check_count"] = 2
    observations["version_evidence"]["warning_count"] = 1
    result = diagnostics.build_diagnosis("v1", observations)
    findings = {item["code"]: item for item in result["findings"]}
    version_evidence = next(item for item in result["evidence"]
                            if item["tool"] == "version_evidence")

    assert result["status"] == "abstained"
    assert findings["published_quality_gate_failed"]["evidence_ids"] == [
        version_evidence["evidence_id"]
    ]


def test_warning_status_count_is_reported_when_quality_status_passes():
    observations = sample_observations()
    observations["version_evidence"]["warning_count"] = 1
    result = diagnostics.build_diagnosis("v1", observations)
    finding = next(item for item in result["findings"]
                   if item["code"] == "published_quality_warnings")

    assert result["status"] == "completed"
    assert "warning-status checks" in finding["summary"]


def test_response_budget_fails_closed_instead_of_truncating_evidence(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_RESPONSE_BYTES", 512)
    result = diagnostics.build_diagnosis("v1", sample_observations())

    assert result["status"] == "abstained"
    assert result["evidence"] == []
    assert result["findings"] == []
    assert result["limitations"] == ["response_limit"]
    assert len(json.dumps(result).encode()) <= diagnostics.MAX_RESPONSE_BYTES


def test_channel_observation_limits_abstain_without_truncating():
    too_many = sample_observations()
    too_many["channel_funnel"] = [
        {"origin": f"origin-{index}", "lead_count": 1, "contract_count": 0,
         "linked_contract_count": 0, "unlinked_contract_count": 0}
        for index in range(65)
    ]
    too_long = sample_observations()
    too_long["channel_funnel"][1]["origin"] = "x" * (diagnostics.MAX_ORIGIN_CHARS + 1)

    for observations in (too_many, too_long):
        result = diagnostics.build_diagnosis("v1", observations)
        assert result["status"] == "abstained"
        assert result["evidence"] == []
        assert result["limitations"][-1] == "channel_evidence_bounds_exceeded"


def test_pipeline_statuses_have_distinct_findings():
    observations = sample_observations()
    observations["run_history"] = [
        {"run_id": "run-running", "status": "running"},
        {"run_id": "run-abandoned", "status": "abandoned"},
    ]
    result = diagnostics.build_diagnosis("v1", observations)
    codes = {finding["code"] for finding in result["findings"]}

    assert "pipeline_runs_in_progress" in codes
    assert "abandoned_pipeline_runs" in codes
    assert "recent_pipeline_failures" not in codes


def test_evidence_identifier_changes_when_payload_changes():
    original = diagnostics.build_diagnosis("v1", sample_observations())
    changed = sample_observations()
    changed["channel_funnel"][0]["lead_count"] += 1
    updated = diagnostics.build_diagnosis("v1", changed)
    original_channel = next(item for item in original["evidence"]
                            if item["tool"] == "channel_funnel")
    updated_channel = next(item for item in updated["evidence"]
                           if item["tool"] == "channel_funnel")

    assert original_channel["evidence_id"] != updated_channel["evidence_id"]
