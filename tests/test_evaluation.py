import json
import subprocess
from pathlib import Path

import pytest

from llm_ops_lab import evaluation

ROOT = Path(__file__).resolve().parents[1]


def case(case_id="retry", source="retry.md"):
    return {
        "id": case_id, "question": "일시적인 API 오류의 재시도 간격과 최대 시도 횟수는?",
        "expected_source": source, "must_include": ["backoff와 jitter; 최대 5회"],
        "must_not_claim": ["간격은 5회"],
    }


def setup_batch(tmp_path, cases=None):
    dataset = tmp_path / "set.jsonl"
    dataset.write_text("\n".join(json.dumps(row) for row in (cases or [case()])))
    corpus = tmp_path / "original"
    corpus.mkdir()
    (corpus / "retry.md").write_bytes((ROOT / "data/runbooks/retry.md").read_bytes())
    return evaluation.parse_args([
        "--dataset", str(dataset), "--corpus-dir", str(corpus), "--search-only",
        "--output-dir", str(tmp_path / "results"), "--operator", "assistant",
    ])


@pytest.mark.parametrize("rows", [
    [case(), case()], [dict(case(), id="../escape")], [dict(case(), question=" ")],
    [dict(case(), expected_source="../retry.md")], [dict(case(), must_include=[])],
    [dict(case(), must_not_claim="bad")], [dict(case(), extra="unknown")], [None],
])
def test_dataset_contract_rejected_before_execution(tmp_path, monkeypatch, rows):
    args = setup_batch(tmp_path, rows)
    monkeypatch.setattr(evaluation.subprocess, "run", lambda *a, **k: pytest.fail("executed"))
    with pytest.raises(ValueError):
        evaluation.run_batch(args)
    assert not args.output_dir.exists()


def test_missing_expected_document_is_a_dataset_error(tmp_path):
    args = setup_batch(tmp_path, [case(source="missing.md")])
    with pytest.raises(ValueError, match="absent"):
        evaluation.run_batch(args)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("raw", [b"", b"not-json", b"\xff", b" " * (256 * 1024 + 1)])
def test_invalid_dataset_bytes_are_rejected(tmp_path, raw):
    path = tmp_path / "set.jsonl"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        evaluation.load_cases(path)


def test_snapshot_survives_original_edit_and_runs_keep_independent_records(tmp_path, monkeypatch):
    args = setup_batch(tmp_path, [case(), case("retry-again")])
    real_run = evaluation.subprocess.run

    def edit_original_then_run(*command, **kwargs):
        (args.corpus_dir / "retry.md").write_text("# unrelated\nno matching terms")
        return real_run(*command, **kwargs)

    monkeypatch.setattr(evaluation.subprocess, "run", edit_original_then_run)
    first = evaluation.run_batch(args)
    summary = json.loads((first / "summary.json").read_text())
    assert summary["metrics"]["retrieval_pass"] == 2
    assert summary["metrics"]["errors"] == 0
    for row in summary["cases"]:
        record = json.loads((first / row["run_file"]).read_text())
        assert record["operator"] == "assistant"
        assert record["model_executed"] is False
        assert record["retrieval"]["selected_source"] == "corpus/retry.md"
        assert row["answer_quality"] == "not_evaluated"
    before = (first / "summary.json").read_bytes()
    second = evaluation.run_batch(args)
    assert first != second
    assert (first / "summary.json").read_bytes() == before


def test_timeout_is_recorded_and_next_case_still_runs(tmp_path, monkeypatch):
    args = setup_batch(tmp_path, [case(), case("retry-again")])
    real_run = evaluation.subprocess.run
    calls = 0

    def timeout_once(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(evaluation.subprocess, "run", timeout_once)
    batch = evaluation.run_batch(args)
    summary = json.loads((batch / "summary.json").read_text())
    assert calls == 2
    assert summary["status"] == "completed_with_errors"
    assert summary["metrics"]["errors"] == 1
    assert summary["metrics"]["total"] == 2
    assert summary["metrics"]["retrieval_pass"] == 1
    assert "TimeoutExpired" in summary["cases"][0]["error"]
    assert (batch / "cases/retry/result.json").is_file()


def test_bad_semantics_never_automatically_pass_answer_quality():
    record = {
        "retrieval": {"selected_source": "corpus/retry.md"},
        "status": "generated", "model_executed": True,
        "output": "재시도 간격은 5회입니다.",
    }
    assessed = evaluation.assess_record(case(), record, False)
    assert assessed["retrieval_pass"] is True
    assert assessed["behavior_pass"] is True
    assert assessed["answer_quality"] == "unreviewed"
    negative = evaluation.assess_record(case(source=None), record, False)
    assert negative["retrieval_pass"] is False
    assert negative["behavior_pass"] is False


@pytest.mark.parametrize("source_name", ["cli.py", "query_support.py"])
def test_code_drift_invalidates_case_instead_of_silent_comparison(
    tmp_path, monkeypatch, source_name,
):
    args = setup_batch(tmp_path)
    real_run = evaluation.subprocess.run

    def tamper_record(command, **kwargs):
        process = real_run(command, **kwargs)
        folder = Path(command[command.index("--output-dir") + 1])
        path = next(folder.glob("*.json"))
        record = json.loads(path.read_text())
        record["source_sha256"][source_name] = "changed"
        path.write_text(json.dumps(record))
        return process

    monkeypatch.setattr(evaluation.subprocess, "run", tamper_record)
    batch = evaluation.run_batch(args)
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["metrics"]["errors"] == 1
    assert "code changed" in summary["cases"][0]["error"]


def test_shipped_development_set_contract():
    _, cases = evaluation.load_cases(ROOT / "data/eval/development-v1.jsonl")
    assert len(cases) == 20
    assert sum(row["expected_source"] is None for row in cases) == 5


def test_error_exit_code(tmp_path, monkeypatch):
    args = setup_batch(tmp_path)
    monkeypatch.setattr(evaluation, "parse_args", lambda _: args)
    monkeypatch.setattr(
        evaluation.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1),
    )
    assert evaluation.main([]) == 1


@pytest.mark.parametrize("variant,version", [("grounded-v2", "v2"), ("grounded-v3", "v3")])
def test_variant_is_forwarded_to_cli_and_recorded_in_manifest(tmp_path, variant, version):
    args = setup_batch(tmp_path)
    args.prompt_variant = variant
    batch = evaluation.run_batch(args)
    manifest = json.loads((batch / "manifest.json").read_text())
    summary = json.loads((batch / "summary.json").read_text())
    row = summary["cases"][0]
    record = json.loads((batch / row["run_file"]).read_text())
    assert manifest["prompt_variant"] == record["prompt_variant"] == variant
    assert record["prompt_template_version"] == f"document.{version}"
    assert row["retrieval_pass"] is True
    assert summary["metrics"]["errors"] == 0


def test_unknown_variant_is_rejected_before_batch_execution():
    with pytest.raises(SystemExit):
        evaluation.parse_args(["--prompt-variant", "typo"])


def test_model_selection_is_forwarded_without_claiming_search_executed_model(tmp_path):
    from llm_ops_lab.cli import MODEL_SPECS

    args = setup_batch(tmp_path)
    args.model = "qwen3-4b"
    batch = evaluation.run_batch(args)
    manifest = json.loads((batch / "manifest.json").read_text())
    summary = json.loads((batch / "summary.json").read_text())
    record = json.loads((batch / summary["cases"][0]["run_file"]).read_text())
    assert manifest["model_id"] == MODEL_SPECS["qwen3-4b"]["id"]
    assert manifest["model_revision"] == MODEL_SPECS["qwen3-4b"]["revision"]
    assert record["model_profile"] == "qwen3-4b"
    assert record["model_executed"] is False
    assert record["model_id"] is None
    assert record["model_revision"] is None


def test_wrong_executed_model_invalidates_record(tmp_path, monkeypatch):
    args = setup_batch(tmp_path)
    real_run = evaluation.subprocess.run

    def wrong_model(command, **kwargs):
        process = real_run(command, **kwargs)
        folder = Path(command[command.index("--output-dir") + 1])
        path = next(folder.glob("*.json"))
        record = json.loads(path.read_text())
        record.update(model_executed=True, model_id="incorrect/model", model_revision="wrong")
        path.write_text(json.dumps(record))
        return process

    monkeypatch.setattr(evaluation.subprocess, "run", wrong_model)
    batch = evaluation.run_batch(args)
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["metrics"]["errors"] == 1
    assert "Model changed" in summary["cases"][0]["error"]
