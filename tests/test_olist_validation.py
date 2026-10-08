import csv
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from llm_ops_lab.olist.validation import validate_sources

REPO = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_fixture(
    tmp_path: Path,
    *,
    price: str = "10.25",
    item_order: str = "order-1",
    deal_mql: str = "lead-1",
    item_number: str = "1",
    order_timestamp: str = "2018-02-04 00:00:00",
    declared_revenue: str = "0.00",
    duplicate_deal_seller: bool = False,
) -> Path:
    contract = json.loads((REPO / "data/olist/contract-v1.json").read_text())
    (tmp_path / "data/olist").mkdir(parents=True)
    shutil.copy(REPO / "data/olist/contract-v1.json", tmp_path / "data/olist/contract-v1.json")

    rows = {
        "olist_marketing_qualified_leads_dataset.csv": [
            {
                "mql_id": "lead-1",
                "first_contact_date": "2018-01-01",
                "landing_page_id": "page-1",
                "origin": "",
            }
        ],
        "olist_closed_deals_dataset.csv": [
            {
                "mql_id": deal_mql,
                "seller_id": "outside-sample",
                "sdr_id": "sdr-1",
                "sr_id": "sr-1",
                "won_date": "2018-02-03 04:05:06",
                "business_segment": "retail",
                "lead_type": "online",
                "lead_behaviour_profile": "",
                "has_company": "",
                "has_gtin": "",
                "average_stock": "",
                "business_type": "reseller",
                "declared_product_catalog_size": "",
                "declared_monthly_revenue": declared_revenue,
            }
        ],
        "olist_orders_dataset.csv": [
            {
                "order_id": "order-1",
                "customer_id": "customer-1",
                "order_status": "delivered",
                "order_purchase_timestamp": order_timestamp,
                "order_approved_at": "",
                "order_delivered_carrier_date": "",
                "order_delivered_customer_date": "",
                "order_estimated_delivery_date": "2018-02-10 00:00:00",
            }
        ],
        "olist_order_items_dataset.csv": [
            {
                "order_id": item_order,
                "order_item_id": item_number,
                "product_id": "product-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2018-02-05 00:00:00",
                "price": price,
                "freight_value": "2.00",
            }
        ],
        "olist_sellers_dataset.csv": [
            {
                "seller_id": "seller-1",
                "seller_zip_code_prefix": "12345",
                "seller_city": "city",
                "seller_state": "SP",
            }
        ],
    }
    if duplicate_deal_seller:
        rows["olist_marketing_qualified_leads_dataset.csv"].append(
            {
                "mql_id": "lead-2",
                "first_contact_date": "2018-01-02",
                "landing_page_id": "page-2",
                "origin": "search",
            }
        )
        second_deal = rows["olist_closed_deals_dataset.csv"][0].copy()
        second_deal["mql_id"] = "lead-2"
        rows["olist_closed_deals_dataset.csv"].append(second_deal)
    file_paths: dict[str, Path] = {}
    files_metadata = []
    for file_name, columns in contract["tables"].items():
        dataset = (
            "marketing-funnel-olist"
            if file_name.startswith("olist_marketing") or file_name.startswith("olist_closed")
            else "brazilian-ecommerce"
        )
        relative = Path("work/olist-intake/raw") / dataset / file_name
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows.get(file_name, []))
        file_paths[file_name] = path
        files_metadata.append(
            {
                "file": file_name,
                "local_path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "rows": len(rows.get(file_name, [])),
                "columns": columns,
            }
        )

    datasets = []
    for dataset, archive_name in (
        ("brazilian-ecommerce", "brazilian-ecommerce.zip"),
        ("marketing-funnel-olist", "marketing-funnel-olist.zip"),
    ):
        archive_rel = Path("work/olist-intake") / archive_name
        archive_path = tmp_path / archive_rel
        members = contract["archives"][dataset]["members"]
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member in members:
                archive.write(file_paths[member], arcname=member)
        datasets.append(
            {
                "dataset": f"olistbr/{dataset}",
                "local_path": archive_rel.as_posix(),
                "bytes": archive_path.stat().st_size,
                "sha256": _sha256(archive_path),
            }
        )
    intake = {"datasets": datasets, "profile": {"files": files_metadata}}
    intake_path = tmp_path / "data/olist/intake-fixture.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    return intake_path


def _load_intake(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_intake(path: Path, intake: dict) -> None:
    path.write_text(json.dumps(intake), encoding="utf-8")


def _file_path(tmp_path: Path, intake_path: Path, file_name: str) -> Path:
    metadata = next(
        entry
        for entry in _load_intake(intake_path)["profile"]["files"]
        if entry["file"] == file_name
    )
    return tmp_path / metadata["local_path"]


def _refresh_csv_pin(tmp_path: Path, intake_path: Path, file_name: str) -> None:
    intake = _load_intake(intake_path)
    metadata = next(entry for entry in intake["profile"]["files"] if entry["file"] == file_name)
    path = tmp_path / metadata["local_path"]
    metadata["bytes"] = path.stat().st_size
    metadata["sha256"] = _sha256(path)
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.reader(source)
        metadata["columns"] = next(reader, [])
        metadata["rows"] = sum(1 for _row in reader)
    _save_intake(intake_path, intake)


def _rewrite_csv(
    tmp_path: Path,
    intake_path: Path,
    file_name: str,
    mutate,
    *,
    refresh_pin: bool = True,
) -> None:
    path = _file_path(tmp_path, intake_path, file_name)
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        columns = reader.fieldnames or []
        rows = list(reader)
    rows = mutate(columns, rows)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    if refresh_pin:
        _refresh_csv_pin(tmp_path, intake_path, file_name)


def test_validates_small_intake_and_reports_expected_warnings(tmp_path):
    intake = _make_fixture(tmp_path)

    first = validate_sources(tmp_path, intake)
    second = validate_sources(tmp_path, intake)

    assert first["status"] == "passed"
    assert first["dataset_id"] == second["dataset_id"]
    assert len(first["files"]) == 11
    assert (
        next(check for check in first["checks"] if check["rule_id"] == "leads.origin_present")[
            "status"
        ]
        == "warning"
    )
    assert (
        next(
            check
            for check in first["checks"]
            if check["rule_id"] == "deals.commerce_seller_coverage"
        )["observed"]
        == 1
    )
    assert first["errors"] == []


def test_rejects_invalid_money_and_missing_fks_without_echoing_values(tmp_path):
    intake = _make_fixture(
        tmp_path, price="10.251", item_order="private-order-id", deal_mql="private-mql-id"
    )

    report = validate_sources(tmp_path, intake)

    assert report["status"] == "failed"
    failed = {check["rule_id"] for check in report["checks"] if check["status"] == "failed"}
    assert "olist_order_items_dataset.csv.money_invalid" in failed
    assert "items.order_fk" in failed
    assert "deals.mql_fk" in failed
    assert "private-order-id" not in json.dumps(report)
    assert "private-mql-id" not in json.dumps(report)


def test_rejects_multiple_deals_for_one_seller_without_dropping_rows(tmp_path):
    intake = _make_fixture(tmp_path, duplicate_deal_seller=True)

    report = validate_sources(tmp_path, intake)

    assert report["status"] == "failed"
    check = next(
        check
        for check in report["checks"]
        if check["rule_id"] == "olist_closed_deals_dataset.csv.unique_duplicate"
    )
    assert check["observed"] == 1
    assert (
        next(item for item in report["files"] if item["file"] == "olist_closed_deals_dataset.csv")[
            "rows"
        ]
        == 2
    )


@pytest.mark.parametrize(
    ("price", "order_timestamp", "item_number", "declared_revenue", "expected_rule"),
    [
        ("NaN", "2018-02-04 00:00:00", "1", "0.00", "money_invalid"),
        ("-0.01", "2018-02-04 00:00:00", "1", "0.00", "money_invalid"),
        ("10.251", "2018-02-04 00:00:00", "1", "0.00", "money_invalid"),
        ("10000000000000000.00", "2018-02-04 00:00:00", "1", "0.00", "money_invalid"),
        ("10.25", "2018-02-04T00:00:00-03:00", "1", "0.00", "timestamp_invalid"),
        ("10.25", "2018-02-04", "1", "0.00", "timestamp_invalid"),
        ("10.25", "2018-02-04 00:00:00", "0", "0.00", "positive_integer_invalid"),
        ("10.25", "2018-02-04 00:00:00", "01", "0.00", "positive_integer_invalid"),
        ("10.25", "2018-02-04 00:00:00", "1", "NaN", "money_invalid"),
    ],
)
def test_rejects_invalid_types_and_money(
    tmp_path, price, order_timestamp, item_number, declared_revenue, expected_rule
):
    intake = _make_fixture(
        tmp_path,
        price=price,
        order_timestamp=order_timestamp,
        item_number=item_number,
        declared_revenue=declared_revenue,
    )

    report = validate_sources(tmp_path, intake)

    assert report["status"] == "failed"
    assert any(
        check["rule_id"].endswith(expected_rule) and check["status"] == "failed"
        for check in report["checks"]
    )


@pytest.mark.parametrize("missing_kind", ["csv", "zip"])
def test_reports_missing_source_files_as_failed(tmp_path, missing_kind):
    intake_path = _make_fixture(tmp_path)
    intake = _load_intake(intake_path)
    if missing_kind == "csv":
        _file_path(tmp_path, intake_path, "olist_sellers_dataset.csv").unlink()
    else:
        archive = next(
            entry
            for entry in intake["datasets"]
            if entry["dataset"].endswith("brazilian-ecommerce")
        )
        (tmp_path / archive["local_path"]).unlink()

    report = validate_sources(tmp_path, intake_path)

    assert report["status"] == "failed"
    assert report["errors"]


def test_reports_csv_hash_tampering(tmp_path):
    intake = _make_fixture(tmp_path)
    path = _file_path(tmp_path, intake, "olist_sellers_dataset.csv")
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    report = validate_sources(tmp_path, intake)

    assert "olist_sellers_dataset.csv.sha256" in {
        check["rule_id"] for check in report["checks"] if check["status"] == "failed"
    }


def test_rejects_archive_member_drift(tmp_path):
    intake_path = _make_fixture(tmp_path)
    intake = _load_intake(intake_path)
    archive_meta = next(
        entry for entry in intake["datasets"] if entry["dataset"].endswith("brazilian-ecommerce")
    )
    archive_path = tmp_path / archive_meta["local_path"]
    with zipfile.ZipFile(archive_path) as original:
        members = [(info, original.read(info.filename)) for info in original.infolist()]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as changed:
        for info, contents in members[:-1]:
            changed.writestr(info.filename, contents)
    archive_meta["bytes"] = archive_path.stat().st_size
    archive_meta["sha256"] = _sha256(archive_path)
    _save_intake(intake_path, intake)

    report = validate_sources(tmp_path, intake_path)

    assert "brazilian-ecommerce.archive_members" in {
        check["rule_id"] for check in report["checks"] if check["status"] == "failed"
    }


def test_rejects_header_and_duplicate_primary_key(tmp_path):
    intake = _make_fixture(tmp_path)
    path = _file_path(tmp_path, intake, "olist_marketing_qualified_leads_dataset.csv")
    path.write_text(
        path.read_text(encoding="utf-8").replace("landing_page_id", "unexpected_column", 1),
        encoding="utf-8",
    )
    _refresh_csv_pin(tmp_path, intake, "olist_marketing_qualified_leads_dataset.csv")
    _rewrite_csv(
        tmp_path,
        intake,
        "olist_marketing_qualified_leads_dataset.csv",
        lambda _columns, rows: rows + [rows[0].copy()],
    )

    report = validate_sources(tmp_path, intake)

    failed = {check["rule_id"] for check in report["checks"] if check["status"] == "failed"}
    assert "olist_marketing_qualified_leads_dataset.csv.header" in failed
    assert "olist_marketing_qualified_leads_dataset.csv.key_duplicate" in failed


def test_contract_content_change_changes_dataset_id(tmp_path):
    intake = _make_fixture(tmp_path)
    before = validate_sources(tmp_path, intake)["dataset_id"]
    contract_path = tmp_path / "data/olist/contract-v1.json"
    contract_path.write_bytes(contract_path.read_bytes() + b"\n")

    after = validate_sources(tmp_path, intake)["dataset_id"]

    assert before != after


def test_malformed_intake_metadata_returns_failed_report(tmp_path):
    intake = _make_fixture(tmp_path)
    intake.write_text(json.dumps({"datasets": None, "profile": []}), encoding="utf-8")

    report = validate_sources(tmp_path, intake)

    assert report["status"] == "failed"
    assert report["checks"][0]["rule_id"] == "validation_input"
