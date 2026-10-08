"""Additive CLI: python -m llm_ops_lab.marketing build|report|diagnose."""

import argparse
import json
import sys
from pathlib import Path

import duckdb

from llm_ops_lab.marketing.contracts import ContractError
from llm_ops_lab.marketing.pipeline import marketer_report, publish_dataset


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("json_key", "duplicate JSON object key")
        result[key] = value
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--input", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--output-dir", type=Path, required=True)
    report.add_argument("--now", help="UTC clock override for reproducible fixture reports")
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("--output-dir", type=Path, required=True)
    diagnose.add_argument("--snapshot-id", required=True)
    diagnose.add_argument("--account-id", required=True)
    diagnose.add_argument("--date-start", required=True)
    diagnose.add_argument("--date-end", required=True)
    diagnose.add_argument("--requests", type=Path, required=True, help="Bounded JSONL actions")
    diagnose.add_argument("--now", help="Host-only UTC clock override for fixture replay")
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            from llm_ops_lab.marketing.diagnostic_cli import run

            return run(args)
        if args.command == "build":
            if args.input.stat().st_size > 32 * 1024 * 1024:
                raise ContractError("input_limit", "input exceeds 32 MiB")
            raw = args.input.read_bytes()
            bundle = json.loads(raw, object_pairs_hook=unique_object)
            result = publish_dataset(bundle, args.output_dir, raw_bytes=raw)
            print(json.dumps({"snapshot_id": result["snapshot_id"],
                              "quality": result["manifest"]["quality"]["status"]}))
        else:
            print(marketer_report(args.output_dir, now=args.now), end="")
    except (ValueError, OSError, RuntimeError, KeyError, duckdb.Error) as error:
        detail = error.as_dict() if isinstance(error, ContractError) else {
            "code": "operation_failed", "detail": str(error),
        }
        print(json.dumps({"status": "error", "error": detail}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
