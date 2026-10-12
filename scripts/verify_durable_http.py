#!/usr/bin/env python3
"""Verify public durable-investigation HTTP behavior and write redacted receipts.

This client never starts or restarts a service. Use it against a running local,
staging, or cloud endpoint. A token is read only from an owner-only file and is
never written to receipts or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

QUESTIONS = {
    "coverage": "커머스 표본에 연결되지 않은 판매자 계약은 몇 개인가요?",
    "origin": "유입 경로 origin이 누락된 리드 수와 전체 리드 수를 확인해 주세요.",
}
FINDING_CODES = {
    "coverage": "seller_sample_coverage_unknown",
    "origin": "missing_origin_leads",
}


class VerificationError(RuntimeError):
    pass


def _token(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise VerificationError("token file must be owned by this user and mode 0600")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            raise VerificationError("token file exceeds the size limit")
    finally:
        os.close(descriptor)
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    if len(raw) < 32 or b"\r" in raw or b"\n" in raw:
        raise VerificationError("token file must contain one bearer token of at least 32 bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VerificationError("token file must contain UTF-8") from error


def _counts(value: str) -> tuple[int, int]:
    if re.fullmatch(r"[0-9]+/[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("expected counts must use NUMERATOR/DENOMINATOR")
    numerator, denominator = (int(part) for part in value.split("/"))
    if numerator > denominator or denominator <= 0:
        raise argparse.ArgumentTypeError(
            "expected counts must satisfy 0 <= numerator <= denominator"
        )
    return numerator, denominator


def _database_counts(root: Path, version_id: str | None) -> tuple[str, dict[str, tuple[int, int]]]:
    from llm_ops_lab.olist.config import connection_info
    from llm_ops_lab.olist.tool_session import OlistToolSession

    with OlistToolSession(
        connection_info(root, reader=True), version_id=version_id,
        deadline=time.monotonic() + 30,
    ) as session:
        quality = session.initialize()
        if not quality or quality["data"].get("quality_status") != "passed" or (
            quality["data"].get("failed_check_count", 0) != 0
        ):
            raise VerificationError("independent database quality gate failed")
        evidence = session.call("channel_funnel")
        rows = evidence["data"]
        coverage = (
            sum(row["unlinked_contract_count"] for row in rows),
            sum(row["contract_count"] for row in rows),
        )
        origin = (
            sum(row["lead_count"] for row in rows if row["origin"] is None),
            sum(row["lead_count"] for row in rows),
        )
        return session.dataset_version, {"coverage": coverage, "origin": origin}


def _semantic_view(body: dict) -> dict:
    """Fields that must remain byte-for-byte equivalent across terminal replay."""
    keys = (
        "status", "dataset_version", "evidence", "findings", "limitations",
        "diagnostic_mode", "request_plan", "model_execution_status", "provider",
        "revision_pinned", "stop_reason", "pipeline_context", "metric_policy", "model", "prompt",
        "usage", "model_executed",
    )
    return {key: body.get(key) for key in keys}


def _semantic_sha256(body: dict) -> str:
    encoded = json.dumps(
        _semantic_view(body), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _budget(metrics: dict) -> dict:
    value = metrics.get("remote_budget")
    if (type(value) is not dict or value.get("scope") != "durable_ledger"
            or type(value.get("reserved")) is not int
            or type(value.get("limit")) is not int
            or type(value.get("remaining")) is not int):
        raise VerificationError("service did not expose a durable budget ledger")
    if (not 0 <= value["reserved"] <= value["limit"]
            or value["remaining"] != value["limit"] - value["reserved"]):
        raise VerificationError("service exposed an inconsistent durable budget")
    return {key: value[key] for key in ("limit", "reserved", "remaining", "scope")}


def _public_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise VerificationError("URL must include http(s) scheme and hostname")
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        raise VerificationError("URL must be an http(s) endpoint without embedded credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise VerificationError("URL must be an origin without a path, query, or fragment")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def _client_headers(url: str, proxy_test: bool, proxy_host: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    parts = urlsplit(url)
    if proxy_test:
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not proxy_host):
            raise VerificationError(
                "--proxy-test requires a loopback HTTP URL and --proxy-host"
            )
        headers["Host"] = proxy_host
        headers["X-Forwarded-Proto"] = "https"
    return headers


def _get_json(client: httpx.Client, url: str, headers: dict, expected_status: int) -> dict:
    response = client.get(url, headers=headers)
    try:
        body = response.json()
    except ValueError as error:
        raise VerificationError(f"GET {urlsplit(url).path} returned non-JSON") from error
    if response.status_code != expected_status:
        raise VerificationError(
            f"GET {urlsplit(url).path} expected HTTP {expected_status}, got {response.status_code}"
        )
    if type(body) is not dict:
        raise VerificationError("service returned an invalid JSON object")
    return body


def _post(client, base_url, headers, token, version_id, intent, key, body_override=None):
    request_headers = headers | {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    body = body_override or {"question": QUESTIONS[intent], "version_id": version_id}
    response = client.post(
        f"{base_url}/v1/investigations", headers=request_headers, json=body
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise VerificationError("investigation endpoint returned non-JSON") from error
    return response.status_code, payload, body


def _validate_success(
    payload: dict, intent: str, expected: tuple[int, int], version_id: str | None
):
    if payload.get("status") != "completed":
        raise VerificationError(f"{intent} did not complete: {payload.get('stop_reason')}")
    if payload.get("request_plan") != {"disposition": "supported", "intents": [intent]}:
        raise VerificationError(f"planner did not select the expected {intent} intent")
    if version_id is not None and payload.get("dataset_version") != version_id:
        raise VerificationError("response dataset version differs from the pinned request")
    finding = next(
        (item for item in payload.get("findings", [])
         if item.get("code") == FINDING_CODES[intent]), None
    )
    if finding is None:
        raise VerificationError(f"response omitted the {intent} finding")
    counts = finding.get("observed_counts", {})
    actual = (counts.get("numerator"), counts.get("denominator"))
    if actual != expected:
        raise VerificationError(f"{intent} counts differ from independent expectation")
    evidence_ids = {item.get("evidence_id") for item in payload.get("evidence", [])}
    if not set(finding.get("evidence_ids", [])) <= evidence_ids:
        raise VerificationError("finding references missing evidence")
    return finding["observed_counts"]


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def run_smoke(args, client: httpx.Client) -> dict:
    base_url = _public_url(args.url)
    headers = _client_headers(args.url, args.proxy_test, args.proxy_host)
    token = _token(args.token_file)
    anon = _get_json(client, f"{base_url}/health/live", headers, 200)
    if anon.get("status") != "alive":
        raise VerificationError("anonymous live health was not minimal/alive")
    unauthorized = _get_json(client, f"{base_url}/metrics", headers, 401)
    if unauthorized.get("error", {}).get("code") != "authentication_required":
        raise VerificationError("unauthenticated metrics did not return authentication_required")
    auth_headers = headers | {"Authorization": f"Bearer {token}"}
    ready = _get_json(client, f"{base_url}/health/ready", auth_headers, 200)
    if ready.get("ready") is not True:
        raise VerificationError("authorized readiness was not ready")
    metrics = _get_json(client, f"{base_url}/metrics", auth_headers, 200)
    before = _budget(metrics)

    if args.olist_root:
        database_version, expected = _database_counts(args.olist_root, args.version_id)
        if args.version_id and database_version != args.version_id:
            raise VerificationError("local Olist database does not contain the pinned version")
        version_id = args.version_id or database_version
        expectation_source = "independent_read_only_olist_snapshot"
    else:
        expected = {"coverage": args.expected_coverage, "origin": args.expected_origin}
        if expected["coverage"] is None or expected["origin"] is None:
            raise VerificationError(
                "supply --olist-root or both --expected-coverage and --expected-origin"
            )
        version_id = args.version_id
        expectation_source = "explicit_expected_counts"

    report = {
        "schema_version": 1,
        "kind": "durable_http_smoke",
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": _public_url(args.url),
        "version_id": version_id,
        "expectation_source": expectation_source,
        "expected_counts": {key: list(value) for key, value in expected.items()},
        "anonymous_live_health": {"status": 200, "minimal": True},
        "unauthenticated_metrics": {"status": 401, "code": "authentication_required"},
        "authorized_ready": {"status": 200, "ready": True},
        "durable_budget_before": before,
        "cases": [],
    }
    args._receipt = report
    for intent in ("coverage", "origin"):
        key = f"durable-{uuid4()}"
        status, first, _ = _post(client, base_url, headers, token, version_id, intent, key)
        time.sleep(args.request_spacing_seconds)
        if status != 200:
            raise VerificationError(f"{intent} initial request returned HTTP {status}")
        counts = _validate_success(first, intent, expected[intent], version_id)
        if first.get("replayed") is not False or not first.get("investigation_id"):
            raise VerificationError("initial request lacked stable durable identity")
        initial_budget = _budget(_get_json(client, f"{base_url}/metrics", auth_headers, 200))
        replay_status, replay, _ = _post(client, base_url, headers, token, version_id, intent, key)
        time.sleep(args.request_spacing_seconds)
        if replay_status != 200:
            raise VerificationError(f"{intent} replay returned HTTP {replay_status}")
        _validate_success(replay, intent, expected[intent], version_id)
        if replay.get("investigation_id") != first["investigation_id"]:
            raise VerificationError("replay changed investigation_id")
        if (replay.get("replayed") is not True
                or _semantic_sha256(replay) != _semantic_sha256(first)):
            raise VerificationError("replay changed the committed investigation result")
        replay_budget = _budget(_get_json(client, f"{base_url}/metrics", auth_headers, 200))
        if replay_budget["reserved"] != initial_budget["reserved"]:
            raise VerificationError("same-key replay reserved additional model calls")
        changed_body = {"question": QUESTIONS[intent] + " (changed)", "version_id": version_id}
        conflict_status, conflict, _ = _post(
            client, base_url, headers, token, version_id, intent, key, changed_body
        )
        time.sleep(args.request_spacing_seconds)
        if (conflict_status != 409
                or conflict.get("error", {}).get("code") != "idempotency_conflict"):
            raise VerificationError("changed body under the same key did not return 409 conflict")
        conflict_budget = _budget(_get_json(client, f"{base_url}/metrics", auth_headers, 200))
        if conflict_budget["reserved"] != replay_budget["reserved"]:
            raise VerificationError("idempotency conflict reserved additional model calls")
        report["cases"].append({
            "intent": intent,
            "question_sha256": hashlib.sha256(QUESTIONS[intent].encode()).hexdigest(),
            "idempotency_key": key,
            "investigation_id": first["investigation_id"],
            "semantic_sha256": _semantic_sha256(first),
            "counts": {"numerator": counts["numerator"], "denominator": counts["denominator"]},
            "initial_budget_reserved": initial_budget["reserved"],
            "replay_budget_reserved": replay_budget["reserved"],
            "conflict_http_status": conflict_status,
            "replay_verified": True,
            "conflict_did_not_reserve": True,
        })
    report["durable_budget_after"] = _budget(
        _get_json(client, f"{base_url}/metrics", auth_headers, 200)
    )
    report["status"] = "passed"
    return report


def run_replay(args, client: httpx.Client) -> dict:
    baseline = json.loads(args.replay_only.read_text(encoding="utf-8"))
    if baseline.get("kind") != "durable_http_smoke" or baseline.get("status") != "passed":
        raise VerificationError("replay baseline is not a passed durable_http_smoke report")
    base_url = _public_url(args.url)
    if baseline.get("endpoint") != base_url:
        raise VerificationError("replay endpoint differs from the baseline report")
    headers = _client_headers(args.url, args.proxy_test, args.proxy_host)
    token = _token(args.token_file)
    auth_headers = headers | {"Authorization": f"Bearer {token}"}
    ready = _get_json(client, f"{base_url}/health/ready", auth_headers, 200)
    if ready.get("ready") is not True:
        raise VerificationError("authorized readiness was not ready")
    starting_budget = _budget(_get_json(client, f"{base_url}/metrics", auth_headers, 200))
    baseline_reserved = baseline.get("durable_budget_after", {}).get("reserved")
    if type(baseline_reserved) is not int or starting_budget["reserved"] < baseline_reserved:
        raise VerificationError("durable model-call budget decreased since the baseline report")
    results = []
    for case in baseline.get("cases", []):
        intent, key = case.get("intent"), case.get("idempotency_key")
        if intent not in QUESTIONS or type(key) is not str:
            raise VerificationError("baseline contains an invalid replay case")
        version_id = baseline.get("version_id")
        status, replay, _ = _post(client, base_url, headers, token, version_id, intent, key)
        if status != 200 or replay.get("replayed") is not True:
            raise VerificationError(f"{intent} was not replayed from the durable store")
        if replay.get("investigation_id") != case.get("investigation_id"):
            raise VerificationError(f"{intent} investigation_id changed across restart")
        if _semantic_sha256(replay) != case.get("semantic_sha256"):
            raise VerificationError(f"{intent} committed result changed across restart")
        current = _budget(_get_json(client, f"{base_url}/metrics", auth_headers, 200))
        if current["reserved"] != starting_budget["reserved"]:
            raise VerificationError("replay-only request reserved additional model calls")
        results.append({"intent": intent, "investigation_id": case["investigation_id"],
                        "replay_verified": True, "reserved": current["reserved"]})
    report = {
        "schema_version": 1,
        "kind": "durable_http_replay_after_restart",
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": _public_url(args.url),
        "baseline_report_sha256": hashlib.sha256(args.replay_only.read_bytes()).hexdigest(),
        "baseline_durable_budget_reserved": baseline_reserved,
        "durable_budget_before": starting_budget,
        "durable_budget_after": _budget(
            _get_json(client, f"{base_url}/metrics", auth_headers, 200)
        ),
        "cases": results,
        "status": "passed",
    }
    args._receipt = report
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Base URL of the running service")
    parser.add_argument("--token-file", type=Path, required=True,
                        help="Owner-only bearer-token file; never copied into the report")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version-id", help="Pin requests to a 64-character dataset version")
    parser.add_argument("--olist-root", type=Path,
                        help=("Read expected counts independently using the local read-only "
                              "Olist adapter"))
    parser.add_argument("--expected-coverage", type=_counts,
                        help="Expected unlinked/total contract counts, NUM/DEN")
    parser.add_argument("--expected-origin", type=_counts,
                        help="Expected missing-origin/total lead counts, NUM/DEN")
    parser.add_argument("--replay-only", type=Path,
                        help="Replay a previous passed report after process restart or restore")
    parser.add_argument("--proxy-test", action="store_true",
                        help="Add trusted-proxy headers only for a loopback HTTP URL")
    parser.add_argument("--proxy-host", help="Exact Host accepted by the local proxy test app")
    parser.add_argument("--request-spacing-seconds", type=float, default=2.1,
                        help="Pause between write requests to accommodate token-bucket refill")
    parser.add_argument("--timeout-seconds", type=float, default=70)
    args = parser.parse_args(argv)
    _public_url(args.url)
    if args.version_id is not None and re.fullmatch(r"[0-9a-fA-F]{64}", args.version_id) is None:
        parser.error("--version-id must be a 64-character hexadecimal digest")
    if not 0 <= args.request_spacing_seconds <= 60:
        parser.error("--request-spacing-seconds must be between 0 and 60")
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 1 and 300")
    if bool(args.expected_coverage) != bool(args.expected_origin):
        parser.error("provide both expected count values or neither")
    if not args.replay_only and not args.olist_root and not args.expected_coverage:
        parser.error("provide --olist-root or both --expected-coverage and --expected-origin")
    if args.replay_only and (args.olist_root or args.expected_coverage or args.expected_origin):
        parser.error("replay-only uses expectations from its baseline report")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "report.json"
    report = {
        "schema_version": 1,
        "kind": "durable_http_replay_after_restart" if args.replay_only
        else "durable_http_smoke",
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": _public_url(args.url),
        "status": "failed",
    }
    try:
        with httpx.Client(timeout=args.timeout_seconds, follow_redirects=False) as client:
            report = run_replay(args, client) if args.replay_only else run_smoke(args, client)
    except (httpx.HTTPError, OSError, ValueError, VerificationError) as error:
        # Deliberately omit exception text; HTTP errors may contain the supplied URL.
        report = getattr(args, "_receipt", report)
        report["error"] = type(error).__name__
        _write_report(report_path, report)
        print(f"Verification failed ({report['error']}); receipt: {report_path}", file=sys.stderr)
        return 1
    _write_report(report_path, report)
    print(f"Verification passed; receipt: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
