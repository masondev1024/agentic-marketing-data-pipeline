"""Strict v1 source contracts. Fail closed before any dimensional join."""

import json
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

VERSION = "marketing.v1"
KST = ZoneInfo("Asia/Seoul")
MAX_ROWS = 100_000
MAX_MONEY = 10**12
SOURCES = ("spend", "orders", "refunds")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
COMMON = {"account_id": str, "source": str, "revision": int,
          "event_time": str, "ingested_at": str}
SCHEMAS = {
    "campaigns": {"account_id": str, "campaign_id": str, "name": str},
    "spend_snapshots": COMMON | {
        "snapshot_id": str, "campaign_id": str, "business_date": str,
        "snapshot_at": str, "spend_krw": int,
    },
    "orders": COMMON | {"order_id": str, "campaign_id": str, "gross_sales_krw": int},
    "refunds": COMMON | {"refund_id": str, "order_id": str, "refund_krw": int},
    "coverage": {"account_id": str, "business_date": str, "source": str,
                 "complete": bool, "through_utc": str},
}
TOP_KEYS = {"schema_version", "fixture_revision", "accounts", "as_of", "date_start",
            "date_end", *SCHEMAS}


class ContractError(ValueError):
    """A machine-readable, publication-blocking source error."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def strict_json(raw: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("invalid_json", "duplicate object key")
            result[key] = value
        return result

    def constant(_):
        raise ContractError("invalid_json", "nonfinite JSON number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid_json", "strict JSON required") from error


def utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
        # Require one representation to avoid equivalent keys with different text.
        if parsed.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
            raise ValueError("use second precision Z")
        return parsed
    except (AttributeError, ValueError) as error:
        raise ContractError("invalid_timestamp", "expected YYYY-MM-DDTHH:MM:SSZ") from error


def day(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError("not canonical ISO date")
        return result
    except (TypeError, ValueError) as error:
        raise ContractError("invalid_date", "expected YYYY-MM-DD") from error


def validate_rows(table: str, rows: list[dict]) -> None:
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise ContractError("row_limit", f"{table}: expected list of at most {MAX_ROWS}")
    schema = SCHEMAS[table]
    for index, row in enumerate(rows):
        label = f"{table}[{index}]"
        if type(row) is not dict or set(row) != set(schema):
            raise ContractError("schema", f"{label}: exact v1 fields required")
        for key, kind in schema.items():
            value = row[key]
            if type(value) is not kind:
                raise ContractError("type", f"{label}.{key}: {kind.__name__} required")
            if kind is str and (not value or len(value) > 256):
                raise ContractError("missing_key", f"{label}.{key}: empty/oversized")
            if key.endswith("_id") and not ID.fullmatch(value):
                raise ContractError("invalid_key", f"{label}.{key}")
            if key.endswith("_krw") and not 0 <= value <= MAX_MONEY:
                raise ContractError("money", f"{label}.{key}: nonnegative bounded integer KRW")
            if key == "revision" and not 1 <= value <= 1_000_000:
                raise ContractError("revision", f"{label}: invalid revision")
        for key in ("event_time", "ingested_at", "snapshot_at", "through_utc"):
            if key in row:
                utc(row[key])
        if "business_date" in row:
            day(row["business_date"])
        if "event_time" in row and utc(row["event_time"]) > utc(row["ingested_at"]):
            raise ContractError("time_order", f"{label}: event after ingestion")
        if table == "spend_snapshots":
            if utc(row["snapshot_at"]) > utc(row["event_time"]):
                raise ContractError("time_order", f"{label}: snapshot after source event")
            if utc(row["snapshot_at"]).astimezone(KST).date() < day(row["business_date"]):
                raise ContractError("time_order", f"{label}: snapshot before business date")
        if "source" in row:
            expected = {"spend_snapshots": "spend", "orders": "orders", "refunds": "refunds"}
            if (table in expected and row["source"] != expected[table]) or (
                table == "coverage" and row["source"] not in SOURCES
            ):
                raise ContractError("source", f"{label}: unrecognized source")


def deduplicate_versions(rows: list[dict], entity: str, immutable: tuple[str, ...]) -> list[dict]:
    """Collapse delivery retries; reject differing business payload at identical revision.

    Ingestion time is delivery metadata: retain earliest ingestion of identical payloads.
    Validate all revisions including ones superseded or beyond the requested as-of.
    """
    versions = {}
    identities = {}
    for row in rows:
        identity = (row["account_id"], row["source"], row[entity])
        stable = tuple(row[key] for key in immutable)
        if identity in identities and identities[identity] != stable:
            raise ContractError("identity_changed", f"{entity}: immutable grain changed")
        identities[identity] = stable
        key = (*identity, row["revision"])
        payload = {k: v for k, v in row.items() if k != "ingested_at"}
        if key in versions:
            previous = versions[key]
            if payload != {k: v for k, v in previous.items() if k != "ingested_at"}:
                raise ContractError("payload_conflict", f"{entity}: contradictory same revision")
            if row["ingested_at"] < previous["ingested_at"]:
                versions[key] = dict(row)
        else:
            versions[key] = dict(row)
    return sorted(versions.values(), key=lambda r: canonical(r))


def latest_events(rows: list[dict], entity: str, as_of: str) -> list[dict]:
    latest = {}
    for row in rows:
        if row["ingested_at"] > as_of:
            continue
        key = (row["account_id"], row[entity])
        if key not in latest or latest[key]["revision"] < row["revision"]:
            latest[key] = row
    return sorted(latest.values(), key=lambda r: canonical(r))


def normalize_spend(spend_snapshots: list[dict], as_of: str | None = None) -> list[dict]:
    validate_rows("spend_snapshots", spend_snapshots)
    if as_of is not None:
        utc(as_of)
    versions = deduplicate_versions(
        spend_snapshots, "snapshot_id", ("campaign_id", "business_date", "snapshot_at"),
    )
    # Separate IDs cannot claim the same source snapshot position.
    positions = {}
    for row in versions:
        key = (row["account_id"], row["campaign_id"], row["business_date"], row["snapshot_at"])
        if key in positions and positions[key] != row["snapshot_id"]:
            raise ContractError("snapshot_key", "multiple IDs for one cumulative snapshot")
        positions[key] = row["snapshot_id"]
    latest = {}
    for row in versions:
        if as_of is not None and row["ingested_at"] > as_of:
            continue
        key = (row["account_id"], row["campaign_id"], row["business_date"])
        rank = (row["snapshot_at"], row["revision"])
        if key not in latest or rank > (latest[key]["snapshot_at"], latest[key]["revision"]):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: canonical(r))



def normalize_standalone_spend(rows: list[dict]) -> list[dict]:
    """Validate self-describing spend v1 records without discarding their metadata.

    Here as_of is the source snapshot position, not a dataset ingestion cutoff.
    Delivery can occur after as_of. Internal bundle rows retain their own contract.
    """
    required = {
        "schema_version", "fixture_version", "snapshot_id", "account_id", "campaign_id",
        "business_date", "spend_krw", "currency", "complete", "event_time",
        "ingestion_time", "as_of",
    }
    revision_names = {"source_revision", "revision", "snapshot_version", "source_version"}
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise ContractError("row_limit", f"expected list of at most {MAX_ROWS}")
    versions, identities, positions = {}, {}, {}
    for row in rows:
        if type(row) is not dict:
            raise ContractError("schema", "spend record must be an object")
        revisions = revision_names & row.keys()
        if (not required <= row.keys() or len(revisions) != 1
                or row.keys() - required - revisions - {"source"}):
            raise ContractError("schema", "standalone spend v1 fields and one revision required")
        revision = row[next(iter(revisions))]
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise ContractError("revision", "bounded integer revision required")
        for key in required - {"spend_krw", "complete"} | ({"source"} & row.keys()):
            if type(row[key]) is not str or not row[key] or len(row[key]) > 256:
                raise ContractError("type", f"{key}: bounded nonempty string required")
        for key in ("snapshot_id", "account_id", "campaign_id", "fixture_version"):
            if not ID.fullmatch(row[key]):
                raise ContractError("invalid_key", key)
        if row["schema_version"] not in {"spend.v1", "spend_snapshot.v1", "spend_snapshots.v1"}:
            raise ContractError("schema_version", "unsupported standalone spend version")
        if row["currency"] != "KRW":
            raise ContractError("currency", "KRW required")
        if type(row["complete"]) is not bool:
            raise ContractError("type", "complete: bool required")
        if type(row["spend_krw"]) is not int or not 0 <= row["spend_krw"] <= MAX_MONEY:
            raise ContractError("money", "nonnegative bounded integer KRW required")
        event, ingestion, as_of = (utc(row[k]) for k in
                                    ("event_time", "ingestion_time", "as_of"))
        business_date = day(row["business_date"])
        if event > ingestion or as_of > ingestion or event > as_of:
            raise ContractError("time_order", "event <= source as_of <= ingestion required")
        if as_of.astimezone(KST).date() < business_date:
            raise ContractError("time_order", "snapshot before business date")
        identity = (row["account_id"], row.get("source", "spend"), row["snapshot_id"])
        grain = (row["campaign_id"], row["business_date"], row["as_of"])
        if identity in identities and identities[identity] != grain:
            raise ContractError("identity_changed", "snapshot grain changed")
        identities[identity] = grain
        position = (row["account_id"], *grain)
        if position in positions and positions[position] != identity:
            raise ContractError("snapshot_key", "multiple IDs/sources for one snapshot")
        positions[position] = identity
        key = (*identity, revision)
        payload = {k: v for k, v in row.items() if k != "ingestion_time"}
        if key in versions:
            previous = versions[key]
            if payload != {k: v for k, v in previous.items() if k != "ingestion_time"}:
                raise ContractError("payload_conflict", "contradictory same revision")
            if row["ingestion_time"] >= previous["ingestion_time"]:
                continue
        versions[key] = dict(row)
    latest = {}
    for key, row in versions.items():
        grain = (row["account_id"], row["campaign_id"], row["business_date"])
        rank = (row["as_of"], key[-1])
        if grain not in latest or rank > latest[grain][0]:
            latest[grain] = (rank, row)
    return sorted((r for _, r in latest.values()), key=canonical)

def validate_bundle(bundle: dict) -> None:
    if type(bundle) is not dict or set(bundle) != TOP_KEYS:
        raise ContractError("schema", "exact dataset v1 fields required")
    if bundle["schema_version"] != VERSION:
        raise ContractError("schema_version", "unsupported dataset version")
    if type(bundle["fixture_revision"]) is not str or not ID.fullmatch(bundle["fixture_revision"]):
        raise ContractError("fixture_revision", "bounded revision identifier required")
    accounts = bundle["accounts"]
    if (type(accounts) is not list or not 1 <= len(accounts) <= 32
            or any(type(a) is not str or not ID.fullmatch(a) for a in accounts)
            or len(set(accounts)) != len(accounts)):
        raise ContractError("accounts", "1..32 unique account identifiers required")
    start, end = day(bundle["date_start"]), day(bundle["date_end"])
    if not 0 <= (end - start).days < 31:
        raise ContractError("date_window", "1..31 business dates required")
    as_of = utc(bundle["as_of"])
    if end > as_of.astimezone(KST).date():
        raise ContractError("date_window", "future business date")
    for table in SCHEMAS:
        validate_rows(table, bundle[table])
        for row in bundle[table]:
            if row["account_id"] not in accounts:
                raise ContractError("scope", f"{table}: undeclared account")
    seen = set()
    for row in bundle["coverage"]:
        key = (row["account_id"], row["business_date"], row["source"])
        if key in seen:
            raise ContractError("coverage_key", "duplicate coverage declaration")
        seen.add(key)
        if utc(row["through_utc"]) > as_of:
            raise ContractError("coverage_time", "coverage exceeds dataset as-of")
        # Complete means the source attests all events through the KST end of day.
        end_day = datetime.combine(day(row["business_date"]), datetime.max.time(), KST)
        if row["complete"] and utc(row["through_utc"]) < end_day.astimezone(UTC):
            raise ContractError("coverage_time", "complete source before business day closed")
