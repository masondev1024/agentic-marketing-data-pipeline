"""Offline contract tests for the durable HTTP verification client."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_durable_http.py"
_SPEC = importlib.util.spec_from_file_location("verify_durable_http", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_replay, run_smoke = _MODULE.run_replay, _MODULE.run_smoke


def _fixture_server():
    records = {}
    budget = {"limit": 32, "reserved": 0, "remaining": 32, "scope": "durable_ledger"}

    def handler(request):
        path = request.url.path
        if path == "/health/live":
            return httpx.Response(200, json={"status": "alive"})
        if path == "/health/ready":
            return httpx.Response(200, json={"ready": True})
        if path == "/metrics":
            if request.headers.get("authorization") != "Bearer " + "t" * 32:
                return httpx.Response(401, json={"error": {"code": "authentication_required"}})
            return httpx.Response(200, json={"remote_budget": dict(budget)})
        if path != "/v1/investigations":
            return httpx.Response(404, json={})
        if request.headers.get("authorization") != "Bearer " + "t" * 32:
            return httpx.Response(401, json={"error": {"code": "authentication_required"}})
        key = request.headers["idempotency-key"]
        body = json.loads(request.content)
        if key in records:
            saved_body, saved = records[key]
            if body != saved_body:
                return httpx.Response(409, json={"error": {"code": "idempotency_conflict"}})
            return httpx.Response(200, json=saved | {"request_id": "replay-id", "replayed": True})
        intent = "coverage" if "판매자 계약" in body["question"] else "origin"
        code = "seller_sample_coverage_unknown" if intent == "coverage" else "missing_origin_leads"
        counts = (462, 842) if intent == "coverage" else (60, 8000)
        evidence = [{"evidence_id": "evidence-1"}]
        result = {
            "request_id": "first-id",
            "status": "completed",
            "model_executed": True,
            "dataset_version": body["version_id"],
            "evidence": evidence,
            "findings": [{"code": code, "evidence_ids": ["evidence-1"],
                          "observed_counts": {"numerator": counts[0], "denominator": counts[1]}}],
            "limitations": [],
            "diagnostic_mode": "model_planned_investigation",
            "request_plan": {"disposition": "supported", "intents": [intent]},
            "model_execution_status": "returned",
            "provider": "nvidia_hosted",
            "revision_pinned": False,
            "stop_reason": "completed",
            "pipeline_context": None,
            "metric_policy": {"version": "v1", "sha256": "b" * 64},
            "model": {"id": "fixture", "revision": "r1"},
            "prompt": {"version": "v1", "sha256": "c" * 64},
            "usage": {"generation_attempts": 1, "generations": 1, "tool_calls": 1},
            "investigation_id": "investigation-" + intent,
            "replayed": False,
        }
        records[key] = (body, result)
        budget["reserved"] += 1
        budget["remaining"] -= 1
        return httpx.Response(200, json=result)

    return httpx.MockTransport(handler), budget


def _args(tmp_path, *, replay=None):
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    token.chmod(0o600)
    return SimpleNamespace(
        url="https://service.example",
        token_file=token,
        output_dir=tmp_path,
        proxy_test=False,
        proxy_host=None,
        version_id="a" * 64,
        olist_root=None,
        expected_coverage=(462, 842),
        expected_origin=(60, 8000),
        request_spacing_seconds=0,
        replay_only=replay,
    )


def test_smoke_and_restart_replay_use_same_ids_and_no_extra_budget(tmp_path):
    transport, _ = _fixture_server()
    args = _args(tmp_path)
    with httpx.Client(transport=transport) as client:
        baseline = run_smoke(args, client)
    assert baseline["status"] == "passed"
    assert len(baseline["cases"]) == 2
    assert all(case["replay_verified"] and case["conflict_did_not_reserve"]
               for case in baseline["cases"])
    report_path = tmp_path / "baseline.json"
    report_path.write_text(json.dumps(baseline), encoding="utf-8")
    replay_args = _args(tmp_path, replay=report_path)
    with httpx.Client(transport=transport) as client:
        report = run_replay(replay_args, client)
    assert report["status"] == "passed"
    assert report["durable_budget_before"]["reserved"] == 2
    assert report["durable_budget_after"]["reserved"] == 2
    assert all(case["replay_verified"] for case in report["cases"])
