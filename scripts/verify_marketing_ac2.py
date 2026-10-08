"""Read-only workspace observer: run validation in an external disposable checkout.

No symlinked source/environment, no use of port 8000, no inference worker.
Pass a command after --; default is the focused AC 2 test module.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEMS = ("src", "tests", "data", "docs", "evidence", "scripts", "pyproject.toml", "uv.lock",
         "README.md", "AGENTS.md", ".python-version", ".gitignore")


def fingerprints():
    result = {}
    for item in ITEMS:
        path = ROOT / item
        files = path.rglob("*") if path.is_dir() else [path]
        for file in files:
            if file.is_file() and "__pycache__" not in file.parts:
                result[str(file.relative_to(ROOT))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def main():
    args = sys.argv[1:]
    command = args[1:] if args[:1] == ["--"] else args
    command = command or [
        "uv", "run", "--locked", "pytest", "-q", "tests/test_marketing_pipeline.py",
    ]
    before = fingerprints()
    with tempfile.TemporaryDirectory(prefix="marketing-ac2-") as temporary:
        copy = Path(temporary) / "repo"
        copy.mkdir()
        for item in ITEMS:
            source, destination = ROOT / item, copy / item
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, destination)
        environment = os.environ.copy()
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
            environment.pop(key, None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        # All mutable package/runtime caches stay outside the source workspace.
        environment["UV_CACHE_DIR"] = str(Path(temporary) / "uv-cache")
        print("isolated validation: " + " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=copy, env=environment, check=False)
    if fingerprints() != before:
        raise SystemExit("Source workspace changed during observer validation")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
