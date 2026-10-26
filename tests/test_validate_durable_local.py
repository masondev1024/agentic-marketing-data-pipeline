"""The offline evidence command must validate the real local execution path."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_durable_local.py"
SPEC = importlib.util.spec_from_file_location("validate_durable_local", SCRIPT)
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)


def test_local_receipt_joins_http_ledger_provider_and_budget():
    report = validation.run_validation()
    assert report["status"] == "passed"
    assert report["summary"]["http_attempts"] == 7
    assert report["summary"]["http_failures"] == 4
    assert report["summary"]["logical_jobs"] == 4
    assert report["summary"]["provider_invocations"] == 4
    assert report["summary"]["pending_attempts"] == 2
    assert report["summary"]["reserved_calls"] == 4
    assert all(sample["elapsed_seconds"] > 0 for sample in report["samples"])
    assert report["cleanup"] == {"workers_reaped": True, "supervisor_closed": True}
