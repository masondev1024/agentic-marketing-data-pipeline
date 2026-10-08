"""AC4 observer: reuse external-copy validation without racing sibling-owned artifacts.

All project tests still run. Only fingerprint comparison is limited to AC4 ownership
and its stable source/data dependencies; unrelated concurrent edits are not our writes.
"""

import verify_marketing_ac2 as observer

WATCHED = (
    "src/llm_ops_lab/marketing/agent.py",
    "src/llm_ops_lab/marketing/offline_model.py",
    "src/llm_ops_lab/marketing/model_worker.py",
    "src/llm_ops_lab/marketing/diagnostics.py",
    "src/llm_ops_lab/marketing/diagnostic_worker.py",
    "src/llm_ops_lab/marketing/pipeline.py",
    "src/llm_ops_lab/marketing/contracts.py",
    "src/llm_ops_lab/marketing/fixtures.py",
    "src/llm_ops_lab/inference.py",
    "data/marketing/",
    "scripts/marketing_model_experiment.py",
    "scripts/run_marketing_model_isolated.py",
    "scripts/verify_marketing_ac4.py",
    "tests/test_marketing_agent.py",
    "docs/marketing/model-experiment.md",
    "evidence/marketing-ac4-experiment.json",
)
original = observer.fingerprints


def fingerprints():
    return {k: v for k, v in original().items() if k.startswith(WATCHED)}


if __name__ == "__main__":
    observer.fingerprints = fingerprints
    raise SystemExit(observer.main())
