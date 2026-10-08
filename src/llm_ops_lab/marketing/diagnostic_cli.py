"""Host-only CLI wiring. Request data cannot choose a filesystem path or scope."""

import json
import sys

from llm_ops_lab.marketing.contracts import ContractError
from llm_ops_lab.marketing.diagnostics import (
    DiagnosticSession,
    Scope,
    Snapshot,
    read_bounded,
)


def run(args) -> int:
    scope = Scope(args.snapshot_id, args.account_id, args.date_start, args.date_end)
    session = DiagnosticSession(Snapshot.open(args.output_dir, scope), now=args.now)
    raw = read_bounded(args.requests, 32768).decode("utf-8")
    lines = raw.splitlines()
    if not lines or len(lines) > 8:
        raise ContractError("input_limit", "1..8 JSONL actions required")
    results = []
    for line in lines:
        result = session.dispatch(line)
        results.append(result)
        if result["status"] == "error":
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=sys.stderr)
            return 1
    print(json.dumps(results, ensure_ascii=False, allow_nan=False))
    return 0
