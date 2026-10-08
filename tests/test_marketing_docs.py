"""AC6: evidence-backed Korean docs and their literal runnable walkthrough."""

import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
checker = runpy.run_path(str(ROOT / "scripts/check_marketing_docs.py"))


def test_documented_results_match_reviewed_artifacts():
    result = checker["validate"](ROOT)
    assert result["status"] == "documentation_verified"
    assert result["agent_designation"] == "experimental"
    assert result["inference_rerun"] is False


@pytest.mark.parametrize(
    "mutation", ["missing_artifact", "altered_score", "false_promotion", "unreviewed_source"]
)
def test_documentation_receipt_rejects_missing_or_overclaimed_evidence(tmp_path, mutation):
    for name in ("docs", "evidence", "data", "src", "scripts", "tests"):
        shutil.copytree(ROOT / name, tmp_path / name)
    for name in ("README.md", "AGENTS.md", "pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, tmp_path / name)
    receipt = tmp_path / "evidence/marketing-ac6-docs.json"
    content = json.loads(receipt.read_text())
    if mutation == "missing_artifact":
        (tmp_path / "docs/marketing/aws-migration.md").unlink()
    elif mutation == "altered_score":
        content["model_summary"]["correct"] = 3
    elif mutation == "false_promotion":
        content["agent_designation"] = "production"
    else:
        with (tmp_path / "src/llm_ops_lab/marketing/agent.py").open("a") as stream:
            stream.write("\n# unreviewed change\n")
    receipt.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        checker["validate"](tmp_path)


def test_readme_walkthrough_runs_as_written_in_new_processes(tmp_path):
    text = (ROOT / "docs/marketing/README.md").read_text()
    block = text.split("<!-- marketing-walkthrough:start -->")[1].split(
        "<!-- marketing-walkthrough:end -->"
    )[0].split("```bash\n")[1].split("```")[0]
    environment = os.environ.copy()
    environment["TMPDIR"] = str(tmp_path)
    run = subprocess.run(["bash", "-c", block], cwd=ROOT, env=environment,
                         capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    # uv may report an editable-package rebuild on stderr in a clean checkout.
    # The shell uses set -eu; exit status and the validated artifacts below are
    # the success contract, not whether the package manager emitted progress.
    output = Path(run.stdout.strip())
    assert output.is_relative_to(tmp_path)
    report = (output / "report.md").read_text()
    assert "alpha | 2026-09-20 | 15000 | 3 | 60000 | 7000 | 53000" in report
    assert "alpha | 2026-09-21 | 10000 | 2 | 20000 | 0 | 20000" in report
    pending = json.loads((output / "start.json").read_text())
    reviewed = json.loads((output / "review.json").read_text())
    assert pending["pending_review"]
    assert pending["proposal"]["kind"] == "investigation_only"
    assert reviewed["decision"] == "reject"
    for key in ("binding", "session", "result"):
        assert reviewed[key] == pending[key]
    assert reviewed["result"]["conclusion"] == "observed_decline"
    assert reviewed["session"]["usage"]["tool_calls"] == 3
