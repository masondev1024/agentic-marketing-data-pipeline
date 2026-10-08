"""AC1: run locked release checks in an external copy and preserve exact output.

The required output directory must be outside this workspace. No inference worker
is launched: accepted real inference is checked by source hashes and observations.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
from verify_marketing_ac2 import ITEMS, ROOT, fingerprints  # noqa: E402

BRIEF = "work/marketing-aws-implementation-brief.md"
COMMANDS = (
    ["uv", "run", "--locked", "python", "scripts/check_marketing_release.py"],
    ["uv", "run", "--locked", "pytest"],
    ["uv", "run", "--locked", "pytest", "-q"],
    ["uv", "run", "--locked", "ruff", "check", "."],
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("output must be outside the source workspace")
    output.mkdir(parents=True, exist_ok=False)
    before = fingerprints()
    brief_before = (ROOT / BRIEF).read_bytes()
    receipt = {"schema_version": "marketing-release.v1", "operator": "assistant",
               "started_at_utc": datetime.now(UTC).isoformat(), "commands": [],
               "source_sha256": before, "inference_rerun": False,
               "accepted_inference": "evidence/marketing-ac4-experiment.json"}
    with tempfile.TemporaryDirectory(prefix="marketing-release-") as temporary:
        copy = Path(temporary) / "repo"
        copy.mkdir()
        for item in (*ITEMS, BRIEF):
            source, target = ROOT / item, copy / item
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        env = os.environ.copy()
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
            env.pop(key, None)
        env.update(PYTHONDONTWRITEBYTECODE="1", UV_CACHE_DIR=str(Path(temporary) / "cache"))
        # Separate expected environment bootstrap output from product/check stderr.
        commands = [["uv", "sync", "--locked"], *COMMANDS]
        for number, command in enumerate(commands):
            print("validating: " + " ".join(command), flush=True)
            run = subprocess.run(command, cwd=copy, env=env, capture_output=True,
                                 text=True, timeout=600, check=False)
            (output / f"{number}.stdout").write_text(run.stdout)
            (output / f"{number}.stderr").write_text(run.stderr)
            entry = {"command": " ".join(command), "exit_code": run.returncode,
                     "stdout": run.stdout, "stderr": run.stderr,
                     "stderr_policy": "environment_bootstrap" if number == 0 else "must_be_empty"}
            receipt["commands"].append(entry)
            print(run.stdout[-1500:], flush=True)
            if run.returncode or (number > 0 and run.stderr):
                print(run.stderr, file=sys.stderr)
                break
    receipt["source_unchanged"] = (before == fingerprints() and
                                   brief_before == (ROOT / BRIEF).read_bytes())
    receipt["passed"] = (receipt["source_unchanged"] and
                         len(receipt["commands"]) == len(COMMANDS) + 1 and
                         all(c["exit_code"] == 0 and
                             (c["stderr_policy"] == "environment_bootstrap" or not c["stderr"])
                             for c in receipt["commands"]))
    receipt["finished_at_utc"] = datetime.now(UTC).isoformat()
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": receipt["passed"], "receipt": str(output / "receipt.json")}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
