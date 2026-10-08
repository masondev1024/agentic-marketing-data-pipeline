import builtins
import json
from pathlib import Path

import pytest

from llm_ops_lab.cli import main, parse_args
from llm_ops_lab.retrieval import MAX_DOCUMENTS, lexical_terms, search_documents

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    json.loads(line)
    for line in (ROOT / "data/eval/retrieval-smoke.jsonl").read_text().splitlines()
]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_known_corpus_smoke_cases(case):
    result = search_documents(case["question"], ROOT / "data/runbooks")
    source = Path(result.document.source).name if result.document else None
    assert source == case["expected_source"], result.metadata


def test_common_particles_and_english_case_are_normalized():
    assert lexical_terms("멱등성이") == lexical_terms("멱등성은")
    assert lexical_terms("API") == lexical_terms("api")


def test_tied_results_abstain_instead_of_arbitrary_selection(tmp_path):
    for name in ["a.md", "b.md"]:
        (tmp_path / name).write_text("# 멱등성\n고유 식별자로 중복 반영 방지", encoding="utf-8")
    result = search_documents("멱등성이 필요한 이유", tmp_path)
    assert result.document is None
    assert result.metadata["reason"] == "ambiguous"


def test_edits_and_deletions_update_corpus_fingerprint_without_stale_results(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("# 멱등성\n중복 반영 방지", encoding="utf-8")
    original = search_documents("멱등성", tmp_path)
    assert original.document is not None
    path.write_text("# 백필\n과거 구간 계산", encoding="utf-8")
    updated = search_documents("멱등성", tmp_path)
    assert updated.document is None
    assert updated.metadata["corpus_sha256"] != original.metadata["corpus_sha256"]
    path.unlink()
    removed = search_documents("멱등성", tmp_path)
    assert removed.metadata["reason"] == "empty_corpus"
    assert removed.metadata["corpus_sha256"] != updated.metadata["corpus_sha256"]


def test_corrupt_document_fails_instead_of_partial_corpus(tmp_path):
    (tmp_path / "valid.md").write_text("# 멱등성", encoding="utf-8")
    (tmp_path / "broken.md").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        search_documents("멱등성", tmp_path)


def test_large_corpus_is_bounded(tmp_path):
    for index in range(MAX_DOCUMENTS + 1):
        (tmp_path / f"{index}.md").write_text("test", encoding="utf-8")
    with pytest.raises(ValueError, match="limit"):
        search_documents("test", tmp_path)


@pytest.mark.parametrize("search_only", [True, False])
def test_search_only_and_abstention_never_import_or_execute_model(
    tmp_path, monkeypatch, search_only,
):
    real_import = builtins.__import__

    def reject_model_import(name, *args, **kwargs):
        if name.split(".")[0] in {"mlx", "mlx_lm", "huggingface_hub"}:
            raise AssertionError("This path must not import the model")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_model_import)
    arguments = [
        "--prompt", "멱등성" if search_only else "서울 날씨",
        "--corpus-dir", str(ROOT / "data/runbooks"), "--output-dir", str(tmp_path),
    ]
    if search_only:
        arguments.append("--search-only")
    assert main(arguments) == 0
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["model_executed"] is False
    assert record["model_id"] is None
    assert record["messages"] == []
    assert record["status"] == ("search_only" if search_only else "abstained")
    assert "generation_tokens" not in record


def test_context_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--prompt", "test", "--context-file", "a.md", "--corpus-dir", "data"])


def test_search_only_requires_corpus():
    with pytest.raises(SystemExit):
        parse_args(["--prompt", "test", "--search-only"])
