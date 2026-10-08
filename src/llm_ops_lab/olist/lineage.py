"""Fixed, version-bound dependency lineage for the Olist publication pipeline.

This module describes dependencies from the checked-in pipeline. It does not
query runtime metadata or infer which individual nodes ran successfully.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

MAX_RUN_HISTORY = 5
MAX_ID_CHARS = 128
LINEAGE_MAP_VERSION = "olist-dependency-map.v1"
_TRANSFORM_FILES = ("schema.sql", "transform.sql", "pipeline.py", "validation.py")

# Journal step names are written by olist/pipeline.py. These map the persisted
# run cursor to the nearest stage boundary; they are not per-node receipts.
STAGES = (
    ("source_validation", "Validate source contract", "validate"),
    ("bronze_freeze", "Freeze validated source files", "freeze_sources"),
    ("core_copy_reconcile", "Copy and reconcile versioned core data", "load"),
    ("mart_build", "Build and reconcile marts", "mart"),
    ("publication", "Atomically publish the version", "publish"),
)

_STEP_STAGE = {
    "validate": "source_validation",
    "freeze_sources": "bronze_freeze",
    "load": "core_copy_reconcile",
    "analyze": "core_copy_reconcile",
    "mart": "mart_build",
    "publish": "publication",
    "complete": "publication",
}
_STAGE_IDS = tuple(stage_id for stage_id, _, _ in STAGES)
_READ_ONLY_CHECKS = [
    "Read the run receipt and validation report for the exact run_id and error_code.",
    "Verify the selected version_id and transform_sha256 against the immutable version evidence.",
    "Inspect the frozen Bronze manifest and compare each recorded SHA-256 with the retained files.",
    "Inspect recorded core row-count and mart reconciliation in the run receipt "
    "and quality report.",
    "Read publication and run history in one repeatable-read snapshot before deciding recovery.",
]


def _safe_id(value: Any) -> str | None:
    if type(value) is str and value and len(value) <= MAX_ID_CHARS:
        return value
    return None


def _stage_index(step: Any) -> int | None:
    stage = _STEP_STAGE.get(step) if type(step) is str else None
    return _STAGE_IDS.index(stage) if stage else None


def _quality_passed(version_evidence: dict[str, Any]) -> bool:
    return (
        version_evidence.get("quality_status") == "passed"
        and type(version_evidence.get("failed_check_count")) is int
        and version_evidence.get("failed_check_count") == 0
    )


def _source_fingerprint() -> tuple[dict[str, str], str]:
    """Match pipeline.transform_hash() and retain the individual source digests."""
    package = Path(__file__).parent
    source_hashes: dict[str, str] = {}
    transform_digest = hashlib.sha256()
    for name in _TRANSFORM_FILES:
        content = (package / name).read_bytes()
        source_hashes[name] = hashlib.sha256(content).hexdigest()
        transform_digest.update(name.encode())
        transform_digest.update(content)
    return source_hashes, transform_digest.hexdigest()


def _graph(version_id: str, transform_sha256: str,
           source_hashes: dict[str, str]) -> dict[str, Any]:
    nodes = [
        {
            "id": stage_id,
            "label": label,
            "version_id": version_id,
            "transform_sha256": transform_sha256,
            "runtime_state": "not_observed",
        }
        for stage_id, label, _ in STAGES
    ]
    edges = [
        {"from": _STAGE_IDS[index], "to": _STAGE_IDS[index + 1],
         "version_id": version_id}
        for index in range(len(_STAGE_IDS) - 1)
    ]
    return {"version_id": version_id, "transform_sha256": transform_sha256,
            "lineage_map_version": LINEAGE_MAP_VERSION,
            "source_hashes": source_hashes,
            "nodes": nodes, "edges": edges}


def _empty_result(reason: str, *, graph: dict[str, Any] | None = None,
                  active_id: str | None = None, provenance: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    return {"status": "abstained", "reason": reason,
            "graph": graph, "lineage_provenance": provenance,
            "incident_observations": [],
            "candidate_affected_downstream_nodes": [],
            "active_published_version_id": active_id,
            "active_published_version_available": None,
            "distinct_published_version_available": None,
            "last_good_preserved": None,
            "diagnosis": {"root_cause": "unknown"},
            "recommended_read_only_checks": list(_READ_ONLY_CHECKS)}


def pipeline_context(version_evidence: dict, run_history: list) -> dict:
    """Build a bounded dependency graph and cautious incident context.

    ``version_evidence`` is the version row plus, when available, the current
    publication pointer captured in the same database snapshot. ``run_history``
    is the already bounded newest-first run history (at most five rows).
    """
    if type(version_evidence) is not dict:
        return _empty_result("version_evidence_missing")

    version_id = _safe_id(version_evidence.get("version_id"))
    transform_sha256 = version_evidence.get("transform_sha256")
    if (version_id is None or type(transform_sha256) is not str
            or len(transform_sha256) != 64
            or any(char not in "0123456789abcdef" for char in transform_sha256)):
        return _empty_result("version_identity_missing")

    active_id = _safe_id(version_evidence.get("active_published_version_id"))
    try:
        source_hashes, current_transform_sha256 = _source_fingerprint()
    except OSError:
        return _empty_result("lineage_source_unavailable")
    provenance = {
        "lineage_map_version": LINEAGE_MAP_VERSION,
        "source_hashes": source_hashes,
        "evidence_transform_sha256": transform_sha256,
        "current_transform_sha256": current_transform_sha256,
        "source_hash_match": current_transform_sha256 == transform_sha256,
    }
    graph = _graph(version_id, transform_sha256, source_hashes)
    if current_transform_sha256 != transform_sha256:
        result = _empty_result("lineage_source_hash_mismatch", graph=graph,
                               active_id=active_id, provenance=provenance)
        if active_id == version_id:
            result["active_published_version_available"] = _quality_passed(version_evidence)
        return result
    if active_id is not None and active_id != version_id:
        result = _empty_result("active_version_conflict", graph=graph, active_id=active_id,
                               provenance=provenance)
        result["diagnosis"]["uncertainty"] = "selected version and active publication differ"
        return result

    if type(run_history) is not list or len(run_history) > MAX_RUN_HISTORY:
        return _empty_result("run_history_bounds_exceeded", graph=graph,
                             active_id=active_id, provenance=provenance)

    if any(type(row) is not dict for row in run_history):
        return _empty_result("run_history_invalid", graph=graph, active_id=active_id,
                             provenance=provenance)

    history = run_history
    # Keep statuses separate: abandoned or running runs are never relabeled failed.
    failed = next((row for row in history if row.get("status") == "failed"), None)
    if failed is None:
        active_available = _quality_passed(version_evidence) if active_id == version_id else None
        return {"status": "completed", "dataset_version": version_id, "graph": graph,
                "lineage_provenance": provenance,
                "incident_observations": [], "candidate_affected_downstream_nodes": [],
                "active_published_version_id": active_id,
                "active_published_version_available": active_available,
                "distinct_published_version_available": None,
                "last_good_preserved": None,
                "diagnosis": {"root_cause": "unknown",
                              "uncertainty": "no failed run is present in the bounded history"},
                "recommended_read_only_checks": list(_READ_ONLY_CHECKS)}

    stage_index = _stage_index(failed.get("step"))
    stage_id = _STAGE_IDS[stage_index] if stage_index is not None else None
    downstream = list(_STAGE_IDS[stage_index + 1:]) if stage_index is not None else list(_STAGE_IDS)
    raw_run_version = failed.get("version_id")
    run_version = _safe_id(raw_run_version)
    valid_run_version = raw_run_version is None or run_version is not None
    quality_passed = _quality_passed(version_evidence)
    active_available = quality_passed if active_id == version_id else None
    distinct_available = None
    if active_available is True and valid_run_version and raw_run_version is not None:
        distinct_available = run_version != active_id
    observation = {
        "run_id": _safe_id(failed.get("run_id")),
        "status": "failed",
        "failed_version_id": run_version,
        "active_published_version_id": active_id,
        "step": failed.get("step") if type(failed.get("step")) is str else None,
        "mapped_stage": stage_id,
        "error_code": failed.get("error_code") if type(failed.get("error_code")) is str else None,
        "observation_only": True,
    }
    if observation["error_code"] == "QueryCanceled":
        observation["error_note"] = "QueryCanceled is observed; its underlying cause is unproven."

    return {
        "status": "completed",
        "dataset_version": version_id,
        "graph": graph,
        "lineage_provenance": provenance,
        "incident_observations": [observation],
        "candidate_affected_downstream_nodes": downstream,
        "active_published_version_id": active_id,
        "active_published_version_available": active_available,
        "distinct_published_version_available": distinct_available,
        # A single current snapshot cannot prove that publication was unchanged
        # across the earlier run; retain the legacy field as unknown.
        "last_good_preserved": None,
        "diagnosis": {
            "root_cause": "unknown",
            "uncertainty": (
                "The run cursor identifies a stage boundary only; it does not prove per-node "
                "completion or the cause of the error."
            ),
        },
        "recommended_read_only_checks": list(_READ_ONLY_CHECKS),
    }
