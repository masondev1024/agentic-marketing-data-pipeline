import builtins
import json
import subprocess
from pathlib import Path

import pytest

from llm_ops_lab import cli, evaluation
from llm_ops_lab.context import load_document
from llm_ops_lab.evidence import (
    answer_messages,
    parse_answer,
    parse_selection,
    run_pipeline,
    snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("# 참고\n\n무한 반복하지 않고 최대 시도 횟수와\n전체 시간 예산을 정한다.\n"
                    "한도를 넘으면 실패를 기록하고 알린다.\n선택하지 않은 내용이다.\n")
    return cli.prepare_experiment(cli.parse_args([
        "--prompt", "재시도 횟수를 제한하는 이유는?", "--context-file", str(path),
        "--answer-mode", "evidence-v1",
    ]))


def writer_output(ids=None):
    return json.dumps({"sentences": [
        {"text": "무한 반복을 방지한다.", "evidence_ids": ids or ["s001"]},
        {"text": "전체 시간 예산을 정한다.", "evidence_ids": ids or ["s001"]},
    ]}, ensure_ascii=False)


def backend(outputs, *, truncated_at=None, fail_at=None):
    calls = []

    def generate(messages, limit, trace):
        calls.append(messages)
        trace.update(model_executed=True, prompt_tokens=10, generation_tokens=4,
                     generation_seconds=0.01, output=outputs[len(calls) - 1])
        if fail_at == len(calls):
            raise RuntimeError("fake runtime failure")
        trace["max_tokens_reached"] = truncated_at == len(calls)
        return trace

    return generate, calls


def test_real_offsets_multiline_spans_and_selected_only_writer(prepared):
    evidence = prepared["evidence"]
    assert len(evidence["catalog"]) == 3
    assert "\n" in evidence["catalog"][0]["text"]
    generate, calls = backend(['{"sentence_ids":["s001"]}', writer_output()])
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "generated"
    assert len(calls) == 2
    assert "선택하지 않은 내용" not in calls[1][1]["content"]
    assert prepared["input"] in calls[1][1]["content"]
    assert result["evidence"]["quote_membership"] == "verified"
    assert result["evidence"]["semantic_entailment"] == "not_checked"
    for quote in result["citations"]:
        assert evidence["source_text"][quote["start"]:quote["end"]] == quote["text"]
    assert [s["status"] for s in result["stages"]] == ["validated", "validated"]


@pytest.mark.parametrize("raw", [
    'not json', '[]', '{"sentence_ids":["s999"]}', '{"sentence_ids":["s001","s001"]}',
    '{"sentence_ids":[1]}', '{"sentence_ids":"s001"}',
    '{"sentence_ids":[],"sentence_ids":["s001"]}',
    '{"sentence_ids":["s001"],"extra":true}',
])
def test_invalid_selection_blocks_writer_and_keeps_raw_output(prepared, raw):
    generate, calls = backend([raw])
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "evidence_rejected"
    assert len(calls) == 1
    assert result["stages"][0]["output"] == raw
    assert result["citations"] == []


def test_empty_selection_abstains_without_writer(prepared):
    generate, calls = backend(['{"sentence_ids":[]}'])
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "abstained"
    assert result["model_executed"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("stage", [1, 2])
def test_truncated_valid_json_is_rejected(prepared, stage):
    generate, calls = backend(['{"sentence_ids":["s001"]}', writer_output()], truncated_at=stage)
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "evidence_rejected"
    assert len(calls) == stage
    assert result["citations"] == []


@pytest.mark.parametrize("raw", [
    writer_output(["s002"]), writer_output(["s999"]), writer_output(["s001", "s001"]),
    '{"sentences":[]}',
    '{"sentences":[{"text":"답.","evidence_ids":[]},{"text":"답.","evidence_ids":[]}]}',
])
def test_invalid_answer_never_publishes_unvalidated_text(prepared, raw):
    generate, _ = backend(['{"sentence_ids":["s001"]}', raw])
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "evidence_rejected"
    assert result["output"] != raw
    assert result["answer_sentences"] == result["citations"] == []


@pytest.mark.parametrize("damage", ["text", "span", "quote", "selected_id"])
def test_source_or_catalog_tampering_is_rejected(prepared, damage):
    evidence = prepared["evidence"]
    evidence["selected_ids"] = ["s001"]
    if damage == "text":
        evidence["source_text"] += "changed"
    elif damage == "span":
        evidence["catalog"][0]["start"] += 1
    elif damage == "quote":
        evidence["catalog"][0]["text"] = "조작한 인용"
    else:
        evidence["selected_ids"] = ["s999"]
    with pytest.raises(ValueError):
        parse_answer(writer_output(), evidence)


def test_stage_two_runtime_failure_preserves_stage_one_and_is_saved(
    prepared, tmp_path, monkeypatch,
):
    generate, _ = backend(['{"sentence_ids":["s001"]}', 'partial'], fail_at=2)
    result = run_pipeline(prepared, generate, 256)
    assert result["status"] == "pipeline_error"
    assert result["stages"][0]["status"] == "validated"
    assert result["stages"][1]["output"] == "partial"
    # Complete CLI contract for a failed generation: persist trace and return failure.
    result.update(prompt_tokens=20, generation_tokens=8, generation_seconds=0.02,
                  mlx_peak_memory_gb=0, max_tokens_reached=False)
    monkeypatch.setattr(cli, "run_experiment", lambda _: result)
    assert cli.main(["--prompt", "test", "--output-dir", str(tmp_path)]) == 1
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["stages"][0]["output"] == '{"sentence_ids":["s001"]}'


def test_preflight_failure_does_not_claim_model_execution(prepared):
    def reject(messages, limit, trace):
        raise ValueError("Input exceeds token budget")

    result = run_pipeline(prepared, reject, 256)
    assert result["status"] == "pipeline_error"
    assert result["model_executed"] is False


def test_quote_presence_does_not_prove_answer_truth(prepared):
    evidence = prepared["evidence"]
    evidence["selected_ids"] = parse_selection('{"sentence_ids":["s001"]}', evidence)
    raw = writer_output().replace("무한 반복을 방지한다.", "오류 발생 자체를 방지한다.")
    # Deliberately false causal claim still has valid ID structure. Manual semantics must catch it.
    sentences, citations = parse_answer(raw, evidence)
    assert sentences and citations
    assert evidence["semantic_entailment"] == "not_checked"


@pytest.mark.parametrize("search_only", [True, False])
def test_retrieval_refusal_or_search_only_never_imports_model(tmp_path, monkeypatch, search_only):
    real_import = builtins.__import__

    def forbid(name, *args, **kwargs):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            pytest.fail("Model imported before retrieval gate")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid)
    args = ["--prompt", "재시도 간격" if search_only else "서울 날씨",
            "--corpus-dir", str(ROOT / "data/runbooks"), "--retriever", "scoped-v3",
            "--answer-mode", "evidence-v1", "--output-dir", str(tmp_path)]
    if search_only:
        args.append("--search-only")
    assert cli.main(args) == 0
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["model_executed"] is False
    assert record["messages"] == []


def test_evaluation_forwards_mode_and_pins_evidence_code(tmp_path):
    args = evaluation.parse_args([
        "--dataset", str(ROOT / "data/eval/development-v1.jsonl"),
        "--corpus-dir", str(ROOT / "data/runbooks"), "--answer-mode", "evidence-v1",
        "--search-only", "--output-dir", str(tmp_path / "runs"),
    ])
    dataset = tmp_path / "one.jsonl"
    dataset.write_text(args.dataset.read_text().splitlines()[0])
    args.dataset = dataset
    batch = evaluation.run_batch(args)
    manifest = json.loads((batch / "manifest.json").read_text())
    summary = json.loads((batch / "summary.json").read_text())
    assert manifest["answer_mode"] == summary["cases"][0]["answer_mode"] == "evidence-v1"
    assert "evidence.py" in manifest["source_sha256"]
    assert summary["metrics"]["errors"] == 0


def test_evidence_mode_requires_source_and_defaults_stay_direct(tmp_path):
    assert cli.parse_args(["--prompt", "test"]).answer_mode == "direct"
    with pytest.raises(SystemExit):
        cli.parse_args(["--prompt", "test", "--answer-mode", "evidence-v1"])
    path = tmp_path / "empty.md"
    path.write_text("# Only a heading\n")
    evidence = snapshot(load_document(path))
    assert evidence["catalog"] == []
    assert len(answer_messages("질문", evidence, "baseline")) == 2


def test_stage_journal_survives_interruption_during_writer(prepared, tmp_path):
    path = tmp_path / "run.stages.jsonl"
    cli.append_stage_event(path, {"event": "start", "run": prepared}, start=True)

    def generate(messages, limit, stage):
        if stage["name"] == "answer":
            raise KeyboardInterrupt("simulated interruption")
        stage.update(output='{"sentence_ids":["s001"]}', model_executed=True,
                     max_tokens_reached=False)
        return stage

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(prepared, generate, 256,
                     checkpoint=lambda stage: cli.append_stage_event(path, {"stage": stage}))
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[1]["stage"]["status"] == "validated"
    assert events[1]["stage"]["output"] == '{"sentence_ids":["s001"]}'
    with pytest.raises(FileExistsError):
        cli.append_stage_event(path, {}, start=True)


@pytest.mark.parametrize("timed_out", [False, True])
def test_evaluation_keeps_failure_trace_and_counts_error(tmp_path, monkeypatch, timed_out):
    dataset = tmp_path / "one.jsonl"
    dataset.write_text((ROOT / "data/eval/development-v1.jsonl").read_text().splitlines()[0])
    args = evaluation.parse_args([
        "--dataset", str(dataset), "--corpus-dir", str(ROOT / "data/runbooks"),
        "--answer-mode", "evidence-v1", "--search-only", "--output-dir", str(tmp_path / "runs"),
    ])
    real_run = evaluation.subprocess.run

    def saved_failure(command, **kwargs):
        # Produce a provenance-valid record without importing a model, then inject the failure.
        real_run(command, **kwargs)
        folder = Path(command[command.index("--output-dir") + 1])
        path = next(folder.glob("*.json"))
        record = json.loads(path.read_text())
        journal = folder / f"{record['run_id']}.stages.jsonl"
        cli.append_stage_event(journal, {"stage": "selection", "status": "validated"}, start=True)
        if timed_out:
            path.unlink()  # A killed process may have only its durable stage journal.
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        record.update(status="pipeline_error", stage_journal=journal.name,
                      stages=[{"name": "selection", "status": "validated"},
                              {"name": "answer", "status": "error"}],
                      pipeline_error={"stage": "answer", "type": "RuntimeError"})
        path.write_text(json.dumps(record))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(evaluation.subprocess, "run", saved_failure)
    batch = evaluation.run_batch(args)
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["metrics"]["errors"] == summary["metrics"]["total"] == 1
    assert summary["metrics"]["behavior_pass"] == summary["metrics"]["retrieval_pass"] == 0
    row = summary["cases"][0]
    assert row["execution_status"] == "error"
    assert (batch / row["stage_journals"][0]).exists()
    if not timed_out:
        assert row["stage_statuses"] == {"selection": "validated", "answer": "error"}
        assert row["inference_status"] == "pipeline_error"
        assert (batch / row["run_file"]).exists()
