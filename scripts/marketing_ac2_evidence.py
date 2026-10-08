"""Produce small reviewable actual DuckDB/Parquet evidence in a disposable directory."""

import json
import sys
import tempfile
from pathlib import Path

from llm_ops_lab.marketing.contracts import ContractError
from llm_ops_lab.marketing.fixtures import scenario
from llm_ops_lab.marketing.pipeline import (
    marketer_report,
    process_spend_snapshots,
    publish_dataset,
    read_published,
)


def main():
    fixtures = Path("data/marketing/v1")
    base = json.loads((fixtures / "acceptance.json").read_text())
    expectations = json.loads((fixtures / "acceptance-expected.json").read_text())
    row = base["spend_snapshots"][0]
    duplicate = process_spend_snapshots([row, row])
    assert duplicate["silver_row_count"] == 1 and duplicate["gold"][0]["spend_krw"] == 10000
    evidence = {"schema_version": "marketing-ac2-evidence.v1", "operator": "assistant",
                "environment": "local isolated temporary checkout; actual DuckDB/Parquet",
                "learner_executed": False, "model_executed": False,
                "frozen_cases": json.loads((fixtures / "acceptance-freeze.json").read_text()),
                "exact_duplicate": duplicate, "scenarios": {}}
    with tempfile.TemporaryDirectory(prefix="marketing-products-") as temporary:
        root = Path(temporary)
        base_result = publish_dataset(base, root / "base")
        manifest, metrics = read_published(root / "base")
        evidence["reviewed_manifest"] = manifest
        evidence["reviewed_metrics"] = metrics
        evidence["marketer_report"] = marketer_report(root / "base", now=base["as_of"])
        for expected in expectations["base_account"]:
            actual = next(r for r in metrics["account"] if
                          (r["account_id"], r["business_date"]) ==
                          (expected["account_id"], expected["business_date"]))
            assert all(actual[k] == v for k, v in expected.items() if not k.startswith("roas_"))
        for name, expected in expectations["scenario_expectations"].items():
            output = root / name
            old = publish_dataset(base, output)
            before = (output / "CURRENT.json").read_bytes()
            changed = scenario(base, name)
            if name in {"late_order", "late_refund"}:
                initial = publish_dataset(changed, output)
                assert read_published(output)[1] == metrics
                assert initial["manifest"]["quality"]["future_delivery_rows"] == 1
                changed["as_of"] = expected["as_of"]
            if "blocked" in expected:
                try:
                    publish_dataset(changed, output)
                except ContractError as error:
                    assert error.code == expected["blocked"]
                    assert (output / "CURRENT.json").read_bytes() == before
                    assert read_published(output)[1] == metrics
                    evidence["scenarios"][name] = {
                        "error": error.as_dict(), "previous_manifest_preserved": True,
                        "previous_snapshot_id": old["snapshot_id"],
                    }
                else:
                    raise AssertionError("invalid input unexpectedly published")
                continue
            new = publish_dataset(changed, output)
            replay = publish_dataset(changed, output)
            assert replay == new
            actual_metrics = read_published(output)[1]
            if "account_id" in expected:
                target = next(r for r in actual_metrics["account"] if
                              (r["account_id"], r["business_date"]) ==
                              (expected["account_id"], expected["business_date"]))
                assert all(target[k] == v for k, v in expected.items() if k != "as_of")
            if name in {"duplicate", "observed_decline"}:
                assert actual_metrics == metrics
            evidence["scenarios"][name] = {
                "snapshot_id": new["snapshot_id"], "rerun_snapshot_identical": True,
                "quality": new["manifest"]["quality"]["status"],
                "account_metrics": actual_metrics["account"],
            }
        assert base_result["snapshot_id"] == manifest["snapshot_id"]
    if sys.argv[1:] == ["--check"]:
        stored = json.loads(Path("evidence/marketing-ac2-data-product.json").read_text())
        assert stored == evidence, "reviewed evidence differs from actual regenerated artifacts"
        verification = json.loads(Path("evidence/marketing-ac2-verification.json").read_text())
        assert all(len(json.dumps(item, ensure_ascii=False)) <= 2000
                   for item in verification["requirement_evidence"])
        for item in verification["requirement_evidence"]:
            for reference in item["implementation"]:
                assert Path(reference.split(":", 1)[0]).is_file(), reference
        report = Path("docs/marketing/acceptance-report.md").read_text()
        assert report == evidence["marketer_report"]
        print("AC2 reviewed evidence reproduced; requirement entries <=2000 characters")
    elif not sys.argv[1:]:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: marketing_ac2_evidence.py [--check]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
