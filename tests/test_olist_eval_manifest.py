import json
from pathlib import Path

import pytest

from llm_ops_lab.olist.eval_manifest import (
    EvalManifestError,
    load_eval_manifest,
    parse_eval_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
TRANSFER_MANIFEST = ROOT / "data/olist/investigation-personas-v1.json"
BASELINE_MANIFEST = ROOT / "data/olist/investigation-eval-v1.json"
TRANSFER_RESCORING = ROOT / "data/olist/investigation-personas-v2.json"
BASELINE_RESCORING = ROOT / "data/olist/investigation-eval-v2.json"


def write_manifest(tmp_path: Path, payload: object, name: str = "evaluation-v1.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def valid_case(**overrides):
    case = {
        "id": "coverage_case",
        "question": "판매자 표본 연결 상태를 조사해 주세요.",
        "required_codes": ["seller_sample_coverage_unknown"],
    }
    return case | overrides


def valid_payload(case=None):
    return {
        "version": "olist.test.v1",
        "purpose": "Contract test manifest.",
        "cases": [case or valid_case()],
    }


def test_loader_accepts_original_contract_and_transfer_persona_manifest():
    baseline = load_eval_manifest(BASELINE_MANIFEST)
    assert baseline.version == "olist.investigation-eval.v1"
    assert len(baseline.cases) == 6
    assert baseline.provenance is None
    assert all(case.allowed_codes is None and case.max_tool_calls is None
               for case in baseline.cases)

    transfer = load_eval_manifest(TRANSFER_MANIFEST)
    assert len(transfer.cases) == 12
    assert len({case.pair_id for case in transfer.cases}) == 6
    assert all(case.pair_id and case.persona for case in transfer.cases)
    assert transfer.provenance == {
        "source": "assistant-authored",
        "nvidia_data_downloaded": False,
        "nvidia_dataset_derived": False,
        "evaluation_use": "reserved-transfer-evaluation",
        "freeze_policy": "freeze-before-transfer-evaluation",
    }
    assert "not been model-tested" in transfer.purpose


def test_byte_parser_and_path_loader_validate_identical_manifest_bytes():
    raw = TRANSFER_MANIFEST.read_bytes()
    assert parse_eval_manifest(raw) == load_eval_manifest(TRANSFER_MANIFEST)
    with pytest.raises(EvalManifestError, match="must be bytes"):
        parse_eval_manifest("{}")


@pytest.mark.parametrize(
    "case_id",
    ["../escape", "UPPER", "has space", "", ".", "a" * 65],
)
def test_rejects_unsafe_case_ids(case_id, tmp_path):
    with pytest.raises(EvalManifestError):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(valid_case(id=case_id))))


def test_rejects_duplicate_case_ids_and_unknown_fields(tmp_path):
    duplicate = valid_payload()
    duplicate["cases"].append(valid_case())
    with pytest.raises(EvalManifestError, match="unique"):
        load_eval_manifest(write_manifest(tmp_path, duplicate))

    unknown = valid_payload(valid_case(unreviewed_policy=True))
    with pytest.raises(EvalManifestError, match="unknown fields"):
        load_eval_manifest(write_manifest(tmp_path, unknown))

    unknown_root = valid_payload() | {"extra": "silently changing the contract is unsafe"}
    with pytest.raises(EvalManifestError, match="unknown fields"):
        load_eval_manifest(write_manifest(tmp_path, unknown_root))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_codes", ["unregistered_finding"]),
        ("reference_codes", ["unsupported_metric"]),
        ("required_tools", ["run_sql"]),
        ("expected_refusal", "not_a_limitation"),
    ],
)
def test_rejects_unregistered_finding_tool_and_limitation_codes(field, value, tmp_path):
    case = valid_case(**{field: value})
    with pytest.raises(EvalManifestError):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(case)))


def test_allowed_finding_codes_are_registered_unique_and_cover_required_expectations(tmp_path):
    valid = valid_payload(valid_case(allowed_codes=["seller_sample_coverage_unknown"]))
    assert load_eval_manifest(write_manifest(tmp_path, valid)).cases[0].allowed_codes == (
        "seller_sample_coverage_unknown",
    )

    for values in (
        ["unregistered_finding"],
        ["seller_sample_coverage_unknown", "seller_sample_coverage_unknown"],
        ["missing_origin_leads"],
    ):
        payload = valid_payload(valid_case(allowed_codes=values))
        with pytest.raises(EvalManifestError):
            load_eval_manifest(write_manifest(tmp_path, payload))

    missing_reference = valid_payload(
        valid_case(
            required_codes=[],
            reference_codes=["missing_origin_leads"],
            allowed_codes=[],
        )
    )
    with pytest.raises(EvalManifestError, match="contain all"):
        load_eval_manifest(write_manifest(tmp_path, missing_reference))


@pytest.mark.parametrize("value", [True, False, 1.0, "0", None, -1, 5])
def test_max_tool_calls_requires_exact_integer_in_range(value, tmp_path):
    payload = valid_payload(valid_case(max_tool_calls=value))
    with pytest.raises(EvalManifestError, match="max_tool_calls"):
        load_eval_manifest(write_manifest(tmp_path, payload))


@pytest.mark.parametrize("value", [0, 1, 4])
def test_max_tool_calls_accepts_bounded_integer(value, tmp_path):
    payload = valid_payload(valid_case(max_tool_calls=value))
    assert load_eval_manifest(write_manifest(tmp_path, payload)).cases[0].max_tool_calls == value


@pytest.mark.parametrize(
    "case",
    [
        valid_case(required_codes=[], expected_refusal="unsupported_metric", allowed_codes=[]),
        valid_case(required_codes=[], expected_abstention=True, allowed_codes=[]),
    ],
)
def test_rejects_finding_allowlist_on_refusal_or_abstention(case, tmp_path):
    with pytest.raises(EvalManifestError, match="cannot define allowed_codes"):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(case)))


def test_v2_manifests_are_exact_question_id_copies_with_stricter_rescoring_bounds():
    purpose = (
        "Stricter post-run rescoring of v1 responses, not new held-out evaluation."
    )
    for v1_path, v2_path in (
        (BASELINE_MANIFEST, BASELINE_RESCORING),
        (TRANSFER_MANIFEST, TRANSFER_RESCORING),
    ):
        v1 = load_eval_manifest(v1_path)
        v2 = load_eval_manifest(v2_path)
        assert v2.purpose == purpose
        assert v2.provenance is None
        assert [(case.case_id, case.question) for case in v1.cases] == [
            (case.case_id, case.question) for case in v2.cases
        ]

    smoke = {case.case_id: case for case in load_eval_manifest(BASELINE_RESCORING).cases}
    assert smoke["coverage"].allowed_codes == ("seller_sample_coverage_unknown",)
    assert smoke["origin"].allowed_codes == ("missing_origin_leads",)
    assert set(smoke["pipeline"].allowed_codes or ()) == {
        "recent_pipeline_failures",
        "abandoned_pipeline_runs",
        "pipeline_runs_in_progress",
        "published_quality_warnings",
    }
    assert smoke["injection"].max_tool_calls == 0

    personas = {
        case.case_id: case for case in load_eval_manifest(TRANSFER_RESCORING).cases
    }
    for case in personas.values():
        if case.pair_id == "seller_coverage":
            assert case.allowed_codes == ("seller_sample_coverage_unknown",)
        elif case.pair_id == "missing_origin_denominator":
            assert case.allowed_codes == ("missing_origin_leads",)
        elif case.case_id == "pipeline_platform_owner":
            assert set(case.allowed_codes or ()) == {
                "recent_pipeline_failures",
                "abandoned_pipeline_runs",
                "pipeline_runs_in_progress",
                "published_quality_warnings",
            }
        elif case.pair_id == "injection_boundary":
            assert case.max_tool_calls == 0
        else:
            assert case.allowed_codes is None and case.max_tool_calls is None


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_abstention": 1},
        {"expected_abstention": "true"},
        {"expected_abstention": True, "expected_refusal": "unsupported_metric"},
        {"expected_abstention": True, "required_tools": ["channel_funnel"]},
        {"expected_refusal": "unsupported_metric", "required_codes": ["missing_origin_leads"]},
        {"expected_refusal": "unsupported_metric", "reference_codes": ["missing_origin_leads"]},
        {"required_codes": [], "required_tools": [], "reference_codes": []},
    ],
)
def test_rejects_wrong_boolean_types_and_ambiguous_expectations(changes, tmp_path):
    with pytest.raises(EvalManifestError):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(valid_case(**changes))))


def test_rejects_wrong_container_types_empty_questions_and_oversized_bounds(tmp_path):
    with pytest.raises(EvalManifestError):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(valid_case(question="  "))))
    with pytest.raises(EvalManifestError):
        load_eval_manifest(
            write_manifest(
                tmp_path,
                valid_payload(valid_case(required_codes="missing_origin_leads")),
            )
        )
    with pytest.raises(EvalManifestError):
        load_eval_manifest(write_manifest(tmp_path, valid_payload(valid_case(question="x" * 2049))))
    with pytest.raises(EvalManifestError):
        load_eval_manifest(
            write_manifest(tmp_path, valid_payload() | {"cases": [valid_case()] * 65})
        )


def test_rejects_malformed_or_duplicate_json_keys_and_unsafe_filename(tmp_path):
    duplicate_keys = tmp_path / "duplicate.json"
    duplicate_keys.write_text(
        '{"version":"a","version":"b","purpose":"test","cases":[]}',
        encoding="utf-8",
    )
    with pytest.raises(EvalManifestError, match="duplicate keys"):
        load_eval_manifest(duplicate_keys)

    unsafe_name = write_manifest(tmp_path, valid_payload(), name="UPPER.json")
    with pytest.raises(EvalManifestError, match="filename"):
        load_eval_manifest(unsafe_name)


def test_transfer_pairs_are_two_personas_but_can_express_scope_contrast(tmp_path):
    payload = json.loads(TRANSFER_MANIFEST.read_text(encoding="utf-8"))
    cases = [case for case in payload["cases"] if case["pair_id"] == "pipeline_boundary"]
    assert len(cases) == 2
    assert cases[0].get("expected_abstention") is None
    assert cases[1]["expected_abstention"] is True
    assert len({case["persona"] for case in cases}) == 2

    broken = valid_payload(valid_case(pair_id="one", persona="owner"))
    with pytest.raises(EvalManifestError, match="exactly two"):
        load_eval_manifest(write_manifest(tmp_path, broken))


def test_rejects_untruthful_or_malformed_transfer_provenance(tmp_path):
    payload = valid_payload() | {
        "provenance": {
            "source": "assistant-authored",
            "nvidia_data_downloaded": False,
            "nvidia_dataset_derived": True,
            "evaluation_use": "reserved-transfer-evaluation",
            "freeze_policy": "freeze-before-transfer-evaluation",
        }
    }
    with pytest.raises(EvalManifestError, match="provenance"):
        load_eval_manifest(write_manifest(tmp_path, payload))


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (valid_payload(valid_case(expected_refusal=None)), "expected_refusal"),
        (valid_payload(valid_case(expected_abstention=None)), "expected_abstention"),
        (valid_payload() | {"provenance": None}, "provenance"),
    ],
)
def test_rejects_explicit_null_optional_contract_fields(payload, expected_error, tmp_path):
    with pytest.raises(EvalManifestError, match=expected_error):
        load_eval_manifest(write_manifest(tmp_path, payload))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_json_numbers(constant):
    raw = (
        '{"version":"olist.test.v1","purpose":"test","cases":'
        '[{"id":"case","question":"question",'
        '"required_codes":["missing_origin_leads"]}],"unused":' + constant + "}"
    ).encode()
    with pytest.raises(EvalManifestError, match="valid UTF-8 JSON"):
        parse_eval_manifest(raw)


@pytest.mark.parametrize("code", [
    "origin_missing_count_zero", "seller_sample_unlinked_count_zero",
    "published_quality_no_warnings", "recent_pipeline_no_adverse_status",
])
def test_intent_clean_observations_can_be_evaluated(code, tmp_path):
    payload = valid_payload(valid_case(required_codes=[code], allowed_codes=[code]))
    case = load_eval_manifest(write_manifest(tmp_path, payload)).cases[0]
    assert case.required_codes == case.allowed_codes == (code,)
