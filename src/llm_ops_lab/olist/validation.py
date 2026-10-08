"""Streaming validation for the pinned Olist raw intake."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path("data/olist/contract-v1.json")
DEFAULT_INTAKE_PATH = Path("data/olist/intake-v1.json")

TABLE_FILES = {
    "leads": (
        "work/olist-intake/raw/marketing-funnel-olist/olist_marketing_qualified_leads_dataset.csv"
    ),
    "deals": "work/olist-intake/raw/marketing-funnel-olist/olist_closed_deals_dataset.csv",
    "orders": "work/olist-intake/raw/brazilian-ecommerce/olist_orders_dataset.csv",
    "items": "work/olist-intake/raw/brazilian-ecommerce/olist_order_items_dataset.csv",
    "sellers": "work/olist-intake/raw/brazilian-ecommerce/olist_sellers_dataset.csv",
}


def file_sha256(path: Path) -> str:
    """Return a file's SHA256 without loading it all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(
    rule_id: str, status: str, observed: Any, expected: Any, severity: str = "error"
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "status": status,
        "observed": observed,
        "expected": expected,
        "severity": severity,
    }


def _empty_report(contract_version: str = "unknown") -> dict[str, Any]:
    return {
        "status": "failed",
        "dataset_id": None,
        "contract_version": contract_version,
        "files": [],
        "checks": [],
        "errors": [],
    }


def _timestamp_valid(value: str, kind: str) -> bool:
    try:
        if kind == "date":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                return False
            date.fromisoformat(value)
        else:
            # Olist source datetimes are naive wall-clock strings. Require the
            # documented representation so date-only and offset values cannot
            # be silently normalized by a downstream database.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value) is None:
                return False
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return True
    except (ValueError, OverflowError):
        return False


def _money_valid(value: str, scale: int, precision: int, minimum: Decimal) -> bool:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < minimum:
            return False
        quantum = Decimal(1).scaleb(-scale)
        integer_digits = max(amount.copy_abs().adjusted() + 1, 0) if amount else 0
        return integer_digits <= precision - scale and amount.quantize(quantum) == amount
    except (InvalidOperation, ValueError):
        return False


def _validate_sources(repo_root: Path, intake_path: Path | None = None) -> dict[str, Any]:
    """Validate pinned archive/CSV bytes and selected Olist raw semantics.

    Returns a JSON-serializable report for both passing and expected data/input
    failures. Error samples contain only file, row, column, and rule identifiers.
    """
    root = Path(repo_root)
    report = _empty_report()
    try:
        contract_bytes = (root / CONTRACT_PATH).read_bytes()
        contract = json.loads(contract_bytes)
        intake_file = Path(intake_path) if intake_path is not None else root / DEFAULT_INTAKE_PATH
        if not intake_file.is_absolute():
            intake_file = root / intake_file
        intake = json.loads(intake_file.read_text(encoding="utf-8"))
        contract_version = str(contract["contract_version"])
        report["contract_version"] = contract_version
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        # Exception text may contain local paths; expose only a stable code.
        report["errors"].append(
            {"rule_id": "intake_metadata", "file": None, "row": None, "column": None}
        )
        report["checks"].append(
            _check(
                "intake_metadata", "failed", "unreadable_or_invalid", "valid_contract_and_intake"
            )
        )
        return report

    errors: list[dict[str, Any]] = report["errors"]
    checks: list[dict[str, Any]] = report["checks"]
    max_samples = int(contract.get("limits", {}).get("max_error_samples", 50))

    def add_error(
        rule: str, file_name: str | None, row: int | None = None, column: str | None = None
    ) -> None:
        if len(errors) < max_samples:
            errors.append({"rule_id": rule, "file": file_name, "row": row, "column": column})

    file_meta = {entry.get("file"): entry for entry in intake.get("profile", {}).get("files", [])}
    archives = {
        entry.get("dataset", "").split("/")[-1]: entry for entry in intake.get("datasets", [])
    }
    expected_tables: dict[str, list[str]] = contract.get("tables", {})
    semantic_by_file = {
        spec["file"]: (name, spec) for name, spec in contract.get("semantic_tables", {}).items()
    }
    pinned_descriptors: list[dict[str, Any]] = []
    key_sets: dict[str, set[tuple[str, ...]]] = {}
    deal_references: list[tuple[str, str]] = []
    item_references: list[tuple[str, str]] = []
    missing_origins = 0
    contract_digest = hashlib.sha256(contract_bytes).hexdigest()
    rules_observed: dict[tuple[str, str], int] = {}

    def increment(rule: str, file_name: str) -> None:
        key = (rule, file_name)
        rules_observed[key] = rules_observed.get(key, 0) + 1
        add_error(rule, file_name)

    # Archive identity, exact member set, and CRC are checked independently of
    # extracted-file checks so a valid extraction cannot hide a damaged ZIP.
    for archive_name, archive_spec in sorted(contract.get("archives", {}).items()):
        metadata = archives.get(archive_name)
        if metadata is None:
            checks.append(_check("archive_present", "failed", False, True))
            add_error("archive_present", archive_name)
            continue
        rel_path = metadata.get("local_path")
        archive_path = root / rel_path if rel_path else None
        expected_members = sorted(archive_spec.get("members", []))
        observed_members: list[str] = []
        crc_ok = False
        members_match = False
        byte_count = None
        digest = None
        if archive_path and archive_path.is_file():
            byte_count = archive_path.stat().st_size
            try:
                digest = file_sha256(archive_path)
                with zipfile.ZipFile(archive_path) as zipped:
                    observed_members = sorted(zipped.namelist())
                    crc_ok = zipped.testzip() is None
                    members_match = observed_members == expected_members
                    if members_match:
                        for member in expected_members:
                            member_hash = hashlib.sha256()
                            with zipped.open(member) as stream:
                                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                    member_hash.update(chunk)
                            if member_hash.hexdigest() != file_meta.get(member, {}).get('sha256'):
                                members_match = False
            except (OSError, zipfile.BadZipFile, RuntimeError):
                crc_ok = False
        else:
            add_error("archive_missing", archive_name)
        for rule, observed, expected in (
            ("archive_bytes", byte_count, metadata.get("bytes")),
            ("archive_sha256", digest, metadata.get("sha256")),
            ("archive_members", observed_members, expected_members),
            ("archive_crc", crc_ok, True),
            ("archive_member_hashes", members_match, True),
        ):
            passed = observed == expected
            checks.append(
                _check(
                    f"{archive_name}.{rule}", "passed" if passed else "failed", observed, expected
                )
            )
            if not passed:
                add_error(rule, archive_name)
        pinned_descriptors.append(
            {
                "dataset": archive_name,
                "bytes": metadata.get("bytes"),
                "sha256": metadata.get("sha256"),
            }
        )

    for file_name, expected_header in sorted(expected_tables.items()):
        metadata = file_meta.get(file_name)
        summary: dict[str, Any] = {
            "file": file_name,
            "status": "failed",
            "rows": 0,
            "bytes": None,
            "sha256": None,
        }
        report["files"].append(summary)
        if metadata is None:
            checks.append(_check(f"{file_name}.metadata_present", "failed", False, True))
            add_error("csv_metadata_present", file_name)
            continue
        rel_path = metadata.get("local_path")
        source_path = root / rel_path if rel_path else None
        actual_bytes = source_path.stat().st_size if source_path and source_path.is_file() else None
        actual_digest = file_sha256(source_path) if actual_bytes is not None else None
        summary.update(bytes=actual_bytes, sha256=actual_digest)
        for rule, observed, expected in (
            ("bytes", actual_bytes, metadata.get("bytes")),
            ("sha256", actual_digest, metadata.get("sha256")),
        ):
            passed = observed == expected
            checks.append(
                _check(f"{file_name}.{rule}", "passed" if passed else "failed", observed, expected)
            )
            if not passed:
                add_error(f"csv_{rule}", file_name)
        pinned_descriptors.append(
            {"file": file_name, "bytes": metadata.get("bytes"), "sha256": metadata.get("sha256")}
        )
        if source_path is None or not source_path.is_file():
            add_error("csv_missing", file_name)
            continue

        row_count = 0
        header: list[str] | None = None
        shape_errors = 0
        semantic_name_spec = semantic_by_file.get(file_name)
        semantic_name, semantic = semantic_name_spec if semantic_name_spec else (None, None)
        seen_keys: set[tuple[str, ...]] = set()
        seen_unique_keys = [set() for _ in semantic.get("unique_keys", [])] if semantic else []
        # All rows are checked for CSV shape. Only selected keys and FK pairs
        # remain in memory; full source records are never materialized.
        try:
            with source_path.open("r", encoding="utf-8-sig", newline="") as source:
                reader = csv.reader(source, strict=True)
                header = next(reader, None)
                expected_exact = list(expected_header)
                header_pass = header == expected_exact
                checks.append(
                    _check(
                        f"{file_name}.header",
                        "passed" if header_pass else "failed",
                        header,
                        expected_exact,
                    )
                )
                if not header_pass:
                    add_error("csv_header", file_name)
                for row in reader:
                    row_count += 1
                    if len(row) != len(header or []):
                        shape_errors += 1
                        add_error("csv_row_shape", file_name, row_count + 1)
                        continue
                    if semantic is None:
                        continue
                    record = dict(zip(header or [], row))
                    if semantic_name == "deals":
                        deal_references.append(
                            (record.get("mql_id", ""), record.get("seller_id", ""))
                        )
                    elif semantic_name == "items":
                        item_references.append(
                            (record.get("order_id", ""), record.get("seller_id", ""))
                        )
                    elif semantic_name == "leads" and not record.get("origin", "").strip():
                        missing_origins += 1
                    key_values = tuple(record.get(column, "") for column in semantic.get("key", []))
                    if not key_values or any(not value.strip() for value in key_values):
                        increment("key_missing", file_name)
                    elif key_values in seen_keys:
                        increment("key_duplicate", file_name)
                    else:
                        seen_keys.add(key_values)
                    for unique_index, unique_columns in enumerate(semantic.get("unique_keys", [])):
                        unique_values = tuple(record.get(column, "") for column in unique_columns)
                        if all(value.strip() for value in unique_values):
                            if unique_values in seen_unique_keys[unique_index]:
                                increment("unique_duplicate", file_name)
                            else:
                                seen_unique_keys[unique_index].add(unique_values)
                    for column in semantic.get("required_nonempty", []):
                        if not record.get(column, "").strip():
                            increment("required_missing", file_name)
                    for column in semantic.get("positive_integers", []):
                        value = record.get(column, "")
                        if re.fullmatch(r"[1-9][0-9]*", value) is None:
                            increment("positive_integer_invalid", file_name)
                    for column, spec in semantic.get("timestamps", {}).items():
                        value = record.get(column, "")
                        if not value.strip() and spec.get("nullable"):
                            continue
                        if not value.strip() or not _timestamp_valid(value, spec["format"]):
                            increment("timestamp_invalid", file_name)
                    for column, spec in semantic.get("money", {}).items():
                        value = record.get(column, "")
                        if not value.strip() and spec.get("nullable"):
                            continue
                        if not value.strip() or not _money_valid(
                            value,
                            int(spec["scale"]),
                            int(spec["precision"]),
                            Decimal(spec["minimum"]),
                        ):
                            increment("money_invalid", file_name)
                if row_count != metadata.get("rows"):
                    add_error("csv_row_count", file_name)
                checks.append(
                    _check(
                        f"{file_name}.row_count",
                        "passed" if row_count == metadata.get("rows") else "failed",
                        row_count,
                        metadata.get("rows"),
                    )
                )
                checks.append(
                    _check(
                        f"{file_name}.row_shape",
                        "passed" if shape_errors == 0 else "failed",
                        shape_errors,
                        0,
                    )
                )
                summary.update(
                    rows=row_count,
                    status="passed"
                    if header_pass and shape_errors == 0 and row_count == metadata.get("rows")
                    else "failed",
                )
                if semantic is not None:
                    key_sets[semantic_name] = seen_keys
        except (OSError, UnicodeDecodeError, csv.Error, StopIteration):
            add_error("csv_parse", file_name)
            checks.append(
                _check(f"{file_name}.parse", "failed", "parse_error", "valid_utf8_rectangular_csv")
            )

    # Emit the same semantic check set on every run, including zero counts.
    for file_name in sorted(semantic_by_file):
        for rule in (
            "key_missing",
            "key_duplicate",
            "unique_duplicate",
            "required_missing",
            "positive_integer_invalid",
            "timestamp_invalid",
            "money_invalid",
        ):
            observed = rules_observed.get((rule, file_name), 0)
            checks.append(
                _check(
                    f"{file_name}.{rule}",
                    "passed" if observed == 0 else "failed",
                    observed,
                    0,
                )
            )
    # Cross-table checks use the preserved composite keys/IDs. Seller coverage
    # is intentionally warning-only because these are separate source samples.
    leads = key_sets.get("leads", set())
    orders = key_sets.get("orders", set())
    sellers = {key[0] for key in key_sets.get("sellers", set()) if key}
    missing_deal_mql = sum(1 for mql_id, _seller_id in deal_references if (mql_id,) not in leads)
    checks.append(
        _check("deals.mql_fk", "passed" if missing_deal_mql == 0 else "failed", missing_deal_mql, 0)
    )
    for _ in range(min(missing_deal_mql, max_samples - len(errors))):
        add_error("deal_mql_fk_missing", "olist_closed_deals_dataset.csv", column="mql_id")
    missing_item_orders = sum(
        1 for order_id, _seller_id in item_references if (order_id,) not in orders
    )
    missing_item_sellers = sum(
        1 for _order_id, seller_id in item_references if seller_id not in sellers
    )
    checks.append(
        _check(
            "items.order_fk",
            "passed" if missing_item_orders == 0 else "failed",
            missing_item_orders,
            0,
        )
    )
    checks.append(
        _check(
            "items.seller_fk",
            "passed" if missing_item_sellers == 0 else "failed",
            missing_item_sellers,
            0,
        )
    )
    if missing_item_orders:
        add_error("item_order_fk_missing", "olist_order_items_dataset.csv")
    if missing_item_sellers:
        add_error("item_seller_fk_missing", "olist_order_items_dataset.csv")
    missing_deal_sellers = sum(
        1 for _mql_id, seller_id in deal_references if seller_id not in sellers
    )
    checks.append(
        _check(
            "deals.commerce_seller_coverage",
            "passed" if missing_deal_sellers == 0 else "warning",
            missing_deal_sellers,
            0,
            "warning",
        )
    )
    checks.append(
        _check(
            "leads.origin_present",
            "passed" if missing_origins == 0 else "warning",
            missing_origins,
            0,
            "warning",
        )
    )

    descriptors = sorted(
        pinned_descriptors, key=lambda item: item.get("dataset", item.get("file", ""))
    )
    dataset_hash = hashlib.sha256(
        json.dumps(
            {
                "contract_version": contract_version,
                "contract_sha256": contract_digest,
                "sources": descriptors,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    report["dataset_id"] = f"olist-{dataset_hash}"
    for summary in report["files"]:
        prefix = f"{summary['file']}."
        if any(
            check["rule_id"].startswith(prefix)
            and check["status"] == "failed"
            and check["severity"] == "error"
            for check in checks
        ):
            summary["status"] = "failed"
    report["status"] = (
        "failed"
        if any(check["status"] == "failed" and check["severity"] == "error" for check in checks)
        else "passed"
    )
    return report


def validate_sources(repo_root: Path, intake_path: Path | None = None) -> dict[str, Any]:
    """Return a bounded failure report instead of raising for malformed input metadata."""
    try:
        return _validate_sources(repo_root, intake_path)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        InvalidOperation,
        OverflowError,
        zipfile.BadZipFile,
    ):
        report = _empty_report()
        report["errors"].append(
            {"rule_id": "validation_input", "file": None, "row": None, "column": None}
        )
        report["checks"].append(
            _check("validation_input", "failed", "malformed_or_unreadable", "valid_intake")
        )
        return report
