"""Separate host CLI; does not change llm-lab or /v1/answers."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from llm_ops_lab.marketing.contracts import ContractError
from llm_ops_lab.marketing.diagnostics import Scope
from llm_ops_lab.marketing.graph import failure, run_graph


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "resume"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--decision", choices=("accept", "reject"))
    parser.add_argument(
        "--evaluation-now", help="Trusted synthetic replay clock, not live freshness"
    )
    args = parser.parse_args(argv)
    try:
        result = run_graph(
            args.root,
            args.database,
            args.thread,
            Scope(args.snapshot, args.account, args.start, args.end),
            operation=args.operation,
            decision=args.decision,
            now=args.evaluation_now,
        )
        failed = result["result"].get("status") == "error"
    except (ContractError, OSError, ValueError, sqlite3.Error) as error:
        result = failure(getattr(error, "code", "graph_error"), str(error))
        failed = True
    print(json.dumps(result, ensure_ascii=False))
    if failed:
        print(json.dumps(result.get("result", result).get("error")), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
