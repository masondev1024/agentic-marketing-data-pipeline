"""AC6 external-copy observer; distinguish concurrent sibling edits from our writes.

The whole suite runs in a complete copy. Fingerprints cover every reviewed receipt
file, authority present in the copy, and model sources whose drift is documented.
Other ACs may edit their own implementation/tests without invalidating this check.
"""

import json
import sys

sys.dont_write_bytecode = True
import verify_marketing_ac2 as observer  # noqa: E402

original = observer.fingerprints


def fingerprints():
    receipt = json.loads((observer.ROOT / "evidence/marketing-ac6-docs.json").read_text())
    watched = set(receipt["reviewed_sha256"]) | set(receipt["model_source_comparison"])
    watched.update({"evidence/marketing-ac6-docs.json", "scripts/verify_marketing_ac6.py",
                    "AGENTS.md", "pyproject.toml", "uv.lock"})
    return {name: sha for name, sha in original().items() if name in watched}


if __name__ == "__main__":
    observer.fingerprints = fingerprints
    raise SystemExit(observer.main())
