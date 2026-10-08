"""Read-only observer of source workspace; real inference in an external temporary copy.

The pinned existing HF cache is read offline; all model/runtime outputs stay in the copy.
An explicit external output path preserves raw evidence for subsequent human/assistant review.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from verify_marketing_ac2 import ITEMS, ROOT, fingerprints


def observed_fingerprints():
    # Sibling AC owns graph/docs files and may change them concurrently.
    watched = ("src/llm_ops_lab/marketing/agent.py",
               "src/llm_ops_lab/marketing/offline_model.py",
               "src/llm_ops_lab/marketing/model_worker.py",
               "src/llm_ops_lab/marketing/diagnostics.py",
               "src/llm_ops_lab/marketing/pipeline.py",
               "scripts/marketing_model_experiment.py",
               "data/marketing/")
    return {k: v for k, v in fingerprints().items() if k.startswith(watched)}


def main():
    output = Path(sys.argv[1]).resolve()
    if output.is_relative_to(ROOT):
        raise SystemExit("observer output must be outside source workspace")
    before = observed_fingerprints()
    with tempfile.TemporaryDirectory(prefix="marketing-inference-") as temporary:
        copy = Path(temporary)
        for item in ITEMS:
            source, target = ROOT / item, copy / item
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, target)
        env = os.environ.copy()
        env.update(PYTHONPATH=str(copy / "src"), PYTHONDONTWRITEBYTECODE="1",
                   HF_HOME=str(ROOT / "work/huggingface"), HF_HUB_OFFLINE="1",
                   TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        result = subprocess.run([str(ROOT / ".venv/bin/python"),
                                 "scripts/marketing_model_experiment.py", "--output", str(output)],
                                cwd=copy, env=env, check=False)
    if before != observed_fingerprints():
        raise SystemExit("source changed during inference observer")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
