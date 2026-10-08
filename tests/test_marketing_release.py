"""AC1 requirement coverage must fail closed if references or boundaries drift."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "marketing_release_check", ROOT / "scripts/check_marketing_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_release_covers_every_authoritative_requirement():
    result = checker().validate(ROOT)
    assert result["requirements_checked"] == 19
    assert result["deliverables"] == 7
    assert result["inference_source_drift"] == []


@pytest.mark.parametrize("mutation", ["missing", "oversized", "reference", "escape"])
def test_release_rejects_incomplete_or_invalid_requirement_index(tmp_path, monkeypatch, mutation):
    module = checker()
    # Dependency docs checker is independently covered; isolate this index validator.
    monkeypatch.setattr(module, "validate_docs", lambda root: {"inference_source_drift": []})
    index = json.loads((ROOT / module.INDEX).read_text())
    if mutation == "missing":
        index["requirement_evidence"].pop()
    elif mutation == "oversized":
        index["requirement_evidence"][0]["text"] = "x" * 2001
    elif mutation == "reference":
        index["requirement_evidence"][0]["references"] = ["does-not-exist.py"]
    else:
        index["requirement_evidence"][0]["references"] = ["../outside.py"]
    for entry in index["requirement_evidence"]:
        for ref in entry["references"]:
            if ref == "does-not-exist.py" or ".." in Path(ref).parts:
                continue
            target = tmp_path / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    target = tmp_path / module.INDEX
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(index))
    with pytest.raises(ValueError):
        module.validate(tmp_path)
