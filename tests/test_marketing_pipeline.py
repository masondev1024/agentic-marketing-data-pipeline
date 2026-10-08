"""AC 2: independently frozen arithmetic, actual Parquet, atomic failure and recovery."""

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest

from llm_ops_lab.marketing import pipeline
from llm_ops_lab.marketing.contracts import (
    ContractError,
    canonical,
    normalize_spend,
    normalize_standalone_spend,
)
from llm_ops_lab.marketing.fixtures import make_fixture, scenario
from llm_ops_lab.marketing.pipeline import (
    marketer_report,
    process_spend_snapshots,
    publish_dataset,
    read_published,
    writer_lock,
)

FIXTURES = Path(__file__).resolve().parents[1] / "data/marketing/v1"
EXPECTED = json.loads((FIXTURES / "acceptance-expected.json").read_text())


@pytest.fixture
def bundle():
    return json.loads((FIXTURES / "acceptance.json").read_text())


def parquet(result, name):
    layer = "gold" if name.startswith("gold_") else "silver"
    with duckdb.connect(":memory:") as db:
        query = db.execute("SELECT * FROM read_parquet(?) ORDER BY ALL",
                           [str(Path(result["snapshot_dir"]) / layer / f"{name}.parquet")])
        columns = [c[0] for c in query.description]
        return [dict(zip(columns, r, strict=True)) for r in query.fetchall()]


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_frozen_inputs_and_expectations_are_unchanged():
    frozen = json.loads((FIXTURES / "acceptance-freeze.json").read_text())
    for name, sha in frozen["sha256"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha
    for split in ("development", "acceptance"):
        assert make_fixture(split) == json.loads((FIXTURES / f"{split}.json").read_text())
    assert make_fixture("acceptance")["date_start"] != make_fixture()["date_start"]


def test_exact_10000_duplicate_entry_point_and_idempotence(bundle, tmp_path):
    row = bundle["spend_snapshots"][0]
    assert row["spend_krw"] == 10000
    answer = process_spend_snapshots([row, deepcopy(row)])
    assert answer["silver_row_count"] == 1
    assert normalize_spend([row, deepcopy(row)]) == [row]
    assert answer["gold"] == [{"account_id": "alpha", "campaign_id": "c1",
                               "business_date": "2026-09-20", "spend_krw": 10000}]
    assert process_spend_snapshots([row, deepcopy(row)]) == answer
    bundle["spend_snapshots"] = [row, deepcopy(row)]
    published = publish_dataset(bundle, tmp_path)
    assert parquet(published, "silver_spend") == [row]
    target = next(r for r in parquet(published, "gold_campaign") if
                  (r["account_id"], r["campaign_id"], r["business_date"]) ==
                  ("alpha", "c1", "2026-09-20"))
    assert target["spend_krw"] == 10000
    before = tree_bytes(tmp_path)
    assert publish_dataset(bundle, tmp_path)["snapshot_id"] == published["snapshot_id"]
    assert tree_bytes(tmp_path) == before


def test_full_frozen_arithmetic_parquet_and_lineage(bundle, tmp_path):
    result = publish_dataset(bundle, tmp_path)
    accounts = parquet(result, "gold_account")
    for expected in EXPECTED["base_account"]:
        actual = next(r for r in accounts if (r["account_id"], r["business_date"]) ==
                      (expected["account_id"], expected["business_date"]))
        for key, value in expected.items():
            if not key.startswith("roas_"):
                assert actual[key] == value
        assert actual["roas"] == pytest.approx(
            expected["roas_numerator"] / expected["roas_denominator"])
    columns = ("account_id", "campaign_id", "business_date", "spend_krw", "order_count",
               "gross_sales_krw", "refund_krw", "net_attributed_sales_krw")
    campaign = parquet(result, "gold_campaign")
    assert sorted([[r[c] for c in columns] for r in campaign]) == sorted(EXPECTED["base_campaign"])
    assert len(campaign) == 6
    manifest, metrics = read_published(tmp_path)
    assert metrics["account"] == accounts
    assert metrics["campaign"] == campaign
    assert manifest["quality"]["status"] == "complete"
    assert manifest["lineage"]["bronze_sha256"] == hashlib.sha256(canonical(bundle)).hexdigest()
    assert manifest["freshness"]["as_of"] == bundle["as_of"]
    assert manifest["identity"]["duckdb_version"] == "1.4.3"
    assert (Path(result["snapshot_dir"]) / "bronze/input.json").read_bytes() == canonical(bundle)
    # Same order IDs in beta and alpha must not cross-join/refund across accounts.
    assert next(r for r in accounts if r["account_id"] == "beta")["refund_krw"] == 0


@pytest.mark.parametrize("name", ["duplicate", "corrected_spend", "zero_spend", "incomplete",
                                  "observed_decline"])
def test_frozen_business_scenarios(bundle, tmp_path, name):
    base = publish_dataset(bundle, tmp_path)
    changed = publish_dataset(scenario(bundle, name), tmp_path)
    actual = parquet(changed, "gold_account")
    expected = EXPECTED["scenario_expectations"][name]
    if name in {"duplicate", "observed_decline"}:
        assert actual == parquet(base, "gold_account")
        assert len(parquet(changed, "silver_spend")) == 6
    else:
        row = next(r for r in actual if (r["account_id"], r["business_date"]) ==
                   (expected["account_id"], expected["business_date"]))
        assert {k: row[k] for k in expected} == expected
    if name == "observed_decline":
        alpha = [r for r in actual if r["account_id"] == "alpha"]
        assert alpha[0]["roas"] > alpha[1]["roas"]
    before = tree_bytes(tmp_path)
    publish_dataset(scenario(bundle, name), tmp_path)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("name", ["late_order", "late_refund"])
def test_frozen_backfill_asof_and_restatement(bundle, tmp_path, name):
    old = publish_dataset(bundle, tmp_path)
    late = scenario(bundle, name)
    excluded = publish_dataset(late, tmp_path)
    assert parquet(old, "gold_account") == parquet(excluded, "gold_account")
    assert excluded["manifest"]["quality"]["future_delivery_rows"] == 1
    expected = EXPECTED["scenario_expectations"][name]
    late["as_of"] = expected["as_of"]
    included = publish_dataset(late, tmp_path)
    target = next(r for r in parquet(included, "gold_account") if
                  (r["account_id"], r["business_date"]) ==
                  (expected["account_id"], expected["business_date"]))
    assert {k: target[k] for k in expected if k != "as_of"} == {
        k: v for k, v in expected.items() if k != "as_of"}
    before = tree_bytes(tmp_path)
    publish_dataset(late, tmp_path)
    assert tree_bytes(tmp_path) == before
    # Previous immutable snapshot remains queryable after backfill.
    assert parquet(old, "gold_account") == parquet(excluded, "gold_account")


@pytest.mark.parametrize("name", ["conflict", "missing_mapping", "schema_error"])
def test_block_quarantine_and_preserve_previous_bytes(bundle, tmp_path, name):
    old = publish_dataset(bundle, tmp_path)
    previous = (tmp_path / "CURRENT.json").read_bytes()
    old_files = tree_bytes(Path(old["snapshot_dir"]))
    with pytest.raises(ContractError) as error:
        publish_dataset(scenario(bundle, name), tmp_path)
    assert error.value.code == EXPECTED["scenario_expectations"][name]["blocked"]
    assert (tmp_path / "CURRENT.json").read_bytes() == previous
    assert tree_bytes(Path(old["snapshot_dir"])) == old_files
    assert read_published(tmp_path)[0]["snapshot_id"] == old["snapshot_id"]
    quarantined = list((tmp_path / "quarantine").glob("*/failure.json"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text())["error"]["code"] == error.value.code
    assert json.loads(quarantined[0].with_name("input.json").read_text()) == scenario(bundle, name)
    assert not list((tmp_path / "snapshots").glob(".staging-*"))


def test_snapshot_selection_not_sum_ingestion_or_revision_only(bundle):
    row = bundle["spend_snapshots"][0]
    earlier = row | {"snapshot_id": "earlier", "snapshot_at": "2026-09-20T12:00:00Z",
                     "revision": 9, "spend_krw": 6000, "ingested_at": "2026-09-22T00:00:00Z"}
    correction = row | {"revision": 2, "spend_krw": 12000}
    assert process_spend_snapshots([correction, row, earlier])["gold"][0]["spend_krw"] == 12000
    delivery = row | {"ingested_at": "2026-09-23T00:00:00Z"}
    assert normalize_spend([delivery, row]) == [row]
    with pytest.raises(ContractError, match="payload_conflict"):
        process_spend_snapshots([row, row | {"spend_krw": 1}, correction])
    with pytest.raises(ContractError, match="snapshot_key"):
        process_spend_snapshots([row, row | {"snapshot_id": "another"}])


@pytest.mark.parametrize("field,value", [("spend_krw", True), ("spend_krw", -1),
                                         ("spend_krw", 1.0), ("revision", 0),
                                         ("account_id", ""), ("business_date", "20260920"),
                                         ("event_time", "2026-09-20T15:01:00+09:00")])
def test_strict_schema_key_units_and_time(bundle, field, value):
    row = bundle["spend_snapshots"][0] | {field: value}
    with pytest.raises(ContractError):
        process_spend_snapshots([row])


def test_order_refund_revisions_and_fanout(bundle, tmp_path):
    bundle["orders"].append(bundle["orders"][0] | {"revision": 2, "gross_sales_krw": 35000})
    bundle["refunds"].append(bundle["refunds"][0] | {"revision": 2, "refund_krw": 6000})
    bundle["orders"] *= 3
    bundle["refunds"] *= 3
    bundle["campaigns"] *= 3
    published = publish_dataset(bundle, tmp_path)
    row = next(r for r in parquet(published, "gold_campaign") if
               (r["account_id"], r["campaign_id"], r["business_date"]) ==
               ("alpha", "c1", "2026-09-20"))
    assert (row["spend_krw"], row["order_count"], row["gross_sales_krw"], row["refund_krw"]) == (
        10000, 2, 55000, 8000)


@pytest.mark.parametrize("problem,code", [("refund_conflict", "payload_conflict"),
                                         ("order_conflict", "payload_conflict"),
                                         ("orphan", "orphan_refund"),
                                         ("over_refund", "over_refund"),
                                         ("dimension", "campaign_conflict"),
                                         ("scope", "scope"),
                                         ("coverage", "coverage_key"),
                                         ("premature_complete", "coverage_time")])
def test_data_quality_blocks_all_invalid_relations(bundle, tmp_path, problem, code):
    if problem == "refund_conflict":
        bundle["refunds"].append(bundle["refunds"][0] | {"refund_krw": 1})
    elif problem == "order_conflict":
        bundle["orders"].append(bundle["orders"][0] | {"gross_sales_krw": 1})
    elif problem == "orphan":
        bundle["refunds"][0]["order_id"] = "unknown"
    elif problem == "over_refund":
        bundle["refunds"][0]["refund_krw"] = 999999
    elif problem == "dimension":
        bundle["campaigns"].append(bundle["campaigns"][0] | {"name": "contradiction"})
    elif problem == "scope":
        bundle["orders"][0]["account_id"] = "unknown"
    elif problem == "coverage":
        bundle["coverage"].append(bundle["coverage"][0])
    elif problem == "premature_complete":
        bundle["coverage"][0]["through_utc"] = "2026-09-20T00:00:00Z"
    with pytest.raises(ContractError) as error:
        publish_dataset(bundle, tmp_path)
    assert error.value.code == code
    assert not (tmp_path / "CURRENT.json").exists()


def test_kst_midnight_and_empty_orders(bundle, tmp_path):
    bundle["orders"] = [bundle["orders"][0] | {"event_time": "2026-09-20T15:00:00Z",
                                               "ingested_at": "2026-09-20T15:01:00Z"}]
    bundle["refunds"] = []
    result = publish_dataset(bundle, tmp_path)
    assert parquet(result, "silver_orders")[0]["business_date"] == "2026-09-21"
    bundle["orders"] = []
    result = publish_dataset(bundle, tmp_path)
    assert all(r["gross_sales_krw"] == 0 for r in parquet(result, "gold_account"))


def test_report_unknown_stale_and_incomplete_and_failed_attempt(bundle, tmp_path):
    publish_dataset(bundle, tmp_path)
    report = marketer_report(tmp_path, now=bundle["as_of"])
    assert "53000" in report and "3.5333" in report and "quality: complete" in report
    assert "freshness: fresh" in report and "인과관계" in report and "Bronze SHA256" in report
    stale = marketer_report(tmp_path, now="2026-10-07T00:00:00Z")
    assert "stale_or_clock_mismatch" in stale and "unknown" in stale
    publish_dataset(scenario(bundle, "incomplete"), tmp_path)
    assert "quality: incomplete" in marketer_report(tmp_path, now=bundle["as_of"])
    with pytest.raises(ContractError):
        publish_dataset(scenario(bundle, "conflict"), tmp_path)
    assert "blocked attempts: 1" in marketer_report(tmp_path, now=bundle["as_of"])


def test_snapshot_rename_before_pointer_failure_preserves_current(bundle, tmp_path, monkeypatch):
    old = publish_dataset(bundle, tmp_path)
    before = (tmp_path / "CURRENT.json").read_bytes()
    original = pipeline.os.replace

    def fail_pointer(source, destination):
        if Path(destination).name == "CURRENT.json":
            raise OSError("injected disk failure before commit")
        original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline.os, "replace", fail_pointer)
        with pytest.raises(OSError, match="injected disk"):
            publish_dataset(scenario(bundle, "corrected_spend"), tmp_path)
    assert (tmp_path / "CURRENT.json").read_bytes() == before
    assert read_published(tmp_path)[0]["snapshot_id"] == old["snapshot_id"]
    # An orphan complete snapshot can be verified and safely reused after recovery.
    recovered = publish_dataset(scenario(bundle, "corrected_spend"), tmp_path)
    assert recovered["snapshot_id"] != old["snapshot_id"]


def test_failed_parquet_write_never_exposes_partial_snapshot(bundle, tmp_path, monkeypatch):
    old = publish_dataset(bundle, tmp_path)
    original = pipeline.insert

    def fail_after_one_table(connection, table, records):
        if table == "silver_spend":
            raise OSError("injected transform crash")
        original(connection, table, records)

    monkeypatch.setattr(pipeline, "insert", fail_after_one_table)
    with pytest.raises(OSError, match="transform crash"):
        publish_dataset(scenario(bundle, "corrected_spend"), tmp_path)
    assert read_published(tmp_path)[0]["snapshot_id"] == old["snapshot_id"]
    assert len(list((tmp_path / "snapshots").iterdir())) == 1


def test_scope_change_old_asof_and_corrupt_artifact_rejected(bundle, tmp_path):
    result = publish_dataset(bundle, tmp_path)
    changed = deepcopy(bundle)
    changed["date_start"] = "2026-09-21"
    with pytest.raises(ContractError, match="publication_scope"):
        publish_dataset(changed, tmp_path)
    changed = deepcopy(bundle)
    changed["as_of"] = "2026-09-23T11:00:00Z"
    for row in changed["coverage"]:
        row["through_utc"] = changed["as_of"]
    with pytest.raises(ContractError, match="as_of_regression"):
        publish_dataset(changed, tmp_path)
    (Path(result["snapshot_dir"]) / "gold/metrics.json").write_text("{}")
    with pytest.raises(ContractError, match="snapshot_integrity"):
        read_published(tmp_path)


def cli(*args):
    return subprocess.run([sys.executable, "-m", "llm_ops_lab.marketing", *map(str, args)],
                          capture_output=True, text=True, timeout=30, check=False)


def test_cli_new_process_replay_report_raw_bytes_and_structured_errors(bundle, tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(bundle, indent=3) + "\n")
    output = tmp_path / "product"
    first = cli("build", "--input", source, "--output-dir", output)
    assert first.returncode == 0, first.stderr
    assert not first.stderr
    before = tree_bytes(output)
    second = cli("build", "--input", source, "--output-dir", output)
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout and not second.stderr
    assert tree_bytes(output) == before
    report = cli("report", "--output-dir", output, "--now", bundle["as_of"])
    assert report.returncode == 0 and "53000" in report.stdout and not report.stderr
    manifest, _ = read_published(output)
    snapshot = output / "snapshots" / manifest["snapshot_id"]
    assert (snapshot / "bronze/input.json").read_bytes() == source.read_bytes()
    source.write_text(json.dumps(scenario(bundle, "conflict")))
    failed = cli("build", "--input", source, "--output-dir", output)
    assert failed.returncode != 0 and not failed.stdout
    assert json.loads(failed.stderr)["error"]["code"] == "payload_conflict"
    assert read_published(output)[0]["snapshot_id"] == manifest["snapshot_id"]
    source.write_text('{"schema_version": 1, "schema_version": 2}')
    failed = cli("build", "--input", source, "--output-dir", output)
    assert failed.returncode != 0
    assert json.loads(failed.stderr)["error"]["code"] == "json_key"


def test_single_writer_across_processes(bundle, tmp_path):
    source = tmp_path / "input.json"
    source.write_bytes(canonical(bundle))
    product = tmp_path / "product"
    with writer_lock(product):
        failed = cli("build", "--input", source, "--output-dir", product)
        assert failed.returncode != 0
        assert json.loads(failed.stderr)["error"]["code"] == "writer_busy"
    success = cli("build", "--input", source, "--output-dir", product)
    assert success.returncode == 0, success.stderr


def test_process_kill_before_pointer_commit_and_recovery(bundle, tmp_path):
    old = publish_dataset(bundle, tmp_path)
    source = tmp_path / "corrected.json"
    source.write_bytes(canonical(scenario(bundle, "corrected_spend")))
    code = '''
import json, os, sys
from pathlib import Path
from llm_ops_lab.marketing import pipeline
original = pipeline.os.replace
def crash(source, destination):
    if Path(destination).name == 'CURRENT.json':
        os._exit(73)
    original(source, destination)
pipeline.os.replace = crash
pipeline.publish_dataset(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
'''
    process = subprocess.run([sys.executable, "-c", code, str(source), str(tmp_path)],
                             capture_output=True, timeout=30, check=False)
    assert process.returncode == 73 and not process.stderr
    assert read_published(tmp_path)[0]["snapshot_id"] == old["snapshot_id"]
    recovery = cli("build", "--input", source, "--output-dir", tmp_path)
    assert recovery.returncode == 0, recovery.stderr
    assert read_published(tmp_path)[0]["snapshot_id"] != old["snapshot_id"]
    # Killing the lock owner does not strand a persistent lock-file based mutex.
    assert os.path.exists(tmp_path / ".writer.lock")


def test_post_pointer_sync_failure_restores_old_publication(bundle, tmp_path, monkeypatch):
    old = publish_dataset(bundle, tmp_path)
    before = (tmp_path / "CURRENT.json").read_bytes()
    original = pipeline.sync_directory
    failed = False

    def fail_once(path):
        nonlocal failed
        if path == tmp_path and (path / "CURRENT.json").read_bytes() != before and not failed:
            failed = True
            raise OSError("injected fsync failure after pointer swap")
        original(path)

    monkeypatch.setattr(pipeline, "sync_directory", fail_once)
    with pytest.raises(OSError, match="fsync failure"):
        publish_dataset(scenario(bundle, "corrected_spend"), tmp_path)
    assert failed
    assert (tmp_path / "CURRENT.json").read_bytes() == before
    assert read_published(tmp_path)[0]["snapshot_id"] == old["snapshot_id"]


def test_old_source_watermark_not_hidden_by_fresh_asof(bundle, tmp_path):
    bundle["as_of"] = "2026-09-25T12:00:00Z"
    publish_dataset(bundle, tmp_path)
    report = marketer_report(tmp_path, now=bundle["as_of"])
    assert "freshness: stale_or_clock_mismatch" in report
    assert "3.5333" not in report
    assert "oldest source watermark: 2026-09-23T12:00:00Z" in report


def test_missing_coverage_exposes_unknown_watermark(bundle, tmp_path):
    publish_dataset(scenario(bundle, "incomplete"), tmp_path)
    report = marketer_report(tmp_path, now=bundle["as_of"])
    assert "freshness: unknown_source_coverage" in report
    assert "oldest source watermark: unknown" in report


def test_machine_readable_contract_matches_executable_contract():
    from llm_ops_lab.marketing.contracts import SCHEMAS, VERSION
    from llm_ops_lab.marketing.pipeline import METRICS

    contract = json.loads((FIXTURES / "contract.json").read_text())
    assert contract["schema_version"] == VERSION
    assert contract["schemas"] == {table: {key: kind.__name__ for key, kind in fields.items()}
                                   for table, fields in SCHEMAS.items()}
    assert contract["metrics"] == METRICS


def test_publisher_does_not_trust_corrupt_current_manifest_hash(bundle, tmp_path):
    publish_dataset(bundle, tmp_path)
    current = tmp_path / "CURRENT.json"
    value = json.loads(current.read_text())
    value["manifest_sha256"] = "0" * 64
    current.write_bytes(canonical(value))
    before = current.read_bytes()
    with pytest.raises(ContractError, match="manifest hash mismatch"):
        publish_dataset(scenario(bundle, "corrected_spend"), tmp_path)
    assert current.read_bytes() == before


@pytest.fixture
def standalone_spend():
    # Standalone metadata must not be mistaken for the internal bundle schema.
    return {
        "schema_version": "spend.v1", "fixture_version": "acceptance.v1",
        "account_id": "account-a", "campaign_id": "campaign-a",
        "snapshot_id": "snapshot-a", "source_revision": 1,
        "business_date": "2026-10-05", "spend_krw": 10000,
        "currency": "KRW", "complete": True,
        "event_time": "2026-10-05T14:59:00Z",
        "ingestion_time": "2026-10-05T15:01:00Z",
        "as_of": "2026-10-05T15:00:00Z",
    }


def test_standalone_duplicate_contract_and_replay(standalone_spend):
    inputs = [standalone_spend, deepcopy(standalone_spend)]
    before = deepcopy(inputs)
    result = process_spend_snapshots(inputs)
    assert result["silver_row_count"] == 1
    assert normalize_standalone_spend(inputs) == [standalone_spend]
    assert result["gold"] == [{"account_id": "account-a", "campaign_id": "campaign-a",
                               "business_date": "2026-10-05", "spend_krw": 10000}]
    assert process_spend_snapshots(inputs) == result
    assert inputs == before


def test_spend_entry_point_exact_public_result(standalone_spend):
    row = standalone_spend | {"source": "synthetic"}
    expected = {
        "gold": [{"account_id": "account-a", "business_date": "2026-10-05",
                  "campaign_id": "campaign-a", "spend_krw": 10000}],
        "silver_row_count": 1,
    }
    for _ in range(2):
        assert process_spend_snapshots(spend_snapshots=[row, deepcopy(row)]) == expected


@pytest.mark.parametrize("field,value", [("spend_krw", 12000), ("complete", False),
                                          ("fixture_version", "acceptance.v2")])
def test_standalone_payload_conflict(standalone_spend, field, value):
    with pytest.raises(ContractError, match="payload_conflict"):
        process_spend_snapshots([standalone_spend, standalone_spend | {field: value}])


@pytest.mark.parametrize("patch", [
    {"currency": "USD"}, {"spend_krw": True}, {"source_revision": 0},
    {"schema_version": "spend.v2"}, {"complete": "true"}, {"unknown": 1},
    {"ingestion_time": "2026-10-05T14:00:00Z"},
])
def test_standalone_rejects_invalid_contract(standalone_spend, patch):
    with pytest.raises(ContractError):
        process_spend_snapshots([standalone_spend | patch])


def test_standalone_correction_delivery_and_scope(standalone_spend):
    row = standalone_spend
    correction = row | {"source_revision": 2, "spend_krw": 12000}
    later_delivery = row | {"ingestion_time": "2026-10-06T00:00:00Z"}
    assert normalize_standalone_spend([later_delivery, row]) == [row]
    other = row | {"account_id": "account-b", "spend_krw": 8000}
    result = process_spend_snapshots([row, correction, other])
    assert result["silver_row_count"] == 2
    assert [r["spend_krw"] for r in result["gold"]] == [12000, 8000]
    with pytest.raises(ContractError, match="payload_conflict"):
        process_spend_snapshots([row, row | {"spend_krw": 1}, correction])


@pytest.mark.parametrize("revision_name", ["revision", "snapshot_version", "source_version"])
def test_standalone_explicit_revision_names(standalone_spend, revision_name):
    row = dict(standalone_spend)
    row[revision_name] = row.pop("source_revision")
    row["source"] = "synthetic-ads"
    assert process_spend_snapshots([row, row])["silver_row_count"] == 1
    with pytest.raises(ContractError, match="schema"):
        process_spend_snapshots([row | {"source_revision": 1}])


def test_standalone_conflict_does_not_modify_published_product(bundle, standalone_spend, tmp_path):
    publish_dataset(bundle, tmp_path)
    before = tree_bytes(tmp_path)
    with pytest.raises(ContractError, match="payload_conflict"):
        process_spend_snapshots([standalone_spend, standalone_spend | {"spend_krw": 12000}])
    assert tree_bytes(tmp_path) == before
    assert read_published(tmp_path)[0]["quality"]["status"] == "complete"


def test_entry_point_empty_and_mixed_formats(bundle, standalone_spend):
    assert process_spend_snapshots([]) == {"silver_row_count": 0, "gold": []}
    for inputs in ([bundle["spend_snapshots"][0], standalone_spend],
                   [standalone_spend, bundle["spend_snapshots"][0]]):
        with pytest.raises(ContractError, match="schema"):
            process_spend_snapshots(inputs)
