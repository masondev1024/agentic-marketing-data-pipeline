"""Exercise the installed command without pytest's import path or the project cwd."""

import os
import subprocess
import sys
from pathlib import Path


def test_installed_cli_imports_from_an_unrelated_directory(tmp_path):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    command = Path(sys.executable).with_name("llm-lab")
    result = subprocess.run(
        [str(command), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--temperature" in result.stdout
