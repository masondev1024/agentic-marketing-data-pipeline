"""Bounded full-window restatement, Parquet artifacts and atomic local publication."""

import fcntl
import hashlib
import json
import logging
import os
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb

from llm_ops_lab.marketing.contracts import (
    KST,
    SOURCES,
    VERSION,
    ContractError,
    canonical,
    day,
    deduplicate_versions,
    latest_events,
    normalize_spend,
    normalize_standalone_spend,
    strict_json,
    utc,
    validate_bundle,
)

PIPELINE_VERSION = "marketing-pipeline.v1"
LOGGER = logging.getLogger(__name__)
METRICS = {
    "version": "marketing-metrics.v1",
    "currency": "KRW integer",
    "timezone": "Asia/Seoul",
    "attribution": "synthetic single campaign per order; not causal attribution",
    "spend_krw": "latest cumulative snapshot_at, then latest revision; never sum snapshots",
    "gross_sales_krw": "sum latest visible order gross amount on original KST order date",
    "refund_krw": "sum latest visible refund events, restated on original KST order date",
    "net_attributed_sales_krw": "gross_sales_krw - refund_krw",
    "observed_roas": "net_attributed_sales_krw / spend_krw; null if spend is zero",
    "roas": "observed_roas only if all source coverage complete; otherwise null",
    "coverage": "source assertion through UTC watermark; not independent proof of completeness",
}
TABLES = {
    "silver_campaigns": "account_id VARCHAR, campaign_id VARCHAR, name VARCHAR",
    "silver_spend": "account_id VARCHAR, campaign_id VARCHAR, business_date VARCHAR, "
                    "spend_krw BIGINT, snapshot_id VARCHAR, revision BIGINT, source VARCHAR, "
                    "snapshot_at VARCHAR, event_time VARCHAR, ingested_at VARCHAR",
    "silver_orders": "account_id VARCHAR, campaign_id VARCHAR, business_date VARCHAR, "
                     "order_id VARCHAR, gross_sales_krw BIGINT, revision BIGINT, source VARCHAR, "
                     "event_time VARCHAR, ingested_at VARCHAR",
    "silver_refunds": "account_id VARCHAR, order_id VARCHAR, refund_id VARCHAR, refund_krw BIGINT, "
                      "revision BIGINT, source VARCHAR, event_time VARCHAR, ingested_at VARCHAR",
    "silver_coverage": "account_id VARCHAR, business_date VARCHAR, source VARCHAR, "
                       "complete BOOLEAN, through_utc VARCHAR",
    "quality_daily": "account_id VARCHAR, business_date VARCHAR, complete BOOLEAN, "
                     "through_utc VARCHAR",
    "business_days": "business_date VARCHAR",
}
ARTIFACT_TABLES = (*TABLES, "gold_campaign", "gold_account")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows(connection, table: str) -> list[dict]:
    if table not in ARTIFACT_TABLES:
        raise ValueError("not an artifact table")
    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY ALL")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def insert(connection, table: str, records: list[dict]) -> None:
    columns = [item.split()[0] for item in TABLES[table].split(", ")]
    connection.execute(f"CREATE TABLE {table} ({TABLES[table]})")
    if records:
        connection.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
            [[record[column] for column in columns] for record in records],
        )


def process_spend_snapshots(spend_snapshots: list[dict]) -> dict:
    """Public entry: internal or self-describing v1 spend -> Silver and daily Gold.

    Pure with respect to the filesystem. Returns only silver_row_count and gold;
    identical delivery of 10000 produces one Silver row and one Gold 10000.
    Publication/backfill for full commerce bundles is provided by publish_dataset.
    """
    standalone = (type(spend_snapshots) is list and bool(spend_snapshots)
                  and type(spend_snapshots[0]) is dict
                  and "schema_version" in spend_snapshots[0])
    silver = (normalize_standalone_spend(spend_snapshots) if standalone
              else normalize_spend(spend_snapshots))
    with duckdb.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE silver_spend (account_id VARCHAR, campaign_id VARCHAR, "
            "business_date VARCHAR, spend_krw BIGINT)",
        )
        if silver:
            connection.executemany("INSERT INTO silver_spend VALUES (?, ?, ?, ?)", [
                [r[k] for k in ("account_id", "campaign_id", "business_date", "spend_krw")]
                for r in silver
            ])
        cursor = connection.execute(
            "SELECT account_id, campaign_id, business_date, SUM(spend_krw)::BIGINT AS spend_krw "
            "FROM silver_spend GROUP BY account_id, campaign_id, business_date ORDER BY ALL",
        )
        gold = [dict(zip([c[0] for c in cursor.description], r, strict=True))
                for r in cursor.fetchall()]
    return {"silver_row_count": len(silver), "gold": gold}


def normalized_tables(bundle: dict) -> tuple[dict, dict]:
    validate_bundle(bundle)
    campaigns = {}
    for row in bundle["campaigns"]:
        key = (row["account_id"], row["campaign_id"])
        if key in campaigns and campaigns[key] != row:
            raise ContractError("campaign_conflict", "contradictory dimension key")
        campaigns[key] = row
    if any(not any(key[0] == account for key in campaigns) for account in bundle["accounts"]):
        raise ContractError("missing_mapping", "every account needs a campaign dimension")
    spend = normalize_spend(bundle["spend_snapshots"], bundle["as_of"])
    order_versions = deduplicate_versions(bundle["orders"], "order_id",
                                          ("campaign_id", "event_time"))
    refund_versions = deduplicate_versions(bundle["refunds"], "refund_id",
                                           ("order_id", "event_time"))
    orders = latest_events(order_versions, "order_id", bundle["as_of"])
    refunds = latest_events(refund_versions, "refund_id", bundle["as_of"])
    for row in [*bundle["spend_snapshots"], *bundle["orders"]]:
        if (row["account_id"], row["campaign_id"]) not in campaigns:
            raise ContractError("missing_mapping", "campaign missing in account scope")
    order_map = {(r["account_id"], r["order_id"]): r for r in orders}
    refund_totals = {}
    for row in refunds:
        key = (row["account_id"], row["order_id"])
        order = order_map.get(key)
        if order is None:
            raise ContractError("orphan_refund", "refund has no visible order in account")
        if row["event_time"] < order["event_time"]:
            raise ContractError("refund_time", "refund before order")
        refund_totals[key] = refund_totals.get(key, 0) + row["refund_krw"]
        if refund_totals[key] > order["gross_sales_krw"]:
            raise ContractError("over_refund", "refund total exceeds visible gross order value")
    start, end = day(bundle["date_start"]), day(bundle["date_end"])
    dates = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
    visible_orders = [r | {"business_date": utc(r["event_time"]).astimezone(KST).date().isoformat()}
                      for r in orders]
    visible_orders = [r for r in visible_orders if r["business_date"] in dates]
    included = {(r["account_id"], r["order_id"]) for r in visible_orders}
    coverage = {(r["account_id"], r["business_date"], r["source"]): r
                for r in bundle["coverage"]}
    quality = []
    missing = []
    for account in sorted(bundle["accounts"]):
        for date_str in dates:
            entries = [coverage.get((account, date_str, source)) for source in SOURCES]
            absent = [s for s, r in zip(SOURCES, entries, strict=True) if r is None]
            if absent:
                missing.append({"account_id": account, "business_date": date_str,
                                "sources": absent})
            quality.append({"account_id": account, "business_date": date_str,
                            "complete": all(r and r["complete"] for r in entries),
                            "through_utc": min((r["through_utc"] for r in entries if r),
                                               default=None) if not absent else None})
    tables = {
        "silver_campaigns": sorted(campaigns.values(), key=canonical),
        "silver_spend": [r for r in spend if r["business_date"] in dates],
        "silver_orders": visible_orders,
        "silver_refunds": [r for r in refunds if (r["account_id"], r["order_id"]) in included],
        "silver_coverage": [r for r in bundle["coverage"] if r["business_date"] in dates],
        "quality_daily": quality,
        "business_days": [{"business_date": d} for d in dates],
    }
    dq = {"status": "complete" if all(r["complete"] for r in quality) else "incomplete",
          "missing_coverage": missing, "blocking_errors": [],
          "raw_rows": {table: len(bundle[table]) for table in
                       ("campaigns", "spend_snapshots", "orders", "refunds", "coverage")},
          "silver_rows": {name: len(value) for name, value in tables.items()},
          "future_delivery_rows": sum(r["ingested_at"] > bundle["as_of"]
                                      for name in ("spend_snapshots", "orders", "refunds")
                                      for r in bundle[name])}
    return tables, dq


def write_durable(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def writer_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError("writer_busy", "another local publisher owns the lock") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def source_hashes() -> dict:
    directory = Path(__file__).parent
    return {name: digest((directory / name).read_bytes()) for name in
            ("contracts.py", "pipeline.py", "transform.sql")}


def verify_snapshot(path: Path) -> dict:
    manifest_bytes = (path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    identity = manifest["identity"]
    if (digest(canonical(identity)) != manifest["snapshot_id"]
            or path.name != manifest["snapshot_id"]):
        raise ContractError("snapshot_integrity", "invalid snapshot identity")
    for relative, expected in manifest["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ContractError("snapshot_integrity", "invalid artifact path")
        if digest((path / relative).read_bytes()) != expected:
            raise ContractError("snapshot_integrity", f"artifact hash mismatch: {relative}")
    return manifest


def publish_dataset(
    bundle: dict, output_dir: Path | str, *, raw_bytes: bytes | None = None,
) -> dict:
    """Replace a whole bounded date-window product; never merge partial partitions.

    Other windows/accounts require separate output roots. Readers pin CURRENT once.
    Failures leave CURRENT byte-identical; failed attempts/quarantine are separate.
    """
    root = Path(output_dir)
    raw = raw_bytes if raw_bytes is not None else canonical(bundle)
    if canonical(strict_json(raw)) != canonical(bundle):
        raise ContractError("raw_mismatch", "Bronze bytes differ from parsed dataset")
    with writer_lock(root):
        previous_bytes = ((root / "CURRENT.json").read_bytes()
                          if (root / "CURRENT.json").exists() else None)
        attempt_id = uuid4().hex
        stage = root / "snapshots" / (".staging-" + attempt_id)
        stage.mkdir(parents=True)
        try:
            return _publish(bundle, root, stage, raw)
        except Exception as error:
            failure = error.as_dict() if isinstance(error, ContractError) else {
                "code": "publication_failed", "detail": str(error),
            }
            quarantine = root / "quarantine" / attempt_id
            quarantine.mkdir(parents=True)
            write_durable(quarantine / "input.json", raw)
            write_durable(quarantine / "failure.json", canonical({
                "status": "blocked", "attempt_id": attempt_id, "error": failure,
                "input_sha256": digest(raw), "failed_at": datetime.now(UTC).isoformat(),
                "previous_snapshot_preserved": (
                    ((root / "CURRENT.json").read_bytes()
                     if (root / "CURRENT.json").exists() else None) == previous_bytes
                ),
            }))
            raise
        finally:
            if stage.exists():
                try:
                    shutil.rmtree(stage)
                except OSError:
                    LOGGER.warning("staging cleanup failed: %s", stage, exc_info=True)


def _publish(bundle: dict, root: Path, stage: Path, raw: bytes) -> dict:
    tables, dq = normalized_tables(bundle)
    identity = {"pipeline_version": PIPELINE_VERSION, "contract_version": VERSION,
                "fixture_revision": bundle["fixture_revision"], "input_sha256": digest(raw),
                "source_sha256": source_hashes(), "duckdb_version": duckdb.__version__,
                "metrics_sha256": digest(canonical(METRICS))}
    snapshot_id = digest(canonical(identity))
    final = root / "snapshots" / snapshot_id
    previous = None
    if (root / "CURRENT.json").exists():
        previous = json.loads((root / "CURRENT.json").read_bytes())
        previous_manifest, _ = read_published(root)
        expected_scope = (sorted(bundle["accounts"]), bundle["date_start"], bundle["date_end"])
        old_scope = (previous_manifest["accounts"], previous_manifest["date_start"],
                     previous_manifest["date_end"])
        if old_scope != expected_scope:
            raise ContractError("publication_scope", "use a separate root for another scope/window")
        if bundle["as_of"] < previous_manifest["as_of"]:
            raise ContractError("as_of_regression", "cannot publish an older data horizon")
    if final.exists():
        manifest = verify_snapshot(final)
    else:
        (stage / "bronze").mkdir()
        (stage / "silver").mkdir()
        (stage / "gold").mkdir()
        write_durable(stage / "bronze/input.json", raw)
        write_durable(stage / "metric_definitions.json", canonical(METRICS))
        write_durable(stage / "quality.json", canonical(dq))
        with duckdb.connect(":memory:") as connection:
            connection.execute("SET threads=1")
            connection.execute("SET memory_limit='512MB'")
            for name, records in tables.items():
                insert(connection, name, records)
            connection.execute(Path(__file__).with_name("transform.sql").read_text())
            for name in ARTIFACT_TABLES:
                layer = "gold" if name.startswith("gold_") else "silver"
                target = stage / layer / f"{name}.parquet"
                connection.execute(
                    f"COPY (SELECT * FROM {name} ORDER BY ALL) TO ? (FORMAT PARQUET)",
                    [str(target)],
                )
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
            campaign = rows(connection, "gold_campaign")
            account = rows(connection, "gold_account")
        write_durable(stage / "gold/metrics.json", canonical({"campaign": campaign,
                                                             "account": account}))
        artifacts = {str(p.relative_to(stage)): digest(p.read_bytes())
                     for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {
            "schema_version": "marketing-manifest.v1", "snapshot_id": snapshot_id,
            "identity": identity, "accounts": sorted(bundle["accounts"]),
            "date_start": bundle["date_start"], "date_end": bundle["date_end"],
            "as_of": bundle["as_of"], "quality": dq,
            "freshness": {
                "as_of": bundle["as_of"], "sources": tables["silver_coverage"],
                "oldest_through_utc": (
                    min(r["through_utc"] for r in tables["quality_daily"])
                    if all(r["through_utc"] is not None for r in tables["quality_daily"])
                    else None
                ),
                "policy": "compare both as_of and oldest source watermark to clock; 86400s",
            },
            "lineage": {"bronze": "bronze/input.json", "bronze_sha256": digest(raw),
                        "silver": [n for n in TABLES if n.startswith("silver_")],
                        "gold": ["gold_campaign", "gold_account"],
                        "sql_sha256": identity["source_sha256"]["transform.sql"],
                        "restatement": "full declared window; refunds on original order date"},
            "artifacts": artifacts,
        }
        write_durable(stage / "manifest.json", canonical(manifest))
        for directory in (stage / "bronze", stage / "silver", stage / "gold", stage):
            sync_directory(directory)
        os.replace(stage, final)
        sync_directory(final.parent)
    pointer = {"snapshot_id": snapshot_id,
               "manifest_sha256": digest((final / "manifest.json").read_bytes())}
    if pointer != previous:
        commit_pointer(root, pointer)
    return {"snapshot_id": snapshot_id, "snapshot_dir": str(final), "manifest": manifest}


def commit_pointer(root: Path, pointer: dict) -> None:
    """POSIX atomic visibility. Roll back a caught post-swap fsync failure.

    Process/power loss after rename may leave either complete snapshot current.
    Readers never see a staging directory. This is not a distributed transaction.
    """
    current = root / "CURRENT.json"
    pending = root / f".CURRENT-{uuid4().hex}.json"
    rollback = root / f".ROLLBACK-{uuid4().hex}.json"
    previous = current.read_bytes() if current.exists() else None
    swapped = False
    try:
        if previous is not None:
            write_durable(rollback, previous)
        write_durable(pending, canonical(pointer))
        os.replace(pending, current)
        swapped = True
        sync_directory(root)
    except OSError:
        if swapped:
            if previous is None:
                current.unlink()
            else:
                os.replace(rollback, current)
            sync_directory(root)
        raise
    finally:
        for path in (pending, rollback):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Cleanup cannot turn a durable publication into a reported failure,
                # or hide the original commit/rollback exception.
                LOGGER.warning("pointer cleanup failed: %s", path, exc_info=True)


def read_published(output_dir: Path | str) -> tuple[dict, dict]:
    root = Path(output_dir)
    pointer = json.loads((root / "CURRENT.json").read_bytes())
    snapshot_id = pointer["snapshot_id"]
    if (type(snapshot_id) is not str or len(snapshot_id) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_id)):
        raise ContractError("snapshot_integrity", "invalid current snapshot ID")
    path = root / "snapshots" / snapshot_id
    if digest((path / "manifest.json").read_bytes()) != pointer["manifest_sha256"]:
        raise ContractError("snapshot_integrity", "manifest hash mismatch")
    manifest = verify_snapshot(path)
    return manifest, json.loads((path / "gold/metrics.json").read_bytes())


def marketer_report(output_dir: Path | str, *, now: str | None = None) -> str:
    manifest, metrics = read_published(output_dir)
    clock = utc(now) if now else datetime.now(UTC)
    age = (clock - utc(manifest["as_of"])).total_seconds()
    watermark = manifest["freshness"]["oldest_through_utc"]
    source_age = (clock - utc(watermark)).total_seconds() if watermark else None
    freshness = "stale_or_clock_mismatch"
    if source_age is None:
        freshness = "unknown_source_coverage"
    elif 0 <= age <= 86400 and 0 <= source_age <= 86400:
        freshness = "fresh"
    failures = list((Path(output_dir) / "quarantine").glob("*/failure.json"))
    lines = ["# 합성 마케팅·커머스 보고서", f"snapshot: {manifest['snapshot_id']}",
             f"as_of(UTC): {manifest['as_of']} | freshness: {freshness}",
             f"clock(UTC): {clock.isoformat()} | fixture_clock_override: {now is not None}",
             f"oldest source watermark: {watermark or 'unknown'}",
             f"quality: {manifest['quality']['status']} | blocked attempts: {len(failures)}",
             f"Bronze SHA256: {manifest['lineage']['bronze_sha256']}",
             "단위: 정수 KRW. 합성 단일 캠페인 귀속; 인과관계·예산 변경 판단 불가.",
             "불완전/오래된 데이터의 관측 합계는 잠정치이며 확정 성과 판단은 unknown.",
             "", "계정 | KST 날짜 | 광고비 | 주문수 | 매출 | 환불 | 순귀속매출 | ROAS | 품질",
             "---|---|---:|---:|---:|---:|---:|---:|---"]
    for row in metrics["account"]:
        roas = row["roas"] if freshness == "fresh" else None
        ratio = "unknown" if roas is None else f"{roas:.4f}"
        quality = "complete" if row["complete"] else "incomplete"
        lines.append(f"{row['account_id']} | {row['business_date']} | {row['spend_krw']} | "
                     f"{row['order_count']} | {row['gross_sales_krw']} | {row['refund_krw']} | "
                     f"{row['net_attributed_sales_krw']} | {ratio} | {quality}")
    lines.extend([
        "", "캠페인별 관측치 (계정 합계와 별도 grain)",
        "계정 | 캠페인 | KST 날짜 | 광고비 | 순귀속매출 | ROAS | 품질",
        "---|---|---|---:|---:|---:|---",
    ])
    for row in metrics["campaign"]:
        roas = row["roas"] if freshness == "fresh" else None
        ratio = "unknown" if roas is None else f"{roas:.4f}"
        quality = "complete" if row["complete"] else "incomplete"
        lines.append(f"{row['account_id']} | {row['campaign_id']} | {row['business_date']} | "
                     f"{row['spend_krw']} | {row['net_attributed_sales_krw']} | "
                     f"{ratio} | {quality}")
    lines.extend(["", "원천 watermark (UTC, 원천 선언이며 독립적인 완전성 증명은 아님)"])
    for source in manifest["freshness"]["sources"]:
        lines.append(f"- {source['account_id']}/{source['business_date']}/{source['source']}: "
                     f"{source['through_utc']} complete={source['complete']}")
    return "\n".join(lines) + "\n"
