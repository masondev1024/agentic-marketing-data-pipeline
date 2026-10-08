import json

import pytest

from llm_ops_lab.cli import parse_args, save_result


@pytest.mark.parametrize("value", ["nan", "inf", "-0.1", "2.1"])
def test_invalid_sampling_settings_fail_before_model_load(value):
    with pytest.raises(SystemExit):
        parse_args(["--prompt", "test", "--temperature", value])


@pytest.mark.parametrize("value", ["0", "-1", "513"])
def test_unbounded_generation_is_rejected(value):
    with pytest.raises(SystemExit):
        parse_args(["--prompt", "test", "--max-tokens", value])


@pytest.mark.parametrize("value", ["", "   ", "x" * 4097])
def test_invalid_prompt_fails_before_model_load(value):
    with pytest.raises(SystemExit):
        parse_args(["--prompt", value])


def test_experiment_evidence_cannot_overwrite_existing_run(tmp_path):
    original = {"run_id": "fixed", "output": "첫 번째 결과"}
    path = save_result(original, tmp_path)
    with pytest.raises(FileExistsError):
        save_result({"run_id": "fixed", "output": "replacement"}, tmp_path)
    assert json.loads(path.read_text()) == original


def test_models_are_pinned_and_default_stays_small():
    import re

    from llm_ops_lab.cli import MODEL_ID, MODEL_REVISION, MODEL_SPECS

    assert parse_args(["--prompt", "test"]).model == "qwen3-0.6b"
    assert MODEL_SPECS["qwen3-0.6b"] == {"id": MODEL_ID, "revision": MODEL_REVISION}
    assert all(re.fullmatch(r"[0-9a-f]{40}", spec["revision"]) for spec in MODEL_SPECS.values())
    with pytest.raises(SystemExit):
        parse_args(["--prompt", "test", "--model", "arbitrary/repo"])
