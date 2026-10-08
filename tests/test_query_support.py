import builtins
import hashlib
import json
from pathlib import Path

import pytest

from llm_ops_lab.cli import main, parse_args, prepare_experiment
from llm_ops_lab.retrieval import search_documents

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data/runbooks"
CASES = [
    json.loads(line)
    for dataset in ("development-v1", "retrieval-challenge-v1", "retrieval-support-v1")
    for line in (ROOT / "data/eval" / f"{dataset}.jsonl").read_text().splitlines()
]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_versioned_development_regressions(case):
    result = search_documents(case["question"], CORPUS, "scoped-v3")
    source = Path(result.document.source).name if result.document else None
    assert source == case["expected_source"], result.metadata


def test_unknown_requirement_cannot_hide_behind_many_matching_words(tmp_path, monkeypatch):
    prompt = "재시도 간격 최대 시도 횟수 API 요청 오류와 환율은?"
    result = search_documents(prompt, CORPUS, "scoped-v3")
    assert result.metadata["candidates"][0]["coverage"] >= 0.5
    assert result.metadata["reason"] == "unsupported_query_terms"
    assert "환율" in result.metadata["candidates"][0]["unsupported_words"]
    real_import = builtins.__import__

    def forbid_model(name, *args, **kwargs):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            pytest.fail("Unsupported words must abstain before model import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_model)
    assert main(["--prompt", prompt, "--corpus-dir", str(CORPUS), "--retriever", "scoped-v3",
                 "--output-dir", str(tmp_path)]) == 0
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["status"] == "abstained"
    assert record["model_executed"] is False
    assert record["model_id"] is None
    assert record["messages"] == []
    assert record["sources"] == []


def test_normalization_does_not_rewrite_generation_input():
    prompt = "우리 팀에서 재시도 횟수를 제한해야 하는 이유는?"
    args = parse_args(["--prompt", prompt, "--corpus-dir", str(CORPUS),
                       "--retriever", "scoped-v3"])
    record = prepare_experiment(args)
    assert record["status"] == "prepared"
    assert record["input"] == prompt
    assert prompt in record["messages"][-1]["content"]
    assert (CORPUS / "retry.md").read_text() in record["messages"][-1]["content"]


def test_support_is_bound_to_selected_document_and_fresh_contract(tmp_path):
    prompt = "재시도 간격 최대 시도 횟수 API 요청과 환율은?"
    for path in CORPUS.glob("*.md"):
        (tmp_path / path.name).write_bytes(path.read_bytes())

    def catalog():
        (tmp_path / "scope.json").write_text(json.dumps({
            "schema_version": "corpus-scope.v1", "evidence_kind": "synthetic_reference",
            "documents": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in tmp_path.glob("*.md")},
        }))

    # A word in a different document must not authorize the winning document.
    (tmp_path / "unrelated.md").write_text("# 환율\n환율")
    catalog()
    result = search_documents(prompt, tmp_path, "scoped-v3")
    assert result.document is None
    assert result.metadata["candidates"][0]["source"].endswith("retry.md")
    # Conversely, this is not a hardcoded blacklist: new reference evidence can support it.
    path = tmp_path / "retry.md"
    path.write_text(path.read_text() + "\n이 예시의 환율은 고정된 테스트 값이다.\n")
    with pytest.raises(ValueError, match="stale"):
        search_documents(prompt, tmp_path, "scoped-v3")
    catalog()
    assert search_documents(prompt, tmp_path, "scoped-v3").document is not None


def test_shared_prefix_is_not_evidence_for_a_new_compound(tmp_path):
    path = tmp_path / "reference.md"
    path.write_text("# API 재시도\n재시도 간격 최대 횟수")
    (tmp_path / "scope.json").write_text(json.dumps({
        "schema_version": "corpus-scope.v1", "evidence_kind": "synthetic_reference",
        "documents": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
    }))
    result = search_documents("API 재시도 간격 최대 횟수와 재시도보험료", tmp_path, "scoped-v3")
    assert result.document is None
    assert "재시도보험료" in result.metadata["candidates"][0]["unsupported_words"]


@pytest.mark.parametrize("extra", ["東京気温", "погода", "café", "🌡️", "\u200d"])
def test_unhandled_characters_cannot_disappear_before_gate(tmp_path, monkeypatch, extra):
    prompt = f"재시도 간격과 {extra}는?"
    real_import = builtins.__import__

    def forbid_model(name, *args, **kwargs):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            pytest.fail("Unsupported characters must abstain before model import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_model)
    assert main(["--prompt", prompt, "--corpus-dir", str(CORPUS), "--retriever", "scoped-v3",
                 "--output-dir", str(tmp_path)]) == 0
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["retrieval"]["reason"] == "unsupported_query_characters"
    assert record["retrieval"]["query_analysis"]["unsupported_characters"]
    assert record["model_executed"] is False
    assert record["messages"] == []


def test_company_policy_words_are_not_generic_framing():
    result = search_documents("재시도 간격과 회사 정책은?", CORPUS, "scoped-v3")
    assert result.document is None
    words = result.metadata["query_analysis"]["content_words"]
    assert "회사" in words
    assert "정책" in words
