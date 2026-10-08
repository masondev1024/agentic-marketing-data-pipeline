import builtins
import hashlib
import json
from pathlib import Path

import pytest

from llm_ops_lab.cli import main
from llm_ops_lab.evaluation import parse_args, run_batch
from llm_ops_lab.retrieval import analyze_query, search_documents

ROOT = Path(__file__).resolve().parents[1]


def catalog(directory):
    value = {
        "schema_version": "corpus-scope.v1", "evidence_kind": "synthetic_reference",
        "documents": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in directory.glob("*.md")},
    }
    (directory / "scope.json").write_text(json.dumps(value))


def test_exact_value_question_recovers_document_without_modifying_question():
    query = "이 재시도 문서에서 첫 재시도 간격은 정확히 몇 초인가?"
    original = search_documents(query, ROOT / "data/runbooks")
    scoped = search_documents(query, ROOT / "data/runbooks", "scoped-v2")
    assert original.document is None
    assert scoped.document.source.endswith("retry.md")
    assert scoped.metadata["min_coverage"] == original.metadata["min_coverage"]
    assert "초인" in scoped.metadata["query_analysis"]["original_query_terms"]
    assert "초인" not in scoped.metadata["query_terms"]


@pytest.mark.parametrize("query,records", [
    ("우리 회사 백필 작업의 실제 출력 위치와 지난 실행 ID는?", True),
    ("우리 회사에서 백필 실행 ID를 왜 기록해야 하나요?", False),
    ("우리 팀 재시도 횟수를 제한하는 이유는?", False),
    ("어제 우리 회사 API 장애 원인은?", True),
    ("우리 팀 재시도 정책 원리와 지난 실행 ID는?", True),
    ("run_id=abc 백필 출력 위치는?", True),
    ("당사 재시도 설정의 최대 횟수는 정확히 몇 회인가?", True),
    ("우리 팀 재시도 최대 횟수 제한의 정의는 무엇인가?", False),
])
def test_scope_rules_distinguish_design_from_observed_records(query, records):
    assert (analyze_query(query)["required_evidence"] == "operational_record") is records


@pytest.mark.parametrize("damage", ["missing", "stale", "schema", "kind", "fields"])
@pytest.mark.parametrize("variant", ["scoped-v2", "scoped-v3"])
def test_invalid_scope_contract_fails_before_model_import(tmp_path, monkeypatch, damage, variant):
    (tmp_path / "doc.md").write_text("# 백필\n실행 ID와 출력 위치")
    catalog(tmp_path)
    path = tmp_path / "scope.json"
    value = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "stale":
        (tmp_path / "doc.md").write_text("# 변경된 문서")
    else:
        if damage == "schema":
            value["schema_version"] = "v99"
        elif damage == "kind":
            value["evidence_kind"] = "production_record"
        else:
            value["surprise"] = True
        path.write_text(json.dumps(value))
    real_import = builtins.__import__

    def forbid(name, *a, **k):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            pytest.fail("model import before contract validation")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", forbid)
    output = tmp_path / "runs"
    assert main(["--corpus-dir", str(tmp_path), "--retriever", variant,
                 "--prompt", "백필", "--output-dir", str(output)]) == 1
    assert not output.exists()


@pytest.mark.parametrize("variant", ["scoped-v2", "scoped-v3"])
def test_unsupported_scope_never_imports_model(tmp_path, monkeypatch, variant):
    real_import = builtins.__import__

    def forbid(name, *a, **k):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            pytest.fail("scope refusal imported model")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", forbid)
    assert main([
        "--prompt", "우리 회사 백필 작업의 실제 출력 위치와 지난 실행 ID는?",
        "--corpus-dir", str(ROOT / "data/runbooks"), "--retriever", variant,
        "--output-dir", str(tmp_path),
    ]) == 0
    row = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert row["retrieval"]["reason"] == "unsupported_evidence_scope"
    assert row["model_executed"] is False
    assert row["messages"] == []
    assert "생성" not in row["output"]
    assert "generation_tokens" not in row


@pytest.mark.parametrize("variant", ["scoped-v2", "scoped-v3"])
def test_ties_and_renames_use_content_and_contract_not_known_filenames(tmp_path, variant):
    for name in ("custom-a.md", "custom-b.md"):
        (tmp_path / name).write_text("# 재시도\n재시도 간격과 최대 횟수를 정한다.")
    catalog(tmp_path)
    result = search_documents("재시도 간격은 정확히 몇 초인가?", tmp_path, variant)
    assert result.metadata["reason"] == "ambiguous"
    (tmp_path / "custom-b.md").unlink()
    catalog(tmp_path)
    result = search_documents("재시도 간격은 정확히 몇 초인가?", tmp_path, variant)
    assert result.document.source.endswith("custom-a.md")


@pytest.mark.parametrize("variant", ["scoped-v2", "scoped-v3"])
def test_evaluation_snapshots_contract_and_forwards_variant(tmp_path, variant):
    args = parse_args([
        "--corpus-dir", str(ROOT / "data/runbooks"),
        "--dataset", str(ROOT / "data/eval/retrieval-smoke.jsonl"),
        "--retriever", variant, "--search-only", "--output-dir", str(tmp_path),
    ])
    # The old smoke schema is retrieval-only; use one valid full evaluation row.
    dataset = tmp_path / "one.jsonl"
    dataset.write_text((ROOT / "data/eval/development-v1.jsonl").read_text().splitlines()[0])
    args.dataset = dataset
    batch = run_batch(args)
    manifest = json.loads((batch / "manifest.json").read_text())
    snapshot = batch / "corpus/scope.json"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["scope_sha256"]
    summary = json.loads((batch / "summary.json").read_text())
    record = json.loads((batch / summary["cases"][0]["run_file"]).read_text())
    assert record["retrieval"]["retriever_variant"] == variant
    assert record["source_sha256"]["query_support.py"] == (
        manifest["source_sha256"]["query_support.py"]
    )
    assert record["retrieval"]["scope_sha256"] == manifest["scope_sha256"]
    assert summary["metrics"]["errors"] == 0
