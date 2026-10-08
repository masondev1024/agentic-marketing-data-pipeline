"""Read-only diagnostic boundary. No model, arbitrary SQL, or external action executor."""

import math
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_ops_lab.marketing.contracts import ID, ContractError, canonical, day, strict_json, utc
from llm_ops_lab.marketing.pipeline import METRICS, digest

VERSION = "marketing-diagnostics.v1"
TOOLS = frozenset({"metrics", "quality", "freshness", "definitions"})
MONEY = ("spend_krw", "gross_sales_krw", "refund_krw", "net_attributed_sales_krw")
ROW_FIELDS = {"account_id", "business_date", "order_count", "complete", "through_utc",
              "observed_roas", "roas", *MONEY}
CLAIM_FIELDS = frozenset({*MONEY, "order_count", "roas", "observed_roas", "complete"})


@dataclass(frozen=True)
class Limits:
    # Engineering defaults, not numbers supplied by the user; callers may only tighten.
    max_actions: int = 8
    max_tool_calls: int = 6
    max_rows: int = 31
    max_action_bytes: int = 4096
    max_token_units: int = 32768
    max_output_bytes: int = 16384
    wall_seconds: float = 30.0
    tool_seconds: float = 5.0

    def __post_init__(self):
        ceilings = (8, 6, 31, 4096, 32768, 16384, 30.0, 5.0)
        for (key, value), ceiling in zip(asdict(self).items(), ceilings, strict=True):
            kind = (int, float) if key.endswith("seconds") else (int,)
            if type(value) not in kind or not math.isfinite(value) or not 0 < value <= ceiling:
                raise ContractError("limits", "limits may only tighten v1 ceilings")


@dataclass(frozen=True)
class Scope:
    snapshot_id: str
    account_id: str
    date_start: str
    date_end: str

    def __post_init__(self):
        if type(self.snapshot_id) is not str or not re.fullmatch("[0-9a-f]{64}", self.snapshot_id):
            raise ContractError("scope", "expected SHA256 snapshot ID")
        if type(self.account_id) is not str or not ID.fullmatch(self.account_id):
            raise ContractError("scope", "invalid account")
        if not 0 <= (day(self.date_end) - day(self.date_start)).days < 31:
            raise ContractError("scope", "1..31 business dates required")


def read_bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ContractError("snapshot_integrity", "regular snapshot file required")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ContractError("snapshot_limit", "snapshot artifact too large")
    return raw


def observed_comparison(rows: list[dict]) -> str:
    """Compare exact integer ratios; DOUBLE ROAS is a display value only."""
    if len(rows) != 2 or any(r["roas"] is None or r["spend_krw"] <= 0 for r in rows):
        return "unknown"
    left, right = sorted(rows, key=lambda r: r["business_date"])
    declined = (right["net_attributed_sales_krw"] * left["spend_krw"]
                < left["net_attributed_sales_krw"] * right["spend_krw"])
    return "observed_decline" if declined else "no_observed_decline"


def validate_metric_rows(rows: list, scope: Scope) -> None:
    expected_dates = {(day(scope.date_start) + timedelta(days=i)).isoformat()
                      for i in range((day(scope.date_end) - day(scope.date_start)).days + 1)}
    if type(rows) is not list or len(rows) != len(expected_dates):
        raise ContractError("evidence_schema", "one metric row per scoped date required")
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != ROW_FIELDS:
            raise ContractError("evidence_schema", "exact metric schema required")
        if (row["account_id"] != scope.account_id or row["business_date"] not in expected_dates
                or row["business_date"] in seen):
            raise ContractError("evidence_scope", "metric scope/key mismatch")
        seen.add(row["business_date"])
        if any(type(row[k]) is not int or not 0 <= row[k] <= 10**18
               for k in (*MONEY, "order_count")) or type(row["complete"]) is not bool:
            raise ContractError("evidence_schema", "bounded integer amounts and bool required")
        if row["net_attributed_sales_krw"] != row["gross_sales_krw"] - row["refund_krw"]:
            raise ContractError("evidence_schema", "sales reconciliation mismatch")
        if row["through_utc"] is not None:
            utc(row["through_utc"])
        if row["complete"] and row["through_utc"] is None:
            raise ContractError("evidence_schema", "complete row needs watermark")
        observed = (float(row["net_attributed_sales_krw"]) / float(row["spend_krw"])
                    if row["spend_krw"] else None)
        for key, expected in (("observed_roas", observed),
                              ("roas", observed if row["complete"] else None)):
            value = row[key]
            if (value is not None and (type(value) not in (float, int) or not math.isfinite(value))
                    or value != expected):
                raise ContractError("evidence_schema", "ROAS reconciliation mismatch")


@dataclass(frozen=True)
class Snapshot:
    scope: Scope
    payload: bytes
    manifest_sha256: str

    @classmethod
    def open(cls, root: Path, scope: Scope):
        pointer_raw = read_bounded(root / "CURRENT.json", 1024)
        pointer = strict_json(pointer_raw)
        if type(pointer) is not dict or pointer.get("snapshot_id") != scope.snapshot_id:
            raise ContractError("stale_snapshot", "request does not match current publication")
        directory = root / "snapshots" / scope.snapshot_id
        if directory.is_symlink() or (root / "snapshots").is_symlink():
            raise ContractError("snapshot_integrity", "snapshot directory symlink rejected")
        raw = read_bounded(directory / "manifest.json", 1024 * 1024)
        if digest(raw) != pointer.get("manifest_sha256"):
            raise ContractError("snapshot_integrity", "manifest hash mismatch")
        manifest = strict_json(raw)
        try:
            if (manifest["schema_version"] != "marketing-manifest.v1"
                    or manifest["snapshot_id"] != scope.snapshot_id
                    or digest(canonical(manifest["identity"])) != scope.snapshot_id
                    or scope.account_id not in manifest["accounts"]
                    or not manifest["date_start"] <= scope.date_start <= scope.date_end
                    <= manifest["date_end"]):
                raise ContractError("scope", "publication scope mismatch")
            captured = {}
            for name in ("gold/metrics.json", "metric_definitions.json"):
                if (directory / Path(name).parent).is_symlink():
                    raise ContractError("snapshot_integrity", "artifact directory symlink rejected")
                content = read_bounded(directory / name, 8 * 1024 * 1024)
                if digest(content) != manifest["artifacts"][name]:
                    raise ContractError("snapshot_integrity", "artifact hash mismatch")
                captured[name] = strict_json(content)
            if captured["metric_definitions.json"] != METRICS:
                raise ContractError("evidence_schema", "unknown metric definition revision")
            all_rows = captured["gold/metrics.json"]["account"]
            if type(all_rows) is not list or len(all_rows) > 992:
                raise ContractError("evidence_schema", "account rows exceed bounded product")
            rows = [r for r in all_rows if r["account_id"] == scope.account_id
                    and scope.date_start <= r["business_date"] <= scope.date_end]
            validate_metric_rows(rows, scope)
            utc(manifest["as_of"])
            if any(r["through_utc"] and r["through_utc"] > manifest["as_of"] for r in rows):
                raise ContractError("evidence_schema", "watermark exceeds as-of")
            payload = {"rows": rows, "as_of": manifest["as_of"], "definitions": METRICS,
                       "fixture_revision": manifest["identity"]["fixture_revision"]}
        except (KeyError, TypeError, AttributeError) as error:
            raise ContractError("evidence_schema", "invalid snapshot schema") from error
        if read_bounded(root / "CURRENT.json", 1024) != pointer_raw:
            raise ContractError("stale_snapshot", "publication changed during capture")
        # Only scoped immutable bytes survive. Future publication cannot mix observations.
        return cls(scope, canonical(payload), digest(raw))


def execute_bounded(payload: dict, timeout: float, cancelled: threading.Event) -> dict:
    """Owned process only: a timeout/cancel kills and reaps it before returning."""
    if cancelled.is_set():
        raise ContractError("cancelled", "diagnosis cancelled")
    process = subprocess.Popen(
        [sys.executable, "-m", "llm_ops_lab.marketing.diagnostic_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    pending = canonical(payload)
    try:
        while True:
            if cancelled.is_set():
                raise ContractError("cancelled", "diagnosis cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContractError("timeout", "read-only tool deadline exceeded")
            try:
                stdout, stderr = process.communicate(pending, timeout=min(remaining, 0.05))
                break
            except subprocess.TimeoutExpired:
                pending = None
        if process.returncode or stderr:
            raise ContractError("tool_failed", "read-only worker failed")
        if len(stdout) > 16384:
            raise ContractError("output_limit", "tool output too large")
        return strict_json(stdout)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


class DiagnosticSession:
    """One trusted host scope, never supplied/replaced by a model action.

    Error is terminal. Duplicate tools are no progress because all inputs are immutable.
    UTF-8 bytes debit a conservative token upper bound for the JSON protocol, including
    observations; model prompt/generation budgets remain the inference caller's duty.
    """

    def __init__(self, snapshot: Snapshot, *, limits: Limits = Limits(), now: str | None = None):
        self._snapshot = snapshot
        self._limits = limits
        self._clock = utc(now) if now else datetime.now(UTC)
        self._started = time.monotonic()
        self._actions = self._calls = self._units = 0
        self._seen = set()
        self._evidence = {}
        self._terminal = False
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    @property
    def scope(self):
        return self._snapshot.scope

    @property
    def usage(self):
        return {"actions": self._actions, "tool_calls": self._calls, "token_units": self._units}

    def cancel(self):
        self._cancelled.set()

    def _check_time(self):
        if self._cancelled.is_set():
            raise ContractError("cancelled", "diagnosis cancelled")
        if time.monotonic() - self._started >= self._limits.wall_seconds:
            raise ContractError("timeout", "session wall deadline exceeded")

    def _debit(self, count):
        self._units += count
        if self._units > self._limits.max_token_units:
            raise ContractError("token_budget", "conservative protocol token budget exhausted")

    def _error(self, code, detail):
        return {"status": "error", "error": {"code": code, "detail": detail},
                "conclusion": "unknown", "causality": "unknown", "proposed_actions": [],
                "usage": self.usage}

    def dispatch(self, raw: str) -> dict:
        if not self._lock.acquire(blocking=False):
            return self._error("session_busy", "another action is running")
        try:
            if self._terminal:
                return self._error("session_closed", "terminal session cannot execute tools")
            self._check_time()
            self._actions += 1
            if self._actions > self._limits.max_actions:
                raise ContractError("action_budget", "action budget exhausted")
            if type(raw) is not str or len(raw.encode()) > self._limits.max_action_bytes:
                raise ContractError("input_limit", "bounded JSON text required")
            self._debit(len(raw.encode()))
            request = strict_json(raw)
            if type(request) is not dict:
                raise ContractError("request_schema", "JSON object required")
            if request.get("action") == "finish":
                result = self._finish(request)
                self._check_time()
                self._terminal = True
                return self._emit(result)
            if set(request) != {"action", "tool", "parameters"} or request["action"] != "tool":
                raise ContractError("request_schema", "expected tool or finish protocol")
            if type(request["tool"]) is not str or request["tool"] not in TOOLS:
                raise ContractError("unauthorized_tool", "tool is not read-only allowlisted")
            # Tools have no model-controlled SQL, scope, paths, limits, or free-text parameters.
            if request["parameters"] != {} or type(request["parameters"]) is not dict:
                raise ContractError("scope", "tool parameters must be empty; host scope is fixed")
            tool = request["tool"]
            if tool in self._seen:
                raise ContractError("no_progress", "immutable tool repeated")
            if self._calls >= self._limits.max_tool_calls:
                raise ContractError("tool_budget", "tool call budget exhausted")
            self._seen.add(tool)
            self._calls += 1
            payload = strict_json(self._snapshot.payload)
            timeout = min(self._limits.tool_seconds,
                          self._limits.wall_seconds - (time.monotonic() - self._started))
            result = execute_bounded(payload | {"tool": tool, "scope": asdict(self.scope),
                                               "max_rows": self._limits.max_rows},
                                     timeout, self._cancelled)
            self._check_time()
            self._validate_result(tool, result, payload)
            content = {"version": VERSION, "scope": asdict(self.scope), "tool": tool,
                       "manifest_sha256": self._snapshot.manifest_sha256,
                       "fixture_revision": payload["fixture_revision"], "data": result}
            encoded = canonical(content)
            if len(encoded) > self._limits.max_output_bytes:
                raise ContractError("output_limit", "bounded evidence output exceeded")
            evidence_id = digest(encoded)
            self._evidence[evidence_id] = encoded
            return self._emit({"status": "ok", "evidence_id": evidence_id,
                               "evidence": content})
        except ContractError as error:
            self._terminal = True
            return self._error(error.code, error.detail)
        except (ValueError, TypeError, OSError, KeyError, RecursionError) as error:
            self._terminal = True
            return self._error("invalid_request_or_evidence", type(error).__name__)
        finally:
            self._lock.release()

    def _emit(self, result):
        # Charge the whole envelope, including IDs and usage. Reserve 32 bytes for
        # counter digit growth after this debit. Errors are terminal control messages.
        size = len(canonical(result | {"usage": self.usage})) + 32
        if size > self._limits.max_output_bytes:
            raise ContractError("output_limit", "bounded response envelope exceeded")
        self._debit(size)
        return result | {"usage": self.usage}

    def _validate_result(self, tool, result, payload):
        # Independently reconcile worker schema/content against captured facts before exposure.
        rows = sorted(payload["rows"], key=lambda r: r["business_date"])
        if len(rows) > self._limits.max_rows:
            raise ContractError("row_limit", "refuse partial observations")
        if tool == "metrics":
            validate_metric_rows(result, self.scope)
            expected = rows
        elif tool == "quality":
            expected = [{k: r[k] for k in ("account_id", "business_date", "complete")}
                        for r in rows]
        elif tool == "freshness":
            expected = {"as_of": payload["as_of"], "rows": [
                {k: r[k] for k in ("account_id", "business_date", "through_utc")} for r in rows]}
        else:
            expected = payload["definitions"]
        if canonical(result) != canonical(expected):
            raise ContractError("evidence_schema", "tool output failed independent reconciliation")

    def _finish(self, request):
        if set(request) != {"action", "conclusion", "evidence_ids", "claims"}:
            raise ContractError("request_schema", "exact finish fields required")
        conclusion = request["conclusion"]
        if conclusion not in ("unknown", "observed_decline", "no_observed_decline"):
            raise ContractError("unsupported_claim", "only observed comparison or unknown allowed")
        ids, claims = request["evidence_ids"], request["claims"]
        if (type(ids) is not list or len(ids) > 4 or any(type(i) is not str for i in ids)
                or len(set(ids)) != len(ids) or any(i not in self._evidence for i in ids)
                or type(claims) is not list or len(claims) > 31):
            raise ContractError("evidence_reference", "bounded issued evidence required")
        evidence = {i: strict_json(self._evidence[i]) for i in ids}
        for claim in claims:
            if (type(claim) is not dict or set(claim) != {"evidence_id", "row", "field", "value"}
                    or type(claim["evidence_id"]) is not str
                    or claim["evidence_id"] not in evidence
                    or type(claim["row"]) is not int or claim["row"] < 0
                    or type(claim["field"]) is not str or claim["field"] not in CLAIM_FIELDS):
                raise ContractError("unsupported_claim", "invalid numerical evidence claim")
            item = evidence[claim["evidence_id"]]
            if item["tool"] != "metrics" or claim["row"] >= len(item["data"]):
                raise ContractError("unsupported_claim", "claim must cite a metric row")
            actual = item["data"][claim["row"]][claim["field"]]
            if type(actual) is not type(claim["value"]) or actual != claim["value"]:
                raise ContractError("unsupported_claim", "claim differs from computed fact")
        rows = sorted(strict_json(self._snapshot.payload)["rows"], key=lambda r: r["business_date"])
        reason = "requested_unknown"
        if conclusion != "unknown":
            if not {"metrics", "quality", "freshness"} <= {e["tool"] for e in evidence.values()}:
                raise ContractError("evidence_incomplete", "metrics, quality, freshness required")
            payload = strict_json(self._snapshot.payload)
            age = (self._clock - utc(payload["as_of"])).total_seconds()
            fresh = 0 <= age <= 86400 and all(
                r["through_utc"] is not None
                and 0 <= (self._clock - utc(r["through_utc"])).total_seconds() <= 86400
                for r in rows)
            if not fresh or not all(r["complete"] and r["roas"] is not None for r in rows):
                conclusion, reason = "unknown", "freshness_completeness_or_zero_spend"
            elif len(rows) != 2:
                conclusion, reason = "unknown", "comparison_requires_two_dates"
            else:
                expected = observed_comparison(rows)
                if conclusion != expected:
                    raise ContractError("unsupported_claim", "comparison contradicts evidence")
                reason = "observed_only_not_causal"
        return {"status": "abstained" if conclusion == "unknown" else "ok",
                "scope": asdict(self.scope), "conclusion": conclusion, "reason": reason,
                "claims": claims, "evidence_ids": ids, "causality": "unknown",
                "hypotheses": [], "proposed_actions": []}
