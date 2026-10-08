"""Tests for the static, version-bound Olist dependency lineage."""

from llm_ops_lab.olist.lineage import _source_fingerprint, pipeline_context


def evidence(**overrides):
    _, transform_sha256 = _source_fingerprint()
    value = {
        "version_id": "published-v1",
        "dataset_id": "olist-sample",
        "transform_sha256": transform_sha256,
        "active_published_version_id": "published-v1",
        "quality_status": "passed",
        "failed_check_count": 0,
    }
    value.update(overrides)
    return value


def run(status="failed", step="mart", error_code="QueryCanceled", **overrides):
    value = {
        "run_id": "run-failed-1",
        "version_id": "attempted-v2",
        "status": status,
        "step": step,
        "error_code": error_code,
    }
    value.update(overrides)
    return value


def test_failed_mart_run_maps_downstream_without_claiming_root_cause():
    context = pipeline_context(evidence(), [run()])

    assert context["status"] == "completed"
    assert context["dataset_version"] == "published-v1"
    assert context["graph"]["transform_sha256"] == evidence()["transform_sha256"]
    assert context["lineage_provenance"]["source_hash_match"] is True
    assert [node["id"] for node in context["graph"]["nodes"]] == [
        "source_validation", "bronze_freeze", "core_copy_reconcile", "mart_build",
        "publication",
    ]
    assert all(node["version_id"] == "published-v1" for node in context["graph"]["nodes"])
    assert all(node["runtime_state"] == "not_observed" for node in context["graph"]["nodes"])
    assert context["candidate_affected_downstream_nodes"] == ["publication"]
    assert context["active_published_version_available"] is True
    assert context["distinct_published_version_available"] is True
    assert context["last_good_preserved"] is None
    assert context["diagnosis"]["root_cause"] == "unknown"
    assert "unproven" in context["incident_observations"][0]["error_note"]


def test_known_step_failure_lists_only_later_candidate_nodes():
    failed = run(step="freeze_sources", error_code="bronze_hash_mismatch")
    context = pipeline_context(evidence(), [failed])

    assert context["incident_observations"][0]["mapped_stage"] == "bronze_freeze"
    assert context["candidate_affected_downstream_nodes"] == [
        "core_copy_reconcile", "mart_build", "publication",
    ]


def test_missing_or_unrecognized_step_abstains_from_localizing_downstream():
    for step in (None, "unrecognized_step"):
        context = pipeline_context(evidence(), [run(step=step, error_code="RuntimeError")])

        assert context["incident_observations"][0]["mapped_stage"] is None
        assert context["candidate_affected_downstream_nodes"] == [
            "source_validation", "bronze_freeze", "core_copy_reconcile", "mart_build",
            "publication",
        ]
        assert context["diagnosis"]["root_cause"] == "unknown"


def test_selected_version_must_match_active_publication_pointer():
    context = pipeline_context(
        evidence(active_published_version_id="different-version"), [run()]
    )

    assert context["status"] == "abstained"
    assert context["reason"] == "active_version_conflict"
    assert context["incident_observations"] == []
    assert context["last_good_preserved"] is None


def test_running_and_abandoned_are_not_failed_incidents():
    context = pipeline_context(evidence(), [
        run(status="running", step="load", error_code=None),
        run(status="abandoned", step="mart", error_code="publisher_interrupted"),
    ])

    assert context["incident_observations"] == []
    assert context["candidate_affected_downstream_nodes"] == []
    assert context["last_good_preserved"] is None


def test_only_latest_five_input_rows_are_accepted_and_version_identity_is_required():
    too_many = pipeline_context(evidence(), [run() for _ in range(6)])
    missing_hash = pipeline_context(evidence(transform_sha256=None), [])

    assert too_many["status"] == "abstained"
    assert too_many["reason"] == "run_history_bounds_exceeded"
    assert missing_hash["status"] == "abstained"
    assert missing_hash["reason"] == "version_identity_missing"


def test_failed_same_version_does_not_claim_distinct_last_good_was_preserved():
    context = pipeline_context(evidence(), [run(version_id="published-v1")])

    assert context["active_published_version_id"] == "published-v1"
    assert context["distinct_published_version_available"] is False
    assert context["last_good_preserved"] is None


def test_missing_failed_version_cannot_establish_distinct_publication_or_preservation():
    context = pipeline_context(evidence(), [run(version_id=None)])

    assert context["active_published_version_available"] is True
    assert context["distinct_published_version_available"] is None
    assert context["last_good_preserved"] is None


def test_old_transform_evidence_abstains_from_applying_current_stage_map():
    context = pipeline_context(evidence(transform_sha256="b" * 64), [run()])

    assert context["status"] == "abstained"
    assert context["reason"] == "lineage_source_hash_mismatch"
    assert context["lineage_provenance"]["source_hash_match"] is False
    assert context["incident_observations"] == []
    assert context["candidate_affected_downstream_nodes"] == []
